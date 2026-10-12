"""Configuration snapshots. Secrets are never values in this public schema."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .providers import MODEL_ID_MAX_LENGTH, GenerationParameters


class SessionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_id: str = Field(default="demo", min_length=1, max_length=MODEL_ID_MAX_LENGTH)
    thinking_level: str = Field(default="off", min_length=1, max_length=32)
    max_turns_per_run: int = Field(default=4, ge=1, le=128)
    max_requests_per_input: int = Field(default=8, ge=1, le=512)
    max_tokens_per_input: int = Field(default=64000, ge=1, le=10000000)
    run_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    allowed_tools: tuple[str, ...] = ()
    approval_tools: tuple[str, ...] = ()
    enabled_extensions: tuple[str, ...] = ()
    generation: GenerationParameters = Field(default_factory=GenerationParameters)

    @model_validator(mode="after")
    def check_consistency(self) -> "SessionSettings":
        if self.max_turns_per_run > self.max_requests_per_input:
            raise ValueError("Per-run turns cannot exceed the logical input request budget")
        if not set(self.approval_tools) <= set(self.allowed_tools):
            raise ValueError("Approval tools must be in the tool allowlist")
        for values in (self.allowed_tools, self.approval_tools, self.enabled_extensions):
            if len(values) != len(set(values)) or any(
                not value or len(value) > 100 for value in values
            ):
                raise ValueError("Tool and extension identifiers must be nonempty and unique")
        return self


class HostSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    defaults: SessionSettings = Field(default_factory=SessionSettings)
    max_concurrent_requests: int = Field(default=2, ge=1, le=32)
    approval_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    event_window: int = Field(default=256, ge=8, le=8192)
    subscriber_buffer: int = Field(default=128, ge=2, le=8192)
    max_pending_inputs: int = Field(default=100, ge=1, le=10000)
