"""Avatars: a character kept the same across generations (reference pictures + appearance text)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database
from src.core.time import utcnow


class AvatarRecord(Base):
    __tablename__ = "avatars"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(200))
    appearance: Mapped[str] = mapped_column(Text, default="")
    image_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    pack_job_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    pack_stage: Mapped[str | None] = mapped_column(String(32), default=None)
    pack_pending: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AvatarNotFoundError(LookupError):
    pass


class AvatarService:
    def __init__(self, db: Database) -> None:
        self._session = db.session

    async def all(self, user_id: str | None = None) -> list[AvatarRecord]:
        query = select(AvatarRecord).order_by(AvatarRecord.created_at.desc())
        if user_id is not None:
            query = query.where(AvatarRecord.user_id == user_id)
        async with self._session() as session:
            return list(await session.scalars(query))

    async def get(self, avatar_id: str, user_id: str | None = None) -> AvatarRecord:
        async with self._session() as session:
            record = await session.get(AvatarRecord, avatar_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            raise AvatarNotFoundError(f"avatar {avatar_id}")
        return record

    async def by_pack_job(self, job_id: str) -> AvatarRecord | None:
        async with self._session() as session:
            return await session.scalar(select(AvatarRecord).where(AvatarRecord.pack_job_id == job_id).limit(1))

    async def create(self, name: str, appearance: str, image_keys: list[str], user_id: str | None = None) -> AvatarRecord:
        record = AvatarRecord(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            name=name.strip() or "Без имени",
            appearance=appearance.strip(),
            image_keys=image_keys,
            created_at=utcnow(),
        )
        async with self._session() as session:
            session.add(record)
            await session.commit()
        return record

    async def update(
        self,
        avatar_id: str,
        user_id: str | None = None,
        *,
        clear_pack: bool = False,
        **values: Any,
    ) -> AvatarRecord:
        await self.get(avatar_id, user_id=user_id)
        values = {k: v for k, v in values.items() if v is not None}
        if clear_pack:
            values["pack_job_id"] = None
            values["pack_stage"] = None
            values["pack_pending"] = None
        if values:
            async with self._session() as session:
                await session.execute(update(AvatarRecord).where(AvatarRecord.id == avatar_id).values(**values))
                await session.commit()
        return await self.get(avatar_id, user_id=user_id)

    async def delete(self, avatar_id: str, user_id: str | None = None) -> None:
        await self.get(avatar_id, user_id=user_id)
        async with self._session() as session:
            await session.execute(delete(AvatarRecord).where(AvatarRecord.id == avatar_id))
            await session.commit()
