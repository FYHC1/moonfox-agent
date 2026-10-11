from copy import deepcopy
from types import SimpleNamespace

import pytest

from moonfox_ai.api import (
    Context,
    SystemMessage,
    TextContent,
    ToolDeclaration,
    UserMessage,
    collapse_system_messages,
    create_initial_system_message,
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
    get_tool_state_changes,
    normalize_context,
    to_tool_declaration,
)


def tool(name, description="example"):
    return ToolDeclaration(name, description, {"type": "object", "properties": {"value": {}}})


def test_empty_context_stays_empty():
    assert create_initial_system_message(None, None) is None
    assert create_initial_system_message("", []) is None
    assert normalize_context(Context([])).messages == []
    assert get_current_system_message([]) is None
    assert get_current_system_prompt([]) == ""
    assert get_current_tools([]) == []
    assert collapse_system_messages([]) == []


def test_shorthand_is_prepended_and_detached_even_with_existing_system():
    source = Context(
        [SystemMessage("later", 1), UserMessage([TextContent("question")], 2)],
        system_prompt="base",
        tools=[tool("echo")],
    )
    original = deepcopy(source)
    normalized = normalize_context(source)
    assert source == original
    assert get_current_system_prompt(normalized.messages) == "base\n\nlater"
    assert get_current_tools(normalized.messages) == [tool("echo")]
    normalized.messages[2].content[0].text = "changed"
    normalized.messages[0].tools_added[0].parameters["properties"].clear()
    assert source == original


def test_replay_section_replace_delete_and_tool_deltas_without_mutation():
    messages = [
        SystemMessage(
            "base",
            1,
            sections={"a": "old", "b": "remove"},
            tools_added=[tool("first"), tool("second")],
        ),
        UserMessage("question", 2),
        SystemMessage(
            [TextContent("additional")],
            3,
            sections={"a": "new", "b": None, "c": "last"},
            tools_removed=["first", "second"],
            tools_added=[tool("second", "changed")],
        ),
        SimpleNamespace(role="notice", text="not part of replay"),
    ]
    original = deepcopy(messages)
    assert get_current_system_prompt(messages) == "base\n\nadditional\n\nnew\n\nlast"
    assert get_current_tools(messages) == [tool("second", "changed")]
    baseline = get_current_system_message(messages)
    assert baseline.sections == {"a": "new", "c": "last"}
    assert baseline.timestamp == 3
    assert messages == original
    baseline.tools_added[0].parameters.clear()
    assert messages == original


def test_collapse_is_replayable_and_keeps_non_system_order():
    messages = [
        SystemMessage("base", 1, sections={"a": "first"}, tools_added=[tool("x")]),
        UserMessage("one", 2),
        SystemMessage("later", 3, sections={"a": "second"}),
        UserMessage("two", 4),
    ]
    collapsed = collapse_system_messages(messages)
    assert [message.role for message in collapsed] == ["system", "user", "user"]
    assert [message.content for message in collapsed[1:]] == ["one", "two"]
    assert get_current_system_prompt(collapsed) == get_current_system_prompt(messages)
    assert get_current_tools(collapsed) == get_current_tools(messages)
    removed = SystemMessage("", 5, sections={"a": None})
    assert get_current_system_prompt([*collapsed, removed]) == "base\n\nlater"
    assert collapse_system_messages(collapsed) == collapsed


def test_tool_diff_detects_add_remove_and_schema_or_description_changes():
    before = [tool("keep"), tool("remove"), tool("change")]
    after = [tool("keep"), tool("change", "updated"), tool("new")]
    changes = get_tool_state_changes(before, after)
    assert changes.tools_removed == ["remove"]
    assert changes.tools_added == after[1:]
    replayed = [
        SystemMessage("", 1, tools_added=before),
        SystemMessage("", 2, tools_added=changes.tools_added, tools_removed=changes.tools_removed),
    ]
    assert get_current_tools(replayed) == after
    assert get_tool_state_changes(after, after).tools_added == []
    with pytest.raises(ValueError, match="unique"):
        get_tool_state_changes([], [tool("same"), tool("same")])


def test_structural_tool_adapter_does_not_require_agent():
    declaration = tool("external")

    class ExternalTool:
        def declaration(self):
            return declaration

    assert to_tool_declaration(ExternalTool()) == declaration
    assert to_tool_declaration(ExternalTool()) is not declaration


def test_unknown_message_must_cross_explicit_conversion_boundary():
    with pytest.raises(TypeError, match="non-model"):
        normalize_context(Context([SimpleNamespace(role="notice")]))
