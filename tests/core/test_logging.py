import asyncio
import json
import logging
import sys
import threading

import pytest

from sift.core.events import EventBus
from sift.core.logging import install_crash_handler, setup_logging
from sift.core.paths import AppPaths


def test_json_lines_in_log_file(paths: AppPaths, bus: EventBus) -> None:
    setup_logging(paths, bus)
    logging.getLogger("sift.backup").info("hello %s", "world")
    rec = json.loads((paths.logs / "sift.log").read_text().splitlines()[-1])
    assert rec["level"] == "INFO"
    assert rec["area"] == "backup"
    assert rec["message"] == "hello world"
    assert isinstance(rec["ts"], int)


@pytest.mark.anyio
async def test_log_records_become_events(paths: AppPaths, bus: EventBus) -> None:
    setup_logging(paths, bus)
    async with bus.subscribe() as sub:
        logging.getLogger("sift.device").warning("phone offline")
        ev = await asyncio.wait_for(sub.get(), 1)
    assert ev.name == "log"
    assert ev.data == {"level": "WARNING", "area": "device", "message": "phone offline"}


@pytest.mark.anyio
async def test_log_event_never_carries_a_traceback(paths: AppPaths, bus: EventBus) -> None:
    setup_logging(paths, bus)
    async with bus.subscribe() as sub:
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("sift.backup").exception("copy failed")
        ev = await asyncio.wait_for(sub.get(), 1)
    assert ev.data == {"level": "ERROR", "area": "backup", "message": "copy failed"}
    rec = json.loads((paths.logs / "sift.log").read_text().splitlines()[-1])
    assert "ValueError: boom" in rec["traceback"]


def test_setup_twice_does_not_duplicate_lines(paths: AppPaths, bus: EventBus) -> None:
    setup_logging(paths, bus)
    setup_logging(paths, bus)
    logging.getLogger("sift.backup").info("once")
    assert (paths.logs / "sift.log").read_text().count("once") == 1


def test_crash_handler_writes_report(paths: AppPaths) -> None:
    install_crash_handler(paths)
    try:
        raise ValueError("boom")
    except ValueError as e:
        sys.excepthook(type(e), e, e.__traceback__)
    [report] = paths.logs.glob("crash-*.txt")
    assert "ValueError: boom" in report.read_text()


def test_thread_crash_writes_report(paths: AppPaths) -> None:
    install_crash_handler(paths)

    def boom() -> None:
        raise RuntimeError("worker died")

    t = threading.Thread(target=boom, name="worker-1")
    t.start()
    t.join()
    [report] = paths.logs.glob("crash-*.txt")
    text = report.read_text()
    assert "RuntimeError: worker died" in text
    assert "worker-1" in text


def test_restore_fixture_part_1_installs(paths: AppPaths, bus: EventBus) -> None:
    install_crash_handler(paths)
    setup_logging(paths, bus)


def test_restore_fixture_part_2_sees_clean_state() -> None:  # runs after part 1 (file order)
    # pytest wraps the hooks itself, so check only that Sift's handlers are gone.
    assert getattr(sys.excepthook, "__module__", "") != "sift.core.logging"
    assert getattr(threading.excepthook, "__module__", "") != "sift.core.logging"
    assert logging.getLogger("sift").handlers == []
