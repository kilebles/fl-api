from pathlib import Path

import pytest

from src.engine import scheduler
from src.engine.scheduler import BatchOptions, BatchScheduler, WorkItem
from tests.fakes import FakeSession


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(scheduler, "PICK_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(scheduler, "INTER_GENERATION_PAUSE", (0.0, 0.0))


async def test_t2v_spreads_over_workers(tmp_path: Path):
    a, b = FakeSession("a", tmp_path / "tmp"), FakeSession("b", tmp_path / "tmp")
    out = tmp_path / "out"
    items = [WorkItem(index=i, name=str(i), video_prompt=f"p{i}") for i in range(6)]

    stats = await BatchScheduler([a, b], BatchOptions(output_dir=out, video_model_key="m")).run(items)

    assert stats.done == 6
    assert sorted(p.name for p in out.glob("*.mp4")) == sorted(f"{i}.mp4" for i in range(6))
    assert a.videos_made and b.videos_made
    assert set(a.model_keys + b.model_keys) == {"m"}


async def test_i2v_video_denied_account_feeds_images(tmp_path: Path):
    images_only = FakeSession("images_only", tmp_path / "tmp", videos=False)
    both = FakeSession("both", tmp_path / "tmp")
    out = tmp_path / "out"
    items = [
        WorkItem(index=i, name=str(i), video_prompt=f"v{i}", image_prompt=f"i{i}", image_path=out / f"{i}.jpg")
        for i in range(5)
    ]

    stats = await BatchScheduler([images_only, both], BatchOptions(output_dir=out)).run(items)

    assert stats.done == 5 and stats.images_done == 5
    assert not images_only.videos_made
    assert len(both.videos_made) == 5
    assert all(src.suffix == ".jpg" for src in both.sources)


async def test_image_quota_moves_images_to_other_account(tmp_path: Path):
    quota = FakeSession("quota", tmp_path / "tmp", image_quota_after=1)
    other = FakeSession("other", tmp_path / "tmp")
    out = tmp_path / "out"
    items = [WorkItem(index=i, name=str(i), image_prompt=f"i{i}", image_path=out / f"{i}.jpg") for i in range(4)]

    stats = await BatchScheduler([quota, other], BatchOptions(output_dir=out)).run(items)

    assert stats.done == 4
    assert len(quota.images_made) <= 1
    assert len(quota.images_made) + len(other.images_made) == 4


async def test_filtered_rows_get_skip_marker(tmp_path: Path):
    session = FakeSession("a", tmp_path / "tmp", filtered={"bad"})
    out = tmp_path / "out"
    items = [
        WorkItem(index=0, name="0", video_prompt="good"),
        WorkItem(index=1, name="1", video_prompt="bad"),
    ]

    stats = await BatchScheduler([session], BatchOptions(output_dir=out)).run(items)

    assert stats.done == 1 and stats.skipped == 1
    assert (out / "0.mp4").exists()
    assert (out / "1.skip").read_text().startswith("filtered")


async def test_avatar_is_passed_to_every_image(tmp_path: Path):
    avatar = tmp_path / "face.png"
    avatar.write_bytes(b"x")
    session = FakeSession("a", tmp_path / "tmp")
    out = tmp_path / "out"
    items = [WorkItem(index=i, name=str(i), video_prompt="v", image_prompt="i", image_path=out / f"{i}.jpg") for i in range(2)]

    await BatchScheduler([session], BatchOptions(output_dir=out, reference_image=avatar)).run(items)

    assert all((out / f"{i}.jpg").read_text().endswith("ref=face.png") for i in range(2))


async def test_worker_without_capability_does_not_start(tmp_path: Path):
    idle = FakeSession("idle", tmp_path / "tmp", videos=False, images=False)
    busy = FakeSession("busy", tmp_path / "tmp")
    items = [WorkItem(index=0, name="0", video_prompt="v")]

    await BatchScheduler([idle, busy], BatchOptions(output_dir=tmp_path / "out")).run(items)

    assert not idle.started and busy.started


async def test_ip_input_image_regenerates_the_source_image_once(tmp_path: Path):
    session = FakeSession("a", tmp_path / "tmp", ip_rejections={"v0": 1, "v1": 2})
    out = tmp_path / "out"
    items = [
        WorkItem(index=i, name=str(i), video_prompt=f"v{i}", image_prompt=f"i{i}", image_path=out / f"{i}.jpg")
        for i in range(2)
    ]

    stats = await BatchScheduler([session], BatchOptions(output_dir=out)).run(items)

    assert stats.done == 1 and stats.skipped == 1
    assert session.images_made.count("i0") == 2 and session.images_made.count("i1") == 2
    assert (out / "0.mp4").exists() and "IP_INPUT_IMAGE" in (out / "1.skip").read_text()


async def test_ip_input_image_on_a_static_image_skips_at_once(tmp_path: Path):
    static = tmp_path / "s.jpg"
    static.write_text("x")
    session = FakeSession("a", tmp_path / "tmp", ip_rejections={"v0": 1})
    out = tmp_path / "out"

    stats = await BatchScheduler([session], BatchOptions(output_dir=out)).run([WorkItem(index=0, name="0", video_prompt="v0", image_path=static)])

    assert stats.skipped == 1 and static.exists() and not session.images_made
