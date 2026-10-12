import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from moonfox_ai import api
from moonfox_ai.api import (
    AnthropicProvider,
    AssistantMessage,
    Context,
    HTTPRequest,
    HTTPResponse,
    HTTPTransport,
    ImageContent,
    Model,
    NestedToolCall,
    OpenAIProvider,
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
from moonfox_ai.providers._sse import InvalidResponse, iter_sse

MODEL = Model("test-model", "test-provider", "test-api", max_tokens=123)
PROVIDERS = [OpenAIProvider, AnthropicProvider]
ALL_PROVIDERS = [*PROVIDERS, OpenAIResponsesProvider]
SECRET = "sk-private-credential"


class Signal:
    def __init__(self):
        self.event = asyncio.Event()

    @property
    def aborted(self):
        return self.event.is_set()

    async def wait(self):
        await self.event.wait()


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = asyncio.Event()

    async def wait(self):
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.settled.set()


class Response:
    def __init__(self, lines=(), *, status=200, headers=None, close_gate=None, close_error=None):
        self.status = status
        self.headers = headers or {"content-type": "text/event-stream"}
        self.lines = list(lines)
        self.close_gate = close_gate
        self.close_error = close_error
        self.close_count = 0
        self.closed = False
        self.iterated = False
        self.lines_settled = False

    async def iter_lines(self):
        self.iterated = True
        try:
            for item in self.lines:
                if isinstance(item, Gate):
                    await item.wait()
                elif isinstance(item, Exception):
                    raise item
                else:
                    yield item
        finally:
            self.lines_settled = True

    async def aclose(self):
        self.close_count += 1
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class Transport:
    """Intentionally ignores the signal: the adapter must own the cancellation race."""

    def __init__(self, response, *, gate=None, error=None, return_on_cancel=False):
        self.response = response
        self.gate = gate
        self.error = error
        self.return_on_cancel = return_on_cancel
        self.requests = []
        self.settled = asyncio.Event()

    async def __call__(self, request, signal):
        self.requests.append(request)
        try:
            if self.gate is not None:
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    if not self.return_on_cancel:
                        raise
            if self.error is not None:
                raise self.error
            return self.response
        finally:
            self.settled.set()


def frame(data, event=None):
    prefix = [f"event: {event}"] if event else []
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return [*prefix, f"data: {text}", ""]


def oa(delta=None, finish=None, **extra):
    return frame(
        {
            "id": "response-1",
            "model": "resolved-model",
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
            **extra,
        }
    )


def ae(kind, **data):
    return frame({"type": kind, **data}, kind)


def astart(**usage):
    return ae(
        "message_start",
        message={
            "id": "response-1",
            "model": "resolved-model",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 10, "output_tokens": 0, **usage},
        },
    )


def aend(reason="end_turn", **usage):
    return [
        *ae("message_delta", delta={"stop_reason": reason}, usage={"output_tokens": 2, **usage}),
        *ae("message_stop"),
    ]


def success(cls):
    if cls is OpenAIProvider:
        return [*oa({"content": "ok"}), *oa(finish="stop"), *frame("[DONE]")]
    if cls is OpenAIResponsesProvider:
        identity = {"id": "response-1", "model": "resolved-model"}
        return [
            *ae("response.created", response={**identity, "status": "in_progress", "output": []}),
            *ae(
                "response.completed",
                response={
                    **identity,
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg-1",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                        }
                    ],
                },
            ),
        ]
    return [
        *astart(),
        *ae("content_block_start", index=0, content_block={"type": "text", "text": "ok"}),
        *ae("content_block_stop", index=0),
        *aend(),
    ]


def context(messages=None, **kwargs):
    return normalize_context(Context(messages or [UserMessage("question", 1)], **kwargs))


def assistant(blocks):
    return AssistantMessage(blocks, MODEL.api, MODEL.provider, MODEL.id, Usage(), "stop", 0)


def tool_result(call_id="call-1", name="echo", content=None):
    return ToolResultMessage(call_id, name, content or [TextContent("result")], False, 1)


def provider(cls, lines=None, **kwargs):
    response = Response(success(cls) if lines is None else lines)
    transport = Transport(response)
    instance = cls(transport, base_url="https://example.invalid/v1/", api_key=SECRET, **kwargs)
    return instance, transport, response


def test_public_exports_preserve_existing_surface():
    for symbol in (*ALL_PROVIDERS, HTTPRequest, HTTPResponse, HTTPTransport):
        assert getattr(api, symbol.__name__) is symbol
        assert symbol.__name__ in api.__all__
    assert "FauxProvider" in api.__all__ and "StreamFn" in api.__all__


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
@pytest.mark.parametrize(
    "url",
    [
        "",
        "/v1",
        "ftp://example.invalid",
        "https://u:password@example.invalid/v1",
        "https://example.invalid/v1?key=secret",
        "https://example.invalid/#secret",
        "https://example.invalid/?",
        "https://example.invalid/#",
        "https://",
        "https://example.invalid:bad",
        "https://example.invalid:0",
        "https://[invalid",
        "https://example.invalid\n",
        "https://example.invalid\\other",
    ],
)
def test_constructor_rejects_unsafe_urls_without_echo(cls, url):
    transport = Transport(Response())
    with pytest.raises(ValueError) as exc:
        cls(transport, base_url=url, api_key=SECRET)
    assert str(exc.value) == "Invalid provider base URL"
    assert not transport.requests


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
def test_config_is_immutable_private_and_has_no_construction_io(cls):
    p, transport, _ = provider(cls)
    assert SECRET not in repr(p) and not transport.requests
    with pytest.raises(AttributeError, match="immutable"):
        p._url = "http://other.invalid"
    with pytest.raises(AttributeError, match="immutable"):
        del p._supports_tools
    with pytest.raises(ValueError, match="credential"):
        cls(transport, base_url="https://example.invalid", api_key=f"{SECRET}\r\ninjected")
    for timeout in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            cls(transport, base_url="https://example.invalid", timeout_seconds=timeout)


@pytest.mark.parametrize("cls", PROVIDERS)
async def test_default_request_and_key_override_are_private(cls):
    p, transport, response = provider(cls, timeout_seconds=15)
    captures = []
    options = StreamOptions(
        api_key="override-private",
        headers={"x-trusted": "ok"},
        on_payload=lambda payload, model: captures.append(deepcopy(payload)),
    )
    stream = p.stream(MODEL, context(), options)
    options.headers["x-trusted"] = "changed"
    result = await stream.result()
    request = transport.requests[0]
    assert result.content == [TextContent("ok")]
    assert request.method == "POST" and request.timeout_seconds == 15
    suffix = "chat/completions" if cls is OpenAIProvider else "messages"
    assert request.url == f"https://example.invalid/v1/{suffix}"
    assert request.headers["Accept"] == "text/event-stream"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["x-trusted"] == "ok"
    if cls is OpenAIProvider:
        assert request.headers["Authorization"] == "Bearer override-private"
    else:
        assert request.headers["x-api-key"] == "override-private"
        assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.payload["max_tokens"] == 123
    assert "override-private" not in repr(request) + repr(result) + repr(captures)
    assert SECRET not in repr(request) + repr(p) + repr(captures)
    assert response.closed and response.lines_settled and response.close_count == 1
    await stream.aclose()
    assert response.close_count == 1


async def test_openai_all_sampling_parameters_and_payload_hook():
    p, transport, _ = provider(
        OpenAIProvider, token_field="max_completion_tokens", supports_reasoning=True
    )
    seen = []

    def hook(payload, model):
        seen.append(deepcopy(payload))
        assert model is MODEL
        return {**payload, "custom": True}

    result = await p.stream(
        MODEL,
        context(),
        StreamOptions(
            temperature=0,
            top_p=0,
            frequency_penalty=0,
            presence_penalty=0,
            seed=0,
            max_tokens=8,
            stop=("END",),
            reasoning="medium",
            on_payload=hook,
        ),
    ).result()
    assert result.stop_reason == "stop"
    payload = transport.requests[0].payload
    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed"):
        assert payload[key] == 0
    assert payload["max_completion_tokens"] == 8 and "max_tokens" not in payload
    assert payload["stop"] == ["END"] and payload["reasoning_effort"] == "medium"
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["custom"] is True and "custom" not in seen[0]


@pytest.mark.parametrize("cls", PROVIDERS)
async def test_default_tokens_off_reasoning_and_no_auth(cls):
    transport = Transport(Response(success(cls)))
    p = cls(transport, base_url="http://localhost/v1")
    await p.stream(
        replace(MODEL, max_tokens=None), context(), StreamOptions(reasoning="off")
    ).result()
    request = transport.requests[0]
    assert request.payload["max_tokens"] == 4096
    assert "reasoning_effort" not in request.payload and "thinking" not in request.payload
    assert "Authorization" not in request.headers and "x-api-key" not in request.headers


@pytest.mark.parametrize(
    "options",
    [
        {"frequency_penalty": 0},
        {"presence_penalty": 0},
        {"seed": 0},
        {"reasoning": "low"},
        {"temperature": 1.1},
        {"temperature": 0, "top_p": 0},
    ],
)
def test_anthropic_rejects_unsupported_sampling(options):
    p, transport, _ = provider(AnthropicProvider)
    with pytest.raises(ValueError):
        p.stream(MODEL, context(), StreamOptions(**options))
    assert not transport.requests


@pytest.mark.parametrize("sampling", [{"temperature": 0}, {"top_p": 0}])
async def test_anthropic_sampling_and_stop(sampling):
    p, transport, _ = provider(AnthropicProvider)
    await p.stream(
        MODEL, context(), StreamOptions(**sampling, stop=("END",), max_tokens=9)
    ).result()
    payload = transport.requests[0].payload
    assert payload.items() >= sampling.items()
    assert payload["stop_sequences"] == ["END"] and payload["max_tokens"] == 9


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": 0},
        {"max_tokens": True},
        {"temperature": float("nan")},
        {"top_p": -0.1},
        {"stop": ("END", "")},
        {"stop": []},
        {"headers": {"authorization": "other"}},
        {"headers": {"HOST": "other"}},
        {"headers": {"Content-Type": "other"}},
        {"headers": {"anthropic-version": "other"}},
        {"headers": {"x-api-key": "other"}},
        {"headers": {"Content-Length": "5"}},
        {"headers": {"x-test": "bad\nvalue"}},
        {"headers": {"bad name": "value"}},
    ],
)
def test_bad_options_fail_before_network(cls, options):
    p, transport, _ = provider(cls)
    with pytest.raises((ValueError, TypeError)):
        p.stream(MODEL, context(), StreamOptions(**options))
    assert not transport.requests


def test_openai_rejects_unsupported_reasoning_and_invalid_seed():
    p, transport, _ = provider(OpenAIProvider)
    for options in (
        StreamOptions(reasoning="low"),
        StreamOptions(reasoning="extreme"),
        StreamOptions(seed=True),
        StreamOptions(frequency_penalty=3),
    ):
        with pytest.raises(ValueError):
            p.stream(MODEL, context(), options)
    assert not transport.requests


@pytest.mark.parametrize("cls", PROVIDERS)
async def test_transcript_replay_tools_and_audit_fields_stay_local(cls):
    declaration = ToolDeclaration("echo", "description", {"type": "object"})
    result = tool_result()
    result.details = {"private": "audit-only"}
    result.structured_content = {"private": "structured-only"}
    result.nested_calls = [NestedToolCall(ToolCall("nested", "hidden", {}), tool_result())]
    result.is_error = True
    old = assistant(
        [
            ThinkingContent("hidden-thought", thinking_signature="hidden-signature", redacted=True),
            TextContent("before"),
            ToolCall("call-1", "echo", {"x": "value"}),
        ]
    )
    old.diagnostics = [{"private": "diagnostic-only"}]
    ctx = context(
        [
            SystemMessage("base", 0, sections={"a": "old", "b": "removed"}),
            UserMessage("one", 1),
            UserMessage([TextContent("two")], 2),
            old,
            result,
            SystemMessage(
                "later",
                3,
                sections={"a": "new", "b": None},
                tools_removed=["obsolete"],
                tools_added=[declaration],
            ),
            UserMessage("next", 4),
        ],
        tools=[ToolDeclaration("obsolete", "old", {"type": "object"})],
    )
    snapshot = deepcopy(ctx.messages)
    p, transport, _ = provider(cls)
    stream = p.stream(MODEL, ctx)
    ctx.messages[-1].content = "changed"
    await stream.result()
    payload = transport.requests[0].payload
    serialized = json.dumps(payload)
    for private in (
        "audit-only",
        "structured-only",
        "hidden-thought",
        "hidden-signature",
        "nested",
        "diagnostic-only",
        "obsolete",
        "changed",
    ):
        assert private not in serialized
    assert len(payload["tools"]) == 1
    if cls is OpenAIProvider:
        assert payload["messages"][0] == {"role": "system", "content": "base\n\nlater\n\nnew"}
        assert [item["role"] for item in payload["messages"]] == [
            "system",
            "user",
            "user",
            "assistant",
            "tool",
            "user",
        ]
        call = payload["messages"][3]["tool_calls"][0]
        assert call["id"] == "call-1" and json.loads(call["function"]["arguments"]) == {
            "x": "value"
        }
        assert payload["messages"][4] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "result",
        }
        assert payload["tools"][0]["function"]["parameters"] == {"type": "object"}
        assert payload["tool_choice"] == "auto"
    else:
        assert payload["system"] == "base\n\nlater\n\nnew"
        assert [item["role"] for item in payload["messages"]] == ["user", "assistant", "user"]
        assert len(payload["messages"][0]["content"]) == 2
        assert payload["messages"][1]["content"][-1] == {
            "type": "tool_use",
            "id": "call-1",
            "name": "echo",
            "input": {"x": "value"},
        }
        assert payload["messages"][2]["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "is_error": True,
            "content": [{"type": "text", "text": "result"}],
        }
        assert payload["tools"][0]["input_schema"] == {"type": "object"}
        assert payload["tool_choice"] == {"type": "auto"}
    assert ctx.messages[:-1] == snapshot[:-1]


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
def test_context_brand_pairing_and_tools_capability_before_network(cls):
    p, transport, _ = provider(cls)
    with pytest.raises(TypeError, match="normalized"):
        p.stream(MODEL, Context([]))
    call = assistant([ToolCall("call-1", "echo", {})])
    bad_transcripts = [
        [tool_result()],
        [call],
        [call, tool_result(name="other")],
        [call, tool_result(), tool_result()],
        [call, UserMessage("interrupt", 1), tool_result()],
        [assistant([ToolCall("same", "echo", {}), ToolCall("same", "echo", {})])],
        [assistant([ToolCall("", "echo", {})]), tool_result("")],
    ]
    for messages in bad_transcripts:
        with pytest.raises(ValueError, match="Transcript"):
            p.stream(MODEL, context(messages))
    assert not transport.requests
    disabled, transport, _ = provider(cls, supports_tools=False)
    with pytest.raises(ValueError, match="tools"):
        disabled.stream(MODEL, context(tools=[ToolDeclaration("echo", "", {})]))
    with pytest.raises(ValueError, match="tools"):
        disabled.stream(MODEL, context([call, tool_result()]))
    assert not transport.requests


@pytest.mark.parametrize("cls", PROVIDERS)
async def test_images_require_capability_and_use_protocol_blocks(cls):
    ctx = context([UserMessage([TextContent("describe"), ImageContent("AA==", "image/png")], 0)])
    disabled, transport, _ = provider(cls)
    with pytest.raises(ValueError, match="images"):
        disabled.stream(MODEL, ctx)
    assert not transport.requests
    p, transport, _ = provider(cls, supports_images=True)
    await p.stream(MODEL, ctx).result()
    image = transport.requests[0].payload["messages"][0]["content"][1]
    if cls is OpenAIProvider:
        assert image == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    else:
        assert image == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AA==",
            },
        }
    with pytest.raises(ValueError, match="assistant content"):
        p.stream(MODEL, context([assistant([ImageContent("AA==", "image/png")])]))


async def test_image_tool_results_supported_only_by_anthropic():
    ctx = context(
        [
            assistant([ToolCall("call-1", "echo", {})]),
            tool_result(content=[ImageContent("AA==", "image/png")]),
        ]
    )
    p, transport, _ = provider(OpenAIProvider, supports_images=True)
    with pytest.raises(ValueError, match="text only"):
        p.stream(MODEL, ctx)
    assert not transport.requests
    p, transport, _ = provider(AnthropicProvider, supports_images=True)
    await p.stream(MODEL, ctx).result()
    image = transport.requests[0].payload["messages"][1]["content"][0]["content"][0]
    assert image["type"] == "image"


async def test_openai_text_live_events_usage_and_identity():
    lines = [
        ": comment\r\n",
        "\r\n",
        *oa({"role": "assistant", "content": "a"}),
        *oa({"content": "b"}),
        *oa(finish="stop"),
        *frame(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 6,
                    "total_tokens": 26,
                    "prompt_tokens_details": {"cached_tokens": 5},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            }
        ),
        *frame("[DONE]"),
    ]
    p, _, response = provider(OpenAIProvider, lines)
    stream = p.stream(MODEL, context())
    events, snapshots = [], []
    async for event in stream:
        events.append(event)
        if isinstance(event, TextDelta):
            snapshots.append(deepcopy(event.partial))
    result = await stream.result()
    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]
    assert [item.content[0].text for item in snapshots] == ["a", "ab"]
    assert all(event.partial is result for event in events[:-1])
    assert result.response_id == "response-1" and result.response_model == "resolved-model"
    assert result.model == MODEL.id and result.stop_reason == "stop"
    assert result.usage == Usage(input=15, output=6, cache_read=5, total_tokens=26, reasoning=2)
    assert response.closed and response.lines_settled


async def test_openai_incremental_tool_ids_names_arguments_and_thinking():
    lines = [
        *oa({"reasoning_content": "internal"}),
        *oa({"content": "working"}),
        *oa(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-",
                        "type": "function",
                        "function": {
                            "name": "ec",
                            "arguments": '{"x":',
                        },
                    },
                    {
                        "index": 1,
                        "id": "second",
                        "type": "function",
                        "function": {
                            "name": "other",
                            "arguments": "{",
                        },
                    },
                ]
            }
        ),
        *oa(
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": "}"}},
                    {"index": 0, "id": "1", "function": {"name": "ho", "arguments": "1}"}},
                ]
            }
        ),
        *oa(finish="tool_calls"),
        *frame("[DONE]"),
    ]
    p, _, _ = provider(OpenAIProvider, lines)
    stream = p.stream(MODEL, context())
    events, snapshots = [], []
    async for event in stream:
        events.append(event)
        if isinstance(event, ToolCallDelta):
            snapshots.append(deepcopy(event.partial.content[event.content_index]))
    result = await stream.result()
    assert result.content == [
        ThinkingContent("internal"),
        TextContent("working"),
        ToolCall("call-1", "echo", {"x": 1}),
        ToolCall("second", "other", {}),
    ]
    assert all(block.arguments == {} for block in snapshots)
    assert [event.content_index for event in events if event.type == "toolcall_start"] == [2, 3]
    assert [event.content_index for event in events if event.type == "toolcall_end"] == [2, 3]
    assert events[-1].reason == result.stop_reason == "tool_use"
    assert any(event.type == "thinking_delta" for event in events)


async def test_anthropic_blocks_signatures_tools_usage_and_identity():
    lines = [
        *ae("ping"),
        *astart(cache_read_input_tokens=4, cache_creation_input_tokens=3),
        *ae("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""}),
        *ae(
            "content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "internal"}
        ),
        *ae("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "sig"}),
        *ae("content_block_stop", index=0),
        *ae("content_block_start", index=1, content_block={"type": "text", "text": "a"}),
        *ae("content_block_delta", index=1, delta={"type": "text_delta", "text": "b"}),
        *ae("content_block_stop", index=1),
        *ae(
            "content_block_start",
            index=2,
            content_block={
                "type": "tool_use",
                "id": "call-1",
                "name": "echo",
                "input": {},
            },
        ),
        *ae(
            "content_block_delta",
            index=2,
            delta={"type": "input_json_delta", "partial_json": '{"x":'},
        ),
        *ae(
            "content_block_delta",
            index=2,
            delta={"type": "input_json_delta", "partial_json": '"v"}'},
        ),
        *ae("content_block_stop", index=2),
        *aend("tool_use", output_tokens=8),
    ]
    p, _, response = provider(AnthropicProvider, lines)
    stream = p.stream(MODEL, context())
    events, snapshots = [], []
    async for event in stream:
        events.append(event)
        if isinstance(event, ToolCallDelta):
            snapshots.append(deepcopy(event.partial.content[event.content_index]))
    result = await stream.result()
    assert result.content == [
        ThinkingContent("internal", thinking_signature="sig"),
        TextContent("ab"),
        ToolCall("call-1", "echo", {"x": "v"}),
    ]
    assert all(block.arguments == {} for block in snapshots)
    assert [event.type for event in events] == [
        "start",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    assert all(event.partial is result for event in events[:-1])
    assert result.usage == Usage(input=10, output=8, cache_read=4, cache_write=3, total_tokens=25)
    assert result.response_id == "response-1" and result.response_model == "resolved-model"
    assert events[-1].reason == result.stop_reason == "tool_use"
    assert response.closed


@pytest.mark.parametrize(
    "cls,wire,expected",
    [
        (OpenAIProvider, "stop", "stop"),
        (OpenAIProvider, "length", "length"),
        (AnthropicProvider, "end_turn", "stop"),
        (AnthropicProvider, "stop_sequence", "stop"),
        (AnthropicProvider, "max_tokens", "length"),
    ],
)
async def test_finish_reason_mappings(cls, wire, expected):
    lines = (
        [*oa(finish=wire), *frame("[DONE]")] if cls is OpenAIProvider else [*astart(), *aend(wire)]
    )
    p, _, response = provider(cls, lines)
    stream = p.stream(MODEL, context())
    events = [event async for event in stream]
    result = await stream.result()
    assert result.stop_reason == events[-1].reason == expected
    assert result.raw_stop_reason == wire
    assert response.closed


@pytest.mark.parametrize(
    "lines",
    [
        frame(f'{{"private":"{SECRET}", invalid}}'),
        frame("[DONE]"),
        oa(finish="stop"),
        [*oa(), *frame("[DONE]")],
        [f"data: {json.dumps({'choices': []})}"],
        [*oa(finish="content_filter"), *frame("[DONE]")],
        oa({"content": ["bad"]}),
        frame({"choices": None}),
        oa({"role": "user"}),
        [*oa(finish="stop"), *frame({"choices": [], "usage": {"prompt_tokens": -1}})],
        [*oa(finish="stop"), *oa({"content": "after finish"})],
        frame({"error": {"message": SECRET}}),
        frame('{"choices": [], "choices": []}'),
    ],
)
async def test_openai_malformed_or_truncated_stream_is_sanitized(lines):
    p, _, response = provider(OpenAIProvider, lines)
    stream = p.stream(MODEL, context())
    events = [event async for event in stream]
    result = await stream.result()
    assert events[-1].type == "error" and result.stop_reason == "error"
    assert result.error_message == "Invalid provider response"
    assert SECRET not in repr(result) + repr(events)
    assert response.closed and response.lines_settled


@pytest.mark.parametrize(
    "arguments,call_id,name",
    [
        ('{"x":', "call", "echo"),
        ("[]", "call", "echo"),
        ("{}", "", "echo"),
        ("{}", "call", ""),
        ('{"x": NaN}', "call", "echo"),
        ('{"x": 1e400}', "call", "echo"),
    ],
)
async def test_invalid_openai_calls_are_stripped(arguments, call_id, name):
    lines = [
        *oa(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        },
                    }
                ]
            }
        ),
        *oa(finish="tool_calls"),
        *frame("[DONE]"),
    ]
    p, _, response = provider(OpenAIProvider, lines)
    result = await p.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and not result.content
    assert response.closed


async def test_duplicate_openai_ids_only_keep_complete_unique_calls():
    lines = [
        *oa(
            {
                "tool_calls": [
                    {"index": i, "id": "same", "function": {"name": "echo", "arguments": "{}"}}
                    for i in range(2)
                ]
            }
        ),
        *oa(finish="tool_calls"),
        *frame("[DONE]"),
    ]
    p, _, _ = provider(OpenAIProvider, lines)
    result = await p.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and result.content == [ToolCall("same", "echo", {})]


@pytest.mark.parametrize(
    "lines",
    [
        ae("error", error={"message": SECRET}),
        ae("message_stop"),
        astart(),
        [*astart(), *ae("message_stop")],
        [*astart(), *astart()],
        [*astart(), *ae("content_block_delta", index=0, delta={"type": "text_delta", "text": "x"})],
        [
            *astart(),
            *ae("content_block_start", index=2, content_block={"type": "text", "text": ""}),
        ],
        [*astart(), *ae("content_block_start", index=0, content_block={"type": "image"})],
        [
            *astart(),
            *ae("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            *aend(),
        ],
        [*astart(), *ae("message_delta", delta={"stop_reason": "tool_use"})],
        [*astart(), *ae("message_delta", delta={"stop_reason": "unexpected"})],
        [*astart(), *frame({"type": "message_stop"}, "content_block_stop")],
        [
            *astart(),
            *ae(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "call",
                    "name": "echo",
                    "input": {},
                },
            ),
            *ae(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": "["},
            ),
            *ae("content_block_stop", index=0),
        ],
    ],
)
async def test_anthropic_malformed_order_or_json_is_sanitized(lines):
    p, _, response = provider(AnthropicProvider, lines)
    stream = p.stream(MODEL, context())
    events = [event async for event in stream]
    result = await stream.result()
    assert events[-1].type == "error" and result.error_message == "Invalid provider response"
    assert not any(isinstance(block, ToolCall) for block in result.content)
    assert SECRET not in repr(result) + repr(events)
    assert response.closed


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
@pytest.mark.parametrize(
    "status,text",
    [
        (401, "Provider authentication failed"),
        (403, "Provider authentication failed"),
        (429, "Provider rate limit exceeded"),
        (503, "Provider service unavailable"),
        (400, "Provider request failed"),
    ],
)
async def test_http_failures_never_read_or_expose_body_and_response_hook_is_safe(cls, status, text):
    response = Response(
        frame({"private": SECRET}),
        status=status,
        headers={
            "x-api-key": SECRET,
            "Authorization": SECRET,
            "set-cookie": SECRET,
            "x-request-id": f"id-{SECRET}",
            "retry-after": "5",
            "other": SECRET,
        },
    )
    transport = Transport(response)
    p = cls(transport, base_url="https://example.invalid", api_key=SECRET)
    seen = []
    stream = p.stream(
        MODEL, context(), StreamOptions(on_response=lambda info, model: seen.append(info))
    )
    events = [event async for event in stream]
    result = await stream.result()
    assert [event.type for event in events] == ["error"]
    assert result.error_message == text and result.diagnostics == [
        {"code": "http_error", "status": status}
    ]
    assert seen[0].body is None and seen[0].headers == {
        "x-request-id": "id-[redacted]",
        "retry-after": "5",
    }
    assert SECRET not in repr(result) + repr(events) + repr(seen)
    assert not response.iterated and response.closed and len(transport.requests) == 1


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
@pytest.mark.parametrize("stage", ["open", "read", "close"])
@pytest.mark.parametrize("error", [OSError(SECRET), TimeoutError(SECRET)])
async def test_operational_transport_errors_are_sanitized_without_retries(cls, stage, error):
    response = Response(
        [error] if stage == "read" else success(cls),
        close_error=error if stage == "close" else None,
    )
    transport = Transport(response, error=error if stage == "open" else None)
    p = cls(transport, base_url="https://example.invalid", api_key=SECRET)
    result = await p.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and SECRET not in repr(result)
    assert len(transport.requests) == 1
    assert response.closed == (stage != "open")


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_programmer_failures_are_exceptions_and_still_close(cls):
    response = Response(success(cls))
    bug = AssertionError("transport implementation bug")
    transport = Transport(response, error=bug)
    p = cls(transport, base_url="https://example.invalid")
    with pytest.raises(AssertionError) as exc:
        await p.stream(MODEL, context()).result()
    assert exc.value is bug and len(transport.requests) == 1
    p, transport, response = provider(cls)
    bug = OSError("callback bug, not a network failure")

    def bad_hook(value, model):
        raise bug

    with pytest.raises(OSError) as exc:
        p.stream(MODEL, context(), StreamOptions(on_payload=bad_hook))
    assert exc.value is bug and not transport.requests
    with pytest.raises(OSError) as exc:
        await p.stream(MODEL, context(), StreamOptions(on_response=bad_hook)).result()
    assert exc.value is bug and response.closed


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_preaborted_signal_does_not_open_transport(cls):
    p, transport, response = provider(cls)
    signal = Signal()
    signal.event.set()
    stream = p.stream(MODEL, context(), StreamOptions(signal=signal))
    assert [event.type async for event in stream] == ["error"]
    assert (await stream.result()).stop_reason == "aborted"
    assert not transport.requests and not response.closed


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
@pytest.mark.parametrize("stage", ["open", "read"])
@pytest.mark.parametrize("cancel", ["signal", "close"])
async def test_cancel_pending_operations_joins_every_owned_task(cls, stage, cancel):
    baseline = asyncio.all_tasks()
    gate = Gate()
    response = Response([gate])
    transport = Transport(response, gate=gate if stage == "open" else None)
    p = cls(transport, base_url="https://example.invalid")
    signal = Signal()
    stream = p.stream(MODEL, context(), StreamOptions(signal=signal))
    async with asyncio.timeout(2):
        await gate.entered.wait()
        if cancel == "signal":
            signal.event.set()
        else:
            await stream.aclose()
        result = await stream.result()
        await stream.aclose()
    assert result.stop_reason == "aborted" and result.error_message == "Operation aborted"
    assert gate.settled.is_set() and transport.settled.is_set()
    assert response.closed == (stage == "read")
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_late_open_response_is_closed_when_transport_returns_on_cancel(cls):
    baseline = asyncio.all_tasks()
    gate, signal = Gate(), Signal()
    response = Response()
    transport = Transport(response, gate=gate, return_on_cancel=True)
    p = cls(transport, base_url="https://example.invalid")
    stream = p.stream(MODEL, context(), StreamOptions(signal=signal))
    async with asyncio.timeout(2):
        await gate.entered.wait()
        signal.event.set()
        assert (await stream.result()).stop_reason == "aborted"
    assert response.closed and response.close_count == 1
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_terminal_publication_waits_for_close_despite_repeated_cancellation(cls):
    baseline = asyncio.all_tasks()
    gate = Gate()
    response = Response(success(cls), close_gate=gate)
    p = cls(Transport(response), base_url="https://example.invalid")
    stream = p.stream(MODEL, context())
    result_task = asyncio.create_task(stream.result())
    async with asyncio.timeout(2):
        await gate.entered.wait()
        assert not stream.done and not result_task.done()
        closers = [asyncio.create_task(stream.aclose()) for _ in range(3)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        closers[0].cancel()
        await asyncio.sleep(0)
        assert not stream.done and not gate.settled.is_set()
        gate.release.set()
        await asyncio.gather(*closers, return_exceptions=True)
        result = await result_task
    assert result.stop_reason == "aborted" and response.close_count == 1 and response.closed
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_successful_result_also_joins_tasks_before_return(cls):
    baseline = asyncio.all_tasks()
    gate = Gate()
    response = Response(success(cls), close_gate=gate)
    p = cls(Transport(response), base_url="https://example.invalid")
    stream = p.stream(MODEL, context(), StreamOptions(signal=Signal()))
    async with asyncio.timeout(2):
        await gate.entered.wait()
        assert not stream.done
        gate.release.set()
        assert (await stream.result()).stop_reason == "stop"
    assert response.closed and asyncio.all_tasks() == baseline


@pytest.mark.parametrize("cls", PROVIDERS)
async def test_abort_pending_tool_arguments_removes_only_incomplete_calls(cls):
    gate = Gate()
    if cls is OpenAIProvider:
        lines = [
            *oa(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "partial",
                            "function": {
                                "name": "echo",
                                "arguments": '{"x":',
                            },
                        }
                    ]
                }
            ),
            gate,
        ]
        expected = []
    else:
        lines = [
            *astart(),
            *ae(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "complete",
                    "name": "echo",
                    "input": {},
                },
            ),
            *ae("content_block_stop", index=0),
            *ae(
                "content_block_start",
                index=1,
                content_block={
                    "type": "tool_use",
                    "id": "partial",
                    "name": "echo",
                    "input": {},
                },
            ),
            *ae(
                "content_block_delta",
                index=1,
                delta={
                    "type": "input_json_delta",
                    "partial_json": '{"x":',
                },
            ),
            gate,
        ]
        expected = [ToolCall("complete", "echo", {})]
    p, _, response = provider(cls, lines)
    signal = Signal()
    stream = p.stream(MODEL, context(), StreamOptions(signal=signal))
    async with asyncio.timeout(2):
        await gate.entered.wait()
        signal.event.set()
        result = await stream.result()
    assert result.stop_reason == "aborted" and result.content == expected and response.closed
    # The returned message can be paired and replayed without invalid call IDs/JSON.
    replay = context([result, *[tool_result(call.id, call.name) for call in expected]])
    next_provider, _, _ = provider(cls)
    assert (await next_provider.stream(MODEL, replay).result()).stop_reason == "stop"


async def test_sse_multiline_comments_crlf_and_extension_fields():
    response = Response(
        [
            "\ufeff: comment\r\n",
            "event: custom\r\n",
            "id: unused\r\n",
            "retry: 2\r\n",
            "data: {\r\n",
            'data: "x": 1}\r\n',
            "\r\n",
        ]
    )
    frames = [item async for item in iter_sse(response.iter_lines())]
    assert len(frames) == 1 and frames[0].event == "custom"
    assert json.loads(frames[0].data) == {"x": 1}


@pytest.mark.parametrize(
    "lines,bounds",
    [
        (["data: " + "x" * 100, ""], {"max_frame_bytes": 80}),
        (["data: " + "x" * 40, "data: " + "x" * 40, ""], {"max_frame_bytes": 80}),
        (["data: x", ""] * 10, {"max_frame_bytes": 20, "max_total_bytes": 60}),
        (["data: unfinished"], {}),
        (["data: bad\nembedded", ""], {}),
        (["data: \ud800", ""], {}),
        ([None], {}),
    ],
)
async def test_sse_rejects_oversize_invalid_and_unterminated_frames(lines, bounds):
    with pytest.raises(InvalidResponse, match="Invalid provider response"):
        _ = [item async for item in iter_sse(Response(lines).iter_lines(), **bounds)]


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_immediate_close_and_cancel_without_signal_settle(cls):
    baseline = asyncio.all_tasks()
    p, _, _ = provider(cls)
    stream = p.stream(MODEL, context())
    await stream.aclose()
    assert (await stream.result()).stop_reason == "aborted"
    gate = Gate()
    response = Response([gate])
    p = cls(Transport(response), base_url="https://example.invalid")
    stream = p.stream(MODEL, context())
    async with asyncio.timeout(2):
        await gate.entered.wait()
        await stream.aclose()
    assert response.closed and (await stream.result()).stop_reason == "aborted"
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
@pytest.mark.parametrize("stage", ["open", "read"])
async def test_adapter_timeout_cancels_uncooperative_transport(cls, stage):
    baseline = asyncio.all_tasks()
    gate = Gate()
    response = Response([gate])
    transport = Transport(response, gate=gate if stage == "open" else None)
    p = cls(transport, base_url="https://example.invalid", timeout_seconds=0.05)
    async with asyncio.timeout(2):
        result = await p.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and result.error_message == "Provider request timed out"
    assert gate.settled.is_set() and transport.settled.is_set()
    assert response.closed == (stage == "read")
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("cls", ALL_PROVIDERS)
async def test_transport_decode_failure_is_an_operational_error(cls):
    p, _, response = provider(cls, [UnicodeError(SECRET)])
    result = await p.stream(MODEL, context()).result()
    assert result.stop_reason == "error" and result.error_message == "Invalid provider response"
    assert SECRET not in repr(result) and response.closed


@pytest.mark.parametrize("cls", PROVIDERS)
async def test_empty_stop_tuple_explicitly_clears_stopping(cls):
    p, transport, response = provider(cls)
    stream = p.stream(MODEL, context(), StreamOptions(stop=()))
    assert (await stream.result()).stop_reason == "stop"
    payload = transport.requests[0].payload
    assert payload["stop" if cls is OpenAIProvider else "stop_sequences"] == []
    await stream.aclose()
    assert response.closed
