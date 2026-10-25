"""Bridge to fl-voice_translator: its queue and files on disk, its `tools/api.py` commands for changes.

The translator's worker (worker.py) runs the jobs (translation with length fitting, Fish voiceover, mixing);
the web panel puts jobs into the same queue and reads progress back from `queue/*.json`.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.voice.models import VoiceApiError

JOB_FILE_RE = re.compile(r"^(\d{4})\.json$")
ALIVE_SEC = 30  # the worker touches queue/worker.alive every 2 s

ERROR_STATUS = {
    "script_not_found": 404,
    "job_not_found": 404,
    "paragraph_not_found": 404,
    "profile_not_found": 404,
    "invalid_docx": 422,
    "unknown_language": 422,
    "unknown_engine": 422,
    "unknown_mode": 422,
    "empty_text": 422,
    "voice_missing": 422,
    "bad_request": 400,
    "no_translation": 409,
    "not_stoppable": 409,
    "nothing_to_revoice": 409,
    "limit": 429,
    "translate_failed": 502,
    "mix_failed": 502,
}


def find_root(configured: Path) -> Path:
    """Translator folder: VOICE_DIR when it holds the translator, else a known folder in the home directory."""
    if (configured / "jobs.py").exists() or configured != Path("work/voice"):
        return configured.resolve()
    home = Path.home()
    cands = [home / "fl-voice_translator", home / "script-translator", Path("C:/fl-voice_translator")]
    cands += sorted(home.glob("script-translator*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in cands:
        if (c / "tools" / "api.py").exists():
            return c.resolve()
    for c in cands:
        if (c / "jobs.py").exists():
            return c.resolve()
    return configured.resolve()


def parse_short_time(val: str | None) -> str | None:
    """'24.10 15:30' (translator format, local time) → ISO 8601."""
    if not val:
        return None
    try:
        now = datetime.now()
        dt = datetime.strptime(f"{now.year}.{val}", "%Y.%d.%m %H:%M")
        if dt > now.replace(microsecond=0) and (dt - now).days > 180:
            dt = dt.replace(year=now.year - 1)
        return dt.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


class Translator:
    """Paths of the translator installation plus async calls to `tools/api.py`."""

    def __init__(self, root: Path, python: str = "") -> None:
        self.root = root
        self.tools_dir = root / "tools"
        self.work_dir = root / "work"
        self.input_dir = root / "input"
        self.output_dir = root / "output"
        self.audio_dir = self.output_dir / "audio"
        self.mix_dir = self.output_dir / "mix"
        self.queue_dir = root / "queue"
        self._python = python
        self._config_cache: tuple | None = None

    # ------------------------------------------------------------ setup ---

    @property
    def available(self) -> bool:
        return (self.tools_dir / "api.py").exists()

    def python(self) -> str:
        if self._python:
            return self._python
        for c in (self.root / ".venv" / "Scripts" / "python.exe", self.root / ".venv" / "bin" / "python"):
            if c.exists():
                return str(c)
        return sys.executable

    def require(self) -> None:
        if not self.available:
            if (self.root / "jobs.py").exists():
                raise VoiceApiError(
                    503, "translator_outdated", "Переводчик устарел: обновите его (git pull в папке переводчика)"
                )
            raise VoiceApiError(503, "translator_missing", f"Переводчик не найден в {self.root} (VOICE_DIR в .env)")

    def config(self) -> dict[str, Any]:
        """config.json with config.local.json on top (same merge as the translator)."""

        def merge(base: dict, over: dict) -> dict:
            out = dict(base)
            for k, v in over.items():
                out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
            return out

        files = [self.tools_dir / "config.json", self.tools_dir / "config.local.json"]
        key = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
        if self._config_cache and self._config_cache[0] == key:
            return self._config_cache[1]
        cfg: dict[str, Any] = {}
        for f in files:
            if f.exists():
                try:
                    cfg = merge(cfg, json.loads(f.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
        self._config_cache = (key, cfg)
        return cfg

    def worker_online(self) -> bool:
        f = self.queue_dir / "worker.alive"
        try:
            return time.time() - f.stat().st_mtime < ALIVE_SEC
        except OSError:
            return False

    # ------------------------------------------------------------ calls ---

    async def call(self, *args: str, stdin: str = "", timeout: float = 120) -> dict[str, Any]:
        """Run `tools/api.py <args>`; its JSON answer, or VoiceApiError with its error code."""
        self.require()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        proc = await asyncio.create_subprocess_exec(
            self.python(),
            str(self.tools_dir / "api.py"),
            *args,
            cwd=str(self.root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), timeout)
        except TimeoutError:
            proc.kill()
            raise VoiceApiError(504, "translator_timeout", f"Переводчик не ответил за {int(timeout)} с") from None
        text = out.decode("utf-8", "replace").strip()
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if isinstance(data, dict) and "error" in data:
            e = data["error"]
            raise VoiceApiError(ERROR_STATUS.get(e.get("code", ""), 502), e.get("code", "translator_error"), e.get("message", ""))
        if proc.returncode != 0 or not isinstance(data, dict):
            tail = err.decode("utf-8", "replace").strip().splitlines()[-6:]
            logger.warning(f"[voice] translator {args[0]} failed ({proc.returncode}): {' | '.join(tail)}")
            raise VoiceApiError(502, "translator_error", "Ошибка переводчика: " + (tail[-1] if tail else f"код {proc.returncode}"))
        return data

    # ------------------------------------------------------------ queue ---

    def jobs(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        if not self.queue_dir.exists():
            return out
        for f in self.queue_dir.iterdir():
            m = JOB_FILE_RE.match(f.name)
            if not m:
                continue
            for _ in range(3):
                try:
                    out[int(m.group(1))] = json.loads(f.read_text(encoding="utf-8"))
                    break
                except FileNotFoundError:
                    break
                except (ValueError, PermissionError, OSError):
                    time.sleep(0.05)
        return out

    def events(self, job_id: int) -> list[dict[str, Any]]:
        f = self.queue_dir / f"{job_id:04d}.events.jsonl"
        out: list[dict[str, Any]] = []
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        return out

    # ------------------------------------------------------------ files ---

    def source(self, slug: str) -> dict[str, Any] | None:
        f = self.work_dir / slug / "source.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def stats(self, slug: str, lang: str) -> dict[str, Any] | None:
        f = self.work_dir / slug / f"{lang}.stats.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def slug_by_docx(self) -> dict[str, str]:
        """{docx stem: script folder} for every prepared script (bot jobs only carry the docx path)."""
        out: dict[str, str] = {}
        if not self.work_dir.exists():
            return out
        for d in self.work_dir.iterdir():
            data = self.source(d.name) if d.is_dir() else None
            if data and data.get("docx"):
                out.setdefault(Path(str(data["docx"]).replace("\\", "/")).stem, d.name)
        return out
