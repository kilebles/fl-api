import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        auth_required=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        work_dir=tmp_path / "work",
        storage_dir=tmp_path / "storage",
        accounts_dir=tmp_path / "accounts",
    )


def test_old_database_gets_new_columns(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "db.sqlite")
    con.execute("CREATE TABLE jobs (id VARCHAR(32) PRIMARY KEY, title VARCHAR(200))")
    con.commit()
    con.close()

    with TestClient(create_app(_settings(tmp_path))):
        pass

    columns = {row[1] for row in sqlite3.connect(tmp_path / "db.sqlite").execute("PRAGMA table_info(jobs)")}
    assert {"project_id", "mode", "state"} <= columns


def test_projects_and_avatars(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"title": "Битва при Каннах", "style": "35 мм"}).json()
        other = client.post("/api/projects", json={"title": "Помпеи"}).json()
        client.post("/api/jobs", json={"mode": "t2v", "project_id": project["id"], "rows": [{"name": "1", "prompt": "p"}]})
        client.post("/api/jobs", json={"mode": "t2v", "project_id": other["id"], "rows": [{"name": "1", "prompt": "p"}]})

        jobs = client.get("/api/jobs", params={"project_id": project["id"]}).json()
        assert len(jobs) == 1 and jobs[0]["project_id"] == project["id"]
        assert client.patch(f"/api/projects/{project['id']}", json={"style": "зерно плёнки"}).json()["style"] == "зерно плёнки"
        assert [p["title"] for p in client.get("/api/projects").json()] == ["Помпеи", "Битва при Каннах"]

        key = client.post("/api/uploads", files={"file": ("face.png", b"png")}).json()["key"]
        assert client.post("/api/avatars", json={"name": "Ганнибал", "image_keys": ["inputs/nope.png"]}).status_code == 422
        avatar = client.post("/api/avatars", json={"name": "Ганнибал", "appearance": "борода, красный плащ", "image_keys": [key]}).json()
        assert avatar["image_urls"][0].endswith(key)
        assert client.patch(f"/api/avatars/{avatar['id']}", json={"name": "Hannibal"}).json()["name"] == "Hannibal"
        assert client.delete(f"/api/avatars/{avatar['id']}").status_code == 204
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        assert client.get("/api/avatars").json() == []


def test_projects_sync_across_databases_via_object_store(tmp_path: Path):
    shared_storage = tmp_path / "shared_bucket"
    linux_settings = Settings(
        _env_file=None,
        auth_required=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'linux.sqlite'}",
        work_dir=tmp_path / "work_linux",
        storage_dir=shared_storage,
        accounts_dir=tmp_path / "accounts_linux",
    )
    windows_settings = Settings(
        _env_file=None,
        auth_required=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'windows.sqlite'}",
        work_dir=tmp_path / "work_win",
        storage_dir=shared_storage,
        accounts_dir=tmp_path / "accounts_win",
    )

    with TestClient(create_app(linux_settings)) as linux_client:
        project = linux_client.post("/api/projects", json={"title": "Римская республика", "style": "35mm"}).json()

    with TestClient(create_app(windows_settings)) as win_client:
        resp = win_client.post(
            "/api/jobs",
            json={"mode": "t2v", "project_id": project["id"], "rows": [{"name": "1", "prompt": "p"}]},
        )
        assert resp.status_code == 201
        assert resp.json()["project_id"] == project["id"]


def test_avatar_multi_refs_and_360_studio_generation(tmp_path: Path, monkeypatch):
    import time

    from src.accounts.repository import AccountRepository
    from src.engine import scheduler
    from src.jobs.runner import JobRunner
    from tests.fakes import FakeSession

    monkeypatch.setattr(scheduler, "PICK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(scheduler, "INTER_GENERATION_PAUSE", (0.0, 0.0))

    settings = _settings(tmp_path)
    repo = AccountRepository(settings.accounts_dir)
    repo.create("1")
    fake_session = FakeSession("account_1", tmp_path / "scratch")
    runner = JobRunner(settings, repo)
    monkeypatch.setattr(runner, "_make_sessions", lambda accounts, strategy: [fake_session])

    with TestClient(create_app(settings, runner=runner)) as client:
        # 1. Existing avatar with 1 photo -> build 360° angles -> grows to 4 image_keys
        k1 = client.post("/api/uploads", files={"file": ("portrait.png", b"png-1")}).json()["key"]
        av = client.post(
            "/api/avatars",
            json={"name": "Марк Аврелий", "appearance": "кудрявая борода, белый плащ", "image_keys": [k1]},
        ).json()

        started = client.post(f"/api/avatars/{av['id']}/angles", json={})
        assert started.status_code == 202
        assert started.json()["pack_job_id"] is not None
        assert started.json()["pack_total"] == 3

        for _ in range(200):
            listed = client.get("/api/avatars").json()
            current = next(a for a in listed if a["id"] == av["id"])
            if current["pack_job_id"] is None and len(current["image_keys"]) == 4:
                break
            time.sleep(0.02)

        assert len(current["image_keys"]) == 4
        assert current["image_keys"][0] == k1
        assert len(current["image_urls"]) == 4

        # 2. Submit i2i2v job with all 4 avatar_keys -> verify all 4 reach make_image & 2 anchors reach submit_video
        fake_session.image_references.clear()
        fake_session.video_extra_references.clear()
        job = client.post(
            "/api/jobs",
            json={
                "mode": "i2i2v",
                "avatar": current["image_keys"][0],
                "avatar_keys": current["image_keys"],
                "rows": [{"name": "1", "image_prompt": "standing in senate", "prompt": "slow push in"}],
            },
        ).json()
        for _ in range(200):
            detail = client.get(f"/api/jobs/{job['id']}").json()
            if detail["state"] == "done":
                break
            time.sleep(0.02)

        assert detail["state"] == "done"
        assert detail["avatar_keys"] == current["image_keys"]
        assert len(fake_session.image_references) == 1
        assert len(fake_session.image_references[0]) == 4
        assert len(fake_session.video_extra_references) == 1
        assert len(fake_session.video_extra_references[0]) == 2

        # 3. Generate a complete 4-angle avatar from text alone (Stage 1 front -> Stage 2 turnaround)
        gen = client.post(
            "/api/avatars/generate",
            json={"name": "Клеопатра", "appearance": "золотая диадема, льняное платье"},
        )
        assert gen.status_code == 201
        gen_id = gen.json()["id"]
        assert gen.json()["pack_stage"] == "base"
        assert gen.json()["pack_total"] == 4

        for _ in range(300):
            listed = client.get("/api/avatars").json()
            cleo = next(a for a in listed if a["id"] == gen_id)
            if cleo["pack_job_id"] is None and len(cleo["image_keys"]) == 4:
                break
            time.sleep(0.02)

        assert len(cleo["image_keys"]) == 4
        assert cleo["pack_job_id"] is None


def test_smart_reference_framing_analysis_no_duplicates(tmp_path: Path, monkeypatch):
    from src.accounts.repository import AccountRepository
    from src.avatars.angles import ReferenceAnalyzer, detect_image_mime, recommend_angles_for
    from src.engine import scheduler
    from src.jobs.runner import JobRunner
    from src.storage.objects import make_object_store
    from tests.fakes import FakeSession

    # 1. Pure matrix checks: zero duplicate framing/angles
    assert recommend_angles_for("full_body", "front") == ["front", "three_quarter", "back"]
    assert recommend_angles_for("full_body", "three_quarter") == ["front", "profile", "back"]
    assert recommend_angles_for("full_body", "profile") == ["front", "three_quarter", "back"]
    assert recommend_angles_for("full_body", "back") == ["front", "three_quarter", "profile"]
    assert recommend_angles_for("close_up", "front") == ["three_quarter", "profile", "full_body"]
    assert recommend_angles_for("medium", "three_quarter") == ["front", "profile", "full_body"]
    assert recommend_angles_for("medium", "profile") == ["front", "three_quarter", "full_body"]
    assert recommend_angles_for("turnaround_sheet", "front") == ["front", "three_quarter", "profile"]

    assert detect_image_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert detect_image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPrest") == "image/webp"

    # 2. End-to-end API + cache + auto-selection in AvatarStudio when angles=None
    monkeypatch.setattr(scheduler, "PICK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(scheduler, "INTER_GENERATION_PAUSE", (0.0, 0.0))

    settings = _settings(tmp_path)
    store = make_object_store(settings)
    calls: list[str] = []

    async def fake_classifier(data: bytes, mime: str):
        calls.append(mime)
        return ("full_body", "front")

    analyzer = ReferenceAnalyzer("", store, classifier=fake_classifier)
    repo = AccountRepository(settings.accounts_dir)
    repo.create("1")
    fake_session = FakeSession("account_1", tmp_path / "scratch")
    runner = JobRunner(settings, repo)
    monkeypatch.setattr(runner, "_make_sessions", lambda accounts, strategy: [fake_session])

    with TestClient(create_app(settings, objects=store, runner=runner, analyzer=analyzer)) as client:
        key = client.post("/api/uploads", files={"file": ("fullbody.png", b"\x89PNG\r\n\x1a\nbody")}).json()["key"]
        r1 = client.post("/api/avatars/analyze-reference", json={"key": key})
        assert r1.status_code == 200
        body = r1.json()
        assert body["detected"] is True
        assert body["framing"] == "full_body"
        assert body["angle"] == "front"
        assert body["summary_ru"] == "в полный рост · анфас 0°"
        assert body["recommended_angles"] == ["front", "three_quarter", "back"]

        # Second call hits in-memory LRU cache without invoking classifier again
        r2 = client.post("/api/avatars/analyze-reference", json={"key": key})
        assert r2.status_code == 200
        assert calls == ["image/png"]

        # Calling /angles without explicit angles uses analyzer -> ["front", "three_quarter", "back"] (no "full_body" duplicate!)
        av = client.post("/api/avatars", json={"name": "Цезарь", "appearance": "тога", "image_keys": [key]}).json()
        started = client.post(f"/api/avatars/{av['id']}/angles", json={}).json()
        job_detail = client.get(f"/api/jobs/{started['pack_job_id']}").json()
        assert [row["name"] for row in job_detail["rows"]] == ["front", "three_quarter", "back"]

