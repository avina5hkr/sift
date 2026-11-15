"""Single-instance guard: an exclusive advisory lock on a file in the support folder.

The app retains its guard until process exit: bounded shutdown can return while an abandoned
writer is still alive. Ordinary guards can still be released explicitly. The kernel drops
either kind when the process dies, so a crash never leaves a stale lock behind.
"""

import fcntl
import os
from pathlib import Path

# These descriptors intentionally stay open until the kernel closes them at process exit.
# Do not add an atexit hook: Python cleanup may run while a background writer is still alive.
_process_lifetime_fds: set[int] = set()


class AlreadyRunningError(Exception):
    """Another Sift process holds the lock."""


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock without waiting and record this process id in the file."""
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunningError("Sift is already running.") from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd

    def retain_until_process_exit(self) -> None:
        """Irreversibly retain an acquired guard, before starting background writers.

        Repeated retention and release calls are harmless. The descriptor remains owned by
        this process even if the guard object is discarded; only process exit releases it.
        """
        if self._fd is None:
            raise RuntimeError("Acquire the instance lock before retaining it.")
        _process_lifetime_fds.add(self._fd)

    def release(self) -> None:
        """Drop an ordinary guard; a process-lifetime guard stays locked until process exit.

        The file stays: unlinking it would let two processes lock two files.
        """
        if self._fd is None or self._fd in _process_lifetime_fds:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
