import contextlib
import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from sift.core.clock import ManualClock
from sift.core.context import AppContext
from sift.core.jobs import (
    REFERENCE_SPEED_BPS,
    JobConflict,
    JobControl,
    JobManager,
    JobNotFound,
    JobSnapshot,
    JobState,
    JobStopped,
    NoPhone,
)
from sift.core.paths import AppPaths
from sift.device.session import DeviceSession, PhoneIdentity, WrongDevice
from sift.device.transport import DeviceDisconnected
from sift.services.plainwords import GENERIC, describe
from tests.helpers import make_ctx

PHONE = PhoneIdentity("serial-a", "android-a")
OTHER = PhoneIdentity("serial-b", "android-b")


class FakeTracker:
    """Stands in for DeviceTracker: `session()` is whatever the test last set."""

    def __init__(self, identity: PhoneIdentity | None = PHONE) -> None:
        self._session: DeviceSession | None = None
        if identity is not None:
            self.connect(identity)

    def connect(self, identity: PhoneIdentity) -> DeviceSession:
        self._session = DeviceSession(object(), identity, "uuid-1")  # type: ignore[arg-type]
        return self._session

    def disconnect(self) -> None:
        self._session = None

    def session(self) -> DeviceSession | None:
        return self._session


def wait_until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out waiting for a condition"
        time.sleep(0.005)


@pytest.fixture
def ctx(paths: AppPaths) -> AppContext:
    c = make_ctx(paths)
    c.tracker = FakeTracker()  # type: ignore[assignment]
    return c


def manager(
    ctx: AppContext,
    runner: Callable[[JobControl], None],
    **kwargs: Any,
) -> JobManager:
    kwargs.setdefault("clock", ManualClock())
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("poll_s", 0.005)
    return JobManager(ctx, runner=runner, **kwargs)


def state_of(jobs: JobManager, job_id: int) -> JobSnapshot:
    return next(s for s in jobs.snapshot() if s.id == job_id)


def finished(jobs: JobManager, job_id: int) -> JobSnapshot:
    assert jobs.wait_finished(job_id, 5)
    return state_of(jobs, job_id)


class Driver:
    """A runner the test steers: `do(fn)` runs `fn(control)` on the job thread and waits."""

    def __init__(self) -> None:
        self._commands: queue.Queue[Callable[[JobControl], None] | None] = queue.Queue()
        self._acks: queue.Queue[BaseException | None] = queue.Queue()

    def runner(self, control: JobControl) -> None:
        while (command := self._commands.get(timeout=5)) is not None:
            try:
                command(control)
            except JobStopped:
                self._acks.put(None)
                raise
            self._acks.put(None)

    def do(self, command: Callable[[JobControl], None]) -> None:
        self._commands.put(command)
        self._acks.get(timeout=5)

    def end(self) -> None:
        self._commands.put(None)


def row(ctx: AppContext, job_id: int) -> dict[str, Any]:
    with ctx.db.read() as c:
        r = c.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        names = [d[0] for d in c.execute("SELECT * FROM job").description]
    return dict(zip(names, r, strict=True))


# -- starting ---------------------------------------------------------------------------------


def test_job_is_persisted_and_runs_to_done(ctx: AppContext) -> None:
    seen: list[tuple[tuple[str, ...], bool]] = []

    def runner(control: JobControl) -> None:
        seen.append((control.sets, control.dry_run))
        control.set_state(JobState.scanning)
        control.set_state(JobState.pulling)
        control.report(1, 10, 1, 10)

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera", "whatsapp"], dry_run=True)
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done
    assert seen == [(("camera", "whatsapp"), True)]
    r = row(ctx, job_id)
    assert r["type"] == "backup"
    assert r["state"] == "done"
    assert json.loads(r["params"]) == {"sets": ["camera", "whatsapp"], "dry_run": True}
    assert json.loads(r["progress"])["files_done"] == 1
    assert r["started_at"] is not None and r["finished_at"] is not None
    assert r["error"] is None


def test_one_backup_at_a_time(ctx: AppContext) -> None:
    gate = threading.Event()
    jobs = manager(ctx, lambda control: gate.wait(5))
    first = jobs.start_backup(["camera"])
    with pytest.raises(JobConflict):
        jobs.start_backup(["camera"])
    gate.set()
    finished(jobs, first)
    second = jobs.start_backup(["camera"])
    assert second != first
    finished(jobs, second)


def test_start_needs_a_connected_phone(ctx: AppContext) -> None:
    jobs = manager(ctx, lambda control: None)
    ctx.tracker.disconnect()  # type: ignore[union-attr]
    with pytest.raises(NoPhone):
        jobs.start_backup(["camera"])
    ctx.tracker = None
    with pytest.raises(NoPhone):
        jobs.start_backup(["camera"])


def test_start_needs_at_least_one_set(ctx: AppContext) -> None:
    jobs = manager(ctx, lambda control: None)
    with pytest.raises(ValueError):
        jobs.start_backup([])


def test_unknown_job_is_not_found(ctx: AppContext) -> None:
    jobs = manager(ctx, lambda control: None)
    for call in (jobs.pause, jobs.resume, jobs.stop):
        with pytest.raises(JobNotFound):
            call(99)


def test_job_thread_releases_its_read_connection(ctx: AppContext) -> None:
    def runner(control: JobControl) -> None:
        with ctx.db.read() as c:
            c.execute("SELECT 1").fetchone()

    before = ctx.db.open_read_connections()
    jobs = manager(ctx, runner)
    finished(jobs, jobs.start_backup(["camera"]))
    wait_until(lambda: ctx.db.open_read_connections() == before)


def test_stale_jobs_from_a_crashed_run_are_marked_failed(ctx: AppContext) -> None:
    with ctx.db.tx() as c:
        c.execute("INSERT INTO job (id, type, state) VALUES (7, 'backup', 'pulling')")
        c.execute("INSERT INTO job (id, type, state) VALUES (8, 'backup', 'done')")
    manager(ctx, lambda control: None)
    assert row(ctx, 7)["state"] == "failed"
    assert row(ctx, 7)["error"]
    assert row(ctx, 7)["finished_at"] is not None
    assert row(ctx, 8)["state"] == "done"


# -- pause and stop -----------------------------------------------------------------------------


def file_loop(started: list[int], gates: list[threading.Event]) -> Callable[[JobControl], None]:
    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        for i, gate in enumerate(gates):
            control.checkpoint()
            started.append(i)
            assert gate.wait(5)

    return runner


def test_pause_takes_effect_at_the_file_boundary_and_resume_continues(ctx: AppContext) -> None:
    started: list[int] = []
    gates = [threading.Event() for _ in range(3)]
    jobs = manager(ctx, file_loop(started, gates))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: started == [0])

    jobs.pause(job_id)
    assert state_of(jobs, job_id).state is JobState.pulling  # mid-file: nothing stops yet
    assert state_of(jobs, job_id).pause_requested
    gates[0].set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    time.sleep(0.05)
    assert started == [0]  # the next file did not start
    assert row(ctx, job_id)["state"] == "paused"

    jobs.resume(job_id)
    wait_until(lambda: started == [0, 1])
    assert state_of(jobs, job_id).state is JobState.pulling
    for g in gates:
        g.set()
    assert finished(jobs, job_id).state is JobState.done
    assert started == [0, 1, 2]


def test_stop_takes_effect_at_the_file_boundary(ctx: AppContext) -> None:
    started: list[int] = []
    gates = [threading.Event() for _ in range(4)]
    jobs = manager(ctx, file_loop(started, gates))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: started == [0])
    gates[0].set()
    wait_until(lambda: started == [0, 1])

    jobs.stop(job_id)
    assert state_of(jobs, job_id).state is JobState.stopping
    gates[1].set()  # the current file finishes ...
    snap = finished(jobs, job_id)
    assert started == [0, 1]  # ... and no new one starts
    assert snap.state is JobState.done
    assert snap.stopped
    assert json.loads(row(ctx, job_id)["progress"])["stopped"] is True


def test_stop_wakes_a_paused_job(ctx: AppContext) -> None:
    started: list[int] = []
    gates = [threading.Event() for _ in range(3)]
    jobs = manager(ctx, file_loop(started, gates))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: started == [0])
    jobs.pause(job_id)
    gates[0].set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    jobs.stop(job_id)
    snap = finished(jobs, job_id)
    assert snap.stopped and started == [0]


def test_finished_job_cannot_be_paused_or_resumed_and_stop_is_harmless(ctx: AppContext) -> None:
    jobs = manager(ctx, lambda control: None)
    job_id = jobs.start_backup(["camera"])
    finished(jobs, job_id)
    with pytest.raises(JobConflict):
        jobs.pause(job_id)
    with pytest.raises(JobConflict):
        jobs.resume(job_id)
    jobs.stop(job_id)
    assert state_of(jobs, job_id).state is JobState.done


# -- the phone going away --------------------------------------------------------------------


def test_disconnect_pauses_then_resumes_on_same_identity(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    sessions: list[DeviceSession] = []
    first_done = threading.Event()
    go = threading.Event()

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        sessions.append(control.wait_for_phone())
        first_done.set()
        assert go.wait(5)
        # The phone went away between two files: ask for it again before the next one.
        sessions.append(control.wait_for_phone())
        control.report(1, 1, 1, 1)

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    assert first_done.wait(5)
    tracker.disconnect()
    go.set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.waiting_for_phone)
    assert row(ctx, job_id)["state"] == "waiting_for_phone"

    replacement = tracker.connect(PhoneIdentity("serial-a", None))  # same phone: serial matches
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done
    assert sessions[1] is replacement  # the new session, never the cached old one
    assert sessions[0] is not replacement


def test_state_returns_to_pulling_after_the_phone_comes_back(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    go = threading.Event()
    done = threading.Event()

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        assert go.wait(5)
        control.wait_for_phone()
        assert done.wait(5)

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: state_of(jobs, job_id).state is JobState.pulling)
    tracker.disconnect()
    go.set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.waiting_for_phone)
    tracker.connect(PHONE)
    wait_until(lambda: state_of(jobs, job_id).state is JobState.pulling)
    done.set()
    finished(jobs, job_id)


def test_job_refuses_wrong_device(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    go = threading.Event()

    def runner(control: JobControl) -> None:
        assert go.wait(5)
        control.wait_for_phone()
        raise AssertionError("must not get here")

    jobs = manager(ctx, runner, describe=describe)
    job_id = jobs.start_backup(["camera"])
    tracker.connect(OTHER)  # a different phone has taken the place
    go.set()
    snap = finished(jobs, job_id)
    assert snap.state is JobState.failed
    assert snap.error is not None and "different phone" in snap.error
    assert "serial" not in snap.error
    assert row(ctx, job_id)["state"] == "failed"


def test_wrong_device_raised_by_the_runner_stops_the_job_in_plain_words(ctx: AppContext) -> None:
    def runner(control: JobControl) -> None:
        raise WrongDevice("A different phone answered on this connection.")

    jobs = manager(ctx, runner, describe=describe)
    snap = finished(jobs, jobs.start_backup(["camera"]))
    assert snap.state is JobState.failed
    assert snap.error == describe(WrongDevice("x"))


def test_unexpected_runner_error_fails_the_job_without_leaking_details(ctx: AppContext) -> None:
    def runner(control: JobControl) -> None:
        raise RuntimeError("/Users/someone/secret.jpg exploded")

    jobs = manager(ctx, runner, describe=describe)
    snap = finished(jobs, jobs.start_backup(["camera"]))
    assert snap.state is JobState.failed
    assert snap.error == GENERIC


def test_stop_wakes_a_job_waiting_for_the_phone(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    gate = threading.Event()

    def runner(control: JobControl) -> None:
        assert gate.wait(5)
        control.wait_for_phone()

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    tracker.disconnect()
    gate.set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.waiting_for_phone)
    jobs.stop(job_id)
    snap = finished(jobs, job_id)
    assert snap.stopped and snap.state is JobState.done


# -- progress ---------------------------------------------------------------------------------


def recorded_events(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def publish(name: str, data: dict[str, Any]) -> None:
        if name == "progress":
            events.append(data)

    monkeypatch.setattr(ctx.bus, "publish", publish)
    return events


def test_progress_is_throttled_to_one_event_per_500_ms(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = recorded_events(ctx, monkeypatch)
    clock = ManualClock()
    driver = Driver()
    jobs = manager(ctx, driver.runner, clock=clock)
    job_id = jobs.start_backup(["camera"])
    assert [e["state"] for e in events] == ["queued"]  # the start itself is announced

    for n in range(1, 11):  # ten reports in the same instant
        driver.do(lambda c, n=n: c.report(n, n * 10, 20, 200))
    assert [e["files_done"] for e in events] == [0]  # all dropped: the start was just announced

    clock.advance(0.4)
    driver.do(lambda c: c.report(11, 110, 20, 200))
    assert len(events) == 1  # 0.4 s: still throttled
    clock.advance(0.1)
    driver.do(lambda c: c.report(12, 120, 20, 200))
    assert [e["files_done"] for e in events] == [0, 12]  # 0.5 s: out it goes
    driver.do(lambda c: c.report(13, 130, 20, 200))
    assert len(events) == 2

    driver.do(lambda c: c.set_state(JobState.pulling))  # a state change always publishes
    assert [(e["state"], e["files_done"]) for e in events][-1] == ("pulling", 13)
    driver.end()
    finished(jobs, job_id)
    assert events[-1]["state"] == "done"


def test_speed_is_a_five_second_moving_average_and_eta_uses_it(ctx: AppContext) -> None:
    clock = ManualClock()
    driver = Driver()
    jobs = manager(ctx, driver.runner, clock=clock)
    job_id = jobs.start_backup(["camera"])
    mb = 1_000_000

    driver.do(lambda c: c.report(0, 0, 10, 100 * mb))
    snap = state_of(jobs, job_id)
    assert snap.speed_bps is None  # one sample: nothing measured yet
    assert snap.eta_basis == "reference"

    clock.advance(1)
    driver.do(lambda c: c.report(1, 10 * mb, 10, 100 * mb))
    snap = state_of(jobs, job_id)
    assert snap.speed_bps == pytest.approx(10 * mb)
    assert snap.eta_basis == "measured"
    assert snap.eta_s == pytest.approx(9.0)  # 90 MB left at 10 MB/s

    # The phone then runs at 40 MB/s for 6 s: the old, slower samples drop out of the window.
    for i in range(2, 8):
        clock.advance(1)
        driver.do(lambda c, i=i: c.report(i, 10 * mb + (i - 1) * 40 * mb, 10, 1000 * mb))
    snap = state_of(jobs, job_id)
    assert snap.speed_bps == pytest.approx(40 * mb, rel=0.01)
    driver.end()
    finished(jobs, job_id)


def test_files_matched_without_a_transfer_do_not_speed_up_the_eta(ctx: AppContext) -> None:
    clock = ManualClock()
    driver = Driver()
    jobs = manager(ctx, driver.runner, clock=clock)
    job_id = jobs.start_backup(["camera"])
    mb = 1_000_000

    driver.do(lambda c: c.report(0, 0, 10, 100 * mb))
    clock.advance(1)
    # 50 MB of files were done at once (moved files, nothing crossed), 10 MB really transferred.
    driver.do(
        lambda c: c.report(
            5, 60 * mb, 10, 100 * mb, duplicates_skipped=2, failed=1, transferred_bytes=10 * mb
        )
    )
    snap = state_of(jobs, job_id)
    assert snap.speed_bps == pytest.approx(10 * mb)
    assert snap.eta_s == pytest.approx(4.0)  # 40 MB left at the measured 10 MB/s
    assert (snap.duplicates_skipped, snap.failed) == (2, 1)
    assert json.loads(row(ctx, job_id)["progress"])["duplicates_skipped"] == 2
    assert json.loads(row(ctx, job_id)["progress"])["failed"] == 1
    driver.end()
    finished(jobs, job_id)


def test_on_finished_gets_the_final_snapshot_once(ctx: AppContext) -> None:
    seen: list[JobSnapshot] = []

    def runner(control: JobControl) -> None:
        control.report(1, 10, 1, 10, duplicates_skipped=1)

    jobs = manager(ctx, runner, on_finished=seen.append)
    job_id = jobs.start_backup(["camera"])
    assert jobs.wait_finished(job_id, 5)
    assert [(s.id, s.state, s.duplicates_skipped) for s in seen] == [(job_id, JobState.done, 1)]


def test_an_error_in_on_finished_does_not_break_the_job(ctx: AppContext) -> None:
    def broken(_snap: JobSnapshot) -> None:
        raise RuntimeError("boom")

    jobs = manager(ctx, lambda c: None, on_finished=broken)
    job_id = jobs.start_backup(["camera"])
    assert finished(jobs, job_id).state is JobState.done


def test_time_spent_waiting_does_not_drag_the_speed_down(ctx: AppContext) -> None:
    clock = ManualClock()
    driver = Driver()
    jobs = manager(ctx, driver.runner, clock=clock)
    job_id = jobs.start_backup(["camera"])
    driver.do(lambda c: c.report(0, 0, 10, 1000))
    clock.advance(1)
    driver.do(lambda c: c.report(1, 100, 10, 1000))
    jobs.pause(job_id)
    driver.do(lambda c: None)
    # (paused at the next checkpoint: drive that on the job thread, then resume from here)
    done = threading.Thread(target=lambda: driver.do(lambda c: c.checkpoint()))
    done.start()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    clock.advance(600)
    jobs.resume(job_id)
    done.join(5)
    clock.advance(1)
    driver.do(lambda c: c.report(2, 200, 10, 1000))
    clock.advance(1)
    driver.do(lambda c: c.report(3, 300, 10, 1000))
    snap = state_of(jobs, job_id)
    assert snap.speed_bps == pytest.approx(100)
    driver.end()
    finished(jobs, job_id)


def test_eta_falls_back_to_stored_reader_speed_then_the_reference(ctx: AppContext) -> None:
    release = threading.Event()
    reported = threading.Event()

    def runner(control: JobControl) -> None:
        control.report(0, 0, 10, 75_000_000)
        reported.set()
        assert release.wait(5)

    stored: list[float | None] = [20_000_000.0]
    jobs = manager(ctx, runner, stored_speed=lambda: stored[0])
    job_id = jobs.start_backup(["camera"])
    assert reported.wait(5)
    snap = state_of(jobs, job_id)
    assert snap.eta_basis == "stored"
    assert snap.eta_s == pytest.approx(75_000_000 / 20_000_000)
    stored[0] = None
    snap = state_of(jobs, job_id)
    assert snap.eta_basis == "reference"
    assert snap.eta_s == pytest.approx(75_000_000 / REFERENCE_SPEED_BPS)
    assert REFERENCE_SPEED_BPS == 37_500_000
    release.set()
    finished(jobs, job_id)


# -- retries and consecutive failures -----------------------------------------------------------


def test_retry_schedule_is_2_5_15_then_the_error_comes_back(ctx: AppContext) -> None:
    sleeps: list[float] = []
    calls: list[int] = []
    outcome: list[BaseException] = []

    def runner(control: JobControl) -> None:
        def boom() -> None:
            calls.append(1)
            raise DeviceDisconnected("gone")

        try:
            control.retry(boom, retry_on=(DeviceDisconnected,))
        except DeviceDisconnected as e:
            outcome.append(e)

    jobs = manager(ctx, runner, sleep=sleeps.append)
    finished(jobs, jobs.start_backup(["camera"]))
    assert sleeps == [2.0, 5.0, 15.0]
    assert len(calls) == 4  # the first try plus three retries
    assert len(outcome) == 1


def test_retry_returns_the_value_once_an_attempt_works(ctx: AppContext) -> None:
    sleeps: list[float] = []
    got: list[str] = []

    def runner(control: JobControl) -> None:
        attempts = iter([OSError("x"), OSError("y"), "ok"])

        def flaky() -> str:
            nxt = next(attempts)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        got.append(control.retry(flaky, retry_on=(OSError,)))

    jobs = manager(ctx, runner, sleep=sleeps.append)
    finished(jobs, jobs.start_backup(["camera"]))
    assert got == ["ok"]
    assert sleeps == [2.0, 5.0]


def test_retry_does_not_retry_errors_it_was_not_asked_to(ctx: AppContext) -> None:
    sleeps: list[float] = []
    jobs = manager(
        ctx, lambda c: c.retry(_raise_value_error, retry_on=(OSError,)), sleep=sleeps.append
    )
    snap = finished(jobs, jobs.start_backup(["camera"]))
    assert sleeps == []
    assert snap.state is JobState.failed


def _raise_value_error() -> None:
    raise ValueError("nope")


def test_stop_during_a_retry_wait_ends_the_job(ctx: AppContext) -> None:
    in_sleep = threading.Event()
    release = threading.Event()

    def sleep(_s: float) -> None:
        in_sleep.set()
        assert release.wait(5)

    def runner(control: JobControl) -> None:
        control.retry(lambda: _raise_os(), retry_on=(OSError,))

    jobs = manager(ctx, runner, sleep=sleep)
    job_id = jobs.start_backup(["camera"])
    assert in_sleep.wait(5)
    jobs.stop(job_id)
    release.set()
    snap = finished(jobs, job_id)
    assert snap.stopped


def _raise_os() -> None:
    raise OSError("x")


def test_default_retry_wait_is_cut_short_by_stop(ctx: AppContext) -> None:
    calls: list[int] = []

    def runner(control: JobControl) -> None:
        def boom() -> None:
            calls.append(1)
            raise OSError("x")

        control.retry(boom, retry_on=(OSError,))

    jobs = JobManager(ctx, clock=ManualClock(), runner=runner)  # real sleep: 2 s first wait
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: len(calls) == 1)
    started = time.monotonic()
    jobs.stop(job_id)
    snap = finished(jobs, job_id)
    assert time.monotonic() - started < 1.5
    assert snap.stopped and len(calls) == 1


def test_twenty_failures_in_a_row_pause_the_job_with_the_device_error(ctx: AppContext) -> None:
    files_tried: list[int] = []
    recovered = threading.Event()

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        for i in range(25):
            control.checkpoint()
            files_tried.append(i)
            # the file is failed; the run goes on to the next one
            with contextlib.suppress(DeviceDisconnected):
                control.retry(_raise_disconnected, retry_on=(DeviceDisconnected,))
        recovered.set()

    jobs = manager(ctx, runner, describe=describe)
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    snap = state_of(jobs, job_id)
    assert snap.error == "The phone disconnected."
    assert files_tried == list(range(20))  # the 20th failure was recorded, then the job paused
    assert row(ctx, job_id)["error"] == "The phone disconnected."

    jobs.resume(job_id)
    assert recovered.wait(5)  # failures 21..25: fewer than another 20, so no second pause
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done
    assert snap.error is None


def test_a_success_resets_the_failure_count(ctx: AppContext) -> None:
    def runner(control: JobControl) -> None:
        for i in range(60):
            control.checkpoint()
            ok = i % 10 == 9  # a success every 10th file: never 20 in a row
            with contextlib.suppress(DeviceDisconnected):
                control.retry(
                    (lambda: None) if ok else _raise_disconnected,
                    retry_on=(DeviceDisconnected,),
                )

    jobs = manager(ctx, runner)
    assert finished(jobs, jobs.start_backup(["camera"])).state is JobState.done


def _raise_disconnected() -> None:
    raise DeviceDisconnected("gone")


# -- stop_all ---------------------------------------------------------------------------------


def test_stop_all_stops_a_cooperative_job_within_its_timeout(ctx: AppContext) -> None:
    started: list[int] = []
    gates = [threading.Event() for _ in range(100)]
    jobs = manager(ctx, file_loop(started, gates))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: started == [0])
    threading.Timer(0.1, gates[0].set).start()  # the current file finishes shortly
    t0 = time.monotonic()
    jobs.stop_all(5)
    assert time.monotonic() - t0 < 2
    snap = state_of(jobs, job_id)
    assert snap.state is JobState.done and snap.stopped
    assert started == [0]
    assert row(ctx, job_id)["state"] == "done"


def test_stop_all_with_nothing_running_returns_at_once(ctx: AppContext) -> None:
    jobs = manager(ctx, lambda control: None)
    jobs.stop_all(0.1)
    assert jobs.snapshot() == []


def test_start_is_refused_while_closing(ctx: AppContext) -> None:
    jobs = manager(ctx, lambda control: None)
    jobs.stop_all(0.1)
    with pytest.raises(JobConflict):
        jobs.start_backup(["camera"])


def test_stop_all_abandons_a_job_that_will_not_stop_and_db_close_cannot_race_it(
    ctx: AppContext,
) -> None:
    stuck = threading.Event()
    release = threading.Event()
    late: list[BaseException] = []

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        stuck.set()
        release.wait(10)  # ignores the stop request, like a copy stuck on a dead cable
        try:
            control.report(5, 5, 5, 5)  # late calls from an abandoned job do nothing
            with ctx.db.tx() as c:  # the runner's own write after the database was closed
                c.execute("UPDATE job SET state = 'done'")
        except Exception as e:
            late.append(e)
            raise

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    assert stuck.wait(5)
    t0 = time.monotonic()
    jobs.stop_all(0.2)
    assert time.monotonic() - t0 < 2
    snap = state_of(jobs, job_id)
    assert snap.state is JobState.failed
    assert snap.error and "stop" in snap.error
    r = row(ctx, job_id)
    assert r["state"] == "failed" and r["error"] == snap.error and r["finished_at"] is not None

    ctx.db.close()  # Shutdown's next step
    release.set()  # now the stuck runner wakes up, finds the database gone and fails
    thread = next(t for t in threading.enumerate() if t.name == f"sift-job-{job_id}")
    thread.join(5)
    assert not thread.is_alive()
    assert len(late) == 1  # its own tx() failed ...
    assert state_of(jobs, job_id).state is JobState.failed  # ... and nothing rewrote the outcome


# -- no lock held across the database or the bus (fix round 1) -------------------------------


def call_bounded(fn: Callable[[], Any], timeout: float = 5.0) -> Any:
    """Run `fn` on a thread; fail the test (instead of hanging it) if it does not return."""
    box: list[Any] = []
    thread = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "call did not return: a lock is held across I/O"
    return box[0] if box else None


class TickingClock(ManualClock):
    """Every `monotonic()` call moves time on, so the 500 ms throttle fires all the time."""

    def monotonic(self) -> float:
        self.advance(0.2)
        return super().monotonic()


def test_runner_and_api_threads_never_deadlock_over_pause_resume_stop(ctx: AppContext) -> None:
    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        for i in range(100_000):
            control.checkpoint()
            control.report(i, i * 10, 100_000, 1_000_000)
            if i % 7 == 0:
                control.set_state(JobState.scanning)
                control.set_state(JobState.pulling)

    jobs = manager(ctx, runner, clock=TickingClock())
    job_id = jobs.start_backup(["camera"])
    stop_flag = threading.Event()

    def api() -> None:
        while not stop_flag.is_set():
            with contextlib.suppress(JobConflict):
                jobs.pause(job_id)
                jobs.snapshot()
                jobs.resume(job_id)
            time.sleep(0.003)  # let the runner through a checkpoint now and then

    threads = [threading.Thread(target=api, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    wait_until(lambda: state_of(jobs, job_id).files_done > 20, timeout=30)
    call_bounded(lambda: jobs.stop(job_id))
    stop_flag.set()
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    assert jobs.wait_finished(job_id, 10)
    assert row(ctx, job_id)["state"] == "done"  # the last write won: nothing older overwrote it


def test_a_slow_database_write_does_not_block_snapshot_or_other_callers(ctx: AppContext) -> None:
    started: list[int] = []
    gates = [threading.Event() for _ in range(3)]
    jobs = manager(ctx, file_loop(started, gates))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: started == [0])

    release_db = threading.Event()
    holding = threading.Event()

    def hold_write_lock() -> None:
        with ctx.db.tx():
            holding.set()
            release_db.wait(10)

    holder = threading.Thread(target=hold_write_lock, daemon=True)
    holder.start()
    assert holding.wait(5)
    pauser = threading.Thread(target=jobs.pause, args=(job_id,), daemon=True)
    pauser.start()  # takes the lock, flags the pause, then waits for the database outside it
    # snapshot() needs the job's lock: it must be free although a write is stuck.
    wait_until(lambda: call_bounded(lambda: state_of(jobs, job_id)).pause_requested)
    assert call_bounded(jobs.snapshot)
    release_db.set()
    pauser.join(5)
    holder.join(5)
    gates[0].set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    assert row(ctx, job_id)["state"] == "paused"
    jobs.stop(job_id)
    finished(jobs, job_id)


def test_database_knows_when_a_thread_is_inside_a_write(ctx: AppContext) -> None:
    assert not ctx.db.in_write_tx()
    with ctx.db.tx():
        assert ctx.db.in_write_tx()
        assert call_bounded(ctx.db.in_write_tx) is False  # per thread
    assert not ctx.db.in_write_tx()


def test_job_control_inside_a_db_transaction_fails_loudly_instead_of_hanging(
    ctx: AppContext,
) -> None:
    errors: list[str] = []

    def runner(control: JobControl) -> None:
        calls: list[Callable[[], Any]] = [
            lambda: control.report(1, 1, 1, 1),
            lambda: control.set_state(JobState.pulling),
            control.checkpoint,
            control.wait_for_phone,
            lambda: control.retry(lambda: None, retry_on=(OSError,)),
            lambda: control.on_phone(lambda s: None, disconnect_on=(OSError,)),
        ]
        for call in calls:
            with ctx.db.tx():
                try:
                    call()
                except RuntimeError as e:
                    errors.append(str(e))
        control.report(2, 2, 2, 2)  # outside a transaction it is fine again

    jobs = manager(ctx, runner, clock=TickingClock())
    snap = finished(jobs, jobs.start_backup(["camera"]))
    assert len(errors) == 6
    assert all("transaction" in e for e in errors)
    assert snap.state is JobState.done and snap.files_done == 2


# -- stop_all keeps its deadline (fix round 1) ---------------------------------------------------


def test_stop_all_keeps_its_deadline_when_a_job_thread_holds_the_job_lock(
    ctx: AppContext,
) -> None:
    holding = threading.Event()
    release = threading.Event()

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        with control._job.cond:  # a thread stuck while it holds the job's lock
            holding.set()
            release.wait(10)

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    assert holding.wait(5)
    t0 = time.monotonic()
    jobs.stop_all(0.3)
    assert time.monotonic() - t0 < 1.5
    r = row(ctx, job_id)
    assert r["state"] == "failed" and r["error"] and r["finished_at"] is not None
    release.set()
    assert jobs.wait_finished(job_id, 5)
    assert row(ctx, job_id)["state"] == "failed"  # the late outcome did not overwrite it
    assert state_of(jobs, job_id).state is JobState.failed


def test_stop_all_stops_paused_and_waiting_jobs(ctx: AppContext) -> None:
    started: list[int] = []
    gates = [threading.Event() for _ in range(3)]
    jobs = manager(ctx, file_loop(started, gates))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: started == [0])
    jobs.pause(job_id)
    gates[0].set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    jobs.stop_all(5)
    assert state_of(jobs, job_id).state is JobState.done
    assert state_of(jobs, job_id).stopped


def test_stop_all_stops_a_job_waiting_for_the_phone(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    gate = threading.Event()

    def runner(control: JobControl) -> None:
        assert gate.wait(5)
        control.wait_for_phone()

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    tracker.disconnect()
    gate.set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.waiting_for_phone)
    jobs.stop_all(5)
    assert state_of(jobs, job_id).state is JobState.done


def test_pause_while_waiting_for_the_phone_takes_effect_after_it_returns(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    gate = threading.Event()

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        assert gate.wait(5)
        control.wait_for_phone()
        control.checkpoint()

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: state_of(jobs, job_id).state is JobState.pulling)
    tracker.disconnect()
    gate.set()
    wait_until(lambda: state_of(jobs, job_id).state is JobState.waiting_for_phone)
    jobs.pause(job_id)
    assert state_of(jobs, job_id).state is JobState.waiting_for_phone
    tracker.connect(PHONE)
    wait_until(lambda: state_of(jobs, job_id).state is JobState.paused)
    jobs.resume(job_id)
    assert finished(jobs, job_id).state is JobState.done


# -- on_phone: wait for the phone, then redo the piece of work -------------------------------


def test_on_phone_redoes_the_work_on_the_new_session_after_a_disconnect(ctx: AppContext) -> None:
    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    seen: list[DeviceSession] = []
    first_try = threading.Event()
    results: list[str] = []

    def work(session: DeviceSession) -> str:
        seen.append(session)
        if len(seen) == 1:
            tracker.disconnect()  # the cable is pulled mid-file
            first_try.set()
            raise DeviceDisconnected("gone")
        return "copied"

    def runner(control: JobControl) -> None:
        results.append(control.on_phone(work, disconnect_on=(DeviceDisconnected,)))

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    assert first_try.wait(5)
    wait_until(lambda: state_of(jobs, job_id).state is JobState.waiting_for_phone)
    replacement = tracker.connect(PHONE)
    assert finished(jobs, job_id).state is JobState.done
    assert results == ["copied"]
    assert seen[1] is replacement and seen[0] is not replacement


def test_on_phone_does_not_swallow_other_errors_and_stop_ends_the_wait(ctx: AppContext) -> None:
    jobs = manager(
        ctx, lambda c: c.on_phone(lambda s: _raise_value_error(), disconnect_on=(OSError,))
    )
    assert finished(jobs, jobs.start_backup(["camera"])).state is JobState.failed

    tracker: FakeTracker = ctx.tracker  # type: ignore[assignment]
    calls: list[int] = []

    def work(session: DeviceSession) -> None:
        calls.append(1)
        tracker.disconnect()
        raise DeviceDisconnected("gone")

    jobs2 = manager(ctx, lambda c: c.on_phone(work, disconnect_on=(DeviceDisconnected,)))
    job_id = jobs2.start_backup(["camera"])
    wait_until(lambda: state_of(jobs2, job_id).state is JobState.waiting_for_phone)
    jobs2.stop(job_id)
    snap = finished(jobs2, job_id)
    assert snap.stopped and len(calls) == 1


# -- waiting for something else (the owner's confirmation) -----------------------------------------


def test_wait_until_returns_once_the_condition_holds(ctx: AppContext) -> None:
    flag = threading.Event()
    results: list[bool] = []

    def runner(control: JobControl) -> None:
        control.set_state(JobState.awaiting_confirmation)
        results.append(control.wait_until(flag.is_set))

    jobs = manager(ctx, runner)
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: state_of(jobs, job_id).state is JobState.awaiting_confirmation)
    flag.set()
    assert finished(jobs, job_id).state is JobState.done
    assert results == [True]


def test_wait_until_gives_up_after_its_timeout(ctx: AppContext) -> None:
    results: list[bool] = []
    jobs = manager(ctx, lambda c: results.append(c.wait_until(lambda: False, timeout_s=0.05)))
    assert finished(jobs, jobs.start_backup(["camera"])).state is JobState.done
    assert results == [False]


def test_stop_ends_a_wait_until(ctx: AppContext) -> None:
    calls: list[int] = []

    def never() -> bool:
        calls.append(1)
        return False

    jobs = manager(ctx, lambda c: c.wait_until(never))
    job_id = jobs.start_backup(["camera"])
    wait_until(lambda: len(calls) >= 2)
    jobs.stop(job_id)
    snap = finished(jobs, job_id)
    assert snap.state is JobState.done and snap.stopped


def test_pause_requested_is_visible_to_the_runner(ctx: AppContext) -> None:
    driver = Driver()
    jobs = manager(ctx, driver.runner)
    job_id = jobs.start_backup(["camera"])
    seen: list[bool] = []
    driver.do(lambda c: seen.append(c.pause_requested))
    jobs.pause(job_id)
    driver.do(lambda c: seen.append(c.pause_requested))
    jobs.resume(job_id)
    driver.do(lambda c: seen.append(c.pause_requested))
    driver.end()
    assert finished(jobs, job_id).state is JobState.done
    assert seen == [False, True, False]
