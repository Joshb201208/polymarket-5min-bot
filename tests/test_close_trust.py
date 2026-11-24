"""Unit tests for the drawdown-close valuation rule (stock_agent/close_trust.py).

Rule: notes/trusted_close_gap.md sections 3 and 7. All numbers are synthetic round values.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from dataclasses import replace

import pytest

from stock_agent import close_trust as ct
from stock_agent.ledger import BookSnapshot, Diff, DiffKind, Holding, OptionLeg, Severity

D = Decimal
EXP = date(2026, 11, 27)
EQUITY = D(100_000)


def stock(symbol, qty, price=D(100), ok=True):
    return Holding(symbol=symbol, qty=D(qty), qty_available=D(qty), avg_cost=D(100), price=price if ok else None,
                   price_ok=ok, market_value=D(qty) * (price or 0), lastday_price=None)


def leg(underlying, strike, qty, *, right="call", price=None, ok=None, mv=D(0), reported=True, root=None,
        expiry=EXP):
    cp = "C" if right == "call" else "P"
    sym = f"{root or underlying}{expiry:%y%m%d}{cp}{int(strike * 1000):08d}"
    if ok is None:
        ok = price is not None
    return OptionLeg(occ_symbol=sym, underlying=root or underlying, right=right, strike=D(strike), expiry=expiry,
                     qty=D(qty), multiplier=100, avg_cost=D(1), price=price, price_ok=ok, market_value=D(mv),
                     market_value_reported=reported)


def snap(holdings=(), legs=(), diffs=(), equity=EQUITY):
    invalid = any(not h.price_ok for h in holdings) or any(not item.price_ok for item in legs)
    return BookSnapshot(
        as_of=datetime(2026, 11, 23, 21, 5, tzinfo=timezone.utc), run_id="rc-test", equity=D(equity),
        cash=D(equity), long_mv=D(0), short_mv=D(0), options_mv=D(0), last_equity=None, buying_power=None,
        holdings=tuple(holdings), legs=tuple(legs), open_orders=(), diffs=tuple(diffs),
        quality="indicative" if invalid else "live")


def debit_pair(underlying="AAA", lo=100, hi=110, *, long_mv=0, short_mv=-200, short_price=D(2)):
    return [leg(underlying, lo, 1, mv=long_mv), leg(underlying, hi, -1, price=short_price, mv=short_mv)]


# ------------------------------------------------------------------- the 23 Nov shape
def test_23nov_shape_trusted_with_zero_haircut():
    book = snap(
        holdings=[stock("BBB", 100), stock("CCC", -10), stock("DDD", -50)],
        legs=[*debit_pair("AAA", 100, 120, short_mv=-400, short_price=D(4)),
              *debit_pair("BBB", 50, 70, short_mv=-5, short_price=D("0.05")),
              leg("BBB", 45, -1, price=D(2), mv=-200, expiry=date(2026, 12, 11))],  # covered call, priced
    )
    trust = ct.assess_close(book)
    assert trust.trusted and trust.haircut == 0 and trust.equity == EQUITY
    assert trust.conservative and trust.basis == "conservative_estimate"
    assert len(trust.unpriced_option_legs) == 2
    detail = trust.detail()
    assert detail["conservative"] is True and "conservative estimate: 2 option legs unpriced" in detail["summary"]
    assert {row["group"] for row in detail["legs"]} == {"debit_vertical"}
    assert book.quality == "indicative"    # the raw snapshot is untouched
    copy = trust.close_snapshot(book)
    assert copy.quality == "live" and copy.equity == EQUITY and book.quality == "indicative"


def test_fully_live_snapshot_is_broker_equity():
    trust = ct.assess_close(snap(holdings=[stock("AAA", 10)]))
    assert trust.trusted and trust.equity == EQUITY and not trust.conservative


# ------------------------------------------------------------------- debit / credit verticals
def test_debit_vertical_positive_net_is_removed():
    # unpriced short at mv 0, priced long: haircut = the long's mv
    book = snap(legs=[leg("AAA", 100, 1, price=D(3), mv=300), leg("AAA", 110, -1, mv=0)])
    trust = ct.assess_close(book)
    assert trust.trusted and trust.haircut == 300 and trust.equity == EQUITY - 300


def test_crossed_debit_vertical_has_zero_haircut():
    # crossed: the higher call priced above the lower; the debit net is negative
    book = snap(legs=[leg("AAA", 100, 1, price=D(1), ok=False, mv=100),
                      leg("AAA", 110, -1, price=D(3), ok=False, mv=-300)])
    trust = ct.assess_close(book)
    assert trust.trusted and trust.haircut == 0


def test_put_debit_vertical_uses_higher_strike_long():
    book = snap(legs=[leg("AAA", 110, 1, right="put", mv=0),
                      leg("AAA", 100, -1, right="put", price=D(1), mv=-100)])
    assert ct.assess_close(book).trusted


@pytest.mark.parametrize("right, long_k, short_k", [("call", 110, 100), ("put", 100, 110)])
def test_credit_vertical_untrusted(right, long_k, short_k):
    book = snap(legs=[leg("AAA", long_k, 1, right=right, price=D(3), ok=False, mv=300),
                      leg("AAA", short_k, -1, right=right, price=D(1), ok=False, mv=-100)])
    trust = ct.assess_close(book)
    assert not trust.trusted and trust.reason == ct.CLOSE_OPTION_CREDIT_VERTICAL


def test_pair_unpriced_leg_with_nonzero_mv_untrusted():
    book = snap(legs=debit_pair(long_mv=50))
    assert ct.assess_close(book).reason == ct.CLOSE_OPTION_MARK_INCONSISTENT


@pytest.mark.parametrize("which", [0, 1])
def test_pair_leg_missing_market_value_untrusted(which):
    legs = debit_pair()
    legs[which] = leg(legs[which].underlying, legs[which].strike, legs[which].qty, price=legs[which].price,
                      mv=0, reported=False)
    assert ct.assess_close(snap(legs=legs)).reason == ct.CLOSE_OPTION_MARK_MISSING


# ------------------------------------------------------------------- pairing
def test_ambiguous_pairing_untrusted():
    book = snap(legs=[leg("AAA", 100, 1), leg("AAA", 110, -1, price=D(2), mv=-200),
                      leg("AAA", 120, -1, price=D(1), mv=-100)])
    trust = ct.assess_close(book)
    assert not trust.trusted and trust.reason == ct.CLOSE_OPTION_PAIRING_AMBIGUOUS


@pytest.mark.parametrize("order", [0, 1])
def test_shared_partner_is_ambiguous_in_any_order(order):
    # two invalid equal longs and one valid short: each long alone sees one partner, the short sees two
    longs = [leg("AAA", 100, 1), leg("AAA", 105, 1)]
    if order:
        longs.reverse()
    book = snap(legs=[*longs, leg("AAA", 110, -1, price=D(2), mv=-200)])
    trust = ct.assess_close(book)
    assert not trust.trusted and trust.reason == ct.CLOSE_OPTION_PAIRING_AMBIGUOUS
    assert len(trust.symbols) == 3


@pytest.mark.parametrize("legs", [
    # plan_coverage_reviewer: one long reused by two shorts would hide a 20,000 liability at U=300
    [("SPY", 510, -1, "put", None), ("SPY", 520, -1, "put", None), ("SPY", 530, 1, "put", None)],
    [("SPY", 100, 1, "call", D(3)), ("SPY", 110, -1, "call", None), ("SPY", 120, -1, "call", None)],
])
def test_partner_reuse_is_ambiguous(legs):
    built = [leg(u, k, q, right=r, price=p, mv=(D(300) if p else 0)) for u, k, q, r, p in legs]
    trust = ct.assess_close(snap(holdings=[stock("SPY", 10, price=D(300))], legs=built))
    assert not trust.trusted and trust.reason == ct.CLOSE_OPTION_PAIRING_AMBIGUOUS


def test_malformed_market_value_is_not_missing():
    bad = replace(leg("AAA", 100, 1, mv=0, reported=False), market_value_malformed=True)
    trust = ct.assess_close(snap(legs=[bad]))
    assert trust.reason == ct.CLOSE_OPTION_MARK_INCONSISTENT and trust.symbols == (bad.occ_symbol,)


def test_lone_unpriced_long_missing_mv_needs_book_to_add_up():
    # equity carries 5,000 that no reported value explains: the missing leg value may be nonzero
    book = snap(legs=[leg("AAA", 100, 1, mv=0, reported=False)], equity=D(105_000))
    book = replace(book, cash=D(100_000))
    assert ct.assess_close(book).reason == ct.CLOSE_OPTION_MARK_INCONSISTENT


def test_crossed_group_of_three_untrusted():
    book = snap(legs=[leg("AAA", 100, 2, price=D(1), ok=False, mv=200),
                      leg("AAA", 110, -1, price=D(3), ok=False, mv=-300),
                      leg("AAA", 120, -1, price=D(2), mv=-200)])
    assert ct.assess_close(book).reason == ct.CLOSE_OPTION_MARKS_CROSSED


def test_unparseable_option_row_untrusted_even_when_live():
    diff = Diff(DiffKind.UNEXPLAINED, "BADOPT", Severity.WARNING, broker="1",
                explanation="option symbol not parseable")
    assert ct.assess_close(snap(diffs=[diff])).reason == ct.CLOSE_OPTION_UNPARSEABLE


# ------------------------------------------------------------------- lone longs
def test_lone_unpriced_long_mv_zero_or_missing_trusted():
    for reported in (True, False):
        trust = ct.assess_close(snap(legs=[leg("AAA", 100, 1, mv=0, reported=reported)]))
        assert trust.trusted and trust.haircut == 0


def test_lone_unpriced_long_nonzero_mv_untrusted():
    trust = ct.assess_close(snap(legs=[leg("AAA", 100, 1, mv=50)]))
    assert trust.reason == ct.CLOSE_OPTION_MARK_INCONSISTENT and trust.symbols


def test_lone_crossed_long_haircut_is_its_mv():
    trust = ct.assess_close(snap(legs=[leg("AAA", 100, 2, price=D(4), ok=False, mv=800)]))
    assert trust.trusted and trust.haircut == 800 and trust.equity == EQUITY - 800


def test_lone_crossed_long_missing_mv_untrusted():
    trust = ct.assess_close(snap(legs=[leg("AAA", 100, 1, price=D(4), ok=False, mv=0, reported=False)]))
    assert trust.reason == ct.CLOSE_OPTION_MARK_MISSING


# ------------------------------------------------------------------- lone shorts
@pytest.mark.parametrize("right, u, cut", [
    ("call", D(115), D(3000)),   # 15% ITM call: intrinsic 15 x 100 x 2
    ("call", D(85), D(0)),       # 15% OTM call
    ("put", D(85), D(3000)),     # 15% ITM put
])
def test_lone_short_far_from_strike_held_underlying(right, u, cut):
    book = snap(holdings=[stock("AAA", 200, price=u)], legs=[leg("AAA", 100, -2, right=right, mv=0)])
    trust = ct.assess_close(book)
    assert trust.trusted and trust.haircut == cut and trust.equity == EQUITY - cut
    (row,) = trust.detail()["legs"]
    assert row["group"] == "lone_short" and row["underlying_source"] == "held_stock_mark"


def test_lone_short_carried_liability_offsets_intrinsic():
    book = snap(holdings=[stock("AAA", 100, price=D(120))], legs=[leg("AAA", 100, -1, mv=-1500)])
    trust = ct.assess_close(book)
    assert trust.haircut == 500   # 2000 intrinsic - 1500 already carried


def test_lone_short_session_close_for_unheld_underlying():
    book = snap(legs=[leg("AAA", 100, -1, mv=0)])
    assert ct.short_leg_underlyings(book) == ("AAA",)
    trust = ct.assess_close(book, {"AAA": D(120)})
    assert trust.trusted and trust.haircut == 2000
    assert trust.detail()["legs"][0]["underlying_source"] == "session_close"


def test_lone_short_no_underlying_untrusted():
    trust = ct.assess_close(snap(legs=[leg("AAA", 100, -1, mv=0)]))
    assert trust.reason == ct.CLOSE_OPTION_UNDERLYING_UNPRICED


@pytest.mark.parametrize("u", [D(95), D(105), D("109.9"), D(110)])
def test_lone_short_near_money_untrusted(u):
    book = snap(holdings=[stock("AAA", 100, price=u)], legs=[leg("AAA", 100, -1, mv=0)])
    assert ct.assess_close(book).reason == ct.CLOSE_OPTION_NEAR_MONEY


def test_near_money_guard_is_inclusive_at_exactly_ten_percent():
    # |K - U| / U == 0.10 exactly (K 110, U 100): near the money, untrusted
    book = snap(holdings=[stock("AAA", 100, price=D(100))], legs=[leg("AAA", 110, -1, mv=0)])
    assert ct.assess_close(book).reason == ct.CLOSE_OPTION_NEAR_MONEY
    # just beyond 10% is admitted (K 110, U 99.9 -> 10.11%)
    book = snap(holdings=[stock("AAA", 100, price=D("99.9"))], legs=[leg("AAA", 110, -1, mv=0)])
    assert ct.assess_close(book).trusted


def test_unknown_snapshot_quality_untrusted():
    from dataclasses import replace

    book = replace(snap(holdings=[stock("AAA", 10)]), quality="something_new")
    assert ct.assess_close(book).reason == ct.CLOSE_SNAPSHOT_QUALITY_UNKNOWN


def test_persisted_detail_only_for_conservative_marks():
    trust = ct.assess_close(snap(legs=debit_pair()))
    extra = {"maintenance_margin": "1", **trust.detail()}
    restored = ct.persisted_detail(extra)
    assert restored["conservative"] is True and "maintenance_margin" not in restored
    assert restored["unpriced_option_legs"] == list(trust.unpriced_option_legs)
    assert ct.persisted_detail({"maintenance_margin": "1"}) == {}


def test_lone_short_missing_mv_untrusted():
    book = snap(holdings=[stock("AAA", 100, price=D(150))], legs=[leg("AAA", 100, -1, mv=0, reported=False)])
    assert ct.assess_close(book).reason == ct.CLOSE_OPTION_MARK_MISSING


def test_lone_short_adjusted_contract_untrusted():
    book = snap(legs=[leg("AAA", 100, -1, mv=0, root="AAA1")])
    assert ct.assess_close(book, {"AAA1": D(150)}).reason == ct.CLOSE_OPTION_ADJUSTED_CONTRACT


def test_haircut_that_wipes_out_equity_untrusted():
    book = snap(holdings=[stock("AAA", 100, price=D(1000))], legs=[leg("AAA", 100, -2, mv=0)], equity=D(1000))
    assert ct.assess_close(book).reason == ct.CLOSE_HAIRCUT_NONPOSITIVE


# ------------------------------------------------------------------- stock guards
@pytest.mark.parametrize("qty", [100, -100])
def test_unpriced_stock_untrusted(qty):
    trust = ct.assess_close(snap(holdings=[stock("AAA", qty, ok=False)], legs=debit_pair("BBB")))
    assert trust.reason == ct.CLOSE_UNPRICED_STOCK and trust.symbols == ("AAA",)


def test_reason_codes_are_stable_lowercase_names():
    codes = {name: value for name, value in vars(ct).items() if name.startswith("CLOSE_")}
    assert codes and all(value == name.lower() for name, value in codes.items())


def test_scheduler_uses_only_todays_session_close():
    import asyncio
    from types import SimpleNamespace

    from stock_agent.scheduler import StockAgentScheduler

    today, yesterday = date(2026, 11, 23), date(2026, 11, 20)

    class Prices:
        def __init__(self, day):
            self.day, self.calls = day, []

        async def get_daily_bars(self, symbols, *, start_date=None, end_date=None, sessions=60):
            self.calls.append((tuple(symbols), start_date, end_date))
            return {s: [SimpleNamespace(session_date=self.day, close=D(120))] for s in symbols}

    book = snap(legs=[leg("AAA", 100, -1, mv=0)])
    for day, expected in ((today, {"AAA": D(120)}), (yesterday, {})):
        prices = Prices(day)
        closes = asyncio.run(StockAgentScheduler._underlying_session_closes(
            SimpleNamespace(prices=prices), book, today))
        assert closes == expected and prices.calls == [(("AAA",), today, today)]
        trust = ct.assess_close(book, closes)
        assert trust.trusted is bool(expected)
