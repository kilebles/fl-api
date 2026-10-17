"""Avatars: a character kept the same across generations (reference pictures + appearance text)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database


class AvatarRecord(Base):
    __tablename__ = "avatars"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    appearance: Mapped[str] = mapped_column(Text, default="")
    image_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AvatarNotFoundError(LookupError):
    pass


class AvatarService:
    def __init__(self, db: Database) -> None:
        self._session = db.session

    async def all(self) -> list[AvatarRecord]:
        async with self._session() as session:
            return list(await session.scalars(select(AvatarRecord).order_by(AvatarRecord.created_at.desc())))

    async def get(self, avatar_id: str) -> AvatarRecord:
        async with self._session() as session:
            record = await session.get(AvatarRecord, avatar_id)
        if record is None:
            raise AvatarNotFoundError(f"avatar {avatar_id}")
        return record

    async def create(self, name: str, appearance: str, image_keys: list[str]) -> AvatarRecord:
        record = AvatarRecord(
            id=uuid.uuid4().hex[:12], name=name.strip() or "Без имени", appearance=appearance.strip(), image_keys=image_keys, created_at=datetime.now()
        )
        async with self._session() as session:
            session.add(record)
            await session.commit()
        return record

    async def update(self, avatar_id: str, **values: Any) -> AvatarRecord:
        await self.get(avatar_id)
        values = {k: v for k, v in values.items() if v is not None}
        if values:
            async with self._session() as session:
                await session.execute(update(AvatarRecord).where(AvatarRecord.id == avatar_id).values(**values))
                await session.commit()
        return await self.get(avatar_id)

    async def delete(self, avatar_id: str) -> None:
        await self.get(avatar_id)
        async with self._session() as session:
            await session.execute(delete(AvatarRecord).where(AvatarRecord.id == avatar_id))
            await session.commit()
