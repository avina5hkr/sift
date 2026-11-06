"""Time as an injected dependency, so scanners, jobs and throttles can be tested without sleeping.

`SystemClock` is the real thing. `ManualClock` only moves when a test calls `advance`. It lives
here, not under tests, so tests and code under `src` import the same class.
"""

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> int:
        """Wall-clock time in whole seconds since the epoch (for stored timestamps)."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes back (for durations and throttles)."""
        ...


class SystemClock:
    def now(self) -> int:
        return int(time.time())

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that stands still until `advance` moves it; `now` and `monotonic` move together."""

    def __init__(self, now: int = 1_700_000_000, monotonic: float = 0.0) -> None:
        self._now = float(now)
        self._monotonic = monotonic

    def now(self) -> int:
        return int(self._now)

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot go back")
        self._now += seconds
        self._monotonic += seconds
