"""Immutable static persona configuration. No user profiles or relationship state."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DialogueExample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user: str = Field(min_length=1, max_length=1000)
    assistant: str = Field(min_length=1, max_length=1000)


class PersonaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    profile_id: str = Field(default="moonfox", pattern=r"^[a-z][a-z0-9_-]*$", max_length=80)
    revision: int = Field(default=1, ge=1, le=2147483647)
    name: str = Field(default="月狐", min_length=1, max_length=80)
    identity: str = Field(
        default="你是月狐（Moonfox），一位温和、好奇且可靠的虚构伙伴。",
        min_length=1,
        max_length=4000,
    )
    style: str = Field(
        default="用自然、简洁的中文交流，留意对方的感受；不要为了人设而夸大事实。",
        min_length=1,
        max_length=4000,
    )
    honesty_mode: Literal["roleplay", "honest", "deflect"] = "roleplay"
    examples: tuple[DialogueExample, ...] = Field(default=(), max_length=8)

    @field_validator("name", "identity", "style")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Persona text must not be blank")
        return value

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


def validate_config(raw: str) -> str:
    return PersonaSettings.model_validate_json(raw).model_dump_json()
