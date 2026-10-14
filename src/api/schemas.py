from datetime import datetime

from pydantic import BaseModel

from src.jobs.models import JobRow, JobStatus
from src.jobs.modes import Mode


class ModeInfo(BaseModel):
    id: Mode
    title: str
    makes_images: bool
    makes_videos: bool
    needs_video_prompt: bool
    needs_image_prompt: bool
    avatar: bool
    static_image: bool
    chain: bool


class Upload(BaseModel):
    key: str
    name: str
    size: int


class Rows(BaseModel):
    rows: list[JobRow]


class JobDetail(JobStatus):
    rows: list[JobRow]
    position: int | None = None
    video_model_key: str | None = None
    avatar: str | None = None
    static_image: str | None = None


class JobFile(BaseModel):
    name: str
    row: str
    kind: str
    size: int
    reason: str | None = None
    url: str | None = None
    download_url: str | None = None
    created_at: datetime


class Account(BaseModel):
    name: str
    dolphin_id: str | None
    proxies: int
    dead_proxies: int
    cooldown: float
    image_pause: float
    video_pause: float
