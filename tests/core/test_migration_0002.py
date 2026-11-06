"""Migration 0002_backup: backup scan staging, set tags, two-part identity."""

import sqlite3

import pytest

from sift.core.db import Database
from sift.core.migrations import Migration, apply_migrations, bundled_migrations, schema_version


def _columns(db: Database, table: str) -> dict[str, tuple[str, bool]]:
    """Column name -> (declared type, NOT NULL) for a table."""
    with db.read() as c:
        return {r[1]: (r[2], bool(r[3])) for r in c.execute(f"PRAGMA table_info({table})")}


def _only_v1() -> list[Migration]:
    return [m for m in bundled_migrations() if m.version == 1]


def test_0002_is_bundled_after_init():
    found = bundled_migrations()
    assert [m.version for m in found][:2] == [1, 2]
    assert found[1].name == "backup"


def test_fresh_db_reaches_the_latest_version(db):
    assert schema_version(db) == bundled_migrations()[-1].version


def test_copy_is_taken_before_0002_and_existing_m1_data_survives(paths):
    db = Database(paths.db)
    db.open()
    try:
        assert apply_migrations(db, paths.db_backups, _only_v1()) == 1
        with db.tx() as c:
            c.execute("INSERT INTO device (id, uuid, serial, model) VALUES (1, 'u-1', 'S1', 'M')")
            c.execute(
                "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state)"
                " VALUES (1, '/sdcard/DCIM/a.jpg', 'camera', 10, 20, 'done')"
            )
            c.execute("INSERT INTO scan (id, device_id, started_at, stats) VALUES (5, 1, 7, '{}')")

        assert apply_migrations(db, paths.db_backups) == bundled_migrations()[-1].version

        copies = list(paths.db_backups.iterdir())
        assert [p.name.startswith("state-v1-") for p in copies] == [True]
        old = sqlite3.connect(copies[0])
        try:  # the copy is the schema as it was: no new columns yet
            names = {r[1] for r in old.execute("PRAGMA table_info(device)")}
        finally:
            old.close()
        assert "android_id" not in names

        with db.read() as c:
            assert c.execute("SELECT uuid, serial, model, android_id FROM device").fetchall() == [
                ("u-1", "S1", "M", None)
            ]
            assert c.execute(
                "SELECT remote_path, state, present, set_name, rel_path FROM phone_file"
            ).fetchall() == [("/sdcard/DCIM/a.jpg", "done", 1, None, None)]
            # Scans from before the backup flow are history: applied, with no sets or warnings.
            assert c.execute("SELECT id, stats, sets, state, warnings FROM scan").fetchall() == [
                (5, "{}", None, "applied", None)
            ]
    finally:
        db.close()


def test_new_columns_exist(db):
    assert _columns(db, "device")["android_id"] == ("TEXT", False)
    phone_file = _columns(db, "phone_file")
    assert phone_file["set_name"] == ("TEXT", False)
    assert phone_file["rel_path"] == ("TEXT", False)
    scan = _columns(db, "scan")
    assert scan["sets"] == ("TEXT", False)
    assert scan["state"][0] == "TEXT"
    assert scan["warnings"] == ("TEXT", False)


def test_new_tables_have_the_documented_columns(db):
    assert _columns(db, "scan_entry") == {
        "scan_id": ("INTEGER", True),
        "path": ("TEXT", True),
        "size": ("INTEGER", True),
        "mtime": ("INTEGER", True),
        "set_name": ("TEXT", True),
        "folder": ("TEXT", True),
    }
    assert _columns(db, "folder_count") == {
        "device_id": ("INTEGER", True),
        "folder": ("TEXT", True),
        "set_name": ("TEXT", True),
        "present_files": ("INTEGER", True),
        "scan_id": ("INTEGER", True),
    }


def test_phone_file_set_index_covers_device_set_present(db):
    with db.read() as c:
        indexes = {r[0] for r in c.execute("SELECT name FROM pragma_index_list('phone_file')")}
        cols = [
            r[0]
            for r in c.execute(
                "SELECT name FROM pragma_index_info('ix_phone_file_set') ORDER BY seqno"
            )
        ]
    assert "ix_phone_file_set" in indexes
    assert cols == ["device_id", "set_name", "present"]


def test_scan_state_is_checked(db):
    with db.tx() as c:
        c.execute("INSERT INTO scan (id, state) VALUES (1, 'awaiting_confirmation')")
    for bad in ("done", ""):
        with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
            c.execute("INSERT INTO scan (state) VALUES (?)", (bad,))
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        c.execute("UPDATE scan SET state = 'nonsense' WHERE id = 1")


def test_a_scan_with_no_state_is_applied(db):
    with db.tx() as c:
        c.execute("INSERT INTO scan (id) VALUES (1)")
    with db.read() as c:
        assert c.execute("SELECT state FROM scan").fetchone()[0] == "applied"


def test_scan_entry_and_folder_count_keys_and_references(db):
    with db.tx() as c:
        c.execute("INSERT INTO device (id, uuid) VALUES (1, 'u')")
        c.execute("INSERT INTO scan (id, device_id) VALUES (1, 1)")
        c.execute("INSERT INTO scan_entry VALUES (1, '/sdcard/DCIM/a.jpg', 1, 2, 'Camera', 'DCIM')")
        c.execute("INSERT INTO folder_count VALUES (1, 'DCIM', 'Camera', 1, 1)")
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:  # same path in the same scan
        c.execute("INSERT INTO scan_entry VALUES (1, '/sdcard/DCIM/a.jpg', 1, 2, 'Camera', 'DCIM')")
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:  # no such scan
        c.execute("INSERT INTO scan_entry VALUES (99, '/x', 1, 2, 'Camera', 'DCIM')")
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:  # one count per folder per device
        c.execute("INSERT INTO folder_count VALUES (1, 'DCIM', 'Camera', 2, 1)")
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:  # no such device
        c.execute("INSERT INTO folder_count VALUES (7, 'DCIM', 'Camera', 2, 1)")


def test_migration_does_not_seed_a_destination(db):
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM destination").fetchone()[0] == 0
