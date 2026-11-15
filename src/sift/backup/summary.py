"""What a backup run did, and which files failed (the run summary row and the failures list).

`record_summary` writes one `run_summary` row when a run ends. The failures list and Retry work
on `phone_file` rows in state `failed` of one phone (the one connected); both show a phone path
only in its display form (`%XX` for a byte that is not UTF-8), never the stored NUL-escaped text.
"""

import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from sift.backup.phonepaths import from_db, shown
from sift.backup.puller import PullStats
from sift.backup.ssdcopy import SsdOutcome
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
    discarded: bool = False,
    started_at: int,
    finished_at: int,
    stats: PullStats,
    warnings: list[str],
    pull_finished_at: int | None = None,
    ssd: SsdOutcome | None = None,
) -> None:
    """Save the run's counts. A failure is logged, never raised: it must not end the job.

    A discarded run is stored as outcome `stopped` with its flag set (see migration 0004).
    """
    ssd = ssd or SsdOutcome()
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_summary (job_id, device_id, sets, outcome, started_at, "
                "finished_at, new_files, duplicates_skipped, failed, moved_links, changed_files, "
                "bytes, warnings, discarded, pull_finished_at, ssd_state, ssd_copied, ssd_bytes, "
                "ssd_deferred, ssd_pending_after, ssd_finished_at, ssd_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    device_id,
                    json.dumps(sets),
                    "stopped" if discarded else outcome,
                    started_at,
                    finished_at,
                    stats.copied,
                    stats.duplicates_skipped,
                    stats.failed,
                    stats.linked,
                    stats.changed,
                    stats.bytes_copied,
                    json.dumps([*warnings, *stats.warnings]),
                    int(discarded),
                    pull_finished_at,
                    ssd.state,
                    ssd.copied,
                    ssd.bytes,
                    ssd.deferred,
                    ssd.pending_after,
                    ssd.finished_at,
                    ssd.reason,
                ),
            )
    except Exception:
        log.exception("Could not save the summary of backup %d", job_id)


_SUMMARY_COLUMNS = (
    "s.job_id, s.sets, s.outcome, s.started_at, s.finished_at, s.new_files, "
    "s.duplicates_skipped, s.failed, s.moved_links, s.changed_files, s.bytes, s.warnings, j.error, "
    "s.discarded, s.pull_finished_at, s.ssd_state, s.ssd_copied, s.ssd_bytes, s.ssd_deferred, "
    "s.ssd_pending_after, s.ssd_finished_at, s.ssd_reason"
)


def _json_list(text: str | None, *, display: bool) -> list[str]:
    """A stored JSON list of strings; anything else reads as empty. With `display`, each item in
    its display form, so a raw phone path stored before every path went through `shown` (a lone
    surrogate that no response can encode) still reads."""
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    items = [str(v) for v in value if isinstance(v, (str, int, float))]
    return [shown(v) for v in items] if display else items


def latest_summary(
    db: Database, job_id: int | None = None, *, device_id: int | None = None
) -> dict[str, Any] | None:
    """The newest run's summary (or that of `job_id`, or the newest of the phone `device_id`),
    or None when there is none."""
    clauses: list[str] = []
    args: list[int] = []
    if job_id is not None:
        clauses.append("s.job_id = ?")
        args.append(job_id)
    if device_id is not None:
        clauses.append("s.device_id = ?")
        args.append(device_id)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
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
        "sets": _json_list(r[1], display=False),
        "outcome": "discarded" if r[13] else r[2],
        "started_at": r[3],
        "finished_at": r[4],
        "duration_s": max(0, r[4] - r[3]),
        "new_files": r[5],
        "duplicates_skipped": r[6],
        "failed": r[7],
        "moved_links": r[8],
        "changed_files": r[9],
        "bytes": r[10],
        "warnings": _json_list(r[11], display=True),
        "error": shown(r[12]) if r[12] else r[12],
        "pull_finished_at": r[14],
        "ssd": {
            "state": r[15],
            "copied": r[16],
            "bytes": r[17],
            "deferred": r[18],
            "pending_after": r[19],
            "finished_at": r[20],
            "reason": r[21],
        },
    }


def failed_files(db: Database, device_id: int) -> list[FailedFile]:
    """Files still on the phone `device_id` whose last copy failed, with the reason in plain
    words."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, remote_path, size, set_name, attempts, error FROM phone_file "
            "WHERE device_id = ? AND state = 'failed' AND present = 1 ORDER BY id",
            (device_id,),
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


def reset_failed(
    db: Database, device_id: int, only_sets: Collection[str]
) -> list[tuple[int, str | None, int, str | None]]:
    """Put failed files of the phone `device_id` in the sets named (ignoring case) back to
    `discovered`. Another phone's failed files are never touched.

    A failed file of any other set (one no longer in the settings) is left as it is, still
    listed as failed: a run would never copy it. Returns (id, error, attempts, set_name) of
    each file reset, so a caller can undo it.
    """
    wanted = {name.casefold() for name in only_sets}
    with db.tx() as conn:
        rows = [
            r
            for r in conn.execute(
                "SELECT id, error, attempts, set_name FROM phone_file "
                "WHERE device_id = ? AND state = 'failed' AND present = 1",
                (device_id,),
            )
            if r[3] is not None and r[3].casefold() in wanted
        ]
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


def summarize_unfinished(db: Database, now: int, message: str) -> int:
    """At start: a backup that never wrote its summary (Sift quit or crashed mid-run) gets one,
    as `stopped`, with its SSD part `stopped` when an SSD was chosen. Only jobs newer than the
    newest summarized one, so a fresh install of this version never invents old runs.
    Returns how many were added."""
    with db.tx() as conn:
        newest = conn.execute("SELECT coalesce(max(job_id), 0) FROM run_summary").fetchone()[0]
        ssd_chosen = (
            conn.execute("SELECT 1 FROM destination WHERE kind = 'ssd' LIMIT 1").fetchone()
            is not None
        )
        rows = conn.execute(
            "SELECT id, params, started_at, finished_at FROM job WHERE type = 'backup' "
            "AND id > ? AND NOT EXISTS (SELECT 1 FROM run_summary s WHERE s.job_id = job.id) "
            "ORDER BY id",
            (newest,),
        ).fetchall()
        added = 0
        for job_id, params, started_at, finished_at in rows:
            try:
                p = json.loads(params or "{}")
            except ValueError:
                p = {}
            if not isinstance(p, dict) or p.get("dry_run"):
                continue
            sets = p.get("sets") if isinstance(p.get("sets"), list) else []
            start = int(started_at or now)
            conn.execute(
                "INSERT INTO run_summary (job_id, sets, outcome, started_at, finished_at, "
                "warnings, ssd_state, ssd_reason) VALUES (?, ?, 'stopped', ?, ?, ?, ?, ?)",
                (
                    job_id,
                    json.dumps(sets),
                    start,
                    int(finished_at or now),
                    json.dumps([message]),
                    "stopped" if ssd_chosen else "none",
                    message if ssd_chosen else None,
                ),
            )
            added += 1
    return added
