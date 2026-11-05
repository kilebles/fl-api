"""Studio 360° turnaround angles for avatars: prompt templates and live JobQueue attachment."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from src.avatars.service import AvatarNotFoundError, AvatarRecord, AvatarService
from src.jobs.models import JobRequest, JobRow, JobState, JobStatus
from src.jobs.modes import Mode, Strategy
from src.jobs.queue import JobQueue
from src.storage.objects import ObjectStore

AngleId = Literal["front", "three_quarter", "profile", "full_body"]

TURNAROUND_ORDER: tuple[AngleId, ...] = ("front", "three_quarter", "profile", "full_body")
DEFAULT_TURNAROUND_ANGLES: tuple[AngleId, ...] = ("three_quarter", "profile", "full_body")
MAX_AVATAR_REFS = 8


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
        title="Анфас",
        badge="0°",
        from_reference_prompt=(
            "Studio character reference portrait of the exact same person from the reference image, "
            "clean front-facing view looking directly into the camera, head and shoulders framing, "
            "85mm portrait lens, calm neutral expression, soft even three-point studio lighting, "
            "plain matte neutral grey studio backdrop, identical facial structure, skin texture, hair, and collar"
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

    def __init__(self, avatars: AvatarService, queue: JobQueue, objects: ObjectStore) -> None:
        self._avatars = avatars
        self._queue = queue
        self._objects = objects
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

        wanted = normalize_angles(angles)
        if not record.image_keys:
            if not record.appearance.strip():
                raise ValueError("Опишите внешность аватара или загрузите базовый портрет")
            follow_up = [a for a in wanted if a != "front"][: max(0, MAX_AVATAR_REFS - 1)]
            return await self._submit_base_then_angles(
                record,
                follow_up=follow_up,
                style=style,
                project_id=project_id,
            )

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

            try:
                if not status.state.finished:
                    if new_keys:
                        await self._avatars.update(record.id, image_keys=merged_keys, pack_pending=pending)
                    return

                # Job finished: check if Stage 1 ("base") should chain into Stage 2 ("angles")
                follow_up: list[AngleId] = [
                    a for a in (pending.get("angles") or []) if a in ANGLES and a != "front"
                ]
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
