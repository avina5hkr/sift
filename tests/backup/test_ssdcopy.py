"""The SSD worker beside the pull (plan A1, A3): completion, the bounded final pass, deferred
files, drive identity, Pause and Stop, and the worker-safety guarantees G1-G10."""

import errno
import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sift.backup import ssdcopy, staging
from sift.backup.layout import rel_key
from sift.backup.lease import DriveLease
from sift.backup.replicator import Dest, Replicator
from sift.backup.ssdcopy import SsdCopy, SsdWorker
from sift.core.db import Database


@dataclass
class Control:
    """Stands in for JobControl: the flags the worker reads, and a pause it can wait on."""

    stop: bool = False
    pause: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)
    checkpoints: int = 0
    refreshes: int = 0
    source: Callable[[], dict] | None = None

    @property
    def stop_requested(self) -> bool:
        return self.stop

    @property
    def pause_requested(self) -> bool:
        return self.pause

    def wait_while_paused(self, timeout_s: float = 1.0) -> bool:
        with self.cond:
            if self.pause and not self.stop:
                self.cond.wait(timeout_s)
            return self.pause and not self.stop

    def set(self, *, stop: bool | None = None, pause: bool | None = None) -> None:
        with self.cond:
            if stop is not None:
                self.stop = stop
            if pause is not None:
                self.pause = pause
            self.cond.notify_all()

    def checkpoint(self) -> None:
        self.checkpoints += 1
        from sift.core.jobs import JobStopped

        while self.wait_while_paused(0.05):
            pass
        if self.stop:
            raise JobStopped()

    def refresh(self) -> None:
        self.refreshes += 1

    def set_ssd_source(self, source: Callable[[], dict] | None) -> None:
        self.source = source


@dataclass
class Drives:
    db: Database
    mac: Dest
    ssd: Dest
    n: int = 0

    def add(self, size: int = 64, *, rowid: int | None = None) -> str:
        """One backed-up file on the Mac (file and rows), not yet on the SSD."""
        self.n += 1
        data = hashlib.sha256(f"file-{self.n}".encode()).digest() * (size // 32 + 1)
        data = data[:size]
        digest = hashlib.sha256(data).hexdigest()
        rel = f"Camera/IMG_{self.n:05d}.jpg"
        path = self.mac.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO content (hash, size, kind, created_at) VALUES (?, ?, 'image', 0)",
                (digest, size),
            )
            cols = "content_hash, dest_id, rel_path, rel_key, size, verified_at"
            args: tuple = (digest, self.mac.id, rel, rel_key(rel), size, 0)
            if rowid is not None:
                cols, args = "rowid, " + cols, (rowid, *args)
            marks = ", ".join("?" * len(args))
            conn.execute(f"INSERT INTO replica ({cols}) VALUES ({marks})", args)
        return digest

    def on_ssd(self) -> set[str]:
        with self.db.read() as conn:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT content_hash FROM replica WHERE dest_id = ?", (self.ssd.id,)
                )
            }

    def pending(self) -> int:
        return self.n - len(self.on_ssd())


@pytest.fixture
def drives(db: Database, tmp_path: Path) -> Iterator[Drives]:
    mac_root, ssd_root = tmp_path / "mac", tmp_path / "Volumes" / "SSD" / "PhoneBackup"
    mac_root.mkdir()
    ssd_root.parent.mkdir(parents=True)
    with db.tx() as conn:
        mac_id = conn.execute(
            "INSERT INTO destination (kind, root_path) VALUES ('mac', ?)", (str(mac_root),)
        ).lastrowid
        ssd_id = conn.execute(
            "INSERT INTO destination (kind, root_path, volume_uuid) VALUES ('ssd', ?, 'U')",
            (str(ssd_root),),
        ).lastrowid
    yield Drives(
        db, Dest(int(mac_id or 0), "mac", mac_root), Dest(int(ssd_id or 0), "ssd", ssd_root)
    )


def worker(
    d: Drives,
    control: Control | None = None,
    *,
    lease: DriveLease | None = None,
    pull_done: bool = True,
    locate: Callable[[], Dest | None] | None = None,
    device: Callable[[Path], int] = ssdcopy.device_of,
    batch: int = 500,
) -> SsdWorker:
    done = threading.Event()
    if pull_done:
        done.set()
    return SsdWorker(
        d.db,
        d.mac,
        d.ssd,
        control=control or Control(),
        pull_done=done,
        more=threading.Event(),
        locate=locate or (lambda: d.ssd),
        device=device,
        lease=lease or DriveLease(),
        batch=batch,
        doorbell_s=0.05,
    )


def run_to_end(w: SsdWorker, timeout: float = 20) -> SsdWorker:
    w.start()
    assert w.thread is not None
    w.thread.join(timeout)
    assert not w.thread.is_alive()
    return w


# -- completion -----------------------------------------------------------------------------


def test_a_slow_phone_the_worker_waits_on_the_doorbell_and_ends_after_the_pull(
    drives: Drives,
) -> None:
    w = worker(drives, pull_done=False)
    drives.add()
    w.start()
    for _ in range(5):  # the pull records files one by one, ringing each time
        time.sleep(0.1)
        drives.add()
        w._more.set()
    time.sleep(0.2)
    assert w.thread is not None and w.thread.is_alive()  # caught up, not finished
    assert w.progress.snapshot()["state"] in ("caught_up", "copying")
    w._pull_done.set()
    w._more.set()
    w.thread.join(10)
    assert w.end_state == "done" and drives.pending() == 0 and w.copied == 6


def test_files_from_earlier_runs_are_part_of_this_runs_copy_and_counted(drives: Drives) -> None:
    for _ in range(4):
        drives.add()
    w = run_to_end(worker(drives))
    snap = w.progress.snapshot()
    assert snap["backlog_files"] == 4 and snap["files_done"] == 4 and drives.pending() == 0


def test_the_ssd_copies_are_the_mac_bytes_and_read_back(drives: Drives) -> None:
    drives.add(5 * 1024 * 1024)  # more than one chunk
    run_to_end(worker(drives))
    for rel in ["Camera/IMG_00001.jpg"]:
        assert (drives.ssd.root / rel).read_bytes() == (drives.mac.root / rel).read_bytes()
    assert not list(drives.ssd.staging.glob("*.part"))


# -- the bounded final pass -------------------------------------------------------------------


def test_the_final_pass_visits_every_row_once_within_the_query_bound(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashes = [drives.add(8) for _ in range(1234)]
    w = worker(drives, batch=500)
    for h in hashes[:7]:
        w.deferred[h] = "failed earlier this run"
    visits: list[str] = []
    monkeypatch.setattr(w, "_copy", lambda row: visits.append(row.content_hash))  # stays pending
    w._dev = ssdcopy.device_of(drives.ssd.root.parent)
    drives.ssd.root.mkdir()
    w._dev = ssdcopy.device_of(drives.ssd.root)
    w._final_pass()
    assert len(visits) == 1234 - 7 and len(set(visits)) == len(visits)
    assert w.queries == -(-1234 // 500) + 1  # three full or partial pages, then the empty one


def test_a_row_recorded_below_the_cursor_is_copied_by_the_final_pass(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(5):
        drives.add(rowid=100 + i)
    real = ssdcopy.pending_after
    late: list[str] = []

    def pending_after(db, src, dst, cursor, limit, high=None):  # type: ignore[no-untyped-def]
        rows = real(db, src, dst, cursor, limit, high)
        if not rows and high is None and not late:  # the main pass has just run dry
            late.append(drives.add(rowid=3))  # a reused, lower rowid
        return rows

    monkeypatch.setattr(ssdcopy, "pending_after", pending_after)
    w = run_to_end(worker(drives))
    assert late and late[0] in drives.on_ssd() and w.end_state == "done"


def test_rows_above_the_pass_get_tail_passes_and_endless_arrivals_end_incomplete(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    drives.add()
    real = ssdcopy.max_rowid
    calls = {"n": 0}

    def max_rowid(db, dest):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] >= 2:
            drives.add()  # something keeps recording files during the last pass
        return real(db, dest)

    monkeypatch.setattr(ssdcopy, "max_rowid", max_rowid)
    w = run_to_end(worker(drives))
    assert w.end_state == "incomplete" and w.reason
    assert calls["n"] == 1 + ssdcopy.TAIL_PASSES + 1
    assert drives.n - drives.pending() >= 1 + ssdcopy.TAIL_PASSES - 1


# -- failures -------------------------------------------------------------------------------


def _failing_copy(
    monkeypatch: pytest.MonkeyPatch, fail: Callable[[str], BaseException | None]
) -> None:
    real = Replicator.copy_one

    def copy_one(self, src, dst, digest, rel_path, size, **kw):  # type: ignore[no-untyped-def]
        exc = fail(digest)
        if exc is not None:
            raise exc
        return real(self, src, dst, digest, rel_path, size, **kw)

    monkeypatch.setattr(Replicator, "copy_one", copy_one)


def test_a_file_that_fails_is_deferred_the_run_ends_and_the_next_run_copies_it(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = drives.add()
    drives.add()
    _failing_copy(monkeypatch, lambda h: OSError(errno.EIO, "I/O") if h == bad else None)
    w = run_to_end(worker(drives))
    assert w.end_state == "incomplete" and bad in w.deferred and drives.pending() == 1
    monkeypatch.undo()
    w2 = run_to_end(worker(drives))
    assert w2.end_state == "done" and drives.pending() == 0


def test_twenty_failures_in_a_row_end_the_copy_as_failed(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(25):
        drives.add(8)
    _failing_copy(monkeypatch, lambda h: OSError(errno.EIO, "I/O"))
    w = run_to_end(worker(drives))
    assert w.end_state == "failed" and len(w.deferred) == ssdcopy.FAILURES_IN_A_ROW


def test_a_full_ssd_ends_the_copy_as_full(drives: Drives, monkeypatch: pytest.MonkeyPatch) -> None:
    drives.add()
    _failing_copy(monkeypatch, lambda h: OSError(errno.ENOSPC, "No space"))
    w = run_to_end(worker(drives))
    assert w.end_state == "full" and not w.deferred


def test_a_final_copy_that_reads_back_differently_is_removed_and_never_recorded(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    drives.add()
    monkeypatch.setattr(staging, "_hash_uncached", lambda path: "0" * 64)
    w = run_to_end(worker(drives))
    assert w.end_state == "incomplete" and drives.pending() == 1
    assert not (drives.ssd.root / "Camera" / "IMG_00001.jpg").exists()


# -- drive identity ---------------------------------------------------------------------------


def test_a_renamed_drive_is_followed_by_its_uuid(drives: Drives, tmp_path: Path) -> None:
    for _ in range(6):
        drives.add()
    renamed = tmp_path / "Volumes" / "SSD 1" / "PhoneBackup"
    moved = {"done": False}

    def locate() -> Dest | None:
        return Dest(drives.ssd.id, "ssd", renamed if moved["done"] else drives.ssd.root)

    control = Control()
    w = worker(drives, control, locate=locate, batch=2)
    real_copy = w._copy

    def copy(row):  # type: ignore[no-untyped-def]
        if w.copied == 3 and not moved["done"]:
            os.rename(tmp_path / "Volumes" / "SSD", tmp_path / "Volumes" / "SSD 1")
            moved["done"] = True
        real_copy(row)

    w._copy = copy  # type: ignore[method-assign]
    run_to_end(w)
    assert w.end_state == "done" and drives.pending() == 0
    assert len(list((renamed / "Camera").iterdir())) == 6


def test_a_different_drive_at_the_old_name_gets_nothing(drives: Drives, tmp_path: Path) -> None:
    for _ in range(4):
        drives.add()
    swapped = {"done": False}
    impostor = drives.ssd.root.parent

    def device(path: Path) -> int:
        dev = os.stat(path).st_dev
        return dev + 1 if swapped["done"] and impostor in (path, *path.parents) else dev

    w = worker(drives, locate=lambda: None if swapped["done"] else drives.ssd, device=device)
    real_copy = w._copy

    def copy(row):  # type: ignore[no-untyped-def]
        if w.copied == 2 and not swapped["done"]:
            os.rename(impostor, tmp_path / "away")
            impostor.mkdir()  # another drive mounted under the same name
            swapped["done"] = True
        real_copy(row)

    w._copy = copy  # type: ignore[method-assign]
    run_to_end(w)
    assert w.end_state == "other_drive"
    assert list(impostor.iterdir()) == []


def test_an_unplugged_ssd_ends_the_copy_and_the_files_stay_pending(drives: Drives) -> None:
    drives.add()
    drives.ssd.root.parent.rmdir()
    w = run_to_end(worker(drives, locate=lambda: None))
    assert w.end_state == "unplugged" and drives.pending() == 1


# -- Pause and Stop -------------------------------------------------------------------------


def test_pause_holds_the_worker_and_resume_lets_it_finish(drives: Drives) -> None:
    for _ in range(3):
        drives.add()
    control = Control(pause=True)
    w = worker(drives, control)
    w.start()
    time.sleep(0.3)
    assert w.progress.snapshot()["state"] == "paused" and w.copied == 0
    control.set(pause=False)
    assert w.thread is not None
    w.thread.join(10)
    assert w.end_state == "done" and w.copied == 3


def test_stop_during_a_large_file_discards_the_stage_and_records_nothing(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    drives.add(12 * 1024 * 1024)
    control = Control()
    w = worker(drives, control)
    real = w._between_chunks

    def between(done: int) -> None:
        control.set(stop=True)  # Stop arrives during the first chunk
        real(done)

    w._between_chunks = between  # type: ignore[method-assign]
    run_to_end(w)
    assert w.end_state == "stopped" and drives.pending() == 1
    assert not list(drives.ssd.staging.glob("*.part"))
    assert not (drives.ssd.root / "Camera").exists()


# -- worker safety: abandonment, the lease, commits and shutdown ---------------------------


@pytest.fixture
def blocked_rename(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """The publish blocks inside the OS (a hung drive) until the event is set."""
    go = threading.Event()
    real = staging._rename_no_replace

    def rename(src: Path, dst: Path) -> None:
        go.wait(20)
        real(src, dst)

    monkeypatch.setattr(staging, "_rename_no_replace", rename)
    yield go
    go.set()


def copy_alongside(d: Drives, control: Control, lease: DriveLease, abandon_s: float) -> SsdCopy:
    copy = SsdCopy(
        d.db, control, locate=lambda: d.ssd, lease=lease, abandon_after_s=abandon_s, doorbell_s=0.05
    )
    copy.start(d.mac, d.ssd)
    return copy


def test_a_blocked_publish_is_abandoned_keeps_the_lease_and_leaves_an_orphan_that_heals(
    drives: Drives, blocked_rename: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = drives.add()
    lease = DriveLease()
    control = Control()
    copy = copy_alongside(drives, control, lease, abandon_s=0.2)
    copy.pull_finished()
    time.sleep(0.3)  # the worker is inside the publish now
    control.set(stop=True)
    started = time.monotonic()
    copy.close()
    assert time.monotonic() - started < 3
    w = copy.worker
    assert w is not None and w.abandoned
    assert lease.held() and lease.stuck  # Eject and other writers see the drive busy
    assert not lease.acquire("plug-in", timeout_s=0.1)
    out = copy.outcome(stopped=True, pending_after=1)
    assert out.state == "stopped" and out.reason
    blocked_rename.set()  # the OS call returns after the abandonment
    assert w.thread is not None
    w.thread.join(10)
    assert not lease.held()  # released by the stuck thread itself on its way out
    final = drives.ssd.root / "Camera" / "IMG_00001.jpg"
    assert final.exists() and digest not in drives.on_ssd()  # an orphan: published, no row
    inode = final.stat().st_ino
    monkeypatch.undo()
    w2 = run_to_end(worker(drives))
    assert digest in drives.on_ssd() and final.stat().st_ino == inode  # adopted, not rewritten
    assert w2.copied == 0


@pytest.mark.parametrize("commit_finishes_first", [False, True], ids=["in-flight", "finished"])
def test_abandonment_of_a_commit_in_flight_preserves_the_verified_replica(
    drives: Drives, monkeypatch: pytest.MonkeyPatch, commit_finishes_first: bool
) -> None:
    digest = drives.add()
    entered, release = threading.Event(), threading.Event()
    real = Replicator.record

    def slow_record(self, dst, h, final, size):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(20)
        real(self, dst, h, final, size)

    monkeypatch.setattr(Replicator, "record", slow_record)
    w = worker(drives)
    w.start()
    assert w.thread is not None
    try:
        assert entered.wait(10)  # the worker holds commit_lock inside an already-started write
        if commit_finishes_first:
            release.set()
            w.thread.join(10)
            assert not w.thread.is_alive()
        # Finishing before abandonment is valid too; force both orders instead of racing a timer.
        assert w.abandon() is (not commit_finishes_first)
    finally:
        release.set()
        w.thread.join(10)
    assert not w.thread.is_alive()
    assert w.error is None
    assert digest in drives.on_ssd()  # a write already in flight may finish after abandonment
    final = drives.ssd.root / "Camera" / "IMG_00001.jpg"
    assert hashlib.sha256(final.read_bytes()).hexdigest() == digest


def test_after_abandonment_no_commit_starts(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = drives.add()
    w = worker(drives)
    real_before = w._before_publish

    def before_publish() -> None:
        real_before()
        w.abandon()  # given up on just after the last check, before the row

    w._before_publish = before_publish  # type: ignore[method-assign]
    run_to_end(w)
    assert digest not in drives.on_ssd() and w.end_state == "stopped"


def test_shutdown_stays_bounded_while_the_database_and_commit_locks_are_contended(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another thread holds the database write lock in a slow transaction while the worker waits
    for it inside commit_lock. Abandonment and the database close both stay within bounds, and
    nothing is written after the close."""
    digest = drives.add()
    lease = DriveLease()
    control = Control()
    in_tx, end_tx = threading.Event(), threading.Event()

    def slow_writer() -> None:
        with drives.db.tx():
            in_tx.set()
            end_tx.wait(10)

    real_publish = ssdcopy.SsdWorker._before_publish

    def before_publish(self):  # type: ignore[no-untyped-def]
        real_publish(self)
        threading.Thread(target=slow_writer, daemon=True).start()
        in_tx.wait(5)  # the write lock is now taken: the worker's commit will wait for it

    monkeypatch.setattr(ssdcopy.SsdWorker, "_before_publish", before_publish)
    copy = copy_alongside(drives, control, lease, abandon_s=0.2)
    copy.pull_finished()
    assert in_tx.wait(5)
    time.sleep(0.2)  # the worker now holds commit_lock and waits for the write lock
    control.set(stop=True)
    started = time.monotonic()
    copy.close()  # the jobs step: join, then abandon
    took = time.monotonic() - started
    assert took < 0.2 + ssdcopy.COMMIT_LOCK_WAIT_S + 1.0
    assert copy.worker is not None and copy.worker.abandoned
    threading.Timer(0.5, end_tx.set).start()  # the slow transaction ends later
    started = time.monotonic()
    drives.db.close()  # the db step waits for the transaction in flight, then closes
    assert time.monotonic() - started < 5
    assert copy.worker.thread is not None
    copy.worker.thread.join(5)
    assert not copy.worker.thread.is_alive() and not lease.held()
    drives.db.open()
    try:
        recorded = digest in drives.on_ssd()
        on_disk = (drives.ssd.root / "Camera" / "IMG_00001.jpg").exists()
        assert on_disk or not recorded  # a row, if one landed, describes a real file (G2)
    finally:
        pass


def test_a_waiting_copy_gives_up_at_once_when_the_lease_is_stuck(drives: Drives) -> None:
    drives.add()
    lease = DriveLease()
    lease.acquire("SSD copy", timeout_s=0)
    lease.mark_stuck("SSD copy (not answering)")
    w = run_to_end(worker(drives, lease=lease))
    assert w.end_state == "busy" and drives.pending() == 1
    lease.release()


def test_the_drain_pauses_and_stops_through_the_coordinators_checkpoint(drives: Drives) -> None:
    for _ in range(3):
        drives.add(4 * 1024 * 1024)
    lease, control = DriveLease(), Control()
    copy = copy_alongside(drives, control, lease, abandon_s=5)
    copy.pull_finished()
    control.set(pause=True)
    t = threading.Thread(target=copy.drain)
    t.start()
    time.sleep(0.3)
    assert t.is_alive()  # paused inside checkpoint
    control.set(pause=False)
    t.join(20)
    assert not t.is_alive() and control.checkpoints >= 1
    copy.close()
    assert copy.outcome(stopped=False, pending_after=0).state == "complete"


# -- the review's findings ----------------------------------------------------------------


def test_the_drive_is_found_again_after_the_lease_wait_and_a_same_named_one_gets_nothing(
    drives: Drives,
) -> None:
    drives.add()
    lease = DriveLease()
    lease.acquire("plug-in replication", timeout_s=0)  # the worker has to wait for it
    swapped = {"done": False}
    w = worker(drives, lease=lease, locate=lambda: None if swapped["done"] else drives.ssd)
    w.start()
    time.sleep(0.2)
    swapped["done"] = True  # meanwhile the SSD left and a drive with its name arrived
    lease.release()
    assert w.thread is not None
    w.thread.join(10)
    assert w.end_state == "unplugged" and not drives.ssd.root.exists()


def test_abandoned_while_the_first_operation_is_held_nothing_is_written_after(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    drives.add()
    gone = drives.add()  # an SSD row whose file is missing: self-heal would delete it
    drives.ssd.root.mkdir()
    with drives.db.tx() as conn:
        conn.execute(
            "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
            "VALUES (?, ?, 'x.jpg', 'x.jpg', 1, 0)",
            (gone, drives.ssd.id),
        )
    held = threading.Event()
    real = ssdcopy.device_of

    def device(path: Path) -> int:
        held.wait(10)  # the macOS prompt
        return real(path)

    w = worker(drives, device=device)
    w.start()
    time.sleep(0.2)
    assert w.abandon() is True
    held.set()  # the owner clicks Allow, after Sift gave up
    assert w.thread is not None
    w.thread.join(10)
    assert gone in drives.on_ssd()  # self-heal did not run after the abandonment
    assert not list(drives.ssd.staging.glob("*")) if drives.ssd.staging.exists() else True


def test_a_missing_mac_folder_ends_the_copy_and_keeps_every_mac_row(drives: Drives) -> None:
    for _ in range(3):
        drives.add()
    w = worker(drives)
    real = w._copy_once

    def copy_once(row):  # type: ignore[no-untyped-def]
        if drives.mac.root.exists():
            drives.mac.root.rename(drives.mac.root.with_name("mac-away"))
        real(row)

    w._copy_once = copy_once  # type: ignore[method-assign]
    run_to_end(w)
    assert w.end_state == "failed" and "Mac backup folder" in (w.reason or "")
    with drives.db.read() as conn:
        mac_rows = conn.execute(
            "SELECT count(*) FROM replica WHERE dest_id = ?", (drives.mac.id,)
        ).fetchone()[0]
    assert mac_rows == 3


def test_a_long_pause_reads_paused_not_not_answering(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    drives.add()
    monkeypatch.setattr(ssdcopy, "NOT_ANSWERING_S", 0.3)
    control = Control(pause=True)
    w = worker(drives, control)
    w.start()
    time.sleep(1.5)
    assert w.progress.snapshot()["state"] == "paused"
    control.set(pause=False)
    assert w.thread is not None
    w.thread.join(10)


def test_an_error_in_the_worker_ends_its_copy_as_failed_with_the_reason_kept(
    drives: Drives, monkeypatch: pytest.MonkeyPatch
) -> None:
    drives.add()
    _failing_copy(monkeypatch, lambda h: RuntimeError("boom"))
    w = run_to_end(worker(drives))
    assert w.end_state == "failed" and isinstance(w.error, RuntimeError)
    copy = SsdCopy(drives.db, Control(), locate=lambda: drives.ssd)
    copy.worker = w
    assert copy.outcome(stopped=False, pending_after=1).state == "failed"
