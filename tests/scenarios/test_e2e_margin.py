"""A13 margin on the real StockAgentScheduler, minute by minute (fake broker
margin model: tests/fakes/alpaca.py maintenance_margin/_regt_buying_power).

Gap-down: a levered book (gross ~196% of equity, some 3x ETFs at 75%
maintenance) opens 6% lower, under the 7% stops. Margin excess falls under
MARGIN_TRIM_EXCESS_PCT (15%) -> one critical alert, trims back to at least
MARGIN_MIN_EXCESS_PCT (25%), and every remaining long keeps a stop for its
full quantity.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from tests.fakes import ET
from tests.scenarios import ScenarioRunner

_STOCKS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ")
_ETFS = ("TQQQ", "SOXL", "UPRO", "TNA")                    # 3x leveraged ETFs (75% maintenance)


def et(*a):
    return datetime(*a, tzinfo=ET)


def _excess(a) -> float:
    eq = a.equity()
    return (eq - a.maintenance_margin()) / eq


def _levered_book(world):
    a = world.alpaca
    for sym in _STOCKS:                                 # 10 x 14.5k
        world.price(sym, 100.0, spread=0.02)
        a.seed_position(sym, 145, 100.0)
    for sym in _ETFS:                                   # 4 x 12.75k
        a.add_asset(sym, leverage=3)
        world.price(sym, 50.0, spread=0.02)
        a.seed_position(sym, 255, 50.0)
    a.cash = 100_000 - 145 * 100 * len(_STOCKS) - 255 * 50 * len(_ETFS)   # equity 100k, debit 96k
    return a


def _gap_down_day(world):
    a = _levered_book(world)
    assert a.cash < 0 and 0.15 < _excess(a) < 0.25                     # levered, entry-locked, no trim yet
    runner = ScenarioRunner(world)
    runner.run_until(et(2026, 11, 23, 9, 0))
    for sym in _STOCKS:                                                 # premarket gap, above the 7% stops
        world.price(sym, 94.0, spread=0.02)
    for sym in _ETFS:
        world.price(sym, 47.0, spread=0.02)
    assert _excess(a) < 0.15
    runner.run_until(et(2026, 11, 23, 12, 0))
    return a, runner


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_gap_down_under_15pct_excess_raises_a_critical_alert(world):
    _, runner = _gap_down_day(world)
    alerts = [m for m in world.alerts_sent() if "Margin buffer breached" in json.dumps(m.json)]
    assert alerts, runner.summary()
    assert "Action Needed" in json.dumps(alerts[0].json)                # critical
    assert alerts[0].at <= et(2026, 11, 23, 9, 31)                      # at the open


@pytest.mark.xfail(strict=True, reason="A13: execution.py:900 reads a13.margin_trim_episode with store.kv_get, "
                   "the scheduler writes it with ledger.risk_state_set (scheduler.py:408), so every margin "
                   "trim is skipped as 'earlier margin episode'. Owner: broker_execution")
@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_gap_down_trims_back_to_25pct_and_keeps_stops_right(world):
    a, runner = _gap_down_day(world)
    log = runner.summary()
    assert _excess(a) >= 0.25, (_excess(a), log)
    sells = [o for o in a.orders.values() if o.side == "sell" and o.type != "stop" and o.status == "filled"]
    assert sells, log
    for sym, p in a.positions.items():
        if p.qty > 0:
            stops = [o for o in a.open_orders(sym) if o.type in ("stop", "stop_limit")]
            assert sum(float(o.qty) for o in stops) == p.qty, (sym, p.qty, [o.qty for o in stops])
            assert all(float(o.stop_price) < a.quotes[sym].last for o in stops), sym
