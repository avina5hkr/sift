"""Copying verified content from one backup destination to another (Mac ↔ SSD).

Each destination ends with the union of both: content with a replica on one and not the other is
copied across with the same staged sequence as a pull (I2): `.part` in the destination's
`.staging`, F_FULLFSYNC, rename without replacing, F_NOCACHE read-back, and only then the replica
row. The source bytes are checked against `content.hash` while they are copied, so a corrupt copy
is never spread; it is listed under Problems instead. Nothing is overwritten or deleted (I1): a
file already at the target path is reused if its bytes match, otherwise the copy goes to a
`_HASH8` path. Self-heal first drops replica rows whose files are gone (rows only), so those files
are copied again.
"""

import fcntl
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sift.backup.layout import rel_key, resolve_collision
from sift.backup.problems import add_problem
from sift.backup.staging import (
    ChecksumMismatch,
    IncompleteCopy,
    StagedFile,
    TargetExists,
    make_durable,
    regular_file_sha256,
)
from sift.core.clock import Clock, SystemClock
from sift.core.db import Database

log = logging.getLogger("sift.backup")

CHUNK = 4 * 1024 * 1024
STAGING = ".staging"
NOT_A_FILE = "-"

# One replication at a time, whoever starts it (a backup run or plugging the SSD in).
REPLICATION_LOCK = threading.Lock()


class SourceCorrupt(Exception):
    """The source copy's bytes do not match `content.hash`: it is not copied anywhere."""


@dataclass(frozen=True)
class Dest:
    id: int
    kind: str  # 'mac' or 'ssd'
    root: Path

    @property
    def staging(self) -> Path:
        return self.root / STAGING


@dataclass
class ReplicateStats:
    copied: int = 0
    bytes: int = 0
    reused: int = 0  # an identical file was already there with no row
    healed: int = 0  # rows dropped because their file was gone
    corrupt: int = 0  # the source did not match its hash: listed under Problems
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def load_dest(db: Database, dest_id: int) -> Dest | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT id, kind, root_path FROM destination WHERE id = ?", (dest_id,)
        ).fetchone()
    return None if row is None else Dest(int(row[0]), str(row[1]), Path(row[2]).expanduser())


def self_heal(db: Database, dest: Dest) -> int:
    """Drop replica rows of `dest` whose file is missing or has the wrong size. Rows only.

    Does nothing when the destination root itself is missing (an unmounted drive must never
    look like a drive that lost every file).
    """
    if not dest.root.is_dir():
        return 0
    with db.read() as conn:
        rows = conn.execute(
            "SELECT content_hash, rel_path, size FROM replica WHERE dest_id = ?", (dest.id,)
        ).fetchall()
    gone: list[str] = []
    for content_hash, rel_path, size in rows:
        try:
            if (dest.root / rel_path).stat().st_size == size:
                continue
        except OSError:
            pass
        gone.append(str(content_hash))
    if gone:
        with db.tx() as conn:
            conn.executemany(
                "DELETE FROM replica WHERE dest_id = ? AND content_hash = ?",
                [(dest.id, h) for h in gone],
            )
        log.warning(
            "%d backed-up file(s) are missing from %s; they will be copied again",
            len(gone),
            dest.kind,
        )
    return len(gone)


def pending(db: Database, src: Dest, dst: Dest) -> list[tuple[str, str, int]]:
    """(hash, source rel_path, size) of content on `src` with no replica on `dst`."""
    with db.read() as conn:
        return [
            (str(h), str(p), int(s))
            for h, p, s in conn.execute(
                "SELECT r.content_hash, r.rel_path, r.size FROM replica r WHERE r.dest_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM replica o WHERE o.dest_id = ? "
                "AND o.content_hash = r.content_hash) ORDER BY r.rel_path",
                (src.id, dst.id),
            )
        ]


def _read_uncached(path: Path, staged: StagedFile) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        while chunk := os.read(fd, CHUNK):
            staged.write(chunk)
    finally:
        os.close(fd)


class Replicator:
    def __init__(self, db: Database, *, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def _owner_of(self, dst: Dest, key: str) -> str | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT content_hash FROM replica WHERE dest_id = ? AND rel_key = ?", (dst.id, key)
            ).fetchone()
        return None if row is None else str(row[0])

    def _problem(self, kind: str, dest: Dest, rel_path: str, digest: str, detail: str) -> None:
        add_problem(self._db, self._clock, kind, dest.id, rel_path, digest, detail)

    def _commit(
        self, staged: StagedFile, dst: Dest, base: str, digest: str, mtime: int
    ) -> tuple[str, bool]:
        """Final rel_path on `dst` and whether an identical file already there was reused."""
        on_disk: dict[str, str] = {}

        def owner_of(key: str) -> str | None:
            return on_disk.get(key) or self._owner_of(dst, key)

        for attempt in range(2):
            rel_path = resolve_collision(
                base, hash8=digest[:8], owner_of=owner_of, content_hash=digest
            )
            final = dst.root / rel_path
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                staged.commit(final, mtime=mtime)
                return rel_path, False
            except TargetExists:
                found = regular_file_sha256(final)
                if found == digest:
                    make_durable(final)
                    return rel_path, True
                if attempt == 1:
                    raise
                on_disk[rel_key(rel_path)] = found or NOT_A_FILE
        raise AssertionError("unreachable")

    def copy_one(
        self, src: Dest, dst: Dest, digest: str, rel_path: str, size: int
    ) -> tuple[str, bool]:
        """Copy one replica across. Raises SourceCorrupt when the source is not `digest`."""
        source = src.root / rel_path
        mtime = int(source.stat().st_mtime)
        dst.staging.mkdir(parents=True, exist_ok=True)
        with StagedFile(dst.staging, expected_size=size) as staged:
            _read_uncached(source, staged)
            if staged.size == size and staged.sha256 != digest:
                raise SourceCorrupt(f"{rel_path} on the {src.kind} copy does not match its hash")
            final, reused = self._commit(staged, dst, rel_path, digest, mtime)
        now = self._clock.now()
        with self._db.tx() as conn:
            conn.execute(
                "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (content_hash, dest_id) DO NOTHING",
                (digest, dst.id, final, rel_key(final), size, now),
            )
        return final, reused

    def replicate(
        self,
        src: Dest,
        dst: Dest,
        *,
        should_stop: Callable[[], bool] = lambda: False,
        on_file: Callable[[int, int], None] | None = None,
    ) -> ReplicateStats:
        """Copy everything `src` has and `dst` lacks. Stops between files when asked."""
        stats = ReplicateStats()
        if not src.root.is_dir() or not dst.root.parent.is_dir():
            return stats
        stats.healed = self_heal(self._db, src) + self_heal(self._db, dst)
        dst.root.mkdir(exist_ok=True)
        for digest, rel_path, size in pending(self._db, src, dst):
            if should_stop():
                break
            try:
                _final, reused = self.copy_one(src, dst, digest, rel_path, size)
            except SourceCorrupt as e:
                stats.corrupt += 1
                self._problem("hash_mismatch", src, rel_path, digest, str(e))
                with self._db.tx() as conn:  # this copy is not a verified replica any more
                    conn.execute(
                        "DELETE FROM replica WHERE dest_id = ? AND content_hash = ?",
                        (src.id, digest),
                    )
                continue
            except FileNotFoundError:
                stats.healed += 1
                with self._db.tx() as conn:
                    conn.execute(
                        "DELETE FROM replica WHERE dest_id = ? AND content_hash = ?",
                        (src.id, digest),
                    )
                continue
            except (OSError, IncompleteCopy, ChecksumMismatch, TargetExists) as e:
                stats.failed += 1
                stats.errors.append(f"{rel_path}: {e}")
                log.warning("Could not copy %r to %s: %s", rel_path, dst.kind, e)
                continue
            if reused:
                stats.reused += 1
            else:
                stats.copied += 1
                stats.bytes += size
            if on_file is not None:
                on_file(stats.copied + stats.reused, stats.bytes)
        return stats

    def replicate_union(
        self, a: Dest, b: Dest, *, should_stop: Callable[[], bool] = lambda: False
    ) -> tuple[ReplicateStats, ReplicateStats]:
        """Both directions, so each destination ends with the union of the two."""
        first = self.replicate(a, b, should_stop=should_stop)
        second = self.replicate(b, a, should_stop=should_stop)
        return first, second
