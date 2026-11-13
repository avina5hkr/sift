"""Adopting an existing backup folder in place, and rebuilding the index after a lost database.

The folder is only read: nothing in it is modified, moved, renamed or deleted (I1). Every regular
file is first recorded as a provisional `adopted_file`; it counts as backed up only after its
bytes are hashed (F_NOCACHE) and a `content` + `replica` row with the file's existing relative
path is written (I2). A second copy of content that already has a replica on this destination is
recorded as a `duplicate` and left alone. Symlinks, FIFOs and other non-regular entries are never
opened. Sift's own `.staging` and `.sift` folders are skipped.
"""

import logging
import mimetypes
import os
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sift.backup.layout import rel_key
from sift.backup.puller import content_kind
from sift.backup.replicator import Dest
from sift.backup.staging import regular_file_sha256
from sift.core.clock import Clock, SystemClock
from sift.core.db import Database

log = logging.getLogger("sift.backup")

SKIP_DIRS = frozenset({".staging", ".sift"})
BATCH = 500


@dataclass
class AdoptStats:
    found: int = 0  # regular files seen
    new: int = 0  # recorded as provisional this time
    indexed: int = 0  # hashed and now a replica
    duplicates: int = 0
    unreadable: int = 0
    bytes: int = 0


def _walk(root: Path) -> list[tuple[str, int, int]]:
    """(rel_path, size, mtime) of every regular file under `root`, without following links."""
    found: list[tuple[str, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = Path(dirpath)
        for name in sorted(filenames):
            path = base / name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = path.relative_to(root).as_posix()
            found.append((unicodedata.normalize("NFC", rel), st.st_size, int(st.st_mtime)))
    return found


def record(db: Database, dest: Dest, clock: Clock) -> AdoptStats:
    """Step 1: record every file not already indexed on `dest` as provisional."""
    stats = AdoptStats()
    files = _walk(dest.root)
    stats.found = len(files)
    with db.read() as conn:
        known = {
            str(r[0])
            for r in conn.execute("SELECT rel_key FROM replica WHERE dest_id = ?", (dest.id,))
        } | {
            str(r[0])
            for r in conn.execute("SELECT rel_key FROM adopted_file WHERE dest_id = ?", (dest.id,))
        }
    now = clock.now()
    rows = [
        (dest.id, rel, rel_key(rel), size, mtime, now)
        for rel, size, mtime in files
        if rel_key(rel) not in known
    ]
    for i in range(0, len(rows), BATCH):
        with db.tx() as conn:
            conn.executemany(
                "INSERT INTO adopted_file (dest_id, rel_path, rel_key, size, mtime, found_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (dest_id, rel_key) DO NOTHING",
                rows[i : i + BATCH],
            )
    stats.new = len(rows)
    return stats


def hash_provisional(
    db: Database,
    dest: Dest,
    clock: Clock,
    stats: AdoptStats,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> None:
    """Step 2: hash each provisional file; only then does it become a replica (or a duplicate)."""
    with db.read() as conn:
        todo = conn.execute(
            "SELECT id, rel_path, size FROM adopted_file "
            "WHERE dest_id = ? AND state = 'provisional' ORDER BY rel_path",
            (dest.id,),
        ).fetchall()
    for n, (row_id, rel_path, size) in enumerate(todo, 1):
        if should_stop():
            return
        path = dest.root / rel_path
        try:
            digest = regular_file_sha256(path)
            changed = digest is not None and os.lstat(path).st_size != size
        except OSError:
            digest, changed = None, False
        if digest is None or changed:
            with db.tx() as conn:
                if digest is None:
                    conn.execute(
                        "UPDATE adopted_file SET state = 'unreadable' WHERE id = ?", (row_id,)
                    )
                else:  # it grew or shrank while being read: look again next time
                    conn.execute("DELETE FROM adopted_file WHERE id = ?", (row_id,))
            stats.unreadable += digest is None
            continue
        mime, _ = mimetypes.guess_type(Path(rel_path).name, strict=False)
        now = clock.now()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO content (hash, size, kind, mime, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (hash) DO NOTHING",
                (digest, size, content_kind(rel_path), mime, now),
            )
            has = conn.execute(
                "SELECT 1 FROM replica WHERE dest_id = ? AND content_hash = ?", (dest.id, digest)
            ).fetchone()
            if has:
                state = "duplicate"
                stats.duplicates += 1
            else:
                conn.execute(
                    "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, "
                    "verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (digest, dest.id, rel_path, rel_key(rel_path), size, now),
                )
                state = "indexed"
                stats.indexed += 1
                stats.bytes += int(size)
            conn.execute(
                "UPDATE adopted_file SET hash = ?, state = ? WHERE id = ?", (digest, state, row_id)
            )
        if on_file is not None:
            on_file(n, len(todo))


def adopt(
    db: Database,
    dest: Dest,
    *,
    mode: str = "hash",
    clock: Clock | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> AdoptStats:
    """Index the files already in `dest` without changing any of them. Resumable."""
    clock = clock or SystemClock()
    if mode == "fast":
        log.warning("adopt_mode 'fast' is not built: every file is hashed, exactly as with 'hash'.")
    if not dest.root.is_dir():
        return AdoptStats()
    stats = record(db, dest, clock)
    hash_provisional(db, dest, clock, stats, should_stop=should_stop, on_file=on_file)
    log.info(
        "Adopted the %s folder: %d files, %d indexed, %d duplicates, %d unreadable",
        dest.kind,
        stats.found,
        stats.indexed,
        stats.duplicates,
        stats.unreadable,
    )
    return stats


def provisional_count(db: Database, dest_id: int | None = None) -> int:
    with db.read() as conn:
        if dest_id is None:
            row = conn.execute("SELECT count(*) FROM adopted_file WHERE state = 'provisional'")
        else:
            row = conn.execute(
                "SELECT count(*) FROM adopted_file WHERE state = 'provisional' AND dest_id = ?",
                (dest_id,),
            )
        return int(row.fetchone()[0])
