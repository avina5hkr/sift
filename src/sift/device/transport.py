"""Transport interface: how Sift talks to a phone, independent of the mechanism."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class RemoteEntry:
    path: str  # absolute, as on the phone (decoded with surrogateescape)
    size: int  # 64-bit
    mtime: int  # Unix seconds UTC
    is_dir: bool
    is_symlink: bool


# What `info()` raises (as RemoteError) when the phone has neither identity.
NO_IDENTITY_MESSAGE = (
    "The phone did not report a serial number or an Android ID, so Sift cannot tell which "
    "phone it is."
)


@dataclass(frozen=True)
class DeviceInfo:
    """What one probe of the phone reports. At least one of `serial` and `android_id` is set.

    The two identifiers are kept apart (see `sift.device.session.PhoneIdentity`): a phone is the
    same phone when either one is the same.
    """

    serial: str | None  # ro.serialno; None when the phone reports none
    model: str
    android_version: str
    battery_pct: int | None
    free_bytes: int | None
    total_bytes: int | None
    screen_sizes: tuple[tuple[int, int], ...]  # physical and override from `wm size`
    android_id: str | None = None  # `settings get secure android_id`; None when not reported
    phone_time: int | None = None  # the phone's clock, Unix seconds (`date +%s`), when it said


@dataclass(frozen=True)
class LinkHealth:
    kind: Literal["usb", "wifi"]
    speed_mbps: int | None
    latency_ms: float | None


class TransportError(Exception):
    """Base class for all transport failures."""


class DeviceDisconnected(TransportError):
    """The device went away (unplugged, adb server died, authorisation revoked)."""


class DeviceTimeout(TransportError):
    """The device did not answer in time."""


class RemoteError(TransportError):
    """The phone answered with an error (e.g. ENOENT, EACCES).

    `path` names the remote path the error is about, when there is one. `errno` is the phone's
    Linux errno for it (ENOENT is 2, EACCES is 13) when the phone reported one, else None.
    """

    def __init__(self, message: str, path: str | None = None, *, errno: int | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.errno = errno


class FileChanged(RemoteError):
    """The file changed (shrank, grew or went away) while it was being read.

    Not a disconnect: the phone is still there and the file can be read again from the start.
    The message is "<path> changed while it was being read." and `path` names the file.
    """


@runtime_checkable
class Transport(Protocol):
    """How Sift talks to one phone.

    All remote paths are `str`, decoded from the phone's bytes with `surrogateescape`; encode
    them back with `path.encode("utf-8", "surrogateescape")` when sending them to the phone.
    Timestamps are plain Unix seconds (ints, possibly 0, negative or beyond 2**31).
    """

    def info(self) -> DeviceInfo:
        """Model, Android version, storage, battery, the phone's clock and its identity.

        `serial` is `ro.serialno` and `android_id` the Android ID; either may be None, but not
        both: `RemoteError` (with `NO_IDENTITY_MESSAGE`) is raised when the phone reports
        neither. All of it is read in one request to the phone.
        """
        ...

    def walk(
        self,
        root: str,
        on_error: Callable[[RemoteError], None] | None = None,
        prune: Callable[[str], bool] | None = None,
    ) -> Iterator[RemoteEntry]:
        """Depth-first listing of everything under `root`.

        Yields files and symlinks (not directories). A symlink is yielded as an entry with
        `is_symlink=True` and the link's own size and mtime, and is never descended into.
        Without `on_error`, a subdirectory that cannot be listed (or an entry the phone cannot
        look at) raises `RemoteError` naming the path: it fails loudly rather than skipping.
        With `on_error`, that `RemoteError` is passed to it instead and the folder or entry is
        skipped; the walk goes on. `root` itself must be listable either way, or the walk
        raises. (Some phones list a folder they refuse to read as empty, without any error.)
        With `prune`, each folder below `root` is offered to it (by path) before it is listed;
        a folder it returns True for is not listed, and nothing in it is yielded or reported.
        """
        ...

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """One level only (for shallow_paths), directories included.

        Raises `RemoteError` naming the path if it cannot be listed.
        """
        ...

    def stat(self, path: str) -> RemoteEntry | None:
        """The entry for `path` (a symlink is reported as itself), or None if it is missing."""
        ...

    def open_read(
        self, path: str, offset: int = 0, *, entry: RemoteEntry | None = None
    ) -> BinaryIO:
        """A stream of the file's bytes starting at byte `offset`; the caller must close it.

        `entry` is what a recent `walk`, `list_dir` or `stat` returned for this path. When given
        it is trusted as the file's size at open (no extra look at the phone); a file that has
        changed since then ends short or long of it, which is reported as for any change.

        Like any raw stream, `read(n)` may return fewer than `n` bytes before the end: loop
        until it returns b"". `read()` with no size returns everything that is left.

        A read raises `FileChanged` (naming the path) when the file ends short of the size it
        had when it was opened, or goes away, while the phone is still answering: another app
        changed it, so read it again from the start. Losing the phone mid-read raises
        `DeviceDisconnected` instead.
        """
        ...

    def sha256(self, paths: list[str]) -> dict[str, str | None]:
        """Hex SHA-256 of each file, computed on the phone; None if missing or unreadable."""
        ...

    def delete(self, path: str) -> None: ...

    def push(self, local: Path, remote: str, mtime: int) -> None: ...

    def remove_dir(self, path: str) -> None:
        """Remove an empty folder (`rmdir`, never recursive); RemoteError if it is not empty."""
        ...

    def media_rescan(self, paths: list[str]) -> bool:
        """Ask the phone to rescan `paths`; True if the phone accepted the request."""
        ...

    def link_health(self) -> LinkHealth: ...
