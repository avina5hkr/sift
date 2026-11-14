"""Crash reports (spec 14): an uncaught exception in any thread writes `logs/crash-DATE.txt`
and tells the window, which shows a dialog with Open logs. A report the window never showed
(the app died first) is shown at the next launch."""

from sift.core.context import AppContext
from sift.core.logging import on_crash

SEEN_KEY = "crash_report_seen"


def install(ctx: AppContext) -> None:
    """Crash reports are written by `core.logging.install_crash_handler` (one handler for
    every thread); this tells the window about each one, so it shows the crash dialog."""
    on_crash(lambda name: ctx.bus.publish("crash", {"report": name}))


def latest_unseen(ctx: AppContext) -> dict[str, str] | None:
    """The newest crash report (by its time on disk) unless the owner already closed it."""
    folder = ctx.paths.logs
    reports = list(folder.glob("crash-*.txt")) if folder.is_dir() else []
    if not reports:
        return None
    newest = max(reports, key=lambda p: (p.stat().st_mtime, p.name))
    with ctx.db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (SEEN_KEY,)).fetchone()
    if row is not None and str(row[0]) == newest.name:
        return None
    first = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    last = next((ln for ln in reversed(first) if ln.strip()), "")
    return {"report": newest.name, "summary": last[:300]}


def mark_seen(ctx: AppContext, report: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)", (SEEN_KEY, report)
        )
