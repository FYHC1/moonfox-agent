"""Run with only the two installed core packages; no path injection or network."""

import asyncio

from pydantic import BaseModel, ConfigDict

from moonfox_agent.api import Agent, AgentInitialState, AgentTool, AgentToolResult, RunOptions
from moonfox_ai.api import AssistantMessage, FauxProvider, FauxResponse, Model, TextContent


class AddArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: int
    right: int


async def add(call_id, args: AddArguments, signal, on_update) -> AgentToolResult:
    value = args.left + args.right
    return AgentToolResult(content=[TextContent(str(value))], structured_content={"value": value})


async def main() -> None:
    faux = FauxProvider(
        [
            FauxResponse.tool_call("add", {"left": 2, "right": 3}),
            FauxResponse.text("2 + 3 = 5"),
        ]
    )
    agent = Agent(
        initial_state=AgentInitialState(
            model=Model(id="scripted", provider="faux", api="faux"),
            system_prompt="Use the supplied tools to answer the question.",
            tools=[AgentTool("add", "Add two integers", "Add", AddArguments, add)],
        ),
        stream_fn=faux.stream,
    )
    outcome = await agent.prompt("What is 2 + 3?", options=RunOptions(max_turns_per_run=2))
    answer = agent.state.messages[-1]
    assert isinstance(answer, AssistantMessage)
    assert outcome.reason == "completed" and outcome.turns_used == 2
    assert len(faux.requests) == 2
    assert faux.requests[1][1].messages[-1].role == "toolResult"
    print("".join(block.text for block in answer.content if isinstance(block, TextContent)))
    print(f"reason={outcome.reason}; turns={outcome.turns_used}; requests={len(faux.requests)}")


if __name__ == "__main__":
    asyncio.run(main())
