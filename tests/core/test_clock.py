import time

import pytest

from sift.core.clock import Clock, ManualClock, SystemClock


def test_system_clock_reads_real_time() -> None:
    clock: Clock = SystemClock()
    assert isinstance(clock.now(), int)
    first = clock.monotonic()
    assert clock.monotonic() >= first
    assert abs(clock.now() - time.time()) < 5


def test_manual_clock_only_moves_when_advanced() -> None:
    clock: Clock = ManualClock(now=1_000, monotonic=50.0)
    assert (clock.now(), clock.monotonic()) == (1_000, 50.0)
    assert (clock.now(), clock.monotonic()) == (1_000, 50.0)
    assert isinstance(clock, ManualClock)
    clock.advance(2.5)
    assert clock.monotonic() == 52.5
    assert clock.now() == 1_002  # whole seconds, like SystemClock
    clock.advance(0.5)
    assert clock.now() == 1_003


def test_manual_clock_defaults_and_rejects_going_back() -> None:
    clock = ManualClock()
    clock.advance(0)
    with pytest.raises(ValueError):
        clock.advance(-1)
