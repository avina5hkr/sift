"""The fake phone and transport behave like a real phone where the backup code relies on it.

/sdcard is a symlink to the storage folder, the transport has a limited pool of connection
slots, and walks and reads take the same new options as the real transport.
"""

import pytest

from sift.device.adb_transport import Limits
from sift.device.fake_phone import (
    KNOWN_DATA,
    KNOWN_FILE,
    SD,
    UNREADABLE_DIR,
    FakePhone,
    build_fake_phone,
)
from sift.device.fake_transport import FakeTransport, FaultPlan, ManualClock
from sift.device.transport import DeviceTimeout, RemoteEntry, RemoteError

STORAGE = "/storage/emulated/0"


def make(faults: FaultPlan | None = None, limits: Limits | None = None) -> FakeTransport:
    clock = ManualClock()
    kwargs = {} if limits is None else {"limits": limits}
    return FakeTransport(
        build_fake_phone(20), faults, clock=clock, sleep=clock.sleep, stall_timeout_s=30, **kwargs
    )


# -- /sdcard is a symlink ---------------------------------------------------------------------


def test_sdcard_is_a_symlink_to_the_storage_folder() -> None:
    phone = build_fake_phone(20)
    entry = phone.entry(SD)
    assert entry is not None
    assert entry.is_symlink
    assert not entry.is_dir
    storage = phone.entry(STORAGE)
    assert storage is not None
    assert storage.is_dir
    assert not storage.is_symlink


def test_listing_and_walking_follow_the_sdcard_link() -> None:
    transport = make()
    names = {e.path for e in transport.list_dir(SD)}
    assert f"{SD}/DCIM" in names
    via_storage = {e.path for e in transport.list_dir(STORAGE)}
    assert via_storage == {p.replace(SD, STORAGE, 1) for p in names}
    skip = lambda error: None  # noqa: E731  (Android/data cannot be listed on the fake either)
    walked = {e.path for e in transport.walk(SD, on_error=skip)}
    assert KNOWN_FILE in walked
    through_storage = {e.path for e in transport.walk(STORAGE, on_error=skip)}
    assert {p.replace(STORAGE, SD, 1) for p in through_storage} == walked


def test_the_same_file_reads_through_either_spelling() -> None:
    transport = make()
    storage_path = KNOWN_FILE.replace(SD, STORAGE, 1)
    stat = transport.stat(storage_path)
    assert stat is not None
    assert stat.path == storage_path
    assert not stat.is_symlink
    assert stat.size == len(KNOWN_DATA)
    with transport.open_read(storage_path) as stream:
        assert stream.read() == KNOWN_DATA
    assert (
        transport.sha256([storage_path, KNOWN_FILE])[storage_path]
        == transport.sha256([KNOWN_FILE])[KNOWN_FILE]
    )


def test_the_storage_chain_is_listable_from_the_root() -> None:
    transport = make()
    assert {e.path for e in transport.list_dir("/")} >= {SD, "/storage"}
    assert [e.path for e in transport.list_dir("/storage")] == ["/storage/emulated"]
    assert [e.path for e in transport.list_dir("/storage/emulated")] == [STORAGE]
    root = {e.path: e for e in transport.list_dir("/")}
    assert root[SD].is_symlink
    walked = [e.path for e in transport.walk("/", on_error=lambda error: None)]
    assert SD in walked  # the link itself is reported
    assert not any(p.startswith(SD + "/") for p in walked)  # and never followed


def test_files_added_by_either_spelling_are_the_same_files() -> None:
    phone = FakePhone()
    phone.add_file(f"{STORAGE}/DCIM/a.jpg", b"a", mtime=5)
    entry = phone.entry(f"{SD}/DCIM/a.jpg")
    assert entry is not None
    assert (entry.size, entry.mtime) == (1, 5)
    phone.remove(f"{SD}/DCIM/a.jpg")
    assert phone.entry(f"{STORAGE}/DCIM/a.jpg") is None


def test_the_storage_folders_cannot_be_removed() -> None:
    phone = build_fake_phone(5)
    for path in (SD, STORAGE, "/storage"):
        with pytest.raises(KeyError):
            phone.remove(path)


# -- connection slots ---------------------------------------------------------------------------


def open_streams(transport: FakeTransport, count: int) -> list[object]:
    return [transport.open_read(KNOWN_FILE) for _ in range(count)]


def test_the_seventh_call_times_out_when_six_streams_are_open() -> None:
    transport = make()
    assert Limits().max_connections == 6
    streams = open_streams(transport, 6)
    assert transport.slots_in_use == 6
    with pytest.raises(DeviceTimeout):
        transport.open_read(KNOWN_FILE)
    with pytest.raises(DeviceTimeout):
        transport.stat(KNOWN_FILE)
    for stream in streams:
        stream.close()  # type: ignore[attr-defined]
    assert transport.slots_in_use == 0
    assert transport.stat(KNOWN_FILE) is not None


def test_a_closed_stream_gives_its_slot_back_exactly_once() -> None:
    transport = make()
    stream = transport.open_read(KNOWN_FILE)
    stream.close()
    stream.close()
    assert transport.slots_in_use == 0


def test_a_dropped_stream_gives_its_slot_back() -> None:
    import gc

    transport = make()
    stream = transport.open_read(KNOWN_FILE)
    assert transport.slots_in_use == 1
    del stream
    gc.collect()
    assert transport.slots_in_use == 0


def test_the_wait_for_a_slot_is_the_calls_time_limit() -> None:
    clock = ManualClock()
    transport = FakeTransport(
        build_fake_phone(5),
        clock=clock,
        sleep=clock.sleep,
        limits=Limits(stat_s=7, max_connections=1),
    )
    held = transport.open_read(KNOWN_FILE)
    started = clock()
    with pytest.raises(DeviceTimeout):
        transport.stat(KNOWN_FILE)
    assert clock() - started == pytest.approx(7)
    held.close()


def test_a_walk_holds_one_slot_from_its_first_step_until_it_ends() -> None:
    transport = make()
    walk = transport.walk(SD)
    assert transport.slots_in_use == 0  # nothing is connected before the first next()
    next(walk)
    assert transport.slots_in_use == 1
    walk.close()  # type: ignore[attr-defined]
    assert transport.slots_in_use == 0
    assert len(list(transport.walk(f"{SD}/DCIM"))) > 1
    assert transport.slots_in_use == 0  # run to the end


def test_a_walk_that_is_closed_early_gives_its_slot_back() -> None:
    transport = make()
    walk = transport.walk(SD)
    next(walk)
    walk.close()  # type: ignore[attr-defined]
    assert transport.slots_in_use == 0


def test_a_walk_that_fails_gives_its_slot_back() -> None:
    transport = make()
    with pytest.raises(RemoteError):
        list(transport.walk("/sdcard/nope"))
    assert transport.slots_in_use == 0


def test_short_calls_never_leak_slots() -> None:
    transport = make()
    transport.info()
    transport.list_dir(SD)
    transport.stat(SD)
    transport.sha256([KNOWN_FILE])
    transport.link_health()
    transport.media_rescan([KNOWN_FILE])
    with pytest.raises(RemoteError):
        transport.list_dir("/sdcard/nope")
    with pytest.raises(RemoteError):
        transport.open_read("/sdcard/nope")
    assert transport.slots_in_use == 0


def test_a_walk_and_five_streams_fill_the_pool() -> None:
    transport = make()
    walk = transport.walk(SD)
    next(walk)
    streams = open_streams(transport, 5)
    with pytest.raises(DeviceTimeout):
        transport.open_read(KNOWN_FILE)
    walk.close()  # type: ignore[attr-defined]
    transport.open_read(KNOWN_FILE).close()
    for stream in streams:
        stream.close()  # type: ignore[attr-defined]


# -- walk with on_error, open_read with an entry ----------------------------------------------


def test_walk_with_on_error_skips_what_cannot_be_listed() -> None:
    transport = make()
    errors: list[RemoteError] = []
    skipped = {e.path for e in transport.walk(f"{SD}/Android", on_error=errors.append)}
    assert [e.path for e in errors] == [UNREADABLE_DIR]
    assert not any(p.startswith(UNREADABLE_DIR + "/") for p in skipped)
    with pytest.raises(RemoteError) as info:  # without it, M1 behaviour
        list(transport.walk(f"{SD}/Android"))
    assert info.value.path == UNREADABLE_DIR


def test_walk_with_on_error_still_raises_for_an_unlistable_root() -> None:
    transport = make()
    errors: list[RemoteError] = []
    with pytest.raises(RemoteError):
        list(transport.walk(UNREADABLE_DIR, on_error=errors.append))
    with pytest.raises(RemoteError):
        list(transport.walk("/sdcard/missing", on_error=errors.append))
    assert errors == []


def test_walk_with_on_error_skips_an_entry_that_cannot_be_looked_at() -> None:
    bad = f"{SD}/Download/empty.dat"
    transport = make(FaultPlan(unstatable={bad}))
    errors: list[RemoteError] = []
    paths = {e.path for e in transport.walk(f"{SD}/Download", on_error=errors.append)}
    assert bad not in paths
    assert [e.path for e in errors] == [bad]
    with pytest.raises(RemoteError):
        list(transport.walk(f"{SD}/Download"))


def test_open_read_with_an_entry_does_not_look_the_file_up() -> None:
    transport = make()
    entry = transport.stat(KNOWN_FILE)
    assert entry is not None
    transport.journal.clear()
    with transport.open_read(KNOWN_FILE, entry=entry) as stream:
        assert stream.read() == KNOWN_DATA
    assert [e.op for e in transport.journal] == ["open_read"]


def test_open_read_with_an_entry_for_a_folder_or_link_is_refused() -> None:
    transport = make()
    folder = RemoteEntry(f"{SD}/DCIM", 0, 0, is_dir=True, is_symlink=False)
    link = RemoteEntry(f"{SD}/DCIM", 0, 0, is_dir=False, is_symlink=True)
    for entry in (folder, link):
        with pytest.raises(RemoteError):
            transport.open_read(f"{SD}/DCIM", entry=entry)
    assert transport.slots_in_use == 0


def test_open_read_with_a_stale_entry_reports_the_change() -> None:
    from sift.device.transport import FileChanged

    transport = make()
    entry = transport.stat(KNOWN_FILE)
    assert entry is not None
    transport.phone.remove(KNOWN_FILE)
    with transport.open_read(KNOWN_FILE, entry=entry) as stream, pytest.raises(FileChanged):
        stream.read()
