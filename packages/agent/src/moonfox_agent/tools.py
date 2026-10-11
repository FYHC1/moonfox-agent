"""Minimal sequential tool execution. Implementations are supplied by the caller."""

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass

from pydantic import ValidationError

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    now_ms,
)

from .types import (
    AgentContext,
    AgentTool,
    AgentToolResult,
    EventSink,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
)


class ToolExecutionError(Exception):
    """An expected operational tool failure, reported to the model as an error result."""


@dataclass(slots=True)
class ToolBatch:
    messages: list[ToolResultMessage]
    terminate: bool
    failure: Exception | asyncio.CancelledError | None = None


def error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(message)], is_error=True)


async def _execute(
    tool: AgentTool, call: ToolCall, signal: AbortSignal | None, emit: EventSink
) -> AgentToolResult:
    arguments = tool.parameters.model_validate(deepcopy(call.arguments), strict=True)
    accepting = True

    async def update(result: AgentToolResult) -> None:
        if accepting:
            if not isinstance(result, AgentToolResult):
                raise TypeError("Tool updates must be AgentToolResult")
            await emit(ToolExecutionUpdate(call.id, call.name, deepcopy(result)))

    try:
        result = await tool.execute(call.id, arguments, signal, update)
        if not isinstance(result, AgentToolResult):
            raise TypeError(f"Tool {tool.name} must return AgentToolResult")
        return deepcopy(result)
    finally:
        accepting = False


async def execute_tool_calls(
    context: AgentContext,
    message: AssistantMessage,
    signal: AbortSignal | None,
    emit: EventSink,
) -> ToolBatch:
    """Pair EVERY emitted call, even on truncation, abort, validation failure or bug.

    Operational failures are results. Programmer errors and external task cancellation
    are returned in failure so the loop can commit all pairings before re-raising them.
    A running tool settles cooperatively; abort does not claim to undo side effects.
    """
    calls = [block for block in message.content if isinstance(block, ToolCall)]
    if len({call.id for call in calls}) != len(calls) or any(not call.id for call in calls):
        raise ValueError("Tool call IDs must be nonempty and unique within a response")
    tools = {tool.name: tool for tool in context.tools}
    results: list[ToolResultMessage] = []
    terminate: list[bool] = []
    failure: Exception | asyncio.CancelledError | None = None
    for call in calls:
        await emit(ToolExecutionStart(call.id, call.name, deepcopy(call.arguments)))
        started = time.perf_counter()
        result: AgentToolResult
        if failure is not None:
            result = error_result("Not executed: an earlier tool failed unexpectedly")
        elif message.stop_reason == "length":
            result = error_result(
                "Not executed: tool arguments may be truncated; re-issue the call"
            )
        elif message.stop_reason in ("error", "aborted", "deferred"):
            result = error_result(
                f"Not executed: assistant response ended with {message.stop_reason}"
            )
        elif signal is not None and signal.aborted:
            result = error_result("Not executed: operation aborted")
        elif call.name not in tools:
            result = error_result(f"Unknown tool: {call.name}")
        else:
            try:
                result = await _execute(tools[call.name], call, signal, emit)
            except (ValidationError, ToolExecutionError, OSError) as exc:
                result = error_result(str(exc))
            except asyncio.CancelledError as exc:
                failure = exc
                result = error_result(
                    "Tool execution cancelled; side-effect outcome may be unknown"
                )
            except Exception as exc:
                failure = exc
                result = error_result(
                    "Tool implementation failed; side-effect outcome may be unknown"
                )
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        await emit(
            ToolExecutionEnd(call.id, call.name, deepcopy(result), result.is_error, duration_ms)
        )
        results.append(
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=deepcopy(result.content),
                is_error=result.is_error,
                timestamp=now_ms(),
                details=deepcopy(result.details),
                structured_content=deepcopy(result.structured_content),
                usage=deepcopy(result.usage),
                duration_ms=duration_ms,
            )
        )
        terminate.append(result.terminate)
    return ToolBatch(results, bool(terminate) and all(terminate), failure)
