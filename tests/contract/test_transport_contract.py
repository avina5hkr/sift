"""Behaviour every Transport must have: the fake always, a real phone with -m realphone."""

import hashlib

import pytest

from sift.device.fake_phone import (
    ODD_NAME_FILES,
    SYMLINK,
    ZERO_BYTE_FILE,
)
from sift.device.transport import RemoteEntry, RemoteError, Transport

CANDIDATE_ROOTS = [
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Download",
    "/sdcard/Recordings",
    "/sdcard/WhatsApp",
    "/sdcard/Android/media",
]
MISSING = "/sdcard/definitely/not/here.bin"
MAX_SAMPLE_BYTES = 1_000_000


def roots_of(transport: Transport) -> list[str]:
    roots = []
    for root in CANDIDATE_ROOTS:
        entry = transport.stat(root)
        if entry is not None and entry.is_dir:
            roots.append(root)
    if not roots:
        pytest.skip("none of the standard folders exist on this phone")
    return roots


def walk_all(transport: Transport) -> list[RemoteEntry]:
    entries: list[RemoteEntry] = []
    for root in roots_of(transport):
        entries.extend(transport.walk(root))
    return entries


def sample_files(transport: Transport, *, min_size: int = 0, limit: int = 10) -> list[RemoteEntry]:
    found = [
        e
        for e in walk_all(transport)
        if not e.is_symlink and min_size <= e.size <= MAX_SAMPLE_BYTES
    ]
    if not found:
        pytest.skip("no suitable file on this phone")
    return found[:limit]


def oracle_files(transport: Transport, root: str) -> set[str]:
    """Independent listing: recurse with list_dir, skip symlinks and unreadable folders."""
    found: set[str] = set()
    pending = [root]
    while pending:
        here = pending.pop()
        try:
            children = transport.list_dir(here)
        except RemoteError:
            continue
        for child in children:
            if child.is_symlink:
                found.add(child.path)
            elif child.is_dir:
                pending.append(child.path)
            else:
                found.add(child.path)
    return found


def test_walk_yields_every_file_exactly_once_matching_stat(transport: Transport) -> None:
    entries = walk_all(transport)
    paths = [e.path for e in entries]
    assert len(paths) == len(set(paths))
    assert all(not e.is_dir for e in entries)
    for entry in entries[:500]:
        assert transport.stat(entry.path) == entry
    expected: set[str] = set()
    for root in roots_of(transport):
        expected |= oracle_files(transport, root)
    assert set(paths) == expected


def test_walk_is_depth_first(transport: Transport) -> None:
    """Everything under a folder comes out as one contiguous run."""
    for root in roots_of(transport):
        paths = [e.path for e in transport.walk(root)][:2000]
        folders = {p.rsplit("/", 1)[0] for p in paths}
        for folder in folders:
            idx = [i for i, p in enumerate(paths) if p.startswith(folder + "/")]
            assert idx[-1] - idx[0] + 1 == len(idx), folder


def test_symlinks_are_reported_and_never_followed(transport: Transport) -> None:
    if transport.stat(SYMLINK) is None:
        pytest.skip("no known symlink on this phone")
    parent = SYMLINK.rsplit("/", 1)[0]
    entries = list(transport.walk(parent))
    links = [e for e in entries if e.path == SYMLINK]
    assert len(links) == 1
    assert links[0].is_symlink
    assert not links[0].is_dir
    assert not any(e.path.startswith(SYMLINK + "/") for e in entries)
    stat = transport.stat(SYMLINK)
    assert stat is not None
    assert stat.is_symlink
    assert stat == links[0]


def test_list_dir_is_one_level_only(transport: Transport) -> None:
    root = roots_of(transport)[0]
    entries = transport.list_dir(root)
    assert entries
    for entry in entries:
        assert entry.path.rsplit("/", 1)[0] == root
    deep = {e.path for e in transport.walk(root)}
    direct_files = {e.path for e in entries if not e.is_dir}
    assert direct_files <= deep
    assert len(entries) < len(deep) or all(not e.is_dir for e in entries)


def test_list_dir_loose_files_in_storage_root(transport: Transport) -> None:
    entries = transport.list_dir("/sdcard")
    assert any(e.is_dir for e in entries)
    assert all(e.path.rsplit("/", 1)[0] == "/sdcard" for e in entries)


def test_open_read_matches_stat_size(transport: Transport) -> None:
    for entry in sample_files(transport):
        stream = transport.open_read(entry.path)
        try:
            data = stream.read()
        finally:
            stream.close()
        assert len(data) == entry.size
        stat = transport.stat(entry.path)
        assert stat is not None
        assert len(data) == stat.size


def test_open_read_of_zero_byte_file(transport: Transport) -> None:
    if transport.stat(ZERO_BYTE_FILE) is None:
        pytest.skip("no known zero-byte file on this phone")
    stream = transport.open_read(ZERO_BYTE_FILE)
    try:
        assert stream.read() == b""
    finally:
        stream.close()


def test_open_read_from_offset_returns_the_tail(transport: Transport) -> None:
    for entry in sample_files(transport, min_size=16, limit=5):
        with transport.open_read(entry.path) as stream:
            whole = stream.read()
        for offset in (0, 1, 7, entry.size - 1, entry.size):
            with transport.open_read(entry.path, offset=offset) as stream:
                assert stream.read() == whole[offset:]


def test_open_read_supports_chunked_reads(transport: Transport) -> None:
    entry = sample_files(transport, min_size=16, limit=1)[0]
    with transport.open_read(entry.path) as stream:
        whole = stream.read()
    with transport.open_read(entry.path) as stream:
        chunks = []
        while chunk := stream.read(7):
            assert len(chunk) <= 7
            chunks.append(chunk)
    assert b"".join(chunks) == whole


def test_open_read_of_missing_path_raises_remote_error_naming_it(transport: Transport) -> None:
    with pytest.raises(RemoteError) as info:
        transport.open_read(MISSING)
    assert info.value.path == MISSING


def test_list_dir_of_missing_path_raises_remote_error_naming_it(transport: Transport) -> None:
    with pytest.raises(RemoteError) as info:
        transport.list_dir(MISSING)
    assert info.value.path == MISSING


def test_sha256_is_correct_and_none_for_missing_paths(transport: Transport) -> None:
    files = sample_files(transport, limit=3)
    paths = [e.path for e in files] + [MISSING]
    result = transport.sha256(paths)
    assert set(result) == set(paths)
    assert result[MISSING] is None
    for entry in files:
        with transport.open_read(entry.path) as stream:
            assert result[entry.path] == hashlib.sha256(stream.read()).hexdigest()


def test_sha256_of_a_directory_is_none(transport: Transport) -> None:
    root = roots_of(transport)[0]
    assert transport.sha256([root]) == {root: None}


def test_stat_of_missing_path_is_none(transport: Transport) -> None:
    assert transport.stat(MISSING) is None


def test_stat_of_a_directory(transport: Transport) -> None:
    root = roots_of(transport)[0]
    entry = transport.stat(root)
    assert entry is not None
    assert entry.is_dir
    assert not entry.is_symlink
    assert entry.path == root


def test_odd_names_survive_walk_and_stat(transport: Transport) -> None:
    present = [p for p in ODD_NAME_FILES if transport.stat(p) is not None]
    if not present:
        pytest.skip("no known odd-name files on this phone")
    parent = present[0].rsplit("/", 1)[0]
    listed = {e.path for e in transport.walk(parent)}
    for path in present:
        assert path in listed
        entry = transport.stat(path)
        assert entry is not None
        assert entry.path == path


def test_info_fields_are_populated(transport: Transport) -> None:
    info = transport.info()
    assert info.serial
    assert info.model
    assert info.android_version
    assert info.battery_pct is None or 0 <= info.battery_pct <= 100
    assert info.total_bytes is None or info.total_bytes > 0
    assert info.free_bytes is None or info.free_bytes >= 0
    assert isinstance(info.screen_sizes, tuple)
    assert all(w > 0 and h > 0 for w, h in info.screen_sizes)


def test_link_health_is_reported(transport: Transport) -> None:
    health = transport.link_health()
    assert health.kind in ("usb", "wifi")
