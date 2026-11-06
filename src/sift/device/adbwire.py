"""The one place Sift speaks the adb host protocol over a socket.

Wire format (adb's `SERVICES.TXT` / `OVERVIEW.TXT`):

    request : 4 hex digits of length, then that many bytes of service name
    reply   : b"OKAY", or b"FAIL" + 4 hex digits of length + that many bytes of message
    string  : 4 hex digits of length + that many bytes (`host:version`, `features`, ...)

Everything here is strict about what comes back: lengths are exactly four hex digits (never
what `int(x, 16)` happens to accept, such as "-001" or "0x1f"), a length above the caller's cap
is refused before a single body byte is read, and no read ever asks the socket for more than the
stated length. Whatever the peer sends, the outcome is a value or a `TransportError` subclass:

- the peer closes the stream or the socket fails: `DeviceDisconnected`;
- a socket timeout (or the optional `deadline` running out): `DeviceTimeout`;
- anything else wrong with the bytes: `TransportError`.

Nothing in this module starts, stops or restarts an adb server, or runs any program. A failed
connect closes the socket it opened.
"""

import contextlib
import socket
import time

from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    RemoteError,
    TransportError,
)

HOST_LENGTH_DIGITS = 4
MAX_HOST_LENGTH = 0xFFFF  # what four hex digits can say
MAX_HOST_STRING = 64 * 1024  # default cap for a host string (always above MAX_HOST_LENGTH)
MAX_FAIL_TEXT = 4096  # cap for the message after FAIL; adb's own messages are one short line
_HEX = frozenset(b"0123456789abcdefABCDEF")


def connect(host: str, port: int, timeout_s: float) -> socket.socket:
    """A connection to the adb server at host:port; the socket is closed if connecting fails."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout_s)
    except TimeoutError as e:
        raise DeviceTimeout("The adb server did not answer in time.") from e
    except OSError as e:
        raise DeviceDisconnected(f"Cannot reach the adb server: {e}") from e
    with contextlib.suppress(OSError):  # keepalive is a nicety; the connection works without it
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return sock


def _arm(sock: socket.socket, deadline: float | None) -> None:
    """Let the next socket call wait only for what is left before `deadline` (monotonic)."""
    if deadline is None:
        return
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeviceTimeout("The adb server did not answer in time.")
    sock.settimeout(left)


def recv_exact(sock: socket.socket, n: int, *, deadline: float | None = None) -> bytes:
    """Read exactly `n` bytes (never asking for more); EOF first means the peer went away."""
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        _arm(sock, deadline)
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


def send_all(sock: socket.socket, data: bytes, *, deadline: float | None = None) -> None:
    _arm(sock, deadline)
    try:
        sock.sendall(data)
    except TimeoutError as e:
        raise DeviceTimeout("The phone did not accept the request in time.") from e
    except OSError as e:
        raise DeviceDisconnected(f"Lost the connection to the phone: {e}") from e


def parse_hex_length(raw: bytes) -> int:
    """The value of exactly four hex digits; `TransportError` for anything else."""
    if len(raw) != HOST_LENGTH_DIGITS or not all(c in _HEX for c in raw):
        raise TransportError(f"Malformed length from the adb server: {raw[:16]!r}")
    return int(raw, 16)


def read_hex_length(
    sock: socket.socket, *, max_bytes: int = MAX_HOST_STRING, deadline: float | None = None
) -> int:
    """Read four hex digits and return them as a length of at most `max_bytes`.

    A length above the cap raises `TransportError` without reading anything more.
    """
    length = parse_hex_length(recv_exact(sock, HOST_LENGTH_DIGITS, deadline=deadline))
    if length > max_bytes:
        raise TransportError(f"The adb server announced {length} bytes, more than {max_bytes}.")
    return length


def read_host_string(
    sock: socket.socket, *, max_bytes: int = MAX_HOST_STRING, deadline: float | None = None
) -> str:
    """A length-prefixed host string, decoded as UTF-8 (bad bytes replaced)."""
    length = read_hex_length(sock, max_bytes=max_bytes, deadline=deadline)
    return recv_exact(sock, length, deadline=deadline).decode("utf-8", "replace")


def host_request(
    sock: socket.socket,
    payload: bytes | str,
    *,
    max_bytes: int = MAX_HOST_LENGTH,
    fail: type[TransportError] = DeviceDisconnected,
    deadline: float | None = None,
) -> None:
    """Send one host request and require `OKAY`.

    A payload over `max_bytes` (or over what four hex digits can say) raises `RemoteError`
    before anything is sent. `FAIL` raises `fail` with adb's message, read with a size cap.
    """
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(body) > min(max_bytes, MAX_HOST_LENGTH):
        raise RemoteError("That command is too long to send to the phone.")
    send_all(sock, b"%04x" % len(body) + body, deadline=deadline)
    status = recv_exact(sock, 4, deadline=deadline)
    if status == b"OKAY":
        return
    if status == b"FAIL":
        raise fail(read_host_string(sock, max_bytes=MAX_FAIL_TEXT, deadline=deadline))
    raise TransportError(f"Unexpected reply from the adb server: {status!r}")


def open_service(
    host: str,
    port: int,
    serial: str,
    service: bytes | str,
    *,
    timeout_s: float,
    max_bytes: int = MAX_HOST_LENGTH,
    service_fail: type[TransportError] = RemoteError,
) -> socket.socket:
    """A socket connected to `service` (`b"sync:"`, `b"exec:..."`) on the phone `serial`.

    Sends `host:transport:<serial>` and then the service, each checked for `OKAY`. A refused
    transport (no such phone) raises `DeviceDisconnected`; a refused service raises
    `service_fail`. The socket is closed on any failure; on success the caller owns it.
    """
    sock = connect(host, port, timeout_s)
    try:
        host_request(sock, b"host:transport:" + serial.encode(), max_bytes=max_bytes)
        host_request(sock, service, max_bytes=max_bytes, fail=service_fail)
    except BaseException:
        sock.close()
        raise
    return sock
