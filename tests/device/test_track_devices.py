"""The raw-socket `host:track-devices` reader, against a tiny threaded fake adb server."""

import socket
import threading
import time
from types import TracebackType

import pytest

from sift.device.track_devices import TrackDevices, parse_device_list
from sift.device.transport import DeviceDisconnected

REQUEST = b"0012host:track-devices"


def frame(payload: bytes) -> bytes:
    return b"%04x" % len(payload) + payload


class TrackServer:
    """Accepts one connection, reads the request, then sends `replies` one send at a time.

    With `hold` it keeps the connection open (silent) until the test ends; without it, it
    closes right after the replies (EOF for the client).
    """

    def __init__(self, replies: list[bytes], *, hold: bool = True) -> None:
        self.replies = replies
        self.hold = hold
        self.request = b""
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(5)
        self.port: int = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="fake-track-server")

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            self.connected.set()
            conn.settimeout(5)
            try:
                while len(self.request) < len(REQUEST):
                    chunk = conn.recv(len(REQUEST) - len(self.request))
                    if not chunk:
                        return
                    self.request += chunk
                for reply in self.replies:
                    conn.sendall(reply)
                    time.sleep(0.002)  # separate sends, so split messages really arrive split
                if self.hold:
                    self._stop.wait(5)
            except OSError:
                pass

    def __enter__(self) -> TrackServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._srv.close()
        self._thread.join(5)
        assert not self._thread.is_alive()


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_sends_the_request_and_yields_snapshots() -> None:
    replies = [
        b"OKAY",
        frame(b"FAKE-0001\tdevice\n"),
        frame(b"FAKE-0001\tdevice\n192.0.2.10:5555\tunauthorized\n"),
        frame(b""),
    ]
    with TrackServer(replies) as srv, TrackDevices("127.0.0.1", srv.port) as stream:
        items = iter(stream)
        assert next(items) == {"FAKE-0001": "device"}
        assert next(items) == {"FAKE-0001": "device", "192.0.2.10:5555": "unauthorized"}
        assert next(items) == {}
    assert srv.request == REQUEST


def test_messages_split_across_sends_are_reassembled() -> None:
    message = b"OKAY" + frame(b"FAKE-0001\toffline\n") + frame(b"")
    replies = [message[i : i + 3] for i in range(0, len(message), 3)]
    with TrackServer(replies) as srv, TrackDevices("127.0.0.1", srv.port, idle_s=5) as stream:
        items = iter(stream)
        assert next(items) == {"FAKE-0001": "offline"}
        assert next(items) == {}


def test_two_messages_in_one_send_are_both_yielded() -> None:
    replies = [b"OKAY" + frame(b"A\tdevice\n") + frame(b"B\tdevice\n")]
    with TrackServer(replies) as srv, TrackDevices("127.0.0.1", srv.port) as stream:
        items = iter(stream)
        assert next(items) == {"A": "device"}
        assert next(items) == {"B": "device"}


def test_silence_yields_idle_ticks() -> None:
    with TrackServer([b"OKAY"]) as srv, TrackDevices("127.0.0.1", srv.port, idle_s=0.02) as stream:
        items = iter(stream)
        assert next(items) is None
        assert next(items) is None


def test_fail_reply_raises_disconnected() -> None:
    replies = [b"FAIL" + frame(b"no track for you")]
    with (
        TrackServer(replies, hold=False) as srv,
        pytest.raises(DeviceDisconnected, match="no track for you"),
    ):
        TrackDevices("127.0.0.1", srv.port)


def test_unexpected_reply_raises_disconnected() -> None:
    with TrackServer([b"HUH?"], hold=False) as srv, pytest.raises(DeviceDisconnected):
        TrackDevices("127.0.0.1", srv.port)


def test_no_reply_in_time_raises_disconnected() -> None:
    with TrackServer([]) as srv:
        started = time.monotonic()
        with pytest.raises(DeviceDisconnected):
            TrackDevices("127.0.0.1", srv.port, timeout_s=0.1)
        assert time.monotonic() - started < 2


def test_nothing_listening_raises_disconnected() -> None:
    with pytest.raises(DeviceDisconnected):
        TrackDevices("127.0.0.1", _dead_port(), timeout_s=0.5)


def test_end_of_stream_raises_disconnected() -> None:
    replies = [b"OKAY", frame(b"A\tdevice\n")]
    with TrackServer(replies, hold=False) as srv, TrackDevices("127.0.0.1", srv.port) as stream:
        items = iter(stream)
        assert next(items) == {"A": "device"}
        with pytest.raises(DeviceDisconnected):
            next(items)


def test_end_of_stream_mid_message_raises_disconnected() -> None:
    replies = [b"OKAY", b"0010A\tdev"]
    with (
        TrackServer(replies, hold=False) as srv,
        TrackDevices("127.0.0.1", srv.port) as stream,
        pytest.raises(DeviceDisconnected),
    ):
        next(iter(stream))


def test_garbled_length_raises_disconnected() -> None:
    replies = [b"OKAY", b"zz12A\tdevice\n"]
    with (
        TrackServer(replies) as srv,
        TrackDevices("127.0.0.1", srv.port) as stream,
        pytest.raises(DeviceDisconnected),
    ):
        next(iter(stream))


def test_close_ends_a_blocked_reader_promptly() -> None:
    with TrackServer([b"OKAY"]) as srv:
        stream = TrackDevices("127.0.0.1", srv.port, idle_s=30)
        seen: list[object] = []
        failures: list[BaseException] = []

        def read() -> None:
            try:
                seen.extend(stream)
            except BaseException as e:  # reported to the test below
                failures.append(e)

        reader = threading.Thread(target=read, name="track-reader")
        reader.start()
        time.sleep(0.05)  # let the reader block in recv
        started = time.monotonic()
        stream.close()
        reader.join(2)
        assert not reader.is_alive()
        assert time.monotonic() - started < 1
        assert seen == []
        assert failures == []  # a deliberate close ends the stream quietly
        stream.close()  # idempotent


def test_iterating_after_close_ends_at_once() -> None:
    with TrackServer([b"OKAY", frame(b"A\tdevice\n")]) as srv:
        stream = TrackDevices("127.0.0.1", srv.port)
        stream.close()
        assert list(stream) == []


def test_parse_device_list() -> None:
    payload = b"A\tdevice\n\nB\tunauthorized\r\nno-tab-line\nC\tno permissions\n"
    assert parse_device_list(payload) == {
        "A": "device",
        "B": "unauthorized",
        "C": "no permissions",
    }
    assert parse_device_list(b"") == {}
