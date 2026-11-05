"""The fake phone and its tree builder: edge cases the later milestones lean on."""

import hashlib
import time

import pytest

from sift.device.fake_phone import (
    BIG_VIDEO,
    BIG_VIDEO_SIZE,
    KNOWN_DATA,
    KNOWN_FILE,
    NEWLINE_FILE,
    NON_UTF8_FILE,
    ODD_MTIME_FILES,
    SYMLINK,
    UNREADABLE_DIR,
    ZERO_BYTE_FILE,
    FakePhone,
    build_fake_phone,
    lazy_pattern,
)
from sift.device.fake_transport import FakeTransport
from sift.device.transport import RemoteError, Transport


def test_fake_transport_satisfies_the_transport_protocol() -> None:
    assert isinstance(FakeTransport(FakePhone()), Transport)


def test_fake_size_over_4gib_stat_walk_and_offset_read() -> None:
    transport = FakeTransport(build_fake_phone())
    assert BIG_VIDEO_SIZE > 2**32 + 1024
    entry = transport.stat(BIG_VIDEO)
    assert entry is not None
    assert entry.size == BIG_VIDEO_SIZE
    parent = BIG_VIDEO.rsplit("/", 1)[0]
    walked = {e.path: e for e in transport.walk(parent)}
    assert walked[BIG_VIDEO].size == BIG_VIDEO_SIZE
    assert transport.list_dir(parent).count(entry) == 1

    offset = 2**32 + 5
    pattern = lazy_pattern(BIG_VIDEO, BIG_VIDEO_SIZE)
    expected = bytes(pattern[(offset + i) % len(pattern)] for i in range(32))
    started = time.perf_counter()
    with transport.open_read(BIG_VIDEO, offset=offset) as stream:
        assert stream.read(32) == expected
        assert stream.read(32) == bytes(
            pattern[(offset + 32 + i) % len(pattern)] for i in range(32)
        )
    assert time.perf_counter() - started < 0.5  # O(read size), not O(offset)

    # The last bytes: reading from the final offset yields exactly the remainder.
    with transport.open_read(BIG_VIDEO, offset=BIG_VIDEO_SIZE - 10) as stream:
        tail = stream.read()
    assert len(tail) == 10


def test_fake_odd_and_zero_mtimes() -> None:
    transport = FakeTransport(build_fake_phone())
    assert sorted(ODD_MTIME_FILES.values()) == [-31_536_000, 0, 2**31 + 1, 2**40]
    for path, mtime in ODD_MTIME_FILES.items():
        stat = transport.stat(path)
        assert stat is not None
        assert stat.mtime == mtime
        parent = path.rsplit("/", 1)[0]
        assert {e.path: e.mtime for e in transport.walk(parent)}[path] == mtime
        assert {e.path: e.mtime for e in transport.list_dir(parent)}[path] == mtime


def test_lazy_bytes_are_deterministic_and_consistent_across_ranges() -> None:
    phone = FakePhone()
    phone.add_file("/sdcard/a.bin", size=300_000)
    phone.add_file("/sdcard/b.bin", size=300_000)
    whole = phone.read_range("/sdcard/a.bin", 0, 300_000)
    assert len(whole) == 300_000
    assert whole == phone.read_range("/sdcard/a.bin", 0, 300_000)
    assert whole != phone.read_range("/sdcard/b.bin", 0, 300_000)
    assert phone.read_range("/sdcard/a.bin", 70_001, 5_000) == whole[70_001:75_001]
    assert phone.read_range("/sdcard/a.bin", 299_990, 100) == whole[299_990:]
    assert phone.read_range("/sdcard/a.bin", 300_000, 10) == b""
    assert phone.sha256_of("/sdcard/a.bin") == hashlib.sha256(whole).hexdigest()


@pytest.mark.slow
def test_sha256_of_a_moderate_lazy_file_is_correct() -> None:
    size = 50 * 1024 * 1024 + 3
    phone = FakePhone()
    phone.add_file("/sdcard/big.bin", size=size)
    pattern = lazy_pattern("/sdcard/big.bin", size)
    expected = (pattern * (size // len(pattern) + 1))[:size]
    transport = FakeTransport(phone)
    assert transport.sha256(["/sdcard/big.bin"]) == {
        "/sdcard/big.bin": hashlib.sha256(expected).hexdigest()
    }


def test_build_fake_phone_is_fast_and_deterministic() -> None:
    started = time.perf_counter()
    phone = build_fake_phone(200)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    again = build_fake_phone(200)
    assert sorted(phone.files) == sorted(again.files)
    assert {p: f.mtime for p, f in phone.files.items()} == {
        p: f.mtime for p, f in again.files.items()
    }
    other = build_fake_phone(200, seed=1)
    assert sorted(other.files) != sorted(phone.files)
    assert len(build_fake_phone(0).files) > 20  # the fixed edge cases are always there
    assert len(build_fake_phone(1000).files) > len(phone.files)


def test_build_fake_phone_has_the_documented_tree() -> None:
    phone = build_fake_phone()
    paths = sorted(phone.files)

    def under(prefix: str) -> list[str]:
        return [p for p in paths if p.startswith(prefix)]

    camera = under("/sdcard/DCIM/Camera/")
    assert any(p.endswith(".jpg") for p in camera)
    assert any(p.endswith(".mp4") for p in camera)
    assert any("-WA" in p for p in camera)  # a WhatsApp-named stray
    assert under("/sdcard/DCIM/Screenshots/")
    assert under("/sdcard/Pictures/.thumbnails/")
    for base in (
        "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/",
        "/sdcard/WhatsApp/Media/",
    ):
        assert under(base + "WhatsApp Images/Sent/")
        assert under(base + "WhatsApp Images/Private/")
        for hidden in (".Links", ".Statuses", ".wamocache", ".Wamo"):
            assert any(f"/{hidden}/" in p for p in under(base)), hidden
        assert any(
            p.split("/")[len(base.split("/")) - 1].startswith(".")
            and p.split("/")[len(base.split("/")) - 1]
            not in (".Links", ".Statuses", ".wamocache", ".Wamo")
            for p in under(base)
        )
    media = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/"
    assert under(media + " AI Media/")
    assert under(media + "WhatsApp Backup Excluded Stickers/")
    assert under(media + "WhatsApp Video Notes/")
    docs_sent = under(media + "WhatsApp Documents/Sent/")
    assert sum(1 for p in docs_sent if p.endswith(".")) >= 3
    assert under("/sdcard/Android/media/com.whatsapp.w4b/")
    assert under("/sdcard/Recordings/Call/")
    assert [p for p in paths if p.count("/") == 2]  # loose files directly in /sdcard

    assert any(not p.isascii() and "\udce9" not in p and "\n" not in p for p in paths)
    assert NON_UTF8_FILE in phone.files and "\udce9" in NON_UTF8_FILE
    assert NEWLINE_FILE in phone.files and "\n" in NEWLINE_FILE
    assert phone.files[ZERO_BYTE_FILE].size == 0
    assert sum(1 for f in phone.files.values() if f.size == 0 and not f.is_symlink_to) >= 2
    assert phone.files[SYMLINK].is_symlink_to == "/sdcard/DCIM/Camera"
    assert phone.files[BIG_VIDEO].data is None
    assert phone.files[KNOWN_FILE].data == KNOWN_DATA
    assert UNREADABLE_DIR in phone.unreadable


def test_build_fake_phone_contains_no_real_names() -> None:
    text = "\n".join(build_fake_phone().files)
    assert "/Users/" not in text


def test_walk_over_an_unreadable_folder_fails_loudly_naming_it() -> None:
    transport = FakeTransport(build_fake_phone())
    with pytest.raises(RemoteError) as info:
        list(transport.walk("/sdcard"))
    assert info.value.path == UNREADABLE_DIR
    assert UNREADABLE_DIR in str(info.value)
    with pytest.raises(RemoteError) as listing:
        transport.list_dir(UNREADABLE_DIR)
    assert listing.value.path == UNREADABLE_DIR
    entry = transport.stat(UNREADABLE_DIR)
    assert entry is not None
    assert entry.is_dir


def test_walk_is_depth_first_in_sorted_order() -> None:
    phone = FakePhone()
    for path in ("/sdcard/a/b/1", "/sdcard/a/c", "/sdcard/a/b/d/2", "/sdcard/z", "/sdcard/a/b/0"):
        phone.add_file(path, b"x")
    paths = [e.path for e in FakeTransport(phone).walk("/sdcard")]
    assert paths == [
        "/sdcard/a/b/0",
        "/sdcard/a/b/1",
        "/sdcard/a/b/d/2",
        "/sdcard/a/c",
        "/sdcard/z",
    ]


def test_walk_does_not_follow_symlinks_and_reports_their_own_size() -> None:
    phone = FakePhone()
    phone.add_file("/sdcard/real/x", b"x")
    phone.add_symlink("/sdcard/link", "/sdcard/real")
    entries = {e.path: e for e in FakeTransport(phone).walk("/sdcard")}
    assert set(entries) == {"/sdcard/real/x", "/sdcard/link"}
    link = entries["/sdcard/link"]
    assert link.is_symlink
    assert not link.is_dir
    assert link.size == len(b"/sdcard/real")


def test_phone_mutators() -> None:
    phone = FakePhone()
    phone.add_file("/sdcard/f.bin", b"abc", mtime=5)
    phone.modify("/sdcard/f.bin", b"abcdef", mtime=9)
    entry = FakeTransport(phone).stat("/sdcard/f.bin")
    assert entry is not None
    assert (entry.size, entry.mtime) == (6, 9)
    phone.remove("/sdcard/f.bin")
    assert FakeTransport(phone).stat("/sdcard/f.bin") is None
    assert FakeTransport(phone).list_dir("/sdcard") == []
    with pytest.raises(KeyError):
        phone.remove("/sdcard/f.bin")
    with pytest.raises(KeyError):
        phone.modify("/sdcard/f.bin", b"", 0)
    with pytest.raises(ValueError, match="size"):
        phone.add_file("/sdcard/g.bin", b"abc", size=5)


def test_non_utf8_names_round_trip_through_the_transport() -> None:
    transport = FakeTransport(build_fake_phone())
    entry = transport.stat(NON_UTF8_FILE)
    assert entry is not None
    assert entry.path.encode("utf-8", "surrogateescape").endswith(b"caf\xe9.jpg")


def test_offset_read_past_the_end_is_empty_and_negative_offset_is_rejected() -> None:
    transport = FakeTransport(build_fake_phone())
    with transport.open_read(KNOWN_FILE, offset=len(KNOWN_DATA) + 100) as stream:
        assert stream.read() == b""
    with pytest.raises(ValueError, match="offset"):
        transport.open_read(KNOWN_FILE, offset=-1)
