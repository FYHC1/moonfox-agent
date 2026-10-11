import asyncio
import json
from copy import deepcopy

import pytest

from moonfox_ai.api import (
    Context,
    FauxProvider,
    FauxResponse,
    FauxScriptExhausted,
    ImageContent,
    Model,
    StreamOptions,
    TextContent,
    TextDelta,
    ThinkingContent,
    ToolCall,
    ToolCallDelta,
    TranscriptContext,
    Usage,
    UserMessage,
    normalize_context,
)

MODEL = Model(id="test", provider="faux", api="faux")


class Signal:
    """An independent caller's signal: AI tests never need the agent package."""

    def __init__(self):
        self.event = asyncio.Event()

    @property
    def aborted(self):
        return self.event.is_set()

    async def wait(self):
        await self.event.wait()


async def test_text_events_and_live_partial_with_detached_snapshots():
    response = FauxResponse.text("abcdef", chunk_size=2, usage=Usage(input=10, output=3))
    stream = response.to_stream(MODEL)
    events, snapshots = [], []
    async for event in stream:
        events.append(event)
        if isinstance(event, TextDelta):
            snapshots.append(deepcopy(event.partial))
    final = await stream.result()
    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]
    assert [snapshot.content[0].text for snapshot in snapshots] == ["ab", "abcd", "abcdef"]
    assert all(event.partial is final for event in events[:-1])
    assert final.content == [TextContent("abcdef")]
    assert final.usage.effective_total == 13
    assert response.content == [TextContent("abcdef")]


async def test_structured_content_signatures_indices_and_tool_json():
    blocks = [
        ImageContent("AA==", "image/png"),
        ThinkingContent("plan", thinking_signature="signed", redacted=True),
        TextContent("hello", text_signature="text-signed"),
        ToolCall("call-7", "echo", {"value": "中文", "nested": {"a": [1]}}, "thought-signed"),
    ]
    stream = FauxResponse(blocks, stop_reason="tool_use", chunk_size=2).to_stream(MODEL)
    events = [event async for event in stream]
    final = await stream.result()
    assert final.content == blocks
    assert final.content is not blocks
    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    assert json.loads("".join(event.delta for event in deltas)) == blocks[-1].arguments
    assert all(event.content_index == 3 for event in deltas)
    assert events[-1].reason == "tool_use"


@pytest.mark.parametrize("reason", ["stop", "length", "tool_use", "deferred"])
async def test_success_reasons(reason):
    stream = FauxResponse.text("x", stop_reason=reason).to_stream(MODEL)
    events = [event async for event in stream]
    assert events[0].type == "start"
    assert events[-1].type == "done" and events[-1].reason == reason
    assert (await stream.result()).stop_reason == reason


@pytest.mark.parametrize("reason", ["error", "aborted"])
async def test_error_without_start(reason):
    stream = FauxResponse.error("failed", stop_reason=reason).to_stream(MODEL)
    assert [event.type async for event in stream] == ["error"]
    result = await stream.result()
    assert result.error_message == "failed" and result.stop_reason == reason


async def test_error_after_partial_output():
    stream = FauxResponse.text("partial", stop_reason="error", error_message="broken").to_stream(
        MODEL
    )
    events = [event async for event in stream]
    assert events[0].type == "start" and events[-1].type == "error"
    assert (await stream.result()).content[0].text == "partial"


async def test_preaborted_request_and_abort_during_delay():
    signal = Signal()
    signal.event.set()
    stream = FauxResponse.text("unused").to_stream(MODEL, StreamOptions(signal=signal))
    assert [event.type async for event in stream] == ["error"]
    assert (await stream.result()).stop_reason == "aborted"
    signal = Signal()
    stream = FauxResponse.text("delayed", delay_ms=60_000).to_stream(
        MODEL, StreamOptions(signal=signal)
    )
    iterator = aiter(stream)
    assert (await anext(iterator)).type == "start"
    signal.event.set()
    async with asyncio.timeout(1):
        assert (await stream.result()).stop_reason == "aborted"
        assert [event.type async for event in iterator][-1] == "error"
    await stream.aclose()


async def test_abort_after_first_delta_keeps_partial_output():
    signal = Signal()
    stream = FauxResponse.text("abcdef", chunk_size=2).to_stream(
        MODEL, StreamOptions(signal=signal)
    )
    async for event in stream:
        if isinstance(event, TextDelta):
            signal.event.set()
    result = await stream.result()
    assert result.stop_reason == "aborted"
    assert result.content[0].text == "ab"


async def test_result_only_and_close_do_not_leak_tasks():
    baseline = asyncio.all_tasks()
    stream = FauxResponse.text("x" * 1024, chunk_size=1).to_stream(MODEL)
    async with asyncio.timeout(1):
        assert (await stream.result()).content[0].text == "x" * 1024
    await stream.aclose()
    early = FauxResponse.text("unused", delay_ms=60_000).to_stream(MODEL)
    await early.aclose()
    assert (await early.result()).stop_reason == "aborted"
    assert asyncio.all_tasks() == baseline


async def test_script_and_request_records_are_detached():
    response = FauxResponse.text("original")
    provider = FauxProvider([response, response])
    response.content[0].text = "mutated"
    context = normalize_context(Context([UserMessage([TextContent("question")], 0)]))
    signal = Signal()
    options = StreamOptions(signal=signal, headers={"x-test": "first"})
    first = provider.stream(MODEL, context, options)
    context.messages[0].content[0].text = "mutated"
    options.headers["x-test"] = "changed"
    assert (await first.result()).content[0].text == "original"
    assert provider.requests[0][1].messages[0].content[0].text == "question"
    assert provider.requests[0][2].headers == {"x-test": "first"}
    assert provider.requests[0][2].signal is signal
    second = provider.stream(MODEL, context)
    assert (await second.result()).content[0].text == "original"
    assert provider.remaining == 0
    with pytest.raises(FauxScriptExhausted):
        provider.stream(MODEL, context)


def test_context_brand_and_faux_input_validation():
    with pytest.raises(TypeError, match="normalize_context"):
        TranscriptContext([])
    provider = FauxProvider([FauxResponse.text("x")])
    with pytest.raises(TypeError, match="normalized"):
        provider.stream(MODEL, Context([]))
    assert provider.remaining == 1
    for kwargs in ({"stop_reason": "pending"}, {"chunk_size": 0}, {"delay_ms": -1}):
        with pytest.raises(ValueError):
            FauxResponse.text("x", **kwargs)
