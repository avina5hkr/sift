"""SQLite access: one serialized write connection, thread-local read connections."""

import logging
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_log = logging.getLogger(__name__)

# Held by every writer in the process, whichever Database it goes through.
_WRITE_LOCK = threading.Lock()

# Whether the current thread is inside a write (it holds _WRITE_LOCK). The lock is not reentrant,
# so code that would write on a thread already inside a write must be able to tell and refuse.
_WRITE_STATE = threading.local()

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


class _ReaderHandle:
    """What a thread keeps in its thread-local: its read connection plus a finalizer.

    The thread-local dict is dropped when its thread ends, which drops this handle, which runs
    the finalizer and closes the connection. So short-lived threads do not leak connections.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.release: Callable[[], None]  # set by Database.read(): closes `conn`, once


class Database:
    """The Sift state database.

    Writes go through `tx()` on one connection under a process-wide lock. Reads use one
    connection per thread, so they never wait on a writer (WAL mode). A thread's read
    connection is closed when the thread ends, or earlier by `release_thread()`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._writer: sqlite3.Connection | None = None
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        # Reentrant: a reader's finalizer can run (garbage collection, or a dropped thread-local) on
        # a thread that already holds it.
        self._read_conns_lock = threading.RLock()

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
        handle: _ReaderHandle | None = getattr(self._local, "handle", None)
        if handle is None:
            if self._writer is None:
                raise RuntimeError("The database is not open")
            conn = self._connect()
            try:
                conn.execute("PRAGMA query_only = ON")
            except BaseException:
                conn.close()
                raise
            handle = _ReaderHandle(conn)
            finalizer = weakref.finalize(handle, self._drop_reader, conn)
            finalizer.atexit = False
            handle.release = finalizer
            with self._read_conns_lock:
                self._read_conns.append(conn)
            self._local.handle = handle
        yield handle.conn

    def _drop_reader(self, conn: sqlite3.Connection) -> None:
        """Close one read connection and forget it. Runs when its thread ends; never raises."""
        try:
            with self._read_conns_lock:
                if conn in self._read_conns:
                    self._read_conns.remove(conn)
            conn.close()
        except Exception:
            _log.exception("Could not close a read connection")

    def release_thread(self) -> None:
        """Close this thread's read connection now. Job threads call it as they exit.

        Harmless when the thread has none; a later `read()` simply opens a new one.
        """
        handle: _ReaderHandle | None = getattr(self._local, "handle", None)
        if handle is not None:
            del self._local.handle
            handle.release()

    @staticmethod
    def in_write_tx() -> bool:
        """True when the calling thread is inside `tx()`: a write begun now would deadlock."""
        return bool(getattr(_WRITE_STATE, "active", False))

    def open_read_connections(self) -> int:
        """How many read connections are open across all threads (for tests and diagnostics)."""
        with self._read_conns_lock:
            return len(self._read_conns)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: BEGIN IMMEDIATE, commit on success, roll back on any error."""
        with self._write_connection() as conn:
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
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """The write connection with the process-wide lock held and no transaction begun.

        Migration-only, for code that manages its own transaction (the migration runner). Use
        `tx()` everywhere else.
        """
        if self._writer is None:
            raise RuntimeError("The database is not open")
        with _WRITE_LOCK:
            _WRITE_STATE.active = True
            try:
                yield self._writer
            finally:
                _WRITE_STATE.active = False

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
