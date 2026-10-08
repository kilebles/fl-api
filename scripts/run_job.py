"""
Run one generation job from an .xlsx file (the same files the Telegram bot took).

Usage:
    uv run python scripts/run_job.py t2v prompts/rome_179.xlsx
    uv run python scripts/run_job.py i2v i2v_rome_220.xlsx --strategy sequential
    uv run python scripts/run_job.py i2v i2v_rome_220.xlsx --static-image cover.jpg
    uv run python scripts/run_job.py i2i2v i2v_rome_220.xlsx --avatar face.png
    uv run python scripts/run_job.py f2v f2v_story_1.xlsx

Results go to output/<file name without .xlsx>/ unless --out is given. Re-running the
same command resumes: rows with a result or a .skip marker are not generated again.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.accounts.repository import AccountRepository
from src.core.logger import setup_logger, setup_monitor
from src.core.settings import get_settings
from src.jobs.excel import load_rows
from src.jobs.models import JobProgress, JobSpec, JobState
from src.jobs.modes import Mode, Strategy
from src.jobs.runner import JobRunner


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Flow generation job from an .xlsx file")
    parser.add_argument("mode", choices=[m.value for m in Mode])
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("--strategy", choices=[s.value for s in Strategy], default=Strategy.PARALLEL.value)
    parser.add_argument("--out", type=Path, help="output folder (default: output/<xlsx stem>)")
    parser.add_argument("--avatar", type=Path, help="i2i2v: reference picture")
    parser.add_argument("--static-image", type=Path, help="i2v: one picture for every video")
    parser.add_argument("--model", help="override the mode's video model key")
    args = parser.parse_args()

    settings = get_settings()
    setup_logger(settings.log_level)
    if settings.monitor_url:
        setup_monitor(settings.monitor_url)

    mode = Mode(args.mode)
    spec = JobSpec(
        mode=mode,
        rows=load_rows(args.xlsx, mode),
        output_dir=args.out or settings.output_dir / args.xlsx.stem,
        strategy=Strategy(args.strategy),
        avatar=args.avatar,
        static_image=args.static_image,
        video_model_key=args.model,
    )

    async def on_progress(state: JobState, progress: JobProgress, message: str | None) -> None:
        logger.success(
            f"[{state}] pass {progress.pass_num}: images {progress.images_done}, videos {progress.videos_done}, "
            f"skipped {progress.skipped}, left {progress.remaining}/{progress.total}" + (f" — {message}" if message else "")
        )

    runner = JobRunner(settings, AccountRepository(settings.accounts_dir))
    state, progress, message = await runner.run(spec, on_progress)
    logger.success(f"Finished: {state} — {progress.model_dump()}" + (f" — {message}" if message else ""))


if __name__ == "__main__":
    asyncio.run(main())
