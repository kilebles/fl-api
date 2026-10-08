from pathlib import Path

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from src.accounts.repository import AccountRepository
from src.core.settings import Settings
from src.engine import scheduler
from src.jobs.excel import load_rows
from src.jobs.models import JobRow, JobSpec, JobState
from src.jobs.modes import Mode
from src.jobs.runner import JobRunner
from src.jobs.storage import JobOutput
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


async def test_queue_runs_and_cancels(tmp_path: Path, monkeypatch):
    import asyncio

    from src.jobs.queue import JobQueue

    session = FakeSession("a", tmp_path / "tmp")
    runner = _runner_with(tmp_path, monkeypatch, [session])
    queue = JobQueue(runner)
    queue.start()
    first = queue.submit(JobSpec(mode=Mode.T2V, rows=[JobRow(name="1", prompt="p")], output_dir=tmp_path / "a"))
    second = queue.submit(JobSpec(mode=Mode.T2V, rows=[JobRow(name="1", prompt="p")], output_dir=tmp_path / "b"))
    assert queue.cancel(second.id)

    for _ in range(100):
        if queue.get(first.id).state.finished:
            break
        await asyncio.sleep(0.02)
    await queue.stop()

    assert queue.get(first.id).state is JobState.DONE
    assert queue.get(second.id).state is JobState.CANCELLED
    assert not (tmp_path / "b").exists()
