"""Public projections must not copy internal message/event payloads into the browser."""

import json
from dataclasses import dataclass

import pytest

from moonfox_agent.api import (
    AgentStart,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    ToolExecutionStart,
)
from moonfox_ai.api import (
    AssistantMessage,
    ImageContent,
    NestedToolCall,
    SystemMessage,
    TextContent,
    TextDelta,
    ThinkingContent,
    ThinkingDelta,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from moonfox_app.server.schema import MessageView
from moonfox_app.server.wire import project_agent_event, project_message

SECRET = "internal-secret-must-not-appear"


def assistant(*, error=False):
    return AssistantMessage(
        content=[
            TextContent("公开"),
            ThinkingContent(SECRET),
            TextContent("文本"),
            ToolCall("call-1", "echo", {"secret": SECRET}, thought_signature=SECRET),
        ],
        api=SECRET,
        provider=SECRET,
        model=SECRET,
        usage=Usage(),
        stop_reason="error" if error else "stop",
        timestamp=123,
        error_message=SECRET if error else None,
        response_id=SECRET,
        diagnostics=[{"secret": SECRET}],
    )


def test_message_projection_is_a_strict_allowlist():
    view = project_message("m-1", assistant())
    assert MessageView.model_validate(view).text == "公开文本"
    assert view["tool_calls"] == [{"id": "call-1", "name": "echo"}]
    assert view["id"] == "m-1" and view["role"] == "assistant"
    assert SECRET not in json.dumps(view)
    assert not {"partial", "diagnostics", "reasoning", "provider", "arguments"} & view.keys()
    image = project_message(
        "m-user", UserMessage([TextContent("图片："), ImageContent(SECRET, "image/png")], 456)
    )
    assert image["text"] == "图片：[图片]" and SECRET not in json.dumps(image)
    assert project_message("plain", UserMessage("普通文本", 1))["text"] == "普通文本"


def test_system_and_custom_roles_are_never_projected():
    @dataclass
    class Custom:
        role: str = "system"
        timestamp: int = 1
        content: str = SECRET

    for message in (SystemMessage(SECRET, 1), Custom(), Custom(role="user")):
        assert project_message("hidden", message) is None
        assert project_agent_event(MessageStart(message, "hidden")) is None
        assert project_agent_event(MessageEnd(message, "hidden")) is None


def test_tool_results_expose_only_display_text_not_audit_or_structured_data():
    nested = ToolResultMessage("nested", "private", [TextContent(SECRET)], False, 1)
    result = ToolResultMessage(
        "call-1",
        "echo",
        [TextContent("可见结果")],
        False,
        10,
        details={"secret": SECRET},
        structured_content={"secret": SECRET},
        nested_calls=[NestedToolCall(ToolCall("nested", "private", {"secret": SECRET}), nested)],
    )
    view = project_message("tool-1", result)
    assert view["text"] == "可见结果" and view["tool_name"] == "echo"
    assert SECRET not in json.dumps(view)
    assert not {"details", "structured_content", "nested_calls", "usage"} & view.keys()
    result.is_error = True
    result.content = [TextContent(SECRET)]
    error = project_message("tool-1", result)
    assert error["is_error"] and error["status"] == "error" and SECRET not in json.dumps(error)


def test_raw_assistant_error_is_replaced_with_public_generic_text():
    message = assistant(error=True)
    message.content = [TextContent(SECRET)]
    view = project_message("failed", message)
    assert view["is_error"] and view["status"] == "error"
    assert view["text"] and SECRET not in json.dumps(view)


def test_stable_message_identity_and_text_delta_without_partial():
    message = assistant()
    start = project_agent_event(MessageStart(message, "stable"))
    update = project_agent_event(MessageUpdate(message, TextDelta(0, "你好", message), "stable"))
    end = project_agent_event(MessageEnd(message, "stable"))
    assert start[0] == "message_start" and start[1]["message"]["status"] == "streaming"
    assert end[0] == "message_end" and end[1]["message"]["status"] == "complete"
    assert start[1]["message"]["id"] == end[1]["message"]["id"] == "stable"
    assert update == ("text_delta", {"message_id": "stable", "delta": "你好"})
    assert SECRET not in json.dumps((start, update, end))
    assert "partial" not in json.dumps(update)


@pytest.mark.parametrize(
    "event",
    [
        AgentStart(),
        ToolExecutionStart("call", "echo", {"x": SECRET}),
        MessageUpdate(assistant(), ThinkingDelta(0, SECRET, assistant()), "stable"),
    ],
)
def test_non_display_events_are_ignored(event):
    assert project_agent_event(event) is None
