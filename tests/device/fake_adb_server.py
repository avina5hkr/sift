"""A tiny fake adb server: just enough host protocol and sync v2 to serve scripted data.

It encodes the wire format on its own (not by importing `sift.device.sync2`), so a mistake in the
client's struct layout shows up as a test failure instead of cancelling out.
"""

import contextlib
import socket
import struct
import threading
from dataclasses import dataclass, field
from types import TracebackType

# {u32 error, u64 dev, u64 ino, u32 mode, u32 nlink, u32 uid, u32 gid, u64 size,
#  i64 atime, i64 mtime, i64 ctime}, little-endian, after the 4-byte id.
_STAT = struct.Struct("<IQQIIIIQqqq")
_NAMELEN = struct.Struct("<I")
_REQUEST = struct.Struct("<4sI")

S_IFREG = 0o100000
S_IFDIR = 0o040000


@dataclass
class FakeStat:
    mode: int = S_IFREG | 0o644
    size: int = 0
    mtime: int = 0
    error: int = 0

    def pack(self) -> bytes:
        return _STAT.pack(self.error, 1, 2, self.mode, 1, 0, 0, self.size, 0, self.mtime, 0)


@dataclass
class FakeAdbServer:
    serial: str = "FAKESERIAL"
    features: tuple[str, ...] = ("shell_v2", "stat_v2", "ls_v2", "cmd")
    stats: dict[bytes, FakeStat] = field(default_factory=dict)
    dirs: dict[bytes, list[tuple[bytes, FakeStat]]] = field(default_factory=dict)
    # Send only this many bytes of a sync reply, then close the connection.
    truncate_reply_after: int | None = None
    # Accept sync requests but never answer them (until the server shuts down).
    stall: bool = False
    # Every sync command id received, in order (b"STA2", b"LIS2", ...).
    commands: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.05)
        self._conns: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)

    @property
    def port(self) -> int:
        port: int = self._listener.getsockname()[1]
        return port

    def __enter__(self) -> FakeAdbServer:
        self._accept_thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._accept_thread.join(timeout=5)
        self._listener.close()
        with self._lock:
            conns, threads = list(self._conns), list(self._threads)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        for thread in threads:
            thread.join(timeout=5)
        for conn in conns:
            conn.close()

    # -- connection handling -------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            with self._lock:
                self._conns.append(conn)
                self._threads.append(thread)
            thread.start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            self._host_phase(conn)
        except OSError, EOFError:
            pass
        finally:
            conn.close()  # lets a client see EOF right after a deliberately cut reply

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _read_host_request(self, conn: socket.socket) -> str:
        length = int(self._recv(conn, 4), 16)
        return self._recv(conn, length).decode("ascii")

    @staticmethod
    def _fail(conn: socket.socket, message: str) -> None:
        body = message.encode()
        conn.sendall(b"FAIL" + b"%04x" % len(body) + body)

    def _host_phase(self, conn: socket.socket) -> None:
        selected = False
        while True:
            request = self._read_host_request(conn)
            if request == f"host-serial:{self.serial}:features":
                body = ",".join(self.features).encode()
                conn.sendall(b"OKAY" + b"%04x" % len(body) + body)
                return
            if request.startswith("host:transport:"):
                if request == f"host:transport:{self.serial}":
                    selected = True
                    conn.sendall(b"OKAY")
                else:
                    self._fail(conn, "device not found")
                    return
            elif request == "sync:" and selected:
                conn.sendall(b"OKAY")
                self._sync_phase(conn)
                return
            else:
                self._fail(conn, "unknown host service")
                return

    # -- sync v2 -------------------------------------------------------------------------------

    def _send_reply(self, conn: socket.socket, data: bytes) -> bool:
        """Send a reply; False if it was cut short (and the connection should be dropped)."""
        if self.truncate_reply_after is not None and len(data) > self.truncate_reply_after:
            conn.sendall(data[: self.truncate_reply_after])
            return False
        conn.sendall(data)
        return True

    def _sync_phase(self, conn: socket.socket) -> None:
        while True:
            ident, path_len = _REQUEST.unpack(self._recv(conn, _REQUEST.size))
            path = self._recv(conn, path_len)
            self.commands.append(ident)
            if self.stall:
                self._stop.wait()
                return
            if ident == b"STA2":
                found = self.stats.get(path, FakeStat(mode=0, error=2))
                reply = b"STA2" + found.pack()
            elif ident == b"LIS2":
                reply = self._list_reply(path)
            else:
                # Anything else, including v1 LIST/STAT, is refused.
                body = b"unsupported sync command"
                reply = b"FAIL" + struct.pack("<I", len(body)) + body
            if not self._send_reply(conn, reply):
                return
            if reply.startswith(b"FAIL"):
                return

    def _list_reply(self, path: bytes) -> bytes:
        out = b""
        for name, stat in self.dirs.get(path, []):
            out += b"DNT2" + stat.pack() + _NAMELEN.pack(len(name)) + name
        # DONE is followed by a zeroed entry (stat struct and namelen) of the same size.
        return out + b"DONE" + bytes(_STAT.size + _NAMELEN.size)
