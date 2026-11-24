"""MacroMonitor: regime from computed inputs (D5, F-06-05)."""
import asyncio

import stock_agent.macro_monitor as mm
from stock_agent.config import Config
from stock_agent.macro_monitor import (
    REGIME_AGGRESSIVE_DEPLOY, REGIME_CAUTIOUS, REGIME_NORMAL, MacroMonitor,
)
from stock_agent.price_service import PriceService
from stock_agent.state_store import StateStore


def run(c):
    return asyncio.run(c)


def ramp(start, end, n=210):
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def tail(base, n, last_pct, total=210):
    """Flat at `base`, then a linear move of `last_pct` % over the last `n` sessions."""
    head = [base] * (total - n)
    return head + [base * (1 + last_pct / 100 * (i + 1) / n) for i in range(n)]


def seed(world, series):
    for sym, closes in series.items():
        world.alpaca.seed_daily_bars(sym, closes)


def monitor(world, tmp_path, alerts=None, store=None):
    if store is None:
        store = StateStore(tmp_path / "macro.db", clock=world.clock.now)
        store.open()
    alerts = alerts if alerts is not None else []
    m = MacroMonitor(Config, prices=PriceService(now=world.clock.now), store=store,
                     alert=lambda s, t, b, key=None: alerts.append((s, t)), now=world.clock.now)
    return m, store, alerts


def calm():
    return {"SPY": ramp(400, 480), "QQQ": ramp(300, 380), "TLT": ramp(90, 92),
            "USO": ramp(70, 72), "UUP": ramp(28, 28.2), "VIXY": ramp(20, 18)}


def test_no_hard_coded_index_levels():
    for name in ("SPX_SUPPORT_LOW", "SPX_RESISTANCE_CONFIRM", "QQQ_RESISTANCE_2", "TLT_YIELD_STRESS",
                 "CRUDE_ELEVATED", "UUP_STRONG", "VIX_FEAR"):
        assert not hasattr(mm, name)


def test_calm_uptrend_is_normal_with_conviction_seven(world, tmp_path):
    seed(world, calm())
    m, _, _ = monitor(world, tmp_path)
    snap = run(m.check_conditions())
    assert snap.fresh_factors == 5 and not snap.degraded
    assert snap.regime == REGIME_NORMAL and m.get_effective_min_conviction() == 7
    lv = snap.key_levels["SPY"]
    assert lv["sma50"] is not None and lv["sma200"] is not None and lv["high90"] is not None


def test_broad_risk_off_is_cautious_and_raises_the_bar_to_eight(world, tmp_path):
    seed(world, calm())
    m, store, alerts = monitor(world, tmp_path)
    run(m.check_conditions())
    world.alpaca.trades_history.clear()
    seed(world, {"SPY": tail(480, 30, -6), "QQQ": tail(380, 30, -8), "TLT": tail(92, 20, -6),
                 "USO": tail(72, 20, 20), "UUP": tail(28, 20, 4), "VIXY": tail(18, 10, 15)})
    m2, _, alerts = monitor(world, tmp_path, alerts, store)
    snap = run(m2.check_conditions())
    assert snap.regime == REGIME_CAUTIOUS, snap.factors
    assert m2.get_effective_min_conviction() == 8
    assert snap.regime_changed and any(t == "Macro regime changed" for _, t in alerts)
    assert world.orders_sent() == []  # a regime change never trades


def test_deep_drawdown_with_fear_is_aggressive_but_never_below_seven(world, tmp_path):
    seed(world, {"SPY": tail(480, 20, -14), "QQQ": tail(380, 20, -16), "TLT": ramp(90, 92),
                 "USO": ramp(70, 72), "UUP": ramp(28, 28.2), "VIXY": tail(18, 10, 40)})
    m, _, _ = monitor(world, tmp_path)
    snap = run(m.check_conditions())
    assert snap.regime == REGIME_AGGRESSIVE_DEPLOY and snap.spy_drawdown_pct >= 10
    assert m.get_effective_min_conviction() == 7


def test_missing_inputs_stay_none_and_hold_the_prior_regime(world, tmp_path):
    seed(world, {"SPY": tail(480, 30, -6), "QQQ": tail(380, 30, -8), "TLT": tail(92, 20, -6),
                 "USO": tail(72, 20, 20), "UUP": tail(28, 20, 4), "VIXY": tail(18, 10, 15)})
    m, store, alerts = monitor(world, tmp_path)
    assert run(m.check_conditions()).regime == REGIME_CAUTIOUS
    world.alpaca.trades_history.clear()
    seed(world, {"SPY": ramp(400, 480), "QQQ": ramp(300, 380)})  # only 2 of 5 factors computable
    m2, _, alerts = monitor(world, tmp_path, alerts, store)  # restart: regime read from the store
    assert m2.get_current_regime() == REGIME_CAUTIOUS
    snap = run(m2.check_conditions())
    assert snap.factors["volatility"] is None and snap.factors["rates"] is None
    assert snap.factors["oil_dollar"] is None
    assert snap.degraded and snap.fresh_factors == 2
    assert snap.regime == REGIME_CAUTIOUS and not snap.regime_changed
    assert any(t == "Macro inputs degraded" for _, t in alerts)


def test_no_price_service_is_degraded_normal_not_zeros(world, tmp_path):
    m = MacroMonitor(Config, now=world.clock.now)
    snap = run(m.check_conditions())
    assert snap.degraded and snap.fresh_factors == 0
    assert all(v is None for v in snap.factors.values()) and snap.closes["SPY"] is None
    assert snap.regime == REGIME_NORMAL and m.get_effective_min_conviction() == 7
    assert m.format_discord_brief(snap)["title"].startswith("Macro Brief")


def test_deferred_load_rehydrates_once_the_store_is_open(world, tmp_path):
    seed(world, {"SPY": tail(480, 30, -6), "QQQ": tail(380, 30, -8), "TLT": tail(92, 20, -6),
                 "USO": tail(72, 20, 20), "UUP": tail(28, 20, 4), "VIXY": tail(18, 10, 15)})
    m, store, _ = monitor(world, tmp_path)
    assert run(m.check_conditions()).regime == REGIME_CAUTIOUS
    store.close()

    # Scheduler path: build without a store, bind an unopened store, open, then rehydrate.
    fresh = StateStore(tmp_path / "macro.db", clock=world.clock.now)
    m2 = MacroMonitor(Config, prices=PriceService(now=world.clock.now), defer_load=True,
                      now=world.clock.now)
    m2.bind_store(fresh, load=False)
    fresh.open()
    assert m2.load_state().regime == REGIME_CAUTIOUS
    assert m2.get_effective_min_conviction() == 8

    # Eager load against an unopened store does not lose state: it retries on access.
    again = StateStore(tmp_path / "macro.db", clock=world.clock.now)
    m3 = MacroMonitor(Config, prices=PriceService(now=world.clock.now), store=again,
                      now=world.clock.now)
    again.open()
    assert m3.get_current_regime() == REGIME_CAUTIOUS
