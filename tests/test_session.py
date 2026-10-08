import asyncio
from pathlib import Path

import httpx
import pytest

from src.accounts.account import AccountInfo
from src.core.errors import ConsecutiveFailuresError, GenerationFilteredError, ImageQuotaError, RateLimitError
from src.core.settings import Settings
from src.dolphin import cooldowns
from src.engine import session as session_module
from src.engine.session import AccountSession
from src.flow.models import MediaInfo, VideoGenerationResponse


class FakeProducer:
    def __init__(self, settings, cdp_url=None, proxy_url=None, project_id=None) -> None:
        self.cdp_url = cdp_url
        self.on_authorization = None
        self.on_cookies = None
        self.failures = 0

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def wait_for_cookies(self, timeout: float = 0) -> dict:
        return {"c": "1"}

    async def wait_for_project_id(self, timeout: float = 0) -> str:
        return "proj"

    async def wait_for_authorization(self, timeout: float = 0) -> str:
        return "Bearer t"

    async def wait_for_browser_headers(self, timeout: float = 0) -> dict:
        return {}

    async def get_token(self, action=None) -> str:
        return "tok"

    async def reauthenticate(self) -> str:
        return "Bearer t2"

    def report_failure(self) -> None:
        self.failures += 1

    def report_success(self) -> None:
        pass

    def pause(self, resume_in=None) -> None:
        pass

    def resume(self) -> None:
        pass


class FakeManager:
    def __init__(self, accounts: list[AccountInfo]) -> None:
        self.accounts = accounts
        self.idx = 0
        self.current_ws = None
        self.rotations = 0
        self.current_proxy_url = None
        self.current_proxy_key = "no-proxy"

    @property
    def count(self) -> int:
        return len(self.accounts)

    @property
    def current(self) -> AccountInfo:
        return self.accounts[self.idx]

    def start_current(self) -> str:
        self.current_ws = f"ws-{self.current.name}"
        return self.current_ws

    def rotate(self):
        self.rotations += 1
        self.idx = (self.idx + 1) % len(self.accounts)
        return self.current, self.start_current()

    def forget_browser(self) -> None:
        self.current_ws = None

    def mark_current_on_cooldown(self, max_seconds: float = 7200.0) -> float:
        return cooldowns.mark_cooldown(self.current.dolphin_id, self.current.name, max_seconds)

    def reset_cooldown(self, acc: AccountInfo) -> None:
        cooldowns.reset_cooldown(acc.dolphin_id)

    def pop_was_reset(self) -> bool:
        return False


def _error(status: int, reason: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://aisandbox-pa.googleapis.com/v1/x")
    body = {"error": {"details": [{"reason": reason}]}} if reason else {}
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(status, json=body, request=request))


class FakeVideo:
    def __init__(self, errors: list[httpx.HTTPStatusError]) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def generate(self, **kwargs) -> VideoGenerationResponse:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return VideoGenerationResponse(media=[MediaInfo(media_id="m", project_id="proj", workflow_id="", status="")])


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(session_module, "RecaptchaTokenProducer", FakeProducer)
    monkeypatch.setattr(session_module, "DELAY_403", (0.0, 0.0))
    monkeypatch.setattr(session_module, "DELAY_403_REPEAT", (0.0, 0.0))
    monkeypatch.setattr(session_module, "DELAY_429_STEPS", (0.0, 0.0, 0.0))
    for registry in (cooldowns._COOLDOWNS, cooldowns._COOLDOWN_HITS, cooldowns._IMAGE_COOLDOWNS, cooldowns._IMAGE_COOLDOWN_HITS):
        registry.clear()


def _accounts(tmp_path: Path, n: int) -> list[AccountInfo]:
    accounts = []
    for i in range(n):
        directory = tmp_path / f"account_{i}"
        directory.mkdir()
        accounts.append(AccountInfo(directory=directory, dolphin_id=f"d{i}"))
    return accounts


async def _session(tmp_path: Path, n: int, errors: list) -> tuple[AccountSession, FakeManager, FakeVideo]:
    manager = FakeManager(_accounts(tmp_path, n))
    session = AccountSession(Settings(_env_file=None), manager)
    video = FakeVideo(errors)
    session._video = video
    await session.ensure_started()
    return session, manager, video


async def test_403_backs_off_then_succeeds(tmp_path: Path):
    session, manager, video = await _session(tmp_path, 2, [_error(403), _error(403)])

    submitted = await session.submit_video(0, "p")

    assert submitted.media_id == "m" and video.calls == 3 and manager.rotations == 0
    await session.close()


async def test_third_403_rotates_to_next_account(tmp_path: Path):
    session, manager, _ = await _session(tmp_path, 2, [_error(403)] * 3)

    with pytest.raises(ConsecutiveFailuresError):
        await session.submit_video(0, "p")

    assert manager.rotations == 1 and session.name == "account_1"
    assert (tmp_path / "account_0" / "project_id.txt").read_text().strip() == "proj"
    await session.close()


async def test_second_403_rotation_on_same_proxy_cools_the_account_down(tmp_path: Path):
    session, manager, _ = await _session(tmp_path, 1, [_error(403)] * 6)

    for _ in range(2):
        with pytest.raises(ConsecutiveFailuresError):
            await session.submit_video(0, "p")

    assert session.on_cooldown and manager.rotations == 1
    with pytest.raises(RateLimitError):
        await session.submit_video(0, "p")
    await session.close()


async def test_400_is_a_permanent_skip(tmp_path: Path):
    session, _, _ = await _session(tmp_path, 1, [_error(400, "PUBLIC_ERROR_SOMETHING")])

    with pytest.raises(GenerationFilteredError):
        await session.submit_video(0, "p")
    await session.close()


async def test_image_quota_pauses_only_images(tmp_path: Path):
    session, manager, _ = await _session(tmp_path, 1, [])

    class QuotaImage:
        async def generate(self, **kwargs):
            raise _error(429, "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED")

    session._image = QuotaImage()
    with pytest.raises(ImageQuotaError):
        await session.make_image(0, "p", tmp_path / "x.jpg")

    assert not session.can_make_images() and session.can_make_videos() and manager.rotations == 0
    await session.close()


async def test_401_signs_in_again_without_rotation(tmp_path: Path):
    session, manager, video = await _session(tmp_path, 2, [_error(401)])

    await session.submit_video(0, "p")

    assert video.calls == 2 and manager.rotations == 0
    await session.close()
