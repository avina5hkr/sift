"""A DeviceTracker over the fake phone, for `make dev` and the browser tests (`--fake-phone`).

The device list is scripted: no phone at first, then the fake phone's serial as "device" after
`FAKE_ANNOUNCE_AFTER_S`. The tracker, the status route and the UI behave exactly as with a real
phone, but nothing here ever contacts adb or a USB device.
"""

import threading
import time
from collections.abc import Callable, Iterator

from sift.core.context import AppContext
from sift.device.fake_phone import FakePhone, build_fake_phone
from sift.device.fake_transport import FakeTransport, FaultPlan
from sift.device.track_devices import DeviceEventSource, Snapshot
from sift.device.tracker import DeviceTracker

FAKE_ANNOUNCE_AFTER_S = 0.5
_TICK_S = 1.0
_POLL_S = 0.1  # how soon an unplug or a replug is noticed
_NEVER_BACK_S = 10.0**9
_FAKE_LINK_MBPS = 5000  # a USB 3 link: no "USB 2" tag


class FakePhoneControl:
    """The fake phone, its transport and its fault plan, with the cable under test control.

    Only the browser tests drive it, through `sift.server.routes.testphone` (a test hook that
    is off unless SIFT_TEST_PHONE_CONTROL=1, and never reachable in a packaged app).
    """

    def __init__(self, faults: FaultPlan | None = None) -> None:
        self.phone: FakePhone = build_fake_phone()
        assert self.phone.serial is not None
        self.serial: str = self.phone.serial
        self.faults = faults if faults is not None else FaultPlan()
        self.transport = FakeTransport(self.phone, self.faults)
        self._plugged = True

    @property
    def plugged(self) -> bool:
        return self._plugged

    def unplug(self) -> None:
        """The phone drops out of the device list, and a read in flight fails at once."""
        self.transport.go_offline(_NEVER_BACK_S)
        self._plugged = False

    def replug(self) -> None:
        self.transport.come_online()
        self._plugged = True

    def set_speed(self, bytes_per_s: int | None) -> None:
        """Serve reads at this speed (None: as fast as possible)."""
        self.faults.slow_read_bps = bytes_per_s

    def remove(self, path: str, *, keep_folder: bool = False) -> int:
        """Delete a file, or a folder with everything in it, from the phone.

        With `keep_folder` the folder itself stays, empty (as when its files are deleted on a
        real phone). Returns how many files went.
        """
        before = len(self.phone.files)
        if keep_folder:
            if not self.phone.is_dir(path):
                raise KeyError(path)
            for entry in self.phone.children(path):
                self.phone.remove(entry.path)
        else:
            self.phone.remove(path)
        return before - len(self.phone.files)


class FakeDeviceEvents:
    """No devices, then `serial` as "device" after `announce_after_s`, then idle ticks.

    `plugged` says whether the phone is on the cable now: an unplug shows as an empty list.
    """

    def __init__(
        self,
        serial: str,
        *,
        announce_after_s: float = FAKE_ANNOUNCE_AFTER_S,
        plugged: Callable[[], bool] = lambda: True,
    ) -> None:
        self._serial = serial
        self._announce_after_s = announce_after_s
        self._plugged = plugged
        self._closed = threading.Event()

    def _snapshot(self) -> Snapshot:
        return {self._serial: "device"} if self._plugged() else {}

    def __iter__(self) -> Iterator[Snapshot | None]:
        yield {}
        if self._closed.wait(self._announce_after_s):
            return
        last: Snapshot = {}
        last_item = time.monotonic()
        while True:
            now = self._snapshot()
            if now != last:
                last = now
                last_item = time.monotonic()
                yield now
            elif time.monotonic() - last_item >= _TICK_S:
                last_item = time.monotonic()
                yield None
            if self._closed.wait(_POLL_S):
                return

    def close(self) -> None:
        self._closed.set()


def fake_tracker(
    ctx: AppContext,
    *,
    announce_after_s: float = FAKE_ANNOUNCE_AFTER_S,
    faults: FaultPlan | None = None,
    control: FakePhoneControl | None = None,
) -> DeviceTracker:
    """A tracker wired to `FakeTransport(build_fake_phone())` and the scripted device list.

    The first device list announces the phone after `announce_after_s`; a list reopened later
    (Retry) announces it at once, so the chip does not flicker. `faults` is the transport's
    fault plan (the quit-path tests slow its reads down). With a `control` (browser tests) the
    phone, transport and faults are the control's, so a test can unplug the phone and change
    what is on it.
    """
    if control is None:
        control = FakePhoneControl(faults)
    transport = control.transport
    delay = [announce_after_s]

    def events() -> DeviceEventSource:
        wait, delay[0] = delay[0], 0.0
        return FakeDeviceEvents(
            control.serial, announce_after_s=wait, plugged=lambda: control.plugged
        )

    return DeviceTracker(
        ctx,
        events=events,
        connect=lambda _serial: transport,
        usb_present=lambda: False,
        link_speed=lambda _serial: _FAKE_LINK_MBPS,
    )
