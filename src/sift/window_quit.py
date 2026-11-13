"""Window mode: every quit route (a signal, the close button, Cmd-Q) ends in one place.

AppKit asks `applicationShouldTerminate:` before it quits. The answer is NSTerminateLater, the
shutdown steps run on a worker thread, and only then is AppKit told to go ahead (after which it
exits the process). A signal or the close button just asks the app to quit, so they take the same
route. The PyObjC glue in `sift.cocoa_quit` only forwards to this class.
"""

import contextlib
import signal
import sys
import threading
from collections.abc import Callable

from sift.core.shutdown import Shutdown

NS_TERMINATE_LATER = 2  # NSApplicationTerminateReply.NSTerminateLater


class WindowQuit:
    def __init__(
        self,
        shutdown: Shutdown,
        *,
        ask_app_to_terminate: Callable[[], None],
        reply_to_terminate: Callable[[bool], None],
    ) -> None:
        self._shutdown = shutdown
        self._ask_app_to_terminate = ask_app_to_terminate
        self._reply_to_terminate = reply_to_terminate
        self._lock = threading.Lock()
        self._asked = False
        self._worker: threading.Thread | None = None

    def on_signal(self, signum: int) -> None:
        """SIGINT or SIGTERM (delivered through the run loop): quit like Cmd-Q."""
        self._shutdown.request(f"signal {signal.Signals(signum).name}")
        self._ask_once()

    def on_window_closing(self) -> bool:
        """The close button: quit like Cmd-Q. False keeps the window open until the app exits."""
        self._shutdown.request("window closed")
        self._ask_once()
        return False

    def on_should_terminate(self) -> int:
        """`applicationShouldTerminate:`: start the shutdown on a worker and answer later."""
        self._shutdown.request("quit")
        with self._lock:
            first = self._worker is None
            if first:
                self._worker = threading.Thread(target=self._finish, name="sift-quit", daemon=True)
        if first and self._worker is not None:
            self._worker.start()
        return NS_TERMINATE_LATER

    def _ask_once(self) -> None:
        with self._lock:
            if self._asked:
                return
            self._asked = True
        self._ask_app_to_terminate()

    def _finish(self) -> None:
        try:
            self._shutdown.run()
            for stream in (sys.stdout, sys.stderr):  # None in a windowed bundle
                if stream is not None:
                    # AppKit ends the process with exit(), past Python's own final flush.
                    with contextlib.suppress(OSError, ValueError):
                        stream.flush()
        finally:
            self._reply_to_terminate(True)
