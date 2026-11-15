"""The one status line every surface shows for the last backup (plan A2, owner decision 1).

A run's stored `ssd_state` never changes after it ends. The *current* status comes from the
live count of files the SSD still lacks, so it reads "Complete" only once a later run or a
plug-in copy has really copied the rest. An incomplete SSD copy is never called complete and
never called a failure: "Mac complete · SSD: 120 files pending".
"""

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from sift.core.context import AppContext

_CACHE_S = 5.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, int | None]] = {}


@dataclass(frozen=True)
class BackupOutcome:
    level: str  # none, complete, ssd_pending, stopped, failed, discarded
    headline: str
    finished_at: int | None
    ssd_pending: int | None  # files the chosen SSD lacks now (None: no SSD chosen)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _files(n: int) -> str:
    return f"{n:,} file{'' if n == 1 else 's'}"


def backup_outcome(
    summary: dict[str, Any] | None, live_pending: int | None, *, ssd_chosen: bool
) -> BackupOutcome:
    """The status of the last backup `summary` (as `latest_summary` returns it), given how many
    files the SSD lacks now."""
    pending = live_pending if ssd_chosen else None
    if summary is None:
        return BackupOutcome("none", "No backup yet", None, pending)
    at = summary.get("finished_at")
    outcome = summary.get("outcome")
    if outcome == "failed":
        error = summary.get("error")
        return BackupOutcome("failed", f"Failed: {error}" if error else "Failed", at, pending)
    if outcome == "discarded":
        return BackupOutcome("discarded", "Nothing was copied", at, pending)
    if outcome == "stopped":
        text = "Stopped"
        if pending:
            text += f" · SSD: {_files(pending)} pending"
        return BackupOutcome("stopped", text, at, pending)
    if not ssd_chosen:
        return BackupOutcome("complete", "Complete", at, None)
    if pending == 0:
        return BackupOutcome("complete", "Complete: Mac and SSD", at, 0)
    reason = (summary.get("ssd") or {}).get("reason") or ""
    unplugged = "not connected" in reason or "unplugged" in reason
    text = "Mac complete · SSD: " + (_files(pending) if pending else "some files") + " pending"
    if unplugged:
        text += " (drive unplugged)"
    return BackupOutcome("ssd_pending", text, at, pending)


def live_ssd_pending(ctx: AppContext) -> int | None:
    """Files on the Mac the chosen SSD lacks now (all of them if it was never seen); None when no
    SSD is chosen or there is no Mac folder. Cached for a few seconds: status is polled."""
    if not ctx.config.backup.ssd_volume_uuid or not ctx.config.backup.mac_root:
        return None
    key = f"{ctx.db.path}|{ctx.config.backup.ssd_volume_uuid}|{ctx.config.backup.mac_root}"
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_S:
            return hit[1]
    from sift.backup.replicator import Dest
    from sift.backup.run import mac_target, ssd_pending

    target = mac_target(ctx.db, ctx.config)
    value = ssd_pending(ctx, Dest(target.dest_id, "mac", target.root))
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def forget_cached() -> None:
    """After a run or a copy ends, so the next status is fresh."""
    with _cache_lock:
        _cache.clear()
