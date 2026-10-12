"""Explicit settings and secret-reference loading; no global home or ambient startup."""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from .schema import HostSettings


def load_settings(path: Path) -> HostSettings:
    """JSON seed is used only when the host database has no saved settings."""
    return HostSettings.model_validate_json(Path(path).read_text(encoding="utf-8"))


class EnvironmentSecrets:
    """Provider bootstrap can resolve env:NAME; no read API exposes raw secrets."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def resolve(self, reference: str) -> str:
        if not re.fullmatch(r"env:[A-Z][A-Z0-9_]*", reference):
            raise ValueError("Only explicit env:NAME secret references are supported")
        value = self._environment.get(reference[4:])
        if not value:
            raise ValueError("Required secret reference is not configured")
        return value
