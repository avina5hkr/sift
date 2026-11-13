"""A slow stand-in backup job, for quit-path tests only (SIFT_TEST_SLOW_JOB=1).

It writes small files the way the real job will: under a temporary name, flushed, renamed to a
final name, then a `content` and `replica` row in one transaction. It checks for a stop request
only between files, so stopping always finishes the current file first. The real job (plan
Task 12) replaces it, and the quit-path tests switch to that.
"""

import hashlib
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from sift.core.db import Database

_log = logging.getLogger(__name__)


class SlowJob:
    def __init__(
        self,
        db: Database,
        root: Path,
        *,
        file_size: int = 64 * 1024,
        chunks: int = 16,
        chunk_pause_s: float = 0.01,
        max_files: int = 1000,
    ) -> None:
        self._db = db
        self._root = root
        self._file_size = file_size
        self._chunks = chunks
        self._chunk_pause_s = chunk_pause_s
        self._max_files = max_files
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._files_done = 0

    @property
    def files_done(self) -> int:
        return self._files_done

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sift-test-slow-job", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float) -> bool:
        """Ask the job to stop after the current file and wait; True if it stopped in time."""
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout_s)
        return not self._thread.is_alive()

    def _run(self) -> None:
        staging = self._root / ".staging"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            dest_id = self._destination_id()
            while not self._stop.is_set() and self._files_done < self._max_files:
                self._copy_one(staging, dest_id)
                self._files_done += 1
        except Exception:
            _log.exception("The slow test job failed")
        _log.info("The slow test job stopped after %d files", self._files_done)

    def _destination_id(self) -> int:
        with self._db.tx() as c:
            row = c.execute(
                "SELECT id FROM destination WHERE kind = 'mac' AND root_path = ?",
                (str(self._root),),
            ).fetchone()
            if row is not None:
                return int(row[0])
            cur = c.execute(
                "INSERT INTO destination (kind, root_path, label) VALUES ('mac', ?, ?)",
                (str(self._root), "slow test job"),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def _copy_one(self, staging: Path, dest_id: int) -> None:
        name = f"slow-{uuid.uuid4().hex}.bin"
        part = staging / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        chunk_size = max(1, self._file_size // self._chunks)
        try:
            with part.open("xb") as f:
                for _ in range(self._chunks):
                    chunk = os.urandom(chunk_size)
                    f.write(chunk)
                    digest.update(chunk)
                    time.sleep(self._chunk_pause_s)
                f.flush()
                os.fsync(f.fileno())
            size = part.stat().st_size
            part.rename(self._root / name)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        now = int(time.time())
        with self._db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO content (hash, size, kind, created_at) "
                "VALUES (?, ?, 'other', ?)",
                (digest.hexdigest(), size, now),
            )
            c.execute(
                "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (digest.hexdigest(), dest_id, name, name.casefold(), size, now),
            )
