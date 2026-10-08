"""
Find which image model name flowMedia:batchGenerateImages accepts.

Google retires model names without notice: the request then returns
404 "Requested entity was not found." for every account. This script starts one
Dolphin profile, gets a reCAPTCHA token per attempt and sends a one-image request
with each candidate `imageModelName`, printing the HTTP status and the reason.
Put the accepted name into `.env` as IMAGE_MODEL_NAME and restart.

Usage:
    uv run python scripts/probe_image_model.py [account_name]
    uv run python scripts/probe_image_model.py account_1 --models NARWHAL,GEM_PIX,IMAGEN_4
"""

import argparse
import asyncio
import random
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from loguru import logger

from src.core.logger import setup_logger
from src.core.settings import get_settings
from src.accounts.account import AccountInfo
from src.accounts.repository import AccountRepository
from src.flow.models import (
    ClientContext,
    ImageGenerationRequest,
    ImageRequestItem,
    MediaGenerationContext,
    RecaptchaContext,
    StructuredPrompt,
    TextPart,
)
from src.flow.credentials import CredentialsService
from src.dolphin.local_api import DolphinLocalApi
from src.dolphin.manager import DolphinAntyManager
from src.dolphin.public_api import DolphinPublicApi
from src.flow.http import api_headers
from src.flow.image import ImageGenerationService
from src.flow.browser.recaptcha import RecaptchaTokenProducer

DEFAULT_CANDIDATES = [
    "NARWHAL",
    "GEM_PIX",
    "GEMPIX",
    "GEM_PIX_2",
    "NANO_BANANA",
    "NANO_BANANA_PRO",
    "NANO_BANANA_2",
    "GEMINI_2_5_FLASH_IMAGE",
    "GEMINI_3_PRO_IMAGE",
    "GEMINI_3_FLASH_IMAGE",
    "IMAGEN_4",
    "IMAGEN_4_FAST",
    "IMAGEN_4_ULTRA",
    "NARWHAL_2",
    "BOGUS_MODEL_XYZ",
]
PROMPT = "a red apple on a wooden table, studio light"


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


async def probe(settings, credentials: CredentialsService, producer: RecaptchaTokenProducer, proxy_url: str | None, models: list[str]) -> None:
    image = ImageGenerationService(settings)
    headers = credentials.browser_headers or None
    url = image.IMAGE_API_URL_TEMPLATE.format(project_id=credentials.project_id)
    accepted: list[str] = []
    reauthenticated = False
    pending = list(models)
    while pending:
        model = pending[0]
        token = await asyncio.wait_for(producer.get_token(action="IMAGE_GENERATION"), timeout=120.0)
        context = ClientContext(
            projectId=credentials.project_id,
            sessionId=f";{int(time.time() * 1000)}",
            recaptchaContext=RecaptchaContext(token=token),
            userPaygateTier=None,
        )
        request = ImageGenerationRequest(
            clientContext=context,
            mediaGenerationContext=MediaGenerationContext(batchId=str(uuid.uuid4())),
            requests=[
                ImageRequestItem(
                    clientContext=context,
                    seed=random.randint(1, 999999),
                    imageModelName=model,
                    structuredPrompt=StructuredPrompt(parts=[TextPart(text=PROMPT)]),
                )
            ],
        )
        async with httpx.AsyncClient(proxy=proxy_url, http2=True) as client:
            response = await client.post(
                url,
                content=request.model_dump_json(exclude_none=True),
                headers=api_headers(settings.flow_base_url, credentials.authorization, headers),
                timeout=120.0,
            )
        body = re.sub(r"\s+", " ", response.text)
        if response.status_code == 401 and not reauthenticated:
            logger.warning("HTTP 401 — the labs.google session token is stale, signing in again and retrying")
            credentials.set_authorization(await producer.reauthenticate())
            reauthenticated = True
            continue
        pending.pop(0)
        if response.status_code == 200:
            names = [m.get("image", {}).get("generatedImage", {}).get("modelNameType") for m in response.json().get("media", [])]
            logger.success(f"{model}: HTTP 200 — accepted, response modelNameType={names}")
            accepted.append(model)
        else:
            reason = re.search(r'"reason":\s*"([A-Z_]+)"', body)
            message = re.search(r'"message":\s*"([^"]+)"', body)
            logger.warning(
                f"{model}: HTTP {response.status_code} "
                f"{reason.group(1) if reason else ''} {message.group(1) if message else body[:160]}"
            )
        await asyncio.sleep(2)
    if accepted:
        logger.success(f"Accepted model names: {accepted} — set IMAGE_MODEL_NAME={accepted[0]} in .env")
    else:
        logger.error("No candidate accepted — pass more names with --models")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("account", nargs="?", help="account directory name, e.g. account_1")
    parser.add_argument("--models", default=",".join(DEFAULT_CANDIDATES), help="comma-separated imageModelName candidates")
    args = parser.parse_args()

    setup_logger("INFO")
    settings = get_settings()
    account = _load_account(args.account)
    logger.info(f"Using {account.name} (dolphin_id={account.dolphin_id}); current IMAGE_MODEL_NAME={settings.image_model_name}")

    dolphin_manager = DolphinAntyManager([account], DolphinPublicApi(settings.anty_token), DolphinLocalApi(settings.anty_local_api))
    ws_url = await asyncio.to_thread(dolphin_manager.start_current)
    credentials = CredentialsService(origin=settings.flow_base_url)
    credentials.reload(account)
    producer = RecaptchaTokenProducer(
        settings, cdp_url=ws_url, proxy_url=dolphin_manager.current_proxy_url, project_id=account.project_id
    )
    producer_task = asyncio.create_task(producer.run())
    try:
        credentials.set_cookies(await producer.wait_for_cookies(timeout=120.0))
        credentials.set_project_id(await producer.wait_for_project_id(timeout=180.0))
        credentials.set_authorization(await producer.wait_for_authorization(timeout=90.0))
        credentials.set_browser_headers(await producer.wait_for_browser_headers(timeout=60.0))
        await producer.wait_for_page_ready(timeout=90.0)
        await probe(settings, credentials, producer, dolphin_manager.current_proxy_url, args.models.split(","))
    except Exception as e:
        logger.error(f"Probe failed: {type(e).__name__}: {e}")
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("Dolphin profile left running (the next job reuses a running browser)")


if __name__ == "__main__":
    asyncio.run(main())
