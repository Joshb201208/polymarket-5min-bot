from datetime import date, datetime, timedelta, timezone

import pytest

from stock_agent.options_policy import (
    ContractQuote,
    Cooldown,
    Direction,
    ExitInputs,
    ExitReason,
    Proposal,
    Rejection,
    Strategy,
    apply_result,
    breaks_thesis,
    build_proposal,
    close_limit,
    exit_reason,
    in_cooldown,
    occ_symbol,
    parse_direction,
    parse_occ,
    size_structure,
)
from stock_agent.options_settings import OptionsMode, OptionsSettings, load_options_settings, resolve_mode

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)
EXP = date(2026, 12, 24)
S = OptionsSettings()


def td(a, b):  # weekdays between, exclusive a, inclusive b
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def chain(spot=100.0, strikes=(90, 95, 100, 105, 110, 115), kind="call", spread=0.10, oi=1000, at=NOW, exp=EXP):
    out = []
    for k in strikes:
        intrinsic = max(spot - k, 0) if kind == "call" else max(k - spot, 0)
        mid = intrinsic + 3.0 * max(0.2, 1 - abs(spot - k) / 25)
        out.append(ContractQuote(occ_symbol("AAA", exp, kind, k), "AAA", exp, kind, k,
                                 round(mid - spread / 2, 2), round(mid + spread / 2, 2), at, oi))
    return out


# ----------------------------------------------------------------- settings
def test_mode_default_is_defined_risk():
    assert resolve_mode(None, {}) is OptionsMode.DEFINED_RISK


@pytest.mark.parametrize("env,mode", [
    ({"OPTIONS_MODE": "off"}, OptionsMode.OFF),
    ({"OPTIONS_MODE": "close_only"}, OptionsMode.CLOSE_ONLY),
    ({"OPTIONS_MODE": " Defined_Risk "}, OptionsMode.DEFINED_RISK),
    ({"OPTIONS_ENABLED": "false"}, OptionsMode.OFF),
    ({"OPTIONS_ENABLED": "true"}, OptionsMode.DEFINED_RISK),
    ({"OPTIONS_MODE": "close_only", "OPTIONS_ENABLED": "true"}, OptionsMode.CLOSE_ONLY),
    ({"OPTIONS_MODE": "yolo"}, OptionsMode.CLOSE_ONLY),
])
def test_mode_from_env(env, mode):
    assert resolve_mode(None, env) is mode


def test_mode_config_attribute_wins():
    class C:
        OPTIONS_MODE = "off"
    assert load_options_settings(C, {"OPTIONS_MODE": "defined_risk"}).mode is OptionsMode.OFF


def test_mode_permissions():
    assert OptionsMode.DEFINED_RISK.may_open and OptionsMode.DEFINED_RISK.may_manage
    assert not OptionsMode.CLOSE_ONLY.may_open and OptionsMode.CLOSE_ONLY.may_manage
    assert not OptionsMode.OFF.may_open and not OptionsMode.OFF.may_manage


# ---------------------------------------------------------------- helpers
def test_occ_round_trip():
    sym = occ_symbol("JPM", date(2026, 12, 11), "call", 340)
    assert sym == "JPM261211C00340000"
    o = parse_occ(sym)
    assert (o.underlying, o.expiry, o.type, o.strike) == ("JPM", date(2026, 12, 11), "call", 340.0)
    assert parse_occ("JPM") is None


@pytest.mark.parametrize("raw,d", [("BUY", Direction.BULLISH), ("BEARISH", Direction.BEARISH),
                                   ("HOLD", None), (None, None), ("bullish", Direction.BULLISH)])
def test_parse_direction(raw, d):
    assert parse_direction(raw) is d


def test_breaks_thesis():
    assert breaks_thesis(Direction.BULLISH, "BEARISH")
    assert breaks_thesis(Direction.BULLISH, "EXIT_PROPOSED")
    assert breaks_thesis(Direction.BEARISH, "BUY")
    assert not breaks_thesis(Direction.BULLISH, "HOLD")
    assert not breaks_thesis(Direction.BEARISH, "INSUFFICIENT_EVIDENCE")
    assert not breaks_thesis(Direction.BULLISH, None)


# --------------------------------------------------------------- proposals
def test_bull_call_spread_width_at_least_5pct_of_spot():
    p = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0, chain=chain(),
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert isinstance(p, Proposal), p
    assert p.strategy is Strategy.BULL_CALL_SPREAD
    long_leg, short_leg = p.legs
    assert (long_leg.side, long_leg.strike, short_leg.side, short_leg.strike) == ("buy", 100, "sell", 105)
    assert p.width == 5 and p.width >= 0.05 * p.spot
    assert p.expiry == EXP == long_leg.expiry == short_leg.expiry
    assert p.mid_debit <= p.limit_debit <= p.natural_debit
    assert (p.width - p.limit_debit) / p.limit_debit >= 1.0


def test_bear_put_spread():
    p = build_proposal(underlying="AAA", direction=Direction.BEARISH, conviction=8, spot=100.0,
                       chain=chain(kind="put"), today=TODAY, now=NOW, s=S, trading_days=td)
    assert p.strategy is Strategy.BEAR_PUT_SPREAD
    assert [(lg.side, lg.strike) for lg in p.legs] == [("buy", 100), ("sell", 95)]


def test_high_conviction_prefers_single_long_option():
    p = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=9, spot=100.0, chain=chain(),
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert p.strategy is Strategy.LONG_CALL and len(p.legs) == 1 and p.width is None
    p = build_proposal(underlying="AAA", direction=Direction.BEARISH, conviction=10, spot=100.0,
                       chain=chain(kind="put"), today=TODAY, now=NOW, s=S, trading_days=td)
    assert p.strategy is Strategy.LONG_PUT


def test_narrow_strikes_fall_back_to_single_option():
    # only 1-point strikes near spot within 4%: no spread can reach 5% of spot
    c = chain(spot=100, strikes=(98, 99, 100, 101, 102, 103, 104))
    p = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0, chain=c,
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert p.strategy is Strategy.LONG_CALL


def test_stale_or_wide_quotes_reject():
    stale = chain(at=NOW - timedelta(minutes=10))
    r = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0, chain=stale,
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert isinstance(r, Rejection) and "old" in r.reason
    wide = chain(spread=2.0)
    r = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0, chain=wide,
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert isinstance(r, Rejection) and "spread" in r.reason


def test_low_open_interest_rejects():
    r = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0, chain=chain(oi=10),
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert isinstance(r, Rejection) and "open interest" in r.reason


def test_expiry_window():
    near = chain(exp=TODAY + timedelta(days=10))
    r = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0, chain=near,
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert isinstance(r, Rejection) and "expiry" in r.reason


def test_poor_reward_risk_rejects():
    # deep ITM long strike makes the debit close to the width
    c = chain(spot=120, strikes=(100, 106, 112))
    from stock_agent.options_policy import _spread
    r = _spread("AAA", Direction.BULLISH, 101.0, c, NOW, S)
    assert isinstance(r, Rejection) and "reward/risk" in r.reason
    p = build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=101.0, chain=c,
                       today=TODAY, now=NOW, s=S, trading_days=td)
    assert not (isinstance(p, Proposal) and p.strategy.is_spread)


# ------------------------------------------------------------------ sizing
def test_sizing_by_max_loss():
    z = size_structure(200.0, equity=100_000, open_max_loss=0, open_count=0, s=S)
    assert z.qty == 15 and z.max_loss == 3000  # 3% of equity
    z = size_structure(200.0, equity=100_000, open_max_loss=9_000, open_count=3, s=S)
    assert z.qty == 5 and z.max_loss == 1000  # 10% total
    z = size_structure(200.0, equity=100_000, open_max_loss=9_900, open_count=3, s=S)
    assert z.qty == 0
    z = size_structure(200.0, equity=100_000, open_max_loss=0, open_count=6, s=S)
    assert z.qty == 0 and "6 structures" in z.reason
    z = size_structure(4000.0, equity=100_000, open_max_loss=0, open_count=0, s=S)
    assert z.qty == 0
    z = size_structure(200.0, equity=100_000, open_max_loss=0, open_count=0, s=S, drawdown_halt=True)
    assert z.qty == 0 and "drawdown" in z.reason


# ------------------------------------------------------------------- exits
def _x(**kw):
    base = dict(strategy=Strategy.BULL_CALL_SPREAD, qty=1, entry_debit=2.0, width=5.0, value_now=2.0,
                trading_days_left=10)
    base.update(kw)
    return ExitInputs(**base)


def test_exit_rules():
    assert exit_reason(_x(), S) is None
    assert exit_reason(_x(value_now=3.5), S) is ExitReason.PROFIT_TARGET  # +1.5 of max profit 3.0
    assert exit_reason(_x(value_now=3.4), S) is None
    assert exit_reason(_x(value_now=1.0), S) is ExitReason.LOSS_LIMIT
    assert exit_reason(_x(value_now=1.1), S) is None
    assert exit_reason(_x(trading_days_left=2), S) is ExitReason.TIME_EXIT
    assert exit_reason(_x(trading_days_left=None), S) is ExitReason.TIME_EXIT
    assert exit_reason(_x(thesis_broken=True), S) is ExitReason.THESIS_BREAK
    # single option: target on debit
    assert exit_reason(_x(strategy=Strategy.LONG_CALL, width=None, value_now=3.0), S) is ExitReason.PROFIT_TARGET
    assert exit_reason(_x(strategy=Strategy.LONG_CALL, width=None, value_now=2.9), S) is None


def test_ex_dividend_rule_for_itm_short_call():
    nxt = date(2026, 11, 24)
    base = dict(short_call_strike=105.0, spot=107.0, next_session=nxt)
    assert exit_reason(_x(**base, ex_dividend=(nxt, 0.5), short_call_extrinsic=0.2), S) is ExitReason.EX_DIVIDEND
    assert exit_reason(_x(**base, ex_dividend=(nxt, 0.5), short_call_extrinsic=0.9), S) is None
    assert exit_reason(_x(**base, ex_dividend=None), S) is None
    assert exit_reason(_x(**base, ex_dividend_known=False), S) is ExitReason.EX_DIVIDEND
    # out of the money short call: no early-assignment risk
    assert exit_reason(_x(short_call_strike=105.0, spot=100.0, ex_dividend_known=False), S) is None


# ---------------------------------------------------------------- cool-down
def test_cooldown_after_losses():
    add = lambda d, n: d + timedelta(days=n)  # noqa: E731
    cd = Cooldown("AAA")
    apply_result(cd, -100, TODAY, S, add)
    assert cd.until == TODAY + timedelta(days=10) and in_cooldown(cd, TODAY + timedelta(days=9))
    assert not in_cooldown(cd, TODAY + timedelta(days=10))
    apply_result(cd, -50, TODAY, S, add)
    assert cd.until == TODAY + timedelta(days=30) and cd.consecutive_losses == 2
    apply_result(cd, 10, TODAY, S, add)
    assert cd.until is None and cd.consecutive_losses == 0 and not in_cooldown(cd, TODAY)


def test_close_limit():
    assert close_limit(1.0, 1.2, 0.25) == 1.15
    assert close_limit(1.0, 1.2, 1.0) == 1.0
    assert close_limit(-0.1, 0.0, 1.0) == 0.0


def test_budgets_follow_risk_config():
    class C:
        RISK_OPTION_MAX_LOSS_PCT = 0.02
        RISK_OPTION_TOTAL_LOSS_PCT = 0.08
        RISK_OPTION_MAX_STRUCTURES = 4
    s = load_options_settings(C, {})
    assert (s.max_loss_per_structure_pct, s.max_loss_total_pct, s.max_open_structures) == (0.02, 0.08, 4)
    assert load_options_settings(None, {}).max_open_structures == 6
