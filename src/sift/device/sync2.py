"""Client for adb's sync protocol v2 (`LST2` / `LIS2`).

Why this exists: sync v1 (`STAT`/`LIST`) packs size and mtime as 32-bit integers. Real phones
have files over 4 GiB and timestamps outside the 32-bit range. The v2 replies carry a 64-bit size
and exact signed 64-bit Unix-second times, and this client hands them back untouched: mtime is an
`int` of seconds and is never turned into a datetime.

Wire format (AOSP `file_sync_protocol.h`, all little-endian):

    request   : 4-byte id, u32 path length, path bytes
    LST2 reply: b"LST2" + stat (the daemon echoes the request id)
    LIS2 reply: zero or more (b"DNT2" + stat + u32 namelen + name), then b"DONE" followed by a
                zeroed entry (stat + namelen) that must be consumed
    stat      : u32 error, u64 dev, u64 ino, u32 mode, u32 nlink, u32 uid, u32 gid,
                u64 size, i64 atime, i64 mtime, i64 ctime
    failure   : b"FAIL" + u32 length + message; the daemon then drops the connection

`LST2` is the lstat variant (a symlink is reported as itself). `STA2` follows symlinks, so it is
never sent. Both are gated by the `stat_v2` feature.

`error` is the phone's errno for that path (0 = ok). Names stay `bytes` here; decoding with
`surrogateescape` happens at the `AdbTransport` boundary.
"""

import socket
import struct
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from types import TracebackType

from sift.device import adbwire
from sift.device.transport import DeviceDisconnected, RemoteError, TransportError

_STAT = struct.Struct("<IQQIIIIQqqq")  # the one definition of the v2 stat layout
_U32 = struct.Struct("<I")
_REQUEST = struct.Struct("<4sI")
_REQUIRED_FEATURES = frozenset({"stat_v2", "ls_v2"})
_MAX_PATH_BYTES = 1024  # adb's path limit; also the cap for a name in a LIS2 listing
_MAX_FAIL_BYTES = 64 * 1024  # adbd's SYNC_DATA_MAX: the cap for a sync FAIL message
_MAX_DATA = 64 * 1024  # the largest DATA chunk adbd accepts
_TOO_OLD = "This phone's adb is too old for Sift (needs Android 11 or later)."


@dataclass(frozen=True)
class SyncStat:
    """What the phone reported for one path.

    When `error != 0`, mode, size and mtime are meaningless. `error` is the Linux errno value as
    sent on the wire (ENOENT is 2 even though the Mac's own errno numbering may differ).
    """

    mode: int
    size: int  # 64-bit
    mtime: int  # Unix seconds, signed 64-bit, exactly as the phone reported it
    error: int  # errno from the phone (0 = ok)


def _parse_stat(raw: bytes) -> SyncStat:
    error, _dev, _ino, mode, _nlink, _uid, _gid, size, _atime, mtime, _ctime = _STAT.unpack(raw)
    return SyncStat(mode=mode, size=size, mtime=mtime, error=error)


def device_features(host: str, port: int, serial: str, *, timeout_s: float = 10.0) -> set[str]:
    """The adb feature names the phone advertises (`host-serial:<serial>:features`)."""
    with closing(adbwire.connect(host, port, timeout_s)) as sock:
        adbwire.host_request(sock, f"host-serial:{serial}:features")
        listing = adbwire.read_host_string(sock)
    return {feature for feature in listing.split(",") if feature}


class Sync2:
    """One sync-mode connection to one phone. Not thread-safe; use one per thread."""

    def __init__(
        self,
        host: str,
        port: int,
        serial: str,
        *,
        timeout_s: float,
        features: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Open a sync connection.

        `features` is the phone's adb feature set if the caller already has it (it is fetched
        once per phone, not once per connection); otherwise it is asked for here. Either way the
        phone must support `stat_v2` and `ls_v2`.
        """
        if features is None:
            features = device_features(host, port, serial, timeout_s=timeout_s)
        if not features >= _REQUIRED_FEATURES:
            raise RemoteError(_TOO_OLD)
        self._sock: socket.socket | None = adbwire.open_service(
            host, port, serial, b"sync:", timeout_s=timeout_s, service_fail=DeviceDisconnected
        )
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

    @property
    def is_open(self) -> bool:
        """False once the connection was closed, by `close()` or by a failed request."""
        return self._sock is not None

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def _check(self, path: bytes) -> socket.socket:
        """Validate the request before anything is sent; failures here leave the stream intact."""
        if self._sock is None:
            raise DeviceDisconnected("The sync connection is closed.")
        if self._listing:
            raise RuntimeError("Finish iterating list() before sending another request.")
        if len(path) > _MAX_PATH_BYTES or b"\0" in path:
            raise RemoteError(
                "That path is too long or contains a NUL byte.",
                path.decode("utf-8", "surrogateescape"),
            )
        return self._sock

    def _fail(self, sock: socket.socket, path: bytes) -> RemoteError:
        """Read a sync `FAIL` message. The daemon drops the connection after it, so we do too."""
        try:
            (length,) = _U32.unpack(adbwire.recv_exact(sock, _U32.size))
            if length > _MAX_FAIL_BYTES:
                raise TransportError(
                    f"The phone sent an error message that is too long ({length})."
                )
            message = adbwire.recv_exact(sock, length).decode("utf-8", "replace")
        finally:
            self.close()
        return RemoteError(message, path.decode("utf-8", "surrogateescape"))

    def stat(self, path: bytes) -> SyncStat:
        """`LST2` (lstat semantics: a symlink is reported as itself; `STA2` is never used).

        Any failure mid-request closes the connection: a late reply must never be read as the
        answer to the next path. Later calls then raise `DeviceDisconnected`.
        """
        sock = self._check(path)
        try:
            adbwire.send_all(sock, _REQUEST.pack(b"LST2", len(path)) + path)
            ident = adbwire.recv_exact(sock, 4)
            if ident == b"FAIL":
                raise self._fail(sock, path)
            if ident != b"LST2":
                raise TransportError(f"Unexpected sync reply: {ident!r}")
            return _parse_stat(adbwire.recv_exact(sock, _STAT.size))
        except BaseException:
            self.close()
            raise

    def send(self, path: bytes, data: Iterable[bytes], *, mtime: int, mode: int = 0o644) -> None:
        """`SEND`: write a new regular file (sync v1: `path,mode`, DATA chunks, `DONE` + mtime).
        Writes to the phone; only quarantine restore calls it, after checking the path is free."""
        spec = path + b"," + str(0o100000 | mode).encode()
        sock = self._check(spec)
        try:
            adbwire.send_all(sock, _REQUEST.pack(b"SEND", len(spec)) + spec)
            for chunk in data:
                for start in range(0, len(chunk), _MAX_DATA):
                    part = chunk[start : start + _MAX_DATA]
                    adbwire.send_all(sock, b"DATA" + _U32.pack(len(part)) + part)
            adbwire.send_all(sock, b"DONE" + _U32.pack(mtime & 0xFFFFFFFF))
            ident = adbwire.recv_exact(sock, 4)
            if ident == b"FAIL":
                raise self._fail(sock, path)
            if ident != b"OKAY":
                raise TransportError(f"Unexpected sync reply: {ident!r}")
            adbwire.recv_exact(sock, _U32.size)
        except BaseException:
            self.close()
            raise

    def list(self, path: bytes) -> Iterator[tuple[bytes, SyncStat]]:
        """`LIS2`: the entries of a directory, without "." and "..".

        LIS2 cannot tell a missing or unreadable folder from an empty one (the daemon just ends
        the listing), so callers must `stat()` the folder first. An entry the phone could not
        stat carries its errno in its own `SyncStat.error`; the listing carries on.

        A listing must be read to the end. Abandoning one early leaves the rest of the reply on
        the wire, so the connection is closed and later calls raise `DeviceDisconnected`.
        """
        sock = self._check(path)
        return self._list_entries(sock, path)

    def _list_entries(self, sock: socket.socket, path: bytes) -> Iterator[tuple[bytes, SyncStat]]:
        self._listing = True
        finished = False
        try:
            adbwire.send_all(sock, _REQUEST.pack(b"LIS2", len(path)) + path)
            while True:
                ident = adbwire.recv_exact(sock, 4)
                if ident == b"DONE":
                    # The zeroed-entry trailer (stat + namelen) is consumed and ignored: it may
                    # hold stale fields, and AOSP never reports an error through it.
                    adbwire.recv_exact(sock, _STAT.size + _U32.size)
                    finished = True
                    return
                if ident == b"FAIL":
                    finished = True  # _fail closes the connection itself
                    raise self._fail(sock, path)
                if ident != b"DNT2":
                    raise TransportError(f"Unexpected sync reply: {ident!r}")
                stat = _parse_stat(adbwire.recv_exact(sock, _STAT.size))
                (name_len,) = _U32.unpack(adbwire.recv_exact(sock, _U32.size))
                if name_len > _MAX_PATH_BYTES:
                    raise TransportError(
                        f"The phone sent a file name that is too long ({name_len})."
                    )
                name = adbwire.recv_exact(sock, name_len)
                if name not in (b".", b".."):
                    yield name, stat
        finally:
            self._listing = False
            if not finished:
                self.close()
