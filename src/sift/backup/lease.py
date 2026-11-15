"""The drive lease: one owner at a time for writes and moves in the backup folders.

Whoever copies to or from the backup folders (a backup's SSD copy, a plug-in replication) or
moves files inside them (quarantine, chat-backup deletes, restores) holds the lease meanwhile.

The lease is held for as long as any thread that may still write to a drive is alive. A
backup's SSD copy takes it on its own thread and releases it on its way out; when the backup
gives up on that copy because it is stuck inside the operating system (a permission prompt, a
hung drive), the lease stays with the stuck thread, marked as stuck, until its call returns.
Until then, Eject, "Safe to unplug" and every other holder see the drive as busy: a second
writer never starts beside a stuck one.

`threading.Lock` (not `RLock`) underneath, so the thread that ends up owning the lease may
release it even though another thread acquired it.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Iterator

# How long another holder waits for the lease before reporting the drive as busy.
LEASE_WAIT_S = 30.0
_STEP_S = 1.0

BUSY_MESSAGE = (
    "The SSD is busy: an earlier copy is not answering. Quit and reopen Sift to clear it."
)


class SsdBusy(Exception):
    """The lease could not be had in time. Its text is meant for the person."""

    def __init__(self, message: str = BUSY_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


class DriveLease:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta = threading.Lock()
        self._holder: str | None = None
        self._stuck = False

    def acquire(
        self,
        holder: str,
        *,
        timeout_s: float | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> bool:
        """Take the lease; False when `timeout_s` passed or `should_stop()` turned true.

        `timeout_s=0` tries once without waiting; `None` waits until stopped. The wait is in
        steps of at most 1 s, so a stop is noticed within a second.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            step = _STEP_S if deadline is None else min(_STEP_S, deadline - time.monotonic())
            got = self._lock.acquire(timeout=step) if step > 0 else self._lock.acquire(False)
            if got:
                with self._meta:
                    self._holder, self._stuck = holder, False
                return True
            if should_stop() or (deadline is not None and time.monotonic() >= deadline):
                return False

    def release(self) -> None:
        with self._meta:
            self._holder, self._stuck = None, False
        self._lock.release()

    def mark_stuck(self, holder: str) -> None:
        """Record that the holder is a thread that was given up on while stuck in the operating
        system. It still owns the lease and releases it when (if) its call returns."""
        with self._meta:
            self._holder, self._stuck = holder, True

    def held(self) -> bool:
        return self._lock.locked()

    @property
    def holder(self) -> str | None:
        with self._meta:
            return self._holder

    @property
    def stuck(self) -> bool:
        """True while the owner is a thread that was given up on and has not ended yet."""
        with self._meta:
            return self._stuck and self._lock.locked()

    @contextlib.contextmanager
    def hold(
        self,
        holder: str,
        *,
        timeout_s: float = LEASE_WAIT_S,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> Iterator[None]:
        """Hold the lease for a block; raises `SsdBusy` when it cannot be had in time."""
        if not self.acquire(holder, timeout_s=timeout_s, should_stop=should_stop):
            raise SsdBusy()
        try:
            yield
        finally:
            self.release()


DRIVE_LEASE = DriveLease()
