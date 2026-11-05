from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from src.core.settings import Settings


class Mode(StrEnum):
    T2V = "t2v"
    FLASH_T2V = "flash_t2v"
    IMG = "img"
    I2V = "i2v"
    FLASH_I2V = "flash_i2v"
    I2I2V = "i2i2v"
    F2V = "f2v"


class Strategy(StrEnum):
    PARALLEL = "parallel"      # one worker per account, all accounts at once
    SEQUENTIAL = "sequential"  # one account at a time, rotating to the next on limits


START_FRAME_MODELS: frozenset[str] = frozenset({
    "veo_3_1_i2v_s_lite_4s_low_priority",
    "veo_3_1_i2v_s_lite_6s_low_priority",
    "veo_3_1_i2v_lite_low_priority",
    "veo_3_1_i2v_s_lite_4s",
    "veo_3_1_i2v_s_lite_6s",
    "veo_3_1_i2v_lite",
    "abra_i2v_4s",
    "abra_i2v_6s",
    "abra_i2v_8s",
    "abra_i2v_10s",
})


def is_start_frame_model(model_key: str | None) -> bool:
    """True when `model_key` requires Flow's `batchAsyncGenerateVideoStartImage` endpoint instead of `referenceImages`."""
    if not model_key:
        return False
    return (
        model_key in START_FRAME_MODELS
        or "_i2v_s_" in model_key
        or model_key.startswith("abra_i2v_")
    )


@dataclass(frozen=True)
class ModeSpec:
    """What a mode needs from each row and what it produces."""

    title: str
    makes_images: bool
    makes_videos: bool
    needs_video_prompt: bool
    needs_image_prompt: bool
    chain: bool = False
    avatar: bool = False
    optional_avatar: bool = False
    static_image: bool = False
    durations: tuple[int, ...] = ()
    default_duration: int | None = None
    aspect_ratios: tuple[str, ...] = ("16:9", "9:16")
    default_aspect_ratio: str = "16:9"
    duration_models: dict[int, str] = field(default_factory=dict)
    frame_duration_models: dict[int, str] = field(default_factory=dict)
    default_video_model: Callable[[Settings], str | None] = lambda settings: None

    def video_model(self, settings: Settings, duration: int | None = None) -> str | None:
        if duration is not None and duration in self.duration_models:
            return self.duration_models[duration]
        return self.default_video_model(settings)

    def frame_video_model(self, settings: Settings, duration: int | None = None) -> str | None:
        if duration is not None and duration in self.frame_duration_models:
            return self.frame_duration_models[duration]
        return None


_VEO_T2V_BY_DURATION = {
    4: "veo_3_1_t2v_lite_4s_low_priority",
    6: "veo_3_1_t2v_lite_6s_low_priority",
    8: "veo_3_1_t2v_lite_low_priority",
}

_VEO_I2V_BY_DURATION = {
    4: "veo_3_1_i2v_s_lite_4s_low_priority",
    6: "veo_3_1_i2v_s_lite_6s_low_priority",
    8: "veo_3_1_r2v_lite_low_priority",
}

_FLASH_T2V_BY_DURATION = {
    4: "abra_t2v_4s",
    6: "abra_t2v_6s",
    8: "abra_t2v_8s",
    10: "abra_t2v_10s",
}

_FLASH_I2V_BY_DURATION = {
    4: "abra_r2v_4s",
    6: "abra_r2v_6s",
    8: "abra_r2v_8s",
    10: "abra_r2v_10s",
}

_F2V_FIRST_BY_DURATION = {
    4: "veo_3_1_t2v_lite_4s_low_priority",
    6: "veo_3_1_t2v_lite_6s_low_priority",
    8: "veo_3_1_t2v_lite_low_priority",
    10: "abra_t2v_10s",
}

_F2V_FRAME_BY_DURATION = {
    4: "veo_3_1_i2v_s_lite_4s_low_priority",
    6: "veo_3_1_i2v_s_lite_6s_low_priority",
    8: "veo_3_1_i2v_lite_low_priority",
    10: "abra_i2v_10s",
}


MODES: dict[Mode, ModeSpec] = {
    Mode.T2V: ModeSpec(
        title="Text → video",
        makes_images=False, makes_videos=True, needs_video_prompt=True, needs_image_prompt=False,
        durations=(4, 6, 8), default_duration=8,
        duration_models=_VEO_T2V_BY_DURATION,
    ),
    Mode.FLASH_T2V: ModeSpec(
        title="Text → video (flash model)",
        makes_images=False, makes_videos=True, needs_video_prompt=True, needs_image_prompt=False,
        durations=(4, 6, 8, 10), default_duration=10,
        duration_models=_FLASH_T2V_BY_DURATION,
        default_video_model=lambda s: s.flash_t2v_model_key,
    ),
    Mode.IMG: ModeSpec(
        title="Text → image",
        makes_images=True, makes_videos=False, needs_video_prompt=False, needs_image_prompt=True,
        optional_avatar=True,
        aspect_ratios=("16:9", "9:16", "1:1"),
    ),
    Mode.I2V: ModeSpec(
        title="Text → image → video (or one static image → video)",
        makes_images=True, makes_videos=True, needs_video_prompt=True, needs_image_prompt=True,
        static_image=True,
        durations=(4, 6, 8), default_duration=8,
        duration_models=_VEO_I2V_BY_DURATION,
    ),
    Mode.FLASH_I2V: ModeSpec(
        title="Text → image → video (flash model)",
        makes_images=True, makes_videos=True, needs_video_prompt=True, needs_image_prompt=True,
        durations=(4, 6, 8, 10), default_duration=10,
        duration_models=_FLASH_I2V_BY_DURATION,
        default_video_model=lambda s: s.flash_i2v_model_key,
    ),
    Mode.I2I2V: ModeSpec(
        title="Avatar + text → image → video",
        makes_images=True, makes_videos=True, needs_video_prompt=True, needs_image_prompt=True,
        avatar=True,
        durations=(4, 6, 8), default_duration=8,
        duration_models=_VEO_I2V_BY_DURATION,
    ),
    Mode.F2V: ModeSpec(
        title="Chained clips: each starts from the previous clip's last frame",
        makes_images=False, makes_videos=True, needs_video_prompt=True, needs_image_prompt=False,
        chain=True,
        durations=(4, 6, 8, 10), default_duration=8,
        duration_models=_F2V_FIRST_BY_DURATION,
        frame_duration_models=_F2V_FRAME_BY_DURATION,
        default_video_model=lambda s: s.flash_t2v_model_key,
    ),
}
