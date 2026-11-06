"""A daily copy of `state.db` in Application Support, through SQLite's online backup API.

`daily_copy` makes at most one copy per calendar day, named `state-daily-YYYYMMDD.db`, and keeps
the newest `KEEP` of them. It only ever looks at files of that exact name in the folder it is
given, so the copies `apply_migrations` makes (`state-v<N>-...db`) and anything else there are
never touched. The copy to an SSD is a later milestone.
"""

import contextlib
import logging
import os
import re
import sqlite3
from datetime import date
from pathlib import Path

from sift.core.db import Database

log = logging.getLogger(__name__)

KEEP = 7
_NAME = re.compile(r"^state-daily-(\d{8})\.db$")


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
    folder.mkdir(parents=True, exist_ok=True)
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


def _rotate(folder: Path, keep: int) -> None:
    for old in _daily_copies(folder)[max(1, keep) :]:
        try:
            old.unlink()
        except OSError:
            log.warning("Could not remove the old database copy %s", old.name)
