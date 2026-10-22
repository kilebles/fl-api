import asyncio
import time

import httpx
from loguru import logger

from src.core.errors import GenerationFilteredError, GenerationTimedOutError, InputImageRejectedError, IpProhibitedError
from src.core.settings import Settings
from src.flow.google_auth import SCHEME_SAPISIDHASH, auth_scheme, sapisidhash_header
from src.flow.http import API_BASE, api_endpoint, api_headers
from src.flow.models import MediaInfo

_POLL_FIRST_DELAY = 12.5
_POLL_INTERVAL = 10.0


_DIRECT_URL_KEYS = ("fifeUrl", "downloadUrl", "servingBaseUri", "videoUrl", "url")


def _find_direct_url(media_data: dict) -> str | None:
    """Return a direct https URL for the generated video if the API included one."""
    video_root = media_data.get("video", {}) if "video" in media_data else media_data
    generated = video_root.get("generatedVideo", {}) if isinstance(video_root, dict) else {}
    gcs_urls = (
        video_root.get("servingUrls", {}).get("gcsUrls", {})
        if isinstance(video_root, dict) and isinstance(video_root.get("servingUrls"), dict)
        else {}
    )
    for block in (generated, gcs_urls, video_root, media_data):
        if not isinstance(block, dict):
            continue
        for key in _DIRECT_URL_KEYS:
            value = block.get(key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
        for value in block.values():
            if isinstance(value, str) and value.startswith("https://") and "/video" in value:
                return value
    return None

class PollingService:
    """Polls video generation status until completion or timeout."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def poll(
        self,
        media_id: str,
        project_id: str,
        authorization: str,
        proxy_url: str | None = None,
        browser_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> MediaInfo:
        """
        Poll for video generation status until successful or timeout.

        Raises:
            GenerationFilteredError: The result was filtered (permanent skip).
            IpProhibitedError: The proxy IP is blocked for generation.
            GenerationTimedOutError: The server gave up on this generation.
            TimeoutError: If polling exceeds the configured timeout.
            httpx.HTTPStatusError: If the API returns an error status.
        """
        polling_url = api_endpoint(self._settings.polling_url, authorization)
        body = {
            "media": [{"name": media_id, "projectId": project_id}],
        }

        attempt = 0
        elapsed = 0.0
        start_time = time.monotonic()

        await asyncio.sleep(_POLL_FIRST_DELAY)

        while elapsed < self._settings.polling_timeout:
            attempt += 1
            current_auth = authorization
            if cookies and auth_scheme(authorization) == SCHEME_SAPISIDHASH:
                current_auth = sapisidhash_header(cookies, self._settings.flow_base_url) or authorization
            headers = api_headers(
                self._settings.flow_base_url,
                current_auth,
                browser_headers,
                referer=f"{self._settings.flow_base_url}/project/{project_id}",
                browser_meta=False,
            )

            async with httpx.AsyncClient(timeout=self._settings.api_timeout, proxy=proxy_url, cookies=cookies, http2=True) as client:
                response = await client.post(
                    polling_url,
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()

            data = response.json()
            media_data = data.get("media", [{}])[0]
            metadata = media_data.get("mediaMetadata", {})
            status = metadata.get("mediaStatus", {}).get("mediaGenerationStatus", "")

            logger.info(f"Attempt {attempt}: {status} | mediaStatus={metadata.get('mediaStatus', {})}")

            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                video = media_data.get("video", {}).get("generatedVideo", {})
                video_url = _find_direct_url(media_data)
                if not video_url:
                    video_url = await self._fetch_media_video_url(
                        media_data.get("name", media_id),
                        headers=headers,
                        proxy_url=proxy_url,
                        cookies=cookies,
                    )
                logger.debug(
                    f"Generated video fields: {sorted(video.keys())}"
                    + (f", direct url: {video_url.split('?')[0]}" if video_url else ", no direct url")
                )
                return MediaInfo(
                    media_id=media_data.get("name", media_id),
                    project_id=media_data.get("projectId", project_id),
                    workflow_id=media_data.get("workflowId", ""),
                    status=status,
                    prompt=video.get("prompt"),
                    seed=video.get("seed"),
                    video_url=video_url,
                )

            if status == "MEDIA_GENERATION_STATUS_TIMEOUT":
                media_status = metadata.get("mediaStatus", {})
                logger.warning(
                    f"Generation timed out on server side after {time.monotonic() - start_time:.1f}s "
                    f"({attempt} attempts): {media_status.get('failureReasons') or media_status.get('error')}"
                )
                raise GenerationTimedOutError(f"TIMED_OUT: {media_status.get('failureReasons') or media_status.get('error')}")

            if status == "MEDIA_GENERATION_STATUS_FAILED":
                media_status = metadata.get("mediaStatus", {})
                failure_reasons = media_status.get("failureReasons", [])
                error = media_status.get("error", {})
                visibility = media_data.get("visibility", "")
                error_msg = str(failure_reasons) + " " + str(error)
                if "PUBLIC_ERROR_IP_INPUT_IMAGE" in str(error):
                    logger.warning(
                        f"Generation failed: source image rejected as protected intellectual property "
                        f"(not a proxy problem). reasons={failure_reasons}, error={error}"
                    )
                    raise InputImageRejectedError(f"IP_INPUT_IMAGE: {failure_reasons or error}")
                if "IP_PROHIBITED" in failure_reasons or "IP_PROHIBITED" in str(error):
                    logger.warning(
                        f"Generation failed: IP_PROHIBITED — proxy IP blocked. "
                        f"reasons={failure_reasons}, error={error}"
                    )
                    raise IpProhibitedError(f"IP_PROHIBITED: {failure_reasons or error}")
                if "PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT" in failure_reasons or "TIMED_OUT" in str(error):
                    logger.warning(
                        f"Generation timed out on server side after {time.monotonic() - start_time:.1f}s "
                        f"({attempt} attempts)"
                    )
                    raise GenerationTimedOutError(f"TIMED_OUT: {failure_reasons or error}")
                is_filtered = visibility == "FILTERED" or any(
                    kw in error_msg.lower()
                    for kw in ("filter", "policy", "safety", "violat", "dangerous", "child")
                )
                if is_filtered:
                    logger.warning(
                        f"Generation filtered: reasons={failure_reasons}, "
                        f"error={error}, visibility={visibility}"
                    )
                    raise GenerationFilteredError(
                        f"Video generation filtered: {failure_reasons or error}"
                    )
                waited = time.monotonic() - start_time
                logger.warning(
                    f"Generation FAILED (server error) after {waited:.1f}s ({attempt} attempts): "
                    f"reasons={failure_reasons}, error={error}, visibility={visibility}\n"
                    f"Full response body: {data}"
                )
                raise RuntimeError(
                    f"Video generation failed: {failure_reasons or error}"
                )

            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

        raise TimeoutError(
            f"Polling timed out after {self._settings.polling_timeout} seconds"
        )

    async def _fetch_media_video_url(
        self,
        media_id: str,
        headers: dict[str, str],
        proxy_url: str | None = None,
        cookies: dict[str, str] | None = None,
    ) -> str | None:
        """Fetch /v1/flowMedia/{media_id} to obtain the signed video fifeUrl/videoUrl."""
        url = f"{API_BASE}/flowMedia/{media_id}"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.api_timeout,
                proxy=proxy_url,
                cookies=cookies,
                http2=True,
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return _find_direct_url(response.json())
        except Exception as e:
            logger.warning(f"Could not fetch flowMedia URL for {media_id}: {e}")
            return None
