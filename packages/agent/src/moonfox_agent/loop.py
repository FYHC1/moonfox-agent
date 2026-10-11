"""Bounded tool/steering loop with follow-up scheduling and injected boundary hooks."""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    Context,
    Done,
    Error,
    FailureReason,
    Message,
    Start,
    StreamFn,
    StreamOptions,
    StreamOverflowError,
    StreamProtocolError,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UserMessage,
    get_current_tools,
    get_tool_state_changes,
    normalize_context,
    now_ms,
)

from .abort import AbortToken, CombinedAbortSignal
from .hooks import AgentLoopTurnUpdate, AgentRequestUpdate, PrepareRequestContext, TurnContext
from .tools import execute_tool_calls
from .types import (
    AgentContext,
    AgentEnd,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentStart,
    EventSink,
    MessageEnd,
    MessageQueue,
    MessageStart,
    MessageUpdate,
    RunOutcome,
    RunReason,
    TurnEnd,
    TurnStart,
)


def _snapshot(context: AgentContext) -> AgentContext:
    return AgentContext(deepcopy(context.messages), list(context.tools))


def _turn_snapshot(turn: TurnContext) -> TurnContext:
    return TurnContext(
        deepcopy(turn.message),
        deepcopy(turn.tool_results),
        _snapshot(turn.context),
        deepcopy(turn.new_messages),
        turn.turn,
    )


def _validate_context(context: AgentContext) -> None:
    if not isinstance(context, AgentContext) or not isinstance(context.messages, list):
        raise TypeError("Request hooks must return an AgentContext with a message list")
    names = [tool.name for tool in context.tools]
    if len(names) != len(set(names)):
        raise ValueError("Tool names must be unique")


def validate_tool_pairing(messages: list[Message]) -> None:
    """Reject orphan, duplicate and missing tool results before contacting a provider."""
    pending: dict[str, str] = {}
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, ToolResultMessage):
            if pending.get(message.tool_call_id) != message.tool_name:
                raise ValueError("Tool result has no matching call, or its name does not match")
            del pending[message.tool_call_id]
            continue
        if pending:
            raise ValueError("Tool calls must be paired before the next conversation message")
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    if not block.id or block.id in pending:
                        raise ValueError(
                            "Tool call IDs must be nonempty and unique within a response"
                        )
                    pending[block.id] = block.name
    if pending:
        raise ValueError("Cannot request a model response with unpaired tool calls")


def declare_tool_changes(context: AgentContext, pending: list[AgentMessage]) -> list[AgentMessage]:
    """Executable tools override tool-field intent in pending system entries.

    Prompt text/sections are preserved. One delta is attached to the last pending
    system entry, or inserted before the first non-system entry. Inputs are not mutated.
    """
    messages = deepcopy(pending)
    systems = [message for message in messages if isinstance(message, SystemMessage)]
    for message in systems:
        message.tools_added = None
        message.tools_removed = None
    changes = get_tool_state_changes(
        get_current_tools(context.messages), [tool.declaration() for tool in context.tools]
    )
    if not changes.tools_added and not changes.tools_removed:
        return messages
    delta = systems[-1] if systems else SystemMessage("", now_ms())
    delta.tools_added = changes.tools_added
    delta.tools_removed = changes.tools_removed
    if not systems:
        messages.insert(0, delta)
    return messages


class _Run:
    def __init__(
        self,
        context: AgentContext,
        config: AgentLoopConfig,
        emit: EventSink,
        signal: AbortSignal | None,
        stream_fn: StreamFn,
    ) -> None:
        _validate_context(context)
        self.canonical = context
        self.current = _snapshot(context)
        # Do not let hooks or another run mutate the caller's run configuration.
        self.config = replace(
            config,
            stream_options=replace(
                config.stream_options, headers=deepcopy(config.stream_options.headers)
            ),
        )
        self.budget = config.options.max_turns_per_run
        self.sink = emit
        self.stop = AbortToken()
        self.signal = CombinedAbortSignal(signal, config.stream_options.signal, self.stop)
        self.stream_fn = stream_fn
        self.new_messages: list[AgentMessage] = []
        self.turns = 0
        self.failure: Exception | asyncio.CancelledError | None = None
        self.finished = False

    def fail(self, error: Exception | asyncio.CancelledError) -> None:
        if self.failure is None:
            self.failure = error
        self.stop.abort()

    async def notify(self, event: AgentEvent) -> None:
        try:
            await self.sink(event)
        except (Exception, asyncio.CancelledError) as exc:
            self.fail(exc)

    async def append(self, message: AgentMessage, *, message_id: str | None = None) -> None:
        identity = message_id or uuid4().hex
        stored = deepcopy(message)
        self.canonical.messages.append(stored)
        self.current.messages.append(deepcopy(stored))
        self.new_messages.append(stored)
        if message_id is None:
            await self.notify(MessageStart(deepcopy(stored), identity))
        await self.notify(MessageEnd(deepcopy(stored), identity))

    async def append_inputs(self, messages: list[AgentMessage]) -> None:
        baseline = AgentContext(self.canonical.messages, self.current.tools)
        for message in declare_tool_changes(baseline, messages):
            await self.append(message)

    async def sync_tools(self) -> None:
        await self.append_inputs([])
        # A request projection may omit old declaration entries; repair it separately.
        changes = get_tool_state_changes(
            get_current_tools(self.current.messages),
            [tool.declaration() for tool in self.current.tools],
        )
        if changes.tools_added or changes.tools_removed:
            self.current.messages.append(
                SystemMessage(
                    "",
                    now_ms(),
                    tools_added=changes.tools_added,
                    tools_removed=changes.tools_removed,
                )
            )

    def apply(self, update: AgentRequestUpdate) -> None:
        if not isinstance(update, AgentRequestUpdate):
            raise TypeError("Request hooks must return AgentRequestUpdate or None")
        if update.context is not None:
            _validate_context(update.context)
            self.current = _snapshot(update.context)
        if update.model is not None:
            self.config.model = update.model
        if update.thinking_level is not None:
            self.config.thinking_level = update.thinking_level

    async def finish(self, reason: RunReason) -> RunOutcome:
        if self.failure is not None:
            reason = "aborted" if isinstance(self.failure, asyncio.CancelledError) else "error"
        outcome = RunOutcome(reason, self.turns)
        if not self.finished:
            self.finished = True
            await self.notify(AgentEnd(deepcopy(self.new_messages), outcome))
        if self.failure is not None:
            raise self.failure
        return outcome

    async def prepare_request(
        self, *, validate_continue: bool
    ) -> tuple[TranscriptContext, StreamOptions]:
        if self.config.prepare_request is not None:
            update = await self.config.prepare_request(
                PrepareRequestContext(
                    _snapshot(self.current),
                    self.config.model,
                    self.config.thinking_level,
                    deepcopy(self.canonical.messages),
                ),
                self.signal,
            )
            if update is not None:
                self.apply(update)
        await self.sync_tools()
        messages = deepcopy(self.current.messages)
        if self.config.transform_context is not None:
            messages = await self.config.transform_context(messages, self.signal)
        converted = self.config.convert_to_llm(messages)
        llm_messages = await converted if inspect.isawaitable(converted) else converted
        request = normalize_context(Context(messages=llm_messages))
        validate_tool_pairing(request.messages)
        conversation = [
            message for message in request.messages if not isinstance(message, SystemMessage)
        ]
        if validate_continue and (
            not conversation or not isinstance(conversation[-1], (UserMessage, ToolResultMessage))
        ):
            raise ValueError("Cannot continue: converted context must end with user or toolResult")
        options = replace(
            self.config.stream_options,
            signal=self.signal,
            reasoning=self.config.thinking_level,
            headers=deepcopy(self.config.stream_options.headers),
        )
        if self.config.get_api_key is not None:
            key = await self.config.get_api_key(self.config.model.provider)
            if key is not None:
                options.api_key = key
        return request, options

    async def response(
        self, request: TranscriptContext, options: StreamOptions
    ) -> tuple[AssistantMessage, str]:
        identity = uuid4().hex
        started = False
        partial: AssistantMessage | None = None
        final: AssistantMessage | None = None
        stream = None
        try:
            if not self.signal.aborted:
                self.turns += 1
                stream = self.stream_fn(self.config.model, request, options)
                try:
                    async for event in stream:
                        if isinstance(event, Start):
                            started = True
                            partial = deepcopy(event.partial)
                            await self.notify(MessageStart(deepcopy(partial), identity))
                        elif isinstance(event, (Done, Error)):
                            break
                        else:
                            partial = deepcopy(event.partial)
                            await self.notify(MessageUpdate(deepcopy(partial), event, identity))
                except StreamOverflowError:
                    # Recover from slow observation using the authoritative final state.
                    final = deepcopy(await stream.result())
                    final.diagnostics = [*(final.diagnostics or []), {"type": "stream_overflow"}]
                if final is None:
                    final = deepcopy(await stream.result())
        except (Exception, asyncio.CancelledError) as exc:
            self.fail(exc)
        finally:
            if stream is not None:
                cleanup = asyncio.create_task(stream.aclose())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as exc:
                        self.fail(exc)
                    except Exception as exc:
                        self.fail(exc)
                try:
                    cleanup.result()
                except (Exception, asyncio.CancelledError) as exc:
                    self.fail(exc)
                if final is None and stream.done:
                    try:
                        final = deepcopy(await stream.result())
                    except (Exception, asyncio.CancelledError) as exc:
                        self.fail(exc)
        if final is None:
            reason: FailureReason = (
                "error"
                if self.failure is not None and not isinstance(self.failure, asyncio.CancelledError)
                else "aborted"
            )
            final = partial or AssistantMessage(
                [],
                self.config.model.api,
                self.config.model.provider,
                self.config.model.id,
                Usage(),
                reason,
                now_ms(),
            )
            final.stop_reason = reason
            final.error_message = (
                str(self.failure) if self.failure is not None else "Operation aborted"
            )
        calls = [block for block in final.content if isinstance(block, ToolCall)]
        invalid_calls = len({call.id for call in calls}) != len(calls) or any(
            not call.id for call in calls
        )
        if invalid_calls or final.stop_reason == "pending":
            error = StreamProtocolError(
                "Provider returned invalid tool IDs or a pending final response"
            )
            self.fail(error)
            final.stop_reason = "error"
            final.error_message = str(error)
            if invalid_calls:
                # No safe one-to-one pairing exists; retain the raw calls for diagnosis, not replay.
                final.diagnostics = [
                    *(final.diagnostics or []),
                    {"type": "invalid_tool_calls", "calls": [asdict(call) for call in calls]},
                ]
                final.content = [
                    block for block in final.content if not isinstance(block, ToolCall)
                ]
        final.thinking_level = self.config.thinking_level
        if not started:
            await self.notify(MessageStart(deepcopy(final), identity))
        return final, identity

    def queue(self, *, follow_up: bool) -> MessageQueue | None:
        steering = self.config.steering_queue
        if steering is not None and steering.has_items():
            return steering
        follow = self.config.follow_up_queue
        if follow_up and follow is not None and follow.has_items():
            return follow
        return None

    async def run(self, prompts: list[AgentMessage], *, continuing: bool = False) -> RunOutcome:
        await self.notify(AgentStart())
        try:
            await self.append_inputs(prompts)
            if self.signal.aborted:
                return await self.finish("aborted")
            conversation = [m for m in self.current.messages if m.role != "system"]
            first_queue = self.queue(
                follow_up=bool(continuing and conversation and conversation[-1].role == "assistant")
            )
            if first_queue is not None:
                await self.append_inputs(first_queue.drain())
            last: TurnContext | None = None
            selected: MessageQueue | None = None
            while True:
                if self.signal.aborted:
                    return await self.finish("aborted")
                if self.turns >= self.budget:
                    return await self.finish("limit_reached")
                if last is not None:
                    prepared: list[AgentMessage] = []
                    if self.config.prepare_next_turn is not None:
                        update = await self.config.prepare_next_turn(_turn_snapshot(last))
                        if update is not None:
                            if not isinstance(update, AgentLoopTurnUpdate):
                                raise TypeError(
                                    "prepare_next_turn must return AgentLoopTurnUpdate or None"
                                )
                            self.apply(update)
                            prepared = update.messages or []
                    if self.signal.aborted:
                        return await self.finish("aborted")
                    # Only one drain per request, including steering arriving during a slow hook.
                    selected = selected or self.queue(follow_up=False)
                    pending = selected.drain() if selected is not None else []
                    await self.append_inputs([*prepared, *pending])
                request, options = await self.prepare_request(
                    validate_continue=continuing and last is None
                )
                if self.signal.aborted:
                    return await self.finish("aborted")
                turn = self.turns + 1
                await self.notify(TurnStart(turn))
                message, identity = await self.response(request, options)
                await self.append(message, message_id=identity)
                # Skip execution on error/abort, but still pair any partial tool calls.
                batch = await execute_tool_calls(
                    self.current, message, self.signal, self.notify, self.config
                )
                for result in batch.messages:
                    await self.append(result)
                if batch.failure is not None:
                    self.fail(batch.failure)
                last = TurnContext(
                    deepcopy(message),
                    deepcopy(batch.messages),
                    _snapshot(self.current),
                    deepcopy(self.new_messages),
                    turn,
                )
                decision = None
                if self.config.finish_turn is not None:
                    try:
                        decision = await self.config.finish_turn(_turn_snapshot(last), self.signal)
                        if decision not in (None, "continue", "end"):
                            raise TypeError("finish_turn must return continue, end or None")
                    except (Exception, asyncio.CancelledError) as exc:
                        self.fail(exc)
                await self.notify(TurnEnd(deepcopy(message), deepcopy(batch.messages), turn))
                if self.failure is not None:
                    return await self.finish("error")
                if message.stop_reason == "aborted" or self.signal.aborted:
                    return await self.finish("aborted")
                if message.stop_reason == "error":
                    return await self.finish("error")
                if message.stop_reason == "deferred" or decision == "end":
                    return await self.finish("completed")
                has_tools = (
                    any(isinstance(block, ToolCall) for block in message.content)
                    and not batch.terminate
                )
                selected = self.queue(follow_up=not has_tools)
                if not has_tools and selected is None and decision != "continue":
                    return await self.finish("completed")
                # Natural work satisfies continue. No budget reset and no premature queue drain.
        except (Exception, asyncio.CancelledError) as exc:
            self.fail(exc)
            return await self.finish("error")


async def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventSink,
    signal: AbortSignal | None,
    stream_fn: StreamFn,
) -> RunOutcome:
    """Append inputs and run. Request projections never replace the supplied transcript."""
    return await _Run(context, config, emit, signal, stream_fn).run(prompts)


async def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventSink,
    signal: AbortSignal | None,
    stream_fn: StreamFn,
) -> RunOutcome:
    """Continue a runnable context, or consume one queued batch after an assistant.

    The same unique conversion boundary validates the first request; no speculative
    provider call or duplicate codec call is used to decide whether continuation works.
    """
    return await _Run(context, config, emit, signal, stream_fn).run([], continuing=True)
