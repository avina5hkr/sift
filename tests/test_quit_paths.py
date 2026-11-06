"""Quitting mid-backup: stop at a file boundary, close the database cleanly, free the lock.

Until the real backup job lands (plan Task 12), the job is the test-only slow job behind
SIFT_TEST_SLOW_JOB=1. The window routes (Cmd-Q, the close button) are checked by hand.
"""

import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sift.app import start_test_jobs
from sift.core.context import AppContext
from sift.core.instance import SingleInstanceLock
from sift.core.paths import AppPaths

_OPEN_DB = (
    "from sift.core.db import Database\n"
    "from sift.core.migrations import apply_migrations\n"
    "from sift.core.paths import AppPaths\n"
    "p = AppPaths.default()\n"
    "db = Database(p.db)\n"
    "db.open()\n"
    "apply_migrations(db, p.db_backups)\n"
    "with db.read() as c:\n"
    "    print(c.execute('SELECT count(*) FROM replica').fetchone()[0])\n"
    "db.close()\n"
)


def _final_files(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def _quit_mid_backup(paths: AppPaths, home: Path, sig: signal.Signals) -> None:
    root = home / "slow-job"
    proc = subprocess.Popen(
        [sys.executable, "-m", "sift", "--no-window", "--fake-phone", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "SIFT_TEST_SLOW_JOB": "1"},
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().startswith("Sift running at ")
        deadline = time.monotonic() + 15
        while not root.is_dir() or len(_final_files(root)) < 2:
            assert proc.poll() is None
            assert time.monotonic() < deadline, "the slow job never wrote a file"
            time.sleep(0.02)
        time.sleep(0.05)  # most of a file's time is spent mid-write
        proc.send_signal(sig)
        assert proc.wait(timeout=40) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    # The database was closed cleanly: the last close checkpoints and removes the WAL, so the
    # next open has nothing to recover. (Checked first: opening it again recreates the WAL.)
    assert not paths.db.with_name(paths.db.name + "-wal").exists()

    lock = SingleInstanceLock(paths.lock)
    lock.acquire()  # at once: raises AlreadyRunningError if anything still holds it
    lock.release()

    # A new process opens (and quick-checks) the database without complaint.
    reopened = subprocess.run(
        [sys.executable, "-c", _OPEN_DB], capture_output=True, text=True, check=False
    )
    assert reopened.returncode == 0, reopened.stderr
    assert reopened.stderr == ""

    # No file under a final name without its replica row, and no half-written file left.
    files = _final_files(root)
    assert int(reopened.stdout) == len(files) >= 2
    assert list((root / ".staging").iterdir()) == []

    conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT r.rel_path, r.content_hash, r.size FROM replica r "
            "JOIN destination d ON d.id = r.dest_id WHERE d.root_path = ?",
            (str(root),),
        ).fetchall()
    finally:
        conn.close()
    replicas = {rel: (h, size) for rel, h, size in rows}
    assert set(files) == set(replicas)
    for name, data in files.items():
        assert replicas[name] == (hashlib.sha256(data).hexdigest(), len(data))


def test_sigterm_stops_job_at_file_boundary_and_releases_lock(
    paths: AppPaths, _isolate_home: Path
) -> None:
    _quit_mid_backup(paths, _isolate_home, signal.SIGTERM)


def test_sigint_stops_job_at_file_boundary_and_releases_lock(
    paths: AppPaths, _isolate_home: Path
) -> None:
    _quit_mid_backup(paths, _isolate_home, signal.SIGINT)


# -- the test-hook gate --------------------------------------------------------------------------


def test_test_hooks_ignored_when_frozen(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("SIFT_TEST_SLOW_JOB", "1")
    monkeypatch.delenv("SIFT_ENABLE_TEST_HOOKS", raising=False)
    jobs = start_test_jobs(ctx)
    try:
        assert jobs is None  # no slow job: the normal (for now, empty) job set runs
    finally:
        if jobs is not None:
            jobs.stop(timeout_s=5)


def test_test_hooks_honoured_when_frozen_with_enable_flag(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("SIFT_TEST_SLOW_JOB", "1")
    monkeypatch.setenv("SIFT_ENABLE_TEST_HOOKS", "1")
    jobs = start_test_jobs(ctx)
    try:
        assert jobs is not None
        assert jobs.running
    finally:
        if jobs is not None:
            assert jobs.stop(timeout_s=5)


def test_slow_job_needs_a_separate_data_folder(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like the fake phone, the slow job must never write into the owner's real database."""
    monkeypatch.setenv("SIFT_TEST_SLOW_JOB", "1")
    monkeypatch.delenv("SIFT_HOME")
    assert start_test_jobs(ctx) is None
