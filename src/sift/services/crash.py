"""Crash reports (spec 14): an uncaught exception in any thread writes `logs/crash-DATE.txt`
and tells the window, which shows a dialog with Open logs. A report the window never showed
(the app died first) is shown at the next launch."""

import contextlib
import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from sift.core.context import AppContext

log = logging.getLogger("sift.app")
SEEN_KEY = "crash_report_seen"


def write_report(ctx: AppContext, kind: type[BaseException], error: BaseException,
                 tb: TracebackType | None, where: str) -> Path | None:  # fmt: skip
    folder = ctx.paths.logs
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"crash-{datetime.now():%Y-%m-%d-%H%M%S}.txt"
        n = 1
        while path.exists():
            path = folder / f"crash-{datetime.now():%Y-%m-%d-%H%M%S}-{n}.txt"
            n += 1
        text = "".join(traceback.format_exception(kind, error, tb))
        path.write_text(f"Sift {ctx.version}, {where}\n\n{text}", encoding="utf-8")
    except OSError:
        return None
    log.critical("Sift hit an unexpected error in %s; report: %s", where, path.name)
    with contextlib.suppress(Exception):
        ctx.bus.publish("crash", {"report": path.name})
    return path


def install(ctx: AppContext) -> None:
    """Route uncaught exceptions of the main thread and every other thread to a report."""
    previous = sys.excepthook

    def on_main(kind: type[BaseException], error: BaseException, tb: TracebackType | None) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            write_report(ctx, kind, error, tb, "the main thread")
        previous(kind, error, tb)

    def on_thread(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "a thread"
        write_report(ctx, args.exc_type, args.exc_value, args.exc_traceback, name)

    sys.excepthook = on_main
    threading.excepthook = on_thread


def latest_unseen(ctx: AppContext) -> dict[str, str] | None:
    reports = sorted(ctx.paths.logs.glob("crash-*.txt")) if ctx.paths.logs.is_dir() else []
    if not reports:
        return None
    newest = reports[-1]
    with ctx.db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (SEEN_KEY,)).fetchone()
    if row is not None and str(row[0]) >= newest.name:
        return None
    first = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    last = next((ln for ln in reversed(first) if ln.strip()), "")
    return {"report": newest.name, "summary": last[:300]}


def mark_seen(ctx: AppContext, report: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)", (SEEN_KEY, report)
        )
