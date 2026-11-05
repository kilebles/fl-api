import asyncio
import io
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse

from src.accounts.account import AccountInfo
from src.accounts.repository import AccountNotFoundError, AccountRepository
from src.api.auth import AdminCaller, Caller
from src.api.schemas import (
    Account,
    CooldownResetIn,
    JobDetail,
    JobFile,
    LogTailOut,
    ModeInfo,
    ReviveProxiesIn,
    Rows,
    Upload,
)
from src.core.settings import Settings
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


def check_upload_key_owner(key: str, caller: Caller) -> None:
    if not key.startswith("inputs/") or ".." in key:
        raise HTTPException(422, f"uploaded file not found: {key}")
    parts = key.split("/")
    if caller.user_id is not None:
        if len(parts) == 3 and parts[1] != caller.user_id and not caller.is_admin:
            raise HTTPException(422, f"uploaded file not found: {key}")
        if len(parts) == 2 and not caller.is_admin:
            raise HTTPException(422, f"uploaded file not found: {key}")


@router.post("/uploads")
async def upload(request: Request, file: UploadFile, caller: Caller) -> Upload:
    """Store an input picture (avatar / static image); pass the returned key in the job."""
    name = file.filename or "image.jpg"
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in IMAGE_TYPES:
        raise HTTPException(415, f"expected a picture: {', '.join(sorted(IMAGE_TYPES))}")
    data = await _read_limited(file, MAX_UPLOAD_BYTES)
    prefix = f"inputs/{caller.user_id}" if caller.user_id else "inputs"
    key = f"{prefix}/{uuid.uuid4().hex}{suffix}"
    await request.app.state.objects.put(data, key, name)
    return Upload(key=key, name=name, size=len(data))


@router.get("/uploads/download")
async def download_upload(request: Request, key: str, caller: Caller, name: str | None = None) -> RedirectResponse:
    """Redirect to a presigned Content-Disposition attachment URL for an uploaded reference image."""
    check_upload_key_owner(key, caller)
    if not await request.app.state.objects.exists(key):
        raise HTTPException(404, f"uploaded file not found: {key}")
    suffix = PurePosixPath(key).suffix.lower() or ".jpg"
    clean_base = re.sub(r'[\\/:*?"<>|]+', "-", (name or "avatar").strip()).strip() or "avatar"
    download_name = clean_base if clean_base.lower().endswith(tuple(IMAGE_TYPES)) else f"{clean_base}{suffix}"
    return RedirectResponse(request.app.state.objects.url(key, download_name=download_name), status_code=302)


@router.get("/uploads/zip")
async def download_uploads_zip(
    request: Request,
    caller: Caller,
    key: Annotated[list[str], Query(min_length=1, max_length=16)],
    name: str | None = None,
) -> StreamingResponse:
    """Stream multiple uploaded reference images as a single .zip archive."""
    objects = request.app.state.objects
    clean_base = re.sub(r'[\\/:*?"<>|]+', "-", (name or "avatar").strip()).strip() or "avatar"
    items: list[tuple[str, str]] = []
    for idx, k in enumerate(key, start=1):
        check_upload_key_owner(k, caller)
        if not await objects.exists(k):
            raise HTTPException(404, f"uploaded file not found: {k}")
        ext = PurePosixPath(k).suffix.lower() or ".jpg"
        arcname = f"{clean_base} - {idx}{ext}" if len(key) > 1 else f"{clean_base}{ext}"
        items.append((arcname, k))
    return StreamingResponse(
        stream_zip(objects, items),
        media_type="application/zip",
        headers=zip_headers(f"{clean_base}.zip", "avatar_refs.zip"),
    )


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
async def list_jobs(queue: Queue, caller: Caller, limit: int = 100, project_id: str | None = None) -> list[JobStatus]:
    return await queue.recent(min(max(limit, 1), 500), project_id, user_id=caller.user_id)


@router.post("/jobs/sync")
async def sync_jobs(queue: Queue, _admin: AdminCaller) -> dict[str, int]:
    restored = await queue.sync()
    return {"restored": restored}


@router.post("/jobs", status_code=201)
async def create_job(body: JobRequest, request: Request, queue: Queue, caller: Caller) -> JobStatus:
    if body.project_id:
        await request.app.state.projects.get(body.project_id, user_id=caller.user_id)
    for key in (body.avatar, body.static_image, *(body.avatar_keys or ())):
        if key is not None:
            check_upload_key_owner(key, caller)
    return await queue.submit(body, user_id=caller.user_id)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, queue: Queue, caller: Caller) -> JobDetail:
    scope = None if caller.is_admin else caller.user_id
    record = await queue.record(job_id, user_id=scope)
    status = await queue.get(job_id, user_id=scope)
    return JobDetail(
        **status.model_dump(),
        rows=record.job_rows,
        position=await queue.position(job_id),
        video_model_key=record.video_model_key,
        avatar=record.avatar_key,
        avatar_keys=record.resolved_avatar_keys,
        static_image=record.static_image_key,
    )


@router.get("/jobs/{job_id}/files")
async def job_files(job_id: str, request: Request, queue: Queue, caller: Caller) -> list[JobFile]:
    """Results with presigned links: `url` to show / play, `download_url` to save."""
    scope = None if caller.is_admin else caller.user_id
    record = await queue.record(job_id, user_id=scope)
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
        for f in sorted(await queue.files(job_id, user_id=scope), key=lambda f: (natural_key(f.row), f.name))
        if f.row in valid_rows
    ]


@router.get("/jobs/{job_id}/zip")
async def job_zip(
    job_id: str,
    request: Request,
    queue: Queue,
    caller: Caller,
    kind: Literal["video", "image", "all"] | None = None,
) -> StreamingResponse:
    """Stream all done files of a job (or only `video` / `image`) as a ZIP/ZIP64 archive."""
    scope = None if caller.is_admin else caller.user_id
    record = await queue.record(job_id, user_id=scope)
    valid_rows = {r.name for r in record.job_rows}
    files = [
        f
        for f in await queue.files(job_id, user_id=scope)
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
        for _ in (0,)
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
async def cancel_job(job_id: str, queue: Queue, caller: Caller) -> JobStatus:
    scope = None if caller.is_admin else caller.user_id
    if not await queue.cancel(job_id, user_id=scope):
        raise HTTPException(409, "job has already finished")
    return await queue.get(job_id, user_id=scope)


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, queue: Queue, caller: Caller) -> JobStatus:
    scope = None if caller.is_admin else caller.user_id
    if not await queue.retry(job_id, user_id=scope):
        raise HTTPException(409, "only stopped, failed or cancelled jobs can be retried")
    return await queue.get(job_id, user_id=scope)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, queue: Queue, caller: Caller) -> None:
    scope = None if caller.is_admin else caller.user_id
    await queue.delete(job_id, user_id=scope)


@router.get("/events")
async def events(request: Request, queue: Queue, caller: Caller) -> StreamingResponse:
    """Server-sent events: a `job` event with the JobStatus on every change of the caller's jobs."""

    async def stream() -> AsyncIterator[str]:
        async with queue.subscribe(user_id=caller.user_id) as updates:
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


_SECRET_RE = re.compile(r"(Bearer\s+\S+|ya29\.[A-Za-z0-9_\-.]+|https?://[^:@/\s]+:[^@/\s]+@)")


def _account_out(account: AccountInfo, repo: AccountRepository, settings: Settings) -> Account:
    provider = settings.browser_provider
    resolved = account.for_provider(provider)
    profile_id = resolved.dolphin_id if resolved is not None else account.dolphin_id
    enabled_set = set(settings.accounts_enabled)
    is_enabled = resolved is not None and (not enabled_set or account.name in enabled_set)
    dead = sorted(repo.dead_proxies(account.name))
    login, _ = account.local_credentials()
    image_models = settings.image_models
    return Account(
        name=account.name,
        dolphin_id=profile_id,
        provider=provider,
        enabled=is_enabled,
        proxies=len(account.proxies),
        dead_proxies=len(dead),
        dead_proxy_list=dead,
        cooldown=round(cooldowns.cooldown_remaining(profile_id), 1),
        image_pause=round(cooldowns.image_cooldown_remaining(profile_id, image_models), 1),
        video_pause=round(cooldowns.video_denied_remaining(profile_id), 1),
        active_image_model=cooldowns.pick_image_model(profile_id, image_models) if profile_id else None,
        image_quota_limited=cooldowns.image_quota_limited(profile_id),
        flow_project_id=account.project_id,
        has_credentials=login is not None,
        has_totp=account.local_totp_secret() is not None,
    )


@router.get("/accounts")
async def accounts(request: Request, _admin: AdminCaller) -> list[Account]:
    """Accounts with their pauses in seconds (0 = free)."""
    repo: AccountRepository = request.app.state.accounts
    settings: Settings = request.app.state.settings
    return [_account_out(acc, repo, settings) for acc in repo.all()]


@router.post("/accounts/{account_name}/reset-cooldown")
async def reset_account_cooldown(
    account_name: str, body: CooldownResetIn, request: Request, _admin: AdminCaller
) -> Account:
    """Clear cooldowns ('all', 'account', 'image', or 'video') on one account."""
    repo: AccountRepository = request.app.state.accounts
    settings: Settings = request.app.state.settings
    try:
        acc = repo.get(account_name)
    except AccountNotFoundError:
        raise HTTPException(404, f"account {account_name} not found") from None
    resolved = acc.for_provider(settings.browser_provider)
    profile_id = resolved.dolphin_id if resolved is not None else acc.dolphin_id
    kind = body.kind.strip().lower()
    if kind not in ("all", "account", "image", "video"):
        raise HTTPException(422, "kind must be 'all', 'account', 'image', or 'video'")
    if kind in ("all", "account"):
        cooldowns.reset_cooldown(profile_id)
    if kind in ("all", "image"):
        cooldowns.reset_image_cooldown(profile_id)
    if kind in ("all", "video"):
        cooldowns.reset_video_denied(profile_id)
    return _account_out(acc, repo, settings)


@router.post("/accounts/{account_name}/revive-proxies")
async def revive_account_proxies(
    account_name: str, body: ReviveProxiesIn, request: Request, _admin: AdminCaller
) -> Account:
    """Restore one dead proxy (`host:port`) or all dead proxies on an account."""
    repo: AccountRepository = request.app.state.accounts
    settings: Settings = request.app.state.settings
    try:
        acc = repo.get(account_name)
    except AccountNotFoundError:
        raise HTTPException(404, f"account {account_name} not found") from None
    targets = [body.proxy.strip()] if body.proxy and body.proxy.strip() else list(repo.dead_proxies(account_name))
    for proxy_key in targets:
        repo.revive_proxy(account_name, proxy_key)
    return _account_out(repo.get(account_name), repo, settings)


@router.get("/admin/logs")
async def admin_logs(
    request: Request,
    _admin: AdminCaller,
    lines: int = 100,
    level: str | None = None,
    search: str | None = None,
) -> LogTailOut:
    """Return sanitized recent lines from the latest server log file."""
    logs_dir: Path = request.app.state.settings.logs_dir
    if not logs_dir.is_dir():
        return LogTailOut()
    files = sorted((p for p in logs_dir.glob("*.log") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return LogTailOut()
    latest = files[0]
    raw = await asyncio.to_thread(latest.read_text, "utf-8", "replace")
    entries = raw.splitlines()
    if level:
        lvl = f"| {level.strip().upper()}"
        entries = [ln for ln in entries if lvl in ln]
    if search:
        needle = search.strip().lower()
        entries = [ln for ln in entries if needle in ln.lower()]
    capped = min(max(lines, 1), 500)
    sanitized = [_SECRET_RE.sub("[REDACTED]", ln) for ln in entries[-capped:]]
    return LogTailOut(file=latest.name, total_matched=len(entries), lines=sanitized)
