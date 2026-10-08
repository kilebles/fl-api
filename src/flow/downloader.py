import errno
from pathlib import Path

import httpx
from loguru import logger

from src.core.errors import DiskFullError
from src.core.settings import Settings


class VideoDownloaderService:
    """Downloads generated videos (direct URL from polling, else the labs.google redirect endpoint)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cookies: dict[str, str] = {}

    @property
    def cookies(self) -> dict[str, str]:
        """Current cookies snapshot."""
        return self._cookies

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """Set cookies for download requests (from CDP browser)."""
        self._cookies = cookies
        logger.debug(f"Cookies set: {len(cookies)} items")

    async def download(
        self,
        media_id: str,
        cookies: dict[str, str] | None = None,
        proxy_url: str | None = None,
        video_url: str | None = None,
    ) -> Path:
        """Download a video by media ID.

        Uses the direct URL from the polling response when the API provided one,
        otherwise resolves it through the legacy Labs redirect endpoint.
        """
        logger.debug(f"Starting download for media_id: {media_id}")
        try:
            if video_url:
                try:
                    return await self._download_video(media_id, video_url, proxy_url=proxy_url)
                except Exception as e:
                    logger.warning(f"Direct video URL failed for {media_id}: {e} — falling back to redirect endpoint")
            url = await self._get_redirect_url(media_id, cookies=cookies, proxy_url=proxy_url)
            video_path = await self._download_video(media_id, url, proxy_url=proxy_url)
            return video_path
        except Exception as e:
            logger.error(f"Download failed for {media_id}: {e}")
            raise

    async def _get_redirect_url(self, media_id: str, cookies: dict[str, str] | None = None, proxy_url: str | None = None) -> str:
        """Resolve the video URL via the legacy Labs redirect endpoint (needs a labs.google session)."""
        effective_cookies = cookies if cookies is not None else self._cookies
        if not effective_cookies:
            raise RuntimeError("Cookies not set. Call set_cookies() first.")

        url = f"{self._settings.labs_base_url}/fx/api/trpc/media.getMediaUrlRedirect"

        async with httpx.AsyncClient(
            timeout=self._settings.api_timeout,
            follow_redirects=False,
            proxy=proxy_url,
        ) as client:
            response = await client.get(
                url,
                params={"name": media_id},
                cookies=effective_cookies,
            )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if location:
                    logger.debug(f"Redirect URL: {location}")
                    return location

            response.raise_for_status()

            raise RuntimeError(
                f"Expected redirect response, got {response.status_code}"
            )

    async def _download_video(self, media_id: str, video_url: str, proxy_url: str | None = None) -> Path:
        """Download video from URL with progress logging."""
        self._settings.download_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._settings.download_dir / f"{media_id}.mp4"

        async with httpx.AsyncClient(timeout=None, proxy=proxy_url) as client:
            async with client.stream("GET", video_url) as response:
                response.raise_for_status()

                total_size = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                last_logged_percent = -1

                try:
                    with open(output_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                            downloaded += len(chunk)

                            if total_size > 0:
                                percent = int((downloaded / total_size) * 100)
                                if percent >= last_logged_percent + 10:
                                    logger.info(f"Downloading: {percent}%")
                                    last_logged_percent = percent
                except OSError as e:
                    output_path.unlink(missing_ok=True)
                    if e.errno == errno.ENOSPC:
                        raise DiskFullError(f"No space left on device while saving {output_path}") from e
                    raise

        logger.info(f"Download complete: {output_path}")
        return output_path
