import asyncio
from dataclasses import replace

import pytest

from moonfox_ai.api import (
    AssistantMessage,
    AssistantMessageEventStream,
    Done,
    Error,
    EventStream,
    IncompleteStreamError,
    Start,
    StreamClosedError,
    StreamOverflowError,
    StreamProtocolError,
    TextDelta,
    Usage,
    now_ms,
)


def message(reason="stop", timestamp=None):
    return AssistantMessage([], "faux", "faux", "test", Usage(), reason, timestamp or now_ms())


async def test_terminal_event_ends_iteration_without_end_call():
    stream = EventStream(lambda event: event == "done", lambda event: 42)
    stream.push("start")
    stream.push("done")
    stream.push("ignored")
    assert [event async for event in stream] == ["start", "done"]
    assert await stream.result() == 42
    assert await stream.result() == 42
    assert stream.done


async def test_explicit_none_is_a_valid_generic_result():
    stream = EventStream(lambda event: False, lambda event: None)
    stream.end(None)
    assert await stream.result() is None
    assert [event async for event in stream] == []


async def test_unfinished_end_wakes_both_interfaces():
    stream = EventStream(lambda event: False, lambda event: None)
    iterator = aiter(stream)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    stream.end()
    async with asyncio.timeout(1):
        with pytest.raises(IncompleteStreamError):
            await pending
        with pytest.raises(IncompleteStreamError):
            await stream.result()


async def test_result_waiter_cancellation_does_not_cancel_stream():
    stream = EventStream(lambda event: True, lambda event: event)
    cancelled = asyncio.create_task(stream.result())
    other = asyncio.create_task(stream.result())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    stream.push(7)
    assert await other == 7
    assert await stream.result() == 7


async def test_competing_iterators_are_rejected_not_load_balanced():
    stream = EventStream(lambda event: True, lambda event: event)
    iterator = aiter(stream)
    with pytest.raises(StreamProtocolError, match="one event iterator"):
        aiter(stream)
    stream.push(1)
    assert await anext(iterator) == 1
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


async def test_overflow_is_explicit_but_does_not_block_result_only_use():
    stream = EventStream(lambda event: event == 10, lambda event: event, max_buffer=2)
    for number in range(11):
        stream.push(number)
    assert await stream.result() == 10
    with pytest.raises(StreamOverflowError):
        await anext(aiter(stream))


async def test_producer_bug_is_not_an_operational_error():
    stream = EventStream(lambda event: False, lambda event: None)

    async def broken():
        raise TypeError("bug")

    stream.start(broken())
    with pytest.raises(TypeError, match="bug"):
        await stream.result()
    with pytest.raises(TypeError, match="bug"):
        await anext(aiter(stream))
    await stream.aclose()


async def test_producer_return_without_result_never_hangs():
    stream = EventStream(lambda event: False, lambda event: None)

    async def unfinished():
        await asyncio.sleep(0)

    stream.start(unfinished())
    async with asyncio.timeout(1):
        with pytest.raises(IncompleteStreamError):
            await stream.result()
    await stream.aclose()


async def test_close_joins_producer_even_before_first_tick():
    stream = EventStream(lambda event: False, lambda event: None)
    settled = asyncio.Event()

    async def producer():
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    stream.start(producer())
    await stream.aclose()
    assert settled.is_set()
    with pytest.raises(StreamClosedError):
        await stream.result()
    await stream.aclose()


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
async def test_invalid_buffer_size(size):
    with pytest.raises(ValueError):
        EventStream(lambda event: True, lambda event: event, max_buffer=size)


async def test_provider_event_protocol_and_duration():
    stream = AssistantMessageEventStream()
    final = message()
    with pytest.raises(StreamProtocolError, match="preceding Start"):
        stream.push(Done("stop", final))
    with pytest.raises(StreamProtocolError, match="preceding Start"):
        stream.push(TextDelta(0, "x", final))
    stream.push(Start(final))
    with pytest.raises(StreamProtocolError, match="Duplicate"):
        stream.push(Start(final))
    with pytest.raises(StreamProtocolError, match="reason"):
        stream.push(Error("error", final))
    stream.push(Done("stop", final))
    assert await stream.result() is final
    assert final.duration_ms is not None and final.duration_ms >= 0
    assert [event.type async for event in stream] == ["start", "done"]


async def test_prestart_failure_and_forwarded_duration():
    stream = AssistantMessageEventStream()
    final = message("error", timestamp=1)
    stream.push(Error("error", final))
    assert (await stream.result()).duration_ms is None
    assert [event.type async for event in stream] == ["error"]
    forwarded = AssistantMessageEventStream()
    timed = replace(final, duration_ms=123)
    forwarded.push(Error("error", timed))
    assert (await forwarded.result()).duration_ms == 123
