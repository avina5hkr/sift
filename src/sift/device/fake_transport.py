"""FakeTransport: a Transport over a FakePhone, with injectable faults.

Time never comes from the wall clock directly: `clock` and `sleep` are injected, so a hang or a
slow link can be tested instantly with a ManualClock.
"""

import hashlib
import io
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, cast

from sift.device.fake_phone import FakePhone
from sift.device.transport import (
    NO_IDENTITY_MESSAGE,
    DeviceDisconnected,
    DeviceInfo,
    DeviceTimeout,
    FileChanged,
    LinkHealth,
    RemoteEntry,
    RemoteError,
)

_READ_CHUNK = 1 << 20


@dataclass
class FaultPlan:
    """What should go wrong. The transport reads this live, so a test may change it mid-way."""

    disconnect_after_bytes: int | None = None  # one-shot: after N bytes served, reads fail once
    hang_on: set[str] = field(default_factory=set)  # reads (and hashing) never yield
    slow_read_bps: int | None = None
    size_change_mid_read: set[str] = field(default_factory=set)  # FileChanged at the old end
    wrong_sha256: set[str] = field(default_factory=set)
    refuse_delete: set[str] = field(default_factory=set)
    offline_for_s: float | None = None  # every call raises DeviceDisconnected for this long
    identity: str | None = None  # report a different serial
    garbage_output: bool = False  # shell-like calls return unparseable text
    short_reads: bool = False  # read(n) returns about a third of n (at least 1 byte)


@dataclass(frozen=True)
class JournalEntry:
    op: str
    path: str | None
    mutating: bool


class ManualClock:
    """A clock for tests: it only moves when told to. `sleep` advances it instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class _FakeStream(io.RawIOBase):
    """A non-seekable byte stream over a phone file, subject to the fault plan.

    Like the real transport, a file that ends short of the size it had at open (or is removed)
    while the phone is there raises `FileChanged`, after whatever bytes were still there, and
    again on every later read. A path in `size_change_mid_read` raises it when the read reaches
    the size seen at open (the file grew meanwhile).
    """

    def __init__(self, transport: FakeTransport, path: str, size: int, offset: int) -> None:
        super().__init__()
        self._t = transport
        self._path = path
        self._pos = offset
        self._end = size
        self._grows = path in transport.faults.size_change_mid_read
        self._broken = False
        self._changed = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def read(self, size: int | None = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if size is None or size < 0:
            parts = []
            while chunk := self._read_chunk(_READ_CHUNK):
                parts.append(chunk)
            return b"".join(parts)
        return self._read_chunk(size)

    def readall(self) -> bytes:
        return self.read(-1)

    def _file_changed(self) -> FileChanged:
        self._changed = True
        return FileChanged(f"{self._path} changed while it was being read.", self._path)

    def _read_chunk(self, wanted: int) -> bytes:
        t = self._t
        if self._broken:
            raise DeviceDisconnected("device disconnected during read")
        if self._changed:
            raise self._file_changed()
        t.check_online()
        if wanted == 0:
            return b""
        if self._pos >= self._end:
            if self._grows:
                raise self._file_changed()
            return b""
        if self._path in t.faults.hang_on:
            t.wait_for_stall()
        count = min(wanted, self._end - self._pos)
        if t.faults.short_reads:
            count = max(1, count // 3)
        limit = t.faults.disconnect_after_bytes
        if limit is not None and not t.disconnect_spent:
            remaining = limit - t.bytes_served
            if remaining <= 0:
                t.disconnect_spent = True
                self._broken = True
                raise DeviceDisconnected(f"device disconnected after {limit} bytes")
            count = min(count, remaining)
        try:
            data = t.phone.read_range(self._path, self._pos, count)
        except KeyError:  # removed (or replaced by a folder) mid-read
            raise self._file_changed() from None
        if not data:
            raise self._file_changed()
        if len(data) < count:
            self._changed = True  # the file shrank: what is left now, FileChanged next
        if t.faults.slow_read_bps:
            t.sleep(len(data) / t.faults.slow_read_bps)
        self._pos += len(data)
        t.bytes_served += len(data)
        return data


class FakeTransport:
    """Implements Transport over a FakePhone.

    Every call is recorded in `journal` (mutating ones flagged) before any fault can fire.
    Calls the real transport makes through the adb shell (info, sha256, delete, media_rescan,
    link_health) are the ones `garbage_output` affects; sync-protocol calls are not.
    """

    def __init__(
        self,
        phone: FakePhone,
        faults: FaultPlan | None = None,
        clock: Callable[[], float] = time.monotonic,
        stall_timeout_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.phone = phone
        self.faults = faults if faults is not None else FaultPlan()
        self.clock = clock
        self.sleep = sleep
        self.stall_timeout_s = stall_timeout_s
        self.journal: list[JournalEntry] = []
        self.bytes_served = 0
        self.disconnect_spent = False
        self._offline_until: float | None = None
        if self.faults.offline_for_s is not None:
            self.go_offline(self.faults.offline_for_s)

    # -- fault plumbing -------------------------------------------------------------------

    def go_offline(self, seconds: float) -> None:
        """Every call raises DeviceDisconnected until `seconds` have passed on the clock."""
        self._offline_until = self.clock() + seconds

    def check_online(self) -> None:
        if self._offline_until is None:
            return
        if self.clock() < self._offline_until:
            raise DeviceDisconnected("device offline")
        self._offline_until = None

    def wait_for_stall(self) -> None:
        """Block (on the injected clock) until the stall timeout passes, then raise."""
        started = self.clock()
        while True:
            waited = self.clock() - started
            if waited >= self.stall_timeout_s:
                raise DeviceTimeout(f"no data from the phone for {self.stall_timeout_s:g} s")
            self.sleep(min(self.stall_timeout_s - waited, 1.0))

    def _begin(self, op: str, path: str | None = None, *, mutating: bool = False) -> None:
        self.journal.append(JournalEntry(op, path, mutating))
        self.check_online()

    def _record_each(self, op: str, paths: list[str], *, mutating: bool) -> None:
        targets: list[str | None] = [*paths] if paths else [None]
        self.journal.extend(JournalEntry(op, path, mutating) for path in targets)

    def _shell(self, op: str) -> None:
        if self.faults.garbage_output:
            raise RemoteError(f"unparseable output from the phone for {op}: '\\x00<<garbage>>'")

    # -- Transport ------------------------------------------------------------------------

    def info(self) -> DeviceInfo:
        self._begin("info")
        self._shell("info")
        phone = self.phone
        free = None if phone.total_bytes is None else max(phone.total_bytes - phone.used_bytes(), 0)
        identity = self.faults.identity or phone.serial or phone.android_id
        if not identity:
            raise RemoteError(NO_IDENTITY_MESSAGE)
        return DeviceInfo(
            serial=identity,
            model=phone.model,
            android_version=phone.android_version,
            battery_pct=phone.battery_pct,
            free_bytes=free,
            total_bytes=phone.total_bytes,
            screen_sizes=phone.screen_sizes,
        )

    def walk(self, root: str) -> Iterator[RemoteEntry]:
        self._begin("walk", root)
        return self._walk(root)

    def _walk(self, root: str) -> Iterator[RemoteEntry]:
        for entry in self._list(root):
            if entry.is_dir:
                yield from self._walk(entry.path)
            else:
                yield entry

    def list_dir(self, path: str) -> list[RemoteEntry]:
        self._begin("list_dir", path)
        return self._list(path)

    def _list(self, path: str) -> list[RemoteEntry]:
        self.check_online()
        if not self.phone.is_dir(path):
            raise RemoteError(f"cannot list {path}: no such directory", path)
        if path.rstrip("/") in self.phone.unreadable:
            raise RemoteError(f"cannot list {path}: permission denied", path)
        return self.phone.children(path)

    def stat(self, path: str) -> RemoteEntry | None:
        self._begin("stat", path)
        return self.phone.entry(path)

    def open_read(self, path: str, offset: int = 0) -> BinaryIO:
        self._begin("open_read", path)
        if offset < 0:
            raise ValueError(f"offset must not be negative: {offset}")
        entry = self.phone.entry(path)
        if entry is None:
            raise RemoteError(f"cannot open {path}: no such file", path)
        if entry.is_dir or entry.is_symlink:
            kind = "a directory" if entry.is_dir else "a symlink"
            raise RemoteError(f"cannot open {path}: it is {kind}", path)
        return cast(BinaryIO, _FakeStream(self, path.rstrip("/"), entry.size, offset))

    def sha256(self, paths: list[str]) -> dict[str, str | None]:
        self._record_each("sha256", paths, mutating=False)
        self.check_online()
        self._shell("sha256")
        result: dict[str, str | None] = {}
        for path in paths:
            entry = self.phone.entry(path)
            if entry is None or entry.is_dir or entry.is_symlink:
                result[path] = None
                continue
            if path in self.faults.hang_on:
                self.wait_for_stall()
            digest = self.phone.sha256_of(path)
            if path in self.faults.wrong_sha256:
                digest = hashlib.sha256(b"wrong:" + digest.encode()).hexdigest()
            result[path] = digest
        return result

    def delete(self, path: str) -> None:
        self._begin("delete", path, mutating=True)
        self._shell("delete")
        if path in self.faults.refuse_delete:
            raise RemoteError(f"cannot delete {path}: permission denied", path)
        entry = self.phone.entry(path)
        if entry is None:
            raise RemoteError(f"cannot delete {path}: no such file", path)
        if entry.is_dir:
            raise RemoteError(f"cannot delete {path}: it is a directory", path)
        self.phone.remove(path)

    def push(self, local: Path, remote: str, mtime: int) -> None:
        self._begin("push", remote, mutating=True)
        parent = remote.rstrip("/").rsplit("/", 1)[0] or "/"
        if parent in self.phone.unreadable:
            raise RemoteError(f"cannot write {remote}: permission denied", remote)
        self.phone.add_file(remote, local.read_bytes(), mtime=mtime)

    def media_rescan(self, paths: list[str]) -> bool:
        self._record_each("media_rescan", paths, mutating=True)
        self.check_online()
        self._shell("media_rescan")
        return True

    def link_health(self) -> LinkHealth:
        self._begin("link_health")
        self._shell("link_health")
        return LinkHealth(kind="usb", speed_mbps=480, latency_ms=1.0)
