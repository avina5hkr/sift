"""Device status and Retry over the API, with the fake phone behind a real DeviceTracker."""

import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from sift.core.context import AppContext
from sift.device.fake_tracker import FAKE_ANNOUNCE_AFTER_S, FakeDeviceEvents, fake_tracker
from sift.device.tracker import ConnectionState, DeviceStatus, DeviceTracker
from tests.helpers import H, client_for


def _wait_for(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture
def fake_ctx(ctx: AppContext) -> Iterator[AppContext]:
    """A context whose tracker runs the fake phone, announced at once (no 0.5 s wait)."""
    tracker = fake_tracker(ctx, announce_after_s=0.0)
    ctx.tracker = tracker
    tracker.start()
    yield ctx
    tracker.stop()


def test_status_device_is_null_without_a_tracker(client: TestClient) -> None:
    assert client.get("/api/status", headers=H).json()["device"] is None


def test_status_shows_the_fake_phone_once_announced(fake_ctx: AppContext) -> None:
    client = client_for(fake_ctx)
    assert fake_ctx.tracker is not None
    tracker = fake_ctx.tracker
    _wait_for(lambda: tracker.status().state is ConnectionState.connected_usb)
    device = client.get("/api/status", headers=H).json()["device"]
    assert device["state"] == "connected_usb"
    assert device["model"] == "TestPhone"
    assert device["android_version"] == "14"
    assert device["battery_pct"] == 80
    assert device["usb2_hint"] is False
    assert isinstance(device["free_bytes"], int)
    assert set(device) == {
        "state",
        "message",
        "serial",
        "model",
        "android_version",
        "battery_pct",
        "free_bytes",
        "total_bytes",
        "link_mbps",
        "usb2_hint",
        "device_uuid",
        "reason",
    }


def test_status_device_before_the_phone_is_announced(ctx: AppContext) -> None:
    class Idle:
        def status(self) -> DeviceStatus:
            return DeviceStatus(ConnectionState.no_device, "No phone found.")

    ctx.tracker = Idle()  # type: ignore[assignment]
    device = client_for(ctx).get("/api/status", headers=H).json()["device"]
    assert device["state"] == "no_device"
    assert device["message"] == "No phone found."
    assert device["model"] is None
    assert device["reason"] is None


def test_status_device_passes_the_no_adb_reason_through(ctx: AppContext) -> None:
    class Unavailable:
        def status(self) -> DeviceStatus:
            return DeviceStatus(
                ConnectionState.no_adb, "adb is not answering.", reason="adb_unavailable"
            )

    ctx.tracker = Unavailable()  # type: ignore[assignment]
    device = client_for(ctx).get("/api/status", headers=H).json()["device"]
    assert (device["state"], device["reason"]) == ("no_adb", "adb_unavailable")


def test_the_fake_source_is_empty_then_announces_the_phone() -> None:
    source = FakeDeviceEvents("FAKE-0001", announce_after_s=0.05)
    items = iter(source)
    assert next(items) == {}
    started = time.monotonic()
    assert next(items) == {"FAKE-0001": "device"}
    assert time.monotonic() - started >= 0.04
    source.close()
    assert list(items) == []
    assert FAKE_ANNOUNCE_AFTER_S == 0.5


def test_the_fake_source_ends_when_closed_while_waiting() -> None:
    source = FakeDeviceEvents("FAKE-0001", announce_after_s=30.0)
    items = iter(source)
    assert next(items) == {}
    source.close()
    assert list(items) == []


def test_devices_lists_the_stored_phone(fake_ctx: AppContext) -> None:
    client = client_for(fake_ctx)
    assert fake_ctx.tracker is not None
    tracker = fake_ctx.tracker
    _wait_for(lambda: tracker.status().state is ConnectionState.connected_usb)
    rows = client.get("/api/devices", headers=H).json()
    assert len(rows) == 1
    assert set(rows[0]) == {"uuid", "serial", "model", "android_version", "last_seen_at"}
    assert rows[0]["serial"] == "FAKE-0001"
    assert rows[0]["model"] == "TestPhone"
    assert isinstance(rows[0]["last_seen_at"], int)
    assert rows[0]["uuid"] == tracker.status().device_uuid


def test_devices_is_empty_before_any_phone_is_stored(client: TestClient) -> None:
    assert client.get("/api/devices", headers=H).json() == []


def test_devices_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/devices").status_code == 401
    assert client.get("/api/devices?token=t0k").status_code == 401


def test_retry_calls_the_tracker(ctx: AppContext, client: TestClient) -> None:
    calls: list[str] = []

    class Spy:
        def retry(self) -> None:
            calls.append("retry")

    ctx.tracker = Spy()  # type: ignore[assignment]
    r = client.post("/api/devices/retry", headers=H)
    assert r.status_code == 204
    assert r.content == b""
    assert calls == ["retry"]


def test_retry_needs_the_token(ctx: AppContext, client: TestClient) -> None:
    calls: list[str] = []

    class Spy:
        def retry(self) -> None:
            calls.append("retry")

    ctx.tracker = Spy()  # type: ignore[assignment]
    assert client.post("/api/devices/retry").status_code == 401
    assert client.post("/api/devices/retry?token=t0k").status_code == 401
    assert calls == []


def test_retry_without_a_tracker_is_a_plain_409(client: TestClient) -> None:
    r = client.post("/api/devices/retry", headers=H)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "no_tracker"
    assert body["message"] == "Sift is not looking for a phone right now."


def test_the_tracker_type_is_a_device_tracker(fake_ctx: AppContext) -> None:
    assert isinstance(fake_ctx.tracker, DeviceTracker)
