"""The daily database copy: SQLite's backup API, one per day, the newest seven kept."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from sift.core import dbbackup
from sift.core.db import Database
from sift.core.dbbackup import KEEP, copy_name, daily_copy

DAY = date(2026, 11, 7)


def test_a_copy_is_a_complete_readable_database(db: Database, tmp_path: Path) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO setting (key, value) VALUES ('k', 'v')")
    folder = tmp_path / "copies"
    made = daily_copy(db, folder, DAY)
    assert made == folder / "state-daily-20261107.db"
    copy = sqlite3.connect(made)
    try:
        assert copy.execute("SELECT value FROM setting WHERE key = 'k'").fetchone() == ("v",)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        copy.close()
    assert [p.name for p in folder.iterdir()] == [copy_name(DAY)]  # no .partial left behind


def test_one_copy_per_day(db: Database, tmp_path: Path) -> None:
    tmp_path = tmp_path / "c"
    assert daily_copy(db, tmp_path, DAY) is not None
    first = (tmp_path / copy_name(DAY)).stat().st_mtime_ns
    with db.tx() as c:
        c.execute("INSERT INTO setting (key, value) VALUES ('later', '1')")
    assert daily_copy(db, tmp_path, DAY) is None
    assert (tmp_path / copy_name(DAY)).stat().st_mtime_ns == first
    assert daily_copy(db, tmp_path, DAY + timedelta(days=1)) is not None


def test_only_the_newest_seven_are_kept(db: Database, tmp_path: Path) -> None:
    tmp_path = tmp_path / "c"
    tmp_path.mkdir()
    for n in range(10):
        daily_copy(db, tmp_path, DAY + timedelta(days=n))
    kept = sorted(p.name for p in tmp_path.iterdir())
    assert kept == [copy_name(DAY + timedelta(days=n)) for n in range(10 - KEEP, 10)]


def test_rotation_never_touches_other_files(db: Database, tmp_path: Path) -> None:
    tmp_path = tmp_path / "c"
    tmp_path.mkdir()
    others = [
        "state-v1-20260101T000000Z.db",  # a migration copy
        "state-v1-20260101T000000Z.db.partial",  # a migration copy that was left half done
        "state-daily-2026.db",
        "notes.txt",
        "state-daily-20260101.db.bak",
    ]
    for name in others:
        (tmp_path / name).write_text("keep me")
    (tmp_path / "sub").mkdir()
    for n in range(10):
        daily_copy(db, tmp_path, DAY + timedelta(days=n))
    assert all((tmp_path / name).read_text() == "keep me" for name in others)
    assert (tmp_path / "sub").is_dir()


def test_an_old_copy_does_not_stop_todays(db: Database, tmp_path: Path) -> None:
    tmp_path = tmp_path / "c"
    tmp_path.mkdir()
    daily_copy(db, tmp_path, DAY)
    assert daily_copy(db, tmp_path, DAY + timedelta(days=30)) is not None
    assert (tmp_path / copy_name(DAY + timedelta(days=30))).exists()


def test_a_failed_copy_leaves_nothing_behind(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_src: object, _dest: object) -> None:
        raise OSError("disk full")

    folder = tmp_path / "c"
    monkeypatch.setattr(dbbackup.os, "replace", broken)
    with pytest.raises(OSError):
        daily_copy(db, folder, DAY)
    assert list(folder.iterdir()) == []


def test_a_folder_that_does_not_exist_is_made(db: Database, tmp_path: Path) -> None:
    assert daily_copy(db, tmp_path / "a" / "b", DAY) is not None


def test_stale_partial_copies_are_swept_but_only_our_own(db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "c"
    folder.mkdir()
    stale = folder / "state-daily-20250101.db.partial"
    stale.write_text("half")
    ours_not = [folder / "state-v1-x.db.partial", folder / "other.partial", folder / "a.txt"]
    for p in ours_not:
        p.write_text("keep")
    (folder / "state-daily-20250102.db.partial").mkdir()  # not a file: left alone
    daily_copy(db, folder, DAY)
    assert not stale.exists()
    assert all(p.read_text() == "keep" for p in ours_not)
    assert (folder / "state-daily-20250102.db.partial").is_dir()


def test_stale_partials_go_even_when_todays_copy_exists(db: Database, tmp_path: Path) -> None:
    daily_copy(db, tmp_path / "c", DAY)
    stale = tmp_path / "c" / "state-daily-20250101.db.partial"
    stale.write_text("half")
    assert daily_copy(db, tmp_path / "c", DAY) is None
    assert not stale.exists()
