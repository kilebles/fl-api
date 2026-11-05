"""Studio 360° turnaround angles for avatars: prompt templates, smart reference framing analysis, and live JobQueue attachment."""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from google import genai
from loguru import logger
from pydantic import BaseModel, Field

from src.avatars.service import AvatarNotFoundError, AvatarRecord, AvatarService
from src.jobs.models import JobRequest, JobRow, JobState, JobStatus
from src.jobs.modes import Mode, Strategy
from src.jobs.queue import JobQueue
from src.storage.objects import ObjectStore

AngleId = Literal["front", "three_quarter", "profile", "back", "full_body"]
ReferenceFraming = Literal["close_up", "medium", "full_body", "turnaround_sheet"]
ReferenceAngle = Literal["front", "three_quarter", "profile", "back"]

TURNAROUND_ORDER: tuple[AngleId, ...] = ("front", "three_quarter", "profile", "back", "full_body")
DEFAULT_TURNAROUND_ANGLES: tuple[AngleId, ...] = ("three_quarter", "profile", "full_body")
MAX_AVATAR_REFS = 8
ANALYSIS_CACHE_MAX = 256


@dataclass(frozen=True)
class AngleSpec:
    id: AngleId
    title: str
    badge: str
    from_reference_prompt: str
    from_scratch_prompt: str


ANGLES: dict[AngleId, AngleSpec] = {
    "front": AngleSpec(
        id="front",
        title="Крупный анфас",
        badge="0°",
        from_reference_prompt=(
            "Studio character reference close-up portrait of the exact same person from the reference image, "
            "clean front-facing 0-degree view looking directly into the camera, tight head and shoulders framing "
            "for sharp facial detail, 85mm portrait lens, calm neutral expression, soft even three-point studio lighting, "
            "plain matte neutral grey studio backdrop, identical facial bone structure, eyes, skin texture, hair, and collar"
        ),
        from_scratch_prompt=(
            "Studio character reference portrait, clean front-facing head and shoulders view looking directly "
            "into the camera, 85mm portrait lens, calm neutral expression, soft even three-point studio lighting, "
            "plain matte neutral grey studio backdrop, sharp photorealistic skin texture, natural hair, and fabric detail"
        ),
    ),
    "three_quarter": AngleSpec(
        id="three_quarter",
        title="Полуоборот 3/4",
        badge="45°",
        from_reference_prompt=(
            "Studio character reference portrait of the exact same person from the reference image, "
            "three-quarter 45-degree angle view, chest-up medium close-up framing, 85mm portrait lens, "
            "calm natural expression, soft even three-point studio lighting, plain matte neutral grey studio backdrop, "
            "preserving exact facial bone structure, eyes, nose, hairstyle, and outfit details"
        ),
        from_scratch_prompt=(
            "Studio character reference portrait, three-quarter 45-degree angle view, chest-up framing, "
            "85mm portrait lens, calm natural expression, soft even studio lighting, plain matte neutral grey studio backdrop"
        ),
    ),
    "profile": AngleSpec(
        id="profile",
        title="Профиль",
        badge="90°",
        from_reference_prompt=(
            "Studio character reference portrait of the exact same person from the reference image, "
            "strict 90-degree side profile view, chest-up framing, 85mm lens, neutral expression, "
            "soft even studio lighting, plain matte neutral grey studio backdrop, clearly showing exact nose bridge, "
            "jawline, ear, back of hairstyle, and neckline matching the reference character"
        ),
        from_scratch_prompt=(
            "Studio character reference portrait, strict 90-degree side profile view, chest-up framing, "
            "85mm lens, neutral expression, soft even studio lighting, plain matte neutral grey studio backdrop"
        ),
    ),
    "back": AngleSpec(
        id="back",
        title="Со спины",
        badge="180°",
        from_reference_prompt=(
            "Studio full-length rear reference view of the exact same person from the reference image "
            "standing on a plain matte neutral grey cyclorama studio floor, strict 180-degree back view from head to footwear, "
            "head slightly turned 15 degrees to reveal hairline and ear silhouette, soft even full-length studio lighting, "
            "50mm lens, single figure only, matching exact hair back, collar, shoulders, back of complete outfit, and shoes"
        ),
        from_scratch_prompt=(
            "Studio full-length rear character reference standing on a plain matte neutral grey cyclorama studio floor, "
            "strict 180-degree back view from head to footwear, head slightly turned 15 degrees showing hairline and ear silhouette, "
            "soft even full-length studio lighting, 50mm lens, single figure only"
        ),
    ),
    "full_body": AngleSpec(
        id="full_body",
        title="Полный рост",
        badge="0°/180°",
        from_reference_prompt=(
            "Studio character turnaround reference sheet of the exact same person from the reference image "
            "standing on a plain matte neutral grey cyclorama studio floor, soft even full-length studio lighting, "
            "50mm lens: two full-body views side by side in one frame, full-body front view from head to footwear "
            "on the left and full-body back view on the right, identical face, proportions, complete outfit, and shoes"
        ),
        from_scratch_prompt=(
            "Studio character turnaround reference sheet standing on a plain matte neutral grey cyclorama studio floor, "
            "soft even full-length studio lighting, 50mm lens: two full-body views side by side in one frame, "
            "full-body front view from head to footwear on the left and full-body back view on the right"
        ),
    ),
}

_FRAMING_LABELS_RU: dict[ReferenceFraming, str] = {
    "close_up": "крупный портрет",
    "medium": "поясной портрет",
    "full_body": "в полный рост",
    "turnaround_sheet": "лист ракурсов",
}

_ANGLE_LABELS_RU: dict[ReferenceAngle, str] = {
    "front": "анфас 0°",
    "three_quarter": "полуоборот 45°",
    "profile": "профиль 90°",
    "back": "со спины 180°",
}


class _VisionClassification(BaseModel):
    framing: ReferenceFraming = Field(
        description=(
            "'close_up' = head/face/shoulders only; "
            "'medium' = chest-up, waist-up, or knees-up without feet; "
            "'full_body' = single figure visible from head all the way down to legs/feet; "
            "'turnaround_sheet' = multiple poses/angles of the same character side-by-side in one image."
        )
    )
    angle: ReferenceAngle = Field(
        description=(
            "Dominant camera angle on the character: "
            "'front' = facing camera (0-20 deg), "
            "'three_quarter' = angled 3/4 view (25-65 deg), "
            "'profile' = side view (70-115 deg), "
            "'back' = seen from behind (135-180 deg)."
        )
    )


@dataclass(frozen=True)
class ReferenceAnalysis:
    key: str
    detected: bool
    framing: ReferenceFraming
    angle: ReferenceAngle
    summary_ru: str
    recommended_angles: list[AngleId]


def recommend_angles_for(framing: ReferenceFraming, angle: ReferenceAngle) -> list[AngleId]:
    """Return 3 optimal non-duplicating studio angles so #1 + 3 angles = 4 Flow multi-reference slots."""
    if framing == "turnaround_sheet":
        return ["front", "three_quarter", "profile"]
    if framing == "full_body":
        # User already uploaded a full-body shot: add a crisp close-up face anchor ("front") + missing angles,
        # and use single rear view ("back") instead of repeating a front+back full_body sheet.
        if angle == "front":
            return ["front", "three_quarter", "back"]
        if angle == "three_quarter":
            return ["front", "profile", "back"]
        if angle == "profile":
            return ["front", "three_quarter", "back"]
        return ["front", "three_quarter", "profile"]
    # close_up or medium portrait: include full_body (front+back) and skip whichever portrait angle #1 already covers
    if angle == "front":
        return ["three_quarter", "profile", "full_body"]
    if angle == "three_quarter":
        return ["front", "profile", "full_body"]
    return ["front", "three_quarter", "full_body"]


def format_reference_summary(framing: ReferenceFraming, angle: ReferenceAngle) -> str:
    if framing == "turnaround_sheet":
        return _FRAMING_LABELS_RU[framing]
    return f"{_FRAMING_LABELS_RU[framing]} · {_ANGLE_LABELS_RU[angle]}"


def detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/jpeg"


ReferenceClassifier = Callable[[bytes, str], Awaitable[tuple[ReferenceFraming, ReferenceAngle]]]

_CLASSIFY_PROMPT = (
    "Classify the primary character photo by shot framing and camera angle so a 360-degree reference studio "
    "can generate only the missing angles without duplicates."
)


class ReferenceAnalyzer:
    """Classifies an avatar's primary reference image (`framing` + `angle`) via Gemini Interactions API."""

    def __init__(
        self,
        google_api_key: str,
        objects: ObjectStore,
        *,
        classifier: ReferenceClassifier | None = None,
    ) -> None:
        self._objects = objects
        self._client = genai.Client(api_key=google_api_key) if google_api_key else None
        self._classifier = classifier
        self._cache: OrderedDict[str, ReferenceAnalysis] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[ReferenceAnalysis]] = {}

    def _fallback(self, key: str) -> ReferenceAnalysis:
        return ReferenceAnalysis(
            key=key,
            detected=False,
            framing="medium",
            angle="front",
            summary_ru=format_reference_summary("medium", "front"),
            recommended_angles=list(DEFAULT_TURNAROUND_ANGLES),
        )

    async def analyze_key(self, key: str) -> ReferenceAnalysis:
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            return await existing

        task = asyncio.create_task(self._compute(key))
        self._inflight[key] = task
        try:
            return await task
        finally:
            self._inflight.pop(key, None)

    async def _compute(self, key: str) -> ReferenceAnalysis:
        if self._classifier is None and self._client is None:
            return self._fallback(key)
        try:
            data = await self._objects.get_bytes(key)
            mime_type = detect_image_mime(data)
            if self._classifier is not None:
                framing, angle = await self._classifier(data, mime_type)
            else:
                framing, angle = await self._classify_with_gemini(data, mime_type)
            result = ReferenceAnalysis(
                key=key,
                detected=True,
                framing=framing,
                angle=angle,
                summary_ru=format_reference_summary(framing, angle),
                recommended_angles=recommend_angles_for(framing, angle),
            )
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > ANALYSIS_CACHE_MAX:
                self._cache.popitem(last=False)
            return result
        except Exception as e:
            logger.warning(f"[avatar-studio] reference analysis fallback for {key}: {type(e).__name__}: {e}")
            return self._fallback(key)

    async def _classify_with_gemini(self, data: bytes, mime_type: str) -> tuple[ReferenceFraming, ReferenceAngle]:
        assert self._client is not None
        image_b64 = base64.b64encode(data).decode("ascii")
        schema = _VisionClassification.model_json_schema()
        inputs: list[dict[str, Any]] = [
            {"type": "text", "text": _CLASSIFY_PROMPT},
            {"type": "image", "data": image_b64, "mime_type": mime_type},
        ]
        response_format: dict[str, Any] = {
            "type": "text",
            "mime_type": "application/json",
            "schema": schema,
        }
        try:
            interaction = await self._client.aio.interactions.create(
                model="gemini-3.5-flash-lite",
                input=inputs,
                response_format=response_format,
                generation_config={"temperature": 0.0},
                store=False,
                timeout=12.0,
            )
        except Exception as first_err:
            logger.debug(f"[avatar-studio] gemini-3.5-flash-lite failed ({first_err}), retrying gemini-3.8-flash")
            interaction = await self._client.aio.interactions.create(
                model="gemini-3.8-flash",
                input=inputs,
                response_format=response_format,
                generation_config={"thinking_level": "low", "temperature": 0.0},
                store=False,
                timeout=15.0,
            )
        parsed = _VisionClassification.model_validate_json(interaction.output_text or "{}")
        return parsed.framing, parsed.angle


def build_angle_prompt(angle_id: AngleId, appearance: str, style: str = "", *, has_reference: bool) -> str:
    spec = ANGLES[angle_id]
    base = spec.from_reference_prompt if has_reference else spec.from_scratch_prompt
    parts = [base]
    clean_appearance = appearance.strip().rstrip(".")
    if clean_appearance:
        parts.append(f"Character details: {clean_appearance}")
    clean_style = style.strip().rstrip(".")
    if clean_style:
        parts.append(clean_style)
    return ". ".join(parts) + "."


def normalize_angles(angles: list[AngleId] | None) -> list[AngleId]:
    raw = list(angles) if angles is not None else list(DEFAULT_TURNAROUND_ANGLES)
    seen: list[AngleId] = []
    for a in TURNAROUND_ORDER:
        if a in raw and a not in seen:
            seen.append(a)
    return seen


class AvatarBusyError(RuntimeError):
    """Avatar already has an active 360° generation job."""


class AvatarStudio:
    """Coordinates 360° studio angle generation through JobQueue and attaches finished frames to the avatar."""

    def __init__(
        self,
        avatars: AvatarService,
        queue: JobQueue,
        objects: ObjectStore,
        *,
        analyzer: ReferenceAnalyzer | None = None,
    ) -> None:
        self._avatars = avatars
        self._queue = queue
        self._objects = objects
        self._analyzer = analyzer
        self._lock = asyncio.Lock()
        queue.add_hook(self._on_job_event)

    async def start_angles(
        self,
        avatar_id: str,
        *,
        user_id: str | None,
        angles: list[AngleId] | None = None,
        style: str = "",
        project_id: str | None = None,
    ) -> AvatarRecord:
        record = await self._avatars.get(avatar_id, user_id=user_id)
        await self._ensure_not_running(record)

        free_slots = MAX_AVATAR_REFS - len(record.image_keys)
        if free_slots <= 0:
            raise ValueError("У аватара уже максимум референсов (8)")

        if not record.image_keys:
            wanted = normalize_angles(angles)
            if not record.appearance.strip():
                raise ValueError("Опишите внешность аватара или загрузите базовый портрет")
            follow_up = [a for a in wanted if a != "front"][: max(0, MAX_AVATAR_REFS - 1)]
            return await self._submit_base_then_angles(
                record,
                follow_up=follow_up,
                style=style,
                project_id=project_id,
            )

        if angles is None and self._analyzer is not None:
            analysis = await self._analyzer.analyze_key(record.image_keys[0])
            wanted = normalize_angles(analysis.recommended_angles)
        else:
            wanted = normalize_angles(angles)

        selected = wanted[:free_slots]
        if not selected:
            raise ValueError("Выберите хотя бы один ракурс")
        return await self._submit_angles_job(
            record,
            angles=selected,
            style=style,
            project_id=project_id,
        )

    async def create_and_generate(
        self,
        *,
        name: str,
        appearance: str,
        user_id: str | None,
        angles: list[AngleId] | None = None,
        style: str = "",
        project_id: str | None = None,
    ) -> AvatarRecord:
        if not appearance.strip():
            raise ValueError("Опишите внешность персонажа для генерации с нуля")
        wanted = normalize_angles(angles)
        follow_up = [a for a in wanted if a != "front"][: MAX_AVATAR_REFS - 1]
        record = await self._avatars.create(name=name, appearance=appearance, image_keys=[], user_id=user_id)
        try:
            return await self._submit_base_then_angles(
                record,
                follow_up=follow_up,
                style=style,
                project_id=project_id,
            )
        except Exception:
            await self._avatars.delete(record.id, user_id=user_id)
            raise

    async def cancel_angles(self, avatar_id: str, *, user_id: str | None) -> AvatarRecord:
        record = await self._avatars.get(avatar_id, user_id=user_id)
        job_id = record.pack_job_id
        await self._avatars.update(
            avatar_id,
            user_id=user_id,
            clear_pack=True,
        )
        if job_id:
            try:
                await self._queue.cancel(job_id, user_id=user_id)
            except Exception as e:
                logger.debug(f"[avatar-studio] cancel job {job_id} ignored: {e}")
        return await self._avatars.get(avatar_id, user_id=user_id)

    async def _ensure_not_running(self, record: AvatarRecord) -> None:
        if not record.pack_job_id:
            return
        try:
            status = await self._queue.get(record.pack_job_id)
            if not status.state.finished:
                raise AvatarBusyError("Генерация ракурсов для этого аватара уже выполняется")
        except LookupError:
            pass
        await self._avatars.update(record.id, clear_pack=True)

    async def _submit_base_then_angles(
        self,
        record: AvatarRecord,
        *,
        follow_up: list[AngleId],
        style: str,
        project_id: str | None,
    ) -> AvatarRecord:
        row = JobRow(
            name="front",
            image_prompt=build_angle_prompt("front", record.appearance, style, has_reference=False),
        )
        status = await self._queue.submit(
            JobRequest(
                mode=Mode.IMG,
                title=f"Аватар · {record.name} (портрет 0°)",
                project_id=project_id,
                strategy=Strategy.PARALLEL,
                rows=[row],
            ),
            user_id=record.user_id,
        )
        return await self._avatars.update(
            record.id,
            pack_job_id=status.id,
            pack_stage="base",
            pack_pending={
                "angles": list(follow_up),
                "style": style,
                "project_id": project_id,
                "expected": ["front"],
                "imported": {},
            },
        )

    async def _submit_angles_job(
        self,
        record: AvatarRecord,
        *,
        angles: list[AngleId],
        style: str,
        project_id: str | None,
        base_done: int = 0,
    ) -> AvatarRecord:
        anchor = record.image_keys[0]
        rows = [
            JobRow(
                name=angle_id,
                image_prompt=build_angle_prompt(angle_id, record.appearance, style, has_reference=True),
            )
            for angle_id in angles
        ]
        status = await self._queue.submit(
            JobRequest(
                mode=Mode.IMG,
                title=f"360° ракурсы · {record.name}",
                project_id=project_id,
                strategy=Strategy.PARALLEL,
                avatar=anchor,
                avatar_keys=[anchor],
                rows=rows,
            ),
            user_id=record.user_id,
        )
        return await self._avatars.update(
            record.id,
            pack_job_id=status.id,
            pack_stage="angles",
            pack_pending={
                "angles": list(angles),
                "style": style,
                "project_id": project_id,
                "expected": list(angles),
                "imported": {},
                "base_done": base_done,
            },
        )

    async def _on_job_event(self, status: JobStatus) -> None:
        """Import any newly stored angle images into the owning avatar, and chain Stage 1 -> Stage 2."""
        chain_args: tuple[AvatarRecord, list[AngleId], str, str | None] | None = None
        async with self._lock:
            record = await self._avatars.by_pack_job(status.id)
            if record is None:
                return

            pending: dict[str, Any] = dict(record.pack_pending or {})
            imported: dict[str, str] = dict(pending.get("imported") or {})
            expected: list[str] = list(pending.get("expected") or [])

            try:
                job_files = await self._queue.files(status.id)
            except LookupError:
                return

            by_row = {f.row: f for f in job_files if f.kind == "image" and f.key}
            prefix = f"inputs/{record.user_id}" if record.user_id else "inputs"
            new_keys: list[str] = []

            for row_name in expected:
                if row_name in imported or row_name not in by_row:
                    continue
                src_key = by_row[row_name].key
                if not src_key:
                    continue
                try:
                    data = await self._objects.get_bytes(src_key)
                    dest_key = f"{prefix}/{uuid.uuid4().hex}.jpg"
                    await self._objects.put(data, dest_key, f"{record.name}_{row_name}.jpg")
                    imported[row_name] = dest_key
                    new_keys.append(dest_key)
                except Exception as e:
                    logger.warning(f"[avatar-studio] failed to import {src_key} for avatar {record.id}: {e}")

            merged_keys = list(record.image_keys)
            for k in new_keys:
                if k not in merged_keys and len(merged_keys) < MAX_AVATAR_REFS:
                    merged_keys.append(k)

            pending["imported"] = imported
            all_expected_imported = bool(expected) and all(row_name in imported for row_name in expected)
            follow_up: list[AngleId] = [
                a for a in (pending.get("angles") or []) if a in ANGLES and a != "front"
            ]

            try:
                if not status.state.finished:
                    if all_expected_imported and (record.pack_stage == "angles" or not follow_up):
                        await self._avatars.update(record.id, image_keys=merged_keys, clear_pack=True)
                    elif new_keys:
                        await self._avatars.update(record.id, image_keys=merged_keys, pack_pending=pending)
                    return

                # Job finished: check if Stage 1 ("base") should chain into Stage 2 ("angles")
                if (
                    record.pack_stage == "base"
                    and status.state == JobState.DONE
                    and merged_keys
                    and follow_up
                ):
                    updated = await self._avatars.update(record.id, image_keys=merged_keys, pack_pending=pending)
                    free_slots = MAX_AVATAR_REFS - len(updated.image_keys)
                    if free_slots > 0:
                        chain_args = (
                            updated,
                            follow_up[:free_slots],
                            str(pending.get("style") or ""),
                            pending.get("project_id"),
                        )
                    else:
                        await self._avatars.update(record.id, image_keys=merged_keys, clear_pack=True)
                else:
                    await self._avatars.update(record.id, image_keys=merged_keys, clear_pack=True)
            except AvatarNotFoundError:
                return

        if chain_args is not None:
            updated_rec, next_angles, next_style, next_proj = chain_args
            try:
                await self._submit_angles_job(
                    updated_rec,
                    angles=next_angles,
                    style=next_style,
                    project_id=next_proj,
                    base_done=1,
                )
            except Exception as e:
                logger.error(f"[avatar-studio] failed to chain stage 2 for {updated_rec.id}: {e}")
                try:
                    await self._avatars.update(updated_rec.id, clear_pack=True)
                except AvatarNotFoundError:
                    pass
