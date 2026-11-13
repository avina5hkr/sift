"""The backup scanner: staging, the settle rule, warnings and the mass-disappearance guard."""

import json
import os
import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from sift.backup import scanner
from sift.backup.scanner import (
    Disappearance,
    ScanNotPending,
    ScanResult,
    confirm_scan,
    discard_scan,
    scan,
    space_check,
)
from sift.backup.sets import ScanRoot
from sift.backup.skiprules import SkipRules
from sift.core import crashpoints
from sift.core.clock import ManualClock
from sift.core.db import Database
from sift.core.origin import WHATSAPP_MEDIA_ROOTS
from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity

NOW = 1_800_000_000
OLD = NOW - 86_400  # a day ago: long settled
DCIM = ScanRoot("/sdcard/DCIM", "Camera", False, ())
DOWNLOADS = ScanRoot("/sdcard/Download", "Downloads", True, ())
RULES = SkipRules(
    hidden_folders=(".thumbnails", ".trash"),
    name_prefixes=(".pending-", ".trashed-"),
    name_suffixes=(".tmp", ".part"),
    skip_zero_byte=True,
    include_whatsapp_db=False,
    include_whatsapp_statuses=False,
    whatsapp_media_roots=WHATSAPP_MEDIA_ROOTS,
)
GUARD_ADVICE = (
    "Nothing was changed. If you deleted them on purpose, confirm; otherwise check the phone "
    "and scan again."
)


@dataclass
class Rig:
    db: Database
    phone: FakePhone
    transport: FakeTransport
    session: DeviceSession
    clock: ManualClock
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Callable[[], None] | None = None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

    def scan(self, roots: tuple[ScanRoot, ...] = (DCIM,), rules: SkipRules = RULES) -> ScanResult:
        self.transport.journal.clear()
        return scan(self.session, self.db, roots, rules, clock=self.clock, sleep=self.sleep)

    def add(self, path: str, size: int = 10, mtime: int = OLD) -> None:
        self.phone.add_file(path, b"x" * size, mtime=mtime)

    def ops(self, op: str) -> list[str | None]:
        return [e.path for e in self.transport.journal if e.op == op]


@pytest.fixture
def rig(db: Database) -> Iterator[Rig]:
    clock = ManualClock(now=NOW)
    phone = FakePhone()
    transport = FakeTransport(phone, wall_clock=lambda: clock.now())
    with db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial) VALUES ('dev-1', 'FAKE-0001')")
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    yield Rig(db, phone, transport, session, clock)


def rows(db: Database) -> dict[str, tuple[int, int, int, str, str | None]]:
    """remote_path -> (size, mtime, present, state, set_name)."""
    with db.read() as conn:
        found = conn.execute(
            "SELECT remote_path, size, mtime, present, state, set_name FROM phone_file"
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in found}


def present(db: Database) -> set[str]:
    return {path for path, row in rows(db).items() if row[2] == 1}


def scan_row(db: Database, scan_id: int) -> tuple[str, list[str], list[str]]:
    """(state, sets, warnings) of one scan."""
    with db.read() as conn:
        state, sets, warnings = conn.execute(
            "SELECT state, sets, warnings FROM scan WHERE id = ?", (scan_id,)
        ).fetchone()
    return state, json.loads(sets), json.loads(warnings or "[]")


def staged_count(db: Database, scan_id: int) -> int:
    with db.read() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM scan_entry WHERE scan_id = ?", (scan_id,)
        ).fetchone()
    return int(n)


def folder_counts(db: Database) -> dict[str, tuple[str, int]]:
    with db.read() as conn:
        found = conn.execute("SELECT folder, set_name, present_files FROM folder_count").fetchall()
    return {r[0]: (r[1], r[2]) for r in found}


def fill(rig: Rig, folder: str, n: int) -> list[str]:
    paths = [f"{folder}/IMG_{i:04d}.jpg" for i in range(n)]
    for path in paths:
        rig.add(path)
    return paths


# -- a plain scan --------------------------------------------------------------------------------


def test_first_scan_records_new_files_with_set_origin_and_folder_counts(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", 100)
    rig.add("/sdcard/DCIM/Camera/b.jpg", 200)
    rig.add("/sdcard/DCIM/Screenshots/s.png", 30)
    rig.add("/sdcard/Download/doc.pdf", 5)

    result = rig.scan((DCIM, DOWNLOADS))

    assert result.state == "applied"
    assert (result.new_or_changed, result.bytes_needed, result.unchanged) == (4, 335, 0)
    assert (result.settling, result.skipped_by_rule) == (0, 0)
    assert result.warnings == () and result.disappearance == ()
    found = rows(rig.db)
    assert found["/sdcard/DCIM/Camera/a.jpg"] == (100, OLD, 1, "discovered", "Camera")
    assert found["/sdcard/Download/doc.pdf"] == (5, OLD, 1, "discovered", "Downloads")
    with rig.db.read() as conn:
        origin = conn.execute(
            "SELECT origin, last_scan_id FROM phone_file WHERE remote_path LIKE '%s.png'"
        ).fetchone()
    assert origin == ("screenshot", result.scan_id)
    assert folder_counts(rig.db) == {
        "/sdcard/dcim/camera": ("Camera", 2),
        "/sdcard/dcim/screenshots": ("Camera", 1),
        "/sdcard/download": ("Downloads", 1),
    }
    state, sets, warnings = scan_row(rig.db, result.scan_id)
    assert (state, sets, warnings) == ("applied", ["Camera", "Downloads"], [])
    assert staged_count(rig.db, result.scan_id) == 0  # the staging rows are spent


def test_a_new_scan_starts_running_and_is_staged_before_anything_changes(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg")

    def crash(name: str) -> None:
        if name == "scanner.staged":
            raise RuntimeError("crash")

    crashpoints.install(crash)
    with pytest.raises(RuntimeError):
        rig.scan()
    crashpoints.install(None)

    with rig.db.read() as conn:
        (first,) = conn.execute("SELECT id FROM scan").fetchone()
    assert scan_row(rig.db, first)[0] == "running"
    assert staged_count(rig.db, first) == 1
    assert rows(rig.db) == {}

    result = rig.scan()  # the next scan drops the unfinished one and starts afresh
    assert result.state == "applied"
    assert scan_row(rig.db, first)[0] == "discarded"
    assert staged_count(rig.db, first) == 0
    assert present(rig.db) == {"/sdcard/DCIM/Camera/a.jpg"}


def test_a_crash_while_applying_changes_nothing(rig: Rig) -> None:
    paths = fill(rig, "/sdcard/DCIM/Camera", 10)
    rig.scan()
    rig.phone.remove(paths[0])
    rig.add("/sdcard/DCIM/Camera/b.jpg")
    before = rows(rig.db)

    def crash(name: str) -> None:
        if name == "scanner.applying":
            raise RuntimeError("crash")

    crashpoints.install(crash)
    with pytest.raises(RuntimeError):
        rig.scan((DCIM, DOWNLOADS))
    assert rows(rig.db) == before


def test_unchanged_files_are_not_read(rig: Rig) -> None:
    fill(rig, "/sdcard/DCIM/Camera", 5)
    rig.scan()

    result = rig.scan()

    assert (result.unchanged, result.new_or_changed, result.bytes_needed) == (5, 0, 0)
    assert rig.ops("open_read") == [] and rig.ops("sha256") == [] and rig.ops("stat") == []
    assert rig.sleeps == []


def test_a_changed_file_is_found_again_and_its_old_hash_forgotten(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", 10)
    rig.add("/sdcard/DCIM/Camera/b.jpg", 10)
    rig.scan()
    with rig.db.tx() as conn:
        conn.execute(
            "INSERT INTO content (hash, size, kind, created_at) VALUES ('h1', 10, 'image', 0)"
        )
        conn.execute("UPDATE phone_file SET state = 'done', content_hash = 'h1'")
    rig.phone.modify("/sdcard/DCIM/Camera/a.jpg", b"y" * 12, mtime=OLD + 5)

    result = rig.scan()

    assert (result.new_or_changed, result.bytes_needed, result.unchanged) == (1, 12, 1)
    found = rows(rig.db)
    assert found["/sdcard/DCIM/Camera/a.jpg"][:4] == (12, OLD + 5, 1, "discovered")
    assert found["/sdcard/DCIM/Camera/b.jpg"][3] == "done"
    with rig.db.read() as conn:
        hashes = dict(conn.execute("SELECT remote_path, content_hash FROM phone_file").fetchall())
    assert hashes == {"/sdcard/DCIM/Camera/a.jpg": None, "/sdcard/DCIM/Camera/b.jpg": "h1"}


# -- the settle rule -----------------------------------------------------------------------------


def test_settle_rule_skips_recent_and_growing_files(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/old.jpg", 10, OLD)
    rig.add("/sdcard/DCIM/Camera/fresh.jpg", 10, NOW - 60)  # within 120 s of the phone's clock
    rig.add("/sdcard/DCIM/Camera/steady.jpg", 10, NOW - 1800)  # within the hour: checked twice
    rig.add("/sdcard/DCIM/Camera/growing.mp4", 10, NOW - 1800)
    rig.on_sleep = lambda: rig.phone.modify(
        "/sdcard/DCIM/Camera/growing.mp4", b"x" * 50, mtime=NOW - 1800
    )

    result = rig.scan()

    assert rig.sleeps == [2.0]  # one shared wait
    assert sorted(p for p in rig.ops("stat") if p) == [
        "/sdcard/DCIM/Camera/growing.mp4",
        "/sdcard/DCIM/Camera/steady.jpg",
    ]
    assert (result.new_or_changed, result.settling, result.bytes_needed) == (2, 2, 20)
    assert set(rows(rig.db)) == {"/sdcard/DCIM/Camera/old.jpg", "/sdcard/DCIM/Camera/steady.jpg"}


def test_settle_rule_uses_the_phones_clock(rig: Rig) -> None:
    rig.phone.time_offset_s = -3_600  # the phone's clock runs an hour behind the Mac's
    rig.add("/sdcard/DCIM/Camera/a.jpg", 10, NOW - 3_600 - 30)

    result = rig.scan()

    assert (result.settling, result.new_or_changed) == (1, 0)


def test_a_file_dated_in_the_future_is_backed_up_after_a_stable_double_stat(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/future.jpg", 10, NOW + 3 * 86_400)

    result = rig.scan()

    assert (result.new_or_changed, result.settling) == (1, 0)
    assert rig.sleeps == [2.0]
    assert rig.ops("stat") == ["/sdcard/DCIM/Camera/future.jpg"]
    assert present(rig.db) == {"/sdcard/DCIM/Camera/future.jpg"}


def test_a_file_dated_in_the_future_that_grows_is_left_for_later(rig: Rig) -> None:
    path = "/sdcard/DCIM/Camera/future.mp4"
    rig.add(path, 10, NOW + 3 * 86_400)
    rig.on_sleep = lambda: rig.phone.modify(path, b"x" * 20, mtime=NOW + 3 * 86_400)

    result = rig.scan()

    assert (result.new_or_changed, result.settling) == (0, 1)
    assert rows(rig.db) == {}


def test_a_known_file_that_is_settling_keeps_its_row_as_it_was(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg", 10, OLD)
    rig.add("/sdcard/DCIM/Camera/b.jpg", 10, OLD)
    rig.scan()
    rig.phone.modify("/sdcard/DCIM/Camera/a.jpg", b"x" * 99, mtime=NOW - 10)

    result = rig.scan()

    assert (result.settling, result.unchanged, result.new_or_changed) == (1, 1, 0)
    assert rows(rig.db)["/sdcard/DCIM/Camera/a.jpg"] == (10, OLD, 1, "discovered", "Camera")


# -- skip rules ----------------------------------------------------------------------------------


def test_skip_rules_leave_out_files_and_whole_folders(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/keep.jpg")
    rig.add("/sdcard/DCIM/.thumbnails/t.jpg")  # hidden folder
    rig.add("/sdcard/DCIM/Camera/upload.part")  # name suffix
    rig.add("/sdcard/DCIM/Camera/empty.jpg", 0)  # zero bytes
    rig.add("/sdcard/DCIM/.trashed-123/deep/inside.jpg")  # a folder whose name is a skip prefix
    rig.phone.add_dir("/sdcard/DCIM/.thumbnails/refused", readable=False)

    result = rig.scan()

    # Skipped folders are pruned: never listed, so their files are not even counted, and a
    # folder the phone refuses inside one is never asked for (no warning).
    assert (result.new_or_changed, result.skipped_by_rule) == (1, 2)
    assert result.warnings == ()
    assert set(rows(rig.db)) == {"/sdcard/DCIM/Camera/keep.jpg"}
    assert rig.transport.listed == ["/sdcard/DCIM", "/sdcard/DCIM/Camera"]


def test_a_new_skip_rule_keeps_known_rows_and_never_trips_the_guard(rig: Rig) -> None:
    cache = fill(rig, "/sdcard/DCIM/Cache", 50)
    camera = fill(rig, "/sdcard/DCIM/Camera", 10)
    rig.add("/sdcard/DCIM/Camera/clip.tmp")
    lenient = replace(RULES, name_suffixes=(".part",))
    rig.scan(rules=lenient)
    with rig.db.tx() as conn:  # the Camera ones were backed up; the Cache ones not yet
        conn.execute("UPDATE phone_file SET state = 'done' WHERE remote_path LIKE '%/Camera/IMG%'")
    stricter = replace(RULES, hidden_folders=("Cache",))
    rig.phone.remove(cache[0])  # unknown to the scan: the folder is not listed any more

    result = rig.scan(rules=stricter)

    assert result.state == "applied" and result.disappearance == ()
    assert present(rig.db) == {*cache, *camera, "/sdcard/DCIM/Camera/clip.tmp"}
    states = {p: r[3] for p, r in rows(rig.db).items()}
    assert {states[p] for p in cache} == {"skipped"}
    assert states["/sdcard/DCIM/Camera/clip.tmp"] == "skipped"
    assert {states[p] for p in camera} == {"done"}
    assert folder_counts(rig.db)["/sdcard/dcim/cache"] == ("Camera", 50)

    result = rig.scan(rules=lenient)  # the rules are back as they were

    assert result.state == "applied"
    states = {p: r[3] for p, r in rows(rig.db).items()}
    assert {states[p] for p in cache[1:]} == {"discovered"}
    assert states["/sdcard/DCIM/Camera/clip.tmp"] == "discovered"
    assert present(rig.db) == {*cache[1:], *camera, "/sdcard/DCIM/Camera/clip.tmp"}


def test_symlinks_are_not_backed_up(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg")
    rig.phone.add_symlink("/sdcard/DCIM/Camera/link.jpg", "/sdcard/DCIM/Camera/a.jpg")

    rig.scan()

    assert set(rows(rig.db)) == {"/sdcard/DCIM/Camera/a.jpg"}


def test_a_narrow_set_inside_a_wide_one_takes_its_own_files_once(rig: Rig) -> None:
    pictures = ScanRoot("/sdcard/Pictures", "Pictures", False, ())
    shots = ScanRoot("/sdcard/Pictures/Screenshots", "Screenshots", False, ())
    rig.add("/sdcard/Pictures/p.jpg")
    rig.add("/sdcard/Pictures/Screenshots/s.png")

    result = rig.scan((pictures, shots))

    assert result.new_or_changed == 2
    assert {p: r[4] for p, r in rows(rig.db).items()} == {
        "/sdcard/Pictures/p.jpg": "Pictures",
        "/sdcard/Pictures/Screenshots/s.png": "Screenshots",
    }


def test_excluded_folders_are_left_out_and_their_rows_left_alone(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg")
    rig.add("/sdcard/DCIM/Private/p.jpg")
    rig.scan()
    narrowed = ScanRoot("/sdcard/DCIM", "Camera", False, ("/sdcard/DCIM/Private",))
    rig.phone.remove("/sdcard/DCIM/Private/p.jpg")

    result = rig.scan((narrowed,))

    assert result.state == "applied"
    assert present(rig.db) == {"/sdcard/DCIM/Camera/a.jpg", "/sdcard/DCIM/Private/p.jpg"}


# -- unreadable folders --------------------------------------------------------------------------


def test_unreadable_folder_is_skipped_with_warning_and_rows_kept(rig: Rig) -> None:
    fill(rig, "/sdcard/DCIM/Camera", 3)
    other = fill(rig, "/sdcard/DCIM/Other", 2)
    rig.scan()
    rig.phone.add_dir("/sdcard/DCIM/Other", readable=False)
    rig.phone.remove(other[0])  # unknown to the scan: the folder cannot be read
    rig.add("/sdcard/DCIM/Camera/new.jpg")

    result = rig.scan()

    assert result.state == "applied"
    assert result.warnings == ("Could not read /sdcard/DCIM/Other; skipped.",)
    assert result.new_or_changed == 1
    assert set(other) <= present(rig.db)
    assert folder_counts(rig.db)["/sdcard/dcim/other"] == ("Camera", 2)
    assert scan_row(rig.db, result.scan_id)[2] == ["Could not read /sdcard/DCIM/Other; skipped."]


def test_unreadable_root_is_skipped_with_warning_and_rows_kept(rig: Rig) -> None:
    paths = fill(rig, "/sdcard/DCIM/Camera", 3)
    rig.add("/sdcard/Download/a.pdf")
    rig.scan((DCIM, DOWNLOADS))
    rig.phone.add_dir("/sdcard/DCIM", readable=False)
    rig.phone.add_dir("/sdcard/Download", readable=False)

    result = rig.scan((DCIM, DOWNLOADS))

    assert result.state == "applied"
    assert result.warnings == (
        "Could not read /sdcard/DCIM; skipped.",
        "Could not read /sdcard/Download; skipped.",
    )
    assert present(rig.db) == {*paths, "/sdcard/Download/a.pdf"}


def test_an_entry_that_cannot_be_looked_at_is_skipped_with_warning_and_kept(rig: Rig) -> None:
    paths = fill(rig, "/sdcard/DCIM/Camera", 3)
    rig.scan()
    rig.transport.faults.unstatable.add(paths[0])

    result = rig.scan()

    assert result.warnings == (f"Could not read {paths[0]}; skipped.",)
    assert present(rig.db) == set(paths)


# -- the mass-disappearance guard ----------------------------------------------------------------


def test_mass_disappearance_stops_until_confirmed_when_a_folder_empties(rig: Rig) -> None:
    camera = fill(rig, "/sdcard/DCIM/Camera", 50)
    trips = fill(rig, "/sdcard/DCIM/Trips", 300)
    rig.scan()
    for path in camera:
        rig.phone.remove(path)

    result = rig.scan()

    assert result.state == "awaiting_confirmation"
    assert result.disappearance == (Disappearance("folder", "/sdcard/DCIM/Camera", 50, 0),)
    assert result.warnings == (
        f"The folder /sdcard/DCIM/Camera had 50 files last time and now shows 0. {GUARD_ADVICE}",
    )
    assert present(rig.db) == {*camera, *trips}  # phone_file untouched
    assert folder_counts(rig.db)["/sdcard/dcim/camera"] == ("Camera", 50)
    assert scan_row(rig.db, result.scan_id)[:1] == ("awaiting_confirmation",)

    confirm_scan(rig.db, result.scan_id)

    assert present(rig.db) == set(trips)
    assert len(rows(rig.db)) == 350
    assert folder_counts(rig.db)["/sdcard/dcim/camera"] == ("Camera", 0)
    assert scan_row(rig.db, result.scan_id)[0] == "applied"
    assert staged_count(rig.db, result.scan_id) == 0
    with pytest.raises(ScanNotPending):
        confirm_scan(rig.db, result.scan_id)

    assert rig.scan().state == "applied"  # an emptied folder, once confirmed, is no news


@pytest.mark.parametrize(("removed", "guarded"), [(25, True), (20, False), (15, False)])
def test_mass_disappearance_stops_when_a_set_drops_more_than_a_fifth(
    rig: Rig, removed: int, guarded: bool
) -> None:
    paths = fill(rig, "/sdcard/DCIM/Camera", 100)
    rig.scan()
    for path in paths[:removed]:
        rig.phone.remove(path)

    result = rig.scan()

    if guarded:
        assert result.state == "awaiting_confirmation"
        assert result.disappearance == (Disappearance("set", "Camera", 100, 75),)
        assert result.warnings == (
            f"Camera had 100 files last time and now shows 75. {GUARD_ADVICE}",
        )
        assert present(rig.db) == set(paths)
    else:
        assert result.state == "applied"
        assert present(rig.db) == set(paths[removed:])


def test_a_new_narrower_set_is_not_a_disappearance(rig: Rig) -> None:
    pictures = ScanRoot("/sdcard/Pictures", "Pictures", False, ())
    shots = ScanRoot("/sdcard/Pictures/Screenshots", "Screenshots", False, ())
    fill(rig, "/sdcard/Pictures/Saved", 70)
    fill(rig, "/sdcard/Pictures/Screenshots", 30)
    rig.scan((pictures,))

    first = rig.scan((pictures, shots))
    again = rig.scan((pictures, shots))

    assert (first.state, first.disappearance) == ("applied", ())
    assert (again.state, again.disappearance) == ("applied", ())
    assert Counter(r[4] for r in rows(rig.db).values()) == {"Pictures": 70, "Screenshots": 30}


def test_mass_disappearance_names_a_whole_set_that_empties(rig: Rig) -> None:
    whatsapp = ScanRoot("/sdcard/WhatsApp/Media", "WhatsApp media", False, ())
    paths = fill(rig, "/sdcard/WhatsApp/Media/WhatsApp Images", 3)
    rig.scan((whatsapp,))
    for path in paths:
        rig.phone.remove(path)

    result = rig.scan((whatsapp,))

    assert result.disappearance == (
        Disappearance("set", "WhatsApp media", 3, 0),
        Disappearance("folder", "/sdcard/WhatsApp/Media/WhatsApp Images", 3, 0),
    )
    assert result.warnings[0] == (
        f"WhatsApp media had 3 files last time and now shows 0. {GUARD_ADVICE}"
    )


def test_mass_disappearance_catches_a_refused_folder_that_lists_empty(rig: Rig) -> None:
    camera = fill(rig, "/sdcard/DCIM/Camera", 50)
    trips = fill(rig, "/sdcard/DCIM/Trips", 300)
    rig.scan()
    rig.transport.faults.lists_empty.add("/sdcard/DCIM/Camera")  # M1 ruling R14

    result = rig.scan()

    assert result.state == "awaiting_confirmation"
    assert result.disappearance == (Disappearance("folder", "/sdcard/DCIM/Camera", 50, 0),)
    assert present(rig.db) == {*camera, *trips}


def test_a_discarded_scan_changes_nothing(rig: Rig) -> None:
    camera = fill(rig, "/sdcard/DCIM/Camera", 4)
    rig.scan()
    for path in camera:
        rig.phone.remove(path)
    result = rig.scan()
    assert result.state == "awaiting_confirmation"

    discard_scan(rig.db, result.scan_id)

    assert scan_row(rig.db, result.scan_id)[0] == "discarded"
    assert staged_count(rig.db, result.scan_id) == 0
    assert present(rig.db) == set(camera)
    with pytest.raises(ScanNotPending):
        confirm_scan(rig.db, result.scan_id)
    with pytest.raises(ScanNotPending):
        discard_scan(rig.db, result.scan_id)
    with pytest.raises(ScanNotPending):
        confirm_scan(rig.db, 999)


def test_a_new_scan_replaces_one_awaiting_confirmation(rig: Rig) -> None:
    camera = fill(rig, "/sdcard/DCIM/Camera", 4)
    rig.scan()
    rig.transport.faults.lists_empty.add("/sdcard/DCIM/Camera")
    waiting = rig.scan()
    rig.transport.faults.lists_empty.clear()  # the owner checked the phone and scanned again

    again = rig.scan()

    assert again.state == "applied"
    assert scan_row(rig.db, waiting.scan_id)[0] == "discarded"
    assert present(rig.db) == set(camera)
    with pytest.raises(ScanNotPending):
        confirm_scan(rig.db, waiting.scan_id)


def test_first_scan_has_no_guard(rig: Rig) -> None:
    fill(rig, "/sdcard/DCIM/Camera", 10)
    assert rig.scan().state == "applied"

    # A set scanned for the first time has no history either, even beside one that has.
    pictures = ScanRoot("/sdcard/Pictures", "Pictures", False, ())
    fill(rig, "/sdcard/Pictures/Saved", 5)
    result = rig.scan((DCIM, pictures))

    assert result.state == "applied" and result.disappearance == ()
    assert result.new_or_changed == 5


# -- shallow roots and rows ----------------------------------------------------------------------


def test_shallow_root_lists_only_direct_files(rig: Rig) -> None:
    rig.add("/sdcard/Download/a.pdf")
    rig.add("/sdcard/Download/sub/b.pdf")
    rig.scan((DOWNLOADS,))
    with rig.db.tx() as conn:  # a row under a subfolder (from a wider set once) is out of scope
        conn.execute(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, set_name)"
            " SELECT id, '/sdcard/Download/sub/c.pdf', 'other', 1, 1, 'done', 'Old' FROM device"
        )

    rig.scan((DOWNLOADS,))

    assert rig.ops("walk") == []
    assert rig.ops("list_dir") == ["/sdcard/Download"]
    assert present(rig.db) == {"/sdcard/Download/a.pdf", "/sdcard/Download/sub/c.pdf"}


def test_rows_never_deleted(rig: Rig) -> None:
    paths = fill(rig, "/sdcard/DCIM/Camera", 10)
    rig.scan()
    rig.phone.remove(paths[0])
    rig.scan()
    assert set(rows(rig.db)) == set(paths)
    assert present(rig.db) == set(paths[1:])

    for path in paths[1:]:
        rig.phone.remove(path)
    confirm_scan(rig.db, rig.scan().scan_id)
    assert set(rows(rig.db)) == set(paths)
    assert present(rig.db) == set()

    rig.add(paths[0])  # back on the phone, unchanged
    result = rig.scan()
    assert (result.unchanged, result.new_or_changed) == (1, 0)
    assert present(rig.db) == {paths[0]}


def test_set_tags_follow_the_sets(rig: Rig) -> None:
    rig.add("/sdcard/DCIM/Camera/a.jpg")
    rig.scan()

    rig.scan((ScanRoot("/sdcard/DCIM", "Photos", False, ()),))

    assert rows(rig.db)["/sdcard/DCIM/Camera/a.jpg"][4] == "Photos"
    assert folder_counts(rig.db)["/sdcard/dcim/camera"] == ("Photos", 1)


# -- space check ---------------------------------------------------------------------------------


def _free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    usage = shutil._ntuple_diskusage(10**13, 10**13 - free, free)  # type: ignore[attr-defined]
    monkeypatch.setattr(scanner.shutil, "disk_usage", lambda _path: usage)


def test_space_check_names_the_shortfall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _free(monkeypatch, 9_000_000_000)

    message = space_check(10_000_000_000, tmp_path, 2.0)

    assert message == (
        "The Mac needs 1.2 GB more free space for this backup: it needs 10.2 GB, "
        "with 2% to spare, and 9.0 GB is free."
    )


def test_space_check_passes_with_room_to_spare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, 10_200_000_000)
    assert space_check(10_000_000_000, tmp_path, 2.0) is None
    assert space_check(10_000_000_000, tmp_path / "not" / "made" / "yet", 2.0) is None
    _free(monkeypatch, 10_199_999_999)
    assert space_check(10_000_000_000, tmp_path, 2.0) is not None


def test_space_check_needs_a_writable_destination(tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    dest.mkdir()
    os.chmod(dest, 0o500)
    try:
        message = space_check(1, dest, 2.0)
    finally:
        os.chmod(dest, 0o700)
    assert message == (
        "Sift cannot save files in the backup folder on the Mac. Check that you can write to it."
    )
    assert list(dest.iterdir()) == []  # the write probe leaves nothing behind
    assert space_check(1, dest, 2.0) is None


def test_a_set_folder_missing_on_this_phone_is_empty_without_a_warning(rig: Rig) -> None:
    rig.add("/sdcard/Download/a.pdf")  # /sdcard/DCIM does not exist on this phone

    result = rig.scan((DCIM, DOWNLOADS))

    assert result.state == "applied"
    assert result.warnings == ()
    assert present(rig.db) == {"/sdcard/Download/a.pdf"}


def test_a_set_folder_that_had_files_and_is_now_missing_trips_the_guard(rig: Rig) -> None:
    paths = fill(rig, "/sdcard/DCIM/Camera", 3)
    rig.scan((DCIM,))
    rig.phone.remove("/sdcard/DCIM")

    result = rig.scan((DCIM,))

    assert result.state == "awaiting_confirmation"
    assert present(rig.db) == set(paths)  # nothing changes until the owner confirms
