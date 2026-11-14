"""M10: crash reports and the crash dialog."""

import threading

import pytest

from sift.services import crash
from tests.backup.test_run import Rig
from tests.cleanup.test_browse import client


def test_a_thread_crash_writes_a_report_and_the_dialog_shows_it_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(rig.ctx.bus, "publish", lambda kind, data: events.append((kind, data)))
    old_main, old_thread = __import__("sys").excepthook, threading.excepthook
    try:
        from sift.core.logging import install_crash_handler

        install_crash_handler(rig.ctx.paths)
        crash.install(rig.ctx)

        def boom() -> None:
            raise RuntimeError("something broke")

        t = threading.Thread(target=boom, name="sift-test-boom")
        t.start()
        t.join()
    finally:
        from sift.core.logging import on_crash

        on_crash(None)
        __import__("sys").excepthook, threading.excepthook = old_main, old_thread
    reports = list(rig.ctx.paths.logs.glob("crash-*.txt"))
    assert len(reports) == 1 and "RuntimeError: something broke" in reports[0].read_text()
    # an older report seen earlier does not hide this one (ordered by time, compared by name)
    assert events and events[-1][0] == "crash"
    c, h = client(rig)
    shown = c.get("/api/crash", headers=h).json()["crash"]
    assert shown["report"] == reports[0].name and "something broke" in shown["summary"]
    assert c.post("/api/crash/seen", headers=h, json={"report": shown["report"]}).status_code == 204
    assert c.get("/api/crash", headers=h).json() == {"crash": None}


def test_open_logs_opens_the_folder(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    import sift.server.routes.crash as route

    opened: list[str] = []
    monkeypatch.setattr(route, "_open", opened.append)
    c, h = client(rig)
    assert c.post("/api/logs/open", headers=h).status_code == 204
    assert opened == [str(rig.ctx.paths.logs)]


def test_onnxruntime_telemetry_is_off() -> None:
    import os

    import sift  # noqa: F401

    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
