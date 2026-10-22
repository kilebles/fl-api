"""Jobs and their result files in the database."""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database
from src.jobs.models import JobProgress, JobRequest, JobRow, JobState, JobStatus
from src.jobs.modes import MODES, Mode, Strategy
from src.storage.objects import ObjectStore, StoredObject
from src.storage.zip_stream import natural_key

_UUID_STEM_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200))
    project_id: Mapped[str | None] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    total: Mapped[int] = mapped_column(Integer)
    video_model_key: Mapped[str | None] = mapped_column(String(100))
    avatar_key: Mapped[str | None] = mapped_column(String(300))
    static_image_key: Mapped[str | None] = mapped_column(String(300))
    pass_num: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def job_rows(self) -> list[JobRow]:
        return [JobRow.model_validate(row) for row in self.rows]


class JobFileRecord(Base):
    """One result of a row: an image or video in the bucket, or a skip with its reason."""

    __tablename__ = "job_files"
    __table_args__ = (UniqueConstraint("job_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    row: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(8))
    key: Mapped[str | None] = mapped_column(String(400))
    size: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


KIND_BY_SUFFIX = {".jpg": "image", ".mp4": "video", ".skip": "skip"}


class JobRepository:
    def __init__(self, db: Database) -> None:
        self._session = db.session

    async def create(self, job_id: str, request: JobRequest) -> JobRecord:
        record = JobRecord(
            id=job_id,
            title=request.title,
            project_id=request.project_id,
            mode=request.mode.value,
            strategy=request.strategy.value,
            state=JobState.QUEUED.value,
            rows=[row.model_dump() for row in request.rows],
            total=len(request.rows),
            video_model_key=request.video_model_key,
            avatar_key=request.avatar,
            static_image_key=request.static_image,
            pass_num=0,
            created_at=datetime.now(),
        )
        async with self._session() as session:
            session.add(record)
            await session.commit()
        return record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._session() as session:
            return await session.get(JobRecord, job_id)

    async def recent(self, limit: int = 100, project_id: str | None = None) -> list[JobRecord]:
        query = select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)
        if project_id is not None:
            query = query.where(JobRecord.project_id == project_id)
        async with self._session() as session:
            return list(await session.scalars(query))

    async def next_queued(self) -> JobRecord | None:
        async with self._session() as session:
            return await session.scalar(
                select(JobRecord).where(JobRecord.state == JobState.QUEUED.value).order_by(JobRecord.created_at).limit(1)
            )

    async def queued_ids(self) -> list[str]:
        async with self._session() as session:
            result = await session.scalars(
                select(JobRecord.id).where(JobRecord.state == JobState.QUEUED.value).order_by(JobRecord.created_at)
            )
            return list(result)

    async def update(self, job_id: str, **fields: Any) -> None:
        async with self._session() as session:
            await session.execute(update(JobRecord).where(JobRecord.id == job_id).values(**fields))
            await session.commit()

    async def requeue_interrupted(self) -> int:
        """Jobs left running by a crash or restart go back to the queue."""
        async with self._session() as session:
            result = await session.execute(
                update(JobRecord)
                .where(JobRecord.state.in_([JobState.RUNNING.value, JobState.WAITING.value]))
                .values(state=JobState.QUEUED.value, message="interrupted by a restart")
            )
            await session.commit()
            return result.rowcount or 0

    async def delete(self, job_id: str) -> None:
        async with self._session() as session:
            await session.execute(delete(JobFileRecord).where(JobFileRecord.job_id == job_id))
            await session.execute(delete(JobRecord).where(JobRecord.id == job_id))
            await session.commit()

    async def add_file(self, job_id: str, name: str, key: str | None, size: int = 0, reason: str | None = None) -> None:
        path = Path(name)
        record = JobFileRecord(
            job_id=job_id,
            name=name,
            row=path.stem,
            kind=KIND_BY_SUFFIX[path.suffix],
            key=key,
            size=size,
            reason=reason,
            created_at=datetime.now(),
        )
        async with self._session() as session:
            await session.execute(delete(JobFileRecord).where(JobFileRecord.job_id == job_id, JobFileRecord.name == name))
            session.add(record)
            await session.commit()

    async def files(self, job_id: str) -> list[JobFileRecord]:
        async with self._session() as session:
            result = await session.scalars(
                select(JobFileRecord).where(JobFileRecord.job_id == job_id).order_by(JobFileRecord.id)
            )
            return list(result)

    async def kind_counts(self, job_ids: list[str]) -> dict[str, Counter[str]]:
        counts: dict[str, Counter[str]] = {job_id: Counter() for job_id in job_ids}
        if not job_ids:
            return counts
        async with self._session() as session:
            result = await session.execute(
                select(JobFileRecord.job_id, JobFileRecord.kind, func.count())
                .where(JobFileRecord.job_id.in_(job_ids))
                .group_by(JobFileRecord.job_id, JobFileRecord.kind)
            )
            for job_id, kind, count in result:
                counts[job_id][kind] = count
        return counts

    async def status(self, record: JobRecord) -> JobStatus:
        counts = await self.kind_counts([record.id])
        return to_status(record, counts[record.id])

    async def statuses(self, records: list[JobRecord]) -> list[JobStatus]:
        counts = await self.kind_counts([r.id for r in records])
        return [to_status(r, counts[r.id]) for r in records]

    async def sync_from_store(self, objects: ObjectStore) -> int:
        """Restore jobs and result files present in `objects` (`jobs/{job_id}/…`) that are missing from SQLite."""
        try:
            stored = await objects.list_objects("jobs/")
        except Exception as e:
            logger.warning(f"[repo] could not list jobs from object store: {e}")
            return 0
        if not stored:
            return 0

        files_by_job: dict[str, list[tuple[str, str, str, StoredObject]]] = defaultdict(list)
        meta_by_job: dict[str, str] = {}
        for obj in stored:
            parts = obj.key.split("/")
            if len(parts) != 3 or parts[0] != "jobs":
                continue
            _, job_id, filename = parts
            if filename == "meta.json":
                meta_by_job[job_id] = obj.key
                continue
            path = Path(filename)
            kind = KIND_BY_SUFFIX.get(path.suffix)
            if not kind or _UUID_STEM_RE.match(path.stem):
                continue
            files_by_job[job_id].append((filename, path.stem, kind, obj))

        async with self._session() as session:
            existing_jobs = {r.id: r for r in await session.scalars(select(JobRecord))}
            existing_files = {
                (jid, name)
                for jid, name in (await session.execute(select(JobFileRecord.job_id, JobFileRecord.name))).all()
            }

            restored_jobs = 0
            restored_files = 0
            all_job_ids = sorted(set(files_by_job) | set(meta_by_job))

            for job_id in all_job_ids:
                jfiles = sorted(files_by_job.get(job_id, []), key=lambda x: (natural_key(x[1]), x[0]))
                row_names = sorted({stem for _, stem, _, _ in jfiles}, key=natural_key)
                kinds = {kind for _, _, kind, _ in jfiles}
                videos_done = sum(1 for _, _, kind, _ in jfiles if kind == "video")
                images_done = sum(1 for _, _, kind, _ in jfiles if kind == "image")
                skipped = sum(1 for _, _, kind, _ in jfiles if kind == "skip")

                if job_id not in existing_jobs:
                    meta: dict[str, Any] | None = None
                    if job_id in meta_by_job:
                        try:
                            meta = json.loads((await objects.get_bytes(meta_by_job[job_id])).decode("utf-8"))
                        except Exception as e:
                            logger.warning(f"[repo] could not read {meta_by_job[job_id]}: {e}")

                    if meta:
                        mode = Mode(meta.get("mode", Mode.I2V.value))
                        rows = meta.get("rows") or [{"name": r, "prompt": f"Сцена {r}", "image_prompt": f"Кадр {r}"} for r in row_names]
                        title = meta.get("title")
                        project_id = meta.get("project_id")
                        strategy = meta.get("strategy", Strategy.PARALLEL.value)
                        video_model_key = meta.get("video_model_key")
                        avatar_key = meta.get("avatar_key")
                        static_image_key = meta.get("static_image_key")
                        created_raw = meta.get("created_at")
                        created_at = datetime.fromisoformat(created_raw) if isinstance(created_raw, str) else datetime.now()
                    else:
                        if not jfiles:
                            continue
                        if "image" in kinds and "video" in kinds:
                            mode = Mode.I2V
                            rows = [{"name": r, "prompt": f"Сцена {r}", "image_prompt": f"Кадр {r}"} for r in row_names]
                        elif "image" in kinds:
                            mode = Mode.IMG
                            rows = [{"name": r, "prompt": None, "image_prompt": f"Кадр {r}"} for r in row_names]
                        else:
                            mode = Mode.T2V
                            rows = [{"name": r, "prompt": f"Сцена {r}", "image_prompt": None} for r in row_names]
                        created_at = min(obj.last_modified for _, _, _, obj in jfiles)
                        title = (
                            f"Пакет {created_at.strftime('%d.%m %H:%M')} ({len(row_names)} сцен)"
                            if len(row_names) > 1
                            else f"Видео {job_id[:6]}"
                        )
                        project_id = None
                        strategy = Strategy.PARALLEL.value
                        video_model_key = None
                        avatar_key = None
                        static_image_key = None

                    finished_at = max((obj.last_modified for _, _, _, obj in jfiles), default=created_at)
                    total = len(rows)
                    final_done = videos_done if MODES[mode].makes_videos else images_done
                    state = (
                        JobState.DONE.value
                        if (total > 0 and (final_done + skipped >= total or final_done >= int(total * 0.9)))
                        else JobState.STOPPED.value
                    )

                    record = JobRecord(
                        id=job_id,
                        title=title,
                        project_id=project_id,
                        mode=mode.value,
                        strategy=strategy,
                        state=state,
                        rows=rows,
                        total=total,
                        video_model_key=video_model_key,
                        avatar_key=avatar_key,
                        static_image_key=static_image_key,
                        pass_num=1,
                        created_at=created_at,
                        started_at=created_at,
                        finished_at=finished_at,
                    )
                    session.add(record)
                    existing_jobs[job_id] = record
                    restored_jobs += 1
                else:
                    record = existing_jobs[job_id]
                    mode = Mode(record.mode)
                    known_rows = {r.get("name") for r in (record.rows or [])}
                    missing_rows = [r for r in row_names if r not in known_rows]
                    if missing_rows:
                        new_rows = list(record.rows or [])
                        for r in missing_rows:
                            new_rows.append(
                                {
                                    "name": r,
                                    "prompt": f"Сцена {r}" if MODES[mode].needs_video_prompt else None,
                                    "image_prompt": f"Кадр {r}" if MODES[mode].needs_image_prompt else None,
                                }
                            )
                        new_rows.sort(key=lambda item: natural_key(str(item.get("name", ""))))
                        record.rows = new_rows
                        record.total = len(new_rows)
                    final_done = videos_done if MODES[mode].makes_videos else images_done
                    db_skipped = sum(1 for (jid, fname) in existing_files if jid == job_id and fname.endswith(".skip"))
                    total_skipped = max(skipped, db_skipped)
                    if record.total > 0 and (final_done + total_skipped >= record.total or final_done >= int(record.total * 0.9)):
                        record.state = JobState.DONE.value
                        record.message = None
                        record.finished_at = max((obj.last_modified for _, _, _, obj in jfiles), default=record.finished_at or record.created_at)

                for filename, stem, kind, obj in jfiles:
                    if (job_id, filename) in existing_files:
                        continue
                    session.add(
                        JobFileRecord(
                            job_id=job_id,
                            name=filename,
                            row=stem,
                            kind=kind,
                            key=None if kind == "skip" else obj.key,
                            size=obj.size,
                            reason=None,
                            created_at=obj.last_modified,
                        )
                    )
                    existing_files.add((job_id, filename))
                    restored_files += 1

            if restored_jobs or restored_files:
                await session.commit()
                logger.info(f"[repo] synced from object store: {restored_jobs} job(s), {restored_files} file(s)")
            return restored_jobs


def to_status(record: JobRecord, counts: Counter[str]) -> JobStatus:
    mode = MODES[Mode(record.mode)]
    final = counts["video"] if mode.makes_videos else counts["image"]
    return JobStatus(
        id=record.id,
        title=record.title,
        project_id=record.project_id,
        mode=Mode(record.mode),
        strategy=Strategy(record.strategy),
        state=JobState(record.state),
        progress=JobProgress(
            total=record.total,
            images_done=counts["image"],
            videos_done=counts["video"],
            skipped=counts["skip"],
            remaining=max(0, record.total - final - counts["skip"]),
            pass_num=record.pass_num,
        ),
        message=record.message,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
