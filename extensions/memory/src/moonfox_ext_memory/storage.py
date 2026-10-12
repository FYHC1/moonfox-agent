"""Construction of the only database path authorized for a managed scope."""

from __future__ import annotations

from pathlib import Path

from moonfox_app.extension_api import ExtensionResources

from .store import MemoryScope, ScopedMemoryStore


def scoped_path(resources: ExtensionResources) -> Path:
    """Use only the host's extension-owned data directory and a fixed filename."""
    directory = resources.data_dir
    if directory.is_symlink():
        raise ValueError("Memory data directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "memory.sqlite3"
    if path.is_symlink():
        raise ValueError("Memory database must not be a symlink")
    return path


def open_scoped_store(resources: ExtensionResources) -> ScopedMemoryStore:
    scope = MemoryScope(resources.domain, resources.binding.scope_id)
    return ScopedMemoryStore(scoped_path(resources), scope)
