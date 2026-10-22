import asyncio
import random
import re
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
from loguru import logger
from playwright.async_api import Page, async_playwright

from src.core.errors import RecaptchaInterfaceError
from src.core.settings import Settings
from src.flow.browser.human_behavior import HumanBehavior
from src.flow.browser.timing import HumanTiming
from src.flow.google_auth import SCHEME_BEARER, auth_scheme, sapisidhash_header


_GRECAPTCHA_INIT_SCRIPT = """
(() => {
  const KEY = '__originalGrecaptchaExecute';
  const isObj = (v) => v !== null && (typeof v === 'object' || typeof v === 'function');
  const plain = (obj, name, value) => {
    try {
      Object.defineProperty(obj, name, { value, writable: true, configurable: true, enumerable: true });
    } catch (e) {}
  };
  const captured = () => typeof window[KEY] === 'function';
  const finalize = () => {
    const g = window.grecaptcha;
    plain(window, 'grecaptcha', g);
    if (!isObj(g)) return;
    const ent = g.enterprise;
    plain(g, 'enterprise', ent);
    if (isObj(ent)) plain(ent, 'execute', ent.execute);
  };
  const capture = (fn) => {
    if (typeof fn !== 'function' || captured()) return false;
    plain(window, KEY, fn);
    finalize();
    return true;
  };
  const trapExecute = (ent) => {
    if (!isObj(ent) || captured()) return;
    if (capture(ent.execute)) return;
    let execute = ent.execute;
    try {
      Object.defineProperty(ent, 'execute', {
        configurable: true, enumerable: true,
        get() { return execute; },
        set(v) { execute = v; capture(v); },
      });
    } catch (e) {}
  };
  const trapEnterprise = (g) => {
    if (!isObj(g) || captured()) return;
    if (isObj(g.enterprise)) { trapExecute(g.enterprise); return; }
    let ent = g.enterprise;
    try {
      Object.defineProperty(g, 'enterprise', {
        configurable: true, enumerable: true,
        get() { return ent; },
        set(v) { ent = v; trapExecute(v); },
      });
    } catch (e) {}
  };
  let g = window.grecaptcha;
  if (isObj(g)) { trapEnterprise(g); return; }
  Object.defineProperty(window, 'grecaptcha', {
    configurable: true, enumerable: true,
    get() { return g; },
    set(v) { g = v; trapEnterprise(v); },
  });
})();
"""

_GRECAPTCHA_READY_JS = "() => typeof window.grecaptcha?.enterprise?.execute === 'function'"

_GRECAPTCHA_STATE_JS = """
() => {
  const g = window.grecaptcha;
  const ent = g && g.enterprise;
  const fn = ent && ent.execute;
  const backup = window.__originalGrecaptchaExecute;
  return {
    execute: typeof fn === 'function',
    backup: typeof backup === 'function',
    wrapped: typeof fn === 'function' && typeof backup === 'function' && fn !== backup,
  };
}
"""

_GRECAPTCHA_EXECUTE_JS = """
async ({ siteKey, action }) => {
  const g = window.grecaptcha;
  const ent = g && g.enterprise;
  const fn = window.__originalGrecaptchaExecute || (ent && ent.execute);
  if (typeof fn !== 'function') {
    throw new Error('RecaptchaApiNotReady');
  }
  return await fn.call(ent, siteKey, { action });
}
"""

_RECAPTCHA_NOT_READY_MARKERS = ("RecaptchaApiNotReady", "grecaptcha is not defined")
_MAX_CRASHES_BEFORE_GIVING_UP = 4  # consecutive reconnect failures before run() lets the pipeline replace the browser


def _is_recaptcha_not_ready(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _RECAPTCHA_NOT_READY_MARKERS)


class RecaptchaTokenProducer:
    """
    Connects to an existing Chrome instance via CDP and generates reCAPTCHA tokens on demand.

    Reuses a single flow.google.com tab to avoid detection.
    Periodically reloads the page and simulates human activity to maintain
    a healthy reCAPTCHA score during long sessions.
    Also extracts cookies and live auth headers from the browser for use by
    the HTTP services.
    """

    _LABS_HOSTS = ("labs.google", "flow.google.com")
    _API_HOST = "aisandbox-pa.googleapis.com"
    _PROJECT_RE = re.compile(r"/project/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
    _NEW_PROJECT_RE = re.compile(r"new project|create project|новый проект|создать проект", re.IGNORECASE)
    _FINGERPRINT_HEADERS = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "user-agent", "x-client-data")
    _API_EXTRA_HEADERS = ("x-goog-api-key", "x-goog-authuser")
    _SESSION_PATH = "/fx/api/auth/session"
    _SIGNIN_PATH = "/fx/api/auth/signin?callbackUrl=https%3A%2F%2Flabs.google%2Ffx%2F"

    def __init__(
        self,
        settings: Settings,
        cdp_url: str | None = None,
        proxy_url: str | None = None,
        recaptcha_action: str | None = None,
        project_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._cdp_url = cdp_url
        self._proxy_url = proxy_url
        self._recaptcha_action = recaptcha_action or settings.recaptcha_action
        self._known_project_id = project_id
        self._authorization: str | None = None
        self.on_authorization: Callable[[str], None] | None = None
        self.on_cookies: Callable[[dict[str, str]], None] | None = None
        self._last_session_refresh: float = 0.0
        self._seen_api_paths: set[str] = set()
        self._seen_rpc_ids: set[str] = set()
        self._timing = HumanTiming()
        self._behavior = HumanBehavior()
        self._cookies: dict[str, str] = {}
        self._cookies_ready = asyncio.Event()
        self._bearer_token: str | None = None
        self._bearer_token_ready = asyncio.Event()
        self._browser_headers: dict[str, str] = {}
        self._browser_headers_ready = asyncio.Event()
        self._project_id: str | None = None
        self._project_id_ready = asyncio.Event()
        self._listener_pages: set[int] = set()
        self._tokens_since_reload: int = 0
        self._reload_threshold: int = self._next_reload_threshold()
        self._total_tokens: int = 0
        self._total_reloads: int = 0
        self._total_403s: int = 0
        self._last_execute_time: float = 0.0
        self._last_token_time: float = 0.0
        self._ever_connected: bool = False
        self._token_lock = asyncio.Lock()
        self._page_lost = asyncio.Event()
        self._context = None
        self._auth_lock = asyncio.Lock()
        self._page: Page | None = None
        self._page_ready = asyncio.Event()
        self._paused = asyncio.Event()
        self._paused.set()  # not paused by default
        self._pause_count: int = 0  # reference-counted pauses

    def pause(self, resume_in: float | None = None) -> None:
        """Pause token generation. Reference-counted — producer stays paused until all callers resume."""
        self._pause_count += 1
        self._paused.clear()
        if resume_in is not None:
            logger.info(f"reCAPTCHA producer paused for {resume_in:.1f}s (pause_count={self._pause_count})")
        else:
            logger.info(f"reCAPTCHA producer paused (pause_count={self._pause_count})")

    def resume(self) -> None:
        """Resume token generation. Only unpauses when all callers have resumed."""
        if self._pause_count > 0:
            self._pause_count -= 1
        if self._pause_count == 0 and not self._paused.is_set():
            self._paused.set()
            logger.info("reCAPTCHA producer resumed")

    @property
    def cookies(self) -> dict[str, str]:
        """Get cookies extracted from browser."""
        return self._cookies

    async def wait_for_cookies(self, timeout: float = 120.0) -> dict[str, str]:
        """Wait until cookies are extracted and return them."""
        try:
            await asyncio.wait_for(self._cookies_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Cookies not intercepted within timeout")
            raise
        return self._cookies

    @property
    def bearer_token(self) -> str | None:
        """OAuth2 bearer token intercepted from browser requests to aisandbox-pa.googleapis.com."""
        return self._bearer_token

    @property
    def authorization(self) -> str | None:
        """Full Authorization header value the page sends to aisandbox-pa.googleapis.com."""
        return self._authorization

    async def wait_for_authorization(self, timeout: float = 60.0) -> str:
        """Wait until the page authenticates against the API and return its Authorization header."""
        try:
            await asyncio.wait_for(self._bearer_token_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(
                "Authorization not intercepted within timeout — the page made no authenticated "
                f"request to {self._API_HOST} (seen paths: {sorted(self._seen_api_paths) or 'none'}, "
                f"rpc ids: {sorted(self._seen_rpc_ids) or 'none'})"
            )
            raise
        return self._authorization  # type: ignore[return-value]

    async def wait_for_bearer_token(self, timeout: float = 60.0) -> str:
        """Wait for auth; returns the bare bearer token, or the full header for other schemes."""
        authorization = await self.wait_for_authorization(timeout=timeout)
        if auth_scheme(authorization) == SCHEME_BEARER:
            return authorization.split(" ", 1)[1]
        return authorization

    @property
    def browser_headers(self) -> dict[str, str]:
        """Browser headers intercepted from fetchUserRecommendations request."""
        return self._browser_headers

    async def wait_for_browser_headers(self, timeout: float = 60.0) -> dict[str, str]:
        """Wait until browser headers are intercepted and return them."""
        try:
            await asyncio.wait_for(self._browser_headers_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Browser headers not intercepted within timeout")
            raise
        return self._browser_headers

    @property
    def project_id(self) -> str | None:
        """Flow project ID the browser tab is working in."""
        return self._project_id

    def _project_url(self, project_id: str) -> str:
        return f"{self._settings.flow_base_url}/project/{project_id}"

    async def wait_for_project_id(self, timeout: float = 120.0) -> str:
        """Wait until project ID is created and return it."""
        try:
            await asyncio.wait_for(self._project_id_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Project ID not received within timeout")
            raise
        return self._project_id  # type: ignore[return-value]

    async def wait_for_page_ready(self, timeout: float = 60.0) -> None:
        """Wait until the browser page is fully initialized and ready for token generation."""
        try:
            await asyncio.wait_for(self._page_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Page not ready within timeout")
            raise

    def report_failure(self) -> None:
        """Called by RetryService on each 403 — signals producer to reload page."""
        self._total_403s += 1
        logger.warning(
            f"reCAPTCHA 403 reported (total 403s: {self._total_403s}, "
            f"tokens since last reload: {self._tokens_since_reload})"
        )
        self._tokens_since_reload = self._reload_threshold  # trigger reload on next iteration

    def report_success(self) -> None:
        """Called by RetryService on successful generation."""
        logger.debug(
            f"Generation success reported to producer "
            f"(tokens since reload: {self._tokens_since_reload}/{self._reload_threshold})"
        )

    async def _setup_browser(self, p) -> tuple:
        """Connect to Chrome, set up page, extract cookies, create project. Returns (browser, context, page)."""
        logger.info(f"Connecting to Chrome via CDP on {self._cdp_url}...")
        browser = await self._connect_with_retry(p)
        self._ever_connected = True
        context = browser.contexts[0]
        self._context = context

        await context.add_init_script("""
            (() => {
                // Patch Runtime.Enable CDP detection via console API leak
                const _warn = console.warn.bind(console);
                const _debug = console.debug.bind(console);
                Object.defineProperty(console, 'warn', { value: _warn });
                Object.defineProperty(console, 'debug', { value: _debug });

                // Remove Playwright global markers
                delete window.__playwright__binding__;
                delete window.__pwInitScripts;
                delete window.__playwright__;

                // Ensure navigator.webdriver is not detectable
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                    configurable: true,
                });
            })();
        """)
        await context.add_init_script(_GRECAPTCHA_INIT_SCRIPT)

        page = await self._find_or_create_labs_page(context)

        await self._extract_cookies(context)
        self._publish_cookies()
        await self._ensure_labs_session(context)
        self._cookies_ready.set()
        project_url = await self._ensure_project(page)
        await self._ensure_browser_headers(page)
        project_settle_delay = random.uniform(5.0, 10.0)
        logger.info(f"Waiting {project_settle_delay:.1f}s for project to settle...")
        await asyncio.sleep(project_settle_delay)
        self._page = page
        self._page_ready.set()
        logger.info(f"Navigated to project page: {project_url}")
        logger.success(
            f"reCAPTCHA producer ready — "
            f"reload every {self._reload_threshold} tokens"
        )
        return browser, context, page

    async def run(self) -> None:
        """
        Connect to existing Chrome via CDP and keep the session healthy.
        Tokens are generated on demand via get_token(). Loop continues until cancelled.
        On unexpected errors (CDP disconnect, page crash, timeout) — reconnects automatically.
        """
        reconnect_delay = 5.0
        crashes_since_setup = 0
        async with async_playwright() as p:
            browser: object | None = None
            context: object | None = None

            while True:
                try:
                    if browser is None:
                        self._page_lost.clear()
                        browser, context, _ = await self._setup_browser(p)
                        reconnect_delay = 5.0
                        crashes_since_setup = 0
                    ticks = 0
                    while True:
                        await asyncio.sleep(5.0)
                        if self._page_lost.is_set():
                            raise RuntimeError("page lost — reconnecting")
                        ticks += 1
                        if ticks % 12 == 0:
                            await self._maybe_refresh_session(context)
                            if self._page is not None:
                                await self._close_stale_labs_pages(context, keep=self._page)

                except asyncio.CancelledError:
                    logger.info(
                        f"Producer stopped — total tokens: {self._total_tokens}, "
                        f"reloads: {self._total_reloads}, 403s: {self._total_403s}"
                    )
                    raise
                except RecaptchaInterfaceError as e:
                    logger.bind(event_type="producer_crashed").error(
                        f"Producer stopped: {e} — page/script problem, not reconnecting"
                    )
                    raise
                except Exception as e:
                    if not self._ever_connected:
                        logger.bind(event_type="producer_crashed").error(
                            f"Producer could not connect to {self._cdp_url} ({type(e).__name__}: {e}) — giving up"
                        )
                        raise
                    logger.bind(event_type="producer_crashed").error(
                        f"Producer crashed ({type(e).__name__}: {e}) — "
                        f"reconnecting in {reconnect_delay:.0f}s..."
                    )
                    crashes_since_setup += 1
                    if crashes_since_setup >= _MAX_CRASHES_BEFORE_GIVING_UP:
                        logger.bind(event_type="producer_crashed").error(
                            f"Producer could not get back to {self._cdp_url} after {crashes_since_setup} attempts — "
                            "giving up so the pipeline starts a fresh browser"
                        )
                        raise
                    browser = None
                    self._page = None
                    self._page_ready.clear()
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60.0)
                    logger.info("Producer reconnecting to Chrome via CDP...")

    async def get_token(self, action: str | None = None) -> str:
        """Generate a reCAPTCHA token on demand using the current page.

        `action` overrides the producer's default reCAPTCHA action for this token
        (e.g. IMAGE_GENERATION for an image request from a video producer).

        Calls are serialised; a page/CDP failure marks the page lost so the
        supervisor loop in run() reconnects, and the error is raised to the caller.
        """
        async with self._token_lock:
            await self._paused.wait()
            if self._page is None:
                await self.wait_for_page_ready(timeout=90.0)
            try:
                return await self._get_token_locked(action)
            except (asyncio.CancelledError, RecaptchaInterfaceError):
                raise
            except Exception:
                self._page = None
                self._page_ready.clear()
                self._page_lost.set()
                raise

    async def _get_token_locked(self, action: str | None = None) -> str:
        if self._tokens_since_reload >= self._reload_threshold:
            self._page = await self._reload_page(self._page, self._page.context)

        await self._close_stale_labs_pages(self._page.context, keep=self._page)
        await self._behavior.idle_activity(self._page)

        try:
            token = await self._generate_token(self._page, action)
        except Exception as e:
            if not _is_recaptcha_not_ready(e):
                raise
            logger.warning(f"grecaptcha not ready, reloading page: {e}")
            self._page = await self._reload_page(self._page, self._page.context)
            try:
                token = await self._generate_token(self._page, action)
            except Exception as retry_exc:
                if _is_recaptcha_not_ready(retry_exc):
                    raise RecaptchaInterfaceError(
                        "grecaptcha.enterprise.execute is unavailable even after a page reload"
                    ) from retry_exc
                raise

        self._total_tokens += 1
        self._tokens_since_reload += 1
        logger.info(
            f"Token #{self._total_tokens} generated "
            f"(since reload: {self._tokens_since_reload}/{self._reload_threshold}, "
            f"reloads: {self._total_reloads}, 403s: {self._total_403s})"
        )
        logger.debug(
            f"Token #{self._total_tokens} value: "
            f"{token[:60]}...{token[-20:]} (len={len(token)})"
        )
        return token

    async def _generate_token(self, page: Page, action: str | None = None) -> str:
        """Execute reCAPTCHA for `action` (default: the producer's action) and return the token.

        Enforces a minimum 5s gap between consecutive execute() calls to avoid
        suspicious burst patterns that trigger PUBLIC_ERROR_UNUSUAL_ACTIVITY.
        """
        min_interval = 10.0
        elapsed = asyncio.get_event_loop().time() - self._last_execute_time
        if self._last_execute_time > 0 and elapsed < min_interval:
            wait = min_interval - elapsed
            logger.debug(f"Enforcing min execute interval: waiting {wait:.2f}s")
            await asyncio.sleep(wait)

        action = action or self._recaptcha_action
        logger.debug(
            f"Calling grecaptcha.enterprise.execute("
            f"site_key={self._settings.recaptcha_site_key[:12]}..., "
            f"action={action})"
        )
        token: str = await asyncio.wait_for(
            page.evaluate(
                _GRECAPTCHA_EXECUTE_JS,
                {"siteKey": self._settings.recaptcha_site_key, "action": action},
            ),
            timeout=30.0,
        )
        self._last_execute_time = asyncio.get_event_loop().time()
        self._last_token_time = self._last_execute_time
        return token

    def token_age(self) -> float | None:
        """Seconds since the newest token was generated (None before the first token)."""
        if self._last_token_time <= 0:
            return None
        return asyncio.get_event_loop().time() - self._last_token_time

    async def _reload_page(self, page: Page, context) -> Page:
        """Reload the Flow project page and wait for reCAPTCHA to reinitialize."""
        self._reload_threshold = self._next_reload_threshold()
        self._tokens_since_reload = 0
        self._total_reloads += 1

        logger.info(
            f"Reloading Flow page (reload #{self._total_reloads}, "
            f"next reload in {self._reload_threshold} tokens)..."
        )

        try:
            await page.reload(timeout=self._settings.reload_timeout * 1000)
            await self._wait_for_recaptcha(page, self._settings.reload_timeout * 1000)
            await self._anchor_delay(cold_start=True)
            await self._extract_cookies(context)
            self._publish_cookies()
            self._last_execute_time = asyncio.get_event_loop().time()  # keep interval after reload
            logger.success(
                f"Page reloaded successfully (reload #{self._total_reloads})"
            )
        except (asyncio.CancelledError, RecaptchaInterfaceError):
            raise
        except Exception as e:
            logger.warning(f"Reload failed: {e}, attempting to find/create new tab...")
            page = await self._find_or_create_labs_page(context, retire=page)

        self._page = page
        return page

    async def _wait_for_recaptcha(self, page: Page, timeout_ms: float) -> None:
        """Wait for grecaptcha.enterprise.execute to appear, then verify the original was captured."""
        await page.wait_for_function(_GRECAPTCHA_READY_JS, timeout=timeout_ms)
        await self._check_recaptcha_integrity(page)

    async def _check_recaptcha_integrity(self, page: Page) -> None:
        """Raise RecaptchaInterfaceError unless the untouched grecaptcha execute is callable.

        The init script stores the original grecaptcha.enterprise.execute in
        window.__originalGrecaptchaExecute the moment the reCAPTCHA library assigns it.
        If Flow later wraps the public method, tokens are still produced through the
        original. If nothing was captured (init script did not run, or the library
        changed how it initialises), calling the public method would only yield
        tokens the API rejects, so stop here instead of rotating accounts and proxies.
        """
        state: dict = await page.evaluate(_GRECAPTCHA_STATE_JS)
        if not state.get("execute"):
            raise RecaptchaInterfaceError("grecaptcha.enterprise.execute is not a function")
        if not state.get("backup"):
            raise RecaptchaInterfaceError(
                "original grecaptcha.enterprise.execute was not captured by the init script"
            )
        if state.get("wrapped"):
            logger.info("grecaptcha.enterprise.execute is wrapped by the page — using the captured original")

    async def _fetch_labs_session(self) -> dict:
        """Read the legacy labs.google next-auth session (holds the API access token)."""
        cookie_header = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        async with httpx.AsyncClient(proxy=self._proxy_url, timeout=20.0) as client:
            response = await client.get(
                f"{self._settings.labs_base_url}{self._SESSION_PATH}",
                headers={"Cookie": cookie_header, "Referer": f"{self._settings.labs_base_url}/fx/"},
            )
        response.raise_for_status()
        try:
            return response.json() or {}
        except ValueError:
            return {}

    async def _labs_signin(self, context) -> None:
        """Re-establish the labs.google session by clicking "Sign in with Google" in the profile.

        The Google account is already logged in, so the OAuth round-trip completes
        without user interaction; an account chooser, if shown, is clicked through.
        """
        url = f"{self._settings.labs_base_url}{self._SIGNIN_PATH}"
        page = await context.new_page()
        try:
            await page.goto(url, timeout=self._settings.reload_timeout * 1000)
            button = page.locator("form button, form input[type=submit]").first
            await button.wait_for(state="visible", timeout=15_000)
            await asyncio.sleep(random.uniform(0.8, 2.0))
            await button.click()
            deadline = asyncio.get_event_loop().time() + 60.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2.0)
                current = page.url
                if current.startswith(f"{self._settings.labs_base_url}/fx") and "/api/auth" not in current:
                    logger.success("labs.google session re-established via Google sign-in")
                    return
                if "accounts.google.com" in current:
                    await self._click_through_google_prompt(page)
            raise RuntimeError(f"Sign-in did not return to labs.google (stuck at {page.url})")
        finally:
            await self._close_page(page)

    async def _click_through_google_prompt(self, page: Page) -> None:
        for selector in ("div[data-email]", "[data-identifier]", "li div[role=link]"):
            locator = page.locator(selector).first
            if await locator.count():
                try:
                    await locator.click(timeout=3_000)
                    logger.info(f"Account chooser: clicked {selector}")
                    return
                except Exception:
                    continue
        for name in ("Continue", "Продолжить", "Allow", "Разрешить"):
            locator = page.get_by_role("button", name=name).first
            if await locator.count():
                try:
                    await locator.click(timeout=3_000)
                    logger.info(f"Consent prompt: clicked {name}")
                    return
                except Exception:
                    continue

    async def _ensure_labs_session(self, context, force: bool = False) -> None:
        """Build and publish the Authorization header from the browser's Google cookies (SAPISIDHASH).

        Google retired https://labs.google/fx/api/auth/* (410 Gone) on 2026-10-21;
        Flow authenticates against aisandbox-pa.clients6.google.com using first-party
        Google cookies and SAPISIDHASH.
        """
        async with self._auth_lock:
            if force and self._page is not None and not self._page.is_closed():
                try:
                    await self._page.reload(timeout=self._settings.reload_timeout * 1000)
                except Exception as e:
                    logger.warning(f"Page reload during reauthentication failed: {e}")
            if force or not self._cookies:
                await self._extract_cookies(context)
                self._publish_cookies()

            sapisid_auth = sapisidhash_header(self._cookies, self._settings.flow_base_url)
            if sapisid_auth:
                self._last_session_refresh = asyncio.get_event_loop().time()
                self._publish_authorization(sapisid_auth, source="Google cookies (SAPISIDHASH)")
                return

            raise RuntimeError(
                "Google account is not signed in on flow.google.com "
                f"(no SAPISID cookies in browser; cookies={sorted(self._cookies)})"
            )

    @staticmethod
    def _session_expired(expires: str | None) -> bool:
        if not expires:
            return False
        try:
            when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            return False
        return when <= datetime.now(timezone.utc)

    async def reauthenticate(self) -> str:
        """Sign in to labs.google again after the API rejected the current token; returns the new header."""
        if self._context is None:
            raise RuntimeError("Producer has no browser context yet")
        await self._ensure_labs_session(self._context, force=True)
        return self._authorization  # type: ignore[return-value]

    def _publish_cookies(self) -> None:
        """Hand the current cookie jar to the pipeline (downloader / tRPC calls) after it changed."""
        if self.on_cookies is None:
            return
        try:
            self.on_cookies(dict(self._cookies))
        except Exception as e:
            logger.warning(f"on_cookies callback failed: {e}")

    def _publish_authorization(self, authorization: str, source: str) -> None:
        if authorization == self._authorization:
            return
        self._authorization = authorization
        if auth_scheme(authorization) == SCHEME_BEARER:
            self._bearer_token = authorization.split(" ", 1)[1]
        logger.success(f"Authorization ready from {source} (scheme={auth_scheme(authorization)}, len={len(authorization)})")
        self._bearer_token_ready.set()
        if self.on_authorization is not None:
            try:
                self.on_authorization(authorization)
            except Exception as e:
                logger.warning(f"on_authorization callback failed: {e}")

    async def _maybe_refresh_session(self, context) -> None:
        """Refresh the labs.google access token on an interval (tokens expire after ~1h)."""
        elapsed = asyncio.get_event_loop().time() - self._last_session_refresh
        if elapsed < self._settings.labs_session_refresh_interval:
            return
        logger.info(f"Refreshing labs.google session ({elapsed / 60:.0f} min since last refresh)...")
        await self._extract_cookies(context)
        self._publish_cookies()
        await self._ensure_labs_session(context)

    async def _ensure_browser_headers(self, page: Page) -> None:
        """Derive fingerprint and WIZ API key headers from the page."""
        try:
            derived: dict[str, str] = await page.evaluate(
                """async () => {
                    const out = {'user-agent': navigator.userAgent};
                    const data = navigator.userAgentData;
                    if (data) {
                        out['sec-ch-ua'] = data.brands.map(b => `"${b.brand}";v="${b.version}"`).join(', ');
                        out['sec-ch-ua-mobile'] = data.mobile ? '?1' : '?0';
                        out['sec-ch-ua-platform'] = `"${data.platform}"`;
                    }
                    const wiz = window.WIZ_global_data;
                    if (wiz) {
                        if (wiz.K21R3e) out['x-goog-api-key'] = String(wiz.K21R3e);
                        out['x-goog-authuser'] = String(wiz.QrtxK ?? '0');
                    }
                    return out;
                }"""
            )
        except Exception as e:
            logger.warning(f"Could not derive browser headers from page: {e}")
            derived = {}
        if derived:
            for key, value in derived.items():
                self._browser_headers.setdefault(key, value)
            logger.info(f"Browser headers derived from page: {sorted(self._browser_headers)}")
        self._browser_headers_ready.set()

    async def _ensure_project(self, page: Page) -> str:
        """Make sure the tab is on a usable Flow project page and return its URL.

        Order of preference: a project ID persisted for this account, a fresh
        project created through the legacy labs.google tRPC (works only while
        that session is still valid), a project created through the Flow UI,
        and finally any existing project listed on the Flow home page.
        """
        candidates: list[tuple[str, object]] = [
            ("persisted", self._known_project),
            ("trpc", self._create_project_trpc),
            ("ui", self._create_project_via_ui),
            ("existing", self._discover_project_on_page),
        ]
        errors: list[str] = []
        for source, getter in candidates:
            try:
                project_id = await getter(page)  # type: ignore[operator]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors.append(f"{source}: {type(e).__name__}: {e}")
                logger.warning(f"Project via {source} failed: {type(e).__name__}: {e}")
                continue
            if not project_id:
                continue
            if await self._open_project_page(page, project_id):
                if source != "persisted":
                    logger.success(f"Project ready via {source}: {project_id}")
                self._project_id = project_id
                self._project_id_ready.set()
                return self._project_url(project_id)
            errors.append(f"{source}: project page {project_id} did not load")
            if source == "persisted":
                self._known_project_id = None
                self._project_id = None
        raise RuntimeError("Could not obtain a Flow project: " + "; ".join(errors))

    async def _known_project(self, page: Page) -> str | None:
        """Project already used by this producer (after a reconnect) or persisted for the account."""
        project_id = self._project_id or self._known_project_id
        if project_id and not self._project_id:
            logger.info(f"Reusing persisted project {project_id}")
        return project_id

    async def _open_project_page(self, page: Page, project_id: str) -> bool:
        """Navigate to the project page and wait for reCAPTCHA; False if Flow bounced us elsewhere."""
        timeout_ms = self._settings.reload_timeout * 1000
        url = self._project_url(project_id)
        try:
            await page.goto(url, timeout=timeout_ms)
            await self._wait_for_recaptcha(page, timeout_ms)
        except (asyncio.CancelledError, RecaptchaInterfaceError):
            raise
        except Exception as e:
            logger.warning(f"Project page {url} not ready: {type(e).__name__}: {e} (now at {page.url})")
            return False
        if project_id not in page.url:
            logger.warning(f"Flow redirected away from project {project_id} to {page.url}")
            return False
        return True

    async def _create_project_trpc(self, page: Page) -> str | None:
        """Create a project via the legacy labs.google tRPC endpoint (needs a valid labs.google session)."""
        if auth_scheme(self._authorization or "") != SCHEME_BEARER or "__Secure-next-auth.session-token" not in self._cookies:
            logger.info("No active legacy labs.google session — skipping tRPC project creation")
            return None
        title = datetime.now(timezone.utc).strftime("%b %d, %I:%M %p")
        payload = {"json": {"projectTitle": title, "toolName": "PINHOLE"}}
        cookie_header = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        headers = {
            "Content-Type": "application/json",
            "Origin": self._settings.labs_base_url,
            "Referer": f"{self._settings.labs_base_url}/fx/tools/flow",
            "Cookie": cookie_header,
        }
        async with httpx.AsyncClient(proxy=self._proxy_url) as client:
            response = await client.post(
                f"{self._settings.labs_base_url}/fx/api/trpc/project.createProject",
                json=payload,
                headers=headers,
                timeout=15.0,
            )
        if response.status_code == 401:
            logger.info("Legacy labs.google session expired (tRPC 401) — trying other ways to get a project")
            return None
        response.raise_for_status()
        data = response.json()
        project_id = data["result"]["data"]["json"]["result"]["projectId"]
        logger.success(f"Project created via tRPC: {project_id}")
        return project_id

    async def _create_project_via_ui(self, page: Page) -> str | None:
        """Click Flow's "New project" button and read the project ID from the resulting URL."""
        home = self._settings.labs_url
        if self._PROJECT_RE.search(page.url) or not page.url.startswith(home.rstrip("/")):
            await page.goto(home, timeout=self._settings.reload_timeout * 1000)
        await asyncio.sleep(random.uniform(2.0, 4.0))
        button = page.get_by_role("button", name=self._NEW_PROJECT_RE).first
        try:
            await button.wait_for(state="visible", timeout=10_000)
        except Exception:
            button = page.get_by_text(self._NEW_PROJECT_RE).first
            await button.wait_for(state="visible", timeout=5_000)
        await self._behavior.idle_activity(page)
        await button.click(timeout=5_000)
        await page.wait_for_url(self._PROJECT_RE, timeout=20_000)
        match = self._PROJECT_RE.search(page.url)
        return match.group(1) if match else None

    async def _discover_project_on_page(self, page: Page) -> str | None:
        """Find an existing project ID on the Flow home page (links or rendered markup)."""
        home = self._settings.labs_url
        if self._PROJECT_RE.search(page.url) or not page.url.startswith(home.rstrip("/")):
            await page.goto(home, timeout=self._settings.reload_timeout * 1000)
        deadline = asyncio.get_event_loop().time() + 25.0
        while True:
            match = self._PROJECT_RE.search(page.url)
            if match:
                return match.group(1)
            found: list[str] = await page.evaluate(
                """() => {
                    const re = /[/]project[/]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/g;
                    const out = [];
                    for (const a of document.querySelectorAll('a[href*="/project/"]')) {
                        const m = a.getAttribute('href').match(re);
                        if (m) out.push(m[0]);
                    }
                    const html = document.documentElement.outerHTML;
                    let m;
                    while ((m = re.exec(html)) !== null) out.push(m[0]);
                    return out;
                }"""
            )
            ids: list[str] = []
            for item in found:
                m = self._PROJECT_RE.search(item)
                if m and m.group(1) not in ids:
                    ids.append(m.group(1))
            if ids:
                logger.info(f"Found {len(ids)} existing project(s) on Flow home, using {ids[0]}")
                return ids[0]
            if asyncio.get_event_loop().time() > deadline:
                logger.warning(f"No project links found on {page.url}")
                return None
            await asyncio.sleep(2.0)

    async def _extract_cookies(self, context) -> None:
        """Extract cookies for flow.google.com, labs.google and google.com into one flat dict.

        labs.google cookies win on name collisions (legacy next-auth session), then
        flow.google.com, then the shared google.com account cookies.
        """
        all_cookies = await context.cookies(
            ["https://labs.google", self._settings.flow_base_url, "https://google.com", "https://clients6.google.com"]
        )

        def rank(domain: str) -> int:
            if domain.lstrip(".") == "labs.google":
                return 0
            if domain.lstrip(".") == "flow.google.com":
                return 1
            return 2

        self._cookies = {}
        for c in sorted(all_cookies, key=lambda c: rank(c["domain"])):
            self._cookies.setdefault(c["name"], c["value"])
        cookie_names = list(self._cookies.keys())
        logger.info(
            f"Extracted {len(self._cookies)} cookies from browser: {cookie_names}"
        )

    async def _on_request(self, request) -> None:
        """Intercept outgoing requests to aisandbox-pa.googleapis.com and Flow RPCs.

        Captures the Authorization header (any scheme) and browser fingerprint
        headers from API requests. Logs each distinct API path / RPC id once so
        the log shows how the current Flow frontend talks to the backend.
        """
        url = request.url
        try:
            if self._API_HOST in url:
                await self._on_api_request(request)
            elif "/batchexecute" in url and any(h in url for h in self._LABS_HOSTS):
                self._log_rpc_ids(url)
                if not self._browser_headers_ready.is_set():
                    await self._capture_fingerprint(request, "batchexecute")
        except Exception as e:
            logger.debug(f"Request interception skipped: {e}")

    def _log_rpc_ids(self, url: str) -> None:
        match = re.search(r"[?&]rpcids=([^&]+)", url)
        if not match:
            return
        for rpc_id in match.group(1).split(","):
            if rpc_id and rpc_id not in self._seen_rpc_ids:
                self._seen_rpc_ids.add(rpc_id)
                logger.debug(f"Flow frontend RPC observed: rpcids={rpc_id}")

    async def _on_api_request(self, request) -> None:
        headers = await request.all_headers()
        path = request.url.split(self._API_HOST, 1)[1].split("?")[0]
        auth = headers.get("authorization", "")
        scheme = auth_scheme(auth)

        if path not in self._seen_api_paths:
            self._seen_api_paths.add(path)
            logger.info(
                f"API request observed: {request.method} {path} "
                f"(auth={scheme or 'none'}, api-key={'yes' if headers.get('x-goog-api-key') else 'no'})"
            )

        if auth and (scheme == SCHEME_BEARER or self._authorization is None):
            self._publish_authorization(auth, source=f"page request {path}")

        validation = headers.get("x-browser-validation", "")
        if validation and validation != self._browser_headers.get("x-browser-validation"):
            self._browser_headers["x-browser-validation"] = validation
            logger.debug(f"x-browser-validation updated: {validation[:20]}...")

        extra = {k: v for k, v in headers.items() if k in self._API_EXTRA_HEADERS}
        if extra:
            self._browser_headers.update(extra)

        if not self._browser_headers_ready.is_set():
            await self._capture_fingerprint(request, path, headers)

    async def _capture_fingerprint(self, request, source: str, headers: dict | None = None) -> None:
        headers = headers if headers is not None else await request.all_headers()
        extracted = {k: v for k, v in headers.items() if k in self._FINGERPRINT_HEADERS}
        if not extracted:
            return
        first_capture = not bool(self._browser_headers)
        self._browser_headers.update(extracted)
        if "x-goog-api-key" in self._browser_headers:
            self._browser_headers_ready.set()
        if first_capture:
            logger.success(
                f"Browser headers intercepted from {source} ({len(extracted)} keys): "
                f"user-agent={extracted.get('user-agent', '?')!r}, sec-ch-ua={extracted.get('sec-ch-ua', '?')!r}"
            )

    def _register_request_listener(self, page) -> None:
        """Register bearer token request listener on page if not already registered."""
        page_id = id(page)
        if page_id in self._listener_pages:
            return
        page.on("request", self._on_request)
        self._listener_pages.add(page_id)

    async def _close_page(self, page) -> None:
        """Close a tab, ignoring errors from an already-dead page."""
        self._listener_pages.discard(id(page))
        try:
            if not page.is_closed():
                await page.close()
        except Exception as e:
            logger.debug(f"Failed to close tab: {e}")

    def _stale_labs_pages(self, context, keep, retire=None) -> list:
        """Tabs safe to retire: the ones this producer owns or left behind.

        Matches labs/flow tabs, blank popups the page spawned, and any tab this
        producer explicitly replaced. Foreign tabs of the Dolphin profile (the
        Google account tab, anything the operator opened) are left alone.
        """
        stale = []
        for page in list(context.pages):
            if page is keep or page.is_closed():
                continue
            if (
                page is retire
                or page is self._page
                or page.url in ("", "about:blank")
                or any(h in page.url for h in self._LABS_HOSTS)
            ):
                stale.append(page)
        return stale

    async def _close_stale_labs_pages(self, context, keep, retire=None) -> None:
        """Close leftover labs.google tabs so they don't pile up across reloads/reconnects.

        `keep` must be a live page — it is never closed, so the browser is never
        left without tabs.
        """
        stale = self._stale_labs_pages(context, keep, retire)
        if not stale:
            return
        for page in stale:
            await self._close_page(page)
        logger.info(
            f"Closed {len(stale)} stale tab(s), {len(context.pages)} tab(s) left in profile"
        )

    async def _connect_with_retry(self, p, retries: int = 5, delay: float = 3.0):
        """Connect to Chrome via CDP, retrying if it's not yet ready."""
        for attempt in range(1, retries + 1):
            try:
                browser = await p.chromium.connect_over_cdp(self._cdp_url)
                logger.info(f"Connected to Chrome on attempt {attempt}")
                return browser
            except Exception as e:
                if attempt == retries:
                    raise
                logger.warning(f"CDP connect attempt {attempt} failed: {e}, retrying in {delay}s...")
                await asyncio.sleep(delay)

    async def _anchor_delay(self, cold_start: bool = False) -> None:
        """Wait after reCAPTCHA ready to avoid suspicious anchor-ms timing.

        cold_start=True uses a longer delay (after fresh Chrome launch or rotation).
        """
        if cold_start:
            delay = random.uniform(6.0, 10.0)
        else:
            delay = random.uniform(1.0, 3.0)
        logger.debug(f"anchor-ms delay: {delay:.2f}s (cold_start={cold_start})")
        await asyncio.sleep(delay)

    async def _find_or_create_labs_page(self, context, retire: Page | None = None) -> Page:
        """Open a working labs.google tab, retiring `retire` and any other labs tabs.

        Retries with increasing delays if proxy is unstable.
        """
        timeout_ms = self._settings.reload_timeout * 1000
        retry_delays = [10.0, 20.0, 40.0, 60.0]

        for attempt in range(len(retry_delays) + 1):
            try:
                page = await self._try_find_or_create_labs_page(context, timeout_ms, retire)
                if attempt > 0:
                    logger.success(f"labs.google tab ready after {attempt} retries")
                return page
            except (asyncio.CancelledError, RecaptchaInterfaceError):
                raise
            except Exception as e:
                if attempt >= len(retry_delays):
                    logger.error(f"All attempts to get labs.google tab failed: {e}")
                    raise
                delay = retry_delays[attempt]
                logger.warning(
                    f"labs.google tab not ready (attempt {attempt + 1}): {e}. "
                    f"Retrying in {delay:.0f}s..."
                )
                await asyncio.sleep(delay)

        raise RuntimeError("Unreachable")

    async def _try_find_or_create_labs_page(
        self, context, timeout_ms: float, retire: Page | None = None
    ) -> Page:
        """Open a fresh flow.google.com tab and close the ones it replaces.

        grecaptcha only initializes on a project page, not on the generic tool page,
        so navigate straight to the project once one exists (this is also the path
        reload-fallback takes); otherwise just load the tool page (initial setup,
        before a project has been created).

        The new tab is opened before the old ones are closed so the browser is never
        left without pages; a tab that fails to load is closed immediately so failed
        attempts don't accumulate.
        """
        url = self._project_url(self._project_id) if self._project_id else self._settings.labs_url
        page = await context.new_page()
        self._register_request_listener(page)
        try:
            await page.goto(url, timeout=timeout_ms)
            if self._project_id:
                await self._wait_for_recaptcha(page, timeout_ms)
        except BaseException:
            await self._close_page(page)
            raise
        await self._close_stale_labs_pages(context, keep=page, retire=retire)
        await self._anchor_delay(cold_start=True)
        logger.info(f"Flow tab ready: {page.url}")
        return page

    def _next_reload_threshold(self) -> int:
        """Pick a random token count before next page reload."""
        return random.randint(
            self._settings.reload_every_min,
            self._settings.reload_every_max,
        )
