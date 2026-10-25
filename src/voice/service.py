"""Translation & voiceover for the web panel, run by fl-voice_translator.

Jobs go into the translator's own queue (`queue/`), and its worker (worker.py) runs them exactly like jobs sent
from its Telegram bot: translation with the chosen model and length fitting, Fish voiceover, mixing under the original.
This service turns the queue and the translator's files into the web API: a web job is a chain of translator
jobs (translate → voice → mix) linked by `root`; its id is the id of the first one.
"""

import asyncio
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

from loguru import logger

from src.core.settings import Settings
from src.voice.fish import (
    DRAMA_MODEL,
    FREE_MODEL_DEFAULT,
    PAID_MODEL_DEFAULT,
    SAMPLE_TEXTS,
    check_voice_fish,
    clean_for_tts,
    compute_waveform_peaks,
    fetch_voice_info,
    mp3_duration_sec,
    parse_voice_ref,
    synthesize_fish,
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
    carry_plan,
    disk_lang,
    evaluate_paragraph,
    has_tags,
    list_languages,
    load_translation,
    short_name,
    spoken_len,
    strip_tags,
    with_carry,
)
from src.voice.translator import Translator, find_root, parse_short_time

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

ALL_DISK_LANGS = ["EN", "RU", "ES", "PT_BR", "DE", "IT", "PL", "FR"]
LANG_SUFFIX_RE = re.compile(r"_(PT_BR|RU|ES|DE|IT|PL|FR|EN)$")
WATCH_EVERY = 1.5  # seconds between queue scans for SSE events
LIST_LIMIT = 200  # newest web jobs in the list (the queue history can be long)
RETRANSLATE_TIMEOUT = 25 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind(job: dict[str, Any]) -> str:
    return job.get("kind", "translate")


def _engine(job: dict[str, Any]) -> str:
    return (job.get("engine") or "claude").lower()


def _count_mp3(folder: Path) -> int:
    try:
        return sum(1 for p in folder.glob("*.mp3") if p.stem.isdigit() and p.stat().st_size > 500)
    except OSError:
        return 0


def _voiced_counts(result: str | None) -> tuple[int, int] | None:
    """'122 дорожек (…, озвучено сейчас 20, готовых взято 102)' → (new, reused)."""
    if not result:
        return None
    m = re.search(r"озвучено сейчас (\d+), готовых взято (\d+)", result)
    if m:
        return int(m.group(1)), int(m.group(2))
    if "все дорожки уже были готовы" in result:
        n = re.match(r"(\d+)", result)
        return (0, int(n.group(1))) if n else None
    return None


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
    """Scripts, voice profiles, Fish voices and jobs of fl-voice_translator; SSE events for the web panel."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.tr = Translator(find_root(settings.voice_dir), settings.voice_python)
        self.root = self.tr.root
        self.tools_dir = self.tr.tools_dir
        self.work_dir = self.tr.work_dir
        self.input_dir = self.tr.input_dir
        self.output_dir = self.tr.output_dir
        self.audio_dir = self.tr.audio_dir
        self.mix_dir = self.tr.mix_dir
        self.samples_dir = self.output_dir / "samples"
        self.compare_dir = self.output_dir / "compare"
        self.api_dir = self.work_dir / "_api"  # zip archives built for downloads

        self._event_seq = int(time.time() * 10) % 100000
        self._event_history: deque[dict[str, Any]] = deque(maxlen=500)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._file_registry: dict[str, Path] = {}
        self._watch_task: asyncio.Task | None = None
        self._seen: dict[int, tuple] = {}
        self._seen_queue: tuple = ()
        self._voices_cache: tuple | None = None
        self._built: dict[int, tuple[tuple, VoiceJob]] = {}

    async def start(self) -> None:
        if self.tr.available:
            logger.info(f"[voice] translator: {self.root} (python: {self.tr.python()})")
        else:
            logger.warning(f"[voice] translator not found in {self.root}: voice jobs are unavailable")
        self._watch_task = asyncio.create_task(self._watch(), name="voice_watch")

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

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

    async def _watch(self) -> None:
        """Scan the translator queue and turn changes into SSE events (the translator's worker changes it, not this process)."""
        first = True
        while True:
            try:
                self.scan(emit=not first)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[voice] queue scan failed")
            await asyncio.sleep(WATCH_EVERY)

    def scan(self, emit: bool = True) -> None:
        """Compare the queue with the last scan; emit job.status / job.done / job.failed / voice.missing."""
        chains = self._chains()
        seen: dict[int, tuple] = {}
        by_docx: dict[str, str] = {}
        for root, chain in chains.items():
            cur = chain[-1]
            sig = tuple((j["id"], j.get("status"), json.dumps(j.get("progress"), sort_keys=True),
                         json.dumps(j.get("results"), sort_keys=True, ensure_ascii=False), j.get("pause_until"),
                         json.dumps(j.get("skipped"), sort_keys=True)) for j in chain)
            seen[root] = sig
            if not emit or self._seen.get(root) == sig:
                continue
            job = self._build_cached(root, chain, by_docx)
            old = self._seen.get(root)
            self.emit("job.status", {"job_id": root, "status": job.status})
            for j in chain:
                prev = next((x for x in (old or ()) if x[0] == j["id"]), None)
                new_skips = set(j.get("skipped") or {}) - set(json.loads(prev[5] or "null") or {} if prev else {})
                for lang in new_skips:
                    vid = (j.get("skipped") or {})[lang]
                    self.emit("voice.missing", {"job_id": root, "lang": api_lang(lang), "voice_id": vid,
                                                "voice_name": self._voice_name(vid)})
            if old and job.status == "done" and not all(x[1] == "done" for x in old):
                self.emit("job.done", {"job_id": root, "files": [f.model_dump() for f in job.files],
                                       "summary": job.summary.model_dump() if job.summary else {}})
            if job.status == "failed" and (not old or old[-1][1] != "failed" or len(old) != len(sig)):
                self.emit("job.failed", {"job_id": root, "code": job.error_code or "translator_error",
                                         "message": job.error_message or "Ошибка задачи", "log_tail": self._log_tail(cur)})
        if emit:
            for gone in set(self._seen) - set(seen):
                self.emit("job.status", {"job_id": gone, "status": "deleted"})
        self._seen = seen
        q = tuple(sorted((j["id"], j.get("status")) for chain in chains.values() for j in chain))
        if emit and q != self._seen_queue:
            self._emit_queue_changed()
        self._seen_queue = q

    def _log_tail(self, job: dict[str, Any]) -> list[str]:
        if job.get("error"):
            return str(job["error"]).splitlines()[-12:]
        return [e.get("text", "") for e in self.tr.events(job["id"])[-5:]]

    def _voice_name(self, ref: str) -> str:
        sid = self.resolve_short_voice_id(ref)
        v = next((x for x in self.list_voices() if x.id == sid), None)
        return v.name if v else ref

    # ------------------------------------------------------- Voices & Profiles ---

    def _voices_file(self) -> Path:
        f = self.tools_dir / "voices.json"
        if not f.exists():
            self.tools_dir.mkdir(parents=True, exist_ok=True)
            ex = self.tools_dir / "voices.example.json"
            if ex.exists():
                shutil.copyfile(ex, f)
            else:
                f.write_text(json.dumps({"voices": [], "profiles": [], "active": "p1"}, ensure_ascii=False, indent=2), encoding="utf-8")
        return f

    def _read_voices_raw(self) -> dict[str, Any]:
        f = self._voices_file()
        try:
            key = (f.stat().st_mtime_ns, f.stat().st_size)
            if self._voices_cache and self._voices_cache[0] == key:
                return json.loads(self._voices_cache[1])
            text = f.read_text(encoding="utf-8")
            data = json.loads(text)
            self._voices_cache = (key, text)
            return data
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
            base = self._base_norms()
            for lang_code in MAIN_LANG_ORDER:
                pair = percents.get(lang_code) or percents.get(disk_lang(lang_code)) or base.get(lang_code)
                pair = pair or LANG_CONFIG[lang_code]["norm"]
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
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        out = self.samples_dir / f"{v.fish_id}.mp3"
        if out.exists() and out.stat().st_size > 1000:
            return out

        lang = v.langs[0] if v.langs else "EN"
        sample_text = SAMPLE_TEXTS.get(lang, SAMPLE_TEXTS["EN"])
        key = self.fish_api_key()
        if key:
            try:
                audio, _ = await self._synth_s2(sample_text, v.fish_id, key)
                out.write_bytes(audio)
                return out
            except VoiceApiError:
                raise
            except Exception as e:
                logger.warning(f"[voice] could not synthesize sample for {v.id}: {e}")
                raise VoiceApiError(502, "fish_error", f"Fish не озвучил пример голоса: {e}") from e
        raise VoiceApiError(503, "fish_key_missing", "Нет ключа Fish Audio (FISH_API_KEY или bot_config.json переводчика)")

    def _base_norms(self) -> dict[str, tuple[int, int]]:
        """Length ranges from the translator's config (percent of an English original)."""
        out: dict[str, tuple[int, int]] = {}
        for code, v in (self.tr.config().get("languages") or {}).items():
            try:
                out[api_lang(code)] = (int(v["min"]), int(v["max"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------- Models, Languages, Scripts, Estimate ---

    def get_models(self) -> ModelsResponse:
        return ModelsResponse(translation_models=TRANSLATION_MODELS, voice_models=VOICE_MODELS)

    def get_languages(self, profile_id: str | None = None) -> list[LanguageInfo]:
        prof = self.get_profile(profile_id)
        langs = list_languages(prof.norms)
        # the translator works with these languages; the extra ones in LANG_CONFIG are not supported yet
        return [lang for lang in langs if lang.code in MAIN_LANG_ORDER]

    def list_scripts(self) -> list[ScriptInfo]:
        out: list[ScriptInfo] = []
        if not self.work_dir.exists():
            return out
        dirs = [d for d in self.work_dir.iterdir() if d.is_dir() and (d / "source.json").exists()]
        for d in sorted(dirs, key=lambda p: (p / "source.json").stat().st_mtime, reverse=True):
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
        raw_docx = str(data.get("docx") or proj.name).replace("\\", "/")
        title = data.get("title") or Path(raw_docx).stem.replace("_", " ")
        return ScriptInfo(
            script_id=proj.name,
            name=proj.name,
            title=title,
            source_lang=api_lang(data.get("source_lang") or "EN"),
            paragraphs_total=len(numbered),
            words_total=words,
            emotion_tags_count=tags_count,
            bytes_total=bytes_total,
            errors=[],
        )

    async def upload_script(self, filename: str, content: bytes, title: str | None = None) -> ScriptInfo:
        self.tr.require()
        stem = Path(filename).stem
        # a language suffix in the name of an original would read as a translation (rome_RU = Russian track)
        clean = LANG_SUFFIX_RE.sub("", short_name(stem)) or "script"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        docx_path = self.input_dir / f"{clean}.docx"
        docx_path.write_bytes(content)
        args = ["prepare", str(docx_path)]
        if title or stem:
            args += ["--title", title or stem.replace("_", " ")]
        res = await self.tr.call(*args)
        return self._script_info_from_dir(self.work_dir / res["script_id"])

    async def _set_source_lang(self, script_id: str, lang: str) -> None:
        src = self.tr.source(script_id) or {}
        if src and disk_lang(lang) != (src.get("source_lang") or "EN") and src.get("docx"):
            await self.tr.call("prepare", src["docx"], "--source-lang", disk_lang(lang))

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
        parallel = int((self.tr.config().get("parallel") or {}).get(t_model.engine, 3))

        trans_sec = 0
        if req.mode in ("all", "translate"):
            # languages of one job go one after another; split jobs run in parallel up to the engine's limit
            batches = targets_count / max(1, min(parallel, targets_count)) if req.split_by_language else targets_count
            trans_sec = int(batches * t_model.est_minutes_per_lang * 60 * (paras / 120.0))
        voice_sec = int((targets_count + 1) * paras * 1.5) if req.mode in ("all", "voice") else 0
        mix_sec = int(paras * 1.2) if req.mode in ("all", "mix") else 0

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

    # ------------------------------------------------------- Jobs: translator queue → web jobs ---

    def _chains(self) -> dict[int, list[dict[str, Any]]]:
        """Translator jobs grouped into web jobs: {root id: [jobs in order]}. Model tests are not shown."""
        jobs = self.tr.jobs()
        chains: dict[int, list[dict[str, Any]]] = {}
        for jid in sorted(jobs):
            j = jobs[jid]
            if j.get("test"):
                continue
            j["id"] = jid
            chains.setdefault(int(j.get("root") or jid), []).append(j)
        return chains

    def _chain(self, job_id: int) -> list[dict[str, Any]]:
        chain = self._chains().get(job_id)
        if not chain:
            raise VoiceApiError(404, "job_not_found", f"Задача #{job_id} не найдена")
        return chain

    def _chain_slug(self, chain: list[dict[str, Any]], by_docx: dict[str, str]) -> str:
        for j in chain:
            if (j.get("web") or {}).get("script"):
                return j["web"]["script"]
        if not by_docx:
            by_docx.update(self.tr.slug_by_docx())
        for j in chain:
            if _kind(j) in ("mix", "join") and j.get("script"):
                return j["script"]
            if _kind(j) == "translate" and j.get("docx"):
                stem = Path(str(j["docx"]).replace("\\", "/")).stem
                if stem in by_docx:
                    return by_docx[stem]
            if _kind(j) == "voice":
                for it in j.get("items") or []:
                    stem = Path(str(it["docx"]).replace("\\", "/")).stem
                    if stem in by_docx:
                        return by_docx[stem]
                    base = LANG_SUFFIX_RE.sub("", stem)
                    if (self.work_dir / base / "source.json").exists():
                        return base
        return LANG_SUFFIX_RE.sub("", short_name(Path(str(chain[0].get("name") or chain[0]["id"])).stem))

    def _targets(self, chain: list[dict[str, Any]], src_lang: str, slug: str) -> list[str]:
        """Target languages (disk codes) of a web job."""
        first = chain[0]
        order = [src_lang] + [lang for lang in ALL_DISK_LANGS if lang != src_lang]
        if _kind(first) == "translate":
            langs = first.get("langs") or [lang for lang in ALL_DISK_LANGS if lang != src_lang]
            if src_lang == "EN" and not first.get("langs"):
                langs = [lang for lang in (self.tr.config().get("language_order") or ALL_DISK_LANGS[1:]) if lang != "EN"]
        elif _kind(first) == "voice":
            langs = [it["lang"] for it in first.get("items") or [] if not it.get("source") and it["lang"] != src_lang]
        else:
            done = (first.get("mix") or {}).get("stats") or {}
            langs = [lang for lang in done if lang != src_lang] or [
                lang for lang in ALL_DISK_LANGS if lang != src_lang and _count_mp3(self.audio_dir / f"{slug}_{lang}")
            ]
        return sorted({lang for lang in langs if lang != src_lang}, key=lambda x: order.index(x) if x in order else 99)

    def _build(self, root: int, chain: list[dict[str, Any]], by_docx: dict[str, str]) -> VoiceJob:
        first, cur = chain[0], chain[-1]
        web = next((j.get("web") for j in chain if j.get("web")), None) or {}
        slug = self._chain_slug(chain, by_docx)
        src = self.tr.source(slug) or {}
        src_lang = disk_lang(src.get("source_lang") or "EN")
        numbered = [it for it in src.get("items") or [] if not it.get("heading")]
        paras_total = len(numbered)
        tags_count = sum(1 for it in numbered if has_tags(it.get("text", "")))

        kind0 = _kind(first)
        mode = web.get("mode") or ("all" if first.get("pipeline") and kind0 == "translate" else
                                   {"translate": "translate", "voice": "voice"}.get(kind0, "mix"))
        targets = self._targets(chain, src_lang, slug)
        tr_job = next((j for j in chain if _kind(j) == "translate"), None)
        v_job = next((j for j in reversed(chain) if _kind(j) == "voice"), None)
        v_jobs = [j for j in chain if _kind(j) == "voice"]
        m_job = next((j for j in reversed(chain) if _kind(j) in ("mix", "join")), None)

        prof = self.get_profile(first.get("profile"))
        chosen = next((j.get("voices") for j in chain if j.get("voices")), None) or {}
        voices = dict(prof.default)
        for lang, ref in chosen.items():
            voices[api_lang(lang)] = self.resolve_short_voice_id(ref)
        cfg = self.tr.config()
        if web.get("model"):
            model = web["model"]
        elif tr_job and _engine(tr_job) == "claude":
            model = "claude-code"
        elif tr_job:
            model = tr_job.get("agy_model") or (cfg.get("agy") or {}).get("model") or "gemini-4-argon-medium"
        else:
            model = "gemini-4-argon-medium"
        fish = next((j.get("voice_model") for j in chain if j.get("voice_model")), None) or (cfg.get("fish") or {}).get("model", "")
        voice_model = web.get("voice_model") or ("drama-3" if str(fish).startswith("drama") else "s2.1")
        carry = next((bool(j["carry_mood"]) for j in chain if "carry_mood" in j), bool((cfg.get("fish") or {}).get("drama_carry_mood")))

        # ---- status of the whole chain
        error_code = error_message = pause_until = pause_reason = None
        running = next((j for j in chain if j.get("status") == "running"), None)
        status: Any
        if running:
            status = "paused" if running.get("pause_until") else "running"
            pause_until = parse_short_time(running.get("pause_until"))
            pause_reason = running.get("pause_reason")
        elif cur.get("status") == "pending":
            status = "queued" if all(j.get("status") == "pending" for j in chain) else "running"
        elif cur.get("status") == "failed":
            status = "failed"
            error_code = "translator_error"
            error_message = (str(cur.get("error") or "").strip().splitlines() or ["Ошибка, подробности — в журнале задачи"])[-1]
        elif cur.get("status") == "stopped":
            status = "stopped"
        elif mode == "all" and _kind(cur) != "mix" and cur.get("status") == "done":
            if _kind(cur) == "voice" and src_lang in (cur.get("skipped") or {}):
                status, error_code = "failed", "voice_missing"
                error_message = f"Голос оригинала ({api_lang(src_lang)}) недоступен в Fish Audio — сводить не с чем"
            else:
                status = "running"  # the next step is being queued
        else:
            status = "done"

        skipped: dict[str, dict[str, str]] = {}
        for j in v_jobs:
            for lang, vid in (j.get("skipped") or {}).items():
                skipped[api_lang(lang)] = {"code": "voice_missing", "voice_id": self.resolve_short_voice_id(vid)}
        for j in v_jobs:  # languages voiced again later are no longer missing
            for lang, res in (j.get("results") or {}).items():
                if res != "голос недоступен" and j is not next((x for x in v_jobs if lang in (x.get("skipped") or {})), None):
                    skipped.pop(api_lang(lang), None)

        mix_info = (m_job or {}).get("mix") or {}
        duration = mix_info.get("duration")
        if duration is None and paras_total:
            duration = round(sum(max(4.5, spoken_len(it["text"], src_lang) / 15.5) for it in numbered), 1)

        langs_out: list[VoiceJobLanguage] = []
        summary_langs: dict[str, VoiceJobSummaryLang] = {}

        def voice_status(lang: str) -> VoiceStageStatus:
            vid = voices.get(api_lang(lang))
            name = self._voice_name(vid) if vid else None
            folder = self.audio_dir / (slug if lang == src_lang else f"{slug}_{lang}")
            have = _count_mp3(folder)
            fish_model: Any = "drama" if voice_model == "drama-3" else "free"
            if mode == "translate" or (mode == "mix" and not v_job):
                state: Any = "done" if have >= paras_total > 0 else "skipped"
                return VoiceStageStatus(state=state, done=have, total=paras_total, voice_id=vid, voice_name=name, fish_model=fish_model)
            if api_lang(lang) in skipped:
                return VoiceStageStatus(state="voice_missing", done=have, total=paras_total, voice_id=vid, voice_name=name)
            j = next((x for x in reversed(v_jobs) if lang in (x.get("results") or {}) or
                      any(it["lang"] == lang for it in x.get("items") or [])), None)
            if not j:
                return VoiceStageStatus(state="waiting", done=have, total=paras_total, voice_id=vid, voice_name=name, fish_model=fish_model)
            res = (j.get("results") or {}).get(lang)
            prog = j.get("progress") or {}
            counts = _voiced_counts(res)
            new, reused = counts if counts else (have, 0)
            if res and res != "голос недоступен":
                state = "done"
            elif j.get("status") == "running" and prog.get("lang") == lang:
                state = "running"
                have = int(prog.get("done", have))
            elif j.get("status") == "failed" and prog.get("lang") == lang:
                state = "failed"
            else:
                state = "waiting"
            if res and "Drama" not in res and voice_model != "drama-3":
                fish_model = "paid" if "платн" in res or "s2.1-pro:" in res else "free"
            return VoiceStageStatus(state=state, done=have, total=paras_total, new=new, reused=reused,
                                    fish_model=fish_model, voice_id=vid, voice_name=name)

        def mix_status(lang: str, voice_state: str) -> MixStageStatus:
            have = _count_mp3(self.mix_dir / slug / lang)
            st = (mix_info.get("stats") or {}).get(lang) or {}
            if mode in ("translate", "voice") or voice_state == "voice_missing":
                return MixStageStatus(state="skipped", done=have, total=paras_total)
            if not m_job:
                return MixStageStatus(state="waiting", done=have, total=paras_total)
            ms = m_job.get("status")
            prog = m_job.get("progress") or {}
            if ms == "done":
                return MixStageStatus(state="done", done=have, total=paras_total, duration_sec=duration,
                                      sped_up=st.get("sped"), max_speedup=st.get("max_speed"))
            if ms == "running":
                return MixStageStatus(state="running", done=int(prog.get("done", 0)), total=paras_total)
            return MixStageStatus(state="failed" if ms == "failed" else "waiting", done=have, total=paras_total)

        # source language: voiceover only
        src_voice = voice_status(src_lang)
        langs_out.append(VoiceJobLanguage(lang=api_lang(src_lang), stage="done" if src_voice.state in ("done", "skipped") else "voice",
                                          translate=None, voice=src_voice, mix=None))

        for lang in targets:
            stats = self.tr.stats(slug, lang) or {}
            paras = stats.get("paragraphs") or {}
            translated = int(stats.get("translated") or 0)
            if tr_job is None:
                t_state: Any = "done" if translated >= paras_total > 0 else "skipped"
            elif lang in (tr_job.get("results") or {}):
                t_state = "done"
            else:
                prog = tr_job.get("progress") or {}
                ts = tr_job.get("status")
                if ts == "running":
                    t_state = "running" if prog.get("lang") == lang else "waiting"
                elif ts == "failed" and prog.get("lang") == lang:
                    t_state = "failed"
                elif ts == "done":
                    t_state = "done"
                else:
                    t_state = "waiting"
            prog = (tr_job or {}).get("progress") or {}
            parts_total = int(prog.get("parts") or 1) if prog.get("lang") == lang else 1
            parts_done = parts_total if t_state == "done" else (int(prog.get("part", 1)) - 1 if prog.get("lang") == lang else 0)
            tags_issues = sum(1 for p in paras.values() if p.get("tags_problem"))
            v = voice_status(lang)
            m = mix_status(lang, v.state)
            stage: Any = "done"
            for name, state in (("translate", t_state), ("voice", v.state), ("mix", m.state)):
                if state not in ("done", "skipped", "voice_missing"):
                    stage = name
                    break
            langs_out.append(
                VoiceJobLanguage(
                    lang=api_lang(lang),
                    stage=stage,
                    translate=TranslateStageStatus(state=t_state, parts_done=max(0, parts_done), parts_total=parts_total,
                                                   length_pct=stats.get("total"), in_norm=int(stats.get("in_norm") or 0),
                                                   total=paras_total, tags_issues=tags_issues),
                    voice=v,
                    mix=m,
                )
            )
            mst = (mix_info.get("stats") or {}).get(lang) or {}
            summary_langs[api_lang(lang)] = VoiceJobSummaryLang(
                length_pct=stats.get("total"),
                in_norm=int(stats.get("in_norm") or 0),
                total=paras_total,
                sped_up=int(mst.get("sped") or 0),
                max_speedup=float(mst.get("max_speed") or 1.0),
                fish_model=v.fish_model,
                voice_model=voice_model,
                voice_new=v.new,
                voice_reused=v.reused,
                carried=len(carry_plan([t for t in load_translation(self.work_dir / slug, lang).values()])) if carry else 0,
            )

        title = web.get("title") or src.get("title") or Path(str(first.get("name") or slug)).stem.replace("_", " ")
        created = parse_short_time(first.get("added")) or _now_iso()
        return VoiceJob(
            id=root,
            title=title,
            script=slug,
            project_id=web.get("project_id"),
            mode=mode,
            source_lang=api_lang(src_lang),
            targets=[api_lang(t) for t in targets],
            profile_id=prof.id,
            profile_name=prof.name,
            voices=voices,
            model=model,
            voice_model=voice_model,
            carry_mood=carry,
            status=status,
            paragraphs_total=paras_total,
            emotion_tags_count=tags_count,
            source_duration_sec=duration,
            created_at=created,
            started_at=parse_short_time(first.get("started")),
            finished_at=parse_short_time(cur.get("finished")) if status == "done" else None,
            pause_until=pause_until,
            pause_reason=pause_reason,
            error_code=error_code,
            error_message=error_message,
            waiting_for_job_id=None,
            languages=langs_out,
            summary=VoiceJobSummary(duration_sec=duration, languages=summary_langs, norm_suggestions={}, skipped=skipped),
            files=self._collect_job_files(root, slug, src_lang, targets),
        )

    def _collect_job_files(self, job_id: int, slug: str, src_lang: str, targets: list[str]) -> list[VoiceJobFile]:
        """Only files that exist (sizes are real; zip size = the tracks inside)."""
        out: list[VoiceJobFile] = []

        def add(kind: Any, lang: str, name: str, size: int) -> None:
            fid = f"f_{job_id}_{api_lang(lang)}_{kind}"
            out.append(VoiceJobFile(id=fid, kind=kind, lang=api_lang(lang), name=name, size=size, url=f"/api/voice/files/{fid}"))

        def tracks_size(folder: Path) -> int:
            try:
                return sum(p.stat().st_size for p in folder.glob("*.mp3") if p.stem.isdigit())
            except OSError:
                return 0

        for lang in [src_lang] + targets:
            a = api_lang(lang)
            docx_p = self.output_dir / f"{slug}_{lang}.docx"
            if lang != src_lang and docx_p.exists():
                add("docx", lang, f"{slug}_{a}.docx", docx_p.stat().st_size)
            raw = self.audio_dir / (slug if lang == src_lang else f"{slug}_{lang}")
            if _count_mp3(raw):
                add("raw_zip", lang, f"{slug}_{a}_audio.zip", tracks_size(raw))
            mixed = self.mix_dir / slug / lang
            if _count_mp3(mixed):
                add("mixed_zip", lang, f"{slug}_{a}.zip", tracks_size(mixed))
            joined = self.mix_dir / slug / f"{slug}_{lang}.mp3"
            if joined.exists():
                add("joined_mp3", lang, f"{slug}_{a}.mp3", joined.stat().st_size)
        return out

    def _cache_key(self, chain: list[dict[str, Any]], slug: str) -> tuple:
        """What a built job depends on: its translator jobs and the folders the worker writes for the script."""

        def mtime(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0

        dirs = [self.work_dir / slug, self.audio_dir / slug, self.mix_dir / slug, self.tools_dir / "voices.json"]
        dirs += [self.audio_dir / f"{slug}_{lang}" for lang in ALL_DISK_LANGS]
        dirs += [self.mix_dir / slug / lang for lang in ALL_DISK_LANGS]
        return (json.dumps(chain, sort_keys=True, ensure_ascii=False), tuple(mtime(d) for d in dirs))

    def _build_cached(self, root: int, chain: list[dict[str, Any]], by_docx: dict[str, str]) -> VoiceJob:
        key = self._cache_key(chain, self._chain_slug(chain, by_docx))
        hit = self._built.get(root)
        if hit and hit[0] == key:
            return hit[1]
        job = self._build(root, chain, by_docx)
        self._built[root] = (key, job)
        return job

    def list_jobs(self, status: str | None = None, project_id: str | None = None) -> list[VoiceJob]:
        by_docx: dict[str, str] = {}
        items = []
        chains = sorted(self._chains().items(), reverse=True)[:LIST_LIMIT]
        for gone in set(self._built) - {root for root, _ in chains}:
            self._built.pop(gone, None)
        for root, chain in chains:
            try:
                items.append(self._build_cached(root, chain, by_docx))
            except Exception:
                logger.exception(f"[voice] cannot show translator job #{root}")
        if status:
            items = [j for j in items if j.status == status]
        if project_id:
            items = [j for j in items if j.project_id in (project_id, None)]
        return items

    def get_job(self, job_id: int) -> VoiceJob:
        return self._build_cached(job_id, self._chain(job_id), {})

    async def create_jobs(self, req: CreateVoiceJobRequest) -> list[VoiceJob]:
        self.tr.require()
        proj = self.work_dir / req.script_id
        if not (proj / "source.json").exists():
            raise VoiceApiError(404, "script_not_found", f"Сценарий {req.script_id} не найден")
        if req.source_lang:
            await self._set_source_lang(req.script_id, req.source_lang)
        src_lang = disk_lang((self.tr.source(req.script_id) or {}).get("source_lang") or "EN")
        prof = self.get_profile(req.profile_id)
        t_model = next((m for m in TRANSLATION_MODELS if m.id == req.model), None)
        if not t_model:
            raise VoiceApiError(422, "unknown_model", f"Неизвестная модель перевода {req.model}")

        targets = [disk_lang(t) for t in req.targets if disk_lang(t) != src_lang]
        if req.targets and not targets and req.mode != "voice":
            raise VoiceApiError(422, "no_targets", f"Языки перевода совпадают с языком оригинала ({api_lang(src_lang)})")
        voices: dict[str, str] = {}
        for lang in [src_lang] + targets:
            ref = req.voices.get(api_lang(lang)) or req.voices.get(lang) or prof.default.get(api_lang(lang))
            if ref:
                voices[lang] = self.resolve_fish_id(ref)
        missing_voice = [api_lang(lang) for lang in [src_lang] + targets if lang not in voices]
        if req.mode in ("all", "voice") and missing_voice:
            raise VoiceApiError(422, "voice_missing", "Не выбран голос: " + ", ".join(missing_voice))
        spec = {
            "mode": req.mode,
            "script": req.script_id,
            "langs": targets,
            "engine": t_model.engine,
            "agy_model": t_model.id if t_model.engine == "agy" else None,
            "profile": prof.id,
            "voices": voices,
            "voice_model": req.voice_model,
            "carry_mood": req.carry_mood if req.voice_model == "drama-3" else False,
            "split": req.split_by_language,
            "web": {"title": req.title or None, "project_id": req.project_id, "model": req.model,
                    "voice_model": req.voice_model, "mode": req.mode, "script": req.script_id},
        }
        res = await self.tr.call("submit", stdin=json.dumps(spec, ensure_ascii=False))
        ids = [int(j["id"]) for j in res.get("jobs") or []]
        self.scan()
        return [self.get_job(i) for i in ids]

    async def stop_job(self, job_id: int) -> VoiceJob:
        chain = self._chain(job_id)
        active = [j for j in chain if j.get("status") in ("running", "pending")]
        if any(_kind(j) in ("mix", "join") and j.get("status") == "running" for j in active):
            raise VoiceApiError(409, "not_stoppable", "Сведение дойдёт до конца — оно занимает несколько минут")
        for j in active:
            await self.tr.call("stop", str(j["id"]))
        self.scan()
        return self.get_job(job_id)

    async def resume_job(self, job_id: int) -> VoiceJob:
        chain = self._chain(job_id)
        cur = chain[-1]
        if cur.get("status") in ("stopped", "failed"):
            await self.tr.call("resume", str(cur["id"]))
        self.scan()
        return self.get_job(job_id)

    async def delete_job(self, job_id: int) -> None:
        for j in self._chain(job_id):
            await self.tr.call("drop", str(j["id"]))
        self.scan()

    async def revoice_skipped(self, job_id: int, req: RevoiceSkippedRequest) -> VoiceJob:
        chain = self._chain(job_id)
        v_job = next((j for j in reversed(chain) if _kind(j) == "voice" and j.get("skipped")), None)
        if not v_job:
            raise VoiceApiError(409, "nothing_to_revoice", "Пропущенных языков нет")
        prof = self.get_profile(chain[0].get("profile"))
        voices_by_id = {v.id: v for v in self.list_voices()}
        voices: dict[str, str] = {}
        for lang in v_job["skipped"]:
            ref = req.voices.get(api_lang(lang)) or req.voices.get(lang) or prof.default.get(api_lang(lang))
            if not ref:
                raise VoiceApiError(422, "voice_missing", f"Для языка {api_lang(lang)} не выбран доступный голос")
            entry = voices_by_id.get(self.resolve_short_voice_id(ref))
            if entry and entry.status == "missing":
                raise VoiceApiError(422, "voice_missing", f"Голос «{entry.name}» для {api_lang(lang)} всё ещё недоступен в Fish Audio")
            voices[lang] = self.resolve_fish_id(ref)
        await self.tr.call("revoice", str(v_job["id"]), stdin=json.dumps({"voices": voices}))
        self.scan()
        return self.get_job(job_id)

    def queue_status(self) -> QueueStatusResponse:
        jobs = [j for chain in self._chains().values() for j in chain]
        running = [j for j in jobs if j.get("status") == "running"]
        par = self.tr.config().get("parallel") or {}
        roots = {int(j.get("root") or j["id"]) for j in running}
        queued_roots = {int(j.get("root") or j["id"]) for j in jobs if j.get("status") == "pending"} - roots
        return QueueStatusResponse(
            running=len(roots),
            queued=len(queued_roots),
            slots={
                "gemini": SlotUsage(used=sum(1 for j in running if _kind(j) == "translate" and _engine(j) == "agy"),
                                    total=int(par.get("agy", 3))),
                "claude": SlotUsage(used=sum(1 for j in running if _kind(j) == "translate" and _engine(j) == "claude"),
                                    total=int(par.get("claude", 3))),
                "voice": SlotUsage(used=sum(1 for j in running if _kind(j) == "voice"), total=1),
                "mix": SlotUsage(used=sum(1 for j in running if _kind(j) in ("mix", "join")), total=1),
            },
            worker_online=self.tr.worker_online(),
        )

    def _emit_queue_changed(self) -> None:
        self.emit("queue.changed", self.queue_status().model_dump())

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
        tmp = f.with_name(f"edits.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)

    def _busy(self, job_id: int, lang: str) -> bool:
        """The bot is working on this language of this job (translating, voicing or mixing it)."""
        for j in self._chain(job_id):
            if j.get("status") != "running":
                continue
            prog = j.get("progress") or {}
            if _kind(j) in ("mix", "join") or prog.get("lang") in (None, lang):
                return True
        return False

    async def _stats(self, job: VoiceJob, lang: str) -> dict[str, Any]:
        """Length stats of a translation, refreshed by the translator when the translation file is newer."""
        proj = self.work_dir / job.script
        md = proj / f"{lang}.md"
        stats_f = proj / f"{lang}.stats.json"
        if md.exists() and (not stats_f.exists() or stats_f.stat().st_mtime < md.stat().st_mtime):
            try:
                return await self.tr.call("stats", job.script, lang, "--profile", job.profile_id)
            except VoiceApiError as e:
                logger.warning(f"[voice] stats {job.script} {lang}: {e.message}")
        return self.tr.stats(job.script, lang) or {}

    async def get_paragraphs(
        self,
        job_id: int,
        lang: str,
        filter_by: ParagraphFilter = "all",
    ) -> list[VoiceParagraph]:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        src_lang = disk_lang(job.source_lang)
        proj = self.work_dir / job.script
        src = self.tr.source(job.script)
        if not src:
            return []
        numbered = [it for it in src.get("items", []) if not it.get("heading")]
        is_source = t_lang == src_lang
        tr_blocks = {it["key"]: it["text"] for it in numbered} if is_source else load_translation(proj, t_lang)
        stats = {} if is_source else await self._stats(job, t_lang)
        p_stats = stats.get("paragraphs") or {}
        lang_range = tuple(stats.get("range") or job_norm(self, job, t_lang))
        edits = self._read_edits(proj).get(api_lang(t_lang), {})

        ordered_texts = [tr_blocks.get(it["key"], "") for it in numbered]
        carried_map = carry_plan(ordered_texts) if job.carry_mood and job.voice_model == "drama-3" else {}
        pause = float((self.tr.config().get("mix") or {}).get("pause_sec", 0.5))

        out: list[VoiceParagraph] = []
        elapsed_sec = 0.0
        for idx, it in enumerate(numbered, 1):
            key = it["key"]
            n = int(key) if key.isdigit() else idx
            src_text = it["text"]
            tr_text = tr_blocks.get(key, "")
            meta = edits.get(str(n), {})
            st = p_stats.get(key)
            if is_source:
                pct, in_norm, t_ok, t_prob, delta, norm = 100.0, True, True, None, 0, (100, 100)
            elif st and tr_text:
                norm = (int(round(st["lo"])), int(round(st["hi"])))
                pct, in_norm, delta = float(st["pct"]), bool(st["in_norm"]), int(st.get("delta") or 0)
                t_prob = st.get("tags_problem")
                t_ok = not t_prob
            else:
                norm = (int(lang_range[0]), int(lang_range[1]))
                pct, in_norm, t_ok, t_prob, delta = evaluate_paragraph(src_text, tr_text, src_lang, t_lang, norm)
                in_norm = in_norm and bool(tr_text)

            src_mp3 = self.audio_dir / job.script / f"{n}.mp3"
            raw_mp3 = self.audio_dir / (job.script if is_source else f"{job.script}_{t_lang}") / f"{n}.mp3"
            mixed_mp3 = self.mix_dir / job.script / t_lang / f"{n}.mp3"
            src_sec = mp3_duration_sec(src_mp3)
            raw_sec = mp3_duration_sec(raw_mp3)
            slot_sec = mp3_duration_sec(mixed_mp3) or (round(src_sec + pause, 2) if src_sec else None)
            audio = None
            if src_sec or raw_sec:
                speedup = round(raw_sec / slot_sec, 2) if raw_sec and slot_sec and raw_sec > slot_sec else 1.0
                audio = ParagraphAudioTiming(source_sec=src_sec, slot_sec=slot_sec, track_sec=raw_sec, speedup=speedup)
            mins, secs = divmod(int(elapsed_sec), 60)
            elapsed_sec += slot_sec or max(4.5, spoken_len(src_text, src_lang) / 15.5) + pause

            p_obj = VoiceParagraph(
                n=n,
                timecode=f"{mins}:{secs:02d}",
                source=src_text,
                text=tr_text,
                length_pct=pct,
                norm=norm,
                in_norm=in_norm,
                edited=bool(meta.get("edited", False)),
                tags_ok=t_ok,
                tags_problem=t_prob,
                carried_mood=carried_map.get(idx),
                chars_delta=delta,
                audio=audio,
                audio_url=f"/api/voice/jobs/{job_id}/languages/{api_lang(t_lang)}/paragraphs/{n}/audio?track=mixed",
                source_audio_url=f"/api/voice/jobs/{job_id}/languages/{api_lang(t_lang)}/paragraphs/{n}/audio?track=source",
                version=int(meta.get("version", 1)),
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

    async def _get_single_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        paras = await self.get_paragraphs(job_id, lang, filter_by="all")
        p = next((x for x in paras if x.n == n), None)
        if not p:
            raise VoiceApiError(404, "paragraph_not_found", f"Абзац #{n} не найден")
        return p

    async def measure_paragraph_length(self, job_id: int, lang: str, n: int, text: str) -> ParagraphLengthResponse:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        st = await self.tr.call("measure", job.script, t_lang, str(n), "--profile", job.profile_id, stdin=text)
        return ParagraphLengthResponse(
            length_pct=float(st["pct"]),
            norm=(int(round(st["lo"])), int(round(st["hi"]))),
            in_norm=bool(st["in_norm"]),
            tags_ok=not st.get("tags_problem"),
            tags_problem=st.get("tags_problem"),
            chars_delta=int(st.get("delta") or 0),
        )

    def _paragraph_event(self, job_id: int, lang: str, p: VoiceParagraph) -> None:
        self.emit("paragraph.updated", {"job_id": job_id, "lang": api_lang(lang), "n": p.n, "version": p.version,
                                        "length_pct": p.length_pct, "in_norm": p.in_norm,
                                        "speedup": p.audio.speedup if p.audio else 1.0})

    async def _write_paragraph(self, job: VoiceJob, lang: str, n: int, text: str, edited: bool,
                               version: int | None = None) -> VoiceParagraph:
        proj = self.work_dir / job.script
        edits_all = self._read_edits(proj)
        meta = edits_all.setdefault(api_lang(lang), {}).setdefault(str(n), {"version": 1, "edited": False})
        if version is not None and version != int(meta.get("version", 1)):
            raise VoiceApiError(409, "version_conflict", "Абзац уже изменили, обновите")
        current = load_translation(proj, lang).get(str(n))
        if edited and "model_text" not in meta and current:
            meta["model_text"] = current
        if not edited:
            meta["model_text"] = text
        await self.tr.call("set-paragraph", job.script, lang, str(n), "--profile", job.profile_id, stdin=text)
        meta["version"] = int(meta.get("version", 1)) + 1
        meta["edited"] = edited
        self._write_edits(proj, edits_all)
        updated = await self._get_single_paragraph(job.id, lang, n)
        self._paragraph_event(job.id, lang, updated)
        return updated

    async def patch_paragraph(self, job_id: int, lang: str, n: int, req: PatchParagraphRequest) -> VoiceParagraph:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        if t_lang == disk_lang(job.source_lang):
            raise VoiceApiError(409, "source_readonly", "Оригинал не редактируется")
        if self._busy(job_id, t_lang):
            raise VoiceApiError(409, "language_busy", "Язык сейчас в работе — дождитесь окончания этапа")
        return await self._write_paragraph(job, t_lang, n, req.text.strip(), edited=True, version=req.version)

    async def revert_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        meta = self._read_edits(self.work_dir / job.script).get(api_lang(t_lang), {}).get(str(n), {})
        if not meta.get("model_text") or not meta.get("edited"):
            return await self._get_single_paragraph(job_id, t_lang, n)
        if self._busy(job_id, t_lang):
            raise VoiceApiError(409, "language_busy", "Язык сейчас в работе — дождитесь окончания этапа")
        return await self._write_paragraph(job, t_lang, n, meta["model_text"], edited=False)

    async def retranslate_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        """Translate one paragraph again with the job's model, rules and length counter (takes 1–3 min)."""
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        if t_lang == disk_lang(job.source_lang):
            raise VoiceApiError(409, "source_readonly", "Оригинал не переводится")
        if self._busy(job_id, t_lang):
            raise VoiceApiError(409, "language_busy", "Язык сейчас в работе — дождитесь окончания этапа")
        t_model = next((m for m in TRANSLATION_MODELS if m.id == job.model), TRANSLATION_MODELS[0])
        args = ["retranslate", job.script, t_lang, str(n), "--engine", t_model.engine, "--profile", job.profile_id]
        if t_model.engine == "agy":
            args += ["--model", t_model.id]
        res = await self.tr.call(*args, timeout=RETRANSLATE_TIMEOUT)
        proj = self.work_dir / job.script
        edits_all = self._read_edits(proj)
        meta = edits_all.setdefault(api_lang(t_lang), {}).setdefault(str(n), {"version": 1, "edited": False})
        meta.update(model_text=res["text"], edited=False, version=int(meta.get("version", 1)) + 1)
        self._write_edits(proj, edits_all)
        updated = await self._get_single_paragraph(job_id, t_lang, n)
        self._paragraph_event(job_id, t_lang, updated)
        return updated

    async def revoice_paragraph(self, job_id: int, lang: str, n: int) -> VoiceParagraph:
        """Voice one paragraph again with the job's voice and model, then mix this paragraph again."""
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        if self._busy(job_id, t_lang):
            raise VoiceApiError(409, "language_busy", "Язык сейчас в работе — дождитесь окончания этапа")
        p = await self._get_single_paragraph(job_id, t_lang, n)
        if not p.text:
            raise VoiceApiError(409, "no_translation", f"Абзац #{n} ещё не переведён")
        vid = job.voices.get(api_lang(t_lang)) or self.get_profile(job.profile_id).default.get(api_lang(t_lang))
        if not vid:
            raise VoiceApiError(422, "voice_missing", f"Для {api_lang(t_lang)} не выбран голос")
        key = self.fish_api_key()
        if not key:
            raise VoiceApiError(503, "fish_key_missing", "Нет ключа Fish Audio (FISH_API_KEY или bot_config.json переводчика)")
        tts_text = with_carry(p.text, p.carried_mood) if job.voice_model == "drama-3" and job.carry_mood else p.text
        if job.voice_model == "drama-3":
            audio = await synthesize_fish(clean_for_tts(tts_text), self.resolve_fish_id(vid), key, model=DRAMA_MODEL, attempts=2)
        else:
            audio, _ = await self._synth_s2(clean_for_tts(tts_text), self.resolve_fish_id(vid), key)
        is_source = t_lang == disk_lang(job.source_lang)
        folder = self.audio_dir / (job.script if is_source else f"{job.script}_{t_lang}")
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{n}.mp3.part"
        tmp.write_bytes(audio)
        tmp.replace(folder / f"{n}.mp3")
        if (self.mix_dir / job.script).exists() and (self.audio_dir / job.script / f"{n}.mp3").exists():
            await self.tr.call("mix-one", job.script, str(n), timeout=300)
        updated = await self._get_single_paragraph(job_id, t_lang, n)
        self._paragraph_event(job_id, t_lang, updated)
        return updated

    async def _synth_s2(self, text: str, fish_id: str, key: str) -> tuple[bytes, str]:
        """Fish S2.1 like the translator: the free model first, the paid one only if the free one refuses
        (translator config fish.free_first / free_model / model)."""
        fish = self.tr.config().get("fish") or {}
        paid = fish.get("model") or PAID_MODEL_DEFAULT
        if str(paid).startswith("drama"):
            paid = PAID_MODEL_DEFAULT
        free = fish.get("free_model") or FREE_MODEL_DEFAULT
        if fish.get("free_first", True) and free != paid and str(free).startswith(paid):
            try:
                return await synthesize_fish(text, fish_id, key, model=free, attempts=2), free
            except VoiceApiError as e:
                if e.code in ("voice_missing", "fish_auth"):  # another model will not help
                    raise
                logger.warning(f"[voice] free Fish model {free} refused ({e.message}), using {paid}")
        return await synthesize_fish(text, fish_id, key, model=paid, attempts=2), paid

    async def compare_paragraph_voices(self, job_id: int, lang: str, n: int) -> list[CompareVoiceVariant]:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        p = await self._get_single_paragraph(job_id, t_lang, n)
        vid = job.voices.get(api_lang(t_lang)) or self.get_profile(job.profile_id).default.get(api_lang(t_lang), "")
        fish_id = self.resolve_fish_id(vid)
        key = self.fish_api_key()
        if not key:
            raise VoiceApiError(503, "fish_key_missing", "Нет ключа Fish Audio (FISH_API_KEY или bot_config.json переводчика)")
        specs: list[tuple[str, str, str, str]] = [
            ("S2.1 Free", FREE_MODEL_DEFAULT, p.text, "s2_free"),
            ("S2.1 Pro", PAID_MODEL_DEFAULT, p.text, "s2_pro"),
            ("Drama 3", DRAMA_MODEL, p.text, "drama3"),
        ]
        if p.carried_mood:
            specs.append((f"Drama 3 + перенос {p.carried_mood}", DRAMA_MODEL, with_carry(p.text, p.carried_mood), "drama3_carry"))
        self.compare_dir.mkdir(parents=True, exist_ok=True)
        out: list[CompareVoiceVariant] = []
        for label, model_id, text_val, tag in specs:
            fpath = self.compare_dir / f"cmp_{job.script}_{t_lang}_{n}_{tag}_v{p.version}.mp3"
            fid = f"cmp_{job_id}_{api_lang(t_lang)}_{n}_{tag}"
            self._file_registry[fid] = fpath
            if not fpath.exists():
                try:
                    fpath.write_bytes(await synthesize_fish(clean_for_tts(text_val), fish_id, key, model=model_id, attempts=2))
                except Exception as e:
                    out.append(CompareVoiceVariant(label=label, model=model_id, error=str(e)))
                    continue
            out.append(CompareVoiceVariant(label=label, model=model_id, audio_url=f"/api/voice/files/{fid}",
                                           sec=mp3_duration_sec(fpath)))
        return out

    async def get_paragraph_audio_path(self, job_id: int, lang: str, n: int, track: str = "mixed") -> Path:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        src_lang = disk_lang(job.source_lang)
        raw_dir = self.audio_dir / (job.script if t_lang == src_lang else f"{job.script}_{t_lang}")
        if track == "source":
            candidates = [self.mix_dir / job.script / src_lang / f"{n}.mp3", self.audio_dir / job.script / f"{n}.mp3"]
        elif track == "raw":
            candidates = [raw_dir / f"{n}.mp3"]
        else:
            candidates = [self.mix_dir / job.script / t_lang / f"{n}.mp3", raw_dir / f"{n}.mp3"]
        for c in candidates:
            if c.exists() and c.stat().st_size > 500:
                return c
        raise VoiceApiError(404, "audio_not_found", f"Дорожки абзаца #{n} ({api_lang(t_lang)}) ещё нет")

    def get_language_peaks(self, job_id: int, lang: str) -> dict[int, list[float]]:
        job = self.get_job(job_id)
        t_lang = disk_lang(lang)
        raw_dir = self.audio_dir / (job.script if t_lang == disk_lang(job.source_lang) else f"{job.script}_{t_lang}")
        out: dict[int, list[float]] = {}
        for folder in (raw_dir, self.mix_dir / job.script / t_lang):  # mixed tracks win
            if not folder.exists():
                continue
            for mp3 in folder.glob("*.mp3"):
                if mp3.stem.isdigit():
                    peaks = compute_waveform_peaks(mp3, bars=32)
                    if peaks:
                        out[int(mp3.stem)] = peaks
        return out

    # ------------------------------------------------------- Downloads ---

    def _zip_folder(self, folder: Path, name: str) -> Path:
        """Zip of the tracks in folder; rebuilt when a track is newer than the archive."""
        self.api_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.api_dir / name
        files = sorted((p for p in folder.glob("*.mp3") if p.stem.isdigit()), key=lambda p: int(p.stem))
        if not files:
            raise VoiceApiError(404, "file_not_found", "Дорожек ещё нет")
        newest = max(p.stat().st_mtime for p in files)
        if not zip_path.exists() or zip_path.stat().st_mtime < newest:
            tmp = zip_path.with_suffix(".zip.part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
                for mp3 in files:
                    zf.write(mp3, mp3.name)
            os.replace(tmp, zip_path)
        return zip_path

    async def resolve_download_file(self, file_id: str) -> tuple[Path, str]:
        if file_id in self._file_registry and self._file_registry[file_id].exists():
            p = self._file_registry[file_id]
            return p, p.name
        m = re.match(r"^f_(\d+)_([A-Z_]+)_(docx|raw_zip|mixed_zip|joined_mp3)$", file_id)
        if not m:
            raise VoiceApiError(404, "file_not_found", "Файл не найден")
        job = self.get_job(int(m.group(1)))
        lang, kind = disk_lang(m.group(2)), m.group(3)
        a, slug = api_lang(lang), job.script
        if kind == "docx":
            p = self.output_dir / f"{slug}_{lang}.docx"
            if p.exists():
                return p, f"{slug}_{a}.docx"
        elif kind == "raw_zip":
            folder = self.audio_dir / (slug if lang == disk_lang(job.source_lang) else f"{slug}_{lang}")
            return self._zip_folder(folder, f"{slug}_{a}_audio.zip"), f"{slug}_{a}_audio.zip"
        elif kind == "mixed_zip":
            return self._zip_folder(self.mix_dir / slug / lang, f"{slug}_{a}.zip"), f"{slug}_{a}.zip"
        elif kind == "joined_mp3":
            p = self.mix_dir / slug / f"{slug}_{lang}.mp3"
            if p.exists():
                return p, f"{slug}_{a}.mp3"
        raise VoiceApiError(404, "file_not_found", "Файл ещё не готов")

    async def build_job_zip(self, job_id: int) -> tuple[Path, str]:
        """All translations (docx) and mixed tracks of a job in one archive."""
        job = self.get_job(job_id)
        files = [f for f in job.files if f.kind in ("docx", "mixed_zip")]
        if not files:
            raise VoiceApiError(404, "file_not_found", "Готовых файлов ещё нет")
        self.api_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.api_dir / f"{job.script}_all_{job_id}.zip"
        tmp = zip_path.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                p, name = await self.resolve_download_file(f.id)
                if f.kind == "docx":
                    zf.write(p, f"docx/{name}")
                else:
                    folder = self.mix_dir / job.script / disk_lang(f.lang)
                    for mp3 in sorted(folder.glob("*.mp3"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0):
                        zf.write(mp3, f"mix/{f.lang}/{mp3.name}")
        os.replace(tmp, zip_path)
        return zip_path, f"{job.script}_all.zip"


def job_norm(svc: VoiceService, job: VoiceJob, lang: str) -> tuple[int, int]:
    """Profile range of a language (percent of an English original) — used before the translator wrote stats."""
    prof = svc.get_profile(job.profile_id)
    return prof.norms.get(api_lang(lang)) or LANG_CONFIG.get(api_lang(lang), LANG_CONFIG["RU"])["norm"]
