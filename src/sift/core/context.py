"""The shared application context handed to the API and, later, to the backup engine."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sift.core.config import Config
from sift.core.db import Database
from sift.core.events import EventBus
from sift.core.paths import AppPaths

if TYPE_CHECKING:
    from sift.core.jobs import JobManager
    from sift.device.fake_tracker import FakePhoneControl
    from sift.device.tracker import DeviceTracker
    from sift.services.ssd import SsdWatcher
    from sift.services.tasks import TaskRunner


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
    tracker: DeviceTracker | None = None  # the phone's connection state; None until wired
    jobs: JobManager | None = None  # backup jobs; None until wired
    ssd: SsdWatcher | None = None  # the backup SSD's presence; None until wired
    tasks: TaskRunner | None = None  # Verify, Adopt, Rebuild; None until wired
    phone_control: FakePhoneControl | None = None  # test hook only: see routes/testphone.py
    pick_folder: Callable[[str | None], str | None] | None = None  # the window's folder dialog
