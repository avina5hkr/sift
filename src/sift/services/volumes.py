"""List the mounted external volumes, read-only, for the wizard's SSD step.

Two `diskutil` queries and nothing else: `diskutil list -plist external` (which disks are
external) and `diskutil info -plist <id>` for each mounted volume on them (name, file system,
free space, UUID). Nothing is mounted, unmounted, ejected or written, and no file on a volume
is opened. A volume that cannot be read is left out rather than guessed at.
"""

import logging
import plistlib
import subprocess
from dataclasses import dataclass
from typing import Any

from sift.core.proc import Runner, subprocess_runner

logger = logging.getLogger("sift.services")

_DISKUTIL = "/usr/sbin/diskutil"
_TIMEOUT_S = 15.0
GOOD_FILE_SYSTEMS = frozenset({"exfat", "apfs"})


@dataclass(frozen=True)
class Volume:
    name: str
    file_system: str  # as diskutil names it ("ExFAT", "APFS", "MS-DOS FAT32", ...)
    uuid: str
    free_bytes: int | None
    total_bytes: int | None
    suitable: bool  # exFAT or APFS: holds big files and is readable by a Mac
    warning: str | None  # plain words, set when it is not suitable


def _plist(runner: Runner, *args: str) -> dict[str, Any] | None:
    try:
        done = runner([_DISKUTIL, *args], _TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("diskutil %s failed: %s", args[0], e)
        return None
    if done.returncode != 0:
        logger.warning("diskutil %s exited with %s", args[0], done.returncode)
        return None
    try:
        data = plistlib.loads(done.stdout.encode())
    except (plistlib.InvalidFileException, ValueError, UnicodeError) as e:
        logger.warning("diskutil %s gave unreadable output: %s", args[0], e)
        return None
    return data if isinstance(data, dict) else None


def _mounted_ids(listing: dict[str, Any]) -> list[str]:
    """Device identifiers of every mounted volume on an external disk."""
    found: list[str] = []
    for disk in listing.get("AllDisksAndPartitions", []):
        if not isinstance(disk, dict):
            continue
        for item in (disk, *disk.get("Partitions", []), *disk.get("APFSVolumes", [])):
            if isinstance(item, dict) and item.get("MountPoint") and item.get("DeviceIdentifier"):
                found.append(str(item["DeviceIdentifier"]))
    return list(dict.fromkeys(found))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _volume(info: dict[str, Any]) -> Volume | None:
    uuid = info.get("VolumeUUID")
    if not uuid or info.get("Internal") is True or not info.get("MountPoint"):
        return None
    fs_key = str(info.get("FilesystemType", "")).casefold()
    fs_name = str(info.get("FilesystemName") or info.get("FilesystemType") or "unknown")
    name = str(info.get("VolumeName") or "").strip() or "Untitled"
    free = _int(info.get("FreeSpace"))
    if free is None:
        free = _int(info.get("APFSContainerFree"))
    suitable = fs_key in GOOD_FILE_SYSTEMS
    return Volume(
        name=name,
        file_system=fs_name,
        uuid=str(uuid),
        free_bytes=free,
        total_bytes=_int(info.get("TotalSize")) or _int(info.get("Size")),
        suitable=suitable,
        warning=None
        if suitable
        else f"This drive is formatted as {fs_name}. Sift works best with exFAT or APFS: "
        "other formats cannot hold large videos or are slow and unsafe for backups.",
    )


def list_external_volumes(runner: Runner = subprocess_runner) -> list[Volume]:
    """Mounted external volumes, by name. Empty when there are none or diskutil fails."""
    listing = _plist(runner, "list", "-plist", "external")
    if listing is None:
        return []
    volumes: dict[str, Volume] = {}
    for ident in _mounted_ids(listing):
        info = _plist(runner, "info", "-plist", ident)
        volume = _volume(info) if info is not None else None
        if volume is not None:
            volumes.setdefault(volume.uuid, volume)
    return sorted(volumes.values(), key=lambda v: (v.name.casefold(), v.uuid))
