"""Stateful transcript, delivery queues, and one owned run at a time."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace

from moonfox_ai.api import (
    ImageContent,
    Model,
    StreamFn,
    StreamOptions,
    SystemMessage,
    TextContent,
    UserMessage,
    create_initial_system_message,
    get_current_system_message,
    get_current_system_prompt,
    now_ms,
)

from .abort import AbortToken
from .hooks import AfterToolCall, BeforeToolCall, FinishTurn, PrepareNextTurn, PrepareRequest
from .loop import agent_loop, agent_loop_continue
from .queues import PendingQueue, QueueMode
from .types import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentTool,
    ConvertToLlm,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    RunOptions,
    RunOutcome,
    ToolCallLimits,
    ToolExecutionEnd,
    ToolExecutionMode,
    ToolExecutionStart,
    TransformContext,
    TurnEnd,
    default_convert_to_llm,
)

type AgentListener = Callable[[AgentEvent, AbortToken], Awaitable[None] | None]
_IN_RUN: ContextVar[object | None] = ContextVar("active_agent_run", default=None)


@dataclass(slots=True)
class AgentInitialState:
    model: Model
    system_prompt: str = ""
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    thinking_level: str = "off"


@dataclass(frozen=True, slots=True)
class AgentState:
    """Detached state view. system_prompt is derived, not independently writable."""

    model: Model
    messages: list[AgentMessage]
    tools: list[AgentTool]
    is_streaming: bool
    streaming_message: AgentMessage | None
    pending_tool_calls: frozenset[str]
    error_message: str | None
    thinking_level: str

    @property
    def system_prompt(self) -> str:
        return get_current_system_prompt(self.messages)


@dataclass(slots=True)
class _ActiveRun:
    token: AbortToken
    done: asyncio.Future[None]


class Agent:
    """A bounded run is not a persistent worker. Queue work is accepted, not auto-started.

    Subscribers are awaited in snapshot registration order and see detached events.
    agent_end is the last event; its listeners remain part of the run's settle.
    Tools and child tasks cannot wait for their own run to become idle.
    """

    def __init__(
        self,
        *,
        initial_state: AgentInitialState,
        stream_fn: StreamFn,
        convert_to_llm: ConvertToLlm = default_convert_to_llm,
        transform_context: TransformContext | None = None,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        finish_turn: FinishTurn | None = None,
        prepare_request: PrepareRequest | None = None,
        prepare_next_turn: PrepareNextTurn | None = None,
        steering_mode: QueueMode = "one_at_a_time",
        follow_up_mode: QueueMode = "one_at_a_time",
        tool_execution: ToolExecutionMode = "parallel",
        tool_limits: ToolCallLimits | None = None,
        stream_options: StreamOptions | None = None,
        get_api_key: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        if tool_execution not in ("parallel", "sequential"):
            raise ValueError("Invalid tool execution mode")
        self._model = initial_state.model
        self._thinking = initial_state.thinking_level
        self._context = AgentContext(deepcopy(initial_state.messages), list(initial_state.tools))
        self._validate_tools(self._context.tools)
        # A restored transcript owns its existing system baseline; don't duplicate shorthand.
        if not any(isinstance(message, SystemMessage) for message in self._context.messages):
            head = create_initial_system_message(
                initial_state.system_prompt, [tool.declaration() for tool in self._context.tools]
            )
            if head is not None:
                self._context.messages.insert(0, head)
        self._stream_fn = stream_fn
        self._convert = convert_to_llm
        self._transform = transform_context
        self._before = before_tool_call
        self._after = after_tool_call
        self._finish = finish_turn
        self._prepare = prepare_request
        self._next = prepare_next_turn
        self._tool_execution = tool_execution
        self._tool_limits = tool_limits or ToolCallLimits()
        self._stream_options = (
            replace(stream_options, headers=deepcopy(stream_options.headers))
            if stream_options
            else StreamOptions()
        )
        self._get_api_key = get_api_key
        self._steering = PendingQueue(steering_mode)
        self._follow_up = PendingQueue(follow_up_mode)
        self._listeners: dict[int, AgentListener] = {}
        self._next_listener = 0
        self._active: _ActiveRun | None = None
        self._streaming_message: AgentMessage | None = None
        self._pending: frozenset[str] = frozenset()
        self._error: str | None = None
        self._event_lock = asyncio.Lock()

    @staticmethod
    def _validate_tools(tools: Sequence[AgentTool]) -> None:
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique")

    def _require_idle(self) -> None:
        if self._active is not None:
            raise RuntimeError("Agent is already processing; wait_for_idle() before changing state")

    @property
    def state(self) -> AgentState:
        return AgentState(
            model=self._model,
            messages=deepcopy(self._context.messages),
            tools=list(self._context.tools),
            is_streaming=self._active is not None,
            streaming_message=deepcopy(self._streaming_message),
            pending_tool_calls=self._pending,
            error_message=self._error,
            thinking_level=self._thinking,
        )

    @property
    def steering_mode(self) -> QueueMode:
        return self._steering.mode

    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None:
        self._steering.mode = mode

    @property
    def follow_up_mode(self) -> QueueMode:
        return self._follow_up.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: QueueMode) -> None:
        self._follow_up.mode = mode

    def steer(self, message: AgentMessage) -> None:
        """Deliver one batch before the next request, after the current tool batch settles."""
        self._steering.enqueue(message)

    def follow_up(self, message: AgentMessage) -> None:
        """Deliver only after tool/steering work would otherwise stop."""
        self._follow_up.enqueue(message)

    def clear_steering_queue(self) -> None:
        self._steering.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self._steering.has_items() or self._follow_up.has_items()

    def peek_queued_messages(self) -> list[AgentMessage]:
        return self._steering.peek() or self._follow_up.peek()

    def replace_messages(self, messages: list[AgentMessage]) -> None:
        self._require_idle()
        self._context.messages = deepcopy(messages)

    def set_tools(self, tools: Sequence[AgentTool]) -> None:
        self._require_idle()
        self._validate_tools(tools)
        self._context.tools = list(tools)

    def set_model(self, model: Model) -> None:
        self._require_idle()
        self._model = model

    def set_thinking_level(self, level: str) -> None:
        self._require_idle()
        self._thinking = level

    def subscribe(self, listener: AgentListener) -> Callable[[], None]:
        key = self._next_listener
        self._next_listener += 1
        self._listeners[key] = listener

        def unsubscribe() -> None:
            self._listeners.pop(key, None)

        return unsubscribe

    async def _emit(self, event: AgentEvent) -> None:
        active = self._active
        if active is None:
            raise RuntimeError("Cannot dispatch events outside an active run")
        async with self._event_lock:
            if isinstance(event, (MessageStart, MessageUpdate)):
                self._streaming_message = deepcopy(event.message)
            elif isinstance(event, MessageEnd):
                self._streaming_message = None
            elif isinstance(event, ToolExecutionStart):
                self._pending = self._pending | {event.tool_call_id}
            elif isinstance(event, ToolExecutionEnd):
                self._pending = self._pending - {event.tool_call_id}
            elif isinstance(event, TurnEnd) and event.message.error_message:
                self._error = event.message.error_message
            failure: Exception | asyncio.CancelledError | None = None
            for listener in tuple(self._listeners.values()):
                try:
                    result = listener(deepcopy(event), active.token)
                    if inspect.isawaitable(result):
                        await result
                except (Exception, asyncio.CancelledError) as exc:
                    active.token.abort()
                    failure = failure or exc
            if failure is not None:
                raise failure

    async def prompt(
        self,
        message: str | AgentMessage | list[AgentMessage],
        *,
        options: RunOptions,
        images: list[ImageContent] | None = None,
    ) -> RunOutcome:
        self._require_idle()
        prompts: list[AgentMessage]
        if isinstance(message, str):
            content: str | list[TextContent | ImageContent] = (
                [TextContent(message), *deepcopy(images)] if images else message
            )
            prompts = [UserMessage(content, now_ms())]
        elif images:
            raise ValueError("images is only supported with a string prompt")
        elif isinstance(message, list):
            prompts = deepcopy(message)
        else:
            prompts = [deepcopy(message)]
        if not prompts:
            raise ValueError("prompt requires at least one message")
        return await self._run(prompts, options, continuing=False)

    async def continue_(self, *, options: RunOptions) -> RunOutcome:
        """Continue user/tool-result context, or queued input after an assistant response."""
        self._require_idle()
        return await self._run([], options, continuing=True)

    async def _run(
        self, prompts: list[AgentMessage], options: RunOptions, *, continuing: bool
    ) -> RunOutcome:
        self._require_idle()
        if not isinstance(options, RunOptions):
            raise TypeError("A run requires RunOptions")
        config = AgentLoopConfig(
            model=self._model,
            options=options,
            convert_to_llm=self._convert,
            transform_context=self._transform,
            before_tool_call=self._before,
            after_tool_call=self._after,
            finish_turn=self._finish,
            prepare_request=self._prepare,
            prepare_next_turn=self._next,
            steering_queue=self._steering,
            follow_up_queue=self._follow_up,
            tool_execution=self._tool_execution,
            tool_limits=self._tool_limits,
            thinking_level=self._thinking,
            stream_options=self._stream_options,
            get_api_key=self._get_api_key,
        )
        active = _ActiveRun(AbortToken(), asyncio.get_running_loop().create_future())
        self._active = active
        marker = _IN_RUN.set(active)
        self._error = None
        try:
            if continuing:
                return await agent_loop_continue(
                    self._context, config, self._emit, active.token, self._stream_fn
                )
            return await agent_loop(
                prompts, self._context, config, self._emit, active.token, self._stream_fn
            )
        except (Exception, asyncio.CancelledError) as exc:
            self._error = str(exc) or "Operation cancelled"
            raise
        finally:
            _IN_RUN.reset(marker)
            self._streaming_message = None
            self._pending = frozenset()
            self._active = None
            active.done.set_result(None)

    def abort(self) -> None:
        if self._active is not None:
            self._active.token.abort()

    async def wait_for_idle(self) -> None:
        active = self._active
        if active is not None:
            if _IN_RUN.get() is active:
                raise RuntimeError(
                    "A run cannot wait for its own tasks or listeners to become idle"
                )
            await asyncio.shield(active.done)

    def reset(self) -> None:
        """Clear conversation and queues, preserving replayed prompt/sections and active tools."""
        self._require_idle()
        baseline = get_current_system_message(self._context.messages)
        declarations = [tool.declaration() for tool in self._context.tools]
        if baseline is None:
            baseline = create_initial_system_message(None, declarations)
        else:
            baseline.tools_added = declarations or None
            baseline.tools_removed = None
        self._context.messages = [baseline] if baseline is not None else []
        self.clear_all_queues()
        self._error = None
        self._streaming_message = None
        self._pending = frozenset()
