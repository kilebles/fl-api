"""
Run the web API.

Usage:
    uv run python scripts/serve.py [--host 0.0.0.0] [--port 8000]

Uvicorn's own loop setup is turned off: on Windows it would switch to the selector
loop, and Playwright needs the default proactor loop to start its driver.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from loguru import logger

from src.api.app import create_app
from src.core.logger import setup_logger, setup_monitor
from src.core.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fl-api web API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings = get_settings()
    setup_logger(settings.log_level)
    if settings.monitor_url:
        setup_monitor(settings.monitor_url)
    logger.info(f"Starting fl-api server on http://{args.host}:{args.port}")

    config = uvicorn.Config(create_app(settings), host=args.host, port=args.port, loop="none", log_level="warning")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
