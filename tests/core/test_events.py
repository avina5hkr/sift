import asyncio
import threading
import time

import pytest

from sift.core.events import EventBus, Subscription


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


@pytest.mark.anyio
async def test_cancelled_consumer_removes_its_subscriber(bus: EventBus) -> None:
    ready = asyncio.Event()

    async def consume() -> None:
        async with bus.subscribe() as sub:
            ready.set()
            await sub.get()

    task = asyncio.ensure_future(consume())
    await ready.wait()
    assert bus.subscriber_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.subscriber_count == 0


def test_publish_after_the_consumer_loop_closed_does_not_raise(bus: EventBus) -> None:
    # A subscriber whose loop is gone but which is still registered, as after a killed server.
    sub = Subscription(10)
    sub._loop = asyncio.new_event_loop()
    sub._loop.close()
    bus._subscriptions.append(sub)
    bus.publish("toast", {"text": "nobody listens"})  # must not raise
    bus.publish("toast", {"text": "still nobody"})
    assert len(sub._buffer) == 2


@pytest.mark.anyio
async def test_many_threads_publish_to_a_live_consumer(bus: EventBus) -> None:
    threads, per_thread = 8, 50
    async with bus.subscribe(maxsize=threads * per_thread) as sub:

        def work(t: int) -> None:
            for n in range(per_thread):
                bus.publish("progress", {"t": t, "n": n})

        workers = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
        for w in workers:
            w.start()
        got = [await asyncio.wait_for(sub.get(), 2) for _ in range(threads * per_thread)]
        for w in workers:
            w.join()
    assert [e.id for e in got] == sorted(e.id for e in got)  # ids arrive in order
    assert len({e.id for e in got}) == threads * per_thread
    for t in range(threads):  # each thread's own events stay in order
        assert [e.data["n"] for e in got if e.data["t"] == t] == list(range(per_thread))


def test_non_json_payload_raises_at_the_publisher(bus: EventBus) -> None:
    with pytest.raises(TypeError):
        bus.publish("toast", {"when": object()})
    with pytest.raises(ValueError):
        bus.publish("toast", {"x": float("nan")})  # not valid JSON for the browser


@pytest.mark.anyio
async def test_a_rejected_publish_leaves_no_trace(bus: EventBus) -> None:
    async with bus.subscribe() as sub:
        with pytest.raises(TypeError):
            bus.publish("toast", {"when": object()})
        bus.publish("toast", {"text": "ok"})
        ev = await asyncio.wait_for(sub.get(), 1)
        assert ev.id == 1  # the failed publish did not use an id


@pytest.mark.anyio
async def test_payload_is_frozen_at_publish(bus: EventBus) -> None:
    payload: dict[str, object] = {"files": [1, 2]}
    async with bus.subscribe() as sub:
        bus.publish("progress", payload)
        payload["files"] = [9]  # the publisher keeps mutating its own dict
        ev = await asyncio.wait_for(sub.get(), 1)
    assert ev.text == '{"files":[1,2]}'
    assert ev.data == {"files": [1, 2]}
