import httpx
from loguru import logger

from src.accounts.proxy import ProxyInfo

DOLPHIN_PUBLIC_API_BASE = "https://dolphin-anty-api.com"
DARKWING_API_BASE = "https://darkwing.dolphin-anty-api-cf.com/api/v1"


class DolphinPublicApi:
    """
    Client for the Dolphin Anty Public API.

    Uses sync httpx to match the pattern in DolphinAntyManager.
    Requires a Dolphin Anty JWT token for Authorization.
    """

    def __init__(self, anty_token: str) -> None:
        self._headers = {"Authorization": f"Bearer {anty_token}"}

    def get_profile_proxy(self, profile_id: str) -> ProxyInfo | None:
        """
        Fetch the currently configured proxy for a browser profile.

        Returns None if no proxy is set on the profile.
        Raises httpx.HTTPStatusError on API errors.
        """
        try:
            resp = httpx.get(
                f"{DARKWING_API_BASE}/start/preliminary/{profile_id}",
                headers=self._headers,
                timeout=10.0,
            )
            resp.raise_for_status()
            proxy = resp.json().get("proxy") or {}
        except Exception:
            resp = httpx.get(
                f"{DOLPHIN_PUBLIC_API_BASE}/browser_profiles/{profile_id}",
                headers=self._headers,
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            proxy = data.get("proxy") or {}
        host = proxy.get("host")
        port = proxy.get("port")
        if not host or not port:
            return None
        return ProxyInfo(
            host=host,
            port=int(port),
            user=proxy.get("login") or "",
            password=proxy.get("password") or "",
            type=proxy.get("type", "http"),
        )

    def set_profile_proxy(self, profile_id: str, proxy: ProxyInfo) -> None:
        """
        Update the proxy configuration on a browser profile.

        Raises httpx.HTTPStatusError on failure.
        """
        resp = httpx.patch(
            f"{DOLPHIN_PUBLIC_API_BASE}/browser_profiles/{profile_id}",
            json={
                "proxy": {
                    "type": proxy.type,
                    "host": proxy.host,
                    "port": proxy.port,
                    "login": proxy.user,
                    "password": proxy.password,
                }
            },
            headers=self._headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        logger.debug(f"Proxy updated on profile {profile_id}: {proxy.host}:{proxy.port}")
