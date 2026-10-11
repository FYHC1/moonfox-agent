import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from moonfox_agent.api import (
    Agent,
    AgentContext,
    AgentInitialState,
    AgentLoopTurnUpdate,
    AgentRequestUpdate,
    AgentTool,
    AgentToolResult,
    RunOptions,
    default_convert_to_llm,
)
from moonfox_ai.api import (
    FauxProvider,
    FauxResponse,
    Model,
    StreamOptions,
    SystemMessage,
    TextContent,
    UserMessage,
    get_current_system_prompt,
    get_current_tools,
)

MODEL = Model("test", "faux", "faux")


class Args(BaseModel):
    value: int


async def echo(call_id, args, signal, update):
    return AgentToolResult([TextContent(str(args.value))])


TOOL = AgentTool("echo", "Echo", "Echo", Args, echo)


def setup(script, tools=(), **kwargs):
    provider = FauxProvider(script)
    agent = Agent(
        initial_state=AgentInitialState(MODEL, "base", list(tools)),
        stream_fn=provider.stream,
        **kwargs,
    )
    return agent, provider


def inputs(request):
    return [m.content for m in request[1].messages if m.role == "user"]


@pytest.mark.parametrize(
    "mode, expected",
    [
        (
            "one_at_a_time",
            [["initial", "s1"], ["initial", "s1", "s2"], ["initial", "s1", "s2", "f1"]],
        ),
        ("all", [["initial", "s1", "s2"], ["initial", "s1", "s2", "f1"]]),
    ],
)
async def test_steering_precedes_follow_up_and_respects_mode(mode, expected):
    agent, provider = setup([FauxResponse.text("ok") for _ in expected], steering_mode=mode)
    agent.steer(UserMessage("s1", 1))
    agent.steer(UserMessage("s2", 2))
    agent.follow_up(UserMessage("f1", 3))
    assert [m.content for m in agent.peek_queued_messages()] == (
        ["s1"] if mode == "one_at_a_time" else ["s1", "s2"]
    )
    outcome = await agent.prompt("initial", options=RunOptions(len(expected)))
    assert outcome.reason == "completed"
    assert [inputs(request) for request in provider.requests] == expected
    assert not agent.has_queued_messages()


async def test_follow_up_waits_for_tool_loop_to_finish():
    agent, provider = setup(
        [
            FauxResponse.tool_call("echo", {"value": 1}),
            FauxResponse.tool_call("echo", {"value": 2}),
            FauxResponse.text("done"),
            FauxResponse.text("followed"),
        ],
        [TOOL],
    )
    agent.follow_up(UserMessage("later", 1))
    outcome = await agent.prompt("initial", options=RunOptions(4))
    assert outcome.reason == "completed"
    assert [inputs(request) for request in provider.requests] == [["initial"]] * 3 + [
        ["initial", "later"]
    ]


async def test_steering_arriving_during_tool_or_slow_prepare_is_delivered_once():
    entered, release = asyncio.Event(), asyncio.Event()
    prepared = []

    async def next_turn(turn):
        prepared.append(turn.turn)
        if turn.turn == 1:
            entered.set()
            await release.wait()

    agent, provider = setup(
        [
            FauxResponse.tool_call("echo", {"value": 1}),
            FauxResponse.text("second"),
            FauxResponse.text("third"),
        ],
        [TOOL],
        prepare_next_turn=next_turn,
    )
    task = asyncio.create_task(agent.prompt("initial", options=RunOptions(3)))
    await asyncio.wait_for(entered.wait(), 1)
    agent.steer(UserMessage("s1", 1))
    agent.steer(UserMessage("s2", 2))
    release.set()
    assert (await task).reason == "completed"
    assert [inputs(request) for request in provider.requests] == [
        ["initial"],
        ["initial", "s1"],
        ["initial", "s1", "s2"],
    ]
    assert prepared == [1, 2]


@pytest.mark.parametrize("delivery", ["steer", "follow_up"])
async def test_budget_preserves_unconsumed_queued_input(delivery):
    agent, provider = setup([FauxResponse.text("first"), FauxResponse.text("next")])

    def queue_after_response(event, token):
        if event.type == "turn_end" and len(provider.requests) == 1:
            getattr(agent, delivery)(UserMessage("pending", 1))

    agent.subscribe(queue_after_response)
    first = await agent.prompt("initial", options=RunOptions(1))
    assert first.reason == "limit_reached" and first.turns_used == 1
    assert [m.content for m in agent.peek_queued_messages()] == ["pending"]
    assert len(provider.requests) == 1
    second = await agent.continue_(options=RunOptions(1))
    assert second.reason == "completed" and second.turns_used == 1
    assert inputs(provider.requests[1]) == ["initial", "pending"]
    assert not agent.has_queued_messages()


async def test_finish_end_does_not_poll_queue_or_prepare_another_request():
    next_calls = []

    async def finish(turn, signal):
        agent.follow_up(UserMessage("not consumed", 1))
        return "end"

    async def next_turn(turn):
        next_calls.append(turn)

    agent, provider = setup(
        [FauxResponse.text("done")], finish_turn=finish, prepare_next_turn=next_turn
    )
    result = await agent.prompt("initial", options=RunOptions(5))
    assert result.reason == "completed" and len(provider.requests) == 1
    assert next_calls == [] and agent.has_queued_messages()


@pytest.mark.parametrize("natural", ["none", "tool", "steering", "follow_up"])
async def test_finish_continue_is_satisfied_by_exactly_one_next_request(natural):
    async def finish(turn, signal):
        if turn.turn == 1:
            if natural == "steering":
                agent.steer(UserMessage("extra", 1))
            elif natural == "follow_up":
                agent.follow_up(UserMessage("extra", 1))
            return "continue"
        return None

    first = (
        FauxResponse.tool_call("echo", {"value": 1})
        if natural == "tool"
        else FauxResponse.text("first")
    )
    agent, provider = setup([first, FauxResponse.text("second")], [TOOL], finish_turn=finish)
    outcome = await agent.prompt("initial", options=RunOptions(5))
    assert outcome.reason == "completed" and outcome.turns_used == 2
    assert len(provider.requests) == 2


async def test_unconditional_continue_stays_within_one_budget():
    async def finish(turn, signal):
        return "continue"

    agent, provider = setup([FauxResponse.text("again")] * 3, finish_turn=finish)
    outcome = await agent.prompt("initial", options=RunOptions(2))
    assert outcome.reason == "limit_reached" and outcome.turns_used == 2
    assert provider.remaining == 1


@pytest.mark.parametrize("reason", ["error", "aborted"])
async def test_hard_outcomes_ignore_finish_continue_and_preserve_queues(reason):
    calls = []

    async def finish(turn, signal):
        calls.append(turn.message.stop_reason)
        agent.follow_up(UserMessage("later", 1))
        return "continue"

    agent, provider = setup([FauxResponse.error("failure", stop_reason=reason)], finish_turn=finish)
    outcome = await agent.prompt("initial", options=RunOptions(5))
    assert outcome.reason == reason and calls == [reason]
    assert len(provider.requests) == 1 and agent.has_queued_messages()


async def test_prepare_request_first_turn_updates_persist_only_within_run():
    replacement = Model("other", "faux-other", "faux")
    seen, keys = [], []

    async def prepare(request, signal):
        seen.append((request.model.id, request.thinking_level))
        if len(seen) == 1:
            projected = request.context
            projected.messages.insert(1, UserMessage("reference", 1))
            return AgentRequestUpdate(projected, replacement, "high")

    async def get_key(provider):
        keys.append(provider)
        return f"key-for-{provider}"

    agent, provider = setup(
        [
            FauxResponse.tool_call("echo", {"value": 1}),
            FauxResponse.text("done"),
            FauxResponse.text("new run"),
        ],
        [TOOL],
        prepare_request=prepare,
        get_api_key=get_key,
        stream_options=StreamOptions(temperature=0.25, headers={"x-test": "value"}),
    )
    await agent.prompt("initial", options=RunOptions(2))
    await agent.prompt("another", options=RunOptions(1))
    assert seen == [("test", "off"), ("other", "high"), ("test", "off")]
    assert keys == ["faux-other", "faux-other", "faux"]
    assert [request[0].id for request in provider.requests] == ["other", "other", "test"]
    assert inputs(provider.requests[1]) == ["reference", "initial"]
    assert inputs(provider.requests[2]) == ["initial", "another"]
    assert provider.requests[0][2].temperature == 0.25
    assert provider.requests[0][2].reasoning == "high"
    assert provider.requests[0][2].api_key == "key-for-faux-other"
    assert agent.state.model == MODEL


async def test_canonical_projection_rebuild_does_not_accumulate_context():
    counts = []

    async def prepare(request, signal):
        counts.append(sum(m.role == "user" for m in request.canonical_messages))
        context = AgentContext(
            [*request.canonical_messages, UserMessage("reference", 1)], request.context.tools
        )
        return AgentRequestUpdate(context=context)

    agent, provider = setup(
        [
            FauxResponse.tool_call("echo", {"value": 1}),
            FauxResponse.text("done"),
        ],
        [TOOL],
        prepare_request=prepare,
    )
    await agent.prompt("initial", options=RunOptions(2))
    assert counts == [1, 1]
    assert [inputs(request).count("reference") for request in provider.requests] == [1, 1]
    assert [m.content for m in agent.state.messages if m.role == "user"] == ["initial"]


async def test_prepare_next_turn_can_change_tools_and_append_drafts_without_mutating_transcript():
    async def next_turn(turn):
        context = turn.context
        context.tools = []
        return AgentLoopTurnUpdate(context=context, messages=[SystemMessage("extra", 2)])

    agent, provider = setup(
        [FauxResponse.tool_call("echo", {"value": 1}), FauxResponse.text("done")],
        [TOOL],
        prepare_next_turn=next_turn,
    )
    await agent.prompt("initial", options=RunOptions(2))
    assert get_current_tools(provider.requests[0][1].messages) == [TOOL.declaration()]
    assert get_current_tools(provider.requests[1][1].messages) == []
    assert get_current_system_prompt(agent.state.messages) == "base\n\nextra"
    assert (
        len(agent.state.tools) == 1
    )  # per-run hook change does not change the next run's defaults


async def test_hook_exception_stops_without_draining_next_queue_and_releases_agent():
    async def next_turn(turn):
        raise TypeError("hook bug")

    agent, provider = setup([FauxResponse.text("first")], prepare_next_turn=next_turn)
    agent.follow_up(UserMessage("pending", 1))
    with pytest.raises(TypeError, match="hook bug"):
        await agent.prompt("initial", options=RunOptions(2))
    assert len(provider.requests) == 1 and agent.has_queued_messages()
    assert not agent.state.is_streaming


async def test_continue_rejects_unrunnable_context_without_provider_call():
    agent, provider = setup([FauxResponse.text("unused")])
    with pytest.raises(ValueError, match="Cannot continue"):
        await agent.continue_(options=RunOptions(1))
    assert not provider.requests and not agent.state.is_streaming
    agent.replace_messages([UserMessage("ready", 1)])
    assert (await agent.continue_(options=RunOptions(1))).reason == "completed"
    with pytest.raises(ValueError, match="Cannot continue"):
        await agent.continue_(options=RunOptions(1))
    assert len(provider.requests) == 1


async def test_continue_converts_custom_role_once_and_validates_converted_role():
    @dataclass
    class Custom:
        role: str = "custom"
        timestamp: int = 0

    calls = []

    async def convert(messages):
        calls.append(True)
        return default_convert_to_llm(
            [UserMessage("converted", 0) if m.role == "custom" else m for m in messages]
        )

    agent, provider = setup([FauxResponse.text("done")], convert_to_llm=convert)
    agent.replace_messages([Custom()])
    await agent.continue_(options=RunOptions(1))
    assert calls == [True] and inputs(provider.requests[0]) == ["converted"]


async def test_runtime_queue_mode_change_and_reset_preserve_current_system_baseline():
    agent, provider = setup([FauxResponse.text("one"), FauxResponse.text("two")], [TOOL])
    agent.follow_up(UserMessage("f1", 1))
    agent.follow_up(UserMessage("f2", 2))
    agent.follow_up_mode = "all"
    agent.steering_mode = "all"
    assert agent.follow_up_mode == "all" and agent.steering_mode == "all"
    await agent.prompt(
        [SystemMessage("", 1, sections={"section": "current"}), UserMessage("initial", 2)],
        options=RunOptions(2),
    )
    assert inputs(provider.requests[-1]) == ["initial", "f1", "f2"]
    agent.steer(UserMessage("clear", 3))
    agent.follow_up(UserMessage("clear", 4))
    agent.reset()
    assert not agent.has_queued_messages()
    assert len(agent.state.messages) == 1
    assert agent.state.system_prompt == "base\n\ncurrent"
    assert get_current_tools(agent.state.messages) == [TOOL.declaration()]
