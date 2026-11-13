import asyncio

import pytest

from sift.core.events import Event
from sift.server.sse import format_sse, sse_stream


def test_format_sse():
    assert (
        format_sse(Event.of(7, "toast", {"text": "hi"}))
        == 'id: 7\nevent: toast\ndata: {"text":"hi"}\n\n'
    )


@pytest.mark.anyio
async def test_hello_then_relay(bus):
    async def connected():
        return False

    agen = sse_stream(bus, connected, version="0.1.0", heartbeat_s=5)
    first = await anext(agen)
    assert "event: hello" in first and '"version":"0.1.0"' in first
    bus.publish("toast", {"text": "hi"})
    assert "event: toast" in await anext(agen)
    await agen.aclose()
    assert bus.subscriber_count == 0


@pytest.mark.anyio
async def test_heartbeat_when_idle(bus):
    async def connected():
        return False

    agen = sse_stream(bus, connected, version="0.1.0", heartbeat_s=0.05)
    await anext(agen)
    assert await asyncio.wait_for(anext(agen), 1) == ": ping\n\n"
    await agen.aclose()


@pytest.mark.anyio
async def test_stream_unsubscribes_on_disconnect(bus):
    gone = False

    async def is_disconnected():
        return gone

    agen = sse_stream(bus, is_disconnected, version="0.1.0", heartbeat_s=0.05)
    await anext(agen)
    gone = True
    with pytest.raises(StopAsyncIteration):
        while True:
            await asyncio.wait_for(anext(agen), 1)
    assert bus.subscriber_count == 0


def test_events_requires_token(client):
    assert client.get("/api/events").status_code == 401


def test_format_sse_sends_the_stored_text():
    assert format_sse(Event(id=3, name="x", text='{"a":1}')) == 'id: 3\nevent: x\ndata: {"a":1}\n\n'


@pytest.mark.anyio
async def test_stream_ends_when_the_bus_closes(bus):
    async def connected():
        return False

    agen = sse_stream(bus, connected, version="0.1.0", heartbeat_s=30)
    await anext(agen)
    bus.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(agen), 1)
    assert bus.subscriber_count == 0
