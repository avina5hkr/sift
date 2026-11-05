"""Server-sent events: wire format and the per-connection stream."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from sift.core.events import Event, EventBus


def format_sse(event: Event) -> str:
    data = json.dumps(event.data, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.name}\ndata: {data}\n\n"


async def sse_stream(
    bus: EventBus,
    is_disconnected: Callable[[], Awaitable[bool]],
    version: str,
    heartbeat_s: float = 15.0,
) -> AsyncIterator[str]:
    """Yield `hello`, then every bus event; a `: ping` comment whenever idle for `heartbeat_s`.

    Subscribes before the first yield, so nothing published after the client sees `hello` is
    lost. The subscription lives inside this generator, so `aclose()`, cancellation or a
    detected disconnect always unsubscribes.
    """
    async with bus.subscribe() as sub:
        yield format_sse(Event(id=0, name="hello", data={"version": version}))
        while True:
            try:
                event = await asyncio.wait_for(sub.get(), heartbeat_s)
            except TimeoutError:
                if await is_disconnected():
                    return
                yield ": ping\n\n"
            else:
                yield format_sse(event)
