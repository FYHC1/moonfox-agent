"""Host-owned authorization/configuration boundary for explicitly installed extensions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from moonfox_app.config import SessionSettings
from moonfox_app.extension_api import (
    ExtensionConflictError,
    ExtensionRecordNotFoundError,
    ExtensionResources,
    MeteredModelService,
    SessionBinding,
    SessionExtension,
    SessionExtensionDefinition,
)
from moonfox_app.extensions import ExtensionRegistry

from .errors import ConflictError, InvalidConfiguration, NotFoundError
from .types import SessionRecord


def json_object(value: str, *, maximum: int = 32768) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Extension JSON exceeds the permitted size")
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("Extension payload must be an object")
    json.dumps(data, allow_nan=False)
    return data


def _unbound() -> SessionExtension:
    raise RuntimeError("Managed extension factory needs a host-bound session")


class ManagedExtensions:
    def __init__(self, home: Path, definitions: Sequence[SessionExtensionDefinition]) -> None:
        self._home = home
        self.definitions: dict[str, SessionExtensionDefinition] = {}
        for definition in definitions:
            identity = definition.manifest.id
            if identity in self.definitions or definition.manifest.scope != "session":
                raise ValueError("Managed extensions require unique session manifests")
            if len(set(definition.tool_names)) != len(definition.tool_names) or any(
                not name.startswith(identity + ".") for name in definition.tool_names
            ):
                raise ValueError("Managed tool names must be unique and namespaced")
            if len(set(definition.commands)) != len(definition.commands) or any(
                not name or len(name) > 64 or not name.replace("_", "").isalnum()
                for name in definition.commands
            ):
                raise ValueError("Invalid extension command names")
            if definition.commands and definition.manage is None:
                raise ValueError("Management commands require an implementation")
            json_object(definition.config_schema_json, maximum=65536)
            json_object(definition.validate_config(definition.default_config_json))
            self.definitions[identity] = definition

    def install(self, registry: ExtensionRegistry) -> None:
        for definition in self.definitions.values():
            registry.register(definition.manifest, _unbound)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(name for item in self.definitions.values() for name in item.tool_names)

    def validate(self, settings: SessionSettings) -> None:
        configs = {item.extension_id: item.config_json for item in settings.extension_config}
        if set(configs) - self.definitions.keys():
            raise InvalidConfiguration("Configuration for an unavailable managed extension")
        for identity, definition in self.definitions.items():
            if identity in configs or identity in settings.enabled_extensions:
                try:
                    json_object(
                        definition.validate_config(
                            configs.get(identity, definition.default_config_json)
                        )
                    )
                except ValueError:
                    raise InvalidConfiguration("Invalid extension configuration") from None

    def configuration(self, identity: str, settings: SessionSettings) -> str:
        definition = self.definitions[identity]
        raw = next(
            (
                item.config_json
                for item in settings.extension_config
                if item.extension_id == identity
            ),
            definition.default_config_json,
        )
        return definition.validate_config(raw)

    def resources(
        self, record: SessionRecord, identity: str, models: MeteredModelService | None = None
    ) -> ExtensionResources:
        if identity not in self.definitions:
            raise NotFoundError("Extension is not installed")
        # Scope IDs are opaque, not filesystem paths, even for trusted headless callers.
        folder = hashlib.sha256(record.scope_id.encode("utf-8")).hexdigest()
        return ExtensionResources(
            SessionBinding(record.id, record.scope_id),
            record.domain,
            self._home / "extensions" / identity / record.domain / folder,
            models,
        )

    def describe(self, identity: str) -> dict[str, Any]:
        definition = self.definitions.get(identity)
        if definition is None:
            return {}
        return {
            "config_schema": json_object(definition.config_schema_json, maximum=65536),
            "default_config_json": definition.validate_config(definition.default_config_json),
            "commands": list(definition.commands),
            "tools": list(definition.tool_names),
        }

    async def command(
        self, record: SessionRecord, identity: str, command: str, arguments_json: str
    ) -> str:
        definition = self.definitions.get(identity)
        if definition is None or command not in definition.commands or definition.manage is None:
            raise NotFoundError("Extension management command not found")
        try:
            json_object(arguments_json)
            # Management intentionally receives no model capability and never starts a runtime.
            async with asyncio.timeout(10):
                result = await definition.manage(
                    self.resources(record, identity), command, arguments_json
                )
            json_object(result, maximum=1024 * 1024)
            return result
        except ExtensionConflictError:
            raise ConflictError("Extension record revision changed") from None
        except ExtensionRecordNotFoundError:
            raise NotFoundError("Extension record not found in this session") from None
        except ValueError:
            raise InvalidConfiguration("Invalid extension command") from None
