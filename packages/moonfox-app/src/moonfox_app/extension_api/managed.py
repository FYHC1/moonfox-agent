"""Optional managed-extension contracts. No discovery, databases or host implementations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .capabilities import SessionExtension
from .types import ExtensionManifest, SessionBinding


class MeteredModelService(Protocol):
    """Bound to one extension/session. Tickets share the originating input's durable budget.

    A ticket is issued only during a host run. Completion has no tools, does not enter
    the conversation, and must not be automatically retried after an unknown outcome.
    """

    def current_ticket(self) -> str | None: ...

    async def complete(self, ticket: str, *, system: str, text: str, max_tokens: int) -> str: ...


@dataclass(frozen=True, slots=True)
class ExtensionResources:
    """Host-authorized scope, not caller-supplied tool arguments or global storage.

    data_dir belongs only to this extension/domain/scope. Trusted same-process Python
    code is not sandboxed; this contract does not grant a global database connection.
    """

    binding: SessionBinding
    domain: Literal["agent", "qq"]
    data_dir: Path
    models: MeteredModelService | None = None


@dataclass(frozen=True, slots=True)
class SessionExtensionDefinition:
    """Explicitly installed implementation and JSON-only management metadata.

    Configurations and command payloads are bounded JSON object strings, keeping the
    SDK independent of each extension's models. validate_config returns canonical JSON.
    Management never appends chat input and must return a JSON object string.
    """

    manifest: ExtensionManifest
    factory: Callable[[ExtensionResources, str], SessionExtension]
    validate_config: Callable[[str], str]
    config_schema_json: str
    default_config_json: str
    tool_names: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    manage: Callable[[ExtensionResources, str, str], Awaitable[str]] | None = None


class ExtensionConflictError(ValueError):
    """A record/config revision changed; do not silently overwrite it."""


class ExtensionRecordNotFoundError(ValueError):
    """No such record in the authorized scope (never reveal another scope)."""


class ModelRequestRejectedError(RuntimeError):
    """The host rejected a model request before provider invocation; no retry is implied."""
