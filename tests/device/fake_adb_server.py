"""A tiny fake adb server: just enough host protocol and sync v2 to serve scripted data.

It encodes the wire format on its own (not by importing `sift.device.sync2`), so a mistake in the
client's struct layout shows up as a test failure instead of cancelling out.
"""

import contextlib
import socket
import struct
import threading
from collections.abc import Callable, Iterable
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
    # Fill dev, ino and nlink too (a real daemon zeroes everything but `error` on failure).
    bare: bool = False

    @classmethod
    def missing(cls, error: int = 2) -> FakeStat:
        return cls(mode=0, size=0, mtime=0, error=error, bare=True)

    def pack(self) -> bytes:
        dev, ino, nlink = (0, 0, 0) if self.bare else (1, 2, 1)
        return _STAT.pack(self.error, dev, ino, self.mode, nlink, 0, 0, self.size, 0, self.mtime, 0)


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
    # What follows DONE in a LIS2 reply: a zeroed entry by default, or stale/odd fields.
    done_trailer: FakeStat | None = None
    # Answer every sync request with FAIL + this message (and drop the connection, like adbd).
    fail_message: bytes | None = None
    # Send this instead of the 4 hex digits of length in the features reply.
    features_length_override: bytes | None = None
    # Every sync command id received, in order (b"LST2", b"LIS2", ...).
    commands: list[bytes] = field(default_factory=list)
    # Every host request received, in order (b"host:transport:FAKESERIAL", b"exec:...", ...).
    requests: list[bytes] = field(default_factory=list)
    # File contents served by RECV (a missing path gets FAIL, like adbd).
    files: dict[bytes, bytes] = field(default_factory=dict)
    # RECV: after this many bytes of file data, stop sending (until the server shuts down).
    recv_stall_after: int | None = None
    # RECV: leave out this many bytes at the end of the file, then send DONE as if complete.
    recv_short_by: int = 0
    # RECV: after this many bytes of file data, drop the connection (no DONE).
    recv_cut_after: int | None = None
    # `exec:<command>`: the command bytes in, stdout chunks out. None refuses exec.
    exec_handler: Callable[[bytes], Iterable[bytes]] | None = None
    # SEND: called with (path, data, mtime) once the file is complete.
    on_send: Callable[[bytes, bytes, int], None] | None = None

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

    @property
    def stopping(self) -> threading.Event:
        """Set when the server shuts down; a stalling exec handler can wait on it."""
        return self._stop

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

    def _read_host_request(self, conn: socket.socket) -> bytes:
        length = int(self._recv(conn, 4), 16)
        request = self._recv(conn, length)
        self.requests.append(request)
        return request

    @staticmethod
    def _fail(conn: socket.socket, message: str) -> None:
        body = message.encode()
        conn.sendall(b"FAIL" + b"%04x" % len(body) + body)

    def _host_phase(self, conn: socket.socket) -> None:
        selected = False
        while True:
            request = self._read_host_request(conn)
            serial = self.serial.encode()
            if request == b"host-serial:" + serial + b":features":
                body = ",".join(self.features).encode()
                length = self.features_length_override or b"%04x" % len(body)
                conn.sendall(b"OKAY" + length + body)
                return
            if request.startswith(b"host:transport:"):
                if request == b"host:transport:" + serial:
                    selected = True
                    conn.sendall(b"OKAY")
                else:
                    self._fail(conn, "device not found")
                    return
            elif request == b"sync:" and selected:
                conn.sendall(b"OKAY")
                self._sync_phase(conn)
                return
            elif request.startswith(b"exec:") and selected and self.exec_handler is not None:
                conn.sendall(b"OKAY")
                for chunk in self.exec_handler(request.removeprefix(b"exec:")):
                    conn.sendall(chunk)
                return  # adbd closes the stream when the command exits
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
            if self.fail_message is not None:
                body = self.fail_message
                self._send_reply(conn, b"FAIL" + struct.pack("<I", len(body)) + body)
                return
            if self.stall:
                self._stop.wait()
                return
            if ident in (b"LST2", b"STA2"):
                # The daemon echoes the request id. (STA2 would follow symlinks; not modelled.)
                found = self.stats.get(path, FakeStat.missing())
                reply = ident + found.pack()
            elif ident == b"LIS2":
                reply = self._list_reply(path)
            elif ident == b"RECV":
                if not self._send_file(conn, path):
                    return
                continue
            elif ident == b"SEND":
                target = path.rsplit(b",", 1)[0]
                data = b""
                while True:
                    kind, size = _REQUEST.unpack(self._recv(conn, _REQUEST.size))
                    if kind == b"DONE":
                        if self.on_send is not None:
                            self.on_send(target, data, size)
                        reply = b"OKAY" + struct.pack("<I", 0)
                        break
                    data += self._recv(conn, size)
            else:
                # Anything else, including v1 LIST/STAT, is refused.
                body = b"unsupported sync command"
                reply = b"FAIL" + struct.pack("<I", len(body)) + body
            if not self._send_reply(conn, reply):
                return
            if reply.startswith(b"FAIL"):
                return

    def _send_file(self, conn: socket.socket, path: bytes) -> bool:
        """Answer RECV: DATA chunks then DONE, or FAIL. False if the connection should drop."""
        data = self.files.get(path)
        if data is None:
            body = b"No such file or directory"
            conn.sendall(b"FAIL" + struct.pack("<I", len(body)) + body)
            return False
        data = data[: len(data) - self.recv_short_by]
        if self.recv_stall_after is not None:
            data = data[: self.recv_stall_after]
        if self.recv_cut_after is not None:
            data = data[: self.recv_cut_after]
        for start in range(0, len(data), 65536):
            chunk = data[start : start + 65536]
            conn.sendall(b"DATA" + struct.pack("<I", len(chunk)) + chunk)
        if self.recv_stall_after is not None:
            self._stop.wait()
            return False
        if self.recv_cut_after is not None:
            return False
        conn.sendall(b"DONE" + struct.pack("<I", 0))
        return True

    def _list_reply(self, path: bytes) -> bytes:
        out = b""
        for name, stat in self.dirs.get(path, []):
            out += b"DNT2" + stat.pack() + _NAMELEN.pack(len(name)) + name
        # DONE is followed by an entry-sized trailer (stat struct and namelen), zeroed by default.
        trailer = self.done_trailer.pack() if self.done_trailer else bytes(_STAT.size)
        return out + b"DONE" + trailer + bytes(_NAMELEN.size)
