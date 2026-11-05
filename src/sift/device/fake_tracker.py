"""A DeviceTracker over the fake phone, for `make dev` and the browser tests (`--fake-phone`).

The device list is scripted: no phone at first, then the fake phone's serial as "device" after
`FAKE_ANNOUNCE_AFTER_S`. The tracker, the status route and the UI behave exactly as with a real
phone, but nothing here ever contacts adb or a USB device.
"""

import threading
from collections.abc import Iterator

from sift.core.context import AppContext
from sift.device.fake_phone import build_fake_phone
from sift.device.fake_transport import FakeTransport
from sift.device.track_devices import DeviceEventSource, Snapshot
from sift.device.tracker import DeviceTracker

FAKE_ANNOUNCE_AFTER_S = 0.5
_TICK_S = 1.0
_FAKE_LINK_MBPS = 5000  # a USB 3 link: no "USB 2" tag


class FakeDeviceEvents:
    """No devices, then `serial` as "device" after `announce_after_s`, then idle ticks."""

    def __init__(self, serial: str, *, announce_after_s: float = FAKE_ANNOUNCE_AFTER_S) -> None:
        self._serial = serial
        self._announce_after_s = announce_after_s
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[Snapshot | None]:
        yield {}
        if self._closed.wait(self._announce_after_s):
            return
        yield {self._serial: "device"}
        while not self._closed.wait(_TICK_S):
            yield None

    def close(self) -> None:
        self._closed.set()


def fake_tracker(
    ctx: AppContext, *, announce_after_s: float = FAKE_ANNOUNCE_AFTER_S
) -> DeviceTracker:
    """A tracker wired to `FakeTransport(build_fake_phone())` and the scripted device list.

    The first device list announces the phone after `announce_after_s`; a list reopened later
    (Retry) announces it at once, so the chip does not flicker.
    """
    phone = build_fake_phone()
    assert phone.serial is not None
    serial = phone.serial
    transport = FakeTransport(phone)
    delay = [announce_after_s]

    def events() -> DeviceEventSource:
        wait, delay[0] = delay[0], 0.0
        return FakeDeviceEvents(serial, announce_after_s=wait)

    return DeviceTracker(
        ctx,
        events=events,
        connect=lambda _serial: transport,
        usb_present=lambda: False,
        link_speed=lambda _serial: _FAKE_LINK_MBPS,
    )
