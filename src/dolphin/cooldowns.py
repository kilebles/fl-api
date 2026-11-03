"""Per-account pauses that outlive a single manager or session.

Three independent clocks, all keyed by dolphin_id (with image model quotas tracked per model):
- account cooldown (403 limit / daily video quota): the account makes nothing;
- image cooldown (per-model ~24h image quota): when one model (e.g. BELUGA / Nano Banana 2.1)
  hits quota, the account automatically switches to the next configured model (e.g. GEM_PIX_2 /
  Nano Banana Pro); only when all image models are on cooldown does the account pause images
  while continuing videos;
- video denied (plan lacks the video model): the account still makes images.
"""

from collections.abc import Sequence
import time

from loguru import logger

_COOLDOWNS: dict[str, float] = {}       # dolphin_id -> monotonic resume time
_COOLDOWN_HITS: dict[str, int] = {}     # dolphin_id -> consecutive cooldown count


def cooldown_remaining(dolphin_id: str | None) -> float:
    """Seconds until the account may generate again (0 when not on cooldown)."""
    if not dolphin_id:
        return 0.0
    return max(0.0, _COOLDOWNS.get(dolphin_id, 0.0) - time.monotonic())


# Each image model (BELUGA = Nano Banana 2.1, GEM_PIX_2 = Nano Banana Pro) has its own
# rolling ~24h quota (~1000 images per account per model). Account-level _IMAGE_COOLDOWNS
# reflects when ALL configured image models on the account are simultaneously paused.
_IMAGE_COOLDOWNS: dict[str, float] = {}
_IMAGE_COOLDOWN_HITS: dict[str, int] = {}
_MODEL_IMAGE_COOLDOWNS: dict[tuple[str, str], float] = {}
_MODEL_IMAGE_COOLDOWN_HITS: dict[tuple[str, str], int] = {}
_IMAGE_COOLDOWN_MAX = 4 * 3600.0


def image_model_cooldown_remaining(dolphin_id: str | None, model: str) -> float:
    """Seconds until `model` may generate images on `dolphin_id` again (0 when ready)."""
    if not dolphin_id:
        return 0.0
    return max(0.0, _MODEL_IMAGE_COOLDOWNS.get((dolphin_id, model), 0.0) - time.monotonic())


def pick_image_model(dolphin_id: str | None, models: Sequence[str]) -> str | None:
    """Return the first configured image model not currently on cooldown for `dolphin_id`."""
    if not models:
        return None
    if not dolphin_id:
        return models[0]
    has_model_entries = any((dolphin_id, m) in _MODEL_IMAGE_COOLDOWNS for m in models)
    if not has_model_entries and _IMAGE_COOLDOWNS.get(dolphin_id, 0.0) > time.monotonic():
        return None
    for model in models:
        if image_model_cooldown_remaining(dolphin_id, model) <= 0:
            return model
    return None


def mark_image_cooldown(
    dolphin_id: str | None,
    account_name: str = "",
    model: str | None = None,
    models: Sequence[str] | None = None,
) -> float:
    """Put an image model (or the whole account's images) on exponential cooldown (5m → 10m → … → 4h)."""
    if not dolphin_id:
        return 0.0
    now = time.monotonic()
    if model:
        key = (dolphin_id, model)
        hits = _MODEL_IMAGE_COOLDOWN_HITS.get(key, 0) + 1
        _MODEL_IMAGE_COOLDOWN_HITS[key] = hits
        delay = min(300 * (2 ** (hits - 1)), _IMAGE_COOLDOWN_MAX)
        _MODEL_IMAGE_COOLDOWNS[key] = now + delay

        pool = list(models) if models else [model]
        next_model = pick_image_model(dolphin_id, pool)
        if next_model is not None:
            logger.bind(event_type="quota", account=account_name or None).warning(
                f"{account_name or dolphin_id}: image quota on {model} — model paused for {delay / 60:.0f}m "
                f"(hit #{hits}), switching to {next_model}"
            )
            return delay

        acc_hits = _IMAGE_COOLDOWN_HITS.get(dolphin_id, 0) + 1
        _IMAGE_COOLDOWN_HITS[dolphin_id] = acc_hits
        earliest_resume = min(_MODEL_IMAGE_COOLDOWNS.get((dolphin_id, m), now + delay) for m in pool)
        _IMAGE_COOLDOWNS[dolphin_id] = earliest_resume
        wait = max(0.0, earliest_resume - now)
        logger.bind(event_type="quota", account=account_name or None).warning(
            f"{account_name or dolphin_id}: image quota on {model} (all {len(pool)} image models exhausted) — "
            f"images paused for {wait / 60:.0f}m (hit #{acc_hits}), videos continue"
        )
        return wait

    hits = _IMAGE_COOLDOWN_HITS.get(dolphin_id, 0) + 1
    _IMAGE_COOLDOWN_HITS[dolphin_id] = hits
    delay = min(300 * (2 ** (hits - 1)), _IMAGE_COOLDOWN_MAX)
    _IMAGE_COOLDOWNS[dolphin_id] = now + delay
    logger.bind(event_type="quota", account=account_name or None).warning(
        f"{account_name or dolphin_id}: image quota — images paused for {delay / 60:.0f}m (hit #{hits}), videos continue"
    )
    return delay


def image_cooldown_remaining(dolphin_id: str | None, models: Sequence[str] | None = None) -> float:
    """Seconds until the account may generate images again with at least one model (0 when ready)."""
    if not dolphin_id:
        return 0.0
    if models:
        if pick_image_model(dolphin_id, models) is not None:
            return 0.0
        has_model_entries = any((dolphin_id, m) in _MODEL_IMAGE_COOLDOWNS for m in models)
        if has_model_entries:
            return min(image_model_cooldown_remaining(dolphin_id, m) for m in models)
    return max(0.0, _IMAGE_COOLDOWNS.get(dolphin_id, 0.0) - time.monotonic())


def image_quota_limited(dolphin_id: str | None) -> bool:
    """True while all image models on the account hit quota (no image succeeded since).

    The cooldown clock is only a retry pacer: it can run out long before the rolling
    ~24h quota frees up, so "limited" is tracked by the hit counter instead.
    """
    return bool(dolphin_id) and _IMAGE_COOLDOWN_HITS.get(dolphin_id, 0) > 0


def reset_image_cooldown(dolphin_id: str | None, model: str | None = None) -> None:
    """Clear the image cooldown after a successful image (for `model`, or all models when omitted)."""
    if not dolphin_id:
        return
    _IMAGE_COOLDOWNS.pop(dolphin_id, None)
    _IMAGE_COOLDOWN_HITS.pop(dolphin_id, None)
    if model is not None:
        _MODEL_IMAGE_COOLDOWNS.pop((dolphin_id, model), None)
        _MODEL_IMAGE_COOLDOWN_HITS.pop((dolphin_id, model), None)
    else:
        for key in [k for k in _MODEL_IMAGE_COOLDOWNS if k[0] == dolphin_id]:
            _MODEL_IMAGE_COOLDOWNS.pop(key, None)
        for key in [k for k in _MODEL_IMAGE_COOLDOWN_HITS if k[0] == dolphin_id]:
            _MODEL_IMAGE_COOLDOWN_HITS.pop(key, None)


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
