"""Dolphin Anty Local API (the desktop app on localhost): start/stop browser profiles."""

import threading
import time

import httpx
from loguru import logger

# Dolphin Local API cannot handle concurrent profile launches and answers 500 when hit
# simultaneously, so every start/stop goes through this lock.
_LOCAL_LOCK = threading.Lock()


class DolphinNotRunningError(RuntimeError):
    """The Dolphin Anty desktop app does not answer on its Local API port."""


class DolphinLocalApi:
    """Starts and stops browser profiles through the Dolphin Anty desktop app."""

    def __init__(self, base_url: str = "http://localhost:3001") -> None:
        self._base_url = base_url.rstrip("/")

    def stop(self, profile_id: str) -> None:
        """Stop a profile; failures are logged, never raised (the profile may not be running)."""
        with _LOCAL_LOCK:
            try:
                resp = httpx.get(f"{self._base_url}/v1.0/browser_profiles/{profile_id}/stop", timeout=15.0)
                resp.raise_for_status()
                logger.debug(f"Stopped Dolphin Anty profile {profile_id}")
                time.sleep(6)
            except httpx.ConnectError:
                logger.warning(f"Could not stop profile {profile_id}: Dolphin Anty not running")
            except Exception as e:
                logger.warning(f"Could not stop profile {profile_id}: {e}")

    def start(self, profile_id: str) -> str:
        """Start a profile with automation enabled and return its CDP WebSocket endpoint."""
        with _LOCAL_LOCK:
            try:
                resp = httpx.post(
                    f"{self._base_url}/v1.0/browser_profiles/{profile_id}/start",
                    json={"automation": True},
                    timeout=60.0,
                )
            except httpx.ConnectError as e:
                raise DolphinNotRunningError("Dolphin Anty is not running — start the app and retry") from e
            resp.raise_for_status()
            data = resp.json()
            port: int = data["automation"]["port"]
            path: str = data["automation"]["wsEndpoint"]
            ws_endpoint = f"ws://127.0.0.1:{port}{path}"
            logger.info(f"Started profile {profile_id}: {ws_endpoint}")
            return ws_endpoint
