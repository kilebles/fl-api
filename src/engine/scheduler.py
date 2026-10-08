"""Batch scheduler: one worker per session pulling image and video jobs from two shared queues."""

import asyncio
import collections
import math
import random
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from src.core.errors import (
    ConsecutiveFailuresError,
    DiskFullError,
    GenerationFilteredError,
    GenerationTimedOutError,
    ImageQuotaError,
    IpProhibitedError,
    RateLimitError,
    VideoAccessDeniedError,
)
from src.engine.session import FATAL_ERRORS, AccountSession, SubmittedVideo

INTER_GENERATION_PAUSE = (0.0, 0.5)

# Error-rate guard: this many errors per minute across all workers pauses everyone.
ERROR_RATE_THRESHOLD = 30
ERROR_RATE_WINDOW = 60.0
GLOBAL_COOLDOWN = (120.0, 300.0)

BACKOFF_BASE = 7.0  # 7s → 14s → 28s → … capped
BACKOFF_MAX = 60.0
IP_BACKOFF_BASE = 600.0  # IP_PROHIBITED: 10m → 20m → … capped at 4h
IP_BACKOFF_MAX = 14400.0

# An idle worker re-checks the queues and its account's pauses this often.
PICK_POLL_INTERVAL = 5.0
# How long an idle worker keeps waiting for re-queued work before it gives up.
QUEUE_DRAIN_TIMEOUT = IP_BACKOFF_MAX + 60.0
# No item settled and no image made for this long → cancel the workers (the runner starts a new pass).
WATCHDOG_STALL_TIMEOUT = 600.0
# A row the server times out on this many times is skipped for good: each attempt costs
# ~5 minutes and the same prompt/image fails on every account.
ITEM_TIMEOUT_SKIP_AFTER = 2

MIN_FREE_BYTES = 1024 ** 3

ChangeCallback = Callable[[], Awaitable[None]]


@dataclass
class WorkItem:
    """One row of a batch.

    - text-to-video: `video_prompt` only;
    - image only: `image_prompt` + `image_path` (the result);
    - image-to-video: `video_prompt` + `image_path`, and `image_prompt` when the source
      image has to be generated first (otherwise `image_path` is a ready/static picture).
    """

    index: int
    name: str
    video_prompt: str | None = None
    image_prompt: str | None = None
    image_path: Path | None = None
    retries: int = 0

    @property
    def needs_image(self) -> bool:
        return self.image_prompt is not None and self.image_path is not None and not self.image_path.exists()


@dataclass
class BatchOptions:
    output_dir: Path
    video_model_key: str | None = None
    reference_image: Path | None = None
    max_retries: int = 5


@dataclass
class BatchStats:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    images_done: int = 0

    @property
    def progress(self) -> int:
        return self.done + self.skipped + self.failed + self.images_done


@dataclass
class _BatchState:
    pending: set[int]
    video_queue: asyncio.Queue[WorkItem] = field(default_factory=asyncio.Queue)
    image_queue: asyncio.Queue[WorkItem] = field(default_factory=asyncio.Queue)
    stats: BatchStats = field(default_factory=BatchStats)
    fatal: BaseException | None = None
    timeouts: dict[int, int] = field(default_factory=dict)
    inflight: set[asyncio.Task] = field(default_factory=set)
    deadline: float = 0.0


@dataclass
class _Worker:
    session: AccountSession
    fails: int = 0

    @property
    def name(self) -> str:
        return self.session.name


def check_disk_space(path: Path) -> None:
    """Refuse to start a batch when the output disk is (nearly) full."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < MIN_FREE_BYTES:
        raise DiskFullError(f"Only {free // 1024 ** 2} MB free on the output disk — free some space and run again")


def mark_skip(output_dir: Path, name: str, reason: str) -> None:
    """Write a .skip marker so later passes stop re-running a permanently failing item."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{name}.skip").write_text(reason)
    except OSError as e:
        logger.warning(f"Could not write skip marker for {name}: {e}")


def scaled_poll_pause(accounts: int) -> tuple[float, float]:
    """Producer pause while a poll is in flight, shrinking with sqrt(accounts): n=1 → 8-11s, n≥4 → 3-5.5s."""
    factor = math.sqrt(max(1, accounts))
    return max(3.0, 8.0 / factor), max(5.0, 11.0 / factor)


def _backoff(fails: int) -> float:
    return min(BACKOFF_BASE * 2 ** (fails - 1) + random.uniform(-2.0, 2.0), BACKOFF_MAX)


def _ip_backoff(fails: int) -> float:
    return min(IP_BACKOFF_BASE * 2 ** (fails - 1) + random.uniform(-30.0, 30.0), IP_BACKOFF_MAX)


class _ErrorRateTracker:
    """Sliding-window error counter shared across all workers."""

    def __init__(self, window: float = ERROR_RATE_WINDOW) -> None:
        self._window = window
        self._timestamps: collections.deque[float] = collections.deque()

    def record(self) -> int:
        now = time.monotonic()
        self._timestamps.append(now)
        while self._timestamps and self._timestamps[0] < now - self._window:
            self._timestamps.popleft()
        return len(self._timestamps)


class BatchScheduler:
    """
    Runs one worker per session against two shared queues (images, videos).

    A worker takes a video whose source image is ready if its account may make videos,
    otherwise an image if it may make images: an account without video access feeds the
    image queue only, an account on image quota takes videos only, and a finished image
    moves its row to the video queue for any worker. Video polling runs in the background
    so a worker submits the next request right away. Failed items go back to the queue
    (up to `max_retries`) so another account can take them.

    With one multi-account session this is the sequential strategy: the single worker
    rotates accounts instead of handing work to other workers.
    """

    def __init__(self, sessions: list[AccountSession], options: BatchOptions, on_change: ChangeCallback | None = None) -> None:
        if not sessions:
            raise ValueError("At least one session required")
        self._sessions = sessions
        self._options = options
        self._on_change = on_change
        self._errors = _ErrorRateTracker()
        self._cooldown_until = 0.0

    async def run(self, items: list[WorkItem]) -> BatchStats:
        """Process the items; returns when every item settled, the workers gave up, or the watchdog fired."""
        out = self._options.output_dir
        out.mkdir(parents=True, exist_ok=True)
        check_disk_space(out)
        state = _BatchState(pending={item.index for item in items})
        state.deadline = asyncio.get_running_loop().time() + QUEUE_DRAIN_TIMEOUT
        for item in items:
            if item.needs_image:
                state.image_queue.put_nowait(item)
            elif item.video_prompt is not None:
                state.video_queue.put_nowait(item)
            else:
                state.pending.discard(item.index)

        workers = [
            asyncio.create_task(self._worker(_Worker(session), state), name=f"worker_{session.name}")
            for session in self._sessions
        ]
        watchdog = asyncio.create_task(self._watchdog(workers, state))
        try:
            await asyncio.gather(*workers, return_exceptions=True)
            while state.inflight:
                await asyncio.gather(*list(state.inflight), return_exceptions=True)
        finally:
            watchdog.cancel()
            for task in [*workers, *state.inflight]:
                task.cancel()

        s = state.stats
        logger.info(f"Batch pass done: {s.done} done, {s.images_done} images, {s.skipped} skipped, {s.failed} failed, {len(state.pending)} unsettled")
        if state.fatal is not None:
            raise state.fatal
        return s

    # ── worker ────────────────────────────────────────────────────────────

    async def _worker(self, w: _Worker, state: _BatchState) -> None:
        session = w.session
        if not session.can_rotate and not self._has_work_for(session, state):
            logger.warning(f"[{w.name}] skipped — the account cannot do any of the queued work (cooldown or no model access)")
            return
        try:
            await session.ensure_started()
        except FATAL_ERRORS as e:
            state.fatal = e
            return
        except Exception as e:
            logger.error(f"[{w.name}] could not start: {type(e).__name__}: {e} — worker exits")
            return

        idle_rotations = 0
        while state.fatal is None:
            await self._wait_global_cooldown(w.name)
            picked = await self._pick(session, state)
            if picked is None:
                if session.can_rotate and self._stuck(session, state) and idle_rotations < session.account_count:
                    idle_rotations += 1
                    logger.warning(f"[{w.name}] cannot do any queued work — rotating")
                    try:
                        await session.rotate()
                    except FATAL_ERRORS as e:
                        state.fatal = e
                    except Exception as e:
                        logger.error(f"[{w.name}] rotation failed: {type(e).__name__}: {e}")
                        break
                    continue
                break
            idle_rotations = 0
            kind, item = picked

            if item.retries > self._options.max_retries:
                logger.warning(f"[{w.name}] {item.name} exceeded max retries ({self._options.max_retries}), skipping")
                await self._settle(state, item, "failed", skip_reason=f"Max retries exceeded ({self._options.max_retries})")
                continue

            if kind == "image":
                stop = await self._run_image(w, state, item)
            else:
                stop = await self._run_video(w, state, item)
            if stop:
                break

    def _has_work_for(self, session: AccountSession, state: _BatchState) -> bool:
        return (session.can_make_videos() and not state.video_queue.empty()) or (
            session.can_make_images() and not state.image_queue.empty()
        )

    def _stuck(self, session: AccountSession, state: _BatchState) -> bool:
        """Work is queued, but none of it can be done by the session's current account."""
        queued = not state.video_queue.empty() or not state.image_queue.empty()
        return bool(state.pending) and queued and not self._has_work_for(session, state)

    async def _pick(self, session: AccountSession, state: _BatchState) -> tuple[str, WorkItem] | None:
        """Next job for the session: a ready video first, otherwise an image.

        Returns None when the batch is settled, the drain timeout passed, or the account
        can do neither kind of job.
        """
        while True:
            videos_ok = session.can_make_videos()
            images_ok = session.can_make_images()
            if videos_ok:
                try:
                    return "video", state.video_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            if images_ok:
                try:
                    return "image", state.image_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            if not state.pending or asyncio.get_running_loop().time() > state.deadline:
                return None
            if not videos_ok and not images_ok:
                return None
            queue, kind = (state.video_queue, "video") if videos_ok else (state.image_queue, "image")
            try:
                return kind, await asyncio.wait_for(queue.get(), timeout=PICK_POLL_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def _run_image(self, w: _Worker, state: _BatchState, item: WorkItem) -> bool:
        """Make the item's image. Returns True when the worker should stop."""
        try:
            await w.session.make_image(item.index, item.image_prompt, item.image_path, reference=self._options.reference_image)
        except ImageQuotaError:
            logger.warning(f"[{w.name}/img] image quota — this account switches to videos, {item.name} re-queued")
            state.image_queue.put_nowait(item)
            return False
        except GenerationFilteredError as e:
            logger.warning(f"[{w.name}/img] Filtered (permanent skip): {item.name}")
            await self._settle(state, item, "skipped", skip_reason=str(e))
            return False
        except (ConsecutiveFailuresError, RateLimitError, IpProhibitedError) as e:
            return await self._requeue_after_failure(w, state, item, state.image_queue, e)
        except Exception as e:
            return await self._requeue_after_error(w, state, item, state.image_queue, e)

        w.fails = 0
        state.stats.images_done += 1
        logger.bind(event_type="success", account=w.name).success(f"[{w.name}/img] Saved: {item.image_path}")
        if item.video_prompt is None:
            await self._settle(state, item, "done")
        else:
            state.video_queue.put_nowait(item)
            await self._notify()
        return False

    async def _run_video(self, w: _Worker, state: _BatchState, item: WorkItem) -> bool:
        """Submit the item's video and poll it in the background. Returns True when the worker should stop."""
        if item.image_path is not None and not item.image_path.exists():
            if item.image_prompt is not None:
                state.image_queue.put_nowait(item)
            else:
                await self._settle(state, item, "failed", skip_reason=f"source image missing: {item.image_path}")
            return False
        try:
            submitted = await w.session.submit_video(
                item.index, item.video_prompt, image=item.image_path, model_key=self._options.video_model_key
            )
        except GenerationFilteredError as e:
            logger.warning(f"[{w.name}/video] Filtered (permanent skip): {item.name}")
            await self._settle(state, item, "skipped", skip_reason=str(e))
            return False
        except VideoAccessDeniedError as e:
            logger.warning(f"[{w.name}/video] {e} — {item.name} re-queued for an account with access")
            state.video_queue.put_nowait(item)
            return False
        except (ConsecutiveFailuresError, RateLimitError, IpProhibitedError) as e:
            return await self._requeue_after_failure(w, state, item, state.video_queue, e)
        except Exception as e:
            return await self._requeue_after_error(w, state, item, state.video_queue, e)

        w.fails = 0
        task = asyncio.create_task(self._finish_video(w, state, item, submitted), name=f"poll_{item.name}")
        state.inflight.add(task)
        task.add_done_callback(state.inflight.discard)
        pause = random.uniform(*INTER_GENERATION_PAUSE)
        if pause > 0:
            await asyncio.sleep(pause)
        return False

    async def _finish_video(self, w: _Worker, state: _BatchState, item: WorkItem, submitted: SubmittedVideo) -> None:
        try:
            path = await w.session.finish_video(item.index, submitted)
            final_path = self._options.output_dir / f"{item.name}.mp4"
            path.replace(final_path)
            logger.bind(event_type="success", account=w.name).success(f"[{w.name}/video] Saved: {final_path}")
            await self._settle(state, item, "done")
        except GenerationFilteredError as e:
            logger.warning(f"[{w.name}/video] Filtered (permanent skip): {item.name}")
            await self._settle(state, item, "skipped", skip_reason=str(e))
        except GenerationTimedOutError as e:
            timeouts = state.timeouts.get(item.index, 0) + 1
            state.timeouts[item.index] = timeouts
            if timeouts >= ITEM_TIMEOUT_SKIP_AFTER:
                logger.warning(f"[{w.name}/video] {item.name} timed out on the server {timeouts}x — permanent skip")
                await self._settle(state, item, "skipped", skip_reason=f"server TIMED_OUT {timeouts}x: {e}")
            else:
                logger.warning(f"[{w.name}/video] {item.name} timed out on the server ({timeouts}/{ITEM_TIMEOUT_SKIP_AFTER}) — re-queuing")
                item.retries += 1
                state.video_queue.put_nowait(item)
        except IpProhibitedError:
            logger.warning(f"[{w.name}/video] IP_PROHIBITED on {item.name} while polling — re-queuing")
            item.retries += 1
            state.video_queue.put_nowait(item)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if isinstance(e, FATAL_ERRORS):
                state.fatal = e
            logger.error(f"[{w.name}/video] Poll error on {item.name}: {type(e).__name__}: {e}")
            if item.retries < self._options.max_retries:
                await asyncio.sleep(_backoff(item.retries + 1))
                item.retries += 1
                state.video_queue.put_nowait(item)
            else:
                await self._settle(state, item, "failed")

    async def _requeue_after_failure(
        self, w: _Worker, state: _BatchState, item: WorkItem, queue: asyncio.Queue[WorkItem], e: Exception
    ) -> bool:
        """403/429 limit or blocked IP: hand the item to another worker and back off (or stop on cooldown)."""
        w.fails += 1
        self._record_error(w.name)
        item.retries += 1
        queue.put_nowait(item)
        if w.session.on_cooldown and not w.session.can_rotate:
            logger.warning(f"[{w.name}] account on cooldown — {item.name} re-queued, worker exits")
            return True
        delay = _ip_backoff(item.retries) if isinstance(e, IpProhibitedError) else _backoff(w.fails)
        logger.warning(f"[{w.name}] {type(e).__name__} on {item.name} (attempt {item.retries}/{self._options.max_retries}), re-queued, backing off {delay:.0f}s")
        await asyncio.sleep(delay)
        return False

    async def _requeue_after_error(
        self, w: _Worker, state: _BatchState, item: WorkItem, queue: asyncio.Queue[WorkItem], e: Exception
    ) -> bool:
        """Unexpected error: retry the item a few times; fatal errors stop the batch."""
        if isinstance(e, FATAL_ERRORS):
            state.fatal = e
            queue.put_nowait(item)
            return True
        w.fails += 1
        logger.error(f"[{w.name}] Unexpected error on {item.name}: {type(e).__name__}: {e}")
        if item.retries < self._options.max_retries:
            await asyncio.sleep(_backoff(w.fails))
            item.retries += 1
            queue.put_nowait(item)
        else:
            await self._settle(state, item, "failed")
        return False

    # ── bookkeeping ───────────────────────────────────────────────────────

    async def _settle(self, state: _BatchState, item: WorkItem, outcome: str, skip_reason: str | None = None) -> None:
        if item.index not in state.pending:
            return
        state.pending.discard(item.index)
        if outcome == "done":
            state.stats.done += 1
        elif outcome == "skipped":
            state.stats.skipped += 1
        else:
            state.stats.failed += 1
        if skip_reason is not None and state.fatal is None:
            mark_skip(self._options.output_dir, item.name, skip_reason)
        await self._notify()

    async def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception as e:
            logger.debug(f"on_change callback failed: {e}")

    def _record_error(self, worker_name: str) -> None:
        """Too many errors per minute across all workers → pause everyone for 2–5 minutes."""
        rate = self._errors.record()
        now = asyncio.get_running_loop().time()
        if rate >= ERROR_RATE_THRESHOLD and self._cooldown_until <= now:
            duration = random.uniform(*GLOBAL_COOLDOWN)
            self._cooldown_until = now + duration
            logger.warning(f"[{worker_name}] Error rate {rate}/min ≥ {ERROR_RATE_THRESHOLD} — global cooldown {duration:.0f}s")

    async def _wait_global_cooldown(self, worker_name: str) -> None:
        wait = self._cooldown_until - asyncio.get_running_loop().time()
        if wait > 0:
            logger.warning(f"[{worker_name}] Global cooldown active — waiting {wait:.0f}s")
            await asyncio.sleep(wait)

    async def _watchdog(self, workers: list[asyncio.Task], state: _BatchState) -> None:
        last = -1
        while True:
            await asyncio.sleep(WATCHDOG_STALL_TIMEOUT)
            if state.stats.progress == last:
                logger.error(f"Watchdog: no progress for {WATCHDOG_STALL_TIMEOUT:.0f}s ({state.stats}) — cancelling workers")
                for task in workers:
                    task.cancel()
                return
            last = state.stats.progress
