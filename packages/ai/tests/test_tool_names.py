from copy import deepcopy

import pytest

from moonfox_ai.providers._tool_names import normalize_tool_names


@pytest.mark.parametrize("anthropic", [False, True])
def test_namespaced_unicode_and_reserved_tool_names_are_stable_unique_and_replayable(anthropic):
    names = ["simple", "test.echo", "test_echo", "插件.工具", "x" * 100, "mfx_reserved"]
    definitions = [{"name": name} if anthropic else {"function": {"name": name}} for name in names]
    calls = [
        {"type": "tool_use", "name": name} if anthropic else {"function": {"name": name}}
        for name in names
    ]
    history = {"role": "assistant", "content" if anthropic else "tool_calls": calls}
    payload = {"tools": definitions, "messages": [history]}
    original = deepcopy(payload)
    reverse = normalize_tool_names(payload, anthropic=anthropic)
    aliases = [item["name"] if anthropic else item["function"]["name"] for item in definitions]
    assert len(set(aliases)) == len(names)
    assert aliases[0] == "simple" and aliases[2] == "test_echo"
    assert all(
        len(alias) <= 64 and all(c.isascii() and (c.isalnum() or c in "_-") for c in alias)
        for alias in aliases
    )
    assert [reverse[alias] for alias in aliases] == names
    assert [item["name"] if anthropic else item["function"]["name"] for item in calls] == aliases
    del original["tools"]
    assert normalize_tool_names(original, anthropic=anthropic) == reverse
    assert original["messages"] == payload["messages"]
