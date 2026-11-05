import sqlite3
import threading

import pytest

from sift.core.db import Database, DatabaseCorruptError


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
