"""Recovery at startup: undo what a crash or a kill left half done (invariant I5).

Run `recover` once at startup, before the tracker and any job start:

- `.part` files left in each Mac destination's `.staging` folder are deleted (`sweep_staging`);
- rows left `pulling` go back to `discovered`, so the next run copies them;
- `reconcile`: every Mac replica row must have its file, a regular file of the recorded size.
  A row whose file is missing or has another size is deleted, and the phone files that pointed
  at that content go back to `discovered` so the next run copies them again. Nothing on disk is
  touched. A destination whose folder is not there (not mounted) is skipped, rows and all.

Only destinations recorded in the database are looked at: Sift never sweeps a folder it has not
written to.
"""

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from sift.backup.puller import STAGING_DIR
from sift.backup.staging import sweep_staging
from sift.core.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    parts_removed: int
    rows_reset: int
    replicas_dropped: int
    destinations_skipped: int


def _mac_destinations(db: Database) -> list[tuple[int, Path]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, root_path FROM destination WHERE kind = 'mac' ORDER BY id"
        ).fetchall()
    return [(int(r[0]), Path(r[1])) for r in rows]


def reset_pulling(db: Database) -> int:
    """Rows a crash left `pulling` go back to `discovered`; returns how many."""
    with db.tx() as conn:
        cur = conn.execute("UPDATE phone_file SET state = 'discovered' WHERE state = 'pulling'")
        return cur.rowcount


def _intact(path: Path, size: int) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == size


def reconcile(db: Database) -> tuple[int, int]:
    """Drop Mac replica rows whose file is gone or the wrong size; (dropped, skipped dests)."""
    dropped = skipped = 0
    for dest_id, root in _mac_destinations(db):
        if not root.is_dir():
            skipped += 1
            log.info("Backup folder %d is not there; not checked", dest_id)
            continue
        with db.read() as conn:
            replicas = conn.execute(
                "SELECT content_hash, rel_path, size FROM replica WHERE dest_id = ?", (dest_id,)
            ).fetchall()
        gone = [h for h, rel, size in replicas if not _intact(root / rel, int(size))]
        if not gone:
            continue
        log.warning("%d backed-up files are missing or changed; copying them again", len(gone))
        with db.tx() as conn:
            for digest in gone:
                conn.execute(
                    "DELETE FROM replica WHERE content_hash = ? AND dest_id = ?", (digest, dest_id)
                )
                conn.execute(
                    "UPDATE phone_file SET state = 'discovered' WHERE content_hash = ? "
                    "AND state = 'done' "
                    "AND NOT EXISTS (SELECT 1 FROM replica WHERE content_hash = ?)",
                    (digest, digest),
                )
        dropped += len(gone)
    return dropped, skipped


def recover(db: Database) -> RecoveryReport:
    """Everything above, in order. See the module docstring."""
    parts = 0
    for _dest_id, root in _mac_destinations(db):
        if root.is_dir():
            parts += sweep_staging(root / STAGING_DIR)
    reset = reset_pulling(db)
    dropped, skipped = reconcile(db)
    if parts or reset or dropped:
        log.info(
            "Recovery: removed %d unfinished copies, requeued %d files, dropped %d records",
            parts,
            reset,
            dropped,
        )
    return RecoveryReport(parts, reset, dropped, skipped)
