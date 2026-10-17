import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
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
