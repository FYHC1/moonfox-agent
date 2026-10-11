import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel

from moonfox_agent.api import (
    AbortToken,
    AfterToolCallResult,
    Agent,
    AgentContext,
    AgentInitialState,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentRequestUpdate,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    RunOptions,
    ToolCallLimits,
    agent_loop,
    declare_tool_changes,
    default_convert_to_llm,
    run_tool_call,
    validate_tool_pairing,
)
from moonfox_ai.api import (
    AssistantMessage,
    FauxProvider,
    FauxResponse,
    Model,
    NestedToolCall,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    Usage,
    UserMessage,
    get_current_system_prompt,
    get_current_tools,
)

MODEL = Model("test", "faux", "faux")


class Args(BaseModel):
    value: int


async def execute(call_id, args, signal, update):
    return AgentToolResult([TextContent(str(args.value))], structured_content={"value": args.value})


TOOL = AgentTool("echo", "Echo", "Echo", Args, execute)


def setup(script, tools=(TOOL,), **kwargs):
    provider = FauxProvider(script)
    agent = Agent(
        initial_state=AgentInitialState(MODEL, tools=list(tools)),
        stream_fn=provider.stream,
        **kwargs,
    )
    return agent, provider


def call_message(calls=None):
    return AssistantMessage(
        calls or [ToolCall("a", "echo", {"value": 1})],
        "faux",
        "faux",
        "test",
        Usage(),
        "tool_use",
        0,
    )


def result_message():
    return ToolResultMessage("a", "echo", [TextContent("1")], False, 1)


@pytest.mark.parametrize(
    "kwargs", [{"max_depth": -1}, {"max_depth": True}, {"max_calls": 0}, {"max_calls": 1.5}]
)
def test_invalid_tool_limits(kwargs):
    with pytest.raises(ValueError):
        ToolCallLimits(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"execute": None},
        {"execute_with_context": execute},
        {"execution_mode": "invalid"},
        {"replay": "invalid"},
    ],
)
def test_invalid_tool_definitions(kwargs):
    with pytest.raises(ValueError):
        replace(TOOL, **kwargs)


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"tool_execution": "invalid"}, ValueError),
        ({"options": None}, TypeError),
        ({"tool_limits": None}, TypeError),
    ],
)
def test_invalid_loop_configuration(kwargs, error):
    values = {"model": MODEL, "options": RunOptions(1), **kwargs}
    with pytest.raises(error):
        AgentLoopConfig(**values)


def test_invalid_agent_configuration_and_duplicate_tools():
    with pytest.raises(ValueError, match="execution mode"):
        setup([], tool_execution="invalid")
    with pytest.raises(ValueError, match="unique"):
        setup([], tools=[TOOL, TOOL])


@pytest.mark.parametrize(
    "bad_result", [None, AgentToolResult([object()]), AgentToolResult([], is_error="yes")]
)
async def test_invalid_tool_results_propagate_after_pairing(bad_result):
    async def invalid(*args):
        return bad_result

    agent, provider = setup(
        [FauxResponse.tool_call("echo", {"value": 1})], [replace(TOOL, execute=invalid)]
    )
    with pytest.raises(TypeError):
        await agent.prompt("run", options=RunOptions(2))
    assert agent.state.messages[-1].role == "toolResult"
    assert agent.state.messages[-1].is_error and len(provider.requests) == 1


@pytest.mark.parametrize(
    "hook_value", [False, AfterToolCallResult(is_error="yes"), AfterToolCallResult(terminate=1)]
)
async def test_invalid_after_hook_return_is_not_silently_accepted(hook_value):
    async def after(ctx, signal):
        return hook_value

    agent, _ = setup([FauxResponse.tool_call("echo", {"value": 1})], after_tool_call=after)
    with pytest.raises(TypeError):
        await agent.prompt("run", options=RunOptions(2))
    assert agent.state.messages[-1].is_error


async def test_after_hook_operational_error_is_reported_and_omitted_fields_are_preserved():
    async def after(ctx, signal):
        if ctx.tool_call.id == "a":
            raise OSError("transform unavailable")
        return AfterToolCallResult(details={"note": 1})

    agent, _ = setup(
        [
            FauxResponse(
                [ToolCall("a", "echo", {"value": 1}), ToolCall("b", "echo", {"value": 2})],
                stop_reason="tool_use",
            ),
            FauxResponse.text("done"),
        ],
        after_tool_call=after,
    )
    await agent.prompt("run", options=RunOptions(2))
    results = [m for m in agent.state.messages if m.role == "toolResult"]
    assert results[0].is_error and "unavailable" in results[0].content[0].text
    assert results[1].structured_content == {"value": 2} and results[1].details == {"note": 1}


async def test_preparation_value_error_is_validation_failure():
    def prepare(args):
        raise ValueError("bad encoding")

    agent, _ = setup(
        [FauxResponse.tool_call("echo", {"value": 1}), FauxResponse.text("handled")],
        [replace(TOOL, prepare_arguments=prepare)],
    )
    await agent.prompt("run", options=RunOptions(2))
    result = next(m for m in agent.state.messages if m.role == "toolResult")
    assert result.is_error and "bad encoding" in result.content[0].text


async def test_invalid_before_hook_flags_fail_closed():
    async def before(ctx, signal):
        return BeforeToolCallResult(block="false")

    agent, _ = setup([FauxResponse.tool_call("echo", {"value": 1})], before_tool_call=before)
    with pytest.raises(TypeError, match="flags"):
        await agent.prompt("run", options=RunOptions(2))
    assert agent.state.messages[-1].is_error


async def test_abort_during_preflight_pairs_entire_batch():
    checked = []

    async def before(ctx, signal):
        checked.append(ctx.tool_call.id)
        agent.abort()

    agent, provider = setup(
        [
            FauxResponse(
                [ToolCall("a", "echo", {"value": 1}), ToolCall("b", "echo", {"value": 2})],
                stop_reason="tool_use",
            )
        ],
        before_tool_call=before,
    )
    result = await agent.prompt("run", options=RunOptions(2))
    assert result.reason == "aborted" and checked == ["a"]
    assert all(m.is_error for m in agent.state.messages if m.role == "toolResult")
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "hook, value",
    [
        ("finish_turn", "invalid"),
        ("prepare_request", False),
        ("prepare_request", AgentRequestUpdate(context=False)),
        ("prepare_request", AgentRequestUpdate(context=AgentContext([], [TOOL, TOOL]))),
        ("prepare_next_turn", AgentRequestUpdate()),
    ],
)
async def test_invalid_hook_updates_release_run_without_extra_request(hook, value):
    async def invalid(*args):
        return value

    agent, provider = setup([FauxResponse.tool_call("echo", {"value": 1})], **{hook: invalid})
    with pytest.raises((TypeError, ValueError)):
        await agent.prompt("run", options=RunOptions(2))
    assert len(provider.requests) == (0 if hook == "prepare_request" else 1)
    assert not agent.state.is_streaming


async def test_turn_snapshots_preserve_callable_tool_identity():
    class Service:
        def __init__(self):
            self.calls = []

        def __deepcopy__(self, memo):
            raise TypeError("injected service must not be cloned")

        async def execute(self, call_id, args, signal, update):
            self.calls.append(call_id)
            return AgentToolResult([])

    service = Service()
    live_tool = replace(TOOL, execute=service.execute)

    async def finish(turn, signal):
        assert turn.context.tools[0] is live_tool

    async def next_turn(turn):
        assert turn.context.tools[0] is live_tool
        return AgentLoopTurnUpdate(context=turn.context)

    agent, _ = setup(
        [FauxResponse.tool_call("echo", {"value": 1}), FauxResponse.text("done")],
        [live_tool],
        finish_turn=finish,
        prepare_next_turn=next_turn,
    )
    await agent.prompt("run", options=RunOptions(2))
    assert service.calls == ["call_1"]


def test_default_codec_strips_audit_only_data_without_changing_transcript():
    message = result_message()
    message.nested_calls = [NestedToolCall(ToolCall("a/1", "echo", {"value": 2}), result_message())]
    message.details = {"internal": 1}
    message.structured_content = {"value": 1}
    converted = default_convert_to_llm([message])[0]
    assert converted.nested_calls is None and converted.details is None
    assert converted.structured_content is None and message.nested_calls is not None
    assert message.details == {"internal": 1}


@pytest.mark.parametrize(
    "messages",
    [
        [result_message()],
        [call_message()],
        [call_message(), UserMessage("too early", 0)],
        [call_message(), replace(result_message(), tool_name="wrong")],
        [call_message(), result_message(), result_message()],
        [call_message([ToolCall("a", "echo", {}), ToolCall("a", "echo", {})])],
        [call_message([ToolCall("", "echo", {})])],
    ],
)
def test_invalid_tool_pairing(messages):
    with pytest.raises(ValueError):
        validate_tool_pairing(messages)


def test_tool_declaration_delta_overrides_pending_intent_without_mutating_inputs():
    old = ToolDeclaration("removed", "old", {})
    pending = SystemMessage("new instructions", 2, sections={"test": "keep"}, tools_added=[old])
    context = AgentContext([SystemMessage("base", 1, tools_added=[old])], [TOOL])
    declared = declare_tool_changes(context, [pending, UserMessage("run", 3)])
    assert pending.tools_added == [old]
    assert get_current_tools([*context.messages, *declared]) == [TOOL.declaration()]
    assert (
        get_current_system_prompt([*context.messages, *declared])
        == "base\n\nnew instructions\n\nkeep"
    )
    validate_tool_pairing([call_message(), SystemMessage("safe boundary", 2), result_message()])


async def test_low_level_run_freezes_budget_despite_caller_configuration_change():
    context = AgentContext([])
    config = AgentLoopConfig(MODEL, RunOptions(1))

    async def finish(turn, signal):
        config.options = RunOptions(100)
        return "continue"

    config.finish_turn = finish
    provider = FauxProvider([FauxResponse.text("one"), FauxResponse.text("unused")])

    async def emit(event):
        pass

    result = await agent_loop([UserMessage("run", 0)], context, config, emit, None, provider.stream)
    assert result.reason == "limit_reached" and len(provider.requests) == 1


async def test_agents_have_independent_queues_context_and_budgets():
    a, pa = setup([FauxResponse.text("a"), FauxResponse.text("a-follow")], tools=[])
    b, pb = setup([FauxResponse.text("b")], tools=[])
    a.follow_up(UserMessage("only-a", 1))
    ra, rb = await asyncio.gather(
        a.prompt("a-input", options=RunOptions(2)), b.prompt("b-input", options=RunOptions(1))
    )
    assert (ra.turns_used, rb.turns_used) == (2, 1)
    assert len(pa.requests) == 2 and len(pb.requests) == 1
    assert [m.content for m in b.state.messages if m.role == "user"] == ["b-input"]


async def test_standalone_validation_and_errors():
    context = AgentContext([], [TOOL])
    call = ToolCall("a", "echo", {"value": 1})
    with pytest.raises(ValueError, match="unique"):
        await run_tool_call(
            call, tools=[TOOL, TOOL], assistant_message=call_message(), context=context
        )
    with pytest.raises(ValueError, match="empty"):
        await run_tool_call(
            replace(call, id=""), tools=[TOOL], assistant_message=call_message(), context=context
        )

    async def bad(*args):
        raise TypeError("standalone bug")

    with pytest.raises(TypeError, match="standalone bug"):
        await run_tool_call(
            call,
            tools=[replace(TOOL, execute=bad)],
            assistant_message=call_message(),
            context=context,
        )


async def test_prompt_validation_empty_reset_and_idle_calls():
    agent, _ = setup([], tools=[])
    agent.abort()
    await agent.wait_for_idle()
    agent.reset()
    assert agent.state.messages == []
    with pytest.raises(ValueError, match="at least one"):
        await agent.prompt([], options=RunOptions(1))
    with pytest.raises(TypeError, match="RunOptions"):
        await agent.prompt("input", options=None)
    with pytest.raises(ValueError, match="unique"):
        agent.set_tools([TOOL, TOOL])
    agent.steer(UserMessage("clear", 0))
    agent.follow_up(UserMessage("clear", 0))
    agent.clear_steering_queue()
    assert agent.has_queued_messages()
    agent.clear_follow_up_queue()
    assert not agent.has_queued_messages()


async def test_preaborted_standalone_call_is_not_executed():
    token = AbortToken()
    token.abort()
    outcome = await run_tool_call(
        ToolCall("a", "echo", {"value": 1}),
        tools=[TOOL],
        assistant_message=call_message(),
        context=AgentContext([]),
        signal=token,
    )
    assert outcome.is_error and "aborted" in outcome.result.content[0].text
