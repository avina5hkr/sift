"""The puller and recovery: verified copies, dedupe, moved files, collisions, crashes."""

import hashlib
import io
import os
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from sift.backup import puller as puller_module
from sift.backup.layout import mirror_rel_path
from sift.backup.phonepaths import from_db, to_db
from sift.backup.puller import (
    CRASH_POINT,
    Puller,
    PullStats,
    Target,
    content_kind,
    pending_items,
)
from sift.backup.recovery import reconcile, recover
from sift.backup.scanner import scan
from sift.backup.sets import ScanRoot
from sift.backup.skiprules import SkipRules
from sift.backup.staging import IncompleteCopy, TargetExists
from sift.core import crashpoints
from sift.core.clock import ManualClock
from sift.core.db import Database
from sift.core.jobs import CurrentFile, JobStopped
from sift.core.origin import WHATSAPP_MEDIA_ROOTS
from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity
from sift.device.transport import FileChanged, RemoteEntry
from sift.services.plainwords import describe

NOW = 1_800_000_000
OLD = NOW - 86_400
DCIM = ScanRoot("/sdcard/DCIM", "Camera", False, ())
WA = ScanRoot(WHATSAPP_MEDIA_ROOTS[0], "WhatsApp media", False, ())
DOCS = ScanRoot("/sdcard/Documents", "Documents", False, ())
ROOTS = (DCIM, WA, DOCS)
SETS = ("Camera", "WhatsApp media", "Documents")
RULES = SkipRules(
    hidden_folders=(".thumbnails",),
    name_prefixes=(".pending-",),
    name_suffixes=(".tmp",),
    skip_zero_byte=True,
    include_whatsapp_db=False,
    include_whatsapp_statuses=False,
    whatsapp_media_roots=WHATSAPP_MEDIA_ROOTS,
)


class SimulatedCrash(BaseException):
    """A crash: not an Exception, so nothing on the way treats it as a failed file."""


class Control:
    """A single-session stand-in for JobControl: no pause, retries without waiting."""

    def __init__(self, session: DeviceSession) -> None:
        self.session = session
        self.stop_requested = False
        self.pause_requested = False
        self.reports: list[tuple[int, int, int | None, int | None]] = []
        self.transferred: list[int] = []
        self.current_files: list[list[CurrentFile]] = []
        self.on_report: Callable[[Control], None] | None = None
        self.waits = 0

    def checkpoint(self) -> None:
        if self.stop_requested:
            raise JobStopped

    def report(
        self,
        files_done: int,
        bytes_done: int,
        files_total: int | None = None,
        bytes_total: int | None = None,
        *,
        current: str | None = None,
        duplicates_skipped: int = 0,
        failed: int = 0,
        transferred_bytes: int | None = None,
        current_files: Sequence[CurrentFile] = (),
    ) -> None:
        self.current_files.append(list(current_files))
        self.reports.append((files_done, bytes_done, files_total, bytes_total))
        self.transferred.append(bytes_done if transferred_bytes is None else transferred_bytes)
        if self.on_report is not None:
            self.on_report(self)

    def wait_for_phone(self) -> DeviceSession:
        return self.session

    def wait_until(self, done: Callable[[], bool], *, timeout_s: float | None = None) -> bool:
        self.waits += 1
        return done()

    def retry[T](self, fn: Callable[[], T], *, retry_on: tuple[type[BaseException], ...]) -> T:
        for attempt in range(4):
            if self.stop_requested:
                raise JobStopped
            try:
                return fn()
            except JobStopped:
                raise
            except retry_on:
                if attempt == 3:
                    raise
        raise AssertionError("unreachable")


@dataclass
class Rig:
    db: Database
    phone: FakePhone
    transport: FakeTransport
    session: DeviceSession
    clock: ManualClock
    root: Path
    dest_id: int
    layout: str = "mirror"
    control: Control = field(init=False)

    def __post_init__(self) -> None:
        self.control = Control(self.session)

    def add(self, path: str, data: bytes, mtime: int = OLD) -> None:
        self.phone.add_file(path, data, mtime=mtime)

    def scan(self) -> None:
        scan(self.session, self.db, ROOTS, RULES, clock=self.clock, sleep=lambda _s: None)

    def target(self) -> Target:
        return Target(self.dest_id, self.root, "Phone", self.layout)  # type: ignore[arg-type]

    def puller(self, readers: int = 1) -> Puller:
        return Puller(
            self.db,
            self.target(),
            device_id=1,
            clock=self.clock,
            describe=describe,
            readers=readers,
            reconnect_pause_s=0.0,
        )

    def pull(self, readers: int = 1, *, scan_first: bool = True) -> PullStats:
        if scan_first:
            self.scan()
        items = pending_items(self.db, 1, SETS)
        return self.puller(readers).run(self.control, items)


@pytest.fixture
def rig(db: Database, tmp_path: Path) -> Iterator[Rig]:
    clock = ManualClock(now=NOW)
    phone = FakePhone()
    transport = FakeTransport(phone, wall_clock=lambda: clock.now())
    root = tmp_path / "backup"
    with db.tx() as conn:
        conn.execute("INSERT INTO device (id, uuid, serial) VALUES (1, 'dev-1', 'FAKE-0001')")
        cur = conn.execute(
            "INSERT INTO destination (kind, root_path, label) VALUES ('mac', ?, 'Mac')",
            (str(root),),
        )
        dest_id = int(cur.lastrowid or 0)
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    yield Rig(db, phone, transport, session, clock, root, dest_id)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree(root: Path) -> dict[str, bytes]:
    """Every final file under the backup folder (the staging folder left out)."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".staging"]
        for name in filenames:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def staging_files(root: Path) -> list[str]:
    staging = root / ".staging"
    return sorted(p.name for p in staging.iterdir()) if staging.is_dir() else []


def replicas(db: Database) -> dict[str, tuple[str, int]]:
    """rel_path -> (content_hash, size)."""
    with db.read() as conn:
        rows = conn.execute("SELECT rel_path, content_hash, size FROM replica").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def phone_rows(db: Database) -> dict[str, tuple[str, str | None, str | None, str | None]]:
    """remote_path -> (state, content_hash, rel_path, error)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT remote_path, state, content_hash, rel_path, error FROM phone_file"
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def assert_tree_matches_rows(rig: Rig) -> None:
    """Every final file has its replica row with the right hash, and every row its file."""
    files = tree(rig.root)
    rows = replicas(rig.db)
    assert set(files) == set(rows)
    for rel, data in files.items():
        assert rows[rel] == (sha(data), len(data))
    hashes = [sha(d) for d in files.values()]
    assert len(hashes) == len(set(hashes)), "some content is stored twice"
    assert staging_files(rig.root) == []


# -- copying ------------------------------------------------------------------------------------


def test_pull_copies_every_file_verified_and_records_it(rig: Rig) -> None:
    files = {
        "/sdcard/DCIM/Camera/IMG_1.jpg": b"one" * 1000,
        "/sdcard/DCIM/Camera/sub dir/VID_2.mp4": b"two" * 3000,
        f"{WA.path}/WhatsApp Images/IMG-3.jpg": b"three",
        "/sdcard/Documents/notes.txt": b"four",
    }
    for path, data in files.items():
        rig.add(path, data)
    stats = rig.pull()
    assert (stats.files_total, stats.copied, stats.failed) == (4, 4, 0)
    assert stats.bytes_total == sum(len(d) for d in files.values())
    on_disk = tree(rig.root)
    assert on_disk == {
        "Phone/DCIM/Camera/IMG_1.jpg": files["/sdcard/DCIM/Camera/IMG_1.jpg"],
        "Phone/DCIM/Camera/sub dir/VID_2.mp4": files["/sdcard/DCIM/Camera/sub dir/VID_2.mp4"],
        "Phone/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-3.jpg": b"three",
        "Phone/Documents/notes.txt": b"four",
    }
    assert os.stat(rig.root / "Phone/Documents/notes.txt").st_mtime == OLD  # phone mtime kept
    rows = phone_rows(rig.db)
    for path, data in files.items():
        state, digest, rel, error = rows[path]
        assert (state, digest, error) == ("done", sha(data), None)
        assert rel is not None and on_disk[rel] == data
    assert_tree_matches_rows(rig)
    with rig.db.read() as conn:
        kinds = dict(conn.execute("SELECT hash, kind FROM content").fetchall())
    assert kinds[sha(b"three")] == "image" and kinds[sha(b"four")] == "doc"
    assert rig.control.reports[-1] == (4, stats.bytes_total, 4, stats.bytes_total)
    assert rig.transport.slots_in_use == 0
    assert not any(e.mutating for e in rig.transport.journal)  # I7: nothing written to the phone


def test_a_second_run_copies_nothing(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", b"a" * 50)
    rig.pull()
    rig.transport.journal.clear()
    stats = rig.pull()
    assert stats.files_total == 0
    assert [e for e in rig.transport.journal if e.op == "open_read"] == []


def test_parallel_readers_copy_everything_once_within_the_slot_pool(rig: Rig) -> None:
    rig.transport.faults.slow_read_bps = 200_000
    for i in range(12):
        rig.add(f"/sdcard/DCIM/Camera/IMG_{i:02d}.jpg", bytes([i]) * 2000)
    rig.add("/sdcard/DCIM/Camera/copy.jpg", bytes([3]) * 2000)  # a duplicate, pulled alongside
    stats = rig.pull(readers=3)
    assert stats.copied + stats.duplicates_skipped == 13 and stats.failed == 0
    assert stats.duplicates_skipped == 1
    assert_tree_matches_rows(rig)
    assert len(tree(rig.root)) == 12
    assert rig.transport.slots_in_use == 0


def test_a_duplicate_becomes_an_alias(rig: Rig) -> None:
    data = b"same bytes" * 100
    rig.add("/sdcard/DCIM/Camera/IMG_1.jpg", data, mtime=OLD)
    rig.add(f"{WA.path}/WhatsApp Images/IMG-forwarded.jpg", data, mtime=OLD - 50)
    stats = rig.pull()
    assert (stats.copied, stats.duplicates_skipped) == (1, 1)
    assert list(tree(rig.root).values()) == [data]
    rows = phone_rows(rig.db)
    assert rows["/sdcard/DCIM/Camera/IMG_1.jpg"][:2] == ("done", sha(data))
    assert rows[f"{WA.path}/WhatsApp Images/IMG-forwarded.jpg"][:2] == ("done", sha(data))
    assert len(replicas(rig.db)) == 1
    assert_tree_matches_rows(rig)


def test_a_name_that_is_not_utf8_is_backed_up_with_its_bytes(rig: Rig) -> None:
    odd, data = "/sdcard/DCIM/Camera/caf\udce9.jpg", b"one raw byte in the name" * 9
    lookalike = "/sdcard/DCIM/Camera/caf%E9.jpg"  # valid UTF-8, same name on the Mac
    rig.add(odd, data, mtime=OLD + 1)  # newest: pulled first
    rig.add(lookalike, b"another file")
    stats = rig.pull()
    assert (stats.copied, stats.failed) == (2, 0)
    files = tree(rig.root)
    assert files["Phone/DCIM/Camera/caf%E9.jpg"] == data
    assert files[f"Phone/DCIM/Camera/caf%E9_{sha(b'another file')[:8]}.jpg"] == b"another file"
    rows = phone_rows(rig.db)
    assert {from_db(p) for p in rows} == {odd, lookalike}
    assert {r[0] for r in rows.values()} == {"done"}
    assert rows[to_db(odd)][1] == sha(data)
    assert [e.path for e in rig.transport.journal if e.op == "open_read"] == [odd, lookalike]
    assert_tree_matches_rows(rig)
    # Scanned again: the same rows, nothing to copy.
    rig.transport.journal.clear()
    assert rig.pull().files_total == 0
    assert len(phone_rows(rig.db)) == 2
    assert [e for e in rig.transport.journal if e.op == "open_read"] == []


def test_a_name_that_cannot_be_stored_is_left_out_with_a_warning(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/bad\x00name.jpg", b"bad")
    rig.add("/sdcard/DCIM/Camera/fine.jpg", b"fine")
    result = scan(rig.session, rig.db, ROOTS, RULES, clock=rig.clock, sleep=lambda _s: None)
    assert result.state == "applied"
    assert any("bad" in w and "left out" in w for w in result.warnings)
    stats = rig.pull(scan_first=False)
    assert (stats.copied, stats.failed) == (1, 0)
    assert set(phone_rows(rig.db)) == {"/sdcard/DCIM/Camera/fine.jpg"}


def test_moved_file_is_linked_without_a_transfer(rig: Rig) -> None:
    data = b"a photo" * 500
    rig.add("/sdcard/DCIM/Camera/IMG_1.jpg", data, mtime=OLD)
    for i in range(5):  # so the move does not empty the folder (the guard would ask)
        rig.add(f"/sdcard/DCIM/Camera/stay_{i}.jpg", bytes([i]) * 10)
    rig.pull()
    rig.phone.remove("/sdcard/DCIM/Camera/IMG_1.jpg")
    rig.add("/sdcard/DCIM/Camera/Moved/IMG_1.jpg", data, mtime=OLD)
    rig.scan()
    rig.transport.journal.clear()
    stats = rig.pull(scan_first=False)
    assert (stats.linked, stats.copied) == (1, 0)
    ops = [(e.op, e.path) for e in rig.transport.journal]
    assert ("sha256", "/sdcard/DCIM/Camera/Moved/IMG_1.jpg") in ops
    assert not any(op == "open_read" for op, _ in ops)
    assert phone_rows(rig.db)["/sdcard/DCIM/Camera/Moved/IMG_1.jpg"][:2] == ("done", sha(data))
    assert len(tree(rig.root)) == 6
    # Speed and ETA count what crossed the cable: a linked file is done but not transferred.
    assert rig.control.reports[-1][1] == len(data)
    assert rig.control.transferred[-1] == 0
    assert (stats.bytes_copied, stats.bytes_not_transferred) == (0, len(data))


def test_reports_name_the_files_in_flight_in_display_form(rig: Rig) -> None:
    for i in range(6):
        rig.add(f"/sdcard/DCIM/Camera/f_{i}.jpg", bytes([i]) * 3000)
    rig.scan()
    rig.pull(scan_first=False)
    seen = [f for report in rig.control.current_files for f in report]
    assert seen, "no report named a file in flight"
    assert all(len(report) <= 3 for report in rig.control.current_files)
    assert all(f.path.startswith("/sdcard/DCIM/Camera/f_") for f in seen)
    assert all(f.size == 3000 and 0 <= f.done <= 3000 for f in seen)
    assert rig.control.current_files[-1] == []  # nothing in flight at the end


def test_the_callers_stats_object_is_filled_in_and_survives_a_stop(rig: Rig) -> None:
    for i in range(4):
        rig.add(f"/sdcard/DCIM/Camera/s_{i}.jpg", bytes([i]) * 100)
    rig.scan()
    mine = PullStats()

    def stop_after_the_first(control: Control) -> None:
        if control.reports[-1][0] >= 1:
            control.stop_requested = True

    rig.control.on_report = stop_after_the_first
    with pytest.raises(JobStopped):
        rig.puller().run(rig.control, pending_items(rig.db, 1, SETS), mine)
    assert 1 <= mine.copied < 4
    assert mine.bytes_copied == 100 * mine.copied


def test_same_size_and_mtime_but_other_bytes_is_copied(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", b"aaaa", mtime=OLD)
    rig.pull()
    rig.add("/sdcard/DCIM/Camera/b.jpg", b"bbbb", mtime=OLD)  # same size and mtime as a.jpg
    stats = rig.pull()
    assert (stats.linked, stats.copied) == (0, 1)
    assert sorted(tree(rig.root).values()) == [b"aaaa", b"bbbb"]


def test_file_changed_mid_read_goes_back_to_discovered(rig: Rig) -> None:
    path = "/sdcard/DCIM/Camera/growing.mp4"
    rig.add(path, b"x" * 5000)
    rig.transport.faults.size_change_mid_read.add(path)
    stats = rig.pull()
    assert (stats.changed, stats.copied, stats.failed) == (1, 0, 0)
    assert stats.warnings and path in stats.warnings[0]
    state, digest, _rel, error = phone_rows(rig.db)[path]
    assert (state, digest) == ("discovered", None)
    assert error == describe(FileChanged("x", "x"))
    assert tree(rig.root) == {} and staging_files(rig.root) == []


class ShortStreams(FakeTransport):
    """Every read stream ends early while the file on the phone stays as it was."""

    def open_read(
        self, path: str, offset: int = 0, *, entry: RemoteEntry | None = None
    ) -> BinaryIO:
        self.journal_open = getattr(self, "journal_open", 0) + 1
        stream = super().open_read(path, offset, entry=entry)
        data = stream.read(3)
        stream.close()
        return cast(BinaryIO, io.BytesIO(data))


def test_short_read_of_an_unchanged_file_is_retried_then_failed(
    db: Database, tmp_path: Path
) -> None:
    clock = ManualClock(now=NOW)
    phone = FakePhone()
    phone.add_file("/sdcard/DCIM/Camera/a.jpg", b"abcdefgh", mtime=OLD)
    transport = ShortStreams(phone, wall_clock=lambda: clock.now())
    with db.tx() as conn:
        conn.execute("INSERT INTO device (id, uuid, serial) VALUES (1, 'dev-1', 'FAKE-0001')")
        conn.execute(
            "INSERT INTO destination (id, kind, root_path) VALUES (1, 'mac', ?)",
            (str(tmp_path / "b"),),
        )
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    rig = Rig(db, phone, transport, session, clock, tmp_path / "b", 1)
    stats = rig.pull()
    assert stats.failed == 1
    assert transport.journal_open == 4  # the first try and three retries
    state, _digest, _rel, error = phone_rows(db)["/sdcard/DCIM/Camera/a.jpg"]
    assert (state, error) == ("failed", describe(IncompleteCopy("x")))
    assert tree(rig.root) == {} and staging_files(rig.root) == []


def test_disconnect_mid_file_waits_for_the_phone_and_finishes(rig: Rig) -> None:
    for i in range(3):
        rig.add(f"/sdcard/DCIM/Camera/IMG_{i}.jpg", bytes([i]) * 4000)
    rig.transport.faults.disconnect_after_bytes = 5000
    stats = rig.pull()
    assert (stats.copied, stats.failed) == (3, 0)
    assert rig.control.waits >= 1  # it paused before asking for the phone again
    assert_tree_matches_rows(rig)


def test_stop_finishes_the_file_in_flight_and_leaves_nothing_pulling(rig: Rig) -> None:
    rig.transport.faults.slow_read_bps = 100_000
    for i in range(6):
        rig.add(f"/sdcard/DCIM/Camera/IMG_{i}.jpg", bytes([i]) * 3000)

    def stop_after_the_first(control: Control) -> None:
        if control.reports[-1][0] >= 1:
            control.stop_requested = True

    rig.control.on_report = stop_after_the_first
    with pytest.raises(JobStopped):
        rig.pull(readers=2)
    states = [row[0] for row in phone_rows(rig.db).values()]
    assert "pulling" not in states
    assert 1 <= states.count("done") < 6
    assert_tree_matches_rows(rig)


def test_pull_order_is_camera_newest_first_then_whatsapp_then_the_rest(rig: Rig) -> None:
    rig.add("/sdcard/Documents/z.txt", b"doc", mtime=OLD - 10)
    rig.add(f"{WA.path}/WhatsApp Images/w.jpg", b"wa", mtime=OLD - 5)
    rig.add("/sdcard/DCIM/Camera/old.jpg", b"o", mtime=OLD - 300)
    rig.add("/sdcard/DCIM/Camera/new.jpg", b"n", mtime=OLD - 100)
    rig.add("/sdcard/DCIM/Camera/mid.jpg", b"m", mtime=OLD - 200)
    rig.scan()
    order = [i.path for i in pending_items(rig.db, 1, SETS)]
    assert order == [
        "/sdcard/DCIM/Camera/new.jpg",
        "/sdcard/DCIM/Camera/mid.jpg",
        "/sdcard/DCIM/Camera/old.jpg",
        f"{WA.path}/WhatsApp Images/w.jpg",
        "/sdcard/Documents/z.txt",
    ]
    rig.transport.journal.clear()
    rig.pull(scan_first=False)
    assert [e.path for e in rig.transport.journal if e.op == "open_read"] == order


def test_only_the_ticked_sets_are_pulled(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", b"camera")
    rig.add("/sdcard/Documents/b.txt", b"doc")
    rig.scan()
    items = pending_items(rig.db, 1, ["camera"])  # names match ignoring case
    assert [i.path for i in items] == ["/sdcard/DCIM/Camera/a.jpg"]


def test_by_date_layout_applies_to_camera_only(rig: Rig) -> None:
    rig.layout = "by_date"
    taken = datetime.fromtimestamp(OLD)
    rig.add("/sdcard/DCIM/Camera/IMG_1.jpg", b"not really a jpeg")
    rig.add("/sdcard/Documents/notes.txt", b"notes")
    rig.pull()
    assert set(tree(rig.root)) == {
        f"Phone/Camera/{taken:%Y}/{taken:%Y-%m}/IMG_1.jpg",
        "Phone/Documents/notes.txt",
    }


def test_content_kind_from_the_extension() -> None:
    assert content_kind("/sdcard/DCIM/Camera/A.JPG") == "image"
    assert content_kind("/x/clip.mp4") == "video"
    assert content_kind("/x/voice.opus") == "audio"
    assert content_kind("/x/report.pdf") == "doc"
    assert content_kind("/x/noext") == "other"
    assert content_kind("/x/.hidden") == "other"


# -- something is already at the target ---------------------------------------------------------


def _target_of(path: str, data: bytes) -> str:
    return mirror_rel_path("Phone", path, hash8=sha(data)[:8])


def test_target_exists_same_hash_reuses_file(rig: Rig) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"the same bytes" * 10
    rig.add(path, data)
    existing = rig.root / _target_of(path, data)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(data)
    stats = rig.pull()
    assert (stats.copied, stats.failed) == (1, 0)
    assert tree(rig.root) == {_target_of(path, data): data}  # one file, no _HASH8 sibling
    assert replicas(rig.db) == {_target_of(path, data): (sha(data), len(data))}
    assert phone_rows(rig.db)[path][:3] == ("done", sha(data), _target_of(path, data))
    assert staging_files(rig.root) == []  # the .part is gone


def test_reused_file_is_made_durable_before_its_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"the same bytes" * 10
    rig.add(path, data)
    existing = rig.root / _target_of(path, data)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(data)
    events: list[tuple[str, object]] = []
    real_durable, real_record = puller_module.make_durable, Puller._record_copy

    def spy_durable(target: Path) -> None:
        events.append(("durable", target))
        real_durable(target)

    def spy_record(self: Puller, item: object, digest: str, rel_path: str) -> None:
        with rig.db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM replica").fetchone()[0] == 0
        events.append(("rows", rel_path))
        real_record(self, item, digest, rel_path)  # type: ignore[arg-type]

    monkeypatch.setattr(puller_module, "make_durable", spy_durable)
    monkeypatch.setattr(Puller, "_record_copy", spy_record)
    assert rig.pull().copied == 1
    assert events == [("durable", existing), ("rows", _target_of(path, data))]


def _pull_in_a_thread(rig: Rig) -> PullStats:
    """Pull, but fail the test instead of hanging if something blocks (a FIFO opened)."""
    out: list[PullStats] = []
    thread = threading.Thread(target=lambda: out.append(rig.pull()), daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "the pull hung on what was at the target"
    return out[0]


def test_a_fifo_or_symlink_at_the_target_is_never_opened_and_gets_hash8(
    rig: Rig, tmp_path: Path
) -> None:
    fifo_path, fifo_data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"first photo"
    link_path, link_data = "/sdcard/DCIM/Camera/IMG_2.jpg", b"second photo"
    rig.add(fifo_path, fifo_data)
    rig.add(link_path, link_data)
    fifo = rig.root / _target_of(fifo_path, fifo_data)
    link = rig.root / _target_of(link_path, link_data)
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    elsewhere = tmp_path / "elsewhere.jpg"
    elsewhere.write_bytes(link_data)  # identical bytes, but outside the backup: never adopted
    link.symlink_to(elsewhere)
    stats = _pull_in_a_thread(rig)
    assert (stats.copied, stats.failed) == (2, 0)
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode) and os.readlink(link) == str(elsewhere)
    with rig.db.read() as conn:
        rows = dict(conn.execute("SELECT rel_path, content_hash FROM replica").fetchall())
    assert rows == {
        f"Phone/DCIM/Camera/IMG_1_{sha(fifo_data)[:8]}.jpg": sha(fifo_data),
        f"Phone/DCIM/Camera/IMG_2_{sha(link_data)[:8]}.jpg": sha(link_data),
    }
    for rel, digest in rows.items():
        assert sha((rig.root / rel).read_bytes()) == digest


def test_non_files_at_both_paths_fail_the_file_in_plain_words(rig: Rig, tmp_path: Path) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"photo"
    rig.add(path, data)
    base = rig.root / _target_of(path, data)
    hashed = rig.root / f"Phone/DCIM/Camera/IMG_1_{sha(data)[:8]}.jpg"
    base.parent.mkdir(parents=True)
    os.mkfifo(base)
    elsewhere = tmp_path / "same.jpg"
    elsewhere.write_bytes(data)
    hashed.symlink_to(elsewhere)
    stats = _pull_in_a_thread(rig)
    assert (stats.copied, stats.failed) == (0, 1)
    state, _digest, _rel, error = phone_rows(rig.db)[path]
    assert (state, error) == ("failed", describe(TargetExists("x")))
    assert stat.S_ISFIFO(os.lstat(base).st_mode) and os.readlink(hashed) == str(elsewhere)
    assert replicas(rig.db) == {} and staging_files(rig.root) == []


def test_target_exists_different_hash_gets_hash8(rig: Rig) -> None:
    path, data, other = "/sdcard/DCIM/Camera/IMG_1.jpg", b"new bytes", b"someone else's bytes"
    rig.add(path, data)
    existing = rig.root / _target_of(path, data)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(other)
    before = os.stat(existing)
    stats = rig.pull()
    assert (stats.copied, stats.failed) == (1, 0)
    hashed = f"Phone/DCIM/Camera/IMG_1_{sha(data)[:8]}.jpg"
    assert tree(rig.root) == {_target_of(path, data): other, hashed: data}
    after = os.stat(existing)
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)  # untouched
    assert replicas(rig.db) == {hashed: (sha(data), len(data))}
    assert phone_rows(rig.db)[path][:3] == ("done", sha(data), hashed)


def test_target_and_its_hash8_path_both_taken_fails_the_file(rig: Rig) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"new bytes"
    rig.add(path, data)
    base = rig.root / _target_of(path, data)
    hashed = rig.root / f"Phone/DCIM/Camera/IMG_1_{sha(data)[:8]}.jpg"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"first stranger")
    hashed.write_bytes(b"second stranger")
    stats = rig.pull()
    assert (stats.copied, stats.failed) == (0, 1)
    state, _digest, _rel, error = phone_rows(rig.db)[path]
    assert (state, error) == ("failed", describe(TargetExists("x")))
    assert base.read_bytes() == b"first stranger" and hashed.read_bytes() == b"second stranger"
    assert replicas(rig.db) == {} and staging_files(rig.root) == []


def test_a_path_recorded_for_other_content_gets_hash8(rig: Rig) -> None:
    path = "/sdcard/DCIM/Camera/IMG_1.jpg"
    rig.add(path, b"version one", mtime=OLD)
    rig.pull()
    rig.phone.modify(path, b"version two!", mtime=OLD + 60)  # edited on the phone
    stats = rig.pull()
    assert stats.copied == 1
    files = tree(rig.root)
    assert files[_target_of(path, b"version one")] == b"version one"
    assert files[f"Phone/DCIM/Camera/IMG_1_{sha(b'version two!')[:8]}.jpg"] == b"version two!"
    assert_tree_matches_rows(rig)


# -- crashes and recovery -----------------------------------------------------------------------


def test_crash_between_rename_and_rows_recovers_without_duplicate(rig: Rig) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"crash me" * 100
    rig.add(path, data)
    rig.add("/sdcard/Documents/other.txt", b"another file")
    hits: list[str] = []

    def crash_once(name: str) -> None:
        if name == CRASH_POINT and not hits:
            hits.append(name)
            raise SimulatedCrash

    crashpoints.install(crash_once)
    with pytest.raises(SimulatedCrash):
        rig.pull()
    assert hits == [CRASH_POINT]
    # The file is final but has no rows; its phone row is left `pulling`.
    assert tree(rig.root) == {_target_of(path, data): data}
    assert replicas(rig.db) == {}
    assert phone_rows(rig.db)[path][0] == "pulling"

    report = recover(rig.db)
    assert report.rows_reset == 1
    stats = rig.pull()
    assert stats.failed == 0
    assert set(tree(rig.root)) == {_target_of(path, data), "Phone/Documents/other.txt"}
    assert_tree_matches_rows(rig)  # among others: no content is stored twice
    assert {row[0] for row in phone_rows(rig.db).values()} == {"done"}


def _crash_at_rename(rig: Rig) -> None:
    """Pull until the first crash between a rename and its rows, then recover."""
    hits: list[str] = []

    def crash_once(name: str) -> None:
        if name == CRASH_POINT and not hits:
            hits.append(name)
            raise SimulatedCrash

    crashpoints.install(crash_once)
    with pytest.raises(SimulatedCrash):
        rig.pull()
    crashpoints.install(None)
    recover(rig.db)


def _fill_camera(rig: Rig, count: int = 6) -> None:
    """Older files beside the one under test, so one file renamed never trips the guard."""
    for i in range(count):
        rig.add(f"/sdcard/DCIM/Camera/old_{i}.jpg", f"old {i}".encode(), mtime=OLD - 1000 - i)


def test_a_file_renamed_after_a_crash_at_the_rename_is_not_stored_twice(rig: Rig) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"crash me" * 100
    _fill_camera(rig)
    rig.add(path, data)  # the newest: pulled first
    _crash_at_rename(rig)
    rig.phone.remove(path)
    rig.add("/sdcard/DCIM/Camera/IMG_1 renamed.jpg", data)
    stats = rig.pull()
    assert stats.failed == 0
    assert tree(rig.root)[_target_of(path, data)] == data  # the file the crash left is used
    assert_tree_matches_rows(rig)


def test_a_newer_copy_found_after_a_crash_at_the_rename_is_not_stored_twice(rig: Rig) -> None:
    path, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"crash me" * 100
    _fill_camera(rig)
    rig.add(path, data)
    _crash_at_rename(rig)
    rig.add("/sdcard/DCIM/Camera/IMG_1 copy.jpg", data, mtime=OLD + 1)  # newer: pulled first
    stats = rig.pull()
    assert stats.failed == 0
    assert_tree_matches_rows(rig)
    assert {row[0] for row in phone_rows(rig.db).values()} == {"done"}


def test_a_crash_at_a_hash8_rename_then_a_rename_on_the_phone_stores_once(rig: Rig) -> None:
    first, data = "/sdcard/DCIM/Camera/IMG_1.jpg", b"crash me" * 100
    _fill_camera(rig)
    rig.add("/sdcard/DCIM/Camera/img_1.JPG", b"same name, other bytes", mtime=OLD + 1)
    rig.add(first, data)  # takes IMG_1_HASH8.jpg: the other one owns the name
    hits: list[str] = []

    def crash_second(name: str) -> None:
        if name == CRASH_POINT:
            hits.append(name)
            if len(hits) == 2:
                raise SimulatedCrash

    crashpoints.install(crash_second)
    with pytest.raises(SimulatedCrash):
        rig.pull()
    crashpoints.install(None)
    recover(rig.db)
    tagged = f"Phone/DCIM/Camera/IMG_1_{sha(data)[:8]}.jpg"
    assert tree(rig.root)[tagged] == data
    rig.phone.remove(first)
    rig.add("/sdcard/DCIM/Camera/IMG_1 renamed.jpg", data)
    assert rig.pull().failed == 0
    assert tree(rig.root)[tagged] == data
    assert_tree_matches_rows(rig)


def test_recovery_sweeps_parts_and_resets_pulling(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", b"a")
    rig.scan()
    with rig.db.tx() as conn:
        conn.execute("UPDATE phone_file SET state = 'pulling'")
    staging = rig.root / ".staging"
    staging.mkdir(parents=True)
    (staging / "abc.part").write_bytes(b"half")
    (staging / "keep.txt").write_bytes(b"not ours to remove")
    report = recover(rig.db)
    assert (report.parts_removed, report.rows_reset) == (1, 1)
    assert staging_files(rig.root) == ["keep.txt"]
    assert phone_rows(rig.db)["/sdcard/DCIM/Camera/a.jpg"][0] == "discovered"


def test_recovery_ignores_folders_it_has_no_destination_for(db: Database, tmp_path: Path) -> None:
    stray = tmp_path / "not-a-destination" / ".staging"
    stray.mkdir(parents=True)
    (stray / "x.part").write_bytes(b"x")
    assert recover(db).parts_removed == 0
    assert (stray / "x.part").exists()


def test_reconcile_drops_the_row_of_a_missing_file(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/gone.jpg", b"gone" * 10)
    rig.add("/sdcard/DCIM/Camera/shrunk.jpg", b"shrunk" * 10)
    rig.add("/sdcard/DCIM/Camera/fine.jpg", b"fine" * 10)
    rig.pull()
    (rig.root / "Phone/DCIM/Camera/gone.jpg").unlink()
    (rig.root / "Phone/DCIM/Camera/shrunk.jpg").write_bytes(b"short")
    dropped, skipped = reconcile(rig.db)
    assert (dropped, skipped) == (2, 0)
    assert set(replicas(rig.db)) == {"Phone/DCIM/Camera/fine.jpg"}
    rows = phone_rows(rig.db)
    assert rows["/sdcard/DCIM/Camera/gone.jpg"][0] == "discovered"
    assert rows["/sdcard/DCIM/Camera/fine.jpg"][0] == "done"
    # The next run copies the missing one again (the changed one is never overwritten).
    stats = rig.pull()
    assert stats.copied == 2
    assert (rig.root / "Phone/DCIM/Camera/gone.jpg").read_bytes() == b"gone" * 10
    assert (rig.root / "Phone/DCIM/Camera/shrunk.jpg").read_bytes() == b"short"


def test_reconcile_skips_a_destination_that_is_not_there(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", b"a" * 10)
    rig.pull()
    moved = rig.root.with_name("elsewhere")
    rig.root.rename(moved)  # as if the disk were not mounted
    assert reconcile(rig.db) == (0, 1)
    assert len(replicas(rig.db)) == 1
