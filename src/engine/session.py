"""One browser session (a Dolphin profile + reCAPTCHA producer) and the recovery policy around every Flow request."""

import asyncio
import random
import shutil
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import httpx
from loguru import logger

from src.core.errors import (
    ConsecutiveFailuresError,
    DiskFullError,
    GenerationFilteredError,
    GenerationTimedOutError,
    ImageQuotaError,
    IpProhibitedError,
    RateLimitError,
    RecaptchaInterfaceError,
    VideoAccessDeniedError,
)
from src.core.settings import Settings
from src.dolphin import cooldowns
from src.dolphin.local_api import DolphinNotRunningError
from src.dolphin.manager import DolphinAntyManager
from src.flow.browser.recaptcha import RecaptchaTokenProducer
from src.flow.credentials import CredentialsService
from src.flow.downloader import VideoDownloaderService
from src.flow.http import MODEL_ACCESS_DENIED, QUOTA_REACHED, TOO_MUCH_TRAFFIC, error_reason
from src.flow.image import ImageGenerationService
from src.flow.polling import PollingService
from src.flow.video import VideoGenerationService

T = TypeVar("T")

ACCOUNT_ROTATION_403_LIMIT = 3
ACCOUNT_ROTATION_429_LIMIT = 4
ACCOUNT_ROTATION_TIMEOUT_LIMIT = 2  # consecutive server-side TIMED_OUT results before rotating
PROXY_COOLDOWN_AFTER_ROTATIONS = 2  # 403-limit rotations on the same proxy before the account cools down
DELAY_403 = (7.0, 14.0)
DELAY_403_REPEAT = (30.0, 60.0)
DELAY_429_STEPS = (7.0, 30.0, 90.0)  # pause for all requests of the session after the 1st, 2nd, 3rd consecutive 429
DELAY_TRAFFIC_STEPS = (120.0, 300.0, 600.0)  # pause on 429 TOO_MUCH_TRAFFIC (no rotation)
TRAFFIC_QUIET_RESET = 900.0
TOKEN_TIMEOUT = 120.0
DOLPHIN_TIMEOUT = 600.0
START_RETRY_DELAYS = (30.0, 60.0, 120.0)
DAILY_QUOTA_COOLDOWN_MAX = 4 * 3600.0

# Errors that no retry or rotation can fix: the whole job stops.
FATAL_ERRORS: tuple[type[BaseException], ...] = (DiskFullError, RecaptchaInterfaceError, DolphinNotRunningError)

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task:
    """Run a fire-and-forget task while keeping a strong reference so it is not GC'd."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class _Count403(Enum):
    WAIT = "wait"          # below the limit: pause, then retry with a fresh token
    ROTATE = "rotate"      # this request reached the limit: count the proxy failure and rotate
    PENDING = "pending"    # another request already triggered rotation: just follow it


@dataclass(frozen=True)
class SubmittedVideo:
    """A video request Flow accepted; poll it with `AccountSession.finish_video`."""

    media_id: str
    project_id: str
    proxy_url: str | None
    cookies: dict[str, str]


class AccountSession:
    """
    Drives one Dolphin manager: keeps its browser and reCAPTCHA producer alive, sends
    Flow requests with a fresh token each, and applies the recovery policy:

    - 401: sign in to labs.google again once, then rotate;
    - 403: pause 7–14s, then 30–60s; the 3rd in a row rotates (2 such rotations on the
      same proxy put the account on cooldown); MODEL_ACCESS_DENIED pauses only videos
      (or only images for an image request);
    - 429: daily quota → account cooldown + rotation (images: image cooldown only),
      TOO_MUCH_TRAFFIC → 2/5/10 min pause, others → 7/30/90s pause, 4th in a row rotates;
    - 400: permanent skip of the item.

    With a multi-account manager, rotation moves to the next account (sequential
    strategy); with a single account it restarts the same profile, and does nothing
    while that account is on cooldown so the caller can stop using it.
    """

    def __init__(self, settings: Settings, manager: DolphinAntyManager, poll_pause: tuple[float, float] = (11.0, 14.0)) -> None:
        self._settings = settings
        self._manager = manager
        self._poll_pause = poll_pause
        self._credentials = CredentialsService(origin=settings.flow_base_url)
        self._video = VideoGenerationService(settings)
        self._image = ImageGenerationService(settings)
        self._polling = PollingService(settings)
        self._downloader = VideoDownloaderService(settings)
        self._producer: RecaptchaTokenProducer | None = None
        self._producer_task: asyncio.Task | None = None
        self._producer_gen = 0
        self._rotation_lock = asyncio.Lock()
        self._generate_lock = asyncio.Lock()
        self._stop_generations = False
        self._successful_generations = 0
        self._consecutive_403s = 0
        self._consecutive_429s = 0
        self._auth_failures = 0
        self._proxy_failures: dict[str, int] = {}
        self._resume_at = 0.0
        self._traffic_429s = 0
        self._last_traffic_429_at = 0.0
        self._consecutive_timeouts = 0
        self._last_timeout_index: int | None = None
        self._alive_token_timeouts = 0
        self._reference_media: dict[tuple[str, str], str] = {}

    @property
    def name(self) -> str:
        """Name of the account the session currently runs on."""
        return self._manager.current.name

    @property
    def can_rotate(self) -> bool:
        """True when rotation switches to another account (sequential strategy)."""
        return self._manager.count > 1

    @property
    def account_count(self) -> int:
        return self._manager.count

    @property
    def on_cooldown(self) -> bool:
        return cooldowns.cooldown_remaining(self._manager.current.dolphin_id) > 0

    def can_make_images(self) -> bool:
        did = self._manager.current.dolphin_id
        return cooldowns.cooldown_remaining(did) <= 0 and cooldowns.image_cooldown_remaining(did) <= 0

    def can_make_videos(self) -> bool:
        did = self._manager.current.dolphin_id
        return cooldowns.cooldown_remaining(did) <= 0 and cooldowns.video_denied_remaining(did) <= 0

    @property
    def is_running(self) -> bool:
        return self._producer_task is not None and not self._producer_task.done()

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def ensure_started(self, attempts: int = 4) -> None:
        """Start (or reuse) the account's browser and wait until the producer has credentials.

        A failed start restarts the browser and retries with backoff, so a stale CDP
        endpoint or a Dolphin hiccup does not take the account out of the batch.
        """
        if self.is_running:
            return
        for attempt in range(1, attempts + 1):
            try:
                if self._manager.current_ws is None:
                    await asyncio.to_thread(self._manager.start_current)
                self._credentials.reload(self._manager.current)
                await self._restart_producer(self._manager.current_ws, wait_ready=True)
                logger.info(f"[{self.name}] session ready @ {self._manager.current_ws}")
                return
            except FATAL_ERRORS:
                raise
            except Exception as e:
                if attempt == attempts:
                    raise
                delay = START_RETRY_DELAYS[min(attempt, len(START_RETRY_DELAYS)) - 1]
                logger.warning(
                    f"[{self.name}] Failed to start session (attempt {attempt}/{attempts}): "
                    f"{type(e).__name__}: {e} — restarting browser in {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                self._manager.forget_browser()

    async def close(self) -> None:
        """Stop the producer; the browser keeps running so the next job can reuse it."""
        await self._stop_producer()
        self._stop_generations = False

    async def rotate(self) -> None:
        """Rotate now (e.g. the current account cannot do any of the queued work)."""
        self._stop_generations = True
        await self._rotate_account()

    # ── public operations ─────────────────────────────────────────────────

    async def submit_video(
        self,
        index: int,
        prompt: str,
        image: Path | None = None,
        frame: Path | None = None,
        model_key: str | None = None,
    ) -> SubmittedVideo:
        """Send a video request: text-to-video, image-to-video (`image`) or frame-to-video (`frame`)."""
        if image is not None and frame is not None:
            raise ValueError("pass either image or frame, not both")
        source = image or frame
        label = f"i2v {source.name}" if image else f"f2v {source.name}" if frame else "t2v"
        logger.info(f"[{index}] {label}: {prompt[:50]}...")

        async def call(token: str, proxy_url: str | None):
            async with self._generate_lock:
                common = dict(
                    authorization=self._credentials.authorization,
                    project_id=self._credentials.project_id,
                    recaptcha_token=token,
                    proxy_url=proxy_url,
                    browser_headers=self._credentials.browser_headers or None,
                    model_key=model_key,
                )
                if source is None:
                    return await self._video.generate(prompt=prompt, cookies=dict(self._downloader.cookies) or None, **common)
                media_id = await self._video.upload_image(
                    image_path=source,
                    authorization=self._credentials.authorization,
                    project_id=self._credentials.project_id,
                    proxy_url=proxy_url,
                    browser_headers=self._credentials.browser_headers or None,
                )
                generate = self._video.generate_from_image if image else self._video.generate_from_frame
                return await generate(prompt=prompt, media_id=media_id, **common)

        response = await self._request(index, label, call)
        if not response.media:
            raise RuntimeError(f"[{index}] No media returned from {label} generation")
        media = response.media[0]
        logger.info(f"[{index}] {label} generation started, media_id: {media.media_id}")
        return SubmittedVideo(
            media_id=media.media_id,
            project_id=media.project_id,
            proxy_url=self._manager.current_proxy_url,
            cookies=dict(self._downloader.cookies),
        )

    async def finish_video(self, index: int, submitted: SubmittedVideo) -> Path:
        """Poll an accepted video until it is ready and download it into the scratch directory."""
        producer = self._producer
        delay = random.uniform(*self._poll_pause)
        if producer is not None:
            producer.pause(resume_in=delay)
            _spawn(self._resume_later(producer, delay))

        proxy_errors = 0
        while True:
            try:
                media = await self._polling.poll(
                    media_id=submitted.media_id,
                    project_id=submitted.project_id,
                    authorization=self._credentials.authorization,
                    proxy_url=submitted.proxy_url,
                    browser_headers=self._credentials.browser_headers,
                    cookies=submitted.cookies,
                )
                break
            except httpx.ProxyError as exc:
                proxy_errors += 1
                pause = random.uniform(*DELAY_403)
                logger.warning(f"[{index}] ProxyError during polling (attempt {proxy_errors}): {exc}, retrying in {pause:.0f}s")
                await asyncio.sleep(pause)
            except GenerationFilteredError:
                logger.warning(f"[{index}] Content filtered — will be skipped")
                raise
            except IpProhibitedError:
                logger.bind(event_type="ip_prohibited", account=self.name).warning(f"[{index}] IP_PROHIBITED — triggering account rotation")
                self._stop_generations = True
                await self._rotate_account()
                raise
            except GenerationTimedOutError:
                await self._on_timed_out(index)
                raise

        try:
            path = await self._downloader.download(
                media.media_id, cookies=submitted.cookies, proxy_url=submitted.proxy_url, video_url=media.video_url
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401 or self._producer is None:
                raise
            logger.warning(f"[{index}] Download 401 — labs.google session stale, signing in again and retrying with fresh cookies")
            await self._producer.reauthenticate()
            path = await self._downloader.download(
                media.media_id, cookies=self._downloader.cookies, proxy_url=submitted.proxy_url, video_url=media.video_url
            )
        self._consecutive_timeouts = 0
        self._last_timeout_index = None
        self._count_success()
        return path

    async def make_image(self, index: int, prompt: str, dest: Path, reference: Path | None = None) -> Path:
        """Generate one image and save it as `dest`.

        With `reference` the picture is uploaded to the project once and passed as an
        image input, so the generated image keeps its subject (avatar mode).
        """
        logger.info(f"[{index}] image: {prompt[:50]}..." + (f" (reference {reference.name})" if reference else ""))
        used_proxy: list[str | None] = [None]

        async def call(token: str, proxy_url: str | None) -> list[str]:
            async with self._generate_lock:
                used_proxy[0] = proxy_url
                reference_ids = [await self._reference_media_id(reference, proxy_url)] if reference else None
                return await self._image.generate(
                    prompt=prompt,
                    project_id=self._credentials.project_id,
                    recaptcha_token=token,
                    authorization=self._credentials.authorization,
                    proxy_url=proxy_url,
                    browser_headers=self._credentials.browser_headers or None,
                    reference_media_ids=reference_ids,
                )

        media_ids = await self._request(index, "image", call, kind="image", action="IMAGE_GENERATION")
        if not media_ids:
            raise RuntimeError(f"[{index}] No media returned from image generation")
        dest.parent.mkdir(parents=True, exist_ok=True)
        downloaded = await self._image.download(
            media_ids[0], dict(self._downloader.cookies), proxy_url=used_proxy[0]
        )
        shutil.move(str(downloaded), str(dest))
        return dest

    # ── request + recovery policy ─────────────────────────────────────────

    async def _request(
        self,
        index: int,
        label: str,
        call: Callable[[str, str | None], Awaitable[T]],
        kind: str = "video",
        action: str | None = None,
    ) -> T:
        """Run `call(token, proxy_url)` until it succeeds, applying the session's recovery policy."""
        delay = 0.0
        while True:
            if self._stop_generations or self._rotation_lock.locked():
                await self._rotate_account()
                if self._stop_generations and self.on_cooldown:
                    raise RateLimitError(f"{self.name} is on cooldown")
            if delay > 0:
                await asyncio.sleep(delay)
                delay = 0.0
            wait = self._resume_at - asyncio.get_running_loop().time()
            if wait > 0:
                logger.debug(f"[{index}] waiting {wait:.1f}s (session 429 pause)...")
                await asyncio.sleep(wait)

            gen = self._producer_gen
            try:
                token = await asyncio.wait_for(self._producer.get_token(action=action), timeout=TOKEN_TIMEOUT)
            except asyncio.TimeoutError:
                await self._recover_after_token_timeout(index, gen)
                continue
            self._alive_token_timeouts = 0

            try:
                result = await call(token, self._manager.current_proxy_url)
            except httpx.HTTPStatusError as e:
                delay = await self._handle_http_error(index, label, kind, e)
                continue
            except httpx.ProxyError as e:
                self._consecutive_429s = 0
                logger.warning(f"[{index}] ProxyError (treated as 403): {e}")
                if self._count_403(index) is not _Count403.WAIT:
                    await self._rotate_account()
                delay = self._delay_after_403()
                continue
            except Exception as e:
                logger.error(f"[{index}] {label} exception: {type(e).__name__}: {e}")
                raise

            self._producer.report_success()
            self._on_accepted(kind)
            return result

    async def _handle_http_error(self, index: int, label: str, kind: str, e: httpx.HTTPStatusError) -> float:
        """React to an API error; returns the pause before the retry or raises to give the item up."""
        status = e.response.status_code
        reason = error_reason(e.response)
        account = self._manager.current

        if status == 401:
            self._auth_failures += 1
            if self._auth_failures <= 1:
                logger.bind(event_type="401", account=account.name).warning(f"[{index}] HTTP 401 — token rejected, signing in to labs.google again")
                try:
                    await self._producer.reauthenticate()
                    return 0.0
                except Exception as exc:
                    logger.warning(f"[{index}] Re-sign-in failed: {type(exc).__name__}: {exc}")
            logger.bind(event_type="401", account=account.name).critical(f"[{index}] HTTP 401 persists after re-sign-in — rotating account")
            self._auth_failures = 0
            self._stop_generations = True
            await self._rotate_account()
            return 0.0

        if status == 429:
            if reason == QUOTA_REACHED and kind == "image":
                cooldowns.mark_image_cooldown(account.dolphin_id, account.name)
                raise ImageQuotaError(f"[{index}] image quota reached on {account.name}") from e
            if reason == QUOTA_REACHED:
                secs = self._manager.mark_current_on_cooldown(max_seconds=DAILY_QUOTA_COOLDOWN_MAX)
                logger.bind(event_type="quota", account=account.name).warning(f"[{index}] Daily quota exhausted — {account.name} on cooldown {secs / 60:.0f}m")
                self._stop_generations = True
                await self._rotate_account()
                raise RateLimitError("Daily quota exhausted") from e
            if reason == TOO_MUCH_TRAFFIC:
                self._pause_for_traffic(index)
                return 0.0
            self._consecutive_429s += 1
            logger.bind(event_type="429", account=account.name).warning(
                f"[{index}] HTTP 429 (total consecutive: {self._consecutive_429s}/{ACCOUNT_ROTATION_429_LIMIT})"
            )
            if self._consecutive_429s >= ACCOUNT_ROTATION_429_LIMIT:
                logger.warning(f"[{index}] 429 limit reached — escalating to account rotation")
                self._stop_generations = True
                await self._rotate_account()
                raise RateLimitError("API rate limit exceeded") from e
            pause = DELAY_429_STEPS[min(self._consecutive_429s, len(DELAY_429_STEPS)) - 1]
            self._resume_at = asyncio.get_running_loop().time() + pause
            logger.warning(f"[{index}] HTTP 429, all requests of {account.name} paused for {pause:.0f}s")
            return 0.0

        if status == 400:
            logger.bind(event_type="filtered", account=account.name).warning(f"[{index}] HTTP 400 ({reason}) — skipping {label}")
            raise GenerationFilteredError(f"400 {reason}") from e

        if status == 403:
            if reason == MODEL_ACCESS_DENIED:
                if kind == "image":
                    cooldowns.mark_image_cooldown(account.dolphin_id, account.name)
                    raise ImageQuotaError(f"[{index}] image model access denied on {account.name}") from e
                cooldowns.mark_video_denied(account.dolphin_id, account.name)
                raise VideoAccessDeniedError(f"[{index}] no access to the video model on {account.name}") from e
            self._consecutive_429s = 0
            verdict = self._count_403(index)
            if verdict is _Count403.WAIT:
                pause = self._delay_after_403()
                logger.warning(f"[{index}] HTTP 403, waiting {pause:.1f}s then fresh token")
                return pause
            if verdict is _Count403.ROTATE:
                proxy_key = self._manager.current_proxy_key
                failures = self._proxy_failures.get(proxy_key, 0) + 1
                self._proxy_failures[proxy_key] = failures
                logger.critical(f"[{index}] 403 limit on {account.name} / {proxy_key} ({failures}/{PROXY_COOLDOWN_AFTER_ROTATIONS}) — rotating")
                if failures >= PROXY_COOLDOWN_AFTER_ROTATIONS:
                    self._proxy_failures.pop(proxy_key, None)
                    self._manager.mark_current_on_cooldown()
            await self._rotate_account()
            raise ConsecutiveFailuresError("403 limit reached") from e

        raise e

    def _count_403(self, index: int) -> _Count403:
        if self._stop_generations:
            return _Count403.PENDING
        if self._producer is not None:
            self._producer.report_failure()
        self._consecutive_403s += 1
        logger.bind(event_type="403", account=self.name, proxy=self._manager.current_proxy_url).warning(
            f"[{index}] 403 on {self.name} / {self._manager.current_proxy_key} "
            f"(total consecutive: {self._consecutive_403s}/{ACCOUNT_ROTATION_403_LIMIT})"
        )
        if self._consecutive_403s >= ACCOUNT_ROTATION_403_LIMIT:
            self._stop_generations = True
            return _Count403.ROTATE
        return _Count403.WAIT

    def _delay_after_403(self) -> float:
        """Short pause after the first 403, longer after repeats."""
        return random.uniform(*(DELAY_403 if self._consecutive_403s <= 1 else DELAY_403_REPEAT))

    def _pause_for_traffic(self, index: int) -> None:
        """429 TOO_MUCH_TRAFFIC: Google throttles this account or IP.

        Restarting the profile on this reason only made it worse (a fresh page scores
        lower on reCAPTCHA and gets throttled again), so the session pauses for
        2 -> 5 -> 10 minutes instead; the step resets once the throttle has been quiet
        for 15 minutes. Never counts towards 429 rotation.
        """
        now = asyncio.get_running_loop().time()
        if now - self._last_traffic_429_at > TRAFFIC_QUIET_RESET:
            self._traffic_429s = 0
        self._traffic_429s += 1
        self._last_traffic_429_at = now
        pause = DELAY_TRAFFIC_STEPS[min(self._traffic_429s, len(DELAY_TRAFFIC_STEPS)) - 1]
        self._resume_at = max(self._resume_at, now + pause)
        logger.bind(event_type="429", account=self.name).warning(
            f"[{index}] HTTP 429 TOO_MUCH_TRAFFIC #{self._traffic_429s} — pausing {self.name} for {pause / 60:.0f}m, no rotation"
        )

    def _on_accepted(self, kind: str) -> None:
        account = self._manager.current
        self._consecutive_403s = 0
        self._consecutive_429s = 0
        self._auth_failures = 0
        self._proxy_failures.pop(self._manager.current_proxy_key, None)
        self._manager.reset_cooldown(account)
        if kind == "image":
            cooldowns.reset_image_cooldown(account.dolphin_id)
        else:
            cooldowns.reset_video_denied(account.dolphin_id)

    async def _on_timed_out(self, index: int) -> None:
        """Server-side TIMED_OUT: rotate after ACCOUNT_ROTATION_TIMEOUT_LIMIT different items in a row."""
        if self._last_timeout_index == index:
            logger.warning(f"[{index}] TIMED_OUT again for the same item — not counting towards rotation")
            return
        self._last_timeout_index = index
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts < ACCOUNT_ROTATION_TIMEOUT_LIMIT:
            logger.warning(f"[{index}] TIMED_OUT ({self._consecutive_timeouts}/{ACCOUNT_ROTATION_TIMEOUT_LIMIT}) — re-queuing without rotation")
            return
        logger.warning(f"[{index}] TIMED_OUT {self._consecutive_timeouts}x in a row — triggering account rotation")
        self._consecutive_timeouts = 0
        self._stop_generations = True
        await self._rotate_account()

    def _count_success(self) -> None:
        """Count a finished video; every `rotate_every_generations` the account rotates in the background."""
        self._successful_generations += 1
        limit = self._settings.rotate_every_generations
        if self._successful_generations < limit:
            return
        self._successful_generations = 0
        logger.info(f"[{self.name}] Scheduled rotation after {limit} generations")
        self._stop_generations = True
        _spawn(self._rotate_quietly(), name="scheduled_rotation")

    async def _rotate_quietly(self) -> None:
        try:
            await self._rotate_account()
        except Exception as e:
            logger.warning(f"Scheduled rotation failed: {type(e).__name__}: {e}")

    async def _reference_media_id(self, image_path: Path, proxy_url: str | None) -> str:
        """Upload a reference picture (avatar) once per project and return its media id.

        Uploaded media belongs to a Flow project, so the cache key includes the current
        project: after an account rotation the picture is uploaded again.
        """
        project_id = self._credentials.project_id
        key = (project_id, str(image_path))
        media_id = self._reference_media.get(key)
        if media_id is None:
            media_id = await self._video.upload_image(
                image_path=image_path,
                authorization=self._credentials.authorization,
                project_id=project_id,
                proxy_url=proxy_url,
                browser_headers=self._credentials.browser_headers or None,
            )
            self._reference_media[key] = media_id
            logger.info(f"Reference image {image_path.name} uploaded to project {project_id} as {media_id}")
        return media_id

    # ── rotation and producer management ─────────────────────────────────

    async def _rotate_account(self) -> None:
        """
        Rotate to the next account/profile and restart the reCAPTCHA producer.

        Protected by a lock — only one rotation happens even if several requests hit the
        limit at once; later waiters return as soon as the rotation is done. A single
        account on cooldown is not restarted: the scheduler stops using it instead.
        """
        async with self._rotation_lock:
            if not self._stop_generations:
                return
            if not self.can_rotate and self.on_cooldown:
                logger.warning(f"[{self.name}] on cooldown — not restarting, the session stays paused")
                return

            while True:
                await self._stop_producer()
                try:
                    account, cdp_url = await asyncio.wait_for(asyncio.to_thread(self._manager.rotate), timeout=DOLPHIN_TIMEOUT)
                except RuntimeError as e:
                    logger.bind(event_type="rotation_failed").critical(f"Account rotation failed: {e}")
                    raise
                if self._manager.pop_was_reset():
                    logger.warning("Full proxy reset occurred — all accounts back in rotation")
                self._credentials.reload(account)
                try:
                    await self._restart_producer(cdp_url, wait_ready=True)
                except FATAL_ERRORS:
                    raise
                except Exception as e:
                    logger.warning(f"New producer crashed immediately, rotating again: {e}")
                    continue
                break

            logger.bind(event_type="rotation_complete", account=account.name).success(
                f"Rotation complete — now using: {account.name} (dolphin_id={account.dolphin_id})"
            )
            self._successful_generations = 0
            self._consecutive_403s = 0
            self._consecutive_429s = 0
            self._resume_at = 0.0
            self._stop_generations = False

    async def _recover_after_token_timeout(self, index: int, gen: int) -> None:
        """No token within the wait.

        A producer that is still alive is usually reconnecting after a page crash, so the
        first timeout only waits a bit. A second timeout in a row means its browser is not
        coming back (the CDP endpoint died with the profile), and a dead producer task
        means the same: both get a new producer on a freshly started browser.
        """
        async with self._rotation_lock:
            if gen != self._producer_gen:
                return
            task = self._producer_task
            if task is not None and not task.done():
                self._alive_token_timeouts += 1
                if self._alive_token_timeouts < 2:
                    logger.warning(f"[{index}] No reCAPTCHA token within timeout — producer still alive (reconnecting?), retrying")
                    await asyncio.sleep(10.0)
                    return
                logger.bind(event_type="token_timeout", account=self.name, proxy=self._manager.current_proxy_url).error(
                    f"[{index}] No reCAPTCHA token for {self._alive_token_timeouts} timeouts in a row while the producer "
                    "keeps reconnecting — its browser is presumed dead, restarting with a fresh one"
                )
            else:
                exc = None if task is None or task.cancelled() else task.exception()
                logger.bind(event_type="token_timeout", account=self.name, proxy=self._manager.current_proxy_url).error(
                    f"[{index}] No reCAPTCHA token and producer task is dead ({exc}) — restarting with a fresh browser"
                )
            self._alive_token_timeouts = 0
            await self._stop_producer()
            cdp_url = await asyncio.wait_for(asyncio.to_thread(self._manager.start_current), timeout=DOLPHIN_TIMEOUT)
            self._credentials.reload(self._manager.current)
            await self._restart_producer(cdp_url, wait_ready=True)

    async def _restart_producer(self, cdp_url: str | None, wait_ready: bool) -> None:
        """Replace the producer with a new one on `cdp_url` (default: the running browser)."""
        if cdp_url is None:
            cdp_url = self._manager.current_ws
        if cdp_url is None:
            logger.warning("No running browser for producer restart — starting current profile")
            cdp_url = await asyncio.to_thread(self._manager.start_current)
            self._credentials.reload(self._manager.current)
        await self._stop_producer()

        producer = RecaptchaTokenProducer(
            self._settings,
            cdp_url=cdp_url,
            proxy_url=self._manager.current_proxy_url,
            project_id=self._credentials.known_project_id,
        )
        task = asyncio.create_task(producer.run(), name=f"producer_{self.name}")
        self._producer, self._producer_task = producer, task
        self._producer_gen += 1

        if wait_ready:
            try:
                await self._await_ready(producer, task)
            except BaseException:
                await self._stop_producer()
                raise
        else:
            _spawn(self._collect_credentials(producer))
        task.add_done_callback(self._on_producer_done)

    def _on_producer_done(self, task: asyncio.Task) -> None:
        """A producer that dies on its own (not cancelled) gives up its browser: rotate."""
        if task is not self._producer_task or task.cancelled() or task.exception() is None:
            return
        logger.bind(event_type="producer_crashed", account=self.name).error(f"reCAPTCHA producer crashed: {task.exception()}")
        self._stop_generations = True
        _spawn(self._rotate_quietly(), name="producer_crash_rotate")

    async def _await_ready(self, producer: RecaptchaTokenProducer, task: asyncio.Task) -> None:
        """Wait for producer credentials, failing fast if the producer task dies meanwhile."""
        ready = asyncio.ensure_future(self._collect_credentials(producer))
        try:
            done, _ = await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not ready.done():
                ready.cancel()
        if ready in done:
            ready.result()
            return
        try:
            await ready
        except (asyncio.CancelledError, Exception):
            pass
        exc = None if task.cancelled() else task.exception()
        if isinstance(exc, RecaptchaInterfaceError):
            raise exc
        raise RuntimeError(f"Producer died before becoming ready: {exc}")

    async def _collect_credentials(self, producer: RecaptchaTokenProducer) -> None:
        """Await cookies, project ID, authorization and browser headers from a producer."""
        producer.on_authorization = self._credentials.set_authorization
        producer.on_cookies = self._update_cookies
        self._update_cookies(await producer.wait_for_cookies())
        project_id = await producer.wait_for_project_id(timeout=120.0)
        self._credentials.set_project_id(project_id)
        self._manager.current.save_project_id(project_id)
        authorization = await producer.wait_for_authorization(timeout=120.0)
        self._credentials.set_authorization(authorization)
        self._credentials.set_browser_headers(await producer.wait_for_browser_headers(timeout=120.0))
        logger.info(f"[{self.name}] Authorization from browser (len={len(authorization)})")

    def _update_cookies(self, cookies: dict[str, str]) -> None:
        self._downloader.set_cookies(cookies)
        self._credentials.set_cookies(cookies)

    async def _stop_producer(self) -> None:
        task = self._producer_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @staticmethod
    async def _resume_later(producer: RecaptchaTokenProducer, delay: float) -> None:
        await asyncio.sleep(delay)
        producer.resume()
