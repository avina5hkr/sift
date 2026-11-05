"""Transport interface: how Sift talks to a phone, independent of the mechanism."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol


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
    """The phone answered with an error (e.g. ENOENT, EACCES)."""


class Transport(Protocol):
    def info(self) -> DeviceInfo: ...

    def walk(self, root: str) -> Iterator[RemoteEntry]:
        """Depth-first listing under root; never follows symlinks."""
        ...

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """One level only (for shallow_paths)."""
        ...

    def stat(self, path: str) -> RemoteEntry | None: ...

    def open_read(self, path: str, offset: int = 0) -> BinaryIO: ...

    def sha256(self, paths: list[str]) -> dict[str, str | None]: ...

    def delete(self, path: str) -> None: ...

    def push(self, local: Path, remote: str, mtime: int) -> None: ...

    def media_rescan(self, paths: list[str]) -> bool: ...

    def link_health(self) -> LinkHealth: ...
