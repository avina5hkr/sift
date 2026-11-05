"""DeviceTracker: the adb server's device list turned into one plain-words connection state.

How it runs. One daemon thread ("sift-device-tracker") runs `run()`:

1. Open the device list with `events()`. In production that is `AdbManager.ensure_server()` (the
   only way the tracker ever runs an adb program, and only when no server answers) followed by a
   raw-socket `host:track-devices` stream (`TrackDevices`). An `AdbError` here is `no_adb`.
2. Read the stream. Each snapshot (`serial -> adb state`) and each idle tick (`None`, about once
   a second while nothing changes) re-evaluates the state with the injected clock:
   - no device line: `no_device`, or `held_by_other` once `usb_present()` has stayed true for
     3 s (another adb server holds the phone's USB interface);
   - `unauthorized`/`authorizing`: `unauthorized`; any other non-ready state: `offline`;
   - `device`: connect a transport, read `info()` (identity: ro.serialno, else android_id) and,
     over USB, the link speed; then check the identity against the stored phone (the first phone
     ever seen) and give `connected_usb`/`connected_wifi`, or `wrong_device`. A transport error
     gives `offline`; the phone is read again on the next event, or on a tick once its own
     backoff (1, 2, 4 ... s) has passed.
     A ready phone is read once per appearance, not on every snapshot.
3. When the stream breaks (`DeviceDisconnected`, or it ends), the state becomes `offline` (never
   "connected" while adb is gone) and the tracker reopens it after 1, 2, 4, 8, 16, then every
   30 s. A snapshot resets that schedule; so does `retry()`, which also reconnects at once.

`stop()` sets a flag, closes the open stream (which wakes a blocked reader) and joins the thread;
backoff waits are taken in short steps of the injected `sleep`, so they notice stop and retry.

Every status change publishes a `device` event carrying a fresh `asdict(status)`, with the state
as its plain string value. Nothing here writes to the phone.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from sift.core.context import AppContext
from sift.device.adb_manager import ADB_SERVER_HOST, AdbError, AdbManager
from sift.device.adb_transport import AdbTransport, is_wifi_serial
from sift.device.adbutils_guard import install_no_spawn_guard
from sift.device.track_devices import DeviceEventSource, Snapshot, TrackDevices
from sift.device.transport import (
    NO_IDENTITY_MESSAGE,
    DeviceInfo,
    RemoteError,
    Transport,
    TransportError,
)
from sift.services.usblink import android_usb_present, link_speed_mbps

logger = logging.getLogger("sift.device")

HELD_AFTER_S = 3.0  # USB shows a phone, adb does not, for this long: another tool holds it
USB_POLL_S = 1.0  # how often `usb_present()` is asked while no phone is in adb's list
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0)
BACKOFF_MAX_S = 30.0
_WAIT_STEP_S = 0.2  # backoff waits are slept in steps this long so stop/retry are noticed
_JOIN_TIMEOUT_S = 5.0
_THREAD_NAME = "sift-device-tracker"

LOOKING_MESSAGE = "Looking for your phone."
NO_DEVICE_MESSAGE = "No phone found. Connect your phone with a USB cable and unlock it."
UNAUTHORIZED_MESSAGE = (
    "Unlock the phone and tap Allow on the USB debugging prompt. If nothing appears, set USB "
    "mode to File transfer; on Samsung phones also check that Auto Blocker is off."
)
OFFLINE_MESSAGE = (
    "The phone is connected but not responding. Unlock it, or unplug it and plug it back in."
)
NOT_ANSWERING_MESSAGE = (
    "The phone is connected but did not answer. Keep it unlocked and connected; Sift will try "
    "again."
)
SERVER_LOST_MESSAGE = "Lost contact with adb. Sift is reconnecting."
HELD_BY_OTHER_MESSAGE = (
    "Another tool may be using the phone (for example Android Studio). Close it, then press Retry."
)
WRONG_DEVICE_MESSAGE = (
    "A different phone is connected. Sift backs up the first phone it was connected to; "
    "connect that phone to continue."
)
USB_MESSAGE = "Connected over USB."
WIFI_MESSAGE = "Connected over Wi-Fi."

_UNAUTHORIZED_STATES = frozenset({"unauthorized", "authorizing"})


class ConnectionState(StrEnum):
    no_adb = "no_adb"
    no_device = "no_device"
    unauthorized = "unauthorized"
    offline = "offline"
    wrong_device = "wrong_device"
    held_by_other = "held_by_other"
    connected_usb = "connected_usb"
    connected_wifi = "connected_wifi"


@dataclass(frozen=True)
class DeviceStatus:
    state: ConnectionState
    message: str  # plain words, shown to the person as is
    serial: str | None = None  # the adb serial (USB serial number, or host:port over Wi-Fi)
    model: str | None = None
    android_version: str | None = None
    battery_pct: int | None = None
    free_bytes: int | None = None
    total_bytes: int | None = None
    link_mbps: int | None = None
    usb2_hint: bool = False
    device_uuid: str | None = None  # the stored phone's `device.uuid` when connected

    def as_event(self) -> dict[str, Any]:
        """A fresh dict for the `device` event, with the state as its plain string."""
        data = asdict(self)
        data["state"] = self.state.value
        return data


class Backoff:
    """1, 2, 4, 8, 16, then 30 s for ever, until `reset()`."""

    def __init__(self) -> None:
        self._attempts = 0

    def next(self) -> float:
        delay = BACKOFF_S[self._attempts] if self._attempts < len(BACKOFF_S) else BACKOFF_MAX_S
        self._attempts += 1
        return delay

    def reset(self) -> None:
        self._attempts = 0


class DeviceTracker:
    def __init__(
        self,
        ctx: AppContext,
        *,
        events: Callable[[], DeviceEventSource],
        connect: Callable[[str], Transport],
        usb_present: Callable[[], bool],
        link_speed: Callable[[str], int | None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctx = ctx
        self._events = events
        self._connect = connect
        self._usb_present = usb_present
        self._link_speed = link_speed
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock

        # Shared with other threads: guarded by _lock.
        self._lock = threading.Lock()
        self._status = DeviceStatus(ConnectionState.no_device, LOOKING_MESSAGE)
        self._stopping = False
        self._retry_requested = False
        self._source: DeviceEventSource | None = None
        self._thread: threading.Thread | None = None

        # Used by the tracker thread only.
        self._backoff = Backoff()
        self._snapshot: Snapshot = {}
        self._resolved: str | None = None  # adb serial already read (connected or wrong phone)
        self._failed: str | None = None  # adb serial whose read failed ...
        self._failed_retry_at = 0.0  # ... and when to read it again
        self._failed_backoff = Backoff()
        self._usb_since: float | None = None
        self._usb_polled_at: float | None = None

    # -- public -----------------------------------------------------------------------------

    def start(self) -> None:
        """Run the tracker on a daemon thread. Call once."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("The device tracker was already started.")
            self._thread = threading.Thread(target=self.run, name=_THREAD_NAME, daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """Stop the thread: close the stream, wake any wait, and join. Idempotent."""
        with self._lock:
            self._stopping = True
            source, thread = self._source, self._thread
        if source is not None:
            source.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.warning("The device tracker did not stop within %s s", _JOIN_TIMEOUT_S)

    def status(self) -> DeviceStatus:
        with self._lock:
            return self._status

    def retry(self) -> None:
        """Reconnect now: reset the backoff, reopen the device list, and read the phone again."""
        with self._lock:
            self._retry_requested = True
            source = self._source
        if source is not None:
            source.close()

    def run(self) -> None:
        """The tracker loop (what `start()` runs on its thread); returns after `stop()`."""
        while not self._is_stopping():
            if self._take_retry_request():
                self._backoff.reset()
                self._forget_phone()
            try:
                self._run_stream()
            except Exception:
                logger.exception("The device tracker hit an unexpected error")
                self._forget_phone()
                self._set(DeviceStatus(ConnectionState.offline, SERVER_LOST_MESSAGE))
            if self._is_stopping():
                break
            if self._retry_pending():
                continue
            self._wait(self._backoff.next())

    # -- the stream -------------------------------------------------------------------------

    def _run_stream(self) -> None:
        """Open the device list and follow it until it breaks, or until stop or retry."""
        try:
            source = self._events()
        except AdbError as e:
            self._forget_phone()
            self._set(DeviceStatus(ConnectionState.no_adb, e.message))
            return
        except (TransportError, OSError) as e:
            logger.info("Could not open adb's device list: %s", e)
            self._lost()
            return
        with self._lock:
            ending = self._stopping
            if not ending:
                self._source = source
        if ending:
            source.close()
            return
        try:
            for item in source:
                if self._is_stopping() or self._retry_pending():
                    return
                if item is not None:
                    self._snapshot = item
                    self._backoff.reset()
                    self._failed_retry_at = 0.0  # a new event retries a failed read at once
                self._evaluate()
        except TransportError as e:
            logger.info("adb's device list broke: %s", e)
        finally:
            with self._lock:
                self._source = None
            source.close()
        if not self._is_stopping() and not self._retry_pending():
            self._lost()

    def _lost(self) -> None:
        self._snapshot = {}
        self._forget_phone()
        self._set(DeviceStatus(ConnectionState.offline, SERVER_LOST_MESSAGE))

    def _wait(self, seconds: float) -> None:
        end = self._clock() + seconds
        while not self._is_stopping() and not self._retry_pending():
            left = end - self._clock()
            if left <= 0:
                return
            self._sleep(min(_WAIT_STEP_S, left))

    # -- the state --------------------------------------------------------------------------

    def _evaluate(self) -> None:
        now = self._clock()
        if not self._snapshot:
            self._resolved = self._failed = None
            self._check_usb(now)
            return
        self._usb_since = self._usb_polled_at = None
        serial, state = self._pick(self._snapshot)
        if state == "device":
            if serial == self._resolved:
                return
            if serial == self._failed and now < self._failed_retry_at:
                return
            self._read_phone(serial, now)
            return
        self._forget_phone()
        if state in _UNAUTHORIZED_STATES:
            self._set(DeviceStatus(ConnectionState.unauthorized, UNAUTHORIZED_MESSAGE, serial))
        else:
            self._set(DeviceStatus(ConnectionState.offline, OFFLINE_MESSAGE, serial))

    def _pick(self, snapshot: Snapshot) -> tuple[str, str]:
        """The phone to report: a ready one first, then one waiting for Allow, then any.

        Among equals, the one already reported wins, then the lowest serial.
        """

        def rank(item: tuple[str, str]) -> tuple[int, bool, str]:
            serial, state = item
            order = 0 if state == "device" else 1 if state in _UNAUTHORIZED_STATES else 2
            return order, serial != self._resolved, serial

        return min(snapshot.items(), key=rank)

    def _check_usb(self, now: float) -> None:
        if self._usb_polled_at is None or now - self._usb_polled_at >= USB_POLL_S:
            self._usb_polled_at = now
            if self._usb_present():
                if self._usb_since is None:
                    self._usb_since = now
            else:
                self._usb_since = None
        if self._usb_since is not None and now - self._usb_since >= HELD_AFTER_S:
            self._set(DeviceStatus(ConnectionState.held_by_other, HELD_BY_OTHER_MESSAGE))
        else:
            self._set(DeviceStatus(ConnectionState.no_device, NO_DEVICE_MESSAGE))

    def _read_phone(self, serial: str, now: float) -> None:
        try:
            info = self._connect(serial).info()
            if not info.serial:
                raise RemoteError(NO_IDENTITY_MESSAGE)
        except TransportError as e:
            logger.info("Could not read the phone's details: %s", e)
            no_identity = isinstance(e, RemoteError) and str(e) == NO_IDENTITY_MESSAGE
            message = NO_IDENTITY_MESSAGE if no_identity else NOT_ANSWERING_MESSAGE
            self._resolved = None
            self._failed = serial
            self._failed_retry_at = now + self._failed_backoff.next()
            self._set(DeviceStatus(ConnectionState.offline, message, serial))
            return
        self._failed = None
        self._failed_backoff.reset()
        self._resolved = serial
        self._set(self._identify(serial, info))

    def _identify(self, serial: str, info: DeviceInfo) -> DeviceStatus:
        device_uuid = self._remember(info)
        if device_uuid is None:
            return DeviceStatus(
                ConnectionState.wrong_device,
                WRONG_DEVICE_MESSAGE,
                serial,
                model=info.model,
                android_version=info.android_version,
            )
        wifi = is_wifi_serial(serial)
        link = None if wifi else self._link_speed(serial)
        return DeviceStatus(
            ConnectionState.connected_wifi if wifi else ConnectionState.connected_usb,
            WIFI_MESSAGE if wifi else USB_MESSAGE,
            serial,
            model=info.model,
            android_version=info.android_version,
            battery_pct=info.battery_pct,
            free_bytes=info.free_bytes,
            total_bytes=info.total_bytes,
            link_mbps=link,
            usb2_hint=link == 480,
            device_uuid=device_uuid,
        )

    def _remember(self, info: DeviceInfo) -> str | None:
        """Store or refresh the phone; its `device.uuid`, or None if another phone is stored.

        M1 rule until the setup wizard picks a phone: the first phone ever seen (the row with
        the lowest id) is the phone Sift backs up.
        """
        seen_at = int(self._wall_clock())
        with self._ctx.db.tx() as conn:
            row = conn.execute("SELECT id, uuid, serial FROM device ORDER BY id LIMIT 1").fetchone()
            if row is None:
                device_uuid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO device (uuid, serial, model, android_version, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (device_uuid, info.serial, info.model, info.android_version, seen_at),
                )
                return device_uuid
            row_id, stored_uuid, stored_identity = row
            if stored_identity != info.serial:
                return None
            conn.execute(
                "UPDATE device SET model = ?, android_version = ?, last_seen_at = ? WHERE id = ?",
                (info.model, info.android_version, seen_at, row_id),
            )
            return str(stored_uuid)

    def _forget_phone(self) -> None:
        """Read the phone afresh next time, and restart the held-by-another-tool timer."""
        self._resolved = self._failed = None
        self._failed_backoff.reset()
        self._usb_since = self._usb_polled_at = None

    def _set(self, status: DeviceStatus) -> None:
        with self._lock:
            if status == self._status:
                return
            self._status = status
        self._ctx.bus.publish("device", status.as_event())

    # -- flags ------------------------------------------------------------------------------

    def _is_stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def _retry_pending(self) -> bool:
        with self._lock:
            return self._retry_requested

    def _take_retry_request(self) -> bool:
        with self._lock:
            requested, self._retry_requested = self._retry_requested, False
            return requested


def adb_tracker(
    ctx: AppContext,
    *,
    manager: AdbManager | None = None,
    usb_present: Callable[[], bool] = android_usb_present,
    link_speed: Callable[[str], int | None] = link_speed_mbps,
) -> DeviceTracker:
    """The production tracker: the shared adb server's device list and `AdbTransport`.

    `ensure_server()` attaches to a server that answers (no adb program is run) or starts one;
    it is called each time the device list is (re)opened.
    """
    install_no_spawn_guard()
    adb = manager if manager is not None else AdbManager(ctx.paths, ctx.config.phone)
    port = ctx.config.phone.adb_server_port

    def events() -> DeviceEventSource:
        nonlocal port
        port = adb.ensure_server()
        return TrackDevices(ADB_SERVER_HOST, port)

    def connect(serial: str) -> Transport:
        return AdbTransport(serial, port)

    return DeviceTracker(
        ctx, events=events, connect=connect, usb_present=usb_present, link_speed=link_speed
    )
