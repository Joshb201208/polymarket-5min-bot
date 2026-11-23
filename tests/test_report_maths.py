"""Regression tests for report maths (benchmark windows, flows, realised lots, post-stop horizon).

Ported from the strategy review scratch suite."""
from asyncio import run
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json

from tests.conftest import world
from tests.ledger_helpers import make_ledger
from tests.test_reporting import StubPriceService, _bar

from stock_agent.benchmark import BenchmarkTracker, LAUNCH_BASELINE_DATE
from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, CashFlow, EquityMark
from stock_agent.reporting import ReportBuilder


NOW = datetime(2026, 11, 23, 21, 25, tzinfo=timezone.utc)
DAY = NOW.date()
PREV = date(2026, 11, 20)


def mark(day, equity, missing=False):
    val = None if missing else D(0)
    return EquityMark(day, "close", D(equity), val, val, val, val, NOW)


def snapshot(equity="110", option_value="500", quality="live"):
    return BookSnapshot(NOW, "fixture", D(equity), D(equity), D(0), D(0), D(option_value),
                        None, None, (), (), (), (), quality)


def tracker(bars):
    return BenchmarkTracker(StubPriceService({
        sym: [_bar(sym, day, px) for day, px in bars] for sym in ("QQQ", "SPY")
    }))


def test_session_excludes_same_day_external_deposit():
    bm = run(tracker([(PREV, 100), (DAY, 100)]).compute(
        as_of_date=DAY, marks=[mark(PREV, 100), mark(DAY, 150)],
        flows=[CashFlow("topup", DAY, D(50), "CSD")]))
    assert bm.portfolio_session_pct == D(0)


def test_late_ledger_history_is_not_launch_active_return():
    bm = run(tracker([(LAUNCH_BASELINE_DATE, 50), (PREV, 100), (DAY, 101)]).compute(
        as_of_date=DAY, marks=[mark(PREV, 100), mark(DAY, 101)], flows=[]))
    assert bm.active_vs_qqq_launch_pct is None


def test_stale_etf_bar_is_not_todays_return():
    bm = run(tracker([(LAUNCH_BASELINE_DATE, 50), (PREV, 100)]).compute(
        as_of_date=DAY, marks=[mark(PREV, 100), mark(DAY, 101)], flows=[]))
    assert bm.qqq.stale
    assert bm.qqq.session_return_pct is None


def test_paired_stats_do_not_pair_returns_with_different_interval_start():
    days = [LAUNCH_BASELINE_DATE + timedelta(days=i) for i in range(1, 8)]
    bm = run(tracker([(day, 100 + i) for i, day in enumerate(days)]).compute(
        as_of_date=days[-1], marks=[mark(day, 100 + i) for i, day in enumerate(days) if i != 3],
        flows=[]))
    assert bm.paired_sessions == 4
    assert bm.beta_to_qqq is None


def test_indicative_first_day_is_not_since_launch_return(world):
    report = ReportBuilder(Config(), clock=lambda: NOW)
    embeds = run(report.build_daily_summary(snapshot=snapshot(equity="75000", quality="indicative")))
    assert "-25.00%" not in str(embeds)


def test_repeated_symbol_closes_count_actual_pnl_only_once(world, tmp_path):
    ledger = make_ledger(tmp_path, world)
    for i, (d0, d1, exit_price) in enumerate((("03", "04", 110), ("19", "20", 95))):
        opened, closed = f"2026-11-{d0}T16:00:00+00:00", f"2026-11-{d1}T20:00:00+00:00"
        ledger.store.conn.execute("INSERT INTO positions_meta(position_id,symbol,opened_at,closed_at) VALUES(?,?,?,?)",
                             (str(i), "XYZ", opened, closed))
        for side, ts, price in (("buy", opened, 100), ("sell", closed, exit_price)):
            aid = f"{i}-{side}"
            raw = {"id": aid, "transaction_time": ts, "activity_type": "FILL", "qty": "1",
                   "price": str(price), "side": side, "symbol": "XYZ"}
            ledger.store.conn.execute("INSERT INTO broker_activities(activity_id,activity_type,ts,raw_json) VALUES(?,?,?,?)",
                                 (aid, "FILL", ts, json.dumps(raw)))
    report = ReportBuilder(Config(), ledger=ledger, clock=lambda: NOW)
    sleeves, _, _, _ = report._compile_sleeves_and_orders(snapshot())
    assert sleeves["CORE_EQUITY"]["realized_pnl"] == 5
    assert sleeves["CORE_EQUITY"]["wins"] == 1


def test_recent_stop_not_mature_twenty_sessions_after_stop(world, tmp_path):
    ledger = make_ledger(tmp_path, world)
    ledger.journal.append("shadow.stop", anchor="one", entry_session="2026-10-01",
        through_session=DAY.isoformat(), results=[
            {"variant": "atr_placed", "stopped": True, "exit_session": PREV.isoformat(), "pnl": -50},
            {"variant": "no_stop", "stopped": False, "pnl": -500},
        ])
    report = ReportBuilder(Config(), ledger=ledger, clock=lambda: NOW)
    bm = run(tracker([]).compute(as_of_date=DAY, marks=[], flows=[]))
    assert report._compile_what_we_learned(bm)["stopped_out_20d_check"]["mature_count"] == 0


def test_benchmark_failure_does_not_log_raw_query_credential(caplog):
    class OfflineBrokenPrice:
        async def get_daily_bars(self, *_args, **_kwargs):
            raise RuntimeError("https://fixture.invalid/?apikey=synthetic-benchmark-secret")

    run(BenchmarkTracker(OfflineBrokenPrice()).compute(as_of_date=DAY, marks=[], flows=[]))
    assert "synthetic-benchmark-secret" not in caplog.text


def test_day_pnl_removes_same_day_deposit():
    from stock_agent.performance import day_pnl

    pnl, pct = day_pnl([mark(PREV, 100)], D(160), DAY, [CashFlow("topup", DAY, D(50), "CSD")])
    assert pnl == D(10) and pct == D("10.0000")


def test_shadow_stop_rows_count_sessions_after_exit_and_outlive_the_close(world, tmp_path):
    from stock_agent.performance import shadow_stops

    ledger = make_ledger(tmp_path, world)
    ledger.store.conn.execute("INSERT INTO positions_meta(position_id,symbol,opened_at,closed_at) VALUES(?,?,?,?)",
                              ("p1", "XYZ", "2026-11-02T15:00:00+00:00", "2026-11-04T15:00:00+00:00"))
    ledger.journal.append("entry.filled", position_id="p1", symbol="XYZ", session_date="2026-11-02",
                          fill_price="100", qty="1", stop_fraction="0.07", stop_price="93", fill_id="f1")
    days = [date(2026, 11, 2) + timedelta(days=i) for i in range(22) if (date(2026, 11, 2) + timedelta(days=i)).weekday() < 5]
    rows = [{"session_date": d, "open": 100, "high": 101, "low": 90 if d == date(2026, 11, 4) else 99, "close": 100}
            for d in days]

    async def bars(_sym, _start, end):
        return [r for r in rows if r["session_date"] <= end]

    warnings: list[str] = []
    assert run(shadow_stops(ledger, days[-1], bars, warnings)) == 1
    [row] = list(ledger.journal.read(kinds={"shadow.stop"}))
    atr = next(r for r in row["results"] if r["variant"] == "atr_placed")
    assert str(row["through_session"])[:10] == days[-1].isoformat()   # replayed past the close
    assert atr["stopped"] and atr["sessions_after_exit"] == len([d for d in days if d > date(2026, 11, 4)])


def test_indicative_terminal_not_return_even_with_trusted_opening_mark(world, tmp_path):
    ledger = make_ledger(tmp_path, world)
    ledger.record_equity_mark(PREV, "close", snapshot(equity="100"))
    report = ReportBuilder(Config(), ledger=ledger, clock=lambda: NOW)
    embeds = run(report.build_daily_summary(snapshot=snapshot(equity="70", quality="indicative")))
    assert "-30.00%" not in str(embeds)
    brief = run(report.build_morning_brief(snapshot=snapshot(equity="70", quality="indicative"), as_of=NOW))
    assert "-30.00%" not in str(brief)


def test_missing_terminal_mark_does_not_publish_stale_window_return():
    bm = run(tracker([(LAUNCH_BASELINE_DATE, 100), (PREV, 101), (DAY, 150)]).compute(
        as_of_date=DAY, marks=[mark(LAUNCH_BASELINE_DATE, 100), mark(PREV, 101)], flows=[]))
    assert bm.portfolio_since_launch_pct is None and bm.active_vs_qqq_launch_pct is None


def test_quote_from_an_earlier_session_is_not_todays_price():
    from stock_agent.price_service import PriceBatchResult, Quote

    class Px(StubPriceService):
        async def get_quotes(self, symbols, purpose=""):
            old = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
            return PriceBatchResult(quotes={s: Quote(s, D(150), None, None, D(100), None, old, old, "fixture", False)
                                            for s in symbols}, missing={})

    bm = run(BenchmarkTracker(Px({s: [_bar(s, PREV, 100)] for s in ("QQQ", "SPY")})).compute(
        as_of_date=DAY, marks=[mark(PREV, 100)], flows=[], live_equity=D(101)))
    assert bm.qqq.stale and bm.qqq.session_return_pct is None and bm.active_vs_qqq_session_pct is None
