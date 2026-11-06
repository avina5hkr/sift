"""adbwire: the one adb host-protocol module (strict hex, caps, open_service, no adbutils)."""

import ast
import importlib.util
import socket
import time
from pathlib import Path

import pytest

from sift.device import adbwire
from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    RemoteError,
    TransportError,
)
from tests.device.fake_adb_server import FakeAdbServer
from tests.device.fake_socket import ScriptedSocket, as_socket

ROOT = Path(__file__).resolve().parents[2]


def _dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


# -- strict hex ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "value"),
    [(b"0000", 0), (b"0004", 4), (b"00ff", 255), (b"00FF", 255), (b"ffff", 0xFFFF)],
)
def test_parse_hex_length_takes_four_hex_digits(raw: bytes, value: int) -> None:
    assert adbwire.parse_hex_length(raw) == value


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"000",
        b"00000",
        b"-001",  # int() would take these
        b"+001",
        b" 01a",
        b"01a ",
        b"0_1a",
        b"0x1f",
        b"00\n1",
        b"\xff\xff\xff\xff",
        b"zzzz",
        b"\xef\xbc\x91\xef",  # a full-width digit one
    ],
)
def test_parse_hex_length_refuses_everything_else(raw: bytes) -> None:
    with pytest.raises(TransportError, match="Malformed length"):
        adbwire.parse_hex_length(raw)


def test_a_length_above_the_cap_is_refused_before_the_body_is_read() -> None:
    fake = ScriptedSocket(b"0041" + b"x" * 0x41)
    with pytest.raises(TransportError, match="more than 64"):
        adbwire.read_host_string(as_socket(fake), max_bytes=64)
    assert fake.delivered == 4


def test_read_host_string_reads_exactly_the_stated_length() -> None:
    fake = ScriptedSocket(b"0003abcTRAILING")
    assert adbwire.read_host_string(as_socket(fake)) == "abc"
    assert fake.delivered == 7  # the trailing bytes were never requested
    assert fake.largest_request <= 4


def test_a_zero_length_string_is_empty() -> None:
    fake = ScriptedSocket(b"0000")
    assert adbwire.read_host_string(as_socket(fake)) == ""
    assert fake.delivered == 4


def test_invalid_utf8_in_a_string_is_replaced_not_raised() -> None:
    assert adbwire.read_host_string(as_socket(ScriptedSocket(b"0002\xff\xfe"))) == "��"


# -- host_request -------------------------------------------------------------------------------


def test_host_request_sends_length_prefixed_payload_and_accepts_okay() -> None:
    fake = ScriptedSocket(b"OKAY")
    adbwire.host_request(as_socket(fake), "host:version", max_bytes=100)
    assert fake.sent == b"000chost:version"


def test_host_request_fail_raises_the_given_type_with_adbs_message() -> None:
    fake = ScriptedSocket(b"FAIL0009no device")
    with pytest.raises(RemoteError, match="no device"):
        adbwire.host_request(as_socket(fake), b"x", fail=RemoteError)
    fake = ScriptedSocket(b"FAIL0009no device")
    with pytest.raises(DeviceDisconnected, match="no device"):  # the default
        adbwire.host_request(as_socket(fake), b"x")


def test_a_fail_message_over_the_cap_is_refused() -> None:
    too_long = adbwire.MAX_FAIL_TEXT + 1
    fake = ScriptedSocket(b"FAIL" + b"%04x" % too_long + b"x" * too_long)
    with pytest.raises(TransportError, match="more than"):
        adbwire.host_request(as_socket(fake), b"x")
    assert fake.delivered == 8


def test_an_oversized_request_is_refused_before_anything_is_sent() -> None:
    fake = ScriptedSocket(b"OKAY")
    with pytest.raises(RemoteError, match="too long"):
        adbwire.host_request(as_socket(fake), b"x" * 11, max_bytes=10)
    with pytest.raises(RemoteError, match="too long"):  # four hex digits cannot say more
        adbwire.host_request(as_socket(fake), b"x" * 0x10000, max_bytes=10**9)
    assert fake.sent == b""


@pytest.mark.parametrize("status", [b"OKA", b"", b"OKAYX"[:3], b"HUH?", b"okay"])
def test_anything_but_okay_or_fail_is_a_transport_error(status: bytes) -> None:
    with pytest.raises(TransportError):
        adbwire.host_request(as_socket(ScriptedSocket(status)), b"x")


def test_a_peer_that_closes_mid_reply_is_a_disconnect() -> None:
    with pytest.raises(DeviceDisconnected):
        adbwire.read_host_string(as_socket(ScriptedSocket(b"0005ab")))


def test_a_silent_peer_is_a_timeout_and_a_reset_is_a_disconnect() -> None:
    with pytest.raises(DeviceTimeout):
        adbwire.recv_exact(as_socket(ScriptedSocket(b"", end="stall")), 4)
    with pytest.raises(DeviceDisconnected):
        adbwire.recv_exact(as_socket(ScriptedSocket(b"", end="reset")), 4)


def test_the_deadline_bounds_the_whole_exchange() -> None:
    fake = ScriptedSocket(b"OKAY")
    with pytest.raises(DeviceTimeout):
        adbwire.host_request(as_socket(fake), b"x", deadline=time.monotonic() - 1)
    assert fake.sent == b""
    fake = ScriptedSocket(b"OKAY")
    adbwire.host_request(as_socket(fake), b"x", deadline=time.monotonic() + 5)
    assert fake.timeout is not None and 0 < fake.timeout <= 5


# -- connect / open_service ---------------------------------------------------------------------


def test_connect_failure_raises_disconnected_and_leaks_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[socket.socket] = []
    real = socket.socket.__init__

    def tracking(self: socket.socket, *args: object, **kwargs: object) -> None:
        real(self, *args, **kwargs)  # type: ignore[arg-type]
        made.append(self)

    monkeypatch.setattr(socket.socket, "__init__", tracking)
    with pytest.raises(DeviceDisconnected, match="Cannot reach the adb server"):
        adbwire.connect("127.0.0.1", _dead_port(), 1)
    assert made
    assert all(s.fileno() == -1 for s in made)


def test_open_service_sends_transport_then_service() -> None:
    with FakeAdbServer() as server:
        sock = adbwire.open_service("127.0.0.1", server.port, server.serial, b"sync:", timeout_s=2)
        sock.close()
        assert server.requests == [b"host:transport:" + server.serial.encode(), b"sync:"]


def test_open_service_unknown_phone_is_a_disconnect_and_closes_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[socket.socket] = []
    create = socket.create_connection

    def tracking(*args: object, **kwargs: object) -> socket.socket:
        s = create(*args, **kwargs)  # type: ignore[arg-type]
        made.append(s)
        return s

    monkeypatch.setattr(socket, "create_connection", tracking)
    with FakeAdbServer() as server, pytest.raises(DeviceDisconnected, match="device not found"):
        adbwire.open_service("127.0.0.1", server.port, "OTHER", b"sync:", timeout_s=2)
    assert len(made) == 1
    assert made[0].fileno() == -1


def test_open_service_refused_service_raises_the_requested_type() -> None:
    with FakeAdbServer() as server:
        with pytest.raises(RemoteError, match="unknown host service"):
            adbwire.open_service("127.0.0.1", server.port, server.serial, b"nonsense:", timeout_s=2)
        with pytest.raises(DeviceDisconnected, match="unknown host service"):
            adbwire.open_service(
                "127.0.0.1",
                server.port,
                server.serial,
                b"nonsense:",
                timeout_s=2,
                service_fail=DeviceDisconnected,
            )


# -- no adbutils anywhere -----------------------------------------------------------------------


def test_no_adbutils_import() -> None:
    assert importlib.util.find_spec("adbutils") is None, "adbutils is installed"
    for path in (ROOT / "src" / "sift").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] == "adbutils" for n in names), f"{path} imports it"
    for name in ("pyproject.toml", "uv.lock"):
        assert "adbutils" not in (ROOT / name).read_text(), f"{name} still lists adbutils"


def test_nothing_in_sift_can_start_an_adb_server() -> None:
    """The only `start-server` caller is AdbManager, behind its ECONNREFUSED probe."""
    users = []
    for path in sorted((ROOT / "src" / "sift").rglob("*.py")):
        text = path.read_text()
        if "start-server" in text and not path.name.startswith("adb_manager"):
            users.append(path.name)
    assert users == []
