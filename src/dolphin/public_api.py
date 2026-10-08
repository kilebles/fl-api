import httpx
from loguru import logger

from src.accounts.proxy import ProxyInfo

DOLPHIN_PUBLIC_API_BASE = "https://dolphin-anty-api.com"


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
        resp = httpx.get(
            f"{DOLPHIN_PUBLIC_API_BASE}/browser_profiles/{profile_id}",
            headers=self._headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        proxy = data.get("proxy") or {}
        host = proxy.get("host")
        if not host:
            return None
        return ProxyInfo(
            host=host,
            port=int(proxy.get("port", 0)),
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
