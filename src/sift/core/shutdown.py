"""One shutdown routine for every way Sift can quit: a signal, Cmd-Q, or closing the window.

Steps are added as each resource comes up and run in reverse order, each exactly once. A step
that fails or overruns its time budget is logged and the next one still runs, so the database
is closed however the earlier steps went. The application's instance guard remains held until
process exit, since a timed-out step may still be writing after logical shutdown completes.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)

# wait_requested() polls the flag this often (seconds). See request() for why it polls.
_POLL_S = 0.05


@dataclass(frozen=True)
class _Step:
    name: str
    fn: Callable[[], None]
    timeout_s: float


class Shutdown:
    def __init__(self) -> None:
        self._steps: list[_Step] = []
        self._requested = False
        self._reason: str | None = None
        self._run_lock = threading.Lock()
        self._started = False

    def add(self, name: str, step: Callable[[], None], *, timeout_s: float) -> None:
        """Register a cleanup step. Steps run in reverse order of adding."""
        if self._started:
            raise RuntimeError(f"Shutdown already started; cannot add {name!r}")
        self._steps.append(_Step(name, step, timeout_s))

    def request(self, reason: str) -> None:
        """Ask for shutdown. Idempotent: the first reason is kept.

        Safe from any thread and from a signal handler: it only assigns two attributes and
        takes no lock. A Python signal handler runs on the main thread between bytecodes, so
        taking a lock here (as `threading.Event.set` does) could deadlock against the very
        wait it interrupted. That is why `wait_requested` polls instead of blocking on an Event.
        """
        if self._requested:
            return
        self._reason = reason
        self._requested = True

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def reason(self) -> str | None:
        """Why shutdown was first requested, or None if it has not been."""
        return self._reason

    def wait_requested(self, timeout: float | None = None) -> bool:
        """Block until shutdown is requested or `timeout` seconds pass; True if requested."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._requested:
            if deadline is None:
                time.sleep(_POLL_S)
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(_POLL_S, left))
        return True

    def run(self) -> None:
        """Run every step once, last added first. Later callers wait until the run is done."""
        self.request("shutdown")
        with self._run_lock:
            if self._started:
                return
            self._started = True
            _log.info("Shutting down (%s)", self._reason)
            for step in reversed(self._steps):
                self._run_step(step)

    @staticmethod
    def _run_step(step: _Step) -> None:
        """Run one step on its own thread, so a step that hangs cannot hold up the rest."""

        def target() -> None:
            try:
                step.fn()
            except Exception:
                _log.exception("Shutdown step %r failed", step.name)

        thread = threading.Thread(target=target, name=f"sift-shutdown-{step.name}", daemon=True)
        thread.start()
        thread.join(step.timeout_s)
        if thread.is_alive():
            _log.warning(
                "Shutdown step %r did not finish within %s s; going on", step.name, step.timeout_s
            )
