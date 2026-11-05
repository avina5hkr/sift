"""Client for adb's sync protocol v2 (`STA2` / `LIS2`).

Why this exists: adbutils only speaks sync v1 (`STAT`/`LIST`), which packs size and mtime as
32-bit integers. Real phones have files over 4 GiB and timestamps outside the 32-bit range. The
v2 replies carry a 64-bit size and exact signed 64-bit Unix-second times, and this client hands
them back untouched: mtime is an `int` of seconds and is never turned into a datetime.

Wire format (AOSP `file_sync_protocol.h`, all little-endian):

    request   : 4-byte id, u32 path length, path bytes
    STA2 reply: b"STA2" + stat
    LIS2 reply: zero or more (b"DNT2" + stat + u32 namelen + name), then b"DONE" followed by a
                zeroed entry (stat + namelen) that must be consumed
    stat      : u32 error, u64 dev, u64 ino, u32 mode, u32 nlink, u32 uid, u32 gid,
                u64 size, i64 atime, i64 mtime, i64 ctime
    failure   : b"FAIL" + u32 length + message

`error` is the phone's errno for that path (0 = ok). Names stay `bytes` here; decoding with
`surrogateescape` happens at the `AdbTransport` boundary.
"""

import socket
import struct
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from types import TracebackType

from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    RemoteError,
    TransportError,
)

_STAT = struct.Struct("<IQQIIIIQqqq")  # the one definition of the v2 stat layout
_U32 = struct.Struct("<I")
_REQUEST = struct.Struct("<4sI")
_REQUIRED_FEATURES = frozenset({"stat_v2", "ls_v2"})
_TOO_OLD = "This phone's adb is too old for Sift (needs Android 11 or later)."


@dataclass(frozen=True)
class SyncStat:
    mode: int
    size: int  # 64-bit
    mtime: int  # Unix seconds, signed 64-bit, exactly as the phone reported it
    error: int  # errno from the phone (0 = ok)


def _parse_stat(raw: bytes) -> SyncStat:
    error, _dev, _ino, mode, _nlink, _uid, _gid, size, _atime, mtime, _ctime = _STAT.unpack(raw)
    return SyncStat(mode=mode, size=size, mtime=mtime, error=error)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly `n` bytes; EOF first means the device or adb server went away."""
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        try:
            chunk = sock.recv(remaining)
        except TimeoutError as e:
            raise DeviceTimeout("The phone did not answer in time.") from e
        except OSError as e:
            raise DeviceDisconnected(f"Lost the connection to the phone: {e}") from e
        if not chunk:
            raise DeviceDisconnected("The connection to the phone closed mid-reply.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_all(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except TimeoutError as e:
        raise DeviceTimeout("The phone did not accept the request in time.") from e
    except OSError as e:
        raise DeviceDisconnected(f"Lost the connection to the phone: {e}") from e


def _connect(host: str, port: int, timeout_s: float) -> socket.socket:
    try:
        return socket.create_connection((host, port), timeout=timeout_s)
    except TimeoutError as e:
        raise DeviceTimeout("The adb server did not answer in time.") from e
    except OSError as e:
        raise DeviceDisconnected(f"Cannot reach the adb server: {e}") from e


def _host_request(sock: socket.socket, payload: str) -> None:
    """Send one host request (4 hex digits of length + payload) and require `OKAY`."""
    body = payload.encode("ascii")
    _send_all(sock, b"%04x" % len(body) + body)
    status = _recv_exact(sock, 4)
    if status == b"OKAY":
        return
    if status == b"FAIL":
        raise DeviceDisconnected(_read_host_string(sock))
    raise TransportError(f"Unexpected reply from the adb server: {status!r}")


def _read_host_string(sock: socket.socket) -> str:
    length = int(_recv_exact(sock, 4), 16)
    return _recv_exact(sock, length).decode("utf-8", "replace")


def device_features(host: str, port: int, serial: str, *, timeout_s: float = 10.0) -> set[str]:
    """The adb feature names the phone advertises (`host-serial:<serial>:features`)."""
    with closing(_connect(host, port, timeout_s)) as sock:
        _host_request(sock, f"host-serial:{serial}:features")
        listing = _read_host_string(sock)
    return {feature for feature in listing.split(",") if feature}


class Sync2:
    """One sync-mode connection to one phone. Not thread-safe; use one per thread."""

    def __init__(self, host: str, port: int, serial: str, *, timeout_s: float) -> None:
        features = device_features(host, port, serial, timeout_s=timeout_s)
        if not features >= _REQUIRED_FEATURES:
            raise RemoteError(_TOO_OLD)
        sock = _connect(host, port, timeout_s)
        try:
            _host_request(sock, f"host:transport:{serial}")
            _host_request(sock, "sync:")
        except BaseException:
            sock.close()
            raise
        self._sock: socket.socket | None = sock
        self._listing = False

    def __enter__(self) -> Sync2:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def _begin(self, ident: bytes, path: bytes) -> socket.socket:
        if self._sock is None:
            raise DeviceDisconnected("The sync connection is closed.")
        if self._listing:
            raise RuntimeError("Finish iterating list() before sending another request.")
        _send_all(self._sock, _REQUEST.pack(ident, len(path)) + path)
        return self._sock

    def _fail(self, sock: socket.socket, path: bytes) -> RemoteError:
        (length,) = _U32.unpack(_recv_exact(sock, _U32.size))
        message = _recv_exact(sock, length).decode("utf-8", "replace")
        return RemoteError(message, path.decode("utf-8", "surrogateescape"))

    def stat(self, path: bytes) -> SyncStat:
        """`STA2` (lstat semantics: a symlink is reported as itself)."""
        sock = self._begin(b"STA2", path)
        ident = _recv_exact(sock, 4)
        if ident == b"FAIL":
            raise self._fail(sock, path)
        if ident != b"STA2":
            raise TransportError(f"Unexpected sync reply: {ident!r}")
        return _parse_stat(_recv_exact(sock, _STAT.size))

    def list(self, path: bytes) -> Iterator[tuple[bytes, SyncStat]]:
        """`LIS2`: the entries of a directory, without "." and "..".

        A listing must be read to the end. Abandoning one early leaves the rest of the reply on
        the wire, so the connection is closed and later calls raise `DeviceDisconnected`.
        """
        sock = self._begin(b"LIS2", path)
        self._listing = True
        finished = False
        try:
            while True:
                ident = _recv_exact(sock, 4)
                if ident == b"DONE":
                    # DONE is followed by a zeroed entry; it carries an errno only on failure.
                    raw = _recv_exact(sock, _STAT.size + _U32.size)
                    error = _parse_stat(raw[: _STAT.size]).error
                    finished = True
                    if error:
                        raise RemoteError(
                            f"The phone could not list this folder (errno {error}).",
                            path.decode("utf-8", "surrogateescape"),
                        )
                    return
                if ident == b"FAIL":
                    finished = True
                    raise self._fail(sock, path)
                if ident != b"DNT2":
                    raise TransportError(f"Unexpected sync reply: {ident!r}")
                stat = _parse_stat(_recv_exact(sock, _STAT.size))
                (name_len,) = _U32.unpack(_recv_exact(sock, _U32.size))
                name = _recv_exact(sock, name_len)
                if name not in (b".", b".."):
                    yield name, stat
        finally:
            self._listing = False
            if not finished:
                self.close()
