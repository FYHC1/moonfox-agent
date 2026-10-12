"""Single source for versioned HTTP/WS DTOs and exported frontend JSON schemas."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from moonfox_app.config import HostSettings, SessionSettings

PROTOCOL_VERSION = 1


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateSession(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="新对话", min_length=1, max_length=120)


class SubmitInput(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ControlCommand(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    action: Literal["cancel", "continue", "compact"]
    input_id: str | None = None


class UpdateConfig(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    settings: SessionSettings


class UpdateHostConfig(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    settings: HostSettings


class ApprovalCommand(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "deny"]


class Subscribe(DTO):
    type: Literal["subscribe"] = "subscribe"
    token: str = Field(min_length=1, max_length=4096)
    epoch: str
    after_seq: int = Field(ge=0)


class Receipt(DTO):
    request_id: str
    session_id: str
    input_id: str | None = None
    run_id: str | None = None
    status: str


class MessageView(DTO):
    id: str
    role: Literal["user", "assistant", "toolResult"]
    text: str
    timestamp: int
    status: str = "complete"
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_name: str | None = None
    is_error: bool = False


class SessionSummary(DTO):
    id: str
    title: str
    domain: Literal["agent", "qq"]
    owner_id: str
    created_at: int
    updated_at: int
    status: str
    pending_count: int
    model_id: str


class InputView(DTO):
    id: str
    request_id: str
    run_id: str | None
    logical_id: str
    status: str
    kind: str
    created_at: int
    error: str | None = None
    requests_used: int
    tokens_used: int
    max_requests: int


class ApprovalView(DTO):
    id: str
    session_id: str
    input_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    expires_at: int


class ExtensionView(DTO):
    id: str
    version: str
    scope: str
    capabilities: list[str]
    enabled: bool


class Snapshot(DTO):
    protocol_version: Literal[1] = 1
    epoch: str
    session_id: str
    seq: int
    session: SessionSummary
    messages: list[MessageView]
    inputs: list[InputView]
    approvals: list[ApprovalView]
    settings: SessionSettings
    config_revision: int
    extensions: list[ExtensionView]
    capabilities: list[str]


class WireEvent(DTO):
    protocol_version: Literal[1] = 1
    epoch: str
    session_id: str
    seq: int
    type: Literal[
        "session_changed",
        "input_received",
        "run_started",
        "run_finished",
        "message_start",
        "text_delta",
        "message_end",
        "approval_requested",
        "approval_resolved",
        "config_changed",
        "resync_required",
        "heartbeat",
    ]
    payload: dict[str, Any]


class ConfigView(DTO):
    revision: int
    settings: HostSettings
    models: list[dict[str, str]]
    tools: list[str]
    extensions: list[dict[str, Any]]


class ErrorResponse(DTO):
    error: str
    detail: str


SCHEMAS: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        CreateSession,
        SubmitInput,
        ControlCommand,
        UpdateConfig,
        UpdateHostConfig,
        ApprovalCommand,
        Subscribe,
        Receipt,
        MessageView,
        SessionSummary,
        InputView,
        ApprovalView,
        ExtensionView,
        Snapshot,
        WireEvent,
        ConfigView,
        ErrorResponse,
    )
}
