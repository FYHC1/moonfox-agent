"""Offline Responses wire fixtures, independent of credentials or paid model calls."""

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from moonfox_ai.api import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    OpenAIResponsesProvider,
    StreamOptions,
    SystemMessage,
    TextContent,
    TextDelta,
    ThinkingContent,
    ToolCall,
    ToolCallDelta,
    ToolDeclaration,
    ToolResultMessage,
    Usage,
    UserMessage,
    normalize_context,
)

from .test_remote_providers import Gate, Response, Signal, Transport, frame

MODEL = Model("gpt-4.1", "test", "openai-responses", max_tokens=128)
SECRET = "synthetic-responses-secret"
USAGE = {
    "input_tokens": 100,
    "output_tokens": 10,
    "total_tokens": 110,
    "input_tokens_details": {"cached_tokens": 20},
    "output_tokens_details": {"reasoning_tokens": 4},
}


def event(kind, **data):
    return {"type": kind, **data}


def response(output=None, status="in_progress", **extra):
    return {
        "id": "resp-1",
        "model": "gpt-4.1-resolved",
        "status": status,
        "output": output or [],
        **extra,
    }


def created():
    return event("response.created", response=response())


def completed(output, **extra):
    return event("response.completed", response=response(output, "completed", usage=USAGE, **extra))


def text_item(text="你好", **extra):
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **extra,
    }


def text_events():
    ref = {"item_id": "msg-1", "output_index": 0, "content_index": 0}
    return [
        created(),
        event("response.in_progress", response=response()),
        event(
            "response.output_item.added",
            output_index=0,
            item=text_item(content=[], status="in_progress"),
        ),
        event(
            "response.content_part.added",
            **ref,
            part={"type": "output_text", "text": "", "annotations": []},
        ),
        event("response.output_text.delta", **ref, delta="你"),
        event("response.output_text.delta", **ref, delta="好"),
        event(
            "response.output_text.annotation.added",
            **ref,
            annotation={"type": "url_citation", "url": "https://example.invalid"},
        ),
        event("response.output_text.done", **ref, text="你好"),
        event("response.content_part.done", **ref, part=text_item()["content"][0]),
        event("response.output_item.done", output_index=0, item=text_item()),
        completed([text_item()]),
    ]


def call_item(index=0, name="echo", arguments='{"text":"ok"}', status="completed"):
    return {
        "type": "function_call",
        "id": f"fc-{index}",
        "call_id": f"call-{index}",
        "name": name,
        "arguments": arguments,
        "status": status,
    }


def call_events(index=0, name="echo"):
    ref = {"item_id": f"fc-{index}", "output_index": index}
    call = call_item(index, name)
    return [
        event(
            "response.output_item.added",
            output_index=index,
            item=call_item(index, name, "", "in_progress"),
        ),
        event("response.function_call_arguments.delta", **ref, delta='{"text":'),
        event("response.function_call_arguments.delta", **ref, delta='"ok"}'),
        event("response.function_call_arguments.done", **ref, arguments=call["arguments"]),
        event("response.output_item.done", output_index=index, item=call),
    ]


def context(messages=None, **kwargs):
    return normalize_context(Context(messages or [UserMessage("question", 1)], **kwargs))


def provider(events=None, **kwargs):
    lines = []
    for data in text_events() if events is None else events:
        if isinstance(data, Gate):
            lines.append(data)
        else:
            lines.extend(frame(data, data["type"]))
    result = Response(lines)
    transport = Transport(result)
    adapter = OpenAIResponsesProvider(
        transport, base_url="https://example.invalid/v1", api_key=SECRET, **kwargs
    )
    return adapter, transport, result


async def test_responses_live_text_terminal_usage_and_request_options():
    adapter, transport, raw = provider(supports_reasoning=True)
    captured = []
    stream = adapter.stream(
        MODEL,
        context(system_prompt="Be useful"),
        StreamOptions(
            api_key="synthetic-override",
            temperature=0,
            top_p=0.7,
            max_tokens=40,
            reasoning="medium",
            headers={"x-test": "yes"},
            on_payload=lambda payload, model: captured.append(deepcopy(payload)),
        ),
    )
    deltas = []
    async for entry in stream:
        if isinstance(entry, TextDelta):
            deltas.append((entry.delta, deepcopy(entry.partial.content)))
    result = await stream.result()
    assert result.content == [TextContent("你好")]
    assert deltas == [("你", [TextContent("你")]), ("好", [TextContent("你好")])]
    assert result.stop_reason == "stop" and result.raw_stop_reason == "completed"
    assert result.response_id == "resp-1" and result.response_model == "gpt-4.1-resolved"
    assert result.usage.input == 80 and result.usage.cache_read == 20
    assert result.usage.output == 10 and result.usage.reasoning == 4
    assert result.usage.effective_total == 110
    request = transport.requests[0]
    assert request.url.endswith("/v1/responses") and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer synthetic-override"
    assert request.headers["x-test"] == "yes"
    assert (
        request.payload
        == captured[0]
        == {
            "model": "gpt-4.1",
            "input": [{"role": "user", "content": "question"}],
            "instructions": "Be useful",
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": 40,
            "temperature": 0,
            "top_p": 0.7,
            "reasoning": {"effort": "medium"},
        }
    )
    assert SECRET not in repr(request) + repr(result) + repr(captured)
    assert raw.closed and raw.close_count == 1


@pytest.mark.parametrize(
    "options",
    [
        StreamOptions(stop=()),
        StreamOptions(stop=("END",)),
        StreamOptions(seed=0),
        StreamOptions(frequency_penalty=0),
        StreamOptions(presence_penalty=0),
        StreamOptions(reasoning="high"),
        StreamOptions(reasoning="invalid"),
    ],
)
def test_responses_rejects_unsupported_options_before_network(options):
    adapter, transport, _ = provider()
    with pytest.raises(ValueError):
        adapter.stream(MODEL, context(), options)
    assert not transport.requests


async def test_responses_default_tokens_off_reasoning_no_auth_and_snapshot():
    _, transport, raw = provider()
    adapter = OpenAIResponsesProvider(transport, base_url="http://localhost/v1")
    ctx = context()
    stream = adapter.stream(replace(MODEL, max_tokens=None), ctx, StreamOptions(reasoning="off"))
    ctx.messages[0].content = "edited after request"
    assert (await stream.result()).stop_reason == "stop"
    request = transport.requests[0]
    assert request.payload["max_output_tokens"] == 4096
    assert request.payload["input"][0]["content"] == "question"
    assert "reasoning" not in request.payload and "Authorization" not in request.headers
    assert raw.closed


async def test_responses_tool_deltas_aliases_replay_and_audit_fields_remain_local():
    name = "extension.echo"
    alias = "mfx_" + hashlib.sha256(name.encode()).hexdigest()[:60]
    adapter, transport, _ = provider(
        [created(), *call_events(name=alias), completed([call_item(name=alias)])]
    )
    history = AssistantMessage(
        [
            TextContent("before tool"),
            ThinkingContent("private"),
            ToolCall("old-call", name, {"text": "old"}),
        ],
        MODEL.api,
        MODEL.provider,
        MODEL.id,
        Usage(),
        "tool_use",
        1,
    )
    tool_result = ToolResultMessage(
        "old-call",
        name,
        [TextContent("old-result")],
        False,
        2,
        details={"audit-only": "do not send"},
        structured_content={"private": True},
    )
    tool = ToolDeclaration(
        name, "Echo", {"type": "object", "properties": {"text": {"type": "string"}}}
    )
    ctx = context(
        [
            SystemMessage("rules", 0, tools_added=[tool]),
            UserMessage("question", 1),
            history,
            tool_result,
        ]
    )
    original = deepcopy(ctx.messages)
    stream = adapter.stream(MODEL, ctx)
    partial = []
    async for entry in stream:
        if isinstance(entry, ToolCallDelta):
            partial.append(deepcopy(entry.partial.content[entry.content_index].arguments))
    result = await stream.result()
    assert result.stop_reason == "tool_use"
    assert result.content == [ToolCall("call-0", name, {"text": "ok"})]
    assert partial == [{}, {}]  # Never expose partial JSON as executable arguments.
    payload = transport.requests[0].payload
    assert payload["tools"][0]["name"] == alias and payload["tools"][0]["strict"] is False
    assert "function" not in payload["tools"][0]
    assert payload["input"] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "before tool"},
        {
            "type": "function_call",
            "call_id": "old-call",
            "name": alias,
            "arguments": '{"text":"old"}',
        },
        {"type": "function_call_output", "call_id": "old-call", "output": "old-result"},
    ]
    assert "private" not in str(payload) and "audit-only" not in str(payload)
    assert ctx.messages == original
    followup, sent, _ = provider()
    await followup.stream(
        MODEL,
        context(
            [
                *ctx.messages,
                result,
                ToolResultMessage("call-0", name, [TextContent("new-result")], False, 3),
            ]
        ),
    ).result()
    assert sent.requests[0].payload["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call-0",
        "output": "new-result",
    }
    assert sent.requests[0].payload["input"][-2]["name"] == alias


@pytest.mark.parametrize("tool_image", [False, True])
async def test_responses_images_use_input_blocks_with_capability_gate(tool_image):
    image = ImageContent("eA==", "image/png")
    messages = [UserMessage([TextContent("look"), image], 1)]
    if tool_image:
        messages = [
            UserMessage("look", 1),
            AssistantMessage(
                [ToolCall("c", "echo", {})],
                MODEL.api,
                MODEL.provider,
                MODEL.id,
                Usage(),
                "tool_use",
                1,
            ),
            ToolResultMessage("c", "echo", [TextContent("look"), image], False, 2),
        ]
    ctx = context(messages)
    adapter, transport, _ = provider()
    with pytest.raises(ValueError, match="images"):
        adapter.stream(MODEL, ctx)
    assert not transport.requests
    adapter, transport, _ = provider(supports_images=True)
    await adapter.stream(MODEL, ctx).result()
    blocks = transport.requests[0].payload["input"][-1]["output" if tool_image else "content"]
    assert blocks == [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": "data:image/png;base64,eA=="},
    ]


@pytest.mark.parametrize("summaries", [[], ["first", "second"]])
async def test_responses_reasoning_encrypted_content_survives_stateless_tool_round(summaries):
    item = {"id": "rs-1", "type": "reasoning", "summary": [], "status": None}
    events = [created(), event("response.output_item.added", output_index=0, item=item)]
    for index, text in enumerate(summaries):
        ref = {"item_id": "rs-1", "output_index": 0, "summary_index": index}
        events += [
            event(
                "response.reasoning_summary_part.added",
                **ref,
                part={"type": "summary_text", "text": ""},
            ),
            event("response.reasoning_summary_text.delta", **ref, delta=text),
            event("response.reasoning_summary_text.done", **ref, text=text),
            event(
                "response.reasoning_summary_part.done",
                **ref,
                part={"type": "summary_text", "text": text},
            ),
        ]
    final = {
        **item,
        "summary": [{"type": "summary_text", "text": text} for text in summaries],
        "encrypted_content": "opaque-synthetic-reasoning",
    }
    events += [
        event("response.output_item.done", output_index=0, item=final),
        *call_events(index=1),
        completed([final, call_item(index=1)]),
    ]
    adapter, _, _ = provider(events, supports_reasoning=True)
    result = await adapter.stream(MODEL, context(), StreamOptions(reasoning="high")).result()
    assert result.stop_reason == "tool_use"
    assert result.content[0].thinking == "\n".join(summaries)
    assert "opaque-synthetic-reasoning" in result.content[0].thinking_signature
    tool_result = ToolResultMessage("call-1", "echo", [TextContent("ok")], False, 1)
    next_adapter, transport, _ = provider()
    await next_adapter.stream(
        MODEL, context([UserMessage("question", 1), result, tool_result])
    ).result()
    payload = transport.requests[0].payload
    assert payload["store"] is False and "previous_response_id" not in payload
    assert payload["input"][1] == {key: value for key, value in final.items() if key != "status"}
    assert payload["input"][2]["type"] == "function_call"
    for key in ("api", "provider", "model"):
        foreign = replace(result, **{key: "different"})
        adapter, sent, _ = provider()
        await adapter.stream(MODEL, context([UserMessage("q", 1), foreign, tool_result])).result()
        assert all(item.get("type") != "reasoning" for item in sent.requests[0].payload["input"])


async def test_responses_interleaved_parallel_function_calls_keep_call_ids():
    calls = [call_item(0), call_item(1)]
    events = [
        created(),
        call_events(0)[0],
        call_events(1)[0],
        *call_events(1)[1:],
        *call_events(0)[1:],
        completed(calls),
    ]
    adapter, _, _ = provider(events)
    result = await adapter.stream(MODEL, context()).result()
    assert result.content == [ToolCall(f"call-{i}", "echo", {"text": "ok"}) for i in range(2)]
    assert result.stop_reason == "tool_use"


async def test_responses_refusal_is_public_text_not_an_error_or_tool_call():
    item = text_item(content=[{"type": "refusal", "refusal": "Cannot help"}])
    ref = {"item_id": "msg-1", "output_index": 0, "content_index": 0}
    adapter, _, _ = provider(
        [
            created(),
            event(
                "response.output_item.added",
                output_index=0,
                item=text_item(content=[], status="in_progress"),
            ),
            event("response.content_part.added", **ref, part={"type": "refusal", "refusal": ""}),
            event("response.refusal.delta", **ref, delta="Cannot help"),
            event("response.refusal.done", **ref, refusal="Cannot help"),
            completed([item]),
        ]
    )
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "stop" and result.content == [TextContent("Cannot help")]


async def test_responses_incomplete_output_keeps_text_but_never_partial_tool_json():
    item = call_item(arguments='{"text":', status="incomplete")
    adapter, _, _ = provider(
        [
            created(),
            event(
                "response.incomplete",
                response=response(
                    [text_item("partial", status="incomplete"), item],
                    "incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                    usage=USAGE,
                ),
            ),
        ]
    )
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "length" and result.raw_stop_reason == "max_output_tokens"
    assert result.content == [TextContent("partial")]
    assert result.usage.effective_total == 110


@pytest.mark.parametrize(
    "events",
    [
        [],
        [created()],
        text_events()[:-1],
        text_events()[1:],
        [created(), created()],
        [created(), event("error", message=SECRET)],
        [created(), event("response.failed", response=response(error={"message": SECRET}))],
        [created(), event("response.completed", response=response(status="incomplete"))],
        [created(), completed([], id="other")],
        [
            created(),
            event(
                "response.output_text.delta",
                item_id="absent",
                output_index=0,
                content_index=0,
                delta="x",
            ),
        ],
        [created(), event("response.output_item.added", output_index=True, item=text_item())],
        [
            created(),
            event(
                "response.output_item.added",
                output_index=0,
                item={"id": "web", "type": "web_search_call"},
            ),
        ],
        [*text_events()[:-1], completed([])],
        [*text_events()[:-1], completed([text_item("changed")])],
        [created(), completed([call_item(arguments="{")])],
        [created(), completed([call_item(arguments="[]")])],
        [created(), completed([call_item(arguments='{"x":NaN}')])],
        [created(), completed([call_item(arguments='{"x":1,"x":2}')])],
        [created(), completed([call_item(), {**call_item(1), "call_id": "call-0"}])],
        [created(), completed([call_item(), {**call_item(1), "id": "fc-0"}])],
        [created(), completed([text_item(status="incomplete")])],
        [
            created(),
            event(
                "response.incomplete",
                response=response(
                    status="incomplete", incomplete_details={"reason": "content_filter"}
                ),
            ),
        ],
        [
            created(),
            event(
                "response.completed",
                response=response([text_item()], "completed", usage={**USAGE, "input_tokens": -1}),
            ),
        ],
        [
            created(),
            event(
                "response.completed",
                response=response(
                    [text_item()],
                    "completed",
                    usage={**USAGE, "input_tokens_details": {"cached_tokens": 101}},
                ),
            ),
        ],
        [
            created(),
            event(
                "response.completed",
                response=response(
                    [text_item()],
                    "completed",
                    usage={**USAGE, "output_tokens_details": {"reasoning_tokens": 11}},
                ),
            ),
        ],
    ],
)
async def test_responses_malformed_or_truncated_streams_are_sanitized(events):
    adapter, transport, raw = provider(deepcopy(events))
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and result.error_message == "Invalid provider response"
    assert result.diagnostics == [{"code": "invalid_response"}]
    assert SECRET not in repr(result) and raw.closed and len(transport.requests) == 1


async def test_responses_cancel_partial_call_keeps_only_finalized_calls_and_closes():
    gate, signal = Gate(), Signal()
    adapter, _, raw = provider([created(), *call_events(), *call_events(1)[:2], gate])
    stream = adapter.stream(MODEL, context(), StreamOptions(signal=signal))
    async with asyncio.timeout(2):
        await gate.entered.wait()
        signal.event.set()
        result = await stream.result()
    assert result.stop_reason == "aborted"
    assert result.content == [ToolCall("call-0", "echo", {"text": "ok"})]
    assert raw.closed and gate.settled.is_set()


@pytest.mark.parametrize("field", ["input_tokens_details", "output_tokens_details"])
@pytest.mark.parametrize("invalid", [False, [], "invalid"])
async def test_responses_rejects_nonobject_usage_details(field, invalid):
    adapter, _, raw = provider(
        [
            created(),
            event(
                "response.completed",
                response=response([text_item()], "completed", usage={**USAGE, field: invalid}),
            ),
        ]
    )
    result = await adapter.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and result.error_message == "Invalid provider response"
    assert raw.closed
