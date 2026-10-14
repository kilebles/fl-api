from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from src.prompts.models import GeneratedPair

_COLUMNS = (
    ("number", 8),
    ("img_prompt", 60),
    ("vid_prompt", 60),
    ("stock_query_1", 24),
    ("stock_query_2", 24),
    ("stock_query_3", 24),
    ("paragraph", 80),
)


def build_workbook(pairs: list[GeneratedPair]) -> bytes:
    """xlsx of the pairs; `number`, `img_prompt`, `vid_prompt` are what generation jobs read."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Prompts"
    ws.append([name for name, _ in _COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for pair in pairs:
        # stock queries are per paragraph: only its first pair row carries them
        queries = (pair.stock_queries + ["", "", ""])[:3] if pair.pair_number == 1 else ["", "", ""]
        ws.append([pair.name, pair.img, pair.vid, *queries, pair.paragraph_text])
    for i, (_, width) in enumerate(_COLUMNS):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
