"""Staged writes: the only way bytes reach a backup folder (invariants I1 and I2).

A copy is written to `<staging_dir>/<uuid4>.part`. `StagedFile.commit` makes it final only when
every expected byte arrived:

1. The byte count must equal `expected_size` and every write must have succeeded, else
   `IncompleteCopy` (and the `.part` is deleted).
2. The mtime is set and the file is flushed to the disk (`F_FULLFSYNC`), so the mtime is part
   of what is made durable.
3. The `.part` is renamed to the final path without ever replacing an existing file
   (`renamex_np(RENAME_EXCL)`), else `TargetExists`; the existing file is not touched and the
   stage stays usable, so the caller can commit it under another name.
4. The final file is read back with the cache off (`F_NOCACHE`); a different SHA-256 deletes it
   and raises `ChecksumMismatch`.
5. The parent folder is flushed, so the rename is durable before the caller records it.

On `IncompleteCopy`, `ChecksumMismatch`, or any error after the rename, the final path does not
exist afterwards. Leaving the `with` block without a commit deletes the `.part`; a crash leaves
it for `sweep_staging` at the next start.

Backup folders may sit on exFAT (an external SSD) as well as APFS:

- Where `renamex_np` reports `ENOTSUP` or `EINVAL`, a process-wide lock is taken, the target is
  checked to be absent (`os.lstat`) and `os.rename` moves the file. Sift is the only writer to
  its backup folders and runs as one process (`sift.core.instance`), so the lock is enough.
- Where `F_FULLFSYNC` reports `ENOTSUP`, `EINVAL` or `ENOTTY`, `os.fsync` is used instead, with
  one warning per device.
"""

import ctypes
import errno
import fcntl
import hashlib
import logging
import os
import stat
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self, cast

log = logging.getLogger(__name__)

PART_SUFFIX = ".part"
RENAME_EXCL = 0x00000004  # <sys/stdio.h>
_READ_CHUNK = 1 << 20
_FALLBACK_ERRNOS = frozenset({errno.ENOTSUP, errno.EINVAL})
_NO_FULL_FSYNC_ERRNOS = frozenset({errno.ENOTSUP, errno.EINVAL, errno.ENOTTY})

_rename_fallback_lock = threading.Lock()
_warned_lock = threading.Lock()
_warned_devices: set[int] = set()  # st_dev of every device already warned about fsync


class IncompleteCopy(Exception):
    """The copy did not receive exactly the expected bytes; nothing was made final."""


class ChecksumMismatch(Exception):
    """The final file read back differently from what was written; it was deleted."""


class TargetExists(Exception):
    """A file already exists at the final path; it was not touched."""


def _load_renamex_np() -> Callable[[bytes, bytes, int], int] | None:
    try:
        func = ctypes.CDLL(None, use_errno=True).renamex_np
    except AttributeError:
        return None
    func.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    return cast(Callable[[bytes, bytes, int], int], func)


_renamex_np = _load_renamex_np()


def _rename_no_replace(src: Path, dst: Path) -> None:
    """Rename `src` to `dst`, or raise `TargetExists` if anything is at `dst`."""
    renamex_np = _renamex_np
    if renamex_np is not None:
        if renamex_np(os.fsencode(src), os.fsencode(dst), RENAME_EXCL) == 0:
            return
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise TargetExists(f"{dst} already exists.")
        if code not in _FALLBACK_ERRNOS:
            raise OSError(code, os.strerror(code), os.fspath(dst))
    with _rename_fallback_lock:
        try:
            os.lstat(dst)
        except FileNotFoundError:
            pass
        else:
            raise TargetExists(f"{dst} already exists.")
        try:
            os.rename(src, dst)
        except FileExistsError:
            raise TargetExists(f"{dst} already exists.") from None


def _full_fsync(fd: int) -> None:
    """Flush `fd` to the disk itself; plain fsync (warned once per device) where unsupported."""
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        return
    except OSError as e:
        if e.errno not in _NO_FULL_FSYNC_ERRNOS:
            raise
    os.fsync(fd)
    device = os.fstat(fd).st_dev
    with _warned_lock:
        if device in _warned_devices:
            return
        _warned_devices.add(device)
    log.warning(
        "A backup disk (device %d) cannot do a full flush; using fsync, which may leave the "
        "newest files in the disk's own cache if it loses power.",
        device,
    )


def _flush_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _full_fsync(fd)
    finally:
        os.close(fd)


def _hash_uncached(path: Path) -> str:
    """The SHA-256 of `path`, read with the cache off so the bytes come from the disk."""
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        while chunk := os.read(fd, _READ_CHUNK):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def regular_file_sha256(path: Path) -> str | None:
    """The SHA-256 of `path` when it is a regular file, else None. Never writes, never blocks.

    For a file found at a final path that has no row. Anything else there (a symlink, a FIFO, a
    folder, a socket, a device) is never opened or followed, so it can neither hang the copy
    nor be taken for a backed-up file. The bytes are read with the cache off (`F_NOCACHE`).
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        if e.errno == errno.ELOOP:  # it became a symlink after the lstat
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # it was swapped after the lstat
            return None
        fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        digest = hashlib.sha256()
        while chunk := os.read(fd, _READ_CHUNK):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def make_durable(path: Path) -> None:
    """Flush an existing regular file, then its folder, to the disk; the file is not modified.

    For a file Sift adopts instead of writing (an identical file already at the final path),
    so it is as durable as a committed copy before a row points at it (I2). The file is opened
    read-only and without following a symlink; the flush is `F_FULLFSYNC`, with the same
    `fsync` fallback (and warning) as a commit.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _full_fsync(fd)
    finally:
        os.close(fd)
    _flush_folder(path.parent)


class StagedFile:
    """One copy in progress; a context manager. See the module docstring."""

    def __init__(self, staging_dir: Path, *, expected_size: int) -> None:
        if expected_size < 0:
            raise ValueError("expected_size cannot be negative.")
        self._expected = expected_size
        self._part = staging_dir / f"{uuid.uuid4().hex}{PART_SUFFIX}"
        self._fd: int | None = os.open(
            self._part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644
        )
        self._digest = hashlib.sha256()
        self._size = 0
        self._write_failed = False
        self._done = False  # committed or discarded: no more writes or commits

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._discard()

    @property
    def sha256(self) -> str:
        """The SHA-256 (hex) of the bytes written so far."""
        return self._digest.hexdigest()

    @property
    def size(self) -> int:
        """How many bytes were written so far."""
        return self._size

    @property
    def part_path(self) -> Path:
        """The `.part` file, for reading what was written (an EXIF date, say) before commit."""
        return self._part

    def write(self, chunk: bytes) -> None:
        fd = self._open_fd()
        view = memoryview(chunk)
        try:
            while view:
                view = view[os.write(fd, view) :]
        except BaseException:
            self._write_failed = True
            raise
        self._digest.update(chunk)
        self._size += len(chunk)

    def commit(self, final_path: Path, *, mtime: int) -> str:
        """Make the copy final at `final_path` with `mtime` (Unix seconds); return its SHA-256.

        Raises `IncompleteCopy`, `TargetExists` or `ChecksumMismatch` (see the module
        docstring). `TargetExists` keeps the stage, so another `final_path` can be tried.
        """
        fd = self._open_fd()
        if self._write_failed or self._size != self._expected:
            self._discard()
            raise IncompleteCopy(
                f"Received {self._size} of {self._expected} bytes"
                + (" before a write failed." if self._write_failed else ".")
            )
        try:
            mtime_ns = mtime * 1_000_000_000
            os.utime(fd, ns=(mtime_ns, mtime_ns))
            _full_fsync(fd)
            _rename_no_replace(self._part, final_path)
        except TargetExists:
            raise
        except BaseException:
            self._discard()
            raise
        # The rename landed: from here on, any failure removes the final file.
        try:
            self._close_fd()
            self._done = True
            expected = self.sha256
            if _hash_uncached(final_path) != expected:
                raise ChecksumMismatch(f"{final_path} read back differently from what was written.")
            _flush_folder(final_path.parent)
        except BaseException:
            final_path.unlink(missing_ok=True)
            raise
        return expected

    # -- internals ------------------------------------------------------------------------

    def _open_fd(self) -> int:
        if self._done or self._fd is None:
            raise ValueError("This staged file was already committed or discarded.")
        return self._fd

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def _discard(self) -> None:
        self._done = True
        try:
            self._close_fd()
        finally:
            self._part.unlink(missing_ok=True)


def sweep_staging(staging_dir: Path) -> int:
    """Delete every `*.part` file left in `staging_dir` (from a crash); return how many.

    Call it at startup, before any `StagedFile` exists. Anything else in the folder is left.
    """
    try:
        entries = list(os.scandir(staging_dir))
    except FileNotFoundError:
        return 0
    removed = 0
    for entry in entries:
        if entry.name.endswith(PART_SUFFIX) and entry.is_file(follow_symlinks=False):
            Path(entry.path).unlink(missing_ok=True)
            removed += 1
    return removed
