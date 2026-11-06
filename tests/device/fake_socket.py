"""A scripted stand-in for a socket: it plays back bytes, then ends the way the test says."""

import socket
from typing import Literal, cast

End = Literal["eof", "stall", "reset"]


class ScriptedSocket:
    """`recv(n)` hands out the script in chunks of at most `chunk` bytes (never more than `n`).

    When the script runs out: `eof` returns b"" (the peer closed), `stall` raises
    `TimeoutError` (the socket timeout fired), `reset` raises `ConnectionResetError`.
    It records what the code under test did, so tests can assert it never read past what the
    peer announced.
    """

    def __init__(self, script: bytes, *, chunk: int = 1 << 16, end: End = "eof") -> None:
        self._script = script
        self._chunk = max(chunk, 1)
        self._end = end
        self.delivered = 0  # bytes handed to the code under test
        self.largest_request = 0  # the biggest `n` any recv was called with
        self.sent = b""
        self.closed = False
        self.timeout: float | None = None

    def recv(self, n: int) -> bytes:
        self.largest_request = max(self.largest_request, n)
        if self.delivered >= len(self._script):
            if self._end == "stall":
                raise TimeoutError("timed out")
            if self._end == "reset":
                raise ConnectionResetError("reset by peer")
            return b""
        take = min(n, self._chunk, len(self._script) - self.delivered)
        data = self._script[self.delivered : self.delivered + take]
        self.delivered += take
        return data

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


def as_socket(fake: ScriptedSocket) -> socket.socket:
    """For type checkers: the fake only has to look like a socket to `adbwire`."""
    return cast(socket.socket, fake)
