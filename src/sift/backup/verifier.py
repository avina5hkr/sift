"""Verify: re-hash every backed-up copy and compare with `content.hash`; Problems and Repair.

A copy that is missing or no longer matches loses its replica row (it is not a verified copy any
more) and is listed under Problems. The file itself is never touched (I1). Repair copies the good
copy from the other destination next to it, through the normal staged sequence; when the damaged
file still holds the name, the repaired copy gets a `_HASH8` name beside it.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sift.backup.problems import add_problem
from sift.backup.replicator import Dest, Replicator, SourceCorrupt, load_dest
from sift.backup.staging import regular_file_sha256
from sift.core.clock import Clock, SystemClock
from sift.core.db import Database

log = logging.getLogger("sift.backup")


class NothingToRepairFrom(Exception):
    """No other destination holds a good copy of this content."""


@dataclass
class VerifyStats:
    checked: int = 0
    ok: int = 0
    mismatched: int = 0
    missing: int = 0
    bytes: int = 0
    skipped: bool = False  # the destination is not mounted


def verify_dest(
    db: Database,
    dest: Dest,
    *,
    clock: Clock | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> VerifyStats:
    """Re-hash every replica of `dest` (F_NOCACHE). Stops between files when asked."""
    clock = clock or SystemClock()
    stats = VerifyStats()
    if not dest.root.is_dir():
        stats.skipped = True
        return stats
    with db.read() as conn:
        rows = conn.execute(
            "SELECT content_hash, rel_path, size FROM replica WHERE dest_id = ? ORDER BY rel_path",
            (dest.id,),
        ).fetchall()
    for digest, rel_path, size in rows:
        if should_stop():
            break
        stats.checked += 1
        try:
            found = regular_file_sha256(dest.root / rel_path)
        except FileNotFoundError:
            found = None
        except OSError as e:  # unreadable now (permissions, a drive error): not proof of loss
            log.warning("Verify could not read %r on the %s: %s", rel_path, dest.kind, e)
            add_problem(db, clock, "unreadable", dest.id, rel_path, digest, str(e))
            continue
        if found == digest:
            stats.ok += 1
            stats.bytes += int(size)
            with db.tx() as conn:
                conn.execute(
                    "UPDATE replica SET verified_at = ? WHERE dest_id = ? AND content_hash = ?",
                    (clock.now(), dest.id, digest),
                )
        else:
            kind = "missing_file" if found is None else "hash_mismatch"
            if found is None:
                stats.missing += 1
            else:
                stats.mismatched += 1
            log.warning(
                "Verify: %r on the %s %s",
                rel_path,
                dest.kind,
                "is missing" if found is None else "does not match its checksum",
            )
            with db.tx() as conn:
                conn.execute(
                    "DELETE FROM replica WHERE dest_id = ? AND content_hash = ?", (dest.id, digest)
                )
            add_problem(
                db,
                clock,
                kind,
                dest.id,
                rel_path,
                digest,
                "The file is missing."
                if found is None
                else "The file's bytes no longer match the checksum taken when it was backed up.",
            )
        if on_file is not None:
            on_file(stats.checked, len(rows))
    return stats


def repair(db: Database, problem_id: int, *, clock: Clock | None = None) -> str:
    """Copy a good copy into the damaged destination; returns the repaired copy's rel_path."""
    clock = clock or SystemClock()
    with db.read() as conn:
        row = conn.execute(
            "SELECT dest_id, rel_path, content_hash FROM problem "
            "WHERE id = ? AND resolved_at IS NULL",
            (problem_id,),
        ).fetchone()
        if row is None:
            raise LookupError("That problem is not open.")
        dest_id, rel_path, digest = row
        good = conn.execute(
            "SELECT dest_id, rel_path, size FROM replica WHERE content_hash = ? AND dest_id != ?",
            (digest, dest_id),
        ).fetchall()
    target = load_dest(db, int(dest_id)) if dest_id is not None else None
    if target is None or not good:
        raise NothingToRepairFrom("No good copy of this file is on another backup drive.")
    replicator = Replicator(db, clock=clock)
    for src_id, src_rel, size in good:
        source = load_dest(db, int(src_id))
        if source is None or not source.root.is_dir():
            continue
        try:
            final, _reused = replicator.copy_one(
                source, target, str(digest), str(src_rel), int(size)
            )
        except SourceCorrupt:
            continue
        with db.tx() as conn:
            conn.execute(
                "UPDATE problem SET resolved_at = ?, detail = IFNULL(detail, '') || ? WHERE id = ?",
                (clock.now(), f" Repaired: a good copy is now at {final}.", problem_id),
            )
        log.info("Repaired %r from the %s copy", rel_path, source.kind)
        return final
    raise NothingToRepairFrom("No good copy of this file could be read from another backup drive.")
