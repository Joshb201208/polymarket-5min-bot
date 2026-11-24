from datetime import date, datetime, timezone
from decimal import Decimal

from stock_agent.ledger import CashFlow, EquityMark
from stock_agent.performance import close_session, day_pnl, drawdown, realised_fifo, twr
from tests.ledger_helpers import StubBroker, make_ledger, run

T = datetime(2026, 11, 23, tzinfo=timezone.utc)


def mark(d, eq, q="close"):
    return EquityMark(session_date=d, quality=q, equity=Decimal(str(eq)), cash=None, long_mv=None, short_mv=None,
                      options_mv=None, taken_at=T)


def test_day_pnl_uses_previous_close_mark_not_legacy():
    marks = [mark(date(2026, 11, 19), 1000, "legacy"), mark(date(2026, 11, 20), 1100)]
    pnl, pct = day_pnl(marks, Decimal("1155"), date(2026, 11, 23))
    assert pnl == Decimal("55") and pct == Decimal("5.0000")
    assert day_pnl([mark(date(2026, 11, 20), 1, "legacy")], Decimal(2), date(2026, 11, 23)) == (None, None)


def test_twr_removes_deposits():
    marks = [mark(date(2026, 11, 2), 1000), mark(date(2026, 11, 3), 2100)]
    flows = [CashFlow("a", date(2026, 11, 3), Decimal("1000"), "JNLC")]
    assert twr(marks, flows) == Decimal("10.0000")


def test_drawdown_from_close_marks():
    marks = [mark(date(2026, 11, 2), 1000), mark(date(2026, 11, 3), 1200), mark(date(2026, 11, 4), 900, "legacy")]
    dd, hwm = drawdown(marks, Decimal("1080"))
    assert hwm == Decimal("1200") and dd == Decimal("-10.0000")


def test_realised_fifo_stock_short_options_div_fee():
    acts = [
        {"id": "1", "activity_type": "FILL", "symbol": "AAA", "side": "buy", "qty": "10", "price": "10",
         "transaction_time": "2026-11-02T15:00:00Z"},
        {"id": "2", "activity_type": "FILL", "symbol": "AAA", "side": "buy", "qty": "10", "price": "12",
         "transaction_time": "2026-11-03T15:00:00Z"},
        {"id": "3", "activity_type": "FILL", "symbol": "AAA", "side": "sell", "qty": "15", "price": "13",
         "transaction_time": "2026-11-04T15:00:00Z"},        # stop fill: 10*3 + 5*1 = 35
        {"id": "4", "activity_type": "FILL", "symbol": "PPP", "side": "sell_short", "qty": "5", "price": "20",
         "transaction_time": "2026-11-04T15:00:00Z"},
        {"id": "5", "activity_type": "FILL", "symbol": "PPP", "side": "buy", "qty": "5", "price": "18",
         "transaction_time": "2026-11-05T15:00:00Z"},        # +10
        {"id": "6", "activity_type": "FILL", "symbol": "AAA261127C00100000", "side": "buy", "qty": "1",
         "price": "2.5", "transaction_time": "2026-11-05T15:00:00Z"},
        {"id": "7", "activity_type": "OPEXP", "symbol": "AAA261127C00100000", "qty": "-1", "date": "2026-11-27"},
        {"id": "8", "activity_type": "DIV", "symbol": "AAA", "net_amount": "1.25", "date": "2026-11-10"},
        {"id": "9", "activity_type": "FEE", "net_amount": "-0.50", "date": "2026-11-10"},
    ]
    r = realised_fifo(acts)
    assert r.by_symbol["AAA"] == Decimal("35") and r.by_symbol["PPP"] == Decimal("10")
    assert r.by_symbol["AAA261127C00100000"] == Decimal("-250")
    assert r.total == Decimal("-205") and r.dividends == Decimal("1.25") and r.fees == Decimal("-0.50")


def test_close_session_records_mark_and_shadow_stops(tmp_path):
    from stock_agent.risk_manager import ShadowDailyBar

    b = StubBroker(cash=10000)
    b.act("JNLC", None, ts="2026-11-01T00:00:00Z", net_amount="10000")
    b.fill("AAA", "buy", 10, 100, ts="2026-11-16T15:00:00Z")
    b.pos("AAA", 10, 100, 95)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("close", rebuild=True))
    pid = led.open_position_meta("AAA").position_id
    led.journal.append("entry.filled", position_id=pid, symbol="AAA", session_date="2026-11-16",
                       fill_price="100", qty="10", stop_fraction="0.07", stop_price="93", atr="2", source="research",
                       fill_id="f1")
    calls = []

    async def bars(sym, start, end):
        calls.append((sym, start, end))
        return [ShadowDailyBar(date(2026, 11, d), Decimal(100), Decimal(101), Decimal(lo), Decimal(lo + 1))
                for d, lo in ((16, 99), (17, 95), (18, 92), (19, 89), (20, 94), (23, 94), (24, 50))]

    rep = run(close_session(led, snap, date(2026, 11, 23), daily_bars=bars))
    assert rep.deposits == Decimal("10000") and rep.shadow_rows == 1
    assert led.equity_marks("close")[0].session_date == date(2026, 11, 23)
    [row] = list(led.journal.read(kinds={"shadow.stop"}))
    assert row["through_session"] == "2026-11-23"
    variants = {x["variant"]: x for x in row["results"]}
    assert variants["fixed_7"]["stopped"] and variants["no_stop"]["stopped"] is False
    again = run(close_session(led, snap, date(2026, 11, 23), daily_bars=bars))
    assert again.shadow_rows == 0 and len(calls) == 1          # idempotent per anchor and session
    assert len(led.equity_marks("close")) == 1


def test_close_session_survives_bar_errors(tmp_path):
    b = StubBroker()
    b.fill("AAA", "buy", 1, 10, ts="2026-11-16T15:00:00Z")
    b.pos("AAA", 1, 10)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("x", rebuild=True))
    led.journal.append("entry.filled", position_id=led.open_position_meta("AAA").position_id, symbol="AAA",
                       session_date="2026-11-16", fill_price="10", qty="1", stop_fraction="0.1")

    async def broken(*a):
        raise RuntimeError("provider down")

    rep = run(close_session(led, snap, date(2026, 11, 23), daily_bars=broken))
    assert rep.shadow_rows == 0 and any("provider down" in w for w in rep.warnings)


def test_shadow_waits_for_the_session_bar(tmp_path):
    from stock_agent.risk_manager import ShadowDailyBar

    b = StubBroker()
    b.fill("AAA", "buy", 1, 100, ts="2026-11-16T15:00:00Z")
    b.pos("AAA", 1, 100)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("x", rebuild=True))
    led.journal.append("entry.filled", position_id=led.open_position_meta("AAA").position_id, symbol="AAA",
                       session_date="2026-11-16", fill_price="100", qty="1", stop_fraction="0.07", fill_id="f")
    have = [ShadowDailyBar(date(2026, 11, d), *(Decimal(x) for x in (100, 101, 99, 100))) for d in (16, 17, 18, 19, 20)]

    async def bars(sym, start, end):
        return list(have)

    rep = run(close_session(led, snap, date(2026, 11, 23), daily_bars=bars))
    assert rep.shadow_rows == 0 and any("no completed bar" in w for w in rep.warnings)
    have.append(ShadowDailyBar(date(2026, 11, 23), *(Decimal(x) for x in (100, 101, 99, 100))))
    assert run(close_session(led, snap, date(2026, 11, 23), daily_bars=bars)).shadow_rows == 1


def test_close_session_retry_keeps_mark_and_journal(tmp_path):
    b = StubBroker(cash=1000)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("close"))
    run(close_session(led, snap, date(2026, 11, 23)))
    b.cash = 999
    snap2 = run(led.refresh("later"))
    rep = run(close_session(led, snap2, date(2026, 11, 23)))
    assert rep.equity == Decimal("1000") and led.equity_marks("close")[0].equity == Decimal("1000")
    assert len(list(led.journal.read(kinds={"performance.close"}))) == 1


def test_shadow_skips_when_bars_on_split_basis(tmp_path):
    from stock_agent.risk_manager import ShadowDailyBar

    b = StubBroker()
    b.fill("AAA", "buy", 1, 400, ts="2026-11-16T15:00:00Z")
    b.pos("AAA", 4, 100)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("x", rebuild=True))
    led.journal.append("entry.filled", position_id=led.open_position_meta("AAA").position_id, symbol="AAA",
                       session_date="2026-11-16", fill_price="400", qty="1", stop_fraction="0.07", fill_id="f")
    # 4:1 split later: adjusted bars sit near 100 while the raw fill was 400
    have = [ShadowDailyBar(date(2026, 11, d), *(Decimal(x) for x in (100, 101, 99, 100)))
            for d in (16, 17, 18, 19, 20, 23)]

    async def bars(sym, start, end):
        return list(have)

    rep = run(close_session(led, snap, date(2026, 11, 23), daily_bars=bars))
    assert rep.shadow_rows == 0 and not list(led.journal.read(kinds={"shadow.stop"}))
    [skip] = list(led.journal.read(kinds={"shadow.stop.skipped"}))
    assert skip["reason"] == "price_basis_mismatch"
    run(close_session(led, snap, date(2026, 11, 24), daily_bars=bars))
    assert len(list(led.journal.read(kinds={"shadow.stop.skipped"}))) == 1
