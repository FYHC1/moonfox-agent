"""Stateful facade for the minimal loop, with an explicit and awaitable run lifetime."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field

from moonfox_ai.api import (
    Model,
    StreamFn,
    UserMessage,
    create_initial_system_message,
    get_current_system_prompt,
    now_ms,
)

from .abort import AbortToken
from .loop import agent_loop
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
    ToolExecutionEnd,
    ToolExecutionStart,
    TransformContext,
    TurnEnd,
    default_convert_to_llm,
)

type AgentListener = Callable[[AgentEvent, AbortToken], Awaitable[None] | None]


@dataclass(slots=True)
class AgentInitialState:
    model: Model
    system_prompt: str = ""
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AgentState:
    """Detached state view. Mutating a returned list cannot alter the running agent."""

    model: Model
    messages: list[AgentMessage]
    tools: list[AgentTool]
    is_streaming: bool
    streaming_message: AgentMessage | None
    pending_tool_calls: frozenset[str]
    error_message: str | None

    @property
    def system_prompt(self) -> str:
        return get_current_system_prompt(self.messages)


@dataclass(slots=True)
class _ActiveRun:
    token: AbortToken
    done: asyncio.Future[None]
    task: asyncio.Task[object] | None


class Agent:
    """Only owns generic transcript and run state; all external behavior is injected.

    subscribe listeners are awaited in registration order from a dispatch snapshot.
    agent_end is the last notification, but idle is reached only after its listeners
    finish. Programmer/listener errors propagate; the run still releases its lifetime.
    Cancel through abort(), then wait_for_idle(), before starting another run.
    """

    def __init__(
        self,
        *,
        initial_state: AgentInitialState,
        stream_fn: StreamFn,
        convert_to_llm: ConvertToLlm = default_convert_to_llm,
        transform_context: TransformContext | None = None,
    ) -> None:
        self._model = initial_state.model
        self._context = AgentContext(deepcopy(initial_state.messages), list(initial_state.tools))
        names = [tool.name for tool in self._context.tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique")
        head = create_initial_system_message(
            initial_state.system_prompt, [tool.declaration() for tool in self._context.tools]
        )
        if head is not None:
            self._context.messages.insert(0, head)
        self._stream_fn = stream_fn
        self._convert = convert_to_llm
        self._transform = transform_context
        self._listeners: dict[int, AgentListener] = {}
        self._next_listener = 0
        self._active: _ActiveRun | None = None
        self._streaming_message: AgentMessage | None = None
        self._pending: frozenset[str] = frozenset()
        self._error: str | None = None

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
        )

    def subscribe(self, listener: AgentListener) -> Callable[[], None]:
        key = self._next_listener
        self._next_listener += 1
        self._listeners[key] = listener

        def unsubscribe() -> None:
            self._listeners.pop(key, None)

        return unsubscribe

    async def _emit(self, event: AgentEvent) -> None:
        if self._active is None:
            raise RuntimeError("Cannot dispatch events outside an active run")
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
        for listener in tuple(self._listeners.values()):
            result = listener(event, self._active.token)
            if inspect.isawaitable(result):
                await result

    async def prompt(
        self,
        message: str | AgentMessage | list[AgentMessage],
        *,
        options: RunOptions,
    ) -> RunOutcome:
        if self._active is not None:
            raise RuntimeError("Agent is already processing; wait_for_idle() before a new run")
        if not isinstance(options, RunOptions):
            raise TypeError("prompt requires RunOptions")
        prompts: list[AgentMessage]
        if isinstance(message, str):
            prompts = [UserMessage(message, now_ms())]
        elif isinstance(message, list):
            prompts = deepcopy(message)
        else:
            prompts = [deepcopy(message)]
        if not prompts:
            raise ValueError("prompt requires at least one message")
        active = _ActiveRun(
            AbortToken(), asyncio.get_running_loop().create_future(), asyncio.current_task()
        )
        self._active = active
        self._error = None
        try:
            return await agent_loop(
                prompts,
                self._context,
                AgentLoopConfig(self._model, options, self._convert, self._transform),
                self._emit,
                active.token,
                self._stream_fn,
            )
        finally:
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
            if asyncio.current_task() is active.task:
                raise RuntimeError("A run cannot wait for its own listeners to become idle")
            await asyncio.shield(active.done)
