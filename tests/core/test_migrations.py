import pytest

from sift.core.db import Database
from sift.core.migrations import (
    DatabaseTooNewError,
    Migration,
    MigrationError,
    apply_migrations,
    bundled_migrations,
    schema_version,
)

LATEST = bundled_migrations()[-1].version

SPEC_TABLES = {
    "device", "destination", "content", "phone_file", "replica", "scan", "job",
    "near_group", "near_member", "cluster", "cluster_member", "label", "calibration",
    "candidate", "selection", "deletion_log", "quarantine", "setting", "schema_migration",
}  # fmt: skip


def test_bundled_migrations_start_at_version_1():
    found = bundled_migrations()
    assert found[0].version == 1
    assert found[0].name == "init"
    assert [m.version for m in found] == sorted(m.version for m in found)


def test_fresh_db_gets_spec_schema(paths):
    db = Database(paths.db)
    db.open()
    try:
        assert schema_version(db) == 0
        assert apply_migrations(db, paths.db_backups) == LATEST
        with db.read() as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names >= SPEC_TABLES
        assert list(paths.db_backups.iterdir()) == []  # nothing to copy on a fresh DB
    finally:
        db.close()


def test_idempotent(db, paths):
    assert apply_migrations(db, paths.db_backups) == LATEST
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == LATEST


def test_refuses_newer_db(db, paths):
    with db.tx() as c:
        c.execute("INSERT INTO schema_migration VALUES (99, 0)")
    with pytest.raises(DatabaseTooNewError, match="newer version of Sift"):
        apply_migrations(db, paths.db_backups)


def test_copies_before_migrating(db, paths):
    extra = [*bundled_migrations(), Migration(LATEST + 1, "add_t", "CREATE TABLE t (x INTEGER);")]
    assert apply_migrations(db, paths.db_backups, extra) == LATEST + 1
    assert [p.name.startswith(f"state-v{LATEST}-") for p in paths.db_backups.iterdir()] == [True]


def test_failed_migration_leaves_no_partial_state(db, paths):
    bad = [
        *bundled_migrations(),
        Migration(LATEST + 1, "bad", "CREATE TABLE t (x); SELECT * FROM nope;"),
    ]
    with pytest.raises(MigrationError):
        apply_migrations(db, paths.db_backups, bad)
    assert schema_version(db) == LATEST
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[0] == 0
