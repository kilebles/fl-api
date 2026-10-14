from pathlib import Path

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from src.accounts.repository import AccountRepository
from src.core.settings import Settings
from src.engine import scheduler
from src.jobs.excel import load_rows
from src.jobs.models import JobRequest, JobRow, JobSpec, JobState, JobStatus
from src.jobs.modes import Mode
from src.jobs.runner import JobRunner
from src.jobs.storage import JobOutput
from src.storage.objects import LocalObjectStore
from tests.fakes import FakeSession


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scheduler, "PICK_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(scheduler, "INTER_GENERATION_PAUSE", (0.0, 0.0))


def _xlsx(path: Path, header: list[str], rows: list[list]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_excel_columns_per_mode(tmp_path: Path):
    t2v = _xlsx(tmp_path / "t.xlsx", ["number", "prompt"], [[1, "a"], [2, None], [None, "x"]])
    i2v = _xlsx(tmp_path / "i.xlsx", ["number", "img_prompt", "vid_prompt"], [[1, "img", "vid"]])

    assert load_rows(t2v, Mode.T2V) == [JobRow(name="1", prompt="a")]
    assert load_rows(t2v, Mode.IMG) == [JobRow(name="1", image_prompt="a")]
    assert load_rows(i2v, Mode.I2V) == [JobRow(name="1", prompt="vid", image_prompt="img")]
    with pytest.raises(ValueError):
        load_rows(t2v, Mode.I2V)


def test_spec_validation(tmp_path: Path):
    rows = [JobRow(name="1", prompt="v")]
    with pytest.raises(ValidationError, match="image prompt"):
        JobSpec(mode=Mode.I2V, rows=rows, output_dir=tmp_path)
    with pytest.raises(ValidationError, match="avatar"):
        JobSpec(mode=Mode.I2I2V, rows=[JobRow(name="1", prompt="v", image_prompt="i")], output_dir=tmp_path)
    with pytest.raises(ValidationError, match="row names"):
        JobSpec(mode=Mode.T2V, rows=[JobRow(name="../x", prompt="v")], output_dir=tmp_path)
    static = tmp_path / "s.jpg"
    static.write_bytes(b"x")
    assert JobSpec(mode=Mode.I2V, rows=rows, output_dir=tmp_path, static_image=static)


def test_static_image_items_point_to_the_picture(tmp_path: Path):
    static = tmp_path / "s.jpg"
    static.write_bytes(b"x")
    spec = JobSpec(mode=Mode.I2V, rows=[JobRow(name="1", prompt="v")], output_dir=tmp_path / "out", static_image=static)

    [item] = JobOutput(spec).work_items(spec.rows)

    assert item.image_path == static and item.image_prompt is None and not item.needs_image


def test_repository_proxies(tmp_path: Path):
    repo = AccountRepository(tmp_path)
    acc = repo.create("123")
    added, rejected = repo.add_proxies(acc.name, ["1.2.3.4:80:u:p", "bad"])
    (acc.directory / "dead_proxies.txt").write_text("1.2.3.4:80")

    assert acc.name == "account_1" and repo.create("456").name == "account_2"
    assert [p.key for p in added] == ["1.2.3.4:80"] and rejected == ["bad"]
    assert repo.get(acc.name).proxies[0].url == "http://u:p@1.2.3.4:80"
    repo.revive_proxy(acc.name, "1.2.3.4:80")
    assert repo.dead_proxies(acc.name) == set()


def _runner_with(tmp_path: Path, monkeypatch, sessions: list[FakeSession]) -> JobRunner:
    repo = AccountRepository(tmp_path / "accounts")
    repo.create("1")
    runner = JobRunner(Settings(_env_file=None), repo)
    monkeypatch.setattr(runner, "_make_sessions", lambda accounts, strategy: sessions)
    return runner


async def test_runner_completes_and_resumes(tmp_path: Path, monkeypatch):
    session = FakeSession("a", tmp_path / "tmp")
    runner = _runner_with(tmp_path, monkeypatch, [session])
    spec = JobSpec(mode=Mode.FLASH_T2V, rows=[JobRow(name=str(i), prompt=f"p{i}") for i in range(3)], output_dir=tmp_path / "out")

    state, progress, _ = await runner.run(spec)
    assert state is JobState.DONE and progress.videos_done == 3 and progress.remaining == 0
    assert set(session.model_keys) == {"abra_t2v_10s"}

    state, progress, _ = await runner.run(spec)
    assert state is JobState.DONE and progress.pass_num == 0
    assert len(session.videos_made) == 3


async def test_runner_stops_when_no_account_can_progress(tmp_path: Path, monkeypatch):
    session = FakeSession("a", tmp_path / "tmp", videos=False)
    runner = _runner_with(tmp_path, monkeypatch, [session])
    spec = JobSpec(mode=Mode.T2V, rows=[JobRow(name="1", prompt="p")], output_dir=tmp_path / "out")

    state, progress, message = await runner.run(spec)

    assert state is JobState.STOPPED and progress.remaining == 1 and "no progress" in message


async def test_f2v_chain_feeds_last_good_frame(tmp_path: Path, monkeypatch):
    from src.engine import chain

    monkeypatch.setattr(chain, "extract_last_frame", lambda video, frame: frame.write_text(video.name))
    session = FakeSession("a", tmp_path / "tmp", filtered={"p1"})
    runner = _runner_with(tmp_path, monkeypatch, [session])
    spec = JobSpec(mode=Mode.F2V, rows=[JobRow(name=f"c{i}", prompt=f"p{i}") for i in range(3)], output_dir=tmp_path / "out")

    state, progress, _ = await runner.run(spec)

    assert state is JobState.DONE and progress.videos_done == 2 and progress.skipped == 1
    assert session.sources[0] is None
    assert session.sources[1] == tmp_path / "out" / "frames" / "c0.jpg"
    assert session.model_keys[0] == "abra_t2v_10s" and session.model_keys[1] is None


async def _queue(tmp_path: Path, runner: JobRunner):
    from src.jobs.queue import JobQueue
    from src.jobs.repository import JobRepository

    repo = JobRepository(f"sqlite+aiosqlite:///{tmp_path / 'db' / 'jobs.db'}")
    await repo.init()
    store = LocalObjectStore(Settings(_env_file=None, storage_dir=tmp_path / "bucket"))
    return JobQueue(runner, repo, store, tmp_path / "work"), repo, store


def _keys(store: LocalObjectStore) -> set[str]:
    return {p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()}


async def _until_finished(queue, job_id: str) -> JobStatus:
    import asyncio

    for _ in range(200):
        status = await queue.get(job_id)
        if status.state.finished:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {status}")


async def test_queue_runs_stores_results_and_cancels(tmp_path: Path, monkeypatch):
    session = FakeSession("a", tmp_path / "tmp")
    queue, repo, store = await _queue(tmp_path, _runner_with(tmp_path, monkeypatch, [session]))
    await queue.start()
    rows = [JobRow(name="1", prompt="p", image_prompt="i")]
    first = await queue.submit(JobRequest(mode=Mode.I2V, rows=rows))
    second = await queue.submit(JobRequest(mode=Mode.T2V, rows=[JobRow(name="1", prompt="p")]))
    assert await queue.cancel(second.id)

    status = await _until_finished(queue, first.id)
    await queue.stop()

    assert status.state is JobState.DONE and status.progress.images_done == 1 and status.progress.videos_done == 1
    assert (await queue.get(second.id)).state is JobState.CANCELLED
    assert _keys(store) == {f"jobs/{first.id}/1.jpg", f"jobs/{first.id}/1.mp4"}
    assert not (tmp_path / "work" / first.id).exists()
    await repo.close()


async def test_retry_restores_images_and_runs_only_missing_rows(tmp_path: Path, monkeypatch):
    import asyncio

    session = FakeSession("a", tmp_path / "tmp", videos=False)
    runner = _runner_with(tmp_path, monkeypatch, [session])
    queue, repo, _ = await _queue(tmp_path, runner)
    await queue.start()
    rows = [JobRow(name=str(i), prompt=f"p{i}", image_prompt=f"i{i}") for i in range(2)]
    job = await queue.submit(JobRequest(mode=Mode.I2V, rows=rows))
    for _ in range(200):
        if len(session.images_made) == 2:
            break
        await asyncio.sleep(0.02)
    assert await queue.cancel(job.id)

    status = await _until_finished(queue, job.id)
    assert status.state is JobState.CANCELLED and status.progress.images_done == 2 and status.progress.videos_done == 0
    assert not (tmp_path / "work" / job.id).exists()

    session._videos = True
    assert await queue.retry(job.id)
    status = await _until_finished(queue, job.id)
    await queue.stop()

    assert status.state is JobState.DONE and status.progress.videos_done == 2
    assert session.images_made == ["i0", "i1"]
    assert sorted(p.name for p in session.sources) == ["0.jpg", "1.jpg"]
    await repo.close()


async def test_queue_requeues_job_interrupted_by_stop(tmp_path: Path, monkeypatch):
    import asyncio

    session = FakeSession("a", tmp_path / "tmp")
    runner = _runner_with(tmp_path, monkeypatch, [session])
    started = asyncio.Event()

    async def slow_run(spec, on_progress=None, output=None):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(runner, "run", slow_run)
    queue, repo, _ = await _queue(tmp_path, runner)
    await queue.start()
    job = await queue.submit(JobRequest(mode=Mode.T2V, rows=[JobRow(name="1", prompt="p")]))
    await asyncio.wait_for(started.wait(), 2)
    await queue.stop()

    assert (await queue.get(job.id)).state is JobState.QUEUED
    await repo.close()


def test_api_flow(tmp_path: Path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from src.api.app import create_app

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db' / 'jobs.db'}",
        work_dir=tmp_path / "work",
        api_token="secret",
        storage_dir=tmp_path / "bucket",
    )
    store = LocalObjectStore(settings)
    runner = _runner_with(tmp_path, monkeypatch, [FakeSession("a", tmp_path / "tmp")])
    auth = {"Authorization": "Bearer secret"}

    with TestClient(create_app(settings, objects=store, runner=runner)) as client:
        assert client.get("/api/jobs").status_code == 401
        assert {m["id"] for m in client.get("/api/modes", headers=auth).json()} == set(Mode)

        upload = client.post("/api/uploads", headers=auth, files={"file": ("face.png", b"png")}).json()
        bad = client.post("/api/jobs", headers=auth, json={"mode": "i2i2v", "rows": [{"name": "1", "prompt": "p", "image_prompt": "i"}]})
        assert bad.status_code == 422

        job = client.post(
            "/api/jobs",
            headers=auth,
            json={"mode": "i2i2v", "title": "test", "avatar": upload["key"], "rows": [{"name": "1", "prompt": "p", "image_prompt": "i"}]},
        ).json()
        for _ in range(200):
            detail = client.get(f"/api/jobs/{job['id']}", headers=auth).json()
            if detail["state"] == "done":
                break
            time.sleep(0.02)

        assert detail["state"] == "done" and detail["rows"][0]["name"] == "1"
        files = client.get(f"/api/jobs/{job['id']}/files", headers=auth).json()
        assert {f["name"] for f in files} == {"1.jpg", "1.mp4"}
        video = next(f for f in files if f["name"] == "1.mp4")
        assert video["url"] == f"http://127.0.0.1:8000/files/jobs/{job['id']}/1.mp4"
        path = video["url"].removeprefix("http://127.0.0.1:8000")
        assert client.get(path, headers={"Range": "bytes=0-1"}).status_code == 206
        assert "attachment" in client.get(video["download_url"].removeprefix("http://127.0.0.1:8000")).headers["content-disposition"]
        assert client.get("/files/../pyproject.toml").status_code == 404
        assert client.post(f"/api/jobs/{job['id']}/retry", headers=auth).status_code == 409
        assert client.delete(f"/api/jobs/{job['id']}", headers=auth).status_code == 204
        assert client.get(f"/api/jobs/{job['id']}", headers=auth).status_code == 404
        assert not any(k.startswith("jobs/") for k in _keys(store))
