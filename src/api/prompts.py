"""/api/prompts: templates from briefs and prompt generation runs over scenarios."""

import asyncio
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from src.prompts.models import GENERATION_MODELS, GeneratedPair, I2VPrompt
from src.prompts.scenario import document_text, parse_scenario
from src.prompts.service import PromptRunRecord, PromptService, PromptTemplateRecord
from src.prompts.xlsx import build_workbook

MAX_DOC_BYTES = 10 * 1024**2

router = APIRouter(prefix="/api/prompts")


def get_prompts(request: Request) -> PromptService:
    return request.app.state.prompts


Prompts = Annotated[PromptService, Depends(get_prompts)]


class ModelOption(BaseModel):
    id: str
    label: str


class TemplateSummary(BaseModel):
    id: str
    title: str
    pairs: int
    image_target: str
    video_target: str
    created_at: datetime


class TemplateDetail(TemplateSummary):
    data: I2VPrompt


class TemplatePatch(BaseModel):
    title: str | None = None
    data: I2VPrompt | None = None


class RunSummary(BaseModel):
    id: str
    template_id: str
    template_title: str
    model: str
    pair_count: int
    scenario_name: str
    state: str
    done: int
    total: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    rows: list[GeneratedPair]


def _template(record: PromptTemplateRecord, detail: bool = False) -> TemplateSummary:
    template = record.template
    fields = {
        "id": record.id,
        "title": record.title,
        "pairs": len(template.pairs),
        "image_target": template.targets.image,
        "video_target": template.targets.video,
        "created_at": record.created_at,
    }
    return TemplateDetail(**fields, data=template) if detail else TemplateSummary(**fields)


def _run(record: PromptRunRecord, detail: bool = False) -> RunSummary:
    fields = {
        "id": record.id,
        "template_id": record.template_id,
        "template_title": record.template_title,
        "model": record.model,
        "pair_count": record.pair_count,
        "scenario_name": record.scenario_name,
        "state": record.state,
        "done": record.done,
        "total": len(record.paragraphs),
        "error": record.error,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
    }
    return RunDetail(**fields, rows=record.pairs) if detail else RunSummary(**fields)


async def _read(file: UploadFile) -> bytes:
    data = await file.read(MAX_DOC_BYTES + 1)
    if len(data) > MAX_DOC_BYTES:
        raise HTTPException(413, "file is larger than 10 MB")
    return data


@router.get("/models")
async def models() -> list[ModelOption]:
    return [ModelOption(id=m, label=label) for m, label in GENERATION_MODELS]


@router.get("/templates")
async def list_templates(prompts: Prompts) -> list[TemplateSummary]:
    return [_template(r) for r in await prompts.templates()]


@router.post("/templates", status_code=201)
async def create_template(prompts: Prompts, file: UploadFile, title: Annotated[str | None, Form()] = None) -> TemplateDetail:
    """Extract a template from a brief (.txt / .docx); takes up to a minute."""
    name = file.filename or "brief.txt"
    try:
        brief = document_text(name, await _read(file))
        record = await prompts.create_template(brief, title or PurePosixPath(name).stem)
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(422, str(e)) from None
    return _template(record, detail=True)  # type: ignore[return-value]


@router.get("/templates/{template_id}")
async def get_template(template_id: str, prompts: Prompts) -> TemplateDetail:
    return _template(await prompts.template(template_id), detail=True)  # type: ignore[return-value]


@router.patch("/templates/{template_id}")
async def patch_template(template_id: str, body: TemplatePatch, prompts: Prompts) -> TemplateDetail:
    return _template(await prompts.update_template(template_id, body.title, body.data), detail=True)  # type: ignore[return-value]


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(template_id: str, prompts: Prompts) -> None:
    await prompts.delete_template(template_id)


@router.get("/runs")
async def list_runs(prompts: Prompts) -> list[RunSummary]:
    return [_run(r) for r in await prompts.runs()]


@router.post("/runs", status_code=201)
async def create_run(
    prompts: Prompts,
    file: UploadFile,
    template_id: Annotated[str, Form()],
    model: Annotated[str, Form()],
    pair_count: Annotated[int, Form()],
) -> RunSummary:
    """Generate prompt pairs for a scenario (.docx / .txt) in the background."""
    name = file.filename or "scenario.txt"
    try:
        paragraphs = await asyncio.to_thread(parse_scenario, name, await _read(file))
        record = await prompts.create_run(template_id, model, pair_count, PurePosixPath(name).stem, paragraphs)
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(422, str(e)) from None
    return _run(record)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, prompts: Prompts) -> RunDetail:
    return _run(await prompts.run(run_id), detail=True)  # type: ignore[return-value]


@router.get("/runs/{run_id}/xlsx")
async def run_xlsx(run_id: str, prompts: Prompts) -> Response:
    record = await prompts.run(run_id)
    if not record.rows:
        raise HTTPException(409, "the run has no prompts yet")
    data = await asyncio.to_thread(build_workbook, record.pairs)
    filename = quote(f"{record.scenario_name}.xlsx")
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, prompts: Prompts) -> None:
    await prompts.delete_run(run_id)
