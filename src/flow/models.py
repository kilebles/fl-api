from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AspectRatio(str, Enum):
    """Supported video aspect ratios."""

    LANDSCAPE = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    PORTRAIT = "VIDEO_ASPECT_RATIO_PORTRAIT"
    SQUARE = "VIDEO_ASPECT_RATIO_SQUARE"


class RecaptchaContext(BaseModel):
    """reCAPTCHA verification context for API requests."""

    token: str
    applicationType: str = "RECAPTCHA_APPLICATION_TYPE_WEB"


class ClientContext(BaseModel):
    """Client context containing authentication and session info."""

    projectId: str
    recaptchaContext: RecaptchaContext
    sessionId: str
    tool: str = "PINHOLE"
    userPaygateTier: str | None = "PAYGATE_TIER_TWO"


class TextPart(BaseModel):
    """Single text part inside a structured prompt."""

    text: str


class StructuredPrompt(BaseModel):
    """Structured prompt containing a list of text parts."""

    parts: list[TextPart]


class TextInput(BaseModel):
    """Text prompt input for video generation."""

    structuredPrompt: StructuredPrompt


class MediaGenerationContext(BaseModel):
    """Batch context for media generation."""

    batchId: str
    audioFailurePreference: str | None = None


class VideoRequestItem(BaseModel):
    """Single video generation request within a batch."""

    aspectRatio: str = AspectRatio.LANDSCAPE.value
    seed: int = Field(ge=1, le=99999)
    textInput: TextInput
    videoModelKey: str = "veo_3_1_t2v_fast_ultra_relaxed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoGenerationRequest(BaseModel):
    """Complete request body for Veo video generation API."""

    clientContext: ClientContext
    mediaGenerationContext: MediaGenerationContext
    requests: list[VideoRequestItem]
    useV2ModelConfig: bool = True


class OperationInfo(BaseModel):
    """Operation info from API response."""

    operation_name: str
    scene_id: str
    status: str


class MediaInfo(BaseModel):
    """Media info from API response."""

    media_id: str
    project_id: str
    workflow_id: str
    status: str
    prompt: str | None = None
    seed: int | None = None
    video_url: str | None = None


class UploadImageClientContext(BaseModel):
    """Minimal client context for image upload requests."""

    projectId: str
    tool: str = "PINHOLE"


class UploadImageRequest(BaseModel):
    """Request body for uploading an image to Flow."""

    clientContext: UploadImageClientContext
    fileName: str
    imageBytes: str
    isHidden: bool = False
    isUserUploaded: bool = True
    mimeType: str


class ReferenceImage(BaseModel):
    """Reference image for image-to-video generation."""

    mediaId: str
    imageUsageType: str = "IMAGE_USAGE_TYPE_ASSET"


class CropCoordinates(BaseModel):
    """Crop coordinates for startImage in f2v generation."""

    top: float = 0.0
    left: float = 0.0
    bottom: float = 1.0
    right: float = 1.0


class StartImage(BaseModel):
    """Start image for frame-to-video (f2v) chained generation."""

    mediaId: str
    cropCoordinates: CropCoordinates = Field(default_factory=CropCoordinates)


class F2vRequestItem(BaseModel):
    """Single frame-to-video request within a batch."""

    aspectRatio: str = AspectRatio.LANDSCAPE.value
    seed: int = Field(ge=1, le=99999)
    textInput: TextInput
    videoModelKey: str = "veo_3_1_i2v_lite_low_priority"
    metadata: dict[str, Any] = Field(default_factory=dict)
    startImage: StartImage


class F2vGenerationRequest(BaseModel):
    """Request body for frame-to-video generation."""

    clientContext: ClientContext
    mediaGenerationContext: MediaGenerationContext
    requests: list[F2vRequestItem]
    useV2ModelConfig: bool = True


class ImageVideoRequestItem(BaseModel):
    """Single image-to-video generation request within a batch."""

    aspectRatio: str = AspectRatio.LANDSCAPE.value
    seed: int = Field(ge=1, le=99999)
    textInput: TextInput
    videoModelKey: str = "veo_3_1_r2v_lite_low_priority"
    metadata: dict[str, Any] = Field(default_factory=dict)
    referenceImages: list[ReferenceImage]


class ImageVideoGenerationRequest(BaseModel):
    """Request body for image-to-video generation."""

    clientContext: ClientContext
    mediaGenerationContext: MediaGenerationContext
    requests: list[ImageVideoRequestItem]
    useV2ModelConfig: bool = True


class ImageInput(BaseModel):
    """Uploaded image a generation request uses as reference (image-to-image)."""

    name: str


class ImageRequestItem(BaseModel):
    """Single image generation request within a batch."""

    clientContext: ClientContext
    imageAspectRatio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE"
    imageInputs: list[ImageInput] = Field(default_factory=list)
    imageModelName: str = "BELUGA"
    seed: int = Field(ge=1, le=999999)
    structuredPrompt: StructuredPrompt


class ImageGenerationRequest(BaseModel):
    """Complete request body for flowMedia:batchGenerateImages API."""

    clientContext: ClientContext
    mediaGenerationContext: MediaGenerationContext
    requests: list[ImageRequestItem]
    useNewMedia: bool = True


class VideoGenerationResponse(BaseModel):
    """Response from Veo video generation API."""

    operations: list[OperationInfo] = Field(default_factory=list)
    media: list[MediaInfo] = Field(default_factory=list)
    remaining_credits: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "VideoGenerationResponse":
        """Parse raw API response into structured model."""
        operations = []
        for op in data.get("operations", []):
            operations.append(
                OperationInfo(
                    operation_name=op.get("operation", {}).get("name", ""),
                    scene_id=op.get("sceneId", ""),
                    status=op.get("status", ""),
                )
            )

        media = []
        for m in data.get("media", []):
            metadata = m.get("mediaMetadata", {})
            video = m.get("video", {}).get("generatedVideo", {})
            media.append(
                MediaInfo(
                    media_id=m.get("name", ""),
                    project_id=m.get("projectId", ""),
                    workflow_id=m.get("workflowId", ""),
                    status=metadata.get("mediaStatus", {}).get("mediaGenerationStatus", ""),
                    prompt=video.get("prompt"),
                    seed=video.get("seed"),
                )
            )

        return cls(
            operations=operations,
            media=media,
            remaining_credits=data.get("remainingCredits"),
            raw=data,
        )
