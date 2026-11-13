"""Long tasks that do not need the phone (Verify, Adopt, Rebuild): one at a time, on a thread.

Each task gets a `TaskControl` to report progress (a `task` event at most every 500 ms) and to
check whether it should stop. Shutdown asks the running task to stop and waits a bounded time.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from sift.core.context import AppContext

logger = logging.getLogger("sift.services")

THROTTLE_S = 0.5


class TaskBusy(Exception):
    """Another long task is running."""


@dataclass
class TaskStatus:
    kind: str
    state: str = "running"  # running, done, stopped, failed
    done: int = 0
    total: int = 0
    message: str | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        return asdict(self)


class TaskControl:
    def __init__(self, runner: TaskRunner, status: TaskStatus) -> None:
        self._runner = runner
        self.status = status
        self._last = 0.0

    @property
    def should_stop(self) -> bool:
        return self._runner.stop_requested

    def progress(self, done: int, total: int) -> None:
        self.status.done, self.status.total = done, total
        now = time.monotonic()
        if now - self._last >= THROTTLE_S:
            self._last = now
            self._runner.publish(self.status)


class TaskRunner:
    def __init__(self, ctx: AppContext, describe: Callable[[BaseException], str]) -> None:
        self._ctx = ctx
        self._describe = describe
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: TaskStatus | None = None

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def publish(self, status: TaskStatus) -> None:
        self._ctx.bus.publish("task", status.as_event())

    def current(self) -> TaskStatus | None:
        return self._status

    def start(self, kind: str, work: Callable[[TaskControl], dict[str, Any]]) -> TaskStatus:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                busy = self._status.kind if self._status else "task"
                raise TaskBusy(f"Sift is busy with another task ({busy}).")
            self._stop.clear()
            status = TaskStatus(kind)
            self._status = status
            control = TaskControl(self, status)

            def run() -> None:
                try:
                    status.result = work(control)
                    status.state = "stopped" if self._stop.is_set() else "done"
                except Exception as e:
                    logger.exception("The %s task failed", kind)
                    status.state, status.message = "failed", self._describe(e)
                finally:
                    self.publish(status)
                    self._ctx.db.release_thread()

            self._thread = threading.Thread(target=run, name=f"sift-task-{kind}", daemon=True)
            self._thread.start()
            self.publish(status)
            return status

    def stop(self, timeout_s: float = 30.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)

    def wait(self, timeout_s: float) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            return not thread.is_alive()
        return True
