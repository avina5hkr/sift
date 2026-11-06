import ctypes
import errno
import fcntl
import io
import logging
import os
import stat
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from sift.backup import staging
from sift.backup.staging import (
    ChecksumMismatch,
    IncompleteCopy,
    StagedFile,
    TargetExists,
    sweep_staging,
)

MTIME = 1_700_000_000


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    path = tmp_path / "backup" / ".staging"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def final_dir(tmp_path: Path) -> Path:
    path = tmp_path / "backup" / "DCIM"
    path.mkdir(parents=True)
    return path


@pytest.fixture(autouse=True)
def _fresh_warned_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staging, "_warned_devices", set())


def _parts(staging_dir: Path) -> list[Path]:
    return sorted(staging_dir.glob("*.part"))


def _copy(stream: io.RawIOBase | io.BufferedIOBase, staged: StagedFile) -> None:
    while chunk := stream.read(4):
        staged.write(chunk)


def _spy_fcntl(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, os.stat_result]]:
    """Record (cmd, fstat of the fd) for every fcntl call, then make the real call."""
    calls: list[tuple[int, os.stat_result]] = []
    real = fcntl.fcntl

    def spy(fd: int, cmd: int, arg: Any = 0) -> Any:
        calls.append((cmd, os.fstat(fd)))
        return real(fd, cmd, arg)

    monkeypatch.setattr(fcntl, "fcntl", spy)
    return calls


def _renamex_failing_with(code: int) -> Callable[[bytes, bytes, int], int]:
    def renamex_np(src: bytes, dst: bytes, flags: int) -> int:
        ctypes.set_errno(code)
        return -1

    return renamex_np


# -- the happy path -----------------------------------------------------------------------------


def test_commit_lands_the_file_with_its_mtime_and_hash(staging_dir: Path, final_dir: Path) -> None:
    data = b"hello, phone"
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=len(data)) as staged:
        assert len(_parts(staging_dir)) == 1
        staged.write(data[:5])
        staged.write(data[5:])
        assert staged.sha256 == sha256(data).hexdigest()
        verified = staged.commit(final, mtime=MTIME)
    assert verified == sha256(data).hexdigest()
    assert final.read_bytes() == data
    assert final.stat().st_mtime == MTIME
    assert _parts(staging_dir) == []


def test_part_names_are_uuid4_and_created_exclusively(
    staging_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flags_seen: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        flags_seen.append(flags)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    with StagedFile(staging_dir, expected_size=0):
        (part,) = _parts(staging_dir)
    assert len(part.stem) == 32 and part.stem[12] == "4"
    assert flags_seen and flags_seen[0] & os.O_EXCL and flags_seen[0] & os.O_CREAT


def test_an_empty_file_commits(staging_dir: Path, final_dir: Path) -> None:
    final = final_dir / "empty"
    with StagedFile(staging_dir, expected_size=0) as staged:
        assert staged.commit(final, mtime=MTIME) == sha256(b"").hexdigest()
    assert final.read_bytes() == b""


# -- a short or failed read never becomes a final file ------------------------------------------


def test_short_stream_never_produces_final_file(staging_dir: Path, final_dir: Path) -> None:
    data = b"0123456789"
    final = final_dir / "a.jpg"
    stream = io.BytesIO(data[:-1])  # the stream ends one byte early
    with StagedFile(staging_dir, expected_size=len(data)) as staged:
        _copy(stream, staged)
        with pytest.raises(IncompleteCopy):
            staged.commit(final, mtime=MTIME)
        assert not final.exists()
        assert _parts(staging_dir) == []
    assert not final.exists()
    assert _parts(staging_dir) == []


def test_more_bytes_than_expected_is_incomplete_too(staging_dir: Path, final_dir: Path) -> None:
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=3) as staged:
        staged.write(b"abcd")
        with pytest.raises(IncompleteCopy):
            staged.commit(final, mtime=MTIME)
    assert not final.exists()


def test_exception_mid_write_leaves_no_final_file(staging_dir: Path, final_dir: Path) -> None:
    final = final_dir / "a.jpg"

    def chunks() -> Any:
        yield b"abcd"
        raise ConnectionResetError("phone went away")

    with pytest.raises(ConnectionResetError), StagedFile(staging_dir, expected_size=8) as staged:
        for chunk in chunks():
            staged.write(chunk)
        staged.commit(final, mtime=MTIME)  # pragma: no cover
    assert not final.exists()
    assert _parts(staging_dir) == []


def test_a_failed_write_poisons_the_stage(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = final_dir / "a.jpg"
    real_write = os.write

    def disk_full(fd: int, data: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    with StagedFile(staging_dir, expected_size=4) as staged:
        staged.write(b"ab")
        monkeypatch.setattr(os, "write", disk_full)
        with pytest.raises(OSError, match="No space"):
            staged.write(b"cd")
        monkeypatch.setattr(os, "write", real_write)
        with pytest.raises(IncompleteCopy):
            staged.commit(final, mtime=MTIME)
    assert not final.exists()
    assert _parts(staging_dir) == []


def test_partial_os_writes_are_completed(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data)[:1]))
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=5) as staged:
        staged.write(b"abcde")
        monkeypatch.setattr(os, "write", real_write)
        staged.commit(final, mtime=MTIME)
    assert final.read_bytes() == b"abcde"


# -- never overwrite (I1) -----------------------------------------------------------------------


def test_commit_never_overwrites_existing_final(staging_dir: Path, final_dir: Path) -> None:
    final = final_dir / "a.jpg"
    final.write_bytes(b"the original")
    os.utime(final, (1, 1))
    before = final.stat()
    with StagedFile(staging_dir, expected_size=3) as staged:
        staged.write(b"new")
        with pytest.raises(TargetExists):
            staged.commit(final, mtime=MTIME)
        # The stage survives, so the caller can commit it under another name.
        other = final_dir / "a_1234abcd.jpg"
        assert staged.commit(other, mtime=MTIME) == sha256(b"new").hexdigest()
    after = final.stat()
    assert final.read_bytes() == b"the original"
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    )
    assert other.read_bytes() == b"new"
    assert _parts(staging_dir) == []


def test_target_exists_then_exit_removes_the_part(staging_dir: Path, final_dir: Path) -> None:
    final = final_dir / "a.jpg"
    final.write_bytes(b"x")
    with StagedFile(staging_dir, expected_size=1) as staged:
        staged.write(b"y")
        with pytest.raises(TargetExists):
            staged.commit(final, mtime=MTIME)
    assert final.read_bytes() == b"x"
    assert _parts(staging_dir) == []


def test_a_dangling_symlink_at_the_target_is_not_replaced(
    staging_dir: Path, final_dir: Path
) -> None:
    final = final_dir / "a.jpg"
    final.symlink_to(final_dir / "missing")
    with StagedFile(staging_dir, expected_size=1) as staged:
        staged.write(b"y")
        with pytest.raises(TargetExists):
            staged.commit(final, mtime=MTIME)
    assert final.is_symlink()


# -- read-back ----------------------------------------------------------------------------------


def test_reread_mismatch_removes_final_and_raises(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = final_dir / "a.jpg"
    real_hash = staging._hash_uncached

    def corrupting(path: Path) -> str:
        with open(path, "r+b") as f:  # the disk handed back something else
            f.write(b"X")
        return real_hash(path)

    monkeypatch.setattr(staging, "_hash_uncached", corrupting)
    with StagedFile(staging_dir, expected_size=4) as staged:
        staged.write(b"abcd")
        with pytest.raises(ChecksumMismatch):
            staged.commit(final, mtime=MTIME)
        assert not final.exists()
    assert not final.exists()
    assert _parts(staging_dir) == []


def test_reread_uses_f_nocache(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_fcntl(monkeypatch)
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        staged.commit(final, mtime=MTIME)
    nocache = [st for cmd, st in calls if cmd == fcntl.F_NOCACHE]
    assert [st.st_ino for st in nocache] == [final.stat().st_ino]


def test_an_error_after_the_rename_removes_the_final_file(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = final_dir / "a.jpg"

    def broken(path: Path) -> str:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(staging, "_hash_uncached", broken)
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        with pytest.raises(OSError, match="I/O error"):
            staged.commit(final, mtime=MTIME)
    assert not final.exists()
    assert _parts(staging_dir) == []


# -- flushing -----------------------------------------------------------------------------------


def test_full_fsync_called(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_fcntl(monkeypatch)
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        staged.commit(final, mtime=MTIME)
    full = [st for cmd, st in calls if cmd == fcntl.F_FULLFSYNC]
    file_flushes = [st for st in full if stat.S_ISREG(st.st_mode)]
    assert [st.st_ino for st in file_flushes] == [final.stat().st_ino]


def test_commit_flushes_the_parent_folder_after_the_rename(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = final_dir / "a.jpg"
    events: list[str] = []
    real_fcntl = fcntl.fcntl
    parent_ino = final_dir.stat().st_ino

    def spy(fd: int, cmd: int, arg: Any = 0) -> Any:
        st = os.fstat(fd)
        if cmd == fcntl.F_FULLFSYNC and stat.S_ISDIR(st.st_mode) and st.st_ino == parent_ino:
            events.append("parent flushed, file in place" if final.exists() else "too early")
        return real_fcntl(fd, cmd, arg)

    monkeypatch.setattr(fcntl, "fcntl", spy)
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        staged.commit(final, mtime=MTIME)
    assert events == ["parent flushed, file in place"]


def test_commit_falls_back_to_fsync_and_warns_once(
    staging_dir: Path,
    final_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_fcntl = fcntl.fcntl
    fsynced: list[int] = []
    real_fsync = os.fsync

    def no_full_fsync(fd: int, cmd: int, arg: Any = 0) -> Any:
        if cmd == fcntl.F_FULLFSYNC:
            raise OSError(errno.ENOTSUP, "Operation not supported")
        return real_fcntl(fd, cmd, arg)

    def spy_fsync(fd: int) -> None:
        fsynced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(fcntl, "fcntl", no_full_fsync)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    finals = [final_dir / "a.jpg", final_dir / "b.jpg"]
    with caplog.at_level(logging.WARNING, logger="sift.backup.staging"):
        for final in finals:
            with StagedFile(staging_dir, expected_size=2) as staged:
                staged.write(b"ab")
                staged.commit(final, mtime=MTIME)
    for final in finals:
        assert final.read_bytes() == b"ab"
        assert final.stat().st_ino in fsynced
    assert fsynced.count(final_dir.stat().st_ino) == 2  # the parent, once per commit
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.parametrize("code", [errno.EINVAL, errno.ENOTTY])
def test_other_unsupported_full_fsync_errors_fall_back(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    real_fcntl = fcntl.fcntl

    def no_full_fsync(fd: int, cmd: int, arg: Any = 0) -> Any:
        if cmd == fcntl.F_FULLFSYNC:
            raise OSError(code, os.strerror(code))
        return real_fcntl(fd, cmd, arg)

    monkeypatch.setattr(fcntl, "fcntl", no_full_fsync)
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        staged.commit(final, mtime=MTIME)
    assert final.read_bytes() == b"ab"


def test_a_real_flush_error_is_not_swallowed(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fcntl = fcntl.fcntl

    def failing(fd: int, cmd: int, arg: Any = 0) -> Any:
        if cmd == fcntl.F_FULLFSYNC:
            raise OSError(errno.EIO, "I/O error")
        return real_fcntl(fd, cmd, arg)

    monkeypatch.setattr(fcntl, "fcntl", failing)
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        with pytest.raises(OSError, match="I/O error"):
            staged.commit(final, mtime=MTIME)
    assert not final.exists()
    assert _parts(staging_dir) == []


# -- rename fallbacks (exFAT) -------------------------------------------------------------------


def test_commit_falls_back_when_renamex_unsupported(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renamed: list[tuple[str, str]] = []
    real_rename = os.rename

    def spy_rename(src: Any, dst: Any) -> None:
        renamed.append((os.fspath(src), os.fspath(dst)))
        real_rename(src, dst)

    monkeypatch.setattr(staging, "_renamex_np", _renamex_failing_with(errno.ENOTSUP))
    monkeypatch.setattr(os, "rename", spy_rename)
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        staged.commit(final, mtime=MTIME)
    assert final.read_bytes() == b"ab"
    assert [dst for _, dst in renamed] == [os.fspath(final)]


def test_renamex_einval_falls_back(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staging, "_renamex_np", _renamex_failing_with(errno.EINVAL))
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=2) as staged:
        staged.write(b"ab")
        staged.commit(final, mtime=MTIME)
    assert final.read_bytes() == b"ab"


def test_fallback_rename_refuses_existing_target(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staging, "_renamex_np", _renamex_failing_with(errno.ENOTSUP))
    renamed: list[object] = []
    monkeypatch.setattr(os, "rename", lambda src, dst: renamed.append(dst))
    final = final_dir / "a.jpg"
    final.write_bytes(b"the original")
    with StagedFile(staging_dir, expected_size=3) as staged:
        staged.write(b"new")
        with pytest.raises(TargetExists):
            staged.commit(final, mtime=MTIME)
    assert renamed == []
    assert final.read_bytes() == b"the original"
    assert _parts(staging_dir) == []


def test_renamex_eexist_is_target_exists(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staging, "_renamex_np", _renamex_failing_with(errno.EEXIST))
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=1) as staged:
        staged.write(b"y")
        with pytest.raises(TargetExists):
            staged.commit(final, mtime=MTIME)
    assert not final.exists()


def test_other_renamex_errors_are_raised(
    staging_dir: Path, final_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staging, "_renamex_np", _renamex_failing_with(errno.EXDEV))
    final = final_dir / "a.jpg"
    with StagedFile(staging_dir, expected_size=1) as staged:
        staged.write(b"y")
        with pytest.raises(OSError) as info:
            staged.commit(final, mtime=MTIME)
        assert info.value.errno == errno.EXDEV
    assert not final.exists()
    assert _parts(staging_dir) == []


# -- misuse -------------------------------------------------------------------------------------


def test_a_committed_stage_cannot_be_written_or_committed_again(
    staging_dir: Path, final_dir: Path
) -> None:
    with StagedFile(staging_dir, expected_size=1) as staged:
        staged.write(b"y")
        staged.commit(final_dir / "a", mtime=MTIME)
        with pytest.raises(ValueError):
            staged.write(b"z")
        with pytest.raises(ValueError):
            staged.commit(final_dir / "b", mtime=MTIME)
    assert not (final_dir / "b").exists()


# -- sweep --------------------------------------------------------------------------------------


def test_sweep_staging_removes_only_parts(staging_dir: Path) -> None:
    (staging_dir / "a.part").write_bytes(b"1")
    (staging_dir / "b.part").write_bytes(b"22")
    (staging_dir / "notes.txt").write_bytes(b"keep")
    (staging_dir / "c.part.keep").write_bytes(b"keep")
    (staging_dir / "folder.part").mkdir()
    assert sweep_staging(staging_dir) == 2
    assert sorted(p.name for p in staging_dir.iterdir()) == [
        "c.part.keep",
        "folder.part",
        "notes.txt",
    ]


def test_sweep_of_a_missing_staging_folder_is_zero(tmp_path: Path) -> None:
    assert sweep_staging(tmp_path / "nope") == 0


def test_stage_exposes_its_size_and_part_for_reading_before_commit(
    staging_dir: Path, final_dir: Path
) -> None:
    with StagedFile(staging_dir, expected_size=5) as staged:
        staged.write(b"hello")
        assert staged.size == 5
        assert staged.part_path.parent == staging_dir
        assert staged.part_path.read_bytes() == b"hello"
        staged.commit(final_dir / "f", mtime=1)
    assert staging.regular_file_sha256(final_dir / "f") == sha256(b"hello").hexdigest()


def test_regular_file_sha256_never_opens_or_follows_anything_else(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_bytes(b"data")
    (tmp_path / "link").symlink_to(real)
    os.mkfifo(tmp_path / "fifo")  # opening it for reading would block
    (tmp_path / "dir").mkdir()
    assert staging.regular_file_sha256(real) == sha256(b"data").hexdigest()
    for name in ("link", "fifo", "dir"):
        assert staging.regular_file_sha256(tmp_path / name) is None


def test_make_durable_flushes_the_file_and_its_folder_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "adopted.jpg"
    path.write_bytes(b"identical bytes")
    os.utime(path, (1, 1))
    before = os.stat(path)
    opened: list[tuple[str, int]] = []
    flushed: list[int] = []
    real_open, real_full_fsync = os.open, staging._full_fsync

    def spy_open(p: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append((os.fspath(p), flags))
        return real_open(p, flags, *args, **kwargs)

    def spy_fsync(fd: int) -> None:
        flushed.append(os.fstat(fd).st_ino)
        real_full_fsync(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(staging, "_full_fsync", spy_fsync)
    staging.make_durable(path)
    monkeypatch.undo()
    assert flushed == [before.st_ino, os.stat(tmp_path).st_ino]  # the file, then its folder
    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    assert opened and all(flags & writes == 0 for _p, flags in opened)
    after = os.stat(path)
    assert (after.st_mtime_ns, after.st_size, path.read_bytes()) == (
        before.st_mtime_ns,
        before.st_size,
        b"identical bytes",
    )
