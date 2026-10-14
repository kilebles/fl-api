"""Moves finished results of a running job from its work folder to the object store."""

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from src.jobs.repository import KIND_BY_SUFFIX, JobRepository
from src.jobs.storage import JobOutput
from src.storage.objects import ObjectStore


def result_key(job_id: str, name: str) -> str:
    return f"jobs/{job_id}/{name}"


class ResultSync:
    """
    Uploads every new `.jpg` / `.mp4` of the output folder and records it (a `.skip` is
    only recorded). A row's final result is removed locally right after the upload;
    source images stay until the job ends because its videos are made from them. f2v
    keeps its clips too — the chain resumes from them.

    A file rewritten under the same name (a source image generated again) is uploaded
    again: files are compared by modification time, not only by name.

    `kick()` asks for a sync in the background; `close()` runs the last one and tells
    whether every file made it to the store (only then may the folder be deleted).
    """

    def __init__(
        self,
        job_id: str,
        output: JobOutput,
        objects: ObjectStore,
        repo: JobRepository,
        on_synced: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._job_id = job_id
        self._output = output
        self._objects = objects
        self._repo = repo
        self._on_synced = on_synced
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._failed: set[str] = set()
        directory = output.directory
        self._synced: dict[str, int] = (
            {p.name: p.stat().st_mtime_ns for p in directory.iterdir() if p.is_file() and p.name in output.stored}
            if directory.is_dir()
            else {}
        )
        self._task = asyncio.create_task(self._loop(), name=f"sync_{job_id}")

    def kick(self) -> None:
        self._wake.set()

    async def close(self) -> bool:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await self.sync()
        return not self._failed

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self.sync()

    async def sync(self) -> None:
        async with self._lock:
            directory = self._output.directory
            if not directory.is_dir():
                return
            changed = False
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.suffix not in KIND_BY_SUFFIX:
                    continue
                mtime = path.stat().st_mtime_ns
                if path.name in self._output.stored and self._synced.get(path.name, mtime) == mtime:
                    continue
                try:
                    if path.suffix == ".skip":
                        await self._repo.add_file(self._job_id, path.name, key=None, reason=path.read_text(errors="replace"))
                    else:
                        key = result_key(self._job_id, path.name)
                        size = path.stat().st_size
                        await self._objects.upload(path, key)
                        await self._repo.add_file(self._job_id, path.name, key=key, size=size)
                except Exception as e:
                    if path.name not in self._failed:
                        logger.warning(f"[sync] job {self._job_id}: {path.name} not stored yet: {type(e).__name__}: {e}")
                    self._failed.add(path.name)
                    continue
                self._failed.discard(path.name)
                self._output.stored.add(path.name)
                self._synced[path.name] = mtime
                changed = True
                row = path.stem
                if path == self._output.final(row) and not self._output.mode.chain:
                    path.unlink(missing_ok=True)
            if changed and self._on_synced is not None:
                try:
                    await self._on_synced()
                except Exception as e:
                    logger.debug(f"on_synced callback failed: {e}")
