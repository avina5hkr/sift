"""DeviceSession: the one connection to the stored phone that everything shares.

The `DeviceTracker` owns one session per connected stored phone and hands it out with
`DeviceTracker.session()`. Every user of the phone (the tracker's own refreshes, scans, copies,
checks) goes through `session.transport`, so they all draw from the same pool of connection
slots (`Limits.max_connections`) instead of each opening its own and starving the others.

Identity has two parts, `ro.serialno` and the Android ID, because either one can be missing or
change on its own (a phone with no serial number; a factory reset that makes a new Android ID).
Two readings are the same phone when either non-empty part is equal. A session keeps the
identity it was opened with; `check_identity()` reads the phone again and raises `WrongDevice`
if another phone has taken its place (a different phone plugged in under the same adb serial,
or the cable moved between phones while a job ran).

Where the stored identity lives: the `device` table has only the `serial` column for now (the
tracker keeps the Android ID in memory next to it, in its stored-phone record); the migration
that adds `android_id` to the table, and writes it, is a later step.
"""

from dataclasses import dataclass

from sift.device.transport import (
    NO_IDENTITY_MESSAGE,
    DeviceInfo,
    RemoteError,
    Transport,
    TransportError,
)


class WrongDevice(TransportError):
    """The phone on the other end is not the one this session was opened for."""


@dataclass(frozen=True)
class PhoneIdentity:
    """A phone's two identifiers; either may be None (but a real phone has at least one)."""

    serial: str | None
    android_id: str | None

    @classmethod
    def from_info(cls, info: DeviceInfo) -> PhoneIdentity:
        return cls(info.serial or None, info.android_id or None)

    @classmethod
    def from_stored_key(cls, key: str) -> PhoneIdentity:
        """The identity of a phone known only by the single key an older database holds.

        That key is `ro.serialno`, or the Android ID when the phone had no serial number, so
        it may be either; it is therefore offered as both until the phone is seen again.
        """
        return cls(key or None, key or None)

    def is_empty(self) -> bool:
        return not self.serial and not self.android_id

    def matches(self, other: PhoneIdentity) -> bool:
        """True when a non-empty serial or a non-empty Android ID is equal."""
        if self.serial and self.serial == other.serial:
            return True
        return bool(self.android_id) and self.android_id == other.android_id

    def updated_with(self, newer: PhoneIdentity) -> PhoneIdentity:
        """This identity with any part `newer` reports replaced (a part it lacks is kept)."""
        return PhoneIdentity(newer.serial or self.serial, newer.android_id or self.android_id)

    def key(self) -> str:
        """The single value the `device.serial` column holds: the serial, else the Android ID."""
        return self.serial or self.android_id or ""


class DeviceSession:
    """The stored phone, connected: its transport, identity and `device.uuid`."""

    def __init__(self, transport: Transport, identity: PhoneIdentity, device_uuid: str) -> None:
        self.transport = transport
        self.identity = identity
        self.device_uuid = device_uuid

    def read_info(self) -> DeviceInfo:
        """Probe the phone once and check it is still the same phone.

        Raises `WrongDevice` when it is not. A phone that reports no identity at all raises
        `RemoteError` (as `Transport.info` does). When it is the same phone, any identifier it
        newly reports is added to `identity`.
        """
        info = self.transport.info()
        seen = PhoneIdentity.from_info(info)
        if seen.is_empty():
            raise RemoteError(NO_IDENTITY_MESSAGE)
        if not self.identity.matches(seen):
            raise WrongDevice("A different phone answered on this connection.")
        self.identity = self.identity.updated_with(seen)
        return info

    def check_identity(self) -> None:
        """Re-read the phone's identity; raise `WrongDevice` if it changed."""
        self.read_info()
