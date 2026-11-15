"""Storage locations (Settings, General; plan A7): where this Sift keeps its data, read-only.

Which data folder a running Sift uses (and whether `SIFT_HOME` overrides it), its settings file
and database, the Mac backup folder and the SSD. Reveal in Finder works only for these known
places; nothing here changes them (Destinations does).
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from sift.backup.outcome import backup_outcome, live_ssd_pending
from sift.backup.summary import latest_summary
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter()


class Place(BaseModel):
    path: str
    exists: bool
    free_bytes: int | None = None


class SsdPlace(BaseModel):
    configured: bool
    connected: bool
    name: str | None
    volume_id: str | None  # shortened
    folder: str | None
    status: str | None  # "SSD: 120 files pending", when it lacks files


class StorageOut(BaseModel):
    data_folder: Place
    overridden: bool  # SIFT_HOME points this Sift at another data folder
    settings_file: Place
    database: Place
    mac_backup: Place | None
    ssd: SsdPlace


def _place(path: Path, *, free: bool = False) -> Place:
    exists = path.exists()
    size = None
    if free and exists:
        try:
            size = shutil.disk_usage(path).free
        except OSError:
            size = None
    return Place(path=str(path), exists=exists, free_bytes=size)


def _ssd(ctx: Ctx) -> SsdPlace:
    uuid = ctx.config.backup.ssd_volume_uuid
    state = ctx.ssd.state() if ctx.ssd is not None else None
    status = None
    if uuid:
        out = backup_outcome(latest_summary(ctx.db), live_ssd_pending(ctx), ssd_chosen=True)
        if out.ssd_pending:
            status = f"SSD: {out.ssd_pending:,} file{'' if out.ssd_pending == 1 else 's'} pending"
    return SsdPlace(
        configured=bool(uuid),
        connected=bool(state and state.connected),
        name=state.name if state and state.connected else None,
        volume_id=uuid[:8] if uuid else None,
        folder=state.root if state and state.connected else None,
        status=status,
    )


@router.get("/storage")
def storage(ctx: Ctx) -> StorageOut:
    paths = ctx.paths
    mac = ctx.config.backup.mac_root
    return StorageOut(
        data_folder=_place(paths.support),
        overridden=bool(os.environ.get("SIFT_HOME")),
        settings_file=_place(paths.config),
        database=_place(paths.db),
        mac_backup=_place(Path(mac).expanduser(), free=True) if mac else None,
        ssd=_ssd(ctx),
    )


class RevealRequest(BaseModel):
    which: Literal["data", "settings", "database", "mac", "ssd"]


@router.post("/storage/reveal")
def reveal(ctx: Ctx, body: RevealRequest) -> dict[str, bool]:
    """Show one of the known places in Finder (a file is selected in its folder)."""
    paths = ctx.paths
    target: Path | None
    if body.which == "data":
        target = paths.support
    elif body.which == "settings":
        target = paths.config
    elif body.which == "database":
        target = paths.db
    elif body.which == "mac":
        target = (
            Path(ctx.config.backup.mac_root).expanduser() if ctx.config.backup.mac_root else None
        )
    else:
        state = ctx.ssd.state() if ctx.ssd is not None else None
        target = Path(state.root) if state and state.connected and state.root else None
    if target is None or not target.is_absolute() or not target.exists():
        raise ApiError(409, "not_there", "That place does not exist yet.")
    if target.suffix.casefold() == ".app":
        raise ApiError(409, "not_there", "Sift will not open a place whose name ends in .app.")
    args = ["open", "-R", "--", str(target)] if target.is_file() else ["open", "--", str(target)]
    try:
        subprocess.run(args, check=True, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise ApiError(500, "open_failed", "Sift could not open Finder there.") from e
    return {"opened": True}
