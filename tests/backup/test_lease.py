"""The drive lease: one owner, bounded waits, and a stuck owner keeping the drive busy."""

import threading
import time

import pytest

from sift.backup.lease import BUSY_MESSAGE, DriveLease, SsdBusy


def test_one_holder_at_a_time_and_a_bounded_wait() -> None:
    lease = DriveLease()
    assert lease.acquire("backup", timeout_s=0)
    assert lease.holder == "backup" and lease.held()
    started = time.monotonic()
    assert not lease.acquire("quarantine", timeout_s=0.2)
    assert time.monotonic() - started < 1.0
    lease.release()
    assert not lease.held() and lease.holder is None


def test_a_waiter_gives_up_when_stopped() -> None:
    lease = DriveLease()
    lease.acquire("backup", timeout_s=0)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    started = time.monotonic()
    assert not lease.acquire("plug-in", timeout_s=None, should_stop=stop.is_set)
    assert time.monotonic() - started < 2.0


def test_hold_raises_ssd_busy_with_plain_words_that_never_suggest_unplugging() -> None:
    lease = DriveLease()
    lease.acquire("backup", timeout_s=0)
    with pytest.raises(SsdBusy) as caught, lease.hold("quarantine", timeout_s=0.1):
        pass
    assert caught.value.message == BUSY_MESSAGE
    assert "unplug" not in BUSY_MESSAGE.lower()


def test_a_stuck_owner_keeps_the_lease_until_its_own_thread_releases_it() -> None:
    lease = DriveLease()
    assert lease.acquire("backup", timeout_s=0)  # taken by the coordinator (this thread)
    returned = threading.Event()

    def stuck_worker() -> None:
        returned.wait(5)  # stands in for an OS call that does not return
        lease.release()  # on its way out: the lease was handed to it

    t = threading.Thread(target=stuck_worker)
    t.start()
    lease.hand_to_stuck("SSD copy (not answering)")
    assert lease.stuck and lease.held()
    assert not lease.acquire("plug-in", timeout_s=0.1)
    returned.set()
    t.join(5)
    assert not lease.held() and not lease.stuck
    assert lease.acquire("plug-in", timeout_s=0)
    lease.release()
