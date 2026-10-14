"""A fake AccountSession: no browser, no network — results are written straight to disk."""

import asyncio
from pathlib import Path

from src.core.errors import (
    GenerationFilteredError,
    ImageQuotaError,
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
    ) -> None:
        self.name = name
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

    def can_make_images(self) -> bool:
        return self._images

    def can_make_videos(self) -> bool:
        return self._videos

    async def ensure_started(self) -> None:
        self.started = True

    async def close(self) -> None:
        pass

    async def rotate(self) -> None:
        pass

    async def make_image(self, index: int, prompt: str, dest: Path, reference: Path | None = None) -> Path:
        await asyncio.sleep(0)
        if self._image_quota_after is not None and len(self.images_made) >= self._image_quota_after:
            self._images = False
            raise ImageQuotaError(self.name)
        if prompt in self._filtered:
            raise GenerationFilteredError(f"filtered: {prompt}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"{prompt}|ref={reference.name if reference else ''}")
        self.images_made.append(prompt)
        return dest

    async def submit_video(self, index: int, prompt: str, image: Path | None = None, frame: Path | None = None, model_key: str | None = None) -> SubmittedVideo:
        await asyncio.sleep(0)
        if not self._videos:
            raise VideoAccessDeniedError(self.name)
        if prompt in self._filtered:
            raise GenerationFilteredError(f"filtered: {prompt}")
        self.model_keys.append(model_key)
        self.sources.append(image or frame)
        return SubmittedVideo(media_id=f"{self.name}-{index}-{prompt}", project_id="p", proxy_url=None, cookies={})

    async def finish_video(self, index: int, submitted: SubmittedVideo) -> Path:
        await asyncio.sleep(0.01)
        self._scratch.mkdir(parents=True, exist_ok=True)
        path = self._scratch / f"{submitted.media_id}.mp4"
        path.write_text(submitted.media_id)
        self.videos_made.append(submitted.media_id)
        return path

