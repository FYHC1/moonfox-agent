"""One checked execution path for top-level, parallel and nested tool calls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    ImageContent,
    NestedToolCall,
    TextContent,
    ToolCall,
    ToolResultMessage,
    now_ms,
)

from .abort import AbortToken, CombinedAbortSignal
from .hooks import (
    AfterToolCall,
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCall,
    BeforeToolCallContext,
    BeforeToolCallResult,
)
from .types import (
    AgentContext,
    AgentLoopConfig,
    AgentTool,
    AgentToolCallOutcome,
    AgentToolResult,
    EventSink,
    ToolCallLimits,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    ToolUpdate,
)


class ToolExecutionError(Exception):
    """An operational tool failure, returned as is_error=True rather than retried here."""


@dataclass(slots=True)
class ToolBatch:
    messages: list[ToolResultMessage]
    terminate: bool
    failure: Exception | asyncio.CancelledError | None = None


@dataclass(slots=True)
class _Prepared:
    call: ToolCall
    tool: AgentTool | None = None
    args: BaseModel | None = None
    immediate: AgentToolResult | None = None
    parent_id: str | None = None
    depth: int = 0


def error_result(message: str) -> AgentToolResult:
    return AgentToolResult([TextContent(message)], is_error=True)


def _validate_result(result: AgentToolResult) -> None:
    if not isinstance(result, AgentToolResult):
        raise TypeError("Tools and updates must return AgentToolResult")
    if not isinstance(result.content, list) or any(
        not isinstance(block, (TextContent, ImageContent)) for block in result.content
    ):
        raise TypeError("Tool result content must contain text or image blocks")
    if not isinstance(result.is_error, bool) or not isinstance(result.terminate, bool):
        raise TypeError("Tool result flags must be bool")


def _message(outcome: AgentToolCallOutcome) -> ToolResultMessage:
    result = outcome.result
    return ToolResultMessage(
        tool_call_id=outcome.tool_call.id,
        tool_name=outcome.tool_call.name,
        content=deepcopy(result.content),
        is_error=result.is_error,
        timestamp=now_ms(),
        details=deepcopy(result.details),
        structured_content=deepcopy(result.structured_content),
        usage=deepcopy(result.usage),
        nested_calls=deepcopy(outcome.nested_calls) or None,
        duration_ms=outcome.duration_ms,
    )


@dataclass(slots=True)
class _Engine:
    context: AgentContext
    assistant: AssistantMessage
    signal: AbortSignal | None
    before: BeforeToolCall | None
    after: AfterToolCall | None
    limits: ToolCallLimits
    failure: Exception | asyncio.CancelledError | None = None
    used: int = 0
    stop: AbortToken = field(default_factory=AbortToken)

    def fail(self, error: Exception | asyncio.CancelledError) -> None:
        if self.failure is None:
            self.failure = error
        self.stop.abort()

    def snapshot(self) -> AgentContext:
        return AgentContext(deepcopy(self.context.messages), list(self.context.tools))

    async def prepare(
        self, call: ToolCall, parent_id: str | None = None, depth: int = 0
    ) -> _Prepared:
        prepared = _Prepared(deepcopy(call), parent_id=parent_id, depth=depth)
        try:
            if self.assistant.stop_reason in ("length", "error", "aborted", "deferred"):
                prepared.immediate = error_result(
                    f"Not executed: assistant response ended with {self.assistant.stop_reason}"
                )
                return prepared
            if self.signal is not None and self.signal.aborted:
                prepared.immediate = error_result("Not executed: operation aborted")
                return prepared
            if depth > self.limits.max_depth or self.used >= self.limits.max_calls:
                prepared.immediate = error_result("Not executed: tool call limit reached")
                return prepared
            self.used += 1
            tool = next((tool for tool in self.context.tools if tool.name == call.name), None)
            if tool is None:
                prepared.immediate = error_result(f"Unknown tool: {call.name}")
                return prepared
            arguments: Any = deepcopy(call.arguments)
            if tool.prepare_arguments is not None:
                try:
                    arguments = tool.prepare_arguments(arguments)
                except ValueError as exc:
                    prepared.immediate = error_result(f"Invalid tool arguments: {exc}")
                    return prepared
            prepared.tool = tool
            prepared.args = tool.parameters.model_validate(arguments, strict=True)
            if self.before is not None:
                decision = await self.before(
                    BeforeToolCallContext(
                        deepcopy(self.assistant),
                        deepcopy(call),
                        deepcopy(prepared.args),
                        self.snapshot(),
                        parent_id,
                        depth,
                    ),
                    self.signal,
                )
                if decision is not None and not isinstance(decision, BeforeToolCallResult):
                    raise TypeError("before_tool_call must return BeforeToolCallResult or None")
                if decision is not None and (
                    not isinstance(decision.block, bool) or not isinstance(decision.terminate, bool)
                ):
                    raise TypeError("before_tool_call flags must be bool")
                if decision is not None and decision.block:
                    prepared.immediate = error_result(decision.reason or "Tool execution blocked")
                    prepared.immediate.terminate = decision.terminate
            if self.signal is not None and self.signal.aborted:
                prepared.immediate = error_result("Not executed: operation aborted")
        except (ValidationError, ToolExecutionError, OSError) as exc:
            prepared.immediate = error_result(str(exc))
        except (Exception, asyncio.CancelledError) as exc:
            self.fail(exc)
            prepared.immediate = error_result("Tool preflight failed; execution denied")
        return prepared

    async def finalize(self, prepared: _Prepared, result: AgentToolResult) -> AgentToolResult:
        if self.after is None:
            return result
        try:
            override = await self.after(
                AfterToolCallContext(
                    deepcopy(self.assistant),
                    deepcopy(prepared.call),
                    deepcopy(prepared.args),
                    deepcopy(result),
                    result.is_error,
                    self.snapshot(),
                    prepared.parent_id,
                    prepared.depth,
                ),
                self.signal,
            )
            if override is None:
                return result
            if not isinstance(override, AfterToolCallResult):
                raise TypeError("after_tool_call must return AfterToolCallResult or None")
            if any(
                flag is not None and not isinstance(flag, bool)
                for flag in (override.is_error, override.terminate)
            ):
                raise TypeError("after_tool_call flags must be bool or None")
            structured = (
                override.structured_content
                if override.structured_content is not None
                else (None if override.content is not None else result.structured_content)
            )
            updated = replace(
                result,
                content=override.content if override.content is not None else result.content,
                details=override.details if override.details is not None else result.details,
                structured_content=structured,
                usage=override.usage if override.usage is not None else result.usage,
                is_error=result.is_error or bool(override.is_error),
                terminate=override.terminate
                if override.terminate is not None
                else result.terminate,
            )
            _validate_result(updated)
            return deepcopy(updated)
        except (ToolExecutionError, OSError) as exc:
            return error_result(f"Tool result hook failed: {exc}")
        except (Exception, asyncio.CancelledError) as exc:
            self.fail(exc)
            return replace(result, is_error=True)

    async def execute(
        self, prepared: _Prepared, on_update: ToolUpdate | None = None
    ) -> AgentToolCallOutcome:
        call = prepared.call
        if prepared.immediate is not None:
            return AgentToolCallOutcome(call, deepcopy(prepared.immediate), 0)
        if self.signal is not None and self.signal.aborted:
            return AgentToolCallOutcome(call, error_result("Not executed: operation aborted"), 0)
        assert prepared.tool is not None and prepared.args is not None
        scope = _ToolScope(self, prepared, on_update)
        started = time.perf_counter()
        result: AgentToolResult
        try:
            tool = prepared.tool
            if tool.execute_with_context is not None:
                result = await tool.execute_with_context(call.id, prepared.args, scope)
            else:
                assert tool.execute is not None
                result = await tool.execute(call.id, prepared.args, self.signal, scope.update)
            _validate_result(result)
            result = deepcopy(result)
        except (ToolExecutionError, OSError) as exc:
            result = error_result(str(exc))
        except (Exception, asyncio.CancelledError) as exc:
            self.fail(exc)
            result = error_result("Tool implementation failed; side-effect outcome may be unknown")
        finally:
            await scope.close()
        duration = max(0, round((time.perf_counter() - started) * 1000))
        result = await self.finalize(prepared, result)
        return AgentToolCallOutcome(call, result, duration, scope.records())


class _ToolScope:
    def __init__(self, engine: _Engine, prepared: _Prepared, on_update: ToolUpdate | None) -> None:
        self._engine = engine
        self._prepared = prepared
        self._on_update = on_update
        self._accepting = True
        self._sequence = 0
        self._records: dict[int, NestedToolCall] = {}
        self._operations: set[asyncio.Future[None]] = set()

    @property
    def signal(self) -> AbortSignal | None:
        return self._engine.signal

    @property
    def tool_call_id(self) -> str:
        return self._prepared.call.id

    @property
    def parent_call_id(self) -> str | None:
        return self._prepared.parent_id

    @property
    def depth(self) -> int:
        return self._prepared.depth

    def _begin(self) -> asyncio.Future[None]:
        operation: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._operations.add(operation)
        return operation

    def _end(self, operation: asyncio.Future[None]) -> None:
        operation.set_result(None)
        self._operations.discard(operation)

    async def update(self, result: AgentToolResult) -> None:
        if not self._accepting or self._on_update is None:
            return
        operation = self._begin()
        try:
            _validate_result(result)
            await self._on_update(deepcopy(result))
        except (Exception, asyncio.CancelledError) as exc:
            self._engine.fail(exc)
            raise
        finally:
            self._end(operation)

    async def execute_tool(self, name: str, args: dict[str, Any]) -> AgentToolCallOutcome:
        self._sequence += 1
        sequence = self._sequence
        call = ToolCall(f"{self.tool_call_id}/{sequence}", name, deepcopy(args))
        if not self._accepting:
            return AgentToolCallOutcome(call, error_result("Tool context is closed"), 0)
        operation = self._begin()
        try:
            prepared = await self._engine.prepare(call, self.tool_call_id, self.depth + 1)
            outcome = await self._engine.execute(prepared)
        except (Exception, asyncio.CancelledError) as exc:
            self._engine.fail(exc)
            outcome = AgentToolCallOutcome(
                call, error_result("Nested tool outcome may be unknown"), 0
            )
        finally:
            self._end(operation)
        self._records[sequence] = NestedToolCall(deepcopy(call), _message(outcome))
        return deepcopy(outcome)

    async def close(self) -> None:
        self._accepting = False
        if self._operations:
            joined = asyncio.gather(*self._operations)
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError as exc:
                    self._engine.fail(exc)

    def records(self) -> list[NestedToolCall]:
        return [self._records[index] for index in sorted(self._records)]


def _engine(
    context: AgentContext,
    message: AssistantMessage,
    signal: AbortSignal | None,
    before: BeforeToolCall | None,
    after: AfterToolCall | None,
    limits: ToolCallLimits,
) -> _Engine:
    engine = _Engine(context, message, signal, before, after, limits)
    engine.signal = CombinedAbortSignal(signal, engine.stop)
    return engine


async def run_tool_call(
    tool_call: ToolCall,
    *,
    tools: Sequence[AgentTool],
    assistant_message: AssistantMessage,
    context: AgentContext,
    signal: AbortSignal | None = None,
    on_update: ToolUpdate | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
    limits: ToolCallLimits | None = None,
) -> AgentToolCallOutcome:
    """Standalone checked invocation, without emitting events or appending transcript.

    Nested tools must use their supplied ToolExecutionContext.execute_tool(), which
    preserves hooks, audit ancestry, limits and the parent's cancellation signal.
    Operational failures return an error outcome; programmer errors remain exceptions.
    """
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("Tool names must be unique")
    if not tool_call.id:
        raise ValueError("Tool call ID must not be empty")
    engine = _engine(
        AgentContext(deepcopy(context.messages), list(tools)),
        assistant_message,
        signal,
        before_tool_call,
        after_tool_call,
        limits or ToolCallLimits(),
    )
    outcome = await engine.execute(await engine.prepare(tool_call), on_update)
    if engine.failure is not None:
        raise engine.failure
    return outcome


async def execute_tool_calls(
    context: AgentContext,
    message: AssistantMessage,
    signal: AbortSignal | None,
    emit: EventSink,
    config: AgentLoopConfig,
) -> ToolBatch:
    """Preflight in source order; execution-end in completion order; results in source order.

    A sequential declaration forces the whole batch to be sequential. Every top-level
    call gets a result, even if another call fails or cancellation stops new execution.
    Parallel tasks are always joined before the caller receives the completed batch.
    """
    calls = [block for block in message.content if isinstance(block, ToolCall)]
    if len({call.id for call in calls}) != len(calls) or any(not call.id for call in calls):
        raise ValueError("Tool call IDs must be nonempty and unique within a response")
    engine = _engine(
        context,
        message,
        signal,
        config.before_tool_call,
        config.after_tool_call,
        config.tool_limits,
    )
    event_lock = asyncio.Lock()

    async def notify(event: ToolExecutionStart | ToolExecutionUpdate | ToolExecutionEnd) -> None:
        try:
            async with event_lock:
                await emit(event)
        except (Exception, asyncio.CancelledError) as exc:
            engine.fail(exc)

    async def prepare(call: ToolCall) -> _Prepared:
        await notify(ToolExecutionStart(call.id, call.name, deepcopy(call.arguments)))
        if message.stop_reason in ("length", "error", "aborted", "deferred"):
            reason = (
                "arguments may be truncated"
                if message.stop_reason == "length"
                else message.stop_reason
            )
            return _Prepared(deepcopy(call), immediate=error_result(f"Not executed: {reason}"))
        return await engine.prepare(call)

    async def execute(prepared: _Prepared) -> AgentToolCallOutcome:
        async def update(result: AgentToolResult) -> None:
            await notify(ToolExecutionUpdate(prepared.call.id, prepared.call.name, result))

        try:
            outcome = await engine.execute(prepared, update)
        except (Exception, asyncio.CancelledError) as exc:
            engine.fail(exc)
            outcome = AgentToolCallOutcome(
                prepared.call, error_result("Tool execution failed; outcome may be unknown"), 0
            )
        await notify(
            ToolExecutionEnd(
                prepared.call.id,
                prepared.call.name,
                deepcopy(outcome.result),
                outcome.is_error,
                outcome.duration_ms,
            )
        )
        return outcome

    sequential = config.tool_execution == "sequential" or any(
        tool.execution_mode == "sequential"
        for tool in context.tools
        if any(call.name == tool.name for call in calls)
    )
    outcomes: list[AgentToolCallOutcome] = []
    if sequential:
        for call in calls:
            outcomes.append(await execute(await prepare(call)))
    else:
        prepared = [await prepare(call) for call in calls]
        tasks = [asyncio.create_task(execute(item)) for item in prepared]
        joined = asyncio.gather(*tasks)
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError as exc:
                engine.fail(exc)
                # Repeated cancellation must not detach or prematurely cancel running tools.
        outcomes = list(joined.result())
    return ToolBatch(
        [_message(outcome) for outcome in outcomes],
        bool(outcomes) and all(outcome.result.terminate for outcome in outcomes),
        engine.failure,
    )
