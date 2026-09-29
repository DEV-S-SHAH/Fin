"""Pydantic schema definitions for Cold-Start JIT Graph RAG triple extraction.

Enforces strict typed taxonomies for financial entities and relationships,
disallowing self-loops, capping evidence quote length, and validating confidence scores.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator


EntityType = Literal[
    "Company",
    "Executive",
    "Supplier",
    "Competitor",
    "RiskFactor",
]

RelationType = Literal[
    "SOURCES_FROM",
    "SERVES_AS",
    "COMPETES_WITH",
    "EXPOSED_TO",
    "LED_DIVISION",
]


class ExtractedEntity(BaseModel):
    """An entity extracted from SEC narrative text."""

    id: str
    name: str
    entity_type: EntityType
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        cleaned = v.strip() if isinstance(v, str) else ""
        if not cleaned:
            raise ValueError("Field cannot be empty or whitespace only")
        return cleaned


class ExtractedRelation(BaseModel):
    """A typed, evidenced relationship between two entities."""

    source_id: str
    target_id: str
    relation: RelationType
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence_quote")
    @classmethod
    def validate_evidence_quote(cls, v: str) -> str:
        cleaned = v.strip() if isinstance(v, str) else ""
        if not cleaned:
            raise ValueError("evidence_quote must be non-empty")
        words = cleaned.split()
        if len(words) > 30:
            raise ValueError(
                f"evidence_quote exceeds maximum 30 words (found {len(words)} words)"
            )
        return cleaned

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0 (got {v})")
        return float(v)

    @model_validator(mode="after")
    def validate_no_self_loop(self) -> ExtractedRelation:
        if self.source_id.strip() == self.target_id.strip():
            raise ValueError(
                f"Self-loops are disallowed: source_id and target_id are both '{self.source_id}'"
            )
        return self


class ExtractionPayload(BaseModel):
    """Complete bundle of extracted entities and relationships."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelation] = Field(default_factory=list)
    rejected_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
