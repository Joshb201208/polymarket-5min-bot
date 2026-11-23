"""Shared, read-only entry proof used by both stock and options execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from stock_agent.research_schemas import Direction, utc_datetime


class GateLedger(Protocol):
    def current_thesis(self, symbol: str): ...


class GateHealth(Protocol):
    def research_entry_blockers(self, symbol: str, now: datetime) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reasons: tuple[str, ...]
    thesis_id: str | None = None
    raw_conviction: int | None = None
    effective_conviction: int | None = None
    direction: Direction | None = None
    generated_at: datetime | None = None

    @property
    def conviction(self) -> int | None:
        return self.effective_conviction


class ResearchGate:
    def __init__(self, ledger: GateLedger, health: GateHealth, config):
        self.ledger = ledger
        self.health = health
        self.config = config

    def is_entry_eligible(self, symbol: str, now: datetime) -> Eligibility:
        """Network-free check. Callers independently apply direction and risk rules."""
        utc_now = utc_datetime(now)
        if utc_now is None:
            return Eligibility(False, ("invalid_clock",))
        lookup = getattr(self.health, "research_entry_blockers", None)
        reasons = list(lookup(symbol, utc_now)) if lookup is not None else ["research_health_unavailable"]
        pending = getattr(self.ledger, "list_intents", None)
        if pending is not None and any(
            intent.side == "sell" and not intent.terminal for intent in pending(symbol=symbol)
        ):
            reasons.append("pending_exit")
        record = self.ledger.current_thesis(symbol)
        if record is None:
            return Eligibility(False, (*reasons, "missing_thesis"))

        payload = record.payload
        draft = payload.get("draft")
        if not getattr(record, "actionable", True) or not isinstance(draft, dict) or draft.get("symbol") != symbol:
            return Eligibility(False, (*reasons, "unverified_thesis"))
        raw, effective = payload.get("raw_conviction"), payload.get("effective_conviction")
        generated = utc_datetime(payload.get("generated_at"))
        inputs = utc_datetime(payload.get("inputs_as_of"))
        try:
            direction = Direction(draft.get("direction"))
        except (TypeError, ValueError):
            direction = None
        if direction not in {Direction.BUY, Direction.BEARISH}:
            reasons.append("non_entry_direction")
        choose_floor = getattr(self.health, "extra_conviction_floor", None)
        floor = max(
            8 if payload.get("job") in {"news", "earnings"} else 7,
            choose_floor(utc_now) if choose_floor is not None else 7,
        )
        if type(raw) is not int or not 7 <= raw <= 10 or type(effective) is not int or not floor <= effective <= 10:
            reasons.append("conviction_below_floor_or_invalid")
        max_age_s = self.config.RESEARCH_THESIS_MAX_AGE_H * 3600
        if (
            generated is None or inputs is None or not inputs <= generated <= utc_now
            or (utc_now - inputs).total_seconds() > max_age_s
        ):
            reasons.append("stale_or_invalid_thesis")
        return Eligibility(not reasons, tuple(reasons), record.thesis_id, raw, effective, direction, generated)
