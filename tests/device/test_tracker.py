"""DeviceTracker: adb device states -> ConnectionState, identity in the database, backoff.

Most tests run the tracker's loop synchronously (`tracker.run()`) over a scripted event source
with a manual clock whose `sleep` only advances time, so nothing waits in real time. A few
tests start the real thread to check start/stop/retry from another thread; they use tiny real
waits only.
"""

import itertools
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from sift.core.config import PhoneConfig
from sift.core.context import AppContext
from sift.core.events import EventBus
from sift.device.adb_manager import AdbError, AdbManager
from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport, ManualClock
from sift.device.track_devices import Snapshot
from sift.device.tracker import (
    HELD_BY_OTHER_MESSAGE,
    MULTIPLE_MESSAGE,
    NO_DEVICE_MESSAGE,
    NOT_ANSWERING_MESSAGE,
    OFFLINE_MESSAGE,
    UNAUTHORIZED_MESSAGE,
    ConnectionState,
    DeviceStatus,
    DeviceTracker,
    adb_tracker,
)
from sift.device.transport import (
    NO_IDENTITY_MESSAGE,
    DeviceDisconnected,
    DeviceTimeout,
    Transport,
)
from tests.device.fake_adb_server import FakeAdbServer

S = ConnectionState
USB = "FAKE-0001"
WIFI = "192.0.2.10:5555"


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a subprocess was started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, name: str, data: dict[str, Any]) -> None:
        self.published.append((name, data))
        super().publish(name, data)

    def states(self) -> list[str]:
        return [data["state"] for name, data in self.published if name == "device"]


class Stop:
    """A script item: stop the tracker (the source then ends)."""


class Retry:
    """A script item: press Retry."""


STOP = Stop()
RETRY = Retry()
Item = Snapshot | None | Callable[[], object] | Stop | Retry | Exception


class ScriptedSource:
    def __init__(self, tracker: DeviceTracker, items: list[Item]) -> None:
        self._tracker = tracker
        self._items = items
        self.closed = False

    def __iter__(self) -> Iterator[Snapshot | None]:
        for item in self._items:
            if self.closed:
                return
            if isinstance(item, Stop):
                self._tracker.stop()
            elif isinstance(item, Retry):
                self._tracker.retry()
            elif isinstance(item, Exception):
                raise item
            elif callable(item):
                item()
            else:
                yield item

    def close(self) -> None:
        self.closed = True


@dataclass
class Events:
    """The `events` factory: each call opens the next scripted source (or raises)."""

    clock: ManualClock
    scripts: list[list[Item] | Exception]
    tracker: DeviceTracker | None = None
    opened_at: list[float] = field(default_factory=list)
    sources: list[ScriptedSource] = field(default_factory=list)

    def __call__(self) -> ScriptedSource:
        assert self.tracker is not None
        self.opened_at.append(self.clock())
        if not self.scripts:
            self.tracker.stop()
            return ScriptedSource(self.tracker, [])
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        source = ScriptedSource(self.tracker, script)
        self.sources.append(source)
        return source


@dataclass
class Rig:
    tracker: DeviceTracker
    events: Events
    clock: ManualClock
    bus: RecordingBus
    phones: dict[str, Transport | Exception]
    usb: list[bool]
    link_calls: list[str]

    def run(self, *scripts: list[Item] | Exception) -> DeviceTracker:
        self.events.scripts.extend(scripts)
        self.tracker.run()
        return self.tracker


def tick(clock: ManualClock, seconds: float) -> Callable[[], None]:
    return lambda: clock.advance(seconds)


def rig(
    ctx: AppContext,
    *,
    link: int | None = 480,
    usb: bool = False,
    on_sleep: Callable[[ManualClock], None] | None = None,
) -> Rig:
    clock = ManualClock(1000.0)

    def sleep(seconds: float) -> None:
        clock.advance(seconds)
        if on_sleep is not None:
            on_sleep(clock)

    bus = RecordingBus()
    ctx.bus = bus
    phones: dict[str, Transport | Exception] = {
        USB: FakeTransport(FakePhone(serial="FAKE-0001")),
        WIFI: FakeTransport(FakePhone(serial="FAKE-0001", android_version="15")),
    }
    usb_state = [usb]
    link_calls: list[str] = []

    def connect(serial: str) -> Transport:
        found = phones[serial]
        if isinstance(found, Exception):
            raise found
        return found

    def link_speed(serial: str) -> int | None:
        link_calls.append(serial)
        return link

    events = Events(clock, [])
    tracker = DeviceTracker(
        ctx,
        events=events,
        connect=connect,
        usb_present=lambda: usb_state[0],
        link_speed=link_speed,
        clock=clock,
        sleep=sleep,
        wall_clock=lambda: 1_700_000_000.0 + clock(),
    )
    events.tracker = tracker
    return Rig(tracker, events, clock, bus, phones, usb_state, link_calls)


def device_rows(ctx: AppContext) -> list[tuple[Any, ...]]:
    with ctx.db.read() as conn:
        return conn.execute(
            "SELECT id, uuid, serial, model, android_version, last_seen_at FROM device ORDER BY id"
        ).fetchall()


# -- states ------------------------------------------------------------------------------------


def test_states_follow_events(ctx: AppContext) -> None:
    r = rig(ctx)
    seen: list[DeviceStatus] = []

    def note() -> None:
        seen.append(r.tracker.status())

    r.run(
        [
            {},
            note,
            {USB: "unauthorized"},
            note,
            {USB: "offline"},
            note,
            {USB: "device"},
            note,
            STOP,
        ]
    )
    assert [s.state for s in seen] == [S.no_device, S.unauthorized, S.offline, S.connected_usb]
    assert seen[1].message == UNAUTHORIZED_MESSAGE
    assert "Auto Blocker" in UNAUTHORIZED_MESSAGE
    assert seen[1].serial == USB
    assert seen[2].message == OFFLINE_MESSAGE
    connected = seen[3]
    phone = FakePhone()
    assert connected.serial == USB
    assert connected.model == "TestPhone"
    assert connected.android_version == "14"
    assert connected.battery_pct == phone.battery_pct
    assert connected.total_bytes == phone.total_bytes
    assert connected.free_bytes is not None and connected.free_bytes <= phone.total_bytes
    assert connected.link_mbps == 480
    assert connected.device_uuid is not None
    assert "USB" in connected.message


def test_unknown_adb_states_are_never_connected(ctx: AppContext) -> None:
    r = rig(ctx)
    seen: list[ConnectionState] = []
    note = lambda: seen.append(r.tracker.status().state)  # noqa: E731
    r.run([{USB: "authorizing"}, note, {USB: "recovery"}, note, {USB: "connecting"}, note, STOP])
    assert seen == [S.unauthorized, S.offline, S.offline]


def test_wifi_serial_is_connected_wifi_without_a_usb_speed(ctx: AppContext) -> None:
    r = rig(ctx)
    r.run([{WIFI: "device"}, STOP])
    status = r.tracker.status()
    assert status.state is S.connected_wifi
    assert status.link_mbps is None
    assert status.usb2_hint is False
    assert r.link_calls == []


@pytest.mark.parametrize("serial", ["adb-FAKE0001-AbCdEf._adb-tls-connect._tcp"])
def test_mdns_serial_is_wifi(ctx: AppContext, serial: str) -> None:
    r = rig(ctx)
    r.phones[serial] = FakeTransport(FakePhone())
    r.run([{serial: "device"}, STOP])
    assert r.tracker.status().state is S.connected_wifi


def test_a_ready_phone_wins_over_one_waiting_for_allow(ctx: AppContext) -> None:
    r = rig(ctx)
    r.run([{"OTHER-0002": "unauthorized", USB: "device"}, STOP])
    assert r.tracker.status().state is S.connected_usb
    assert r.tracker.status().serial == USB


def test_no_adb_uses_the_managers_message_and_retries(ctx: AppContext) -> None:
    r = rig(ctx)
    message = "adb was not found. Sift can download Google's platform-tools for you."
    seen: list[DeviceStatus] = []
    r.run(AdbError(message), AdbError(message), [lambda: seen.append(r.tracker.status())])
    assert seen[0].state is S.no_adb
    assert seen[0].message == message
    assert r.bus.states()[0] == "no_adb"
    assert len(r.events.opened_at) >= 3


# -- identity ----------------------------------------------------------------------------------


def test_first_phone_is_stored_and_identity_reused(ctx: AppContext) -> None:
    r = rig(ctx)
    assert device_rows(ctx) == []
    r.run([{USB: "device"}, STOP])
    first = r.tracker.status()
    rows = device_rows(ctx)
    assert len(rows) == 1
    _, uuid, serial, model, version, seen_at = rows[0]
    assert (serial, model, version) == ("FAKE-0001", "TestPhone", "14")
    assert first.device_uuid == uuid
    assert seen_at == 1_700_000_000 + 1000

    # The same phone again, later, over Wi-Fi and updated to a newer Android: same identity.
    again = rig(ctx)
    again.clock.advance(60)
    again.run([{WIFI: "device"}, STOP])
    status = again.tracker.status()
    assert status.state is S.connected_wifi
    assert status.device_uuid == uuid
    rows = device_rows(ctx)
    assert len(rows) == 1
    assert rows[0][1:5] == (uuid, "FAKE-0001", "TestPhone", "15")
    assert rows[0][5] == 1_700_000_000 + 1060


def test_wrong_device_blocks(ctx: AppContext) -> None:
    r = rig(ctx)
    r.phones["OTHER-0002"] = FakeTransport(FakePhone(serial="OTHER-0002", model="OtherPhone"))
    r.run([{USB: "device"}, STOP])
    before = device_rows(ctx)

    other = rig(ctx)
    other.phones["OTHER-0002"] = FakeTransport(FakePhone(serial="OTHER-0002", model="OtherPhone"))
    other.clock.advance(60)
    other.run([{"OTHER-0002": "device"}, STOP])
    status = other.tracker.status()
    assert status.state is S.wrong_device
    assert status.device_uuid is None
    assert status.model == "OtherPhone"
    assert "different phone" in status.message
    assert device_rows(ctx) == before  # nothing stored or touched
    assert "connected_usb" not in other.bus.states()


def test_identity_fallback(ctx: AppContext) -> None:
    r = rig(ctx)
    r.phones[USB] = FakeTransport(FakePhone(serial="", android_id="0000fake000000aa"))
    r.run([{USB: "device"}, STOP])
    assert r.tracker.status().state is S.connected_usb
    assert [row[2] for row in device_rows(ctx)] == ["0000fake000000aa"]


def test_identity_fallback_both_missing_is_offline(ctx: AppContext) -> None:
    r = rig(ctx)
    r.phones[USB] = FakeTransport(FakePhone(serial="", android_id=""))
    r.run([{USB: "device"}, STOP])
    status = r.tracker.status()
    assert status.state is S.offline
    assert status.message == NO_IDENTITY_MESSAGE
    assert device_rows(ctx) == []
    assert not any(state.startswith("connected") for state in r.bus.states())


# -- transport errors while "device" ---------------------------------------------------------


def test_transport_error_is_offline_then_retried(ctx: AppContext) -> None:
    r = rig(ctx)
    good = r.phones[USB]
    r.phones[USB] = DeviceTimeout("The adb server did not answer in time.")
    seen: list[DeviceStatus] = []

    def heal() -> None:
        seen.append(r.tracker.status())
        r.phones[USB] = good

    r.run([{USB: "device"}, heal, tick(r.clock, 0.5), None, tick(r.clock, 0.6), None, STOP])
    assert seen[0].state is S.offline
    assert "Traceback" not in seen[0].message and "adb server" not in seen[0].message
    assert r.tracker.status().state is S.connected_usb


def test_transport_error_is_retried_on_the_next_event(ctx: AppContext) -> None:
    r = rig(ctx)
    good = r.phones[USB]
    r.phones[USB] = DeviceTimeout("The adb server did not answer in time.")
    seen: list[ConnectionState] = []

    def heal() -> None:
        seen.append(r.tracker.status().state)
        r.phones[USB] = good

    other = {USB: "device", "OTHER-0002": "offline"}
    r.run([{USB: "device"}, heal, None, lambda: seen.append(r.tracker.status().state), other])
    assert seen == [S.offline, S.offline]  # a tick before the backoff does not re-read
    assert r.bus.states()[-2] == "connected_usb"  # the event did, at once


def test_offline_messages() -> None:
    expected = (
        "The phone is connected but not responding. Unlock it, or unplug and reconnect the "
        "cable. On Samsung phones also check that Auto Blocker is off."
    )
    assert expected == OFFLINE_MESSAGE == NOT_ANSWERING_MESSAGE


def test_info_error_is_offline(ctx: AppContext) -> None:
    r = rig(ctx)
    transport = r.phones[USB]
    assert isinstance(transport, FakeTransport)
    transport.go_offline(5)
    r.run([{USB: "device"}, STOP])
    assert r.tracker.status().state is S.offline


# -- held by another tool ----------------------------------------------------------------------


def test_held_by_other_tool_state(ctx: AppContext) -> None:
    r = rig(ctx, usb=True)
    seen: list[ConnectionState] = []
    note = lambda: seen.append(r.tracker.status().state)  # noqa: E731
    step = tick(r.clock, 1.0)

    def unplug() -> None:
        r.usb[0] = False

    r.run(
        [
            {},
            note,  # t=0: USB shows a phone, adb has none
            step,
            None,
            note,  # t=1
            step,
            None,
            note,  # t=2
            step,
            None,
            note,  # t=3: held
            unplug,
            step,
            None,
            note,  # t=4: gone from USB too
            STOP,
        ]
    )
    assert seen == [S.no_device, S.no_device, S.no_device, S.held_by_other, S.no_device]
    held = [d for n, d in r.bus.published if d["state"] == "held_by_other"]
    assert held[0]["message"] == HELD_BY_OTHER_MESSAGE
    assert HELD_BY_OTHER_MESSAGE == (
        "Another tool may be using the phone (for example Android Studio). "
        "Close it, then press Retry."
    )


def test_no_usb_phone_never_becomes_held(ctx: AppContext) -> None:
    r = rig(ctx, usb=False)
    script: list[Item] = [{}]
    for _ in range(10):
        script += [tick(r.clock, 1.0), None]
    r.run([*script, STOP])
    assert r.tracker.status().state is S.no_device
    assert r.bus.states() == ["no_device"]


def test_held_clears_when_adb_sees_the_phone(ctx: AppContext) -> None:
    r = rig(ctx, usb=True)
    script: list[Item] = [{}]
    for _ in range(4):
        script += [tick(r.clock, 1.0), None]
    r.run([*script, {USB: "device"}, STOP])
    assert r.bus.states() == ["no_device", "held_by_other", "connected_usb"]


# -- backoff -----------------------------------------------------------------------------------


def _deltas(times: list[float]) -> list[float]:
    return [round(b - a, 6) for a, b in itertools.pairwise(times)]


def test_backoff_schedule(ctx: AppContext) -> None:
    r = rig(ctx)
    drop = DeviceDisconnected("adb server went away")
    r.run(*[drop] * 7, [STOP])
    assert _deltas(r.events.opened_at) == [1, 2, 4, 8, 16, 30, 30]


def test_a_stream_that_dies_quickly_keeps_growing_the_backoff(ctx: AppContext) -> None:
    r = rig(ctx)
    drop = DeviceDisconnected("adb server went away")
    r.run(
        [{USB: "device"}, drop],  # one snapshot, then dies
        drop,
        drop,
        [{USB: "device"}],  # one snapshot again, then ends (also a drop)
        [STOP],
    )
    assert _deltas(r.events.opened_at) == [1, 2, 4, 8]  # ensure_server is not hammered
    # Never left showing "connected" while adb is gone; connected again once it is back.
    assert r.bus.states() == ["connected_usb", "offline", "connected_usb", "offline"]


def test_a_stream_that_stayed_up_30_s_resets_the_backoff(ctx: AppContext) -> None:
    r = rig(ctx)
    drop = DeviceDisconnected("adb server went away")
    up_29s: list[Item] = [{USB: "device"}] + [tick(r.clock, 1.0), None] * 29
    r.run(
        drop,
        drop,
        [*up_29s, drop],  # up 29 s: not long enough
        [*up_29s, tick(r.clock, 1.0), None, drop],  # up 30 s
        [STOP],
    )
    deltas = _deltas(r.events.opened_at)
    assert deltas[:3] == [1, 2, 29 + 4]
    assert deltas[3] == 30 + 1


def test_retry_resets_backoff(ctx: AppContext) -> None:
    pressed: list[float] = []

    def press_retry_once(clock: ManualClock) -> None:
        if not pressed and clock() >= 1000 + 1 + 2 + 1:  # 1 s into the 4 s wait
            pressed.append(clock())
            r.tracker.retry()

    r = rig(ctx, on_sleep=press_retry_once)
    drop = DeviceDisconnected("adb server went away")
    r.run(drop, drop, drop, drop, drop, [STOP])
    deltas = _deltas(r.events.opened_at)
    assert deltas[:2] == [1, 2]
    assert r.events.opened_at[3] == pressed[0]  # reconnected now, not after the other 3 s
    assert deltas[3:] == [1, 2]  # and the schedule started over


def test_retry_pressed_while_the_list_opens_keeps_the_fresh_list(ctx: AppContext) -> None:
    r = rig(ctx)
    real_factory = r.tracker._events

    def press_retry_then_open() -> Any:
        r.tracker.retry()
        return real_factory()

    r.tracker._events = press_retry_then_open
    r.run([{USB: "device"}, STOP])
    assert len(r.events.opened_at) == 1  # the fresh list was used, not thrown away
    assert r.tracker.status().state is S.connected_usb


def test_retry_reconnects_the_stream_and_rereads_the_phone(ctx: AppContext) -> None:
    r = rig(ctx, usb=True)
    script: list[Item] = [{}]
    for _ in range(4):
        script += [tick(r.clock, 1.0), None]
    r.run([*script, RETRY, {"never": "device"}], [{}, STOP])
    assert len(r.events.opened_at) == 2
    assert r.events.opened_at[1] == r.events.opened_at[0] + 4  # no backoff wait
    assert r.events.sources[0].closed
    assert r.bus.states() == ["no_device", "held_by_other", "no_device"]  # timer restarted


# -- events ------------------------------------------------------------------------------------


def test_publishes_device_event_on_change_only(ctx: AppContext) -> None:
    r = rig(ctx)
    r.run(
        [
            {},
            {},
            None,
            {USB: "device"},
            None,
            {USB: "device"},
            {USB: "device", "OTHER-0002": "offline"},
            {},
            {},
            STOP,
        ]
    )
    names = {name for name, _ in r.bus.published}
    assert names == {"device"}
    assert r.bus.states() == ["no_device", "connected_usb", "no_device"]
    payloads = [data for _, data in r.bus.published]
    assert type(payloads[1]["state"]) is str
    assert payloads[1]["model"] == "TestPhone"
    assert set(payloads[1]) == {
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
    }
    assert payloads[0] is not payloads[2]
    payloads[2]["state"] = "tampered"
    assert r.tracker.status().state is S.no_device


@pytest.mark.parametrize(("link", "hint"), [(480, True), (5000, False), (12, False), (None, False)])
def test_usb2_hint(ctx: AppContext, link: int | None, hint: bool) -> None:
    r = rig(ctx, link=link)
    r.run([{USB: "device"}, STOP])
    status = r.tracker.status()
    assert status.link_mbps == link
    assert status.usb2_hint is hint
    assert r.link_calls == [USB]


# -- which phone (R24) --------------------------------------------------------------------------


def test_an_emulator_is_ignored(ctx: AppContext) -> None:
    r = rig(ctx)
    r.run([{"emulator-5554": "device"}, STOP])
    assert r.tracker.status().state is S.no_device
    assert r.tracker.status().message == NO_DEVICE_MESSAGE
    assert device_rows(ctx) == []


def test_an_emulator_next_to_a_phone_is_not_a_second_phone(ctx: AppContext) -> None:
    r = rig(ctx)
    r.run([{"emulator-5554": "device", "emulator-5556": "offline", USB: "device"}, STOP])
    assert r.tracker.status().state is S.connected_usb
    assert [row[2] for row in device_rows(ctx)] == ["FAKE-0001"]


def test_two_ready_phones_on_first_run_store_nothing(ctx: AppContext) -> None:
    r = rig(ctx)
    r.phones["AAA-0003"] = FakeTransport(FakePhone(serial="AAA-0003"))
    seen: list[DeviceStatus] = []
    r.run([{"AAA-0003": "device", USB: "device"}, lambda: seen.append(r.tracker.status()), STOP])
    assert seen[0].state is S.no_device
    assert seen[0].message == MULTIPLE_MESSAGE
    assert device_rows(ctx) == []


def test_two_ready_phones_then_one_stores_that_one(ctx: AppContext) -> None:
    r = rig(ctx)
    r.phones["AAA-0003"] = FakeTransport(FakePhone(serial="AAA-0003"))
    r.run([{"AAA-0003": "device", USB: "device"}, {USB: "device"}, STOP])
    assert r.tracker.status().state is S.connected_usb
    assert [row[2] for row in device_rows(ctx)] == ["FAKE-0001"]


def test_the_stored_phone_wins_over_one_that_sorts_first(ctx: AppContext) -> None:
    rig(ctx).run([{USB: "device"}, STOP])  # FAKE-0001 is stored
    r = rig(ctx)
    r.phones["AAA-0003"] = FakeTransport(FakePhone(serial="AAA-0003"))
    r.run([{"AAA-0003": "device", USB: "device"}, STOP])
    status = r.tracker.status()
    assert status.state is S.connected_usb
    assert status.serial == USB
    assert "wrong_device" not in r.bus.states()


def test_a_different_phone_alone_is_still_the_wrong_device(ctx: AppContext) -> None:
    rig(ctx).run([{USB: "device"}, STOP])
    r = rig(ctx)
    r.phones["AAA-0003"] = FakeTransport(FakePhone(serial="AAA-0003"))
    r.run([{"AAA-0003": "device"}, STOP])
    assert r.tracker.status().state is S.wrong_device


# -- refresh while connected (R23) ---------------------------------------------------------------


class Flaky:
    """A transport whose `info()` fails `fail` more times before working again."""

    def __init__(self, inner: FakeTransport) -> None:
        self.inner = inner
        self.fail = 0
        self.calls = 0

    def info(self) -> Any:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise DeviceTimeout("The phone did not finish in 10 s.")
        return self.inner.info()


def _flaky_rig(ctx: AppContext) -> tuple[Rig, Flaky, FakePhone]:
    r = rig(ctx)
    inner = r.phones[USB]
    assert isinstance(inner, FakeTransport)
    flaky = Flaky(inner)
    r.phones[USB] = flaky  # type: ignore[assignment]
    return r, flaky, inner.phone


def _ticks(r: Rig, seconds: int) -> list[Item]:
    return [tick(r.clock, 1.0), None] * seconds


def test_refresh_updates_battery_and_storage_after_60_s(ctx: AppContext) -> None:
    r, flaky, phone = _flaky_rig(ctx)
    seen: list[DeviceStatus] = []
    note = lambda: seen.append(r.tracker.status())  # noqa: E731

    def drain() -> None:
        phone.battery_pct = 41
        phone.add_file("/sdcard/new.bin", size=10**9)

    r.run([{USB: "device"}, note, drain, *_ticks(r, 59), note, *_ticks(r, 1), note, STOP])
    assert seen[0].battery_pct == 80
    assert seen[1] == seen[0]  # nothing re-read before 60 s
    assert seen[2].battery_pct == 41
    assert seen[0].free_bytes is not None and seen[2].free_bytes is not None
    assert seen[2].free_bytes == seen[0].free_bytes - 10**9
    assert seen[2].link_mbps == 480 and seen[2].usb2_hint
    assert r.link_calls == [USB]  # the link speed is not asked again on refreshes
    assert flaky.calls == 2
    assert r.bus.states() == ["connected_usb", "connected_usb"]  # published: it changed


def test_unchanged_refresh_publishes_nothing(ctx: AppContext) -> None:
    r, flaky, _ = _flaky_rig(ctx)
    r.run([{USB: "device"}, *_ticks(r, 180), STOP])
    assert flaky.calls == 4
    assert r.bus.states() == ["connected_usb"]


def test_one_failed_refresh_keeps_connected(ctx: AppContext) -> None:
    r, flaky, _ = _flaky_rig(ctx)
    fail_once = lambda: setattr(flaky, "fail", 1)  # noqa: E731
    r.run([{USB: "device"}, fail_once, *_ticks(r, 60), STOP])
    assert flaky.calls == 2
    assert r.bus.states() == ["connected_usb"]


def test_two_failed_refreshes_in_a_row_go_offline(ctx: AppContext) -> None:
    r, flaky, _ = _flaky_rig(ctx)
    fail_twice = lambda: setattr(flaky, "fail", 2)  # noqa: E731
    seen: list[DeviceStatus] = []
    r.run(
        [
            {USB: "device"},
            fail_twice,
            *_ticks(r, 120),
            lambda: seen.append(r.tracker.status()),
            *_ticks(r, 2),  # its read is retried after the backoff, and works
            STOP,
        ]
    )
    assert seen[0].state is S.offline
    assert seen[0].message == OFFLINE_MESSAGE
    assert r.bus.states() == ["connected_usb", "offline", "connected_usb"]


def test_failures_must_be_in_a_row(ctx: AppContext) -> None:
    r, flaky, _ = _flaky_rig(ctx)
    fail_once = lambda: setattr(flaky, "fail", 1)  # noqa: E731
    r.run([{USB: "device"}, fail_once, *_ticks(r, 120), fail_once, *_ticks(r, 60), STOP])
    assert flaky.calls == 4  # read, failed at 60 s, fine at 120 s, failed at 180 s
    assert r.bus.states() == ["connected_usb"]


def test_a_changed_identity_on_refresh_is_the_wrong_device(ctx: AppContext) -> None:
    r, _, phone = _flaky_rig(ctx)

    def swap() -> None:
        phone.serial = "OTHER-0002"

    r.run([{USB: "device"}, swap, *_ticks(r, 60), STOP])
    assert r.bus.states() == ["connected_usb", "wrong_device"]
    assert [row[2] for row in device_rows(ctx)] == ["FAKE-0001"]


def test_refreshes_write_last_seen_at_most_every_10_minutes(ctx: AppContext) -> None:
    r, _, _ = _flaky_rig(ctx)
    stamps: list[int] = []
    note = lambda: stamps.append(device_rows(ctx)[0][5])  # noqa: E731
    r.run([{USB: "device"}, note, *_ticks(r, 540), note, *_ticks(r, 60), note, STOP])
    first = 1_700_000_000 + 1000
    assert stamps == [first, first, first + 600]


# -- stop -------------------------------------------------------------------------------------


def test_nothing_is_written_or_published_after_stop(ctx: AppContext) -> None:
    r = rig(ctx)
    inner = r.phones[USB]

    class StopsDuringInfo:
        def info(self) -> Any:
            r.tracker.stop()
            return inner.info()

    r.phones[USB] = StopsDuringInfo()  # type: ignore[assignment]
    r.run([{}, {USB: "device"}])
    assert device_rows(ctx) == []
    assert r.bus.states() == ["no_device"]


def test_a_refresh_after_stop_writes_nothing(ctx: AppContext) -> None:
    r, _, phone = _flaky_rig(ctx)

    def stop_then_change() -> None:
        phone.battery_pct = 12
        r.tracker.stop()

    r.run([{USB: "device"}, *_ticks(r, 599), stop_then_change, *_ticks(r, 1)])
    assert r.bus.states() == ["connected_usb"]
    assert device_rows(ctx)[0][5] == 1_700_000_000 + 1000


# -- the thread --------------------------------------------------------------------------------


class BlockingSource:
    """Yields one snapshot, then blocks (like a quiet adb server) until closed."""

    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[Snapshot | None]:
        yield self._snapshot
        self._closed.wait(10)

    def close(self) -> None:
        self._closed.set()


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _threaded(ctx: AppContext, events: Callable[[], Any]) -> DeviceTracker:
    return DeviceTracker(
        ctx,
        events=events,
        connect=lambda serial: FakeTransport(FakePhone(serial=serial)),
        usb_present=lambda: False,
        link_speed=lambda serial: 480,
    )


def _tracker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "sift-device-tracker"]


def test_start_runs_a_daemon_thread_and_stop_ends_it_promptly(ctx: AppContext) -> None:
    sources: list[BlockingSource] = []

    def events() -> BlockingSource:
        sources.append(BlockingSource({USB: "device"}))
        return sources[-1]

    tracker = _threaded(ctx, events)
    tracker.start()
    try:
        _wait_for(lambda: tracker.status().state is S.connected_usb)
        (thread,) = _tracker_threads()
        assert thread.daemon
    finally:
        started = time.monotonic()
        tracker.stop()
    assert time.monotonic() - started < 1
    assert _tracker_threads() == []
    assert len(sources) == 1
    tracker.stop()  # idempotent


def test_retry_from_another_thread_wakes_a_backoff_wait(ctx: AppContext) -> None:
    opened: list[int] = []
    clock = ManualClock()

    def events() -> BlockingSource:
        opened.append(len(opened))
        if len(opened) < 3:
            raise DeviceDisconnected("adb server went away")
        return BlockingSource({})

    def sleep(seconds: float) -> None:
        if len(opened) < 2:
            clock.advance(seconds)
        else:
            time.sleep(0.002)  # the second wait never ends on its own: only Retry ends it

    tracker = DeviceTracker(
        ctx,
        events=events,
        connect=lambda serial: FakeTransport(FakePhone(serial=serial)),
        usb_present=lambda: False,
        link_speed=lambda serial: 480,
        clock=clock,
        sleep=sleep,
    )
    tracker.start()
    try:
        _wait_for(lambda: len(opened) == 2, timeout=2)
        time.sleep(0.02)
        assert len(opened) == 2  # still waiting
        tracker.retry()
        _wait_for(lambda: len(opened) == 3, timeout=1)
        _wait_for(lambda: tracker.status().state is S.no_device)
    finally:
        tracker.stop()
    assert _tracker_threads() == []


def test_stop_before_start_and_status_before_any_event(ctx: AppContext) -> None:
    tracker = _threaded(ctx, lambda: BlockingSource({}))
    status = tracker.status()
    assert status.state is S.no_device
    assert status.message
    tracker.stop()


def test_context_has_no_tracker_by_default(ctx: AppContext) -> None:
    assert ctx.tracker is None


# -- production wiring -------------------------------------------------------------------------


def _phone_shell(command: bytes) -> list[bytes]:
    answers = {
        b"getprop ro.serialno": b"FAKESERIAL\n",
        b"getprop ro.product.model": b"TestPhone\n",
        b"getprop ro.build.version.release": b"16\n",
    }
    return [answers.get(command, b"")]


class TrackingServer(FakeAdbServer):
    """The fake adb server, plus `host:track-devices` announcing its one phone as ready."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self._pending = threading.local()

    def _read_host_request(self, conn: Any) -> bytes:
        pending: bytes | None = getattr(self._pending, "request", None)
        if pending is not None:
            self._pending.request = None
            return pending
        return super()._read_host_request(conn)

    def _host_phase(self, conn: Any) -> None:
        request = self._read_host_request(conn)
        if request == b"host:track-devices":
            body = self.serial.encode() + b"\tdevice\n"
            conn.sendall(b"OKAY" + b"%04x" % len(body) + body)
            self.stopping.wait(10)
            return
        self._pending.request = request
        super()._host_phase(conn)


def test_adb_tracker_reads_the_real_protocols(ctx: AppContext) -> None:
    probes: list[tuple[str, int]] = []

    def probe(host: str, port: int) -> str:
        probes.append((host, port))
        return "41"

    with TrackingServer(exec_handler=_phone_shell) as server:
        config = PhoneConfig(adb_server_port=server.port)
        manager = AdbManager(ctx.paths, config, server_probe=probe)
        tracker = adb_tracker(
            ctx, manager=manager, usb_present=lambda: False, link_speed=lambda serial: 480
        )
        tracker.start()
        try:
            _wait_for(lambda: tracker.status().state is S.connected_usb)
        finally:
            tracker.stop()
        status = tracker.status()
        assert status.serial == "FAKESERIAL"
        assert status.model == "TestPhone"
        assert status.android_version == "16"
        assert status.usb2_hint is True
        assert probes == [("127.0.0.1", server.port)]
        assert server.requests[0] == b"host:track-devices"
        commands = [r.removeprefix(b"exec:") for r in server.requests if r.startswith(b"exec:")]
        assert commands
        assert set(commands) <= READ_COMMANDS  # reads only
    assert _tracker_threads() == []


READ_COMMANDS = {
    b"getprop ro.serialno",
    b"getprop ro.product.model",
    b"getprop ro.build.version.release",
    b"df /sdcard",
    b"wm size",
    b"dumpsys battery",
}
