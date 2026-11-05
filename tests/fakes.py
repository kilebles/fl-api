"""A fake AccountSession: no browser, no network — results are written straight to disk."""

import asyncio
from pathlib import Path

from src.core.errors import (
    GenerationFilteredError,
    ImageQuotaError,
    InputImageRejectedError,
    VideoAccessDeniedError,
)
from src.engine.session import SubmittedVideo


class FakeSession:
    def __init__(
        self,
        name: str,
        scratch: Path,
        videos: bool = True,
        images: bool = True,
        filtered: set[str] | None = None,
        image_quota_after: int | None = None,
        ip_rejections: dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self._ip_rejections = dict(ip_rejections or {})
        self.can_rotate = False
        self.account_count = 1
        self.on_cooldown = False
        self._scratch = scratch
        self._videos = videos
        self._images = images
        self._filtered = filtered or set()
        self._image_quota_after = image_quota_after
        self.started = False
        self.images_made: list[str] = []
        self.videos_made: list[str] = []
        self.model_keys: list[str | None] = []
        self.sources: list[Path | None] = []
        self.end_sources: list[Path | None] = []
        self.image_references: list[list[Path]] = []
        self.video_extra_references: list[list[Path]] = []
        self.image_aspect_ratios: list[str | None] = []
        self.video_aspect_ratios: list[str | None] = []

    def can_make_images(self) -> bool:
        return not self.on_cooldown and self._images

    def can_make_videos(self) -> bool:
        return not self.on_cooldown and self._videos

    @property
    def is_running(self) -> bool:
        return self.started

    async def ensure_started(self) -> None:
        self.started = True

    async def close(self, stop_browser: bool = False) -> None:
        if stop_browser:
            self.started = False

    async def rotate(self) -> None:
        pass

    async def make_image(
        self,
        index: int,
        prompt: str,
        dest: Path,
        reference: Path | None = None,
        references: list[Path] | None = None,
        aspect_ratio: str | None = None,
    ) -> Path:
        await asyncio.sleep(0)
        if self._image_quota_after is not None and len(self.images_made) >= self._image_quota_after:
            self._images = False
            raise ImageQuotaError(self.name)
        if prompt in self._filtered:
            raise GenerationFilteredError(f"filtered: {prompt}")
        refs = list(references) if references else ([reference] if reference else [])
        self.image_references.append(refs)
        self.image_aspect_ratios.append(aspect_ratio)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"{prompt}|ref={reference.name if reference else ''}")
        self.images_made.append(prompt)
        return dest

    async def submit_video(
        self,
        index: int,
        prompt: str,
        image: Path | None = None,
        frame: Path | None = None,
        end_image: Path | None = None,
        model_key: str | None = None,
        extra_references: list[Path] | None = None,
        aspect_ratio: str | None = None,
    ) -> SubmittedVideo:
        await asyncio.sleep(0)
        if not self._videos:
            raise VideoAccessDeniedError(self.name)
        if prompt in self._filtered:
            raise GenerationFilteredError(f"filtered: {prompt}")
        self.model_keys.append(model_key)
        self.sources.append(image or frame)
        self.end_sources.append(end_image)
        self.video_extra_references.append(list(extra_references or []))
        self.video_aspect_ratios.append(aspect_ratio)
        return SubmittedVideo(media_id=f"{self.name}-{index}-{prompt}", project_id="p", proxy_url=None, cookies={})

    async def finish_video(self, index: int, submitted: SubmittedVideo) -> Path:
        await asyncio.sleep(0.01)
        prompt = submitted.media_id.split("-", 2)[2]
        if self._ip_rejections.get(prompt, 0) > 0:
            self._ip_rejections[prompt] -= 1
            raise InputImageRejectedError(f"IP_INPUT_IMAGE: {prompt}")
        self._scratch.mkdir(parents=True, exist_ok=True)
        path = self._scratch / f"{submitted.media_id}.mp4"
        path.write_text(submitted.media_id)
        self.videos_made.append(submitted.media_id)
        return path

