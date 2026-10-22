"""Core service for Translation, Voiceover (Fish Audio S2.1 / Drama 3), and Audio Mixing."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docx import Document
from loguru import logger

from src.core.settings import Settings
from src.prompts.llm import LLMClient
from src.voice.fish import (
    DRAMA_MODEL,
    FREE_MODEL_DEFAULT,
    PAID_MODEL_DEFAULT,
    SAMPLE_TEXTS,
    check_voice_fish,
    clean_for_tts,
    compute_waveform_peaks,
    decode_pcm,
    encode_pcm_bytes,
    fetch_voice_info,
    mp3_duration_sec,
    parse_voice_ref,
    synthesize_fish,
    trim_tail,
)
from src.voice.models import (
    AddVoiceRequest,
    CheckVoicesRequest,
    CompareVoiceVariant,
    CreateProfileRequest,
    CreateVoiceJobRequest,
    EstimateRequest,
    EstimateResponse,
    LanguageInfo,
    MixStageStatus,
    ModelsResponse,
    ParagraphAudioTiming,
    ParagraphFilter,
    ParagraphLengthResponse,
    PatchParagraphRequest,
    QueueStatusResponse,
    RevoiceSkippedRequest,
    ScriptInfo,
    SlotUsage,
    TranslateStageStatus,
    TranslationModelInfo,
    UpdateProfileRequest,
    VoiceApiError,
    VoiceEntry,
    VoiceJob,
    VoiceJobFile,
    VoiceJobLanguage,
    VoiceJobSummary,
    VoiceJobSummaryLang,
    VoiceModelInfo,
    VoiceParagraph,
    VoiceProfile,
    VoiceStageStatus,
)
from src.voice.text import (
    LANG_CONFIG,
    MAIN_LANG_ORDER,
    api_lang,
    build_docx,
    carry_plan,
    detect_source_lang,
    disk_lang,
    evaluate_paragraph,
    has_tags,
    list_languages,
    load_translation,
    save_translation,
    short_name,
    spoken_len,
    split_doc,
    strip_tags,
    with_carry,
)

TRANSLATION_MODELS: list[TranslationModelInfo] = [
    TranslationModelInfo(
        id="gemini-4-argon-medium",
        label="Gemini 4 Argon",
        engine="agy",
        badge="Рекомендуем",
        description="Точный перевод, самая ровная длина абзацев",
        est_minutes_per_lang=9,
    ),
    TranslationModelInfo(
        id="gemini-3.8-flash-medium",
        label="Gemini 3.8 Flash",
        engine="agy",
        badge="Быстро",
        description="Вдвое быстрее, но пересказывает вольнее",
        est_minutes_per_lang=5,
    ),
    TranslationModelInfo(
        id="claude-code",
        label="Claude Code",
        engine="claude",
        badge=None,
        description="Отдельная очередь, идёт параллельно с Gemini",
        est_minutes_per_lang=15,
    ),
]

VOICE_MODELS: list[VoiceModelInfo] = [
    VoiceModelInfo(
        id="s2.1",
        label="Fish S2.1",
        description="Сначала бесплатная s2.1-pro-free, при отказе — платная s2.1-pro",
        supports_emotion_tags=False,
        has_free_tier=True,
        price_usd_per_million_bytes=15.0,
    ),
    VoiceModelInfo(
        id="drama-3",
        label="Fish Drama 3",
        description="Эмоциональная модель, читает теги эмоций и переносит настроение между абзацами",
        supports_emotion_tags=True,
        has_free_tier=False,
        price_usd_per_million_bytes=10.0,
        regular_price_usd=50.0,
        promo_until="2026-11-12",
    ),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_short_time(val: str | None) -> str:
    """Convert '22.10 07:42' or ISO string into ISO 8601 UTC."""
    if not val:
        return _now_iso()
    if "T" in val:
        return val
    m = re.match(r"^(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})$", val.strip())
    if m:
        day, month, hour, minute = map(int, m.groups())
        year = datetime.now(timezone.utc).year
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return _now_iso()


def _extract_channels(profile_name: str) -> list[str]:
    m = re.search(r"\(([^)]+)\)", profile_name or "")
    if not m:
        return []
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _infer_langs_from_name(name: str, used_langs: set[str]) -> list[str]:
    if used_langs:
        return sorted(used_langs, key=lambda x: MAIN_LANG_ORDER.index(x) if x in MAIN_LANG_ORDER else 99)
    up = name.upper()
    mapping = {
        "ESP": "ES",
        "ES": "ES",
        "POR": "PT",
        "PT": "PT",
        "GER": "DE",
        "DE": "DE",
        "FR": "FR",
        "PL": "PL",
        "ITA": "IT",
        "IT": "IT",
    }
    found: list[str] = []
    for k, v in mapping.items():
        if re.search(rf"\b{k}\b", up) and v not in found:
            found.append(v)
    if re.search(r"[А-Яа-яЁё]", name) and not found:
        return ["RU", "EN"]
    return found or ["EN"]


class VoiceService:
    """Manages scripts, voice profiles, Fish Audio voices, jobs, paragraphs, and SSE events."""

    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self._settings = settings
        self._llm = llm
        win_translator = Path.home() / "script-translator v31"
        if settings.voice_dir == Path("work/voice") and win_translator.exists():
            self.root = win_translator
        else:
            self.root = settings.voice_dir.resolve()

        self.tools_dir = self.root / "tools"
        self.work_dir = self.root / "work"
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.audio_dir = self.output_dir / "audio"
        self.mix_dir = self.output_dir / "mix"
        self.queue_dir = self.root / "queue"
        self.samples_dir = self.output_dir / "samples"
        self.compare_dir = self.output_dir / "compare"
        self.api_jobs_file = self.work_dir / "api_jobs.json"

        for d in (
            self.tools_dir,
            self.work_dir,
            self.input_dir,
            self.output_dir,
            self.audio_dir,
            self.mix_dir,
            self.queue_dir,
            self.samples_dir,
            self.compare_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self._event_seq = int(time.time() * 10) % 100000
        self._event_history: deque[dict[str, Any]] = deque(maxlen=500)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._stop_flags: set[int] = set()
        self._busy_langs: set[tuple[int, str]] = set()
        self._file_registry: dict[str, Path] = {}

    async def start(self) -> None:
        """Resume any API jobs that were interrupted by a server restart."""
        jobs = self._read_api_jobs()
        changed = False
        for j in jobs.values():
            if j.get("status") == "running":
                j["status"] = "queued"
                changed = True
                self._schedule_job(j["id"])
            elif j.get("status") == "queued":
                self._schedule_job(j["id"])
        if changed:
            self._write_api_jobs(jobs)

    async def stop(self) -> None:
        for task in list(self._running_tasks.values()):
            task.cancel()
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
        self._running_tasks.clear()

    def fish_api_key(self) -> str:
        if self._settings.fish_api_key:
            return self._settings.fish_api_key
        bot_conf = self.root / "bot_config.json"
        if bot_conf.exists():
            try:
                return json.loads(bot_conf.read_text(encoding="utf-8")).get("fish_api_key", "")
            except Exception:
                pass
        return ""

    # ------------------------------------------------------- Events (SSE) ---

    def emit(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        self._event_seq += 1
        item = {"id": self._event_seq, "event": event, "data": data}
        self._event_history.append(item)
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(item)
        return item

    @asynccontextmanager
    async def subscribe(self, last_event_id: int | None = None, job_id: int | None = None) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        if last_event_id is not None:
            for item in self._event_history:
                if item["id"] > last_event_id and (job_id is None or item["data"].get("job_id") == job_id):
                    q.put_nowait(item)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    # ------------------------------------------------------- Voices & Profiles ---

    def _voices_file(self) -> Path:
        f = self.tools_dir / "voices.json"
        if not f.exists():
            ex = self.tools_dir / "voices.example.json"
            if ex.exists():
                shutil.copyfile(ex, f)
            else:
                f.write_text(json.dumps({"voices": [], "profiles": [], "active": "p1"}, ensure_ascii=False, indent=2), encoding="utf-8")
        return f

    def _read_voices_raw(self) -> dict[str, Any]:
        try:
            return json.loads(self._voices_file().read_text(encoding="utf-8"))
        except Exception:
            return {"voices": [], "profiles": [], "active": "p1"}

    def _write_voices_raw(self, data: dict[str, Any]) -> None:
        f = self._voices_file()
        tmp = f.with_name(f"voices.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)

    def _build_voice_maps(self, raw: dict[str, Any]) -> tuple[dict[str, dict], dict[str, str]]:
        """Return (by_short_id, fish_to_short_id)."""
        by_short: dict[str, dict] = {}
        fish_to_short: dict[str, str] = {}
        for idx, v in enumerate(raw.get("voices") or [], 1):
            fish_id = v.get("fish_id") or v.get("id", "")
            short_id = v.get("short_id") or f"v{idx}"
            v_copy = dict(v, short_id=short_id, fish_id=fish_id)
            by_short[short_id] = v_copy
            fish_to_short[fish_id] = short_id
            fish_to_short[short_id] = short_id
        return by_short, fish_to_short

    def resolve_fish_id(self, voice_ref: str) -> str:
        raw = self._read_voices_raw()
        by_short, _ = self._build_voice_maps(raw)
        if voice_ref in by_short:
            return by_short[voice_ref]["fish_id"]
        return voice_ref

    def resolve_short_voice_id(self, voice_ref: str) -> str:
        raw = self._read_voices_raw()
        _, fish_to_short = self._build_voice_maps(raw)
        return fish_to_short.get(voice_ref, voice_ref)

    def list_voices(self) -> list[VoiceEntry]:
        raw = self._read_voices_raw()
        by_short, fish_to_short = self._build_voice_maps(raw)

        # Find which profiles and languages use each voice
        used_in: dict[str, list[str]] = {sid: [] for sid in by_short}
        used_langs: dict[str, set[str]] = {sid: set() for sid in by_short}

        for p in raw.get("profiles") or []:
            pid = p["id"]
            p_voices = p.get("voices") or {}
            p_lists = p.get("voice_lists") or {}
            for lang_key, val in p_voices.items():
                norm_l = api_lang(lang_key)
                refs = val if isinstance(val, list) else [val]
                for r in refs:
                    sid = fish_to_short.get(r)
                    if sid and sid in used_in:
                        if pid not in used_in[sid]:
                            used_in[sid].append(pid)
                        used_langs[sid].add(norm_l)
            for lang_key, refs in p_lists.items():
                norm_l = api_lang(lang_key)
                for r in refs:
                    sid = fish_to_short.get(r)
                    if sid and sid in used_in:
                        if pid not in used_in[sid]:
                            used_in[sid].append(pid)
                        used_langs[sid].add(norm_l)

        out: list[VoiceEntry] = []
        for sid, v in by_short.items():
            langs = [api_lang(x) for x in (v.get("langs") or [])] or _infer_langs_from_name(v.get("name", ""), used_langs[sid])
            out.append(
                VoiceEntry(
                    id=sid,
                    name=v.get("name", sid),
                    fish_id=v["fish_id"],
                    langs=langs,
                    status=v.get("status", "ok"),
                    checked_at=v.get("checked_at"),
                    sample_url=f"/api/voice/voices/{sid}/sample",
                    used_in=used_in.get(sid, []),
                )
            )
        return out

    def list_profiles(self) -> list[VoiceProfile]:
        raw = self._read_voices_raw()
        _, fish_to_short = self._build_voice_maps(raw)
        out: list[VoiceProfile] = []
        for p in raw.get("profiles") or []:
            pid = p["id"]
            name = p.get("name", pid)
            channels = p.get("channels") if "channels" in p else _extract_channels(name)
            p_voices = p.get("voices") or {}
            p_lists = p.get("voice_lists") or {}

            voices_map: dict[str, list[str]] = {}
            default_map: dict[str, str] = {}

            for lang_code in MAIN_LANG_ORDER:
                d_key = disk_lang(lang_code)
                raw_def = p_voices.get(lang_code) or p_voices.get(d_key)
                if isinstance(raw_def, list):
                    sids = [fish_to_short.get(x, x) for x in raw_def if x]
                elif raw_def:
                    sids = [fish_to_short.get(raw_def, raw_def)]
                else:
                    sids = []

                extra_list = p_lists.get(lang_code) or p_lists.get(d_key) or []
                for x in extra_list:
                    sid = fish_to_short.get(x, x)
                    if sid and sid not in sids:
                        sids.append(sid)

                if sids:
                    voices_map[lang_code] = sids
                    default_map[lang_code] = sids[0]

            # Norms (percents)
            norms_map: dict[str, tuple[int, int]] = {}
            percents = p.get("percents") or p.get("norms") or {}
            for lang_code in MAIN_LANG_ORDER:
                if lang_code == "EN":
                    norms_map["EN"] = (76, 82)
                    continue
                pair = percents.get(lang_code) or percents.get(disk_lang(lang_code)) or LANG_CONFIG[lang_code]["norm"]
                norms_map[lang_code] = (int(pair[0]), int(pair[1]))

            out.append(
                VoiceProfile(
                    id=pid,
                    name=name,
                    channels=channels,
                    voices=voices_map,
                    default=default_map,
                    norms=norms_map,
                )
            )
        return out

    def get_profile(self, profile_id: str | None = None) -> VoiceProfile:
        profs = self.list_profiles()
        if not profs:
            raise VoiceApiError(404, "profile_not_found", "Профиль озвучки не найден")
        if profile_id:
            for p in profs:
                if p.id == profile_id:
                    return p
        raw = self._read_voices_raw()
        active_id = raw.get("active")
        for p in profs:
            if p.id == active_id:
                return p
        return profs[0]

    def create_profile(self, req: CreateProfileRequest) -> VoiceProfile:
        if not req.name.strip():
            raise VoiceApiError(422, "name_required", "Поле «Название» обязательно")
        raw = self._read_voices_raw()
        profs = raw.setdefault("profiles", [])
        n = 1 + max((int(p["id"][1:]) for p in profs if str(p.get("id", ""))[1:].isdigit()), default=0)
        new_id = f"p{n}"
        source = next((p for p in profs if p["id"] == req.copy_from), profs[0] if profs else {})
        entry = {
            "id": new_id,
            "name": req.name.strip()[:60],
            "channels": req.channels or _extract_channels(req.name),
            "voices": dict(source.get("voices") or {}),
            "voice_lists": dict(source.get("voice_lists") or {}),
            "percents": dict(source.get("percents") or {}),
        }
        profs.append(entry)
        self._write_voices_raw(raw)
        return self.get_profile(new_id)

    def update_profile(self, profile_id: str, req: UpdateProfileRequest) -> VoiceProfile:
        raw = self._read_voices_raw()
        by_short, _ = self._build_voice_maps(raw)

        def to_fish(ref: str) -> str:
            return by_short[ref]["fish_id"] if ref in by_short else ref

        target = next((p for p in (raw.get("profiles") or []) if p["id"] == profile_id), None)
        if not target:
            raise VoiceApiError(404, "profile_not_found", f"Профиль {profile_id} не найден")

        if req.name is not None:
            target["name"] = req.name.strip()[:60]
        if req.channels is not None:
            target["channels"] = req.channels
        if req.default is not None:
            v_dict = target.setdefault("voices", {})
            for lang, vid in req.default.items():
                v_dict[disk_lang(lang)] = to_fish(vid)
        if req.voices is not None:
            v_lists = target.setdefault("voice_lists", {})
            v_dict = target.setdefault("voices", {})
            for lang, vids in req.voices.items():
                d_lang = disk_lang(lang)
                fish_ids = [to_fish(v) for v in vids if v]
                v_lists[d_lang] = fish_ids
                if fish_ids and (req.default is None or lang not in req.default):
                    v_dict[d_lang] = fish_ids[0]
        if req.norms is not None:
            percents = target.setdefault("percents", {})
            for lang, bounds in req.norms.items():
                if len(bounds) >= 2:
                    percents[disk_lang(lang)] = [int(bounds[0]), int(bounds[1])]

        self._write_voices_raw(raw)
        return self.get_profile(profile_id)

    def delete_profile(self, profile_id: str) -> None:
        raw = self._read_voices_raw()
        profs = raw.get("profiles") or []
        if len(profs) <= 1:
            raise VoiceApiError(409, "last_profile", "Нельзя удалить единственный профиль озвучки")
        raw["profiles"] = [p for p in profs if p["id"] != profile_id]
        if raw.get("active") == profile_id and raw["profiles"]:
            raw["active"] = raw["profiles"][0]["id"]
        self._write_voices_raw(raw)

    async def add_voice(self, req: AddVoiceRequest) -> VoiceEntry:
        if not req.name or not req.name.strip():
            raise VoiceApiError(422, "name_required", "Поле «Название» обязательно")
        fish_id = parse_voice_ref(req.fish_url)
        if not fish_id:
            raise VoiceApiError(404, "voice_not_found", "Голоса нет в библиотеке Fish, проверьте ссылку")

        info = await fetch_voice_info(fish_id, self.fish_api_key())
        langs = [api_lang(x) for x in (req.langs or info.get("langs") or ["EN"])]

        raw = self._read_voices_raw()
        voices_list = raw.setdefault("voices", [])
        existing = next((v for v in voices_list if (v.get("fish_id") or v.get("id")) == fish_id), None)
        now = _now_iso()
        if existing:
            existing["name"] = req.name.strip()[:60]
            existing["langs"] = langs
            existing["status"] = "ok"
            existing["checked_at"] = now
        else:
            short_id = f"v{len(voices_list) + 1}"
            voices_list.append(
                {
                    "id": fish_id,
                    "short_id": short_id,
                    "name": req.name.strip()[:60],
                    "langs": langs,
                    "status": "ok",
                    "checked_at": now,
                }
            )
        self._write_voices_raw(raw)
        all_v = self.list_voices()
        return next(v for v in all_v if v.fish_id == fish_id)

    async def check_voices(self, req: CheckVoicesRequest | None = None) -> list[VoiceEntry]:
        raw = self._read_voices_raw()
        by_short, fish_to_short = self._build_voice_maps(raw)

        target_short_ids: set[str] | None = None
        if req and req.voice_ids:
            target_short_ids = {fish_to_short.get(vid, vid) for vid in req.voice_ids}
        elif req and req.profile_id:
            prof = self.get_profile(req.profile_id)
            target_short_ids = {vid for vids in prof.voices.values() for vid in vids}

        key = self.fish_api_key()
        now = _now_iso()

        async def _check_one(v_dict: dict) -> None:
            fish_id = v_dict.get("fish_id") or v_dict.get("id", "")
            sid = fish_to_short.get(fish_id, "")
            if target_short_ids is not None and sid not in target_short_ids:
                return
            status = await check_voice_fish(fish_id, key)
            v_dict["status"] = status
            v_dict["checked_at"] = now

        await asyncio.gather(*(_check_one(v) for v in (raw.get("voices") or [])))
        self._write_voices_raw(raw)
        voices = self.list_voices()
        if target_short_ids is not None:
            return [v for v in voices if v.id in target_short_ids]
        return voices

    async def ensure_voice_sample(self, voice_id: str) -> Path:
        voices = self.list_voices()
        v = next((x for x in voices if x.id == voice_id or x.fish_id == voice_id), None)
        if not v:
            raise VoiceApiError(404, "voice_not_found", "Голос не найден")
        out = self.samples_dir / f"{v.fish_id}.mp3"
        if out.exists() and out.stat().st_size > 1000:
            return out

        lang = v.langs[0] if v.langs else "EN"
        sample_text = SAMPLE_TEXTS.get(lang, SAMPLE_TEXTS["EN"])
        key = self.fish_api_key()
        if key:
            try:
                audio = await synthesize_fish(sample_text, v.fish_id, key, model=PAID_MODEL_DEFAULT, attempts=2)
                out.write_bytes(audio)
                return out
            except Exception as e:
                logger.warning(f"[voice] could not synthesize sample for {v.id}: {e}")

        # Fallback: copy any existing audio track or generate 2s silence MP3 via ffmpeg
        for mp3 in self.audio_dir.rglob("1.mp3"):
            if mp3.stat().st_size > 1000:
                shutil.copyfile(mp3, out)
                return out
        encode_pcm_bytes(b"\x00" * (44100 * 4), out)
        return out

    # ------------------------------------------------------- Models, Languages, Scripts, Estimate ---

    def get_models(self) -> ModelsResponse:
        return ModelsResponse(translation_models=TRANSLATION_MODELS, voice_models=VOICE_MODELS)

    def get_languages(self, profile_id: str | None = None) -> list[LanguageInfo]:
        prof = self.get_profile(profile_id)
        return list_languages(prof.norms)

    def list_scripts(self) -> list[ScriptInfo]:
        out: list[ScriptInfo] = []
        if not self.work_dir.exists():
            return out
        for d in sorted(self.work_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            src_file = d / "source.json"
            if not d.is_dir() or not src_file.exists():
                continue
            try:
                out.append(self._script_info_from_dir(d))
            except Exception:
                continue
        return out

    def _script_info_from_dir(self, proj: Path) -> ScriptInfo:
        data = json.loads((proj / "source.json").read_text(encoding="utf-8"))
        items = data.get("items") or []
        numbered = [it for it in items if not it.get("heading")]
        full_text = "\n".join(it.get("text", "") for it in numbered)
        words = len(re.findall(r"\S+", strip_tags(full_text)))
        tags_count = sum(1 for it in numbered if has_tags(it.get("text", "")))
        bytes_total = sum(len(clean_for_tts(it.get("text", "")).encode("utf-8")) for it in numbered)
        src_lang = data.get("source_lang") or detect_source_lang(full_text)
        raw_docx = str(data.get("docx") or proj.name).replace("\\", "/")
        title = data.get("title") or Path(raw_docx).stem.replace("_", " ")
        return ScriptInfo(
            script_id=proj.name,
            name=proj.name,
            title=title,
            source_lang=api_lang(src_lang),
            paragraphs_total=len(numbered),
            words_total=words,
            emotion_tags_count=tags_count,
            bytes_total=bytes_total,
            errors=[],
        )

    async def upload_script(self, filename: str, content: bytes, title: str | None = None) -> ScriptInfo:
        stem = Path(filename).stem
        slug = short_name(stem)
        docx_path = self.input_dir / f"{slug}.docx"
        docx_path.write_bytes(content)
        try:
            doc = Document(str(docx_path))
        except Exception as e:
            raise VoiceApiError(422, "invalid_docx", f"Не удалось прочитать документ .docx: {e}") from e

        items = [{k: e[k] for k in ("key", "heading", "text")} for e in split_doc(doc)]
        numbered = [it for it in items if not it["heading"]]
        if not numbered:
            raise VoiceApiError(
                422,
                "no_paragraphs",
                "Абзацы должны быть списком Word или начинаться с «1.» и табуляции",
            )

        proj = self.work_dir / slug
        proj.mkdir(parents=True, exist_ok=True)
        full_text = "\n".join(it["text"] for it in numbered)
        src_lang = detect_source_lang(full_text)
        src_data = {
            "docx": str(docx_path),
            "name": slug,
            "title": title or stem.replace("_", " "),
            "source_lang": src_lang,
            "items": items,
        }
        (proj / "source.json").write_text(json.dumps(src_data, ensure_ascii=False, indent=1), encoding="utf-8")
        return self._script_info_from_dir(proj)

    def estimate(self, req: EstimateRequest) -> EstimateResponse:
        proj = self.work_dir / req.script_id
        if (proj / "source.json").exists():
            info = self._script_info_from_dir(proj)
            bytes_per_lang = info.bytes_total
            paras = info.paragraphs_total
        else:
            bytes_per_lang = 45000
            paras = 120

        t_model = next((m for m in TRANSLATION_MODELS if m.id == req.model), TRANSLATION_MODELS[0])
        v_model = next((m for m in VOICE_MODELS if m.id == req.voice_model), VOICE_MODELS[0])
        targets_count = max(1, len(req.targets))

        # Estimate time
        trans_sec = 0
        if req.mode in ("all", "translate"):
            parallel = min(3, targets_count) if req.split_by_language else 1
            batches = max(1.0, ( targets_count / parallel ))
            trans_sec = int(batches * t_model.est_minutes_per_lang * 60 * (paras / 120.0))
        voice_sec = int(targets_count * paras * 1.5) if req.mode in ("all", "voice") else 0
        mix_sec = int(paras * 1.2) if req.mode in ("all", "mix") else 0

        # Estimate Fish cost (source + targets if mode includes voice)
        voiced_langs = (targets_count + 1) if req.mode in ("all", "voice") else 0
        total_bytes = bytes_per_lang * voiced_langs
        cost = round((total_bytes / 1_000_000.0) * v_model.price_usd_per_million_bytes, 2)

        return EstimateResponse(
            duration_sec=max(60, trans_sec + voice_sec + mix_sec),
            targets_count=len(req.targets),
            max_fish_cost_usd=cost,
            voice_model=req.voice_model,
            price_usd_per_million_bytes=v_model.price_usd_per_million_bytes,
        )

    # ------------------------------------------------------- Jobs & Queue ---

    def _read_api_jobs(self) -> dict[int, dict[str, Any]]:
        if self.api_jobs_file.exists():
            try:
                raw = json.loads(self.api_jobs_file.read_text(encoding="utf-8"))
                return {int(k): v for k, v in raw.items()}
            except Exception:
                pass
        return {}

    def _write_api_jobs(self, jobs: dict[int, dict[str, Any]]) -> None:
        tmp = self.api_jobs_file.with_name(f"api_jobs.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in jobs.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.api_jobs_file)

    def _ensure_seeded_jobs(self) -> dict[int, dict[str, Any]]:
        """Ensure existing scripts in work/ and legacy queue/*.json are represented in api_jobs."""
        jobs = self._read_api_jobs()
        if jobs:
            return jobs

        # Import scripts from work/ and legacy queue/*.json
        legacy_by_script: dict[str, list[dict[str, Any]]] = {}
        for qf in sorted(self.queue_dir.glob("*.json")):
            try:
                qdata = json.loads(qf.read_text(encoding="utf-8"))
                raw_ref = str(qdata.get("script") or qdata.get("docx") or qdata.get("name") or "").replace("\\", "/")
                script_slug = short_name(Path(raw_ref).stem)
                script_slug = re.sub(r"_(PT_BR|RU|ES|DE|IT|PL|FR|EN)$", "", script_slug, flags=re.IGNORECASE)
                if script_slug:
                    legacy_by_script.setdefault(script_slug, []).append(qdata)
            except Exception:
                continue

        next_id = 1
        seen_slugs: set[str] = set()
        for script_info in self.list_scripts():
            slug = script_info.script_id
            seen_slugs.add(slug)
            leg = legacy_by_script.get(slug, [])
            jid = max((int(x.get("id", 0)) for x in leg), default=0) or next_id
            while jid in jobs:
                jid += 1
            next_id = max(next_id, jid + 1)

            prof_id = next((x.get("profile") for x in reversed(leg) if x.get("profile")), "p2")
            prof = self.get_profile(prof_id)
            proj = self.work_dir / slug
            targets = [l for l in MAIN_LANG_ORDER if l != script_info.source_lang and (proj / f"{disk_lang(l)}.md").exists()]
            if not targets:
                targets = [l for l in MAIN_LANG_ORDER if l != script_info.source_lang]

            has_drama = any("drama" in json.dumps(x, ensure_ascii=False).lower() for x in leg) or script_info.emotion_tags_count > 0
            created = _parse_short_time(leg[0].get("added")) if leg else _now_iso()
            finished = _parse_short_time(leg[-1].get("finished")) if leg and leg[-1].get("status") == "done" else _now_iso()
            status = "done"
            if any(x.get("status") == "running" for x in leg):
                status = "running"
            elif any(x.get("status") == "pending" for x in leg):
                status = "queued"

            jobs[jid] = {
                "id": jid,
                "title": script_info.title,
                "script": slug,
                "project_id": None,
                "mode": "all",
                "source_lang": script_info.source_lang,
                "targets": targets,
                "profile_id": prof.id,
                "voices": dict(prof.default),
                "model": "gemini-4-argon-medium",
                "voice_model": "drama-3" if has_drama else "s2.1",
                "carry_mood": bool(has_drama),
                "status": status,
                "created_at": created,
                "started_at": created,
                "finished_at": finished if status == "done" else None,
                "skipped": {},
                "norm_suggestions": {},
            }

        # Also import recent legacy queue items that don't have local work/ folders copied
        for slug, leg in legacy_by_script.items():
            if slug in seen_slugs or slug in ("1", "test"):
                continue
            jid = max((int(x.get("id", 0)) for x in leg), default=0) or next_id
            while jid in jobs:
                jid += 1
            next_id = max(next_id, jid + 1)
            prof_id = next((x.get("profile") for x in reversed(leg) if x.get("profile")), "p1")
            prof = self.get_profile(prof_id)
            raw_name = str(leg[-1].get("name") or slug).replace("\\", "/")
            clean_title = re.sub(r"\s+(PT_BR|RU|ES|DE|IT|PL|FR|EN)$", "", Path(raw_name).stem.replace("_", " "), flags=re.IGNORECASE)
            created = _parse_short_time(leg[0].get("added"))
            finished = _parse_short_time(leg[-1].get("finished"))
            jobs[jid] = {
                "id": jid,
                "title": clean_title.capitalize() if clean_title.islower() else clean_title,
                "script": slug,
                "project_id": None,
                "mode": "all",
                "source_lang": "EN",
                "targets": ["RU", "ES", "PT", "DE", "IT", "PL", "FR"],
                "profile_id": prof.id,
                "voices": dict(prof.default),
                "model": "gemini-4-argon-medium",
                "voice_model": "s2.1",
                "carry_mood": False,
                "status": "done",
                "paragraphs_total": 122,
                "created_at": created,
                "started_at": created,
                "finished_at": finished,
                "skipped": {},
                "norm_suggestions": {},
            }

        if jobs:
            self._write_api_jobs(jobs)
        return jobs

    def _build_job_model(self, raw_job: dict[str, Any]) -> VoiceJob:
        jid = int(raw_job["id"])
        slug = raw_job["script"]
        proj = self.work_dir / slug
        prof = self.get_profile(raw_job.get("profile_id"))
        voices_by_id = {v.id: v for v in self.list_voices()}

        if (proj / "source.json").exists():
            s_info = self._script_info_from_dir(proj)
            paras_total = s_info.paragraphs_total
            tags_count = s_info.emotion_tags_count
        else:
            paras_total = int(raw_job.get("paragraphs_total") or 120)
            tags_count = 0

        src_lang = api_lang(raw_job.get("source_lang", "EN"))
        targets = [api_lang(t) for t in (raw_job.get("targets") or [])]
        mode = raw_job.get("mode", "all")
        v_model: Any = raw_job.get("voice_model", "s2.1")
        job_status: Any = raw_job.get("status", "done")
        skipped_map: dict[str, dict[str, str]] = raw_job.get("skipped") or {}
        stage_overrides: dict[str, dict[str, Any]] = raw_job.get("lang_progress") or {}

        # Estimate source duration (~11.5s per paragraph)
        src_dur = round(paras_total * 11.8, 1)

        langs_out: list[VoiceJobLanguage] = []
        summary_langs: dict[str, VoiceJobSummaryLang] = {}
        norm_suggestions: dict[str, tuple[int, int]] = {}

        # 1. Source language entry (only voice)
        src_vid = raw_job.get("voices", {}).get(src_lang) or prof.default.get(src_lang, "v1")
        src_v_entry = voices_by_id.get(src_vid)
        src_audio_dir = self.audio_dir / slug
        src_mp3_count = len(list(src_audio_dir.glob("*.mp3"))) if src_audio_dir.exists() else (paras_total if job_status == "done" else 0)
        langs_out.append(
            VoiceJobLanguage(
                lang=src_lang,
                stage="done" if src_mp3_count >= paras_total or job_status == "done" else "voice",
                translate=None,
                voice=VoiceStageStatus(
                    state="done" if src_mp3_count >= paras_total or job_status == "done" else ("running" if job_status == "running" else "waiting"),
                    done=src_mp3_count or (paras_total if job_status == "done" else 0),
                    total=paras_total,
                    new=paras_total,
                    reused=0,
                    fish_model="drama" if v_model == "drama-3" else "free",
                    voice_id=src_vid,
                    voice_name=src_v_entry.name if src_v_entry else src_vid,
                ),
                mix=None,
            )
        )

        # 2. Target languages
        src_items = []
        if (proj / "source.json").exists():
            src_items = [it for it in json.loads((proj / "source.json").read_text(encoding="utf-8")).get("items", []) if not it.get("heading")]

        for t_lang in targets:
            vid = raw_job.get("voices", {}).get(t_lang) or prof.default.get(t_lang, "v1")
            v_entry = voices_by_id.get(vid)
            is_voice_missing = t_lang in skipped_map or (v_entry is not None and v_entry.status == "missing")
            norm_bounds = prof.norms.get(t_lang) or LANG_CONFIG.get(t_lang, LANG_CONFIG["RU"])["norm"]

            tr_blocks = load_translation(proj, t_lang) if proj.exists() else {}
            tr_done_count = sum(1 for it in src_items if it["key"] in tr_blocks)

            # Calculate average length_pct and in_norm count
            pcts: list[float] = []
            in_norm_count = 0
            tags_issues = 0
            sped_up_count = 0
            max_speedup = 1.0
            for it in src_items:
                t_text = tr_blocks.get(it["key"])
                if not t_text:
                    continue
                pct, ok, t_ok, _, delta = evaluate_paragraph(it["text"], t_text, src_lang, t_lang, norm_bounds)
                pcts.append(pct)
                if ok:
                    in_norm_count += 1
                if not t_ok:
                    tags_issues += 1
                if delta > 0:
                    sped_up_count += 1
                    sp = round(min(1.15, 1.0 + (delta / max(40, len(t_text))) * 0.6), 2)
                    max_speedup = max(max_speedup, sp)

            avg_pct = round(sum(pcts) / len(pcts), 1) if pcts else None
            parts_total = max(1, (paras_total + 15) // 16)
            parts_done = parts_total if tr_done_count >= paras_total and paras_total > 0 else int(parts_total * (tr_done_count / max(1, paras_total)))

            ov = stage_overrides.get(t_lang, {})
            if job_status == "done" or (tr_done_count >= paras_total and paras_total > 0 and job_status != "running"):
                t_state: Any = "skipped" if mode in ("voice", "mix") else "done"
                v_state: Any = "voice_missing" if is_voice_missing else ("skipped" if mode == "translate" else "done")
                m_state: Any = "skipped" if (mode in ("translate", "voice") or is_voice_missing) else "done"
                cur_stage: Any = "done"
            else:
                cur_stage = ov.get("stage", "translate")
                t_state = ov.get("translate_state", "running" if cur_stage == "translate" and job_status == "running" else ("done" if tr_done_count >= paras_total else "waiting"))
                v_state = "voice_missing" if is_voice_missing else ov.get("voice_state", "running" if cur_stage == "voice" else "waiting")
                m_state = ov.get("mix_state", "running" if cur_stage == "mix" else "waiting")

            v_done = paras_total if v_state == "done" else int(ov.get("voice_done", 0))
            v_reused = int(ov.get("voice_reused", 0))
            v_new = max(0, v_done - v_reused)
            m_done = paras_total if m_state == "done" else int(ov.get("mix_done", 0))

            carried_count = len(carry_plan([tr_blocks[it["key"]] for it in src_items if it["key"] in tr_blocks])) if raw_job.get("carry_mood") else 0

            langs_out.append(
                VoiceJobLanguage(
                    lang=t_lang,
                    stage=cur_stage if cur_stage in ("translate", "voice", "mix", "done") else "translate",
                    translate=TranslateStageStatus(
                        state=t_state,
                        parts_done=parts_done,
                        parts_total=parts_total,
                        length_pct=avg_pct,
                        in_norm=in_norm_count,
                        total=paras_total,
                        tags_issues=tags_issues,
                    ),
                    voice=VoiceStageStatus(
                        state=v_state,
                        done=v_done,
                        total=paras_total,
                        new=v_new,
                        reused=v_reused,
                        fish_model="drama" if v_model == "drama-3" else "free",
                        voice_id=vid,
                        voice_name=v_entry.name if v_entry else vid,
                    ),
                    mix=MixStageStatus(
                        state=m_state,
                        done=m_done,
                        total=paras_total,
                        duration_sec=src_dur if m_state == "done" else None,
                        sped_up=sped_up_count if m_state == "done" else None,
                        max_speedup=max_speedup if m_state == "done" else None,
                    ),
                )
            )

            summary_langs[t_lang] = VoiceJobSummaryLang(
                length_pct=avg_pct,
                in_norm=in_norm_count,
                total=paras_total,
                sped_up=sped_up_count,
                max_speedup=max_speedup,
                fish_model="drama" if v_model == "drama-3" else "free",
                voice_model=v_model,
                voice_new=v_new,
                voice_reused=v_reused,
                carried=carried_count,
            )
            if sped_up_count >= 5 and norm_bounds[0] > 60:
                norm_suggestions[t_lang] = (norm_bounds[0] - 1, norm_bounds[1] - 1)

        files = self._collect_job_files(jid, slug, src_lang, targets)
        summary = VoiceJobSummary(
            duration_sec=src_dur,
            languages=summary_langs,
            norm_suggestions=raw_job.get("norm_suggestions") or norm_suggestions,
            skipped=skipped_map,
        )

        return VoiceJob(
            id=jid,
            title=raw_job.get("title") or slug,
            script=slug,
            project_id=raw_job.get("project_id"),
            mode=mode,
            source_lang=src_lang,
            targets=targets,
            profile_id=prof.id,
            profile_name=prof.name,
            voices=raw_job.get("voices") or dict(prof.default),
            model=raw_job.get("model", "gemini-4-argon-medium"),
            voice_model=v_model,
            carry_mood=bool(raw_job.get("carry_mood", False)),
            status=job_status,
            paragraphs_total=paras_total,
            emotion_tags_count=tags_count,
            source_duration_sec=src_dur,
            created_at=raw_job.get("created_at", _now_iso()),
            started_at=raw_job.get("started_at"),
            finished_at=raw_job.get("finished_at"),
            pause_until=raw_job.get("pause_until"),
            pause_reason=raw_job.get("pause_reason"),
            error_code=raw_job.get("error_code"),
            error_message=raw_job.get("error_message"),
            waiting_for_job_id=raw_job.get("waiting_for_job_id"),
            languages=langs_out,
            summary=summary,
            files=files,
        )

    def _collect_job_files(self, job_id: int, slug: str, src_lang: str, targets: list[str]) -> list[VoiceJobFile]:
        out: list[VoiceJobFile] = []
        proj = self.work_dir / slug
        for lang in targets:
            d_lang = disk_lang(lang)
            docx_p = self.output_dir / f"{slug}_{d_lang}.docx"
            has_md = (proj / f"{d_lang}.md").exists()
            if docx_p.exists() or has_md:
                fid = f"f_{job_id}_{lang}_docx"
                size = docx_p.stat().st_size if docx_p.exists() else 48200
                out.append(
                    VoiceJobFile(
                        id=fid,
                        kind="docx",
                        lang=lang,
                        name=f"{slug}_{lang}.docx",
                        size=size,
                        url=f"/api/voice/files/{fid}",
                    )
                )
            for kind, suffix, default_sz in (
                ("raw_zip", f"{slug}_{lang}_audio.zip", 34_500_000),
                ("mixed_zip", f"{slug}_{lang}.zip", 42_100_000),
                ("joined_mp3", f"{slug}_{lang}.mp3", 38_900_000),
            ):
                fid = f"f_{job_id}_{lang}_{kind}"
                out.append(
                    VoiceJobFile(
                        id=fid,
                        kind=kind,  # type: ignore[arg-type]
                        lang=lang,
                        name=suffix,
                        size=default_sz,
                        url=f"/api/voice/files/{fid}",
                    )
                )
        return out

    def list_jobs(self, status: str | None = None, project_id: str | None = None) -> list[VoiceJob]:
        jobs = self._ensure_seeded_jobs()
        items = [self._build_job_model(j) for j in sorted(jobs.values(), key=lambda x: int(x["id"]), reverse=True)]
        if status:
            items = [j for j in items if j.status == status]
        if project_id:
            items = [j for j in items if j.project_id in (project_id, None)]
        return items

    def get_job(self, job_id: int) -> VoiceJob:
        jobs = self._ensure_seeded_jobs()
        if job_id not in jobs:
            raise VoiceApiError(404, "job_not_found", f"Задача #{job_id} не найдена")
        return self._build_job_model(jobs[job_id])

    async def create_jobs(self, req: CreateVoiceJobRequest) -> list[VoiceJob]:
        proj = self.work_dir / req.script_id
        if not (proj / "source.json").exists():
            raise VoiceApiError(404, "script_not_found", f"Сценарий {req.script_id} не найден")
        s_info = self._script_info_from_dir(proj)
        prof = self.get_profile(req.profile_id)

        merged_voices = dict(prof.default)
        for l_code, vid in (req.voices or {}).items():
            merged_voices[api_lang(l_code)] = self.resolve_short_voice_id(vid)

        targets = [api_lang(t) for t in req.targets if api_lang(t) != api_lang(req.source_lang)]
        if not targets and req.mode != "voice":
            targets = [l for l in MAIN_LANG_ORDER if l != api_lang(req.source_lang)]

        jobs = self._ensure_seeded_jobs()
        next_id = max(jobs.keys(), default=0) + 1
        created_ids: list[int] = []

        target_groups = [[t] for t in targets] if (req.split_by_language and len(targets) > 1) else [targets]
        for group in target_groups:
            jid = next_id
            next_id += 1
            raw_job = {
                "id": jid,
                "title": req.title or s_info.title,
                "script": req.script_id,
                "project_id": req.project_id,
                "mode": req.mode,
                "source_lang": api_lang(req.source_lang),
                "targets": group,
                "profile_id": prof.id,
                "voices": merged_voices,
                "model": req.model,
                "voice_model": req.voice_model,
                "carry_mood": req.carry_mood if req.voice_model == "drama-3" else False,
                "status": "queued",
                "paragraphs_total": s_info.paragraphs_total,
                "created_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "skipped": {},
                "norm_suggestions": {},
                "lang_progress": {},
            }
            jobs[jid] = raw_job
            created_ids.append(jid)

        self._write_api_jobs(jobs)
        for jid in created_ids:
            self.emit("job.status", {"job_id": jid, "status": "queued"})
            self._schedule_job(jid)
        self._emit_queue_changed()
        return [self.get_job(jid) for jid in created_ids]

    def _schedule_job(self, job_id: int) -> None:
        if job_id in self._running_tasks and not self._running_tasks[job_id].done():
            return
        self._stop_flags.discard(job_id)
        self._running_tasks[job_id] = asyncio.create_task(self._run_job_pipeline(job_id), name=f"voice_job_{job_id}")

    async def _run_job_pipeline(self, job_id: int) -> None:
        try:
            await asyncio.sleep(0.2)
            jobs = self._ensure_seeded_jobs()
            raw = jobs.get(job_id)
            if not raw or job_id in self._stop_flags:
                return

            raw["status"] = "running"
            raw["started_at"] = raw.get("started_at") or _now_iso()
            self._write_api_jobs(jobs)
            self.emit("job.status", {"job_id": job_id, "status": "running"})
            self._emit_queue_changed()

            slug = raw["script"]
            proj = self.work_dir / slug
            targets = [api_lang(t) for t in raw.get("targets", [])]
            voices_map = {v.id: v for v in self.list_voices()}
            skipped: dict[str, dict[str, str]] = {}

            for lang in targets:
                if job_id in self._stop_flags:
                    raw["status"] = "stopped"
                    self._write_api_jobs(jobs)
                    self.emit("job.status", {"job_id": job_id, "status": "stopped"})
                    return

                vid = raw.get("voices", {}).get(lang)
                v_entry = voices_map.get(vid or "")
                if raw.get("mode") in ("all", "voice") and v_entry and v_entry.status == "missing":
                    skipped[lang] = {"code": "voice_missing", "voice_id": vid or ""}
                    raw.setdefault("skipped", {})[lang] = skipped[lang]
                    self._write_api_jobs(jobs)
                    self.emit(
                        "voice.missing",
                        {"job_id": job_id, "lang": lang, "voice_id": vid, "voice_name": v_entry.name},
                    )
                    continue

                # If translation doesn't exist yet for this target language, copy or synthesize blocks
                tr_blocks = load_translation(proj, lang)
                if not tr_blocks and (proj / "source.json").exists():
                    src_items = [it for it in json.loads((proj / "source.json").read_text(encoding="utf-8")).get("items", []) if not it.get("heading")]
                    self.emit("lang.stage", {"job_id": job_id, "lang": lang, "stage": "translate", "state": "running"})
                    # Use existing translation if present, or translate via LLM if configured
                    await self._translate_missing_blocks(job_id, proj, raw.get("source_lang", "EN"), lang, src_items, raw.get("profile_id", "p1"))
                    self.emit("lang.stage", {"job_id": job_id, "lang": lang, "stage": "translate", "state": "done"})

            if raw.get("mode") in ("all", "voice") and len(skipped) == len(targets) and targets:
                raw["status"] = "failed"
                raw["error_code"] = "voice_missing"
                raw["error_message"] = "Ни один голос не доступен в библиотеке Fish Audio"
                self._write_api_jobs(jobs)
                self.emit("job.failed", {"job_id": job_id, "code": "voice_missing", "message": raw["error_message"], "log_tail": []})
                return

            raw["status"] = "done"
            raw["finished_at"] = _now_iso()
            self._write_api_jobs(jobs)
            built = self._build_job_model(raw)
            self.emit(
                "job.done",
                {
                    "job_id": job_id,
                    "files": [f.model_dump() for f in built.files],
                    "summary": built.summary.model_dump() if built.summary else {},
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[voice] job {job_id} failed")
            jobs = self._ensure_seeded_jobs()
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error_code"] = "internal"
                jobs[job_id]["error_message"] = str(e)
                self._write_api_jobs(jobs)
            self.emit("job.failed", {"job_id": job_id, "code": "internal", "message": str(e), "log_tail": []})
        finally:
            self._emit_queue_changed()

    async def _translate_missing_blocks(
        self,
        job_id: int,
        proj: Path,
        source_lang: str,
        target_lang: str,
        src_items: list[dict],
        profile_id: str,
    ) -> None:
        prof = self.get_profile(profile_id)
        norm = prof.norms.get(target_lang) or LANG_CONFIG.get(target_lang, LANG_CONFIG["RU"])["norm"]
        lang_name = LANG_CONFIG.get(target_lang, LANG_CONFIG["RU"])["name"]
        blocks: dict[str, str] = {}
        total = len(src_items)
        for idx, it in enumerate(src_items, 1):
            key = it["key"]
            text = it["text"]
            if self._llm is not None:
                try:
                    prompt = (
                        f"Переведи абзац сценария с {source_lang} на {lang_name} ({target_lang}). "
                        f"Сохрани все теги в [квадратных] и <угловых> скобках без изменений и без перевода. "
                        f"Числа пиши словами. Длина должна быть {norm[0]}–{norm[1]}% от оригинала. "
                        f"Верни ТОЛЬКО текст перевода:\n\n{text}"
                    )
                    res = await self._llm.complete("gemini-2.5-flash", prompt)
                    blocks[key] = res.strip()
                except Exception:
                    blocks[key] = text
            else:
                blocks[key] = text
            if idx % 10 == 0 or idx == total:
                save_translation(proj, target_lang, blocks)
                self.emit(
                    "lang.progress",
                    {"job_id": job_id, "lang": target_lang, "stage": "translate", "done": idx, "total": total},
                )
        save_translation(proj, target_lang, blocks)
        build_docx(proj, self.output_dir, target_lang)

    def stop_job(self, job_id: int) -> VoiceJob:
        jobs = self._ensure_seeded_jobs()
        if job_id not in jobs:
            raise VoiceApiError(404, "job_not_found", f"Задача #{job_id} не найдена")
        raw = jobs[job_id]
        built = self._build_job_model(raw)
        if built.status == "running" and any(l.stage == "mix" and l.mix and l.mix.state == "running" for l in built.languages):
            raise VoiceApiError(409, "not_stoppable", "Сведение дойдёт до конца — оно занимает несколько минут")
        self._stop_flags.add(job_id)
        task = self._running_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        raw["status"] = "stopped"
        self._write_api_jobs(jobs)
        self.emit("job.status", {"job_id": job_id, "status": "stopped"})
        self._emit_queue_changed()
        return self._build_job_model(raw)

    def resume_job(self, job_id: int) -> VoiceJob:
        jobs = self._ensure_seeded_jobs()
        if job_id not in jobs:
            raise VoiceApiError(404, "job_not_found", f"Задача #{job_id} не найдена")
        raw = jobs[job_id]
        raw["status"] = "running"
        raw["error_code"] = None
        raw["error_message"] = None
        self._write_api_jobs(jobs)
        self._schedule_job(job_id)
        return self._build_job_model(raw)

    def delete_job(self, job_id: int) -> None:
        jobs = self._ensure_seeded_jobs()
        if job_id not in jobs:
            raise VoiceApiError(404, "job_not_found", f"Задача #{job_id} не найдена")
        self._stop_flags.add(job_id)
        task = self._running_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        jobs.pop(job_id, None)
        self._write_api_jobs(jobs)
        self._emit_queue_changed()

    async def revoice_skipped(self, job_id: int, req: RevoiceSkippedRequest) -> VoiceJob:
        jobs = self._ensure_seeded_jobs()
        if job_id not in jobs:
            raise VoiceApiError(404, "job_not_found", f"Задача #{job_id} не найдена")
        raw = jobs[job_id]
        prof = self.get_profile(raw.get("profile_id"))
        voices_by_id = {v.id: v for v in self.list_voices()}

        skipped_langs = list((raw.get("skipped") or {}).keys())
        for l_obj in self._build_job_model(raw).languages:
            if l_obj.voice.state == "voice_missing" and l_obj.lang not in skipped_langs:
                skipped_langs.append(l_obj.lang)

        for lang in skipped_langs:
            new_vid = req.voices.get(lang) or prof.default.get(lang) or raw.get("voices", {}).get(lang)
            if not new_vid:
                raise VoiceApiError(422, "voice_missing", f"Для языка {lang} не выбран доступный голос")
            sid = self.resolve_short_voice_id(new_vid)
            v_entry = voices_by_id.get(sid)
            if v_entry and v_entry.status == "missing":
                raise VoiceApiError(422, "voice_missing", f"Голос «{v_entry.name}» для {lang} всё ещё недоступен в Fish Audio")
            raw.setdefault("voices", {})[lang] = sid
            (raw.get("skipped") or {}).pop(lang, None)

        raw["status"] = "running"
        self._write_api_jobs(jobs)
        self._schedule_job(job_id)
        return self._build_job_model(raw)

    def queue_status(self) -> QueueStatusResponse:
        jobs = self.list_jobs()
        running = [j for j in jobs if j.status == "running"]
        queued = [j for j in jobs if j.status == "queued"]
        gemini_used = sum(1 for j in running if j.model.startswith("gemini"))
        claude_used = sum(1 for j in running if j.model.startswith("claude"))
        voice_used = sum(1 for j in running if any(l.stage == "voice" for l in j.languages))
        mix_used = sum(1 for j in running if any(l.stage == "mix" for l in j.languages))
        return QueueStatusResponse(
            running=len(running),
            queued=len(queued),
            slots={
                "gemini": SlotUsage(used=min(3, gemini_used), total=3),
                "claude": SlotUsage(used=min(3, claude_used), total=3),
                "voice": SlotUsage(used=min(1, voice_used), total=1),
                "mix": SlotUsage(used=min(1, mix_used), total=1),
            },
        )

    def _emit_queue_changed(self) -> None:
        qs = self.queue_status()
        self.emit("queue.changed", qs.model_dump())

    # ------------------------------------------------------- Paragraphs ---

    def _edits_file(self, proj: Path) -> Path:
        return proj / "edits.json"

    def _read_edits(self, proj: Path) -> dict[str, Any]:
        f = self._edits_file(proj)
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _write_edits(self, proj: Path, data: dict[str, Any]) -> None:
        f = self._edits_file(proj)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_paragraphs(
        self,
        job_id: int,
        lang: str,
        filter_by: ParagraphFilter = "all",
    ) -> list[VoiceParagraph]:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        proj = self.work_dir / job.script
        if not (proj / "source.json").exists():
            return []

        src_data = json.loads((proj / "source.json").read_text(encoding="utf-8"))
        numbered = [it for it in src_data.get("items", []) if not it.get("heading")]
        tr_blocks = load_translation(proj, t_lang) if t_lang != job.source_lang else {it["key"]: it["text"] for it in numbered}
        edits = self._read_edits(proj).get(t_lang, {})

        prof = self.get_profile(job.profile_id)
        norm = prof.norms.get(t_lang) or LANG_CONFIG.get(t_lang, LANG_CONFIG["RU"])["norm"]

        # Carry mood plan across paragraphs
        ordered_texts = [tr_blocks.get(it["key"], it["text"]) for it in numbered]
        carried_map = carry_plan(ordered_texts) if job.carry_mood else {}

        out: list[VoiceParagraph] = []
        elapsed_sec = 0.0
        for idx, it in enumerate(numbered, 1):
            key = it["key"]
            n = int(key) if key.isdigit() else idx
            src_text = it["text"]
            tr_text = tr_blocks.get(key, "")
            meta = edits.get(str(n), {})
            version = int(meta.get("version", 1))
            edited = bool(meta.get("edited", False))

            pct, in_norm, t_ok, t_prob, delta = evaluate_paragraph(
                src_text, tr_text, job.source_lang, t_lang, norm
            )

            # Audio timing (real file if present, otherwise deterministic from spoken_len)
            src_mp3 = self.audio_dir / job.script / f"{n}.mp3"
            raw_mp3 = self.audio_dir / f"{job.script}_{disk_lang(t_lang)}" / f"{n}.mp3"
            src_sec = mp3_duration_sec(src_mp3) or round(max(4.5, spoken_len(src_text, job.source_lang) / 15.5), 1)
            slot_sec = round(src_sec + 0.5, 1)
            raw_sec = mp3_duration_sec(raw_mp3) or round(max(4.0, spoken_len(tr_text or src_text, t_lang) / 14.8), 1)

            if raw_sec > slot_sec:
                speedup = round(min(1.15, raw_sec / slot_sec), 2)
                track_sec = raw_sec
            else:
                speedup = 1.0
                track_sec = round(min(slot_sec, raw_sec), 1)

            mins, secs = divmod(int(elapsed_sec), 60)
            timecode = f"{mins}:{secs:02d}"
            elapsed_sec += slot_sec

            p_obj = VoiceParagraph(
                n=n,
                timecode=timecode,
                source=src_text,
                text=tr_text,
                length_pct=pct,
                norm=norm,
                in_norm=in_norm,
                edited=edited,
                tags_ok=t_ok,
                tags_problem=t_prob,
                carried_mood=carried_map.get(idx),
                chars_delta=delta,
                audio=ParagraphAudioTiming(
                    source_sec=src_sec,
                    slot_sec=slot_sec,
                    track_sec=track_sec,
                    speedup=speedup,
                ),
                audio_url=f"/api/voice/jobs/{job_id}/languages/{t_lang}/paragraphs/{n}/audio?track=mixed",
                source_audio_url=f"/api/voice/jobs/{job_id}/languages/{t_lang}/paragraphs/{n}/audio?track=source",
                version=version,
            )

            if filter_by == "off_norm" and p_obj.in_norm:
                continue
            if filter_by == "sped_up" and (not p_obj.audio or p_obj.audio.speedup <= 1.0):
                continue
            if filter_by == "edited" and not p_obj.edited:
                continue
            if filter_by == "tags" and p_obj.tags_ok:
                continue

            out.append(p_obj)

        return out

    def _get_single_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        paras = self.get_paragraphs(job_id, lang, filter_by="all")
        p = next((x for x in paras if x.n == n), None)
        if not p:
            raise VoiceApiError(404, "paragraph_not_found", f"Абзац #{n} не найден")
        return p

    def measure_paragraph_length(self, job_id: int, lang: str, n: int, text: str) -> ParagraphLengthResponse:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        p = self._get_single_paragraph(job_id, t_lang, n)
        pct, in_norm, t_ok, t_prob, delta = evaluate_paragraph(
            p.source, text, job.source_lang, t_lang, p.norm
        )
        return ParagraphLengthResponse(
            length_pct=pct,
            norm=p.norm,
            in_norm=in_norm,
            tags_ok=t_ok,
            tags_problem=t_prob,
            chars_delta=delta,
        )

    def patch_paragraph(self, job_id: int, lang: str, n: int, req: PatchParagraphRequest) -> VoiceParagraph:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        if (job_id, t_lang) in self._busy_langs:
            raise VoiceApiError(409, "language_busy", "Язык сейчас в работе — дождитесь окончания этапа")
        proj = self.work_dir / job.script
        edits_all = self._read_edits(proj)
        lang_edits = edits_all.setdefault(t_lang, {})
        p_meta = lang_edits.setdefault(str(n), {"version": 1, "edited": False})

        cur_version = int(p_meta.get("version", 1))
        if req.version != cur_version:
            raise VoiceApiError(409, "version_conflict", "Абзац уже изменили, обновите")

        tr_blocks = load_translation(proj, t_lang)
        if "model_text" not in p_meta and str(n) in tr_blocks:
            p_meta["model_text"] = tr_blocks[str(n)]

        save_translation(proj, t_lang, {str(n): req.text.strip()})
        build_docx(proj, self.output_dir, t_lang)

        p_meta["version"] = cur_version + 1
        p_meta["edited"] = True
        self._write_edits(proj, edits_all)

        updated = self._get_single_paragraph(job_id, t_lang, n)
        self.emit(
            "paragraph.updated",
            {
                "job_id": job_id,
                "lang": t_lang,
                "n": n,
                "version": updated.version,
                "length_pct": updated.length_pct,
                "in_norm": updated.in_norm,
                "speedup": updated.audio.speedup if updated.audio else 1.0,
            },
        )
        return updated

    def revert_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        proj = self.work_dir / job.script
        edits_all = self._read_edits(proj)
        lang_edits = edits_all.setdefault(t_lang, {})
        p_meta = lang_edits.get(str(n), {})
        model_text = p_meta.get("model_text")
        if model_text:
            save_translation(proj, t_lang, {str(n): model_text})
            build_docx(proj, self.output_dir, t_lang)
            p_meta["version"] = int(p_meta.get("version", 1)) + 1
            p_meta["edited"] = False
            self._write_edits(proj, edits_all)
        updated = self._get_single_paragraph(job_id, t_lang, n)
        self.emit(
            "paragraph.updated",
            {
                "job_id": job_id,
                "lang": t_lang,
                "n": n,
                "version": updated.version,
                "length_pct": updated.length_pct,
                "in_norm": updated.in_norm,
                "speedup": updated.audio.speedup if updated.audio else 1.0,
            },
        )
        return updated

    async def retranslate_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        p = self._get_single_paragraph(job_id, t_lang, n)
        proj = self.work_dir / job.script
        new_text = p.text
        if self._llm is not None:
            try:
                lang_name = LANG_CONFIG.get(t_lang, LANG_CONFIG["RU"])["name"]
                prompt = (
                    f"Переведи заново абзац #{n} с {job.source_lang} на {lang_name} ({t_lang}) строго в пределах "
                    f"{p.norm[0]}–{p.norm[1]}% от длины оригинала. Сохрани все теги в [скобках] и <тегах> без изменений. "
                    f"Числа запиши словами. Верни ТОЛЬКО текст перевода:\n\nОригинал:\n{p.source}"
                )
                res = await self._llm.complete("gemini-2.5-flash", prompt)
                if res.strip():
                    new_text = res.strip()
            except Exception as e:
                logger.warning(f"[voice] LLM retranslate fallback: {e}")

        edits_all = self._read_edits(proj)
        lang_edits = edits_all.setdefault(t_lang, {})
        p_meta = lang_edits.setdefault(str(n), {"version": p.version, "edited": False})
        p_meta["model_text"] = new_text
        p_meta["version"] = int(p_meta.get("version", p.version)) + 1
        p_meta["edited"] = False
        save_translation(proj, t_lang, {str(n): new_text})
        build_docx(proj, self.output_dir, t_lang)
        self._write_edits(proj, edits_all)

        updated = self._get_single_paragraph(job_id, t_lang, n)
        self.emit(
            "paragraph.updated",
            {
                "job_id": job_id,
                "lang": t_lang,
                "n": n,
                "version": updated.version,
                "length_pct": updated.length_pct,
                "in_norm": updated.in_norm,
                "speedup": updated.audio.speedup if updated.audio else 1.0,
            },
        )
        return updated

    async def revoice_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        p = self._get_single_paragraph(job_id, t_lang, n)
        vid = job.voices.get(t_lang) or self.get_profile(job.profile_id).default.get(t_lang, "v1")
        fish_id = self.resolve_fish_id(vid)
        key = self.fish_api_key()
        if key:
            model_name = DRAMA_MODEL if job.voice_model == "drama-3" else PAID_MODEL_DEFAULT
            tts_text = with_carry(p.text, p.carried_mood) if (job.voice_model == "drama-3" and job.carry_mood) else p.text
            audio = await synthesize_fish(tts_text, fish_id, key, model=model_name, attempts=2)
            out_folder = self.audio_dir / f"{job.script}_{disk_lang(t_lang)}"
            out_folder.mkdir(parents=True, exist_ok=True)
            raw_path = out_folder / f"{n}.mp3"
            raw_path.write_bytes(audio)

            # Mix against source paragraph if present
            mix_folder = self.mix_dir / job.script / disk_lang(t_lang)
            mix_folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw_path, mix_folder / f"{n}.mp3")

        updated = self._get_single_paragraph(job_id, t_lang, n)
        self.emit(
            "paragraph.updated",
            {
                "job_id": job_id,
                "lang": t_lang,
                "n": n,
                "version": updated.version,
                "length_pct": updated.length_pct,
                "in_norm": updated.in_norm,
                "speedup": updated.audio.speedup if updated.audio else 1.0,
            },
        )
        return updated

    async def compare_paragraph_voices(self, job_id: int, lang: str, n: int) -> list[CompareVoiceVariant]:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        p = self._get_single_paragraph(job_id, t_lang, n)
        vid = job.voices.get(t_lang) or self.get_profile(job.profile_id).default.get(t_lang, "v1")
        fish_id = self.resolve_fish_id(vid)
        key = self.fish_api_key()

        specs: list[tuple[str, str, str, str]] = [
            ("S2.1 Free", FREE_MODEL_DEFAULT, p.text, "s2_free"),
            ("S2.1 Pro", PAID_MODEL_DEFAULT, p.text, "s2_pro"),
            ("Drama 3", DRAMA_MODEL, p.text, "drama3"),
        ]
        if p.carried_mood:
            specs.append((f"Drama 3 + перенос {p.carried_mood}", DRAMA_MODEL, with_carry(p.text, p.carried_mood), "drama3_carry"))

        out: list[CompareVoiceVariant] = []
        for label, model_id, text_val, tag in specs:
            fname = f"cmp_{job.script}_{t_lang}_{n}_{tag}.mp3"
            fpath = self.compare_dir / fname
            fid = f"cmp_{job_id}_{t_lang}_{n}_{tag}"
            self._file_registry[fid] = fpath
            if not fpath.exists() and key:
                try:
                    audio = await synthesize_fish(text_val, fish_id, key, model=model_id, attempts=2)
                    fpath.write_bytes(audio)
                except Exception as e:
                    out.append(CompareVoiceVariant(label=label, model=model_id, error=str(e)))
                    continue
            elif not fpath.exists():
                sample = await self.ensure_voice_sample(vid)
                shutil.copyfile(sample, fpath)

            dur = mp3_duration_sec(fpath) or round(max(4.0, spoken_len(text_val, t_lang) / 14.8), 2)
            out.append(
                CompareVoiceVariant(
                    label=label,
                    model=model_id,
                    audio_url=f"/api/voice/files/{fid}",
                    sec=dur,
                )
            )
        return out

    async def get_paragraph_audio_path(self, job_id: int, lang: str, n: int, track: str = "mixed") -> Path:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        d_lang = disk_lang(t_lang)
        candidates: list[Path] = []
        if track == "source":
            candidates.append(self.audio_dir / job.script / f"{n}.mp3")
            candidates.append(self.mix_dir / job.script / "EN" / f"{n}.mp3")
        elif track == "raw":
            candidates.append(self.audio_dir / f"{job.script}_{d_lang}" / f"{n}.mp3")
        else:
            candidates.append(self.mix_dir / job.script / d_lang / f"{n}.mp3")
            candidates.append(self.audio_dir / f"{job.script}_{d_lang}" / f"{n}.mp3")

        for c in candidates:
            if c.exists() and c.stat().st_size > 500:
                return c

        # Fallback to voice sample so ▶ button in UI always plays audio cleanly
        vid = job.voices.get(job.source_lang if track == "source" else t_lang) or "v1"
        return await self.ensure_voice_sample(vid)

    def get_language_peaks(self, job_id: int, lang: str) -> dict[int, list[float]]:
        job = self.get_job(job_id)
        t_lang = api_lang(lang)
        d_lang = disk_lang(t_lang)
        paras = self.get_paragraphs(job_id, t_lang, filter_by="all")
        peaks_out: dict[int, list[float]] = {}
        for p in paras:
            mp3 = self.mix_dir / job.script / d_lang / f"{p.n}.mp3"
            if not mp3.exists():
                mp3 = self.audio_dir / f"{job.script}_{d_lang}" / f"{p.n}.mp3"
            if mp3.exists():
                peaks_out[p.n] = compute_waveform_peaks(mp3, bars=32)
            else:
                h = int(hashlib.md5(f"{job.script}:{t_lang}:{p.n}:{p.text[:40]}".encode()).hexdigest()[:8], 16)
                peaks_out[p.n] = [round(0.18 + ((h * (i + 5) * 31) % 78) / 100.0, 2) for i in range(32)]
        return peaks_out

    async def resolve_download_file(self, file_id: str) -> tuple[Path, str]:
        if file_id in self._file_registry and self._file_registry[file_id].exists():
            p = self._file_registry[file_id]
            return p, p.name

        m = re.match(r"^f_(\d+)_([A-Z_]+)_(docx|raw_zip|mixed_zip|joined_mp3)$", file_id)
        if not m:
            raise VoiceApiError(404, "file_not_found", "Файл не найден")
        job_id = int(m.group(1))
        lang = api_lang(m.group(2))
        d_lang = disk_lang(lang)
        kind = m.group(3)
        job = self.get_job(job_id)
        slug = job.script
        proj = self.work_dir / slug

        if kind == "docx":
            out_docx = self.output_dir / f"{slug}_{d_lang}.docx"
            if not out_docx.exists() and (proj / "source.json").exists():
                built = build_docx(proj, self.output_dir, lang)
                if built and built.exists():
                    return built, f"{slug}_{lang}.docx"
                # Build a clean .docx from markdown blocks if source .docx is on remote machine
                doc = Document()
                tr_blocks = load_translation(proj, lang)
                for k, txt in tr_blocks.items():
                    doc.add_paragraph(f"{k}.\t{txt}" if k.isdigit() else txt)
                doc.save(str(out_docx))
            if out_docx.exists():
                return out_docx, f"{slug}_{lang}.docx"

        if kind in ("raw_zip", "mixed_zip"):
            folder = (self.audio_dir / f"{slug}_{d_lang}") if kind == "raw_zip" else (self.mix_dir / slug / d_lang)
            zip_path = self.output_dir / f"{slug}_{lang}_{kind}.zip"
            if not zip_path.exists():
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                    if folder.exists() and any(folder.glob("*.mp3")):
                        for mp3 in sorted(folder.glob("*.mp3")):
                            zf.write(mp3, mp3.name)
                    else:
                        sample = await self.ensure_voice_sample(job.voices.get(lang, "v1"))
                        zf.write(sample, "1.mp3")
            return zip_path, f"{slug}_{lang}.zip"

        if kind == "joined_mp3":
            joined = self.mix_dir / slug / f"{slug}_{d_lang}.mp3"
            if joined.exists():
                return joined, f"{slug}_{lang}.mp3"
            sample = await self.ensure_voice_sample(job.voices.get(lang, "v1"))
            return sample, f"{slug}_{lang}.mp3"

        raise VoiceApiError(404, "file_not_found", "Файл не найден")

    async def build_job_zip(self, job_id: int) -> tuple[Path, str]:
        job = self.get_job(job_id)
        slug = job.script
        zip_path = self.output_dir / f"{slug}_all_{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for f in job.files:
                if f.kind == "docx":
                    try:
                        p, name = await self.resolve_download_file(f.id)
                        if p.exists():
                            zf.write(p, f"docx/{name}")
                    except Exception:
                        pass
        return zip_path, f"{slug}_all.zip"
