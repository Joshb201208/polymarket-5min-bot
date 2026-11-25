"""Shared, read-only entry proof used by both stock and options execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from stock_agent import research_focus
from stock_agent.research_schemas import Direction, utc_datetime

OWNER_DIRECTIVE = "owner_directive"
OwnerLane = Literal["stock", "option"]
_MARKET_TZ = ZoneInfo("America/New_York")


class GateLedger(Protocol):
    def current_thesis(self, symbol: str): ...
    def owner_directive_used(self, symbol: str, added: date, *, lane: OwnerLane = "stock",
                             reservation_id: str | None = None) -> bool: ...
    def owner_directive_vetoed(self, symbol: str, added: date) -> bool: ...


class GateHealth(Protocol):
    def research_entry_blockers(self, symbol: str, now: datetime) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class OwnerEntryProof:
    """Current gate result for one opening stock or option entry, never serialized in an intent."""

    symbol: str
    added: date
    expires: date
    thesis_id: str
    raw_conviction: int
    effective_conviction: int
    generated_at: datetime
    lane: OwnerLane


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reasons: tuple[str, ...]
    thesis_id: str | None = None
    raw_conviction: int | None = None
    effective_conviction: int | None = None
    direction: Direction | None = None
    generated_at: datetime | None = None
    owner_entry: OwnerEntryProof | None = None

    @property
    def conviction(self) -> int | None:
        return self.effective_conviction


class ResearchGate:
    def __init__(self, ledger: GateLedger, health: GateHealth, config):
        self.ledger = ledger
        self.health = health
        self.config = config

    def is_entry_eligible(self, symbol: str, now: datetime, *, allow_owner: bool = False,
                          owner_lane: OwnerLane = "stock", reservation_id: str | None = None) -> Eligibility:
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
        held_option = allow_owner and owner_lane == "option" and (
            "owner_option_directive_added" in payload or "owner_option_conviction" in payload
        )
        if held_option:
            if (getattr(record, "role", None) != "held_review" or direction not in {Direction.HOLD, Direction.BUY}
                    or draft.get("thesis_change") not in {"unchanged", "strengthened"}):
                reasons.append("owner_option_held_review_ineligible")
        elif direction not in {Direction.BUY, Direction.BEARISH}:
            reasons.append("non_entry_direction")
        choose_floor = getattr(self.health, "extra_conviction_floor", None)
        floor = max(
            8 if payload.get("job") in {"news", "earnings"} else 7,
            choose_floor(utc_now) if choose_floor is not None else 7,
        )
        owner_entry = None
        owner_record = payload.get("adjustment_reason") == OWNER_DIRECTIVE or "owner_directive_added" in payload
        if owner_record or held_option:
            grade = payload.get("owner_option_conviction" if held_option else "owner_conviction")
            added = payload.get("owner_option_directive_added" if held_option else "owner_directive_added")
            if (not allow_owner or owner_lane not in ("stock", "option")
                    or (not held_option and direction is not Direction.BUY)):
                reasons.append("owner_directive_requires_opening_buy")
            elif type(raw) is not int or not 1 <= raw <= 10 or type(grade) is not int or not floor <= grade <= 9:
                reasons.append("conviction_below_floor_or_invalid")
            elif generated is not None:
                if generated.astimezone(_MARKET_TZ).date() != utc_now.astimezone(_MARKET_TZ).date():
                    reasons.append("owner_directive_requires_same_session_review")
                directive = research_focus.owner_backed(now=utc_now).get(symbol)
                if (directive is None or directive.symbol != symbol or directive.stance != "bullish"
                        or payload.get("job") != "prescan" or added != directive.added.isoformat()
                        or directive.owner_conviction != grade
                        or (held_option and owner_record)
                        or (not held_option and (payload.get("adjustment_reason") != OWNER_DIRECTIVE
                                                 or type(effective) is not int or effective != grade))):
                    reasons.append("owner_directive_invalid")
                else:
                    used = getattr(self.ledger, "owner_directive_used", None)
                    vetoed = getattr(self.ledger, "owner_directive_vetoed", None)
                    if vetoed is None or vetoed(symbol, directive.added) is not False:
                        reasons.append("owner_directive_vetoed_or_unavailable")
                    elif used is None or used(symbol, directive.added, lane=owner_lane,
                                             reservation_id=reservation_id) is not False:
                        reasons.append("owner_directive_used_or_unavailable")
                    else:
                        owner_entry = OwnerEntryProof(
                            symbol, directive.added, directive.expires, record.thesis_id, raw, grade, generated,
                            owner_lane,
                        )
                        if held_option:
                            effective = grade
        elif type(raw) is not int or not 7 <= raw <= 10 or type(effective) is not int or not floor <= effective <= 10:
            reasons.append("conviction_below_floor_or_invalid")
        max_age_s = self.config.RESEARCH_THESIS_MAX_AGE_H * 3600
        if (
            generated is None or inputs is None or not inputs <= generated <= utc_now
            or (utc_now - inputs).total_seconds() > max_age_s
        ):
            reasons.append("stale_or_invalid_thesis")
        return Eligibility(not reasons, tuple(reasons), record.thesis_id, raw, effective, direction, generated,
                           owner_entry if not reasons else None)
