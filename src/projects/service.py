"""Projects (ролики): what generations, prompt runs and infographics belong to."""

import json
import uuid
from datetime import datetime

from loguru import logger
from sqlalchemy import DateTime, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database
from src.core.time import parse_naive_utc, utcnow
from src.core.titles import unique_title
from src.storage.objects import ObjectStore


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    title: Mapped[str] = mapped_column(String(300))
    style: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProjectNotFoundError(LookupError):
    pass


def _project_key(project_id: str) -> str:
    return f"projects/{project_id}/project.json"


class ProjectService:
    def __init__(self, db: Database, objects: ObjectStore | None = None) -> None:
        self._session = db.session
        self._objects = objects

    async def _mirror(self, record: ProjectRecord) -> None:
        if self._objects is None:
            return
        payload = {
            "id": record.id,
            "user_id": record.user_id,
            "title": record.title,
            "style": record.style,
            "created_at": f"{record.created_at.isoformat()}Z",
        }
        try:
            await self._objects.put(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                _project_key(record.id),
                "project.json",
            )
        except Exception as e:
            logger.warning(f"[projects] could not mirror project {record.id} to object store: {e}")

    async def _restore_from_store(self, project_id: str) -> ProjectRecord | None:
        if self._objects is None:
            return None
        key = _project_key(project_id)
        try:
            if not await self._objects.exists(key):
                return None
            data = json.loads((await self._objects.get_bytes(key)).decode("utf-8"))
        except Exception as e:
            logger.warning(f"[projects] could not restore project {project_id} from object store: {e}")
            return None
        created_raw = data.get("created_at")
        created_at = parse_naive_utc(created_raw if isinstance(created_raw, str) else None)
        record = ProjectRecord(
            id=str(data.get("id") or project_id),
            user_id=data.get("user_id"),
            title=str(data.get("title") or "Без названия"),
            style=str(data.get("style") or ""),
            created_at=created_at,
        )
        async with self._session() as session:
            await session.merge(record)
            await session.commit()
            return await session.get(ProjectRecord, record.id)

    async def all(self, user_id: str | None = None) -> list[ProjectRecord]:
        query = select(ProjectRecord).order_by(ProjectRecord.created_at.desc())
        if user_id is not None:
            query = query.where(ProjectRecord.user_id == user_id)
        async with self._session() as session:
            return list(await session.scalars(query))

    async def get(self, project_id: str, user_id: str | None = None) -> ProjectRecord:
        async with self._session() as session:
            record = await session.get(ProjectRecord, project_id)
        if record is None:
            record = await self._restore_from_store(project_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            raise ProjectNotFoundError(f"project {project_id}")
        return record

    async def create(self, title: str, style: str = "", user_id: str | None = None) -> ProjectRecord:
        base_title = title.strip() or "Без названия"
        async with self._session() as session:
            existing_query = select(ProjectRecord.title)
            if user_id is not None:
                existing_query = existing_query.where(ProjectRecord.user_id == user_id)
            else:
                existing_query = existing_query.where(ProjectRecord.user_id.is_(None))
            existing_titles = list(await session.scalars(existing_query))
            final_title = unique_title(base_title, existing_titles, max_len=300)
            record = ProjectRecord(
                id=uuid.uuid4().hex[:12],
                user_id=user_id,
                title=final_title,
                style=style,
                created_at=utcnow(),
            )
            session.add(record)
            await session.commit()
        await self._mirror(record)
        return record

    async def update(self, project_id: str, title: str | None, style: str | None, user_id: str | None = None) -> ProjectRecord:
        await self.get(project_id, user_id=user_id)
        values = {k: v for k, v in (("title", title), ("style", style)) if v is not None}
        if values:
            async with self._session() as session:
                await session.execute(update(ProjectRecord).where(ProjectRecord.id == project_id).values(**values))
                await session.commit()
        updated = await self.get(project_id, user_id=user_id)
        await self._mirror(updated)
        return updated

    async def delete(self, project_id: str, user_id: str | None = None) -> None:
        """Delete the project; its generations stay, without a project."""
        await self.get(project_id, user_id=user_id)
        async with self._session() as session:
            await session.execute(delete(ProjectRecord).where(ProjectRecord.id == project_id))
            await session.commit()
        if self._objects is not None:
            try:
                await self._objects.delete_prefix(f"projects/{project_id}/")
            except Exception as e:
                logger.warning(f"[projects] could not remove project {project_id} from object store: {e}")
