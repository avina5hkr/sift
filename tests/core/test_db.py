import sqlite3
import threading
import time

import pytest

from sift.core.db import Database, DatabaseBusyError, DatabaseCorruptError


def test_pragmas(db):
    with db.read() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        c.execute(
            "INSERT INTO phone_file(device_id, remote_path, origin, size, mtime, state)"
            " VALUES (999, '/sdcard/x.jpg', 'other', 1, 1, 'discovered')"
        )


def test_tx_rolls_back_on_exception(db):
    with pytest.raises(RuntimeError), db.tx() as c:
        c.execute("INSERT INTO setting VALUES ('a', '1')")
        raise RuntimeError
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM setting").fetchone()[0] == 0


def test_concurrent_writers(db):
    def work(n):
        for i in range(50):
            with db.tx() as c:
                c.execute("INSERT INTO setting VALUES (?, '1')", (f"{n}-{i}",))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM setting").fetchone()[0] == 400


def test_garbage_file_is_reported_corrupt(tmp_path):
    p = tmp_path / "state.db"
    p.write_bytes(b"not a database" * 100)
    with pytest.raises(DatabaseCorruptError):
        Database(p).open()


def test_close_closes_other_threads_read_connections(db):
    seen = []

    def work():
        with db.read() as c:
            seen.append(c)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        seen[0].execute("SELECT 1")


def _hold_write_lock(db):
    holder = sqlite3.connect(db.path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_tx_retries_until_the_lock_is_released(db):
    holder = _hold_write_lock(db)
    timer = threading.Timer(0.2, holder.rollback)
    timer.start()
    try:
        with db.tx() as c:
            c.execute("INSERT INTO setting VALUES ('a', '1')")
    finally:
        timer.join()
        holder.close()
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM setting").fetchone()[0] == 1


def test_tx_gives_up_with_a_plain_error_and_frees_the_lock(db):
    holder = _hold_write_lock(db)
    try:
        start = time.monotonic()
        with pytest.raises(DatabaseBusyError, match="database is busy") as info, db.tx():
            pass
        assert time.monotonic() - start < 3
        assert isinstance(info.value.__cause__, sqlite3.OperationalError)
        holder.rollback()
        with db.tx() as c:  # the process-wide lock was released
            c.execute("INSERT INTO setting VALUES ('a', '1')")
    finally:
        holder.close()


def test_short_lived_threads_leave_no_read_connections(db):
    db.release_thread()  # the fixture's migrations read on this thread; start from none

    def read_once():
        with db.read() as c:
            c.execute("SELECT 1").fetchone()

    for _ in range(20):
        t = threading.Thread(target=read_once)
        t.start()
        t.join()
    assert db.open_read_connections() == 0


def test_a_live_thread_keeps_its_read_connection_until_released(db):
    with db.read() as first:
        pass
    with db.read() as again:
        assert again is first
    assert db.open_read_connections() == 1
    db.release_thread()
    assert db.open_read_connections() == 0
    db.release_thread()  # nothing left to release; harmless
    with db.read() as c:  # and the thread can read again
        assert c.execute("SELECT 1").fetchone()[0] == 1
    assert db.open_read_connections() == 1


def test_release_thread_closes_the_connection(db):
    with db.read() as c:
        pass
    db.release_thread()
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_close_still_closes_live_readers(paths):
    database = Database(paths.db)
    database.open()
    seen = []
    done = threading.Event()
    stop = threading.Event()

    def reader():
        with database.read() as c:
            seen.append(c)
        done.set()
        stop.wait(2)

    t = threading.Thread(target=reader)
    t.start()
    assert done.wait(2)
    assert database.open_read_connections() == 1
    database.close()
    assert database.open_read_connections() == 0
    with pytest.raises(sqlite3.ProgrammingError):
        seen[0].execute("SELECT 1")
    stop.set()
    t.join()


def test_write_connection_is_migration_only(db):
    assert not hasattr(db, "write_connection")
    with db._write_connection() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
