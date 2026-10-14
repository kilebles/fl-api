"""One async SQLAlchemy engine for every table (SQLite locally, Postgres later — same code)."""

from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create missing tables (and the SQLite file's folder). Models must be imported first."""
        parsed = make_url(self.engine.url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
