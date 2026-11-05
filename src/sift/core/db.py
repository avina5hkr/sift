"""SQLite access: one serialized write connection, thread-local read connections."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Held by every writer in the process, whichever Database it goes through.
_WRITE_LOCK = threading.Lock()

# Pauses (seconds) between attempts when BEGIN IMMEDIATE finds the database locked.
_LOCK_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


class DatabaseCorruptError(Exception):
    """The database file is damaged or is not a SQLite database."""


class DatabaseTooNewError(Exception):
    """The database was written by a newer version of Sift."""


class DatabaseBusyError(Exception):
    """Another connection held the write lock for longer than the retries allow."""


class MigrationError(Exception):
    """A schema migration failed and was rolled back."""


class Database:
    """The Sift state database.

    Writes go through `tx()` on one connection under a process-wide lock. Reads use one
    connection per thread, so they never wait on a writer (WAL mode).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._writer: sqlite3.Connection | None = None
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._read_conns_lock = threading.Lock()

    def open(self) -> None:
        """Connect, set pragmas and run `PRAGMA quick_check`.

        Raises DatabaseCorruptError for any sqlite3.DatabaseError on the way: a file that is
        not a database fails at the very first statement.
        """
        if self._writer is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            rows = conn.execute("PRAGMA quick_check").fetchall()
            if [r[0] for r in rows] != ["ok"]:
                detail = "; ".join(str(r[0]) for r in rows[:5])
                raise DatabaseCorruptError(f"The Sift database is damaged: {detail}")
        except sqlite3.DatabaseError as e:
            if conn is not None:
                conn.close()
            raise DatabaseCorruptError(f"The Sift database is damaged: {e}") from e
        except DatabaseCorruptError:
            if conn is not None:
                conn.close()
            raise
        # From here on SQLite must not wait on its own: tx() runs the bounded retry schedule.
        conn.execute("PRAGMA busy_timeout = 0")
        self._writer = conn

    def _connect(self) -> sqlite3.Connection:
        # Explicit transactions (isolation_level=None). check_same_thread=False lets close()
        # shut every connection from one thread; each is still used by one thread at a time.
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """This thread's read-only connection."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            if self._writer is None:
                raise RuntimeError("The database is not open")
            conn = self._connect()
            try:
                conn.execute("PRAGMA query_only = ON")
            except BaseException:
                conn.close()
                raise
            with self._read_conns_lock:
                self._read_conns.append(conn)
            self._local.conn = conn
        yield conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: BEGIN IMMEDIATE, commit on success, roll back on any error."""
        with self.write_connection() as conn:
            for delay in (*_LOCK_RETRY_DELAYS, None):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as e:
                    if "database is locked" not in str(e):
                        raise
                    if delay is None:
                        raise DatabaseBusyError(
                            "The Sift database is busy. Another program may be using it; try again."
                        ) from e
                    time.sleep(delay)
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """The write connection with the process-wide lock held and no transaction begun.

        For code that manages its own transaction (the migration runner). Use `tx()` otherwise.
        """
        if self._writer is None:
            raise RuntimeError("The database is not open")
        with _WRITE_LOCK:
            yield self._writer

    def close(self) -> None:
        """Close every connection this Database opened, including other threads' readers."""
        with _WRITE_LOCK:
            writer, self._writer = self._writer, None
            if writer is not None:
                writer.close()
        with self._read_conns_lock:
            conns, self._read_conns = self._read_conns, []
            self._local = threading.local()
        for conn in conns:
            conn.close()
