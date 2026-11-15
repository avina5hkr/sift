"""The app keeps its profile exclusive until process exit, even after bounded shutdown."""

import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from sift.core.instance import SingleInstanceLock


@contextmanager
def owner(code: str, *args: str) -> Iterator[tuple[subprocess.Popen[str], queue.Queue[str]]]:
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "SIFT_TRACKER": "off"},
    )
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line.rstrip())
        lines.put("EOF")

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        yield proc, lines
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        reader.join(timeout=5)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def expect_line(lines: queue.Queue[str], prefix: str) -> None:
    seen = []
    while True:
        line = lines.get(timeout=15)
        seen.append(line)
        if line.startswith(prefix):
            return
        assert line != "EOF", "\n".join(seen)


def send(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.write("continue\n")
    proc.stdin.flush()


def competing_lock(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from sift.core.instance import SingleInstanceLock; "
            "SingleInstanceLock(Path(sys.argv[1])).acquire()",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )


_APP_OWNER = """
import sys, threading
import sift.app as app
from sift.backup.lease import DRIVE_LEASE
from sift.core.shutdown import Shutdown
from sift.window_quit import WindowQuit

started, finish, finished = threading.Event(), threading.Event(), threading.Event()
def writer(ctx):
    with DRIVE_LEASE.hold('synthetic blocked SSD write', timeout_s=0):
        started.set()
        finish.wait()
    finished.set()
app.purge_quarantine = writer
real_build = app.build_jobs
def build(*args, **kwargs):
    assert started.wait(10)
    return real_build(*args, **kwargs)
app.build_jobs = build

real_run = Shutdown.run
entered = threading.Event()
def shutdown(self):
    if entered.is_set():
        return real_run(self)
    entered.set()
    if sys.argv[2] == 'blocked':
        self.add('synthetic blocked writer', finished.wait, timeout_s=0.05)
    real_run(self)
    assert not finished.is_set()
    print('LOGICAL_SHUTDOWN', flush=True)
    sys.stdin.readline()
    finish.set()
    assert finished.wait(10)
    print('WRITER_FINISHED', flush=True)
    sys.stdin.readline()
Shutdown.run = shutdown

if sys.argv[1] == 'window':
    def window(url, shutdown, ctx):
        replied = threading.Event()
        quit = WindowQuit(shutdown, ask_app_to_terminate=lambda: None,
                          reply_to_terminate=lambda ok: replied.set())
        quit.on_window_closing()
        quit.on_should_terminate()
        assert replied.wait(30)
    app._run_window = window
    args = []
else:
    args = ['--no-window', '--port', '0']
raise SystemExit(app.main(args))
"""


@pytest.mark.parametrize("route", ["headless", "window"])
@pytest.mark.parametrize("step", ["normal", "blocked"])
def test_app_refuses_restart_after_shutdown_until_process_exit(paths, route, step) -> None:
    with owner(_APP_OWNER, route, step) as (proc, lines):
        if route == "headless":
            expect_line(lines, "Sift running at ")
            proc.send_signal(signal.SIGTERM)
        expect_line(lines, "LOGICAL_SHUTDOWN")
        blocked = competing_lock(paths.lock)
        assert blocked.returncode != 0, "A second process acquired the profile before exit"
        assert "AlreadyRunningError" in blocked.stderr
        send(proc)
        expect_line(lines, "WRITER_FINISHED")
        assert competing_lock(paths.lock).returncode != 0
        send(proc)
        assert proc.wait(timeout=10) == 0
    assert competing_lock(paths.lock).returncode == 0


_RETAINED_OWNER = """
import gc, sys
from pathlib import Path
from sift.core.instance import SingleInstanceLock
lock = SingleInstanceLock(Path(sys.argv[1]))
lock.acquire()
lock.retain_until_process_exit()
lock.retain_until_process_exit()
lock.release()
lock.release()
del lock
gc.collect()
print('RETAINED', flush=True)
sys.stdin.readline()
"""


@pytest.mark.parametrize("killed", [False, True], ids=["normal-exit", "killed"])
def test_retained_lock_survives_cleanup_and_is_released_at_exit(tmp_path, killed) -> None:
    path = tmp_path / "sift.lock"
    with owner(_RETAINED_OWNER, str(path)) as (proc, lines):
        expect_line(lines, "RETAINED")
        blocked = competing_lock(path)
        assert blocked.returncode != 0
        assert "AlreadyRunningError" in blocked.stderr
        if killed:
            proc.kill()
        else:
            send(proc)
        assert proc.wait(timeout=10) == (-signal.SIGKILL if killed else 0)
    assert path.is_file()  # Its presence is not a stale lock.
    assert competing_lock(path).returncode == 0


def test_retention_requires_an_acquired_lock(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        SingleInstanceLock(tmp_path / "sift.lock").retain_until_process_exit()
