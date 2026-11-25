import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from stock_agent.execution import ExecutionEngine
from stock_agent.ledger import IntentStatus, NewIntent, NewStockEntryRefused, ThesisRole
from stock_agent.research_focus import FocusItem
from stock_agent.research_gate import ResearchGate
from stock_agent.risk_manager import RiskControls
from stock_agent.scheduler import StockAgentScheduler
from tests.exec_helpers import make_engine
from tests.test_risk_projection import NOW, buy, margin_book, order, snapshot, stock


@pytest.fixture
def setup(world, tmp_path, monkeypatch, request):
    world.clock.set_et(*getattr(request, "param", (2026, 11, 23, 10, 0)))
    world.price("NVDA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    now = eng.now()
    added = now.date() - timedelta(days=1)
    directive = FocusItem("NVDA", None, "synthetic", added, added + timedelta(days=3), stance="bullish",
                          owner_conviction=8)
    focus = {"NVDA": directive}
    monkeypatch.setattr("stock_agent.research_gate.research_focus.owner_backed", lambda **_: focus)
    health = SimpleNamespace(research_entry_blockers=lambda *_: ())
    eng._research_gate = ResearchGate(eng.ledger, health, eng.config)
    fields = {
        "adjustment_reason": "owner_directive", "owner_directive_added": added.isoformat(),
        "owner_conviction": 8, "raw_conviction": 3, "effective_conviction": 8,
        "signal_at": now.isoformat(),
    }
    thesis_id = eng.ledger.record_thesis(
        "NVDA", dict(fields, draft={"symbol": "NVDA", "direction": "BUY"}, generated_at=now.isoformat(),
                     inputs_as_of=now.isoformat(), job="prescan"), ThesisRole.ENTRY_CANDIDATE,
        source="synthetic/model", decision_id="owner-thesis", generated_at=now, activate=True,
    )

    def enqueue(key, **meta):
        [intent_id] = eng.ledger.enqueue_intents([
            NewIntent("NVDA", "buy", key, thesis_ids=(thesis_id,), reference_price=D(50), origin="prescan",
                      meta=fields | meta),
        ])
        return eng.ledger.get_intent(intent_id)

    return eng, added, focus, enqueue


def test_owner_stock_fill_spends_only_stock_and_stop_out_does_not_rearm(setup, world):
    eng, added, _, enqueue = setup
    assert not eng._research_gate.is_entry_eligible("NVDA", eng.now()).ok
    intent = enqueue("first")
    result = asyncio.run(eng.execute_intent(intent))
    assert result.status == IntentStatus.FILLED, result
    assert eng.ledger.owner_directive_used("NVDA", added)
    assert eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True,
                                                owner_lane="option").ok
    world.price("NVDA", 40, spread=0.02)
    assert world.alpaca.position("NVDA") is None
    world.price("NVDA", 50, spread=0.02)
    second = asyncio.run(eng.execute_intent(enqueue("next", signal_at=eng.now().isoformat())))
    assert second.status == IntentStatus.SKIPPED
    assert "owner_directive_used" in second.reason
    assert eng.ledger.owner_directive_used("NVDA", added)


@pytest.mark.parametrize("change", ["removed", "score", "veto", "throw", "intent_score", "health"])
def test_owner_metadata_never_substitutes_for_binding_proof(setup, world, monkeypatch, change):
    eng, added, focus, enqueue = setup
    intent = enqueue("owner", **({"effective_conviction": 9} if change == "intent_score" else {}))
    if change == "removed":
        focus.clear()
    elif change == "score":
        focus["NVDA"] = replace(focus["NVDA"], owner_conviction=9)
    elif change == "veto":
        assert eng.ledger.latch_owner_directive_veto("NVDA", added)
        assert not eng.ledger.latch_owner_directive_veto("NVDA", added)
        assert not eng.ledger.claim_owner_option("NVDA", added, "struct")
    elif change == "throw":
        def raising(*args, **kwargs):
            raise OSError("test")
        monkeypatch.setattr(eng._research_gate, "is_entry_eligible", raising)
    elif change == "health":
        eng._research_gate.health.research_entry_blockers = lambda *_: ("llm_unavailable",)
    result = asyncio.run(eng.execute_intent(intent))
    assert result.status == (IntentStatus.QUEUED if change in {"health", "throw"} else IntentStatus.SKIPPED)
    world.assert_no_orders()
    assert not eng.ledger.owner_directive_used("NVDA", added)


def test_atomic_owner_claim_persists_pending_and_excludes_a_second_intent(setup):
    eng, added, _, enqueue = setup
    proof = eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True).owner_entry
    first, second = enqueue("a"), enqueue("b")
    session = eng.session_date()

    def claim(intent):
        return eng.ledger.claim_intent(intent.intent_id, new_stock_session=session, new_stock_limit=3,
                                       owner_entry=proof)

    with pytest.raises(NewStockEntryRefused, match="no longer matches"):
        eng.ledger.claim_intent(first.intent_id, new_stock_session=session, new_stock_limit=3,
                                owner_entry=replace(proof, raw_conviction=4))
    assert claim(first).meta["owner_stock_claim"] == added.isoformat()
    with pytest.raises(NewStockEntryRefused, match="already reserved or filled"):
        claim(second)
    eng.ledger.ack_intent(first.intent_id, IntentStatus.AMBIGUOUS)
    assert eng.ledger.owner_directive_used("NVDA", added)
    eng.ledger.ack_intent(first.intent_id, IntentStatus.QUEUED)
    assert eng.ledger.owner_directive_used("NVDA", added)
    assert not eng.ledger.owner_directive_used("NVDA", added, reservation_id=first.intent_id)
    assert claim(first).status == IntentStatus.SUBMITTING


def test_option_reservation_is_independent_and_veto_blocks_its_own_later_rung(setup):
    eng, added, _, _ = setup
    ledger = eng.ledger
    assert ledger.claim_owner_option("NVDA", added, "one")
    assert not ledger.owner_directive_used("NVDA", added)
    assert ledger.owner_directive_used("NVDA", added, lane="option")
    assert ledger.claim_owner_option("NVDA", added, "one")
    assert not ledger.claim_owner_option("NVDA", added, "two")
    assert eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True, owner_lane="option",
                                                reservation_id="one").ok
    ledger.latch_owner_directive_veto("NVDA", added)
    assert not ledger.claim_owner_option("NVDA", added, "one")
    assert not eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True, owner_lane="option",
                                                    reservation_id="one").ok


def test_option_zero_fill_release_is_compare_and_delete_and_never_undoes_veto(setup):
    eng, added, _, _ = setup
    ledger = eng.ledger
    assert ledger.claim_owner_option("NVDA", added, "one")
    assert not ledger.release_owner_option("NVDA", added, "other")
    assert ledger.release_owner_option("NVDA", added, "one")
    assert not ledger.owner_directive_used("NVDA", added, lane="option")
    assert ledger.claim_owner_option("NVDA", added, "two")
    assert not ledger.release_owner_option("NVDA", added, "one")
    ledger.latch_owner_directive_veto("NVDA", added)
    assert not ledger.release_owner_option("NVDA", added, "two")
    assert ledger.owner_directive_used("NVDA", added, lane="option")


@pytest.mark.parametrize("setup", [(2026, 11, 27, 12, 49, 59)], indirect=True)
def test_owner_submit_time_rechecks_half_day_cutoff(setup, world, monkeypatch):
    eng, added, _, enqueue = setup
    original = ExecutionEngine._stop_distance

    async def cross_cutoff(self, *args):
        result = await original(self, *args)
        world.clock.advance(1)
        return result

    monkeypatch.setattr(ExecutionEngine, "_stop_distance", cross_cutoff)
    assert asyncio.run(eng.calendar.market_gate("entry")).allowed
    intent = enqueue("early-close")
    result = asyncio.run(eng.execute_intent(intent))
    assert result.status == IntentStatus.QUEUED
    assert result.code == "market_closed"
    assert "entry closing buffer" in result.reason
    assert eng.ledger.get_intent(intent.intent_id).meta["owner_stock_claim"] == added.isoformat()
    world.assert_no_orders()


def test_owner_stock_missing_after_restart_stays_ambiguous_and_alerts_once(setup, world):
    eng, added, _, enqueue = setup
    proof = eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True).owner_entry
    first = enqueue("crash")
    eng.ledger.claim_intent(first.intent_id, new_stock_session=eng.session_date(), new_stock_limit=3, owner_entry=proof)
    asyncio.run(eng.resume())
    asyncio.run(eng.resolve_pending())
    assert eng.ledger.get_intent(first.intent_id).status == IntentStatus.AMBIGUOUS
    assert eng.ledger.owner_directive_used("NVDA", added)
    assert sum("owner stock order outcome unknown" in title for _, title, _ in eng.alerts) == 1
    second = asyncio.run(eng.execute_intent(enqueue("duplicate")))
    assert second.status == IntentStatus.SKIPPED and "owner_directive_used" in second.reason
    world.assert_no_orders()


@pytest.mark.parametrize("filled_qty,remains_used", [(0, False), (1, True)])
def test_owner_stock_only_confirmed_zero_fill_frees_claim(setup, filled_qty, remains_used):
    eng, added, _, enqueue = setup
    proof = eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True).owner_entry
    first = enqueue("fill-check")
    eng.ledger.claim_intent(first.intent_id, new_stock_session=eng.session_date(), new_stock_limit=3, owner_entry=proof)
    eng.ledger.ack_intent(first.intent_id, IntentStatus.SUBMITTED)
    eng.ledger.ack_intent(first.intent_id, IntentStatus.CANCELLED, meta={"filled_qty": filled_qty})
    assert eng.ledger.owner_directive_used("NVDA", added) is remains_used


@pytest.mark.parametrize("direction, change, eligible", [("HOLD", "unchanged", True), ("HOLD", "weakened", False),
                                                         ("EXIT_PROPOSED", "unchanged", False)])
def test_held_review_authorizes_only_explicit_option_lane_without_grade_leakage(setup, direction, change, eligible):
    eng, added, _, _ = setup
    now = eng.now()
    eng.ledger.record_thesis("NVDA", {
        "owner_option_directive_added": added.isoformat(), "owner_option_conviction": 8,
        "raw_conviction": 3, "effective_conviction": 3, "generated_at": now.isoformat(),
        "inputs_as_of": now.isoformat(), "job": "prescan",
        "draft": {"symbol": "NVDA", "direction": direction, "thesis_change": change},
    }, ThesisRole.HELD_REVIEW, activate=True)
    gate = eng._research_gate
    ordinary = gate.is_entry_eligible("NVDA", now, allow_owner=True)
    assert not ordinary.ok and ordinary.owner_entry is None and ordinary.effective_conviction == 3
    option = gate.is_entry_eligible("NVDA", now, allow_owner=True, owner_lane="option")
    assert option.ok is eligible
    if eligible:
        assert option.owner_entry.lane == "option" and option.raw_conviction == 3 and option.effective_conviction == 8
        prior_session = gate.is_entry_eligible("NVDA", now + timedelta(days=1), allow_owner=True, owner_lane="option")
        assert not prior_session.ok and "owner_directive_requires_same_session_review" in prior_session.reasons


def test_owner_risk_scope_and_protective_trim_uses_raw_model_grade(setup):
    eng, _, _, _ = setup
    proof = eng._research_gate.is_entry_eligible("NVDA", eng.now(), allow_owner=True).owner_entry
    proof = replace(proof, generated_at=NOW, added=NOW.date(), expires=NOW.date())
    intent = buy("NVDA", qty="1", signal_at=NOW, quote_at=NOW, raw_conviction=3, effective_conviction=8)
    controls = RiskControls(owner_entry=proof)
    empty = snapshot()
    assert eng.risk.evaluate_trade(empty, intent, controls=controls).ok
    assert eng.risk.evaluate_trade(empty, intent).reasons == ("conviction_floor",)
    assert "owner_directive_scope_mismatch" in eng.risk.evaluate_trade(
        replace(empty, open_orders=(order("NVDA", qty="1", done="0"),)), intent, controls=controls,
    ).reasons
    assert "owner_directive_scope_mismatch" in eng.risk.evaluate_trade(
        replace(empty, holdings=(stock("NVDA", 1, "Technology"),)), intent, controls=controls,
    ).reasons
    owner = stock("NVDA", 100, "Technology")
    peer = stock("PEER", 100, "Energy")
    trim_book = margin_book(owner, peer, maintenance="85001", regt="5000")
    now = eng.now()
    eng.ledger.record_thesis("PEER", {"effective_conviction": 7}, ThesisRole.HELD_REVIEW, source="synthetic/model",
                             decision_id="peer", generated_at=now, activate=True)
    scheduler_context = SimpleNamespace(ledger=eng.ledger, config=eng.config, options=None)
    trim_controls = StockAgentScheduler._margin_controls(scheduler_context, trim_book)
    assert trim_controls.holding_convictions == {"NVDA": 3, "PEER": 7}
    assert eng.risk.plan_margin_trim(trim_book, controls=trim_controls)[0].symbol == "NVDA"
