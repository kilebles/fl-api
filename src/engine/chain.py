"""Frame-to-video chain: each clip starts from the last frame of the previous one."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

from src.core.errors import GenerationFilteredError
from src.engine.frames import extract_last_frame
from src.engine.scheduler import BatchStats, check_disk_space, mark_skip
from src.engine.session import FATAL_ERRORS, AccountSession


async def run_chain(
    session: AccountSession,
    clips: list[tuple[str, str]],
    output_dir: Path,
    first_model_key: str | None = None,
    on_change: Callable[[], Awaitable[None]] | None = None,
) -> BatchStats:
    """
    Generate `clips` ([(name, prompt)], in order) as one chain:

    clip 0 → text-to-video → {name}.mp4 → last frame → frames/{name}.jpg
    clip 1 → frame-to-video from that frame → … and so on.

    Sequential by design. Existing clips are kept (resume), their frames re-extracted when
    missing. A filtered clip gets a .skip marker; a failed one is retried by the next pass.
    The chain continues from the last good frame, so one bad clip does not break the rest.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    check_disk_space(output_dir)
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    stats = BatchStats()
    last_frame: Path | None = None

    async def notify() -> None:
        if on_change is not None:
            try:
                await on_change()
            except Exception as e:
                logger.debug(f"on_change callback failed: {e}")

    await session.ensure_started()

    for index, (name, prompt) in enumerate(clips):
        final_path = output_dir / f"{name}.mp4"
        frame_path = frames_dir / f"{name}.jpg"

        if final_path.exists():
            if not frame_path.exists():
                try:
                    await asyncio.to_thread(extract_last_frame, final_path, frame_path)
                except Exception as e:
                    logger.warning(f"[{index}] f2v: could not extract frame from existing {name}.mp4: {e}")
            if frame_path.exists():
                last_frame = frame_path
            stats.done += 1
            continue
        if (output_dir / f"{name}.skip").exists():
            stats.skipped += 1
            continue

        try:
            if index == 0:
                submitted = await session.submit_video(index, prompt, model_key=first_model_key)
            else:
                if last_frame is None or not last_frame.exists():
                    raise RuntimeError(f"No usable frame to continue the chain at {name} — no prior clip succeeded")
                logger.info(f"[{index}] f2v: {name} from {last_frame.name}")
                submitted = await session.submit_video(index, prompt, frame=last_frame)
            path = await session.finish_video(index, submitted)
        except GenerationFilteredError as e:
            logger.warning(f"[{index}] f2v: {name} filtered — skipped")
            mark_skip(output_dir, name, str(e))
            stats.skipped += 1
            await notify()
            continue
        except FATAL_ERRORS:
            raise
        except Exception as e:
            logger.error(f"[{index}] f2v: failed {name}: {type(e).__name__}: {e}")
            stats.failed += 1
            await notify()
            continue

        path.replace(final_path)
        await asyncio.to_thread(extract_last_frame, final_path, frame_path)
        logger.bind(event_type="success", account=session.name).success(f"[{index}] f2v: saved {final_path}, frame → {frame_path.name}")
        last_frame = frame_path
        stats.done += 1
        await notify()

    return stats
