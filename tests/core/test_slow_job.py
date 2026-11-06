import hashlib
import threading
import time
from pathlib import Path

from sift.core.db import Database
from sift.core.slow_job import SlowJob


def _replicas(db: Database, root: Path) -> dict[str, tuple[str, int]]:
    with db.read() as c:
        rows = c.execute(
            "SELECT r.rel_path, r.content_hash, r.size FROM replica r "
            "JOIN destination d ON d.id = r.dest_id WHERE d.root_path = ?",
            (str(root),),
        ).fetchall()
    return {rel: (h, size) for rel, h, size in rows}


def _final_files(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def test_stop_finishes_the_current_file_first(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "slow"
    job = SlowJob(db, root, file_size=8 * 1024, chunks=8, chunk_pause_s=0.02)
    job.start()
    deadline = time.monotonic() + 5
    while job.files_done < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.05)  # mid-file
    assert job.stop(timeout_s=5)
    files = _final_files(root)
    replicas = _replicas(db, root)
    assert len(files) >= 2
    assert set(files) == set(replicas)
    for name, data in files.items():
        assert replicas[name] == (hashlib.sha256(data).hexdigest(), len(data))
    assert list((root / ".staging").iterdir()) == []
    assert job.files_done == len(files)


def test_stops_by_itself_after_max_files(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "slow"
    job = SlowJob(db, root, file_size=1024, chunks=1, chunk_pause_s=0, max_files=3)
    job.start()
    deadline = time.monotonic() + 5
    while job.running:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert job.files_done == 3
    assert len(_replicas(db, root)) == 3


def test_a_second_run_reuses_the_destination_row(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "slow"
    for _ in range(2):
        job = SlowJob(db, root, file_size=1024, chunks=1, chunk_pause_s=0, max_files=2)
        job.start()
        deadline = time.monotonic() + 5
        while job.files_done < 2:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        job.stop(timeout_s=5)
    with db.read() as c:
        (dests,) = c.execute(
            "SELECT count(*) FROM destination WHERE root_path = ?", (str(root),)
        ).fetchone()
    assert dests == 1
    assert len(_replicas(db, root)) == 4


def test_stop_before_start_is_harmless(db: Database, tmp_path: Path) -> None:
    job = SlowJob(db, tmp_path / "slow")
    assert job.stop(timeout_s=1)


def test_runs_on_its_own_thread(db: Database, tmp_path: Path) -> None:
    job = SlowJob(db, tmp_path / "slow", file_size=1024, chunks=1, chunk_pause_s=0.01)
    before = {t.name for t in threading.enumerate()}
    job.start()
    try:
        assert "sift-test-slow-job" in {t.name for t in threading.enumerate()} - before
    finally:
        job.stop(timeout_s=5)
