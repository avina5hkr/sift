"""Launcher: single-instance lock, database, local API server, and the desktop window."""

import argparse
import logging
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn

from sift import __version__
from sift.core.config import load_config
from sift.core.context import AppContext
from sift.core.db import (
    Database,
    DatabaseBusyError,
    DatabaseCorruptError,
    DatabaseTooNewError,
    MigrationError,
)
from sift.core.events import EventBus
from sift.core.instance import AlreadyRunningError, SingleInstanceLock
from sift.core.logging import install_crash_handler, setup_logging, teardown_logging
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths
from sift.device.fake_tracker import FAKE_ANNOUNCE_AFTER_S, fake_tracker
from sift.device.tracker import DeviceTracker, adb_tracker
from sift.server.app import create_app

_HOST = "127.0.0.1"
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0
_log = logging.getLogger("sift.app")


def bind_socket(port: int = 0) -> socket.socket:
    """A listening TCP socket on 127.0.0.1 only. Port 0 lets the OS pick a free one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((_HOST, port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock


def build_context(paths: AppPaths, token: str, port: int, web_dist: Path | None) -> AppContext:
    """Load the config, open and migrate the database, and assemble the shared context."""
    config, warnings = load_config(paths.config)
    db = Database(paths.db)
    db.open()
    try:
        apply_migrations(db, paths.db_backups)
    except BaseException:
        db.close()
        raise
    return AppContext(
        paths=paths,
        config=config,
        config_warnings=warnings,
        db=db,
        bus=EventBus(),
        token=token,
        allowed_hosts=frozenset({f"{_HOST}:{port}", f"localhost:{port}"}),
        version=__version__,
        web_dist=web_dist,
    )


class _UvicornErrorForwarder(logging.Handler):
    """Send uvicorn's own error log (it is not under `sift.`) into Sift's log."""

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger("sift.server.uvicorn").handle(record)


@dataclass
class ServerHandle:
    url: str
    server: uvicorn.Server
    thread: threading.Thread
    sock: socket.socket
    _forwarder: logging.Handler

    def stop(self) -> None:
        """Ask the server to exit, wait for its thread (bounded) and free the socket."""
        self.server.should_exit = True
        self.thread.join(_STOP_TIMEOUT_S)
        logging.getLogger("uvicorn.error").removeHandler(self._forwarder)
        self.sock.close()


def start_server(ctx: AppContext, sock: socket.socket) -> ServerHandle:
    """Run the API on a daemon thread over the already-bound `sock`; wait until it is up."""
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        create_app(ctx),
        access_log=False,  # access lines carry the query string, and with it the token
        log_config=None,
        log_level="warning",
        timeout_graceful_shutdown=2,  # an open SSE stream must not hold up shutdown
    )
    server = uvicorn.Server(config)
    forwarder = _UvicornErrorForwarder(level=logging.WARNING)
    logging.getLogger("uvicorn.error").addHandler(forwarder)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="sift-server", daemon=True
    )
    handle = ServerHandle(f"http://{_HOST}:{port}", server, thread, sock, forwarder)
    thread.start()
    deadline = time.monotonic() + _START_TIMEOUT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            handle.stop()
            raise RuntimeError("The Sift server did not start.")
        time.sleep(0.01)
    return handle


def tracker_mode(*, fake_phone: bool, no_window: bool, env: Mapping[str, str]) -> str:
    """Which device source to use: "adb" (default), "fake" or "off".

    `--fake-phone` and `SIFT_TRACKER=fake` are honoured only with `--no-window`, like
    SIFT_DEV_TOKEN: a normal Sift window always looks at the real phone. `SIFT_TRACKER=off`
    runs no tracker at all (tests use it so nothing ever contacts the real adb server).
    """
    wanted = env.get("SIFT_TRACKER", "adb").strip().lower()
    if no_window and (fake_phone or wanted == "fake"):
        return "fake"
    return "off" if wanted == "off" else "adb"


def fake_announce_delay(env: Mapping[str, str]) -> float:
    """Seconds before the fake phone appears: SIFT_FAKE_PHONE_DELAY_S, else 0.5.

    The browser tests set it very high, so the "No phone" chip is there to be seen, and then
    press Retry (which shows the phone at once) to watch the chip change without a reload.
    """
    try:
        delay = float(env["SIFT_FAKE_PHONE_DELAY_S"])
    except KeyError, ValueError:
        return FAKE_ANNOUNCE_AFTER_S
    return delay if 0 <= delay < float("inf") else FAKE_ANNOUNCE_AFTER_S


def build_tracker(mode: str, ctx: AppContext, env: Mapping[str, str]) -> DeviceTracker | None:
    if mode == "off":
        return None
    if mode == "fake":
        return fake_tracker(ctx, announce_after_s=fake_announce_delay(env))
    return adb_tracker(ctx)


def find_web_dist() -> Path | None:
    """The built UI folder: SIFT_WEB_DIST, the bundled copy when frozen, or <repo>/web/dist."""
    candidates: list[Path] = []
    override = os.environ.get("SIFT_WEB_DIST")
    if override:
        candidates.append(Path(override).expanduser().resolve())
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "web" / "dist")
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    return next((c for c in candidates if c.is_dir()), None)


def _wait_for_signal() -> None:
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        stop.wait()
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def close_windows(windows: Iterable[Any]) -> None:
    """Destroy every window, so the GUI loop ends and `main` can clean up."""
    for window in list(windows):
        window.destroy()


@contextmanager
def _close_windows_on_signal(get_windows: Callable[[], Iterable[Any]]) -> Iterator[None]:
    """SIGINT/SIGTERM close the windows instead of killing the process mid-cleanup."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def on_signal(signum: int, frame: FrameType | None) -> None:
        close_windows(get_windows())

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def _quiet_pywebview_logger() -> None:
    """pywebview logs the page URL, with its token, at DEBUG when PYWEBVIEW_LOG=debug."""
    logging.getLogger("pywebview").setLevel(logging.INFO)


def _run_window(url: str) -> None:
    import webview  # imported here so headless runs never load Cocoa

    _quiet_pywebview_logger()
    webview.create_window("Sift", url, width=1280, height=800, min_size=(1000, 680))
    with _close_windows_on_signal(lambda: webview.windows):
        webview.start()


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="sift", description="Back up an Android phone.")
    parser.add_argument("--no-window", action="store_true", help="serve the UI without a window")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: any)")
    parser.add_argument(
        "--fake-phone",
        action="store_true",
        help="with --no-window: show a fake phone instead of looking for a real one (dev, tests)",
    )
    args = parser.parse_args(argv)

    paths = AppPaths.default()
    paths.ensure()
    lock = SingleInstanceLock(paths.lock)
    try:
        lock.acquire()
    except AlreadyRunningError:
        print("Sift is already running.")
        return 1

    ctx: AppContext | None = None
    sock: socket.socket | None = None
    handle: ServerHandle | None = None
    tracker: DeviceTracker | None = None
    logging_on = False
    try:
        install_crash_handler(paths)
        token = secrets.token_urlsafe(32)
        if args.no_window and os.environ.get("SIFT_DEV_TOKEN"):
            token = os.environ["SIFT_DEV_TOKEN"]
        try:
            # The port is only known after binding, so bind first and let build_context follow.
            sock = bind_socket(args.port)
        except OSError:
            print(
                f"Sift can't use port {args.port} because another program is using it. "
                "Try a different --port, or leave it out."
            )
            return 2
        try:
            ctx = build_context(paths, token, sock.getsockname()[1], find_web_dist())
        except DatabaseTooNewError:
            print(
                "This database was made by a newer version of Sift. "
                "Install the newer version, or move state.db aside."
            )
            return 2
        except DatabaseCorruptError:
            print(
                "The Sift database is damaged. "
                "Move state.db aside and restart; your backups are not affected."
            )
            return 2
        except DatabaseBusyError as e:
            print(e)
            return 2
        except MigrationError as e:
            print(
                f"Sift could not update its database: {e}. "
                "Nothing was changed; your backups are not affected."
            )
            return 2
        setup_logging(paths, ctx.bus, level="INFO")
        logging_on = True
        for warning in ctx.config_warnings:
            _log.warning("%s", warning)
        handle = start_server(ctx, sock)
        mode = tracker_mode(fake_phone=args.fake_phone, no_window=args.no_window, env=os.environ)
        tracker = build_tracker(mode, ctx, os.environ)
        if tracker is not None:
            ctx.tracker = tracker
            tracker.start()
        url = f"{handle.url}/?t={token}"
        if args.no_window:
            print(f"Sift running at {url}", flush=True)
            _wait_for_signal()
        else:
            _run_window(url)
        return 0
    finally:
        if tracker is not None:
            tracker.stop()  # before the database closes: the tracker writes to it
        if handle is not None:
            handle.stop()
        elif sock is not None:
            sock.close()
        if ctx is not None:
            ctx.db.close()
        if logging_on:
            teardown_logging()
        lock.release()
