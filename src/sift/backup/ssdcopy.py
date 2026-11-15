"""The Mac → SSD copy that runs alongside a backup's pull (the SSD worker), and its coordinator.

Roles (plan A1, A3):

- The job's own thread is the **coordinator**. It owns `JobControl` and every job-state change.
  It starts the worker before the pull, rings the doorbell after each file the pull records,
  says when the pull has ended, then drains (waits for the worker, pausing and stopping as the
  owner asks) and finally copies SSD → Mac what the Mac lacks.
- The **SSD worker** is one thread that copies Mac → SSD. It never changes the job's state. It
  holds the drive lease for as long as it can write, and releases it itself on its way out, so
  a copy stuck inside macOS keeps the drive busy until its call returns (G8).

The queue is the database: Mac replicas with no SSD replica, read in rowid order after a
cursor, 500 at a time. A file that fails on the SSD is *deferred* for the rest of the run (it
stays pending for the next run). The worker ends when the pull has ended and nothing is left
after its cursor, then makes one bounded final pass from the start (plan A1).

Each file: a unique `.part` in the SSD's `.staging`, copied from the Mac copy with the cache off,
checked against the Mac's recorded hash, then published by `StagedFile.commit` (full flush,
rename without replacing, the read-back with the cache off, folder flush), and only then the row,
written under `commit_lock` unless the worker was abandoned (G1-G5).
"""

import errno
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sift.backup.lease import DRIVE_LEASE, DriveLease
from sift.backup.problems import add_problem
from sift.backup.replicator import (
    Dest,
    PendingRow,
    Replicator,
    SourceCorrupt,
    max_rowid,
    pending_after,
    pending_totals,
    self_heal,
    ssd_writing,
)
from sift.backup.staging import ChecksumMismatch, IncompleteCopy, TargetExists, sweep_staging
from sift.core.clock import Clock, SystemClock
from sift.core.db import Database, DatabaseClosedError

log = logging.getLogger("sift.backup")

BATCH = 500
DOORBELL_S = 2.0
TAIL_PASSES = 3
FAILURES_IN_A_ROW = 20
ABANDON_AFTER_S = 10.0  # a worker not ended this long after Stop is given up on
ACCESS_HINT_S = 3.0  # the first SSD operation not back by then: "check for a macOS prompt"
NOT_ANSWERING_S = 10.0
SPEED_WINDOW_S = 5.0
COMMIT_LOCK_WAIT_S = 2.0  # how long `abandon()` waits for a commit in flight


class SsdCopyStopped(Exception):
    """Raised inside the worker to leave the current file (the stage is discarded)."""


class _Moved(SsdCopyStopped):
    """The drive was found at a new mount mid-file: that file starts again there."""


class SsdCopyEnded(Exception):
    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class WorkerControl(Protocol):
    @property
    def stop_requested(self) -> bool: ...

    @property
    def pause_requested(self) -> bool: ...

    def wait_while_paused(self, timeout_s: float = 1.0) -> bool: ...


def device_of(path: Path) -> int:
    return os.stat(path).st_dev


@dataclass
class SsdProgress:
    """What the SSD copy has done; every field is guarded by `lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    state: str = "starting"
    files_done: int = 0
    bytes_done: int = 0
    known_files: int = 0  # done + still pending, as of the last count
    known_bytes: int = 0
    backlog_files: int = 0  # pending when the run began (earlier runs' files)
    deferred: int = 0
    current: str | None = None
    current_size: int = 0
    current_done: int = 0
    reason: str | None = None
    started: float | None = None  # monotonic, when the first SSD operation began
    tick: float | None = None  # monotonic, the last time an SSD operation returned
    first_op_done: bool = False
    samples: deque[tuple[float, int]] = field(default_factory=deque, repr=False)

    def update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            state = self.state
            if (
                not self.first_op_done
                and self.started is not None
                and now - self.started >= ACCESS_HINT_S
                and state in ("starting", "copying")
            ):
                state = "waiting_access"
            elif (
                state in ("copying", "stopping")  # paused: waiting by choice, not stuck
                and self.tick is not None
                and now - self.tick >= NOT_ANSWERING_S
            ):
                state = "not_answering"
            speed = None
            if len(self.samples) >= 2 and self.samples[-1][0] > self.samples[0][0]:
                (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
                speed = max(0.0, (b1 - b0) / (t1 - t0))
            return {
                "state": state,
                "files_done": self.files_done,
                "bytes_done": self.bytes_done,
                "known_files": max(self.known_files, self.files_done),
                "known_bytes": max(self.known_bytes, self.bytes_done),
                "backlog_files": self.backlog_files,
                "deferred": self.deferred,
                "current": self.current,
                "current_size": self.current_size,
                "current_done": self.current_done,
                "speed_bps": speed,
                "reason": self.reason,
            }

    def add_sample(self, total_bytes: int) -> None:
        now = time.monotonic()
        with self.lock:
            self.tick = now
            self.samples.append((now, total_bytes))
            while len(self.samples) > 2 and self.samples[1][0] <= now - SPEED_WINDOW_S:
                self.samples.popleft()


class SsdWorker:
    """One thread's worth of Mac → SSD copying for one backup run. See the module docstring."""

    def __init__(
        self,
        db: Database,
        mac: Dest,
        ssd: Dest,
        *,
        control: WorkerControl,
        pull_done: threading.Event,
        more: threading.Event,
        locate: Callable[[], Dest | None],
        device: Callable[[Path], int] = device_of,
        clock: Clock | None = None,
        lease: DriveLease = DRIVE_LEASE,
        batch: int = BATCH,
        doorbell_s: float = DOORBELL_S,
    ) -> None:
        self._db = db
        self.mac = mac
        self.ssd = ssd
        self._control = control
        self._pull_done = pull_done
        self._more = more
        self._locate = locate
        self._device = device
        self._clock = clock or SystemClock()
        self._replicator = Replicator(db, clock=self._clock)
        self._lease = lease
        self._batch = batch
        self._doorbell_s = doorbell_s
        self.progress = SsdProgress()
        self.commit_lock = threading.Lock()
        self._handoff = threading.Lock()  # flag flips only; never held across a wait
        self._abandoned = False
        self._ended = False
        self._holds_lease = False
        self._stop = threading.Event()  # the coordinator's own stop (a failed run, say)
        self._dev: int | None = None
        self.deferred: dict[str, str] = {}  # content hash -> reason, for this run only
        self._failures_in_a_row = 0
        self.queries = 0  # pending queries made (tests check the final pass's bound)
        self.end_state = "starting"  # how the worker ended; see `outcome` in the coordinator
        self.reason: str | None = None
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None
        self.copied = 0
        self.bytes = 0

    # -- the coordinator's side ----------------------------------------------------------

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="sift-ssd-copy", daemon=True)
        self.thread.start()

    def request_stop(self) -> None:
        self._stop.set()
        self._more.set()

    @property
    def abandoned(self) -> bool:
        return self._abandoned

    def abandon(self) -> bool:
        """Give up on a worker that does not end. True if it was still running (it now owns the
        lease until its thread ends and is shown as stuck); False if it had just ended.

        Takes `commit_lock` (waiting at most COMMIT_LOCK_WAIT_S), so a commit in flight either
        lands first or never starts (G1). If a slow write holds it longer, the flag is set
        anyway: only that one write, already begun, can still land, and its row is true (G2).
        """
        locked = self.commit_lock.acquire(timeout=COMMIT_LOCK_WAIT_S)
        try:
            with self._handoff:
                if self._ended:
                    return False
                self._abandoned = True
                if self._holds_lease:
                    self._lease.mark_stuck("SSD copy (not answering)")
        finally:
            if locked:
                self.commit_lock.release()
        self._stop.set()
        self._more.set()
        log.warning("The SSD copy did not stop in time; it was given up on")
        return True

    # -- the worker thread ---------------------------------------------------------------

    def _should_stop(self) -> bool:
        return self._abandoned or self._stop.is_set() or self._control.stop_requested

    def run(self) -> None:
        try:
            self._run()
        except SsdCopyStopped:
            self.end_state = "stopped"
        except SsdCopyEnded as gone:
            self.end_state, self.reason = gone.state, gone.reason
        except DatabaseClosedError:
            self._abandoned = True
            self.end_state = "stopped"
        except BaseException as e:
            if self._abandoned:
                self.end_state = "stopped"
            else:
                log.exception("The SSD copy failed")
                self.error = e
                self.end_state, self.reason = "failed", "The SSD copy stopped because of an error."
        finally:
            self.progress.update(state=self.end_state, reason=self.reason, current=None)
            with self._handoff:
                self._ended = True
                if self._holds_lease:
                    self._holds_lease = False
                    self._lease.release()
            try:
                self._db.release_thread()
            except Exception:
                log.debug("release_thread after the SSD copy", exc_info=True)

    def _run(self) -> None:
        if self._lease.stuck:
            raise SsdCopyEnded("busy", "SSD busy: an earlier copy is not answering.")
        self.progress.update(state="waiting_drive")
        got = self._lease.acquire("backup SSD copy", timeout_s=None, should_stop=self._should_stop)
        if not got:
            raise SsdCopyStopped()
        with self._handoff:
            self._holds_lease = True
            if self._abandoned:
                raise SsdCopyStopped()
        # The drive may have changed while the lease was awaited: find it again by its UUID
        # (never by name) before anything is written, so a same-named drive gets nothing.
        found = self._locate()
        if found is None:
            raise SsdCopyEnded("unplugged", "The SSD is not connected.")
        self.ssd = found
        self.progress.update(state="starting", started=time.monotonic())
        self._first_op()
        files, size = pending_totals(self._db, self.mac, self.ssd)
        self.progress.update(
            state="copying", backlog_files=files, known_files=files, known_bytes=size
        )
        self._main_pass()
        self._final_pass()
        self.end_state = "incomplete" if self.deferred else "done"
        if self.deferred:
            self.reason = f"{len(self.deferred)} files could not be copied to the SSD this time."

    def _first_op(self) -> None:
        """The first operations on the drive: they are what a macOS prompt holds up."""
        if not self.ssd.root.parent.is_dir():
            raise SsdCopyEnded("unplugged", "The SSD is not connected.")
        self.ssd.root.mkdir(exist_ok=True)
        self._dev = self._device(self.ssd.root)
        self.ssd.staging.mkdir(exist_ok=True)
        # The calls above are what a macOS prompt holds up: given up on meanwhile, do no more.
        if self._should_stop():
            raise SsdCopyStopped()
        # Unfinished copies an earlier run left (a crash, a quit): only the lease holder, which
        # every writer to this drive is, may clear them (G5).
        sweep_staging(self.ssd.staging)
        self.progress.update(first_op_done=True, tick=time.monotonic())
        if not self._write(lambda: self_heal(self._db, self.ssd)):  # rows: under commit_lock
            raise SsdCopyStopped()

    def _main_pass(self) -> None:
        cursor = 0
        while True:
            self._between_files()
            done = self._pull_done.is_set()  # read before the query: then it saw every commit
            self._more.clear()
            self._check_drive()
            rows = self._query(cursor, None)
            if rows:
                cursor = self._copy_rows(rows)
                self._recount()
                continue
            if done:
                return
            self.progress.update(state="caught_up", current=None)
            self._more.wait(self._doorbell_s)

    def _final_pass(self) -> None:
        """One pass from the start up to the highest rowid now, then at most TAIL_PASSES over
        rows above it. Every row is visited once: the cursor moves past each returned row."""
        high = max_rowid(self._db, self.mac)
        self._sweep(0, high)
        for _ in range(TAIL_PASSES):
            newer = max_rowid(self._db, self.mac)
            if newer <= high:
                return
            log.warning("Backup files were recorded during the SSD copy's last pass")
            self._sweep(high, newer)
            high = newer
        if max_rowid(self._db, self.mac) > high:
            self.reason = "Files kept arriving; the rest will be copied next time."
            raise SsdCopyEnded("incomplete", self.reason)

    def _sweep(self, cursor: int, high: int) -> None:
        while True:
            self._between_files()
            self._check_drive()
            rows = self._query(cursor, high)
            if not rows:
                return
            cursor = self._copy_rows(rows)

    def _query(self, cursor: int, high: int | None) -> list[PendingRow]:
        self.queries += 1
        return pending_after(self._db, self.mac, self.ssd, cursor, self._batch, high)

    def _recount(self) -> None:
        files, size = pending_totals(self._db, self.mac, self.ssd)
        with self.progress.lock:
            self.progress.known_files = self.progress.files_done + files
            self.progress.known_bytes = self.progress.bytes_done + size

    def _copy_rows(self, rows: list[PendingRow]) -> int:
        cursor = rows[0].rowid
        for row in rows:
            cursor = row.rowid
            if row.content_hash in self.deferred:
                continue
            self._between_files()
            self._copy(row)
        return cursor

    def _between_files(self) -> None:
        if self._should_stop():
            raise SsdCopyStopped()
        if self._control.pause_requested:
            self.progress.update(state="paused", tick=time.monotonic())
            while self._control.wait_while_paused(1.0):
                self.progress.update(tick=time.monotonic())  # paused, not "not answering"
                if self._should_stop():
                    raise SsdCopyStopped()
            if self._should_stop():
                raise SsdCopyStopped()
            self.progress.update(state="copying", tick=time.monotonic())

    def _between_chunks(self, done: int) -> None:
        with self.progress.lock:
            self.progress.current_done = done
        self.progress.add_sample(self.progress.bytes_done + done)
        if self._should_stop():
            raise SsdCopyStopped()  # the stage is discarded: the Mac copy is the source (P3)
        if self._control.pause_requested:
            self._between_files()

    def _before_publish(self) -> None:
        if self._should_stop():
            raise SsdCopyStopped()
        if self._check_drive():
            raise _Moved()

    def _check_drive(self) -> bool:
        """The drive is found by its UUID, never its name (A1): a renamed drive is followed (True
        when it moved), a different drive at the old name gets nothing (raises)."""
        try:
            if self._dev is not None and self._device(self.ssd.root) == self._dev:
                return False
        except OSError:
            pass
        found = self._locate()
        if found is not None:
            try:
                dev = self._device(found.root)
            except OSError:
                dev = None
            if dev is not None:
                moved = found.root != self.ssd.root
                if moved:
                    log.info("The SSD was renamed or mounted again; following it")
                self.ssd, self._dev = found, dev
                return moved
        try:
            self._device(self.ssd.root.parent)
            other = os.path.ismount(self.ssd.root.parent) or self.ssd.root.parent.is_dir()
        except OSError:
            other = False
        if other and found is None and self.ssd.root.parent.exists():
            raise SsdCopyEnded("other_drive", "A different drive is mounted with this name.")
        raise SsdCopyEnded("unplugged", "The SSD was unplugged.")

    def _write(self, fn: Callable[[], object]) -> bool:
        """Run one database write under `commit_lock`, unless abandoned (G1)."""
        with self.commit_lock:
            if self._abandoned:
                return False
            fn()
            return True

    def _copy(self, row: PendingRow) -> None:
        for _attempt in range(3):
            self._check_drive()  # before anything is made on the drive
            try:
                self._copy_once(row)
                return
            except _Moved:
                continue  # the stage went with the old path: copy again at the new one
        self._fail(row, OSError("The SSD kept moving while this file was copied."))

    def _copy_once(self, row: PendingRow) -> None:
        self.progress.update(
            state="copying",
            current=row.rel_path,
            current_size=row.size,
            current_done=0,
            tick=time.monotonic(),
        )

        def record(digest: str, final: str, size: int) -> None:
            if not self._write(lambda: self._replicator.record(self.ssd, digest, final, size)):
                raise SsdCopyStopped()

        try:
            with ssd_writing():
                _final, reused = self._replicator.copy_one(
                    self.mac,
                    self.ssd,
                    row.content_hash,
                    row.rel_path,
                    row.size,
                    between_chunks=self._between_chunks,
                    before_publish=self._before_publish,
                    record=record,
                )
        except SsdCopyStopped, SsdCopyEnded, DatabaseClosedError, _Moved:
            raise
        except SourceCorrupt as corrupt:
            detail = str(corrupt)
            self._defer(row, "The Mac copy no longer matches its checksum.")
            self._write(lambda: self._mark_corrupt(row, detail))
            return
        except FileNotFoundError:
            self._check_drive()  # unplugged: raises; otherwise the Mac file is gone
            if not self.mac.root.is_dir():  # the Mac folder itself went: its rows stay
                raise SsdCopyEnded("failed", "The Mac backup folder is not there.") from None
            self._defer(row, "The file is missing from the Mac backup folder.")
            self._write(lambda: self._drop_mac_row(row))
            return
        except OSError as e:
            self._check_drive()
            if e.errno == errno.ENOSPC:
                raise SsdCopyEnded("full", "The SSD is full.") from e
            self._fail(row, e)
            return
        except (IncompleteCopy, ChecksumMismatch, TargetExists) as e:
            self._check_drive()
            self._fail(row, e)
            return
        self._failures_in_a_row = 0
        self.copied += 0 if reused else 1
        self.bytes += 0 if reused else row.size
        with self.progress.lock:
            self.progress.files_done += 1
            self.progress.bytes_done += row.size
            self.progress.current = None
        self.progress.add_sample(self.progress.bytes_done)

    def _fail(self, row: PendingRow, e: BaseException) -> None:
        log.warning("Could not copy %r to the SSD: %s", row.rel_path, e)
        self._defer(row, "The copy to the SSD did not succeed; it will be tried next time.")
        self._failures_in_a_row += 1
        if self._failures_in_a_row >= FAILURES_IN_A_ROW:
            raise SsdCopyEnded("failed", "The SSD is not accepting writes.")

    def _defer(self, row: PendingRow, reason: str) -> None:
        self.deferred[row.content_hash] = reason
        with self.progress.lock:
            self.progress.deferred = len(self.deferred)
            self.progress.current = None

    def _mark_corrupt(self, row: PendingRow, detail: str) -> None:
        add_problem(
            self._db,
            self._clock,
            "hash_mismatch",
            self.mac.id,
            row.rel_path,
            row.content_hash,
            detail,
        )
        with self._db.tx() as conn:  # this copy is not a verified replica any more
            conn.execute(
                "DELETE FROM replica WHERE dest_id = ? AND content_hash = ?",
                (self.mac.id, row.content_hash),
            )

    def _drop_mac_row(self, row: PendingRow) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "DELETE FROM replica WHERE dest_id = ? AND content_hash = ?",
                (self.mac.id, row.content_hash),
            )


class CoordinatorControl(WorkerControl, Protocol):
    def checkpoint(self) -> None: ...

    def refresh(self) -> None: ...

    def set_ssd_source(self, source: Callable[[], dict[str, Any]] | None) -> None: ...


@dataclass
class SsdOutcome:
    """How the SSD part of a run ended, as `run_summary` stores it."""

    state: str = "none"  # none, complete, incomplete, stopped, failed
    copied: int = 0
    bytes: int = 0
    deferred: int = 0
    pending_after: int | None = None
    reason: str | None = None
    finished_at: int | None = None


class SsdCopy:
    """The coordinator's side of the SSD copy (the job's own thread calls every method)."""

    def __init__(
        self,
        db: Database,
        control: CoordinatorControl,
        *,
        clock: Clock | None = None,
        locate: Callable[[], Dest | None],
        device: Callable[[Path], int] = device_of,
        lease: DriveLease = DRIVE_LEASE,
        abandon_after_s: float = ABANDON_AFTER_S,
        doorbell_s: float = DOORBELL_S,
        batch: int = BATCH,
    ) -> None:
        self._db = db
        self._control = control
        self._clock = clock or SystemClock()
        self._locate = locate
        self._device = device
        self._lease = lease
        self._abandon_after_s = abandon_after_s
        self._doorbell_s = doorbell_s
        self._batch = batch
        self.pull_done = threading.Event()
        self.more = threading.Event()
        self.worker: SsdWorker | None = None
        self.not_connected = False
        self.mac: Dest | None = None

    def start(self, mac: Dest, ssd: Dest | None) -> None:
        """Start the worker before the pull. With no SSD plugged in, the run notes it."""
        self.mac = mac
        if ssd is None:
            self.not_connected = True
            return
        self.worker = SsdWorker(
            self._db,
            mac,
            ssd,
            control=self._control,
            pull_done=self.pull_done,
            more=self.more,
            locate=self._locate,
            device=self._device,
            clock=self._clock,
            lease=self._lease,
            batch=self._batch,
            doorbell_s=self._doorbell_s,
        )
        self._control.set_ssd_source(self.worker.progress.snapshot)
        self.worker.start()

    def ring(self) -> None:
        """The pull recorded a file: wake the worker if it is waiting."""
        self.more.set()

    def pull_finished(self) -> None:
        self.pull_done.set()
        self.more.set()

    def running(self) -> bool:
        w = self.worker
        return w is not None and w.thread is not None and w.thread.is_alive() and not w.abandoned

    def drain(self) -> None:
        """Wait for the worker after the pull. Pause lands here (`checkpoint` blocks); Stop
        raises `JobStopped` from `checkpoint`, and the caller's `close()` ends the worker."""
        while self.running():
            self._control.checkpoint()
            self._control.refresh()
            assert self.worker is not None and self.worker.thread is not None
            self.worker.thread.join(0.5)

    def close(self) -> None:
        """However the run ends: the worker is stopped, and given up on if it does not end
        within `abandon_after_s` (it then keeps the drive lease until its own thread ends)."""
        self.pull_finished()
        w = self.worker
        if w is None or w.thread is None:
            return
        if w.thread.is_alive():
            w.request_stop()
            w.thread.join(self._abandon_after_s)
            if w.thread.is_alive():
                w.abandon()

    def outcome(self, *, stopped: bool, pending_after: int | None) -> SsdOutcome:
        out = SsdOutcome(pending_after=pending_after, finished_at=self._clock.now())
        w = self.worker
        if w is None:
            if self.not_connected:
                out.state, out.reason = "incomplete", "The SSD was not connected."
            return out
        out.copied, out.bytes, out.deferred = w.copied, w.bytes, len(w.deferred)
        out.reason = w.reason
        if w.abandoned or w.end_state == "stopped" or stopped:
            out.state = "stopped"
            if w.abandoned:
                out.reason = "The SSD did not answer, so its copy was stopped."
        elif w.end_state == "failed":
            out.state = "failed"
        elif w.end_state == "done" and not pending_after:
            out.state = "complete"
        else:
            out.state = "incomplete"
        return out
