"""The browser tests' control of the fake phone: refused unless the test hook is on."""

import sys
import time
from collections.abc import Callable

import pytest

from sift.core.context import AppContext
from sift.device.fake_tracker import FakeDeviceEvents, FakePhoneControl, fake_tracker
from sift.device.tracker import ConnectionState
from tests.helpers import H, client_for

UNPLUG = "/api/test/phone/unplug"


def _wait_for(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture
def hooked(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    monkeypatch.setenv("SIFT_TEST_PHONE_CONTROL", "1")
    ctx.phone_control = FakePhoneControl()
    return ctx


def test_refused_when_the_hook_is_off(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIFT_TEST_PHONE_CONTROL", raising=False)
    control = FakePhoneControl()
    ctx.phone_control = control
    r = client_for(ctx).post(UNPLUG, headers=H)
    assert r.status_code == 404
    assert control.plugged


def test_refused_in_a_packaged_app_even_with_the_variable(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIFT_TEST_PHONE_CONTROL", "1")
    monkeypatch.delenv("SIFT_ENABLE_TEST_HOOKS", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    control = FakePhoneControl()
    ctx.phone_control = control
    assert client_for(ctx).post(UNPLUG, headers=H).status_code == 404
    assert control.plugged


def test_needs_the_token(hooked: AppContext) -> None:
    assert client_for(hooked).post(UNPLUG).status_code == 401
    assert hooked.phone_control is not None and hooked.phone_control.plugged


def test_refused_without_a_fake_phone(hooked: AppContext) -> None:
    hooked.phone_control = None
    assert client_for(hooked).post(UNPLUG, headers=H).status_code == 409


def test_unplug_and_replug(hooked: AppContext) -> None:
    client = client_for(hooked)
    control = hooked.phone_control
    assert control is not None
    assert client.post(UNPLUG, headers=H).status_code == 204
    assert not control.plugged
    assert client.post("/api/test/phone/replug", headers=H).status_code == 204
    assert control.plugged


def test_speed_and_remove(hooked: AppContext) -> None:
    client = client_for(hooked)
    control = hooked.phone_control
    assert control is not None
    assert (
        client.post("/api/test/phone/speed", json={"bytes_per_s": 500}, headers=H).status_code
        == 204
    )
    assert control.faults.slow_read_bps == 500
    assert (
        client.post("/api/test/phone/speed", json={"bytes_per_s": None}, headers=H).status_code
        == 204
    )
    assert control.faults.slow_read_bps is None
    shots = "/sdcard/DCIM/Screenshots"
    r = client.post("/api/test/phone/remove", json={"path": shots, "keep_folder": True}, headers=H)
    assert r.status_code == 200 and r.json()["files"] > 0
    assert control.phone.is_dir(shots) and control.phone.children(shots) == []
    r = client.post("/api/test/phone/remove", json={"path": shots}, headers=H)
    assert r.status_code == 200 and r.json()["files"] == 0
    assert not control.phone.is_dir(shots)
    r = client.post("/api/test/phone/remove", json={"path": "/sdcard/DCIM/Screenshots"}, headers=H)
    assert r.status_code == 404


def test_the_device_list_follows_the_cable() -> None:
    plugged = [True]
    source = FakeDeviceEvents("FAKE-0001", announce_after_s=0.0, plugged=lambda: plugged[0])
    items = iter(source)
    assert next(items) == {}
    assert next(items) == {"FAKE-0001": "device"}
    plugged[0] = False
    assert next(items) == {}
    plugged[0] = True
    assert next(items) == {"FAKE-0001": "device"}
    source.close()


def test_the_tracker_loses_and_regains_the_phone(ctx: AppContext) -> None:
    control = FakePhoneControl()
    tracker = fake_tracker(ctx, announce_after_s=0.0, control=control)
    ctx.tracker = tracker
    tracker.start()
    try:
        _wait_for(lambda: tracker.status().state is ConnectionState.connected_usb)
        control.unplug()
        _wait_for(lambda: tracker.status().state is ConnectionState.no_device)
        control.replug()
        _wait_for(lambda: tracker.status().state is ConnectionState.connected_usb)
    finally:
        tracker.stop()
