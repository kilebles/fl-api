"""Jobs and their result files in the database."""

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

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
