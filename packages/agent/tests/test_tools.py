import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel, ConfigDict

from moonfox_agent.api import (
    AfterToolCallResult,
    Agent,
    AgentContext,
    AgentInitialState,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    RunOptions,
    ToolCallLimits,
    run_tool_call,
)
from moonfox_ai.api import (
    AssistantMessage,
    FauxProvider,
    FauxResponse,
    Model,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)

MODEL = Model("test", "faux", "faux")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


async def echo(call_id, args, signal, update):
    return AgentToolResult(
        [TextContent(str(args.value))], details={"old": 1}, structured_content={"value": args.value}
    )


def tool(name="echo", execute=echo, **kwargs):
    return AgentTool(name, name, name, Args, execute, **kwargs)


def setup(calls, tools, **kwargs):
    provider = FauxProvider(
        [FauxResponse(calls, stop_reason="tool_use"), FauxResponse.text("done")]
    )
    agent = Agent(
        initial_state=AgentInitialState(MODEL, tools=tools), stream_fn=provider.stream, **kwargs
    )
    events = []
    agent.subscribe(lambda event, token: events.append(event))
    return agent, provider, events


def results(agent):
    return [message for message in agent.state.messages if isinstance(message, ToolResultMessage)]


async def test_parallel_preflight_order_completion_order_and_transcript_order():
    first_entered, second_finished = asyncio.Event(), asyncio.Event()
    order = []

    async def before(ctx, signal):
        order.append(f"before:{ctx.tool_call.id}")

    async def execute(call_id, args, signal, update):
        order.append(f"execute:{call_id}")
        if call_id == "a":
            first_entered.set()
            await second_finished.wait()
        else:
            await first_entered.wait()
        return AgentToolResult([TextContent(call_id)])

    agent, provider, events = setup(
        [ToolCall("a", "echo", {"value": 1}), ToolCall("b", "echo", {"value": 2})],
        [tool(execute=execute)],
        before_tool_call=before,
    )

    def release_first(event, token):
        if event.type == "tool_execution_end" and event.tool_call_id == "b":
            second_finished.set()

    agent.subscribe(release_first)
    async with asyncio.timeout(2):
        assert (await agent.prompt("run", options=RunOptions(2))).reason == "completed"
    assert order[:2] == ["before:a", "before:b"]
    assert [event.tool_call_id for event in events if event.type == "tool_execution_end"] == [
        "b",
        "a",
    ]
    assert [message.tool_call_id for message in results(agent)] == ["a", "b"]
    assert [
        message.tool_call_id
        for message in provider.requests[1][1].messages
        if message.role == "toolResult"
    ] == ["a", "b"]


@pytest.mark.parametrize("override", ["global", "tool"])
async def test_any_sequential_declaration_makes_batch_sequential(override):
    order = []

    async def execute(call_id, args, signal, update):
        order.append(f"start:{call_id}")
        await asyncio.sleep(0)
        order.append(f"end:{call_id}")
        return AgentToolResult([])

    tools = [
        tool("first", execute),
        tool("second", execute, execution_mode="sequential" if override == "tool" else None),
    ]
    agent, _, _ = setup(
        [ToolCall("a", "first", {"value": 1}), ToolCall("b", "second", {"value": 2})],
        tools,
        tool_execution="sequential" if override == "global" else "parallel",
    )
    await agent.prompt("run", options=RunOptions(2))
    assert order == ["start:a", "end:a", "start:b", "end:b"]


async def test_preparation_validation_before_and_after_share_detached_arguments():
    seen = []

    def prepare(args):
        args["value"] = int(args["value"])
        return args

    async def before(ctx, signal):
        seen.append(("before", ctx.args.value))
        ctx.args.value = 99
        ctx.tool_call.arguments["value"] = 100

    async def execute(call_id, args, signal, update):
        seen.append(("execute", args.value))
        return await echo(call_id, args, signal, update)

    async def after(ctx, signal):
        seen.append(("after", ctx.args.value))
        return AfterToolCallResult(content=[TextContent("replaced")], details={"new": 2})

    agent, _, _ = setup(
        [ToolCall("a", "echo", {"value": "7"})],
        [tool(execute=execute, prepare_arguments=prepare)],
        before_tool_call=before,
        after_tool_call=after,
    )
    await agent.prompt("run", options=RunOptions(2))
    assert seen == [("before", 7), ("execute", 7), ("after", 7)]
    result = results(agent)[0]
    assert result.content == [TextContent("replaced")]
    assert result.details == {"new": 2} and result.structured_content is None
    call_message = next(m for m in agent.state.messages if m.role == "assistant")
    assert call_message.content[0].arguments == {"value": "7"}


@pytest.mark.parametrize("failure", ["blocked", "operational", "bug", "invalid"])
async def test_permission_fail_closed(failure):
    executed = []

    async def execute(*args):
        executed.append(True)
        return AgentToolResult([])

    async def before(ctx, signal):
        if failure == "blocked":
            return BeforeToolCallResult(block=True, reason="denied")
        if failure == "operational":
            raise PermissionError("denied")
        if failure == "bug":
            raise TypeError("permission bug")
        return False

    agent, _, _ = setup(
        [ToolCall("a", "echo", {"value": 1})], [tool(execute=execute)], before_tool_call=before
    )
    if failure in ("bug", "invalid"):
        with pytest.raises(TypeError):
            await agent.prompt("run", options=RunOptions(2))
    else:
        await agent.prompt("run", options=RunOptions(2))
    assert not executed and results(agent)[0].is_error
    assert not agent.state.is_streaming


async def test_after_hook_cannot_hide_failure_and_replaces_fields_not_merges():
    async def fail(*args):
        return AgentToolResult(
            [TextContent("failed")],
            details={"original": 1},
            structured_content={"before": 1},
            is_error=True,
        )

    async def after(ctx, signal):
        return AfterToolCallResult(
            content=[TextContent("visible")],
            details={"new": 2},
            structured_content={"after": 2},
            is_error=False,
            usage=Usage(input=3),
            terminate=True,
        )

    agent, provider, _ = setup(
        [ToolCall("a", "echo", {"value": 1})], [tool(execute=fail)], after_tool_call=after
    )
    outcome = await agent.prompt("run", options=RunOptions(2))
    result = results(agent)[0]
    assert result.is_error and result.details == {"new": 2}
    assert result.structured_content == {"after": 2} and result.usage.input == 3
    assert outcome.reason == "completed" and len(provider.requests) == 1


async def test_terminate_requires_every_final_result():
    async def execute(call_id, args, signal, update):
        return AgentToolResult([], terminate=call_id == "a")

    agent, provider, _ = setup(
        [ToolCall("a", "echo", {"value": 1}), ToolCall("b", "echo", {"value": 2})],
        [tool(execute=execute)],
    )
    assert (await agent.prompt("run", options=RunOptions(2))).turns_used == 2
    assert len(provider.requests) == 2


async def test_nested_calls_share_hooks_ancestry_and_audit_without_extra_transcript_messages():
    seen, saved = [], []

    async def before(ctx, signal):
        seen.append(("before", ctx.tool_call.id, ctx.parent_call_id, ctx.depth))
        if ctx.tool_call.name == "denied":
            return BeforeToolCallResult(block=True, reason="denied child")

    async def after(ctx, signal):
        seen.append(("after", ctx.tool_call.id, ctx.parent_call_id, ctx.depth))

    async def parent(call_id, args, context):
        saved.append(context)
        child = await context.execute_tool("echo", {"value": 7})
        denied = await context.execute_tool("denied", {"value": 8})
        assert not child.is_error and denied.is_error
        assert context.tool_call_id == call_id and context.depth == 0
        return AgentToolResult(child.result.content)

    tools = [tool("parent", None, execute_with_context=parent), tool(), tool("denied")]
    agent, provider, events = setup(
        [ToolCall("root", "parent", {"value": 1})],
        tools,
        before_tool_call=before,
        after_tool_call=after,
    )
    await agent.prompt("run", options=RunOptions(2))
    result = results(agent)[0]
    assert result.content == [TextContent("7")]
    assert len(results(agent)) == 1
    assert [entry.tool_call.id for entry in result.nested_calls] == ["root/1", "root/2"]
    assert [entry.result.is_error for entry in result.nested_calls] == [False, True]
    assert provider.requests[1][1].messages[-1].nested_calls is None
    assert provider.requests[1][1].messages[-1].structured_content is None
    assert ("before", "root/1", "root", 1) in seen
    assert ("after", "root/1", "root", 1) in seen
    assert ("before", "root/2", "root", 1) in seen
    assert [event.tool_call_id for event in events if event.type == "tool_execution_start"] == [
        "root"
    ]
    assert len([m for m in provider.requests[1][1].messages if m.role == "toolResult"]) == 1
    late = await saved[0].execute_tool("echo", {"value": 9})
    assert late.is_error and "closed" in late.result.content[0].text


@pytest.mark.parametrize(
    "limits, child_count", [(ToolCallLimits(max_depth=0), 1), (ToolCallLimits(max_calls=2), 3)]
)
async def test_nested_limits_are_shared_not_reset_per_child(limits, child_count):
    outcomes = []
    executed = []

    async def child(call_id, args, signal, update):
        executed.append(call_id)
        return AgentToolResult([])

    async def parent(call_id, args, context):
        for _ in range(child_count):
            outcomes.append(await context.execute_tool("child", {"value": 1}))
        return AgentToolResult([])

    agent, _, _ = setup(
        [ToolCall("root", "parent", {"value": 1})],
        [tool("parent", None, execute_with_context=parent), tool("child", child)],
        tool_limits=limits,
    )
    await agent.prompt("run", options=RunOptions(2))
    assert len(executed) == (0 if limits.max_depth == 0 else 1)
    assert outcomes[-1].is_error and "limit" in outcomes[-1].result.content[0].text


async def test_nested_recursive_depth_has_audited_failure():
    async def recursive(call_id, args, context):
        child = await context.execute_tool("recursive", {"value": 1})
        return child.result

    agent, _, _ = setup(
        [ToolCall("root", "recursive", {"value": 1})],
        [tool("recursive", None, execute_with_context=recursive)],
        tool_limits=ToolCallLimits(max_depth=2),
    )
    await agent.prompt("run", options=RunOptions(2))
    result = results(agent)[0]
    assert result.is_error
    child = result.nested_calls[0].result.nested_calls[0].result.nested_calls[0]
    assert child.tool_call.id == "root/1/1/1" and child.result.is_error


async def test_nested_bug_is_not_swallowed_by_parent_success():
    async def buggy(*args):
        raise TypeError("nested bug")

    async def parent(call_id, args, context):
        await context.execute_tool("child", {"value": 1})
        return AgentToolResult([TextContent("parent returned")])

    agent, provider, _ = setup(
        [ToolCall("root", "parent", {"value": 1})],
        [tool("parent", None, execute_with_context=parent), tool("child", buggy)],
    )
    with pytest.raises(TypeError, match="nested bug"):
        await agent.prompt("run", options=RunOptions(2))
    assert results(agent)[0].nested_calls[0].result.is_error
    assert len(provider.requests) == 1 and not agent.state.is_streaming


async def test_standalone_runner_uses_same_checked_path_without_transcript_writes():
    call = ToolCall("a", "echo", {"value": 3})
    assistant = AssistantMessage([call], "faux", "faux", "test", Usage(), "tool_use", 0)
    context = AgentContext([assistant], [tool()])
    seen = []

    async def before(ctx, signal):
        seen.append(ctx.tool_call.id)

    outcome = await run_tool_call(
        call,
        tools=context.tools,
        assistant_message=assistant,
        context=context,
        before_tool_call=before,
    )
    assert outcome.result.content == [TextContent("3")] and seen == ["a"]
    assert context.messages == [assistant]
    blocked = await run_tool_call(
        call,
        tools=context.tools,
        assistant_message=replace(assistant, stop_reason="length"),
        context=context,
    )
    assert blocked.is_error
