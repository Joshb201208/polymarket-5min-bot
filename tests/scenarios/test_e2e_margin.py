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
from collections import defaultdict
from datetime import date, datetime, time

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


# ── leveraged week, drawdown tier, margin off ────────────────────────────────
# Equity 100k: eight 10k longs in four sectors and 20k cash; research returns a
# conviction-10 BUY for twelve more names spread over seven sectors. With margin
# on, buys run past 100% gross on borrowed cash and stop at the caps.
_HELD = ("XOM", "CVX", "JPM", "GS", "LLY", "JNJ", "CAT", "HON")
_IDEAS = ("COP", "OXY", "BAC", "WFC", "MRK", "PFE", "DE", "UNP", "AMZN", "TJX", "AMD", "INTC")
_WEEK = date(2026, 11, 23)


def _idea_week(world, monkeypatch, *, margin=True, startup=None):
    from stock_agent.config import Config
    from tests.scenarios.test_e2e_day_one import _providers

    monkeypatch.setattr(Config, "PRIORITY_SYMBOLS", _IDEAS)
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setattr(Config, "MARGIN_ENABLED", margin)
    a = world.alpaca
    a.cash = 20_000
    for sym in _HELD:
        world.price(sym, 100.0, spread=0.02)
        a.seed_position(sym, 100, 100.0)
    for sym in _IDEAS:
        world.price(sym, 100.0, spread=0.02)
    for sym in ("SPY", "QQQ", "VIXY", "TLT", "USO", "UUP"):          # flat: normal regime
        a.seed_daily_bars(sym, [100.0] * 210, end=_WEEK)
        world.price(sym, 100.0, spread=0.02)
    _providers(world, conviction=lambda sym: 10, symbols=_IDEAS)
    low = {"cash": a.cash}
    runner = ScenarioRunner(world, **({"startup": startup} if startup else {}))
    runner.when(lambda w, r: low.__setitem__("cash", min(low["cash"], w.alpaca.cash)) and False,
                lambda w, r: None, once=False)
    runner.run_days(_WEEK, 5, start=time(7, 0), end=time(17, 30))
    return a, runner, low["cash"]


def _bought(a):
    return [o for o in a.orders.values() if o.side == "buy" and o.symbol in _IDEAS and o.status == "filled"]


def _gross(a) -> float:
    return sum(p.qty * a.quotes[s].last for s, p in a.positions.items() if p.qty > 0) / a.equity()


def _assert_leveraged_limits(a):
    from stock_agent.risk_manager import classify_symbol

    eq = a.equity()
    assert _gross(a) <= 2.0 + 1e-6                                  # MAX_GROSS_EXPOSURE_PCT
    longs = {s: p.qty * a.quotes[s].last for s, p in a.positions.items() if p.qty > 0}
    assert len(longs) <= 20
    assert all(v <= 0.15 * eq + 1e-6 for v in longs.values()), longs
    sectors: dict = {}
    for s, v in longs.items():
        sec = classify_symbol(s).sector
        sectors[sec] = sectors.get(sec, 0.0) + v
    assert all(v <= 0.40 * eq + 1e-6 for v in sectors.values()), sectors
    assert _excess(a) >= 0.25, _excess(a)
    assert a._regt_buying_power() >= 0.02 * eq - 1e-6                 # overnight buying-power floor
    return longs


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_leveraged_week_buys_past_100pct_gross_and_stops_at_the_caps(world, monkeypatch):
    from stock_agent.config import Config

    # Isolate the exposure and buying-power controls from the daily pacing control.
    monkeypatch.setattr(Config, "MAX_NEW_STOCK_POSITIONS_PER_SESSION", len(_IDEAS))
    a, runner, low = _idea_week(world, monkeypatch)
    log = runner.summary()
    assert _bought(a), log
    assert low < 0 and _gross(a) > 1.0, (low, _gross(a), log)       # borrowed past 100% gross
    longs = _assert_leveraged_limits(a)
    qtys = [float(o.qty) for o in _bought(a)]
    assert len(longs) == 20 or min(qtys) < max(qtys), qtys             # the last buy was cut at a cap


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_leveraged_week_with_default_entry_limit_is_paced_and_stays_within_caps(world, monkeypatch):
    a, runner, low = _idea_week(world, monkeypatch)
    log = runner.summary()
    assert low < 0 and _gross(a) > 1.0, (low, _gross(a), log)
    _assert_leveraged_limits(a)
    first_fill = {}
    for order in sorted(_bought(a), key=lambda o: o.filled_at):
        first_fill.setdefault(order.symbol, order.filled_at.astimezone(ET).date())
    by_session = defaultdict(set)
    for symbol, day in first_fill.items():
        by_session[day].add(symbol)
    assert len(by_session) >= 2 and max(map(len, by_session.values())) == 3, (dict(by_session), log)
    assert any(
        row.get("code") == "new_stock_session_cap"
        for row in runner.agent.journal.read(kinds={"execution.buy_funnel"})
    ), log


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_margin_off_never_borrows(world, monkeypatch):
    a, runner, low = _idea_week(world, monkeypatch, margin=False)
    assert _bought(a), runner.summary()
    assert low >= 0 and a.cash >= 0, low
    assert _gross(a) <= 1.0 + 1e-6


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_drawdown_throttle_tier_takes_no_new_borrowing(world, monkeypatch):
    from stock_agent.risk_manager import DRAWDOWN_STATE_KEY
    from tests.scenarios.runner import _default_startup

    async def startup(agent):
        await _default_startup(agent)
        agent.ledger.risk_state_set(DRAWDOWN_STATE_KEY, {        # 16%: the 15% throttle tier, not paused
            "version": 2, "high_water": "1.19", "drawdown": "0.16", "paused": False,
            "recovery_closes": 0, "last_session": "2026-11-20", "last_equity": "100000"})

    a, runner, low = _idea_week(world, monkeypatch, startup=startup)
    assert not runner.errors, runner.summary()
    assert runner.agent._drawdown_state().is_throttled()
    assert _bought(a), runner.summary()
    assert low >= 0 and a.cash >= 0, low
