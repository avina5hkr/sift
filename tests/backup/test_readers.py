"""The first-run reader test: picks the fastest reader count and never creates a file."""

import builtins
import io
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

from sift.backup.readers import (
    ReaderTestCancelled,
    largest_files,
    measure_readers,
    run_reader_test,
    stored_read_speed,
)
from sift.core.config import load_config
from sift.core.context import AppContext
from sift.core.paths import AppPaths
from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity
from sift.device.transport import RemoteEntry
from tests.helpers import make_ctx

MB = 1_000_000


class ModelStream(io.RawIOBase):
    """Serves bytes at the phone's per-reader speed. Paced against a deadline, not one sleep per
    chunk: a sleep that overshoots (a busy CI machine) makes the next one shorter, so the average
    speed stays what the model says however coarse the sleeps are."""

    def __init__(self, phone: ThroughputPhone, size: int) -> None:
        super().__init__()
        self._phone = phone
        self._left = size
        self._due = time.monotonic()  # when the bytes served so far are due

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        n = min(self._left, 64 * 1024 if size is None or size < 0 else min(size, 64 * 1024))
        if n <= 0:
            return b""
        self._due += n / self._phone.per_reader_bps()
        wait = self._due - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._left -= n
        return bytes(n)

    def close(self) -> None:
        if not self.closed:
            with self._phone.lock:
                self._phone.open_streams -= 1
        super().close()


class ThroughputPhone:
    """A phone whose total read speed depends on how many streams are open at once."""

    def __init__(self, model: dict[int, float]) -> None:
        self.model = model
        self.lock = threading.Lock()
        self.open_streams = 0
        self.most_at_once = 0

    def per_reader_bps(self) -> float:
        with self.lock:
            n = max(1, self.open_streams)
        return self.model[n] / n

    def open_read(
        self, path: str, offset: int = 0, *, entry: RemoteEntry | None = None
    ) -> BinaryIO:
        assert entry is not None
        with self.lock:
            self.open_streams += 1
            self.most_at_once = max(self.most_at_once, self.open_streams)
        return cast(BinaryIO, ModelStream(self, entry.size))


FILES = [RemoteEntry(f"/sdcard/DCIM/Camera/V{i}.mp4", 10**9, 0, False, False) for i in range(4)]


@pytest.mark.parametrize("oversleep_s", [0.0, 0.02])
def test_reader_test_picks_the_fastest_reader_count(
    oversleep_s: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also when every sleep overshoots by 20 ms, as measured on a busy CI runner."""
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(s + oversleep_s))
    phone = ThroughputPhone({1: 10 * MB, 2: 30 * MB, 3: 24 * MB, 4: 18 * MB})
    result = measure_readers(phone, FILES, duration_s=0.5)  # type: ignore[arg-type]
    assert result.readers == 2
    speeds = dict(result.speeds)
    assert speeds[2] > speeds[4] > speeds[1]
    assert result.speed_bps == speeds[2]
    assert phone.most_at_once == 4 and phone.open_streams == 0


def test_a_tie_goes_to_fewer_readers() -> None:
    phone = ThroughputPhone({n: 20 * MB for n in range(1, 5)})
    speeds = {1: 5.0, 2: 5.0, 4: 5.0}
    calls: list[int] = []

    def fake_round(*args: Any) -> float:
        calls.append(args[2])
        return speeds[args[2]]

    import sift.backup.readers as readers

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(readers, "_round", fake_round)
        result = measure_readers(phone, FILES)  # type: ignore[arg-type]
    assert calls == [1, 2, 4] and result.readers == 1


@pytest.fixture
def setup(paths: AppPaths, tmp_path: Path) -> Iterator[tuple[AppContext, DeviceSession, Path]]:
    backup = tmp_path / "backup"
    (backup / ".staging").mkdir(parents=True)
    (backup / "Phone").mkdir()
    (backup / "Phone" / "kept.jpg").write_bytes(b"already backed up")
    ctx = make_ctx(paths)
    ctx.config.backup.mac_root = str(backup)
    phone = FakePhone()
    for i in range(5):
        phone.add_file(f"/sdcard/DCIM/Camera/VID_{i}.mp4", size=(i + 1) * 200_000, mtime=1)
    phone.add_file("/sdcard/DCIM/Camera/small.jpg", b"tiny", mtime=1)
    transport = FakeTransport(phone)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (id, uuid, serial) VALUES (1, 'dev-1', 'FAKE-0001')")
        for entry in transport.walk("/sdcard/DCIM"):
            conn.execute(
                "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state) "
                "VALUES (1, ?, 'camera', ?, ?, 'discovered')",
                (entry.path, entry.size, entry.mtime),
            )
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    yield ctx, session, backup


def test_largest_files_come_from_the_last_scan(
    setup: tuple[AppContext, DeviceSession, Path],
) -> None:
    ctx, _session, _backup = setup
    files = largest_files(ctx.db, "dev-1", limit=3)
    assert [f.path.rsplit("/", 1)[-1] for f in files] == ["VID_4.mp4", "VID_3.mp4", "VID_2.mp4"]


def _snapshot(root: Path, skip: Path | None = None) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if skip is not None and (here == skip or skip in here.parents):
            continue
        for name in dirnames:  # a folder's own size and mtime move with excluded files
            out[str((here / name).relative_to(root)) + "/"] = (0, 0)
        for name in filenames:
            path = here / name
            st = path.lstat()
            out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def _home_snapshot(home: Path, paths: AppPaths) -> dict[str, tuple[int, int]]:
    """SIFT_HOME without the database (and its WAL and SHM), the config and the logs."""
    left_out = {paths.db, Path(f"{paths.db}-wal"), Path(f"{paths.db}-shm"), paths.config}
    snap = _snapshot(home, skip=paths.logs)
    return {k: v for k, v in snap.items() if home / k not in left_out}


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def test_reader_test_creates_no_files(
    setup: tuple[AppContext, DeviceSession, Path],
    paths: AppPaths,
    _isolate_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, session, backup = setup
    trees = [backup, backup / ".staging", _isolate_home]

    def snapshots() -> tuple[dict[str, tuple[int, int]], ...]:
        staging = backup / ".staging"
        return _snapshot(backup), _snapshot(staging), _home_snapshot(_isolate_home, paths)

    before = snapshots()
    writes: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wax+"):
            writes.append(os.fspath(file))
        return real_open(file, mode, *args, **kwargs)

    def spy_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & _WRITE_FLAGS:
            writes.append(os.fspath(path))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    result = run_reader_test(ctx, session, duration_s=0.05)
    monkeypatch.undo()

    assert result is not None
    assert snapshots() == before

    def config_write(path: str) -> bool:  # saving the result: the config and its temp file
        p = Path(path)
        return p == paths.config or (
            p.parent == paths.config.parent and p.name.startswith(f".{paths.config.name}.")
        )

    under_trees = [
        w for w in writes if any(Path(w).resolve().is_relative_to(t.resolve()) for t in trees)
    ]
    assert [w for w in under_trees if not config_write(w)] == []
    assert not any(e.mutating for e in session.transport.journal)  # type: ignore[attr-defined]


def test_reader_test_stores_its_result_in_the_settings(
    setup: tuple[AppContext, DeviceSession, Path], paths: AppPaths
) -> None:
    ctx, session, _backup = setup
    assert stored_read_speed(ctx) is None
    result = run_reader_test(ctx, session, duration_s=0.02)
    assert result is not None
    assert ctx.config.phone.parallel_reads_usb == result.readers
    assert stored_read_speed(ctx) == float(int(result.speed_bps)) > 0
    saved, warnings = load_config(paths.config)
    assert warnings == []
    assert saved.phone.parallel_reads_usb == result.readers
    assert saved.phone.measured_read_bps == int(result.speed_bps)


def test_reader_test_with_no_known_files_does_nothing(paths: AppPaths) -> None:
    ctx = make_ctx(paths)
    transport = FakeTransport(FakePhone())
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "nobody")
    assert run_reader_test(ctx, session, duration_s=0.01) is None
    assert not paths.config.exists()


def test_a_cancelled_reader_test_stops_and_stores_nothing(
    setup: tuple[AppContext, DeviceSession, Path], paths: AppPaths
) -> None:
    ctx, session, _backup = setup
    asked = [0]

    def cancelled() -> bool:
        asked[0] += 1
        return asked[0] > 5

    began = time.monotonic()
    with pytest.raises(ReaderTestCancelled):
        run_reader_test(ctx, session, duration_s=30.0, cancelled=cancelled)
    assert time.monotonic() - began < 5
    assert stored_read_speed(ctx) is None and not paths.config.exists()
