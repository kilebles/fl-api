import shutil
from pathlib import Path

from src.accounts.account import AccountInfo
from src.accounts.proxy import ProxyInfo

DOLPHIN_ID_FILE = "dolphin_id.txt"
PROXIES_FILE = "proxies.txt"
DEAD_PROXIES_FILE = "dead_proxies.txt"


class AccountNotFoundError(LookupError):
    """No account directory with the given name."""


class AccountRepository:
    """File-backed storage of accounts: `accounts/account_N/{dolphin_id,proxies,dead_proxies,project_id}.txt`."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def names(self) -> list[str]:
        """All account directory names, sorted."""
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.iterdir() if p.is_dir())

    def get(self, name: str) -> AccountInfo:
        """Load one account by directory name."""
        return AccountInfo.load(self._dir(name))

    def all(self) -> list[AccountInfo]:
        """All accounts, including ones without a dolphin_id yet."""
        return [AccountInfo.load(self._root / name) for name in self.names()]

    def active(self) -> list[AccountInfo]:
        """Accounts that can run: those with a dolphin_id."""
        return [a for a in self.all() if a.dolphin_id]

    def create(self, dolphin_id: str) -> AccountInfo:
        """Create the next `account_N` directory for a Dolphin profile."""
        numbers = []
        for name in self.names():
            suffix = name.removeprefix("account_")
            if suffix.isdigit():
                numbers.append(int(suffix))
        directory = self._root / f"account_{max(numbers, default=0) + 1}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / DOLPHIN_ID_FILE).write_text(dolphin_id.strip())
        return AccountInfo.load(directory)

    def delete(self, name: str) -> None:
        """Remove the account directory with everything in it."""
        shutil.rmtree(self._dir(name))

    def set_dolphin_id(self, name: str, dolphin_id: str) -> None:
        (self._dir(name) / DOLPHIN_ID_FILE).write_text(dolphin_id.strip())

    def proxies(self, name: str) -> list[ProxyInfo]:
        return self.get(name).proxies

    def set_proxies(self, name: str, proxies: list[ProxyInfo]) -> None:
        """Replace proxies.txt with the given list."""
        (self._dir(name) / PROXIES_FILE).write_text("\n".join(p.to_line() for p in proxies))

    def add_proxies(self, name: str, lines: list[str]) -> tuple[list[ProxyInfo], list[str]]:
        """Append `host:port:user:password` lines; returns (added, rejected lines)."""
        added: list[ProxyInfo] = []
        rejected: list[str] = []
        for line in (line.strip() for line in lines):
            if not line:
                continue
            try:
                added.append(ProxyInfo.from_line(line))
            except ValueError:
                rejected.append(line)
        self.set_proxies(name, self.proxies(name) + added)
        return added, rejected

    def remove_proxy(self, name: str, key: str) -> None:
        """Drop a proxy (`host:port`) from proxies.txt."""
        self.set_proxies(name, [p for p in self.proxies(name) if p.key != key])

    def dead_proxies(self, name: str) -> set[str]:
        """`host:port` entries the rotation marked dead."""
        path = self._dir(name) / DEAD_PROXIES_FILE
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}

    def revive_proxy(self, name: str, key: str) -> None:
        """Remove `host:port` from dead_proxies.txt so rotation uses it again."""
        dead = self.dead_proxies(name) - {key}
        path = self._dir(name) / DEAD_PROXIES_FILE
        if dead:
            path.write_text("\n".join(sorted(dead)))
        else:
            path.unlink(missing_ok=True)

    def _dir(self, name: str) -> Path:
        directory = self._root / name
        if not directory.is_dir() or directory.parent != self._root:
            raise AccountNotFoundError(name)
        return directory
