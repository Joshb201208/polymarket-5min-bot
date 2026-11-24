"""V1 restore Part 2: the submit-time research recheck must test ``Eligibility.ok``.

Every reason the gate can return is classified, and healthy queued buys (queued 08:00,
submitted 09:35; a weekend handoff submitted Monday) still go through: an old news or
quote proof is refreshed at submit instead of rejecting the buy.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole
from stock_agent.research_schemas import Direction
from stock_agent.research_gate import Eligibility
from tests.exec_helpers import make_engine


def run(coro):
    return asyncio.run(coro)


NEWS_TTL = timedelta(minutes=20)  # ResearchSources._NEWS_TTL


class ProofGate:
    """Fake gate that ages its news proof like ResearchSources and can add fixed reasons."""

    def __init__(self, clock, *, thesis_id="th-1", direction=Direction.BUY, extra=(), news_at=None):
        self.clock, self.thesis_id, self.direction, self.extra = clock, thesis_id, direction, tuple(extra)
        self.news_at = news_at
        self.refreshes = 0
        self.refresh_works = True

    def is_entry_eligible(self, symbol, now):
        reasons = list(self.extra)
        if self.news_at is None or now - self.news_at > NEWS_TTL:
            reasons.append("news_stale_or_unavailable")
        return Eligibility(not reasons, tuple(reasons), self.thesis_id, 8, 8, self.direction, now)

    async def refresh(self, symbol, now):
        self.refreshes += 1
        if not self.refresh_works:
            raise RuntimeError("news provider timeout")
        self.news_at = now


def research_buy(eng, symbol="AAA", *, origin="prescan", thesis=None, ref=50, key=None):
    thesis = thesis or eng.thesis_id
    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol=symbol, side="buy", dedupe_key=key or f"{origin}:{symbol}", reference_price=Decimal(str(ref)),
        origin=origin, thesis_ids=(thesis,), sleeve="core",
        meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology"})])
    return eng.ledger.get_intent(ids[0])


def wired(world, tmp_path, monkeypatch, **gate_kw):
    eng = make_engine(world, tmp_path, monkeypatch)
    eng.thesis_id = eng.ledger.record_thesis("AAA", {"conviction": 8, "draft": {"symbol": "AAA", "direction": "BUY"}},
                                             ThesisRole.ENTRY_CANDIDATE, source="test", activate=True)
    gate = ProofGate(world.clock.now, thesis_id=eng.thesis_id, **gate_kw)
    eng._research_gate = gate
    eng._research_refresh = gate.refresh
    return eng, gate


def test_queued_0800_buy_submits_at_0935_after_refreshing_old_news_proof(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 8, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now())
    it = research_buy(eng, origin="prescan")
    world.clock.set_et(2026, 11, 30, 9, 35)
    world.price("AAA", 50, spread=0.02)
    res = run(eng.execute_intent(eng.ledger.get_intent(it.intent_id)))
    assert res.status != IntentStatus.SKIPPED, res.reason
    assert gate.refreshes == 1
    assert world.orders_sent()


def test_weekend_handoff_buy_submits_monday_after_refresh(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 29, 18, 0)  # Sunday weekly run
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now())
    it = research_buy(eng, origin="weekly", key="weekly:AAA:2026-11-30")
    world.clock.set_et(2026, 11, 30, 9, 35)
    world.price("AAA", 50, spread=0.02)
    res = run(eng.execute_intent(eng.ledger.get_intent(it.intent_id)))
    assert res.status != IntentStatus.SKIPPED, res.reason
    assert gate.refreshes == 1


def test_fresh_proof_needs_no_refresh(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 9, 35)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now() - timedelta(minutes=5))
    res = run(eng.execute_intent(research_buy(eng)))
    assert res.status != IntentStatus.SKIPPED, res.reason
    assert gate.refreshes == 0


def test_refresh_failure_holds_and_retries_instead_of_skipping(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 9, 35)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=None)
    gate.refresh_works = False
    res = run(eng.execute_intent(research_buy(eng)))
    assert res.status == IntentStatus.QUEUED and res.code == "research_inputs_pending"
    world.assert_no_orders()
    gate.refresh_works = True  # provider recovers later in the session
    world.clock.set_et(2026, 11, 30, 9, 45)
    world.price("AAA", 50, spread=0.02)
    res = run(eng.execute_intent(eng.ledger.get_intent(res.intent_id)))
    assert res.status != IntentStatus.SKIPPED, res.reason
    assert world.orders_sent()


@pytest.mark.parametrize("reason", ["pending_exit", "confirmed_exit_pending", "broker_book_blocked",
                                    "exit_confirmation_pending",
                                    "conviction_below_floor_or_invalid", "stale_or_invalid_thesis",
                                    "invalid_clock"])
def test_buy_direction_with_matching_thesis_but_not_ok_is_refused(world, tmp_path, monkeypatch, reason):
    """The second red repro: direction BUY + matching thesis used to pass even when ok=False."""
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now(), extra=(reason,))
    res = run(eng.execute_intent(research_buy(eng)))
    assert res.status == IntentStatus.SKIPPED and res.code == "research_stale"
    assert reason in res.reason
    world.assert_no_orders()


def test_hard_reason_still_refuses_after_news_refresh(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=None, extra=("conviction_below_floor_or_invalid",))
    res = run(eng.execute_intent(research_buy(eng)))
    assert res.status == IntentStatus.SKIPPED and res.code == "research_stale"
    assert "news_stale" not in res.reason and gate.refreshes == 1
    world.assert_no_orders()


@pytest.mark.parametrize("reason", ["broker_book_unavailable", "research_health_unavailable",
                                    "quote_stale_or_unavailable"])
def test_transient_reasons_hold_not_skip(world, tmp_path, monkeypatch, reason):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, _ = wired(world, tmp_path, monkeypatch, news_at=world.clock.now(), extra=(reason,))
    res = run(eng.execute_intent(research_buy(eng)))
    assert res.status == IntentStatus.QUEUED and res.code == "research_inputs_pending"
    world.assert_no_orders()


def test_model_cooldown_holds_then_buys_when_it_clears(world, tmp_path, monkeypatch):
    """ok=False is never overridden; a model cooldown is transient, so the buy waits, not dies."""
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now(), extra=("llm_unavailable",))
    res = run(eng.execute_intent(research_buy(eng)))
    assert res.status == IntentStatus.QUEUED and res.code == "research_inputs_pending"
    world.assert_no_orders()
    gate.extra = ()
    world.clock.set_et(2026, 11, 30, 10, 6)
    world.price("AAA", 50, spread=0.02)
    gate.news_at = world.clock.now()
    res = run(eng.execute_intent(eng.ledger.get_intent(res.intent_id)))
    assert res.status != IntentStatus.SKIPPED, res.reason
    assert world.orders_sent()


def test_market_move_rescan_buy_needs_explicit_ok(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, _ = wired(world, tmp_path, monkeypatch, news_at=world.clock.now(),
                   extra=("conviction_below_floor_or_invalid",))
    res = run(eng.execute_intent(research_buy(eng, origin="market_move")))
    assert res.status == IntentStatus.SKIPPED and res.code == "research_stale"
    world.assert_no_orders()


def test_hung_proof_refresh_is_bounded_and_holds(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=None)

    async def hung(symbol, now):
        await asyncio.Event().wait()  # world patches asyncio.sleep; this really hangs

    eng._research_refresh = hung
    monkeypatch.setattr(type(eng), "RESEARCH_REFRESH_TIMEOUT_S", 0.05)
    res = run(asyncio.wait_for(eng.execute_intent(research_buy(eng)), 5))
    assert res.status == IntentStatus.QUEUED and res.code == "research_inputs_pending"
    world.assert_no_orders()


def test_gate_outage_fallback_still_refuses_when_an_exit_is_pending(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, _ = wired(world, tmp_path, monkeypatch, news_at=world.clock.now())

    class Broken:
        def is_entry_eligible(self, *a, **k):
            raise RuntimeError("gate down")

    eng._research_gate = Broken()
    it = research_buy(eng)
    assert run(eng._research_recheck(it, None)) is None  # A14a: degrade, don't block every buy
    real = eng.ledger.current_thesis
    from types import SimpleNamespace as NS
    monkeypatch.setattr(eng.ledger, "current_thesis", lambda s: NS(
        thesis_id=eng.thesis_id, actionable=True, payload={"draft": {"direction": "HOLD"}}))
    assert "not an actionable BUY" in run(eng._research_recheck(it, None))
    monkeypatch.setattr(eng.ledger, "current_thesis", lambda s: NS(
        thesis_id=eng.thesis_id, actionable=False, payload={"draft": {"direction": "BUY"}}))
    assert "not an actionable BUY" in run(eng._research_recheck(it, None))
    monkeypatch.setattr(eng.ledger, "current_thesis", real)
    from types import SimpleNamespace
    monkeypatch.setattr(eng.ledger, "list_intents",
                        lambda **k: [SimpleNamespace(side="sell", terminal=False)])
    assert "exit is pending" in run(eng._research_recheck(it, None))


@pytest.mark.parametrize(("origin", "is_handoff"), [("weekly", True), ("market_move", False)])
def test_new_entry_producer_holds_when_gate_raises_and_can_retry(world, tmp_path, monkeypatch, origin, is_handoff):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now())
    it = research_buy(eng, origin=origin)
    if is_handoff:
        it = eng.ledger.ack_intent(it.intent_id, IntentStatus.QUEUED, meta={"scheduled_handoff": True})

    class Broken:
        def is_entry_eligible(self, *args):
            raise RuntimeError("unavailable")

    eng._research_gate = Broken()
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.QUEUED and res.code == "research_inputs_pending"
    world.assert_no_orders()
    eng._research_gate = gate
    world.clock.set_et(2026, 11, 30, 10, 6)
    world.price("AAA", 50, spread=0.02)
    res = run(eng.execute_intent(eng.ledger.get_intent(it.intent_id)))
    assert res.status == IntentStatus.FILLED
    assert len([req for req in world.orders_sent() if req.json.get("side") == "buy"]) == 1


def test_skipped_research_buy_reaches_blocked_buy_alert(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, _ = wired(world, tmp_path, monkeypatch, news_at=world.clock.now(), extra=("pending_exit",))
    calls = []

    class Notifier:
        def alert_blocked_buy(self, symbol, code, reason="", **kw):
            calls.append((symbol, code, kw.get("session_date")))
            return True

    eng._notifier = Notifier()
    run(eng.execute_intent(research_buy(eng)))
    assert calls and calls[0][:2] == ("AAA", "research_stale") and calls[0][2] is not None


def test_newer_thesis_or_non_buy_direction_refused(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now())
    it = research_buy(eng, key="a")
    gate.thesis_id = "th-2"
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.SKIPPED and "thesis" in res.reason
    gate.thesis_id, gate.direction = eng.thesis_id, Direction.BEARISH
    res = run(eng.execute_intent(research_buy(eng, key="b")))
    assert res.status == IntentStatus.SKIPPED and "BUY" in res.reason
    world.assert_no_orders()


def test_top_up_for_a_closed_position_is_refused(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng, _ = wired(world, tmp_path, monkeypatch, news_at=world.clock.now())
    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol="AAA", side="buy", dedupe_key="topup:AAA", reference_price=Decimal("50"), origin="weekly",
        thesis_ids=(eng.thesis_id,), position_id="pos-gone", sleeve="core",
        meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology"})])
    res = run(eng.execute_intent(eng.ledger.get_intent(ids[0])))
    assert res.status == IntentStatus.SKIPPED and res.code == "position_changed"
    world.assert_no_orders()


def test_every_gate_reason_is_classified():
    """Table: each reason the gate/health can emit is refreshable, transient, ignored or hard."""
    from stock_agent.execution import ExecutionEngine as E

    all_reasons = {
        "invalid_clock", "broker_book_blocked", "broker_book_unavailable", "quote_stale_or_unavailable",
        "news_stale_or_unavailable", "llm_unavailable", "confirmed_exit_pending",
        "research_health_unavailable", "pending_exit", "missing_thesis", "unverified_thesis",
        "non_entry_direction", "conviction_below_floor_or_invalid", "stale_or_invalid_thesis",
    }
    soft = E.RESEARCH_TRANSIENT
    assert "market_move" in E.RESEARCH_ORIGINS
    assert E.RESEARCH_REFRESHABLE <= E.RESEARCH_TRANSIENT
    assert soft <= all_reasons
    hard = all_reasons - soft
    assert hard == {"invalid_clock", "broker_book_blocked", "confirmed_exit_pending", "pending_exit",
                    "missing_thesis", "unverified_thesis", "non_entry_direction",
                    "conviction_below_floor_or_invalid", "stale_or_invalid_thesis"}
    assert "research_inputs_pending" in E.SKIP_CODES


def test_scheduler_wires_refresh_to_research_sources():
    import inspect

    from stock_agent import scheduler

    assert "research_refresh=getattr(self.research_sources, \"refresh_entry_inputs\"" in inspect.getsource(scheduler)


def test_refresh_budget_is_shared_across_due_buys(world, tmp_path, monkeypatch):
    """N due buys with stale proofs cannot stall the tick by N x timeout."""
    import time

    world.clock.set_et(2026, 11, 30, 10, 0)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=None)
    calls = []

    async def hung(symbol, now):
        calls.append(symbol)
        await asyncio.Event().wait()  # world patches asyncio.sleep; this really hangs

    eng._research_refresh = hung
    monkeypatch.setattr(type(eng), "RESEARCH_REFRESH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(type(eng), "RESEARCH_REFRESH_BUDGET_S", 0.3)
    its = []
    for i in range(5):
        its.append(research_buy(eng, key=f"k{i}"))
    t0 = time.monotonic()
    for it in its:
        run(eng._refresh_research_proof(it))
    assert time.monotonic() - t0 < 1.0
    assert len(calls) == 2  # 0.2s + 0.1s, then the budget is spent


def test_risk_refusal_alert_carries_structured_risk_reasons(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 30, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    calls = []

    class Notifier:
        def alert_blocked_buy(self, symbol, code, reason="", **kw):
            calls.append((symbol, code, kw.get("risk_reasons")))
            return True

    eng._notifier = Notifier()
    from tests.exec_helpers import enqueue
    it = enqueue(eng, "AAA", "buy", ref=50)
    eng._skip(it, "risk refused: sector cap", "risk_refused", risk_reasons=["sector_cap"])
    assert calls == [("AAA", "risk_refused", ("sector_cap",))]


def test_all_day_model_outage_gives_one_blocked_buy_alert_at_expiry(world, tmp_path, monkeypatch):
    """Holds never alert; the hold that would cross the close skips once as intent_expired."""
    world.clock.set_et(2026, 11, 30, 15, 40)
    world.price("AAA", 50, spread=0.02)
    eng, gate = wired(world, tmp_path, monkeypatch, news_at=world.clock.now(), extra=("llm_unavailable",))
    calls = []

    class Notifier:
        def alert_blocked_buy(self, symbol, code, reason="", **kw):
            calls.append((symbol, code, reason))
            return True

    eng._notifier = Notifier()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    close = datetime(2026, 11, 30, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol="AAA", side="buy", dedupe_key="prescan:AAA:exp", reference_price=Decimal("50"), origin="prescan",
        thesis_ids=(eng.thesis_id,), sleeve="core", expires_at=close,
        meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology"})])
    statuses = []
    for minute in (40, 45, 50, 56):
        world.clock.set_et(2026, 11, 30, 15, minute)
        world.price("AAA", 50, spread=0.02)
        gate.news_at = world.clock.now()
        res = run(eng.execute_intent(eng.ledger.get_intent(ids[0])))
        statuses.append(res.code)
        if res.status != IntentStatus.QUEUED:
            break
    assert statuses[:-1] and all(c == "research_inputs_pending" for c in statuses[:-1])
    assert statuses[-1] == "intent_expired"
    assert len(calls) == 1 and calls[0][1] == "intent_expired" and "llm_unavailable" in calls[0][2]
    world.assert_no_orders()
