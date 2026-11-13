"""Live SSD presence: is the configured backup SSD plugged in, and where is its backup folder.

The SSD is found by its volume UUID (`backup.ssd_volume_uuid`), never by name, and only at a real
mount point (`os.path.ismount`), so a stray empty folder in /Volumes never receives files. A
thread looks at /Volumes every 2 s; `diskutil` runs only when the set of mounts changed. On every
change an `ssd` event is published, and the SSD's `destination` row is created or updated.
Nothing on the volume is written here.
"""

import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sift.core.context import AppContext
from sift.core.proc import Runner, subprocess_runner
from sift.services.volumes import list_external_volumes

logger = logging.getLogger("sift.services")

VOLUMES = Path("/Volumes")
POLL_S = 2.0


@dataclass(frozen=True)
class SsdState:
    configured: bool  # an SSD was chosen in setup
    connected: bool
    name: str | None = None
    mount: str | None = None
    root: str | None = None  # the backup folder on the SSD
    free_bytes: int | None = None
    dest_id: int | None = None

    def as_event(self) -> dict[str, Any]:
        return asdict(self)


def ssd_root(mount: str, mac_root: str) -> Path:
    """Assumption A1: the SSD's backup folder has the same name as the Mac backup folder."""
    name = Path(mac_root).expanduser().name or "PhoneBackup"
    return Path(mount) / name


def ensure_destination(ctx: AppContext, uuid: str, root: Path, label: str) -> int:
    """The `destination` row for this SSD, created on first sight; its root follows the mount."""
    with ctx.db.tx() as conn:
        row = conn.execute(
            "SELECT id, root_path FROM destination WHERE kind = 'ssd' AND volume_uuid = ?", (uuid,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO destination (kind, root_path, volume_uuid, label) "
                "VALUES ('ssd', ?, ?, ?)",
                (str(root), uuid, label),
            )
            return int(cur.lastrowid or 0)
        if row[1] != str(root):
            conn.execute("UPDATE destination SET root_path = ? WHERE id = ?", (str(root), row[0]))
        return int(row[0])


class SsdWatcher:
    def __init__(
        self,
        ctx: AppContext,
        *,
        runner: Runner = subprocess_runner,
        ismount: Callable[[str], bool] = os.path.ismount,
        volumes_dir: Path = VOLUMES,
        interval_s: float = POLL_S,
        on_connect: Callable[[SsdState], None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._runner = runner
        self._ismount = ismount
        self._dir = volumes_dir
        self._interval = interval_s
        self._on_connect = on_connect
        self._lock = threading.Lock()
        self._state = SsdState(configured=bool(ctx.config.backup.ssd_volume_uuid), connected=False)
        self._mounts: frozenset[str] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> SsdState:
        with self._lock:
            state = self._state
        if state.connected and state.mount:
            try:
                free = shutil.disk_usage(state.mount).free
            except OSError:
                free = None
            state = SsdState(**{**asdict(state), "free_bytes": free})
        return state

    def _mount_points(self) -> frozenset[str]:
        try:
            names = os.listdir(self._dir)
        except OSError:
            return frozenset()
        return frozenset(str(self._dir / n) for n in names if self._ismount(str(self._dir / n)))

    def _look(self) -> SsdState:
        uuid = self._ctx.config.backup.ssd_volume_uuid
        if not uuid:
            return SsdState(configured=False, connected=False)
        for volume in list_external_volumes(self._runner):
            if volume.uuid != uuid or not volume.mount_point:
                continue
            if not self._ismount(volume.mount_point):
                break  # listed but not really mounted: never write into a plain folder
            root = ssd_root(volume.mount_point, self._ctx.config.backup.mac_root)
            dest_id = ensure_destination(self._ctx, uuid, root, volume.name)
            return SsdState(True, True, volume.name, volume.mount_point, str(root), None, dest_id)
        return SsdState(configured=True, connected=False)

    def poll_once(self, *, force: bool = False) -> SsdState:
        """Check now; diskutil runs only when the mounts changed (or `force`). Returns the state."""
        mounts = self._mount_points()
        with self._lock:
            unchanged = mounts == self._mounts and not force
            self._mounts = mounts
            previous = self._state
        if unchanged:
            return self.state()
        state = self._look()
        with self._lock:
            self._state = state
        if (state.connected, state.mount) != (previous.connected, previous.mount):
            logger.info("SSD %s", "connected" if state.connected else "not connected")
            self._ctx.bus.publish("ssd", self.state().as_event())
            if state.connected and self._on_connect is not None:
                try:
                    self._on_connect(state)
                except Exception:
                    logger.exception("Could not start work for the connected SSD")
        return self.state()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    logger.exception("SSD check failed")
                self._stop.wait(self._interval)
        finally:
            self._ctx.db.release_thread()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sift-ssd", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5.0)
