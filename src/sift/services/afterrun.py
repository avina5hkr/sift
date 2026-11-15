"""What happens when a backup job ends: tell the owner, and keep a daily copy of the database.

`JobManager` calls `after_run` on the job's thread with the final snapshot. A dry run copies
nothing, so it is not announced. Each step is on its own: one failing never skips the other,
and nothing here raises.
"""

import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sift.backup.lease import DRIVE_LEASE
from sift.backup.replicator import ssd_writing
from sift.backup.summary import latest_summary
from sift.core.context import AppContext
from sift.core.dbbackup import daily_copy, run_copy
from sift.core.jobs import JobSnapshot, JobState
from sift.services.notify import Notifier, announce

SSD_KEEP = 5  # database copies kept on the SSD
log = logging.getLogger(__name__)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def message_for(snap: JobSnapshot, summary: dict[str, Any] | None) -> tuple[str, str, str]:
    """(title, body, level) for the notification or toast."""
    if snap.state is JobState.failed:
        return "Backup did not finish", snap.error or "Something went wrong.", "error"
    if summary is None:
        title = "Backup stopped" if snap.stopped else "Backup finished"
        return title, "Nothing was copied.", "info"
    new, dup, failed = summary["new_files"], summary["duplicates_skipped"], summary["failed"]
    parts = [
        f"{_plural(new, 'new file', 'new files')} backed up",
        f"{_plural(dup, 'duplicate', 'duplicates')} skipped",
    ]
    if failed:
        parts.append(f"{_plural(failed, 'file', 'files')} failed")
    level = "warning" if failed else "info"
    if summary["outcome"] == "discarded":
        return "Backup ended", "You discarded the scan, so nothing was copied.", "info"
    stopped = summary["outcome"] == "stopped" or snap.stopped
    return ("Backup stopped" if stopped else "Backup finished"), ", ".join(parts) + ".", level


def after_run(
    ctx: AppContext,
    snap: JobSnapshot,
    notifier: Notifier,
    *,
    today: Callable[[], date] = date.today,
    closing: Callable[[], bool] = lambda: False,
) -> None:
    """The daily copy first (so a slow permission prompt never delays it), then the message.

    While Sift is closing nothing is announced: the owner is leaving and quit must not wait.
    """
    if snap.dry_run:
        return
    try:
        daily_copy(ctx.db, ctx.paths.db_backups, today())
    except Exception:
        log.exception("Could not make the daily copy of the database")
    ssd = ctx.ssd.state() if ctx.ssd is not None else None
    if ssd is not None and ssd.connected and ssd.root:
        # A copy beside the backups after every run, so the SSD alone can rebuild. Skipped while
        # another writer holds the drive (a copy stuck inside macOS would hang this one too).
        if not DRIVE_LEASE.acquire("database copy", timeout_s=0):
            log.warning("The SSD is busy; the database copy on it is made after the next run")
        else:
            try:
                with ssd_writing():
                    run_copy(ctx.db, Path(ssd.root) / ".sift", datetime.now(), keep=SSD_KEEP)
            except Exception:
                log.exception("Could not copy the database to the SSD")
            finally:
                DRIVE_LEASE.release()
    if closing():
        return
    try:
        title, body, level = message_for(snap, latest_summary(ctx.db, snap.id))
        announce(ctx.bus, notifier, title, body, level=level, cancelled=closing)
    except Exception:
        log.exception("Could not announce the end of backup %d", snap.id)
