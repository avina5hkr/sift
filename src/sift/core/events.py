"""Thread-safe in-process event bus.

Backup worker threads publish progress here; the web layer streams it to the UI over SSE.
Publishers must never block: a stalled subscriber costs a publisher one deque append.
"""

import asyncio
import contextlib
import json
import threading
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    """One published event. The payload is stored as JSON text, so it cannot change after publish.

    `text` is what SSE sends. `data` parses it afresh, so a reader can never alter what another
    reader (or the wire) sees.
    """

    id: int
    name: str
    text: str

    @classmethod
    def of(cls, id: int, name: str, data: dict[str, Any]) -> Event:
        """Serialise `data` now. TypeError or ValueError if it is not valid JSON."""
        return cls(id, name, _dumps(data))

    @property
    def data(self) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.text)
        return parsed


def _dumps(data: dict[str, Any]) -> str:
    # allow_nan=False: NaN and Infinity are not JSON, and the browser's JSON.parse rejects them.
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


class BusClosed(Exception):
    """The bus was closed (the app is shutting down); no more events will come."""


class Subscription:
    """One consumer's bounded buffer. Drop-oldest on overflow; `dropped` counts the losses.

    Producers (any thread) call `_push`; the one consumer (on the event loop) calls `get`.
    `_wake_pending` is set by `_push` (which then schedules one `event.set`) and cleared only by
    `get`, in the same critical section that observes an empty buffer. So a push after that
    moment always schedules a fresh wake-up, and a push before it is seen by the consumer as a
    non-empty buffer. At most one wake-up is scheduled per drain; a stale one only makes `get`
    loop once more.
    """

    def __init__(self, maxsize: int) -> None:
        self.dropped = 0
        self._buffer: deque[Event] = deque(maxlen=maxsize)
        self._lock = threading.Lock()
        self._wake_pending = False
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def _wake(self) -> None:
        if self._loop is not None:
            # RuntimeError: the consumer's loop is closed, nobody is left to wake.
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._event.set)

    def _close(self) -> None:
        with self._lock:
            self._closed = True
        self._wake()

    def _push(self, event: Event) -> None:
        with self._lock:
            if self._buffer.maxlen is not None and len(self._buffer) == self._buffer.maxlen:
                self.dropped += 1
            self._buffer.append(event)
            wake = not self._wake_pending
            self._wake_pending = True
        if wake:
            self._wake()

    async def get(self) -> Event:
        """The next event; raises `BusClosed` once the bus is closed and the buffer is drained."""
        while True:
            with self._lock:
                if self._buffer:
                    return self._buffer.popleft()
                if self._closed:
                    raise BusClosed
                self._wake_pending = False
                self._event.clear()
            await self._event.wait()


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: list[Subscription] = []
        self._next_id = 1
        self._closed = False

    def close(self) -> None:
        """End every subscription (now and later): their `get` raises `BusClosed` once drained.

        Called at shutdown so open event streams finish on their own instead of being cancelled.
        Idempotent; callable from any thread.
        """
        with self._lock:
            self._closed = True
            subs = list(self._subscriptions)
        for sub in subs:
            sub._close()

    def publish(self, name: str, data: dict[str, Any]) -> None:
        """Deliver to every subscriber. Callable from any thread; never blocks.

        The payload is serialised here, before the lock: a payload that is not JSON raises at the
        publisher (where the bug is) and uses no id, and the lock is held only for the appends.
        """
        text = _dumps(data)
        # Id assignment and delivery share one lock so every subscriber sees ids in order.
        with self._lock:
            event = Event(self._next_id, name, text)
            self._next_id += 1
            for sub in self._subscriptions:
                sub._push(event)

    @asynccontextmanager
    async def subscribe(self, maxsize: int = 1000) -> AsyncIterator[Subscription]:
        sub = Subscription(maxsize)
        sub._loop = asyncio.get_running_loop()
        with self._lock:
            self._subscriptions.append(sub)
            if self._closed:
                sub._closed = True
        try:
            yield sub
        finally:
            with self._lock:
                self._subscriptions.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)
