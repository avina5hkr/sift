"""Single-instance guard: an exclusive advisory lock on a file in the support folder.

The kernel drops the lock when the process dies, however it dies, so a crash never leaves a
stale lock behind.
"""

import fcntl
import os
from pathlib import Path


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

    def release(self) -> None:
        """Drop the lock. The file stays: unlinking it would let two processes lock two files."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
