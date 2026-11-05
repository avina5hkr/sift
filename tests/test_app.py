import itertools
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from sift.app import (
    _close_windows_on_signal,
    bind_socket,
    build_context,
    close_windows,
    main,
    start_server,
)
from sift.core.db import Database, DatabaseBusyError
from sift.core.events import EventBus
from sift.core.instance import SingleInstanceLock
from sift.core.logging import setup_logging
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths


def test_live_server_serves_status_and_sse(paths: AppPaths) -> None:
    sock = bind_socket(0)
    port = sock.getsockname()[1]
    ctx = build_context(paths, token="t0k", port=port, web_dist=None)
    handle = start_server(ctx, sock)
    try:
        assert (
            httpx.get(f"{handle.url}/api/status", headers={"X-Sift-Token": "t0k"}).status_code
            == 200
        )
        assert httpx.get(f"{handle.url}/api/status?token=t0k").status_code == 401
        with httpx.stream("GET", f"{handle.url}/api/events?token=t0k", timeout=5) as s:
            lines = s.iter_lines()
            assert "event: hello" in [next(lines) for _ in range(3)]
            ctx.bus.publish("toast", {"text": "hi"})
            assert any(line == "event: toast" for line in itertools.islice(lines, 10))
        deadline = time.monotonic() + 2
        while ctx.bus.subscriber_count and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ctx.bus.subscriber_count == 0
    finally:
        handle.stop()
        ctx.db.close()


def test_token_never_logged(
    paths: AppPaths, bus: EventBus, capfd: pytest.CaptureFixture[str]
) -> None:
    setup_logging(paths, bus, level="DEBUG")
    records: list[tuple[str, str]] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append((record.name, record.getMessage()))

    logging.getLogger().addHandler(Grab(level=logging.DEBUG))
    secret = "SECRET-tok-123"
    sock = bind_socket(0)
    port = sock.getsockname()[1]
    ctx = build_context(paths, token=secret, port=port, web_dist=None)
    handle = start_server(ctx, sock)
    try:
        httpx.get(f"{handle.url}/?t={secret}")
        httpx.get(f"{handle.url}/api/status", headers={"X-Sift-Token": secret})
        httpx.get(f"{handle.url}/api/status?token={secret}")  # refused, still not logged
        with httpx.stream("GET", f"{handle.url}/api/events?token={secret}", timeout=5) as s:
            next(s.iter_lines())
    finally:
        handle.stop()
        ctx.db.close()
    out, err = capfd.readouterr()
    assert any("GET /api/status" in msg for _, msg in records)  # request logging is on
    for text in (out, err, (paths.logs / "sift.log").read_text(), *(m for _, m in records)):
        assert secret not in text
    assert not any(name == "uvicorn.access" for name, _ in records)


def test_main_exits_when_already_running(
    paths: AppPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = SingleInstanceLock(paths.lock)
    lock.acquire()
    try:
        assert main(["--no-window"]) == 1
        assert "Sift is already running." in capsys.readouterr().out
    finally:
        lock.release()


def test_main_refuses_newer_database(paths: AppPaths, capsys: pytest.CaptureFixture[str]) -> None:
    db = Database(paths.db)
    db.open()
    apply_migrations(db, paths.db_backups)
    with db.tx() as c:
        c.execute("INSERT INTO schema_migration VALUES (99, 0)")
    db.close()
    assert main(["--no-window"]) == 2
    assert "newer version of Sift" in capsys.readouterr().out
    # the early return released the lock
    lock = SingleInstanceLock(paths.lock)
    lock.acquire()
    lock.release()


def test_main_refuses_damaged_database(paths: AppPaths, capsys: pytest.CaptureFixture[str]) -> None:
    paths.db.write_bytes(b"this is not a sqlite database" * 50)
    assert main(["--no-window"]) == 2
    assert "database is damaged" in capsys.readouterr().out


def test_find_web_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sift.app import find_web_dist

    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setenv("SIFT_WEB_DIST", str(dist))
    assert find_web_dist() == dist
    monkeypatch.setenv("SIFT_WEB_DIST", "dist")
    monkeypatch.chdir(tmp_path)
    assert find_web_dist() == dist.resolve()
    monkeypatch.setenv("SIFT_WEB_DIST", str(tmp_path / "missing"))
    assert find_web_dist() != tmp_path / "missing"  # falls through to the next candidate


def test_headless_run_serves_and_stops_on_sigterm(paths: AppPaths) -> None:
    env = {**os.environ, "SIFT_DEV_TOKEN": "dev-tok"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "sift", "--no-window", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        base, _, token = line.removeprefix("Sift running at ").partition("/?t=")
        assert base.startswith("http://127.0.0.1:")
        assert token == "dev-tok"
        r = httpx.get(f"{base}/api/status", headers={"X-Sift-Token": token})
        assert r.status_code == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
        assert proc.stdout.read() == ""
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    lock = SingleInstanceLock(paths.lock)
    lock.acquire()  # the lock was released
    lock.release()


def test_main_reports_port_in_use(paths: AppPaths, capsys: pytest.CaptureFixture[str]) -> None:
    holder = bind_socket(0)
    port = holder.getsockname()[1]
    try:
        assert main(["--no-window", "--port", str(port)]) == 2
    finally:
        holder.close()
    out = capsys.readouterr().out
    assert out == (
        f"Sift can't use port {port} because another program is using it. "
        "Try a different --port, or leave it out.\n"
    )
    assert "Traceback" not in out
    lock = SingleInstanceLock(paths.lock)
    lock.acquire()  # the early return released the lock
    lock.release()


def test_main_reports_busy_database(
    paths: AppPaths, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(self: Database) -> None:
        raise DatabaseBusyError("Sift's database is in use by another program. Try again shortly.")

    monkeypatch.setattr(Database, "open", busy)
    assert main(["--no-window"]) == 2
    out = capsys.readouterr().out
    assert out == "Sift's database is in use by another program. Try again shortly.\n"
    lock = SingleInstanceLock(paths.lock)
    lock.acquire()
    lock.release()


def test_server_never_enables_uvicorn_access_log(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access lines carry the query string (and the token), so the setting itself is guarded."""
    import uvicorn

    seen: dict[str, object] = {}
    real_init = uvicorn.Config.__init__

    def spy(self: uvicorn.Config, *args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(uvicorn.Config, "__init__", spy)
    sock = bind_socket(0)
    ctx = build_context(paths, token="t0k", port=sock.getsockname()[1], web_dist=None)
    handle = start_server(ctx, sock)
    handle.stop()
    ctx.db.close()
    assert seen["access_log"] is False
    assert seen["log_config"] is None


class _FakeWindow:
    def __init__(self) -> None:
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


def test_close_windows_destroys_every_window() -> None:
    windows = [_FakeWindow(), _FakeWindow()]
    close_windows(windows)
    assert [w.destroyed for w in windows] == [1, 1]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_in_window_mode_closes_windows(sig: signal.Signals) -> None:
    windows = [_FakeWindow(), _FakeWindow()]
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    with _close_windows_on_signal(lambda: windows):
        os.kill(os.getpid(), sig)
        deadline = time.monotonic() + 2
        while not all(w.destroyed for w in windows) and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [w.destroyed for w in windows] == [1, 1]
    assert {s: signal.getsignal(s) for s in before} == before  # handlers restored
