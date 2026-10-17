from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from src.accounts.proxy import ProxyInfo


class AccountInfo(BaseModel):
    """One Google account: its Dolphin Anty profile, proxies and persisted Flow project."""

    directory: Path
    dolphin_id: str | None = None
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

    @classmethod
    def load(cls, directory: Path) -> "AccountInfo":
        """Load account credentials and proxy list from directory."""
        dolphin_id_path = directory / "dolphin_id.txt"
        dolphin_id = dolphin_id_path.read_text().strip() if dolphin_id_path.exists() else None

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
            proxies=proxies,
            project_id=project_id,
        )
