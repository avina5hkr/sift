"""What a backup run did, and which files failed (the run summary row and the failures list).

`record_summary` writes one `run_summary` row when a run ends. The failures list and Retry work
on `phone_file` rows in state `failed`; both show a phone path only in its display form (`%XX`
for a byte that is not UTF-8), never the stored NUL-escaped text.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from sift.backup.phonepaths import from_db, shown
from sift.backup.puller import PullStats
from sift.core.db import Database
from sift.services.plainwords import GENERIC

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailedFile:
    id: int
    path: str  # display form
    size: int
    set_name: str | None
    attempts: int
    reason: str  # plain words


def record_summary(
    db: Database,
    *,
    job_id: int,
    device_id: int | None,
    sets: list[str],
    outcome: str,
    started_at: int,
    finished_at: int,
    stats: PullStats,
    warnings: list[str],
) -> None:
    """Save the run's counts. A failure is logged, never raised: it must not end the job."""
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_summary (job_id, device_id, sets, outcome, started_at, "
                "finished_at, new_files, duplicates_skipped, failed, moved_links, changed_files, "
                "bytes, warnings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    device_id,
                    json.dumps(sets),
                    outcome,
                    started_at,
                    finished_at,
                    stats.copied,
                    stats.duplicates_skipped,
                    stats.failed,
                    stats.linked,
                    stats.changed,
                    stats.bytes_copied,
                    json.dumps([*warnings, *stats.warnings]),
                ),
            )
    except Exception:
        log.exception("Could not save the summary of backup %d", job_id)


_SUMMARY_COLUMNS = (
    "s.job_id, s.sets, s.outcome, s.started_at, s.finished_at, s.new_files, "
    "s.duplicates_skipped, s.failed, s.moved_links, s.changed_files, s.bytes, s.warnings, j.error"
)


def latest_summary(db: Database, job_id: int | None = None) -> dict[str, Any] | None:
    """The newest run's summary (or that of `job_id`), or None when there is none."""
    where, args = ("WHERE s.job_id = ? ", (job_id,)) if job_id is not None else ("", ())
    with db.read() as conn:
        r = conn.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM run_summary s LEFT JOIN job j ON j.id = s.job_id "
            f"{where}ORDER BY s.id DESC LIMIT 1",
            args,
        ).fetchone()
    if r is None:
        return None
    return {
        "job_id": r[0],
        "sets": json.loads(r[1] or "[]"),
        "outcome": r[2],
        "started_at": r[3],
        "finished_at": r[4],
        "duration_s": max(0, r[4] - r[3]),
        "new_files": r[5],
        "duplicates_skipped": r[6],
        "failed": r[7],
        "moved_links": r[8],
        "changed_files": r[9],
        "bytes": r[10],
        "warnings": json.loads(r[11] or "[]"),
        "error": r[12],
    }


def failed_files(db: Database) -> list[FailedFile]:
    """Files still on the phone whose last copy failed, with the reason in plain words."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, remote_path, size, set_name, attempts, error FROM phone_file "
            "WHERE state = 'failed' AND present = 1 ORDER BY id"
        ).fetchall()
    return [
        FailedFile(
            id=int(r[0]),
            path=shown(from_db(r[1])),
            size=int(r[2]),
            set_name=r[3],
            attempts=int(r[4]),
            reason=shown(r[5]) if r[5] else GENERIC,
        )
        for r in rows
    ]


def reset_failed(db: Database) -> list[tuple[int, str | None, int, str | None]]:
    """Put every failed file back to `discovered`, so the next run copies it again.

    Returns (id, error, attempts, set_name) of each, so a caller can undo it.
    """
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id, error, attempts, set_name FROM phone_file "
            "WHERE state = 'failed' AND present = 1"
        ).fetchall()
        conn.executemany(
            "UPDATE phone_file SET state = 'discovered', attempts = 0, error = NULL WHERE id = ?",
            [(r[0],) for r in rows],
        )
    return [(int(r[0]), r[1], int(r[2]), r[3]) for r in rows]


def undo_reset(db: Database, rows: list[tuple[int, str | None, int, str | None]]) -> None:
    """Put files `reset_failed` returned back to failed (a retry that could not start)."""
    with db.tx() as conn:
        conn.executemany(
            "UPDATE phone_file SET state = 'failed', error = ?, attempts = ? "
            "WHERE id = ? AND state = 'discovered'",
            [(r[1], r[2], r[0]) for r in rows],
        )
