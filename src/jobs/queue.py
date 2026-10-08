"""In-memory job queue: the entry point an API wraps (submit / status / cancel)."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from src.jobs.models import JobProgress, JobSpec, JobState, JobStatus
from src.jobs.runner import JobRunner
from src.jobs.storage import JobOutput


@dataclass
class _Job:
    spec: JobSpec
    status: JobStatus
    task: asyncio.Task | None = None


class JobQueue:
    """
    FIFO of jobs, executed one at a time: every job drives the same Dolphin profiles,
    so two jobs at once would restart each other's browsers.

    Call `start()` once inside the running event loop (e.g. on API startup).
    """

    def __init__(self, runner: JobRunner) -> None:
        self._runner = runner
        self._jobs: dict[str, _Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_forever(), name="job_queue")

    async def stop(self) -> None:
        """Cancel the running job and stop taking new ones."""
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    def submit(self, spec: JobSpec) -> JobStatus:
        """Queue a job; its progress starts from what is already in the output folder."""
        job_id = uuid.uuid4().hex[:12]
        status = JobStatus(
            id=job_id,
            mode=spec.mode,
            strategy=spec.strategy,
            output_dir=spec.output_dir,
            progress=JobOutput(spec).progress(),
        )
        self._jobs[job_id] = _Job(spec=spec, status=status)
        self._queue.put_nowait(job_id)
        logger.info(f"[queue] job {job_id} queued: {spec.mode}, {len(spec.rows)} rows → {spec.output_dir}")
        return status

    def get(self, job_id: str) -> JobStatus | None:
        job = self._jobs.get(job_id)
        return job.status if job else None

    def all(self) -> list[JobStatus]:
        return [job.status for job in self._jobs.values()]

    def position(self, job_id: str) -> int | None:
        """0 for the running job, 1.. for queued ones, None when finished or unknown."""
        job = self._jobs.get(job_id)
        if job is None or job.status.state.finished:
            return None
        if job.status.state is not JobState.QUEUED:
            return 0
        queued = [j for j in self._jobs.values() if j.status.state is JobState.QUEUED]
        return queued.index(job) + 1

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns False if it is unknown or already finished."""
        job = self._jobs.get(job_id)
        if job is None or job.status.state.finished:
            return False
        if job.task is not None:
            job.task.cancel()
        else:
            self._set(job, JobState.CANCELLED, finished=True)
        return True

    async def _run_forever(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs[job_id]
            if job.status.state is JobState.CANCELLED:
                continue
            job.task = asyncio.create_task(self._run(job), name=f"job_{job_id}")
            try:
                await job.task
            except asyncio.CancelledError:
                if not job.task.cancelled():
                    raise
            finally:
                job.task = None

    async def _run(self, job: _Job) -> None:
        job.status.started_at = datetime.now()
        self._set(job, JobState.RUNNING)

        async def on_progress(state: JobState, progress: JobProgress, message: str | None) -> None:
            job.status.progress = progress
            self._set(job, state, message)

        try:
            state, progress, message = await self._runner.run(job.spec, on_progress)
            job.status.progress = progress
            self._set(job, state, message, finished=True)
        except asyncio.CancelledError:
            job.status.progress = JobOutput(job.spec).progress(job.status.progress.pass_num)
            self._set(job, JobState.CANCELLED, finished=True)
            raise
        except Exception as e:
            logger.exception(f"[queue] job {job.status.id} failed")
            self._set(job, JobState.FAILED, f"{type(e).__name__}: {e}", finished=True)

    @staticmethod
    def _set(job: _Job, state: JobState, message: str | None = None, finished: bool = False) -> None:
        job.status.state = state
        if message is not None or state is not JobState.RUNNING:
            job.status.message = message
        if finished:
            job.status.finished_at = datetime.now()
