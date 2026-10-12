import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from moonfox_agent.api import AgentTool, AgentToolResult, RunOptions
from moonfox_ai.api import FauxProvider, FauxResponse, Model, TextContent, get_current_system_prompt
from moonfox_app.api import (
    ExtensionRegistry,
    ExtensionResources,
    InMemorySessionStore,
    JsonlSessionStore,
    PermissionDecision,
    SessionBinding,
    create_headless_app,
)
from moonfox_ext_persona import EXTENSION, DialogueExample, PersonaSettings, render_persona

MODEL = Model("offline", "faux", "faux")
BINDING = SessionBinding("persona-test", "private-scope")


def registry(tmp_path, settings=None, binding=BINDING):
    resources = ExtensionResources(binding, "agent", tmp_path)
    result = ExtensionRegistry()
    plugin = EXTENSION.factory(resources, (settings or PersonaSettings()).model_dump_json())
    result.register(EXTENSION.manifest, lambda: plugin)
    return result, plugin


async def create(tmp_path, faux, *, settings=None, store=None, tools=(), permission_checker=None):
    registered, plugin = registry(tmp_path, settings)
    app = await create_headless_app(
        store=store or InMemorySessionStore(),
        binding=BINDING,
        model=MODEL,
        stream_fn=faux.stream,
        system_prompt="base",
        tools=tools,
        permission_checker=permission_checker,
        registry=registered,
        enabled_extensions=("persona",),
        grants={"persona": frozenset(EXTENSION.manifest.capabilities)},
    )
    return app, plugin


@pytest.mark.parametrize("mode", ["roleplay", "honest", "deflect"])
def test_modes_versions_and_examples_are_static(mode):
    settings = PersonaSettings(
        honesty_mode=mode,
        examples=(DialogueExample(user="你好", assistant="晚上好"),),
    )
    rendered = render_persona(settings)
    assert mode in rendered
    assert settings.content_hash in rendered
    assert "仅为风格示例" in rendered
    assert "不得伪造工具成功" in rendered
    assert "scope_id" not in rendered
    assert PersonaSettings().honesty_mode == "roleplay"
    assert settings.content_hash != PersonaSettings(name="另一个名字").content_hash


@pytest.mark.parametrize(
    "data",
    [
        {"name": " "},
        {"identity": ""},
        {"style": "\n"},
        {"honesty_mode": "lie"},
        {"version": 2},
        {"revision": 0},
        {"revision": "2"},
        {"domain": "qq"},
        {"profile_id": "../escape"},
        {"examples": [{"user": "x", "assistant": "y", "tool": "z"}]},
    ],
)
def test_configuration_is_strict(data):
    with pytest.raises(ValidationError):
        PersonaSettings.model_validate_json(json.dumps(data))


def test_configuration_and_examples_cannot_be_mutated():
    settings = PersonaSettings(examples=(DialogueExample(user="x", assistant="y"),))
    with pytest.raises(ValidationError):
        settings.honesty_mode = "honest"
    with pytest.raises(ValidationError):
        settings.examples[0].user = "changed"


async def test_first_request_repetition_and_disable(tmp_path):
    faux = FauxProvider([FauxResponse.text("ok")] * 3)
    app, plugin = await create(tmp_path, faux)
    async with app:
        for _ in range(2):
            await app.prompt("hi", options=RunOptions(max_turns_per_run=1))
        for _, request, _ in faux.requests:
            prompt = get_current_system_prompt(request.messages)
            assert prompt.startswith("base")
            assert prompt.count("人设：月狐") == 1
        original = app.view()
        with pytest.raises(RuntimeError):
            await plugin.sections(
                replace(original, binding=SessionBinding("other", "private-scope"))
            )
        await app.disable_extension("persona")
        assert app.active_extensions == ()
        assert get_current_system_prompt(app.view().messages) == "base"
        with pytest.raises(RuntimeError):
            await plugin.sections(original)
        await app.prompt("still works", options=RunOptions(max_turns_per_run=1))
        assert get_current_system_prompt(faux.requests[-1][1].messages) == "base"
    await plugin.dispose()


async def test_tool_failure_is_not_rewritten_and_next_turn_has_one_section(tmp_path):
    from pydantic import BaseModel

    class Args(BaseModel):
        pass

    async def execute(*args):
        return AgentToolResult([TextContent("actual failure")], is_error=True)

    async def allow(request):
        return PermissionDecision(True)

    tool = AgentTool("demo.failure", "Fail", "Fail", Args, execute=execute)
    faux = FauxProvider(
        [
            FauxResponse.tool_call("demo.failure", {}, call_id="call-1"),
            FauxResponse.text("reported failure"),
        ]
    )
    app, _ = await create(tmp_path, faux, tools=(tool,), permission_checker=allow)
    async with app:
        outcome = await app.prompt("test", options=RunOptions(max_turns_per_run=2))
        assert outcome.reason == "completed"
        for _, request, _ in faux.requests:
            assert get_current_system_prompt(request.messages).count("人设：月狐") == 1
        results = [m for m in app.view().messages if m.role == "toolResult"]
        assert results[0].is_error and results[0].content[0].text == "actual failure"


async def test_restore_replace_and_metadata_replay(tmp_path):
    path = tmp_path / "session.jsonl"
    app, _ = await create(
        tmp_path,
        FauxProvider([FauxResponse.text("one")]),
        store=JsonlSessionStore(path, create=True),
    )
    async with app:
        await app.prompt("hello", options=RunOptions(max_turns_per_run=1))
    second = PersonaSettings(name="星狐", revision=2)
    faux = FauxProvider([FauxResponse.text("two")])
    app, _ = await create(tmp_path, faux, settings=second, store=JsonlSessionStore(path))
    async with app:
        await app.prompt("again", options=RunOptions(max_turns_per_run=1))
        prompt = get_current_system_prompt(faux.requests[0][1].messages)
        assert "人设：星狐" in prompt and "人设：月狐" not in prompt
        assert second.content_hash in prompt
    text = path.read_text("utf-8")
    assert "persona.profile" in text and PersonaSettings().content_hash in text
    assert second.content_hash in text


async def test_binding_mismatch_rolls_back_and_disposes(tmp_path):
    registered, plugin = registry(tmp_path, binding=SessionBinding("other", "private-scope"))
    with pytest.raises(ValueError, match="binding"):
        await create_headless_app(
            store=InMemorySessionStore(),
            binding=BINDING,
            model=MODEL,
            stream_fn=FauxProvider([]).stream,
            registry=registered,
            enabled_extensions=("persona",),
            grants={"persona": frozenset(EXTENSION.manifest.capabilities)},
        )
    assert not plugin._active
