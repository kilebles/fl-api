"""FastAPI routes for Translation, Voiceover (Fish Audio S2.1 / Drama 3), and Mixing."""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from src.voice.models import (
    AddVoiceRequest,
    CheckVoicesRequest,
    CompareVoiceVariant,
    CreateProfileRequest,
    CreateVoiceJobRequest,
    EstimateRequest,
    EstimateResponse,
    LanguageInfo,
    ModelsResponse,
    ParagraphFilter,
    ParagraphLengthRequest,
    ParagraphLengthResponse,
    PatchParagraphRequest,
    QueueStatusResponse,
    RevoiceSkippedRequest,
    ScriptInfo,
    UpdateProfileRequest,
    VoiceEntry,
    VoiceJob,
    VoiceJobFile,
    VoiceParagraph,
    VoiceProfile,
)
from src.voice.service import VoiceService

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _svc(request: Request) -> VoiceService:
    return request.app.state.voice


@router.get("/models")
async def get_models(request: Request) -> ModelsResponse:
    return _svc(request).get_models()


@router.get("/languages")
async def get_languages(request: Request, profile_id: str | None = None) -> list[LanguageInfo]:
    return _svc(request).get_languages(profile_id)


@router.get("/scripts")
async def list_scripts(request: Request) -> list[ScriptInfo]:
    return _svc(request).list_scripts()


@router.post("/scripts")
async def upload_script(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
) -> ScriptInfo:
    content = await file.read()
    return await _svc(request).upload_script(file.filename or "script.docx", content, title=title)


@router.post("/estimate")
async def estimate_job(body: EstimateRequest, request: Request) -> EstimateResponse:
    return _svc(request).estimate(body)


@router.get("/profiles")
async def list_profiles(request: Request) -> list[VoiceProfile]:
    return _svc(request).list_profiles()


@router.post("/profiles", status_code=201)
async def create_profile(body: CreateProfileRequest, request: Request) -> VoiceProfile:
    return _svc(request).create_profile(body)


@router.patch("/profiles/{profile_id}")
async def update_profile(profile_id: str, body: UpdateProfileRequest, request: Request) -> VoiceProfile:
    return _svc(request).update_profile(profile_id, body)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, request: Request) -> Response:
    _svc(request).delete_profile(profile_id)
    return Response(status_code=204)


@router.get("/voices")
async def list_voices(request: Request) -> list[VoiceEntry]:
    return _svc(request).list_voices()


@router.post("/voices", status_code=201)
async def add_voice(body: AddVoiceRequest, request: Request) -> VoiceEntry:
    return await _svc(request).add_voice(body)


@router.post("/voices/check")
async def check_voices(request: Request, body: CheckVoicesRequest | None = None) -> list[VoiceEntry]:
    return await _svc(request).check_voices(body)


@router.get("/voices/{voice_id}/sample")
async def voice_sample(voice_id: str, request: Request) -> FileResponse:
    path = await _svc(request).ensure_voice_sample(voice_id)
    return FileResponse(path, media_type="audio/mpeg", content_disposition_type="inline")


@router.get("/queue")
async def get_queue(request: Request) -> QueueStatusResponse:
    return _svc(request).queue_status()


@router.get("/jobs")
async def list_jobs(
    request: Request,
    status: str | None = None,
    project_id: str | None = None,
) -> list[VoiceJob]:
    return _svc(request).list_jobs(status=status, project_id=project_id)


@router.post("/jobs", status_code=202)
async def create_job(body: CreateVoiceJobRequest, request: Request) -> dict[str, list[VoiceJob]] | VoiceJob:
    created = await _svc(request).create_jobs(body)
    if body.split_by_language:
        return {"jobs": created}
    return created[0]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request) -> VoiceJob:
    return _svc(request).get_job(job_id)


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: int, request: Request) -> VoiceJob:
    return _svc(request).stop_job(job_id)


@router.post("/jobs/{job_id}/resume", status_code=202)
async def resume_job(job_id: int, request: Request) -> VoiceJob:
    return _svc(request).resume_job(job_id)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: int, request: Request) -> Response:
    _svc(request).delete_job(job_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/revoice-skipped", status_code=202)
async def revoice_skipped(
    job_id: int,
    request: Request,
    body: RevoiceSkippedRequest | None = None,
) -> VoiceJob:
    return await _svc(request).revoice_skipped(job_id, body or RevoiceSkippedRequest())


@router.get("/jobs/{job_id}/languages/{lang}/paragraphs")
async def get_paragraphs(
    job_id: int,
    lang: str,
    request: Request,
    filter: ParagraphFilter = Query(default="all"),
) -> list[VoiceParagraph]:
    return _svc(request).get_paragraphs(job_id, lang, filter_by=filter)


@router.patch("/jobs/{job_id}/languages/{lang}/paragraphs/{n}")
async def patch_paragraph(
    job_id: int,
    lang: str,
    n: int,
    body: PatchParagraphRequest,
    request: Request,
) -> VoiceParagraph:
    return _svc(request).patch_paragraph(job_id, lang, n, body)


@router.post("/jobs/{job_id}/languages/{lang}/paragraphs/{n}/length")
async def measure_paragraph_length(
    job_id: int,
    lang: str,
    n: int,
    body: ParagraphLengthRequest,
    request: Request,
) -> ParagraphLengthResponse:
    return _svc(request).measure_paragraph_length(job_id, lang, n, body.text)


@router.post("/jobs/{job_id}/languages/{lang}/paragraphs/{n}/retranslate", status_code=202)
async def retranslate_paragraph(
    job_id: int,
    lang: str,
    n: int,
    request: Request,
) -> VoiceParagraph:
    return await _svc(request).retranslate_paragraph(job_id, lang, n)


@router.post("/jobs/{job_id}/languages/{lang}/paragraphs/{n}/revoice", status_code=202)
async def revoice_paragraph(
    job_id: int,
    lang: str,
    n: int,
    request: Request,
) -> VoiceParagraph:
    return await _svc(request).revoice_paragraph(job_id, lang, n)


@router.post("/jobs/{job_id}/languages/{lang}/paragraphs/{n}/revert")
async def revert_paragraph(
    job_id: int,
    lang: str,
    n: int,
    request: Request,
) -> VoiceParagraph:
    return _svc(request).revert_paragraph(job_id, lang, n)


@router.post("/jobs/{job_id}/languages/{lang}/paragraphs/{n}/compare-voices")
async def compare_voices(
    job_id: int,
    lang: str,
    n: int,
    request: Request,
) -> list[CompareVoiceVariant]:
    return await _svc(request).compare_paragraph_voices(job_id, lang, n)


@router.get("/jobs/{job_id}/languages/{lang}/paragraphs/{n}/audio")
async def paragraph_audio(
    job_id: int,
    lang: str,
    n: int,
    request: Request,
    track: str = Query(default="mixed"),
) -> FileResponse:
    path = await _svc(request).get_paragraph_audio_path(job_id, lang, n, track=track)
    return FileResponse(path, media_type="audio/mpeg", content_disposition_type="inline")


@router.get("/jobs/{job_id}/languages/{lang}/peaks")
async def language_peaks(job_id: int, lang: str, request: Request) -> dict[int, list[float]]:
    return _svc(request).get_language_peaks(job_id, lang)


@router.get("/jobs/{job_id}/files")
async def job_files(job_id: int, request: Request) -> list[VoiceJobFile]:
    return _svc(request).get_job(job_id).files


@router.get("/jobs/{job_id}/zip")
async def job_zip(job_id: int, request: Request) -> FileResponse:
    path, filename = await _svc(request).build_job_zip(job_id)
    return FileResponse(path, filename=filename, media_type="application/zip", content_disposition_type="attachment")


@router.get("/files/{file_id}")
async def download_file(file_id: str, request: Request) -> FileResponse:
    path, filename = await _svc(request).resolve_download_file(file_id)
    is_audio = filename.endswith(".mp3")
    return FileResponse(
        path,
        filename=filename,
        media_type="audio/mpeg" if is_audio else "application/octet-stream",
        content_disposition_type="inline" if is_audio else "attachment",
    )


async def _sse_stream(
    svc: VoiceService,
    request: Request,
    last_event_id: int | None = None,
    job_id: int | None = None,
) -> AsyncIterator[str]:
    async with svc.subscribe(last_event_id=last_event_id, job_id=job_id) as q:
        while not await request.is_disconnected():
            try:
                item = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"id: {item['id']}\nevent: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            except TimeoutError:
                yield ": keepalive\n\n"


@router.get("/events")
async def global_events(
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    lid = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
    return StreamingResponse(_sse_stream(_svc(request), request, lid, None), media_type="text/event-stream")


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: int,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    lid = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
    return StreamingResponse(_sse_stream(_svc(request), request, lid, job_id), media_type="text/event-stream")
