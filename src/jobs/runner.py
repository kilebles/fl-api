"""Runs one job: builds sessions for its strategy and repeats passes until every row is finished."""

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from src.accounts.account import AccountInfo
from src.accounts.repository import AccountRepository
from src.core.settings import Settings
from src.dolphin import cooldowns
from src.dolphin.local_api import DolphinLocalApi
from src.dolphin.manager import DolphinAntyManager
from src.dolphin.public_api import DolphinPublicApi
from src.engine.chain import run_chain
from src.engine.scheduler import BatchOptions, BatchScheduler, scaled_poll_pause
from src.engine.session import FATAL_ERRORS, AccountSession
from src.jobs.models import JobProgress, JobSpec, JobState
from src.jobs.modes import MODES, Strategy
from src.jobs.storage import JobOutput

MAX_COOLDOWN_WAIT = 3600.0
# While an account sits on the rolling (~24h) image quota a stalled pass is not the end:
# wait this long and try again, up to IMAGE_QUOTA_MAX_WAITS times in a row (~26h).
IMAGE_QUOTA_WAIT = 900.0
IMAGE_QUOTA_MAX_WAITS = 104

ProgressCallback = Callable[[JobState, JobProgress, str | None], Awaitable[None]]


class NoAccountsError(RuntimeError):
    """No account with a dolphin_id is configured."""


class JobRunner:
    """
    Runs jobs against the configured accounts.

    A job runs in passes: each pass gives the missing rows to the scheduler (or the f2v
    chain). Rows that keep failing are skipped, so every pass either makes
    progress or ends the job: when a pass changes nothing, the runner waits for the
    earliest account cooldown if every account is paused, otherwise it stops.
    """

    def __init__(self, settings: Settings, accounts: AccountRepository) -> None:
        self._settings = settings
        self._accounts = accounts
        self._public_api = DolphinPublicApi(settings.anty_token)
        self._local_api = DolphinLocalApi(settings.anty_local_api)

    async def run(
        self,
        spec: JobSpec,
        on_progress: ProgressCallback | None = None,
        output: JobOutput | None = None,
    ) -> tuple[JobState, JobProgress, str | None]:
        """Run the job to the end; returns its final state, progress and a message.

        `output` lets the caller share the result state (e.g. with files already moved to
        the object store); by default it is read from `spec.output_dir`.
        """
        accounts = [a for a in self._accounts.active() if not self._settings.accounts_enabled or a.name in self._settings.accounts_enabled]
        if not accounts:
            raise NoAccountsError(f"No accounts with dolphin_id in {self._accounts.root}")

        output = output or JobOutput(spec)
        output.directory.mkdir(parents=True, exist_ok=True)
        mode = MODES[spec.mode]
        model_key = spec.video_model_key or mode.video_model(self._settings)
        sessions = self._make_sessions(accounts, Strategy.SEQUENTIAL if mode.chain else spec.strategy)
        pass_num = 0

        async def report(state: JobState, message: str | None = None) -> None:
            if on_progress is not None:
                try:
                    await on_progress(state, output.progress(pass_num), message)
                except Exception as e:
                    logger.debug(f"on_progress callback failed: {e}")

        async def on_change() -> None:
            await report(JobState.RUNNING)

        try:
            prev_progress: int | None = None
            quota_waits = 0
            while True:
                missing = output.missing()
                if not missing:
                    return await self._finish(JobState.DONE, output, pass_num, None, report)

                progress = output.progress(pass_num)
                current = progress.images_done + progress.videos_done + progress.skipped
                if prev_progress is not None and current <= prev_progress:
                    images_needed = output.needs_images(missing)
                    wait = self._cooldown_wait(accounts, images_needed)
                    if wait > 0:
                        wait = min(wait, MAX_COOLDOWN_WAIT) + 5.0
                        message = f"all accounts paused (quota or model access) — waiting {wait / 60:.0f}m"
                    elif images_needed and quota_waits < IMAGE_QUOTA_MAX_WAITS and any(
                        cooldowns.image_quota_limited(a.dolphin_id) for a in accounts
                    ):
                        quota_waits += 1
                        wait = IMAGE_QUOTA_WAIT
                        message = f"image quota on some accounts — next pass in {wait / 60:.0f}m"
                    else:
                        message = f"pass {pass_num} made no progress, {len(missing)} rows left — see logs"
                        logger.warning(f"[job] {message}")
                        return await self._finish(JobState.STOPPED, output, pass_num, message, report)
                    logger.warning(f"[job] {message}")
                    await report(JobState.WAITING, message)
                    for session in sessions:
                        await session.close()
                    await asyncio.sleep(wait)
                    prev_progress = None
                    continue
                if prev_progress is not None and current > prev_progress:
                    quota_waits = 0
                prev_progress = current

                pass_num += 1
                logger.info(f"[job] {spec.mode} pass {pass_num}: {len(missing)} of {len(spec.rows)} rows left")
                await report(JobState.RUNNING)
                if mode.chain:
                    clips = [(row.name, row.prompt) for row in spec.rows]
                    await run_chain(sessions[0], clips, output.directory, first_model_key=model_key, on_change=on_change)
                else:
                    options = BatchOptions(output_dir=output.directory, video_model_key=model_key, reference_image=spec.avatar)
                    await BatchScheduler(sessions, options, on_change=on_change).run(output.work_items(missing))
        except FATAL_ERRORS as e:
            message = f"{type(e).__name__}: {e}"
            logger.error(f"[job] stopped: {message}")
            return await self._finish(JobState.FAILED, output, pass_num, message, report)
        finally:
            for session in sessions:
                await session.close()

    def _make_sessions(self, accounts: list[AccountInfo], strategy: Strategy) -> list[AccountSession]:
        if strategy is Strategy.SEQUENTIAL:
            manager = DolphinAntyManager(accounts, self._public_api, self._local_api)
            return [AccountSession(self._settings, manager)]
        pause = scaled_poll_pause(len(accounts))
        return [
            AccountSession(self._settings, DolphinAntyManager([account], self._public_api, self._local_api), poll_pause=pause)
            for account in accounts
        ]

    @staticmethod
    def _cooldown_wait(accounts: list[AccountInfo], images_needed: bool) -> float:
        """Seconds until at least one account can work again; 0 when any account is free now.

        With `images_needed` an account also counts as busy while its image quota lasts,
        otherwise while its videos are paused (no access to the video model).
        """
        waits = []
        for account in accounts:
            wait = cooldowns.cooldown_remaining(account.dolphin_id)
            extra = cooldowns.image_cooldown_remaining if images_needed else cooldowns.video_denied_remaining
            waits.append(max(wait, extra(account.dolphin_id)))
        return min(waits) if waits and min(waits) > 0 else 0.0

    @staticmethod
    async def _finish(state: JobState, output: JobOutput, pass_num: int, message: str | None, report) -> tuple[JobState, JobProgress, str | None]:
        await report(state, message)
        return state, output.progress(pass_num), message
