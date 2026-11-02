"""Dolphin Anty Local API (the desktop app on localhost): start/stop browser profiles."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from loguru import logger

# Dolphin Local API cannot handle concurrent profile launches and answers 500 when hit
# simultaneously, so every start/stop goes through this lock.
_LOCAL_LOCK = threading.Lock()
_DOLPHIN_EXE = Path(r"C:\Program Files\Dolphin Anty\Dolphin Anty.exe")
_DOLPHIN_TASK = "start-dolphin-anty"


class DolphinNotRunningError(RuntimeError):
    """The Dolphin Anty desktop app does not answer on its Local API port."""


class DolphinLocalApi:
    """Starts and stops browser profiles through the Dolphin Anty desktop app."""

    def __init__(
        self,
        base_url: str = "http://localhost:3001",
        *,
        auto_recover: bool | None = None,
        recover_timeout: float = 35.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auto_recover = (sys.platform == "win32") if auto_recover is None else auto_recover
        self._recover_timeout = recover_timeout

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

    def _launch_dolphin_app(self) -> None:
        launched = False
        try:
            res = subprocess.run(
                ["schtasks", "/Run", "/TN", _DOLPHIN_TASK],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            launched = res.returncode == 0
            if not launched:
                logger.warning(f"schtasks /Run {_DOLPHIN_TASK} returned {res.returncode}: {res.stderr.strip()}")
        except Exception as e:
            logger.warning(f"Could not invoke schtasks for {_DOLPHIN_TASK}: {e}")

        if not launched and _DOLPHIN_EXE.is_file():
            try:
                subprocess.Popen(
                    [str(_DOLPHIN_EXE)],
                    cwd=str(_DOLPHIN_EXE.parent),
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
                )
            except Exception as e:
                logger.warning(f"Direct launch of {_DOLPHIN_EXE} failed: {e}")

    def _wait_local_api(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                httpx.get(f"{self._base_url}/v1.0/browser_profiles", timeout=5.0)
                time.sleep(3.0)
                logger.info("Dolphin Anty Local API recovered and is listening")
                return True
            except httpx.ConnectError:
                continue
            except Exception:
                time.sleep(3.0)
                logger.info("Dolphin Anty Local API recovered and is listening")
                return True
        return False

    def _recover_dolphin(self) -> bool:
        """Attempt to relaunch Dolphin Anty without killing live browsers unless required."""
        logger.warning("Dolphin Anty Local API is unreachable; attempting soft recovery...")
        self._launch_dolphin_app()
        soft_wait = min(15.0, self._recover_timeout)
        if self._wait_local_api(soft_wait):
            return True

        logger.warning("Soft recovery did not restore Local API; killing orphaned anty.exe and retrying...")
        try:
            subprocess.run(["taskkill", "/F", "/IM", "anty.exe"], capture_output=True, timeout=15, check=False)
        except Exception as e:
            logger.warning(f"Could not run taskkill on anty.exe: {e}")
        self._launch_dolphin_app()
        if self._wait_local_api(max(5.0, self._recover_timeout - soft_wait)):
            return True

        logger.error(f"Dolphin Anty Local API did not recover within {self._recover_timeout:.0f}s")
        return False

    def start(self, profile_id: str) -> str:
        """Start a profile with automation enabled and return its CDP WebSocket endpoint."""
        url = f"{self._base_url}/v1.0/browser_profiles/{profile_id}/start"
        stop_url = f"{self._base_url}/v1.0/browser_profiles/{profile_id}/stop"
        with _LOCAL_LOCK:
            try:
                resp = httpx.post(url, json={"automation": True}, timeout=60.0)
            except httpx.ConnectError:
                time.sleep(3.0)
                try:
                    resp = httpx.post(url, json={"automation": True}, timeout=60.0)
                except httpx.ConnectError as e:
                    if not self._auto_recover or not self._recover_dolphin():
                        raise DolphinNotRunningError("Dolphin Anty is not running — start the app and retry") from e
                    try:
                        resp = httpx.post(url, json={"automation": True}, timeout=60.0)
                    except httpx.ConnectError as retry_err:
                        raise DolphinNotRunningError(
                            "Dolphin Anty is not running — auto-recovery could not reach Local API"
                        ) from retry_err
            if resp.status_code == 409:
                logger.warning(f"Profile {profile_id} already running (409 Conflict) — stopping and restarting")
                try:
                    httpx.get(stop_url, timeout=15.0)
                    time.sleep(6.0)
                except Exception as stop_err:
                    logger.warning(f"Could not stop conflicting profile {profile_id}: {stop_err}")
                resp = httpx.post(url, json={"automation": True}, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            port: int = data["automation"]["port"]
            path: str = data["automation"]["wsEndpoint"]
            ws_endpoint = f"ws://127.0.0.1:{port}{path}"
            logger.info(f"Started profile {profile_id}: {ws_endpoint}")
            return ws_endpoint
