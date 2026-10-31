import asyncio
import json
import time
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from src.api.app import create_app
from src.core.settings import Settings
from src.prompts.generation import generate_pairs
from src.prompts.models import I2VPrompt, Pair, SubPeriod
from src.prompts.scenario import parse_docx


class FakeLLM:
    """Answers each structured call by its schema: summaries, subjects, paragraph pairs, templates."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def json(self, model, prompt, schema, temperature, system=None, name="response") -> str:
        await asyncio.sleep(0)
        self.calls.append(name)
        if not isinstance(schema, dict):
            template = I2VPrompt(
                lore="Rome",
                pairs=[Pair(title="Wide Shot"), Pair(title="Close Up"), Pair(title="Detail")],
                sub_periods=[SubPeriod(title="Early Empire", when="27 BC to 1180")],
                deterministic={"image_suffix": "film grain", "negatives": "no text"},
            )
            return template.model_dump_json()
        if name == "summary_response":
            return json.dumps({"summary": "so far"})
        if name == "subject_extraction_response":
            return json.dumps({"subjects": [{"paragraph_number": 1, "subject": "bronze helmet", "era": "Rome"}]})
        count = schema["properties"]["pairs"]["minItems"]
        return json.dumps({"pairs": [{"img": f"image prompt number {i} " * 3, "vid": f"video prompt number {i} " * 3} for i in range(count)]})


def _docx(paragraphs: list[str]) -> bytes:
    doc = Document()
    doc.add_heading("Chapter 1", level=1)
    for text in paragraphs:
        doc.add_paragraph(text)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_scenario_drops_headings():
    assert parse_docx(_docx(["The legion marched north in 1100.", "Winter came early."])) == [
        "The legion marched north in 1100.",
        "Winter came early.",
    ]


async def test_generate_pairs_applies_suffixes_and_stock_queries():
    template = I2VPrompt(
        pairs=[Pair(title="a"), Pair(title="b")],
        sub_periods=[SubPeriod(title="High Middle Ages", when="1000 to 1200")],
        deterministic={"image_suffix": "film grain", "negatives": "no text"},
    )
    progress = []

    async def on_progress(done, total):
        progress.append((done, total))

    pairs = await generate_pairs(FakeLLM(), "gemini-2.5-flash", template, template.pairs, ["In 1100 a knight rode.", "Night fell."], on_progress)

    assert [p.name for p in pairs] == ["1.1", "1.2", "2.1", "2.2"]
    assert pairs[0].img.endswith("film grain. Avoid: no text")
    assert pairs[0].stock_queries == ["bronze helmet, High Middle Ages"] and pairs[2].stock_queries == []
    assert progress[-1] == (2, 2)


def test_prompts_api_flow(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        auth_required=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        work_dir=tmp_path / "work",
        storage_dir=tmp_path / "storage",
        accounts_dir=tmp_path / "accounts",
    )
    with TestClient(create_app(settings, llm=FakeLLM())) as client:
        assert len(client.get("/api/prompts/models").json()) == 9

        template = client.post("/api/prompts/templates", files={"file": ("rome brief.txt", b"Pair 1 wide shot...")}).json()
        assert template["title"] == "rome brief" and template["pairs"] == 3

        bad = client.post("/api/prompts/runs", data={"template_id": template["id"], "model": "gemini-2.5-flash", "pair_count": "9"}, files={"file": ("s.docx", _docx(["A."]))})
        assert bad.status_code == 422

        run = client.post(
            "/api/prompts/runs",
            data={"template_id": template["id"], "model": "gemini-2.5-flash", "pair_count": "2"},
            files={"file": ("scenario.docx", _docx(["The legion marched in 1100.", "They camped by the river."]))},
        ).json()
        for _ in range(100):
            detail = client.get(f"/api/prompts/runs/{run['id']}").json()
            if detail["state"] in ("done", "failed"):
                break
            time.sleep(0.02)

        assert detail["state"] == "done" and detail["total"] == 2 and len(detail["rows"]) == 4
        workbook = load_workbook(BytesIO(client.get(f"/api/prompts/runs/{run['id']}/xlsx").content))
        header = [c.value for c in workbook.active[1]]
        assert header[:3] == ["number", "img_prompt", "vid_prompt"] and workbook.active["A3"].value == "1.2"

        renamed = client.patch(f"/api/prompts/templates/{template['id']}", json={"title": "Рим"}).json()
        assert renamed["title"] == "Рим" and renamed["data"]["title"] == "Рим"
        assert client.delete(f"/api/prompts/runs/{run['id']}").status_code == 204
        assert client.delete(f"/api/prompts/templates/{template['id']}").status_code == 204
        assert client.get(f"/api/prompts/templates/{template['id']}").status_code == 404
