import base64
import mimetypes
import random
import time
import uuid
from pathlib import Path

import httpx
from loguru import logger

from src.core.errors import GenerationFilteredError
from src.core.settings import Settings
from src.flow.http import API_BASE, api_endpoint, api_headers, error_reason
from src.flow.models import (
    ClientContext,
    CropCoordinates,
    F2vGenerationRequest,
    F2vRequestItem,
    ImageVideoGenerationRequest,
    ImageVideoRequestItem,
    MediaGenerationContext,
    RecaptchaContext,
    ReferenceImage,
    StartImage,
    StructuredPrompt,
    TextInput,
    TextPart,
    UploadImageClientContext,
    UploadImageRequest,
    VideoGenerationRequest,
    VideoGenerationResponse,
    VideoRequestItem,
)

_UPLOAD_FILTER_REASONS = ("PUBLIC_ERROR_MINOR_UPLOAD", "PUBLIC_ERROR_UNSAFE_GENERATION", "PUBLIC_ERROR_MINOR")


class VideoGenerationService:
    """Veo requests: text-to-video, image-to-video (reference image), frame-to-video, image upload."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def generate(
        self,
        prompt: str,
        authorization: str,
        project_id: str,
        recaptcha_token: str,
        proxy_url: str | None = None,
        browser_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        model_key: str | None = None,
    ) -> VideoGenerationResponse:
        """Submit a text-to-video request; returns the accepted media."""
        logger.info(f"Sending generation request: {prompt}")
        request = VideoGenerationRequest(
            clientContext=self._client_context(project_id, recaptcha_token),
            mediaGenerationContext=self._media_context(),
            requests=[
                VideoRequestItem(
                    aspectRatio=self._settings.video_aspect_ratio,
                    seed=random.randint(1, 99999),
                    textInput=self._text_input(prompt),
                    videoModelKey=model_key or self._settings.video_model_key,
                )
            ],
        )
        return await self._submit(self._settings.api_url, request.model_dump_json(), authorization, proxy_url, browser_headers, cookies, "Generation")

    async def generate_from_image(
        self,
        prompt: str,
        media_id: str,
        authorization: str,
        project_id: str,
        recaptcha_token: str,
        proxy_url: str | None = None,
        browser_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        model_key: str | None = None,
    ) -> VideoGenerationResponse:
        """Submit an image-to-video request with an uploaded image as the reference."""
        logger.info(f"Sending i2v generation request: {prompt}")
        request = ImageVideoGenerationRequest(
            clientContext=self._client_context(project_id, recaptcha_token),
            mediaGenerationContext=self._media_context(),
            requests=[
                ImageVideoRequestItem(
                    aspectRatio=self._settings.video_aspect_ratio,
                    seed=random.randint(1, 99999),
                    textInput=self._text_input(prompt),
                    videoModelKey=model_key or self._settings.i2v_model_key,
                    referenceImages=[ReferenceImage(mediaId=media_id)],
                )
            ],
        )
        url = f"{API_BASE}/video:batchAsyncGenerateVideoReferenceImages"
        return await self._submit(url, request.model_dump_json(), authorization, proxy_url, browser_headers, cookies, "I2V generation")

    async def generate_from_frame(
        self,
        prompt: str,
        media_id: str,
        authorization: str,
        project_id: str,
        recaptcha_token: str,
        proxy_url: str | None = None,
        browser_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        model_key: str | None = None,
        crop_coordinates: CropCoordinates | None = None,
    ) -> VideoGenerationResponse:
        """Submit a frame-to-video request: the uploaded image is the clip's first frame (f2v chains)."""
        logger.info(f"Sending f2v generation request: {prompt}")
        request = F2vGenerationRequest(
            clientContext=self._client_context(project_id, recaptcha_token),
            mediaGenerationContext=self._media_context(),
            requests=[
                F2vRequestItem(
                    aspectRatio=self._settings.video_aspect_ratio,
                    seed=random.randint(1, 99999),
                    textInput=self._text_input(prompt),
                    videoModelKey=model_key or self._settings.f2v_model_key,
                    startImage=StartImage(mediaId=media_id, cropCoordinates=crop_coordinates or CropCoordinates()),
                )
            ],
        )
        url = f"{API_BASE}/video:batchAsyncGenerateVideoStartImage"
        return await self._submit(url, request.model_dump_json(), authorization, proxy_url, browser_headers, cookies, "F2V generation")

    async def upload_image(
        self,
        image_path: Path,
        authorization: str,
        project_id: str,
        proxy_url: str | None = None,
        browser_headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> str:
        """Upload an image into the Flow project and return its media id."""
        request = UploadImageRequest(
            clientContext=UploadImageClientContext(projectId=project_id),
            fileName=image_path.name,
            imageBytes=base64.b64encode(image_path.read_bytes()).decode(),
            mimeType=mimetypes.guess_type(str(image_path))[0] or "image/jpeg",
        )
        headers = api_headers(self._settings.flow_base_url, authorization, browser_headers, browser_meta=False)
        upload_url = api_endpoint(f"{API_BASE}/flow/uploadImage", authorization)
        async with httpx.AsyncClient(proxy=proxy_url, cookies=cookies) as client:
            response = await client.post(
                upload_url,
                content=request.model_dump_json(),
                headers=headers,
                timeout=self._settings.api_timeout,
            )

        if response.status_code != 200:
            reason = error_reason(response) if response.status_code == 400 else ""
            if reason in _UPLOAD_FILTER_REASONS:
                logger.warning(f"Upload image filtered ({reason})")
                raise GenerationFilteredError(f"{reason}: upload image filtered")
            logger.error(f"Upload image error: {response.status_code} - {response.text}")
            response.raise_for_status()

        media_id = response.json()["media"]["name"]
        logger.success(f"Image uploaded, media_id: {media_id}")
        return media_id

    async def _submit(
        self,
        url: str,
        body: str,
        authorization: str,
        proxy_url: str | None,
        browser_headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        label: str,
    ) -> VideoGenerationResponse:
        headers = api_headers(self._settings.flow_base_url, authorization, browser_headers)
        target_url = api_endpoint(url, authorization)
        async with httpx.AsyncClient(proxy=proxy_url, cookies=cookies, http2=True) as client:
            response = await client.post(target_url, content=body, headers=headers, timeout=self._settings.api_timeout)

        if response.status_code != 200:
            log = logger.warning if response.status_code == 429 else logger.error
            log(f"API error: {response.status_code} - {response.text}")
            response.raise_for_status()

        result = VideoGenerationResponse.from_api_response(response.json())
        logger.success(f"{label} response: {result.raw}")
        return result

    @staticmethod
    def _client_context(project_id: str, recaptcha_token: str) -> ClientContext:
        return ClientContext(
            projectId=project_id,
            sessionId=f";{int(time.time() * 1000)}",
            recaptchaContext=RecaptchaContext(token=recaptcha_token),
        )

    @staticmethod
    def _media_context() -> MediaGenerationContext:
        return MediaGenerationContext(batchId=str(uuid.uuid4()), audioFailurePreference="BLOCK_SILENCED_VIDEOS")

    @staticmethod
    def _text_input(prompt: str) -> TextInput:
        return TextInput(structuredPrompt=StructuredPrompt(parts=[TextPart(text=prompt)]))
