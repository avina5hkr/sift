"""A backup run as the app runs it: JobManager, then scan, the guard, the space check and pull."""

import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from sift.backup import run as run_module
from sift.backup.readers import stored_read_speed
from sift.backup.run import UNKNOWN_SET_MESSAGE, mac_target, reader_count, run_backup
from sift.backup.scanner import confirm_scan, discard_scan
from sift.core.clock import ManualClock, SystemClock
from sift.core.context import AppContext
from sift.core.jobs import JobControl, JobManager, JobSnapshot, JobState
from sift.core.paths import AppPaths
from sift.device.fake_phone import BIG_VIDEO, build_fake_phone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity
from sift.device.transport import DeviceTimeout
from sift.services.plainwords import describe
from tests.helpers import make_ctx

ENABLED_SMALL = ["Camera", "Screenshots", "WhatsApp media", "Documents", "Recordings"]


class Tracker:
    def __init__(self, session: DeviceSession | None) -> None:
        self.current = session

    def session(self) -> DeviceSession | None:
        return self.current


@dataclass
class Rig:
    ctx: AppContext
    transport: FakeTransport
    session: DeviceSession
    root: Path

    def jobs(self) -> JobManager:
        def runner(control: JobControl) -> None:
            run_backup(
                self.ctx,
                control,
                control.sets,
                control.dry_run,
                sleep=lambda _s: None,
                reconnect_pause_s=0.0,
            )

        return JobManager(
            self.ctx,
            clock=SystemClock(),
            runner=runner,
            describe=describe,
            stored_speed=lambda: stored_read_speed(self.ctx),
            sleep=lambda _s: None,
            poll_s=0.01,
        )


@pytest.fixture
def rig(paths: AppPaths, tmp_path: Path) -> Iterator[Rig]:
    ctx = make_ctx(paths)
    root = tmp_path / "backup"
    ctx.config.backup.mac_root = str(root)
    phone = build_fake_phone(60)
    phone.remove(BIG_VIDEO)
    for path, file in list(phone.files.items()):  # keep the test's copies small
        if file.size > 100_000:
            phone.modify(path, hashlib.sha256(path.encode()).digest() * 100, file.mtime)
    transport = FakeTransport(phone)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial) VALUES ('dev-1', 'FAKE-0001')")
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    ctx.tracker = Tracker(session)  # type: ignore[assignment]
    yield Rig(ctx, transport, session, root)


def finished(jobs: JobManager, job_id: int) -> JobSnapshot:
    assert jobs.wait_finished(job_id, 30)
    return next(s for s in jobs.snapshot() if s.id == job_id)


def tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".staging"]
        for name in filenames:
            p = Path(dirpath) / name
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def test_end_to_end_backup_of_the_fake_phone(rig: Rig) -> None:
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(ENABLED_SMALL))
    assert snap.state is JobState.done and snap.error is None
    assert snap.files_total is not None and snap.files_done == snap.files_total > 20
    files = tree(rig.root)
    with rig.ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT remote_path, state, content_hash, set_name FROM phone_file WHERE present = 1"
        ).fetchall()
        replicas = dict(conn.execute("SELECT rel_path, content_hash FROM replica").fetchall())
    phone = rig.transport.phone
    backed_up = [r for r in rows if r[3] in ENABLED_SMALL]
    assert backed_up and {r[1] for r in backed_up} == {"done"}
    for path, _state, digest, _set in backed_up:
        assert digest == phone.sha256_of(path)  # the bytes on the phone, verified
    assert {r[3] for r in rows} <= set(ENABLED_SMALL)  # only the ticked sets were scanned
    assert set(files) == set(replicas)
    for rel, data in files.items():
        assert hashlib.sha256(data).hexdigest() == replicas[rel]
    hashes = [hashlib.sha256(d).hexdigest() for d in files.values()]
    assert len(hashes) == len(set(hashes))  # no content stored twice
    assert {r[2] for r in backed_up} == set(hashes)  # every content has exactly one file
    assert not any(e.mutating for e in rig.transport.journal)  # I7

    # A second run with no changes on the phone transfers nothing.
    rig.transport.journal.clear()
    again = finished(jobs, jobs.start_backup(ENABLED_SMALL))
    assert again.state is JobState.done and again.files_total == 0
    assert [e for e in rig.transport.journal if e.op == "open_read"] == []


def test_a_dry_run_scans_but_copies_nothing(rig: Rig) -> None:
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(["Screenshots"], dry_run=True))
    assert snap.state is JobState.done
    assert snap.files_total is not None and snap.files_total > 0
    assert not rig.root.exists()
    assert [e for e in rig.transport.journal if e.op == "open_read"] == []


def _guarded_second_run(rig: Rig) -> tuple[JobManager, int, int]:
    jobs = rig.jobs()
    assert finished(jobs, jobs.start_backup(["Screenshots"])).state is JobState.done
    for path in [p for p in rig.transport.phone.files if "/Screenshots/" in p]:
        rig.transport.phone.remove(path)
    job_id = jobs.start_backup(["Screenshots"])
    deadline = 100
    while True:
        snap = next(s for s in jobs.snapshot() if s.id == job_id)
        if snap.state is JobState.awaiting_confirmation:
            break
        assert snap.state not in (JobState.done, JobState.failed), snap
        deadline -= 1
        assert deadline > 0
        time.sleep(0.02)
    with rig.ctx.db.read() as conn:
        (scan_id,) = conn.execute(
            "SELECT id FROM scan WHERE state = 'awaiting_confirmation'"
        ).fetchone()
    return jobs, job_id, int(scan_id)


def test_guard_waits_for_the_owner_then_discard_ends_the_run(rig: Rig) -> None:
    jobs, job_id, scan_id = _guarded_second_run(rig)
    discard_scan(rig.ctx.db, scan_id)
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done and not snap.stopped
    with rig.ctx.db.read() as conn:
        present = conn.execute(
            "SELECT COUNT(*) FROM phone_file WHERE set_name = 'Screenshots' AND present = 1"
        ).fetchone()[0]
    assert present > 0  # nothing was applied


def test_guard_waits_for_the_owner_then_confirm_applies_and_goes_on(rig: Rig) -> None:
    jobs, job_id, scan_id = _guarded_second_run(rig)
    confirm_scan(rig.ctx.db, scan_id)
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done and snap.files_total == 0
    with rig.ctx.db.read() as conn:
        present = conn.execute(
            "SELECT COUNT(*) FROM phone_file WHERE set_name = 'Screenshots' AND present = 1"
        ).fetchone()[0]
    assert present == 0
    assert len(tree(rig.root)) > 0  # the copies already made stay (I1)


def test_stop_while_awaiting_confirmation_ends_the_job(rig: Rig) -> None:
    jobs, job_id, _scan_id = _guarded_second_run(rig)
    jobs.stop(job_id)
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done and snap.stopped


def test_not_enough_space_refuses_the_backup_in_plain_words(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "space_check", lambda *_a: "The Mac needs more free space.")
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(["Screenshots"]))
    assert snap.state is JobState.failed and snap.error == "The Mac needs more free space."
    assert not rig.root.exists() or tree(rig.root) == {}


def test_an_unknown_set_fails_in_plain_words(rig: Rig) -> None:
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(["No such set"]))
    assert snap.state is JobState.failed and snap.error == UNKNOWN_SET_MESSAGE


def test_mac_destination_row_is_made_once(paths: AppPaths, tmp_path: Path) -> None:
    ctx = make_ctx(paths)
    ctx.config.backup.mac_root = str(tmp_path / "b")
    first, second = mac_target(ctx.db, ctx.config), mac_target(ctx.db, ctx.config)
    assert first == second and first.root == tmp_path / "b"
    with ctx.db.read() as conn:
        assert conn.execute("SELECT kind, root_path FROM destination").fetchall() == [
            ("mac", str(tmp_path / "b"))
        ]


def test_reader_count_keeps_one_connection_slot_free(rig: Rig) -> None:
    rig.ctx.config.phone.parallel_reads_usb = 6
    assert reader_count(rig.ctx.config, rig.session) == rig.transport.limits.max_connections - 1
    rig.ctx.config.phone.parallel_reads_usb = 2
    assert reader_count(rig.ctx.config, rig.session) == 2


def test_a_manual_clock_runs_too(rig: Rig) -> None:
    """The run takes its clock from the caller (the scan's 'now' comes from the phone)."""
    jobs = JobManager(
        rig.ctx,
        clock=ManualClock(),
        runner=lambda c: run_backup(
            rig.ctx, c, c.sets, c.dry_run, clock=ManualClock(), sleep=lambda _s: None
        ),
        describe=describe,
    )
    assert finished(jobs, jobs.start_backup(["Documents"])).state is JobState.done


def summary_rows(rig: Rig) -> list[tuple[int, str, int, int, int, int]]:
    """(job_id, outcome, new_files, failed, bytes, warnings count) of every run_summary row."""
    with rig.ctx.db.read() as conn:
        return [
            (r[0], r[1], r[2], r[3], r[4], len(json.loads(r[5])))
            for r in conn.execute(
                "SELECT job_id, outcome, new_files, failed, bytes, warnings FROM run_summary "
                "ORDER BY id"
            )
        ]


def test_a_finished_run_leaves_one_summary_row_with_its_counts(rig: Rig) -> None:
    jobs = rig.jobs()
    job_id = jobs.start_backup(ENABLED_SMALL)
    snap = finished(jobs, job_id)
    ((row_job, outcome, new, failed, size, _w),) = summary_rows(rig)
    assert (row_job, outcome, failed) == (job_id, "done", 0)
    assert new == snap.files_done - snap.duplicates_skipped
    assert size == sum(len(d) for d in tree(rig.root).values())
    assert snap.failed == 0


def test_a_dry_run_leaves_no_summary_row(rig: Rig) -> None:
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Screenshots"], dry_run=True))
    assert summary_rows(rig) == []


def test_a_stopped_run_still_records_what_it_copied(rig: Rig) -> None:
    jobs = rig.jobs()
    job_id = jobs.start_backup(ENABLED_SMALL)
    deadline = time.monotonic() + 20
    while True:
        snap = next(s for s in jobs.snapshot() if s.id == job_id)
        if snap.state is JobState.pulling and snap.files_done >= 1:
            break
        assert snap.state not in (JobState.done, JobState.failed) and time.monotonic() < deadline
        time.sleep(0.005)
    jobs.stop(job_id)
    assert finished(jobs, job_id).stopped
    ((_, outcome, new, _failed, _size, _w),) = summary_rows(rig)
    assert outcome == "stopped" and new >= 1


def test_a_refused_run_records_failed_and_the_job_has_the_reason(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "space_check", lambda *_a: "The Mac needs more free space.")
    jobs = rig.jobs()
    job_id = jobs.start_backup(["Screenshots"])
    finished(jobs, job_id)
    ((row_job, outcome, new, *_rest),) = summary_rows(rig)
    assert (row_job, outcome, new) == (job_id, "failed", 0)


def test_scan_warnings_are_kept_in_the_summary(rig: Rig) -> None:
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Screenshots"]))
    for path in [p for p in rig.transport.phone.files if "/Screenshots/" in p]:
        rig.transport.phone.remove(path)
    job_id = jobs.start_backup(["Screenshots"])
    deadline = time.monotonic() + 10
    while next(s for s in jobs.snapshot() if s.id == job_id).state is not (
        JobState.awaiting_confirmation
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with rig.ctx.db.read() as conn:
        (scan_id,) = conn.execute(
            "SELECT id FROM scan WHERE state = 'awaiting_confirmation'"
        ).fetchone()
    discard_scan(rig.ctx.db, scan_id)
    finished(jobs, job_id)
    assert summary_rows(rig)[-1][5] >= 1  # the guard's warning


def test_a_discarded_scan_is_recorded_as_discarded_not_done(rig: Rig) -> None:
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Screenshots"]))
    for path in [p for p in rig.transport.phone.files if "/Screenshots/" in p]:
        rig.transport.phone.remove(path)
    job_id = jobs.start_backup(["Screenshots"])
    deadline = time.monotonic() + 10
    while next(s for s in jobs.snapshot() if s.id == job_id).state is not (
        JobState.awaiting_confirmation
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with rig.ctx.db.read() as conn:
        (scan_id,) = conn.execute(
            "SELECT id FROM scan WHERE state = 'awaiting_confirmation'"
        ).fetchone()
    discard_scan(rig.ctx.db, scan_id)
    finished(jobs, job_id)
    with rig.ctx.db.read() as conn:
        row = conn.execute(
            "SELECT outcome, discarded, new_files FROM run_summary WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row == ("stopped", 1, 0)  # stored within the table's CHECK; the API says "discarded"
    from sift.backup.summary import latest_summary

    summary = latest_summary(rig.ctx.db)
    assert summary is not None and summary["outcome"] == "discarded"


def test_a_stop_asked_for_as_the_run_ends_is_still_a_stop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = rig.jobs()

    def ends_normally_after_a_stop(_ctx: AppContext, ctl: JobControl, *_a: object) -> None:
        jobs.stop(ctl.job_id)  # the owner pressed Stop; the run returns without noticing

    monkeypatch.setattr(run_module, "_run", ends_normally_after_a_stop)
    job_id = jobs.start_backup(["Screenshots"])
    finished(jobs, job_id)
    ((_, outcome, *_rest),) = summary_rows(rig)
    assert outcome == "stopped"


def test_a_stall_during_the_scan_scans_again_instead_of_failing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_walk = rig.transport.walk
    stalls = [1]

    def stalling_walk(*args: object, **kwargs: object) -> object:
        if stalls:
            stalls.pop()
            raise DeviceTimeout("no data from the phone")
        return real_walk(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rig.transport, "walk", stalling_walk)
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(["Screenshots"]))
    assert snap.state is JobState.done and snap.error is None
    assert snap.files_done > 0 and stalls == []


class FakeSsd:
    """Stands in for SsdWatcher: an SSD destination row at a temp folder, plugged in or not."""

    def __init__(self, rig: Rig, root: Path) -> None:
        from sift.services.ssd import SsdState, ensure_destination

        self.connected = True
        rig.ctx.config.backup.ssd_volume_uuid = "uuid-ssd"
        root.parent.mkdir(parents=True, exist_ok=True)
        dest_id = ensure_destination(rig.ctx, "uuid-ssd", root, "SSD")
        self._on = SsdState(True, True, "SSD", str(root.parent), str(root), None, dest_id)
        self._off = SsdState(True, False)

    def state(self):
        return self._on if self.connected else self._off


def test_a_backup_with_the_ssd_plugged_in_ends_with_the_same_files_on_both(
    rig: Rig, tmp_path: Path
) -> None:
    rig.ctx.ssd = FakeSsd(rig, tmp_path / "ssd" / "backup")  # type: ignore[assignment]
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(ENABLED_SMALL))
    assert snap.state is JobState.done
    mac, ssd = tree(rig.root), tree(tmp_path / "ssd" / "backup")
    assert mac and mac == ssd
    with rig.ctx.db.read() as conn:
        per_dest = conn.execute("SELECT dest_id, count(*) FROM replica GROUP BY dest_id").fetchall()
    assert len(per_dest) == 2 and per_dest[0][1] == per_dest[1][1] == len(mac)


def test_without_the_ssd_the_run_says_what_is_pending_and_a_later_plug_in_catches_up(
    rig: Rig, tmp_path: Path
) -> None:
    from sift.backup.run import replicate_now

    fake = FakeSsd(rig, tmp_path / "ssd" / "backup")
    fake.connected = False
    rig.ctx.ssd = fake  # type: ignore[assignment]
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(ENABLED_SMALL))
    with rig.ctx.db.read() as conn:
        warnings = conn.execute("SELECT warnings FROM run_summary").fetchone()[0]
    assert "Mac done, SSD pending:" in warnings
    fake.connected = True
    replicate_now(rig.ctx, should_stop=lambda: False)
    assert tree(rig.root) == tree(tmp_path / "ssd" / "backup")


def test_p3_a_later_run_keeps_every_file_of_the_earlier_run(rig: Rig) -> None:
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(ENABLED_SMALL))
    first = tree(rig.root)
    phone = rig.transport.phone
    for path in list(phone.files)[:5]:  # files vanish from the phone and others change
        phone.remove(path)
    for path, file in list(phone.files.items())[:3]:
        phone.modify(path, b"changed " + path.encode(), file.mtime + 10)
    finished(jobs, jobs.start_backup(ENABLED_SMALL))
    second = tree(rig.root)
    assert all(second.get(rel) == data for rel, data in first.items())  # I1
