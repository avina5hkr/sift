import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sift.backup.lease import DRIVE_LEASE
from sift.backup.outcome import backup_outcome, live_ssd_pending
from sift.backup.replicator import writes_in_progress
from sift.backup.summary import latest_summary
from sift.core.jobs import TERMINAL
from sift.core.migrations import schema_version
from sift.device.tracker import ConnectionState, NoAdbReason
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.services.ssd import eject

logger = logging.getLogger(__name__)
router = APIRouter()


class AppStatus(BaseModel):
    version: str
    schema_version: int
    config_warnings: list[str]


class FolderStatus(BaseModel):
    path: str
    exists: bool
    free_bytes: int | None


class DeviceStatusModel(BaseModel):
    """Mirrors `sift.device.tracker.DeviceStatus`; `state` goes out as its plain string."""

    state: ConnectionState
    message: str
    serial: str | None = None
    model: str | None = None
    android_version: str | None = None
    battery_pct: int | None = None
    free_bytes: int | None = None
    total_bytes: int | None = None
    link_mbps: int | None = None
    usb2_hint: bool = False
    device_uuid: str | None = None
    reason: NoAdbReason | None = None


class SsdStatus(BaseModel):
    configured: bool
    connected: bool
    name: str | None = None
    mount: str | None = None
    root: str | None = None
    free_bytes: int | None = None
    dest_id: int | None = None
    safe_to_unplug: bool = False  # connected and nothing is being written to it


class StatusResponse(BaseModel):
    app: AppStatus
    device: DeviceStatusModel | None = None  # None when no tracker runs (SIFT_TRACKER=off)
    ssd: SsdStatus | None = None  # None when no SSD watcher runs
    mac_folder: FolderStatus
    jobs: list[dict[str, Any]] = []
    backup: dict[str, Any] | None = None  # the last backup's status line (`backup_outcome`)


def _free_bytes(path: Path) -> int | None:
    """Free space on the disk holding `path`, or its nearest existing ancestor. Read-only."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            try:
                return shutil.disk_usage(candidate).free
            except OSError:
                return None
    return None


def _mac_folder(mac_root: str) -> FolderStatus:
    root = Path(mac_root).expanduser()
    return FolderStatus(path=str(root), exists=root.is_dir(), free_bytes=_free_bytes(root))


STUCK_EJECT_MESSAGE = (
    "An earlier copy to the SSD has not finished. Quit and reopen Sift before ejecting."
)


def ssd_busy(ctx: Ctx) -> bool:
    """True while a backup, a long task or a replication may be writing to the SSD.

    That includes a copy the backup gave up on that is still stuck inside macOS: it keeps the
    drive lease until it ends, even after its job has finished.
    """
    if writes_in_progress() or DRIVE_LEASE.held():
        return True
    if (
        ctx.tasks is not None
        and (cur := ctx.tasks.current()) is not None
        and cur.state == "running"
    ):
        return True
    return ctx.jobs is not None and any(
        s.state not in TERMINAL for s in ctx.jobs.snapshot() if not s.dry_run
    )


def _ssd(ctx: Ctx) -> SsdStatus | None:
    if ctx.ssd is None:
        return None
    state = ctx.ssd.state()
    return SsdStatus(**state.as_event(), safe_to_unplug=state.connected and not ssd_busy(ctx))


@router.post("/ssd/eject")
def eject_ssd(ctx: Ctx) -> SsdStatus | None:
    """Eject the backup SSD once nothing is writing to it."""
    state = ctx.ssd.state() if ctx.ssd is not None else None
    if state is None or not state.connected or not state.mount:
        raise ApiError(409, "no_ssd", "The backup SSD is not connected.")
    if DRIVE_LEASE.stuck:
        raise ApiError(409, "ssd_busy", STUCK_EJECT_MESSAGE)
    if ssd_busy(ctx):
        raise ApiError(409, "ssd_busy", "Sift is still writing to the SSD. Wait for it to finish.")
    try:
        eject(state.mount)
    except OSError as e:
        raise ApiError(409, "eject_failed", str(e)) from e
    assert ctx.ssd is not None
    ctx.ssd.poll_once(force=True)
    return _ssd(ctx)


@router.get("/status")
def get_status(ctx: Ctx) -> StatusResponse:
    tracker = ctx.tracker
    return StatusResponse(
        device=DeviceStatusModel(**tracker.status().as_event()) if tracker else None,
        app=AppStatus(
            version=ctx.version,
            schema_version=schema_version(ctx.db),
            config_warnings=ctx.config_warnings,
        ),
        mac_folder=_mac_folder(ctx.config.backup.mac_root),
        ssd=_ssd(ctx),
        backup=_last_backup(ctx),
    )


def _last_backup(ctx: Ctx) -> dict[str, Any] | None:
    try:
        return backup_outcome(
            latest_summary(ctx.db),
            live_ssd_pending(ctx),
            ssd_chosen=bool(ctx.config.backup.ssd_volume_uuid),
        ).as_dict()
    except Exception:
        logger.exception("Could not work out the last backup's status")
        return None
