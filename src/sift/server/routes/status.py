import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sift.core.migrations import schema_version
from sift.device.tracker import ConnectionState, NoAdbReason
from sift.server.deps import Ctx

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


class StatusResponse(BaseModel):
    app: AppStatus
    device: DeviceStatusModel | None = None  # None when no tracker runs (SIFT_TRACKER=off)
    ssd: None = None  # typed model arrives in M3
    mac_folder: FolderStatus
    jobs: list[dict[str, Any]] = []


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
    )
