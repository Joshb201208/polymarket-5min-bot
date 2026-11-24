"""A13: debit balances, margin figures and (estimated) margin interest in the ledger maths."""

from datetime import date, datetime, timezone
from decimal import Decimal as D

from stock_agent.benchmark import BenchmarkTracker
from stock_agent.ledger import EquityMark
from stock_agent.performance import (
    after_cost_marks,
    average_leverage,
    close_session,
    margin_interest,
    margin_rate,
)
from tests.ledger_helpers import StubBroker, make_ledger, run
from tests.test_reporting import StubPriceService, _bar

T = datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc)


def mark(d, eq, cash, long_mv):
    return EquityMark(d, "close", D(eq), D(cash), D(long_mv), D(0), D(0), T)


def margin_book(tmp_path):
    b = StubBroker(cash=-36000.0)
    b.pos("AAA", 100, 1000.0)            # long 100k on 64k equity: borrowed 36k
    b.account_extra = {"regt_buying_power": "28000", "maintenance_margin": "30000",
                       "options_buying_power": "0", "non_marginable_buying_power": "0",
                       "trade_suspended_by_user": False}
    return b, make_ledger(tmp_path, broker=b)


def test_debit_balance_snapshot_fields(tmp_path):
    b, led = margin_book(tmp_path)
    snap = run(led.refresh("t"))
    assert snap.cash == D(-36000) and snap.equity == D(64000)
    assert snap.borrowed == D(36000)
    assert snap.leverage == D(100000) / D(64000)
    assert snap.maintenance_margin == D(30000) and snap.regt_buying_power == D(28000)
    assert snap.margin_excess == D(34000) and snap.margin_excess_pct == D("0.53125")
    assert not [d for d in snap.diffs if d.severity.value == "critical"]
    assert snap.account_flags["trade_suspended_by_user"] is False


def test_close_session_with_debit_balance_keeps_broker_equity(tmp_path):
    b, led = margin_book(tmp_path)
    snap = run(led.refresh("t"))
    rep = run(close_session(led, snap, date(2026, 11, 20)))
    [m] = led.equity_marks("close")
    assert m.equity == D(64000) and m.cash == D(-36000) and m.borrowed == D(36000)
    assert m.extra["maintenance_margin"] == "30000"
    assert rep.equity == D(64000) and rep.borrowed == D(36000)
    assert rep.leverage == D("1.5625") and rep.margin_excess_pct == D("53.1250")
    row = list(led.journal.read(kinds={"performance.close"}))[-1]
    assert row["borrowed"] in ("36000", 36000) or D(str(row["borrowed"])) == D(36000)


def test_estimated_interest_friday_three_days_and_rate_formats():
    marks = [mark(date(2026, 11, 19), 64000, -36000, 100000), mark(date(2026, 11, 20), 64000, -36000, 100000)]
    s = margin_interest(marks, [], rate=D("0.065"))
    assert [(e.session_date.day, e.days, e.amount) for e in s.estimated] == [(19, 1, D("6.50")), (20, 3, D("19.50"))]
    assert s.unposted(date(2026, 11, 23)) == D("26.00") and s.basis == "estimated"
    assert margin_rate(type("C", (), {"MARGIN_INTEREST_RATE_PCT": 6.5})) == D("0.065")
    assert margin_rate(type("C", (), {"MARGIN_INTEREST_RATE_PCT": 0.05})) == D("0.05")
    assert margin_rate(None) == D("0.065")


def test_real_interest_posting_replaces_estimates_no_double_count():
    marks = [mark(date(2026, 11, d), 64000, -36000, 100000) for d in (26, 27, 30)]
    acts = [{"id": "i1", "activity_type": "INT", "date": "2026-11-30", "net_amount": "-60.00"},
            {"id": "i1", "activity_type": "INT", "date": "2026-11-30", "net_amount": "-60.00"},   # replay
            {"id": "i2", "activity_type": "INT", "date": "2026-11-30", "net_amount": "1.25"}]     # credit
    s = margin_interest(marks, acts)
    assert s.postings == [(date(2026, 11, 30), D("60.00"))] and s.credits == D("1.25")
    assert s.unposted(date(2026, 11, 30)) == 0          # covered by the real posting
    assert s.unposted(date(2026, 12, 1)) == D("6.50")   # the 30th's overnight is still an estimate
    assert s.period_cost(date(2026, 11, 26), date(2026, 12, 1)) == D("60.00") + D("6.50") - D("0")
    assert s.basis == "mixed"
    adj = after_cost_marks(marks, s)
    assert [m.equity for m in marks] == [D(64000)] * 3             # broker marks untouched
    assert adj[1].equity == D(64000) - D("6.50")


def test_benchmark_after_cost_returns_and_average_leverage():
    d0, d1 = date(2026, 11, 19), date(2026, 11, 20)
    marks = [mark(d0, 64000, -36000, 100000), mark(d1, 64640, -36000, 100640)]
    s = margin_interest(marks, [])
    ps = StubPriceService({sym: [_bar(sym, d0, 100), _bar(sym, d1, 101)] for sym in ("QQQ", "SPY")})
    bm = run(BenchmarkTracker(ps, launch_baseline_date=d0).compute(as_of_date=d1, marks=marks, flows=[], interest=s))
    assert bm.portfolio_session_pct == D("1.00")
    # 6.50 of estimated interest accrued overnight: (64640 - 6.50) / 64000 - 1
    assert bm.portfolio_session_after_cost_pct == D("0.99")
    assert bm.portfolio_since_launch_after_cost_pct == D("0.99")
    assert bm.avg_leverage_since_launch == average_leverage(marks) == D("1.56")


def test_reports_show_borrowing_leverage_and_after_cost_lines(tmp_path):
    import dataclasses
    from types import SimpleNamespace

    from stock_agent.benchmark import LAUNCH_BASELINE_DATE
    from stock_agent.config import Config
    from stock_agent.reporting import ReportBuilder

    b, led = margin_book(tmp_path)
    snap = run(led.refresh("t"))
    for d in (date(2026, 11, 19), date(2026, 11, 20)):
        led.record_equity_mark(d, "close", snap)
    cfg = Config()
    cfg.DATA_DIR = tmp_path
    bars = {s: [_bar(s, LAUNCH_BASELINE_DATE, 500.0), _bar(s, date(2026, 11, 19), 600.0),
                _bar(s, date(2026, 11, 20), 600.0), _bar(s, date(2026, 11, 23), 606.0)] for s in ("QQQ", "SPY")}
    clock = [datetime(2026, 11, 23, 21, 10, tzinfo=timezone.utc)]
    rb = ReportBuilder(cfg, ledger=led, price_service=StubPriceService(bars=bars),
                       health_registry=SimpleNamespace(summary_rows=lambda: []), clock=lambda: clock[0])
    live = dataclasses.replace(snap, as_of=clock[0], quality="live")
    daily = str(run(rb.build_daily_summary(snapshot=live)))
    assert "Borrowing & Leverage" in daily and "Borrowed $36,000" in daily and "Leverage 1.56x" in daily
    assert "Margin excess: 53.1%" in daily and "After est. borrowing cost" in daily
    clock[0] = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
    brief = str(run(rb.build_morning_brief(snapshot=dataclasses.replace(live, as_of=clock[0]))))
    assert "Borrowing & Leverage" in brief and "Cash (Debit Balance)" in brief
    clock[0] = datetime(2026, 11, 27, 21, 30, tzinfo=timezone.utc)
    embeds, sc = run(rb.build_weekly_scorecard(snapshot=dataclasses.replace(live, as_of=clock[0])))
    assert "Borrowing & Leverage" in str(embeds)
    assert sc["margin"]["borrowed"] == 36000.0 and sc["margin"]["leverage_now"] == 1.562 and sc["margin"]["interest_basis"] == "estimated"


def test_list_intents_filters_by_origin(tmp_path):
    from stock_agent.ledger import NewIntent
    led = make_ledger(tmp_path, broker=StubBroker())
    led.enqueue_intents([NewIntent("AAA", "sell", "k1", origin="margin_buffer"),
                         NewIntent("BBB", "sell", "k2", origin="scheduled")])
    assert [i.symbol for i in led.list_intents(origin="margin_buffer")] == ["AAA"]
    assert len(led.list_intents()) == 2


def test_a14c_unexplained_close_under_block_self_resolves_next_reconcile(tmp_path):
    from stock_agent.ledger import PositionMeta
    alerts = []
    b = StubBroker()
    b.pos("AAA", 10, 100.0)
    led = make_ledger(tmp_path, broker=b, alerts=alerts)
    run(led.refresh("t1"))
    led.store.set_trading_block("state quarantined (test)")
    del b.positions["AAA"]                       # gone with no broker activity
    run(led.refresh("t2", rebuild=True))
    assert not led.last_reconcile()["clean"] and led.trading_block()
    body = [a for a in alerts if a[3] == "state:still_blocked"][-1][2]
    assert "Cause: closed AAA" in body and "next reconcile is clean" in body
    run(led.refresh("t3", rebuild=True))
    assert led.last_reconcile()["clean"] and led.trading_block() is None
    assert PositionMeta  # imported for readability of the scenario


def test_a14c_unusable_broker_read_alerts_exact_cause(tmp_path):
    import pytest

    from stock_agent.reconcile import ReconcileError
    alerts = []
    b = StubBroker()
    b.account_extra = {"cash": "not-a-number"}
    led = make_ledger(tmp_path, broker=b, alerts=alerts)
    with pytest.raises(ReconcileError):
        run(led.refresh("t1"))
    [a] = [a for a in alerts if a[3] == "state:read_unusable"]
    assert "account cash missing" in a[2] and "Clears automatically" in a[2]
    b.account_extra = {}
    run(led.refresh("t2"))
    assert led.last_reconcile()["clean"]
