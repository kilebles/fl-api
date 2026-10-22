import json
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    voice_dir = tmp_path / "voice"
    (voice_dir / "tools").mkdir(parents=True, exist_ok=True)
    voices_data = {
        "voices": [
            {"id": "98762dc8579647fcbed3fd2cdeca80a1", "name": "Голос 1 (Brian)"},
            {"id": "1c0fbdf66f484cde9bb6fd27d7012a98", "name": "RU 2"},
            {"id": "e4cd5ab1a4c64b0a9ec9bf156bc8e0bd", "name": "ESP 2"},
        ],
        "profiles": [
            {
                "id": "p1",
                "name": "Голос 1 (Indians)",
                "voices": {
                    "EN": "98762dc8579647fcbed3fd2cdeca80a1",
                    "RU": "1c0fbdf66f484cde9bb6fd27d7012a98",
                    "ES": "e4cd5ab1a4c64b0a9ec9bf156bc8e0bd",
                },
                "percents": {"RU": [70, 75], "ES": [90, 95]},
            }
        ],
        "active": "p1",
    }
    (voice_dir / "tools" / "voices.json").write_text(json.dumps(voices_data, ensure_ascii=False), encoding="utf-8")
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        work_dir=tmp_path / "work",
        storage_dir=tmp_path / "storage",
        accounts_dir=tmp_path / "accounts",
        voice_dir=voice_dir,
    )


def _make_docx_bytes() -> bytes:
    doc = Document()
    doc.add_paragraph(
        "1.\t[calm, intimate documentary narrator] How do you turn a frozen plain into a warm home? "
        "[pause] They had <emphasis>no wood for a fire</emphasis>."
    )
    doc.add_paragraph("2.\tIn 1923 the Arctic travelers built an igloo in forty-five minutes.")
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_voice_api_end_to_end(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        # 1. Models & Languages
        models = client.get("/api/voice/models").json()
        assert [m["id"] for m in models["voice_models"]] == ["s2.1", "drama-3"]

        langs = client.get("/api/voice/languages").json()
        assert any(l["code"] == "RU" for l in langs)

        # 2. Profiles & Voices (22.10: mandatory name + check)
        profs = client.get("/api/voice/profiles").json()
        assert len(profs) == 1 and profs[0]["id"] == "p1"

        assert client.post("/api/voice/voices", json={"fish_url": "98762dc8579647fcbed3fd2cdeca80a1", "name": ""}).status_code == 422
        added = client.post(
            "/api/voice/voices",
            json={"fish_url": "https://fish.audio/m/abcdef0123456789abcdef0123456789", "name": "Narrator DE", "langs": ["DE"]},
        ).json()
        assert added["name"] == "Narrator DE"
        assert added["status"] == "ok"

        checked = client.post("/api/voice/voices/check", json={"profile_id": "p1"}).json()
        assert len(checked) >= 1

        # 3. Upload .docx script with Drama 3 emotion tags
        script = client.post(
            "/api/voice/scripts",
            files={"file": ("arctic_story.docx", _make_docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        ).json()
        assert script["paragraphs_total"] == 2
        assert script["emotion_tags_count"] == 1

        # 4. Create Drama 3 job with carry_mood
        created = client.post(
            "/api/voice/jobs",
            json={
                "script_id": script["script_id"],
                "mode": "translate",
                "source_lang": "EN",
                "targets": ["RU"],
                "profile_id": "p1",
                "model": "gemini-4-argon-medium",
                "voice_model": "drama-3",
                "carry_mood": True,
            },
        ).json()
        job_id = created["id"]
        assert created["voice_model"] == "drama-3"
        assert created["carry_mood"] is True

        # 5. Paragraphs, carry_mood, tags check, and optimistic locking (version)
        paras = client.get(f"/api/voice/jobs/{job_id}/languages/RU/paragraphs").json()
        assert len(paras) == 2
        assert paras[1]["carried_mood"] == "[calm, intimate documentary narrator]"

        # Edit paragraph #1 and test version conflict
        p1 = client.patch(
            f"/api/voice/jobs/{job_id}/languages/RU/paragraphs/1",
            json={
                "text": "[calm, intimate documentary narrator] Как превратить ледяную пустошь в тёплый дом? [pause] У них <emphasis>не было дров</emphasis>.",
                "version": paras[0]["version"],
            },
        ).json()
        assert p1["edited"] is True
        assert p1["tags_ok"] is True

        conflict = client.patch(
            f"/api/voice/jobs/{job_id}/languages/RU/paragraphs/1",
            json={"text": "Конфликт версий", "version": paras[0]["version"]},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "version_conflict"

        # Break emotion tag and verify filter=tags catches it
        client.patch(
            f"/api/voice/jobs/{job_id}/languages/RU/paragraphs/1",
            json={"text": "[спокойно] Как превратить пустошь в дом?", "version": p1["version"]},
        )
        tag_issues = client.get(f"/api/voice/jobs/{job_id}/languages/RU/paragraphs?filter=tags").json()
        assert len(tag_issues) == 1
        assert tag_issues[0]["tags_ok"] is False
