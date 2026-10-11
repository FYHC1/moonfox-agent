import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict

from moonfox_agent.api import (
    Agent,
    AgentContext,
    AgentInitialState,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    RunOptions,
    ToolExecutionError,
    agent_loop,
    default_convert_to_llm,
)
from moonfox_ai.api import (
    AssistantMessageEventStream,
    FauxProvider,
    FauxResponse,
    FauxScriptExhausted,
    IncompleteStreamError,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    get_current_tools,
)

MODEL = Model(id="test", provider="faux", api="faux")


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


async def echo(call_id, args, signal, on_update):
    return AgentToolResult([TextContent(str(args.value))], structured_content={"value": args.value})


def make_tool(execute=echo):
    return AgentTool("echo", "Echo a number", "Echo", Arguments, execute)


def make_agent(script, tools=(), **kwargs):
    faux = FauxProvider(script)
    agent = Agent(
        initial_state=AgentInitialState(MODEL, system_prompt="base", tools=list(tools)),
        stream_fn=faux.stream,
        **kwargs,
    )
    return agent, faux


def tool_results(agent):
    return [message for message in agent.state.messages if isinstance(message, ToolResultMessage)]


async def test_tool_call_then_next_response_and_lifecycle_ids():
    agent, faux = make_agent(
        [
            FauxResponse.tool_call("echo", {"value": 7}, chunk_size=100),
            FauxResponse.text("done", chunk_size=100),
        ],
        [make_tool()],
    )
    events = []
    agent.subscribe(lambda event, token: events.append(event))
    outcome = await agent.prompt("echo 7", options=RunOptions(2))
    assert outcome.reason == "completed" and outcome.turns_used == 2
    assert len(faux.requests) == 2
    assert get_current_tools(faux.requests[0][1].messages) == [make_tool().declaration()]
    assert len(faux.requests[0][1].messages) == 2
    assert faux.requests[1][1].messages[-1].tool_call_id == "call_1"
    assert faux.requests[1][1].messages[-1].content == [TextContent("7")]
    assert [event.type for event in events] == [
        "agent_start",
        "message_start",
        "message_end",
        "turn_start",
        "message_start",
        "message_update",
        "message_update",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "message_start",
        "message_end",
        "turn_end",
        "turn_start",
        "message_start",
        "message_update",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    starts = {event.message_id for event in events if event.type == "message_start"}
    ends = [event.message_id for event in events if event.type == "message_end"]
    assert len(starts) == len(ends) == 4 and starts == set(ends)
    assert all(event.message_id in starts for event in events if event.type == "message_update")
    assert events[-1].outcome == outcome
    assert not agent.state.is_streaming
    assert agent.state.pending_tool_calls == frozenset()


async def test_budget_stops_before_next_call_but_commits_tool_results():
    agent, faux = make_agent(
        [FauxResponse.tool_call("echo", {"value": 7}), FauxResponse.text("not reached")],
        [make_tool()],
    )
    outcome = await agent.prompt("echo", options=RunOptions(1))
    assert outcome.reason == "limit_reached" and outcome.turns_used == 1
    assert len(faux.requests) == 1 and faux.remaining == 1
    assert len(tool_results(agent)) == 1 and not tool_results(agent)[0].is_error
    assert agent.state.messages[-1].role == "toolResult"


@pytest.mark.parametrize(
    "response, reason",
    [
        (FauxResponse.text("done"), "completed"),
        (FauxResponse.error("failure"), "error"),
        (FauxResponse.error("stopped", stop_reason="aborted"), "aborted"),
    ],
)
async def test_single_turn_outcomes(response, reason):
    agent, faux = make_agent([response])
    outcome = await agent.prompt("question", options=RunOptions(1))
    assert outcome.reason == reason and outcome.turns_used == 1
    assert len(faux.requests) == 1
    assert agent.state.error_message == response.error_message


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("unknown", {"value": 1}),
        ("echo", {"value": "bad"}),
        ("echo", {"value": 1, "extra": 2}),
    ],
)
async def test_unknown_tools_and_invalid_arguments_are_error_results(name, arguments):
    executed = []

    async def forbidden(*args):
        executed.append(True)
        return AgentToolResult([])

    agent, faux = make_agent(
        [FauxResponse.tool_call(name, arguments), FauxResponse.text("handled")],
        [make_tool(forbidden)],
    )
    assert (await agent.prompt("test", options=RunOptions(2))).reason == "completed"
    assert tool_results(agent)[0].is_error
    assert not executed and len(faux.requests) == 2


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), ToolExecutionError("failed")])
async def test_operational_tool_failures_are_encoded(error):
    async def fail(*args):
        raise error

    agent, _ = make_agent(
        [FauxResponse.tool_call("echo", {"value": 1}), FauxResponse.text("handled")],
        [make_tool(fail)],
    )
    await agent.prompt("test", options=RunOptions(2))
    assert tool_results(agent)[0].is_error
    assert str(error) in tool_results(agent)[0].content[0].text


async def test_tool_bug_propagates_after_pairing_entire_batch():
    executed = []

    async def buggy(*args):
        executed.append(True)
        raise TypeError("implementation bug")

    response = FauxResponse(
        [ToolCall("a", "echo", {"value": 1}), ToolCall("b", "echo", {"value": 2})],
        stop_reason="tool_use",
    )
    agent, faux = make_agent([response], [make_tool(buggy)])
    with pytest.raises(TypeError, match="implementation bug"):
        await agent.prompt("test", options=RunOptions(2))
    assert [result.tool_call_id for result in tool_results(agent)] == ["a", "b"]
    assert all(result.is_error for result in tool_results(agent))
    assert executed == [True] and len(faux.requests) == 1
    assert not agent.state.is_streaming


@pytest.mark.parametrize("reason", ["length", "error", "aborted", "deferred"])
async def test_nonexecutable_responses_pair_calls_without_executing(reason):
    executed = []

    async def forbidden(*args):
        executed.append(True)
        return AgentToolResult([])

    agent, faux = make_agent(
        [
            FauxResponse.tool_call("echo", {"value": 1}, stop_reason=reason),
            FauxResponse.text("handled"),
        ],
        [make_tool(forbidden)],
    )
    outcome = await agent.prompt("test", options=RunOptions(2))
    assert not executed and tool_results(agent)[0].is_error
    assert len(faux.requests) == (2 if reason == "length" else 1)
    assert outcome.reason == ({"error": "error", "aborted": "aborted"}.get(reason, "completed"))


async def test_explicit_tool_error_and_terminate():
    async def stop(*args):
        return AgentToolResult([TextContent("failed")], is_error=True, terminate=True)

    agent, faux = make_agent([FauxResponse.tool_call("echo", {"value": 1})], [make_tool(stop)])
    outcome = await agent.prompt("test", options=RunOptions(2))
    assert outcome.reason == "completed" and outcome.turns_used == 1
    assert tool_results(agent)[0].is_error and len(faux.requests) == 1


async def test_abort_during_tool_pairs_remaining_calls_and_uses_fresh_token_next_run():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    tokens = []

    async def slow(call_id, args, signal, on_update):
        calls.append(call_id)
        entered.set()
        await release.wait()
        return AgentToolResult([TextContent("effect completed")])

    agent, faux = make_agent(
        [
            FauxResponse(
                [ToolCall("first", "echo", {"value": 1}), ToolCall("second", "echo", {"value": 2})],
                stop_reason="tool_use",
            ),
            FauxResponse.text("next run"),
        ],
        [make_tool(slow)],
    )
    agent.subscribe(
        lambda event, token: tokens.append(token) if event.type == "agent_start" else None
    )
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(2)))
    await asyncio.wait_for(entered.wait(), 1)
    agent.abort()
    idle = asyncio.create_task(agent.wait_for_idle())
    await asyncio.sleep(0)
    assert not idle.done()
    with pytest.raises(RuntimeError, match="already processing"):
        await agent.prompt("overlap", options=RunOptions(1))
    release.set()
    assert (await run).reason == "aborted"
    await idle
    assert calls == ["first"]
    assert [result.is_error for result in tool_results(agent)] == [False, True]
    assert len(faux.requests) == 1
    assert (await agent.prompt("again", options=RunOptions(1))).reason == "completed"
    assert tokens[0] is not tokens[1] and tokens[0].aborted and not tokens[1].aborted


async def test_abort_during_stream_settles_without_background_tasks():
    baseline = asyncio.all_tasks()
    agent, _ = make_agent([FauxResponse.text("abcdef", chunk_size=1)])

    def abort_on_delta(event, token):
        if event.type == "message_update" and event.assistant_message_event.type == "text_delta":
            agent.abort()

    agent.subscribe(abort_on_delta)
    result = await agent.prompt("run", options=RunOptions(2))
    assert result.reason == "aborted" and result.turns_used == 1
    assert agent.state.messages[-1].content[0].text == "a"
    assert asyncio.all_tasks() == baseline


async def test_idle_waits_for_agent_end_listeners_and_dispatch_is_snapshot_based():
    entered, release = asyncio.Event(), asyncio.Event()
    order = []
    agent, _ = make_agent([FauxResponse.text("done")])

    def first(event, token):
        if event.type == "agent_start":
            unsubscribe()
            order.append("first")

    def second(event, token):
        if event.type == "agent_start":
            order.append("second")

    async def last(event, token):
        if event.type == "agent_end":
            entered.set()
            await release.wait()

    agent.subscribe(first)
    unsubscribe = agent.subscribe(second)
    agent.subscribe(last)
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(1)))
    await asyncio.wait_for(entered.wait(), 1)
    idle = asyncio.create_task(agent.wait_for_idle())
    await asyncio.sleep(0)
    assert agent.state.is_streaming and not idle.done()
    assert order == ["first", "second"]
    release.set()
    await run
    await idle
    assert not agent.state.is_streaming


async def test_first_and_subsequent_requests_use_unique_codec_without_accumulating_context():
    @dataclass
    class Notice:
        text: str
        role: str = "notice"
        timestamp: int = 0

    converted_lengths = []

    async def transform(messages, signal):
        messages.append(Notice("temporary"))
        return messages

    async def convert(messages):
        converted_lengths.append(len([item for item in messages if item.role == "notice"]))
        return default_convert_to_llm([item for item in messages if item.role != "notice"])

    agent, faux = make_agent(
        [FauxResponse.tool_call("echo", {"value": 1}), FauxResponse.text("done")],
        [make_tool()],
        transform_context=transform,
        convert_to_llm=convert,
    )
    await agent.prompt("run", options=RunOptions(2))
    assert converted_lengths == [1, 1] and len(faux.requests) == 2
    assert all(message.role != "notice" for message in agent.state.messages)


async def test_state_and_message_update_snapshots_are_detached():
    agent, _ = make_agent([FauxResponse.text("abcd", chunk_size=1)])
    updates = []
    agent.subscribe(
        lambda event, token: updates.append(event) if event.type == "message_update" else None
    )
    await agent.prompt("run", options=RunOptions(1))
    text_updates = [
        event for event in updates if event.assistant_message_event.type == "text_delta"
    ]
    assert [event.message.content[0].text for event in text_updates] == ["a", "ab", "abc", "abcd"]
    view = agent.state
    assert view.system_prompt == "base"
    view.messages[0].content = "mutated"
    assert agent.state.system_prompt == "base"


async def test_tool_updates_are_awaited_and_late_updates_are_ignored():
    saved = []
    events = []

    async def updating(call_id, args, signal, on_update):
        saved.append(on_update)
        await on_update(AgentToolResult([TextContent("working")]))
        return AgentToolResult([TextContent("done")], terminate=True)

    agent, _ = make_agent([FauxResponse.tool_call("echo", {"value": 1})], [make_tool(updating)])
    agent.subscribe(lambda event, token: events.append(event.type))
    await agent.prompt("run", options=RunOptions(1))
    assert events.index("tool_execution_update") < events.index("tool_execution_end")
    before = list(events)
    await saved[0](AgentToolResult([TextContent("late")]))
    assert events == before


async def test_script_exhaustion_and_incomplete_provider_propagate_but_release_run():
    agent, _ = make_agent([])
    with pytest.raises(FauxScriptExhausted):
        await agent.prompt("run", options=RunOptions(1))
    assert not agent.state.is_streaming

    def incomplete(model, context, options):
        stream = AssistantMessageEventStream()
        stream.end()
        return stream

    agent = Agent(initial_state=AgentInitialState(MODEL), stream_fn=incomplete)
    with pytest.raises(IncompleteStreamError):
        await agent.prompt("run", options=RunOptions(1))
    assert not agent.state.is_streaming


async def test_low_level_loop_is_independent_of_stateful_facade():
    context = AgentContext([SystemMessage("base", 0)], [make_tool()])
    faux = FauxProvider([FauxResponse.tool_call("echo", {"value": 3}), FauxResponse.text("done")])
    events = []

    async def emit(event):
        events.append(event)

    result = await agent_loop(
        [UserMessage("echo", 1)],
        context,
        AgentLoopConfig(MODEL, RunOptions(2)),
        emit,
        None,
        faux.stream,
    )
    assert result.reason == "completed"
    assert context.messages[-1].role == "assistant" and events[-1].type == "agent_end"
    assert get_current_tools(context.messages) == [make_tool().declaration()]
