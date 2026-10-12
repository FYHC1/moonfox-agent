"""Internal application facts, separate from HTTP wire schemas."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from moonfox_agent.api import AgentTool, RunOptions, RunOutcome
from moonfox_ai.api import Model, StreamFn, StreamOptions
from moonfox_app.config import SessionSettings
from moonfox_app.extension_api import (
    EventHandler,
    PermissionChecker,
    RegistrationHandle,
    SessionView,
)
from moonfox_app.runtime.compaction import CompactionResult

type Domain = Literal["agent", "qq"]


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    owner_id: str
    domain: Domain
    scope_id: str
    route_key: str | None
    title: str
    created_at: int
    updated_at: int
    settings: SessionSettings
    config_revision: int


@dataclass(frozen=True, slots=True)
class InputRecord:
    id: str
    session_id: str
    request_id: str
    idempotency_key: str
    text: str
    kind: str
    logical_id: str
    status: str
    created_at: int
    run_id: str | None = None
    error: str | None = None
    baseline_id: str | None = None


@dataclass(frozen=True, slots=True)
class LogicalBudget:
    id: str
    max_requests: int
    max_tokens: int
    max_milliseconds: int
    requests_used: int
    tokens_used: int
    milliseconds_used: int


@dataclass(frozen=True, slots=True)
class ModelRegistration:
    model: Model
    stream_fn: StreamFn
    label: str
    options: StreamOptions = field(default_factory=StreamOptions, repr=False)
    revision: str = "builtin"
    supports_tools: bool = True


class SessionRuntime(Protocol):
    @property
    def active_extensions(self) -> tuple[str, ...]: ...

    def view(self) -> SessionView: ...

    def subscribe(self, listener: EventHandler) -> RegistrationHandle: ...

    async def prompt(self, message: str, *, options: RunOptions) -> RunOutcome: ...

    async def continue_(self, *, options: RunOptions) -> RunOutcome: ...

    async def compact(self, *, instructions: str | None = None) -> CompactionResult | None: ...

    def abort(self) -> None: ...

    async def wait_for_idle(self) -> None: ...

    async def aclose(self) -> None: ...


type RuntimeFactory = Callable[
    [SessionRecord, StreamFn, PermissionChecker, ModelRegistration], Awaitable[SessionRuntime]
]
type ToolCatalog = dict[str, AgentTool]
type Data = dict[str, Any]
