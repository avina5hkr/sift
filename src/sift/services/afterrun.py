"""What happens when a backup job ends: tell the owner, and keep a daily copy of the database.

`JobManager` calls `after_run` on the job's thread with the final snapshot. A dry run copies
nothing, so it is not announced. Each step is on its own: one failing never skips the other,
and nothing here raises.
"""

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from sift.backup.summary import latest_summary
from sift.core.context import AppContext
from sift.core.dbbackup import daily_copy
from sift.core.jobs import JobSnapshot, JobState
from sift.services.notify import Notifier, announce

log = logging.getLogger(__name__)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def message_for(snap: JobSnapshot, summary: dict[str, Any] | None) -> tuple[str, str, str]:
    """(title, body, level) for the notification or toast."""
    if snap.state is JobState.failed:
        return "Backup did not finish", snap.error or "Something went wrong.", "error"
    if summary is None:
        return "Backup finished", "Nothing was copied.", "info"
    new, dup, failed = summary["new_files"], summary["duplicates_skipped"], summary["failed"]
    parts = [
        f"{_plural(new, 'new file', 'new files')} backed up",
        f"{_plural(dup, 'duplicate', 'duplicates')} skipped",
    ]
    if failed:
        parts.append(f"{_plural(failed, 'file', 'files')} failed")
    title = "Backup stopped" if snap.stopped else "Backup finished"
    return title, ", ".join(parts) + ".", "warning" if failed else "info"


def after_run(
    ctx: AppContext,
    snap: JobSnapshot,
    notifier: Notifier,
    *,
    today: Callable[[], date] = date.today,
) -> None:
    if snap.dry_run:
        return
    try:
        title, body, level = message_for(snap, latest_summary(ctx.db, snap.id))
        announce(ctx.bus, notifier, title, body, level=level)
    except Exception:
        log.exception("Could not announce the end of backup %d", snap.id)
    try:
        daily_copy(ctx.db, ctx.paths.db_backups, today())
    except Exception:
        log.exception("Could not make the daily copy of the database")
