from dataclasses import replace

import pytest

from moonfox_ai.api import (
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    Usage,
    UserMessage,
    calculate_context_tokens,
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
    is_context_overflow,
    is_recoverable_length,
)


def assistant(reason="stop", usage=None, error=None):
    return AssistantMessage(
        [TextContent("answer")],
        "faux",
        "faux",
        "test",
        usage or Usage(),
        reason,
        1,
        error_message=error,
    )


def test_usage_fallback_does_not_count_reasoning_twice():
    usage = Usage(input=10, output=20, cache_read=3, cache_write=4, reasoning=5)
    assert usage.effective_total == 37
    assert calculate_context_tokens(usage) == 37
    assert replace(usage, total_tokens=99).effective_total == 99
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("1234567") == 2


def test_hybrid_estimate_uses_last_successful_usage_and_trailing_messages():
    messages = [
        UserMessage("input", 0),
        assistant(usage=Usage(total_tokens=100)),
        ToolResultMessage("id", "tool", [TextContent("result")], False, 2),
        assistant("error", Usage(total_tokens=900), "failed request"),
    ]
    estimate = estimate_context_tokens(messages)
    trailing = sum(estimate_message_tokens(message) for message in messages[2:])
    assert estimate.last_usage_index == 1
    assert estimate.usage_tokens == 100
    assert estimate.trailing_tokens == trailing
    assert estimate.tokens == 100 + trailing
    messages.append(assistant(usage=Usage(input=150, output=10)))
    assert estimate_context_tokens(messages).tokens == 160


def test_fallback_and_structured_content_are_estimated():
    message = assistant()
    message.content = [
        TextContent("text"),
        ThinkingContent("thought"),
        ImageContent("AA==", "image/png"),
        ToolCall("id", "echo", {"value": "data"}),
    ]
    system = SystemMessage(
        "base",
        0,
        sections={"test": "extra"},
        tools_added=[ToolDeclaration("echo", "description", {"type": "object"})],
    )
    messages = [system, message]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None
    assert estimate.tokens == sum(estimate_message_tokens(item) for item in messages)
    assert estimate.tokens > 1000
    assert estimate_message_tokens(system) > estimate_message_tokens(SystemMessage("base", 0))
    assert estimate_context_tokens([]).tokens == 0


@pytest.mark.parametrize(
    "error",
    [
        "prompt is too long: 213462 tokens > 200000 maximum",
        '413 {"error":{"type":"request_too_large"}}',
        "context_length_exceeded",
        "Your input exceeds the context window of this model",
        "Requested token count exceeds the model maximum context length of 131072 tokens",
        "Input length (265330) exceeds model maximum context length (262144).",
        "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)",
        "This model maximum prompt length is 131072 but the request contains 537812 tokens",
        "Please reduce the length of the messages or completion",
        "This endpoint maximum context length is 100 tokens. However, you requested 200 tokens",
        "The input (200 tokens) is longer than the model context length (100 tokens).",
        "the request exceeds the available context size, try increasing it",
    ],
)
def test_known_context_overflow_errors(error):
    assert is_context_overflow(assistant("error", error=error))
    assert not is_context_overflow(assistant("stop", error=error))


@pytest.mark.parametrize(
    "error", ["rate limit exceeded", "invalid API key", "network timeout", None]
)
def test_unrelated_errors_are_not_context_overflow(error):
    assert not is_context_overflow(assistant("error", error=error))


def test_output_limit_is_not_input_overflow():
    assert is_recoverable_length(assistant("length"))
    assert not is_context_overflow(assistant("length"))
    assert not is_recoverable_length(assistant("error"))
