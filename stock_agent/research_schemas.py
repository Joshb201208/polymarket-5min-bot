"""Public, versioned research contracts. Model output is not an order."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Direction(StrEnum):
    BUY = "BUY"
    BEARISH = "BEARISH"
    HOLD = "HOLD"
    AVOID = "AVOID"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXIT_PROPOSED = "EXIT_PROPOSED"


class ResearchTask(StrEnum):
    THESIS = "thesis"
    REVIEW = "review"
    NEWS = "news"
    CONFIRM_EXIT = "confirm_exit"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Evidence(StrictModel):
    evidence_id: str
    url: str
    title: str
    published_at: datetime | None
    retrieved_at: datetime
    publisher: str
    snippet: str


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=700)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class ThesisDraft(StrictModel):
    symbol: str = Field(min_length=1, max_length=15)
    direction: Direction
    conviction: int = Field(ge=1, le=10)
    summary: str = Field(min_length=1, max_length=1000)
    bull_case: str = Field(min_length=1, max_length=700)
    bear_case: str = Field(min_length=1, max_length=700)
    claims: list[Claim] = Field(max_length=8)
    catalysts: list[str] = Field(max_length=6)
    risks: list[str] = Field(max_length=6)
    thesis_break: str | None
    thesis_change: Literal["new", "strengthened", "unchanged", "weakened", "broken"]
    target_price: float | None = Field(gt=0)
    horizon_days: int = Field(ge=1, le=180)


class ExitConfirmation(StrictModel):
    symbol: str = Field(min_length=1, max_length=15)
    verdict: Literal["CONFIRM_BREAK", "DISAGREE", "INSUFFICIENT_EVIDENCE"]
    conviction: int = Field(ge=1, le=10)
    reason: str = Field(min_length=1, max_length=700)
    evidence_ids: list[str] = Field(max_length=8)


class ThesisRecord(StrictModel):
    """Ledger payload; ledger supplies the identifier and activation state."""

    draft: ThesisDraft
    raw_conviction: int = Field(ge=1, le=10)
    effective_conviction: int = Field(ge=1, le=10)
    adjustment_reason: str
    provider: str
    model: str
    prompt_version: str
    generated_at: datetime
    inputs_as_of: datetime
    evidence: list[Evidence]
    input_hash: str
    exit_confirmation_degraded: bool = False


def strict_json(value_type: type[StrictModel], text: str) -> StrictModel:
    """Reject duplicate properties and non-standard numbers before Pydantic."""
    def object_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate JSON property")
            obj[key] = value
        return obj

    def invalid_constant(_value):
        raise ValueError("non-finite JSON constant")

    json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    return value_type.model_validate_json(text)


def utc_datetime(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.astimezone(UTC) if isinstance(value, datetime) and value.tzinfo else None


def transport_schema(value_type: type[StrictModel]) -> dict:
    """Portable strict transport schema. Pydantic checks all local bounds."""
    def visit(node):
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {key: visit(value) for key, value in node.items() if key not in {
            "title", "description", "default", "format", "minimum", "maximum",
            "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
            "minItems", "maxItems", "const",
        }}
        if "const" in node:
            out["enum"] = [node["const"]]
        if "properties" in node:
            # Property names are data, not schema keywords.
            out["properties"] = {key: visit(value) for key, value in node["properties"].items()}
            out["required"] = list(node["properties"])
            out["additionalProperties"] = False
        return out

    return visit(value_type.model_json_schema())
