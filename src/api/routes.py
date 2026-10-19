import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from src.api.schemas import Account, JobDetail, JobFile, ModeInfo, Rows, Upload
from src.dolphin import cooldowns
from src.jobs.excel import load_rows
from src.jobs.models import JobRequest, JobStatus
from src.jobs.modes import MODES, Mode
from src.jobs.queue import JobQueue
from src.storage.zip_stream import natural_key, stream_zip, zip_headers

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_BYTES = 20 * 1024 ** 2
MAX_XLSX_BYTES = 10 * 1024 ** 2
SSE_PING_SECONDS = 15.0

router = APIRouter(prefix="/api")


def get_queue(request: Request) -> JobQueue:
    return request.app.state.queue


Queue = Annotated[JobQueue, Depends(get_queue)]


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"file is larger than {limit // 1024 ** 2} MB")
    return data


@router.get("/modes")
async def modes() -> list[ModeInfo]:
    return [
        ModeInfo(
            id=mode,
            title=spec.title,
            makes_images=spec.makes_images,
            makes_videos=spec.makes_videos,
            needs_video_prompt=spec.needs_video_prompt,
            needs_image_prompt=spec.needs_image_prompt,
            avatar=spec.avatar,
            static_image=spec.static_image,
            chain=spec.chain,
        )
        for mode, spec in MODES.items()
    ]


@router.post("/uploads")
async def upload(request: Request, file: UploadFile) -> Upload:
    """Store an input picture (avatar / static image); pass the returned key in the job."""
    name = file.filename or "image.jpg"
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in IMAGE_TYPES:
        raise HTTPException(415, f"expected a picture: {', '.join(sorted(IMAGE_TYPES))}")
    data = await _read_limited(file, MAX_UPLOAD_BYTES)
    key = f"inputs/{uuid.uuid4().hex}{suffix}"
    await request.app.state.objects.put(data, key, name)
    return Upload(key=key, name=name, size=len(data))


@router.post("/rows/xlsx")
async def rows_from_xlsx(file: UploadFile, mode: Annotated[Mode, Form()]) -> Rows:
    """Parse an .xlsx into job rows for the given mode (columns as the old bot took them)."""
    data = await _read_limited(file, MAX_XLSX_BYTES)
    try:
        rows = await asyncio.to_thread(load_rows, io.BytesIO(data), mode)
    except Exception as e:
        raise HTTPException(422, f"cannot read the file: {e}")
    if not rows:
        raise HTTPException(422, "no rows with a number and the needed prompts")
    return Rows(rows=rows)


@router.get("/jobs")
async def list_jobs(queue: Queue, limit: int = 100, project_id: str | None = None) -> list[JobStatus]:
    return await queue.recent(min(max(limit, 1), 500), project_id)


@router.post("/jobs/sync")
async def sync_jobs(queue: Queue) -> dict[str, int]:
    restored = await queue.sync()
    return {"restored": restored}


@router.post("/jobs", status_code=201)
async def create_job(body: JobRequest, queue: Queue) -> JobStatus:
    return await queue.submit(body)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, queue: Queue) -> JobDetail:
    record = await queue.record(job_id)
    status = await queue.get(job_id)
    return JobDetail(
        **status.model_dump(),
        rows=record.job_rows,
        position=await queue.position(job_id),
        video_model_key=record.video_model_key,
        avatar=record.avatar_key,
        static_image=record.static_image_key,
    )


@router.get("/jobs/{job_id}/files")
async def job_files(job_id: str, request: Request, queue: Queue) -> list[JobFile]:
    """Results with presigned links: `url` to show / play, `download_url` to save."""
    record = await queue.record(job_id)
    valid_rows = {r.name for r in record.job_rows}
    objects = request.app.state.objects
    prefix = f"{job_id}_"
    return [
        JobFile(
            name=f.name,
            row=f.row,
            kind=f.kind,
            size=f.size,
            reason=f.reason,
            url=objects.url(f.key) if f.key else None,
            download_url=objects.url(f.key, download_name=prefix + f.name) if f.key else None,
            created_at=f.created_at,
        )
        for f in sorted(await queue.files(job_id), key=lambda f: (natural_key(f.row), f.name))
        if f.row in valid_rows
    ]


@router.get("/jobs/{job_id}/zip")
async def job_zip(
    job_id: str,
    request: Request,
    queue: Queue,
    kind: Literal["video", "image", "all"] | None = None,
) -> StreamingResponse:
    """Stream all done files of a job (or only `video` / `image`) as a ZIP/ZIP64 archive."""
    record = await queue.record(job_id)
    valid_rows = {r.name for r in record.job_rows}
    files = [
        f
        for f in await queue.files(job_id)
        if f.key and f.kind in ("video", "image") and f.row in valid_rows
    ]
    if kind in ("video", "image"):
        selected = [f for f in files if f.kind == kind]
    else:
        selected = files
    if not selected:
        raise HTTPException(404, "no files to download")

    selected.sort(key=lambda f: (natural_key(f.row), f.name))
    both_kinds = len({f.kind for f in selected}) > 1
    items = [
        (f"{'videos' if f.kind == 'video' else 'images'}/{f.name}" if both_kinds else f.name, f.key)
        for f in selected
        if f.key
    ]
    base = (record.title or f"job_{job_id}").strip()
    suffix = "_videos" if kind == "video" else "_images" if kind == "image" else ""
    download_name = f"{base}{suffix}.zip"
    return StreamingResponse(
        stream_zip(request.app.state.objects, items),
        media_type="application/zip",
        headers=zip_headers(download_name, f"job_{job_id}{suffix}.zip"),
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, queue: Queue) -> JobStatus:
    if not await queue.cancel(job_id):
        raise HTTPException(409, "job has already finished")
    return await queue.get(job_id)


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, queue: Queue) -> JobStatus:
    if not await queue.retry(job_id):
        raise HTTPException(409, "only stopped, failed or cancelled jobs can be retried")
    return await queue.get(job_id)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, queue: Queue) -> None:
    await queue.delete(job_id)


@router.get("/events")
async def events(request: Request, queue: Queue) -> StreamingResponse:
    """Server-sent events: a `job` event with the JobStatus on every change of any job."""

    async def stream() -> AsyncIterator[str]:
        async with queue.subscribe() as updates:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    status = await asyncio.wait_for(updates.get(), SSE_PING_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: job\ndata: {status.model_dump_json()}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/accounts")
async def accounts(request: Request) -> list[Account]:
    """Accounts with their pauses in seconds (0 = free)."""
    repo = request.app.state.accounts
    result = []
    for account in repo.all():
        dolphin_id = account.dolphin_id
        result.append(Account(
            name=account.name,
            dolphin_id=dolphin_id,
            proxies=len(account.proxies),
            dead_proxies=len(repo.dead_proxies(account.name)),
            cooldown=cooldowns.cooldown_remaining(dolphin_id),
            image_pause=cooldowns.image_cooldown_remaining(dolphin_id),
            video_pause=cooldowns.video_denied_remaining(dolphin_id),
        ))
    return result
