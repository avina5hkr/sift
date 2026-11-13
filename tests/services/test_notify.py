"""Notifications: a system notification in the background, a toast when that cannot be shown."""

from typing import Any

from sift.services import notify
from sift.services.notify import MacNotifier, NullNotifier, announce, is_sift_bundle


class Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, data))


class Notifier:
    def __init__(self, *, background: bool, allowed: bool = True) -> None:
        self.background = background
        self.allowed = allowed
        self.posted: list[tuple[str, str]] = []

    def app_in_background(self) -> bool:
        return self.background

    def post(self, title: str, body: str, cancelled: Any = None) -> bool:
        self.posted.append((title, body))
        return self.allowed


def test_in_the_background_a_system_notification_is_posted_and_no_toast() -> None:
    bus, notifier = Bus(), Notifier(background=True)
    assert announce(bus, notifier, "Backup finished", "3 new files") == "notification"
    assert notifier.posted == [("Backup finished", "3 new files")]
    assert bus.events == []


def test_when_permission_is_denied_it_falls_back_to_a_toast() -> None:
    bus, notifier = Bus(), Notifier(background=True, allowed=False)
    assert announce(bus, notifier, "Backup finished", "3 new files", level="warning") == "toast"
    assert bus.events == [
        ("toast", {"level": "warning", "title": "Backup finished", "message": "3 new files"})
    ]


def test_in_the_foreground_there_is_a_toast_and_no_system_notification() -> None:
    bus, notifier = Bus(), Notifier(background=False)
    assert announce(bus, notifier, "t", "b") == "toast"
    assert notifier.posted == [] and [e[0] for e in bus.events] == ["toast"]


def test_a_notifier_that_raises_still_gives_a_toast() -> None:
    class Broken(Notifier):
        def post(self, title: str, body: str, cancelled: Any = None) -> bool:
            raise RuntimeError("no")

    bus = Bus()
    assert announce(bus, Broken(background=True), "t", "b") == "toast"
    assert len(bus.events) == 1


def test_the_null_notifier_always_means_a_toast() -> None:
    bus = Bus()
    assert announce(bus, NullNotifier(), "t", "b") == "toast"


def test_the_mac_notifier_without_the_bindings_says_it_could_not_post() -> None:
    """Whatever is installed here, a failure is a False (never an exception) so a toast follows.

    This test must never post: it only runs when the UserNotifications bindings are missing.
    """
    try:
        import UserNotifications  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
    except ImportError:
        assert MacNotifier().post("t", "b") is False


def test_the_mac_notifier_outside_an_app_bundle_never_touches_the_center() -> None:
    """From source (no .app) the notification center would abort the process; post must refuse."""
    import subprocess
    import sys

    code = "from sift.services.notify import MacNotifier; print(MacNotifier().post('t', 'b'))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_only_a_packaged_sift_bundle_counts() -> None:
    assert is_sift_bundle("app.sift.Sift", "/Applications/Sift.app")
    assert not is_sift_bundle(None, "/Applications/Sift.app")
    assert not is_sift_bundle("", "/Applications/Sift.app")
    assert not is_sift_bundle("app.sift.Sift", "/usr/local/bin")
    # A framework Python.app has a bundle and an identifier, but it is not Sift.
    assert not is_sift_bundle("org.python.python", "/Library/Frameworks/Python.app")
    assert not is_sift_bundle("ORG.PYTHON.Foo", "/x/Python.app")


def test_background_is_asked_on_the_main_thread_and_unknown_means_in_front() -> None:
    asked: list[float] = []

    def answers(value: bool | None) -> MacNotifier:
        def call(_work: Any, timeout: float) -> bool | None:
            asked.append(timeout)
            return value

        return MacNotifier(call)

    assert answers(True).app_in_background() is True
    assert answers(False).app_in_background() is False
    assert answers(None).app_in_background() is False  # no answer in time: in front, so a toast

    def raises(_work: Any, _timeout: float) -> bool | None:
        raise RuntimeError("no AppKit")

    assert MacNotifier(raises).app_in_background() is False
    assert asked and all(t == notify._MAIN_THREAD_TIMEOUT_S for t in asked)


def test_the_main_thread_helper_gives_up_after_its_timeout() -> None:
    import threading

    result: list[bool | None] = []
    # Off the main thread, with no run loop to answer: it must come back (None), not hang.
    t = threading.Thread(target=lambda: result.append(notify._on_main_thread(lambda: True, 0.05)))
    t.start()
    t.join(5)
    assert not t.is_alive() and result == [None]


def test_the_permission_wait_ends_when_cancelled(monkeypatch: Any) -> None:
    """With the bundle check passed and a center that never answers, `cancelled` ends the wait."""
    import sys
    import time
    import types

    class Center:
        def requestAuthorizationWithOptions_completionHandler_(self, *_a: Any) -> None:
            pass  # never calls back: the owner has not answered the prompt

    class UNCenter:
        @staticmethod
        def currentNotificationCenter() -> Center:
            return Center()

    fake = types.ModuleType("UserNotifications")
    for name in (
        "UNAuthorizationOptionAlert",
        "UNMutableNotificationContent",
        "UNNotificationRequest",
    ):
        setattr(fake, name, object())
    fake.UNUserNotificationCenter = UNCenter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "UserNotifications", fake)
    monkeypatch.setattr(notify, "_inside_app_bundle", lambda: True)
    asked = {"n": 0}

    def cancelled() -> bool:
        asked["n"] += 1
        return asked["n"] >= 2

    start = time.monotonic()
    assert MacNotifier().post("t", "b", cancelled) is False
    assert time.monotonic() - start < 5 and asked["n"] >= 2
