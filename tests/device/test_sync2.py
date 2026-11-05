"""The adb sync v2 client, against a scripted fake adb server (real sockets on 127.0.0.1)."""

import socket
from collections.abc import Iterator

import pytest

from sift.device.sync2 import Sync2, SyncStat, device_features
from sift.device.transport import DeviceDisconnected, DeviceTimeout, RemoteError
from tests.device.fake_adb_server import S_IFDIR, S_IFREG, FakeAdbServer, FakeStat

TIMEOUT_S = 2.0
BIG = 4_420_000_000  # more than 2**32
MTIMES = [0, -315_619_200, 2**31 + 1, 2**40]  # epoch, 1960, just after 2038, far future


@pytest.fixture
def server() -> Iterator[FakeAdbServer]:
    with FakeAdbServer() as srv:
        yield srv


def connect(srv: FakeAdbServer, timeout_s: float = TIMEOUT_S) -> Sync2:
    return Sync2("127.0.0.1", srv.port, srv.serial, timeout_s=timeout_s)


def test_stat_returns_mode_size_mtime(server: FakeAdbServer) -> None:
    server.stats[b"/sdcard/a.jpg"] = FakeStat(size=1234, mtime=1_700_000_000)
    with connect(server) as sync:
        assert sync.stat(b"/sdcard/a.jpg") == SyncStat(
            mode=S_IFREG | 0o644, size=1234, mtime=1_700_000_000, error=0
        )


def test_lis2_reports_size_over_4gib(server: FakeAdbServer) -> None:
    server.dirs[b"/sdcard/DCIM"] = [(b"big.mp4", FakeStat(size=BIG, mtime=1_700_000_000))]
    with connect(server) as sync:
        [(name, stat)] = list(sync.list(b"/sdcard/DCIM"))
    assert name == b"big.mp4"
    assert stat.size == BIG


@pytest.mark.parametrize("size", [BIG, 2**32, 2**40, 2**63 - 1])
def test_sta2_reports_size_over_4gib(server: FakeAdbServer, size: int) -> None:
    server.stats[b"/sdcard/big.bin"] = FakeStat(size=size)
    with connect(server) as sync:
        assert sync.stat(b"/sdcard/big.bin").size == size


@pytest.mark.parametrize("mtime", MTIMES)
def test_odd_and_zero_mtimes_round_trip(server: FakeAdbServer, mtime: int) -> None:
    server.stats[b"/sdcard/f"] = FakeStat(size=1, mtime=mtime)
    server.dirs[b"/sdcard"] = [(b"f", FakeStat(size=1, mtime=mtime))]
    with connect(server) as sync:
        assert sync.stat(b"/sdcard/f").mtime == mtime
        [(_, listed)] = list(sync.list(b"/sdcard"))
    assert listed.mtime == mtime
    assert type(listed.mtime) is int


def test_names_are_bytes_safe(server: FakeAdbServer) -> None:
    names = [b"caf\xe9.jpg", b"new\nline", b"trail.", b" AI Media"]
    server.dirs[b"/sdcard"] = [(name, FakeStat(size=i)) for i, name in enumerate(names)]
    with connect(server) as sync:
        listed = list(sync.list(b"/sdcard"))
    assert [name for name, _ in listed] == names
    assert all(isinstance(name, bytes) for name, _ in listed)


def test_list_excludes_dot_and_dotdot_and_keeps_dir_mode(server: FakeAdbServer) -> None:
    server.dirs[b"/sdcard"] = [
        (b".", FakeStat(mode=S_IFDIR | 0o755)),
        (b"..", FakeStat(mode=S_IFDIR | 0o755)),
        (b"sub", FakeStat(mode=S_IFDIR | 0o755)),
        (b"f.txt", FakeStat(size=3)),
    ]
    with connect(server) as sync:
        listed = dict(sync.list(b"/sdcard"))
    assert set(listed) == {b"sub", b"f.txt"}
    assert listed[b"sub"].mode == S_IFDIR | 0o755


def test_empty_directory_lists_nothing(server: FakeAdbServer) -> None:
    with connect(server) as sync:
        assert list(sync.list(b"/sdcard/empty")) == []


def test_connection_is_reusable_for_several_requests(server: FakeAdbServer) -> None:
    server.stats[b"/a"] = FakeStat(size=1)
    server.dirs[b"/d"] = [(b"x", FakeStat(size=2))]
    with connect(server) as sync:
        assert sync.stat(b"/a").size == 1
        assert [n for n, _ in sync.list(b"/d")] == [b"x"]
        assert sync.stat(b"/a").size == 1


def test_stat_missing_returns_error_errno(server: FakeAdbServer) -> None:
    with connect(server) as sync:
        assert sync.stat(b"/sdcard/nope").error == 2  # ENOENT


@pytest.mark.parametrize("cut", [2, 4, 30, 71])
def test_short_read_raises_disconnected_on_stat(server: FakeAdbServer, cut: int) -> None:
    server.stats[b"/f"] = FakeStat(size=1)
    server.truncate_reply_after = cut
    with connect(server) as sync, pytest.raises(DeviceDisconnected):
        sync.stat(b"/f")


@pytest.mark.parametrize("cut", [10, 79, 93, 168])  # mid-entry, mid-name, mid-DONE
def test_short_read_raises_disconnected(server: FakeAdbServer, cut: int) -> None:
    server.dirs[b"/d"] = [(b"one.jpg", FakeStat(size=1)), (b"two.jpg", FakeStat(size=2))]
    server.truncate_reply_after = cut
    with connect(server) as sync, pytest.raises(DeviceDisconnected):
        list(sync.list(b"/d"))


def test_features_gate(server: FakeAdbServer) -> None:
    assert {"stat_v2", "ls_v2"} <= device_features("127.0.0.1", server.port, server.serial)
    full = server.features
    for missing in ("stat_v2", "ls_v2"):
        server.features = tuple(f for f in full if f != missing)
        with pytest.raises(RemoteError, match="too old for Sift"):
            connect(server)
    assert server.commands == []


def test_timeout_raises_device_timeout(server: FakeAdbServer) -> None:
    server.stall = True
    with connect(server, timeout_s=0.2) as sync, pytest.raises(DeviceTimeout):
        sync.stat(b"/f")


def test_v1_truncation_is_not_used(server: FakeAdbServer) -> None:
    server.stats[b"/f"] = FakeStat(size=BIG)
    server.dirs[b"/d"] = [(b"x", FakeStat(size=BIG))]
    with connect(server) as sync:
        sync.stat(b"/f")
        list(sync.list(b"/d"))
    assert server.commands == [b"STA2", b"LIS2"]
    assert b"LIST" not in server.commands
    assert b"STAT" not in server.commands


def test_unknown_serial_raises_disconnected(server: FakeAdbServer) -> None:
    with pytest.raises(DeviceDisconnected):
        Sync2("127.0.0.1", server.port, "OTHERSERIAL", timeout_s=TIMEOUT_S)


def test_adb_server_not_running_raises_disconnected() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(DeviceDisconnected):
        device_features("127.0.0.1", port, "FAKESERIAL", timeout_s=TIMEOUT_S)


def test_abandoned_listing_closes_the_connection(server: FakeAdbServer) -> None:
    server.dirs[b"/d"] = [(b"a", FakeStat()), (b"b", FakeStat())]
    sync = connect(server)
    entries = sync.list(b"/d")
    next(entries)
    entries.close()  # the rest of the reply is still on the wire, so the stream is unusable
    with pytest.raises(DeviceDisconnected):
        sync.stat(b"/d")
    sync.close()
    sync.close()  # idempotent
