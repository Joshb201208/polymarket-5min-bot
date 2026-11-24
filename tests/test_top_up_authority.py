import asyncio
from decimal import Decimal

import pytest

from stock_agent.execution import ExecSettings
from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole, TopUpAlreadyToday
from tests.exec_helpers import make_engine


def _thesis(engine, grade=8, *, change="unchanged", direction="BUY", selected=True, timestamp=True,
            source="fixture/model"):
    return engine.ledger.record_thesis(
        "AAA", {"effective_conviction": grade, "raw_conviction": grade,
                "draft": {"thesis_change": change, "direction": direction}},
        ThesisRole.HELD_REVIEW, source=source, activate=selected,
        generated_at=engine.now() if timestamp else None,
    )


def _queue(engine, position, thesis, key):
    [intent_id] = engine.ledger.enqueue_intents([NewIntent(
        "AAA", "buy", key, position_id=position, thesis_ids=(thesis,),
        reference_price=Decimal(50), session_date=engine.session_date(),
        meta={"raw_conviction": 8, "effective_conviction": 8, "last_filled_conviction": 8,
              "thesis_change": "unchanged", "sector": "Industrials"},
    )])
    return engine.ledger.get_intent(intent_id)


@pytest.fixture
def held(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    world.alpaca.seed_position("AAA", 10, 50)
    engine = make_engine(world, tmp_path, monkeypatch, settings=ExecSettings(fill_wait_seconds=10))
    asyncio.run(engine.ledger.refresh("initial holding"))
    asyncio.run(engine.ensure_protection())
    position = engine.ledger.open_position_meta("AAA").position_id
    engine.ledger.set_entry_thesis(position, _thesis(engine, selected=False))
    return engine, position


def test_partial_top_up_uses_one_day_and_survives_restart(held, world, tmp_path, monkeypatch):
    engine, position = held
    world.alpaca.script_partial_fills("AAA", [2])
    first = _queue(engine, position, _thesis(engine, 9), "first")
    fill = asyncio.run(engine.execute_intent(first))
    assert (fill.status, fill.filled_qty) == (IntentStatus.FILLED, 2)
    stops = [o for o in world.alpaca.open_orders("AAA") if o.type == "stop"]
    assert len(stops) == 1 and stops[0].qty == 12

    engine.ledger.store.close()
    engine = make_engine(world, tmp_path, monkeypatch)
    same_day = _queue(engine, position, _thesis(engine, 9), "same-day")
    result = asyncio.run(engine.execute_intent(same_day))
    assert result.status == IntentStatus.SKIPPED and result.code == "top_up_already_today"
    assert len([o for o in world.alpaca.orders.values() if o.side == "buy"]) == 1
    assert [o.id for o in world.alpaca.open_orders("AAA") if o.type == "stop"] == [stops[0].id]

    world.clock.set_et(2026, 11, 24, 10, 0)
    world.price("AAA", 50, spread=0.02)
    weaker_grade = _queue(engine, position, _thesis(engine, 8), "next-day-grade-8")
    result = asyncio.run(engine.execute_intent(weaker_grade))
    assert result.status == IntentStatus.SKIPPED and "top_up_conviction" in result.reason

    next_day = _queue(engine, position, _thesis(engine, 9), "next-day-grade-9")
    result = asyncio.run(engine.execute_intent(next_day))
    assert result.status == IntentStatus.FILLED and result.filled_qty > 0


@pytest.mark.parametrize(("grade", "change", "direction", "binding", "baseline", "reason"), [
    (8, "weakened", "BUY", True, 8, "top_up_thesis_change"),
    (8, "unchanged", "HOLD", True, 8, "top_up_thesis_direction"),
    (7, "unchanged", "BUY", True, 7, "top_up_conviction"),
    (8, "unchanged", "BUY", True, 9, "top_up_conviction"),
    (8, "unchanged", "BUY", False, 8, "position_changed"),
])
def test_top_up_uses_authoritative_theses_and_position(held, world, grade, change, direction, binding, baseline, reason):
    engine, position = held
    engine.ledger.set_entry_thesis(position, _thesis(engine, baseline, selected=False))
    intent = _queue(engine, position if binding else None, _thesis(engine, grade, change=change, direction=direction),
                    "check")
    result = asyncio.run(engine.execute_intent(intent))
    assert result.status == IntentStatus.SKIPPED and reason in (result.code, *result.reason.split(": ")[-1].split("; "))
    assert not [o for o in world.alpaca.orders.values() if o.side == "buy"]


def test_daily_claim_excludes_an_unresolved_order_but_not_a_confirmed_zero_fill(held):
    engine, position = held
    thesis = _thesis(engine)
    ledger = engine.ledger
    first = _queue(engine, position, thesis, "first-order")
    second = _queue(engine, position, thesis, "second-order")
    day = engine.session_date()
    assert ledger.claim_intent(first.intent_id, top_up_session=day).status == IntentStatus.SUBMITTING
    with pytest.raises(TopUpAlreadyToday):
        ledger.claim_intent(second.intent_id, top_up_session=day)
    ledger.ack_intent(first.intent_id, IntentStatus.AMBIGUOUS)
    with pytest.raises(TopUpAlreadyToday):
        ledger.claim_intent(second.intent_id, top_up_session=day)
    ledger.ack_intent(first.intent_id, IntentStatus.CANCELLED, meta={"filled_qty": 0})
    assert ledger.claim_intent(second.intent_id, top_up_session=day).status == IntentStatus.SUBMITTING


@pytest.mark.parametrize(("grade", "direction", "timestamp", "expected_code"), [
    (8, "BUY", True, ""),
    (7, "BUY", True, "risk_size_zero"),
    (8, "HOLD", True, "top_up_thesis_direction"),
    (8, "BUY", False, "risk_size_zero"),
])
def test_ungraded_holding_can_only_establish_a_baseline_with_fresh_native_reaffirmation(
    held, world, grade, direction, timestamp, expected_code,
):
    engine, position = held
    ledger = engine.ledger
    # Even if a placeholder contains a number and is linked, it proves no grade.
    placeholder = ledger.record_thesis("AAA", {"legacy_v0": True, "conviction": 10},
                                      ThesisRole.LEGACY_UNVERIFIED, source="v0")
    ledger.set_entry_thesis(position, placeholder)
    thesis = _thesis(engine, grade, direction=direction, timestamp=timestamp)
    intent = _queue(engine, position, thesis, "initial-baseline")
    result = asyncio.run(engine.execute_intent(intent))
    assert result.code == expected_code
    if expected_code:
        assert ledger.last_filled_thesis(position).thesis_id == placeholder
        assert not [o for o in world.alpaca.orders.values() if o.side == "buy"]
    else:
        assert result.status == IntentStatus.FILLED
        stored = ledger.get_intent(intent.intent_id)
        assert stored.meta["top_up_baseline_provenance"] == "first_fresh_reaffirmation"
        assert stored.meta["top_up_baseline_grade"] == grade
        assert ledger.last_filled_thesis(position).thesis_id == thesis
        events = list(ledger.journal.read({"execution.buy_funnel"}))
        assert any(row.get("stage") == "filled" and row.get("top_up_baseline_grade") == grade
                   and row.get("top_up_baseline_provenance") == "first_fresh_reaffirmation" for row in events)


def test_no_grade_then_first_fresh_fill_at_nine_sets_floor_next_session(held, world):
    engine, position = held
    engine.ledger.set_entry_thesis(position, None)
    world.alpaca.script_partial_fills("AAA", [2])
    first = _queue(engine, position, _thesis(engine, 9), "initial-nine")
    assert asyncio.run(engine.execute_intent(first)).filled_qty == 2
    assert engine.ledger.get_intent(first.intent_id).meta["top_up_baseline_grade"] == 9
    world.clock.set_et(2026, 11, 24, 10, 0)
    world.price("AAA", 50, spread=0.02)
    later = _queue(engine, position, _thesis(engine, 8), "next-eight")
    result = asyncio.run(engine.execute_intent(later))
    assert result.status == IntentStatus.SKIPPED and "top_up_conviction" in result.reason


@pytest.mark.parametrize(("grade", "can_add"), [(8, False), (9, True)])
def test_migrated_verified_entry_conviction_remains_the_floor(held, grade, can_add):
    engine, position = held
    legacy = engine.ledger.record_thesis("AAA", {"legacy_v0": True, "conviction": 9},
                                         ThesisRole.ENTRY_CANDIDATE, source="v0")
    engine.ledger.set_entry_thesis(position, legacy)
    intent = _queue(engine, position, _thesis(engine, grade), "legacy-floor")
    result = asyncio.run(engine.execute_intent(intent))
    assert (result.status == IntentStatus.FILLED) is can_add
    saved = engine.ledger.get_intent(intent.intent_id)
    assert "top_up_baseline_provenance" not in saved.meta
    if not can_add:
        assert "top_up_conviction" in result.reason


def test_ungraded_adoption_requires_native_review_source(held, world):
    engine, position = held
    engine.ledger.set_entry_thesis(position, None)
    intent = _queue(engine, position, _thesis(engine, source="v0"), "not-native")
    result = asyncio.run(engine.execute_intent(intent))
    assert result.status == IntentStatus.SKIPPED and "top_up_conviction" in result.reason
    assert not [o for o in world.alpaca.orders.values() if o.side == "buy"]


def test_unknown_grade_of_existing_native_fill_is_not_reinitialized(held, world):
    engine, position = held
    ledger = engine.ledger
    previous = _queue(engine, position, _thesis(engine, None), "known-fill-unknown-grade")
    ledger.claim_intent(previous.intent_id, top_up_session=engine.session_date())
    ledger.ack_intent(previous.intent_id, IntentStatus.FILLED, meta={"filled_qty": 2})
    world.clock.set_et(2026, 11, 24, 10, 0)
    world.price("AAA", 50, spread=0.02)
    intent = _queue(engine, position, _thesis(engine, 8), "not-a-first-reaffirmation")
    result = asyncio.run(engine.execute_intent(intent))
    assert result.status == IntentStatus.SKIPPED and "top_up_conviction" in result.reason
    assert ledger.top_up_baseline(position) == (None, False)
