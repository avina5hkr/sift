"""The adb sync v2 client, against a scripted fake adb server (real sockets on 127.0.0.1)."""

import socket
from collections.abc import Iterator

import pytest

from sift.device.sync2 import Sync2, SyncStat, device_features
from sift.device.transport import DeviceDisconnected, DeviceTimeout, RemoteError, TransportError
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


def test_stat_missing_reply_is_zeroed_apart_from_error(server: FakeAdbServer) -> None:
    with connect(server) as sync:
        assert sync.stat(b"/nope") == SyncStat(mode=0, size=0, mtime=0, error=2)


def test_done_trailer_with_error_and_stale_fields_is_ignored(server: FakeAdbServer) -> None:
    server.dirs[b"/d"] = [(b"a", FakeStat(size=5)), (b"b", FakeStat(size=6))]
    server.done_trailer = FakeStat(mode=S_IFREG | 0o600, size=99, mtime=123, error=13)
    with connect(server) as sync:
        assert [(n, s.size) for n, s in sync.list(b"/d")] == [(b"a", 5), (b"b", 6)]
        assert sync.stat(b"/d").error == 2  # the stream is still in step afterwards


def test_entry_error_is_surfaced_on_that_entry_only(server: FakeAdbServer) -> None:
    server.dirs[b"/d"] = [
        (b"ok", FakeStat(size=5, mtime=9)),
        (b"bad", FakeStat.missing(error=13)),  # EACCES
        (b"ok2", FakeStat(size=6)),
    ]
    with connect(server) as sync:
        listed = dict(sync.list(b"/d"))
    assert listed[b"bad"].error == 13
    assert listed[b"ok"].error == 0
    assert listed[b"ok2"] == SyncStat(mode=S_IFREG | 0o644, size=6, mtime=0, error=0)


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
    assert server.commands == [b"LST2", b"LIS2"]
    assert b"LIST" not in server.commands
    assert b"STAT" not in server.commands


def test_stat_never_sends_sta2(server: FakeAdbServer) -> None:
    # STA2 follows symlinks; stat() must use LST2 so a link is reported as itself.
    server.stats[b"/link"] = FakeStat(size=7)
    with connect(server) as sync:
        sync.stat(b"/link")
        sync.stat(b"/missing")
    assert server.commands == [b"LST2", b"LST2"]
    assert b"STA2" not in server.commands


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


def test_stat_timeout_closes_the_connection(server: FakeAdbServer) -> None:
    server.stall = True
    with connect(server, timeout_s=0.2) as sync:
        with pytest.raises(DeviceTimeout):
            sync.stat(b"/first")
        # A late reply to /first must never be read as the answer for /second.
        with pytest.raises(DeviceDisconnected, match="closed"):
            sync.stat(b"/second")
        with pytest.raises(DeviceDisconnected, match="closed"):
            list(sync.list(b"/d"))


def test_stat_protocol_error_closes_the_connection(server: FakeAdbServer) -> None:
    server.stats[b"/f"] = FakeStat(size=1)
    server.truncate_reply_after = 30
    with connect(server) as sync:
        with pytest.raises(DeviceDisconnected):
            sync.stat(b"/f")
        with pytest.raises(DeviceDisconnected, match="closed"):
            sync.stat(b"/f")


@pytest.mark.parametrize("call", ["stat", "list"])
def test_sync_fail_raises_remote_error_and_closes(server: FakeAdbServer, call: str) -> None:
    server.fail_message = b"remote said no"
    with connect(server) as sync:
        with pytest.raises(RemoteError, match="remote said no") as info:
            if call == "stat":
                sync.stat(b"/f")
            else:
                list(sync.list(b"/f"))
        assert info.value.path == "/f"
        with pytest.raises(DeviceDisconnected, match="closed"):
            sync.stat(b"/f")


@pytest.mark.parametrize("bad", [b"/" + b"a" * 1024, b"/sdcard/a\0b"])
def test_bad_paths_are_rejected_before_sending(server: FakeAdbServer, bad: bytes) -> None:
    with connect(server) as sync:
        with pytest.raises(RemoteError) as info:
            sync.stat(bad)
        assert info.value.path is not None
        with pytest.raises(RemoteError):
            sync.list(bad)  # rejected eagerly, before iteration
        assert server.commands == []
        assert sync.stat(b"/ok").error == 2  # the connection is still usable


def test_path_of_exactly_1024_bytes_is_accepted(server: FakeAdbServer) -> None:
    path = b"/" + b"a" * 1023
    with connect(server) as sync:
        assert sync.stat(path).error == 2
    assert server.commands == [b"LST2"]


def test_malformed_host_length_raises_transport_error(server: FakeAdbServer) -> None:
    server.features_length_override = b"zz!!"
    with pytest.raises(TransportError) as info:
        device_features("127.0.0.1", server.port, server.serial, timeout_s=TIMEOUT_S)
    assert not isinstance(info.value, DeviceDisconnected | DeviceTimeout | RemoteError)


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_given_features_skip_the_features_request(server: FakeAdbServer) -> None:
    server.features = ()  # a features request would now make Sync2 refuse the phone
    server.stats[b"/f"] = FakeStat(size=3)
    sync = Sync2(
        "127.0.0.1", server.port, server.serial, timeout_s=TIMEOUT_S, features={"stat_v2", "ls_v2"}
    )
    with sync:
        assert sync.stat(b"/f").size == 3


@pytest.mark.parametrize("features", [set(), {"stat_v2"}, {"ls_v2", "shell_v2"}])
def test_given_features_are_still_checked(features: set[str]) -> None:
    # Nothing listens on this port: the refusal must come before any connection is made.
    with pytest.raises(RemoteError, match="too old for Sift"):
        Sync2("127.0.0.1", _dead_port(), "FAKESERIAL", timeout_s=TIMEOUT_S, features=features)
