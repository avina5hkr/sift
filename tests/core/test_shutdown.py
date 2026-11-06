import os
import signal
import threading
import time
from types import FrameType

import pytest

from sift.core.shutdown import Shutdown


def test_steps_run_once_in_reverse() -> None:
    ran: list[str] = []
    shutdown = Shutdown()
    for name in ("lock", "db", "logging", "server", "tracker", "jobs"):
        shutdown.add(name, lambda name=name: ran.append(name), timeout_s=5)
    shutdown.run()
    shutdown.run()
    assert ran == ["jobs", "tracker", "server", "logging", "db", "lock"]


def test_failing_step_does_not_skip_others(caplog: pytest.LogCaptureFixture) -> None:
    ran: list[str] = []
    shutdown = Shutdown()

    def boom() -> None:
        raise OSError("disk went away")

    shutdown.add("first", lambda: ran.append("first"), timeout_s=5)
    shutdown.add("broken", boom, timeout_s=5)
    shutdown.add("last", lambda: ran.append("last"), timeout_s=5)
    with caplog.at_level("ERROR", logger="sift"):
        shutdown.run()
    assert ran == ["last", "first"]
    assert any("broken" in r.getMessage() for r in caplog.records)


def test_a_step_that_hangs_is_abandoned_after_its_timeout() -> None:
    ran: list[str] = []
    release = threading.Event()
    shutdown = Shutdown()
    shutdown.add("lock", lambda: ran.append("lock"), timeout_s=5)
    shutdown.add("stuck", release.wait, timeout_s=0.2)
    started = time.monotonic()
    shutdown.run()
    release.set()
    assert ran == ["lock"]
    assert time.monotonic() - started < 2


def test_request_is_idempotent_across_threads() -> None:
    """A signal and a window close (and a Cmd-Q) at once still run every step exactly once."""
    counts = {"jobs": 0, "lock": 0}
    shutdown = Shutdown()
    shutdown.add("lock", lambda: counts.__setitem__("lock", counts["lock"] + 1), timeout_s=5)

    def jobs() -> None:
        time.sleep(0.05)  # long enough for every caller to pile up on run()
        counts["jobs"] += 1

    shutdown.add("jobs", jobs, timeout_s=5)

    def on_signal(signum: int, frame: FrameType | None) -> None:
        shutdown.request("signal")

    previous = signal.signal(signal.SIGTERM, on_signal)
    try:
        go = threading.Barrier(5)

        def quit_route(reason: str) -> None:
            go.wait()
            shutdown.request(reason)
            shutdown.run()

        threads = [threading.Thread(target=quit_route, args=(f"route {i}",)) for i in range(4)]
        for t in threads:
            t.start()
        go.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        assert shutdown.wait_requested(timeout=2)
        shutdown.run()
        for t in threads:
            t.join(5)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert counts == {"jobs": 1, "lock": 1}
    assert shutdown.requested


def test_wait_requested_times_out_then_sees_a_request() -> None:
    shutdown = Shutdown()
    assert not shutdown.requested
    assert shutdown.wait_requested(timeout=0.05) is False
    threading.Timer(0.05, shutdown.request, args=("later",)).start()
    assert shutdown.wait_requested(timeout=5) is True
    assert shutdown.requested
    assert shutdown.reason == "later"


def test_first_reason_wins() -> None:
    shutdown = Shutdown()
    shutdown.request("signal SIGTERM")
    shutdown.request("window closed")
    assert shutdown.reason == "signal SIGTERM"


def test_run_counts_as_a_request() -> None:
    shutdown = Shutdown()
    shutdown.run()
    assert shutdown.requested


def test_run_from_a_second_thread_waits_for_the_first() -> None:
    entered = threading.Event()
    release = threading.Event()
    shutdown = Shutdown()
    shutdown.add("slow", lambda: (entered.set(), release.wait(5)), timeout_s=10)
    first = threading.Thread(target=shutdown.run)
    first.start()
    assert entered.wait(5)
    second_done = threading.Event()
    second = threading.Thread(target=lambda: (shutdown.run(), second_done.set()))
    second.start()
    assert not second_done.wait(0.1)  # run() returns only once the steps are finished
    release.set()
    assert second_done.wait(5)
    first.join(5)
    second.join(5)


def test_add_after_run_is_refused() -> None:
    shutdown = Shutdown()
    shutdown.run()
    with pytest.raises(RuntimeError):
        shutdown.add("late", lambda: None, timeout_s=1)
