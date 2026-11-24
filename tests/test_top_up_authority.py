import asyncio
from decimal import Decimal

import pytest

from stock_agent.execution import ExecSettings
from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole, TopUpAlreadyToday
from tests.exec_helpers import make_engine


def _thesis(engine, grade=8, *, change="unchanged", direction="BUY", selected=True):
    return engine.ledger.record_thesis(
        "AAA", {"effective_conviction": grade, "raw_conviction": grade,
                "draft": {"thesis_change": change, "direction": direction}},
        ThesisRole.HELD_REVIEW, activate=selected,
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
