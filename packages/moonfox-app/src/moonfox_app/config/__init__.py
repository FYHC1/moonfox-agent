"""Validated immutable application settings; no implicit file/environment reads."""

from .schema import HostSettings, SessionSettings

__all__ = ["HostSettings", "SessionSettings"]
