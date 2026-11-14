"""FakeTransport: a Transport over a FakePhone, with injectable faults.

Time never comes from the wall clock directly: `clock` and `sleep` are injected, so a hang or a
slow link can be tested instantly with a ManualClock.

Like the real transport it has a pool of `Limits.max_connections` connection slots. A call holds
one while it runs; an open read stream holds one until it is closed, and a walk from its first
`next()` until it ends, is closed or is collected. A call that cannot get a slot within its time
limit raises `DeviceTimeout`.
"""

import errno
import hashlib
import io
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, cast

from sift.device.adb_transport import DEFAULT_LIMITS, Limits
from sift.device.fake_phone import FakePhone, derived_android_id
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
    unstatable: set[str] = field(default_factory=set)  # entries a listing cannot look at
    # Folders that list as empty, with no error, though they hold files: how some phones answer
    # for a folder they refuse to read.
    lists_empty: set[str] = field(default_factory=set)
    offline_for_s: float | None = None  # every call raises DeviceDisconnected for this long
    identity: str | None = None  # report a different phone (serial and Android ID)
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
        self._slot_held = True  # taken by `open_read`; given back on close
        self._t = transport
        self._path = path
        self._pos = offset
        self._end = size
        self._grows = path in transport.faults.size_change_mid_read
        self._broken = False
        self._changed = False

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        if self._slot_held:
            self._slot_held = False
            self._t.release_slot()
        super().close()

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
        limits: Limits = DEFAULT_LIMITS,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.phone = phone
        self.limits = limits
        self.wall_clock = wall_clock
        self._slots_lock = threading.Lock()
        self._slots_used = 0
        self.faults = faults if faults is not None else FaultPlan()
        self.clock = clock
        self.sleep = sleep
        self.stall_timeout_s = stall_timeout_s
        self.journal: list[JournalEntry] = []
        self.listed: list[str] = []  # every folder a walk listed (or tried to), in order
        self.bytes_served = 0
        self.disconnect_spent = False
        self._offline_until: float | None = None
        if self.faults.offline_for_s is not None:
            self.go_offline(self.faults.offline_for_s)

    # -- fault plumbing -------------------------------------------------------------------

    def go_offline(self, seconds: float) -> None:
        """Every call raises DeviceDisconnected until `seconds` have passed on the clock."""
        self._offline_until = self.clock() + seconds

    def come_online(self) -> None:
        """End a `go_offline` early (the cable is plugged back in)."""
        self._offline_until = None

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

    def acquire_slot(self, wait_s: float) -> None:
        """Take a connection slot, waiting (on the injected clock) up to `wait_s` for one."""
        deadline = self.clock() + wait_s
        while True:
            with self._slots_lock:
                if self._slots_used < self.limits.max_connections:
                    self._slots_used += 1
                    return
            left = deadline - self.clock()
            if left <= 0:
                raise DeviceTimeout("Too many connections to the phone are open at once.")
            self.sleep(min(left, 0.05))

    def release_slot(self) -> None:
        with self._slots_lock:
            self._slots_used -= 1

    @property
    def slots_in_use(self) -> int:
        with self._slots_lock:
            return self._slots_used

    @contextmanager
    def _slot(self, wait_s: float) -> Iterator[None]:
        self.acquire_slot(wait_s)
        try:
            yield
        finally:
            self.release_slot()

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
        with self._slot(self.limits.stat_s):
            self._shell("info")
            phone = self.phone
            free = (
                None
                if phone.total_bytes is None
                else max(phone.total_bytes - phone.used_bytes(), 0)
            )
            if self.faults.identity:
                serial: str | None = self.faults.identity
                android_id: str | None = derived_android_id(serial)
            else:
                serial, android_id = phone.serial or None, phone.android_id or None
            if not serial and not android_id:
                raise RemoteError(NO_IDENTITY_MESSAGE)
            return DeviceInfo(
                serial=serial,
                model=phone.model,
                android_version=phone.android_version,
                battery_pct=phone.battery_pct,
                free_bytes=free,
                total_bytes=phone.total_bytes,
                screen_sizes=phone.screen_sizes,
                android_id=android_id,
                phone_time=int(self.wall_clock()) + phone.time_offset_s,
            )

    def walk(
        self,
        root: str,
        on_error: Callable[[RemoteError], None] | None = None,
        prune: Callable[[str], bool] | None = None,
    ) -> Iterator[RemoteEntry]:
        self._begin("walk", root)
        return self._walk_held(root, on_error, prune)

    def _walk_held(
        self,
        root: str,
        on_error: Callable[[RemoteError], None] | None,
        prune: Callable[[str], bool] | None,
    ) -> Iterator[RemoteEntry]:
        with self._slot(self.limits.list_s):
            yield from self._walk(root, on_error, prune, is_root=True)

    def _walk(
        self,
        root: str,
        on_error: Callable[[RemoteError], None] | None,
        prune: Callable[[str], bool] | None,
        *,
        is_root: bool = False,
    ) -> Iterator[RemoteEntry]:
        self.listed.append(root)
        try:
            entries = self._list(root, on_error)
        except RemoteError as e:
            if on_error is None or is_root:
                raise
            on_error(e)
            return
        for entry in entries:
            if entry.is_dir:
                if prune is not None and prune(entry.path):
                    continue
                yield from self._walk(entry.path, on_error, prune)
            else:
                yield entry

    def list_dir(self, path: str) -> list[RemoteEntry]:
        self._begin("list_dir", path)
        with self._slot(self.limits.list_s):
            return self._list(path)

    def _list(
        self, path: str, on_error: Callable[[RemoteError], None] | None = None
    ) -> list[RemoteEntry]:
        self.check_online()
        if not self.phone.is_dir(path):
            raise RemoteError(f"cannot list {path}: no such directory", path, errno=errno.ENOENT)
        if self.phone.is_unreadable(path):
            raise RemoteError(f"cannot list {path}: permission denied", path, errno=errno.EACCES)
        if path.rstrip("/") in self.faults.lists_empty:
            return []
        entries: list[RemoteEntry] = []
        for entry in self.phone.children(path):
            if entry.path in self.faults.unstatable:
                failure = RemoteError(
                    f"cannot look at {entry.path}: permission denied",
                    entry.path,
                    errno=errno.EACCES,
                )
                if on_error is None:
                    raise failure
                on_error(failure)
                continue
            entries.append(entry)
        return entries

    def stat(self, path: str) -> RemoteEntry | None:
        self._begin("stat", path)
        with self._slot(self.limits.stat_s):
            return self.phone.entry(path)

    def open_read(
        self, path: str, offset: int = 0, *, entry: RemoteEntry | None = None
    ) -> BinaryIO:
        self._begin("open_read", path)
        if offset < 0:
            raise ValueError(f"offset must not be negative: {offset}")
        if entry is None:
            with self._slot(self.limits.stat_s):
                entry = self.phone.entry(path)
        if entry is None:
            raise RemoteError(f"cannot open {path}: no such file", path)
        if entry.is_dir or entry.is_symlink:
            kind = "a directory" if entry.is_dir else "a symlink"
            raise RemoteError(f"cannot open {path}: it is {kind}", path)
        self.acquire_slot(self.limits.stat_s)
        return cast(BinaryIO, _FakeStream(self, path.rstrip("/"), entry.size, offset))

    def sha256(self, paths: list[str]) -> dict[str, str | None]:
        self._record_each("sha256", paths, mutating=False)
        self.check_online()
        with self._slot(self.limits.stat_s):
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
        with self._slot(self.limits.delete_s):
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
        with self._slot(self.limits.stat_s):
            if self.phone.is_unreadable(parent):
                raise RemoteError(f"cannot write {remote}: permission denied", remote)
            self.phone.add_file(remote, local.read_bytes(), mtime=mtime)

    def apps_probe(self, kind: str) -> str | None:
        self._begin("apps_probe", kind)
        with self._slot(self.limits.stat_s):
            self._shell("apps_probe")
            apps = self.phone.apps
            if kind == "packages":
                return "".join(
                    f"package:{p}  installer={a.installer or 'null'}\n" for p, a in apps.items()
                )
            if kind == "diskstats":
                names = list(apps)

                def col(f: str) -> str:
                    return ",".join(str(getattr(apps[n], f)) for n in names)

                quoted = ",".join(f'"{n}"' for n in names)
                return (
                    f"Package Names: [{quoted}]\nApp Sizes: [{col('app_bytes')}]\n"
                    f"App Data Sizes: [{col('data_bytes')}]\nCache Sizes: [{col('cache_bytes')}]\n"
                )
            if kind == "usagestats":
                return "".join(
                    f'    package={p} totalTimeUsed="0:01" lastTimeUsed="{a.last_used * 1000}"\n'
                    for p, a in apps.items()
                    if a.last_used
                )
            if kind == "versions":
                return "".join(
                    f"  Package [{p}] (abc):\n    versionName={a.version}\n"
                    for p, a in apps.items()
                )
            raise ValueError(kind)

    def package_paths(self, package: str) -> list[str]:
        self._begin("package_paths", package)
        app = self.phone.apps.get(package)
        return list(app.apk_paths) if app else []

    def uninstall(self, package: str) -> None:
        self._begin("uninstall", package, mutating=True)
        with self._slot(self.limits.delete_s):
            if self.phone.apps.pop(package, None) is None:
                raise RemoteError(f"Failure [DELETE_FAILED_INTERNAL_ERROR] {package}", package)

    def clear_data(self, package: str) -> None:
        self._begin("clear_data", package, mutating=True)
        with self._slot(self.limits.delete_s):
            app = self.phone.apps.get(package)
            if app is None:
                raise RemoteError(f"Failed {package}", package)
            app.data_bytes = app.cache_bytes = 0

    def trim_caches(self) -> None:
        self._begin("trim_caches", None, mutating=True)
        for app in self.phone.apps.values():
            app.cache_bytes = 0

    def remove_dir(self, path: str) -> None:
        self._begin("remove_dir", path, mutating=True)
        with self._slot(self.limits.delete_s):
            self._shell("remove_dir")
            entry = self.phone.entry(path)
            if entry is None or not entry.is_dir:
                raise RemoteError(f"cannot remove {path}: not a folder", path)
            if self._list(path):
                raise RemoteError(f"cannot remove {path}: folder not empty", path)
            self.phone.remove(path)

    def media_rescan(self, paths: list[str]) -> bool:
        self._record_each("media_rescan", paths, mutating=True)
        self.check_online()
        with self._slot(self.limits.stat_s):
            self._shell("media_rescan")
            return True

    def link_health(self) -> LinkHealth:
        self._begin("link_health")
        with self._slot(self.limits.stat_s):
            self._shell("link_health")
            return LinkHealth(kind="usb", speed_mbps=480, latency_ms=1.0)
