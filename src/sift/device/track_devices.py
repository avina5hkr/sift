"""The adb server's live device list (`host:track-devices`), read over a raw socket.

Wire format: the client sends `0012host:track-devices`; the server answers `OKAY` and then,
at once and after every change, a message of 4 hex digits of length followed by that many bytes
of `serial<TAB>state<LF>` lines. An empty message means no devices.

This speaks the protocol directly (through `adbwire`), so no connection failure can ever lead
to an adb program being started, and `close()` (from any thread) ends a blocked reader at
once: it shuts the socket down, which wakes the `recv` (the reader then closes the descriptor).
"""

import contextlib
import socket
import threading
from collections.abc import Iterator
from types import TracebackType
from typing import Protocol

from sift.device import adbwire
from sift.device.transport import DeviceDisconnected, TransportError

_REQUEST = b"host:track-devices"
_RECV_CHUNK = 4096

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
        self._reading = False  # a reader is inside `__iter__`: it closes the socket, not close()
        self._idle_s = idle_s
        try:
            self._sock = adbwire.connect(host, port, timeout_s)
        except TransportError as e:
            raise DeviceDisconnected(str(e)) from e
        try:
            adbwire.host_request(self._sock, _REQUEST)
            self._sock.settimeout(idle_s)
        except BaseException as e:
            self._sock.close()
            if isinstance(e, TransportError):
                raise DeviceDisconnected(f"The adb server did not send the device list: {e}") from e
            raise

    def __iter__(self) -> Iterator[Snapshot | None]:
        with self._lock:
            if self._closed:
                return
            self._reading = True
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
            with self._lock:
                self._reading = False
            self.close()

    @staticmethod
    def _take(buf: bytearray) -> Snapshot | None:
        """The next whole message in `buf` (removed from it), or None if it is not all there."""
        if len(buf) < 4:
            return None
        head = bytes(buf[:4])
        try:
            length = adbwire.parse_hex_length(head)  # strict: not what int() would also take
        except TransportError as e:
            raise DeviceDisconnected(f"The adb server sent a garbled length: {head!r}") from e
        if len(buf) < 4 + length:
            return None
        payload = bytes(buf[4 : 4 + length])
        del buf[: 4 + length]
        return parse_device_list(payload)

    def close(self) -> None:
        """End the stream; a reader blocked in another thread returns promptly. Idempotent."""
        with self._lock:
            first = not self._closed
            self._closed = True
            reading = self._reading
        if first:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
        if not reading:
            # While a reader is inside `__iter__` the socket stays open until it leaves: closing
            # the descriptor under a reader that is about to wait on it can lose the wake-up
            # (the wait then lasts until the idle timeout). Shutdown alone wakes it reliably.
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
