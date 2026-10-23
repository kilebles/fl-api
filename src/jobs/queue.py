"""Persistent job queue: the service the web API wraps (submit / list / cancel / retry / delete / events)."""

import asyncio
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from loguru import logger

from src.jobs.models import JobProgress, JobRequest, JobSpec, JobState, JobStatus
from src.jobs.modes import MODES, Mode, Strategy
from src.jobs.repository import JobFileRecord, JobRecord, JobRepository
from src.jobs.runner import JobRunner
from src.jobs.storage import JobOutput
from src.jobs.sync import ResultSync
from src.storage.objects import ObjectStore


class JobNotFoundError(LookupError):
    pass


class InputNotFoundError(ValueError):
    """An avatar / static image key that is not in the object store."""


class JobQueue:
    """
    Jobs live in the database and run one at a time: every job drives the same Dolphin
    profiles, so two at once would restart each other's browsers.

    A running job works in `work_dir/{id}/`; its results go to the object store as they
    appear and the folder is removed at the end. Jobs interrupted by a restart are
    queued again on `start()` and continue from what is already stored.
    """

    def __init__(self, runner: JobRunner, repo: JobRepository, objects: ObjectStore, work_dir: Path) -> None:
        self._runner = runner
        self._repo = repo
        self._objects = objects
        self._work_dir = work_dir
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._running_id: str | None = None
        self._running_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue[JobStatus]] = set()
        self._stopping = False

    async def start(self) -> None:
        await self._repo.sync_from_store(self._objects)
        requeued = await self._repo.requeue_interrupted()
        if requeued:
            logger.info(f"[queue] {requeued} interrupted job(s) queued again")
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_forever(), name="job_queue")

    async def sync(self) -> int:
        return await self._repo.sync_from_store(self._objects)

    async def stop(self) -> None:
        """Stop taking jobs; a running job is interrupted and resumes on the next start."""
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def submit(self, request: JobRequest) -> JobStatus:
        for key in (request.avatar, request.static_image):
            if key is not None and not await self._objects.exists(key):
                raise InputNotFoundError(f"uploaded file not found: {key}")
        record = await self._repo.create(uuid.uuid4().hex[:12], request)
        logger.info(f"[queue] job {record.id} queued: {record.mode}, {record.total} rows")
        status = await self._repo.status(record)
        await self._publish(status)
        self._wake.set()
        return status

    async def get(self, job_id: str) -> JobStatus:
        return await self._repo.status(await self._record(job_id))

    async def recent(self, limit: int = 100, project_id: str | None = None) -> list[JobStatus]:
        return await self._repo.statuses(await self._repo.recent(limit, project_id))

    async def record(self, job_id: str) -> JobRecord:
        return await self._record(job_id)

    async def files(self, job_id: str) -> list[JobFileRecord]:
        await self._record(job_id)
        return await self._repo.files(job_id)

    async def position(self, job_id: str) -> int | None:
        """0 for the running job, 1.. for queued ones, None otherwise."""
        if job_id == self._running_id:
            return 0
        queued = await self._repo.queued_ids()
        return queued.index(job_id) + 1 if job_id in queued else None

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job; False when it has already finished."""
        record = await self._record(job_id)
        if JobState(record.state).finished:
            return False
        if job_id == self._running_id and self._running_task is not None:
            self._running_task.cancel()
            return True
        await self._set(job_id, JobState.CANCELLED, finished=True)
        return True

    async def retry(self, job_id: str) -> bool:
        """Queue a stopped / failed / cancelled job again: only its unfinished rows run."""
        record = await self._record(job_id)
        if JobState(record.state) not in (JobState.STOPPED, JobState.FAILED, JobState.CANCELLED):
            return False
        await self._repo.update(job_id, state=JobState.QUEUED.value, message=None, finished_at=None)
        await self._publish(await self.get(job_id))
        self._wake.set()
        return True

    async def delete(self, job_id: str) -> None:
        """Delete a job with its results; a running one is cancelled first."""
        await self._record(job_id)
        if job_id == self._running_id and self._running_task is not None:
            task = self._running_task
            task.cancel()
            await asyncio.wait({task})
        await self._objects.delete_prefix(f"jobs/{job_id}/")
        await self._repo.delete(job_id)
        shutil.rmtree(self._work_dir / job_id, ignore_errors=True)
        logger.info(f"[queue] job {job_id} deleted")

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[JobStatus]]:
        """A queue of status updates of every job, for as long as the context is open."""
        queue: asyncio.Queue[JobStatus] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def _record(self, job_id: str) -> JobRecord:
        record = await self._repo.get(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        return record

    async def _publish(self, status: JobStatus) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(status)

    async def _publish_id(self, job_id: str) -> None:
        record = await self._repo.get(job_id)
        if record is not None:
            await self._publish(await self._repo.status(record))

    async def _set(self, job_id: str, state: JobState, message: str | None = None, finished: bool = False, **fields) -> None:
        fields["state"] = state.value
        if message is not None or state is not JobState.RUNNING:
            fields["message"] = message
        if finished:
            fields["finished_at"] = datetime.now()
        await self._repo.update(job_id, **fields)
        await self._publish_id(job_id)

    async def _run_forever(self) -> None:
        while True:
            record = await self._repo.next_queued()
            if record is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            self._running_id = record.id
            self._running_task = asyncio.create_task(self._run(record), name=f"job_{record.id}")
            try:
                await self._running_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                logger.exception(f"[queue] job {record.id} crashed")
            finally:
                self._running_id = None
                self._running_task = None

    async def _run(self, record: JobRecord) -> None:
        job_id = record.id
        work = self._work_dir / job_id
        await self._set(job_id, JobState.RUNNING, started_at=datetime.now())
        sync: ResultSync | None = None
        state, message = JobState.FAILED, None
        try:
            files = await self._repo.files(job_id)
            spec = await self._prepare(record, work, files)
            output = JobOutput(spec, stored={f.name for f in files})
            sync = ResultSync(job_id, output, self._objects, self._repo, on_synced=lambda: self._publish_id(job_id))

            async def on_progress(state: JobState, progress: JobProgress, message: str | None) -> None:
                sync.kick()
                if not state.finished:
                    await self._set(job_id, state, message, pass_num=progress.pass_num)
                else:
                    await self._repo.update(job_id, pass_num=progress.pass_num)

            state, _, message = await self._runner.run(spec, on_progress, output=output)
        except asyncio.CancelledError:
            state, message = JobState.CANCELLED, None
            raise
        except Exception as e:
            logger.exception(f"[queue] job {job_id} failed")
            state, message = JobState.FAILED, f"{type(e).__name__}: {e}"
        finally:
            stored = await sync.close() if sync is not None else True
            if stored:
                shutil.rmtree(work, ignore_errors=True)
            else:
                message = (message + "; " if message else "") + f"some results are still only in {work}"
                logger.error(f"[queue] job {job_id}: {message}")
            if await self._repo.get(job_id) is None:
                pass
            elif self._stopping:
                await self._set(job_id, JobState.QUEUED, "interrupted by a restart")
            else:
                await self._set(job_id, state, message, finished=True)
            logger.info(f"[queue] job {job_id} finished: {state}")

    async def _prepare(self, record: JobRecord, work: Path, files: list[JobFileRecord]) -> JobSpec:
        """Download the inputs and put back what the run needs from earlier runs of this job."""
        out = work / "out"
        out.mkdir(parents=True, exist_ok=True)
        mode = MODES[Mode(record.mode)]

        inputs: dict[str, Path] = {}
        for field, key in (("avatar", record.avatar_key), ("static_image", record.static_image_key)):
            if key:
                path = work / "inputs" / f"{field}{PurePosixPath(key).suffix}"
                if not path.exists():
                    await self._objects.download(key, path)
                inputs[field] = path

        valid_rows = {r.name for r in record.job_rows}
        done_rows = {f.row for f in files if f.kind == "skip" or f.kind == ("video" if mode.makes_videos else "image")}
        for f in files:
            if f.row not in valid_rows:
                continue
            path = out / f.name
            if path.exists():
                continue
            if f.kind == "skip":
                path.write_text(f.reason or "")
            elif (mode.chain and f.kind == "video") or (mode.makes_videos and f.kind == "image" and f.row not in done_rows):
                try:
                    await self._objects.download(f.key, path)
                except Exception as e:
                    logger.warning(f"[queue] job {record.id}: could not download {f.key}, will regenerate: {e}")

        return JobSpec(
            mode=Mode(record.mode),
            rows=record.job_rows,
            output_dir=out,
            strategy=Strategy(record.strategy),
            video_model_key=record.video_model_key,
            **inputs,
        )
