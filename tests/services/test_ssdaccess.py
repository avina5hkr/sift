"""The SSD access probe: it tells denied, full and read-only apart, and touches nothing else."""

import errno
import os
import threading
from pathlib import Path

import pytest

from sift.core.db import Database
from sift.services import ssdaccess
from sift.services.ssdaccess import PREFIX, finish_leftover, probe, probe_waiting


def _leftover(db: Database) -> str | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT value FROM setting WHERE key = ?", (ssdaccess.PROBE_KEY,)
        ).fetchone()
    return None if row is None else str(row[0])


def test_a_writable_drive_is_ok_and_left_exactly_as_it_was(db: Database, tmp_path: Path) -> None:
    (tmp_path / "PhoneBackup").mkdir()
    (tmp_path / "PhoneBackup" / "keep.jpg").write_bytes(b"x")
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert probe(db, tmp_path).state == "ok"
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert _leftover(db) is None


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (errno.EPERM, "denied"),
        (errno.EACCES, "denied"),
        (errno.ENOSPC, "full"),
        (errno.EROFS, "read_only"),
        (errno.EIO, "error"),
    ],
)
def test_each_failure_is_told_apart_in_plain_words(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, state: str
) -> None:
    real_open = os.open

    def failing_open(path, flags, mode=0o777):  # type: ignore[no-untyped-def]
        if str(path).endswith(ssdaccess.FILE):
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", failing_open)
    result = probe(db, tmp_path)
    assert result.state == state and result.message == ssdaccess.MESSAGES[state]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(PREFIX)]  # its folder is gone


def test_the_denied_message_says_where_to_allow_it() -> None:
    assert "Removable Volumes" in ssdaccess.MESSAGES["denied"]
    assert "unplug" not in " ".join(ssdaccess.MESSAGES.values()).lower()


def test_an_interrupted_probe_is_finished_at_the_next_start_and_nothing_else_goes(
    db: Database, tmp_path: Path
) -> None:
    folder = tmp_path / f"{PREFIX}0123456789abcdef"
    folder.mkdir()
    (folder / ssdaccess.FILE).write_bytes(b"sift")
    other = tmp_path / ".sift-access-not-recorded"
    other.mkdir()
    ssdaccess._record(db, folder)
    assert finish_leftover(db)
    assert not folder.exists() and other.exists() and _leftover(db) is None


def test_a_recorded_folder_that_is_not_ours_is_never_removed(db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "Photos"
    folder.mkdir()
    ssdaccess._record(db, folder)
    assert finish_leftover(db)
    assert folder.exists() and _leftover(db) is None


def test_an_unplugged_drive_keeps_the_record_for_later(db: Database, tmp_path: Path) -> None:
    ssdaccess._record(db, tmp_path / "gone" / f"{PREFIX}0123456789abcdef")
    assert not finish_leftover(db)
    assert _leftover(db) is not None


def test_a_probe_held_by_a_macos_prompt_answers_waiting(db: Database, tmp_path: Path) -> None:
    release = threading.Event()

    def held(db: Database, mount: Path) -> ssdaccess.AccessResult:
        release.wait(5)
        return ssdaccess.AccessResult.of("ok")

    assert probe_waiting(db, tmp_path, wait_s=0.1, run=held).state == "waiting"
    release.set()
