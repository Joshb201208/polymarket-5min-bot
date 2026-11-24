"""A13h/A14: option debits against options buying power; moving-clock entries; research ideas."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS

from stock_agent.options_manager import OptionCandidate
from stock_agent.research_schemas import Direction
from tests.options_support import StubGate, StubRisk, make_manager, seed_chain


def run(coro):
    return asyncio.run(coro)


def _setup(world, tmp_path, **kw):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    return make_manager(world, tmp_path, gate=gate, **kw)


def _option_orders(world):
    return [r for r in world.orders_sent() if (r.json or {}).get("order_class") == "mleg"
            or len(str((r.json or {}).get("symbol", ""))) > 10]


def test_debit_above_options_buying_power_is_skipped_with_reason(world, tmp_path):
    m = _setup(world, tmp_path)
    world.alpaca._options_buying_power = lambda: 50.0
    out = run(m.scan())
    assert not out[0].opened and out[0].reason.startswith("options buying power")
    assert not out[0].final  # retry later; funds may free up
    assert _option_orders(world) == []


def test_debit_balance_with_no_options_buying_power_is_refused_before_the_broker(world, tmp_path):
    m = _setup(world, tmp_path)
    world.alpaca.seed_position("BBB", 1000, 100.0, price=100.0)
    world.alpaca.cash = -5000.0  # a margin debit balance: the broker's options BP is 0
    out = run(m.scan())
    assert not out[0].opened and "options buying power" in out[0].reason
    assert _option_orders(world) == []


def test_debit_balance_does_not_block_when_broker_options_bp_is_enough(world, tmp_path):
    m = _setup(world, tmp_path)
    world.alpaca.seed_position("BBB", 1000, 100.0, price=100.0)
    world.alpaca.cash = -5000.0
    world.alpaca._options_buying_power = lambda: 50_000.0
    out = run(m.scan())
    assert out[0].opened, out[0].reason


def test_pending_unacknowledged_debit_is_deducted(world, tmp_path):
    m = _setup(world, tmp_path)
    world.alpaca._options_buying_power = lambda: 1000.0
    m.structures.append(NS(st=__import__("stock_agent.options_book", fromlist=["StructState"]).StructState.PENDING,
                           open_order_id=None, max_loss=900.0, underlying="ZZZ", source="scan", opened_at=None,
                           story_id=None))
    why = run(m._options_bp_problem(200.0))
    assert why and "reserved for pending option orders" in why
    m.structures.pop()
    assert run(m._options_bp_problem(200.0)) is None


class ClockCheckingRisk(StubRisk):
    def evaluate_trade(self, snapshot, intent, *, now=None, controls=None):
        assert now >= snapshot.as_of, "risk clock earlier than the snapshot"
        assert intent.quote_at is not None and 0 <= (now - intent.quote_at).total_seconds() < 60
        return super().evaluate_trade(snapshot, intent, now=now, controls=controls)


def test_entry_survives_snapshot_stamped_after_scan_start(world, tmp_path):
    risk = ClockCheckingRisk()
    m = _setup(world, tmp_path, risk=risk)

    def late_snapshot():
        world.clock.advance(seconds=2)  # reconcile takes real time
        return NS(as_of=world.clock.now())

    m._snapshot = late_snapshot
    out = run(m.scan())
    assert out[0].opened, out[0].reason
    assert risk.intents[0].quote_at <= world.clock.now()


def test_research_idea_is_handled_once_and_tagged(world, tmp_path):
    m = _setup(world, tmp_path)
    cand = OptionCandidate("AAA", source="research", story_id="research:wk-1")
    out = run(m.scan(candidates=[cand]))
    assert out[0].opened and out[0].story_handled
    assert m.live()[0].source == "research"
    assert m.is_story_handled("research:wk-1")
    again = run(m.scan(candidates=[cand]))
    assert not again[0].opened


def test_orphan_short_leg_repair_still_sends_with_debit_balance(world, tmp_path):
    from tests.fakes import occ_symbol
    from tests.options_support import EXP

    world.clock.set_et(2026, 11, 23, 10, 0)
    a = seed_chain(world)
    long, short = occ_symbol("AAA", EXP, "call", 100), occ_symbol("AAA", EXP, "call", 105)
    a.seed_position(long, 1, 3.0)
    a.seed_position(short, -1, 2.0)
    a.seed_position("BBB", 1000, 100.0, price=100.0)
    a.cash = -5000.0  # margin debit balance
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    world.clock.advance(minutes=1)
    del a.positions[long]
    run(m.reconcile())
    run(m.monitor())
    sent = [r.json for r in world.orders_sent()]
    assert any(b.get("symbol") == short and b.get("side") == "buy" for b in sent), sent
    assert not a.positions.get(short)


def test_options_bp_ignores_margin_buying_power(world, tmp_path):
    m = _setup(world, tmp_path)
    world.alpaca._options_buying_power = lambda: 10.0
    world.alpaca.multiplier = 4  # big intraday margin buying power must not matter
    assert "options buying power" in (run(m._options_bp_problem(500.0)) or "")


def test_level_two_account_falls_back_to_a_long_option(world, tmp_path):
    m = _setup(world, tmp_path)
    world.alpaca.options_level = 2
    assert run(m.broker.get_account()).options_trading_level == 2
    out = run(m.scan())
    assert out[0].opened, out[0].reason
    assert m.live()[0].strategy in ("long_call", "long_put")


def test_underlying_move_between_pricing_and_risk_skips(world, tmp_path):
    m = _setup(world, tmp_path)
    calls = {"n": 0}
    orig = m._spot

    def jumpy(sym):
        calls["n"] += 1
        px, at = orig(sym)
        return (px * 1.05, at) if calls["n"] > 1 else (px, at)

    m._spot = jumpy
    out = run(m.scan())
    assert not out[0].opened and "moved" in out[0].reason and not out[0].final


def test_margin_off_uses_cash_only(world, tmp_path):
    m = _setup(world, tmp_path)
    m._config = NS(MARGIN_ENABLED=False)
    world.alpaca._options_buying_power = lambda: 0.0  # broker figure ignored: old cash rule
    assert run(m._options_bp_problem(500.0)) is None
    world.alpaca.cash = 100.0
    assert "cash" in (run(m._options_bp_problem(500.0)) or "")


def test_debit_closes_are_never_blocked_by_cash_or_buying_power(world, tmp_path):
    from datetime import date

    from tests.fakes import occ_symbol

    world.clock.set_et(2026, 11, 23, 10, 0)
    a = world.alpaca
    world.price("JJJ", 350, spread=0.05)
    a.seed_position("JJJ", 300, 300.0)
    a.add_option_contract("JJJ", date(2026, 12, 11), "call", 340)
    jc = occ_symbol("JJJ", date(2026, 12, 11), "call", 340)
    a.set_option_quote(jc, 12.0, 12.4)
    a.seed_position(jc, -1, 8.0)
    a.cash = -20_000.0  # margin debit balance
    a._options_buying_power = lambda: 0.0
    m = make_manager(world, tmp_path, universe=())
    r = run(m.close_short_call(jc))
    assert r.filled and a.position(jc) is None


def test_falls_back_to_non_marginable_buying_power(world, tmp_path):
    m = _setup(world, tmp_path)
    real = m.broker.get_account

    async def no_options_bp():
        acct = await real()
        return acct.model_copy(update={"options_buying_power": None})

    m.broker.get_account = no_options_bp
    world.alpaca.cash = 300.0  # fake: non_marginable_buying_power = max(cash, 0)
    why = run(m._options_bp_problem(500.0))
    assert why and "non_marginable_buying_power" in why
    assert run(m._options_bp_problem(200.0)) is None
