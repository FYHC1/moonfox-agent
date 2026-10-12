"""Synthetic regressions for optional/empty metadata emitted by OpenAI gateways.

These fixtures reproduce observed 9router stream shapes without credentials, live
response IDs, model output or external network requests.
"""

from copy import deepcopy

import pytest

from moonfox_ai.api import OpenAIProvider, TextContent, TextDelta, ToolCall, Usage

from .test_openai_responses import (
    MODEL as RESPONSES_MODEL,
)
from .test_openai_responses import (
    context as responses_context,
)
from .test_openai_responses import (
    provider as responses_provider,
)
from .test_openai_responses import text_events
from .test_remote_providers import MODEL, context, frame, oa, provider


@pytest.mark.parametrize("unfinished", [None, ""])
@pytest.mark.parametrize(
    "legacy_call",
    [None, {}, {"name": "", "arguments": ""}, {"name": None, "arguments": None}],
)
async def test_chat_empty_gateway_fields_are_not_content_or_tool_calls(unfinished, legacy_call):
    empty = {"content": "", "reasoning_content": "", "refusal": "", "function_call": None}
    lines = [
        *oa({**empty, "role": "assistant"}, unfinished),
        *oa({**empty, "content": "gateway "}, unfinished),
        *oa({**empty, "content": "reply"}, unfinished),
        *oa(
            {**empty, "function_call": legacy_call},
            "stop",
            usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        ),
        *frame("[DONE]"),
    ]
    adapter, transport, raw = provider(OpenAIProvider, lines, supports_tools=False)
    stream = adapter.stream(MODEL, context())
    deltas = [event.delta async for event in stream if isinstance(event, TextDelta)]
    result = await stream.result()
    assert result.stop_reason == "stop" and result.raw_stop_reason == "stop"
    assert deltas == ["gateway ", "reply"]
    assert [block.text for block in result.content if isinstance(block, TextContent)] == [
        "gateway reply"
    ]
    assert not any(isinstance(block, ToolCall) for block in result.content)
    assert result.usage == Usage(input=12, output=3, total_tokens=15)
    assert raw.closed and raw.close_count == 1 and len(transport.requests) == 1


@pytest.mark.parametrize("sentinel", [False, True])
async def test_chat_empty_finish_reason_never_completes_a_truncated_stream(sentinel):
    lines = [*oa({"content": "partial", "refusal": ""}, "")]
    if sentinel:
        lines += frame("[DONE]")
    adapter, _, raw = provider(OpenAIProvider, lines)
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "error"
    assert result.diagnostics == [{"code": "invalid_response"}]
    assert result.content == [TextContent("partial")]
    assert raw.closed


@pytest.mark.parametrize(
    "legacy_call",
    [
        {"name": "echo", "arguments": ""},
        {"name": "", "arguments": "{}"},
        {"name": False, "arguments": ""},
        {"name": 0, "arguments": ""},
        {"name": "", "arguments": []},
        {"unexpected": None},
        "",
        [],
        False,
    ],
)
async def test_chat_empty_placeholder_compatibility_does_not_hide_legacy_calls(legacy_call):
    adapter, _, raw = provider(
        OpenAIProvider,
        [*oa({"function_call": legacy_call}, "stop"), *frame("[DONE]")],
    )
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "error"
    assert result.diagnostics == [{"code": "invalid_response"}]
    assert not any(isinstance(block, ToolCall) for block in result.content)
    assert raw.closed


@pytest.mark.parametrize("finish", [False, 0, [], {}, "unknown"])
async def test_chat_only_null_or_empty_string_are_unfinished_markers(finish):
    adapter, _, raw = provider(
        OpenAIProvider, [*oa({"content": "text"}, finish), *oa(finish="stop"), *frame("[DONE]")]
    )
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and raw.closed


@pytest.mark.parametrize("metadata", ["missing", "null", "late", "initial-only"])
async def test_responses_resolved_model_metadata_is_optional_and_can_arrive_later(metadata):
    events = text_events()
    for index, event in enumerate(events):
        if "response" not in event:
            continue
        response = event["response"]
        if metadata == "null":
            response["model"] = None
        elif not (
            (metadata == "late" and index == len(events) - 1)
            or (metadata == "initial-only" and index == 0)
        ):
            del response["model"]
    adapter, transport, raw = responses_provider(events)
    stream = adapter.stream(RESPONSES_MODEL, responses_context())
    deltas = [event.delta async for event in stream if isinstance(event, TextDelta)]
    result = await stream.result()
    assert result.stop_reason == "stop" and result.content == [TextContent("你好")]
    assert deltas == ["你", "好"] and result.response_id == "resp-1"
    # Requested model is not fabricated response metadata.
    assert result.model == RESPONSES_MODEL.id
    assert result.response_model == (
        "gpt-4.1-resolved" if metadata in ("late", "initial-only") else None
    )
    assert result.usage.effective_total == 110
    assert raw.closed and raw.close_count == 1 and len(transport.requests) == 1


@pytest.mark.parametrize("value", ["", False, 42, [], {}])
@pytest.mark.parametrize("index", [0, -1])
async def test_responses_present_model_metadata_must_remain_a_nonempty_string(value, index):
    events = text_events()
    events[index]["response"]["model"] = value
    adapter, _, raw = responses_provider(events)
    result = await adapter.stream(RESPONSES_MODEL, responses_context()).result()
    assert result.stop_reason == "error" and raw.closed


@pytest.mark.parametrize("field,value", [("id", "other"), ("id", None), ("model", "other")])
async def test_responses_compatibility_keeps_identity_consistency_checks(field, value):
    events = deepcopy(text_events())
    events[-1]["response"][field] = value
    adapter, _, raw = responses_provider(events)
    result = await adapter.stream(RESPONSES_MODEL, responses_context()).result()
    assert result.stop_reason == "error" and raw.closed
