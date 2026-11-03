from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from src.accounts.proxy import ProxyInfo


class AccountInfo(BaseModel):
    """One Google account: its browser profile ID/UUID, proxies and persisted Flow project."""

    directory: Path
    dolphin_id: str | None = None
    octo_uuid: str | None = None
    proxies: list[ProxyInfo] = []
    project_id: str | None = None

    @property
    def name(self) -> str:
        return self.directory.name

    @property
    def project_id_path(self) -> Path:
        return self.directory / "project_id.txt"

    def save_project_id(self, project_id: str) -> None:
        """Persist the Flow project ID so later sessions reuse it instead of creating a new one."""
        if project_id == self.project_id:
            return
        self.project_id = project_id
        try:
            self.project_id_path.write_text(project_id + "\n")
            logger.info(f"Saved project_id for {self.directory.name}: {project_id}")
        except OSError as e:
            logger.warning(f"Could not persist project_id for {self.directory.name}: {e}")

    def forget_project_id(self) -> None:
        """Drop a persisted project ID that turned out to be unusable."""
        self.project_id = None
        try:
            self.project_id_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Could not remove project_id for {self.directory.name}: {e}")

    def local_credentials(self) -> tuple[str | None, str | None]:
        """Read optional `credentials.txt` (`login:password` or two lines) for auto-relogin."""
        creds_path = self.directory / "credentials.txt"
        if not creds_path.exists():
            return None, None
        lines = [ln.strip() for ln in creds_path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        if not lines:
            return None, None
        if len(lines) >= 2 and ":" not in lines[0]:
            return lines[0], lines[1]
        if ":" in lines[0]:
            login, password = lines[0].split(":", 1)
            return login.strip() or None, password.strip() or None
        return None, None

    @staticmethod
    def _clean_totp_secret(raw: str) -> str | None:
        val = raw.strip()
        if not val:
            return None
        if "/" in val:
            val = val.rstrip("/").rsplit("/", 1)[-1]
        cleaned = "".join(ch for ch in val.upper() if ch.isalnum())
        return cleaned or None

    def local_totp_secret(self) -> str | None:
        """Read optional Base32 2FA TOTP secret from `totp.txt` or line 3 of `credentials.txt`."""
        totp_path = self.directory / "totp.txt"
        if totp_path.exists():
            secret = self._clean_totp_secret(totp_path.read_text(encoding="utf-8", errors="ignore"))
            if secret:
                return secret
        creds_path = self.directory / "credentials.txt"
        if not creds_path.exists():
            return None
        lines = [ln.strip() for ln in creds_path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        if len(lines) >= 3 and ":" not in lines[0]:
            return self._clean_totp_secret(lines[2])
        if len(lines) >= 2 and ":" in lines[0]:
            return self._clean_totp_secret(lines[1])
        return None

    def for_provider(self, provider: str = "dolphin") -> "AccountInfo | None":
        """Resolve the active profile identifier for `dolphin` or `octo`."""
        if provider == "octo":
            pid = self.octo_uuid or (self.dolphin_id if self.dolphin_id and not self.dolphin_id.isdigit() else None)
            if not pid:
                return None
            return self.model_copy(update={"dolphin_id": pid})
        if not self.dolphin_id:
            return None
        return self

    @classmethod
    def load(cls, directory: Path) -> "AccountInfo":
        """Load account credentials and proxy list from directory."""
        dolphin_id_path = directory / "dolphin_id.txt"
        dolphin_id = dolphin_id_path.read_text().strip() or None if dolphin_id_path.exists() else None

        octo_uuid_path = directory / "octo_uuid.txt"
        octo_uuid = octo_uuid_path.read_text().strip() or None if octo_uuid_path.exists() else None

        project_id_path = directory / "project_id.txt"
        project_id = project_id_path.read_text().strip() or None if project_id_path.exists() else None

        proxies: list[ProxyInfo] = []
        proxies_path = directory / "proxies.txt"
        if proxies_path.exists():
            for line in proxies_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    proxies.append(ProxyInfo.from_line(line))
                except ValueError as e:
                    logger.warning(f"Skipping malformed proxy line in {proxies_path}: {e}")

        return cls(
            directory=directory,
            dolphin_id=dolphin_id,
            octo_uuid=octo_uuid,
            proxies=proxies,
            project_id=project_id,
        )
