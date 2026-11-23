"""Day-1 mixed-book clean-up and option delivery, driven through the real
StockAgentScheduler minute by minute (no WeekAgent stand-in).

B3: an oversized long carrying a covered short call is cleaned up at the first
session open + 5: the short call is bought back FIRST, then the long is trimmed
to <= MAX_POSITION_PCT and the rest is protected by one stop.
A6: an option assignment (OPASN + OPTRD) that leaves a long over the cap is
trimmed at the next session open + 5, without waiting for two closes.
"""

from datetime import date, datetime

import pytest

from tests.fakes import ET
from tests.scenarios import ScenarioRunner


def et(*a):
    return datetime(*a, tzinfo=ET)


def _equity(alpaca) -> float:
    return alpaca.cash + sum(p.qty * alpaca.quotes[s].last * (100 if len(s) > 10 else 1)
                             for s, p in alpaca.positions.items())


def _orders(alpaca, symbol=None):
    rows = alpaca.orders.values() if isinstance(alpaca.orders, dict) else alpaca.orders
    return sorted((o for o in rows if symbol is None or o.symbol == symbol), key=lambda o: o.created_at)


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_day1_closes_short_call_before_trimming_oversized_long(world):
    a = world.alpaca
    a.cash = 50_000
    world.price("HHH", 100.0, spread=0.02)
    a.seed_position("HHH", 400, 90.0)                       # ~44% of equity
    call = a.add_option_contract("HHH", date(2026, 12, 24), "call", 110)
    a.set_option_quote(call.symbol, 0.9, 1.1)
    a.seed_position(call.symbol, -1, 1.5)                   # covers 100 of the shares

    runner = ScenarioRunner(world)
    runner.run_until(et(2026, 11, 23, 9, 34))
    assert not [o for o in _orders(a) if o.side == "buy" or o.type != "stop"], "nothing trades before open+5"
    runner.run_until(et(2026, 11, 23, 11, 0))
    assert not runner.errors, runner.summary()

    state = runner.agent.engine._norm_state()
    assert state["state"] == "done", state
    buy_back = [o for o in _orders(a, call.symbol) if o.side == "buy" and o.status == "filled"]
    trims = [o for o in _orders(a, "HHH") if o.side == "sell" and o.type in ("limit", "market")
             and o.status == "filled"]
    assert len(buy_back) == 1 and len(trims) == 1
    assert buy_back[0].filled_at <= trims[0].created_at, "short call must be closed before the trim is sent"
    assert buy_back[0].created_at >= et(2026, 11, 23, 9, 35)
    assert call.symbol not in a.positions
    held = a.positions["HHH"].qty
    assert held * 100.0 <= 0.15 * _equity(a) + 1e-6
    (stop,) = [o for o in a.open_orders("HHH") if o.type == "stop"]
    assert stop.qty == held and stop.side == "sell"


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_assignment_over_cap_is_trimmed_next_session_open(world):
    a = world.alpaca
    a.cash = 90_000
    world.price("ALFA", 100.0, spread=0.02)
    a.seed_position("ALFA", 100, 100.0)                     # 10% of equity
    put = a.add_option_contract("ALFA", date(2026, 12, 18), "put", 100)    # a lone short leg is repaired at once
    a.set_option_quote(put.symbol, 1.9, 2.1)
    a.seed_position(put.symbol, -1, 2.0)
    wing = a.add_option_contract("ALFA", date(2026, 12, 18), "put", 95)   # defined risk: a put credit spread
    a.set_option_quote(wing.symbol, 0.4, 0.6)
    a.seed_position(wing.symbol, 1, 0.5)

    runner = ScenarioRunner(world)
    runner.at(et(2026, 11, 23, 16, 20), lambda w, r: w.alpaca.assign_early(put.symbol), "assign ALFA put")
    runner.run_until(et(2026, 11, 23, 17, 0))
    assert a.positions["ALFA"].qty == 200                   # delivery leaves ALFA ~20%
    runner.run_until(et(2026, 11, 24, 9, 34))
    assert not [o for o in _orders(a, "ALFA") if o.side == "sell" and o.type != "stop"
                and o.created_at >= et(2026, 11, 23, 16, 0)], "no trim before open+5"
    runner.run_until(et(2026, 11, 24, 10, 30))
    assert not runner.errors, runner.summary()

    trims = [o for o in _orders(a, "ALFA") if o.side == "sell" and o.type in ("limit", "market")
             and o.status == "filled"]
    assert len(trims) == 1 and trims[0].created_at >= et(2026, 11, 24, 9, 35), [
        (o.created_at, o.type, o.qty, o.status) for o in _orders(a, "ALFA")]
    held = a.positions["ALFA"].qty
    assert held * 100.0 <= 0.15 * _equity(a) + 1e-6 and held == 150
    (stop,) = [o for o in a.open_orders("ALFA") if o.type == "stop"]
    assert stop.qty == held
    (intent,) = [i for i in runner.agent.ledger.list_intents() if i.origin == "option_delivery"]
    assert intent.kind == "trim"
