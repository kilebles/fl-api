"""/api/projects and /api/avatars."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.auth import Caller
from src.api.routes import check_upload_key_owner
from src.avatars.service import AvatarRecord, AvatarService
from src.projects.service import ProjectRecord, ProjectService

router = APIRouter(prefix="/api")


def get_projects(request: Request) -> ProjectService:
    return request.app.state.projects


def get_avatars(request: Request) -> AvatarService:
    return request.app.state.avatars


Projects = Annotated[ProjectService, Depends(get_projects)]
Avatars = Annotated[AvatarService, Depends(get_avatars)]


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
    created_at: datetime


class AvatarIn(BaseModel):
    name: str = Field(max_length=200)
    appearance: str = ""
    image_keys: list[str] = Field(min_length=1, max_length=8)


class AvatarPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    appearance: str | None = None
    image_keys: list[str] | None = Field(default=None, min_length=1, max_length=8)


def _project(r: ProjectRecord) -> Project:
    return Project(id=r.id, title=r.title, style=r.style or "", created_at=r.created_at)


def _avatar(r: AvatarRecord, request: Request) -> Avatar:
    objects = request.app.state.objects
    return Avatar(
        id=r.id, name=r.name, appearance=r.appearance or "", image_keys=r.image_keys, image_urls=[objects.url(k) for k in r.image_keys], created_at=r.created_at
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


@router.get("/avatars")
async def list_avatars(avatars: Avatars, request: Request, caller: Caller) -> list[Avatar]:
    return [_avatar(r, request) for r in await avatars.all(user_id=caller.user_id)]


@router.post("/avatars", status_code=201)
async def create_avatar(body: AvatarIn, avatars: Avatars, request: Request, caller: Caller) -> Avatar:
    await _check_keys(request, body.image_keys, caller)
    return _avatar(await avatars.create(body.name, body.appearance, body.image_keys, user_id=caller.user_id), request)


@router.patch("/avatars/{avatar_id}")
async def patch_avatar(avatar_id: str, body: AvatarPatch, avatars: Avatars, request: Request, caller: Caller) -> Avatar:
    if body.image_keys is not None:
        await _check_keys(request, body.image_keys, caller)
    return _avatar(
        await avatars.update(
            avatar_id, user_id=caller.user_id, name=body.name, appearance=body.appearance, image_keys=body.image_keys
        ),
        request,
    )


@router.delete("/avatars/{avatar_id}", status_code=204)
async def delete_avatar(avatar_id: str, avatars: Avatars, caller: Caller) -> None:
    await avatars.delete(avatar_id, user_id=caller.user_id)
