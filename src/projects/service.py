"""Projects (ролики): what generations, prompt runs and infographics belong to."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    style: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ProjectNotFoundError(LookupError):
    pass


class ProjectService:
    def __init__(self, db: Database) -> None:
        self._session = db.session

    async def all(self) -> list[ProjectRecord]:
        async with self._session() as session:
            return list(await session.scalars(select(ProjectRecord).order_by(ProjectRecord.created_at.desc())))

    async def get(self, project_id: str) -> ProjectRecord:
        async with self._session() as session:
            record = await session.get(ProjectRecord, project_id)
        if record is None:
            raise ProjectNotFoundError(f"project {project_id}")
        return record

    async def create(self, title: str, style: str = "") -> ProjectRecord:
        record = ProjectRecord(id=uuid.uuid4().hex[:12], title=title.strip() or "Без названия", style=style, created_at=datetime.now())
        async with self._session() as session:
            session.add(record)
            await session.commit()
        return record

    async def update(self, project_id: str, title: str | None, style: str | None) -> ProjectRecord:
        await self.get(project_id)
        values = {k: v for k, v in (("title", title), ("style", style)) if v is not None}
        if values:
            async with self._session() as session:
                await session.execute(update(ProjectRecord).where(ProjectRecord.id == project_id).values(**values))
                await session.commit()
        return await self.get(project_id)

    async def delete(self, project_id: str) -> None:
        """Delete the project; its generations stay, without a project."""
        await self.get(project_id)
        async with self._session() as session:
            await session.execute(delete(ProjectRecord).where(ProjectRecord.id == project_id))
            await session.commit()
