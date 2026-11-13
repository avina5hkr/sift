"""External volumes from synthetic `diskutil` plists. No real diskutil, mount or disk is touched."""

import plistlib
import subprocess
from collections.abc import Sequence

from sift.services.volumes import list_external_volumes

LISTING = {
    "AllDisks": ["disk4", "disk4s1", "disk5", "disk5s1", "disk6"],
    "AllDisksAndPartitions": [
        {
            "DeviceIdentifier": "disk4",
            "Content": "GUID_partition_scheme",
            "Partitions": [
                {"DeviceIdentifier": "disk4s1", "MountPoint": "/Volumes/Backup SSD"},
                {"DeviceIdentifier": "disk4s2"},  # not mounted
            ],
        },
        {
            "DeviceIdentifier": "disk5",
            "Partitions": [{"DeviceIdentifier": "disk5s1", "MountPoint": "/Volumes/Old"}],
        },
        {  # an APFS container: its volumes carry the mount points
            "DeviceIdentifier": "disk6",
            "APFSVolumes": [{"DeviceIdentifier": "disk6s1", "MountPoint": "/Volumes/Fast"}],
        },
    ],
}

INFO = {
    "disk4s1": {
        "VolumeName": "Backup SSD",
        "FilesystemType": "exfat",
        "FilesystemName": "ExFAT",
        "VolumeUUID": "11111111-2222-3333-4444-555555555555",
        "FreeSpace": 700_000_000_000,
        "TotalSize": 1_000_000_000_000,
        "MountPoint": "/Volumes/Backup SSD",
        "Internal": False,
    },
    "disk5s1": {
        "VolumeName": "Old",
        "FilesystemType": "msdos",
        "FilesystemName": "MS-DOS FAT32",
        "VolumeUUID": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
        "FreeSpace": 5_000_000_000,
        "TotalSize": 32_000_000_000,
        "MountPoint": "/Volumes/Old",
        "Internal": False,
    },
    "disk6s1": {
        "VolumeName": "Fast",
        "FilesystemType": "apfs",
        "FilesystemName": "APFS",
        "VolumeUUID": "99999999-8888-7777-6666-555555555555",
        "APFSContainerFree": 300_000_000_000,
        "TotalSize": 500_000_000_000,
        "MountPoint": "/Volumes/Fast",
        "Internal": False,
    },
}


class Diskutil:
    def __init__(self, listing: object = LISTING, info: dict[str, object] | None = None) -> None:
        self.listing = listing
        self.info = INFO if info is None else info
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        assert args[0].endswith("diskutil")
        if args[1:] == ["list", "-plist", "external"]:
            data = self.listing
        else:
            assert args[1:3] == ["info", "-plist"]
            if args[3] not in self.info:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            data = self.info[args[3]]
        return subprocess.CompletedProcess(list(args), 0, plistlib.dumps(data).decode(), "")


def test_lists_mounted_external_volumes_with_file_system_free_space_and_uuid() -> None:
    volumes = list_external_volumes(Diskutil())
    by_name = {v.name: v for v in volumes}
    assert [v.name for v in volumes] == ["Backup SSD", "Fast", "Old"]
    ssd = by_name["Backup SSD"]
    assert (ssd.file_system, ssd.uuid, ssd.free_bytes, ssd.total_bytes) == (
        "ExFAT",
        "11111111-2222-3333-4444-555555555555",
        700_000_000_000,
        1_000_000_000_000,
    )
    assert ssd.suitable and ssd.warning is None
    assert by_name["Fast"].suitable and by_name["Fast"].free_bytes == 300_000_000_000


def test_warns_when_the_file_system_is_not_exfat_or_apfs() -> None:
    old = next(v for v in list_external_volumes(Diskutil()) if v.name == "Old")
    assert not old.suitable
    assert old.warning is not None and "MS-DOS FAT32" in old.warning and "exFAT" in old.warning


def test_only_read_only_diskutil_queries_are_made() -> None:
    runner = Diskutil()
    list_external_volumes(runner)
    verbs = {call[1] for call in runner.calls}
    assert verbs == {"list", "info"}
    assert all("-plist" in call for call in runner.calls)
    assert not any(
        verb in call for call in runner.calls for verb in ("mount", "unmount", "eject", "erase")
    )
    assert [c for c in runner.calls if c[1] == "info"] == [
        [runner.calls[1][0], "info", "-plist", ident] for ident in ("disk4s1", "disk5s1", "disk6s1")
    ]


def test_an_internal_or_unreadable_volume_is_left_out() -> None:
    info = {k: dict(v) for k, v in INFO.items()}
    info["disk5s1"]["Internal"] = True
    del info["disk6s1"]  # diskutil info fails for it
    names = [v.name for v in list_external_volumes(Diskutil(info=info))]
    assert names == ["Backup SSD"]


def test_a_volume_without_a_uuid_is_left_out() -> None:
    info = {k: dict(v) for k, v in INFO.items()}
    del info["disk4s1"]["VolumeUUID"]
    assert "Backup SSD" not in [v.name for v in list_external_volumes(Diskutil(info=info))]


def test_no_external_disks_gives_an_empty_list() -> None:
    assert list_external_volumes(Diskutil(listing={"AllDisksAndPartitions": []})) == []


def test_garbage_or_a_failing_diskutil_gives_an_empty_list() -> None:
    def garbage(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 0, "not a plist", "")

    def broken(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise OSError("no diskutil")

    def timeout(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(args), 1.0)

    assert list_external_volumes(garbage) == []
    assert list_external_volumes(broken) == []
    assert list_external_volumes(timeout) == []


def test_a_name_with_odd_characters_is_kept_as_text() -> None:
    info = {k: dict(v) for k, v in INFO.items()}
    info["disk4s1"]["VolumeName"] = "  "
    volumes = list_external_volumes(Diskutil(info=info))
    assert "Untitled" in [v.name for v in volumes]
