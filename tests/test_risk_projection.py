from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, Holding, OpenOrder, OptionLeg, PositionMeta
from stock_agent.risk_manager import (
    CanonicalSector, DrawdownState, OptionCommitment, RiskControls, RiskManager,
    TradeIntent, TradeKind,
)


D = Decimal
NOW = datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc)


def stock(symbol, qty, sector, *, price="100", available=None, stop=None):
    price = D(price) if price is not None else None
    return Holding(
        symbol, D(qty), D(qty if available is None else available), D("100"),
        price, price is not None, D(qty) * (price or D(0)), None,
        meta=PositionMeta("position-" + symbol, symbol, sector=sector,
                          stop_price=D(stop) if stop else None),
    )


def order(symbol, *, qty="100", done="20", limit="100", order_id=None):
    key = order_id or symbol
    return OpenOrder(
        key, key, symbol, "buy", "limit", D(qty), D(done), D(qty) - D(done),
        D(limit) if limit else None, None, "day", "us_equity", NOW,
    )


def leg(symbol, qty, right="call"):
    return OptionLeg(symbol + "-option", symbol, right, D(100), date(2026, 12, 18),
                     D(qty), 100, D(1), D(1), True, D(qty) * 100)


def snapshot(*holdings, cash="90000", equity="100000", orders=(), legs=(), **kwargs):
    return BookSnapshot(
        as_of=NOW, run_id="test-book", equity=D(equity), cash=D(cash),
        long_mv=sum((max(D(0), h.market_value) for h in holdings), D(0)),
        short_mv=sum((min(D(0), h.market_value) for h in holdings), D(0)),
        options_mv=D(0), last_equity=None, buying_power=D(400000),
        holdings=holdings, legs=legs, open_orders=orders, diffs=(), **kwargs,
    )


def buy(symbol="ALFA", qty="10", *, sector="Industrials", kind=TradeKind.BUY, **kwargs):
    fields = dict(price=D(100), analysis_price=D(100), signal_at=NOW, quote_at=NOW,
                  raw_conviction=10, effective_conviction=10)
    fields.update(kwargs)
    return TradeIntent(kind, symbol, D(qty), sector=sector, **fields)


def option(*, loss="3000", debit="3000", **kwargs):
    fields = dict(raw_conviction=8, effective_conviction=8, signal_at=NOW,
                  option_max_loss=D(loss), option_cash_debit=D(debit), structure_id="new-option")
    fields.update(kwargs)
    kind = fields.pop("kind", TradeKind.OPTION_OPEN)
    return TradeIntent(kind, "ALFA", D(1), **fields)


@pytest.fixture
def risk():
    return RiskManager(Config())


def test_projected_full_order_cash_floor_ignores_buying_power(risk):
    book = snapshot(cash="2500")
    assert risk.propose_equity_qty(book, buy()) == 5
    fits = risk.evaluate_trade(book, buy(qty="5"))
    assert fits.ok and fits.after.free_cash == D(2000)
    assert risk.evaluate_trade(book, buy(qty="6")).reasons == ("cash_floor",)


def test_limit_bound_and_marked_slippage_are_conservative(risk):
    trade = buy(qty="10", price=D(101), mark_price=D(100), fee=D(1))
    verdict = risk.evaluate_trade(snapshot(), trade)
    assert verdict.ok
    assert verdict.after.equity == D(99989)
    assert verdict.after.free_cash == D(88989)
    assert verdict.after.gross_exposure == D(1010)


def test_post_trade_sector_catches_aliases_for_new_buys_and_adds(risk):
    book = snapshot(stock("BANKA", 149, "Financials"), stock("BANKB", 149, "Financial Services"),
                    stock("BANKC", 83, "Financials"), stock("ALFA", 10, "Financial Services"))
    add = buy(kind=TradeKind.ADD, sector="Financials", last_filled_conviction=10)
    assert risk.evaluate_trade(book, add).reasons == ("sector_cap",)
    assert risk.propose_equity_qty(book, add) == 9
    new = replace(add, kind=TradeKind.BUY, symbol="BANKD")
    assert risk.evaluate_trade(book, new).reasons == ("sector_cap",)
    assert risk.propose_equity_qty(book, new) == 9
    after = risk.evaluate_trade(book, replace(new, qty=D(9))).after
    assert after.sector_values[CanonicalSector.FINANCIALS] == D(40000)


def test_common_issuer_and_cluster_exposure(risk):
    book = snapshot(stock("GOOG", 80, "Technology"), stock("AMZN", 140, "Consumer Cyclical"),
                    stock("AAPL", 140, "Technology"), stock("MSFT", 110, "Technology"))
    reason = risk.evaluate_trade(book, buy(symbol="META", qty="11", sector="Communication Services"))
    assert reason.reasons == ("cluster_cap",)
    assert risk.evaluate_trade(book, buy(symbol="META", qty="10", sector="Communication Services")).ok
    after = risk.evaluate_trade(book, buy(symbol="GOOGL", qty="10", sector="Technology")).after
    assert after.issuer_values["GOOGL"] == D(9000)
    assert after.sector_values[CanonicalSector.COMMUNICATION_SERVICES] == D(9000)


def test_the_same_pending_remainder_is_reserved_once_and_reduces_sector_and_cash(risk):
    pending = order("BANKD", qty="40", done="30")
    book = snapshot(stock("BANKA", 149, "Financials"), stock("BANKB", 149, "Financial Services"),
                    stock("BANKC", 80, "Financials"), stock("BANKD", 10, "Financials"),
                    cash="4500", orders=(pending, pending))
    new = buy(symbol="BANKE", sector="Financials", qty="2")
    assert risk.evaluate_trade(book, new).ok
    assert risk.evaluate_trade(book, replace(new, qty=D(3))).reasons == ("sector_cap",)
    allowed = risk.evaluate_trade(book, new)
    assert allowed.after.free_cash == D(3300)


def test_unclassified_pending_names_are_not_unlimited_sector_capacity(risk):
    pending = order("BANKD", qty="15", done="0")
    book = snapshot(stock("BANKA", 149, "Financials"), stock("BANKB", 149, "Financials"),
                    stock("BANKC", 90, "Financials"), orders=(pending,))
    new = buy(symbol="BANKE", sector="Financials", qty="2")
    assert risk.evaluate_trade(book, new).reasons == ("sector_cap",)
    controls = RiskControls(pending_sectors={"BANKD": "Information Technology"})
    assert risk.evaluate_trade(book, new, controls=controls).ok


def test_hard_cap_can_clip_a_new_buy_and_throttle_scales_sizes(risk, monkeypatch):
    book = snapshot()
    controls = RiskControls(drawdown=DrawdownState(drawdown=D(".15")))
    assert risk.propose_equity_qty(book, buy(stop_distance=D(".12"))) == 83
    assert risk.propose_equity_qty(book, buy(stop_distance=D(".12")), controls=controls) == 41
    monkeypatch.setattr(risk.config, "MAX_POSITION_PCT", .03)
    assert risk.propose_equity_qty(book, buy()) == 30


def test_top_up_can_use_equal_conviction_but_not_below_stop_or_near_target(risk):
    trade = buy(kind=TradeKind.ADD, effective_conviction=7, raw_conviction=7, last_filled_conviction=7)
    assert risk.evaluate_trade(snapshot(stock("ALFA", 20, "Industrials", stop="92")), trade).ok
    assert risk.evaluate_trade(snapshot(stock("ALFA", 20, "Industrials", stop="100")), trade).reasons == (
        "top_up_below_stop",
    )
    assert risk.evaluate_trade(snapshot(stock("ALFA", 45, "Industrials")), trade).reasons == ("near_target",)
    assert risk.evaluate_trade(snapshot(stock("ALFA", 20, "Industrials")),
                               replace(trade, last_filled_conviction=8)).reasons == ("top_up_conviction",)


def test_position_count_blocks_new_names_but_not_top_up(risk):
    book = snapshot(*(stock(f"STOCK{i}", 1, "Financials") for i in range(20)))
    assert risk.evaluate_trade(book, buy()).reasons == ("position_count",)
    assert risk.evaluate_trade(book, buy(symbol="STOCK1", sector="Financials", kind=TradeKind.ADD,
                                         last_filled_conviction=10)).ok


def test_missing_data_blocks_new_exposure_but_not_known_risk_reductions(risk):
    book = snapshot(stock("BETA", 20, "Industrials", price=None, available=18))
    assert risk.evaluate_trade(book, buy()).reasons == ("invalid_risk_input",)
    paused = RiskControls(pause_new_buys=True, drawdown=DrawdownState(paused=True))
    assert risk.evaluate_trade(book, TradeIntent(TradeKind.SELL, "BETA", D(18)), controls=paused).ok
    assert not risk.evaluate_trade(book, TradeIntent(TradeKind.SELL, "BETA", D(19)), controls=paused).ok


@pytest.mark.parametrize(("changes", "reason"), [
    ({"raw_conviction": 6, "effective_conviction": 7}, "conviction_floor"),
    ({"raw_conviction": 7, "effective_conviction": 9}, "conviction_adjustment"),
    ({"raw_conviction": 7, "effective_conviction": 7, "minimum_conviction": 8}, "conviction_floor"),
    ({"signal_at": NOW - timedelta(hours=97)}, "stale_signal"),
    ({"quote_at": NOW - timedelta(seconds=61)}, "stale_quote"),
    ({"analysis_price": D(96)}, "price_drift"),
    ({"qty": D("1.5")}, "invalid_quantity_or_limit"),
])
def test_stock_entry_rejects_invalid_signal_or_conviction(risk, changes, reason):
    assert reason in risk.evaluate_trade(snapshot(), replace(buy(), **changes)).reasons


def test_option_entry_max_loss_cash_and_drawdown_not_theoretical_delivery(risk):
    book = snapshot(stock("ALFA", 140, "Industrials"), cash="5000")
    trade = option(assignment_shares=D(100), assignment_strike=D(100))
    assert risk.evaluate_trade(book, trade).ok
    assert risk.evaluate_trade(book, option(loss="3000.01")).reasons == ("option_single_budget",)
    assert risk.evaluate_trade(book, option(debit="3000.01")).reasons == ("cash_floor",)
    full = RiskControls(option_commitments=tuple(OptionCommitment(str(i), D(1170))
                                                 for i in range(6)))
    assert risk.evaluate_trade(book, trade, controls=full).reasons == (
        "option_total_budget", "option_count",
    )
    throttled = RiskControls(drawdown=DrawdownState(drawdown=D(".15")))
    assert risk.evaluate_trade(book, trade, controls=throttled).reasons == ("drawdown_pause",)


def test_option_commitments_reserve_cash_and_count_pending_debits_once(risk):
    opt_order = replace(order("OPT", qty="1", done="0", limit="10"), asset_class="us_option")
    book = snapshot(cash="5000", orders=(opt_order,))
    controls = RiskControls(option_commitments=(
        OptionCommitment("committed", D(1000), D(1000), ("OPT",)),
    ))
    verdict = risk.evaluate_trade(book, option(loss="2000", debit="2000"), controls=controls)
    assert verdict.ok and verdict.after.free_cash == D(2000)
    assert verdict.after.option_structures == 2 and verdict.after.option_max_loss == D(3000)
    assert risk.evaluate_trade(book, option(loss="2000", debit="2000")).ok


def test_cash_secured_put_reserves_full_strike_and_checks_assignment_concentration(risk):
    trade = option(kind=TradeKind.CASH_SECURED_PUT, loss="3000", debit="0",
                   assignment_shares=D(100), assignment_strike=D(100))
    book = snapshot(stock("ALFA", 60, "Industrials"), cash="11999")
    assert set(risk.evaluate_trade(book, trade).reasons) == {"issuer_cap", "cash_floor"}
    assert risk.evaluate_trade(snapshot(cash="12000"), trade).after.free_cash == D(2000)


def test_delivered_shares_are_evaluated_as_facts_and_trim_next_session(risk):
    book = snapshot(stock("ALFA", 180, "Industrials"))
    verdict = risk.evaluate_trade(book, TradeIntent(TradeKind.OPTION_DELIVERY, "ALFA", D(100)))
    assert not verdict.ok and verdict.detect_only and verdict.after.gross_exposure == D(18000)
    assert verdict.reasons == ("issuer_cap",)
    assert not risk.plan_remediation(book)
    assert not risk.plan_remediation(book, consecutive_closes={("issuer_cap", "ALFA"): 1})
    trim = risk.plan_remediation(book, consecutive_closes={("issuer_cap", "ALFA"): 2})
    assert trim[0].qty == 50
    assert risk.plan_remediation(book, broker_action=True)[0].qty == 30


def test_trim_and_exit_do_not_sell_shares_behind_a_short_call(risk):
    book = snapshot(stock("ALFA", 180, "Industrials", available=80), legs=(leg("ALFA", -1),))
    plan = risk.plan_remediation(book, broker_action=True)
    assert plan[0].qty == 30
    assert not risk.evaluate_trade(book, TradeIntent(TradeKind.SELL, "ALFA", D(81))).ok
    closed = risk.evaluate_trade(book, TradeIntent(TradeKind.OPTION_CLOSE, "ALFA", D(1)),
                                 controls=RiskControls(pause_new_buys=True))
    assert closed.ok
    assert risk.evaluate_trade(book, option(kind=TradeKind.COVERED_CALL)).reasons == ("covered_calls_deferred",)


def test_hedging_long_call_is_not_counted_as_stock_coverage(risk):
    short = leg("ALFA", -2)
    hedge = replace(short, occ_symbol="ALFA-long-option", strike=D(90), qty=D(1))
    book = snapshot(stock("ALFA", 120, "Industrials"), legs=(short, hedge))
    # One contract is hedged by the long leg; the remaining contract needs 100 shares.
    assert risk.evaluate_trade(book, TradeIntent(TradeKind.SELL, "ALFA", D(20))).ok
    assert not risk.evaluate_trade(book, TradeIntent(TradeKind.SELL, "ALFA", D(21))).ok


def test_any_unexpected_short_blocks_buys_but_not_funded_cover(risk):
    book = snapshot(stock("ALFA", -2, "Industrials"), cash="250")
    assert risk.evaluate_trade(book, buy("BETA")).reasons == ("unexpected_short",)
    repair, = risk.plan_remediation(book)
    assert repair.kind == TradeKind.BUY_TO_COVER and repair.qty == 2
    assert risk.evaluate_trade(book, repair, controls=RiskControls(pause_new_buys=True)).ok
    assert not risk.evaluate_trade(book, replace(repair, qty=D(3))).ok
    assert not risk.evaluate_trade(book, replace(repair, price=D(130))).ok


def test_unknown_broker_or_unbounded_pending_order_denies_new_entries(risk):
    blocked = snapshot(blocked_symbols=frozenset({"ALFA"}))
    assert "broker_book_blocked" in risk.evaluate_trade(blocked, buy()).reasons
    pending = snapshot(orders=(order("BETA", limit=None),))
    assert risk.evaluate_trade(pending, buy()).reasons == ("invalid_risk_input",)


def test_existing_issuer_drift_is_reported_but_only_blocks_worsening(risk):
    book = snapshot(stock("GOOG", 160, "Communication Services"))
    verdict = risk.evaluate_trade(book, buy(price=D(101), mark_price=D(100), fee=D(1)))
    assert verdict.ok
    (breach,) = verdict.allowed_existing_breaches
    assert breach.kind == "issuer_cap" and breach.key == "GOOGL"
    assert not risk.evaluate_trade(book, buy(symbol="GOOGL", sector="Communication Services")).ok


def test_same_sector_cannot_add_to_drift_even_when_ticker_differs(risk):
    book = snapshot(stock("BANKA", 140, "Financials"), stock("BANKB", 140, "Financials"),
                    stock("BANKC", 130, "Financial Services"))
    assert risk.evaluate_trade(book, buy()).allowed_existing_breaches[0].kind == "sector_cap"
    assert risk.evaluate_trade(book, buy(sector="Financials")).reasons == ("sector_cap",)


def test_concentration_exact_cap_alerts_without_buy_or_trim_veto(risk):
    book = snapshot(stock("ALFA", 150, "Industrials"))
    assert [(b.kind, b.key, b.at_limit) for b in risk.evaluate_portfolio(book)] == [
        ("issuer_cap", "ALFA", True),
    ]
    assert not risk.plan_remediation(book, broker_action=True)
    assert risk.evaluate_trade(book, buy("BETA")).ok


def test_options_cannot_cross_gross_via_existing_leverage(risk):
    book = snapshot(*(stock(f"NAME{i}", 101, "Industrials") for i in range(10)))
    assert risk.evaluate_trade(book, option()).reasons == ("gross_cap",)


def test_fee_and_unfavorable_fill_do_not_overshoot_stop_budget(risk):
    trade = buy(qty="100", stop_distance=D(".10"), mark_price=D(99), fee=D(1))
    assert risk.evaluate_trade(snapshot(), trade).reasons == ("stop_risk_budget",)
    assert risk.propose_equity_qty(snapshot(), trade) == 99


def test_option_entry_requires_fresh_thesis(risk):
    assert risk.evaluate_trade(snapshot(), option(signal_at=None)).reasons == ("stale_signal",)


def test_two_share_classes_count_as_two_positions_but_one_exposure_bucket(risk):
    holdings = (stock("GOOG", 1, "Communication Services"), stock("GOOGL", 1, "Communication Services"))
    verdict = risk.evaluate_trade(snapshot(*holdings), buy())
    assert verdict.after.positions == 3
    assert verdict.after.issuer_values["GOOGL"] == D(200)
