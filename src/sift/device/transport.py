"""Transport interface: how Sift talks to a phone, independent of the mechanism."""

from collections.abc import Iterator
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


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    model: str
    android_version: str
    battery_pct: int | None
    free_bytes: int | None
    total_bytes: int | None
    screen_sizes: tuple[tuple[int, int], ...]  # physical and override from `wm size`


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

    `path` names the remote path the error is about, when there is one.
    """

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


@runtime_checkable
class Transport(Protocol):
    """How Sift talks to one phone.

    All remote paths are `str`, decoded from the phone's bytes with `surrogateescape`; encode
    them back with `path.encode("utf-8", "surrogateescape")` when sending them to the phone.
    Timestamps are plain Unix seconds (ints, possibly 0, negative or beyond 2**31).
    """

    def info(self) -> DeviceInfo: ...

    def walk(self, root: str) -> Iterator[RemoteEntry]:
        """Depth-first listing of everything under `root`.

        Yields files and symlinks (not directories). A symlink is yielded as an entry with
        `is_symlink=True` and the link's own size and mtime, and is never descended into.
        Raises `RemoteError` naming the path if a subdirectory cannot be listed: it fails
        loudly rather than skipping.
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

    def open_read(self, path: str, offset: int = 0) -> BinaryIO:
        """A stream of the file's bytes starting at byte `offset`; the caller must close it."""
        ...

    def sha256(self, paths: list[str]) -> dict[str, str | None]:
        """Hex SHA-256 of each file, computed on the phone; None if missing or unreadable."""
        ...

    def delete(self, path: str) -> None: ...

    def push(self, local: Path, remote: str, mtime: int) -> None: ...

    def media_rescan(self, paths: list[str]) -> bool:
        """Ask the phone to rescan `paths`; True if the phone accepted the request."""
        ...

    def link_health(self) -> LinkHealth: ...
