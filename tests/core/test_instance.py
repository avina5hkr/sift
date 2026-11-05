import subprocess
import sys
from pathlib import Path

from sift.core.instance import SingleInstanceLock


def _try_lock(path: Path) -> subprocess.CompletedProcess[str]:
    code = (
        "from pathlib import Path; from sift.core.instance import SingleInstanceLock as L; "
        f"L(Path({str(path)!r})).acquire()"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_second_instance_is_refused(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "sift.lock")
    lock.acquire()
    r = _try_lock(tmp_path / "sift.lock")
    assert r.returncode != 0
    assert "AlreadyRunningError" in r.stderr
    lock.release()
    assert _try_lock(tmp_path / "sift.lock").returncode == 0


def test_lock_of_killed_process_is_free(tmp_path: Path) -> None:
    p = tmp_path / "sift.lock"
    code = (
        "import os; from pathlib import Path; "
        "from sift.core.instance import SingleInstanceLock as L;"
        f"L(Path({str(p)!r})).acquire(); os.kill(os.getpid(), 9)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == -9
    lock = SingleInstanceLock(p)
    lock.acquire()  # must not raise
    lock.release()
