"""Infographic runs: scenario .docx → subjects with Wikimedia images → one animated card per subject.

Both stages run as subprocesses — the subject search is blocking code with long
throttles, the render is Node + Chrome — and report progress with PROGRESS lines in
the run's log. Runs go one at a time (rendering takes every CPU core).
"""

import asyncio
import json
import re
import shutil
import sys
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    delete,
    select,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database
from src.core.settings import Settings
from src.storage.objects import ObjectStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PROGRESS = re.compile(r"PROGRESS (images|render) (\d+)/(\d+)(?: found=(\d+))?")
POLL_SECONDS = 3.0


class InfographicState(StrEnum):
    QUEUED = "queued"
    SEARCHING = "searching"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


ACTIVE_STATES = (InfographicState.QUEUED.value, InfographicState.SEARCHING.value, InfographicState.RENDERING.value)


class InfographicRunRecord(Base):
    __tablename__ = "infographic_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(32), index=True)
    scenario_name: Mapped[str] = mapped_column(String(300))
    scenario_key: Mapped[str] = mapped_column(String(400))
    categories: Mapped[list[str]] = mapped_column(JSON)
    one_per_person: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), index=True)
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    found: Mapped[int] = mapped_column(Integer, default=0)
    entries: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotFoundError(LookupError):
    pass


class StageError(RuntimeError):
    pass


def _key(run_id: str, name: str) -> str:
    return f"infographics/{run_id}/{name}"


def _entries(scenario_json: Path) -> list[dict[str, Any]]:
    """One card per subject that has an image, keyed by the image stem (`<paragraph>.<n>`)."""
    cards = []
    for entry in json.loads(scenario_json.read_text(encoding="utf-8")):
        images = entry.get("images") or []
        if not images:
            continue
        image = images[0]["file"]
        cards.append({
            "id": Path(image).stem,
            "name": entry["personality"]["name"],
            "years": entry["personality"].get("years") or "",
            "category": entry.get("category", "people"),
            "paragraph_number": entry["paragraph_number"],
            "paragraph": entry["paragraph"],
            "image_file": image,
            "image_key": None,
            "video_key": None,
        })
    return cards


class InfographicsService:
    def __init__(self, db: Database, objects: ObjectStore, settings: Settings) -> None:
        self._session = db.session
        self._objects = objects
        self._work_dir = (settings.work_dir / "infographics").resolve()
        self._renderer_dir = settings.renderer_dir.resolve()
        self._concurrency = settings.render_concurrency
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._current: tuple[str, asyncio.Task] | None = None

    async def start(self) -> None:
        async with self._session() as session:
            await session.execute(
                update(InfographicRunRecord)
                .where(InfographicRunRecord.state.in_(ACTIVE_STATES[1:]))
                .values(state=InfographicState.QUEUED.value)
            )
            await session.commit()
        self._worker = asyncio.create_task(self._run_forever(), name="infographics")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    async def runs(self, limit: int = 100, project_id: str | None = None) -> list[InfographicRunRecord]:
        query = select(InfographicRunRecord).order_by(InfographicRunRecord.created_at.desc()).limit(limit)
        if project_id is not None:
            query = query.where(InfographicRunRecord.project_id == project_id)
        async with self._session() as session:
            return list(await session.scalars(query))

    async def run(self, run_id: str) -> InfographicRunRecord:
        async with self._session() as session:
            record = await session.get(InfographicRunRecord, run_id)
        if record is None:
            raise NotFoundError(f"infographic run {run_id}")
        return record

    async def create(
        self, scenario_name: str, content: bytes, categories: list[str], one_per_person: bool, project_id: str | None = None
    ) -> InfographicRunRecord:
        run_id = uuid.uuid4().hex[:12]
        scenario_key = _key(run_id, "scenario.docx")
        await self._objects.put(content, scenario_key, "scenario.docx")
        record = InfographicRunRecord(
            id=run_id,
            project_id=project_id,
            scenario_name=scenario_name,
            scenario_key=scenario_key,
            categories=categories,
            one_per_person=one_per_person,
            state=InfographicState.QUEUED.value,
            entries=[],
            created_at=datetime.now(),
        )
        async with self._session() as session:
            session.add(record)
            await session.commit()
        self._wake.set()
        return record

    async def retry(self, run_id: str) -> bool:
        record = await self.run(run_id)
        if record.state != InfographicState.FAILED.value:
            return False
        await self._set(run_id, state=InfographicState.QUEUED.value, error=None, finished_at=None)
        self._wake.set()
        return True

    async def delete(self, run_id: str) -> None:
        await self.run(run_id)
        if self._current and self._current[0] == run_id:
            task = self._current[1]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._objects.delete_prefix(f"infographics/{run_id}/")
        async with self._session() as session:
            await session.execute(delete(InfographicRunRecord).where(InfographicRunRecord.id == run_id))
            await session.commit()
        shutil.rmtree(self._work_dir / run_id, ignore_errors=True)

    async def _set(self, run_id: str, **values: Any) -> None:
        async with self._session() as session:
            await session.execute(update(InfographicRunRecord).where(InfographicRunRecord.id == run_id).values(**values))
            await session.commit()

    async def _next(self) -> str | None:
        async with self._session() as session:
            return await session.scalar(
                select(InfographicRunRecord.id)
                .where(InfographicRunRecord.state == InfographicState.QUEUED.value)
                .order_by(InfographicRunRecord.created_at)
                .limit(1)
            )

    async def _run_forever(self) -> None:
        while True:
            run_id = await self._next()
            if run_id is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            task = asyncio.create_task(self._execute(run_id), name=f"infographic_{run_id}")
            self._current = (run_id, task)
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            finally:
                self._current = None

    async def _execute(self, run_id: str) -> None:
        record = await self.run(run_id)
        work = self._work_dir / run_id
        scenario_dir, videos_dir = work / "scenario", work / "videos"
        log_path = work / "log.txt"
        docx = work / "scenario.docx"
        work.mkdir(parents=True, exist_ok=True)
        videos_dir.mkdir(exist_ok=True)
        scenario_json = scenario_dir / "scenario.json"
        try:
            if not docx.exists():
                await self._objects.download(record.scenario_key, docx)

            if not scenario_json.exists():
                await self._set(run_id, state=InfographicState.SEARCHING.value, done=0, total=0, found=0)
                args = [sys.executable, "-m", "src.infographics.main", str(docx), "--output-dir", str(scenario_dir), "--categories", ",".join(record.categories)]
                if record.one_per_person:
                    args.append("--one-per-person")
                await self._stage(run_id, "search", args, PROJECT_ROOT, log_path)

            raw_subjects = json.loads(scenario_json.read_text(encoding="utf-8"))
            entries = _entries(scenario_json)
            if raw_subjects and not entries:
                scenario_json.unlink(missing_ok=True)
                raise StageError(f"0 of {len(raw_subjects)} detected subjects got an image — see {log_path}")
            stored = {e["id"]: e for e in (await self.run(run_id)).entries}
            for entry in entries:
                previous = stored.get(entry["id"], {})
                entry["image_key"] = previous.get("image_key")
                entry["video_key"] = previous.get("video_key")
                if entry["image_key"] is None:
                    entry["image_key"] = _key(run_id, f"images/{entry['image_file']}")
                    await self._objects.upload(scenario_dir / entry["image_file"], entry["image_key"])
            await self._set(run_id, state=InfographicState.RENDERING.value, entries=entries, done=0, total=len(entries))

            plan = {
                "imagesDir": str(scenario_dir),
                "concurrency": self._concurrency,
                "items": [
                    {"image": e["image_file"], "props": {"name": e["name"], "years": e["years"]}, "output": str(videos_dir / f"{e['id']}.mp4")}
                    for e in entries
                    if e["video_key"] is None
                ],
            }
            plan_path = work / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            if plan["items"]:
                await self._stage(run_id, "render", ["node", str(self._renderer_dir / "render.mjs"), str(plan_path)], self._renderer_dir, log_path, entries, videos_dir)
            await self._upload_videos(run_id, entries, videos_dir)

            missing = sum(1 for e in entries if e["video_key"] is None)
            if missing:
                raise StageError(f"{missing} of {len(entries)} cards failed to render — see {log_path}")
            await self._set(run_id, state=InfographicState.DONE.value, done=len(entries), finished_at=datetime.now())
            shutil.rmtree(work, ignore_errors=True)
            logger.success(f"[infographics] run {run_id}: {len(entries)} cards")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[infographics] run {run_id} failed: {type(e).__name__}: {e}")
            await self._set(run_id, state=InfographicState.FAILED.value, error=f"{e}", finished_at=datetime.now())

    async def _stage(
        self,
        run_id: str,
        stage: str,
        args: list[str],
        cwd: Path,
        log_path: Path,
        entries: list[dict[str, Any]] | None = None,
        videos_dir: Path | None = None,
    ) -> None:
        """Run one stage, mirroring its PROGRESS lines (and, while rendering, finished clips) into the run."""
        with log_path.open("ab") as log:
            log.write(f"=== {stage}: {' '.join(args)}\n".encode())
            log.flush()
            process = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                while True:
                    try:
                        code = await asyncio.wait_for(process.wait(), POLL_SECONDS)
                        break
                    except TimeoutError:
                        await self._progress(run_id, log_path)
                        if entries is not None and videos_dir is not None:
                            await self._upload_videos(run_id, entries, videos_dir)
            except asyncio.CancelledError:
                process.terminate()
                await process.wait()
                raise
        await self._progress(run_id, log_path)
        if code != 0:
            raise StageError(f"{stage} stage exited with code {code} — see {log_path}")

    async def _progress(self, run_id: str, log_path: Path) -> None:
        try:
            with log_path.open("rb") as log:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 65536))
                tail = log.read().decode("utf-8", errors="replace")
        except OSError:
            return
        matches = _PROGRESS.findall(tail)
        if not matches:
            return
        stage, done, total, found = matches[-1]
        # render progress is counted from uploaded clips (see _upload_videos)
        if stage == "images":
            await self._set(run_id, done=int(done), total=int(total), found=int(found or 0))

    async def _upload_videos(self, run_id: str, entries: list[dict[str, Any]], videos_dir: Path) -> None:
        """Move finished clips to the store; the run's entries and `done` follow."""
        changed = False
        for entry in entries:
            path = videos_dir / f"{entry['id']}.mp4"
            if entry["video_key"] is None and path.exists():
                key = _key(run_id, f"videos/{entry['id']}.mp4")
                await self._objects.upload(path, key)
                entry["video_key"] = key
                path.unlink(missing_ok=True)
                changed = True
        if changed:
            await self._set(run_id, entries=entries, done=sum(1 for e in entries if e["video_key"]))
