import itertools
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from sift.app import (
    _signals_request_shutdown,
    bind_socket,
    build_context,
    fake_announce_delay,
    main,
    start_server,
    tracker_mode,
)
from sift.core.db import Database, DatabaseBusyError
from sift.core.events import EventBus
from sift.core.instance import SingleInstanceLock
from sift.core.logging import setup_logging
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths
from sift.core.shutdown import Shutdown


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


def test_main_refuses_in_plain_words_when_migration_fails(
    paths: AppPaths, capfd: pytest.CaptureFixture[str]
) -> None:
    import sqlite3

    conn = sqlite3.connect(paths.db)
    conn.execute("CREATE TABLE device(x)")
    conn.commit()
    conn.close()
    assert main(["--no-window"]) == 2
    out, err = capfd.readouterr()
    assert "Sift could not update its database: " in out
    assert out.rstrip().endswith("Nothing was changed; your backups are not affected.")
    assert "Traceback" not in out + err
    lock = SingleInstanceLock(paths.lock)
    lock.acquire()
    lock.release()


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


def test_quiet_pywebview_logger_pins_info() -> None:
    from sift.app import _quiet_pywebview_logger

    logger = logging.getLogger("pywebview")
    saved = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        _quiet_pywebview_logger()
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(saved)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_headless_signal_requests_shutdown(sig: signal.Signals) -> None:
    shutdown = Shutdown()
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    with _signals_request_shutdown(shutdown):
        os.kill(os.getpid(), sig)
        assert shutdown.wait_requested(timeout=2)
        os.kill(os.getpid(), sig)  # a second one during cleanup is absorbed
        time.sleep(0.05)
    assert shutdown.reason == f"signal {sig.name}"
    assert {s: signal.getsignal(s) for s in before} == before  # handlers restored


# -- the device source (R26) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fake_phone", "no_window", "env", "expected"),
    [
        (False, True, {}, "adb"),
        (False, False, {}, "adb"),
        (True, True, {}, "fake"),
        (True, False, {}, "adb"),  # --fake-phone needs --no-window
        (False, True, {"SIFT_TRACKER": "fake"}, "fake"),
        (False, False, {"SIFT_TRACKER": "fake"}, "adb"),  # so does SIFT_TRACKER=fake
        (False, True, {"SIFT_TRACKER": "off"}, "off"),
        (False, False, {"SIFT_TRACKER": "off"}, "off"),
        (True, True, {"SIFT_TRACKER": "off"}, "fake"),  # the explicit flag wins
        (False, True, {"SIFT_TRACKER": "adb"}, "adb"),
        (False, True, {"SIFT_TRACKER": "nonsense"}, "adb"),
    ],
)
def test_tracker_mode(
    fake_phone: bool, no_window: bool, env: dict[str, str], expected: str
) -> None:
    assert tracker_mode(fake_phone=fake_phone, no_window=no_window, env=env) == expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 0.5),
        ({"SIFT_FAKE_PHONE_DELAY_S": "3600"}, 3600.0),
        ({"SIFT_FAKE_PHONE_DELAY_S": "0"}, 0.0),
        ({"SIFT_FAKE_PHONE_DELAY_S": "soon"}, 0.5),
        ({"SIFT_FAKE_PHONE_DELAY_S": "-1"}, 0.5),
        ({"SIFT_FAKE_PHONE_DELAY_S": "nan"}, 0.5),
        ({"SIFT_FAKE_PHONE_DELAY_S": "inf"}, 0.5),
    ],
)
def test_fake_announce_delay(env: dict[str, str], expected: float) -> None:
    assert fake_announce_delay(env) == expected


@contextmanager
def _run_headless(args: list[str], env: dict[str, str]) -> Iterator[tuple[str, str]]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "sift", "--no-window", "--port", "0", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "SIFT_DEV_TOKEN": "dev-tok", **env},
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        base, _, token = line.removeprefix("Sift running at ").partition("/?t=")
        yield base, token
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def _device(base: str, token: str) -> dict[str, object] | None:
    r = httpx.get(f"{base}/api/status", headers={"X-Sift-Token": token})
    device: dict[str, object] | None = r.json()["device"]
    return device


def test_headless_without_a_tracker_has_no_device(paths: AppPaths) -> None:
    with _run_headless([], {}) as (base, token):  # conftest sets SIFT_TRACKER=off
        assert _device(base, token) is None


@pytest.mark.parametrize(("args", "env"), [(["--fake-phone"], {}), ([], {"SIFT_TRACKER": "fake"})])
def test_headless_fake_phone_shows_up_and_stops_cleanly(
    paths: AppPaths, args: list[str], env: dict[str, str]
) -> None:
    with _run_headless(args, env) as (base, token):
        deadline = time.monotonic() + 10
        device = _device(base, token)
        while (device or {}).get("state") != "connected_usb":
            assert time.monotonic() < deadline, device
            time.sleep(0.1)
            device = _device(base, token)
        assert device is not None
        assert device["model"] == "TestPhone"
        devices = httpx.get(f"{base}/api/devices", headers={"X-Sift-Token": token}).json()
        assert [d["serial"] for d in devices] == ["FAKE-0001"]


def test_headless_fake_phone_with_a_long_delay_appears_on_retry(paths: AppPaths) -> None:
    headers = {"X-Sift-Token": "dev-tok"}
    with _run_headless(["--fake-phone"], {"SIFT_FAKE_PHONE_DELAY_S": "3600"}) as (base, token):
        time.sleep(0.5)
        device = _device(base, token)
        assert device is not None and device["state"] == "no_device"
        assert httpx.post(f"{base}/api/devices/retry", headers=headers).status_code == 204
        deadline = time.monotonic() + 5
        while (_device(base, token) or {}).get("state") != "connected_usb":
            assert time.monotonic() < deadline
            time.sleep(0.1)


# -- fake mode needs its own data folder (R28) ---------------------------------------------------

_NEEDS_HOME = (
    "Fake-phone mode needs a separate data folder. "
    "Set SIFT_HOME to a test folder (make dev does this).\n"
)


@pytest.mark.parametrize("home", [None, ""])
@pytest.mark.parametrize(
    ("argv", "env"),
    [(["--no-window", "--fake-phone"], {}), (["--no-window"], {"SIFT_TRACKER": "fake"})],
)
def test_fake_phone_without_sift_home_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home: str | None,
    argv: list[str],
    env: dict[str, str],
) -> None:
    fake_user = tmp_path / "user"
    fake_user.mkdir()
    monkeypatch.setenv("HOME", str(fake_user))  # the "real" ~/Library is a tmp folder
    if home is None:
        monkeypatch.delenv("SIFT_HOME")
    else:
        monkeypatch.setenv("SIFT_HOME", home)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert AppPaths.default().support == fake_user / "Library" / "Application Support" / "Sift"
    assert main(argv) == 2
    assert capsys.readouterr().out == _NEEDS_HOME
    assert list(fake_user.iterdir()) == []  # no folder, lock or database was created
