"""JSON-lines logging, the log-to-event bridge, and crash reports.

Tracebacks go only to the log file and crash reports, never into events the UI shows.
"""

import json
import logging
import logging.handlers
import sys
import threading
import traceback
from datetime import datetime
from types import TracebackType

from sift.core.events import EventBus
from sift.core.paths import AppPaths

LOG_FILE = "sift.log"
_ROOT = "sift"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5


def _area(name: str) -> str:
    return name.removeprefix(f"{_ROOT}.")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": int(record.created),
            "level": record.levelname,
            "area": _area(record.name),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["traceback"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(entry, ensure_ascii=False)


class _SiftFileHandler(logging.handlers.RotatingFileHandler):
    pass


class _BusHandler(logging.Handler):
    """Republish records as `log` events. The payload carries no traceback."""

    def __init__(self, bus: EventBus) -> None:
        super().__init__(level=logging.INFO)
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._bus.publish(
                "log",
                {
                    "level": record.levelname,
                    "area": _area(record.name),
                    "message": record.getMessage(),
                },
            )
        except Exception:
            self.handleError(record)


def setup_logging(paths: AppPaths, bus: EventBus, level: str = "INFO") -> None:
    """Attach the JSON file handler and the bus handler to the `sift` logger."""
    levels = logging.getLevelNamesMapping()
    if level.upper() not in levels:
        raise ValueError(f"unknown log level: {level!r}")
    teardown_logging()  # calling twice must not duplicate lines
    paths.logs.mkdir(parents=True, exist_ok=True)
    file_handler = _SiftFileHandler(
        paths.logs / LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(_JsonFormatter())
    logger = logging.getLogger(_ROOT)
    logger.setLevel(levels[level.upper()])
    logger.addHandler(file_handler)
    logger.addHandler(_BusHandler(bus))


def teardown_logging() -> None:
    """Remove and close every handler Sift added (used by tests)."""
    logger = logging.getLogger(_ROOT)
    for handler in list(logger.handlers):
        if isinstance(handler, _SiftFileHandler | _BusHandler):
            logger.removeHandler(handler)
            handler.close()


def _write_crash_report(paths: AppPaths, header: str, text: str) -> str | None:
    """Write `logs/crash-<ts>.txt`; returns its name, or None if even that failed."""
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        report = paths.logs / f"crash-{now:%Y%m%d-%H%M%S-%f}.txt"
        report.write_text(
            f"Sift crash report\n{now.isoformat(timespec='seconds')}\n{header}\n\n{text}",
            encoding="utf-8",
        )
        return report.name
    except OSError:
        return None


def _record_crash(
    paths: AppPaths,
    header: str,
    exc_type: type[BaseException],
    exc: BaseException | None,
    tb: TracebackType | None,
) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    name = _write_crash_report(paths, header, text)
    saved = f"crash report saved as {name}" if name else "crash report could not be saved"
    exc_info = (exc_type, exc, tb) if exc is not None else None
    logging.getLogger(f"{_ROOT}.crash").critical(
        "Unexpected error (%s): %s", saved, exc_type.__name__, exc_info=exc_info
    )


def install_crash_handler(paths: AppPaths) -> None:
    """Route uncaught exceptions (main and worker threads) to a crash report plus the log."""

    def on_main(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            _record_crash(paths, "thread: MainThread", exc_type, exc, tb)
        sys.__excepthook__(exc_type, exc, tb)

    def on_thread(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "unknown"
            _record_crash(
                paths, f"thread: {name}", args.exc_type, args.exc_value, args.exc_traceback
            )
        threading.__excepthook__(args)

    sys.excepthook = on_main
    threading.excepthook = on_thread
