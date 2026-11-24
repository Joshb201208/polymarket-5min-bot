"""Timestamped market-move detection and durable, JSON-safe session state."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

COOLDOWN = timedelta(minutes=30)
MAX_CLAIMS = 3
MAX_QUOTE_AGE = timedelta(seconds=90)
SPY_MOVE = Decimal("0.015")
VIX_MOVE = Decimal(3)


@dataclass(frozen=True)
class Reading:
    value: Decimal
    as_of: datetime


def positive(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def valid_reading(value, as_of, *, now, opens_at) -> Reading | None:
    price = positive(value)
    if (price is None or not isinstance(as_of, datetime) or as_of.utcoffset() is None
            or not opens_at <= as_of <= now or now - as_of > MAX_QUOTE_AGE):
        return None
    return Reading(price, as_of)


def _stamp(raw) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return stamp if stamp.utcoffset() is not None else None


def _signal(kind, reading, base, anchor):
    if base is None:
        return None
    if kind == "spy":
        delta = (reading.value - base) / base
        direction = "up" if delta >= SPY_MOVE else "down" if delta <= -SPY_MOVE else None
        display = f"SPY (S&P 500 proxy) {delta:+.2%}"
    else:
        delta = reading.value - base
        direction = "up" if delta >= VIX_MOVE else None
        display = f"VIX {delta:+.2f} points"
    if direction is None:
        return None
    label = "from the prior regular close" if anchor == "close" else "over the preceding 30-minute window"
    return {"signal": f"{kind}_{direction}_{anchor}", "text": f"{display} {label}"}


def observe(state, *, slot, now, readings, closes, regime, can_claim=True):
    """Compare adjacent slots and consume each direction's first prior-close hit.

    A suppressed signal is never replayed later. Claims count regardless of the
    eventual work outcome. State carries only one exchange session.
    """
    last_slot = state.get("slot", -1)
    if last_slot >= slot:
        return state, ()
    prior = state.get("readings", {})
    crossed = set(state.get("crossed", ()))
    hits = []
    for kind, reading in readings.items():
        before = prior.get(kind, {})
        previous_stamp = _stamp(before.get("as_of"))
        if previous_stamp is not None and reading.as_of <= previous_stamp:
            continue
        if last_slot == slot - 1 and previous_stamp is not None:
            hit = _signal(kind, reading, positive(before.get("value")), "slot")
            if hit:
                hits.append(hit)
        hit = _signal(kind, reading, positive(closes.get(kind)), "close")
        if hit and hit["signal"] not in crossed:
            crossed.add(hit["signal"])
            hits.append(hit)
    previous_regime = state.get("regime") if last_slot == slot - 1 else None
    anchor_regime = state.get("regime_anchor")
    if regime:
        change = previous_regime is not None and regime != previous_regime
        anchor_key = f"macro_close:{anchor_regime}:{regime}"
        first_from_anchor = bool(anchor_regime and regime != anchor_regime and anchor_key not in crossed)
        if first_from_anchor:
            crossed.add(anchor_key)
        if change or first_from_anchor:
            before = previous_regime if change else anchor_regime
            suffix = "" if change else " since the last regular window of the prior trading day"
            hits.append({"signal": "regime_change", "text": f"Macro regime: {before} to {regime}{suffix}"})
    claims = dict(state.get("claims", {}))
    last_claim = _stamp(state.get("last_claim"))
    admitted = bool(can_claim and hits and len(claims) < MAX_CLAIMS
                    and (last_claim is None or now - last_claim >= COOLDOWN))
    if admitted:
        claims[str(slot)] = hits
    return {
        "slot": slot,
        "readings": {key: {"value": str(r.value), "as_of": r.as_of.isoformat()} for key, r in readings.items()},
        "crossed": sorted(crossed),
        "regime": regime,
        "regime_anchor": anchor_regime,
        "claims": claims,
        "last_claim": now.isoformat() if admitted else state.get("last_claim"),
    }, hits if admitted else ()
