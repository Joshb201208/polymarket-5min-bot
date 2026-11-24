"""Options sections of the owner reports (ported from the strategy review)."""
from __future__ import annotations

from asyncio import run
from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, OptionLeg
from stock_agent.options_book import Leg, Structure, open_pnl, structure_mark
from stock_agent.reporting import ReportBuilder
from tests.ledger_helpers import make_ledger

NOW = datetime(2026, 11, 23, 21, 25, tzinfo=timezone.utc)
C100, C105 = "XYZ261218C00100000", "XYZ261218C00105000"


def leg(sym, strike, qty, price):
    return OptionLeg(sym, "XYZ", "call", D(strike), date(2026, 12, 18), D(qty), 100, D("1"),
                     D(price), True, D(price) * D(qty) * 100)


def snapshot(legs=(), option_value="0"):
    return BookSnapshot(NOW, "fixture", D(10000), D(10000), D(0), D(0), D(option_value),
                        None, None, (), tuple(legs), (), (), "live")


def debit_spread(qty=2, **kw):
    kw.setdefault("source", "scan")
    return Structure("d1", "XYZ", "bull_call_spread", "bullish", qty, "2026-12-18",
                     [Leg(C100, "buy", "call", 100, "2026-12-18"), Leg(C105, "sell", "call", 105, "2026-12-18")],
                     state="open", entry_debit=2.0, width=5, max_loss=400, conviction=8, **kw)


def adopted_credit(qty=2, **kw):
    return Structure("a1", "XYZ", "unknown", "bearish", qty, "2026-12-18",
                     [Leg(C100, "sell", "call", 100, "2026-12-18"), Leg(C105, "buy", "call", 105, "2026-12-18")],
                     state="open", source="adopted", entry_debit=-1.5, width=5, max_loss=700, **kw)


def builder(tmp_path, world, structs):
    return ReportBuilder(Config(), ledger=make_ledger(tmp_path, world),
                         options_store=NS(load=lambda: list(structs)), clock=lambda: NOW)


def test_structure_maths_signs():
    d = debit_spread()
    m = structure_mark(d, {C100: 4.0, C105: 1.0})
    assert m == 3.0 and open_pnl(d, m) == 200.0  # (3 - 2) x 100 x 2
    a = adopted_credit()
    m = structure_mark(a, {C100: 1.0, C105: 0.5})
    assert m == -0.5 and open_pnl(a, m) == 200.0  # credit 1.5 decayed to 0.5
    assert open_pnl(a, structure_mark(a, {C100: 3.0, C105: 0.5})) == -200.0
    assert structure_mark(d, {C100: 4.0}) is None


def test_option_pnl_is_per_structure_not_net_market_value(world, tmp_path):
    rb = builder(tmp_path, world, [debit_spread()])
    snap = snapshot([leg(C100, 100, 2, "4"), leg(C105, 105, -2, "1")], option_value="600")
    sleeves, _, _, _ = rb._compile_sleeves_and_orders(snap)
    assert sleeves["OPTIONS"]["unrealized_pnl"] == 200.0  # not the 600 market value


def test_net_market_value_alone_is_not_unrealized_pnl(world, tmp_path):
    rb = ReportBuilder(Config(), ledger=make_ledger(tmp_path, world), clock=lambda: NOW)
    sleeves, _, _, _ = rb._compile_sleeves_and_orders(snapshot(option_value="-200"))
    assert sleeves["OPTIONS"]["unrealized_pnl"] != -200


def test_unmarked_structure_is_counted_not_guessed(world, tmp_path):
    rb = builder(tmp_path, world, [debit_spread()])
    sleeves, _, _, _ = rb._compile_sleeves_and_orders(snapshot([leg(C100, 100, 2, "4")]))
    assert sleeves["OPTIONS"]["unrealized_pnl"] == 0.0
    assert sleeves["OPTIONS"]["unrealized_unknown_count"] == 1


def test_adopted_is_not_scheduled_and_not_in_strategy_stats(world, tmp_path):
    closed = adopted_credit(); closed.state = "closed"; closed.realized_pnl = 200.0
    rb = builder(tmp_path, world, [adopted_credit(), closed])
    snap = snapshot([leg(C100, 100, -2, "1"), leg(C105, 105, 2, "0.5")], option_value="-100")
    sleeves, by_origin, by_conv, _ = rb._compile_sleeves_and_orders(snap)
    assert by_origin["SCHEDULED"]["fills"] == 0
    assert sleeves["OPTIONS"]["fills"] == 0 and sleeves["OPTIONS"]["realized_pnl"] == 0.0
    assert sleeves["OPTIONS"]["unrealized_pnl"] == 0.0
    assert by_origin["ADOPTED"]["fills"] == 2
    assert by_origin["ADOPTED"]["realized_pnl"] == 200.0
    assert by_origin["ADOPTED"]["unrealized_pnl"] == 200.0


def test_adopted_tagged_in_holdings(world, tmp_path):
    rb = builder(tmp_path, world, [adopted_credit()])
    text = str(run(rb.build_morning_brief(snapshot=snapshot([leg(C100, 100, -2, "1"), leg(C105, 105, 2, "0.5")]))))
    assert "adopted/legacy" in text and "scheduled)" not in text


def test_unknown_max_loss_is_never_summed_as_zero(world, tmp_path):
    repair = Structure("r1", "XYZ", "unknown", None, 1, "2026-12-18", [Leg(C100, "sell", "call", 100, "2026-12-18")],
                       state="repair", source="adopted", entry_debit=-1.5, max_loss=None)
    rb = builder(tmp_path, world, [repair, debit_spread()])
    snap = snapshot([leg(C100, 100, 1, "1")], option_value="-200")
    daily = str(run(rb.build_daily_summary(snapshot=snap)))
    assert "Open Max Risk $0" not in daily
    assert "1 structure(s) with unknown max loss" in daily
    brief = str(run(rb.build_morning_brief(snapshot=snap)))
    assert "max possible loss unknown" in brief and "unknown max loss" in brief
    embeds, sc = run(rb.build_weekly_scorecard(snapshot=snap))
    assert sc["account"]["open_option_max_loss"] == 400.0
    assert sc["account"]["open_option_unknown_risk_count"] == 1
    assert sc["account"]["breach_count"] >= 1
    assert "unknown max loss" in str(embeds)


def _repair_only():
    return Structure("r1", "XYZ", "unknown", None, 1, "2026-12-18", [Leg(C100, "sell", "call", 100, "2026-12-18")],
                     state="repair", source="adopted", entry_debit=-1.5, max_loss=None)


def test_all_unknown_risk_has_no_leading_zero(world, tmp_path):
    rb = builder(tmp_path, world, [_repair_only()])
    daily = str(run(rb.build_daily_summary(snapshot=snapshot([leg(C100, 100, -1, "1")]))))
    assert "unknown (1 structure(s); no known maximum)" in daily
    assert "$0 known" not in daily


def test_broker_legs_with_failed_or_empty_book_are_unknown_risk(world, tmp_path):
    snap = snapshot([leg(C100, 100, -2, "1"), leg(C105, 105, 2, "0.5")], option_value="-100")

    def boom():
        raise OSError("store down")

    for store in (NS(load=boom), NS(load=lambda: [])):
        rb = ReportBuilder(Config(), ledger=make_ledger(tmp_path / str(id(store)), world),
                           options_store=store, clock=lambda: NOW)
        daily = str(run(rb.build_daily_summary(snapshot=snap)))
        assert "Open Max Risk $0 " not in daily and "no known maximum" in daily
        brief = str(run(rb.build_morning_brief(snapshot=snap)))
        assert "no known maximum" in brief
        _, sc = run(rb.build_weekly_scorecard(snapshot=snap))
        assert sc["account"]["open_option_unknown_risk_count"] == 1


def test_pnl_is_unknown_when_broker_quantity_differs_from_book(world, tmp_path):
    rb = builder(tmp_path, world, [debit_spread()])  # book expects +2 / -2
    snap = snapshot([leg(C100, 100, 2, "4"), leg(C105, 105, -1, "1")])  # short leg partly gone
    sleeves, _, _, _ = rb._compile_sleeves_and_orders(snap)
    assert sleeves["OPTIONS"]["unrealized_pnl"] == 0.0
    assert sleeves["OPTIONS"]["unrealized_unknown_count"] == 1
    daily = str(run(rb.build_daily_summary(snapshot=snap)))
    assert "unknown max loss" in daily  # the unexplained leg is uncovered risk
