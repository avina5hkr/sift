"""AdbTransport against a scripted fake adb server (real sockets on 127.0.0.1, no phone).

Nothing here may start a process: `subprocess.run` and `subprocess.Popen` fail if called.
"""

import gc
import hashlib
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from adbutils import _adb

from sift.device import adb_transport
from sift.device.adb_transport import (
    AdbTransport,
    Limits,
    StallGuardedStream,
    plan_sha_batches,
    resume_allowance_s,
)
from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    FileChanged,
    LinkHealth,
    RemoteEntry,
    RemoteError,
    Transport,
    TransportError,
)
from tests.device.fake_adb_server import S_IFDIR, FakeAdbServer, FakeStat

FIXTURES = Path(__file__).parent / "fixtures"
S_IFLNK = 0o120000
DIR = FakeStat(mode=S_IFDIR | 0o771, size=3452, mtime=1_600_000_000)
FAST = Limits(stat_s=2, list_s=2, read_stall_s=2)


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a subprocess was started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _no_watchdogs_left() -> bool:
    return not any(t.name == "sift-read-watchdog" for t in threading.enumerate())


class Phone:
    """Files and folders served by a FakeAdbServer, kept consistent across LST2/LIS2/RECV."""

    def __init__(self, server: FakeAdbServer) -> None:
        self.server = server
        server.exec_handler = self.run
        self.add_dir(b"/sdcard")

    def _link(self, path: bytes, stat: FakeStat) -> None:
        self.server.stats[path] = stat
        parent, _, name = path.rpartition(b"/")
        if parent:
            listing = self.server.dirs.setdefault(parent, [])
            listing[:] = [(n, s) for n, s in listing if n != name]
            listing.append((name, stat))

    def add_dir(self, path: bytes) -> None:
        self._link(path, DIR)
        self.server.dirs.setdefault(path, [])

    def add_file(self, path: bytes, data: bytes, mtime: int = 1_700_000_000) -> None:
        self._link(path, FakeStat(size=len(data), mtime=mtime))
        self.server.files[path] = data

    def add_symlink(self, path: bytes) -> None:
        self._link(path, FakeStat(mode=S_IFLNK | 0o777, size=21, mtime=5))

    # -- exec: a tiny interpreter for exactly the commands AdbTransport sends --------------

    def run(self, command: bytes) -> Iterable[bytes]:
        text = command.decode("utf-8", "surrogateescape")
        words = shlex.split(text)
        fixed = {
            "getprop ro.serialno": b"FAKESERIAL\n",
            "getprop ro.product.model": b"Pretend Phone\n",
            "getprop ro.build.version.release": b"16\n",
            "df /sdcard": (FIXTURES / "df_sdcard.txt").read_bytes(),
            "wm size": (FIXTURES / "wm_size_override.txt").read_bytes(),
            "dumpsys battery": (FIXTURES / "dumpsys_battery.txt").read_bytes(),
            "true": b"",
        }
        if text in fixed:
            return [fixed[text]]
        if words[:2] == ["tail", "-c"] and words[3] == "--" and words[5:] == ["2>/dev/null"]:
            data = self.server.files.get(words[4].encode("utf-8", "surrogateescape"), b"")
            return [data[int(words[2].lstrip("+")) - 1 :]]
        if words and words[0] == "sha256sum":
            return [self._hashes(words)]
        raise AssertionError(f"unexpected command: {text!r}")

    def _hashes(self, words: list[str]) -> bytes:
        assert len(words) % 7 == 0
        out = b""
        for i in range(0, len(words), 7):
            assert words[i : i + 3] == ["sha256sum", "2>/dev/null", "<"]
            assert words[i + 4 : i + 7] == ["||", "echo", "!"]
            data = self.server.files.get(words[i + 3].encode("utf-8", "surrogateescape"))
            out += b"!\n" if data is None else hashlib.sha256(data).hexdigest().encode() + b"  -\n"
        return out


@pytest.fixture
def server() -> Iterator[FakeAdbServer]:
    with FakeAdbServer() as srv:
        yield srv


@pytest.fixture
def phone(server: FakeAdbServer) -> Phone:
    return Phone(server)


@pytest.fixture
def transport(server: FakeAdbServer, phone: Phone) -> AdbTransport:
    return AdbTransport(server.serial, server.port, limits=FAST, host="127.0.0.1")


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


# -- basics -------------------------------------------------------------------------------------


def test_is_a_transport(transport: AdbTransport) -> None:
    assert isinstance(transport, Transport)


def test_limits_defaults() -> None:
    limits = Limits()
    assert (limits.stat_s, limits.list_s, limits.read_stall_s, limits.delete_s) == (10, 60, 30, 15)
    assert (limits.sha_batch_files, limits.sha_batch_bytes) == (40, 8 * 10**9)
    assert limits.sha_min_s == 120
    assert (limits.sha_assumed_bps, limits.max_connections) == (40 * 10**6, 6)
    assert limits.max_request_bytes == 0xFFFF  # an adb host request's length is 4 hex digits


def test_constructing_contacts_nothing() -> None:
    AdbTransport("FAKESERIAL", _dead_port())


# -- stat ---------------------------------------------------------------------------------------


def test_stat_reports_files_folders_and_symlinks_as_themselves(
    transport: AdbTransport, phone: Phone
) -> None:
    phone.add_file(b"/sdcard/a.jpg", b"hello", mtime=-5)
    phone.add_symlink(b"/sdcard/link")
    assert transport.stat("/sdcard/a.jpg") == RemoteEntry("/sdcard/a.jpg", 5, -5, False, False)
    assert transport.stat("/sdcard/") == RemoteEntry("/sdcard", 3452, 1_600_000_000, True, False)
    assert transport.stat("/sdcard/link") == RemoteEntry("/sdcard/link", 21, 5, False, True)
    assert transport.stat("/sdcard/missing") is None


def test_stat_sends_non_utf8_names_as_their_bytes(transport: AdbTransport, phone: Phone) -> None:
    phone.add_file(b"/sdcard/caf\xe9.jpg", b"x")
    entry = transport.stat("/sdcard/caf\udce9.jpg")
    assert entry is not None
    assert entry.size == 1


def test_stat_of_an_inaccessible_path_raises(
    transport: AdbTransport, server: FakeAdbServer
) -> None:
    server.stats[b"/data/x"] = FakeStat.missing(error=13)
    with pytest.raises(RemoteError) as info:
        transport.stat("/data/x")
    assert info.value.path == "/data/x"


def test_features_are_fetched_once_per_transport(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    phone.add_file(b"/sdcard/a.jpg", b"x")
    for _ in range(3):
        transport.stat("/sdcard/a.jpg")
    transport.list_dir("/sdcard")
    assert server.requests.count(b"host-serial:FAKESERIAL:features") == 1


def test_a_phone_without_sync_v2_is_refused(transport: AdbTransport, server: FakeAdbServer) -> None:
    server.features = ("shell_v2",)
    with pytest.raises(RemoteError, match="too old"):
        transport.stat("/sdcard")


# -- list_dir and walk --------------------------------------------------------------------------


def test_list_dir_is_one_level_with_folders_and_decoded_names(
    transport: AdbTransport, phone: Phone
) -> None:
    phone.add_dir(b"/sdcard/DCIM")
    phone.add_file(b"/sdcard/DCIM/deep.jpg", b"d")
    phone.add_file(b"/sdcard/caf\xe9.jpg", b"c")
    phone.add_file(b"/sdcard/two\nlines.txt", b"t")
    entries = {e.path: e for e in transport.list_dir("/sdcard")}
    assert set(entries) == {"/sdcard/DCIM", "/sdcard/caf\udce9.jpg", "/sdcard/two\nlines.txt"}
    assert entries["/sdcard/DCIM"].is_dir


def test_list_dir_of_a_missing_folder_raises_without_listing(
    transport: AdbTransport, server: FakeAdbServer
) -> None:
    with pytest.raises(RemoteError) as info:
        transport.list_dir("/sdcard/nope")
    assert info.value.path == "/sdcard/nope"
    assert b"LIS2" not in server.commands  # LIS2 would have looked like an empty folder


def test_list_dir_of_a_file_raises(transport: AdbTransport, phone: Phone) -> None:
    phone.add_file(b"/sdcard/a.jpg", b"x")
    with pytest.raises(RemoteError, match="not a folder") as info:
        transport.list_dir("/sdcard/a.jpg")
    assert info.value.path == "/sdcard/a.jpg"


def test_list_dir_through_a_symlinked_root(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    # /sdcard on a real phone is a symlink to the storage folder.
    server.stats[b"/sdcard"] = FakeStat(mode=S_IFLNK | 0o777, size=21)
    server.stats[b"/sdcard/"] = DIR  # a trailing slash makes lstat resolve the link
    phone.add_file(b"/sdcard/a.jpg", b"x")
    assert [e.path for e in transport.list_dir("/sdcard")] == ["/sdcard/a.jpg"]


def test_a_symlink_to_a_missing_target_is_not_listed(transport: AdbTransport, phone: Phone) -> None:
    phone.add_symlink(b"/sdcard/dangling")
    with pytest.raises(RemoteError) as info:
        transport.list_dir("/sdcard/dangling")
    assert info.value.path == "/sdcard/dangling"


def test_walk_is_depth_first_files_and_symlinks_only(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    phone.add_dir(b"/sdcard/A")
    phone.add_dir(b"/sdcard/A/inner")
    phone.add_file(b"/sdcard/A/inner/1.jpg", b"1")
    phone.add_file(b"/sdcard/A/2.jpg", b"2")
    phone.add_dir(b"/sdcard/B")
    phone.add_file(b"/sdcard/B/3.jpg", b"3")
    phone.add_symlink(b"/sdcard/B/link")
    phone.add_file(b"/sdcard/top.txt", b"t")
    entries = list(transport.walk("/sdcard"))
    assert [e.path for e in entries] == [
        "/sdcard/A/2.jpg",
        "/sdcard/A/inner/1.jpg",
        "/sdcard/B/3.jpg",
        "/sdcard/B/link",
        "/sdcard/top.txt",
    ]
    assert entries[3].is_symlink
    # Each folder is checked with LST2 before its LIS2, one folder after another, all on one
    # sync connection for the whole walk.
    assert server.commands.count(b"LIS2") == 4
    assert server.commands.count(b"LST2") == 4
    assert server.requests.count(b"sync:") == 1


def test_walk_of_a_missing_root_raises_naming_it(transport: AdbTransport) -> None:
    with pytest.raises(RemoteError) as info:
        list(transport.walk("/sdcard/nope"))
    assert info.value.path == "/sdcard/nope"


def test_walk_fails_loudly_on_an_entry_the_phone_cannot_stat(
    transport: AdbTransport, server: FakeAdbServer
) -> None:
    server.dirs[b"/sdcard"] = [(b"bad", FakeStat.missing(error=13))]
    with pytest.raises(RemoteError) as info:
        list(transport.walk("/sdcard"))
    assert info.value.path == "/sdcard/bad"


# -- open_read ----------------------------------------------------------------------------------


def test_open_read_streams_the_whole_file_in_chunks(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    data = bytes(range(256)) * 1000  # several DATA packets
    phone.add_file(b"/sdcard/big.bin", data)
    with transport.open_read("/sdcard/big.bin") as stream:
        assert stream.read(7) == data[:7]
        assert stream.read() == data[7:]
        assert stream.read() == b""
    assert b"RECV" in server.commands
    assert _no_watchdogs_left()


def test_open_read_of_an_empty_file(transport: AdbTransport, phone: Phone) -> None:
    phone.add_file(b"/sdcard/empty.dat", b"")
    with transport.open_read("/sdcard/empty.dat") as stream:
        assert stream.read() == b""


@pytest.mark.parametrize("offset", [1, 7, 99, 100, 150])
def test_open_read_from_an_offset_uses_one_based_tail(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer, offset: int
) -> None:
    data = bytes(range(100))
    phone.add_file(b"/sdcard/it's $here.bin", data)
    with transport.open_read("/sdcard/it's $here.bin", offset) as stream:
        assert stream.read() == data[offset:]
    [request] = [r for r in server.requests if r.startswith(b"exec:")]
    assert request.startswith(b"exec:tail -c +%d -- " % (offset + 1))
    assert b"RECV" not in server.commands


def test_open_read_of_a_non_utf8_name(transport: AdbTransport, phone: Phone) -> None:
    phone.add_file(b"/sdcard/caf\xe9.jpg", b"0123456789")
    with transport.open_read("/sdcard/caf\udce9.jpg") as stream:
        assert stream.read() == b"0123456789"
    with transport.open_read("/sdcard/caf\udce9.jpg", 4) as stream:
        assert stream.read() == b"456789"


def test_open_read_of_a_missing_path_raises_at_call_time(
    transport: AdbTransport, server: FakeAdbServer
) -> None:
    with pytest.raises(RemoteError) as info:
        transport.open_read("/sdcard/nope.jpg")
    assert info.value.path == "/sdcard/nope.jpg"
    assert b"RECV" not in server.commands


@pytest.mark.parametrize("path", ["/sdcard", "/sdcard/link"])
def test_open_read_of_a_folder_or_symlink_raises(
    transport: AdbTransport, phone: Phone, path: str
) -> None:
    phone.add_symlink(b"/sdcard/link")
    with pytest.raises(RemoteError) as info:
        transport.open_read(path)
    assert info.value.path == path


def test_open_read_refused_by_the_phone_raises_at_call_time(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    phone.add_file(b"/sdcard/locked.jpg", b"x")
    del server.files[b"/sdcard/locked.jpg"]  # stat works, RECV answers FAIL
    with pytest.raises(RemoteError, match="No such file") as info:
        transport.open_read("/sdcard/locked.jpg")
    assert info.value.path == "/sdcard/locked.jpg"


def test_open_read_rejects_negative_offsets(transport: AdbTransport) -> None:
    with pytest.raises(ValueError):
        transport.open_read("/sdcard/a.jpg", -1)


def test_recv_done_before_the_size_raises_file_changed(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    phone.add_file(b"/sdcard/a.bin", b"x" * 1000)
    server.recv_short_by = 10  # a clean DONE after 990 bytes: the file shrank
    with transport.open_read("/sdcard/a.bin") as stream:
        for _ in range(2):  # and on every later read
            with pytest.raises(FileChanged) as info:
                stream.read()
            assert info.value.path == "/sdcard/a.bin"
            assert str(info.value) == "/sdcard/a.bin changed while it was being read."


def test_recv_cut_off_mid_file_is_a_disconnect(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    phone.add_file(b"/sdcard/a.bin", b"x" * 1000)
    server.recv_cut_after = 100  # the connection drops without DONE
    with transport.open_read("/sdcard/a.bin") as stream, pytest.raises(DeviceDisconnected):
        stream.read()


ONE_SLOT = Limits(stat_s=0.5, list_s=0.5, read_stall_s=2, max_connections=1)


def test_tail_that_ends_short_while_the_phone_answers_raises_file_changed(
    server: FakeAdbServer, phone: Phone
) -> None:
    # One connection slot: the re-stat must not wait for a second one.
    transport = AdbTransport(server.serial, server.port, limits=ONE_SLOT, host="127.0.0.1")
    data = bytes(range(100))
    phone.add_file(b"/sdcard/a.bin", data)
    server.exec_handler = lambda command: [data[10:50]]  # tail stopped early: the file shrank
    with transport.open_read("/sdcard/a.bin", 10) as stream:
        for _ in range(2):
            with pytest.raises(FileChanged) as info:
                stream.read()
            assert info.value.path == "/sdcard/a.bin"
    assert server.commands.count(b"LST2") == 2  # at open, and once more at the short end


def test_tail_that_ends_short_when_the_phone_is_gone_raises_disconnected(
    server: FakeAdbServer, phone: Phone
) -> None:
    transport = AdbTransport(server.serial, server.port, limits=ONE_SLOT, host="127.0.0.1")
    data = bytes(range(100))
    phone.add_file(b"/sdcard/a.bin", data)

    def unplugged(command: bytes) -> list[bytes]:
        server.serial = "GONE"  # from now on adb answers "device not found"
        return [data[10:50]]

    server.exec_handler = unplugged
    with transport.open_read("/sdcard/a.bin", 10) as stream:
        for _ in range(2):
            with pytest.raises(DeviceDisconnected, match="40 of 90 bytes"):
                stream.read()


def test_stalled_phone_raises_timeout_over_a_real_socket(
    server: FakeAdbServer, phone: Phone
) -> None:
    limits = Limits(stat_s=2, list_s=2, read_stall_s=0.2)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_file(b"/sdcard/a.bin", b"x" * 1000)
    server.recv_stall_after = 100
    started = time.monotonic()
    with transport.open_read("/sdcard/a.bin") as stream:
        assert stream.read(100) == b"x" * 100
        with pytest.raises(DeviceTimeout):
            stream.read(100)
        with pytest.raises(DeviceTimeout):
            stream.read(100)  # stays broken
    assert time.monotonic() - started < 2
    assert _no_watchdogs_left()


def test_connections_are_bounded(server: FakeAdbServer, phone: Phone) -> None:
    limits = Limits(stat_s=0.3, list_s=0.3, read_stall_s=2, max_connections=1)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_file(b"/sdcard/a.bin", b"abc")
    stream = transport.open_read("/sdcard/a.bin")  # holds the only connection until closed
    with pytest.raises(DeviceTimeout, match="connections"):
        transport.stat("/sdcard/a.bin")
    stream.close()
    stream.close()  # idempotent: releases the connection once
    assert transport.stat("/sdcard/a.bin") is not None


# -- the stall watchdog on its own --------------------------------------------------------------


def _blocking_chunks(released: threading.Event) -> Iterator[bytes]:
    yield b"abc"
    released.wait(10)  # a phone that stops sending; aborting the connection ends the wait
    raise DeviceDisconnected("connection closed")


def test_read_stall_raises_timeout() -> None:
    aborted = threading.Event()
    stream = StallGuardedStream(
        _blocking_chunks(aborted),
        path="/sdcard/a.bin",
        expected_size=10,
        stall_s=0.1,
        abort=aborted.set,
    )
    started = time.monotonic()
    assert stream.read(3) == b"abc"
    with pytest.raises(DeviceTimeout, match=r"/sdcard/a\.bin"):
        stream.read(3)
    assert aborted.is_set()
    assert time.monotonic() - started < 2
    stream.close()
    assert _no_watchdogs_left()


def test_read_stall_uses_the_injected_clock() -> None:
    now = [0.0]
    aborted = threading.Event()
    stream = StallGuardedStream(
        _blocking_chunks(aborted),
        path="/p",
        expected_size=None,
        stall_s=30,
        abort=aborted.set,
        clock=lambda: now[0],
        poll_s=0.01,
    )
    assert stream.read(3) == b"abc"

    def advance() -> None:
        time.sleep(0.05)
        now[0] = 31.0

    mover = threading.Thread(target=advance)
    started = time.monotonic()
    mover.start()
    with pytest.raises(DeviceTimeout):
        stream.read(1)
    mover.join()
    assert aborted.is_set()  # the watchdog, not the source giving up, ended the read
    assert time.monotonic() - started < 2
    stream.close()
    assert _no_watchdogs_left()


def test_slow_consumer_is_not_a_stall() -> None:
    aborted = threading.Event()
    stream = StallGuardedStream(
        iter([b"ab", b"cd"]), path="/p", expected_size=4, stall_s=0.05, abort=aborted.set
    )
    assert stream.read(2) == b"ab"
    time.sleep(0.2)  # the caller is busy; the phone is not being waited on
    assert stream.read() == b"cd"
    assert not aborted.is_set()
    stream.close()


def test_short_stream_raises_disconnected_every_time() -> None:
    stream = StallGuardedStream(
        iter([b"ab"]), path="/p", expected_size=4, stall_s=5, abort=lambda: None
    )
    assert stream.read(2) == b"ab"
    for _ in range(2):
        with pytest.raises(DeviceDisconnected, match="2 of 4 bytes"):
            stream.read(2)
    stream.close()


def test_short_stream_asks_the_short_end_hook_once() -> None:
    asked: list[tuple[int, int]] = []

    def short_end(received: int, expected: int) -> TransportError:
        asked.append((received, expected))
        return FileChanged("/p changed while it was being read.", "/p")

    stream = StallGuardedStream(
        iter([b"ab"]),
        path="/p",
        expected_size=4,
        stall_s=5,
        abort=lambda: None,
        short_end=short_end,
    )
    assert stream.read(2) == b"ab"
    for _ in range(2):
        with pytest.raises(FileChanged):
            stream.read(2)
    assert asked == [(2, 4)]
    stream.close()


def test_longer_than_expected_is_fine() -> None:
    stream = StallGuardedStream(
        iter([b"abc", b"de"]), path="/p", expected_size=4, stall_s=5, abort=lambda: None
    )
    assert stream.read() == b"abcde"
    stream.close()


def test_close_releases_once_and_closes_the_source() -> None:
    released: list[int] = []
    closed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        try:
            yield b"a"
            yield b"b"
        finally:
            closed.append(True)

    stream = StallGuardedStream(
        chunks(),
        path="/p",
        expected_size=2,
        stall_s=5,
        abort=lambda: None,
        release=lambda: released.append(1),
    )
    assert stream.read(1) == b"a"
    stream.close()
    stream.close()
    assert released == [1]
    assert closed == [True]
    with pytest.raises(ValueError):
        stream.read(1)


# -- sha256 -------------------------------------------------------------------------------------


def test_sha256_maps_by_position_with_hostile_names(transport: AdbTransport, phone: Phone) -> None:
    hostile = [
        b"/sdcard/two\nlines  -",
        b"/sdcard/back\\slash",
        b'/sdcard/it\'s "quoted"',
        b"/sdcard/$(reboot) `id` ; rm -rf x",
        b"/sdcard/caf\xe9 \xff\xfe",
        b"/sdcard/!",
    ]
    for i, raw in enumerate(hostile):
        phone.add_file(raw, b"content %d" % i)
    phone.add_symlink(b"/sdcard/link")
    names = [raw.decode("utf-8", "surrogateescape") for raw in hostile]
    asked = [*names, "/sdcard/missing", "/sdcard", "/sdcard/link"]
    result = transport.sha256(asked)
    assert result == {
        **{n: hashlib.sha256(b"content %d" % i).hexdigest() for i, n in enumerate(names)},
        "/sdcard/missing": None,
        "/sdcard": None,
        "/sdcard/link": None,
    }


def test_sha256_of_nothing_contacts_nothing() -> None:
    assert AdbTransport("FAKESERIAL", _dead_port()).sha256([]) == {}


def test_sha256_runs_one_command_per_batch(server: FakeAdbServer, phone: Phone) -> None:
    limits = Limits(stat_s=2, list_s=2, sha_batch_files=2)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    paths = [f"/sdcard/{i}.jpg" for i in range(5)]
    for p in paths:
        phone.add_file(p.encode(), p.encode())
    result = transport.sha256(paths)
    assert result == {p: hashlib.sha256(p.encode()).hexdigest() for p in paths}
    assert sum(r.startswith(b"exec:sha256sum") for r in server.requests) == 3


def test_sha256_with_the_wrong_number_of_lines_raises(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    phone.add_file(b"/sdcard/a.jpg", b"a")
    phone.add_file(b"/sdcard/b.jpg", b"b")
    server.exec_handler = lambda command: [b"!\n"]
    with pytest.raises(RemoteError, match="1 lines for 2 files"):
        transport.sha256(["/sdcard/a.jpg", "/sdcard/b.jpg"])


def test_sha256_command_that_hangs_times_out(server: FakeAdbServer, phone: Phone) -> None:
    limits = Limits(stat_s=2, list_s=2, sha_min_s=0.2)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_file(b"/sdcard/a.jpg", b"a")

    def hang(command: bytes) -> Iterator[bytes]:
        server.stopping.wait()
        yield b""

    server.exec_handler = hang
    with pytest.raises(DeviceTimeout):
        transport.sha256(["/sdcard/a.jpg"])


def test_sha_batches_respect_count_and_bytes() -> None:
    limits = Limits()
    small = {f"/s/{i}": 1000 for i in range(45)}
    assert [len(b) for b in plan_sha_batches(small, limits)] == [40, 5]
    gb = 10**9
    boundary = {"/a": 3 * gb, "/b": 3 * gb, "/c": 2 * gb, "/d": 1}
    assert plan_sha_batches(boundary, limits) == [["/a", "/b", "/c"], ["/d"]]
    huge = {"/x": 1000, "/nine": 9 * gb, "/y": 1000}
    assert plan_sha_batches(huge, limits) == [["/x"], ["/nine"], ["/y"]]
    assert plan_sha_batches({}, limits) == []


def test_sha_batch_timeout_grows_with_bytes() -> None:
    limits = Limits()
    bps = limits.sha_assumed_bps
    floor, timeout = limits.sha_min_s, adb_transport.sha_batch_timeout
    assert timeout(1000, limits) == floor
    assert timeout(int(floor * bps / 2), limits) == floor
    assert timeout(int(floor * bps * 5), limits) == floor * 5


def test_a_batch_too_long_for_one_command_is_split(
    transport: AdbTransport, phone: Phone, server: FakeAdbServer
) -> None:
    # Single quotes grow fivefold when quoted, so 40 such names cannot fit one adb request.
    paths = [f"/sdcard/{i}" + "'" * 900 for i in range(40)]
    for p in paths:
        phone.add_file(p.encode(), b"q")
    result = transport.sha256(paths)
    assert set(result.values()) == {hashlib.sha256(b"q").hexdigest()}
    commands = [r for r in server.requests if r.startswith(b"exec:")]
    assert len(commands) > 1
    assert all(len(c) <= 0xFFFF for c in commands)


def test_the_request_size_limit_comes_from_limits(server: FakeAdbServer, phone: Phone) -> None:
    limits = Limits(stat_s=2, list_s=2, max_request_bytes=200)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    paths = [f"/sdcard/{i}.jpg" for i in range(6)]
    for p in paths:
        phone.add_file(p.encode(), b"q")
    result = transport.sha256(paths)
    assert set(result.values()) == {hashlib.sha256(b"q").hexdigest()}
    commands = [r for r in server.requests if r.startswith(b"exec:")]
    assert len(commands) > 1
    assert all(len(c) <= 200 for c in commands)


def test_a_request_over_the_limit_is_refused_before_it_is_sent(
    server: FakeAdbServer, phone: Phone
) -> None:
    limits = Limits(stat_s=2, list_s=2, max_request_bytes=40)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_file(b"/sdcard/a-rather-long-name.jpg", b"q")
    with pytest.raises(RemoteError, match="too long"):
        transport.sha256(["/sdcard/a-rather-long-name.jpg"])
    assert not any(r.startswith(b"exec:") for r in server.requests)


# -- info and link health -----------------------------------------------------------------------


def test_info_reads_props_storage_screen_and_battery(transport: AdbTransport) -> None:
    info = transport.info()
    assert info.serial == "FAKESERIAL"
    assert info.model == "Pretend Phone"
    assert info.android_version == "16"
    assert info.battery_pct == 87
    assert (info.free_bytes, info.total_bytes) == (132115952 * 1024, 231011768 * 1024)
    assert info.screen_sizes == ((1080, 2400), (720, 1600))


def test_info_tolerates_unreadable_extras(transport: AdbTransport, server: FakeAdbServer) -> None:
    def props_only(command: bytes) -> list[bytes]:
        if command.startswith(b"getprop"):
            return [b"value\n"]
        return [b"error: not available\n"]

    server.exec_handler = props_only
    info = transport.info()
    assert (info.battery_pct, info.free_bytes, info.total_bytes) == (None, None, None)
    assert info.screen_sizes == ()


def _without_serialno(phone: Phone, android_id: bytes) -> list[bytes]:
    seen: list[bytes] = []

    def run(command: bytes) -> Iterable[bytes]:
        seen.append(command)
        if command == b"getprop ro.serialno":
            return [b"\n"]
        if command == b"settings get secure android_id":
            return [android_id]
        return phone.run(command)

    phone.server.exec_handler = run
    return seen


def test_info_identity_falls_back_to_android_id(transport: AdbTransport, phone: Phone) -> None:
    seen = _without_serialno(phone, b"0000fake000000aa\n")
    assert transport.info().serial == "0000fake000000aa"
    assert b"settings get secure android_id" in seen


def test_info_reads_android_id_only_when_serialno_is_empty(
    transport: AdbTransport, server: FakeAdbServer
) -> None:
    transport.info()
    assert b"exec:settings get secure android_id" not in server.requests


@pytest.mark.parametrize("android_id", [b"\n", b"null\n"])
def test_info_without_serial_or_android_id_raises(
    transport: AdbTransport, phone: Phone, android_id: bytes
) -> None:
    _without_serialno(phone, android_id)
    with pytest.raises(RemoteError, match="serial number"):
        transport.info()


def test_info_without_a_model_raises(transport: AdbTransport, server: FakeAdbServer) -> None:
    server.exec_handler = lambda command: [b""]
    with pytest.raises(RemoteError):
        transport.info()


def test_link_health_over_usb(transport: AdbTransport) -> None:
    health = transport.link_health()
    assert health.kind == "usb"
    assert health.latency_ms is not None
    assert health.latency_ms >= 0


@pytest.mark.parametrize(
    "serial",
    ["192.0.2.10:5555", "[fe80::1]:5555", "adb-FAKESERIAL-AbCdEf._adb-tls-connect._tcp"],
)
def test_link_health_over_wifi(serial: str) -> None:
    with FakeAdbServer(serial=serial) as srv:
        Phone(srv)
        transport = AdbTransport(serial, srv.port, limits=FAST, host="127.0.0.1")
        health = transport.link_health()
        assert health == LinkHealth("wifi", None, health.latency_ms)


@pytest.mark.parametrize(
    ("serial", "wifi"),
    [
        ("192.0.2.10:5555", True),
        ("[fe80::1]:5555", True),
        ("[2001:db8::7%en0]:37199", True),
        ("adb-FAKESERIAL-AbCdEf._adb-tls-connect._tcp", True),
        ("FAKESERIAL", False),
        ("emulator-5554", False),
        ("[fe80::1]", False),
        ("fe80::1:5555", False),
    ],
)
def test_is_wifi_serial(serial: str, wifi: bool) -> None:
    assert adb_transport.is_wifi_serial(serial) is wifi


# -- errors -------------------------------------------------------------------------------------


def test_adb_server_not_running_raises_disconnected() -> None:
    transport = AdbTransport("FAKESERIAL", _dead_port(), limits=FAST)
    with pytest.raises(DeviceDisconnected):
        transport.stat("/sdcard")
    with pytest.raises(DeviceDisconnected):
        transport.info()


def test_unknown_serial_raises_disconnected(server: FakeAdbServer) -> None:
    transport = AdbTransport("OTHERSERIAL", server.port, limits=FAST, host="127.0.0.1")
    with pytest.raises(DeviceDisconnected):
        transport.info()
    with pytest.raises(DeviceDisconnected):
        transport.stat("/sdcard")


def test_adb_transport_cannot_write_in_m1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise AssertionError("a write reached adb")

    monkeypatch.setattr(adb_transport.adbutils.AdbClient, "make_connection", unreachable)
    monkeypatch.setattr(adb_transport, "Sync2", unreachable)
    monkeypatch.setattr(adb_transport, "device_features", unreachable)
    monkeypatch.setattr(socket, "create_connection", unreachable)
    transport = AdbTransport("FAKESERIAL", _dead_port())
    local = tmp_path / "a.jpg"
    local.write_bytes(b"x")
    calls = [
        lambda: transport.delete("/sdcard/a.jpg"),
        lambda: transport.push(local, "/sdcard/a.jpg", 0),
        lambda: transport.media_rescan(["/sdcard/a.jpg"]),
    ]
    for call in calls:
        with pytest.raises(NotImplementedError, match="Deleting and restoring arrive in M8"):
            call()


def test_a_remote_error_mid_stream_keeps_its_path_on_every_read() -> None:
    def chunks() -> Iterator[bytes]:
        yield b"a"
        raise RemoteError("I/O error", "/sdcard/a.bin")

    stream = StallGuardedStream(
        chunks(), path="/sdcard/a.bin", expected_size=2, stall_s=5, abort=lambda: None
    )
    assert stream.read(1) == b"a"
    for _ in range(2):
        with pytest.raises(RemoteError, match="I/O error") as info:
            stream.read(1)
        assert info.value.path == "/sdcard/a.bin"
    stream.close()


# -- fix round 1 --------------------------------------------------------------------------------


def test_resume_allowance_covers_the_bytes_tail_must_skip() -> None:
    limits = Limits()
    bps = limits.sha_assumed_bps
    assert resume_allowance_s(0, limits) == limits.read_stall_s
    assert resume_allowance_s(int(limits.read_stall_s * bps / 2), limits) == limits.read_stall_s
    assert resume_allowance_s(int(limits.read_stall_s * bps * 2), limits) == limits.read_stall_s * 2


def _clocked_stream(chunks: Iterator[bytes], now: list[float], aborted: threading.Event):
    return StallGuardedStream(
        chunks,
        path="/p",
        expected_size=None,
        stall_s=30,
        first_chunk_stall_s=60,
        abort=aborted.set,
        clock=lambda: now[0],
        poll_s=0.01,
    )


def test_first_chunk_after_the_stall_limit_but_within_the_allowance_is_fine() -> None:
    now = [0.0]
    aborted = threading.Event()

    def chunks() -> Iterator[bytes]:
        now[0] = 45.0  # tail is still skipping: past stall_s, within the allowance
        time.sleep(0.1)  # give the watchdog time to look
        yield b"first"

    stream = _clocked_stream(chunks(), now, aborted)
    assert stream.read() == b"first"
    assert not aborted.is_set()
    stream.close()


def test_first_chunk_past_the_allowance_times_out() -> None:
    now = [0.0]
    aborted = threading.Event()

    def chunks() -> Iterator[bytes]:
        now[0] = 61.0
        aborted.wait(5)
        raise DeviceDisconnected("connection closed")
        yield b""  # pragma: no cover

    stream = _clocked_stream(chunks(), now, aborted)
    with pytest.raises(DeviceTimeout):
        stream.read(1)
    assert aborted.is_set()
    stream.close()


def test_after_the_first_chunk_the_normal_stall_limit_applies() -> None:
    now = [0.0]
    aborted = threading.Event()

    def chunks() -> Iterator[bytes]:
        yield b"first"
        now[0] += 31.0  # under the allowance, over stall_s
        aborted.wait(5)
        raise DeviceDisconnected("connection closed")

    stream = _clocked_stream(chunks(), now, aborted)
    assert stream.read(5) == b"first"
    with pytest.raises(DeviceTimeout):
        stream.read(1)
    assert aborted.is_set()
    stream.close()


def test_offset_read_waits_for_tail_to_skip(server: FakeAdbServer, phone: Phone) -> None:
    # read_stall_s is 0.2 s, but skipping 99 bytes at 100 B/s is allowed 0.99 s.
    limits = Limits(stat_s=2, list_s=2, read_stall_s=0.2, sha_assumed_bps=100)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    data = bytes(range(200))
    phone.add_file(b"/sdcard/big.bin", data)

    def slow_tail(command: bytes) -> Iterator[bytes]:
        time.sleep(0.5)
        yield data[99:]

    server.exec_handler = slow_tail
    with transport.open_read("/sdcard/big.bin", 99) as stream:
        assert stream.read() == data[99:]


def test_an_abandoned_stream_is_collected_and_frees_everything(
    server: FakeAdbServer, phone: Phone, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = Limits(stat_s=0.5, list_s=0.5, read_stall_s=2, max_connections=1)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_file(b"/sdcard/a.bin", b"x" * 200_000)
    sockets: list[socket.socket] = []
    create = _adb.AdbConnection._create_socket

    def tracking(self: object) -> socket.socket:
        s: socket.socket = create(self)
        sockets.append(s)
        return s

    monkeypatch.setattr(_adb.AdbConnection, "_create_socket", tracking)
    stream = transport.open_read("/sdcard/a.bin")
    assert stream.read(10) == b"x" * 10
    del stream
    gc.collect()
    assert sockets
    assert all(s.fileno() == -1 for s in sockets)
    assert _no_watchdogs_left()
    assert transport.stat("/sdcard/a.bin") is not None  # the only slot is free again


def test_close_from_another_thread_wakes_a_blocked_reader(
    server: FakeAdbServer, phone: Phone
) -> None:
    limits = Limits(stat_s=2, list_s=2, read_stall_s=30)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_file(b"/sdcard/a.bin", b"x" * 1000)
    server.recv_stall_after = 100
    stream = transport.open_read("/sdcard/a.bin")
    assert stream.read(100) == b"x" * 100
    outcome: list[object] = []

    def reader() -> None:
        try:
            outcome.append(stream.read(100))
        except (TransportError, ValueError) as e:
            outcome.append(e)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.1)  # the reader is now blocked waiting for the phone
    started = time.monotonic()
    stream.close()
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2
    assert len(outcome) == 1
    assert _no_watchdogs_left()


@pytest.mark.parametrize("ident", [b"DATA", b"FAIL"])
def test_oversized_sync_lengths_are_refused(ident: bytes) -> None:
    a, b = socket.socketpair()
    with a, b:
        b.sendall(adb_transport._U32.pack(64 * 1024 + 1))
        with pytest.raises(TransportError, match="too long") as info:
            next(adb_transport._recv_chunks(a, "/p", ident))
        assert type(info.value) is TransportError


def test_a_full_64k_data_packet_is_accepted() -> None:
    a, b = socket.socketpair()
    with a, b:
        packet = adb_transport._U32.pack(64 * 1024) + b"z" * 64 * 1024
        sender = threading.Thread(target=b.sendall, args=(packet,))  # more than the buffer
        sender.start()
        assert next(adb_transport._recv_chunks(a, "/p", b"DATA")) == b"z" * 64 * 1024
        sender.join()


_RELATIVE_CALLS = {
    "stat": lambda t, p: t.stat(p),
    "list_dir": lambda t, p: t.list_dir(p),
    "walk": lambda t, p: list(t.walk(p)),
    "open_read": lambda t, p: t.open_read(p),
    "open_read_offset": lambda t, p: t.open_read(p, 5),
    "sha256": lambda t, p: t.sha256(["/sdcard/ok.jpg", p]),
}


@pytest.mark.parametrize("call", list(_RELATIVE_CALLS))
@pytest.mark.parametrize("path", ["sdcard/a.jpg", "", "-n"])
def test_relative_paths_are_refused_before_anything_is_sent(
    transport: AdbTransport, server: FakeAdbServer, call: str, path: str
) -> None:
    with pytest.raises(RemoteError) as info:
        _RELATIVE_CALLS[call](transport, path)
    assert info.value.path == path
    assert server.requests == []


def test_an_abandoned_walk_frees_its_connection(server: FakeAdbServer, phone: Phone) -> None:
    limits = Limits(stat_s=0.5, list_s=0.5, read_stall_s=2, max_connections=1)
    transport = AdbTransport(server.serial, server.port, limits=limits, host="127.0.0.1")
    phone.add_dir(b"/sdcard/A")
    phone.add_file(b"/sdcard/A/1.jpg", b"1")
    phone.add_file(b"/sdcard/A/2.jpg", b"2")
    unstarted = transport.walk("/sdcard")  # holds nothing until the first next()
    assert transport.stat("/sdcard/A/1.jpg") is not None
    del unstarted
    walking = transport.walk("/sdcard")
    assert next(walking).path == "/sdcard/A/1.jpg"
    with pytest.raises(DeviceTimeout, match="connections"):
        transport.stat("/sdcard/A/2.jpg")  # the walk holds the only connection
    del walking
    gc.collect()
    assert transport.stat("/sdcard/A/2.jpg") is not None
