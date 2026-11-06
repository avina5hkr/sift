"""Notifications: a system notification in the background, a toast when that cannot be shown."""

from typing import Any

from sift.services.notify import MacNotifier, NullNotifier, announce


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

    def post(self, title: str, body: str) -> bool:
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
        def post(self, title: str, body: str) -> bool:
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
