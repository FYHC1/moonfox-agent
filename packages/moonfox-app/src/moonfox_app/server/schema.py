"""Single source for versioned HTTP/WS DTOs and exported frontend JSON schemas."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from moonfox_app.config import HostSettings, SessionSettings
from moonfox_app.config.providers import (
    MODEL_ID_MAX_LENGTH,
    MODEL_IDENTIFIER,
    ModelSettings,
    ProviderSettings,
)

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


class RevisionCommand(DTO):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


class PutProvider(RevisionCommand):
    settings: ProviderSettings
    secret_action: Literal["keep", "replace", "clear"] = "keep"
    api_key: SecretStr | None = Field(default=None, max_length=4096, repr=False)

    @model_validator(mode="after")
    def check_secret_action(self) -> "PutProvider":
        if self.secret_action == "replace":
            if (
                self.api_key is None
                or not self.api_key.get_secret_value().strip()
                or self.settings.auth_mode != "stored"
            ):
                raise ValueError("Replacing credentials requires a stored key")
            if any(ord(c) < 32 or ord(c) == 127 for c in self.api_key.get_secret_value()):
                raise ValueError("Credential contains control characters")
        elif self.api_key is not None:
            raise ValueError("Key must be omitted for keep/clear")
        return self


class ProviderView(DTO):
    id: str
    revision: int
    settings: ProviderSettings
    key_configured: bool
    credential_source: Literal["stored", "environment", "none"]


class ProviderCatalog(DTO):
    providers: list[ProviderView]
    secret_storage: Literal["dpapi", "env_only", "custom"]


class DiscoveredModel(DTO):
    id: str
    name: str


class ProviderTestResult(DTO):
    ok: bool
    status: Literal["ok", "auth_error", "unavailable", "unsupported", "invalid_response"]
    detail: str
    latency_ms: int
    models: list[DiscoveredModel]


class PutModel(RevisionCommand):
    settings: ModelSettings


class ManagedModelView(DTO):
    id: str = Field(pattern=MODEL_IDENTIFIER, min_length=1, max_length=MODEL_ID_MAX_LENGTH)
    revision: int
    settings: ModelSettings
    available: bool
    unavailable_reason: str | None


class ModelCatalog(DTO):
    models: list[ManagedModelView]
    builtin_models: list[dict[str, str]]
    default_model_id: str
    config_revision: int


class SetDefaultModel(RevisionCommand):
    pass


class Deleted(DTO):
    deleted: Literal[True] = True


SCHEMAS: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        RevisionCommand,
        PutProvider,
        ProviderView,
        ProviderCatalog,
        DiscoveredModel,
        ProviderTestResult,
        PutModel,
        ManagedModelView,
        ModelCatalog,
        SetDefaultModel,
        Deleted,
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
