"""The shared application context handed to the API and, later, to the backup engine."""

from dataclasses import dataclass
from pathlib import Path

from sift.core.config import Config
from sift.core.db import Database
from sift.core.events import EventBus
from sift.core.paths import AppPaths


@dataclass
class AppContext:
    paths: AppPaths
    config: Config
    config_warnings: list[str]
    db: Database
    bus: EventBus
    token: str
    allowed_hosts: frozenset[str]
    version: str
    web_dist: Path | None
