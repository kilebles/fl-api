import threading
import time
from collections import deque
from pathlib import Path

import httpx
from loguru import logger

from src.accounts.account import AccountInfo
from src.accounts.proxy import ProxyInfo
from src.dolphin import cooldowns
from src.dolphin.local_api import DolphinLocalApi
from src.dolphin.public_api import DolphinPublicApi

# rotate() is not thread-safe (asyncio.to_thread calls race on the proxy index), and the
# lock is shared by all managers because they drive the same Dolphin app.
_ROTATE_LOCK = threading.Lock()


class DolphinAntyManager:
    """
    Manages one or more accounts for rotation via Dolphin Anty browser profiles.

    All accounts share a single proxy index. After a full rotation cycle through all
    accounts, the proxy index increments so each account uses the next proxy in its list.
    With a single account, rotation restarts the same profile (on the next proxy after a
    full cycle).
    """

    def __init__(self, accounts: list[AccountInfo], public_api: DolphinPublicApi, local_api: DolphinLocalApi) -> None:
        if not accounts:
            raise ValueError("At least one account required")
        for acc in accounts:
            if not acc.dolphin_id:
                raise ValueError(f"Account {acc.name} has no dolphin_id.txt")
        self._accounts: deque[AccountInfo] = deque(accounts)
        self._current: AccountInfo = accounts[0]
        self._current_ws: str | None = None
        self._public_api = public_api
        self._local_api = local_api
        self._proxy_index: int = 0
        self._accounts_rotated: int = 0
        self._was_reset: bool = False
        self._dead_proxies: dict[str, set[str]] = {acc.name: self._load_dead_proxies(acc.directory) for acc in accounts}
        logger.info(
            f"DolphinAntyManager ready: {len(self._accounts)} profiles, "
            f"active: {self._current.name} (dolphin_id={self._current.dolphin_id})"
        )

    @staticmethod
    def _load_dead_proxies(directory: Path) -> set[str]:
        path = directory / "dead_proxies.txt"
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}

    @staticmethod
    def _save_dead_proxies(directory: Path, dead: set[str]) -> None:
        (directory / "dead_proxies.txt").write_text("\n".join(sorted(dead)))

    @property
    def accounts(self) -> list[AccountInfo]:
        return list(self._accounts)

    @property
    def count(self) -> int:
        return len(self._accounts)

    @property
    def current(self) -> AccountInfo:
        """Currently active account."""
        return self._current

    @property
    def current_ws(self) -> str | None:
        """CDP WebSocket endpoint of the currently running profile, if started."""
        return self._current_ws

    def attach(self, ws_url: str) -> None:
        """Reuse a browser that is already running for the current account (skip stop/start)."""
        self._current_ws = ws_url

    def forget_browser(self) -> None:
        """Drop the cached endpoint so the next start_current() really restarts the profile."""
        self._current_ws = None

    @property
    def current_proxy(self) -> ProxyInfo | None:
        acc = self._current
        if not acc.proxies:
            return None
        return acc.proxies[self._proxy_index % len(acc.proxies)]

    @property
    def current_proxy_url(self) -> str | None:
        """HTTP proxy URL for the current profile's active proxy, or None if no proxies."""
        proxy = self.current_proxy
        return proxy.url if proxy else None

    @property
    def current_proxy_key(self) -> str:
        """`host:port` of the current proxy for failure tracking ("no-proxy" without proxies)."""
        proxy = self.current_proxy
        return proxy.key if proxy else "no-proxy"

    def mark_current_on_cooldown(self, max_seconds: float = 7200.0) -> float:
        """Put the current account on exponential cooldown; see cooldowns.mark_cooldown."""
        return cooldowns.mark_cooldown(self._current.dolphin_id, self._current.name, max_seconds)

    def is_on_cooldown(self, acc: AccountInfo) -> bool:
        return cooldowns.cooldown_remaining(acc.dolphin_id) > 0

    def reset_cooldown(self, acc: AccountInfo) -> None:
        cooldowns.reset_cooldown(acc.dolphin_id)

    def all_proxies_dead(self, acc: AccountInfo) -> bool:
        if not acc.proxies:
            return False
        dead = self._dead_proxies.get(acc.name, set())
        return all(p.key in dead for p in acc.proxies)

    def reset_all_dead_proxies(self) -> None:
        """Clear all dead proxy records for every account and delete dead_proxies.txt files."""
        for acc in self._accounts:
            self._dead_proxies[acc.name] = set()
            (acc.directory / "dead_proxies.txt").unlink(missing_ok=True)
        self._proxy_index = 0
        self._was_reset = True
        logger.warning("All dead proxies reset — full cycle restart")

    def pop_was_reset(self) -> bool:
        """Return True if a full reset happened since last call, then clear the flag."""
        val = self._was_reset
        self._was_reset = False
        return val

    @staticmethod
    def _is_proxy_alive(proxy: ProxyInfo) -> bool:
        try:
            resp = httpx.get("http://api.ipify.org", proxy=proxy.url, timeout=6.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _resolve_working_proxy(self, acc: AccountInfo, proxy: ProxyInfo) -> ProxyInfo:
        """Return `proxy` if alive, else fall back to another entry in proxies.txt or Dolphin Cloud."""
        if self._is_proxy_alive(proxy):
            return proxy
        for candidate in acc.proxies:
            if candidate.key != proxy.key and self._is_proxy_alive(candidate):
                logger.warning(f"Proxy {proxy.key} on {acc.name} is unreachable — using fallback {candidate.key}")
                return candidate
        try:
            cloud_proxy = self._public_api.get_profile_proxy(acc.dolphin_id)
        except Exception as e:
            cloud_proxy = None
            logger.debug(f"Could not fetch cloud proxy for {acc.name}: {e}")
        if cloud_proxy and cloud_proxy.key != proxy.key and self._is_proxy_alive(cloud_proxy):
            logger.warning(
                f"Proxy {proxy.key} in {acc.name}/proxies.txt is dead — adopting working proxy "
                f"{cloud_proxy.key} from Dolphin profile {acc.dolphin_id}"
            )
            acc.proxies = [cloud_proxy if p.key == proxy.key else p for p in acc.proxies]
            try:
                (acc.directory / "proxies.txt").write_text("\n".join(p.to_line() for p in acc.proxies) + "\n")
            except Exception as e:
                logger.warning(f"Could not persist updated proxies.txt for {acc.name}: {e}")
            return cloud_proxy
        return proxy

    def _apply_proxy(self, acc: AccountInfo, proxy: ProxyInfo | None = None) -> None:
        """Patch the Dolphin profile proxy to `proxy` (default: the current proxies.txt entry)."""
        if proxy is None:
            if not acc.proxies:
                return
            proxy = acc.proxies[self._proxy_index % len(acc.proxies)]
        proxy = self._resolve_working_proxy(acc, proxy)
        try:
            self._public_api.set_profile_proxy(acc.dolphin_id, proxy)
            logger.bind(event_type="proxy_patched", account=acc.name, proxy=proxy.key).info(
                f"Profile {acc.name} proxy patched → {proxy.key}"
            )
        except Exception as e:
            logger.warning(f"Could not patch proxy for {acc.name}: {e}")

    def start_current(self, attempts: int = 3, retry_delay: float = 5.0) -> str:
        """Stop and start the current profile. Returns CDP WebSocket URL.

        Dolphin answers 500 while a profile is still shutting down, syncing its
        datadir, or when the configured proxy fails Dolphin's pre-start check;
        a failed start is retried (falling back to the next proxy in proxies.txt).
        """
        acc = self._current
        self._apply_proxy(acc)
        self._local_api.stop(acc.dolphin_id)
        for attempt in range(1, attempts + 1):
            try:
                ws = self._local_api.start(acc.dolphin_id)
                break
            except httpx.HTTPStatusError as e:
                if attempt == attempts or e.response.status_code < 500:
                    raise
                logger.bind(event_type="dolphin_500", account=acc.name).warning(
                    f"Dolphin returned {e.response.status_code} starting profile {acc.dolphin_id} "
                    f"(attempt {attempt}/{attempts}) — retrying in {retry_delay:.0f}s"
                )
                if len(acc.proxies) > 1:
                    fallback = acc.proxies[(self._proxy_index + attempt) % len(acc.proxies)]
                    self._apply_proxy(acc, fallback)
                time.sleep(retry_delay)
        self._current_ws = ws
        return ws

    def rotate(self, depth: int = 0) -> tuple[AccountInfo, str]:
        """
        Stop current profile and start the next account's profile.

        Skips accounts where all proxies are dead or that are on cooldown; when every
        account is on cooldown, sleeps until the soonest one recovers. After a full cycle
        through all accounts, increments the proxy index so all accounts advance to the
        next proxy in their list.

        Returns (new_account, cdp_ws_endpoint).
        """
        with _ROTATE_LOCK:
            return self._rotate_locked(depth)

    def _rotate_locked(self, depth: int = 0) -> tuple[AccountInfo, str]:
        outgoing = self._current
        outgoing_proxy = self.current_proxy
        outgoing_proxy_str = outgoing_proxy.key if outgoing_proxy else "none"
        self._accounts.rotate(-1)
        self._current = self._accounts[0]

        skipped = 0
        while (
            self.all_proxies_dead(self._current) or self.is_on_cooldown(self._current)
        ) and skipped < len(self._accounts) - 1:
            reason = "all proxies dead" if self.all_proxies_dead(self._current) else "on cooldown"
            logger.warning(f"Skipping {self._current.name} — {reason}")
            self._accounts.rotate(-1)
            self._current = self._accounts[0]
            skipped += 1

        if self.is_on_cooldown(self._current) and not self.all_proxies_dead(self._current):
            wait = min(
                cooldowns.cooldown_remaining(acc.dolphin_id)
                for acc in self._accounts
                if not self.all_proxies_dead(acc)
            )
            if wait > 0:
                logger.bind(event_type="all_cooldown").warning(
                    f"All accounts on cooldown — sleeping {wait / 60:.1f}m until soonest recovers"
                )
                # Release the lock while sleeping so other managers are not blocked
                _ROTATE_LOCK.release()
                try:
                    time.sleep(wait)
                finally:
                    _ROTATE_LOCK.acquire()
            for _ in range(len(self._accounts)):
                if not self.is_on_cooldown(self._current) and not self.all_proxies_dead(self._current):
                    break
                self._accounts.rotate(-1)
                self._current = self._accounts[0]

        if self.all_proxies_dead(self._current):
            logger.warning("All accounts exhausted — resetting dead proxies and restarting cycle")
            self.reset_all_dead_proxies()
            self._accounts_rotated = 0

        self._accounts_rotated += 1
        if self._accounts_rotated >= len(self._accounts):
            self._accounts_rotated = 0
            self._proxy_index += 1
            new_proxy = self.current_proxy
            logger.success(
                f"Proxy rotated: {outgoing_proxy_str} → {new_proxy.key if new_proxy else 'none'} "
                f"(index → {self._proxy_index})"
            )

        new_proxy = self.current_proxy
        logger.bind(event_type="rotation", account=outgoing.name, proxy=outgoing_proxy_str).success(
            f"Rotating account: {outgoing.name} ({outgoing_proxy_str}) → "
            f"{self._current.name} ({new_proxy.key if new_proxy else 'none'})"
        )

        self._local_api.stop(outgoing.dolphin_id)
        if outgoing.dolphin_id == self._current.dolphin_id:
            logger.info(f"Self-rotation detected for {self._current.name} — waiting 5s before restart")
            time.sleep(5)

        self._apply_proxy(self._current)
        ws = self._start_with_retry(depth)
        self._current_ws = ws
        return self._current, ws

    def _start_with_retry(self, depth: int = 0, attempts: int = 2) -> str:
        """
        Try to start the current profile up to `attempts` times.

        On failure, if the account has several live proxies, cycle through them (marking
        each failing one dead) before giving up; then move on to the next account.
        """
        acc = self._current
        dead = self._dead_proxies.setdefault(acc.name, set())
        live_proxies = [p for p in acc.proxies if p.key not in dead]
        proxy_attempts: list[ProxyInfo | None] = live_proxies if len(live_proxies) > 1 else [None]

        for proxy in proxy_attempts:
            if proxy is not None:
                self._apply_proxy(acc, proxy)
            for attempt in range(1, attempts + 1):
                try:
                    return self._local_api.start(acc.dolphin_id)
                except Exception as e:
                    proxy_key = proxy.key if proxy is not None else None
                    logger.bind(event_type="dolphin_500", account=acc.name, proxy=proxy_key).warning(
                        f"Failed to start profile {acc.dolphin_id} ({acc.name})"
                        f"{f' proxy {proxy_key}' if proxy_key else ''}, attempt {attempt}/{attempts}: {e}"
                    )
                    if attempt < attempts:
                        time.sleep(15)
            if proxy is not None:
                logger.warning(f"Proxy {proxy.key} failed to start {acc.name} — marking dead")
                dead.add(proxy.key)
                self._save_dead_proxies(acc.directory, dead)

        if depth >= len(self._accounts):
            raise RuntimeError(f"All {len(self._accounts)} accounts failed to start — Dolphin Anty may be down")

        logger.bind(event_type="unresponsive", account=acc.name).warning(
            f"Profile {acc.dolphin_id} ({acc.name}) unresponsive — skipping to next account"
        )
        self._accounts.rotate(-1)
        self._current = self._accounts[0]
        self._local_api.stop(acc.dolphin_id)
        return self._start_with_retry(depth + 1, attempts)
