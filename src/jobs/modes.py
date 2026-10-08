from collections.abc import Callable
from dataclasses import dataclass
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
    static_image: bool = False
    video_model: Callable[[Settings], str | None] = lambda settings: None


MODES: dict[Mode, ModeSpec] = {
    Mode.T2V: ModeSpec(
        title="Text → video",
        makes_images=False, makes_videos=True, needs_video_prompt=True, needs_image_prompt=False,
    ),
    Mode.FLASH_T2V: ModeSpec(
        title="Text → video (flash model)",
        makes_images=False, makes_videos=True, needs_video_prompt=True, needs_image_prompt=False,
        video_model=lambda s: s.flash_t2v_model_key,
    ),
    Mode.IMG: ModeSpec(
        title="Text → image",
        makes_images=True, makes_videos=False, needs_video_prompt=False, needs_image_prompt=True,
    ),
    Mode.I2V: ModeSpec(
        title="Text → image → video (or one static image → video)",
        makes_images=True, makes_videos=True, needs_video_prompt=True, needs_image_prompt=True,
        static_image=True,
    ),
    Mode.FLASH_I2V: ModeSpec(
        title="Text → image → video (flash model)",
        makes_images=True, makes_videos=True, needs_video_prompt=True, needs_image_prompt=True,
        video_model=lambda s: s.flash_i2v_model_key,
    ),
    Mode.I2I2V: ModeSpec(
        title="Avatar + text → image → video",
        makes_images=True, makes_videos=True, needs_video_prompt=True, needs_image_prompt=True,
        avatar=True,
    ),
    Mode.F2V: ModeSpec(
        title="Chained clips: each starts from the previous clip's last frame",
        makes_images=False, makes_videos=True, needs_video_prompt=True, needs_image_prompt=False,
        chain=True, video_model=lambda s: s.flash_t2v_model_key,
    ),
}
