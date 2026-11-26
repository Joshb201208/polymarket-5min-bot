"""Public, versioned research contracts. Model output is not an order."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Direction(StrEnum):
    BUY = "BUY"
    BEARISH = "BEARISH"
    HOLD = "HOLD"
    AVOID = "AVOID"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXIT_PROPOSED = "EXIT_PROPOSED"


BEST_IDEAS_DIRECTIONS = (Direction.BUY, Direction.BEARISH, Direction.AVOID, Direction.INSUFFICIENT_EVIDENCE)
_NON_ACTIONABLE_DIRECTIONS = (Direction.INSUFFICIENT_EVIDENCE, Direction.AVOID, Direction.HOLD)
_CLAIMS_MAX = 8
_CATALYSTS_MAX = 6
_RISKS_MAX = 6


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
    # For day precision, published_at is the earliest possible UTC instant,
    # not an observed publication time. Preserve old wire/stored rows.
    date_precision: Literal["instant", "day"] = Field(default="instant", exclude_if=lambda value: value == "instant")


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
    claims: list[Claim] = Field(max_length=_CLAIMS_MAX)
    catalysts: list[str] = Field(max_length=_CATALYSTS_MAX)
    risks: list[str] = Field(max_length=_RISKS_MAX)
    thesis_break: str | None
    thesis_change: Literal["new", "strengthened", "unchanged", "weakened", "broken"]
    target_price: float | None = Field(gt=0)
    horizon_days: int = Field(ge=1, le=180)


class OwnerThesisDraft(ThesisDraft):
    direction: Literal[Direction.BUY, Direction.BEARISH, Direction.AVOID]
    stop_price: float | None = Field(gt=0)
    veto_claim_index: int | None = Field(ge=0, le=7)


class ExitConfirmation(StrictModel):
    symbol: str = Field(min_length=1, max_length=15)
    verdict: Literal["CONFIRM_BREAK", "DISAGREE", "INSUFFICIENT_EVIDENCE"]
    conviction: int = Field(ge=1, le=10)
    reason: str = Field(min_length=1, max_length=700)
    evidence_ids: list[str] = Field(max_length=8)


class OwnerSummary(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)


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
    normalization_notes: dict[str, int] = Field(default_factory=dict, exclude_if=lambda notes: not notes)


def _normalize_thesis(value_type: type[ThesisDraft], value: dict) -> dict[str, int]:
    removed = {}
    for name, limit in (("catalysts", _CATALYSTS_MAX), ("risks", _RISKS_MAX)):
        items = value.get(name)
        if isinstance(items, list) and len(items) > limit and all(isinstance(item, str) for item in items):
            removed[name] = len(items) - limit
            value[name] = items[:limit]
    claims = value.get("claims")
    if (value_type is ThesisDraft and value.get("direction") in _NON_ACTIONABLE_DIRECTIONS
            and isinstance(claims, list) and len(claims) <= _CLAIMS_MAX):
        kept = []
        for claim in claims:
            if isinstance(claim, dict) and claim.get("evidence_ids") == []:
                try:
                    # Validate all other fields, including extras and text length,
                    # before dropping a claim. The placeholder is never returned.
                    Claim.model_validate({**claim, "evidence_ids": ["_"]})
                except ValidationError:
                    pass
                else:
                    continue
            kept.append(claim)
        if len(kept) != len(claims):
            removed["claims"] = len(claims) - len(kept)
            value["claims"] = kept
    return removed


def strict_json(value_type: type[StrictModel], text: str, *, normalization_notes: dict[str, int] | None = None) -> StrictModel:
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

    value = json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    if normalization_notes is not None and value_type in (ThesisDraft, OwnerThesisDraft) and isinstance(value, dict):
        removed = _normalize_thesis(value_type, value)
        if removed:
            result = value_type.model_validate_json(json.dumps(value, allow_nan=False))
            normalization_notes.update(removed)
            return result
    return value_type.model_validate_json(text)


def utc_datetime(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.astimezone(UTC) if isinstance(value, datetime) and value.tzinfo else None


_BOUND_LABELS = {
    "minLength": "minimum characters", "maxLength": "maximum characters",
    "minItems": "minimum items", "maxItems": "maximum items",
    "minimum": "minimum inclusive", "maximum": "maximum inclusive",
    "exclusiveMinimum": "greater than", "exclusiveMaximum": "less than",
}


@cache
def thesis_output_limits(value_type: type[ThesisDraft]) -> str:
    schema = value_type.model_json_schema()

    def describe(path, node):
        if "$ref" in node:
            yield from describe(path, schema["$defs"][node["$ref"].rsplit("/", 1)[-1]])
        allowed = node.get("anyOf", ())
        nullable = any(branch.get("type") == "null" for branch in allowed)
        limits = [f"{label}: {part[key]}" for part in (node, *allowed)
                  for key, label in _BOUND_LABELS.items() if key in part]
        if nullable:
            limits.append("null allowed")
        if limits:
            yield f"{path}: {', '.join(limits)}."
        for key, prop in node.get("properties", {}).items():
            yield from describe(f"{path}.{key}" if path else key, prop)
        if "items" in node:
            yield from describe(f"{path}[]", node["items"])

    return "\n".join(describe("", schema))


def transport_schema(value_type: type[StrictModel], *, provider: str = "", best_ideas: bool = False) -> dict:
    """Portable strict transport schema. Pydantic checks all local bounds."""
    thesis = value_type in (ThesisDraft, OwnerThesisDraft)

    def preserve_bound(key, value):
        if not thesis:
            return False
        if provider == "anthropic":
            return key == "minItems" and value in (0, 1)
        return provider == "openai" and key not in {"minLength", "maxLength"}

    def visit(node):
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {key: visit(value) for key, value in node.items()
               if key not in {"title", "description", "default", "format", "const"}
               and (key not in _BOUND_LABELS or preserve_bound(key, value))}
        if "const" in node:
            out["enum"] = [node["const"]]
        if "properties" in node:
            # Property names are data, not schema keywords.
            out["properties"] = {key: visit(value) for key, value in node["properties"].items()}
            out["required"] = list(node["properties"])
            out["additionalProperties"] = False
        return out

    out = visit(value_type.model_json_schema())
    if best_ideas and value_type is ThesisDraft:
        out["$defs"]["Direction"]["enum"] = [direction.value for direction in BEST_IDEAS_DIRECTIONS]
    return out
