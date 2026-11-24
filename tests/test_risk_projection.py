from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, Holding, OpenOrder, OptionLeg, PositionMeta
from stock_agent.risk_manager import (
    CanonicalSector, DrawdownState, OptionCommitment, RiskControls, RiskManager,
    TradeIntent, TradeKind, update_drawdown,
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
    return RiskManager(Config(MARGIN_ENABLED=False))


@dataclass(frozen=True)
class _MarginBook(BookSnapshot):
    # These match the ledger's broker-mapped margin fields.
    regt_buying_power: Decimal | None = None
    maintenance_margin: Decimal | None = None
    options_buying_power: Decimal | None = None
    non_marginable_buying_power: Decimal | None = None
    margin_call: bool | None = None


def margin_book(*holdings, regt="25000", maintenance=None, **kwargs):
    return _MarginBook(**(vars(snapshot(*holdings, **kwargs)) | {
        "regt_buying_power": D(regt) if regt is not None else None,
        "maintenance_margin": D(maintenance) if maintenance is not None else None,
    }))


def test_margin_allows_negative_cash_with_reserved_overnight_buying_power():
    risk = RiskManager(Config())
    controls = RiskControls(option_commitments=(OptionCommitment("pending-opt", D(400), D(400)),))
    pending = order("BETA", qty="20", done="10")
    book = margin_book(cash="100", regt="4400", orders=(pending, pending))
    verdict = risk.evaluate_trade(book, buy(), controls=controls)
    assert verdict.ok and verdict.after.free_cash == D(-2300)
    assert verdict.after.overnight_buying_power == D(2000)
    assert risk.evaluate_trade(book, buy(qty="11"), controls=controls).reasons == ("buying_power_floor",)
    assert risk.propose_equity_qty(book, buy(), controls=controls) == 10
    assert risk.evaluate_trade(replace(book, regt_buying_power=None), buy()).reasons == ("buying_power_floor",)


def test_margin_gross_cap_and_cash_only_kill_switch():
    holdings = tuple(
        stock(f"HOLD{i:02d}", qty, sector)
        for i, (qty, sector) in enumerate(
            [(140, sector) for sector in ("Energy", "Materials", "Industrials",
                                         "Financials", "Health Care", "Technology", "Utilities") for _ in range(2)]
        )
    )
    book = margin_book(*holdings, cash="50000", maintenance="60000")
    risk = RiskManager(Config())
    assert risk.evaluate_trade(book, buy(qty="40")).ok
    assert risk.evaluate_trade(book, buy(qty="41")).reasons == ("gross_cap",)
    cash_only = RiskManager(Config(MARGIN_ENABLED=False))
    assert cash_only.evaluate_trade(book, buy(qty="40")).reasons == ("gross_cap",)
    assert cash_only.margin_status(book) is None
    assert not cash_only.plan_margin_trim(book, force=True)


def test_margin_buffer_uses_broker_base_and_projected_pending_and_trade():
    risk = RiskManager(Config())
    pending = order("BETA", qty="10", done="0")
    book = margin_book(maintenance="74400", orders=(pending,))
    assert risk.margin_status(book).excess_pct == D(".256")
    verdict = risk.evaluate_trade(book, buy())
    assert verdict.ok and verdict.after.maintenance_margin == D(75000)
    refused = risk.evaluate_trade(book, buy(qty="11"))
    assert refused.reasons == ("margin_buffer",)


def test_margin_fallback_rates_for_ordinary_cheap_and_leveraged_holdings():
    risk = RiskManager(Config())
    book = margin_book(stock("AAPL", 10, "Technology"), stock("CHEAP", 1000, "Utilities", price="2"),
                       stock("QLD", 10, None), stock("TQQQ", 10, None))
    assert risk.margin_status(book).maintenance_margin == D(3550)


def test_margin_trim_ranks_by_conviction_then_loss_and_never_sells_pledged_shares():
    risk = RiskManager(Config())
    weakest = replace(stock("WEAK", 140, "Industrials", available=40), avg_cost=D(120))
    tied_loss = replace(stock("LOSS", 200, "Financials", available=0), avg_cost=D(150))
    tied_loss = replace(tied_loss, meta=replace(tied_loss.meta, stop_order_id="owned-stop"))
    owned_stop = replace(order("LOSS", qty="200", done="0", order_id="owned-stop"),
                         side="sell", type="stop", time_in_force="gtc")
    strong = replace(stock("STRONG", 400, "Utilities"), avg_cost=D(300))
    book = margin_book(weakest, tied_loss, strong, orders=(owned_stop,), legs=(leg("WEAK", -1),),
                       maintenance="86000")
    controls = RiskControls(holding_convictions={"WEAK": 6, "LOSS": 6, "STRONG": 9})
    assert risk.margin_status(book).needs_trim
    assert [(t.symbol, t.qty, t.reason) for t in risk.plan_margin_trim(book, controls=controls)] == [
        ("LOSS", D(200), "margin_buffer"), ("WEAK", D(40), "margin_buffer"),
        ("STRONG", D(127), "margin_buffer"),
    ]
    improving = replace(book, maintenance_margin=D(80000))
    assert not risk.plan_margin_trim(improving, controls=controls)
    assert risk.plan_margin_trim(improving, controls=controls, force=True)[0].qty == D(167)
    assert risk.plan_margin_trim(replace(improving, margin_call=True), controls=controls)


def test_margin_drawdown_throttle_never_adds_to_debit_but_can_trade_at_zero_cash():
    risk = RiskManager(Config())
    book = margin_book(cash="1000")
    controls = RiskControls(drawdown=DrawdownState(drawdown=D(".15")))
    permitted = risk.evaluate_trade(book, buy(), controls=controls)
    assert permitted.ok and permitted.after.free_cash == 0
    assert risk.evaluate_trade(book, buy(qty="11"), controls=controls).reasons == (
        "drawdown_no_new_borrowing",
    )
    assert risk.propose_equity_qty(book, buy(), controls=controls) == 10


def test_margin_options_use_nonmarginable_funds_and_short_cover_ignores_cash():
    risk = RiskManager(Config())
    book = margin_book(cash="-100")
    assert risk.evaluate_trade(book, option()).reasons == ("options_buying_power",)
    nonmargin = replace(book, non_marginable_buying_power=D(4000))
    assert risk.evaluate_trade(nonmargin, option()).ok
    assert risk.evaluate_trade(replace(nonmargin, regt_buying_power=D(0)), option()).reasons == (
        "buying_power_floor",
    )
    assert risk.evaluate_trade(replace(nonmargin, options_buying_power=D(2999)), option()).reasons == (
        "options_buying_power",
    )
    short = replace(book, holdings=(stock("SHORT", -20, "Energy"),), margin_call=True)
    cover = TradeIntent(TradeKind.BUY_TO_COVER, "SHORT", D(20), D(100))
    assert risk.evaluate_trade(short, cover, controls=RiskControls(pause_new_buys=True)).ok
    assert risk.evaluate_trade(short, replace(cover, qty=D(21))).reasons == ("cover_exceeds_short",)


@pytest.mark.parametrize("margin_enabled", [False, True])
def test_debit_close_never_checks_cash_or_buying_power(margin_enabled):
    risk = RiskManager(Config(MARGIN_ENABLED=margin_enabled))
    short_call = replace(leg("ALFA", -1), occ_symbol="ALFA261218C00100000")
    book = replace(margin_book(cash="-100", regt="0", legs=(short_call,)), options_buying_power=D(0))
    close = TradeIntent(TradeKind.OPTION_CLOSE, short_call.occ_symbol, D(1),
                        option_cash_debit=D(500), fee=D(1))
    assert risk.evaluate_trade(book, close, controls=RiskControls(pause_new_buys=True)).ok
    assert risk.evaluate_trade(book, replace(close, qty=D(2))).reasons == ("no_option_to_close",)


def test_projected_full_order_cash_floor_ignores_buying_power(risk):
    book = snapshot(cash="2500")
    assert risk.propose_equity_qty(book, buy()) == 5
    fits = risk.evaluate_trade(book, buy(qty="5"))
    assert fits.ok and fits.after.free_cash == D(2000)
    assert risk.evaluate_trade(book, buy(qty="6")).reasons == ("cash_floor",)


def test_drawdown_state_and_order_projection_use_configured_thresholds():
    class TightDrawdown(Config):
        DRAWDOWN_ALERT_PCT = .03
        DRAWDOWN_THROTTLE_PCT = .05
        DRAWDOWN_PAUSE_PCT = .12
        DRAWDOWN_RECOVERY_SESSIONS = 2

    cfg = TightDrawdown()
    risk = RiskManager(cfg)
    initial = update_drawdown(DrawdownState(), "2026-11-23", D(100), config=cfg)
    throttled = update_drawdown(initial, "2026-11-24", D(94), config=cfg)
    assert throttled.should_alert(cfg) and throttled.is_throttled(cfg)
    assert not throttled.throttled  # The compatibility property uses the default policy.
    controls = RiskControls(drawdown=throttled)
    assert risk.propose_equity_qty(snapshot(), buy(), controls=controls) == 50
    assert risk.evaluate_trade(snapshot(), option(), controls=controls).reasons == ("drawdown_pause",)
    paused = update_drawdown(throttled, "2026-11-25", D(87), config=cfg)
    assert paused.paused and DrawdownState.from_record(paused.to_record(), config=cfg) == paused
    recovering = update_drawdown(paused, "2026-11-27", D(97), config=cfg)
    assert recovering.paused
    assert not update_drawdown(recovering, "2026-11-30", D(97), config=cfg).paused


def test_unpriced_holding_is_reported_without_suppressing_known_trim(risk):
    book = snapshot(stock("ALFA", 200, "Industrials"),
                    stock("BLND", 100, "Technology", price=None))
    breaches = risk.evaluate_portfolio(book, include_pending=False)
    assert {(b.kind, b.key) for b in breaches} == {("issuer_cap", "ALFA"), ("unpriced_holding", "BLND")}
    first = risk.advance_drift_counters(breaches, {})
    assert first == {("issuer_cap", "ALFA"): 1}
    assert not risk.plan_remediation(book, consecutive_closes=first)
    second = risk.advance_drift_counters(breaches, first)
    (trim,) = risk.plan_remediation(book, consecutive_closes=second)
    assert (trim.symbol, trim.qty, trim.reason) == ("ALFA", D(70), "issuer_cap")
    assert risk.advance_drift_counters((), second) == {}
    assert not risk.evaluate_trade(book, buy("NEW")).ok  # Entries still fail closed.


def test_broker_delivery_repairs_only_attributable_exposure(risk):
    unrelated = snapshot(stock("ALFA", 200, "Industrials"),
                         stock("BETA", 100, "Technology"))
    assert not risk.plan_remediation(unrelated, broker_action=True, broker_symbol="BETA")
    delivered = replace(unrelated, holdings=(unrelated.holdings[0], stock("BETA", 180, "Technology")))
    (trim,) = risk.plan_remediation(delivered, broker_action=True, broker_symbol="BETA")
    assert (trim.symbol, trim.qty, trim.reason) == ("BETA", D(30), "issuer_cap")


def test_limit_bound_and_marked_slippage_are_conservative(risk):
    trade = buy(qty="10", price=D(101), mark_price=D(100), fee=D(1))
    verdict = risk.evaluate_trade(snapshot(), trade)
    assert verdict.ok
    assert verdict.after.equity == D(99989)
    assert verdict.after.free_cash == D(88989)
    assert verdict.after.gross_exposure == D(1010)


def test_post_trade_sector_catches_aliases_for_new_buys_and_adds(risk):
    book = snapshot(stock("BANKA", 149, "Financials"), stock("BANKB", 149, "Financial Services"),
                    stock("BANKC", 83, "Financials"), stock("ALFA", 10, "Financial Services", stop="92"))
    add = buy(kind=TradeKind.ADD, sector="Financials", last_filled_conviction=10,
              thesis_change="strengthened")
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
    trade = buy(kind=TradeKind.ADD, effective_conviction=8, raw_conviction=8, last_filled_conviction=8,
                thesis_change="unchanged")
    assert risk.evaluate_trade(snapshot(stock("ALFA", 20, "Industrials", stop="92")), trade).ok
    assert risk.evaluate_trade(snapshot(stock("ALFA", 20, "Industrials", stop="100")), trade).reasons == (
        "top_up_below_stop",
    )
    assert risk.evaluate_trade(snapshot(stock("ALFA", 63, "Industrials", stop="92")), trade).reasons == (
        "near_target",
    )
    book = snapshot(stock("ALFA", 20, "Industrials", stop="92"))
    for changes, reason in (
        ({"last_filled_conviction": 9}, "top_up_conviction"),
        ({"last_filled_conviction": None}, "top_up_conviction"),
        ({"effective_conviction": 7, "raw_conviction": 7, "last_filled_conviction": 7}, "top_up_conviction"),
        ({"thesis_change": "weakened"}, "top_up_thesis_change"),
        ({"thesis_change": None}, "top_up_thesis_change"),
    ):
        candidate = replace(trade, **changes)
        assert risk.propose_equity_qty(book, candidate) == 0
        assert risk.evaluate_trade(book, candidate).reasons == (reason,)
    assert risk.evaluate_trade(snapshot(stock("ALFA", 20, "Industrials")), trade).reasons == (
        "top_up_below_stop",
    )


def test_top_up_cannot_overshoot_target_with_fresher_mark_or_working_shares(risk):
    trade = buy(kind=TradeKind.ADD, effective_conviction=8, raw_conviction=8, last_filled_conviction=8,
                thesis_change="unchanged", price=D("101"), mark_price=D("102"))
    book = snapshot(stock("ALFA", 40, "Industrials", stop="92"),
                    orders=(order("ALFA", qty="6", done="0", limit="101"),))
    # Target $7,000, held + already pending $4,692, highest permissible new spend $2,244 (22 shares).
    assert risk.propose_equity_qty(book, trade) == 22
    assert risk.evaluate_trade(book, replace(trade, qty=D(22))).ok
    assert risk.evaluate_trade(book, replace(trade, qty=D(23))).reasons == ("sizing_limit",)


def test_position_count_blocks_new_names_but_not_top_up(risk):
    book = snapshot(*(stock(f"STOCK{i}", 1, "Financials", stop="92") for i in range(20)))
    assert risk.evaluate_trade(book, buy()).reasons == ("position_count",)
    assert risk.evaluate_trade(book, buy(symbol="STOCK1", sector="Financials", kind=TradeKind.ADD,
                                         last_filled_conviction=10, thesis_change="strengthened")).ok


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


def test_any_unexpected_short_blocks_buys_but_not_cover_even_in_cash_only_mode(risk):
    book = snapshot(stock("ALFA", -2, "Industrials"), cash="250")
    assert risk.evaluate_trade(book, buy("BETA")).reasons == ("unexpected_short",)
    repair, = risk.plan_remediation(book)
    assert repair.kind == TradeKind.BUY_TO_COVER and repair.qty == 2
    assert risk.evaluate_trade(book, repair, controls=RiskControls(pause_new_buys=True)).ok
    assert not risk.evaluate_trade(book, replace(repair, qty=D(3))).ok
    assert risk.evaluate_trade(book, replace(repair, price=D(130))).ok


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
