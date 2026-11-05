from pydantic import BaseModel

from src.core.time import UtcDatetime
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
    avatar_keys: list[str] = []
    static_image: str | None = None


class JobFile(BaseModel):
    name: str
    row: str
    kind: str
    size: int
    reason: str | None = None
    url: str | None = None
    download_url: str | None = None
    created_at: UtcDatetime


class Account(BaseModel):
    name: str
    dolphin_id: str | None
    provider: str = "dolphin"
    enabled: bool = True
    proxies: int
    dead_proxies: int
    dead_proxy_list: list[str] = []
    cooldown: float
    image_pause: float
    video_pause: float
    active_image_model: str | None = None
    image_quota_limited: bool = False
    flow_project_id: str | None = None
    has_credentials: bool = False
    has_totp: bool = False


class CooldownResetIn(BaseModel):
    kind: str = "all"


class ReviveProxiesIn(BaseModel):
    proxy: str | None = None


class LogTailOut(BaseModel):
    file: str | None = None
    total_matched: int = 0
    lines: list[str] = []
