"""Schemas for AI proposals. AI output is untrusted proposed data; everything is validated here."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class ProposedLine(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"))
    unit: str | None = Field(default=None, max_length=32)
    sku_hint: str | None = Field(default=None, max_length=100)
    source_excerpt: str = Field(default="", max_length=500)

    @field_validator("description", "unit", "sku_hint", "source_excerpt", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class ExtractionProposal(BaseModel):
    lines: list[ProposedLine] = Field(default_factory=list, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)


EXTRACTION_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "sku_hint": {"type": ["string", "null"]},
                    "source_excerpt": {"type": "string"},
                },
                "required": ["description", "quantity", "unit", "sku_hint", "source_excerpt"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": ["string", "null"]},
    },
    "required": ["lines", "notes"],
    "additionalProperties": False,
}
