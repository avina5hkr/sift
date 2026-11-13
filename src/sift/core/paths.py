"""Single source of every folder location Sift reads or writes."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    support: Path
    cache: Path

    @classmethod
    def default(cls) -> AppPaths:
        """macOS locations, or `$SIFT_HOME/{support,cache}` when SIFT_HOME is set."""
        override = os.environ.get("SIFT_HOME")
        if override:
            home = Path(override).expanduser()
            return cls(support=home / "support", cache=home / "cache")
        user = Path.home()
        return cls(
            support=user / "Library" / "Application Support" / "Sift",
            cache=user / "Library" / "Caches" / "Sift",
        )

    @property
    def db(self) -> Path:
        return self.support / "state.db"

    @property
    def config(self) -> Path:
        return self.support / "config.toml"

    @property
    def lock(self) -> Path:
        return self.support / "sift.lock"

    @property
    def logs(self) -> Path:
        return self.support / "logs"

    @property
    def models(self) -> Path:
        return self.support / "models"

    @property
    def lexicons(self) -> Path:
        return self.support / "lexicons"

    @property
    def platform_tools(self) -> Path:
        return self.support / "platform-tools"

    @property
    def db_backups(self) -> Path:
        return self.support / "backups"

    @property
    def thumbs(self) -> Path:
        return self.cache / "thumbs"

    def ensure(self) -> None:
        """Create every directory (mkdir -p)."""
        for d in (
            self.support,
            self.cache,
            self.logs,
            self.models,
            self.platform_tools,
            self.db_backups,
            self.thumbs,
        ):
            d.mkdir(parents=True, exist_ok=True)
