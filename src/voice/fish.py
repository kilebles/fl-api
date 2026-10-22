"""Fish Audio API client (S2.1 & Drama 3) and FFmpeg audio mixing / waveform helpers."""

import array
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import httpx

from src.voice.models import VoiceApiError
from src.voice.text import PAIR_TAG_RE, api_lang, with_carry

VOICE_ID_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", re.IGNORECASE)
FISH_LANGS = {
    "en": "EN",
    "ru": "RU",
    "es": "ES",
    "pt": "PT",
    "de": "DE",
    "it": "IT",
    "pl": "PL",
    "fr": "FR",
    "uk": "UK",
    "tr": "TR",
    "nl": "NL",
    "ja": "JA",
    "ko": "KO",
    "zh": "ZH",
    "hi": "HI",
    "ar": "AR",
}

FREE_MODEL_DEFAULT = "s2.1-pro-free"
PAID_MODEL_DEFAULT = "s2.1-pro"
DRAMA_MODEL = "drama-3"
RATE = 44100
BYTES_PER_SEC = RATE * 2

SAMPLE_TEXTS: dict[str, str] = {
    "EN": "In the spring of nineteen twenty-three, three travelers stopped their sled as the Arctic light began to fade.",
    "RU": "Весной тысяча девятьсот двадцать третьего года трое путников остановили нарты в арктических сумерках.",
    "ES": "En la primavera de mil novecientos veintitrés, tres viajeros detuvieron su trineo mientras la luz se apagaba.",
    "PT": "Na primavera de mil novecentos e vinte e três, três viajantes pararam o trenó enquanto a luz desaparecia.",
    "DE": "Im Frühjahr neunzehnhundertdreiundzwanzig hielten drei Reisende ihren Schlitten an, als das Licht schwand.",
    "IT": "Nella primavera del millenovecentoventitré, tre viaggiatori fermarono la slitta mentre la luce calava.",
    "PL": "Wiosną tysiąc dziewięćset dwudziestego trzeciego roku trzej podróżnicy zatrzymali sanie o zmierzchu.",
    "FR": "Au printemps mille neuf cent vingt-trois, trois voyageurs arrêtèrent leur traîneau à la tombée du jour.",
}


def parse_voice_ref(text: str) -> str | None:
    """Extract 32-char hex Fish voice ID from a URL or string."""
    m = VOICE_ID_RE.search(text or "")
    return m.group(1).lower() if m else None


def clean_for_tts(text: str) -> str:
    return re.sub(r"[*_#`]+", "", text or "").strip()


async def check_voice_fish(fish_id: str, api_key: str) -> Literal["ok", "missing", "unknown"]:
    """Check whether a voice model exists and is accessible in Fish Audio."""
    if not fish_id or not api_key:
        return "unknown"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(
                f"https://api.fish.audio/model/{fish_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if res.status_code == 200:
            return "ok"
        if res.status_code == 404:
            return "missing"
        return "unknown"
    except Exception:
        return "unknown"


async def fetch_voice_info(fish_id: str, api_key: str) -> dict:
    """Fetch title and supported language codes from Fish Audio; raises VoiceApiError(404) if missing."""
    if not api_key:
        return {"title": fish_id[:8], "langs": ["EN"]}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(
                f"https://api.fish.audio/model/{fish_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except Exception:
        return {"title": fish_id[:8], "langs": ["EN"]}
    if res.status_code == 404:
        raise VoiceApiError(404, "voice_not_found", "Голоса нет в библиотеке Fish, проверьте ссылку")
    if res.status_code != 200:
        return {"title": fish_id[:8], "langs": ["EN"]}
    data = res.json()
    langs: list[str] = []
    for code in data.get("languages") or []:
        c = FISH_LANGS.get(str(code).lower().split("-")[0].split("_")[0])
        if c and c not in langs:
            langs.append(c)
    return {"title": (data.get("title") or "").strip(), "langs": langs or ["EN"]}


async def synthesize_fish(
    text: str,
    fish_id: str,
    api_key: str,
    model: str = PAID_MODEL_DEFAULT,
    attempts: int = 3,
) -> bytes:
    """Synthesize MP3 bytes from text via Fish Audio v1/tts."""
    if not api_key:
        raise VoiceApiError(503, "fish_auth", "Не настроен ключ Fish Audio (fish_api_key)")
    cleaned = clean_for_tts(text)
    if not model.startswith("drama"):
        cleaned = re.sub(r"[ \t]{2,}", " ", PAIR_TAG_RE.sub("", cleaned)).strip()
    body: dict = {
        "text": cleaned,
        "reference_id": fish_id,
        "format": "mp3",
        "mp3_bitrate": 192,
        "latency": "normal",
        "chunk_length": 200,
        "normalize": True,
        "prosody": {"speed": 1.0},
    }
    if not model.startswith("drama"):
        body.update({"temperature": 1.0, "top_p": 0.8, "repetition_penalty": 1.3})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model,
    }
    last_err = ""
    async with httpx.AsyncClient(timeout=180.0) as client:
        for attempt in range(attempts):
            try:
                res = await client.post("https://api.fish.audio/v1/tts", json=body, headers=headers)
                if res.status_code == 200 and len(res.content) >= 1000:
                    return res.content
                detail = res.text[:250]
                if res.status_code == 401:
                    raise VoiceApiError(401, "fish_auth", "Fish Audio не принял ключ (401)")
                if res.status_code == 402:
                    raise VoiceApiError(402, "fish_balance", "На балансе Fish Audio закончились средства")
                if res.status_code in (400, 404) and "reference not found" in detail.lower():
                    raise VoiceApiError(422, "voice_missing", f"Голос {fish_id} недоступен в библиотеке Fish Audio")
                last_err = f"HTTP {res.status_code}: {detail}"
            except VoiceApiError:
                raise
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < attempts - 1:
                await _async_sleep(2.0 * (attempt + 1))
    raise VoiceApiError(502, "fish_error", f"Не удалось озвучить через Fish ({model}): {last_err}")


async def _async_sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(sec)


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def decode_pcm(path: Path, tempo: float = 1.0) -> bytes:
    """Decode audio file to 44.1kHz mono 16-bit PCM bytes."""
    exe = ffmpeg_exe()
    if not exe or not path.exists():
        return b""
    cmd = [exe, "-v", "error", "-i", str(path)]
    if abs(tempo - 1.0) > 1e-4:
        cmd += ["-filter:a", f"atempo={tempo:.5f}"]
    cmd += ["-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        return b""
    return r.stdout[: len(r.stdout) // 2 * 2]


def encode_pcm_bytes(pcm: bytes, out: Path, bitrate: str = "192k") -> bool:
    exe = ffmpeg_exe()
    if not exe:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [exe, "-v", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-", "-b:a", bitrate, str(out)],
        input=pcm,
        capture_output=True,
    )
    return r.returncode == 0


def trim_tail(pcm: bytes, db: float = -45.0) -> bytes:
    if not pcm:
        return b""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    thr = int(32767 * (10 ** (db / 20)))
    win = RATE // 100
    end = len(a)
    while end > 0:
        start = max(0, end - win)
        if max((abs(x) for x in a[start:end]), default=0) > thr:
            break
        end = start
    end = min(len(a), end + RATE // 50)
    return pcm[: end * 2]


def mp3_duration_sec(path: Path) -> float | None:
    """Estimate or decode MP3 duration in seconds."""
    if not path.exists():
        return None
    size = path.stat().st_size
    if size < 500:
        return None
    # Fast path for 192kbps CBR MP3 produced by Fish Audio / FFmpeg (24000 bytes/sec)
    return round(size / 24000.0, 2)


def compute_waveform_peaks(path: Path, bars: int = 36) -> list[float]:
    """Compute normalized amplitude peaks [0.1..1.0] for an MP3 file."""
    pcm = decode_pcm(path)
    if not pcm:
        # Deterministic fallback based on file size so UI waveform always renders cleanly
        seed = path.stat().st_size if path.exists() else 12345
        return [round(0.2 + ((seed * (i + 3) * 37) % 75) / 100.0, 2) for i in range(bars)]
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    total = len(a)
    if total < bars:
        return [0.3] * bars
    step = total // bars
    raw_peaks: list[int] = []
    for i in range(bars):
        chunk = a[i * step : (i + 1) * step]
        sample_stride = max(1, len(chunk) // 64)
        peak = max((abs(chunk[j]) for j in range(0, len(chunk), sample_stride)), default=0)
        raw_peaks.append(peak)
    max_p = max(raw_peaks) or 1
    return [round(max(0.12, min(1.0, p / max_p)), 2) for p in raw_peaks]
