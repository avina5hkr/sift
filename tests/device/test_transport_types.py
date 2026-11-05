import dataclasses

import pytest

from sift.device.transport import (
    DeviceDisconnected,
    DeviceInfo,
    DeviceTimeout,
    LinkHealth,
    RemoteEntry,
    RemoteError,
    TransportError,
)


def make_entry() -> RemoteEntry:
    return RemoteEntry(
        path="/sdcard/DCIM/a.jpg", size=2**40, mtime=1_700_000_000, is_dir=False, is_symlink=False
    )


def make_info() -> DeviceInfo:
    return DeviceInfo(
        serial="SERIAL0001",
        model="Model X",
        android_version="14",
        battery_pct=80,
        free_bytes=10,
        total_bytes=100,
        screen_sizes=((1080, 2400), (720, 1600)),
    )


def test_remote_entry_is_frozen_and_hashable() -> None:
    entry = make_entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.size = 1  # type: ignore[misc]
    assert hash(entry) == hash(make_entry())
    assert entry == make_entry()
    assert entry.size == 2**40


def test_device_info_is_frozen_and_hashable() -> None:
    info = make_info()
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.model = "other"  # type: ignore[misc]
    assert hash(info) == hash(make_info())
    assert info.screen_sizes == ((1080, 2400), (720, 1600))


def test_device_info_allows_unknown_values() -> None:
    info = DeviceInfo("s", "m", "11", None, None, None, ())
    assert info.battery_pct is None
    assert info.screen_sizes == ()


def test_link_health_is_frozen_and_hashable() -> None:
    health = LinkHealth(kind="usb", speed_mbps=480, latency_ms=1.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        health.kind = "wifi"  # type: ignore[misc]
    assert hash(health) == hash(LinkHealth("usb", 480, 1.5))


def test_error_hierarchy() -> None:
    assert issubclass(TransportError, Exception)
    for sub in (DeviceDisconnected, DeviceTimeout, RemoteError):
        assert issubclass(sub, TransportError)


def test_remote_error_keeps_message() -> None:
    err = RemoteError("open failed: No such file or directory")
    assert "No such file or directory" in str(err)
    with pytest.raises(TransportError, match="No such file"):
        raise err
