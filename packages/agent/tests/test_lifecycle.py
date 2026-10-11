import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel

from moonfox_agent.api import (
    Agent,
    AgentInitialState,
    AgentTool,
    AgentToolResult,
    RunOptions,
)
from moonfox_ai.api import (
    FauxProvider,
    FauxResponse,
    ImageContent,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

MODEL = Model("test", "faux", "faux")


class Args(BaseModel):
    value: int


def setup(script, tools=(), **kwargs):
    provider = FauxProvider(script)
    agent = Agent(
        initial_state=AgentInitialState(MODEL, tools=list(tools)),
        stream_fn=provider.stream,
        **kwargs,
    )
    events = []
    agent.subscribe(lambda event, token: events.append(event))
    return agent, provider, events


def results(agent):
    return [m for m in agent.state.messages if isinstance(m, ToolResultMessage)]


async def test_abort_before_first_request_does_not_consume_queue_or_budget():
    agent, provider, events = setup([FauxResponse.text("unused")])
    agent.steer(UserMessage("pending", 1))
    agent.subscribe(lambda event, token: agent.abort() if event.type == "agent_start" else None)
    outcome = await agent.prompt("run", options=RunOptions(1))
    assert outcome.reason == "aborted" and outcome.turns_used == 0
    assert not provider.requests and agent.has_queued_messages()
    assert events[-1].type == "agent_end"


@pytest.mark.parametrize("boundary", ["prepare_request", "prepare_next_turn"])
async def test_abort_during_hook_prevents_next_request_and_preserves_pending_queue(boundary):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(*args):
        entered.set()
        await release.wait()

    agent, provider, _ = setup(
        [FauxResponse.text("first"), FauxResponse.text("unused")], **{boundary: hook}
    )
    agent.follow_up(UserMessage("pending", 1))
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(2)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        agent.abort()
    finally:
        release.set()
    assert (await run).reason == "aborted"
    assert len(provider.requests) == (0 if boundary == "prepare_request" else 1)
    assert agent.has_queued_messages()


@pytest.mark.parametrize("cancellation", ["token", "task", "twice"])
async def test_parallel_cancel_waits_for_every_started_tool(cancellation):
    ready, release = asyncio.Event(), asyncio.Event()
    started, settled = [], []
    baseline = asyncio.all_tasks()

    async def execute(call_id, args, signal, update):
        started.append(call_id)
        if len(started) == 2:
            ready.set()
        try:
            await release.wait()
            return AgentToolResult([TextContent("effect completed")])
        finally:
            settled.append(call_id)

    tool = AgentTool("work", "work", "work", Args, execute)
    agent, provider, events = setup(
        [
            FauxResponse(
                [ToolCall("a", "work", {"value": 1}), ToolCall("b", "work", {"value": 2})],
                stop_reason="tool_use",
            )
        ],
        [tool],
    )
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(2)))
    idle = None
    try:
        await asyncio.wait_for(ready.wait(), 1)
        if cancellation == "token":
            agent.abort()
        else:
            run.cancel()
        idle = asyncio.create_task(agent.wait_for_idle())
        await asyncio.sleep(0)
        if cancellation == "twice":
            run.cancel()
            await asyncio.sleep(0)
        assert agent.state.is_streaming and not idle.done() and not settled
        with pytest.raises(RuntimeError, match="already processing"):
            agent.reset()
        with pytest.raises(RuntimeError, match="already processing"):
            agent.set_tools([])
    finally:
        release.set()
    if cancellation == "token":
        assert (await run).reason == "aborted"
    else:
        with pytest.raises(asyncio.CancelledError):
            await run
    if idle is not None:
        await idle
    assert sorted(settled) == ["a", "b"]
    assert [m.tool_call_id for m in results(agent)] == ["a", "b"]
    assert all(not m.is_error for m in results(agent))
    assert events[-1].outcome.reason == "aborted" and len(provider.requests) == 1
    assert not agent.state.is_streaming and asyncio.all_tasks() == baseline


async def test_parallel_bug_signals_and_joins_siblings_before_propagating():
    sibling_started = asyncio.Event()
    sibling_settled = []

    async def execute(call_id, args, signal, update):
        if call_id == "bad":
            await sibling_started.wait()
            raise TypeError("tool bug")
        sibling_started.set()
        await signal.wait()
        sibling_settled.append(True)
        return AgentToolResult([TextContent("stopped")], is_error=True)

    tool = AgentTool("work", "work", "work", Args, execute)
    agent, _, events = setup(
        [
            FauxResponse(
                [ToolCall("bad", "work", {"value": 1}), ToolCall("sibling", "work", {"value": 2})],
                stop_reason="tool_use",
            )
        ],
        [tool],
    )
    async with asyncio.timeout(2):
        with pytest.raises(TypeError, match="tool bug"):
            await agent.prompt("run", options=RunOptions(2))
    assert sibling_settled == [True] and len(results(agent)) == 2
    assert events[-1].outcome.reason == "error"


async def test_external_task_cancel_closes_provider_and_pairs_message_events():
    started = asyncio.Event()
    baseline = asyncio.all_tasks()
    agent, provider, events = setup([FauxResponse.text("delayed", delay_ms=60_000)])

    def observe(event, token):
        if event.type == "message_start" and event.message.role == "assistant":
            started.set()

    agent.subscribe(observe)
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(2)))
    await asyncio.wait_for(started.wait(), 1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert len(provider.requests) == 1
    assert events[-1].outcome.reason == "aborted"
    starts = [event.message_id for event in events if event.type == "message_start"]
    ends = [event.message_id for event in events if event.type == "message_end"]
    assert starts == ends
    assert asyncio.all_tasks() == baseline


async def test_listener_failure_still_pairs_tools_and_finalizes_all_listeners():
    seen = []
    executed = []

    async def execute(*args):
        executed.append(True)
        return AgentToolResult([])

    agent, _, events = setup(
        [FauxResponse.tool_call("work", {"value": 1})],
        [AgentTool("work", "work", "work", Args, execute)],
    )

    def fail(event, token):
        if event.type == "message_end" and event.message.role == "assistant":
            raise TypeError("observer bug")

    agent.subscribe(fail)
    agent.subscribe(lambda event, token: seen.append(event.type))
    with pytest.raises(TypeError, match="observer bug"):
        await agent.prompt("run", options=RunOptions(2))
    assert not executed and results(agent)[0].is_error
    assert seen[-1] == events[-1].type == "agent_end"
    assert seen.count("agent_end") == 1 and not agent.state.is_streaming


async def test_observers_cannot_mutate_other_observers_or_provider_result():
    agent, _, _ = setup([FauxResponse.text("original", chunk_size=1)])
    seen = []

    def mutate(event, token):
        if event.type == "message_update":
            event.message.content.clear()
            event.assistant_message_event.partial.content.clear()
        if event.type == "message_end" and event.message.role == "assistant":
            event.message.content.clear()

    def observe(event, token):
        if event.type == "message_end" and event.message.role == "assistant":
            seen.extend(event.message.content)

    agent.subscribe(mutate)
    agent.subscribe(observe)
    await agent.prompt("run", options=RunOptions(1))
    assert seen == [TextContent("original")]
    assert agent.state.messages[-1].content == seen


async def test_child_tasks_cannot_wait_for_their_own_run():
    async def execute(*args):
        with pytest.raises(RuntimeError, match="own tasks"):
            await asyncio.create_task(agent.wait_for_idle())
        return AgentToolResult([])

    agent, _, _ = setup(
        [FauxResponse.tool_call("work", {"value": 1}), FauxResponse.text("done")],
        [AgentTool("work", "work", "work", Args, execute)],
    )
    async with asyncio.timeout(2):
        await agent.prompt("run", options=RunOptions(2))


async def test_started_nested_call_is_joined_even_when_parent_returns_early():
    entered, release, parent_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children = []

    async def child(*args):
        entered.set()
        await release.wait()
        return AgentToolResult([TextContent("child result")])

    async def parent(call_id, args, context):
        children.append(asyncio.create_task(context.execute_tool("child", {"value": 1})))
        await entered.wait()
        parent_returned.set()
        return AgentToolResult([TextContent("parent result")])

    tools = [
        AgentTool("parent", "parent", "parent", Args, execute_with_context=parent),
        AgentTool("child", "child", "child", Args, child),
    ]
    agent, _, events = setup(
        [FauxResponse.tool_call("parent", {"value": 1}), FauxResponse.text("done")], tools
    )
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(2)))
    try:
        await asyncio.wait_for(parent_returned.wait(), 1)
        await asyncio.sleep(0)
        assert not any(event.type == "tool_execution_end" for event in events)
        assert agent.state.is_streaming
    finally:
        release.set()
    await run
    await asyncio.gather(*children)
    assert results(agent)[0].nested_calls[0].result.content == [TextContent("child result")]


async def test_started_update_is_joined_before_tool_end():
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    updates = []

    async def execute(call_id, args, signal, update):
        updates.append(asyncio.create_task(update(AgentToolResult([TextContent("working")]))))
        await entered.wait()
        returned.set()
        return AgentToolResult([], terminate=True)

    agent, _, events = setup(
        [FauxResponse.tool_call("work", {"value": 1})],
        [AgentTool("work", "work", "work", Args, execute)],
    )

    async def observe(event, token):
        if event.type == "tool_execution_update":
            entered.set()
            await release.wait()

    agent.subscribe(observe)
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(1)))
    try:
        await asyncio.wait_for(returned.wait(), 1)
        assert not any(event.type == "tool_execution_end" for event in events)
    finally:
        release.set()
    await run
    await asyncio.gather(*updates)
    assert [e.type for e in events].index("tool_execution_update") < [e.type for e in events].index(
        "tool_execution_end"
    )


async def test_slow_listener_overflow_recovers_final_response_and_tool_pairs():
    gate, release = asyncio.Event(), asyncio.Event()
    captured = []
    response = FauxResponse(
        [TextContent("x" * 800), ToolCall("a", "work", {"value": 1})],
        stop_reason="tool_use",
        chunk_size=1,
    )
    provider = FauxProvider([response, FauxResponse.text("done")])

    def stream(model, context, options):
        result = provider.stream(model, context, options)
        captured.append(result)
        return result

    async def execute(*args):
        return AgentToolResult([TextContent("ok")])

    agent = Agent(
        initial_state=AgentInitialState(
            MODEL, tools=[AgentTool("work", "work", "work", Args, execute)]
        ),
        stream_fn=stream,
    )

    async def slow(event, token):
        if (
            event.type == "message_start"
            and event.message.role == "assistant"
            and len(captured) == 1
        ):
            gate.set()
            await release.wait()

    agent.subscribe(slow)
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(2)))
    try:
        await asyncio.wait_for(gate.wait(), 1)
        await asyncio.wait_for(captured[0].result(), 2)
    finally:
        release.set()
    assert (await run).reason == "completed"
    assert len(results(agent)) == 1 and not results(agent)[0].is_error
    first = next(m for m in agent.state.messages if m.role == "assistant")
    assert {"type": "stream_overflow"} in first.diagnostics


async def test_restore_reset_model_tools_and_image_prompt_use_public_apis():
    provider = FauxProvider([FauxResponse.text("done")])
    history = [SystemMessage("restored", 1, sections={"test": "section"}), UserMessage("old", 2)]
    agent = Agent(
        initial_state=AgentInitialState(MODEL, "must not duplicate", messages=history),
        stream_fn=provider.stream,
    )
    assert agent.state.system_prompt == "restored\n\nsection"
    agent.set_model(replace(MODEL, id="changed"))
    agent.set_thinking_level("high")
    agent.set_tools([])
    agent.reset()
    image = ImageContent("AA==", "image/png")
    await agent.prompt("image", images=[image], options=RunOptions(1))
    assert provider.requests[0][0].id == "changed"
    assert provider.requests[0][2].reasoning == "high"
    assert provider.requests[0][1].messages[-1].content == [TextContent("image"), image]
    with pytest.raises(ValueError, match="string prompt"):
        await agent.prompt(UserMessage("input", 0), images=[image], options=RunOptions(1))


async def test_repeated_cancel_during_provider_cleanup_still_joins_resources():
    from moonfox_ai.api import AssistantMessage, AssistantMessageEventStream, Start, Usage

    cleanup_entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowClose(AssistantMessageEventStream):
        async def aclose(self):
            cleanup_entered.set()
            await release.wait()
            await super().aclose()

    def stream(model, context, options):
        result = SlowClose()

        async def produce():
            result.push(Start(AssistantMessage([], "faux", "faux", "test", Usage(), "pending", 0)))
            await asyncio.Event().wait()

        result.start(produce())
        return result

    agent = Agent(initial_state=AgentInitialState(MODEL), stream_fn=stream)
    agent.subscribe(lambda event, token: started.set() if event.type == "turn_start" else None)
    baseline = asyncio.all_tasks()
    run = asyncio.create_task(agent.prompt("run", options=RunOptions(1)))
    try:
        await asyncio.wait_for(started.wait(), 1)
        run.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        run.cancel()
        await asyncio.sleep(0)
        assert agent.state.is_streaming and not run.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert not agent.state.is_streaming and asyncio.all_tasks() == baseline


@pytest.mark.parametrize("ids", [["same", "same"], [""]])
async def test_invalid_provider_call_ids_are_diagnosed_without_poisoning_replay(ids):
    from moonfox_agent.api import default_convert_to_llm, validate_tool_pairing
    from moonfox_ai.api import StreamProtocolError

    agent, _, events = setup(
        [
            FauxResponse(
                [ToolCall(identity, "unused", {}) for identity in ids], stop_reason="tool_use"
            )
        ]
    )
    with pytest.raises(StreamProtocolError, match="invalid tool IDs"):
        await agent.prompt("run", options=RunOptions(1))
    final = agent.state.messages[-1]
    assert final.stop_reason == "error"
    assert final.content == []
    assert final.diagnostics[-1]["type"] == "invalid_tool_calls"
    assert len(final.diagnostics[-1]["calls"]) == len(ids)
    validate_tool_pairing(default_convert_to_llm(agent.state.messages))
    assert events[-1].outcome.reason == "error"
