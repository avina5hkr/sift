"""Fault injection on FakeTransport. No test sleeps for real: time comes from a ManualClock."""

import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport, FaultPlan, JournalEntry, ManualClock
from sift.device.transport import DeviceDisconnected, DeviceTimeout, FileChanged, RemoteError

PATH = "/sdcard/DCIM/clip.bin"
OTHER = "/sdcard/DCIM/other.bin"
DATA = bytes(range(256)) * 40  # 10 240 bytes


def make(faults: FaultPlan | None = None) -> tuple[FakeTransport, FakePhone, ManualClock]:
    phone = FakePhone()
    phone.add_file(PATH, DATA, mtime=1_700_000_000)
    phone.add_file(OTHER, b"other", mtime=1_700_000_001)
    clock = ManualClock()
    transport = FakeTransport(phone, faults, clock=clock, stall_timeout_s=30.0, sleep=clock.sleep)
    return transport, phone, clock


def test_disconnect_after_n_bytes_raises_device_disconnected() -> None:
    transport, _, _ = make(FaultPlan(disconnect_after_bytes=1000))
    with transport.open_read(PATH) as stream, pytest.raises(DeviceDisconnected):
        stream.read()


def test_disconnect_after_n_bytes_gives_a_short_read_then_fails_then_recovers() -> None:
    transport, _, _ = make(FaultPlan(disconnect_after_bytes=1000))
    stream = transport.open_read(PATH)
    assert stream.read(600) == DATA[:600]
    assert stream.read(600) == DATA[600:1000]
    with pytest.raises(DeviceDisconnected):
        stream.read(600)
    with pytest.raises(DeviceDisconnected):
        stream.read(1)
    stream.close()
    # The fault is one-shot: a fresh open after the "reconnect" works.
    with transport.open_read(PATH, offset=1000) as again:
        assert again.read() == DATA[1000:]


def test_disconnect_does_not_fire_when_the_file_ends_exactly_at_the_limit() -> None:
    transport, _, _ = make(FaultPlan(disconnect_after_bytes=len(DATA)))
    with transport.open_read(PATH) as stream:
        assert stream.read() == DATA


def test_hang_raises_device_timeout_after_the_stall_timeout_on_the_injected_clock() -> None:
    transport, _, clock = make(FaultPlan(hang_on={PATH}))
    started = clock()
    real_start = time.monotonic()
    with transport.open_read(PATH) as stream, pytest.raises(DeviceTimeout):
        stream.read(10)
    assert clock() - started == pytest.approx(30.0)
    assert time.monotonic() - real_start < 2.0
    # Other files are unaffected.
    with transport.open_read(OTHER) as stream:
        assert stream.read() == b"other"


def test_hang_also_stalls_phone_side_hashing() -> None:
    transport, _, clock = make(FaultPlan(hang_on={PATH}))
    started = clock()
    with pytest.raises(DeviceTimeout):
        transport.sha256([OTHER, PATH])
    assert clock() - started == pytest.approx(30.0)


def test_slow_reads_take_clock_time() -> None:
    transport, _, clock = make(FaultPlan(slow_read_bps=1000))
    started = clock()
    with transport.open_read(PATH) as stream:
        assert stream.read(2000) == DATA[:2000]
    assert clock() - started == pytest.approx(2.0)


def test_size_change_mid_read_raises_file_changed() -> None:
    transport, _, _ = make(FaultPlan(size_change_mid_read={PATH}))
    with transport.open_read(PATH) as stream:
        assert stream.read(100) == DATA[:100]
        for _ in range(2):  # and on every later read
            with pytest.raises(FileChanged) as info:
                stream.read()
            assert info.value.path == PATH
            assert str(info.value) == f"{PATH} changed while it was being read."
    # Untouched files still read in full.
    with transport.open_read(OTHER) as stream:
        assert stream.read() == b"other"


def test_a_file_that_shrinks_mid_read_raises_file_changed_after_what_is_left() -> None:
    transport, phone, _ = make()
    with transport.open_read(PATH) as stream:
        assert stream.read(100) == DATA[:100]
        phone.add_file(PATH, DATA[:500], mtime=1_700_000_002)
        assert stream.read(1000) == DATA[100:500]
        with pytest.raises(FileChanged) as info:
            stream.read(1000)
    assert info.value.path == PATH


def test_a_file_removed_mid_read_raises_file_changed() -> None:
    transport, phone, _ = make()
    with transport.open_read(PATH) as stream:
        assert stream.read(100) == DATA[:100]
        phone.remove(PATH)
        with pytest.raises(FileChanged) as info:
            stream.read(100)
    assert info.value.path == PATH


def test_wrong_sha256_is_returned_as_configured() -> None:
    transport, _, _ = make(FaultPlan(wrong_sha256={PATH}))
    result = transport.sha256([PATH, OTHER])
    assert result[PATH] != hashlib.sha256(DATA).hexdigest()
    assert result[PATH] is not None
    assert len(result[PATH]) == 64
    assert result[OTHER] == hashlib.sha256(b"other").hexdigest()


def test_refused_delete_raises_remote_error_and_keeps_the_file() -> None:
    transport, _, _ = make(FaultPlan(refuse_delete={PATH}))
    with pytest.raises(RemoteError) as info:
        transport.delete(PATH)
    assert info.value.path == PATH
    assert transport.stat(PATH) is not None
    transport.delete(OTHER)
    assert transport.stat(OTHER) is None


CALLS: dict[str, Callable[[FakeTransport], object]] = {
    "info": lambda t: t.info(),
    "stat": lambda t: t.stat(PATH),
    "list_dir": lambda t: t.list_dir("/sdcard/DCIM"),
    "walk": lambda t: list(t.walk("/sdcard/DCIM")),
    "open_read": lambda t: t.open_read(PATH),
    "sha256": lambda t: t.sha256([PATH]),
    "delete": lambda t: t.delete(OTHER),
    "media_rescan": lambda t: t.media_rescan([PATH]),
    "link_health": lambda t: t.link_health(),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_offline_for_t_seconds_then_recovery(name: str) -> None:
    transport, _, clock = make(FaultPlan(offline_for_s=60.0))
    call = CALLS[name]
    with pytest.raises(DeviceDisconnected):
        call(transport)
    clock.advance(59.0)
    with pytest.raises(DeviceDisconnected):
        call(transport)
    clock.advance(1.0)
    call(transport)  # back online


def test_offline_mid_read_breaks_the_stream() -> None:
    transport, _, clock = make()
    stream = transport.open_read(PATH)
    assert stream.read(10) == DATA[:10]
    transport.go_offline(5.0)
    with pytest.raises(DeviceDisconnected):
        stream.read(10)
    clock.advance(5.0)
    assert stream.read(10) == DATA[10:20]


@pytest.mark.parametrize("name", ["info", "sha256", "delete", "media_rescan", "link_health"])
def test_garbage_output_raises_remote_error_for_shell_like_calls(name: str) -> None:
    transport, _, _ = make(FaultPlan(garbage_output=True))
    with pytest.raises(RemoteError, match="unparseable"):
        CALLS[name](transport)


def test_garbage_output_leaves_sync_protocol_calls_alone() -> None:
    transport, _, _ = make(FaultPlan(garbage_output=True))
    assert transport.stat(PATH) is not None
    assert len(transport.list_dir("/sdcard/DCIM")) == 2
    with transport.open_read(OTHER) as stream:
        assert stream.read() == b"other"


def test_identity_override_changes_info_serial() -> None:
    transport, phone, _ = make(FaultPlan(identity="SOMEONE-ELSE"))
    assert transport.info().serial == "SOMEONE-ELSE"
    assert phone.serial != "SOMEONE-ELSE"
    plain, phone2, _ = make()
    assert plain.info().serial == phone2.serial


def test_info_without_a_serial_reports_the_android_id_alone() -> None:
    phone = FakePhone(serial="", android_id="0000fake000000aa")
    info = FakeTransport(phone).info()
    assert (info.serial, info.android_id) == (None, "0000fake000000aa")


def test_identity_override_changes_both_identifiers() -> None:
    transport, _, _ = make(FaultPlan(identity="SOMEONE-ELSE"))
    plain, _, _ = make()
    assert transport.info().android_id != plain.info().android_id


def test_a_folder_can_list_as_empty_without_an_error() -> None:
    transport, _, _ = make(FaultPlan(lists_empty={"/sdcard/DCIM"}))
    assert transport.list_dir("/sdcard/DCIM") == []
    errors: list[RemoteError] = []
    assert list(transport.walk("/sdcard", on_error=errors.append)) == []
    assert errors == []


def test_info_reports_the_phones_clock() -> None:
    phone = FakePhone(time_offset_s=-90)
    info = FakeTransport(phone, wall_clock=lambda: 1_700_000_000.4).info()
    assert info.phone_time == 1_700_000_000 - 90


def test_info_without_serial_or_android_id_raises() -> None:
    phone = FakePhone(serial="", android_id="")
    with pytest.raises(RemoteError, match="serial number"):
        FakeTransport(phone).info()


def test_journal_marks_reads_as_not_mutating_and_delete_push_as_mutating(tmp_path: Path) -> None:
    transport, _, _ = make()
    transport.info()
    transport.stat(PATH)
    transport.list_dir("/sdcard/DCIM")
    list(transport.walk("/sdcard"))
    with transport.open_read(PATH) as stream:
        stream.read()
    transport.sha256([PATH])
    transport.link_health()
    assert transport.journal
    assert not any(e.mutating for e in transport.journal)
    assert {e.op for e in transport.journal} == {
        "info",
        "stat",
        "list_dir",
        "walk",
        "open_read",
        "sha256",
        "link_health",
    }
    assert JournalEntry("stat", PATH, False) in transport.journal

    transport.journal.clear()
    local = tmp_path / "pushed.bin"
    local.write_bytes(b"pushed")
    transport.push(local, "/sdcard/Backup/pushed.bin", 1_234_567)
    transport.delete(OTHER)
    transport.media_rescan(["/sdcard/Backup/pushed.bin"])
    assert transport.journal == [
        JournalEntry("push", "/sdcard/Backup/pushed.bin", True),
        JournalEntry("delete", OTHER, True),
        JournalEntry("media_rescan", "/sdcard/Backup/pushed.bin", True),
    ]
    pushed = transport.stat("/sdcard/Backup/pushed.bin")
    assert pushed is not None
    assert (pushed.size, pushed.mtime) == (6, 1_234_567)
    assert transport.stat(OTHER) is None


def test_failed_calls_are_journalled_too() -> None:
    transport, _, _ = make(FaultPlan(refuse_delete={PATH}))
    with pytest.raises(RemoteError):
        transport.delete(PATH)
    assert transport.journal == [JournalEntry("delete", PATH, True)]


def test_short_reads_return_less_than_asked_but_everything_in_the_end() -> None:
    transport, _, _ = make(FaultPlan(short_reads=True))
    with transport.open_read(PATH) as stream:
        assert len(stream.read(9)) == 3
        assert len(stream.read(1)) == 1
        while chunk := stream.read(100):
            assert len(chunk) < 100
    with transport.open_read(PATH) as stream:
        assert stream.read() == DATA
    with transport.open_read(PATH, 4) as stream:
        whole = b""
        while chunk := stream.read(64):
            whole += chunk
    assert whole == DATA[4:]
