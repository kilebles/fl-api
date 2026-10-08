"""
Smoke-test for the flow.google.com browser session.

Connects to a Dolphin Anty profile, starts RecaptchaTokenProducer and reports
how the current Flow frontend behaves: which project was obtained and how,
which Authorization scheme the page uses against aisandbox-pa.googleapis.com,
which API paths / frontend RPC ids were observed, and whether a reCAPTCHA
token can be produced. With --generate it also sends one real text-to-video
request, polls it and downloads the result.

Usage:
    uv run python scripts/check_session.py [account_name] [--generate] [--prompt "..."]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.core.logger import setup_logger
from src.core.settings import get_settings
from src.accounts.account import AccountInfo
from src.accounts.repository import AccountRepository
from src.flow.credentials import CredentialsService
from src.dolphin.local_api import DolphinLocalApi
from src.dolphin.manager import DolphinAntyManager
from src.dolphin.public_api import DolphinPublicApi
from src.flow.downloader import VideoDownloaderService
from src.flow.google_auth import auth_scheme
from src.flow.polling import PollingService
from src.flow.browser.recaptcha import RecaptchaTokenProducer
from src.flow.video import VideoGenerationService



def _load_account(name: str | None) -> AccountInfo:
    repo = AccountRepository(get_settings().accounts_dir)
    accounts = repo.active()
    if name:
        matches = [a for a in accounts if a.name == name]
        if not matches:
            logger.error(f"Account {name} not found in {repo.root}")
            sys.exit(1)
        return matches[0]
    if not accounts:
        logger.error(f"No accounts with dolphin_id found in {repo.root}")
        sys.exit(1)
    return accounts[0]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("account", nargs="?", help="account directory name, e.g. account_1")
    parser.add_argument("--generate", action="store_true", help="send one real generation and download it")
    parser.add_argument("--prompt", default="A calm mountain lake at sunrise, gentle mist over the water")
    args = parser.parse_args()

    setup_logger("DEBUG")
    settings = get_settings()
    account = _load_account(args.account)
    logger.info(f"Using {account.name} (dolphin_id={account.dolphin_id}, persisted project={account.project_id})")

    dolphin_manager = DolphinAntyManager([account], DolphinPublicApi(settings.anty_token), DolphinLocalApi(settings.anty_local_api))
    ws_url = await asyncio.to_thread(dolphin_manager.start_current)
    logger.info(f"Dolphin profile started @ {ws_url}")

    credentials = CredentialsService(origin=settings.flow_base_url)
    credentials.reload(account)
    producer = RecaptchaTokenProducer(
        settings,
        cdp_url=ws_url,
        proxy_url=dolphin_manager.current_proxy_url,
        project_id=account.project_id,
    )
    producer_task = asyncio.create_task(producer.run())

    try:
        cookies = await producer.wait_for_cookies(timeout=120.0)
        credentials.set_cookies(cookies)
        logger.success(f"Cookies: {len(cookies)} (labs session: {'__Secure-next-auth.session-token' in cookies})")

        project_id = await producer.wait_for_project_id(timeout=180.0)
        credentials.set_project_id(project_id)
        account.save_project_id(project_id)
        logger.success(f"Project: {project_id} -> {settings.flow_base_url}/project/{project_id}")

        authorization = await producer.wait_for_authorization(timeout=90.0)
        credentials.set_authorization(authorization)
        logger.success(f"Authorization scheme: {auth_scheme(authorization)} (len={len(authorization)})")

        browser_headers = await producer.wait_for_browser_headers(timeout=60.0)
        credentials.set_browser_headers(browser_headers)
        logger.success(f"Browser headers: {sorted(browser_headers)}")

        recaptcha_token = await asyncio.wait_for(producer.get_token(), timeout=120.0)
        logger.success(f"reCAPTCHA token: {recaptcha_token[:40]}... (len={len(recaptcha_token)})")

        if args.generate:
            video = VideoGenerationService(settings)
            polling = PollingService(settings)
            downloader = VideoDownloaderService(settings)
            downloader.set_cookies(cookies)
            proxy_url = dolphin_manager.current_proxy_url
            response = await video.generate(
                prompt=args.prompt,
                authorization=credentials.authorization,
                project_id=project_id,
                recaptcha_token=recaptcha_token,
                proxy_url=proxy_url,
                browser_headers=credentials.browser_headers,
                cookies=cookies,
            )
            media_id = response.media[0].media_id
            logger.success(f"Generation accepted, media_id={media_id}")
            media = await polling.poll(
                media_id=media_id,
                project_id=project_id,
                authorization=credentials.authorization,
                proxy_url=proxy_url,
                browser_headers=credentials.browser_headers,
                cookies=cookies,
            )
            path = await downloader.download(media.media_id, cookies=cookies, proxy_url=proxy_url, video_url=media.video_url)
            logger.success(f"Downloaded: {path}")
    except Exception as e:
        logger.error(f"Session check failed: {type(e).__name__}: {e}")
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("Dolphin profile left running (the next job reuses a running browser)")


if __name__ == "__main__":
    asyncio.run(main())
