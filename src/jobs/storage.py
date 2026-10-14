from pathlib import Path

from src.engine.scheduler import WorkItem
from src.jobs.models import JobProgress, JobRow, JobSpec
from src.jobs.modes import MODES, ModeSpec


class JobOutput:
    """
    The state of a job's results: `{name}.jpg` (image), `{name}.mp4` (video) and
    `{name}.skip` (permanently given up, with the reason inside).

    A file counts when it is in the output folder or in `stored` — the names already
    moved to the object store. Re-running a job only does the rows that have neither a
    final result nor a skip.
    """

    def __init__(self, spec: JobSpec, stored: set[str] | None = None) -> None:
        self._spec = spec
        self.mode: ModeSpec = MODES[spec.mode]
        self.directory = spec.output_dir
        self.stored: set[str] = stored if stored is not None else set()

    def image(self, name: str) -> Path:
        return self.directory / f"{name}.jpg"

    def video(self, name: str) -> Path:
        return self.directory / f"{name}.mp4"

    def skip(self, name: str) -> Path:
        return self.directory / f"{name}.skip"

    def has(self, path: Path) -> bool:
        return path.name in self.stored or path.exists()

    def final(self, name: str) -> Path:
        """The file that finishes a row: its video, or its image in image-only modes."""
        return self.video(name) if self.mode.makes_videos else self.image(name)

    def is_finished(self, row: JobRow) -> bool:
        return self.has(self.skip(row.name)) or self.has(self.final(row.name))

    def missing(self) -> list[JobRow]:
        return [row for row in self._spec.rows if not self.is_finished(row)]

    def needs_images(self, rows: list[JobRow]) -> bool:
        """True when some of `rows` still need a generated image."""
        if not self.mode.makes_images or self._spec.static_image is not None:
            return False
        return any(not self.has(self.image(row.name)) for row in rows)

    def progress(self, pass_num: int = 0) -> JobProgress:
        rows = self._spec.rows
        images = sum(1 for r in rows if self.has(self.image(r.name))) if self.mode.makes_images else 0
        videos = sum(1 for r in rows if self.has(self.video(r.name))) if self.mode.makes_videos else 0
        skipped = sum(1 for r in rows if self.has(self.skip(r.name)))
        return JobProgress(
            total=len(rows),
            images_done=images,
            videos_done=videos,
            skipped=skipped,
            remaining=sum(1 for r in rows if not self.is_finished(r)),
            pass_num=pass_num,
        )

    def work_items(self, rows: list[JobRow]) -> list[WorkItem]:
        """Scheduler items for the given (missing) rows."""
        static = self._spec.static_image
        items = []
        for index, row in enumerate(rows):
            if not self.mode.makes_videos:
                items.append(WorkItem(index=index, name=row.name, image_prompt=row.image_prompt, image_path=self.image(row.name)))
            elif not self.mode.makes_images:
                items.append(WorkItem(index=index, name=row.name, video_prompt=row.prompt))
            else:
                items.append(WorkItem(
                    index=index,
                    name=row.name,
                    video_prompt=row.prompt,
                    image_prompt=None if static else row.image_prompt,
                    image_path=static or self.image(row.name),
                ))
        return items
