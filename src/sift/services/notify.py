"""Telling the owner a backup finished: a macOS notification, or an in-app toast.

`Notifier` is the small interface the rest of Sift uses; `MacNotifier` is the real one, over
UserNotifications through pyobjc. pyobjc's UserNotifications bindings may not be installed, and
macOS only allows notifications for an app the owner has permitted, so `MacNotifier` says so by
returning False and the caller shows an in-app toast through the event bus instead. Tests give
`announce` a fake `Notifier`, so no test ever posts a real notification.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

_ASK_TIMEOUT_S = 10.0
_ASK_POLL_S = 0.2
_MAIN_THREAD_TIMEOUT_S = 1.0
# Bundle identifiers that are not Sift: a framework Python.app (python.org, Homebrew) is a .app
# with an identifier too, and must not post "from Python".
_NOT_SIFT_BUNDLE_PREFIXES = ("org.python",)


class Publisher(Protocol):
    """What `announce` needs of the event bus (`sift.core.events.EventBus` is one)."""

    def publish(self, name: str, data: dict[str, Any]) -> None: ...


class Notifier(Protocol):
    def app_in_background(self) -> bool:
        """True when Sift's window is not the app the owner is looking at."""
        ...

    def post(self, title: str, body: str, cancelled: Callable[[], bool] | None = None) -> bool:
        """Show a system notification. False when it could not be shown (no permission, or no
        notification support here); the caller then falls back to a toast. `cancelled` is asked
        while waiting for the owner's permission: once true the wait ends (Sift is quitting)."""
        ...


class NullNotifier:
    """No system notifications: everything goes to the in-app toast."""

    def app_in_background(self) -> bool:
        return False

    def post(self, title: str, body: str, cancelled: Callable[[], bool] | None = None) -> bool:
        return False


def is_sift_bundle(identifier: str | None, path: str) -> bool:
    """True for a packaged Sift: a .app with an identifier that is not another program's."""
    if not identifier or not path.endswith(".app"):
        return False
    return not identifier.lower().startswith(_NOT_SIFT_BUNDLE_PREFIXES)


def _inside_app_bundle() -> bool:
    try:
        from Foundation import NSBundle  # type: ignore[import-not-found,import-untyped,unused-ignore]

        bundle = NSBundle.mainBundle()
        identifier = bundle.bundleIdentifier()
        return is_sift_bundle(
            None if identifier is None else str(identifier), str(bundle.bundlePath())
        )
    except Exception:
        return False


def _on_main_thread(work: Callable[[], bool], timeout_s: float) -> bool | None:
    """Run `work` on the main thread (AppKit's) and wait up to `timeout_s`; None if it did not
    answer in time (the main thread is busy or not running a run loop)."""
    if threading.current_thread() is threading.main_thread():
        return work()
    from PyObjCTools import AppHelper  # type: ignore[import-not-found,import-untyped,unused-ignore]

    done = threading.Event()
    answer: list[bool] = []

    def call() -> None:
        try:
            answer.append(work())
        finally:
            done.set()

    AppHelper.callAfter(call)
    if not done.wait(timeout_s) or not answer:
        return None
    return answer[0]


class MacNotifier:
    """UserNotifications via pyobjc. Every failure (not installed, not allowed) is a False."""

    def __init__(
        self, call_on_main: Callable[[Callable[[], bool], float], bool | None] = _on_main_thread
    ) -> None:
        self._call_on_main = call_on_main

    def app_in_background(self) -> bool:
        """Asked of AppKit on its main thread. Unknown (no answer in time, any error) counts as
        in front, so the toast shows."""

        def inactive() -> bool:
            from AppKit import NSApplication  # type: ignore[import-not-found,import-untyped,unused-ignore]

            return not bool(NSApplication.sharedApplication().isActive())

        try:
            return bool(self._call_on_main(inactive, _MAIN_THREAD_TIMEOUT_S))
        except Exception:
            return False

    def post(self, title: str, body: str, cancelled: Callable[[], bool] | None = None) -> bool:
        if not _inside_app_bundle():
            # UNUserNotificationCenter aborts the whole process (an uncaught NSException, not a
            # Python error) when Sift runs from source without a .app bundle.
            return False
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
            deadline = time.monotonic() + _ASK_TIMEOUT_S
            while not granted.wait(_ASK_POLL_S):
                if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                    return False
            if not allowed[0]:
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
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Tell the owner: a system notification if Sift is in the background, else (or when that
    cannot be shown) a toast. Returns "notification" or "toast". Never raises."""
    try:
        background = (in_background or notifier.app_in_background)()
        if background and notifier.post(title, body, cancelled):
            return "notification"
    except Exception:
        log.exception("Could not notify")
    try:
        bus.publish("toast", {"level": level, "title": title, "message": body})
    except Exception:
        log.exception("Could not publish a toast")
    return "toast"
