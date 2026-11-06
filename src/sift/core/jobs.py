"""Backup jobs: one thread per job, persisted to the `job` table, with pause, stop and progress.

`JobManager` owns the threads and the rules; the work itself is an injected `runner(control)`
(the real scan, guard and pull is the backup engine's, not this module's). The runner talks to
its job only through `JobControl`:

- `checkpoint()` at every file boundary: blocks while the job is paused, raises `JobStopped`
  once it is stopped. Pause and Stop therefore never cut a file in half.
- `report(...)` for progress, `set_state(...)` for scanning / awaiting_confirmation / pulling.
- `wait_for_phone()` before every piece of work: hands back the tracker's *current* session once
  it matches the phone the job started on (blocking as `waiting_for_phone` until then). Sessions
  are replaced on reconnect, so a runner never keeps one across a disconnect.
- `retry(fn, retry_on=...)`: 2 s, 5 s, 15 s between attempts, then the error comes back to the
  runner (which marks the file failed). Twenty such failures in a row pause the job at the next
  checkpoint, with the device error in plain words.

Locking: each job has one `cond` that guards its fields. It is held only for in-memory changes,
never across a database write or a bus publish: a change takes a numbered snapshot under the
lock, and the snapshot is persisted and published after the lock is released (a snapshot older
than one already written is dropped, so the database and the UI never go back in time). Because
of that, `JobControl` methods must not be called from inside a `db.tx()` on the same thread: the
write lock is not reentrant, so the job's own write would wait for the caller for ever. They
check, and raise `RuntimeError` at once with a clear message, instead of hanging.

Layering: this module is in `sift.core`, which must not import `sift.device` or
`sift.services`. So the plain-words function is injected (`describe`), the tracker is reached
through `ctx.tracker`, and device types appear only in `TYPE_CHECKING` imports and are used by
duck typing (`session.identity.matches(...)`).

What a stopped job becomes: it ends in state `done` with `stopped` set (there is no separate
"stopped" state). A job that fails ends `failed` with a plain-words `error`.
"""

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sift.core.clock import Clock
from sift.core.context import AppContext

if TYPE_CHECKING:
    from sift.device.session import DeviceSession, PhoneIdentity

_log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 0.5
SPEED_WINDOW_S = 5.0
RETRY_DELAYS_S = (2.0, 5.0, 15.0)
MAX_CONSECUTIVE_FAILURES = 20
# 37.5 MB/s: what USB 2 gives, used for the ETA until anything has been measured.
REFERENCE_SPEED_BPS = 37_500_000
POLL_PHONE_S = 0.25
# stop_all gives a lock this long even when its deadline has passed, so a healthy thread that
# holds it for a moment is not mistaken for a stuck one.
ABANDON_GRACE_S = 0.1

WRONG_PHONE_MESSAGE = (
    "A different phone is connected, not the one this backup started on. "
    "Connect that phone to continue."
)
CLOSING_MESSAGE = "Sift is closing, so a new backup cannot start."
NO_PHONE_MESSAGE = "Connect your phone to start a backup."
ABANDONED_MESSAGE = "Sift quit before this backup could stop, so it was left unfinished."
STALE_MESSAGE = "Sift was closed before this backup finished."
GENERIC_MESSAGE = "Something went wrong. Details are in Logs."


class JobState(StrEnum):
    queued = "queued"
    scanning = "scanning"
    awaiting_confirmation = "awaiting_confirmation"
    pulling = "pulling"
    paused = "paused"
    waiting_for_phone = "waiting_for_phone"
    stopping = "stopping"
    done = "done"
    failed = "failed"


TERMINAL = frozenset({JobState.done, JobState.failed})
# The states a runner may choose itself; the rest belong to the manager.
RUNNER_STATES = frozenset(
    {JobState.queued, JobState.scanning, JobState.awaiting_confirmation, JobState.pulling}
)


def _left(deadline: float) -> float:
    """Seconds until a `time.monotonic()` deadline, never negative."""
    return max(0.0, deadline - time.monotonic())


class JobError(Exception):
    """A request about jobs that cannot be done. `code` and `message` are for the API layer.

    The server maps `JobNotFound` to 404 and every other `JobError` to 409.
    """

    code = "job_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JobNotFound(JobError):
    code = "job_not_found"


class JobConflict(JobError):
    code = "job_conflict"


class NoPhone(JobConflict):
    code = "no_phone"


class JobStopped(Exception):
    """Raised inside a runner when its job was stopped (or abandoned): unwind and return.

    `JobControl.retry` never retries it, and the sticky stop flag means a runner that swallows it
    meets it again at the next checkpoint.
    """


class JobRefused(Exception):
    """Raised inside a runner to end its job as failed with this plain message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class JobSnapshot:
    id: int
    type: str
    state: JobState
    sets: tuple[str, ...]
    dry_run: bool
    files_done: int
    bytes_done: int
    files_total: int | None
    bytes_total: int | None
    current: str | None
    speed_bps: float | None  # the measured 5 s average, or None until there is one
    eta_s: float | None
    eta_basis: str  # "measured", "stored" (first-run reader test) or "reference" (37.5 MB/s)
    pause_requested: bool
    stopped: bool
    error: str | None
    started_at: int | None
    finished_at: int | None

    def as_event(self) -> dict[str, Any]:
        data = asdict(self)
        data["job_id"] = data.pop("id")
        data["state"] = self.state.value
        data["sets"] = list(self.sets)
        return data


class _Job:
    """One job's mutable state. Every field is guarded by `cond` (an RLock underneath)."""

    def __init__(
        self, job_id: int, sets: tuple[str, ...], dry_run: bool, identity: PhoneIdentity
    ) -> None:
        self.id = job_id
        self.type = "backup"
        self.sets = sets
        self.dry_run = dry_run
        self.identity = identity
        self.cond = threading.Condition(threading.RLock())
        self.state = JobState.queued
        self.stop_requested = False
        self.pause_requested = False
        self.pause_reason: str | None = None
        self.abandoned = False
        self.stopped = False
        self.error: str | None = None
        self.files_done = 0
        self.bytes_done = 0
        self.files_total: int | None = None
        self.bytes_total: int | None = None
        self.current: str | None = None
        self.consecutive_failures = 0
        self.samples: deque[tuple[float, int]] = deque()
        self.last_published: float | None = None
        self.started_at: int | None = None
        self.finished_at: int | None = None
        self.thread: threading.Thread | None = None
        # Snapshots are numbered under `cond`; `write_lock` orders their persisting. It is never
        # held while taking `cond`, and nobody holding the database write lock takes it.
        self.seq = 0
        self.written_seq = 0
        self.write_lock = threading.Lock()


@dataclass(frozen=True)
class _Capture:
    """A job's state at one moment, taken under its lock and written after the lock is gone."""

    seq: int
    id: int
    type: str
    state: JobState
    sets: tuple[str, ...]
    dry_run: bool
    files_done: int
    bytes_done: int
    files_total: int | None
    bytes_total: int | None
    current: str | None
    measured_speed: float | None
    pause_requested: bool
    stopped: bool
    error: str | None
    started_at: int | None
    finished_at: int | None


class JobControl:
    """A runner's handle on its job. Only the job's own thread should call these.

    Never call a method here from inside a `db.tx()`: each one may write the job row, and the
    database write lock is not reentrant. A call made that way raises `RuntimeError` (it is a
    bug to fix in the runner, caught at once instead of hanging the job). Call `checkpoint()`
    and `report()` before opening a transaction or after it commits.
    """

    def __init__(self, manager: JobManager, job: _Job) -> None:
        self._mgr = manager
        self._job = job

    def _guard(self) -> None:
        if self._mgr._db.in_write_tx():
            raise RuntimeError(
                "JobControl was used inside a database transaction; "
                "call it before or after db.tx(), never inside"
            )

    @property
    def job_id(self) -> int:
        return self._job.id

    @property
    def sets(self) -> tuple[str, ...]:
        return self._job.sets

    @property
    def dry_run(self) -> bool:
        return self._job.dry_run

    @property
    def identity(self) -> PhoneIdentity:
        """The phone this job started on."""
        return self._job.identity

    @property
    def stop_requested(self) -> bool:
        return self._job.stop_requested or self._job.abandoned

    def checkpoint(self) -> None:
        """Call between files. Blocks while paused; raises `JobStopped` once stopped."""
        self._guard()
        mgr, job = self._mgr, self._job
        with job.cond:
            mgr._raise_if_stopped(job)
            if not job.pause_requested:
                return
            previous = job.state
            job.error = job.pause_reason
            capture = mgr._transition(job, JobState.paused)
        mgr._flush(job, capture)
        with job.cond:
            while job.pause_requested and not job.stop_requested and not job.abandoned:
                job.cond.wait()
            mgr._raise_if_stopped(job)
            job.pause_reason = None
            job.error = None
            capture = mgr._transition(job, previous)
        mgr._flush(job, capture)

    def report(
        self,
        files_done: int,
        bytes_done: int,
        files_total: int | None = None,
        bytes_total: int | None = None,
        *,
        current: str | None = None,
    ) -> None:
        """Record progress. At most one `progress` event per 500 ms goes out."""
        self._guard()
        mgr, job = self._mgr, self._job
        capture = None
        with job.cond:
            if job.abandoned:
                return
            job.files_done, job.bytes_done = files_done, bytes_done
            job.files_total, job.bytes_total = files_total, bytes_total
            job.current = current
            now = mgr._clock.monotonic()
            job.samples.append((now, bytes_done))
            while len(job.samples) > 2 and job.samples[1][0] <= now - SPEED_WINDOW_S:
                job.samples.popleft()
            if job.last_published is None or now - job.last_published >= PROGRESS_INTERVAL_S:
                job.last_published = now
                capture = mgr._capture(job)
        mgr._flush(job, capture)

    def set_state(self, state: JobState) -> None:
        """Move between scanning, awaiting_confirmation, pulling (and queued).

        Ignored once a stop was requested: the job stays `stopping`.
        """
        self._guard()
        if state not in RUNNER_STATES:
            raise ValueError(f"A runner cannot set the state {state.value!r}")
        with self._job.cond:
            capture = self._mgr._transition(self._job, state)
        self._mgr._flush(self._job, capture)

    def wait_for_phone(self) -> DeviceSession:
        """The tracker's current session for this job's phone, waiting for it if need be.

        While there is none the job shows `waiting_for_phone`; it goes back to what it was
        when the phone returns. A session whose identity does not match raises `JobRefused`
        (the job fails with a plain message). `JobStopped` ends the wait on stop.
        """
        self._guard()
        mgr, job = self._mgr, self._job
        previous: JobState | None = None
        try:
            while True:
                with job.cond:
                    mgr._raise_if_stopped(job)
                session = mgr._current_session()  # asked outside the lock: it is the tracker's
                if session is not None:
                    if session.identity.matches(job.identity):
                        return session
                    raise JobRefused(WRONG_PHONE_MESSAGE)
                with job.cond:
                    mgr._raise_if_stopped(job)
                    capture = None
                    if previous is None:
                        previous = job.state
                        capture = mgr._transition(job, JobState.waiting_for_phone)
                    else:
                        job.cond.wait(mgr._poll_s)
                mgr._flush(job, capture)
        finally:
            if previous is not None:
                with job.cond:
                    capture = mgr._transition(job, previous)
                mgr._flush(job, capture)

    def on_phone[T](
        self,
        work: Callable[[DeviceSession], T],
        *,
        disconnect_on: tuple[type[BaseException], ...],
    ) -> T:
        """Run one piece of work on the phone, redoing it whenever the phone goes away.

        Each round fetches the tracker's current session (`wait_for_phone`, so a different
        phone fails the job and stop ends the wait), calls `work(session)` and returns its
        value. If `work` raises one of `disconnect_on`, the job waits for the phone and calls
        `work` again with the new session, however many times that takes (a disconnect is not
        a failed file and does not count toward the 20-in-a-row pause). Any other error goes
        to the caller. `work` must be safe to redo from the start (copy to a fresh temporary
        file). While the tracker still hands out the dead session, the retry is paced at the
        phone-poll interval and stop is honoured.
        """
        mgr, job = self._mgr, self._job
        while True:
            session = self.wait_for_phone()
            try:
                return work(session)
            except disconnect_on:
                with job.cond:
                    mgr._raise_if_stopped(job)
                    job.cond.wait(mgr._poll_s)

    def retry[T](self, fn: Callable[[], T], *, retry_on: tuple[type[BaseException], ...]) -> T:
        """Call `fn`; on an error in `retry_on` wait 2 s, 5 s, then 15 s and try again.

        After the last retry the error is raised to the runner, which marks the file failed.
        Each such failure counts toward 20 in a row (a success resets the count); the 20th
        pauses the job at its next checkpoint with the error in plain words. Choose `retry_on`
        carefully: a disconnect is `wait_for_phone`'s business, not a failed file.
        """
        self._guard()
        mgr, job = self._mgr, self._job
        attempt = 0
        while True:
            with job.cond:
                mgr._raise_if_stopped(job)
            try:
                value = fn()
            except JobStopped:
                raise
            except retry_on as exc:
                if attempt >= len(RETRY_DELAYS_S):
                    self._file_failed(exc)
                    raise
                self._wait(RETRY_DELAYS_S[attempt])
                attempt += 1
            else:
                with job.cond:
                    job.consecutive_failures = 0
                return value

    def _file_failed(self, exc: BaseException) -> None:
        mgr, job = self._mgr, self._job
        with job.cond:
            job.consecutive_failures += 1
            if job.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                job.consecutive_failures = 0
                job.pause_reason = mgr._describe_safely(exc)
                job.pause_requested = True

    def _wait(self, seconds: float) -> None:
        mgr, job = self._mgr, self._job
        if mgr._sleep is not None:
            mgr._sleep(seconds)
            return
        with job.cond:
            job.cond.wait_for(lambda: job.stop_requested or job.abandoned, timeout=seconds)


class JobManager:
    def __init__(
        self,
        ctx: AppContext,
        *,
        clock: Clock,
        runner: Callable[[JobControl], None],
        sleep: Callable[[float], None] | None = None,
        describe: Callable[[BaseException], str] | None = None,
        stored_speed: Callable[[], float | None] | None = None,
        poll_s: float = POLL_PHONE_S,
    ) -> None:
        """`sleep` replaces the retry waits (tests); the default waits on the job's stop flag.

        `describe` turns an exception into plain words for the job's `error` (pass
        `sift.services.plainwords.describe`; core cannot import it). `stored_speed` returns the
        first-run reader-test speed in bytes per second, if one was measured. Any job left
        unfinished by an earlier run is marked failed here.
        """
        self._ctx = ctx
        self._db = ctx.db
        self._clock = clock
        self._runner = runner
        self._sleep = sleep
        self._describe = describe
        self._stored_speed = stored_speed
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._jobs: dict[int, _Job] = {}
        self._closing = False
        self._starting = False  # a start_backup is between its checks and registering the job
        self._fail_stale_jobs()

    # -- requests ---------------------------------------------------------------------------

    def start_backup(self, sets: Sequence[str], *, dry_run: bool = False) -> int:
        """Start a backup of `sets` on the connected phone; returns the job id.

        Raises `NoPhone` with no connected phone, and `JobConflict` while another backup is
        active (scans are serialised: one backup at a time) or Sift is closing.
        """
        names = tuple(sets)
        if not names:
            raise ValueError("A backup needs at least one set")
        tracker = self._ctx.tracker
        session = tracker.session() if tracker is not None else None
        if session is None:
            raise NoPhone(NO_PHONE_MESSAGE)
        with self._lock:
            if self._closing:
                raise JobConflict(CLOSING_MESSAGE)
            if self._starting or any(
                j.state not in TERMINAL for j in self._jobs.values() if j.type == "backup"
            ):
                raise JobConflict("A backup is already running.")
            self._starting = True  # held across the insert below, which must not hold `_lock`
        now = self._clock.now()
        params = json.dumps({"sets": list(names), "dry_run": dry_run})
        try:
            with self._db.tx() as c:
                cur = c.execute(
                    "INSERT INTO job (type, state, params, started_at) VALUES ('backup', ?, ?, ?)",
                    (JobState.queued.value, params, now),
                )
                assert cur.lastrowid is not None
                job_id = cur.lastrowid
        except BaseException:
            with self._lock:
                self._starting = False
            raise
        job = _Job(job_id, names, dry_run, session.identity)
        job.started_at = now
        job.thread = threading.Thread(
            target=self._run, args=(job,), name=f"sift-job-{job_id}", daemon=True
        )
        with self._lock:
            self._starting = False
            closing = self._closing
            if not closing:
                self._jobs[job_id] = job
        if closing:  # Sift began closing while the row was being written
            self._mark_failed(job_id, CLOSING_MESSAGE)
            raise JobConflict(CLOSING_MESSAGE)
        with job.cond:
            job.last_published = self._clock.monotonic()
            capture = self._capture(job)
        self._flush(job, capture)
        job.thread.start()
        return job_id

    def pause(self, job_id: int) -> None:
        """Pause at the next file boundary. `JobConflict` if the job has finished or is stopping."""
        job = self._get(job_id)
        with job.cond:
            if job.state in TERMINAL or job.stop_requested:
                raise JobConflict("This backup is not running.")
            job.pause_requested = True
            capture = self._capture(job)
        self._flush(job, capture)

    def resume(self, job_id: int) -> None:
        """Let a paused (or pausing) job go on. `JobConflict` if the job has finished."""
        job = self._get(job_id)
        with job.cond:
            if job.state in TERMINAL:
                raise JobConflict("This backup has finished.")
            job.pause_requested = False
            job.pause_reason = None
            job.cond.notify_all()
            capture = None if job.state is JobState.paused else self._capture(job)
        self._flush(job, capture)

    def stop(self, job_id: int) -> None:
        """Stop at the next file boundary (the current file is finished first). Idempotent."""
        job = self._get(job_id)
        with job.cond:
            capture = self._stop_locked(job)
        self._flush(job, capture)

    def stop_all(self, timeout_s: float) -> None:
        """Shutdown's jobs step: stop every active job and wait up to `timeout_s` in all.

        New backups are refused from here on. The deadline is firm: every lock and join here
        waits no longer than what is left of it, so a job thread stuck holding its lock or
        inside a database write cannot stall shutdown. A job still alive when the time is up
        (or whose lock could not be taken in time) is *abandoned*: marked `failed` in the
        database with a plain reason (never left `running`), its later progress and outcome
        writes are discarded (the database refuses to move a `failed` row, and its `JobControl`
        calls raise `JobStopped`). The manager's own writes never hold a job's lock and an
        abandoned job's are refused, so only a write already begun when the time ran out can
        still be in flight, and `Database.close` waits for the write lock like any writer. A
        runner that is still alive and writes on its own (its own `db.tx()`) is not covered by
        that; it finds the database closed and fails inside its own thread, which is caught and
        discarded here (the job is already `failed`).
        """
        deadline = time.monotonic() + timeout_s
        with self._lock:
            self._closing = True
            jobs = [j for j in self._jobs.values() if j.state not in TERMINAL]
        for job in jobs:
            if job.cond.acquire(timeout=_left(deadline)):
                try:
                    capture = self._stop_locked(job)
                finally:
                    job.cond.release()
                self._flush(job, capture, deadline=deadline)
        for job in jobs:
            if job.thread is not None:
                job.thread.join(_left(deadline))
        for job in jobs:
            if job.state not in TERMINAL:
                self._abandon(job, deadline)

    def _abandon(self, job: _Job, deadline: float) -> None:
        _log.warning("Job %d did not stop in time; abandoning it", job.id)
        locked = job.cond.acquire(timeout=max(_left(deadline), ABANDON_GRACE_S))
        try:
            if locked and job.state in TERMINAL:
                return  # it finished by itself just now
            # Without the lock this is a plain assignment: from here on the job's own writes stop.
            job.abandoned = True
            job.error = ABANDONED_MESSAGE
            job.pause_requested = False
            capture = self._transition(job, JobState.failed)
            if locked:
                job.cond.notify_all()
        finally:
            if locked:
                job.cond.release()
        self._flush(job, capture, force=True, deadline=deadline)

    def snapshot(self) -> list[JobSnapshot]:
        """Every job of this run, oldest first."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.id)
        captures = []
        for job in jobs:
            with job.cond:
                captures.append(self._capture(job, number=False))
        return [self._to_snapshot(c) for c in captures]

    def wait_finished(self, job_id: int, timeout_s: float) -> bool:
        """Block until the job's thread has ended; True if it has (for tests and shutdown)."""
        job = self._get(job_id)
        if job.thread is not None:
            job.thread.join(timeout_s)
            return not job.thread.is_alive()
        return True

    # -- the job thread ---------------------------------------------------------------------

    def _run(self, job: _Job) -> None:
        control = JobControl(self, job)
        outcome, error = JobState.done, None
        try:
            try:
                self._runner(control)
            except JobStopped:
                pass
            except JobRefused as refused:
                outcome, error = JobState.failed, refused.message
            except Exception as exc:
                _log.exception("Job %d failed", job.id)
                outcome, error = JobState.failed, self._describe_safely(exc)
            self._finish(job, outcome, error)
        except Exception:
            _log.exception("Job %d: could not record how it ended", job.id)
        finally:
            self._db.release_thread()

    def _finish(self, job: _Job, outcome: JobState, error: str | None) -> None:
        with job.cond:
            if job.abandoned or job.state in TERMINAL:
                return
            job.stopped = outcome is JobState.done and job.stop_requested
            job.pause_requested = False
            job.error = error
            capture = self._transition(job, outcome)
            job.cond.notify_all()
        self._flush(job, capture)

    # -- state ------------------------------------------------------------------------------

    def _get(self, job_id: int) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound("That backup was not found.")
        return job

    def _current_session(self) -> DeviceSession | None:
        tracker = self._ctx.tracker
        return tracker.session() if tracker is not None else None

    def _stop_locked(self, job: _Job) -> _Capture | None:
        if job.state in TERMINAL or job.stop_requested:
            return None
        job.stop_requested = True
        capture = self._transition(job, JobState.stopping)
        job.cond.notify_all()
        return capture

    @staticmethod
    def _raise_if_stopped(job: _Job) -> None:
        if job.stop_requested or job.abandoned:
            raise JobStopped

    def _transition(self, job: _Job, state: JobState) -> _Capture | None:
        """Change state; returns what to write and publish (`_flush`) once the lock is released.

        The caller holds `job.cond`. Ignored (returns None) for an abandoned or finished job,
        and (apart from finishing) once a stop was requested, so a job that was told to stop
        cannot slip back to `pulling`.
        """
        if job.abandoned and state is not JobState.failed:
            return None
        if job.state in TERMINAL and not job.abandoned:
            return None
        if job.stop_requested and state not in TERMINAL and state is not JobState.stopping:
            return None
        if state in (JobState.paused, JobState.waiting_for_phone) or job.state in (
            JobState.paused,
            JobState.waiting_for_phone,
        ):
            job.samples.clear()  # the time spent waiting would drag the speed down
        job.state = state
        if state in TERMINAL:
            job.finished_at = self._clock.now()
        return self._capture(job)

    def _capture(self, job: _Job, *, number: bool = True) -> _Capture:
        """The job as it is now. The caller holds `job.cond`.

        A numbered capture is one that will be written and published, and counts as an event for
        the throttle; `snapshot()` takes unnumbered ones.
        """
        if number:
            job.seq += 1
            job.last_published = self._clock.monotonic()
        return _Capture(
            seq=job.seq,
            id=job.id,
            type=job.type,
            state=job.state,
            sets=job.sets,
            dry_run=job.dry_run,
            files_done=job.files_done,
            bytes_done=job.bytes_done,
            files_total=job.files_total,
            bytes_total=job.bytes_total,
            current=job.current,
            measured_speed=self._measured_speed(job),
            pause_requested=job.pause_requested,
            stopped=job.stopped,
            error=job.error,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    def _flush(
        self,
        job: _Job,
        capture: _Capture | None,
        *,
        force: bool = False,
        deadline: float | None = None,
    ) -> None:
        """Write and publish a capture. Call with no job lock held (and outside any `db.tx()`).

        Captures are written in order: one older than what was already written is dropped, so a
        slow writer cannot undo a newer state. An abandoned job's captures are dropped, except
        the one that marks it failed (`force`), which is written even if the ordering lock
        cannot be had before `deadline`.
        """
        if capture is None or (job.abandoned and not force):
            return
        timeout = -1.0 if deadline is None else max(_left(deadline), ABANDON_GRACE_S)
        if not job.write_lock.acquire(timeout=timeout):
            if not force:
                return
            self._write(capture, guard=False)
            self._publish(capture)
            return
        try:
            if capture.seq <= job.written_seq:
                return
            job.written_seq = capture.seq
            self._write(capture, guard=not force)
            self._publish(capture)
        finally:
            job.write_lock.release()

    def _write(self, capture: _Capture, *, guard: bool) -> None:
        """Save one capture to the job row. With `guard`, a finished row is never touched."""
        progress = {
            "files_done": capture.files_done,
            "bytes_done": capture.bytes_done,
            "files_total": capture.files_total,
            "bytes_total": capture.bytes_total,
            "stopped": capture.stopped,
        }
        sql = "UPDATE job SET state = ?, progress = ?, error = ?, finished_at = ? WHERE id = ?" + (
            " AND state NOT IN ('done', 'failed')" if guard else ""
        )
        try:
            with self._db.tx() as c:
                c.execute(
                    sql,
                    (
                        capture.state.value,
                        json.dumps(progress),
                        capture.error,
                        capture.finished_at,
                        capture.id,
                    ),
                )
        except Exception:
            _log.exception("Could not save job %d", capture.id)

    def _publish(self, capture: _Capture) -> None:
        try:
            self._ctx.bus.publish("progress", self._to_snapshot(capture).as_event())
        except Exception:
            _log.exception("Could not publish progress for job %d", capture.id)

    def _to_snapshot(self, c: _Capture) -> JobSnapshot:
        """The public view of a capture. Reads the stored speed, so call it outside job locks."""
        speed = c.measured_speed
        basis = "measured"
        effective = speed
        if effective is None or effective <= 0:
            stored = self._read_stored_speed()
            if stored is not None and stored > 0:
                effective, basis = stored, "stored"
            else:
                effective, basis = float(REFERENCE_SPEED_BPS), "reference"
        eta: float | None = None
        if c.bytes_total is not None:
            eta = max(0, c.bytes_total - c.bytes_done) / effective
        return JobSnapshot(
            id=c.id,
            type=c.type,
            state=c.state,
            sets=c.sets,
            dry_run=c.dry_run,
            files_done=c.files_done,
            bytes_done=c.bytes_done,
            files_total=c.files_total,
            bytes_total=c.bytes_total,
            current=c.current,
            speed_bps=speed,
            eta_s=eta,
            eta_basis=basis,
            pause_requested=c.pause_requested,
            stopped=c.stopped,
            error=c.error,
            started_at=c.started_at,
            finished_at=c.finished_at,
        )

    @staticmethod
    def _measured_speed(job: _Job) -> float | None:
        if len(job.samples) < 2:
            return None
        (t0, b0), (t1, b1) = job.samples[0], job.samples[-1]
        if t1 <= t0:
            return None
        return max(0.0, (b1 - b0) / (t1 - t0))

    def _read_stored_speed(self) -> float | None:
        if self._stored_speed is None:
            return None
        try:
            return self._stored_speed()
        except Exception:
            _log.exception("Could not read the stored reader-test speed")
            return None

    def _describe_safely(self, exc: BaseException) -> str:
        if self._describe is None:
            return GENERIC_MESSAGE
        try:
            return self._describe(exc)
        except Exception:
            _log.exception("Could not describe an error")
            return GENERIC_MESSAGE

    def _mark_failed(self, job_id: int, message: str) -> None:
        try:
            with self._db.tx() as c:
                c.execute(
                    "UPDATE job SET state = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    (message, self._clock.now(), job_id),
                )
        except Exception:
            _log.exception("Could not mark job %d failed", job_id)

    def _fail_stale_jobs(self) -> None:
        with self._db.tx() as c:
            c.execute(
                "UPDATE job SET state = 'failed', error = ?, finished_at = ?"
                " WHERE state NOT IN ('done', 'failed')",
                (STALE_MESSAGE, self._clock.now()),
            )
