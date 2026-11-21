"""Risk tripwires.

These tests pin the limits and sizing behaviour the bot runs with TODAY. They
do not say today's behaviour is right; the audit decides that. They exist so
that nobody (human or AI) changes how much the bot can buy, or where it sends
orders, without noticing.

If one of these fails, trading behaviour has changed. Stop and confirm that
the change is an approved plan item before editing the test, and call the
change out at the top of the pull request.
"""

from datetime import datetime, timezone

import pytest

from stock_agent import options_engine, options_portfolio
from stock_agent.config import Config
from stock_agent.models import PortfolioState, Position, Thesis
from stock_agent.risk_manager import RiskManager

NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def make_position(symbol: str, value: float, sector: str = "Unknown", price: float = 100.0) -> Position:
    """A position worth `value` dollars at `price` per share."""
    thesis = Thesis(
        symbol=symbol,
        direction="BUY",
        conviction=8,
        summary="test",
        bull_case="test",
        bear_case="test",
        catalysts=[],
        risks=[],
        generated_at=NOW,
    )
    return Position(
        symbol=symbol,
        shares=int(value / price),
        entry_price=price,
        entry_date=NOW,
        current_price=price,
        stop_loss=price * 0.93,
        thesis=thesis,
        sector=sector,
        last_updated=NOW,
    )


def make_state(positions: list[Position], total: float = 100_000.0) -> PortfolioState:
    """A portfolio worth `total` dollars: the given positions plus cash."""
    invested = sum(p.shares * p.current_price for p in positions)
    return PortfolioState(positions=positions, cash=total - invested)


@pytest.fixture
def risk() -> RiskManager:
    return RiskManager(Config())


# ── Where orders go ───────────────────────────────────────────────────────


def test_default_mode_is_paper():
    assert Config.MODE == "PAPER"


def test_default_broker_endpoint_is_the_paper_endpoint():
    assert Config.ALPACA_BASE_URL == "https://paper-api.alpaca.markets"


# ── Limit values ──────────────────────────────────────────────────────────


def test_equity_limit_values():
    assert Config.MAX_POSITION_PCT == 0.15
    assert Config.MAX_TOTAL_EXPOSURE == 1.00
    assert Config.MAX_POSITIONS == 20
    assert Config.MAX_SECTOR_PCT == 0.40
    assert Config.MAX_CLUSTER_PCT == 0.48
    assert Config.MIN_CONVICTION == 7


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


def test_options_limit_values_in_the_options_modules():
    # The options code carries its own copies of these limits.
    assert options_engine.MAX_OPTIONS_POSITIONS == 10
    assert options_engine.MAX_SINGLE_TRADE_PCT == 0.07
    assert options_engine.MAX_TOTAL_OPTIONS_EXPOSURE_PCT == 0.15
    assert options_engine.MIN_REWARD_TO_RISK == 1.5
    assert options_engine.SHARES_PER_CONTRACT == 100
    assert (options_engine.CC_MIN_DTE, options_engine.CC_MAX_DTE) == (14, 35)
    assert (options_engine.SPREAD_MIN_DTE, options_engine.SPREAD_MAX_DTE) == (14, 45)
    assert options_portfolio.EXPIRY_ALERT_DTE == 3
    assert options_portfolio.PROFIT_TAKE_PCT == 50.0


# ── Sizing ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "conviction, expected_shares",
    [(7, 50), (8, 70), (9, 80), (10, 100)],
)
def test_position_size_by_conviction(risk, conviction, expected_shares):
    # $100,000 portfolio, $100 share price.
    assert risk.calculate_position_size(100_000, 100.0, conviction) == expected_shares


def test_position_size_for_unlisted_conviction_uses_the_smallest_tier(risk):
    assert risk.calculate_position_size(100_000, 100.0, 5) == 50
    assert risk.calculate_position_size(100_000, 100.0, 11) == 50


@pytest.mark.parametrize("portfolio_value, price", [(100_000, 0), (100_000, -5), (0, 100), (-1, 100)])
def test_position_size_is_zero_for_bad_inputs(risk, portfolio_value, price):
    assert risk.calculate_position_size(portfolio_value, price, 9) == 0


def test_position_size_rounds_down_to_whole_shares(risk):
    assert risk.calculate_position_size(100_000, 333.0, 7) == 15  # $5,000 / $333


# ── Stop-loss ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "beta, expected_stop",
    [
        (None, 93.0),   # no beta: minimum stop distance (7%)
        (0.0, 93.0),    # non-positive beta treated as missing
        (1.0, 93.0),    # 5% would be below the floor, clamped to 7%
        (2.0, 90.0),    # 10%
        (3.0, 88.0),    # 15% clamped to the 12% ceiling
    ],
)
def test_stop_loss_price(risk, beta, expected_stop):
    assert risk.get_stop_loss_price(100.0, beta) == expected_stop


def test_stop_loss_trigger(risk):
    assert risk.should_stop_loss(100.0, 93.0) is True
    assert risk.should_stop_loss(100.0, 93.01) is False


# ── Whether a buy is allowed ──────────────────────────────────────────────


def test_new_position_allowed_in_an_empty_portfolio(risk):
    allowed, reason = risk.can_open_position(make_state([]), "XYZ", "Utilities", 100.0, 8)
    assert (allowed, reason) == (True, "OK")


def test_per_symbol_cap_blocks_adding_to_an_oversized_position(risk):
    state = make_state([make_position("XYZ", 20_000)])  # 20% of the portfolio
    allowed, reason = risk.can_open_position(state, "XYZ", "Utilities", 100.0, 10)
    assert allowed is False
    assert "hard cap" in reason


def test_no_add_once_a_position_is_near_its_conviction_target(risk):
    state = make_state([make_position("XYZ", 4_600)])  # 4.6%; target for conviction 7 is 5%
    allowed, _ = risk.can_open_position(state, "XYZ", "Utilities", 100.0, 7)
    assert allowed is False


def test_add_allowed_while_a_position_is_well_below_target(risk):
    state = make_state([make_position("XYZ", 2_000)])  # 2%
    allowed, reason = risk.can_open_position(state, "XYZ", "Utilities", 100.0, 7)
    assert (allowed, reason) == (True, "ADD_TO_EXISTING")


def test_position_count_limit_blocks_new_positions(risk):
    held = [make_position(f"S{i:02d}", 1_000) for i in range(Config.MAX_POSITIONS)]
    allowed, reason = risk.can_open_position(make_state(held), "NEW", "Utilities", 100.0, 8)
    assert allowed is False
    assert "Max positions" in reason


def test_sector_cap_blocks_a_new_position_in_a_full_sector(risk):
    held = [make_position(f"U{i}", 9_000, sector="Utilities") for i in range(5)]  # 45%
    allowed, reason = risk.can_open_position(make_state(held), "UNEW", "Utilities", 100.0, 8)
    assert allowed is False
    assert "Sector" in reason


def test_cluster_cap_blocks_a_new_position_in_a_full_cluster(risk):
    semis = ["AMD", "AVGO", "TSM", "ASML", "MU"]
    held = [make_position(s, 10_000, sector="Unknown") for s in semis]  # 50% in one cluster
    # Sector passed as "Unknown" and symbols mapped to Technology by SECTOR_MAP,
    # so give the candidate a different sector to isolate the cluster rule.
    allowed, reason = risk.can_open_position(make_state(held), "INTC", "Other", 100.0, 8)
    assert allowed is False
    assert "Cluster" in reason


def test_buy_blocked_when_cash_cannot_cover_one_share(risk):
    state = PortfolioState(positions=[make_position("AAA", 10_000)], cash=50.0)
    allowed, reason = risk.can_open_position(state, "XYZ", "Utilities", 100.0, 8)
    assert allowed is False
    assert "Insufficient cash" in reason


def test_buy_blocked_when_portfolio_value_is_zero(risk):
    allowed, _ = risk.can_open_position(PortfolioState(positions=[], cash=0.0), "XYZ", "Utilities", 100.0, 8)
    assert allowed is False
