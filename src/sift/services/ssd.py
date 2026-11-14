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


def ssd_root(mount: str, mac_root: str, folder: str = "") -> Path:
    """The SSD's backup folder: the one the owner chose on the drive, else (assumption A1) a
    folder at the top of the drive with the same name as the Mac backup folder."""
    if folder.strip("/"):
        return Path(mount) / folder.strip("/")
    name = Path(mac_root).expanduser().name or "PhoneBackup"
    return Path(mount) / name


def drive_folder(root_path: str) -> str:
    """The folder part of an SSD root, without its mount (/Volumes/<name>/)."""
    parts = Path(root_path).parts
    return (
        str(Path(*parts[3:])) if len(parts) > 3 and parts[1] == "Volumes" else Path(root_path).name
    )


def ensure_destination(
    ctx: AppContext, uuid: str, root: Path, label: str, mount: str | None = None
) -> int:
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
            held = conn.execute(
                "SELECT 1 FROM replica WHERE dest_id = ? LIMIT 1", (row[0],)
            ).fetchone()
            if held is not None and mount is not None:
                # it holds backups: follow a renamed mount, but keep its folder on the drive
                root = Path(mount) / drive_folder(str(row[1]))
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
        self._worker: threading.Thread | None = None  # on_connect runs here, off the poll loop

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
            backup = self._ctx.config.backup
            root = ssd_root(volume.mount_point, backup.mac_root, backup.ssd_folder)
            dest_id = ensure_destination(self._ctx, uuid, root, volume.name, volume.mount_point)
            with self._ctx.db.read() as conn:
                root = Path(
                    conn.execute(
                        "SELECT root_path FROM destination WHERE id = ?", (dest_id,)
                    ).fetchone()[0]
                )
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
                self._start_work(state)
        return self.state()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _start_work(self, state: SsdState) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        on_connect = self._on_connect
        assert on_connect is not None

        def work() -> None:
            try:
                on_connect(state)
            except Exception:
                logger.exception("Could not bring the connected SSD up to date")

        self._worker = threading.Thread(target=work, name="sift-ssd-work", daemon=True)
        self._worker.start()

    def wait_for_work(self, timeout_s: float) -> None:
        if self._worker is not None:
            self._worker.join(timeout_s)

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
        self.wait_for_work(30.0)  # replication stops between files once `stopping` is set


def eject(mount: str, runner: Runner = subprocess_runner) -> None:
    """`diskutil eject <mount>`; raises OSError with plain words when macOS refuses."""
    done = runner(["/usr/sbin/diskutil", "eject", mount], 60.0)
    if done.returncode != 0:
        raise OSError(
            "macOS could not eject the SSD. Close any window or app using it and try again."
        )
