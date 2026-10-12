"""Provider/model management data. Credentials have a separate write-only command."""

import ipaddress
import json
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER = r"^[a-z][a-z0-9_-]{0,63}$"
# Model references are URL path segments, not provider slugs. Preserve case and Unicode.
MODEL_ID_MAX_LENGTH = 200
MODEL_IDENTIFIER = r"^[^./\\?#%\s\x00-\x1f\x7f][^/\\?#%\s\x00-\x1f\x7f]{0,199}$"
type ProviderProtocol = Literal["openai", "openai-responses", "anthropic"]


class GenerationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=262144)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    stop: tuple[str, ...] | None = Field(default=None, max_length=4)

    @field_validator("stop")
    @classmethod
    def check_stop(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is not None and any(not value or len(value) > 200 for value in values):
            raise ValueError("Stop sequences must contain 1–200 characters")
        return values


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=100)
    protocol: ProviderProtocol = "openai"
    base_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    auth_mode: Literal["stored", "env", "none"] = "stored"
    env_var: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("A display name without control characters is required")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        if any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in "\\?#"):
            raise ValueError("Base URL cannot contain credentials, query, fragment or whitespace")
        try:
            parts = urlsplit(value)
            host, port = parts.hostname, parts.port
            if (
                parts.scheme not in ("http", "https")
                or not host
                or parts.username is not None
                or parts.password is not None
                or parts.netloc.endswith(":")
                or port == 0
            ):
                raise ValueError
            if parts.scheme == "http":
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host.lower() == "localhost"
                if not local:
                    raise ValueError("Plain HTTP is only allowed for loopback providers")
            path = parts.path.rstrip("/")
            if path.endswith(("/chat/completions", "/responses", "/messages", "/models")):
                raise ValueError("Use the API base URL, not an individual endpoint")
            if any(segment in (".", "..") for segment in path.split("/")):
                raise ValueError("Base URL cannot contain path traversal segments")
            netloc = host.lower() if ":" not in host else f"[{host.lower()}]"
            if port is not None:
                netloc += f":{port}"
            return urlunsplit((parts.scheme, netloc, path, "", ""))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Use a valid HTTPS API base URL (HTTP only for loopback)") from exc

    @model_validator(mode="after")
    def check_auth(self) -> "ProviderSettings":
        if self.auth_mode == "env":
            if self.env_var is None or not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.env_var):
                raise ValueError("Environment authentication requires an env variable name")
        elif self.env_var is not None:
            raise ValueError("env_var is only valid for environment authentication")
        return self


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_id: str = Field(pattern=IDENTIFIER)
    name: str = Field(min_length=1, max_length=100)
    remote_id: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    context_window: int = Field(default=128000, ge=256, le=4000000)
    max_output_tokens: int = Field(default=4096, ge=1, le=262144)
    supports_tools: bool = True
    supports_images: bool = False
    supports_reasoning: bool = False
    token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    parameters: GenerationParameters = Field(default_factory=GenerationParameters)

    @field_validator("name", "remote_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Model names must not be empty or contain control characters")
        return value.strip()

    @model_validator(mode="after")
    def check_limits(self) -> "ModelSettings":
        if self.max_output_tokens >= self.context_window:
            raise ValueError("Output limit must be smaller than context window")
        if (
            self.parameters.max_tokens is not None
            and self.parameters.max_tokens > self.max_output_tokens
        ):
            raise ValueError("Default output tokens exceed the model output limit")
        return self


def merge_parameters(
    defaults: GenerationParameters, overrides: GenerationParameters
) -> GenerationParameters:
    data = defaults.model_dump(mode="json")
    data.update(overrides.model_dump(mode="json", exclude_none=True))
    return GenerationParameters.model_validate_json(json.dumps(data))


def validate_parameters(
    settings: ModelSettings,
    provider: ProviderSettings,
    overrides: GenerationParameters | None = None,
    thinking_level: str = "off",
) -> GenerationParameters:
    values = merge_parameters(settings.parameters, overrides or GenerationParameters())
    if values.max_tokens is not None and values.max_tokens > settings.max_output_tokens:
        raise ValueError("Output tokens exceed model limit")
    if thinking_level != "off" and (
        not settings.supports_reasoning
        or provider.protocol not in ("openai", "openai-responses")
        or thinking_level not in ("low", "medium", "high")
    ):
        raise ValueError("Thinking level is not supported by this model/protocol")
    if provider.protocol == "anthropic":
        if settings.supports_reasoning or settings.token_field != "max_tokens":
            raise ValueError(
                "Anthropic extended thinking and alternate token fields are not enabled"
            )
        if values.temperature is not None and values.temperature > 1:
            raise ValueError("Anthropic temperature must be at most 1")
        if values.temperature is not None and values.top_p is not None:
            raise ValueError("Set either temperature or Top-P for Anthropic, not both")
        if any(
            value is not None
            for value in (values.frequency_penalty, values.presence_penalty, values.seed)
        ):
            raise ValueError("Penalties and seed are only supported for Chat Completions")
    if provider.protocol == "openai-responses" and any(
        value is not None
        for value in (values.frequency_penalty, values.presence_penalty, values.seed, values.stop)
    ):
        raise ValueError("Responses does not support penalties, seed or stop sequences")
    return values


def model_id_base(name: str) -> str:
    """Keep model names readable while avoiding ambiguous or unsafe URL path segments."""
    value = re.sub(r"[/\\?#%\s\x00-\x1f\x7f]+", "-", name.strip()).lstrip(".")
    return (value or "model")[:MODEL_ID_MAX_LENGTH]
