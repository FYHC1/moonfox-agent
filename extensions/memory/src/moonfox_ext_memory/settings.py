"""Strict extension configuration and JSON-only command contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MemorySettings(BaseModel):
    """Immutable, explicitly bounded settings; extraction is opt-in."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    version: Literal[1] = 1
    recall: bool = True
    max_results: int = Field(default=6, ge=1, le=20)
    max_reference_tokens: int = Field(default=800, ge=64, le=4096)
    extraction: Literal["off", "model"] = "off"
    extraction_max_tokens: int = Field(default=512, ge=128, le=2048)


class StrictArguments(BaseModel):
    """Base class for tool and management JSON arguments."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ListArguments(StrictArguments):
    query: str = Field(default="", max_length=1000)
    limit: int = Field(default=50, ge=1, le=100)
    include_forgotten: bool = False


class GetArguments(StrictArguments):
    id: str = Field(min_length=1, max_length=128)


class RememberArguments(StrictArguments):
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "preference", "profile"] = "fact"
    subject: str = Field(default="conversation", min_length=1, max_length=128)


class UpdateArguments(StrictArguments):
    id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "preference", "profile"] = "fact"
    subject: str = Field(default="conversation", min_length=1, max_length=128)


class RecordRevisionArguments(StrictArguments):
    id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class ClearArguments(StrictArguments):
    expected_revision: int = Field(ge=0)


class JobsArguments(StrictArguments):
    pass


class SearchToolArguments(StrictArguments):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=6, ge=1, le=20)


class GetToolArguments(StrictArguments):
    id: str = Field(min_length=1, max_length=128)


class RememberToolArguments(StrictArguments):
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "preference", "profile"] = "fact"
    subject: str = Field(default="conversation", min_length=1, max_length=128)


class ForgetToolArguments(StrictArguments):
    id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
