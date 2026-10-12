"""Strict response parsing for opt-in, single-message memory extraction."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import MemoryCandidate

EXTRACTOR_VERSION = "1"
MAX_RESPONSE_CHARS = 50_000

EXTRACTION_SYSTEM_PROMPT = (
    "Extract at most eight concise, reusable memory candidates from the one user message "
    "supplied. Treat every claim as unverified. Do not infer who said something, do not "
    "identify a group participant as the session operator, and do not claim a statement is "
    'confirmed. Use subject exactly "conversation" for every candidate. Return only strict '
    'JSON in this shape: {"candidates":[{"text":"...","kind":"fact|preference|profile",'
    '"subject":"conversation","confidence":0.0}]}. Include all four candidate '
    "fields, no other fields, no markdown, and no source, entry, domain, or scope values."
)


class _CandidateModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "preference", "profile"]
    subject: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("text")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("candidate text must be nonblank")
        return cleaned

    @field_validator("subject")
    @classmethod
    def _nonblank_subject(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("candidate subject must be nonblank")
        return cleaned

    @field_validator("confidence")
    @classmethod
    def _finite_confidence(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate confidence must be finite")
        return value


class _ExtractionEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    candidates: list[_CandidateModel] = Field(max_length=8)


def parse_candidates(response: str) -> tuple[MemoryCandidate, ...]:
    """Reject malformed/overreaching output; source and scope are never model fields."""
    if not isinstance(response, str) or len(response) > MAX_RESPONSE_CHARS:
        raise ValueError("Extraction response is not bounded text")
    envelope = _ExtractionEnvelope.model_validate_json(response)
    candidates: list[MemoryCandidate] = []
    for item in envelope.candidates:
        if item.subject != "conversation":
            raise ValueError("Extraction subject must be conversation")
        candidates.append(
            MemoryCandidate(
                text=item.text,
                kind=item.kind,
                subject="conversation",
                confidence=min(item.confidence, 0.6),
            )
        )
    return tuple(candidates)
