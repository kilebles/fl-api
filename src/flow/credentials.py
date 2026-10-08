from loguru import logger

from src.accounts.account import AccountInfo
from src.flow.google_auth import SCHEME_BEARER, SCHEME_SAPISIDHASH, auth_scheme, sapisidhash_header


class CredentialsService:
    """Live credentials of one browser session: Authorization, cookies, fingerprint headers, project ID."""

    def __init__(self, origin: str = "https://flow.google.com") -> None:
        self._origin = origin
        self._authorization: str | None = None
        self._cookies: dict[str, str] = {}
        self._project_id: str | None = None
        self._browser_headers: dict[str, str] = {}
        logger.info("CredentialsService initialized")

    @property
    def authorization(self) -> str:
        """Authorization header value for aisandbox-pa.googleapis.com requests.

        Bearer tokens are replayed as intercepted from the browser. SAPISIDHASH
        is recomputed on every access because the hash embeds a timestamp.
        """
        if self._authorization is None:
            raise RuntimeError("Authorization not yet available — wait_for_authorization() not awaited")
        if auth_scheme(self._authorization) == SCHEME_SAPISIDHASH:
            fresh = sapisidhash_header(self._cookies, self._origin)
            if fresh:
                return fresh
        return self._authorization

    @property
    def bearer_token(self) -> str:
        """OAuth2 bearer token intercepted from the browser (empty if the page uses SAPISIDHASH)."""
        authorization = self.authorization
        if auth_scheme(authorization) == SCHEME_BEARER:
            return authorization.split(" ", 1)[1]
        return ""

    def set_authorization(self, authorization: str) -> None:
        """Set the Authorization header value captured from live browser traffic."""
        self._authorization = authorization
        logger.info(f"Authorization set (scheme={auth_scheme(authorization)}, len={len(authorization)})")

    def set_bearer_token(self, token: str) -> None:
        """Set a bare OAuth2 bearer token (kept for callers that only have the token)."""
        self.set_authorization(token if " " in token else f"{SCHEME_BEARER} {token}")

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """Set browser cookies (needed for SAPISIDHASH and cookie-authenticated requests)."""
        self._cookies = dict(cookies)

    @property
    def cookies(self) -> dict[str, str]:
        return self._cookies

    @property
    def project_id(self) -> str:
        """Flow project identifier the browser session works in."""
        if self._project_id is None:
            raise RuntimeError("Project ID not yet available — wait_for_project_id() not awaited")
        return self._project_id

    @property
    def known_project_id(self) -> str | None:
        """Project ID if already known (persisted for the account), else None."""
        return self._project_id

    def set_project_id(self, project_id: str) -> None:
        """Set the project ID obtained by the producer."""
        self._project_id = project_id
        logger.info(f"Project ID set: {project_id}")

    @property
    def browser_headers(self) -> dict[str, str]:
        """Browser fingerprint headers intercepted from browser requests."""
        return self._browser_headers

    def set_browser_headers(self, headers: dict[str, str]) -> None:
        """Set browser fingerprint headers from live browser interception."""
        self._browser_headers = headers

    def reload(self, account: AccountInfo) -> None:
        """Switch to a different account. Live credentials are re-captured by the producer."""
        self._authorization = None
        self._cookies = {}
        self._project_id = account.project_id
        self._browser_headers = {}
        logger.info(
            f"Credentials reloaded for {account.name}"
            + (f" (persisted project {account.project_id})" if account.project_id else "")
        )
