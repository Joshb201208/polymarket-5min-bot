"""Risk tripwires.

These tests pin configured limits and deterministic risk helpers. Projected
order sizing, concentration, cash and broker book behavior are covered in
test_risk_projection.py.

If one of these fails, trading behaviour has changed. Stop and confirm that
the change is an approved plan item before editing the test, and call the
change out at the top of the pull request.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from stock_agent.config import Config
from stock_agent.risk_manager import (
    CanonicalSector, DrawdownState, ShadowDailyBar, ShadowEntryAnchor, StopDistance,
    atr14, classify_symbol, normalize_sector, shadow_stop_results, stop_fraction, stop_price,
    update_drawdown,
)

# ── Where orders go ───────────────────────────────────────────────────────


def test_default_mode_is_paper():
    assert Config.MODE == "PAPER"


def test_default_broker_endpoint_is_the_paper_endpoint():
    assert Config.ALPACA_BASE_URL == "https://paper-api.alpaca.markets"


# ── Limit values ──────────────────────────────────────────────────────────


def test_equity_limit_values():
    assert Config.MAX_POSITION_PCT == 0.15
    assert Config.MARGIN_ENABLED is True
    assert Config.MAX_GROSS_EXPOSURE_PCT == 2.00
    assert Config.MARGIN_MIN_EXCESS_PCT == 0.25
    assert Config.MARGIN_TRIM_EXCESS_PCT == 0.15
    assert Config.MARGIN_INTEREST_RATE_PCT == 6.5
    assert Config.MAX_POSITIONS == 20
    assert Config.MAX_SECTOR_PCT == 0.40
    assert Config.MAX_CLUSTER_PCT == 0.48
    assert Config.MIN_CONVICTION == 7
    assert Config.MIN_FREE_CASH_PCT == 0.02
    assert Config.MAX_INITIAL_STOP_RISK_PCT == 0.01
    assert Config.DRAWDOWN_ALERT_PCT == 0.10
    assert Config.DRAWDOWN_THROTTLE_PCT == 0.15
    assert Config.DRAWDOWN_PAUSE_PCT == 0.20
    assert Config.DRAWDOWN_RECOVERY_SESSIONS == 5


def test_conviction_size_map():
    assert Config.CONVICTION_SIZE_MAP == {7: 0.05, 8: 0.07, 9: 0.08, 10: 0.10}


def test_no_conviction_tier_exceeds_the_per_symbol_cap():
    assert max(Config.CONVICTION_SIZE_MAP.values()) <= Config.MAX_POSITION_PCT


def test_stop_loss_range():
    assert Config.STOP_LOSS_PCT_MIN == 0.07
    assert Config.STOP_LOSS_PCT_MAX == 0.12


def test_options_limit_values_in_config():
    assert Config.OPTIONS_MAX_POSITIONS == 10
    assert Config.OPTIONS_MAX_SINGLE_TRADE_PCT == 0.07
    assert Config.OPTIONS_MAX_TOTAL_EXPOSURE_PCT == 0.15
    assert Config.OPTIONS_MIN_DTE == 14
    assert Config.OPTIONS_MAX_DTE == 45
    assert Config.OPTIONS_AUTO_CLOSE_PROFIT_PCT == 0.50
    assert Config.OPTIONS_AUTO_CLOSE_DTE == 3


@pytest.mark.parametrize(("name", "expected"), [
    (" Financial Services ", CanonicalSector.FINANCIALS),
    ("consumer-cyclical", CanonicalSector.CONSUMER_DISCRETIONARY),
    ("Technology", CanonicalSector.INFORMATION_TECHNOLOGY),
    ("healthcare", CanonicalSector.HEALTH_CARE),
    ("basic materials", CanonicalSector.MATERIALS),
    ("nonsense", CanonicalSector.UNKNOWN),
    (None, CanonicalSector.UNKNOWN),
])
def test_canonical_sector_aliases(name, expected):
    assert normalize_sector(name) == expected


def test_common_issuer_and_overlapping_cluster_classification():
    assert classify_symbol("goog", "Technology") == classify_symbol("GOOGL", "Communication Services")
    assert classify_symbol("BRK-A").issuer == classify_symbol("brk.b").issuer
    assert set(classify_symbol("NVDA").clusters) == {"Semiconductors", "MegaCapTech"}


@dataclass
class Bar:
    high: int
    low: int
    close: int


def test_atr_uses_prior_close_then_wilder_smoothing():
    first = Bar(101, 99, 100)
    normal = Bar(104, 102, 103)
    bars = [first, *([normal] * 14), Bar(118, 115, 117)]
    seeded = (Decimal(4) + Decimal(2) * 13) / 14
    assert atr14(bars) == (seeded * 13 + 15) / 14
    assert atr14(bars[:14]) is None
    assert atr14([*bars, Bar(101, 99, 1)]) is None


@pytest.mark.parametrize(("atr", "expected", "fallback"), [
    (None, "0.07", True), (Decimal("NaN"), "0.07", True),
    (Decimal(1), "0.07", False), (Decimal(4), "0.10", False),
    (Decimal(8), "0.12", False),
])
def test_atr_stop_distance_widens_with_usable_bars(atr, expected, fallback):
    assert stop_fraction(Decimal(100), atr) == StopDistance(Decimal(expected), fallback)


def test_fill_stop_uses_conservative_valid_tick():
    assert stop_price(Decimal("100.06"), StopDistance(Decimal("0.12"), False)) == Decimal("88.06")


def test_drawdown_pauses_throttles_and_automatically_recovers():
    state = update_drawdown(DrawdownState(), "2026-10-01", Decimal(100))
    assert not state.alert and not state.paused
    state = update_drawdown(state, "2026-10-02", Decimal(90))
    assert state.alert and not state.throttled
    state = update_drawdown(state, "2026-10-05", Decimal(85))
    assert state.throttled and not state.paused
    state = update_drawdown(state, "2026-10-06", Decimal(80))
    assert state.throttled and not state.paused
    state = update_drawdown(state, "2026-10-07", Decimal(79))
    assert state.paused
    for day in ("08", "09"):
        state = update_drawdown(state, f"2026-10-{day}", Decimal(86))
    assert state.recovery_closes == 2
    state = update_drawdown(state, "2026-10-12", Decimal(85))
    assert state.recovery_closes == 0
    for day in ("13", "14", "15", "16"):
        state = update_drawdown(state, f"2026-10-{day}", Decimal(86))
        assert state.paused
    assert update_drawdown(state, "2026-10-16", Decimal(90)) == state
    state = update_drawdown(state, "2026-10-19", Decimal(86))
    assert state.alert and not state.throttled and not state.paused
    state = update_drawdown(state, "2026-10-20", Decimal(101))
    assert state.high_water == 101 and not state.alert


def test_drawdown_uses_returns_not_deposits_or_withdrawals_across_restarts():
    state = update_drawdown(DrawdownState(), "2026-11-23", Decimal(100),
                            net_external_flow=Decimal(100))
    # The first close is the cut-over baseline even if the account was funded then.
    assert state.high_water == 100 and not state.drawdown
    state = update_drawdown(state, "2026-11-24", Decimal(140), net_external_flow=Decimal(40))
    assert state.last_equity == 140 and state.high_water == 100
    restored = DrawdownState.from_record(state.to_record())
    assert restored == state
    assert update_drawdown(restored, "2026-11-24", Decimal(140), net_external_flow=Decimal(40)) == state

    withdrawal = update_drawdown(restored, "2026-11-25", Decimal(90), net_external_flow=Decimal(-50))
    assert withdrawal.high_water == 100 and not withdrawal.drawdown and not withdrawal.paused
    gain = update_drawdown(withdrawal, "2026-11-27", Decimal(99))
    assert gain.high_water == 110 and not gain.drawdown
    # A real 20% loss throttles even though a deposit increased raw equity.
    loss = update_drawdown(gain, "2026-11-30", Decimal("109.20"), net_external_flow=Decimal(30))
    assert loss.drawdown == Decimal("0.20") and loss.throttled and not loss.paused
    breach = update_drawdown(loss, "2026-12-01", Decimal(108))
    assert breach.decision_drawdown > Decimal("0.20") and breach.paused


def test_legacy_drawdown_record_uses_implied_raw_equity_for_first_adjusted_interval():
    old = DrawdownState(Decimal(100), Decimal("0.10"), last_session="2026-11-23")
    assert old.to_record()["version"] == 1
    restored = DrawdownState.from_record(old.to_record())
    next_close = update_drawdown(restored, "2026-11-24", Decimal(60), net_external_flow=Decimal(-30))
    assert next_close.high_water == 100 and next_close.drawdown == Decimal("0.10")
    assert next_close.to_record()["version"] == 2 and next_close.last_equity == 60


def test_missing_expected_close_breaks_drawdown_recovery_streak():
    state = DrawdownState(Decimal(100), Decimal(".14"), True, 4, "2026-10-14")
    result = update_drawdown(state, "2026-10-16", Decimal(86), previous_session="2026-10-15")
    assert result.paused and result.recovery_closes == 1


def test_drawdown_state_roundtrip_never_resets_a_corrupt_latch_as_cutover():
    assert DrawdownState.from_record(None) == DrawdownState()
    state = DrawdownState(Decimal(100), Decimal(".14"), True, 3, "2026-10-14")
    assert DrawdownState.from_record(state.to_record()) == state
    with pytest.raises(ValueError):
        DrawdownState.from_record({**state.to_record(), "paused": "false"})
    with pytest.raises(ValueError):
        DrawdownState.from_record({**state.to_record(), "last_session": "bad"})


def test_shadow_stops_skip_entry_session_and_use_gap_or_stop_level():
    day1, day2, day3, day4 = (date(2026, 11, day) for day in (16, 17, 18, 19))
    anchor = ShadowEntryAnchor("test", "ALFA", day1, Decimal(100), Decimal(50),
                               Decimal(".09"), Decimal(91))
    bars = (
        ShadowDailyBar(day1, 87, 101, 80, 100),
        ShadowDailyBar(day2, 96, 99, 92, 94),
        ShadowDailyBar(day3, 89, 100, 88, 95),
        ShadowDailyBar(day4, 96, 106, 95, 105),
    )
    results = {result.variant: result for result in shadow_stop_results(anchor, bars)}
    assert [(results[name].exit_session, results[name].price) for name in
            ("fixed_7", "fixed_10", "fixed_12", "atr_placed", "no_stop")] == [
        (day2, Decimal(93)), (day3, Decimal(89)), (day3, Decimal(88)),
        (day3, Decimal(89)), (None, Decimal(105)),
    ]
    assert results["fixed_7"].pnl == -350
    assert results["atr_placed"].return_fraction == Decimal("-.11")
    assert results["no_stop"].through_session == day4 and not results["no_stop"].stopped
    assert not shadow_stop_results(anchor, bars[:1])
    assert not shadow_stop_results(anchor, (*bars, ShadowDailyBar(day2, 96, 99, 90, 94)))
