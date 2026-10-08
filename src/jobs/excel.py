"""Rows from the .xlsx files the prompt step produced (same columns the Telegram bot accepted)."""

from pathlib import Path

from openpyxl import load_workbook

from src.jobs.models import JobRow
from src.jobs.modes import MODES, Mode


def _cell(row: tuple, col: int | None) -> str:
    if col is None or col >= len(row) or row[col] is None:
        return ""
    return str(row[col]).strip()


def load_rows(path: Path, mode: Mode) -> list[JobRow]:
    """
    Read job rows from the first sheet.

    - t2v / flash_t2v / f2v: columns `number`, `prompt`;
    - img: `number`, `prompt` (the image prompt);
    - i2v / flash_i2v / i2i2v: `number`, `img_prompt`, `vid_prompt`.

    Rows without a number or the needed prompts are left out.
    """
    spec = MODES[mode]
    workbook = load_workbook(path, read_only=True)
    try:
        sheet = workbook.active
        header = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
        columns = {str(value).strip(): idx for idx, value in enumerate(header) if value is not None}
        number_col = columns.get("number")
        if spec.makes_images and spec.makes_videos:
            img_col, vid_col = columns.get("img_prompt"), columns.get("vid_prompt")
            if number_col is None or vid_col is None:
                raise ValueError("Excel must have 'number', 'img_prompt', 'vid_prompt' columns")
        else:
            prompt_col = columns.get("prompt")
            if number_col is None or prompt_col is None:
                raise ValueError("Excel must have 'number' and 'prompt' columns")

        rows = []
        for values in sheet.iter_rows(min_row=2, values_only=True):
            name = _cell(values, number_col)
            if not name:
                continue
            if spec.makes_images and spec.makes_videos:
                row = JobRow(name=name, prompt=_cell(values, vid_col) or None, image_prompt=_cell(values, img_col) or None)
                if row.prompt:
                    rows.append(row)
            elif spec.makes_images:
                prompt = _cell(values, prompt_col)
                if prompt:
                    rows.append(JobRow(name=name, image_prompt=prompt))
            else:
                prompt = _cell(values, prompt_col)
                if prompt:
                    rows.append(JobRow(name=name, prompt=prompt))
        return rows
    finally:
        workbook.close()
