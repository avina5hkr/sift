"""Telling the owner a backup finished: a macOS notification, or an in-app toast.

`Notifier` is the small interface the rest of Sift uses; `MacNotifier` is the real one, over
UserNotifications through pyobjc. pyobjc's UserNotifications bindings may not be installed, and
macOS only allows notifications for an app the owner has permitted, so `MacNotifier` says so by
returning False and the caller shows an in-app toast through the event bus instead. Tests give
`announce` a fake `Notifier`, so no test ever posts a real notification.
"""

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

_ASK_TIMEOUT_S = 10.0


class Publisher(Protocol):
    """What `announce` needs of the event bus (`sift.core.events.EventBus` is one)."""

    def publish(self, name: str, data: dict[str, Any]) -> None: ...


class Notifier(Protocol):
    def app_in_background(self) -> bool:
        """True when Sift's window is not the app the owner is looking at."""
        ...

    def post(self, title: str, body: str) -> bool:
        """Show a system notification. False when it could not be shown (no permission, or no
        notification support here); the caller then falls back to a toast."""
        ...


class NullNotifier:
    """No system notifications: everything goes to the in-app toast."""

    def app_in_background(self) -> bool:
        return False

    def post(self, title: str, body: str) -> bool:
        return False


class MacNotifier:
    """UserNotifications via pyobjc. Every failure (not installed, not allowed) is a False."""

    def app_in_background(self) -> bool:
        try:
            from AppKit import NSApplication  # type: ignore[import-not-found,import-untyped,unused-ignore]

            return not bool(NSApplication.sharedApplication().isActive())
        except Exception:
            return False  # cannot tell: treat Sift as in front, so the toast shows

    def post(self, title: str, body: str) -> bool:
        try:
            from UserNotifications import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
                UNAuthorizationOptionAlert,
                UNMutableNotificationContent,
                UNNotificationRequest,
                UNUserNotificationCenter,
            )
        except ImportError:
            return False
        try:
            center = UNUserNotificationCenter.currentNotificationCenter()
            granted = threading.Event()
            allowed: list[bool] = []

            def on_answer(ok: bool, _error: object) -> None:
                allowed.append(bool(ok))
                granted.set()

            center.requestAuthorizationWithOptions_completionHandler_(
                UNAuthorizationOptionAlert, on_answer
            )
            if not granted.wait(_ASK_TIMEOUT_S) or not allowed[0]:
                return False
            content = UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
                "sift-backup-finished", content, None
            )
            center.addNotificationRequest_withCompletionHandler_(request, None)
            return True
        except Exception:
            log.exception("Could not post a notification")
            return False


def announce(
    bus: Publisher,
    notifier: Notifier,
    title: str,
    body: str,
    *,
    level: str = "info",
    in_background: Callable[[], bool] | None = None,
) -> str:
    """Tell the owner: a system notification if Sift is in the background, else (or when that
    cannot be shown) a toast. Returns "notification" or "toast". Never raises."""
    try:
        background = (in_background or notifier.app_in_background)()
        if background and notifier.post(title, body):
            return "notification"
    except Exception:
        log.exception("Could not notify")
    try:
        bus.publish("toast", {"level": level, "title": title, "message": body})
    except Exception:
        log.exception("Could not publish a toast")
    return "toast"
