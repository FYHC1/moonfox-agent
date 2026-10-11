"""Stage A tool-continuation loop; no scheduling, recovery or persistence policy."""

import inspect
from copy import deepcopy
from uuid import uuid4

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    Context,
    Done,
    Error,
    Start,
    StreamFn,
    StreamOptions,
    SystemMessage,
    ToolCall,
    TranscriptContext,
    get_current_tools,
    get_tool_state_changes,
    normalize_context,
    now_ms,
)

from .tools import execute_tool_calls
from .types import (
    AgentContext,
    AgentEnd,
    AgentLoopConfig,
    AgentMessage,
    AgentStart,
    EventSink,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    RunOutcome,
    RunReason,
    TurnEnd,
    TurnStart,
)


async def _stream_response(
    config: AgentLoopConfig,
    context: TranscriptContext,
    emit: EventSink,
    signal: AbortSignal | None,
    stream_fn: StreamFn,
) -> tuple[AssistantMessage, str]:
    message_id = uuid4().hex
    response = stream_fn(config.model, context, StreamOptions(signal=signal))
    started = False
    try:
        async for event in response:
            if isinstance(event, Start):
                started = True
                await emit(MessageStart(deepcopy(event.partial), message_id))
            elif isinstance(event, (Done, Error)):
                break
            else:
                await emit(MessageUpdate(deepcopy(event.partial), event, message_id))
        final = deepcopy(await response.result())
        if not started:
            await emit(MessageStart(deepcopy(final), message_id))
        return final, message_id
    finally:
        await response.aclose()


async def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventSink,
    signal: AbortSignal | None,
    stream_fn: StreamFn,
) -> RunOutcome:
    """Append to the supplied transcript and emit detached lifecycle snapshots.

    Inputs precede the first TurnStart. A turn counts one issued model response and
    its entire tool batch. No new provider request is issued after the explicit
    budget is consumed. A naturally finished final turn returns completed, not a
    spurious limit_reached. transform_context and the unique conversion boundary
    run for the FIRST request too; temporary augmentation never enters transcript.
    """
    names = [tool.name for tool in context.tools]
    if len(set(names)) != len(names):
        raise ValueError("Tool names must be unique")
    new_messages: list[AgentMessage] = []
    turns_used = 0

    async def append(message: AgentMessage) -> None:
        message_id = uuid4().hex
        snapshot = deepcopy(message)
        context.messages.append(snapshot)
        new_messages.append(snapshot)
        await emit(MessageStart(deepcopy(snapshot), message_id))
        await emit(MessageEnd(deepcopy(snapshot), message_id))

    async def finish(reason: RunReason) -> RunOutcome:
        outcome = RunOutcome(reason, turns_used)
        await emit(AgentEnd(deepcopy(new_messages), outcome))
        return outcome

    await emit(AgentStart())
    for prompt in prompts:
        await append(prompt)
    while True:
        if signal is not None and signal.aborted:
            return await finish("aborted")
        if turns_used >= config.options.max_turns_per_run:
            return await finish("limit_reached")
        changes = get_tool_state_changes(
            get_current_tools(context.messages), [tool.declaration() for tool in context.tools]
        )
        if changes.tools_added or changes.tools_removed:
            await append(
                SystemMessage(
                    "",
                    now_ms(),
                    tools_added=changes.tools_added,
                    tools_removed=changes.tools_removed,
                )
            )
        messages = deepcopy(context.messages)
        if config.transform_context is not None:
            messages = await config.transform_context(messages, signal)
        converted = config.convert_to_llm(messages)
        llm_messages = await converted if inspect.isawaitable(converted) else converted
        request = normalize_context(Context(messages=llm_messages))
        if signal is not None and signal.aborted:
            return await finish("aborted")
        await emit(TurnStart(turns_used + 1))
        message, message_id = await _stream_response(config, request, emit, signal, stream_fn)
        turns_used += 1
        context.messages.append(message)
        new_messages.append(message)
        await emit(MessageEnd(deepcopy(message), message_id))
        calls = [block for block in message.content if isinstance(block, ToolCall)]
        batch = await execute_tool_calls(context, message, signal, emit)
        for result in batch.messages:
            await append(result)
        await emit(TurnEnd(deepcopy(message), deepcopy(batch.messages), turns_used))
        if batch.failure is not None:
            await finish("error")
            raise batch.failure
        if message.stop_reason == "aborted" or (signal is not None and signal.aborted):
            return await finish("aborted")
        if message.stop_reason == "error":
            return await finish("error")
        if message.stop_reason == "deferred" or not calls or batch.terminate:
            return await finish("completed")
