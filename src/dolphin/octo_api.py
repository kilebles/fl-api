"""Octo Browser Local & Cloud API adapters (drop-in alternative to Dolphin Anty)."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from loguru import logger

from src.accounts.proxy import ProxyInfo
from src.dolphin.local_api import DolphinNotRunningError

OCTO_PUBLIC_API_BASE = "https://app.octobrowser.net/api/v2/automation"
_OCTO_LOCAL_LOCK = threading.Lock()
_OCTO_TASK = "start-octo-browser"


class OctoNotRunningError(DolphinNotRunningError):
    """The Octo Browser desktop app does not answer on its Local API port."""


def _octo_exe_candidates() -> list[Path]:
    local_app = os.environ.get("LOCALAPPDATA", r"C:\Users\Administrator\AppData\Local")
    return [
        Path(r"C:\Program Files\Octo Browser\Octo Browser.exe"),
        Path(local_app) / "Programs" / "Octo Browser" / "Octo Browser.exe",
        Path(local_app) / "Octo Browser" / "Octo Browser.exe",
    ]


class OctoPublicApi:
    """Client for the Octo Browser Cloud Automation API (`X-Octo-Api-Token`)."""

    def __init__(self, octo_api_token: str) -> None:
        self._token = octo_api_token.strip()
        self._headers = {"X-Octo-Api-Token": self._token} if self._token else {}

    def get_profile_proxy(self, profile_id: str) -> ProxyInfo | None:
        if not self._token:
            return None
        resp = httpx.get(
            f"{OCTO_PUBLIC_API_BASE}/profiles/{profile_id}",
            headers=self._headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        proxy = data.get("proxy") or {}
        host = proxy.get("host")
        port = proxy.get("port")
        if not host or not port:
            return None
        return ProxyInfo(
            host=str(host),
            port=int(port),
            user=str(proxy.get("login") or ""),
            password=str(proxy.get("password") or ""),
            type=str(proxy.get("type") or "http"),
        )

    def set_profile_proxy(self, profile_id: str, proxy: ProxyInfo) -> None:
        if not self._token:
            logger.debug(f"OCTO_API_TOKEN is empty — skipping cloud proxy patch for {profile_id}")
            return
        resp = httpx.patch(
            f"{OCTO_PUBLIC_API_BASE}/profiles/{profile_id}",
            json={
                "proxy": {
                    "type": proxy.type,
                    "host": proxy.host,
                    "port": int(proxy.port),
                    "login": proxy.user,
                    "password": proxy.password,
                }
            },
            headers=self._headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        logger.debug(f"Octo proxy updated on profile {profile_id}: {proxy.host}:{proxy.port}")

    def get_profile_credentials(self, profile_id: str) -> tuple[str | None, str | None]:
        """Octo profiles do not store separate login/password fields in Automation API."""
        return None, None


class OctoLocalApi:
    """Starts and stops browser profiles through the Octo Browser desktop app (:58888)."""

    def __init__(
        self,
        base_url: str = "http://localhost:58888",
        *,
        auto_recover: bool | None = None,
        recover_timeout: float = 35.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auto_recover = (sys.platform == "win32") if auto_recover is None else auto_recover
        self._recover_timeout = recover_timeout

    def stop(self, profile_id: str) -> None:
        with _OCTO_LOCAL_LOCK:
            try:
                resp = httpx.post(
                    f"{self._base_url}/api/profiles/stop",
                    json={"uuid": profile_id},
                    timeout=15.0,
                )
                if resp.status_code >= 400:
                    httpx.post(
                        f"{self._base_url}/api/profiles/force_stop",
                        json={"uuid": profile_id},
                        timeout=15.0,
                    )
                logger.debug(f"Stopped Octo Browser profile {profile_id}")
                time.sleep(3)
            except httpx.ConnectError:
                logger.warning(f"Could not stop Octo profile {profile_id}: Octo Browser not running")
            except Exception as e:
                logger.warning(f"Could not stop Octo profile {profile_id}: {e}")

    def _launch_octo_app(self) -> None:
        launched = False
        try:
            res = subprocess.run(
                ["schtasks", "/Run", "/TN", _OCTO_TASK],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            launched = res.returncode == 0
        except Exception:
            pass

        if not launched:
            for exe in _octo_exe_candidates():
                if exe.is_file():
                    try:
                        subprocess.Popen(
                            [str(exe)],
                            cwd=str(exe.parent),
                            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
                        )
                        break
                    except Exception as e:
                        logger.warning(f"Direct launch of {exe} failed: {e}")

    def _wait_local_api(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                httpx.get(f"{self._base_url}/api/profiles/active", timeout=5.0)
                time.sleep(2.0)
                logger.info("Octo Browser Local API recovered and is listening")
                return True
            except httpx.ConnectError:
                continue
            except Exception:
                time.sleep(2.0)
                return True
        return False

    def _recover_octo(self) -> bool:
        logger.warning("Octo Browser Local API is unreachable; attempting soft recovery...")
        self._launch_octo_app()
        return self._wait_local_api(self._recover_timeout)

    @staticmethod
    def _is_already_running(resp: httpx.Response) -> bool:
        if resp.status_code in (400, 409):
            body = resp.text.lower()
            return "already" in body or "started" in body or "running" in body or resp.status_code == 409
        return False

    def start(self, profile_id: str) -> str:
        """Start an Octo profile with remote debugging enabled and return its CDP WebSocket URL."""
        url = f"{self._base_url}/api/profiles/start"
        payload = {"uuid": profile_id, "headless": False, "debug_port": True}
        with _OCTO_LOCAL_LOCK:
            try:
                resp = httpx.post(url, json=payload, timeout=60.0)
            except httpx.ConnectError:
                time.sleep(3.0)
                try:
                    resp = httpx.post(url, json=payload, timeout=60.0)
                except httpx.ConnectError as e:
                    if not self._auto_recover or not self._recover_octo():
                        raise OctoNotRunningError("Octo Browser is not running — start the app and retry") from e
                    try:
                        resp = httpx.post(url, json=payload, timeout=60.0)
                    except httpx.ConnectError as retry_err:
                        raise OctoNotRunningError(
                            "Octo Browser is not running — auto-recovery could not reach Local API"
                        ) from retry_err

            if self._is_already_running(resp):
                logger.warning(f"Octo profile {profile_id} already running — force-stopping and restarting")
                try:
                    httpx.post(f"{self._base_url}/api/profiles/force_stop", json={"uuid": profile_id}, timeout=15.0)
                    time.sleep(4.0)
                except Exception as stop_err:
                    logger.warning(f"Could not force-stop conflicting Octo profile {profile_id}: {stop_err}")
                resp = httpx.post(url, json=payload, timeout=60.0)

            resp.raise_for_status()
            data = resp.json()
            ws_endpoint = data.get("ws_endpoint") or ""
            if not ws_endpoint and data.get("debug_port"):
                port = data["debug_port"]
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=10.0).json()
                ws_endpoint = ver["webSocketDebuggerUrl"]
            if not ws_endpoint:
                raise RuntimeError(f"Octo start response missing ws_endpoint: {data}")
            logger.info(f"Started Octo profile {profile_id}: {ws_endpoint}")
            return ws_endpoint
