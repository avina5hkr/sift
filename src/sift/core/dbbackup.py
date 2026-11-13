"""A daily copy of `state.db` in Application Support, through SQLite's online backup API.

`daily_copy` makes at most one copy per calendar day, named `state-daily-YYYYMMDD.db`, and keeps
the newest `KEEP` of them. Half copies a crash left (`state-daily-YYYYMMDD.db.partial`) are swept
before each copy. It only ever looks at files of those exact names in the folder it is given, so
the copies `apply_migrations` makes (`state-v<N>-...db`) and anything else there are never
touched. The copy to an SSD is a later milestone.
"""

import contextlib
import logging
import os
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from sift.core.db import Database

log = logging.getLogger(__name__)

KEEP = 7
_NAME = re.compile(r"^state-daily-(\d{8})\.db$")
_STALE = re.compile(r"^state-daily-\d{8}\.db\.partial$")
_LOCK = threading.Lock()  # one copy at a time: a sweep must not remove a copy being written


def copy_name(day: date) -> str:
    return f"state-daily-{day:%Y%m%d}.db"


def _daily_copies(folder: Path) -> list[Path]:
    """Our own copies in `folder`, newest day first (regular files only)."""
    found = [
        p for p in folder.iterdir() if _NAME.match(p.name) and p.is_file() and not p.is_symlink()
    ]
    return sorted(found, key=lambda p: p.name, reverse=True)


def daily_copy(db: Database, folder: Path, today: date, *, keep: int = KEEP) -> Path | None:
    """Copy the database to `folder` unless today's copy is there; then drop all but `keep`.

    Returns the new copy's path, or None when today already has one. The copy is written under
    a `.partial` name and renamed once complete, so a crash never leaves a half copy that looks
    whole. Raises `OSError` or `sqlite3.Error` if the copy cannot be made.
    """
    with _LOCK:
        return _daily_copy(db, folder, today, keep)


def _daily_copy(db: Database, folder: Path, today: date, keep: int) -> Path | None:
    folder.mkdir(parents=True, exist_ok=True)
    _sweep_partials(folder)
    final = folder / copy_name(today)
    if final.exists():
        _rotate(folder, keep)
        return None
    partial = final.with_name(final.name + ".partial")
    # A separate read-only connection: the online backup reads a consistent snapshot (WAL), and
    # the app's own writer is never held up for the length of the copy.
    source = sqlite3.connect(f"{db.path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(partial)
        try:
            source.backup(dest)
        finally:
            dest.close()
        os.replace(partial, final)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise
    finally:
        source.close()
    _rotate(folder, keep)
    return final


def _sweep_partials(folder: Path) -> None:
    """Remove half copies a crash left (only files of Sift's own `.partial` name)."""
    for p in folder.iterdir():
        if _STALE.match(p.name) and p.is_file() and not p.is_symlink():
            try:
                p.unlink()
            except OSError:
                log.warning("Could not remove the unfinished database copy %s", p.name)


def _rotate(folder: Path, keep: int) -> None:
    for old in _daily_copies(folder)[max(1, keep) :]:
        try:
            old.unlink()
        except OSError:
            log.warning("Could not remove the old database copy %s", old.name)


_RUN_NAME = re.compile(r"^state-run-\d{8}-\d{6}\.db$")


def run_copy(db: Database, folder: Path, when: datetime, *, keep: int = 5) -> Path | None:
    """A copy of the database named for this moment (`state-run-YYYYMMDD-HHMMSS.db`), then all
    but the newest `keep` of these copies are dropped. Only Sift's own database copies in its
    own folder are ever removed (plan assumption A9). None when a copy of that second exists."""
    with _LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        _sweep_partials(folder)
        final = folder / f"state-run-{when:%Y%m%d-%H%M%S}.db"
        if final.exists():
            return None
        partial = final.with_name(final.name + ".partial")
        source = sqlite3.connect(f"{db.path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            dest = sqlite3.connect(partial)
            try:
                source.backup(dest)
            finally:
                dest.close()
            os.replace(partial, final)
        except BaseException:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise
        finally:
            source.close()
        copies = sorted((p for p in folder.iterdir() if _RUN_NAME.match(p.name)), reverse=True)
        for old in copies[max(1, keep) :]:
            with contextlib.suppress(OSError):
                old.unlink()
        return final
