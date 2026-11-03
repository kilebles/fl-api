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
_OCTO_CHROME_FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--noerrdialogs",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--window-size=1280,800",
    "--window-position=60,40",
]


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
        self._proxy_cache: dict[str, tuple[str, str, int, str, str]] = {}

    @staticmethod
    def _proxy_sig(proxy: ProxyInfo) -> tuple[str, str, int, str, str]:
        return (proxy.type, proxy.host, int(proxy.port), proxy.user, proxy.password)

    def _request(self, method: str, path: str, *, attempts: int = 3, timeout: float = 25.0, **kwargs) -> httpx.Response:
        url = f"{OCTO_PUBLIC_API_BASE}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = httpx.request(method, url, headers=self._headers, timeout=timeout, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == attempts:
                    raise
                delay = 2.0 * attempt
                logger.warning(f"Octo Cloud API {method} {path} network error ({exc}) — retrying in {delay:.0f}s")
                time.sleep(delay)
                continue

            if resp.status_code == 429 and attempt < attempts:
                retry_after = min(float(resp.headers.get("Retry-After") or 10.0), 60.0)
                logger.warning(f"Octo Cloud API 429 on {method} {path} — sleeping {retry_after:.1f}s")
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500 and attempt < attempts:
                delay = 2.5 * attempt
                logger.warning(f"Octo Cloud API {resp.status_code} on {method} {path} — retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            return resp
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Octo Cloud API {method} {path} exhausted retries")

    def get_profile_proxy(self, profile_id: str) -> ProxyInfo | None:
        if not self._token:
            return None
        resp = self._request("GET", f"/profiles/{profile_id}")
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        proxy = data.get("proxy") or {}
        host = proxy.get("host")
        port = proxy.get("port")
        if not host or not port:
            return None
        info = ProxyInfo(
            host=str(host),
            port=int(port),
            user=str(proxy.get("login") or ""),
            password=str(proxy.get("password") or ""),
            type=str(proxy.get("type") or "http"),
        )
        self._proxy_cache[profile_id] = self._proxy_sig(info)
        return info

    def set_profile_proxy(self, profile_id: str, proxy: ProxyInfo) -> None:
        if not self._token:
            logger.debug(f"OCTO_API_TOKEN is empty — skipping cloud proxy patch for {profile_id}")
            return
        target_sig = self._proxy_sig(proxy)
        if self._proxy_cache.get(profile_id) == target_sig:
            logger.debug(f"Octo proxy unchanged on {profile_id} ({proxy.key}) — skipping cloud PATCH")
            return
        if profile_id not in self._proxy_cache:
            try:
                current = self.get_profile_proxy(profile_id)
                if current is not None and self._proxy_sig(current) == target_sig:
                    logger.debug(f"Octo proxy already {proxy.key} in cloud for {profile_id} — skipping PATCH")
                    return
            except Exception as exc:
                logger.debug(f"Could not inspect current Octo proxy for {profile_id}: {exc}")

        resp = self._request(
            "PATCH",
            f"/profiles/{profile_id}",
            json={
                "proxy": {
                    "type": proxy.type,
                    "host": proxy.host,
                    "port": int(proxy.port),
                    "login": proxy.user,
                    "password": proxy.password,
                }
            },
        )
        resp.raise_for_status()
        self._proxy_cache[profile_id] = target_sig
        logger.debug(f"Octo proxy updated on profile {profile_id}: {proxy.host}:{proxy.port}")
        time.sleep(1.5)

    def force_stop_cloud(self, profile_id: str) -> None:
        """Release a stuck Cloud lock on `profile_id` (`POST /profiles/{uuid}/force_stop`)."""
        if not self._token:
            return
        resp = self._request("POST", f"/profiles/{profile_id}/force_stop", json={"version": None})
        if resp.status_code >= 400:
            logger.warning(f"Cloud force_stop for {profile_id} returned {resp.status_code}: {resp.text[:200]}")
        else:
            logger.info(f"Released cloud lock for Octo profile {profile_id}")

    def get_profile_credentials(self, profile_id: str) -> tuple[str | None, str | None]:
        """Octo profiles do not store separate login/password fields in Automation API."""
        return None, None


class OctoLocalApi:
    """Starts and stops browser profiles through the Octo Browser desktop app (:58888)."""

    def __init__(
        self,
        base_url: str = "http://localhost:58888",
        *,
        octo_api_token: str = "",
        public_api: OctoPublicApi | None = None,
        auto_recover: bool | None = None,
        recover_timeout: float = 45.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._octo_api_token = octo_api_token.strip()
        self._public_api = public_api
        self._auto_recover = (sys.platform == "win32") if auto_recover is None else auto_recover
        self._recover_timeout = recover_timeout

    def _is_active_locally(self, profile_id: str) -> bool | None:
        """Check free `GET /api/profiles/active` (0 rate-limit points) to avoid redundant stops."""
        try:
            resp = httpx.get(f"{self._base_url}/api/profiles/active", timeout=6.0)
            if resp.status_code != 200:
                return None
            items = resp.json()
            if not isinstance(items, list):
                return None
            return any(isinstance(it, dict) and it.get("uuid") == profile_id for it in items)
        except Exception:
            return None

    def stop(self, profile_id: str) -> None:
        with _OCTO_LOCAL_LOCK:
            try:
                active = self._is_active_locally(profile_id)
                if active is False:
                    logger.debug(f"Octo profile {profile_id} not active locally — skipping stop")
                    return
                resp = httpx.post(
                    f"{self._base_url}/api/profiles/stop",
                    json={"uuid": profile_id},
                    timeout=20.0,
                )
                if resp.status_code >= 400 and "not_started" not in resp.text.lower():
                    httpx.post(
                        f"{self._base_url}/api/profiles/force_stop",
                        json={"uuid": profile_id},
                        timeout=20.0,
                    )
                logger.debug(f"Stopped Octo Browser profile {profile_id}")
                time.sleep(2.5)
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
            env = os.environ.copy()
            if self._octo_api_token:
                env["OCTO_API_TOKEN"] = self._octo_api_token
            for exe in _octo_exe_candidates():
                if exe.is_file():
                    try:
                        subprocess.Popen(
                            [str(exe)],
                            cwd=str(exe.parent),
                            env=env,
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
                r = httpx.get(f"{self._base_url}/api/profiles/active", timeout=5.0)
                if r.status_code != 200:
                    continue
                if self._octo_api_token:
                    u = httpx.get(f"{self._base_url}/api/username", timeout=5.0)
                    if u.status_code != 200:
                        continue
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

    @staticmethod
    def _is_version_conflict(resp: httpx.Response) -> bool:
        if resp.status_code in (400, 409):
            body = resp.text.lower()
            return "version" in body or "consistency" in body or '"error_code":9' in body.replace(" ", "")
        return False

    def start(self, profile_id: str) -> str:
        """Start an Octo profile with remote debugging enabled and return its CDP WebSocket URL."""
        url = f"{self._base_url}/api/profiles/start"
        payload = {
            "uuid": profile_id,
            "headless": False,
            "debug_port": True,
            "timeout": 120,
            "flags": list(_OCTO_CHROME_FLAGS),
        }
        with _OCTO_LOCAL_LOCK:
            try:
                resp = httpx.post(url, json=payload, timeout=180.0)
            except httpx.ConnectError:
                time.sleep(3.0)
                try:
                    resp = httpx.post(url, json=payload, timeout=180.0)
                except httpx.ConnectError as e:
                    if not self._auto_recover or not self._recover_octo():
                        raise OctoNotRunningError("Octo Browser is not running — start the app and retry") from e
                    try:
                        resp = httpx.post(url, json=payload, timeout=180.0)
                    except httpx.ConnectError as retry_err:
                        raise OctoNotRunningError(
                            "Octo Browser is not running — auto-recovery could not reach Local API"
                        ) from retry_err

            if resp.status_code == 429:
                retry_after = min(float(resp.headers.get("Retry-After") or 12.0), 60.0)
                logger.warning(f"Octo Local API 429 starting {profile_id} — sleeping {retry_after:.1f}s and retrying")
                time.sleep(retry_after)
                resp = httpx.post(url, json=payload, timeout=180.0)

            if self._is_version_conflict(resp):
                logger.warning(f"Octo profile {profile_id} version syncing — waiting 4s and retrying start")
                time.sleep(4.0)
                resp = httpx.post(url, json=payload, timeout=180.0)

            if self._is_already_running(resp):
                logger.warning(f"Octo profile {profile_id} already running — force-stopping locally and restarting")
                try:
                    httpx.post(f"{self._base_url}/api/profiles/force_stop", json={"uuid": profile_id}, timeout=20.0)
                    time.sleep(4.0)
                except Exception as stop_err:
                    logger.warning(f"Could not force-stop conflicting Octo profile {profile_id}: {stop_err}")
                resp = httpx.post(url, json=payload, timeout=180.0)
                if self._is_already_running(resp) and self._public_api is not None:
                    logger.warning(f"Octo profile {profile_id} still locked — releasing cloud lock and retrying")
                    try:
                        self._public_api.force_stop_cloud(profile_id)
                        time.sleep(4.0)
                    except Exception as cloud_err:
                        logger.warning(f"Cloud force-stop failed for {profile_id}: {cloud_err}")
                    resp = httpx.post(url, json=payload, timeout=180.0)

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
