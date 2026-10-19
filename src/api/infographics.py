"""/api/infographics: animated cards of the people, places and artifacts of a scenario."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.infographics.categories import CATEGORIES
from src.infographics.service import InfographicRunRecord, InfographicsService
from src.storage.zip_stream import stream_zip, zip_headers

MAX_DOC_BYTES = 10 * 1024**2

router = APIRouter(prefix="/api/infographics")


def get_service(request: Request) -> InfographicsService:
    return request.app.state.infographics


Service = Annotated[InfographicsService, Depends(get_service)]


class Category(BaseModel):
    id: str
    label: str


class Card(BaseModel):
    id: str
    name: str
    years: str
    category: str
    paragraph_number: int
    paragraph: str
    image_url: str | None
    video_url: str | None
    download_url: str | None


class RunSummary(BaseModel):
    id: str
    project_id: str | None
    scenario_name: str
    categories: list[str]
    one_per_person: bool
    state: str
    done: int
    total: int
    found: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    cards: list[Card]


def _summary(record: InfographicRunRecord) -> dict:
    return {
        "id": record.id,
        "project_id": record.project_id,
        "scenario_name": record.scenario_name,
        "categories": record.categories,
        "one_per_person": record.one_per_person,
        "state": record.state,
        "done": record.done,
        "total": record.total,
        "found": record.found,
        "error": record.error,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
    }


@router.get("/categories")
async def categories() -> list[Category]:
    return [Category(id=c.id, label=c.label) for c in CATEGORIES.values()]


@router.get("/runs")
async def list_runs(service: Service, project_id: str | None = None) -> list[RunSummary]:
    return [RunSummary(**_summary(r)) for r in await service.runs(project_id=project_id)]


@router.post("/runs", status_code=201)
async def create_run(
    service: Service,
    file: UploadFile,
    categories: Annotated[str, Form()] = "people",
    one_per_person: Annotated[bool, Form()] = False,
    project_id: Annotated[str | None, Form()] = None,
) -> RunSummary:
    """Find the subjects of a scenario (.docx) and render a card for each; takes from minutes to hours."""
    name = file.filename or "scenario.docx"
    if not name.lower().endswith(".docx"):
        raise HTTPException(422, "scenario must be .docx")
    chosen = [c.strip() for c in categories.split(",") if c.strip()]
    unknown = [c for c in chosen if c not in CATEGORIES]
    if not chosen or unknown:
        raise HTTPException(422, f"categories must be some of {', '.join(CATEGORIES)}")
    data = await file.read(MAX_DOC_BYTES + 1)
    if len(data) > MAX_DOC_BYTES:
        raise HTTPException(413, "file is larger than 10 MB")
    record = await service.create(PurePosixPath(name).stem, data, chosen, one_per_person, project_id or None)
    return RunSummary(**_summary(record))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, service: Service, request: Request) -> RunDetail:
    record = await service.run(run_id)
    objects = request.app.state.objects
    cards = [
        Card(
            id=e["id"],
            name=e["name"],
            years=e["years"],
            category=e["category"],
            paragraph_number=e["paragraph_number"],
            paragraph=e["paragraph"],
            image_url=objects.url(e["image_key"]) if e.get("image_key") else None,
            video_url=objects.url(e["video_key"]) if e.get("video_key") else None,
            download_url=objects.url(e["video_key"], download_name=f"{e['id']} {e['name']}.mp4") if e.get("video_key") else None,
        )
        for e in record.entries
    ]
    return RunDetail(**_summary(record), cards=cards)


@router.get("/runs/{run_id}/zip")
async def run_zip(run_id: str, service: Service, request: Request) -> StreamingResponse:
    """Stream all rendered card videos (or source images) of an infographic run as a ZIP archive."""
    record = await service.run(run_id)
    items: list[tuple[str, str]] = []
    for e in record.entries:
        safe_name = str(e.get("name", "")).replace("/", "-").replace("\\", "-").strip()
        if e.get("video_key"):
            items.append((f"{e['id']} {safe_name}.mp4", e["video_key"]))
        elif e.get("image_key"):
            suffix = PurePosixPath(e["image_key"]).suffix or ".jpg"
            items.append((f"{e['id']} {safe_name}{suffix}", e["image_key"]))
    if not items:
        raise HTTPException(404, "no files to download")
    return StreamingResponse(
        stream_zip(request.app.state.objects, items),
        media_type="application/zip",
        headers=zip_headers(f"{record.scenario_name}.zip", f"infographics_{run_id}.zip"),
    )


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, service: Service) -> RunSummary:
    if not await service.retry(run_id):
        raise HTTPException(409, "only failed runs can be retried")
    return RunSummary(**_summary(await service.run(run_id)))


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, service: Service) -> None:
    await service.delete(run_id)
