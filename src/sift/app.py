"""Launcher: single-instance lock, database, local API server, and the desktop window."""

import argparse
import contextlib
import json
import logging
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn

from sift import __version__
from sift.backup.readers import stored_read_speed
from sift.backup.recovery import recover
from sift.backup.run import replicate_now, run_backup
from sift.core import testhooks
from sift.core.clock import SystemClock
from sift.core.config import BackupConfig, Config, join_added_sets, load_config, save_config
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
from sift.core.jobs import JobControl, JobError, JobManager
from sift.core.logging import install_crash_handler, setup_logging, teardown_logging
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths
from sift.core.shutdown import Shutdown
from sift.core.slow_job import SlowJob
from sift.device.fake_tracker import FAKE_ANNOUNCE_AFTER_S, FakePhoneControl, fake_tracker
from sift.device.fake_transport import FaultPlan
from sift.device.tracker import DeviceTracker, adb_tracker
from sift.server.app import create_app
from sift.services.afterrun import after_run
from sift.services.crash import install as install_crash_reports
from sift.services.keepawake import KeepAwake, restore_at_launch
from sift.services.notify import MacNotifier, Notifier, NullNotifier
from sift.services.plainwords import describe
from sift.services.ssd import SsdWatcher
from sift.services.tasks import TaskRunner
from sift.window_quit import WindowQuit

_HOST = "127.0.0.1"
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0
# Time budgets for the shutdown steps (seconds). The jobs step waits for a file boundary.
_JOBS_STOP_TIMEOUT_S = 30.0
_TRACKER_STEP_S = 10.0
_SERVER_STEP_S = _STOP_TIMEOUT_S + 5
_LOGGING_STEP_S = 5.0
_DB_STEP_S = 10.0
_LOCK_STEP_S = 5.0
_SOCKET_STEP_S = 5.0
# SIFT_TEST_SLOW_PHONE=1: the fake phone serves this many bytes per second (quit-path tests).
SLOW_PHONE_BPS = 2_000
_AUTO_BACKUP_WAIT_S = 30.0
_log = logging.getLogger("sift.app")
FAKE_NEEDS_HOME_MESSAGE = (
    "Fake-phone mode needs a separate data folder. "
    "Set SIFT_HOME to a test folder (make dev does this)."
)


def keep_fake_backups_at_home(config: Config, sift_home: str, *, home: Path | None = None) -> None:
    """Fake-phone runs never back up into the real `~/PhoneBackup`.

    When the backup folder is the default one in the real home, it becomes
    `$SIFT_HOME/PhoneBackup` instead (in memory; the wizard saves whatever it shows). A folder
    chosen in config.toml elsewhere is kept.
    """
    home = home if home is not None else Path.home()

    def expand(text: str) -> Path:
        text = text.strip()
        return home / text[2:] if text == "~" or text.startswith("~/") else Path(text)

    if expand(config.backup.mac_root) == expand(BackupConfig().mac_root):
        config.backup.mac_root = str(Path(sift_home).expanduser().resolve() / "PhoneBackup")


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
    _join_added_sets(config, db, paths.config, warnings)
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


def _join_added_sets(config: Config, db: Database, path: Path, warnings: list[str]) -> None:
    """Built-in sets added in a later version join the owner's config once. The setting
    `sets_joined` remembers every such set the config has held, so a set the owner removed is
    not added back. A config that loaded with warnings is not rewritten (that would erase the
    entries the loader skipped): the set joins in memory only, until the owner saves."""
    from sift.core.config import ADDED_LATER

    with db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = 'sets_joined'").fetchone()
    already = set(json.loads(row[0])) if row else set()
    added = join_added_sets(config, already)
    if warnings:
        return
    if added and path.exists():
        try:
            save_config(config, path)
        except OSError as e:
            _log.warning("Could not save the new backup sets: %s", e)
            return
    present = {s.name for s in config.backup.sets} & set(ADDED_LATER)
    if present - already:
        with db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO setting (key, value) VALUES ('sets_joined', ?)",
                (json.dumps(sorted(already | present)),),
            )
    if added:
        _log.info("Added backup sets: %s", ", ".join(added))


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
    _bus: EventBus | None = None
    _stopped: bool = field(default=False, init=False)

    def stop(self) -> None:
        """Ask the server to exit, wait for its thread (bounded) and free the socket.

        Idempotent, and never raises: a failure is logged and the remaining parts still run, so
        one bad part cannot leave the socket open or the log handler attached.
        """
        if self._stopped:
            return
        self._stopped = True
        if self._bus is not None:
            # End open event streams first, so uvicorn's graceful shutdown has nothing to cancel.
            try:
                self._bus.close()
            except Exception:
                _log.exception("Could not close the event bus")
        self.server.should_exit = True
        parts: list[tuple[str, Callable[[], object]]] = [
            ("wait for the server thread", lambda: self.thread.join(_STOP_TIMEOUT_S)),
            (
                "detach the log forwarder",
                lambda: logging.getLogger("uvicorn.error").removeHandler(self._forwarder),
            ),
            ("close the server socket", self.sock.close),
        ]
        for part, action in parts:
            try:
                action()
            except Exception:
                _log.exception("Could not %s", part)


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
    handle = ServerHandle(f"http://{_HOST}:{port}", server, thread, sock, forwarder, ctx.bus)
    thread.start()
    deadline = time.monotonic() + _START_TIMEOUT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            handle.stop()
            raise RuntimeError("The Sift server did not start.")
        time.sleep(0.01)
    return handle


def ssd_connected(ctx: AppContext) -> None:
    """The backup SSD was plugged in: copy across what one destination has and the other lacks."""
    ssd = ctx.ssd
    replicate_now(ctx, should_stop=lambda: ssd is not None and ssd.stopping)
    purge_quarantine(ctx)


def load_starter_teaching(ctx: AppContext) -> None:
    """A fresh install of a build with the owner's teaching bundled starts with it."""
    from sift.cleanup.labels_io import load_starter

    try:
        result = load_starter(ctx.db)
    except Exception as e:  # never stops the app
        _log.warning("The bundled teaching was not loaded: %s", e)
        return
    if result:
        _log.info("Loaded the bundled teaching: %d labels", result["read"])


def reconnect_wifi(ctx: AppContext) -> None:
    """Reconnect phones paired over Wi-Fi that are on the network now (spec 6)."""
    from sift.device import wifi

    try:
        done = wifi.reconnect(ctx.db, ctx.config.phone.adb_server_port)
    except Exception as e:  # never stops the app
        _log.info("Wi-Fi reconnect skipped: %s", e)
        return
    if done:
        _log.info("Reconnected over Wi-Fi: %d phone(s)", len(done))


def purge_quarantine(ctx: AppContext) -> None:
    """Remove quarantined files past their date on the mounted destinations (spec 10)."""
    from sift.cleanup.quarantine import purge_due

    try:
        with ctx.db.read() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM quarantine WHERE purged_at IS NULL "
                    "AND restored_at IS NULL LIMIT 1"
                ).fetchone()
                is None
            ):
                return  # nothing in quarantine
        purged = purge_due(ctx)
    except Exception as e:  # e.g. Sift closing meanwhile; the next start or plug-in tries again
        _log.warning("Purging the quarantine did not finish: %s", e)
        return
    if purged:
        _log.info("Purged %d quarantined files", purged)


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
        slow = testhooks.flag("SIFT_TEST_SLOW_PHONE")
        faults = FaultPlan(slow_read_bps=SLOW_PHONE_BPS) if slow else None
        control = None
        if testhooks.flag("SIFT_TEST_PHONE_CONTROL"):
            # The browser tests unplug the phone and change what is on it (routes/testphone.py).
            control = ctx.phone_control = FakePhoneControl(faults)
        return fake_tracker(
            ctx, announce_after_s=fake_announce_delay(env), faults=faults, control=control
        )
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


def recover_at_startup(ctx: AppContext) -> None:
    """Undo what a crash left half done (`sift.backup.recovery`), before anything else runs.

    A failure is logged, not raised: Sift still starts, and the next start tries again.
    """
    try:
        recover(ctx.db)
    except Exception:
        _log.exception("Recovery at startup failed")
    try:
        with ctx.db.tx() as conn:  # a delete run a crash interrupted ends as stopped
            conn.execute(
                "UPDATE delete_run SET state = 'stopped', finished_at = strftime('%s', 'now') "
                "WHERE state IN ('preflight', 'running')"
            )
        from sift.cleanup.quarantine import reconcile

        reconcile(ctx)
    except Exception:
        _log.exception("Delete-run recovery at startup failed")


def build_jobs(ctx: AppContext, notifier: Notifier | None = None) -> JobManager:
    """The backup job manager: each job runs `run_backup` on the connected phone.

    When a job ends (`after_run`): the owner is told through `notifier` (a system notification,
    or a toast), and the daily copy of the database is made.
    """
    notify = notifier if notifier is not None else NullNotifier()

    def runner(control: JobControl) -> None:
        awake = ctx.awake
        with awake.hold() if awake is not None else contextlib.nullcontext():
            run_backup(ctx, control, control.sets, control.dry_run)  # leaves its run_summary row

    jobs = JobManager(
        ctx,
        clock=SystemClock(),
        runner=runner,
        describe=describe,
        stored_speed=lambda: stored_read_speed(ctx),
        on_finished=lambda snap: after_run(ctx, snap, notify, closing=lambda: jobs.closing),
    )
    return jobs


def start_test_backup(ctx: AppContext, jobs: JobManager) -> threading.Thread | None:
    """With SIFT_TEST_AUTO_BACKUP=1, back up the enabled sets once the phone is connected.

    For the quit-path tests (there is no backup API yet). It needs SIFT_HOME, like every hook
    that writes, so it never runs against the owner's real data.
    """
    if not testhooks.flag("SIFT_TEST_AUTO_BACKUP"):
        return None
    if not os.environ.get("SIFT_HOME"):
        _log.warning("SIFT_TEST_AUTO_BACKUP needs SIFT_HOME set to a test folder; not starting")
        return None

    def start() -> None:
        deadline = time.monotonic() + _AUTO_BACKUP_WAIT_S
        while time.monotonic() < deadline:
            tracker = ctx.tracker
            if tracker is not None and tracker.session() is not None:
                names = [s.name for s in ctx.config.backup.sets if s.enabled]
                try:
                    jobs.start_backup(names)
                except JobError as e:
                    _log.warning("The test backup did not start: %s", e.message)
                return
            time.sleep(0.05)
        _log.warning("The test backup did not start: no phone")

    thread = threading.Thread(target=start, name="sift-test-auto-backup", daemon=True)
    thread.start()
    return thread


def start_test_jobs(ctx: AppContext) -> SlowJob | None:
    """Start the test-only slow job when SIFT_TEST_SLOW_JOB=1 (see `core.testhooks`).

    It stays for the hand-run window quit check: the window always looks at the real phone, so
    the real backup job cannot run there against the fake one. Like the fake phone it needs
    SIFT_HOME, so it never writes into the owner's real database.
    """
    if not testhooks.flag("SIFT_TEST_SLOW_JOB"):
        return None
    home = os.environ.get("SIFT_HOME")
    if not home:
        _log.warning("SIFT_TEST_SLOW_JOB needs SIFT_HOME set to a test folder; not starting it")
        return None
    job = SlowJob(ctx.db, Path(home).expanduser() / "slow-job")
    job.start()
    return job


def _stop_jobs(jobs: JobManager, slow: SlowJob | None) -> None:
    """Stop every backup at a file boundary, waiting up to _JOBS_STOP_TIMEOUT_S in all."""
    deadline = time.monotonic() + _JOBS_STOP_TIMEOUT_S
    jobs.stop_all(_JOBS_STOP_TIMEOUT_S)
    if slow is not None and not slow.stop(max(0.0, deadline - time.monotonic())):
        _log.warning("The test job did not stop within %s s", _JOBS_STOP_TIMEOUT_S)


@contextmanager
def _signals_request_shutdown(shutdown: Shutdown) -> Iterator[None]:
    """Headless mode: SIGINT and SIGTERM ask for shutdown, and `main` then runs it.

    The handlers stay in place until the block ends, so a second Ctrl-C during cleanup does
    not cut it short.
    """

    def on_signal(signum: int, frame: FrameType | None) -> None:
        shutdown.request(f"signal {signal.Signals(signum).name}")

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def _quiet_pywebview_logger() -> None:
    """pywebview logs the page URL, with its token, at DEBUG when PYWEBVIEW_LOG=debug."""
    logging.getLogger("pywebview").setLevel(logging.INFO)


def _choose_folder(window: Any, start: str | None) -> str | None:
    """The macOS folder dialog over the window; None when the owner cancels."""
    import webview

    kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
    if kind is None:
        kind = webview.FOLDER_DIALOG
    folder = os.path.expanduser(start) if start else ""
    picked = window.create_file_dialog(kind, directory=folder if os.path.isdir(folder) else "")
    return str(picked[0]) if picked else None


def _run_window(url: str, shutdown: Shutdown, ctx: AppContext | None = None) -> None:
    """Show the window until the app quits. Every quit route runs `shutdown` (see WindowQuit).

    Cmd-Q, the close button and SIGINT/SIGTERM all end in `applicationShouldTerminate:`, which
    runs the shutdown and then lets AppKit exit the process, so this may never return. It does
    return if the window goes away by itself; `main` then runs the shutdown.
    """
    import webview  # imported here so headless runs never load Cocoa

    from sift import cocoa_quit

    _quiet_pywebview_logger()
    window = webview.create_window("Sift", url, width=1280, height=800, min_size=(1000, 680))
    if window is None:  # only when an `initialized` handler cancels it; Sift adds none
        return
    if ctx is not None:
        ctx.pick_folder = lambda start: _choose_folder(window, start)
    quit = WindowQuit(
        shutdown,
        ask_app_to_terminate=cocoa_quit.ask_app_to_terminate,
        reply_to_terminate=cocoa_quit.reply_to_terminate,
    )
    window.events.closing += quit.on_window_closing
    cocoa_quit.install(quit)
    webview.start(gui="cocoa")


def self_check() -> int:
    """Import the native parts a packaged build needs (onnxruntime, HEIF, Vision, AVFoundation,
    notifications, tokenizers, the window) and the bundled files; 0 when all load."""
    import importlib

    failed = []
    for name in ("numpy", "onnxruntime", "pillow_heif", "PIL.Image", "tokenizers", "Vision",
                 "AVFoundation", "UserNotifications", "webview", "yaml"):  # fmt: skip
        try:
            importlib.import_module(name)
        except Exception as e:  # report every part, then fail
            failed.append(f"{name}: {e}")
    if find_web_dist() is None:
        failed.append("web UI: not found")
    from importlib.resources import files

    if not files("sift.resources").joinpath("migrations").is_dir():
        failed.append("migrations: not found")
    print("self-check: " + ("ok" if not failed else "FAILED\n  " + "\n  ".join(failed)))
    return 0 if not failed else 1


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
    parser.add_argument(
        "--self-check", action="store_true", help="load every native part, print the result"
    )
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check()

    mode = tracker_mode(fake_phone=args.fake_phone, no_window=args.no_window, env=os.environ)
    if mode == "fake" and not os.environ.get("SIFT_HOME"):
        # The fake phone would be stored as "the first phone ever seen" in the real database,
        # and the owner's real phone would then show as a different phone for good.
        print(FAKE_NEEDS_HOME_MESSAGE)
        return 2

    paths = AppPaths.default()
    paths.ensure()
    lock = SingleInstanceLock(paths.lock)
    try:
        lock.acquire()
    except AlreadyRunningError:
        print("Sift is already running.")
        return 1

    # Every exit from here on, early or not, runs these steps in reverse order of adding.
    shutdown = Shutdown()
    shutdown.add("lock", lock.release, timeout_s=_LOCK_STEP_S)
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
        # Closing twice is harmless: the server takes the socket over and closes it on stop.
        shutdown.add("socket", sock.close, timeout_s=_SOCKET_STEP_S)
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
        shutdown.add("db", ctx.db.close, timeout_s=_DB_STEP_S)
        if mode == "fake":  # SIFT_HOME is set (checked above)
            keep_fake_backups_at_home(ctx.config, os.environ["SIFT_HOME"])
        setup_logging(paths, ctx.bus, level="INFO")
        install_crash_reports(ctx)
        shutdown.add("logging", teardown_logging, timeout_s=_LOGGING_STEP_S)
        for warning in ctx.config_warnings:
            _log.warning("%s", warning)
        recover_at_startup(ctx)  # before any job can start
        load_starter_teaching(ctx)
        threading.Thread(target=purge_quarantine, args=(ctx,), daemon=True).start()
        # Headless runs (tests, dev servers) have no Cocoa app, and never post notifications.
        jobs = build_jobs(ctx, NullNotifier() if args.no_window else MacNotifier())
        ctx.jobs = jobs
        tasks = TaskRunner(ctx, describe)
        ctx.tasks = tasks
        handle = start_server(ctx, sock)
        shutdown.add("server", handle.stop, timeout_s=_SERVER_STEP_S)
        tracker = build_tracker(mode, ctx, os.environ)
        if tracker is not None:
            ctx.tracker = tracker
            shutdown.add("tracker", tracker.stop, timeout_s=_TRACKER_STEP_S)  # before the DB closes
            tracker.start()
        if mode == "adb":  # the real app; tests and fake-phone runs never look at real drives
            ctx.awake = KeepAwake(ctx)
            threading.Thread(
                target=reconnect_wifi, args=(ctx,), name="sift-wifi-reconnect", daemon=True
            ).start()
            threading.Thread(
                target=restore_at_launch, args=(ctx,), name="sift-stay-on", daemon=True
            ).start()
            ssd = SsdWatcher(ctx, on_connect=lambda _state: ssd_connected(ctx))
            ctx.ssd = ssd
            shutdown.add("ssd", ssd.stop, timeout_s=40.0)  # waits for its copy to stop
            ssd.start()
        shutdown.add("tasks", tasks.stop, timeout_s=_TRACKER_STEP_S)
        slow = start_test_jobs(ctx)
        # Added last, so it runs first: jobs stop before the tracker, server and database.
        shutdown.add("jobs", lambda: _stop_jobs(jobs, slow), timeout_s=_JOBS_STOP_TIMEOUT_S + 5)
        start_test_backup(ctx, jobs)
        url = f"{handle.url}/?t={token}"
        if args.no_window:
            with _signals_request_shutdown(shutdown):
                print(f"Sift running at {url}", flush=True)
                shutdown.wait_requested()
                shutdown.run()
        else:
            _run_window(url, shutdown, ctx)
        return 0
    finally:
        shutdown.run()
