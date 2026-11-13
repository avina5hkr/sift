"""Migrations 0003_run_summary and 0004_run_summary_discarded, applied to databases that
already hold data (an M2 install from before each of them)."""

import json

from sift.backup.puller import PullStats
from sift.backup.summary import latest_summary, record_summary
from sift.core.db import Database
from sift.core.migrations import Migration, apply_migrations, bundled_migrations, schema_version
from sift.core.paths import AppPaths


def _up_to(version: int) -> list[Migration]:
    return [m for m in bundled_migrations() if m.version <= version]


def _populate_v2(db: Database) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO device (id, uuid, serial, android_id) VALUES (1, 'u-1', 'S1', 'a')")
        c.execute(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, set_name,"
            " rel_path) VALUES (1, '/sdcard/DCIM/a.jpg', 'camera', 10, 20, 'done', 'Camera',"
            " 'Phone/DCIM/a.jpg')"
        )
        c.execute(
            "INSERT INTO job (id, type, state, params, started_at, finished_at) "
            "VALUES (3, 'backup', 'done', '{}', 1, 2)"
        )
        c.execute(
            "INSERT INTO scan (id, device_id, started_at, sets, state) "
            "VALUES (4, 1, 1, '[\"Camera\"]', 'applied')"
        )


def _check_v2_data(db: Database) -> None:
    with db.read() as c:
        assert c.execute("SELECT uuid, android_id FROM device").fetchall() == [("u-1", "a")]
        assert c.execute(
            "SELECT remote_path, state, set_name, rel_path FROM phone_file"
        ).fetchall() == [("/sdcard/DCIM/a.jpg", "done", "Camera", "Phone/DCIM/a.jpg")]
        assert c.execute("SELECT id, state FROM job").fetchall() == [(3, "done")]
        assert c.execute("SELECT id, state FROM scan").fetchall() == [(4, "applied")]


def test_a_populated_v2_database_upgrades_through_0003_and_0004(paths: AppPaths) -> None:
    db = Database(paths.db)
    db.open()
    try:
        assert apply_migrations(db, paths.db_backups, _up_to(2)) == 2
        _populate_v2(db)
        assert apply_migrations(db, paths.db_backups) == bundled_migrations()[-1].version
        assert schema_version(db) >= 4
        assert any(p.name.startswith("state-v2-") for p in paths.db_backups.iterdir())
        _check_v2_data(db)
        with db.read() as c:
            assert c.execute("SELECT COUNT(*) FROM run_summary").fetchone() == (0,)
        # The new table takes a summary, read back with the flag that 0004 added.
        record_summary(
            db,
            job_id=3,
            device_id=1,
            sets=["Camera"],
            outcome="done",
            started_at=1,
            finished_at=2,
            stats=PullStats(copied=1, bytes_copied=10),
            warnings=[],
        )
        found = latest_summary(db, device_id=1)
        assert found is not None and found["outcome"] == "done" and found["new_files"] == 1
    finally:
        db.close()


def test_a_populated_v3_database_keeps_its_summaries_through_0004(paths: AppPaths) -> None:
    db = Database(paths.db)
    db.open()
    try:
        assert apply_migrations(db, paths.db_backups, _up_to(3)) == 3
        _populate_v2(db)
        with db.tx() as c:  # a summary written by the version that had 0003 only
            c.execute(
                "INSERT INTO run_summary (job_id, device_id, sets, outcome, started_at, "
                "finished_at, new_files, bytes, warnings) "
                "VALUES (3, 1, ?, 'stopped', 1, 5, 2, 20, ?)",
                (json.dumps(["Camera"]), json.dumps(["a warning"])),
            )
        assert apply_migrations(db, paths.db_backups) == bundled_migrations()[-1].version
        assert any(p.name.startswith("state-v3-") for p in paths.db_backups.iterdir())
        _check_v2_data(db)
        with db.read() as c:
            assert c.execute("SELECT job_id, outcome, discarded FROM run_summary").fetchall() == [
                (3, "stopped", 0)  # an old stopped run is not taken for a discarded one
            ]
        found = latest_summary(db, device_id=1)
        assert found is not None
        assert (found["outcome"], found["new_files"], found["warnings"]) == (
            "stopped",
            2,
            ["a warning"],
        )
    finally:
        db.close()
