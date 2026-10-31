import json
import time
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.settings import Settings
from src.infographics import service as service_module


def _docx() -> bytes:
    doc = Document()
    doc.add_paragraph("In 1066 William the Conqueror landed at Pevensey.")
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


async def fake_stage(self, run_id, stage, args, cwd, log_path, entries=None, videos_dir=None):
    """search: two subjects, one with an image; render: a clip per plan item."""
    if stage == "search":
        out = Path(args[args.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "1.1.jpg").write_bytes(b"jpg")
        subjects = [
            {"paragraph_number": 1, "paragraph": "p", "personality": {"name": "William", "years": "1028 — 1087"}, "category": "people", "images": [{"file": "1.1.jpg", "source_id": "x", "url": "u"}]},
            {"paragraph_number": 1, "paragraph": "p", "personality": {"name": "Nobody"}, "category": "people", "images": []},
        ]
        (out / "scenario.json").write_text(json.dumps(subjects))
    else:
        plan = json.loads(Path(args[-1]).read_text())
        for item in plan["items"]:
            Path(item["output"]).write_bytes(b"mp4")
        await self._upload_videos(run_id, entries, videos_dir)


def test_infographics_api_flow(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service_module.InfographicsService, "_stage", fake_stage)
    settings = Settings(
        _env_file=None,
        auth_required=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        work_dir=tmp_path / "work",
        storage_dir=tmp_path / "storage",
        accounts_dir=tmp_path / "accounts",
    )
    with TestClient(create_app(settings)) as client:
        assert {c["id"] for c in client.get("/api/infographics/categories").json()} == {"people", "places", "artifacts"}
        assert client.post("/api/infographics/runs", files={"file": ("s.txt", b"x")}).status_code == 422
        assert client.post("/api/infographics/runs", data={"categories": "dragons"}, files={"file": ("s.docx", _docx())}).status_code == 422

        run = client.post("/api/infographics/runs", data={"categories": "people,places"}, files={"file": ("hastings.docx", _docx())}).json()
        for _ in range(100):
            detail = client.get(f"/api/infographics/runs/{run['id']}").json()
            if detail["state"] in ("done", "failed"):
                break
            time.sleep(0.02)

        assert detail["state"] == "done" and detail["done"] == 1 and detail["total"] == 1
        [card] = detail["cards"]
        assert card["name"] == "William" and card["video_url"].endswith(f"/infographics/{run['id']}/videos/1.1.mp4")
        assert client.get(card["video_url"].removeprefix("http://127.0.0.1:8000")).content == b"mp4"
        assert not (tmp_path / "work" / "infographics" / run["id"]).exists()

        assert client.delete(f"/api/infographics/runs/{run['id']}").status_code == 204
        assert not (tmp_path / "storage" / "infographics" / run["id"]).exists()
