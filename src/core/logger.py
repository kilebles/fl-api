import sys
import threading
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger


def setup_monitor(url: str) -> None:
    """Add a loguru sink that forwards log records to flow-stat /ingest."""
    def _sink(message) -> None:
        record = message.record
        extra = record["extra"]

        def _send() -> None:
            try:
                httpx.post(
                    f"{url}/ingest",
                    json={
                        "ts": record["time"].isoformat(),
                        "level": record["level"].name,
                        "module": record["name"],
                        "function": record["function"],
                        "message": record["message"],
                        "event_type": extra.get("event_type"),
                        "account": extra.get("account"),
                        "proxy": extra.get("proxy"),
                    },
                    timeout=2.0,
                )
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()

    logger.add(_sink, level="INFO")


def setup_logger(log_level: str = "WARNING") -> None:
    """
    Configure loguru to write logs to both console and file.

    Each run creates a new log file in logs/ directory with timestamp.
    """
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = logs_dir / f"{timestamp}.log"

    logger.remove()

    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True,
    )

    logger.add(
        log_file,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        level=log_level,
        encoding="utf-8",
    )
