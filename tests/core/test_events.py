import asyncio
import threading
import time

import pytest

from sift.core.events import EventBus


@pytest.mark.anyio
async def test_receives_events_from_other_threads(bus: EventBus) -> None:
    async with bus.subscribe() as sub:
        threading.Thread(target=bus.publish, args=("toast", {"text": "hi"})).start()
        ev = await asyncio.wait_for(sub.get(), 1)
        assert (ev.name, ev.data) == ("toast", {"text": "hi"})
    assert bus.subscriber_count == 0


@pytest.mark.anyio
async def test_slow_subscriber_drops_oldest(bus: EventBus) -> None:
    async with bus.subscribe(maxsize=3) as sub:
        for n in range(5):
            bus.publish("progress", {"n": n})
        got = [(await sub.get()).data["n"] for _ in range(3)]
        assert got == [2, 3, 4]
        assert sub.dropped == 2


@pytest.mark.anyio
async def test_publish_never_blocks_on_stalled_subscriber(bus: EventBus) -> None:
    async with bus.subscribe(maxsize=10):
        start = time.perf_counter()
        for n in range(10_000):
            bus.publish("progress", {"n": n})
        assert time.perf_counter() - start < 0.5


@pytest.mark.anyio
async def test_one_wakeup_per_drain(bus: EventBus, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    calls: list[int] = []
    real = loop.call_soon_threadsafe

    def spy(cb, *a, **k):  # type: ignore[no-untyped-def]
        # count only the bus's wake-ups, not unrelated loop callbacks
        if isinstance(getattr(cb, "__self__", None), asyncio.Event):
            calls.append(1)
        return real(cb, *a, **k)

    monkeypatch.setattr(loop, "call_soon_threadsafe", spy)
    async with bus.subscribe(maxsize=2000) as sub:
        for n in range(1000):
            bus.publish("progress", {"n": n})
        assert len(calls) == 1
        for _ in range(1000):
            await sub.get()
        get_next = asyncio.ensure_future(sub.get())  # buffer empty -> re-arms the wake-up
        await asyncio.sleep(0)
        bus.publish("progress", {"n": 1000})
        assert (await asyncio.wait_for(get_next, 1)).data["n"] == 1000
        assert len(calls) == 2


@pytest.mark.anyio
async def test_ids_increase(bus: EventBus) -> None:
    async with bus.subscribe() as sub:
        bus.publish("a", {})
        bus.publish("b", {})
        assert (await sub.get()).id < (await sub.get()).id
