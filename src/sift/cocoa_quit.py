"""PyObjC glue for window mode: hands every quit route to a `WindowQuit`. Kept thin on purpose.

Imported only in window mode (it loads Cocoa). The logic lives in `sift.window_quit`, where it
is tested; this module cannot run without a real window and is checked by hand.

- pywebview creates one shared app delegate (`BrowserView._shared_app_delegate`) the first time a
  window is built, unless one is already set. `install` sets ours first: a subclass of
  pywebview's own delegate, so everything else it does is kept.
- pywebview calls `AppHelper.installMachInterrupt()` just before `NSApp.run()`, which takes
  SIGINT over through `PyObjCTools.MachSignals`. `applicationDidFinishLaunching:` is sent from
  inside `run()`, after that, so the SIGINT and SIGTERM handlers registered there survive.
- `applicationShouldTerminate:` answers NSTerminateLater; the reply comes once the shutdown
  steps are done. pywebview's version would instead ask each window's `closing` event, which
  Sift uses to turn the close button into a quit.
"""

import signal

import AppKit  # type: ignore[import-untyped]
from PyObjCTools import AppHelper, MachSignals  # type: ignore[import-untyped]
from webview.platforms.cocoa import BrowserView

from sift.window_quit import WindowQuit

_quit: WindowQuit | None = None


class SiftAppDelegate(BrowserView.AppDelegate):
    def applicationDidFinishLaunching_(self, notification: object) -> None:
        if _quit is not None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                MachSignals.signal(signum, _quit.on_signal)

    def applicationShouldTerminate_(self, app: object) -> int:
        if _quit is None:  # not installed: nothing to clean up, quit now
            return int(AppKit.NSTerminateNow)
        return _quit.on_should_terminate()


def install(quit: WindowQuit) -> None:
    """Make pywebview use Sift's app delegate. Call before `webview.start()`."""
    global _quit
    _quit = quit
    BrowserView._shared_app_delegate = SiftAppDelegate.alloc().init()


def ask_app_to_terminate() -> None:
    """Start AppKit's normal quit (as Cmd-Q does), from any thread."""
    AppHelper.callAfter(AppKit.NSApplication.sharedApplication().terminate_, None)


def reply_to_terminate(ok: bool) -> None:
    """Answer a pending NSTerminateLater, from any thread. With True, AppKit exits the process."""
    app = AppKit.NSApplication.sharedApplication()
    AppHelper.callAfter(app.replyToApplicationShouldTerminate_, ok)
