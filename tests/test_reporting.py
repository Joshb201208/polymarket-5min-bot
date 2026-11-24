"""Tests for BenchmarkTracker, ReportBuilder, and scheduler brief/summary wiring."""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stock_agent.benchmark import LAUNCH_BASELINE_DATE, BenchmarkTracker, flow_adjusted_return_pct
from stock_agent.config import Config
from stock_agent.ledger import (
    BookSnapshot,
    CashFlow,
    EquityMark,
    Holding,
    IntentStatus,
    JobStatus,
    NewIntent,
    OpenOrder,
    OptionLeg,
    PositionMeta,
)
from stock_agent.options_book import Leg as OptBookLeg, OptionsStore, Structure
from stock_agent.price_service import DailyBar, PriceBatchResult, Quote
from stock_agent.reporting import ReportBuilder, describe_drawdown_tier, format_input_health_table
from stock_agent.scheduler import StockAgentScheduler
from tests.fakes.world import FakeWorld
from tests.ledger_helpers import make_ledger


@pytest.fixture
def world(monkeypatch, tmp_path: Path) -> FakeWorld:
    w = FakeWorld()
    w.install(monkeypatch, data_dir=tmp_path)
    return w


class StubPriceService:
    def __init__(self, bars: dict[str, list[DailyBar]], quotes: dict[str, Quote] | None = None, missing: dict[str, str] | None = None):
        self.bars = bars
        self.quotes = quotes or {}
        self.missing = missing or {}

    async def get_daily_bars(self, symbols, *, sessions=60, start_date=None, end_date=None):
        out: dict[str, list[DailyBar]] = {}
        for s in symbols:
            seq = [
                b for b in self.bars.get(s, [])
                if (start_date is None or b.session_date >= start_date)
                and (end_date is None or b.session_date <= end_date)
            ]
            out[s] = seq
        return out

    async def get_quotes(self, symbols, *, purpose="brief", max_age_s=None):
        q = {s: self.quotes[s] for s in symbols if s in self.quotes}
        m = {s: self.missing.get(s, "unavailable") for s in symbols if s not in q}
        return PriceBatchResult(quotes=q, missing=m)


def _bar(sym: str, d: date, close: float) -> DailyBar:
    c = Decimal(str(close))
    return DailyBar(symbol=sym, session_date=d, open=c, high=c, low=c, close=c, volume=1_000_000)


def test_benchmark_flow_adjusted_return_and_missing_staleness() -> None:
    async def _run() -> None:
        # Deposit of $50k between Day 1 ($100k -> $105k = +5%) and Day 2 (($158.1k - $50k)/$105k = +2.95%, chained = +8.10%)
        d0 = LAUNCH_BASELINE_DATE
        d1 = date(2026, 3, 30)
        d2 = date(2026, 3, 31)
        marks = [
            EquityMark(session_date=d0, quality="close", equity=Decimal("100000"), cash=Decimal("100000"), long_mv=Decimal("0"), short_mv=Decimal("0"), options_mv=Decimal("0"), taken_at=datetime(2026, 3, 27, 20, tzinfo=timezone.utc)),
            EquityMark(session_date=d1, quality="close", equity=Decimal("105000"), cash=Decimal("55000"), long_mv=Decimal("50000"), short_mv=Decimal("0"), options_mv=Decimal("0"), taken_at=datetime(2026, 3, 30, 20, tzinfo=timezone.utc)),
            EquityMark(session_date=d2, quality="close", equity=Decimal("158100"), cash=Decimal("108100"), long_mv=Decimal("50000"), short_mv=Decimal("0"), options_mv=Decimal("0"), taken_at=datetime(2026, 3, 31, 20, tzinfo=timezone.utc)),
        ]
        flows = [
            CashFlow(activity_id="dep-init", date=d0, amount=Decimal("100000"), kind="CSD"),
            CashFlow(activity_id="dep-topup", date=d2, amount=Decimal("50000"), kind="CSD"),
        ]
        twr_ret = flow_adjusted_return_pct(marks, flows, start_date=d0, as_of_date=d2)
        assert twr_ret == Decimal("8.10")

        ps = StubPriceService(
            bars={
                "QQQ": [_bar("QQQ", d0, 500.0), _bar("QQQ", d1, 510.0), _bar("QQQ", d2, 525.0)],
                "SPY": [],  # Missing SPY bars + missing quote -> stale=True and None return (never 0.0%!)
            },
            missing={"SPY": "provider_timeout"},
        )
        tracker = BenchmarkTracker(ps)
        bm = await tracker.compute(as_of_date=d2, marks=marks, flows=flows)
        assert bm.portfolio_since_launch_pct == Decimal("8.10")
        assert bm.qqq.since_launch_return_pct == Decimal("5.00")
        assert bm.active_vs_qqq_launch_pct == Decimal("3.10")
        assert bm.spy.since_launch_return_pct is None
        assert bm.spy.stale is True
        assert bm.any_stale is True
        # Fewer than 5 paired sessions -> vol/beta/IR are None, never 0
        assert bm.realized_vol_ann_pct is None
        assert bm.beta_to_qqq is None
        assert bm.information_ratio_vs_qqq is None

    asyncio.run(_run())


def test_benchmark_paired_stats_mature_after_20_sessions() -> None:
    async def _run() -> None:
        base = date(2026, 4, 1)
        marks: list[EquityMark] = []
        qqq_bars: list[DailyBar] = []
        spy_bars: list[DailyBar] = []
        eq = Decimal("100000")
        q = 500.0
        for i in range(25):
            d = base + timedelta(days=i)
            step = 0.004 if i % 2 == 0 else -0.002
            eq = (eq * Decimal(str(1.0 + step * 1.2))).quantize(Decimal("0.01"))
            q = round(q * (1.0 + step), 4)
            marks.append(
                EquityMark(
                    session_date=d,
                    quality="close",
                    equity=eq,
                    cash=Decimal("20000"),
                    long_mv=eq - Decimal("20000"),
                    short_mv=Decimal("0"),
                    options_mv=Decimal("0"),
                    taken_at=datetime(2026, 4, 1, 20, tzinfo=timezone.utc),
                )
            )
            qqq_bars.append(_bar("QQQ", d, q))
            spy_bars.append(_bar("SPY", d, q))

        bm = await BenchmarkTracker(
            StubPriceService({"QQQ": qqq_bars, "SPY": spy_bars}),
            launch_baseline_date=base,
        ).compute(as_of_date=base + timedelta(days=24), marks=marks, flows=[])
        assert bm.paired_sessions >= 20
        assert bm.realized_vol_ann_pct is not None and bm.realized_vol_ann_pct > Decimal("0")
        assert bm.beta_to_qqq is not None
        assert bm.information_ratio_vs_qqq is not None

    asyncio.run(_run())


def test_morning_brief_ignores_last_equity_flags_premarket_options_and_separates_shorts(
    world: FakeWorld, tmp_path: Path
) -> None:
    async def _run() -> None:
        ledger = make_ledger(tmp_path, world)
        cfg = Config()
        cfg.DATA_DIR = tmp_path

        # Record prior trusted close mark of $100,000 on Nov 20
        prior_snap = BookSnapshot(
            as_of=datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc),
            run_id="r0",
            equity=Decimal("100000"),
            cash=Decimal("50000"),
            long_mv=Decimal("50000"),
            short_mv=Decimal("0"),
            options_mv=Decimal("0"),
            last_equity=Decimal("93000"),  # Untrusted broker field (omits ~7% holding!)
            buying_power=Decimal("50000"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        ledger.record_equity_mark(date(2026, 11, 20), "close", prior_snap)

        # Record actual yesterday job_runs
        ledger.start_job("prescan", "2026-11-22")
        ledger.finish_job("prescan", "2026-11-22", JobStatus.DONE)

        # Live pre-market snapshot on Nov 23: equity $102,000, poisoned last_equity=$77,777,
        # one protected long (NVDA), one unprotected long >15% cap (JPM), one short (PDD),
        # and a pre-market $0 option leg!
        live_snap = BookSnapshot(
            as_of=datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc),
            run_id="r1",
            equity=Decimal("102000"),
            cash=Decimal("64000"),
            long_mv=Decimal("40000"),
            short_mv=Decimal("-2000"),
            options_mv=Decimal("0"),
            last_equity=Decimal("77777"),  # Poisoned value — MUST NOT BE USED!
            buying_power=Decimal("64000"),
            holdings=(
                Holding(
                    symbol="JPM",
                    qty=Decimal("100"),
                    qty_available=Decimal("100"),
                    avg_cost=Decimal("200"),
                    price=Decimal("220"),
                    price_ok=True,
                    market_value=Decimal("22000"),  # 21.6% > 15% cap, no stop order!
                    lastday_price=Decimal("215"),
                    meta=PositionMeta(position_id="p-jpm", symbol="JPM", sector="Financials"),
                ),
                Holding(
                    symbol="NVDA",
                    qty=Decimal("100"),
                    qty_available=Decimal("0"),
                    avg_cost=Decimal("175"),
                    price=Decimal("180"),
                    price_ok=True,
                    market_value=Decimal("18000"),
                    lastday_price=Decimal("178"),
                    meta=PositionMeta(position_id="p-nvda", symbol="NVDA", sector="Information Technology", stop_price=Decimal("162")),
                ),
                Holding(
                    symbol="PDD",
                    qty=Decimal("-20"),
                    qty_available=Decimal("-20"),
                    avg_cost=Decimal("100"),
                    price=Decimal("100"),
                    price_ok=True,
                    market_value=Decimal("-2000"),
                    lastday_price=Decimal("100"),
                ),
            ),
            legs=(
                OptionLeg(
                    occ_symbol="QQQ261218P00600000",
                    underlying="QQQ",
                    right="put",
                    strike=Decimal("600"),
                    expiry=date(2026, 12, 18),
                    qty=Decimal("1"),
                    multiplier=100,
                    avg_cost=Decimal("9.50"),
                    price=Decimal("0"),
                    price_ok=False,
                    market_value=Decimal("0"),
                ),
            ),
            open_orders=(
                OpenOrder(
                    order_id="ord-stop-nvda",
                    client_order_id="c1",
                    symbol="NVDA",
                    side="sell",
                    type="stop",
                    qty=Decimal("100"),
                    filled_qty=Decimal("0"),
                    remaining_qty=Decimal("100"),
                    limit_price=None,
                    stop_price=Decimal("162"),
                    time_in_force="gtc",
                    asset_class="us_equity",
                    submitted_at=datetime(2026, 11, 20, 15, tzinfo=timezone.utc),
                ),
            ),
            diffs=(),
            quality="indicative",
        )

        health_stub = SimpleNamespace(
            summary_rows=lambda: [
                {"provider": "alpaca", "capability": "broker", "state": "healthy", "required_for_trading": True, "last_error_summary": ""},
                {"provider": "finnhub", "capability": "news", "state": "degraded", "required_for_trading": True, "last_error_summary": "HTTP 429"},
            ]
        )
        opt_store = OptionsStore(tmp_path / "options_book.json")
        opt_store.save(
            [
                Structure(
                    id="s1",
                    underlying="QQQ",
                    strategy="bear_put_spread",
                    direction="bearish",
                    qty=1,
                    expiry="2026-12-18",
                    legs=[OptBookLeg("QQQ261218P00600000", "buy", "put", 600.0, "2026-12-18")],
                    state="open",
                    source="news",
                    max_loss=950.0,
                )
            ],
            {},
            {},
        )

        ps = StubPriceService(
            bars={
                "QQQ": [_bar("QQQ", LAUNCH_BASELINE_DATE, 500.0), _bar("QQQ", date(2026, 11, 20), 600.0)],
                "SPY": [_bar("SPY", LAUNCH_BASELINE_DATE, 550.0), _bar("SPY", date(2026, 11, 20), 605.0)],
            },
            quotes={
                "QQQ": Quote("QQQ", Decimal("606"), Decimal("605.9"), Decimal("606.1"), Decimal("600"), Decimal("1.0"), datetime(2026, 11, 23, 12, tzinfo=timezone.utc), datetime(2026, 11, 23, 12, tzinfo=timezone.utc), "alpaca", False),
                "SPY": Quote("SPY", Decimal("611.05"), Decimal("611"), Decimal("611.1"), Decimal("605"), Decimal("1.0"), datetime(2026, 11, 23, 12, tzinfo=timezone.utc), datetime(2026, 11, 23, 12, tzinfo=timezone.utc), "alpaca", False),
            },
        )

        rb = ReportBuilder(
            cfg,
            ledger=ledger,
            price_service=ps,
            health_registry=health_stub,
            options_store=opt_store,
            clock=lambda: datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc),
        )
        embeds = await rb.build_morning_brief(snapshot=live_snap)
        flat = str(embeds)

        # NEVER $102,000 - $77,777 = +$24,223. The snapshot is indicative (a $0 pre-market option
        # mark), so no overnight % is printed at all; a live snapshot gives +$2,000 vs the close mark.
        assert "24,223" not in flat and "+$2,000.00 (+2.00%)" not in flat
        assert "Not shown: snapshot is indicative" in flat
        live_flat = str(await rb.build_morning_brief(snapshot=dataclasses.replace(live_snap, quality="live")))
        assert "+$2,000.00 (+2.00%)" in live_flat and "24,223" not in live_flat
        assert "24,223" not in flat
        assert "Pre-market pricing note" in flat
        assert "2 / 20 long stocks" in flat  # Short PDD not counted as long stock slot!
        assert "MISSING BROKER STOP" in flat and ">15% cap" in flat
        assert "Stop @ $162.00" in flat
        assert "Unexpected Short Positions" in flat and "PDD" in flat
        assert "Options Sleeve (Separate from Stocks)" in flat and "$950" in flat
        assert "finnhub news" in flat and "degraded" in flat
        assert "Completed steps (2026-11-22): prescan" in flat
        ledger.store.close()

    asyncio.run(_run())


def test_weekly_scorecard_paper_test_keys_no_double_count_and_na_when_missing(
    world: FakeWorld, tmp_path: Path
) -> None:
    async def _run() -> None:
        ledger = make_ledger(tmp_path, world)
        cfg = Config()
        cfg.DATA_DIR = tmp_path

        # Record 2 close marks
        s0 = BookSnapshot(
            as_of=datetime(2026, 11, 19, 21, 5, tzinfo=timezone.utc),
            run_id="r0",
            equity=Decimal("100000"),
            cash=Decimal("60000"),
            long_mv=Decimal("40000"),
            short_mv=Decimal("0"),
            options_mv=Decimal("0"),
            last_equity=None,
            buying_power=Decimal("60000"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        s1 = BookSnapshot(
            as_of=datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc),
            run_id="r1",
            equity=Decimal("101500"),
            cash=Decimal("58000"),
            long_mv=Decimal("42500"),
            short_mv=Decimal("0"),
            options_mv=Decimal("1000"),
            last_equity=None,
            buying_power=Decimal("58000"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        ledger.record_equity_mark(date(2026, 11, 19), "close", s0)
        ledger.record_equity_mark(date(2026, 11, 20), "close", s1)

        # Add one news-origin closed option structure -> must appear in OPTIONS and options_by_origin['NEWS'], NOT NEWS_EQUITY
        opt_store = OptionsStore(tmp_path / "options_book_weekly.json")
        opt_store.save(
            [
                Structure(
                    id="opt-news-1",
                    underlying="NVDA",
                    strategy="bull_call_spread",
                    direction="bullish",
                    qty=1,
                    expiry="2026-12-18",
                    legs=[OptBookLeg("NVDA261218C00180000", "buy", "call", 180.0, "2026-12-18")],
                    state="closed",
                    source="news",
                    conviction=9,
                    max_loss=600.0,
                    realized_pnl=420.0,
                    closed_at="2026-11-20T19:00:00+00:00",
                    exit_reason="profit_target",
                )
            ],
            {},
            {},
        )

        rb = ReportBuilder(
            cfg,
            ledger=ledger,
            options_store=opt_store,
            clock=lambda: datetime(2026, 11, 20, 21, 10, tzinfo=timezone.utc),
        )

        # 1) Before any shadow.stop journal rows exist -> all shadow_stops must be None / N/A, NEVER 0!
        embeds_empty, sc_empty = await rb.build_weekly_scorecard(snapshot=s1)
        for variant_key in ("stop_7pct", "stop_10pct", "stop_12pct", "no_stop", "live_atr_stop"):
            entry = sc_empty["what_we_learned"]["shadow_stops"][variant_key]
            assert entry["total_pnl"] is None
            assert entry["display"].startswith("N/A")

        # Verify news-origin option is counted in OPTIONS + options_by_origin['NEWS'], NOT double-counted in NEWS_EQUITY
        assert sc_empty["sleeves"]["OPTIONS"]["realized_pnl"] == 420.0
        assert sc_empty["options_by_origin"]["NEWS"]["realized_pnl"] == 420.0
        assert sc_empty["sleeves"]["NEWS_EQUITY"]["realized_pnl"] == 0.0
        assert sc_empty["sleeves"]["NEWS_EQUITY"]["fills"] == 0
        assert sc_empty["by_conviction"]["9"]["net_pnl"] == 420.0
        assert sc_empty["by_conviction"]["7"]["net_pnl"] is None

        # 2) Append a shadow.stop journal row and verify What We Learned populates accurately
        ledger.journal.append(
            "shadow.stop",
            anchor="fill-1",
            position_id="p1",
            symbol="AMD",
            entry_session="2026-10-25",
            through_session="2026-11-20",
            closed=True,
            results=[
                {"variant": "live_atr_stop", "stopped": True, "pnl": -350.0, "exit_session": "2026-10-23",
                 "sessions_after_exit": 20},
                {"variant": "fixed_7pct", "stopped": True, "pnl": -280.0},
                {"variant": "fixed_10pct", "stopped": True, "pnl": -400.0},
                {"variant": "fixed_12pct", "stopped": True, "pnl": -480.0},
                {"variant": "no_stop", "stopped": False, "pnl": -920.0},
            ],
        )
        embeds_pop, sc_pop = await rb.build_weekly_scorecard(snapshot=s1)
        ss = sc_pop["what_we_learned"]["shadow_stops"]
        assert ss["stop_7pct"]["total_pnl"] == -280.0
        assert ss["no_stop"]["total_pnl"] == -920.0
        assert sc_pop["what_we_learned"]["stopped_out_20d_check"]["mature_count"] == 1
        assert sc_pop["what_we_learned"]["stopped_out_20d_check"]["net_saved_vs_hold"] == 570.0
        assert "Weekly Paper-Test Scorecard" in str(embeds_pop)
        ledger.store.close()

    asyncio.run(_run())


def test_daily_summary_separates_options_pnl_and_ignores_last_equity(
    world: FakeWorld, tmp_path: Path
) -> None:
    assert describe_drawdown_tier(Decimal("-4.0"))[0] == "normal"
    assert describe_drawdown_tier(Decimal("-11.5"))[0] == "alert_10pct"
    assert describe_drawdown_tier(Decimal("-16.0"))[0] == "throttle_15pct"
    assert describe_drawdown_tier(Decimal("-21.0"))[0] == "halt_20pct"

    txt, rows = format_input_health_table(None)
    assert rows == [] and "not connected" in txt

    async def _run() -> None:
        ledger = make_ledger(tmp_path, world)
        cfg = Config()
        cfg.DATA_DIR = tmp_path

        s_prev = BookSnapshot(
            as_of=datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc),
            run_id="r0",
            equity=Decimal("100000"),
            cash=Decimal("60000"),
            long_mv=Decimal("39000"),
            short_mv=Decimal("0"),
            options_mv=Decimal("1000"),
            last_equity=Decimal("88888"),
            buying_power=Decimal("60000"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        s_close = BookSnapshot(
            as_of=datetime(2026, 11, 23, 21, 5, tzinfo=timezone.utc),
            run_id="r1",
            equity=Decimal("103000"),
            cash=Decimal("61000"),
            long_mv=Decimal("40500"),
            short_mv=Decimal("0"),
            options_mv=Decimal("1500"),
            last_equity=Decimal("70000"),  # Untrusted broker last_equity — MUST BE IGNORED!
            buying_power=Decimal("61000"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        ledger.record_equity_mark(date(2026, 11, 20), "close", s_prev)
        ledger.record_equity_mark(date(2026, 11, 23), "close", s_close)

        # Record a declined intent today so "What We Passed On & Why" is populated
        ledger.enqueue_intents(
            [
                NewIntent(
                    symbol="AMD",
                    side="buy",
                    dedupe_key="scan:2026-11-23:AMD:buy",
                    sleeve="CORE_EQUITY",
                    origin="scheduled",
                    session_date=date(2026, 11, 23),
                    status=IntentStatus.SKIPPED,
                    last_error="sector headroom exceeded",
                )
            ]
        )

        opt_store = OptionsStore(tmp_path / "options_daily.json")
        opt_store.save(
            [
                Structure(
                    id="opt-1",
                    underlying="QQQ",
                    strategy="bull_call_spread",
                    direction="bullish",
                    qty=1,
                    expiry="2026-12-18",
                    legs=[OptBookLeg("QQQ261218C00600000", "buy", "call", 600.0, "2026-12-18")],
                    state="closed",
                    source="scan",
                    max_loss=500.0,
                    realized_pnl=250.0,
                    closed_at="2026-11-23T20:00:00+00:00",
                    exit_reason="profit_target",
                )
            ],
            {},
            {},
        )
        ps = StubPriceService(
            bars={
                "QQQ": [_bar("QQQ", LAUNCH_BASELINE_DATE, 500.0), _bar("QQQ", date(2026, 11, 20), 600.0), _bar("QQQ", date(2026, 11, 23), 606.0)],
                "SPY": [_bar("SPY", LAUNCH_BASELINE_DATE, 550.0), _bar("SPY", date(2026, 11, 20), 605.0), _bar("SPY", date(2026, 11, 23), 611.05)],
            }
        )
        rb = ReportBuilder(
            cfg,
            ledger=ledger,
            price_service=ps,
            options_store=opt_store,
            clock=lambda: datetime(2026, 11, 23, 21, 10, tzinfo=timezone.utc),
        )
        embeds = await rb.build_daily_summary(snapshot=s_close)
        flat = str(embeds)
        assert "Daily Close Summary" in flat
        assert "+$3,000.00 (+3.00%)" in flat  # $103k - $100k close mark, NOT $103k - $70k!
        assert "33,000" not in flat
        assert "Options Sleeve (Separate)" in flat
        assert "+$250" in flat  # Closed structure realized P&L
        assert "+21.20%" in flat  # QQQ cumulative since launch ($500 -> $606 = +21.20%)
        assert "sector headroom exceeded" in flat
        ledger.store.close()

    asyncio.run(_run())


def test_scheduler_morning_brief_and_friday_summary_emit_weekly_scorecard(
    world: FakeWorld, tmp_path: Path
) -> None:
    async def _run() -> None:
        # Friday Nov 27, 2026 at 16:10 ET -> _send_morning_brief + _send_daily_summary post daily + weekly scorecard
        world.clock.set_et(2026, 11, 27, 16, 10)
        cfg = Config()
        cfg.DATA_DIR = tmp_path
        cfg.DISCORD_BOT_TOKEN = "test-discord"
        cfg.TELEGRAM_BOT_TOKEN = ""
        cfg.TELEGRAM_CHAT_ID = ""
        sched = StockAgentScheduler(cfg)
        sched.store.open()
        try:
            await sched._send_morning_brief()
            await sched._send_daily_summary()
            texts = "\n".join(world.discord.texts())
            assert "Morning Brief" in texts
            assert "Daily Close Summary" in texts
            assert "Weekly Paper-Test Scorecard" in texts
        finally:
            await sched.http.aclose()
            sched.store.close()

    asyncio.run(_run())


def test_nullable_close_marks_rendered_cutover_persisted_drawdown_and_degrade(
    world: FakeWorld, tmp_path: Path, caplog: Any, monkeypatch: Any
) -> None:
    from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState

    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path / "null_marks"
        ledger = make_ledger(tmp_path / "null_marks", world)

        # 1) Persisted latched DrawdownState in DRAWDOWN_STATE_KEY ("drawdown") is honored
        # even when current equity marks alone would show -10% (recovery_closes=2 < 5).
        latched = DrawdownState(
            high_water=Decimal("100000"),
            drawdown=Decimal("0.10"),
            paused=True,
            recovery_closes=2,
            last_session="2026-11-23",
            last_equity=Decimal("90000"),
        )
        ledger.risk_state_set(DRAWDOWN_STATE_KEY, latched.to_record())

        # 2) Nullable EquityMark breakdown fields must never raise in daily or weekly builders
        nullable_mark_0 = EquityMark(
            session_date=date(2026, 11, 20),
            quality="close",
            equity=Decimal("100000"),
            cash=None,
            long_mv=None,
            short_mv=None,
            options_mv=None,
            taken_at=datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc),
        )
        nullable_mark_1 = EquityMark(
            session_date=date(2026, 11, 23),
            quality="close",
            equity=Decimal("90000"),
            cash=None,
            long_mv=None,
            short_mv=None,
            options_mv=None,
            taken_at=datetime(2026, 11, 23, 21, 5, tzinfo=timezone.utc),
        )
        ledger.equity_marks = lambda *_: [nullable_mark_0, nullable_mark_1]  # type: ignore[method-assign]

        ps = StubPriceService(
            bars={
                "QQQ": [
                    _bar("QQQ", date(2026, 11, 20), 600.0),
                    _bar("QQQ", date(2026, 11, 23), 606.0),
                ],
                "SPY": [
                    _bar("SPY", date(2026, 11, 20), 605.0),
                    _bar("SPY", date(2026, 11, 23), 611.05),
                ],
            }
        )
        rb = ReportBuilder(
            cfg,
            ledger=ledger,
            price_service=ps,
            clock=lambda: datetime(2026, 11, 23, 21, 10, tzinfo=timezone.utc),
        )

        daily_embeds = await rb.build_daily_summary(snapshot=None)
        daily_txt = str(daily_embeds)
        assert "Daily Close Summary" in daily_txt
        assert "20% Drawdown Stop" in daily_txt
        assert "2/5 recovery closes recorded" in daily_txt

        wk_embeds, sc = await rb.build_weekly_scorecard(
            snapshot=None, cutover_date=date(2026, 11, 20)
        )
        wk_txt = str(wk_embeds)
        assert sc["account"]["drawdown_tier"] == "halt_20pct"
        assert sc["account"]["drawdown_pct"] == -10.0
        assert sc["account"]["peak_cash_pct"] is None
        assert sc["account"]["peak_exposure_pct"] is None
        # 3) Rendered weekly scorecard includes the cut-over series
        assert "**Since Cut-Over (Nov 20):**" in wk_txt

        # 4) Unhandled failure inside builder degrades to "report unavailable: <reason>" with secret redaction
        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("https://fixture.invalid/data?apikey=synthetic-report-secret")

        monkeypatch.setattr(rb, "_daily_decisions_summary", _boom)
        degraded = await rb.build_daily_summary(snapshot=None)
        assert "report unavailable:" in degraded[0]["description"]
        assert "synthetic-report-secret" not in degraded[0]["description"]
        assert "synthetic-report-secret" not in caplog.text

        # 5) BenchmarkTracker failure never logs query credentials
        class _BrokenPrice:
            async def get_daily_bars(self, *_a: Any, **_kw: Any) -> list[Any]:
                raise RuntimeError("https://fixture.invalid/bars?apikey=synthetic-bm-secret")

        await BenchmarkTracker(_BrokenPrice()).compute(
            as_of_date=date(2026, 11, 23), marks=[], flows=[]
        )
        assert "synthetic-bm-secret" not in caplog.text

        # 6) Scheduler routes daily report to daily_pnl (not risk) and contains simultaneous report + alert failures
        cfg_sched = Config()
        cfg_sched.DATA_DIR = tmp_path / "sched_route"
        cfg_sched.DISCORD_BOT_TOKEN = "test-discord"
        cfg_sched.TELEGRAM_BOT_TOKEN = ""
        cfg_sched.DISCORD_CHANNELS = {"daily_pnl": "daily-pnl-chan", "risk": "risk-chan"}
        sched = StockAgentScheduler(cfg_sched)
        sched.store.open()
        try:
            await sched._send_daily_summary()
            routed = [m for m in world.discord.messages if "Daily" in str(m.get("embeds"))]
            assert routed and {m["channel"] for m in routed} == {"daily-pnl-chan"}
        finally:
            await sched.http.aclose()
            sched.store.close()

        def _fail_os(*_a: Any, **_kw: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(ReportBuilder, "build_daily_summary", _fail_os)
        broken_alerts = SimpleNamespace(alert_unhandled_exception=_fail_os, alert=_fail_os, flush=_fail_os)
        stub_sched = SimpleNamespace(
            config=Config(),
            ledger=SimpleNamespace(last_snapshot=lambda: None),
            alerts=broken_alerts,
            discord=None,
        )
        await StockAgentScheduler._send_daily_summary(stub_sched)  # type: ignore[arg-type]
        await StockAgentScheduler._send_morning_brief(stub_sched)  # type: ignore[arg-type]

        ledger.store.close()

    asyncio.run(_run())


def test_entry_funnel_and_locks_in_daily_and_weekly_reports(
    world: FakeWorld, tmp_path: Path
) -> None:
    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path / "funnel_test"
        ledger = make_ledger(tmp_path / "funnel_test", world)

        # 1) A13f: 15% drawdown throttle tier explicitly states no new margin borrowing
        tier, desc = describe_drawdown_tier(Decimal("-16.0"), ledger)
        assert tier == "throttle_15pct"
        assert "no new margin borrowing" in desc

        # 2) Zero-candidate locked session: zero buys never looks like silence, and shows exact lock + clear condition
        (cfg.DATA_DIR / "pause-trading").write_text("paused\n", encoding="utf-8")
        s_day = BookSnapshot(
            as_of=datetime(2026, 11, 23, 21, 5, tzinfo=timezone.utc),
            run_id="r-lock",
            equity=Decimal("100000"),
            cash=Decimal("40000"),
            long_mv=Decimal("60000"),
            short_mv=Decimal("0"),
            options_mv=Decimal("0"),
            last_equity=None,
            buying_power=Decimal("80000"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        ledger.record_equity_mark(date(2026, 11, 23), "close", s_day)
        rb = ReportBuilder(
            cfg,
            ledger=ledger,
            clock=lambda: datetime(2026, 11, 23, 21, 10, tzinfo=timezone.utc),
        )
        zero_daily = str(await rb.build_daily_summary(snapshot=s_day))
        assert "Entries locked: operator pause switch (pause-trading file) — clears when the data/pause-trading file is removed" in zero_daily
        assert "**Screened 0** → **Thesis Written 0** → **Passed Conviction 0** → **Passed Risk 0** → **Ordered 0** → **Filled 0**" in zero_daily
        assert "Zero buys attempted because new entries are currently locked" in zero_daily

        # 3) Populate research.entry_funnel + execution intents (partial fill counted once, risk/hold skips grouped)
        (cfg.DATA_DIR / "pause-trading").unlink()
        world.clock.set_et(2026, 11, 23, 10, 30)
        ledger.journal.append(
            "research.entry_funnel",
            job="scheduled_scan",
            session_date="2026-11-23",
            screened=12,
            thesis_written=5,
            passed_conviction=3,
            buy_candidates=3,
            option_ideas=1,
            rejected_by_reason={"non_entry_direction": 1, "conviction_below_floor": 1},
        )
        ledger.journal.append(
            "research.entry_funnel",
            job="live_news",
            session_date="2026-11-23",
            screened=0,
            thesis_written=2,
            passed_conviction=1,
            buy_candidates=1,
            option_ideas=0,
            rejected_by_reason={"quote_stale_or_unavailable": 1},
        )

        # Intent 1: partial fill whose remainder was cancelled at close -> counts once in Passed Risk, Ordered, Filled (with 1 partial note, NOT skipped!)
        i_fill_id = ledger.enqueue_intents(
            [
                NewIntent(
                    symbol="AAPL",
                    side="buy",
                    dedupe_key="scan:2026-11-23:AAPL:buy",
                    sleeve="CORE_EQUITY",
                    origin="scheduled",
                    session_date=date(2026, 11, 23),
                )
            ]
        )[0]
        ledger.claim_intent(i_fill_id)
        ledger.ack_intent(i_fill_id, IntentStatus.PARTIALLY_FILLED, broker_order_id="ord-aapl-1")
        ledger.journal.append(
            "execution.buy_funnel",
            intent_id=i_fill_id,
            symbol="AAPL",
            origin="scheduled",
            stage="filled",
            code="partial_fill",
            reason="6/10 filled",
        )
        ledger.ack_intent(
            i_fill_id,
            IntentStatus.CANCELLED,
            meta={"skip_code": "unfilled_cancelled", "skip_reason": "remainder cancelled at close"},
        )

        # Intent 2: passed risk + ordered, then unfilled_cancelled at close (zero fills)
        i_unfilled_id = ledger.enqueue_intents(
            [
                NewIntent(
                    symbol="MSFT",
                    side="buy",
                    dedupe_key="news:2026-11-23:MSFT:buy",
                    sleeve="NEWS_EQUITY",
                    origin="news",
                    session_date=date(2026, 11, 23),
                )
            ]
        )[0]
        ledger.claim_intent(i_unfilled_id)
        ledger.ack_intent(i_unfilled_id, IntentStatus.SUBMITTED, broker_order_id="ord-msft-1")
        ledger.ack_intent(
            i_unfilled_id,
            IntentStatus.CANCELLED,
            meta={"skip_code": "unfilled_cancelled", "skip_reason": "unfilled at close"},
        )

        # Intent 3: refused by risk margin_buffer (25% margin safety cushion floor)
        i_risk_id = ledger.enqueue_intents(
            [
                NewIntent(
                    symbol="NVDA",
                    side="buy",
                    dedupe_key="scan:2026-11-23:NVDA:buy",
                    sleeve="CORE_EQUITY",
                    origin="scheduled",
                    session_date=date(2026, 11, 23),
                )
            ]
        )[0]
        ledger.ack_intent(
            i_risk_id,
            IntentStatus.SKIPPED,
            error="risk: margin_buffer",
            meta={
                "skip_code": "risk_refused",
                "skip_reason": "risk: margin_buffer",
                "risk_reasons": ["margin_buffer"],
            },
        )

        # Intent 4: expired while held by cleanup_pending
        i_exp_id = ledger.enqueue_intents(
            [
                NewIntent(
                    symbol="AMZN",
                    side="buy",
                    dedupe_key="scan:2026-11-23:AMZN:buy",
                    sleeve="CORE_EQUITY",
                    origin="scheduled",
                    session_date=date(2026, 11, 23),
                )
            ]
        )[0]
        ledger.ack_intent(
            i_exp_id,
            IntentStatus.EXPIRED,
            error="intent expired",
            meta={"skip_code": "intent_expired", "held_by": "cleanup_pending"},
        )

        explicit_locks = [
            {
                "code": "reconcile_unclean",
                "lock": "broker reconciliation not clean",
                "clears_when": "next reconcile finishes with zero blocking diffs",
            }
        ]
        daily_embeds = await rb.build_daily_summary(snapshot=s_day, entry_locks=explicit_locks)
        daily_flat = str(daily_embeds)
        assert "**Screened 12** → **Thesis Written 7** → **Passed Conviction 4** → **Passed Risk 2** → **Ordered 2** → **Filled 1** (incl. 1 partial)" in daily_flat
        assert "25% margin safety cushion floor" in daily_flat
        assert "expired while held by first-run account clean-up pending" in daily_flat
        assert "limit order unfilled by close" in daily_flat
        assert "Entries locked: broker reconciliation not clean — clears when next reconcile finishes with zero blocking diffs" in daily_flat

        wk_embeds, sc = await rb.build_weekly_scorecard(snapshot=s_day, entry_locks=explicit_locks)
        assert sc["entry_funnel"]["screened"] == 12
        assert sc["entry_funnel"]["thesis_written"] == 7
        assert sc["entry_funnel"]["passed_conviction"] == 4
        assert sc["entry_funnel"]["passed_risk"] == 2
        assert sc["entry_funnel"]["ordered"] == 2
        assert sc["entry_funnel"]["filled"] == 1
        assert sc["entry_funnel"]["partial_fill_count"] == 1
        assert sc["entry_locks"]["locked"] is True
        assert "Entry Funnel (This Week)" in str(wk_embeds)

        ledger.store.close()

    asyncio.run(_run())


def test_rolling_and_all_time_drawdown_pause_cap_reporting(
    world: FakeWorld, tmp_path: Path
) -> None:
    from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState, update_drawdown

    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path / "pause_cap_reporting"
        cfg.DRAWDOWN_ROLLING_SESSIONS = 30
        ledger = make_ledger(tmp_path / "pause_cap_reporting", world)

        def _session_label(idx: int) -> str:
            return (date(2026, 6, 1) + timedelta(days=idx)).isoformat()

        # Build valid v3 states strictly via update_drawdown so every window/episode invariant holds.
        st0 = update_drawdown(DrawdownState(), _session_label(0), Decimal("100000"), config=cfg)
        for idx in range(1, 6):
            st0 = update_drawdown(st0, _session_label(idx), Decimal("85000"), config=cfg)
        for idx in range(6, 31):
            st0 = update_drawdown(st0, _session_label(idx), Decimal("90000"), config=cfg)
        # Day 30 rolled $100k out of the 30-session window: all-time HWM=$100k, rolling HWM=$90k.
        latched0 = update_drawdown(st0, _session_label(31), Decimal("70200"), config=cfg)
        assert latched0.paused is True

        # 1) Active pause: 4 closes at $70,200 + 2 recovery closes at $77,400 ->
        # governed=6 (15 remaining of 21), recovery_closes=2, rolling=-14.00%, all-time=-22.60%.
        active_pause = latched0
        for idx in range(32, 36):
            active_pause = update_drawdown(active_pause, _session_label(idx), Decimal("70200"), config=cfg)
        for idx in range(36, 38):
            active_pause = update_drawdown(active_pause, _session_label(idx), Decimal("77400"), config=cfg)
        assert active_pause.paused is True
        assert active_pause.pause_sessions_remaining(cfg) == 15
        assert active_pause.recovery_closes == 2

        snap_date = date.fromisoformat(active_pause.last_session or "2026-07-08")
        snap = BookSnapshot(
            as_of=datetime(snap_date.year, snap_date.month, snap_date.day, 21, 5, tzinfo=timezone.utc),
            run_id="r-pause-cap",
            equity=Decimal("77400"),
            cash=Decimal("27400"),
            long_mv=Decimal("50000"),
            short_mv=Decimal("0"),
            options_mv=Decimal("0"),
            last_equity=None,
            buying_power=Decimal("104800"),
            holdings=(),
            legs=(),
            open_orders=(),
            diffs=(),
        )
        ledger.record_equity_mark(snap_date, "close", snap)
        rb = ReportBuilder(
            cfg,
            ledger=ledger,
            clock=lambda: datetime(snap_date.year, snap_date.month, snap_date.day, 21, 10, tzinfo=timezone.utc),
        )

        ledger.risk_state_set(DRAWDOWN_STATE_KEY, active_pause.to_record())

        tier, label = describe_drawdown_tier(None, ledger, config=cfg)
        assert tier == "halt_20pct"
        assert "15 more trusted session closes are recorded" in label
        assert "rolling drawdown stays below 15% for 5 consecutive closes (2/5 recovery closes recorded)" in label

        morning_txt = str(await rb.build_morning_brief(snapshot=snap))
        daily_txt = str(await rb.build_daily_summary(snapshot=snap))
        wk_embeds, sc = await rb.build_weekly_scorecard(snapshot=snap)
        wk_txt = str(wk_embeds)

        for rendered in (morning_txt, daily_txt):
            assert "Rolling 30-session decision drawdown -14.00%" in rendered
            assert "All-time fall from peak -22.60%" in rendered
            assert "15 more trusted session closes are recorded" in rendered
        assert "Entries locked: 20% drawdown safety stop — clears when 15 more trusted session closes are recorded" in daily_txt
        assert sc["account"]["drawdown_tier"] == "halt_20pct"
        assert sc["account"]["decision_drawdown_pct"] == -14.0
        assert sc["account"]["all_time_drawdown_pct"] == -22.6
        assert sc["account"]["drawdown_pct"] == -22.6
        assert sc["account"]["drawdown_paused"] is True
        assert sc["account"]["pause_sessions_remaining"] == 15
        assert sc["entry_locks"]["locked"] is True
        assert "Rolling 30-Session Decision Drawdown: -14.00%" in wk_txt
        assert "All-Time Fall from Peak: -22.60%" in wk_txt

        # 2) Expired 21-session pause cap with rolling drawdown still > 20% (-22.00% rolling, -29.80% all-time):
        # MUST NOT report halt_20pct or lock entries; reports throttle_15pct + dual drawdowns.
        expired_cap = latched0
        for idx in range(32, 53):
            expired_cap = update_drawdown(expired_cap, _session_label(idx), Decimal("70200"), config=cfg)
        assert expired_cap.paused is False
        assert expired_cap.decision_drawdown == Decimal("0.22")

        ledger.risk_state_set(DRAWDOWN_STATE_KEY, expired_cap.to_record())

        tier_exp, label_exp = describe_drawdown_tier(None, ledger, config=cfg)
        assert tier_exp == "throttle_15pct"
        assert "21-session pause cap elapsed" in label_exp
        assert "no new margin borrowing" in label_exp

        daily_exp_txt = str(await rb.build_daily_summary(snapshot=snap))
        _, sc_exp = await rb.build_weekly_scorecard(snapshot=snap)
        assert "20% drawdown safety stop" not in daily_exp_txt
        assert "Rolling 30-session decision drawdown -22.00%" in daily_exp_txt
        assert "All-time fall from peak -29.80%" in daily_exp_txt
        assert sc_exp["account"]["drawdown_tier"] == "throttle_15pct"
        assert sc_exp["account"]["drawdown_paused"] is False
        assert sc_exp["account"]["pause_sessions_remaining"] == 0
        assert sc_exp["account"]["decision_drawdown_pct"] == -22.0
        assert sc_exp["account"]["all_time_drawdown_pct"] == -29.8
        assert sc_exp["entry_locks"]["locked"] is False

        # 3) Rolled-off historical peak: rolling decision drawdown is -4.00% (normal),
        # while all-time fall from peak (-23.20%) is still reported honestly.
        rolled_off = update_drawdown(expired_cap, _session_label(53), Decimal("80000"), config=cfg)
        for idx in range(54, 83):
            rolled_off = update_drawdown(rolled_off, _session_label(idx), Decimal("76800"), config=cfg)
        assert rolled_off.paused is False
        assert rolled_off.decision_drawdown == Decimal("0.04")

        ledger.risk_state_set(DRAWDOWN_STATE_KEY, rolled_off.to_record())
        tier_norm, _ = describe_drawdown_tier(None, ledger, config=cfg)
        assert tier_norm == "normal"
        daily_norm_txt = str(await rb.build_daily_summary(snapshot=snap))
        assert "Normal — all safety rules clear" in daily_norm_txt
        assert "Rolling 30-session decision drawdown -4.00%" in daily_norm_txt
        assert "All-time fall from peak -23.20%" in daily_norm_txt

        # 4) Exact -20.00% rolling drawdown without prior crossing is throttled, NOT cap-elapsed.
        exact_20 = update_drawdown(
            update_drawdown(DrawdownState(), "2026-01-02", Decimal("100000"), config=cfg),
            "2026-01-05",
            Decimal("80000"),
            config=cfg,
        )
        tier_20, label_20 = describe_drawdown_tier(None, persisted=exact_20, config=cfg)
        assert tier_20 == "throttle_15pct"
        assert "pause cap elapsed" not in label_20

        # 5) Corrupt persisted drawdown record fails closed in entry-lock status.
        ledger.risk_state_set(DRAWDOWN_STATE_KEY, {"hwm": "50000"})
        _, sc_corrupt = await rb.build_weekly_scorecard(snapshot=snap)
        assert sc_corrupt["entry_locks"]["locked"] is True
        assert any(
            item["code"] == "risk_state_unavailable"
            for item in sc_corrupt["entry_locks"]["locks"]
        )

        ledger.store.close()

    asyncio.run(_run())
