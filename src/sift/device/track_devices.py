"""The adb server's live device list (`host:track-devices`), read over a raw socket.

Wire format: the client sends `0012host:track-devices`; the server answers `OKAY` and then,
at once and after every change, a message of 4 hex digits of length followed by that many bytes
of `serial<TAB>state<LF>` lines. An empty message means no devices.

This speaks the protocol directly instead of using adbutils, so no connection failure can ever
lead to an adb program being started, and `close()` (from any thread) ends a blocked reader at
once: it shuts the socket down, which wakes the `recv`.
"""

import contextlib
import re
import socket
import threading
from collections.abc import Iterator
from types import TracebackType
from typing import Protocol

from sift.device.transport import DeviceDisconnected

_REQUEST = b"host:track-devices"
_RECV_CHUNK = 4096
_LENGTH = re.compile(rb"[0-9a-fA-F]{4}")

# serial -> adb state ("device", "unauthorized", "offline", "authorizing", ...)
Snapshot = dict[str, str]


class DeviceEventSource(Protocol):
    """A stream of device-list snapshots.

    Iterating yields a `Snapshot` whenever the list changes (the first one at once) and `None`
    as an idle tick when nothing has arrived for a while, so a consumer can run timers without
    a thread of its own. It raises `DeviceDisconnected` when the stream breaks and ends quietly
    after `close()`, which may be called from any thread.
    """

    def __iter__(self) -> Iterator[Snapshot | None]: ...

    def close(self) -> None: ...


def parse_device_list(payload: bytes) -> Snapshot:
    """`serial<TAB>state` lines to a dict; blank or tab-less lines are ignored."""
    devices: Snapshot = {}
    for raw in payload.decode("utf-8", "replace").splitlines():
        serial, tab, state = raw.partition("\t")
        if tab and serial:
            devices[serial] = state.strip()
    return devices


class TrackDevices:
    """One `host:track-devices` connection to the adb server at host:port.

    The constructor connects and waits for `OKAY` (each step bounded by `timeout_s`); any
    failure raises `DeviceDisconnected` with the socket closed. After that, reads wait at most
    `idle_s` before yielding an idle tick.
    """

    def __init__(
        self, host: str, port: int, *, timeout_s: float = 3.0, idle_s: float = 1.0
    ) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._idle_s = idle_s
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout_s)
        except OSError as e:
            raise DeviceDisconnected(f"Cannot reach the adb server: {e}") from e
        try:
            self._sock.sendall(b"%04x" % len(_REQUEST) + _REQUEST)
            status = self._recv_exact(4)
            if status == b"FAIL":
                length = int(self._recv_exact(4), 16)
                message = self._recv_exact(length).decode("utf-8", "replace")
                raise DeviceDisconnected(f"The adb server refused the device list: {message}")
            if status != b"OKAY":
                raise DeviceDisconnected(f"Unexpected reply from the adb server: {status!r}")
            self._sock.settimeout(idle_s)
        except BaseException as e:
            self._sock.close()
            if isinstance(e, (OSError, ValueError)):
                raise DeviceDisconnected(f"The adb server did not send the device list: {e}") from e
            raise

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise OSError("the adb server closed the connection")
            data += chunk
        return data

    def __iter__(self) -> Iterator[Snapshot | None]:
        buf = bytearray()
        try:
            while True:
                snapshot = self._take(buf)
                if snapshot is not None:
                    yield snapshot
                    continue
                if self._closed:
                    return
                try:
                    chunk = self._sock.recv(_RECV_CHUNK)
                except TimeoutError:
                    if self._closed:
                        return
                    yield None
                    continue
                except OSError as e:
                    if self._closed:
                        return
                    raise DeviceDisconnected(f"Lost the adb server's device list: {e}") from e
                if not chunk:
                    if self._closed:
                        return
                    raise DeviceDisconnected("The adb server closed the device list.")
                buf += chunk
        finally:
            self.close()

    @staticmethod
    def _take(buf: bytearray) -> Snapshot | None:
        """The next whole message in `buf` (removed from it), or None if it is not all there."""
        if len(buf) < 4:
            return None
        head = bytes(buf[:4])
        if _LENGTH.fullmatch(head) is None:  # int() would also take "-001", " 01a" or "0_1a"
            raise DeviceDisconnected(f"The adb server sent a garbled length: {head!r}")
        length = int(head, 16)
        if len(buf) < 4 + length:
            return None
        payload = bytes(buf[4 : 4 + length])
        del buf[: 4 + length]
        return parse_device_list(payload)

    def close(self) -> None:
        """End the stream; a reader blocked in another thread returns promptly. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()

    def __enter__(self) -> TrackDevices:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
