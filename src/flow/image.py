import errno
import random
import time
import uuid
from pathlib import Path

import httpx
from loguru import logger

from src.core.errors import DiskFullError, GenerationFilteredError
from src.core.settings import Settings
from src.flow.http import API_BASE, api_endpoint, api_headers, error_reason
from src.flow.models import (
    ClientContext,
    ImageGenerationRequest,
    ImageInput,
    ImageRequestItem,
    MediaGenerationContext,
    RecaptchaContext,
    StructuredPrompt,
    TextPart,
)

_DIRECT_URL_KEYS = ("fifeUrl", "downloadUrl", "servingBaseUri", "imageUrl", "url")
_FILTER_REASONS = ("PUBLIC_ERROR_UNSAFE_GENERATION", "PUBLIC_ERROR_MINOR")


def _find_direct_url(media: dict) -> str | None:
    """Return a direct https URL for a generated image if the API included one."""
    image = media.get("image", {}).get("generatedImage", {}) if isinstance(media.get("image"), dict) else {}
    for source in (media, image):
        for key in _DIRECT_URL_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
    return None


class ImageGenerationService:
    """
    Image generation via flowMedia:batchGenerateImages (model from settings.image_model_name,
    BELUGA = Nano Banana 2.1 since 2026-10-13) and download of the results.
    """

    IMAGE_API_URL_TEMPLATE = API_BASE + "/projects/{project_id}/flowMedia:batchGenerateImages"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._direct_urls: dict[str, str] = {}

    async def generate(
        self,
        prompt: str,
        project_id: str,
        recaptcha_token: str,
        authorization: str | None = None,
        proxy_url: str | None = None,
        browser_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        reference_media_ids: list[str] | None = None,
    ) -> list[str]:
        """
        Submit a text-to-image generation request to Flow API.

        Args:
            prompt: Text description of the image to generate.
            authorization: Authorization header value captured from the browser.
            project_id: Google Labs project identifier.
            recaptcha_token: Valid reCAPTCHA Enterprise token.
            reference_media_ids: Media ids of uploaded pictures the model must keep
                (image-to-image, e.g. an avatar). Uses the request's `imageInputs`.

        Returns:
            List of media IDs for the generated images.

        Raises:
            httpx.HTTPStatusError: If API returns non-200 response.
        """
        logger.info(
            f"Sending image generation request ({self._settings.image_model_name}): {prompt}"
            + (f" (reference media: {reference_media_ids})" if reference_media_ids else "")
        )

        timestamp_ms = int(time.time() * 1000)
        seed = random.randint(1, 999999)

        client_context = ClientContext(
            projectId=project_id,
            sessionId=f";{timestamp_ms}",
            recaptchaContext=RecaptchaContext(token=recaptcha_token),
            userPaygateTier=None,
        )

        request = ImageGenerationRequest(
            clientContext=client_context,
            mediaGenerationContext=MediaGenerationContext(batchId=str(uuid.uuid4())),
            requests=[
                ImageRequestItem(
                    clientContext=client_context,
                    seed=seed,
                    imageInputs=[ImageInput(name=m) for m in reference_media_ids or []],
                    imageModelName=self._settings.image_model_name,
                    structuredPrompt=StructuredPrompt(parts=[TextPart(text=prompt)]),
                )
            ],
        )

        headers = api_headers(self._settings.flow_base_url, authorization, browser_headers)
        url = api_endpoint(self.IMAGE_API_URL_TEMPLATE.format(project_id=project_id), authorization)
        body = request.model_dump_json(exclude_none=True)

        async with httpx.AsyncClient(proxy=proxy_url, cookies=cookies, http2=True) as client:
            response = await client.post(
                url,
                content=body,
                headers=headers,
                timeout=self._settings.image_api_timeout,
            )

        if response.status_code != 200:
            reason = error_reason(response) if response.status_code == 400 else ""
            if reason == "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED":
                logger.warning(f"Image filtered (prominent people): {prompt[:80]}")
                raise GenerationFilteredError(f"Prominent people filter: {prompt[:80]}")
            if reason in _FILTER_REASONS:
                logger.warning(f"Image filtered ({reason}): {prompt[:80]}")
                raise GenerationFilteredError(f"{reason}: {prompt[:80]}")
            log = logger.warning if response.status_code == 429 else logger.error
            log(f"API error: {response.status_code} - {response.text}")
            response.raise_for_status()

        data = response.json()
        logger.success(f"Image generation response: {data}")

        remaining = data.get("remainingCredits")
        if remaining is not None:
            logger.info(f"Remaining credits: {remaining}")

        media_ids = [m["name"] for m in data.get("media", []) if "name" in m]
        for m in data.get("media", []):
            direct_url = _find_direct_url(m)
            if "name" in m and direct_url:
                self._direct_urls[m["name"]] = direct_url
        logger.success(f"Generated {len(media_ids)} image(s): {media_ids}")
        return media_ids

    async def download(
        self,
        media_id: str,
        cookies: dict[str, str],
        output_dir: Path | None = None,
        proxy_url: str | None = None,
    ) -> Path:
        """
        Download a generated image by media ID.

        Args:
            media_id: Media ID returned from generate().
            cookies: Browser cookies for authenticated download.
            output_dir: Directory to save the image (defaults to settings.download_dir).

        Returns:
            Path to the downloaded image file.
        """
        save_dir = output_dir or self._settings.download_dir
        save_dir.mkdir(parents=True, exist_ok=True)

        direct_url = self._direct_urls.pop(media_id, None)
        if direct_url:
            try:
                return await self._download_image(media_id, direct_url, save_dir)
            except Exception as e:
                logger.warning(f"Direct image URL failed for {media_id}: {e} — falling back to redirect endpoint")
        redirect_url = await self._get_redirect_url(media_id, cookies, proxy_url)
        return await self._download_image(media_id, redirect_url, save_dir)

    async def _get_redirect_url(
        self,
        media_id: str,
        cookies: dict[str, str],
        proxy_url: str | None,
    ) -> str:
        """Resolve the signed storage URL via the legacy Labs tRPC endpoint (needs a labs.google session)."""
        url = f"{self._settings.labs_base_url}/fx/api/trpc/media.getMediaUrlRedirect"

        async with httpx.AsyncClient(
            timeout=self._settings.api_timeout,
            follow_redirects=False,
            proxy=proxy_url,
            http2=True,
        ) as client:
            response = await client.get(url, params={"name": media_id}, cookies=cookies)

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if location:
                logger.debug(f"Image redirect URL: {location}")
                return location

        response.raise_for_status()
        raise RuntimeError(f"Expected redirect, got {response.status_code}")

    async def _download_image(self, media_id: str, image_url: str, save_dir: Path) -> Path:
        """Stream-download image from signed URL."""
        output_path = save_dir / f"{media_id}.jpg"

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", image_url) as response:
                response.raise_for_status()
                try:
                    with open(output_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                except OSError as e:
                    output_path.unlink(missing_ok=True)
                    if e.errno == errno.ENOSPC:
                        raise DiskFullError(f"No space left on device while saving {output_path}") from e
                    raise

        logger.success(f"Image downloaded: {output_path}")
        return output_path
