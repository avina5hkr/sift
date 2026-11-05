"""Numbered SQL migrations, applied in order, each all-or-nothing."""

import contextlib
import os
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sift.core.db import Database, DatabaseTooNewError, MigrationError

_FILENAME = re.compile(r"^(\d{4})_(.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def bundled_migrations() -> list[Migration]:
    """The migrations shipped in `sift/resources/migrations`, sorted by version."""
    folder = resources.files("sift") / "resources" / "migrations"
    found: list[Migration] = []
    for entry in folder.iterdir():
        match = _FILENAME.match(entry.name)
        if match:
            found.append(
                Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")),
            )
    found.sort(key=lambda m: m.version)
    _require_unique(found)
    return found


def _require_unique(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("Two migrations share the same version number.")


def schema_version(db: Database) -> int:
    """The highest applied migration, or 0 for a database with no schema yet."""
    with db.read() as c:
        has_table = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
        ).fetchone()
        if has_table is None:
            return 0
        row = c.execute("SELECT max(version) FROM schema_migration").fetchone()
    return int(row[0] or 0)


def apply_migrations(
    db: Database, backup_dir: Path, migrations: Sequence[Migration] | None = None
) -> int:
    """Bring the database up to date and return its schema version.

    Refuses a database newer than the known migrations. Before touching a database that
    already has a schema, copies it into `backup_dir`. Each migration is one transaction, so
    a failure leaves the schema exactly as it was.
    """
    known = sorted(bundled_migrations() if migrations is None else migrations, key=_version)
    _require_unique(known)
    current = schema_version(db)
    latest = known[-1].version if known else 0
    if current > latest:
        raise DatabaseTooNewError(
            f"This data was saved by a newer version of Sift (schema {current}; "
            f"this version understands up to {latest}). Update Sift to open it."
        )
    pending = [m for m in known if m.version > current]
    if not pending:
        return current
    if current >= 1:
        _backup(db, backup_dir, current)
    for migration in pending:
        _apply(db, migration)
    return pending[-1].version


def _version(m: Migration) -> int:
    return m.version


def _apply(db: Database, migration: Migration) -> None:
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n;\n"
        "INSERT INTO schema_migration (version, applied_at) "
        f"VALUES ({int(migration.version)}, {int(time.time())});\n"
        "COMMIT;"
    )
    with db.write_connection() as conn:
        try:
            conn.executescript(script)
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.rollback()
            raise MigrationError(f"Migration {migration.version:04d} failed: {e}") from e


def _backup(db: Database, backup_dir: Path, version: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    final = backup_dir / f"state-v{version}-{stamp}.db"
    partial = final.with_name(final.name + ".partial")
    try:
        with db.write_connection() as src:
            dest = sqlite3.connect(partial)
            try:
                src.backup(dest)
            finally:
                dest.close()
        os.replace(partial, final)
    except (sqlite3.Error, OSError) as e:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise MigrationError(f"Could not copy the database before updating it: {e}") from e
    return final
