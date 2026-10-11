"""Offline stage B demo: checked nested tools, follow-up, budget stop and explicit resume."""

import asyncio

from pydantic import BaseModel

from moonfox_agent.api import (
    Agent,
    AgentInitialState,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    RunOptions,
    ToolExecutionContext,
)
from moonfox_ai.api import (
    FauxProvider,
    FauxResponse,
    Model,
    TextContent,
    ToolResultMessage,
    UserMessage,
)


class AddArguments(BaseModel):
    left: int
    right: int


async def add(call_id, args: AddArguments, signal, update) -> AgentToolResult:
    return AgentToolResult([TextContent(str(args.left + args.right))])


async def calculate(call_id, args: AddArguments, context: ToolExecutionContext) -> AgentToolResult:
    child = await context.execute_tool("add", args.model_dump())
    return child.result


async def main() -> None:
    checks = []

    async def before(ctx, signal):
        checks.append((ctx.tool_call.name, ctx.depth))
        return BeforeToolCallResult(block=False)

    provider = FauxProvider(
        [
            FauxResponse.tool_call("calculate", {"left": 2, "right": 3}, call_id="calc"),
            FauxResponse.text("The result is 5."),
            FauxResponse.text("Follow-up completed."),
        ]
    )
    agent = Agent(
        initial_state=AgentInitialState(
            model=Model("scripted", "faux", "faux"),
            tools=[
                AgentTool("add", "Add integers", "Add", AddArguments, add),
                AgentTool(
                    "calculate",
                    "Calculate using checked tools",
                    "Calculate",
                    AddArguments,
                    execute_with_context=calculate,
                ),
            ],
        ),
        stream_fn=provider.stream,
        before_tool_call=before,
    )
    agent.follow_up(UserMessage("Explain briefly", 0))
    first = await agent.prompt("What is 2 + 3?", options=RunOptions(2))
    assert first.reason == "limit_reached" and agent.has_queued_messages()
    result = next(
        message for message in agent.state.messages if isinstance(message, ToolResultMessage)
    )
    assert result.nested_calls[0].tool_call.id == "calc/1"
    assert checks == [("calculate", 0), ("add", 1)]
    second = await agent.continue_(options=RunOptions(1))
    assert second.reason == "completed" and not agent.has_queued_messages()
    print(
        f"first={first.reason}; turns={first.turns_used}; "
        f"nested={result.nested_calls[0].tool_call.id}"
    )
    print(f"second={second.reason}; turns={second.turns_used}; requests={len(provider.requests)}")


if __name__ == "__main__":
    asyncio.run(main())
