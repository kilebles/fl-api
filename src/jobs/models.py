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


def check_rows(mode: Mode, rows: list[JobRow], avatar: bool, static_image: bool) -> None:
    """Validate rows and input pictures against what the mode needs; raises ValueError."""
    spec = MODES[mode]
    if not rows:
        raise ValueError("job has no rows")
    names = [row.name for row in rows]
    bad = [n for n in names if not _NAME_RE.match(n) or n in (".", "..")]
    if bad:
        raise ValueError(f"row names must be letters, digits, '_', '-', '.': {bad[:5]}")
    if len(set(names)) != len(names):
        raise ValueError("row names must be unique")
    if avatar and not spec.avatar:
        raise ValueError(f"mode {mode} does not take an avatar")
    if spec.avatar and not avatar:
        raise ValueError(f"mode {mode} needs an avatar picture")
    if static_image and not spec.static_image:
        raise ValueError(f"mode {mode} does not take a static image")
    needs_image_prompt = spec.needs_image_prompt and not static_image
    for row in rows:
        if spec.needs_video_prompt and not row.prompt:
            raise ValueError(f"row {row.name}: video prompt is required in mode {mode}")
        if needs_image_prompt and not row.image_prompt:
            raise ValueError(f"row {row.name}: image prompt is required in mode {mode}")


class JobSpec(BaseModel):
    """Everything a run needs, with input pictures as local files."""

    mode: Mode
    rows: list[JobRow]
    output_dir: Path
    strategy: Strategy = Strategy.PARALLEL
    static_image: Path | None = Field(default=None, description="i2v: one picture used as the source of every video")
    avatar: Path | None = Field(default=None, description="i2i2v: reference picture kept in every generated image")
    video_model_key: str | None = Field(default=None, description="Override of the mode's video model")

    @model_validator(mode="after")
    def _check(self) -> "JobSpec":
        check_rows(self.mode, self.rows, self.avatar is not None, self.static_image is not None)
        for path in (self.avatar, self.static_image):
            if path is not None and not path.is_file():
                raise ValueError(f"file not found: {path}")
        return self


class JobRequest(BaseModel):
    """A job as the API takes it: input pictures are object-store keys returned by an upload."""

    mode: Mode
    rows: list[JobRow]
    title: str | None = Field(default=None, max_length=200)
    project_id: str | None = Field(default=None, max_length=32)
    strategy: Strategy = Strategy.PARALLEL
    static_image: str | None = Field(default=None, description="i2v: key of the picture used for every video")
    avatar: str | None = Field(default=None, description="i2i2v: key of the reference picture")
    video_model_key: str | None = Field(default=None, description="Override of the mode's video model")

    @model_validator(mode="after")
    def _check(self) -> "JobRequest":
        check_rows(self.mode, self.rows, self.avatar is not None, self.static_image is not None)
        return self


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"      # every account is paused (quota / no model access); resumes on its own
    DONE = "done"            # every row has a result or was skipped
    STOPPED = "stopped"      # a pass made no progress while accounts were free; rows are left
    FAILED = "failed"        # fatal error (disk full, broken reCAPTCHA page, Dolphin down)
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.STOPPED, JobState.FAILED, JobState.CANCELLED)


class JobProgress(BaseModel):
    """Result counts, including those of earlier runs of the same job."""

    total: int
    images_done: int = 0
    videos_done: int = 0
    skipped: int = 0
    remaining: int = 0
    pass_num: int = 0


class JobStatus(BaseModel):
    id: str
    title: str | None = None
    project_id: str | None = None
    mode: Mode
    strategy: Strategy
    state: JobState = JobState.QUEUED
    progress: JobProgress
    message: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
