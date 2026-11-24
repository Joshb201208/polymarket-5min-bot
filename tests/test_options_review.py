"""Independent review repros (strategy_analyst), kept as regression tests."""

import asyncio
from datetime import date

import pytest

from stock_agent.options_book import StructState
from stock_agent.options_policy import ExitReason
from tests.fakes import occ_symbol
from tests.options_support import EXP, StubGate, StubRisk, make_manager, seed_chain


def run(coro):
    return asyncio.run(coro)


def legs(world, expiry=EXP, *, qty=1):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = seed_chain(world, exp=expiry)
    long = occ_symbol("AAA", expiry, "call", 100)
    short = occ_symbol("AAA", expiry, "call", 105)
    a.seed_position(long, qty, 3.0)
    a.seed_position(short, -qty, 2.0)
    return a, long, short


def test_half_day_pin_wont_deliver_shares_during_quote_outage(world, tmp_path):
    exp = date(2026, 11, 27)
    a, _, _ = legs(world, exp)
    a.behaviour.expiry_day_liquidation_time = None
    world.price("AAA", 102, spread=0.02)
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    a.fail("/v1beta1/options/snapshots", "status", method="GET", status=503, times=None)
    for day in (24, 25, 27):
        world.clock.set_et(2026, 11, day, 10, 0)
        run(m.monitor())
    world.clock.set_et(2026, 11, 27, 13, 1)
    assert a.position("AAA") is None


def test_exercise_opxrc_matches_disappearing_leg(world, tmp_path):
    a, long, short = legs(world)
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    world.clock.advance(minutes=1)
    del a.positions[long]
    a.seed_position("AAA", 100, 100)
    a._nta("OPXRC", symbol=long, qty=1)
    a._nta("OPTRD", symbol="AAA", qty=100, price=100, side="buy")
    r = run(m.reconcile())
    assert r.deliveries and m.structures[0].st is StructState.REPAIR
    assert m.structures[0].legs[0].symbol == short


def test_close_evidence_cannot_come_from_before_open_same_day(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = seed_chain(world)
    long = occ_symbol("AAA", EXP, "call", 100)
    a.seed_position(long, 1, 3.0)
    a.submit(dict(symbol=long, side="sell", qty="1", type="market", time_in_force="day",
                  position_intent="sell_to_close", client_order_id="fixture-old-close"))
    world.clock.advance(minutes=1)
    a.seed_position(long, 1, 3.0)
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    world.clock.advance(minutes=1)
    del a.positions[long]  # no closing activity for the new position
    run(m.reconcile())
    assert m.structures[0].st is StructState.UNKNOWN, m.structures[0]


def test_partial_close_tracks_all_fills_for_cooldown(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = seed_chain(world)
    long = occ_symbol("AAA", EXP, "call", 100)
    a.seed_position(long, 2, 3.0)
    m = make_manager(world, tmp_path, universe=(), fill_wait_seconds=10)
    run(m.reconcile())
    st = m.structures[0]
    st.close_attempts = 1
    a.script_partial_fills(long, [1])
    a.set_option_quote(long, 0.50, 0.60)
    run(m._close(st, ExitReason.MANUAL, world.clock.now()))
    assert st.qty == 1 and a.position(long).qty == 1
    assert st.max_loss == 300
    a.set_option_quote(long, 4.00, 4.10)
    run(m._close(st, ExitReason.MANUAL, world.clock.now()))
    assert st.realized_pnl == pytest.approx(-150)
    assert m.cooldowns["AAA"].consecutive_losses == 1


def test_adopted_credit_vertical_counts_defined_max_loss(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = seed_chain(world)
    c100 = occ_symbol("AAA", EXP, "call", 100)
    c105 = occ_symbol("AAA", EXP, "call", 105)
    a.seed_position(c100, -4, 3.0)
    a.seed_position(c105, 4, 2.0)
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    assert m.open_blocker() or m.open_max_loss() == pytest.approx(1600)


def test_open_reservation_stays_at_ladder_ceiling(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    risk = StubRisk()
    m = make_manager(world, tmp_path, gate=gate, risk=risk)
    orig = m.broker.submit_raw_order
    observed = []

    async def submit(body):
        observed.append(m.commitments()[0].cash_reserved)
        return await orig(body)

    monkeypatch.setattr(m.broker, "submit_raw_order", submit)
    out = run(m.scan())
    assert out[0].opened and len(observed) > 1
    assert min(observed) == risk.intents[0].option_cash_debit


def test_unexplained_missing_long_leg_repairs_the_short(world, tmp_path):
    a, long, short = legs(world)
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    world.clock.advance(minutes=1)
    del a.positions[long]  # no record at all
    r = run(m.reconcile())
    st = m.structures[0]
    assert st.st is StructState.REPAIR and [lg.symbol for lg in st.legs] == [short]
    assert st.id in r.repair and not r.unknown
