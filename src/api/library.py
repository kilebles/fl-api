"""/api/projects and /api/avatars."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.auth import Caller
from src.api.routes import check_upload_key_owner
from src.avatars.angles import ANGLES, AngleId, AvatarStudio, ReferenceAnalyzer, ReferenceAngle, ReferenceFraming
from src.avatars.service import AvatarRecord, AvatarService
from src.jobs.models import JobState
from src.projects.service import ProjectRecord, ProjectService

router = APIRouter(prefix="/api")


def get_projects(request: Request) -> ProjectService:
    return request.app.state.projects


def get_avatars(request: Request) -> AvatarService:
    return request.app.state.avatars


def get_avatar_studio(request: Request) -> AvatarStudio:
    return request.app.state.avatar_studio


def get_avatar_analyzer(request: Request) -> ReferenceAnalyzer:
    return request.app.state.avatar_analyzer


Projects = Annotated[ProjectService, Depends(get_projects)]
Avatars = Annotated[AvatarService, Depends(get_avatars)]
Studio = Annotated[AvatarStudio, Depends(get_avatar_studio)]
Analyzer = Annotated[ReferenceAnalyzer, Depends(get_avatar_analyzer)]


class Project(BaseModel):
    id: str
    title: str
    style: str
    created_at: datetime


class ProjectIn(BaseModel):
    title: str = Field(max_length=300)
    style: str = ""


class ProjectPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    style: str | None = None


class Avatar(BaseModel):
    id: str
    name: str
    appearance: str
    image_keys: list[str]
    image_urls: list[str]
    download_urls: list[str] = []
    pack_job_id: str | None = None
    pack_state: JobState | None = None
    pack_stage: Literal["base", "angles"] | None = None
    pack_angles: list[AngleId] = []
    pack_done: int = 0
    pack_total: int = 0
    created_at: datetime


class AvatarIn(BaseModel):
    name: str = Field(max_length=200)
    appearance: str = ""
    image_keys: list[str] = Field(min_length=1, max_length=8)


class AvatarPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    appearance: str | None = None
    image_keys: list[str] | None = Field(default=None, min_length=1, max_length=8)


class AvatarGenerateIn(BaseModel):
    name: str = Field(max_length=200)
    appearance: str = Field(min_length=1, max_length=4000)
    angles: list[AngleId] | None = None
    style: str | None = None
    project_id: str | None = None


class AvatarAnglesIn(BaseModel):
    angles: list[AngleId] | None = None
    style: str | None = None
    project_id: str | None = None


class AvatarAnalyzeIn(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class AvatarRefAnalysisOut(BaseModel):
    key: str
    detected: bool
    framing: ReferenceFraming
    angle: ReferenceAngle
    summary_ru: str
    recommended_angles: list[AngleId]


def _project(r: ProjectRecord) -> Project:
    return Project(id=r.id, title=r.title, style=r.style or "", created_at=r.created_at)


async def _avatar(r: AvatarRecord, request: Request) -> Avatar:
    objects = request.app.state.objects
    total = len(r.image_keys)
    base_name = (r.name or "avatar").strip() or "avatar"
    download_urls = [
        objects.url(k, download_name=f"{base_name}{f' - {i + 1}' if total > 1 else ''}{k[k.rfind('.'):] if '.' in k else '.jpg'}")
        for i, k in enumerate(r.image_keys)
    ]
    pack_job_id: str | None = None
    pack_state: JobState | None = None
    pack_stage: Literal["base", "angles"] | None = None
    pack_angles: list[AngleId] = []
    pack_done = 0
    pack_total = 0

    if r.pack_job_id:
        try:
            status = await request.app.state.queue.get(r.pack_job_id)
        except LookupError:
            status = None
        if status is not None and not status.state.finished:
            pending = r.pack_pending or {}
            imported = pending.get("imported") or {}
            expected: list[AngleId] = [a for a in (pending.get("expected") or []) if a in ANGLES]
            follow_up: list[AngleId] = [a for a in (pending.get("angles") or []) if a in ANGLES and a != "front"]
            if r.pack_stage == "base":
                planned: list[AngleId] = ["front", *follow_up]
                pack_stage = "base"
                pack_done = sum(1 for a in planned if a in imported)
                pack_total = len(planned)
                pack_angles = [a for a in planned if a not in imported]
            else:
                base_done = int(pending.get("base_done") or 0)
                pack_stage = "angles"
                pack_done = base_done + sum(1 for a in expected if a in imported)
                pack_total = base_done + len(expected)
                pack_angles = [a for a in expected if a not in imported]
            pack_job_id = r.pack_job_id
            pack_state = status.state

    return Avatar(
        id=r.id,
        name=r.name,
        appearance=r.appearance or "",
        image_keys=r.image_keys,
        image_urls=[objects.url(k) for k in r.image_keys],
        download_urls=download_urls,
        pack_job_id=pack_job_id,
        pack_state=pack_state,
        pack_stage=pack_stage,
        pack_angles=pack_angles,
        pack_done=pack_done,
        pack_total=pack_total,
        created_at=r.created_at,
    )


@router.get("/projects")
async def list_projects(projects: Projects, caller: Caller) -> list[Project]:
    return [_project(r) for r in await projects.all(user_id=caller.user_id)]


@router.post("/projects", status_code=201)
async def create_project(body: ProjectIn, projects: Projects, caller: Caller) -> Project:
    return _project(await projects.create(body.title, body.style, user_id=caller.user_id))


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch, projects: Projects, caller: Caller) -> Project:
    return _project(await projects.update(project_id, body.title, body.style, user_id=caller.user_id))


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, projects: Projects, caller: Caller) -> None:
    await projects.delete(project_id, user_id=caller.user_id)


async def _check_keys(request: Request, keys: list[str], caller: Caller) -> None:
    for key in keys:
        check_upload_key_owner(key, caller)
        if not await request.app.state.objects.exists(key):
            raise HTTPException(422, f"uploaded file not found: {key}")


async def _resolve_style(request: Request, caller: Caller, project_id: str | None, style: str | None) -> str:
    proj_style = ""
    if project_id:
        proj = await request.app.state.projects.get(project_id, user_id=caller.user_id)
        proj_style = proj.style or ""
    return style if style is not None else proj_style


@router.get("/avatars")
async def list_avatars(avatars: Avatars, request: Request, caller: Caller) -> list[Avatar]:
    records = await avatars.all(user_id=caller.user_id)
    return [await _avatar(r, request) for r in records]


@router.post("/avatars", status_code=201)
async def create_avatar(body: AvatarIn, avatars: Avatars, request: Request, caller: Caller) -> Avatar:
    await _check_keys(request, body.image_keys, caller)
    record = await avatars.create(body.name, body.appearance, body.image_keys, user_id=caller.user_id)
    return await _avatar(record, request)


@router.post("/avatars/analyze-reference")
async def analyze_avatar_reference(
    body: AvatarAnalyzeIn,
    analyzer: Analyzer,
    request: Request,
    caller: Caller,
) -> AvatarRefAnalysisOut:
    """Classify framing and camera angle of an uploaded reference photo to suggest non-duplicating 360° angles."""
    await _check_keys(request, [body.key], caller)
    res = await analyzer.analyze_key(body.key)
    return AvatarRefAnalysisOut(
        key=res.key,
        detected=res.detected,
        framing=res.framing,
        angle=res.angle,
        summary_ru=res.summary_ru,
        recommended_angles=res.recommended_angles,
    )


@router.post("/avatars/generate", status_code=201)
async def generate_avatar(body: AvatarGenerateIn, studio: Studio, request: Request, caller: Caller) -> Avatar:
    """Create a new avatar from text (`name` + `appearance`) and generate its 0° portrait + 360° turnaround."""
    resolved_style = await _resolve_style(request, caller, body.project_id, body.style)
    try:
        record = await studio.create_and_generate(
            name=body.name,
            appearance=body.appearance,
            user_id=caller.user_id,
            angles=body.angles,
            style=resolved_style,
            project_id=body.project_id,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return await _avatar(record, request)


@router.post("/avatars/{avatar_id}/angles", status_code=202)
async def generate_avatar_angles(
    avatar_id: str,
    body: AvatarAnglesIn,
    studio: Studio,
    request: Request,
    caller: Caller,
) -> Avatar:
    """Generate 360° studio turnaround references (`three_quarter`, `profile`, `full_body`) locked to `image_keys[0]`."""
    resolved_style = await _resolve_style(request, caller, body.project_id, body.style)
    try:
        record = await studio.start_angles(
            avatar_id,
            user_id=caller.user_id,
            angles=body.angles,
            style=resolved_style,
            project_id=body.project_id,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return await _avatar(record, request)


@router.post("/avatars/{avatar_id}/angles/cancel")
async def cancel_avatar_angles(avatar_id: str, studio: Studio, request: Request, caller: Caller) -> Avatar:
    record = await studio.cancel_angles(avatar_id, user_id=caller.user_id)
    return await _avatar(record, request)


@router.patch("/avatars/{avatar_id}")
async def patch_avatar(avatar_id: str, body: AvatarPatch, avatars: Avatars, request: Request, caller: Caller) -> Avatar:
    if body.image_keys is not None:
        await _check_keys(request, body.image_keys, caller)
    record = await avatars.update(
        avatar_id, user_id=caller.user_id, name=body.name, appearance=body.appearance, image_keys=body.image_keys
    )
    return await _avatar(record, request)


@router.delete("/avatars/{avatar_id}", status_code=204)
async def delete_avatar(avatar_id: str, avatars: Avatars, studio: Studio, caller: Caller) -> None:
    record = await avatars.get(avatar_id, user_id=caller.user_id)
    if record.pack_job_id:
        await studio.cancel_angles(avatar_id, user_id=caller.user_id)
    await avatars.delete(avatar_id, user_id=caller.user_id)
