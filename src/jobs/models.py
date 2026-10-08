import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from src.jobs.modes import MODES, Mode, Strategy

_NAME_RE = re.compile(r"^[\w\-.]+$")


class JobRow(BaseModel):
    """One row of a job; `name` becomes the file name of its results ({name}.mp4 / {name}.jpg)."""

    name: str
    prompt: str | None = Field(default=None, description="Video prompt")
    image_prompt: str | None = Field(default=None, description="Image prompt (img / i2v modes)")


class JobSpec(BaseModel):
    """Everything a job needs; validated against the mode's requirements."""

    mode: Mode
    rows: list[JobRow]
    output_dir: Path
    strategy: Strategy = Strategy.PARALLEL
    static_image: Path | None = Field(default=None, description="i2v: one picture used as the source of every video")
    avatar: Path | None = Field(default=None, description="i2i2v: reference picture kept in every generated image")
    video_model_key: str | None = Field(default=None, description="Override of the mode's video model")

    @model_validator(mode="after")
    def _check(self) -> "JobSpec":
        spec = MODES[self.mode]
        if not self.rows:
            raise ValueError("job has no rows")
        names = [row.name for row in self.rows]
        bad = [n for n in names if not _NAME_RE.match(n) or n in (".", "..")]
        if bad:
            raise ValueError(f"row names must be letters, digits, '_', '-', '.': {bad[:5]}")
        if len(set(names)) != len(names):
            raise ValueError("row names must be unique")
        if self.avatar is not None and not spec.avatar:
            raise ValueError(f"mode {self.mode} does not take an avatar")
        if spec.avatar and self.avatar is None:
            raise ValueError(f"mode {self.mode} needs an avatar picture")
        if self.static_image is not None and not spec.static_image:
            raise ValueError(f"mode {self.mode} does not take a static image")
        for path in (self.avatar, self.static_image):
            if path is not None and not path.is_file():
                raise ValueError(f"file not found: {path}")
        needs_image_prompt = spec.needs_image_prompt and self.static_image is None
        for row in self.rows:
            if spec.needs_video_prompt and not row.prompt:
                raise ValueError(f"row {row.name}: video prompt is required in mode {self.mode}")
            if needs_image_prompt and not row.image_prompt:
                raise ValueError(f"row {row.name}: image prompt is required in mode {self.mode}")
        return self


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"      # every account is paused (quota / no model access); resumes on its own
    DONE = "done"            # every row has a result or a .skip marker
    STOPPED = "stopped"      # a pass made no progress while accounts were free; rows are left
    FAILED = "failed"        # fatal error (disk full, broken reCAPTCHA page, Dolphin down)
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.STOPPED, JobState.FAILED, JobState.CANCELLED)


class JobProgress(BaseModel):
    """Counts read from the output folder, so they include results of earlier runs."""

    total: int
    images_done: int = 0
    videos_done: int = 0
    skipped: int = 0
    remaining: int = 0
    pass_num: int = 0


class JobStatus(BaseModel):
    id: str
    mode: Mode
    strategy: Strategy
    output_dir: Path
    state: JobState = JobState.QUEUED
    progress: JobProgress
    message: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
