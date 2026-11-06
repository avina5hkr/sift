"""The real phone transport: talks to one phone through the shared adb server. Read-only in M1.

How each call reaches the phone:

- `stat`, `list_dir` and `walk` use the sync v2 client (`Sync2`: LST2 / LIS2), which carries
  64-bit sizes and exact signed mtimes. The adb feature set is fetched once per transport and
  handed to every `Sync2`, so a sync connection costs one socket, not two.
- `open_read(path, 0)` streams the file with sync `RECV`; `open_read(path, n > 0)` runs
  `exec:tail -c +{n+1}` (1-based) and streams its stdout. Both go through
  `StallGuardedStream`, which turns "no bytes for `read_stall_s`" into `DeviceTimeout`. A
  stream that ends short of the size seen by `stat` raises `FileChanged` when the phone is
  still there (RECV: a clean `DONE`; tail: the path is stat'ed once more and the phone answers)
  and `DeviceDisconnected` when it is not.
- `sha256` stats the paths, then hashes on the phone with one `sha256sum` line per file, in
  batches (see `shell.build_sha256_script` for why no name ever appears in the output).
- `info` and `link_health` run read-only commands (`getprop`, `df`, `wm size`,
  `dumpsys battery`, `true`, and `settings get secure android_id` when the phone reports no
  `ro.serialno`). `DeviceInfo.serial` is the phone's identity: `ro.serialno`, else `android_id`.

Connections to the adb server come from `adbwire.open_service`, which only ever connects (no
adb program is run and no server is started or restarted here); the service requests on them
are written as raw bytes so that paths that are not valid UTF-8 reach the phone unchanged. Sync
connections are `Sync2` sockets. A `threading.BoundedSemaphore` caps how many connections are
open at once; an open stream holds one until it is closed.

On a real phone LIS2 cannot tell an unreadable folder from an empty one, and LST2 succeeds on a
folder whose contents cannot be read, so an unreadable folder (such as another app's folder under
Android/data) may appear empty. Every folder is LST2-checked before it is listed, so a missing
folder or a file always raises `RemoteError`.

Nothing here writes to the phone: `delete`, `push` and `media_rescan` raise
`NotImplementedError` until M8.
"""

import copy
import io
import logging
import re
import socket
import stat as stat_mode
import struct
import threading
import time
from collections.abc import Buffer, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from sift.device import adbwire
from sift.device.adb_manager import ADB_SERVER_HOST
from sift.device.shell import (
    build_sha256_script,
    encode_command,
    parse_battery,
    parse_df,
    parse_sha256_output,
    parse_wm_size,
    tail_command,
)
from sift.device.sync2 import Sync2, SyncStat, device_features
from sift.device.transport import (
    NO_IDENTITY_MESSAGE,
    DeviceDisconnected,
    DeviceInfo,
    DeviceTimeout,
    FileChanged,
    LinkHealth,
    RemoteEntry,
    RemoteError,
    TransportError,
)

log = logging.getLogger(__name__)

_NOT_IN_M1 = "Deleting and restoring arrive in M8."
_ENOENT = 2
_ENOTDIR = 20
_ERRNO_TEXT = {2: "no such file or folder", 13: "permission denied", 20: "not a folder"}
_MAX_PATH_BYTES = 1024  # adb's limit for a sync path
_MAX_SYNC_PAYLOAD = 64 * 1024  # adbd's SYNC_DATA_MAX; also the cap for a FAIL message
_U32 = struct.Struct("<I")
_RAW_CHUNK = 1 << 16
# host:port (an IPv6 host in brackets), or an mDNS service name.
_NETWORK_SERIAL = re.compile(r"^(?:\[[^\]\s]+\]|[^:\s\[\]]+):\d+$|_adb-tls-connect\._tcp")


def is_wifi_serial(serial: str) -> bool:
    """True for a Wi-Fi adb serial: `host:port`, `[ipv6]:port`, or an mDNS adb-tls-connect name."""
    return _NETWORK_SERIAL.search(serial) is not None


@dataclass(frozen=True)
class Limits:
    stat_s: float = 10
    list_s: float = 60
    read_stall_s: float = 30
    delete_s: float = 15
    sha_batch_files: int = 40
    sha_batch_bytes: int = 8 * 10**9
    sha_min_s: float = 120
    # ~half the ~86 MB/s cold rate measured on a reference phone; also drives tail resume allowance
    sha_assumed_bps: int = 40 * 10**6
    max_connections: int = 6
    max_request_bytes: int = 0xFFFF  # an adb host request's length is 4 hex digits


DEFAULT_LIMITS = Limits()


def plan_sha_batches(sizes: dict[str, int], limits: Limits) -> list[list[str]]:
    """Group paths (in order) into hashing batches.

    A batch holds at most `sha_batch_files` files and `sha_batch_bytes` bytes, except that a
    single file bigger than the byte cap is a batch of its own.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for path, size in sizes.items():
        full = len(current) >= limits.sha_batch_files
        if current and (full or current_bytes + size > limits.sha_batch_bytes):
            batches.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def sha_batch_timeout(total_bytes: int, limits: Limits) -> float:
    """How long one hashing batch may take: `max(sha_min_s, bytes / sha_assumed_bps)`."""
    return max(limits.sha_min_s, total_bytes / limits.sha_assumed_bps)


def resume_allowance_s(offset: int, limits: Limits) -> float:
    """How long a read from `offset` may wait for its first bytes.

    `tail -c +N` reads and throws away the first N-1 bytes before it prints anything, so a
    resume deep into a big file is silent for a while: `max(read_stall_s, offset /
    sha_assumed_bps)`. After the first bytes arrive the normal `read_stall_s` applies.
    """
    return max(limits.read_stall_s, offset / limits.sha_assumed_bps)


def _norm(path: str) -> str:
    return path if path == "/" else path.rstrip("/")


def _join(folder: str, name: str) -> str:
    return f"{folder.rstrip('/')}/{name}"


def _require_absolute(path: str) -> None:
    if not path.startswith("/"):
        raise RemoteError("Phone paths must start with '/'.", path)


def _encode_path(path: str) -> bytes:
    """The bytes of a remote path; RemoteError if it cannot name anything on the phone."""
    _require_absolute(path)
    try:
        raw = path.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = b""
    if not raw or len(raw) > _MAX_PATH_BYTES or b"\0" in raw:
        raise RemoteError("That path cannot be sent to the phone.", path)
    return raw


def _describe(error: int) -> str:
    return _ERRNO_TEXT.get(error, f"error {error}")


def _entry(path: str, st: SyncStat) -> RemoteEntry:
    return RemoteEntry(
        path=path,
        size=st.size,
        mtime=st.mtime,
        is_dir=stat_mode.S_ISDIR(st.mode),
        is_symlink=stat_mode.S_ISLNK(st.mode),
    )


def _noop() -> None:
    return None


class _Watchdog:
    """The state a stream shares with its watchdog thread.

    The thread runs `_Watchdog.run` and holds only this object, never the stream, so a stream
    the caller drops is collected (and closed, which stops the thread) like any other object.
    """

    def __init__(
        self,
        *,
        abort: Callable[[], None],
        clock: Callable[[], float],
        poll_s: float,
        path: str,
    ) -> None:
        self._abort = abort
        self._clock = clock
        self._poll_s = poll_s
        self._path = path
        self._cond = threading.Condition()
        self._waiting_since: float | None = None
        self._limit_s = 0.0
        self._stalled = False
        self._finished = False
        self._thread: threading.Thread | None = None

    @property
    def stalled(self) -> bool:
        with self._cond:
            return self._stalled

    def begin_wait(self, limit_s: float) -> None:
        """The reader is about to wait for the phone, for at most `limit_s` of silence."""
        with self._cond:
            self._waiting_since = self._clock()
            self._limit_s = limit_s
            if self._thread is None and not self._finished:
                self._thread = threading.Thread(
                    target=self.run, name="sift-read-watchdog", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()

    def end_wait(self) -> None:
        with self._cond:
            self._waiting_since = None

    def stop(self) -> None:
        with self._cond:
            self._finished = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def run(self) -> None:
        with self._cond:
            while not self._finished:
                since = self._waiting_since
                if since is None:
                    self._cond.wait()
                    continue
                if self._clock() - since >= self._limit_s:
                    self._stalled = True
                    break
                self._cond.wait(self._poll_s)
            else:
                return
        try:
            self._abort()
        except Exception:
            log.exception("Could not abort a stalled read of %s", self._path)


class StallGuardedStream(io.RawIOBase):
    """A read-only stream over an iterator of byte chunks coming from the phone.

    Reads may return fewer bytes than asked for (RawIOBase semantics); callers loop.

    - Stall watchdog: while the stream waits for the next chunk, a watchdog thread (one per
      stream, started on the first read, joined on close) checks `clock`. If no chunk arrives
      within `stall_s` (or `first_chunk_stall_s` while nothing has arrived yet), it calls
      `abort`, which must unblock the waiting read (e.g. by shutting the socket down; it runs
      on the watchdog thread and must not free anything the reader may still touch). The read
      then raises `DeviceTimeout`, and so does every later read. Time the caller spends
      between reads never counts as a stall.
    - Early end: if the chunks end before `expected_size` bytes, the read raises what
      `short_end(received, expected)` returns, `DeviceDisconnected` by default (again on every
      later read; `short_end` is asked once, and if it raises, the default is used). A short
      stream never reads as a clean end. More bytes than expected is fine (the file grew).
    - A failure is kept without its traceback (each read raises a fresh copy), so a failed
      stream holds no reference cycle and is freed as soon as the caller drops it.
    - `close()` first calls `abort` (so a read blocked on another thread wakes up), then stops
      the watchdog, closes the chunk iterator and calls `release` exactly once, even when two
      threads close at the same time (the second returns at once). A stream that is dropped
      without `close()` is closed when it is collected.
    """

    def __init__(
        self,
        chunks: Iterator[bytes],
        *,
        path: str,
        expected_size: int | None,
        stall_s: float,
        abort: Callable[[], None],
        first_chunk_stall_s: float | None = None,
        short_end: Callable[[int, int], TransportError] | None = None,
        release: Callable[[], None] = _noop,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float | None = None,
    ) -> None:
        super().__init__()
        self._close_lock = threading.Lock()
        self._closing = False
        self._chunks = chunks
        self._path = path
        self._expected = expected_size
        self._stall_s = stall_s
        self._first_stall_s = stall_s if first_chunk_stall_s is None else first_chunk_stall_s
        self._abort = abort
        self._short_end = short_end
        self._release: Callable[[], None] | None = release
        self._pending = memoryview(b"")
        self._received = 0
        self._got_first = False
        self._ended = False
        self._failure: TransportError | None = None  # re-raised (as a copy) on every read
        self._waited_limit_s = stall_s
        self._watchdog = _Watchdog(
            abort=abort,
            clock=clock,
            poll_s=poll_s if poll_s is not None else max(min(stall_s / 4, 1.0), 0.001),
            path=path,
        )

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        out = memoryview(buffer).cast("B")
        if not out:
            return 0
        if not self._pending:
            chunk = self._next_chunk()
            if chunk is None:
                return 0
            self._pending = memoryview(chunk)
        count = min(len(out), len(self._pending))
        out[:count] = self._pending[:count]
        self._pending = self._pending[count:]
        return count

    def readall(self) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        parts = [bytes(self._pending)]
        self._pending = memoryview(b"")
        while (chunk := self._next_chunk()) is not None:
            parts.append(chunk)
        return b"".join(parts)

    def close(self) -> None:
        with self._close_lock:
            if self._closing or self.closed:
                return
            self._closing = True
        try:
            try:
                self._abort()  # wakes a read blocked on another thread
            except Exception:
                log.exception("Could not abort the read of %s", self._path)
            self._watchdog.stop()
            close_chunks = getattr(self._chunks, "close", None)
            if close_chunks is not None:
                with suppress(ValueError):  # "generator already executing" on another thread
                    close_chunks()
        finally:
            release, self._release = self._release, None
            if release is not None:
                release()
            super().close()

    # -- internals ------------------------------------------------------------------------

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise copy.copy(self._failure)  # a fresh instance (with `path`), no old traceback

    def _fail(self, error: TransportError) -> None:
        self._failure = _detached(error)
        self._raise_failure()

    def _timeout(self) -> DeviceTimeout:
        return DeviceTimeout(
            f"The phone sent nothing for {self._waited_limit_s:g} s while reading {self._path}."
        )

    def _next_chunk(self) -> bytes | None:
        """The next non-empty chunk, or None at a complete end of stream."""
        self._raise_failure()
        if self._ended:
            return None
        while True:
            self._waited_limit_s = self._stall_s if self._got_first else self._first_stall_s
            self._watchdog.begin_wait(self._waited_limit_s)
            try:
                chunk = next(self._chunks, None)
            except (TransportError, OSError, EOFError) as e:
                if self._watchdog.stalled:
                    self._fail(self._timeout())
                if isinstance(e, TransportError):
                    self._failure = _detached(e)  # `e` holds this frame (and self) in its traceback
                    raise
                self._fail(DeviceDisconnected(f"Lost the connection while reading {self._path}."))
            finally:
                self._watchdog.end_wait()
            if self._watchdog.stalled:
                self._fail(self._timeout())
            if chunk is None:
                break
            if chunk:
                self._got_first = True
                self._received += len(chunk)
                return chunk
        if self._expected is not None and self._received < self._expected:
            # The failure is stored before `_ended`, so no later read can mistake it for a
            # clean end.
            self._fail(self._short_end_error(self._received, self._expected))
        self._ended = True
        return None

    def _short_end_error(self, received: int, expected: int) -> TransportError:
        if self._short_end is not None:
            try:
                return self._short_end(received, expected)
            except Exception:
                log.exception("Could not tell why the read of %s ended short", self._path)
        return _ended_short(self._path, received, expected)


def _detached(error: TransportError) -> TransportError:
    """A copy of `error` with no traceback, cause or context to hold frames alive."""
    return copy.copy(error).with_traceback(None)


def _ended_short(path: str, received: int, expected: int) -> DeviceDisconnected:
    return DeviceDisconnected(
        f"The phone sent {received} of {expected} bytes of {path} before the stream ended."
    )


def _file_changed(path: str) -> FileChanged:
    return FileChanged(f"{path} changed while it was being read.", path)


def _raw_chunks(sock: socket.socket) -> Iterator[bytes]:
    """Everything the phone sends until it closes the stream."""
    while True:
        try:
            chunk = sock.recv(_RAW_CHUNK)
        except TimeoutError as e:
            raise DeviceTimeout("The phone did not send anything in time.") from e
        except OSError as e:
            raise DeviceDisconnected(f"Lost the connection to the phone: {e}") from e
        if not chunk:
            return
        yield chunk


def _sync_length(sock: socket.socket, what: str) -> int:
    """A sync packet's u32 length, refused above 64 KiB rather than read into memory."""
    length: int = _U32.unpack(adbwire.recv_exact(sock, _U32.size))[0]
    if length > _MAX_SYNC_PAYLOAD:
        raise TransportError(f"The phone sent a {what} that is too long ({length} bytes).")
    return length


def _recv_chunks(sock: socket.socket, path: str, first: bytes) -> Iterator[bytes]:
    """The DATA payloads of a sync RECV reply whose first 4-byte id was `first`."""
    ident = first
    while True:
        if ident == b"DATA":
            yield adbwire.recv_exact(sock, _sync_length(sock, "data packet"))
        elif ident == b"DONE":
            adbwire.recv_exact(sock, _U32.size)
            return
        elif ident == b"FAIL":
            message = adbwire.recv_exact(sock, _sync_length(sock, "error message"))
            raise RemoteError(message.decode("utf-8", "replace"), path)
        else:
            raise TransportError(f"Unexpected sync reply: {ident!r}")
        ident = adbwire.recv_exact(sock, 4)


class AdbTransport:
    """`Transport` over adb for one phone (`serial`) on the adb server at `port`."""

    def __init__(
        self,
        serial: str,
        port: int,
        *,
        limits: Limits = DEFAULT_LIMITS,
        host: str = ADB_SERVER_HOST,
    ) -> None:
        self.serial = serial
        self.limits = limits
        self._host = host
        self._port = port
        self._slots = threading.BoundedSemaphore(limits.max_connections)
        self._features: frozenset[str] | None = None
        self._features_lock = threading.Lock()

    # -- connections ----------------------------------------------------------------------

    def _acquire(self, wait_s: float) -> None:
        if not self._slots.acquire(timeout=wait_s):
            raise DeviceTimeout("Too many connections to the phone are open at once.")

    @contextmanager
    def _slot(self, wait_s: float) -> Iterator[None]:
        self._acquire(wait_s)
        try:
            yield
        finally:
            self._slots.release()

    def _phone_features(self) -> frozenset[str]:
        with self._features_lock:
            if self._features is None:
                timeout = self.limits.stat_s
                with self._slot(timeout):
                    features = device_features(
                        self._host, self._port, self.serial, timeout_s=timeout
                    )
                self._features = frozenset(features)
            return self._features

    @contextmanager
    def _sync(self, timeout_s: float) -> Iterator[Sync2]:
        features = self._phone_features()
        with (
            self._slot(timeout_s),
            Sync2(self._host, self._port, self.serial, timeout_s=timeout_s, features=features) as s,
        ):
            yield s

    def _open_service(self, service: bytes, timeout_s: float) -> socket.socket:
        """A socket connected to `service` (e.g. b"sync:", b"exec:...") on the phone.

        The caller holds a connection slot and must close the returned socket.
        """
        return adbwire.open_service(
            self._host,
            self._port,
            self.serial,
            service,
            timeout_s=timeout_s,
            max_bytes=self.limits.max_request_bytes,
        )

    def _exec(self, command: str, timeout_s: float) -> bytes:
        """Run `command` on the phone (adbd runs it with `sh -c`) and return its output.

        `exec:` has no separate stderr: anything the command prints there is mixed in, so
        callers redirect it when it matters.
        """
        deadline = time.monotonic() + timeout_s
        with self._slot(timeout_s):
            sock = self._open_service(b"exec:" + encode_command(command), timeout_s)
            try:
                parts: list[bytes] = []
                for chunk in _raw_chunks(sock):
                    parts.append(chunk)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeviceTimeout(f"The phone did not finish in {timeout_s:g} s.")
                    sock.settimeout(remaining)
                return b"".join(parts)
            finally:
                sock.close()

    # -- Transport: reads -----------------------------------------------------------------

    def info(self) -> DeviceInfo:
        timeout = self.limits.stat_s

        def text(command: str) -> str:
            return self._exec(command, timeout).decode("utf-8", "replace")

        model = text("getprop ro.product.model").strip()
        release = text("getprop ro.build.version.release").strip()
        if not model or not release:
            raise RemoteError("The phone did not report its model and Android version.")
        serial = text("getprop ro.serialno").strip()
        if not serial:
            # `settings get` is a read; it prints "null" when the value is not set.
            serial = text("settings get secure android_id").strip()
            if serial == "null":
                serial = ""
        if not serial:
            raise RemoteError(NO_IDENTITY_MESSAGE)
        storage = parse_df(text("df /sdcard"))
        free, total = storage if storage is not None else (None, None)
        return DeviceInfo(
            serial=serial,
            model=model,
            android_version=release,
            battery_pct=parse_battery(text("dumpsys battery")),
            free_bytes=free,
            total_bytes=total,
            screen_sizes=parse_wm_size(text("wm size")),
        )

    def stat(self, path: str) -> RemoteEntry | None:
        path = _norm(path)
        raw = _encode_path(path)
        with self._sync(self.limits.stat_s) as sync:
            st = sync.stat(raw)
        if st.error in (_ENOENT, _ENOTDIR):
            return None
        if st.error:
            raise RemoteError(
                f"The phone cannot look at {path}: {_describe(st.error)}.", path, errno=st.error
            )
        return _entry(path, st)

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """One level, folders included. An unreadable folder may come back empty (see top)."""
        return self._list_folder(_norm(path))

    def walk(self, root: str) -> Iterator[RemoteEntry]:
        """Depth-first files and symlinks under `root`; symlinks are never descended into.

        If `root` is a symlink to a folder, as `/sdcard` is on a phone, the folder it points
        at is listed. The walk starts on the first `next()`. It uses one sync connection (and
        one connection slot) for the whole walk, from the first `next()` until the iterator
        is exhausted, closed or collected. Folders are handled one after another on it: each
        is checked with LST2, then its LIS2 listing is read to the end before anything else
        is sent, so nothing ever overlaps on the connection.
        """
        return self._walk(_norm(root))

    def _walk(self, root: str) -> Iterator[RemoteEntry]:
        _encode_path(root)  # refuse a bad root before connecting
        with self._sync(self.limits.list_s) as sync:
            stack = [iter(self._list_with(sync, root))]
            while stack:
                entry = next(stack[-1], None)
                if entry is None:
                    stack.pop()
                elif entry.is_dir:
                    stack.append(iter(self._list_with(sync, entry.path)))
                else:
                    yield entry

    def _list_folder(self, folder: str) -> list[RemoteEntry]:
        """Check and list one folder on a connection of its own."""
        _encode_path(folder)
        with self._sync(self.limits.list_s) as sync:
            return self._list_with(sync, folder)

    @staticmethod
    def _list_with(sync: Sync2, folder: str) -> list[RemoteEntry]:
        """Check `folder` with LST2, then list it with LIS2 (read to the end) on `sync`."""
        raw = _encode_path(folder)
        st = sync.stat(raw)
        if st.error == 0 and stat_mode.S_ISLNK(st.mode):
            st = sync.stat(raw + b"/")  # a trailing slash makes lstat follow the link
        if st.error:
            raise RemoteError(
                f"Cannot list {folder}: {_describe(st.error)}.", folder, errno=st.error
            )
        if not stat_mode.S_ISDIR(st.mode):
            raise RemoteError(f"Cannot list {folder}: it is not a folder.", folder)
        listed = sorted(sync.list(raw))
        entries: list[RemoteEntry] = []
        for name, child_stat in listed:
            child = _join(folder, name.decode("utf-8", "surrogateescape"))
            if child_stat.error:
                message = f"The phone cannot look at {child}: {_describe(child_stat.error)}."
                raise RemoteError(message, child, errno=child_stat.error)
            entries.append(_entry(child, child_stat))
        return entries

    def open_read(self, path: str, offset: int = 0) -> BinaryIO:
        if offset < 0:
            raise ValueError(f"offset must not be negative: {offset}")
        path = _norm(path)
        entry = self.stat(path)
        if entry is None:
            raise RemoteError(f"Cannot read {path}: no such file.", path)
        if entry.is_dir or entry.is_symlink:
            kind = "a folder" if entry.is_dir else "a symlink"
            raise RemoteError(f"Cannot read {path}: it is {kind}.", path)
        raw = _encode_path(path)
        timeout = self.limits.stat_s
        if offset == 0:
            service = b"sync:"
        else:
            # stderr is mixed into exec: output, so it must not reach the stream.
            service = b"exec:" + encode_command(f"{tail_command(path, offset)} 2>/dev/null")
        self._acquire(timeout)
        try:
            sock = self._open_service(service, timeout)
        except BaseException:
            self._slots.release()
            raise
        short_end: Callable[[int, int], TransportError]
        try:
            if offset == 0:
                adbwire.send_all(sock, b"RECV" + _U32.pack(len(raw)) + raw)
                first = adbwire.recv_exact(sock, 4)
                chunks = _recv_chunks(sock, path, first)
                if first == b"FAIL":
                    next(chunks)  # raises RemoteError naming the path, now rather than later
                expected = entry.size

                # The chunks end only at a clean DONE (a lost connection raises
                # DeviceDisconnected), so ending short means the file shrank.
                def short_end(received: int, wanted: int) -> TransportError:
                    return _file_changed(path)
            else:
                # tail's stream ends the same way whether the file shrank or the phone went
                # away (and stderr is discarded), so the phone is asked once more.
                chunks = _raw_chunks(sock)
                expected = max(entry.size - offset, 0)

                def short_end(received: int, wanted: int) -> TransportError:
                    if self._phone_answers_stat(path):
                        return _file_changed(path)
                    return _ended_short(path, received, wanted)

            # tail is silent while it skips `offset` bytes, so the first chunk gets longer.
            first_chunk_s = resume_allowance_s(offset, self.limits)
            # The watchdog is the stall detector; the socket timeout is only a backstop.
            sock.settimeout(first_chunk_s * 2)
        except BaseException:
            try:
                sock.close()
            finally:
                self._slots.release()
            raise

        def abort() -> None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

        def release() -> None:
            try:
                sock.close()
            finally:
                self._slots.release()

        stream = StallGuardedStream(
            chunks,
            path=path,
            expected_size=expected,
            stall_s=self.limits.read_stall_s,
            first_chunk_stall_s=first_chunk_s,
            short_end=short_end,
            abort=abort,
            release=release,
        )
        return cast(BinaryIO, stream)

    def _phone_answers_stat(self, path: str) -> bool:
        """True if the phone answers a stat of `path` at all (found, missing or refused).

        Called by a stream whose own connection has ended; it reuses that stream's connection
        slot (still held until the stream is closed) instead of waiting for another one.
        """
        try:
            with Sync2(
                self._host,
                self._port,
                self.serial,
                timeout_s=self.limits.stat_s,
                features=self._phone_features(),
            ) as sync:
                sync.stat(_encode_path(path))
        except RemoteError:
            return True  # the phone answered, with an error
        except (TransportError, OSError) as e:
            log.info("The phone did not answer a stat of %s: %s", path, e)
            return False
        return True

    def sha256(self, paths: list[str]) -> dict[str, str | None]:
        """Hashes computed on the phone; None for missing, unreadable, folders and symlinks."""
        for path in paths:
            _require_absolute(path)
        result: dict[str, str | None] = dict.fromkeys(paths)
        sizes: dict[str, int] = {}
        sendable: list[tuple[str, bytes]] = []
        for path in result:
            with suppress(RemoteError):
                sendable.append((path, _encode_path(path)))
        if sendable:
            with self._sync(self.limits.stat_s) as sync:
                for path, raw in sendable:
                    st = sync.stat(raw)
                    if st.error == 0 and stat_mode.S_ISREG(st.mode):
                        sizes[path] = st.size
        for batch in plan_sha_batches(sizes, self.limits):
            for part in self._fit_one_request(batch):
                timeout = sha_batch_timeout(sum(sizes[p] for p in part), self.limits)
                out = self._exec(build_sha256_script(part), timeout)
                hashes = parse_sha256_output(out.decode("ascii", "replace"), len(part))
                result.update(zip(part, hashes, strict=True))
        return result

    def _fit_one_request(self, batch: list[str]) -> list[list[str]]:
        """Split `batch` until each part's script fits in one adb request."""
        command = b"exec:" + encode_command(build_sha256_script(batch))
        if len(command) <= self.limits.max_request_bytes or len(batch) == 1:
            return [batch]
        half = len(batch) // 2
        return self._fit_one_request(batch[:half]) + self._fit_one_request(batch[half:])

    def link_health(self) -> LinkHealth:
        """USB or Wi-Fi (from the serial's form) and the round trip of a no-op command.

        adb does not report the link speed, so `speed_mbps` is None.
        """
        wireless = is_wifi_serial(self.serial)
        started = time.monotonic()
        self._exec("true", self.limits.stat_s)
        latency_ms = (time.monotonic() - started) * 1000
        return LinkHealth(
            kind="wifi" if wireless else "usb", speed_mbps=None, latency_ms=latency_ms
        )

    # -- Transport: writes (not in M1) ----------------------------------------------------

    def delete(self, path: str) -> None:
        raise NotImplementedError(_NOT_IN_M1)

    def push(self, local: Path, remote: str, mtime: int) -> None:
        raise NotImplementedError(_NOT_IN_M1)

    def media_rescan(self, paths: list[str]) -> bool:
        raise NotImplementedError(_NOT_IN_M1)
