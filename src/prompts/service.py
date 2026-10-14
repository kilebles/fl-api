"""Prompt templates and generation runs: stored in the database, runs execute in the background."""

import asyncio
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from loguru import logger
from sqlalchemy import JSON, DateTime, Integer, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database
from src.prompts.generation import generate_pairs, parse_template
from src.prompts.llm import LLMClient
from src.prompts.models import GENERATION_MODELS, GeneratedPair, I2VPrompt


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PromptTemplateRecord(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    @property
    def template(self) -> I2VPrompt:
        return I2VPrompt.model_validate(self.data)


class PromptRunRecord(Base):
    __tablename__ = "prompt_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(32))
    template_title: Mapped[str] = mapped_column(String(300))
    model: Mapped[str] = mapped_column(String(64))
    pair_count: Mapped[int] = mapped_column(Integer)
    scenario_name: Mapped[str] = mapped_column(String(300))
    paragraphs: Mapped[list[str]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), index=True)
    done: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    rows: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def pairs(self) -> list[GeneratedPair]:
        return [GeneratedPair.model_validate(row) for row in self.rows or []]


class NotFoundError(LookupError):
    pass


class PromptService:
    """
    Templates are extracted from briefs once and reused; a run applies a template to a
    scenario. Runs don't touch the browser accounts, so up to `parallel_runs` go at once.
    Queued and interrupted runs start again from their stored paragraphs on `start()`.
    """

    def __init__(self, db: Database, llm: LLMClient, template_model: str, parallel_runs: int = 2, concurrency: int = 6) -> None:
        self._session = db.session
        self._llm = llm
        self._template_model = template_model
        self._slots = asyncio.Semaphore(parallel_runs)
        self._concurrency = concurrency
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        async with self._session() as session:
            result = await session.scalars(
                select(PromptRunRecord.id).where(PromptRunRecord.state.in_([RunState.QUEUED.value, RunState.RUNNING.value]))
            )
            ids = list(result)
        for run_id in ids:
            self._launch(run_id)
        if ids:
            logger.info(f"[prompts] {len(ids)} unfinished run(s) started again")

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Templates

    async def templates(self) -> list[PromptTemplateRecord]:
        async with self._session() as session:
            return list(await session.scalars(select(PromptTemplateRecord).order_by(PromptTemplateRecord.created_at.desc())))

    async def template(self, template_id: str) -> PromptTemplateRecord:
        async with self._session() as session:
            record = await session.get(PromptTemplateRecord, template_id)
        if record is None:
            raise NotFoundError(f"template {template_id}")
        return record

    async def create_template(self, brief: str, title: str) -> PromptTemplateRecord:
        """Extract a template from a brief with the LLM (takes up to a minute)."""
        if not brief.strip():
            raise ValueError("brief is empty")
        template = await parse_template(self._llm, self._template_model, brief, title)
        if not template.pairs:
            raise ValueError("no shot pairs found in the brief")
        return await self.save_template(template)

    async def save_template(self, template: I2VPrompt, template_id: str | None = None) -> PromptTemplateRecord:
        record = PromptTemplateRecord(
            id=template_id or uuid.uuid4().hex,
            title=template.title or "Без названия",
            data=template.model_dump(),
            created_at=datetime.now(),
        )
        async with self._session() as session:
            await session.merge(record)
            await session.commit()
        return record

    async def update_template(self, template_id: str, title: str | None, data: I2VPrompt | None) -> PromptTemplateRecord:
        record = await self.template(template_id)
        template = data or record.template
        if title is not None:
            template.title = title
        values = {"title": template.title or record.title, "data": template.model_dump()}
        async with self._session() as session:
            await session.execute(update(PromptTemplateRecord).where(PromptTemplateRecord.id == template_id).values(**values))
            await session.commit()
        return await self.template(template_id)

    async def delete_template(self, template_id: str) -> None:
        await self.template(template_id)
        async with self._session() as session:
            await session.execute(delete(PromptTemplateRecord).where(PromptTemplateRecord.id == template_id))
            await session.commit()

    # Runs

    async def runs(self, limit: int = 100) -> list[PromptRunRecord]:
        async with self._session() as session:
            return list(await session.scalars(select(PromptRunRecord).order_by(PromptRunRecord.created_at.desc()).limit(limit)))

    async def run(self, run_id: str) -> PromptRunRecord:
        async with self._session() as session:
            record = await session.get(PromptRunRecord, run_id)
        if record is None:
            raise NotFoundError(f"run {run_id}")
        return record

    async def create_run(self, template_id: str, model: str, pair_count: int, scenario_name: str, paragraphs: list[str]) -> PromptRunRecord:
        if model not in {m for m, _ in GENERATION_MODELS}:
            raise ValueError(f"unknown model: {model}")
        if not paragraphs:
            raise ValueError("no paragraphs in the scenario")
        template = await self.template(template_id)
        available = len(template.template.pairs)
        if not 1 <= pair_count <= available:
            raise ValueError(f"pair count must be 1..{available}")
        record = PromptRunRecord(
            id=uuid.uuid4().hex[:12],
            template_id=template_id,
            template_title=template.title,
            model=model,
            pair_count=pair_count,
            scenario_name=scenario_name,
            paragraphs=paragraphs,
            state=RunState.QUEUED.value,
            done=0,
            created_at=datetime.now(),
        )
        async with self._session() as session:
            session.add(record)
            await session.commit()
        self._launch(record.id)
        return record

    async def delete_run(self, run_id: str) -> None:
        await self.run(run_id)
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._session() as session:
            await session.execute(delete(PromptRunRecord).where(PromptRunRecord.id == run_id))
            await session.commit()

    def _launch(self, run_id: str) -> None:
        task = asyncio.create_task(self._execute(run_id), name=f"prompt_run_{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _set(self, run_id: str, **values: Any) -> None:
        async with self._session() as session:
            await session.execute(update(PromptRunRecord).where(PromptRunRecord.id == run_id).values(**values))
            await session.commit()

    async def _execute(self, run_id: str) -> None:
        async with self._slots:
            record = await self.run(run_id)
            await self._set(run_id, state=RunState.RUNNING.value, done=0, error=None)
            logger.info(f"[prompts] run {run_id}: {len(record.paragraphs)} paragraphs × {record.pair_count} pairs, {record.model}")
            try:
                template = (await self.template(record.template_id)).template
                pairs = await generate_pairs(
                    self._llm,
                    record.model,
                    template,
                    template.pairs[: record.pair_count],
                    record.paragraphs,
                    on_progress=lambda done, _total: self._set(run_id, done=done),
                    max_concurrency=self._concurrency,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"[prompts] run {run_id} failed")
                await self._set(run_id, state=RunState.FAILED.value, error=f"{type(e).__name__}: {e}", finished_at=datetime.now())
                return
            await self._set(
                run_id,
                state=RunState.DONE.value,
                done=len(record.paragraphs),
                rows=[p.model_dump() for p in pairs],
                finished_at=datetime.now(),
            )
            logger.success(f"[prompts] run {run_id}: {len(pairs)} prompt pairs")
