"""Public managed-extension definition and supported memory interfaces."""

from __future__ import annotations

import json

from moonfox_app.extension_api import (
    ExtensionManifest,
    ExtensionResources,
    SessionExtensionDefinition,
)

from .extension import MemoryExtension
from .management import COMMANDS, manage
from .settings import MemorySettings
from .store import MemoryScope, ScopedMemoryStore

MANIFEST = ExtensionManifest(
    id="memory",
    version="0.1.0",
    sdk_version_range=">=0.1,<0.2",
    scope="session",
    capabilities=("context", "tools", "events", "tasks", "commands", "models"),
)


def _factory(resources: ExtensionResources, config_json: str) -> MemoryExtension:
    settings = MemorySettings.model_validate_json(config_json)
    return MemoryExtension(resources, settings)


def _validate_config(config_json: str) -> str:
    return MemorySettings.model_validate_json(config_json).model_dump_json()


_DEFAULT_SETTINGS = MemorySettings()
EXTENSION = SessionExtensionDefinition(
    manifest=MANIFEST,
    factory=_factory,
    validate_config=_validate_config,
    config_schema_json=json.dumps(
        MemorySettings.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    ),
    default_config_json=_DEFAULT_SETTINGS.model_dump_json(),
    tool_names=("memory.search", "memory.get", "memory.remember", "memory.forget"),
    commands=COMMANDS,
    manage=manage,
)

__all__ = [
    "EXTENSION",
    "MemorySettings",
    "MemoryScope",
    "ScopedMemoryStore",
    "MemoryExtension",
]
