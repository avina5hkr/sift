"""DeviceTracker: the adb server's device list turned into one plain-words connection state.

How it runs. One daemon thread ("sift-device-tracker") runs `run()`:

1. Open the device list with `events()`. In production that is `AdbManager.ensure_server()` (the
   only way the tracker ever runs an adb program, and only when nothing listens on adb's port)
   followed by a raw-socket `host:track-devices` stream (`TrackDevices`). An `AdbError` here is
   `no_adb` with the manager's message: `reason` "adb_missing" when adb is missing or broken,
   "adb_unavailable" for an `AdbServerError` (the server could not be started, or something
   else on its port does not answer).
2. Read the stream. Each snapshot (`serial -> adb state`) and each idle tick (`None`, about once
   a second while nothing changes) re-evaluates the state with the injected clock. Emulators
   (`emulator-N`) are left out entirely.
   - no device line: `no_device`, or `held_by_other` once `usb_present()` has stayed true for
     3 s (another adb server holds the phone's USB interface);
   - no phone stored yet and more than one phone in any adb state: `no_device` asking for just
     one phone (nothing is stored, so the wrong phone can never be locked in);
   - otherwise one phone is picked: a ready one first (the stored phone before any other),
     then one waiting for Allow, then any;
   - `unauthorized`/`authorizing`: `unauthorized`; any other non-ready state: `offline`;
   - `device`: connect a transport, read `info()` (identity: ro.serialno and android_id, kept
     apart) and, over USB, the link speed; then check the identity against the stored phone (the
     first phone ever seen; the same phone when either identifier is equal) and give
     `connected_usb`/`connected_wifi`, or `wrong_device`. While connected, the transport lives
     in a `DeviceSession` that `session()` hands out, so every user of the phone shares its
     one pool of connection slots. A transport error
     gives `offline`; the phone is read again on the next event, or on a tick once its own
     backoff (1, 2, 4 ... s) has passed. A ready phone is read once per appearance; while it is
     connected, `info()` is re-read every 60 s (battery, storage, identity; not the link speed),
     `last_seen_at` is written at most every 10 minutes, and only two failed refreshes in a row
     show `offline`.
3. When the stream breaks (`DeviceDisconnected`, or it ends), the state becomes `offline` (never
   "connected" while adb is gone) and the tracker reopens it after 1, 2, 4, 8, 16, then every
   30 s. A stream that stayed up for 30 s resets that schedule; so does `retry()`, which also
   reconnects at once.

`stop()` sets a flag, closes the open stream (which wakes a blocked reader) and joins the thread;
backoff waits are taken in short steps of the injected `sleep`, so they notice stop and retry.
Once the flag is set nothing more is written to the database or published.

Every status change publishes a `device` event carrying a fresh `asdict(status)`, with the state
as its plain string value. Nothing here writes to the phone.
"""

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from sift.core.context import AppContext
from sift.device.adb_manager import ADB_SERVER_HOST, AdbError, AdbManager, AdbServerError
from sift.device.adb_transport import AdbTransport, is_wifi_serial
from sift.device.session import DeviceSession, PhoneIdentity
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
STABLE_STREAM_S = 30.0  # a device list that stayed open this long resets the backoff
REFRESH_S = 60.0  # while connected, re-read battery and storage (and identity) this often
REFRESH_FAILURES_OFFLINE = 2  # consecutive failed refreshes before the phone shows offline
LAST_SEEN_EVERY_S = 600.0  # refreshes write device.last_seen_at at most this often
_EMULATOR = re.compile(r"emulator-\d+")
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
    "The phone is connected but not responding. Unlock it, or unplug and reconnect the cable. "
    "On Samsung phones also check that Auto Blocker is off."
)
NOT_ANSWERING_MESSAGE = OFFLINE_MESSAGE
MULTIPLE_MESSAGE = "More than one phone is connected. Connect only the phone you want to back up."
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

# The stored phone: the one the owner chose (`setting.active_device`), else the first one ever
# seen. The wizard's picker and Settings' switch write that setting; nothing else does.
ACTIVE_DEVICE_KEY = "active_device"
STORED_PHONE_ORDER = (
    f"ORDER BY uuid IS NOT (SELECT value FROM setting WHERE key = '{ACTIVE_DEVICE_KEY}'), id"
)
_STORED_PHONE_SQL = f"SELECT uuid, serial, android_id FROM device {STORED_PHONE_ORDER} LIMIT 1"

NOT_ATTACHED_MESSAGE = "That phone is no longer connected. Reconnect it and choose again."
NOT_READY_MESSAGE = (
    "Sift cannot read that phone yet. Unlock it and tap Allow on the USB debugging prompt, "
    "then choose it again."
)


class PhonePickError(Exception):
    """Choosing a phone did not work; the message is written for the person using Sift."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PhoneNotAttached(PhonePickError):
    """The phone chosen is not in adb's device list (any more)."""


class PhoneNotReady(PhonePickError):
    """The phone is attached but cannot be read: it waits for Allow, or does not answer."""


@dataclass(frozen=True)
class AttachedPhone:
    """One phone adb lists, for the device picker (emulators are never listed)."""

    serial: str  # the adb serial: for matching only, the UI shows its last 4 characters
    ready: bool  # adb says "device" and the phone answered
    waiting_for_allow: bool
    model: str | None  # only for a phone that answered
    stored: bool  # this is the phone Sift backs up


class ConnectionState(StrEnum):
    no_adb = "no_adb"
    no_device = "no_device"
    unauthorized = "unauthorized"
    offline = "offline"
    wrong_device = "wrong_device"
    held_by_other = "held_by_other"
    connected_usb = "connected_usb"
    connected_wifi = "connected_wifi"


# Why the state is `no_adb`: adb is missing or broken, or its server is unavailable.
NoAdbReason = Literal["adb_missing", "adb_unavailable"]


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
    reason: NoAdbReason | None = None  # set only for `no_adb`

    def as_event(self) -> dict[str, Any]:
        """A fresh dict for the `device` event, with the state as its plain string."""
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass
class _Connection:
    """The phone currently shown as connected, and when to refresh its details."""

    serial: str
    session: DeviceSession
    link_mbps: int | None
    refresh_at: float
    failures: int = 0


@dataclass
class _StoredPhone:
    """The stored phone: its `device.uuid` and identity.

    The `device` table holds the identity in two columns: `serial` (the key the phone was first
    stored under: `ro.serialno`, else the Android ID) and `android_id`. A row from before the
    `android_id` column existed has no Android ID, so its one key is offered as both identifiers
    until the phone is seen again and its Android ID is learned and saved.
    """

    uuid: str
    identity: PhoneIdentity
    saved_android_id: str | None = None  # what `device.android_id` holds right now


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
        self._session: DeviceSession | None = None
        self._source: DeviceEventSource | None = None
        self._thread: threading.Thread | None = None
        self._peeked: dict[str, DeviceInfo] = {}  # picker readings, cleared by a Retry

        # Used by the tracker thread only.
        self._backoff = Backoff()
        self._snapshot: Snapshot = {}
        self._resolved: str | None = None  # adb serial already read (connected or wrong phone)
        self._failed: str | None = None  # adb serial whose read failed ...
        self._failed_retry_at = 0.0  # ... and when to read it again
        self._failed_backoff = Backoff()
        self._usb_since: float | None = None
        self._usb_polled_at: float | None = None
        self._connection: _Connection | None = None
        self._stored: _StoredPhone | None = None
        self._stored_loaded = False
        self._seen_written_at: float | None = None

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
            self._session = None
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

    def session(self) -> DeviceSession | None:
        """The stored phone's session while it is connected, else None.

        Everything that talks to the phone uses `session.transport`, so all of it shares one
        pool of connection slots. The session is replaced when the phone reconnects, so hold it
        only for as long as one piece of work.
        """
        with self._lock:
            return self._session

    def attached_phones(self) -> list[AttachedPhone]:
        """Every phone adb lists now (no emulators), each read once for its model.

        For the device picker. A phone adb calls ready is read through a short-lived transport
        and remembered until the next Retry, so polling this does not keep asking the phone.
        """
        snapshot = dict(self._snapshot)
        with self._ctx.db.read() as conn:
            stored = self._stored_from_row(conn.execute(_STORED_PHONE_SQL).fetchone())
        out: list[AttachedPhone] = []
        for serial, state in sorted(snapshot.items()):
            if _EMULATOR.fullmatch(serial):
                continue
            info = self._peek(serial) if state == "device" else None
            is_stored = (
                info is not None
                and stored is not None
                and stored.identity.matches(PhoneIdentity.from_info(info))
            )
            out.append(
                AttachedPhone(
                    serial,
                    ready=info is not None,
                    waiting_for_allow=state in _UNAUTHORIZED_STATES,
                    model=info.model if info is not None else None,
                    stored=is_stored,
                )
            )
        return out

    def _peek(self, serial: str) -> DeviceInfo | None:
        with self._lock:
            known = self._peeked.get(serial)
        if known is not None:
            return known
        try:
            info = self._info(self._connect(serial))
        except (TransportError, OSError) as e:
            logger.info("Could not read an attached phone for the picker: %s", e)
            return None
        with self._lock:
            self._peeked[serial] = info
        return info

    def choose_phone(self, serial: str) -> str:
        """Make the attached phone `serial` the phone Sift backs up; returns its `device.uuid`.

        A phone Sift has stored before keeps its row (its backups stay its own); a new one gets
        a row. Either way it becomes the stored phone, and the tracker reads it afresh at once.
        Raises `PhoneNotAttached` or `PhoneNotReady`. The full serial is never logged.
        """
        state = self._snapshot.get(serial)
        if state is None or _EMULATOR.fullmatch(serial):
            raise PhoneNotAttached(NOT_ATTACHED_MESSAGE)
        if state != "device":
            raise PhoneNotReady(NOT_READY_MESSAGE)
        try:
            info = self._info(self._connect(serial))
        except (TransportError, OSError) as e:
            logger.info("Could not read the phone chosen: %s", e)
            raise PhoneNotReady(NOT_READY_MESSAGE) from e
        seen = PhoneIdentity.from_info(info)
        now = int(self._wall_clock())
        with self._ctx.db.tx() as conn:
            chosen: str | None = None
            for row in conn.execute("SELECT uuid, serial, android_id FROM device ORDER BY id"):
                known = self._stored_from_row(row)
                if known is not None and known.identity.matches(seen):
                    chosen = known.uuid
                    conn.execute(
                        "UPDATE device SET model = ?, android_version = ?, last_seen_at = ?, "
                        "android_id = COALESCE(?, android_id) WHERE uuid = ?",
                        (info.model, info.android_version, now, seen.android_id, chosen),
                    )
                    break
            if chosen is None:
                chosen = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO device "
                    "(uuid, serial, android_id, model, android_version, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chosen, seen.key(), seen.android_id, info.model, info.android_version, now),
                )
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (ACTIVE_DEVICE_KEY, chosen),
            )
        self.retry()
        return chosen

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
                self._after_retry()
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
            reason: NoAdbReason = (
                "adb_unavailable" if isinstance(e, AdbServerError) else "adb_missing"
            )
            self._set(DeviceStatus(ConnectionState.no_adb, e.message, reason=reason))
            return
        except (TransportError, OSError) as e:
            logger.info("Could not open adb's device list: %s", e)
            self._lost()
            return
        with self._lock:
            ending = self._stopping
            retried = False
            if not ending:
                self._source = source
                # A Retry pressed while the list was opening is answered by this fresh list.
                retried, self._retry_requested = self._retry_requested, False
        if ending:
            source.close()
            return
        if retried:
            self._after_retry()
        opened_at = self._clock()
        try:
            for item in source:
                if self._is_stopping() or self._retry_pending():
                    return
                if item is not None:
                    self._snapshot = item
                    self._failed_retry_at = 0.0  # a new event retries a failed read at once
                if self._clock() - opened_at >= STABLE_STREAM_S:
                    # Only a list that stays up resets the backoff: one that sends a snapshot
                    # and dies must not reopen (and run ensure_server) every second.
                    self._backoff.reset()
                self._evaluate()
        except TransportError as e:
            logger.info("adb's device list broke: %s", e)
        finally:
            with self._lock:
                self._source = None
            source.close()
        if not self._is_stopping() and not self._retry_pending():
            self._lost()

    def _after_retry(self) -> None:
        with self._lock:
            self._peeked.clear()
        self._backoff.reset()
        self._forget_phone()
        self._stored_loaded = False

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
        # Emulators are never the phone to back up: they are left out entirely.
        devices = {
            serial: state
            for serial, state in self._snapshot.items()
            if not _EMULATOR.fullmatch(serial)
        }
        if not devices:
            self._resolved = self._failed = None
            self._set_connection(None)
            self._check_usb(now)
            return
        self._usb_since = self._usb_polled_at = None
        stored = self._stored_phone()
        identity = stored.identity if stored is not None else None
        if stored is None and len(devices) > 1:
            # Nothing stored yet: never guess which of several phones is the one to keep. A phone
            # waiting for Allow counts too, or a stranger's ready phone would be stored first.
            self._forget_phone()
            self._set(DeviceStatus(ConnectionState.no_device, MULTIPLE_MESSAGE))
            return
        serial, state = self._pick(devices, identity)
        if state == "device":
            if serial == self._resolved:
                connection = self._connection
                if connection is not None and now >= connection.refresh_at:
                    self._refresh(connection, now)
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

    def _pick(self, devices: Snapshot, identity: PhoneIdentity | None) -> tuple[str, str]:
        """The phone to report: a ready one first, then one waiting for Allow, then any.

        Among equals, the stored phone wins (so the owner's phone never shows as a different
        phone because another one sorts first), then the one already reported, then the lowest
        serial.
        """

        def rank(item: tuple[str, str]) -> tuple[int, bool, bool, str]:
            serial, state = item
            order = 0 if state == "device" else 1 if state in _UNAUTHORIZED_STATES else 2
            # The adb serial of a USB phone is its `ro.serialno`, so the stored serial finds it.
            not_stored = identity is None or not (identity.serial and serial == identity.serial)
            return order, not_stored, serial != self._resolved, serial

        return min(devices.items(), key=rank)

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

    @staticmethod
    def _info(transport: Transport) -> DeviceInfo:
        info = transport.info()
        if PhoneIdentity.from_info(info).is_empty():
            raise RemoteError(NO_IDENTITY_MESSAGE)
        return info

    def _read_phone(self, serial: str, now: float) -> None:
        """A ready phone appeared: read it, check its identity, and show it."""
        try:
            transport = self._connect(serial)
            info = self._info(transport)
        except TransportError as e:
            self._read_failed(serial, now, e)
            return
        if self._is_stopping():
            return  # nothing is written or published once stop() was called
        self._failed = None
        self._failed_backoff.reset()
        self._resolved = serial
        stored = self._remember(info, now, touch=True)
        if stored is None:
            self._set_connection(None)
            self._set(self._wrong_device(serial, info))
            return
        link = None if is_wifi_serial(serial) else self._link_speed(serial)
        session = DeviceSession(transport, stored.identity, stored.uuid)
        self._set_connection(_Connection(serial, session, link, now + REFRESH_S))
        self._set(self._connected(serial, info, link, stored.uuid))

    def _refresh(self, connection: _Connection, now: float) -> None:
        """Re-read battery, storage and identity of the connected phone (no link speed).

        One failed refresh keeps the connected state (the phone may just be busy); only
        `REFRESH_FAILURES_OFFLINE` in a row show it as offline.
        """
        connection.refresh_at = now + REFRESH_S
        try:
            info = self._info(connection.session.transport)
        except TransportError as e:
            connection.failures += 1
            logger.info("Refreshing the phone's details failed: %s", e)
            if connection.failures >= REFRESH_FAILURES_OFFLINE:
                self._read_failed(connection.serial, now, e)
            return
        connection.failures = 0
        if self._is_stopping():
            return
        stored = self._remember(info, now, touch=False)
        if stored is None:
            self._set_connection(None)
            self._set(self._wrong_device(connection.serial, info))
            return
        connection.session.identity = stored.identity
        self._set(self._connected(connection.serial, info, connection.link_mbps, stored.uuid))

    def _read_failed(self, serial: str, now: float, error: TransportError) -> None:
        logger.info("Could not read the phone's details: %s", error)
        no_identity = isinstance(error, RemoteError) and str(error) == NO_IDENTITY_MESSAGE
        message = NO_IDENTITY_MESSAGE if no_identity else NOT_ANSWERING_MESSAGE
        self._resolved = None
        self._set_connection(None)
        self._failed = serial
        self._failed_retry_at = now + self._failed_backoff.next()
        self._set(DeviceStatus(ConnectionState.offline, message, serial))

    @staticmethod
    def _wrong_device(serial: str, info: DeviceInfo) -> DeviceStatus:
        return DeviceStatus(
            ConnectionState.wrong_device,
            WRONG_DEVICE_MESSAGE,
            serial,
            model=info.model,
            android_version=info.android_version,
        )

    @staticmethod
    def _connected(
        serial: str, info: DeviceInfo, link: int | None, device_uuid: str
    ) -> DeviceStatus:
        wifi = is_wifi_serial(serial)
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

    def _stored_phone(self) -> _StoredPhone | None:
        """The stored phone, read once (and again after a Retry)."""
        if not self._stored_loaded:
            with self._ctx.db.read() as conn:
                row = conn.execute(_STORED_PHONE_SQL).fetchone()
            self._stored = self._stored_from_row(row)
            self._stored_loaded = True
        return self._stored

    @staticmethod
    def _stored_from_row(row: Any) -> _StoredPhone | None:
        """A stored phone from a `(uuid, serial, android_id)` row."""
        if row is None:
            return None
        key = str(row[1])
        android_id = str(row[2]) if row[2] else None
        if android_id is None:
            identity = PhoneIdentity.from_stored_key(key)  # an older row: the key is both
        elif key == android_id:
            identity = PhoneIdentity(None, android_id)  # stored by Android ID: it had no serial
        else:
            identity = PhoneIdentity(key or None, android_id)
        return _StoredPhone(str(row[0]), identity, android_id)

    def _remember(self, info: DeviceInfo, now: float, *, touch: bool) -> _StoredPhone | None:
        """Store or refresh the phone; the stored phone, or None if another phone is stored.

        Until the owner picks a phone (the wizard, or Settings' switch: `choose_phone`), the
        first phone ever seen (the row with the lowest id) is the phone Sift backs up. A reading
        is that phone when either of its identifiers (serial, Android ID) equals one of the
        stored phone's. With `touch` (the phone just appeared) its row is updated at once; on
        refreshes at most every `LAST_SEEN_EVERY_S`.
        """
        seen = PhoneIdentity.from_info(info)
        stored = self._stored_phone()
        if stored is not None:
            if not stored.identity.matches(seen):
                return None
            stored.identity = stored.identity.updated_with(seen)
            new_android_id = stored.identity.android_id
            android_changed = (
                new_android_id is not None and new_android_id != stored.saved_android_id
            )
            due = self._seen_written_at is None or now - self._seen_written_at >= LAST_SEEN_EVERY_S
            if touch or due or android_changed:
                with self._ctx.db.tx() as conn:
                    if touch or due:
                        conn.execute(
                            "UPDATE device SET model = ?, android_version = ?, last_seen_at = ? "
                            "WHERE uuid = ?",
                            (
                                info.model,
                                info.android_version,
                                int(self._wall_clock()),
                                stored.uuid,
                            ),
                        )
                        self._seen_written_at = now
                    if android_changed:
                        # A new or changed Android ID is saved at once, not on the slow schedule.
                        conn.execute(
                            "UPDATE device SET android_id = ? WHERE uuid = ?",
                            (new_android_id, stored.uuid),
                        )
                        stored.saved_android_id = new_android_id
            return stored
        with self._ctx.db.tx() as conn:
            row = conn.execute(_STORED_PHONE_SQL).fetchone()
            if row is None:
                new_uuid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO device "
                    "(uuid, serial, android_id, model, android_version, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        new_uuid,
                        seen.key(),
                        seen.android_id,
                        info.model,
                        info.android_version,
                        int(self._wall_clock()),
                    ),
                )
                stored = _StoredPhone(new_uuid, seen, seen.android_id)
            else:
                # Another process stored a phone first: it is the stored one.
                stored = self._stored_from_row(row)
        self._stored = stored
        self._stored_loaded = True
        self._seen_written_at = now
        return stored if stored is not None and stored.identity.matches(seen) else None

    def _set_connection(self, connection: _Connection | None) -> None:
        """Replace the connected phone, and with it the session `session()` hands out."""
        self._connection = connection
        with self._lock:
            self._session = connection.session if connection is not None else None

    def _forget_phone(self) -> None:
        """Read the phone afresh next time, and restart the held-by-another-tool timer."""
        self._resolved = self._failed = None
        self._set_connection(None)
        self._failed_backoff.reset()
        self._usb_since = self._usb_polled_at = None

    def _set(self, status: DeviceStatus) -> None:
        with self._lock:
            if self._stopping or status == self._status:
                return  # once stop() was called nothing more is published
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

    `ensure_server()` attaches to a server that answers (no adb program is run), or starts one
    when nothing listens on the port; it is called each time the device list is (re)opened.
    """
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
