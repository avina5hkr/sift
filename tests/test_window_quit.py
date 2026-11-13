"""The window-mode quit logic that the Cocoa hooks (sift.cocoa_quit) forward to."""

import signal
import threading
import time

from sift.core.shutdown import Shutdown
from sift.window_quit import NS_TERMINATE_LATER, WindowQuit


class _App:
    """Stands in for NSApplication: records terminate requests and the deferred reply."""

    def __init__(self) -> None:
        self.terminate_asks = 0
        self.replies: list[bool] = []
        self.replied = threading.Event()

    def ask_to_terminate(self) -> None:
        self.terminate_asks += 1

    def reply(self, ok: bool) -> None:
        self.replies.append(ok)
        self.replied.set()


def _make(steps: list[str] | None = None) -> tuple[Shutdown, _App, WindowQuit]:
    shutdown = Shutdown()
    if steps is not None:
        shutdown.add("lock", lambda: steps.append("lock"), timeout_s=5)
        shutdown.add("jobs", lambda: steps.append("jobs"), timeout_s=5)
    app = _App()
    quit = WindowQuit(
        shutdown, ask_app_to_terminate=app.ask_to_terminate, reply_to_terminate=app.reply
    )
    return shutdown, app, quit


def test_a_signal_requests_shutdown_and_asks_the_app_to_quit() -> None:
    shutdown, app, quit = _make()
    quit.on_signal(signal.SIGTERM)
    assert shutdown.requested
    assert shutdown.reason == "signal SIGTERM"
    assert app.terminate_asks == 1


def test_closing_the_window_takes_the_quit_route_instead() -> None:
    shutdown, app, quit = _make()
    assert quit.on_window_closing() is False  # the window stays until the app quits
    assert shutdown.requested
    assert shutdown.reason == "window closed"
    assert app.terminate_asks == 1


def test_should_terminate_answers_later_and_replies_after_the_steps_ran() -> None:
    steps: list[str] = []
    shutdown, app, quit = _make(steps)
    assert quit.on_should_terminate() == NS_TERMINATE_LATER
    assert app.replied.wait(5)
    assert steps == ["jobs", "lock"]
    assert app.replies == [True]
    assert shutdown.reason == "quit"


def test_the_reply_waits_for_a_slow_step() -> None:
    release = threading.Event()
    shutdown = Shutdown()
    shutdown.add("jobs", release.wait, timeout_s=10)
    app = _App()
    quit = WindowQuit(
        shutdown, ask_app_to_terminate=app.ask_to_terminate, reply_to_terminate=app.reply
    )
    quit.on_should_terminate()
    assert not app.replied.wait(0.1)  # never exit before run() finishes
    release.set()
    assert app.replied.wait(5)


def test_the_reply_is_sent_even_when_a_step_fails() -> None:
    shutdown = Shutdown()

    def boom() -> None:
        raise RuntimeError("no")

    shutdown.add("broken", boom, timeout_s=5)
    app = _App()
    quit = WindowQuit(
        shutdown, ask_app_to_terminate=app.ask_to_terminate, reply_to_terminate=app.reply
    )
    quit.on_should_terminate()
    assert app.replied.wait(5)
    assert app.replies == [True]


def test_every_route_at_once_runs_the_steps_once_and_replies_once() -> None:
    steps: list[str] = []
    _, app, quit = _make(steps)
    go = threading.Barrier(4)

    def route(fn: object) -> None:
        go.wait()
        fn()  # type: ignore[operator]

    threads = [
        threading.Thread(target=route, args=(lambda: quit.on_signal(signal.SIGINT),)),
        threading.Thread(target=route, args=(lambda: quit.on_signal(signal.SIGTERM),)),
        threading.Thread(target=route, args=(quit.on_window_closing,)),
        threading.Thread(target=route, args=(quit.on_should_terminate,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    quit.on_should_terminate()  # AppKit may ask again while the first answer is pending
    assert app.replied.wait(5)
    time.sleep(0.1)
    assert steps == ["jobs", "lock"]
    assert app.replies == [True]
    assert app.terminate_asks == 1
