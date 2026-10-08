"""Per-account pauses that outlive a single manager or session.

Three independent clocks, all keyed by dolphin_id:
- account cooldown (403 limit / daily video quota): the account makes nothing;
- image cooldown (image model quota): the account still makes videos;
- video denied (plan lacks the video model): the account still makes images.
"""

import time

from loguru import logger

_COOLDOWNS: dict[str, float] = {}       # dolphin_id -> monotonic resume time
_COOLDOWN_HITS: dict[str, int] = {}     # dolphin_id -> consecutive cooldown count


def cooldown_remaining(dolphin_id: str | None) -> float:
    """Seconds until the account may generate again (0 when not on cooldown)."""
    if not dolphin_id:
        return 0.0
    return max(0.0, _COOLDOWNS.get(dolphin_id, 0.0) - time.monotonic())


# The image model has its own rolling ~24h quota (~1000 images per account); an account
# that hit it can still generate videos, so this cooldown is tracked separately.
_IMAGE_COOLDOWNS: dict[str, float] = {}
_IMAGE_COOLDOWN_HITS: dict[str, int] = {}
_IMAGE_COOLDOWN_MAX = 4 * 3600.0


def mark_image_cooldown(dolphin_id: str | None, account_name: str = "") -> float:
    """Put the account's image generation on exponential cooldown (5m → 10m → … → 4h). Returns seconds."""
    if not dolphin_id:
        return 0.0
    hits = _IMAGE_COOLDOWN_HITS.get(dolphin_id, 0) + 1
    _IMAGE_COOLDOWN_HITS[dolphin_id] = hits
    delay = min(300 * (2 ** (hits - 1)), _IMAGE_COOLDOWN_MAX)
    _IMAGE_COOLDOWNS[dolphin_id] = time.monotonic() + delay
    logger.bind(event_type="quota", account=account_name or None).warning(
        f"{account_name or dolphin_id}: image quota — images paused for {delay / 60:.0f}m (hit #{hits}), videos continue"
    )
    return delay


def image_cooldown_remaining(dolphin_id: str | None) -> float:
    """Seconds until the account may generate images again (0 when not on image cooldown)."""
    if not dolphin_id:
        return 0.0
    return max(0.0, _IMAGE_COOLDOWNS.get(dolphin_id, 0.0) - time.monotonic())


def reset_image_cooldown(dolphin_id: str | None) -> None:
    """Clear the image cooldown and its hit counter after a successful image."""
    if dolphin_id:
        _IMAGE_COOLDOWNS.pop(dolphin_id, None)
        _IMAGE_COOLDOWN_HITS.pop(dolphin_id, None)


# PUBLIC_ERROR_MODEL_ACCESS_DENIED on a video request means the account's plan does not
# include the requested video model while images still work. Videos are paused per account
# with an exponential pause, so a renewed plan is picked up again without a restart.
_VIDEO_DENIED: dict[str, float] = {}
_VIDEO_DENIED_HITS: dict[str, int] = {}
_VIDEO_DENIED_MAX = 4 * 3600.0


def mark_video_denied(dolphin_id: str | None, account_name: str = "") -> float:
    """Pause the account's video generation (30m → 1h → 2h → 4h). Returns seconds."""
    if not dolphin_id:
        return 0.0
    hits = _VIDEO_DENIED_HITS.get(dolphin_id, 0) + 1
    _VIDEO_DENIED_HITS[dolphin_id] = hits
    delay = min(1800 * (2 ** (hits - 1)), _VIDEO_DENIED_MAX)
    _VIDEO_DENIED[dolphin_id] = time.monotonic() + delay
    logger.bind(event_type="model_denied", account=account_name or None).warning(
        f"{account_name or dolphin_id}: no access to the video model — "
        f"videos paused for {delay / 60:.0f}m (hit #{hits}), images continue"
    )
    return delay


def video_denied_remaining(dolphin_id: str | None) -> float:
    """Seconds until the account may try videos again (0 when videos are allowed)."""
    if not dolphin_id:
        return 0.0
    return max(0.0, _VIDEO_DENIED.get(dolphin_id, 0.0) - time.monotonic())


def reset_video_denied(dolphin_id: str | None) -> None:
    """Clear the video pause and its hit counter after an accepted video request."""
    if dolphin_id:
        _VIDEO_DENIED.pop(dolphin_id, None)
        _VIDEO_DENIED_HITS.pop(dolphin_id, None)


def mark_cooldown(dolphin_id: str | None, account_name: str = "", max_seconds: float = 7200.0) -> float:
    """Put the whole account on exponential cooldown (5m → 10m → … → max). Returns seconds.

    403 limit uses the default 2h cap, the daily video quota passes 4h.
    """
    if not dolphin_id:
        return 0.0
    hits = _COOLDOWN_HITS.get(dolphin_id, 0) + 1
    _COOLDOWN_HITS[dolphin_id] = hits
    delay = min(300 * (2 ** (hits - 1)), max_seconds)
    _COOLDOWNS[dolphin_id] = time.monotonic() + delay
    logger.bind(event_type="cooldown", account=account_name or None).warning(
        f"{account_name or dolphin_id} on cooldown for {delay / 60:.0f}m (hit #{hits})"
    )
    return delay


def reset_cooldown(dolphin_id: str | None) -> None:
    """Clear the account cooldown and its hit counter after a successful generation."""
    if dolphin_id:
        _COOLDOWNS.pop(dolphin_id, None)
        _COOLDOWN_HITS.pop(dolphin_id, None)
