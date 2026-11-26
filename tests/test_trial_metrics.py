"""Tests for broker-neutral trial/data-quality metrics and HEARTBEAT_PING_URL wiring."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from stock_agent.benchmark import BenchmarkTracker
from stock_agent.config import Config, load_settings
from stock_agent.heartbeat import HeartbeatClient
from stock_agent.journal import Journal
from stock_agent.ledger import (
    BookSnapshot,
    Holding,
    IntentStatus,
    NewIntent,
    OptionLeg,
    ThesisRole,
)
from stock_agent.options_book import KvOptionsStore, Leg as OptBookLeg, Structure
from stock_agent.price_service import DailyBar
from stock_agent.reporting import ReportBuilder
from stock_agent.research_store import ResearchStore, week_key
from stock_agent.scheduler import StockAgentScheduler
from stock_agent.trial_metrics import (
    collect_trial_metrics,
    format_trial_metrics_field,
    install_broker_quote_observer,
)
from tests.fakes.world import FakeWorld
from tests.ledger_helpers import StubBroker, make_ledger

ET = ZoneInfo("America/New_York")


class _StubCalendar:
    def __init__(self, *, regular_open: bool = True) -> None:
        self.regular_open = regular_open

    def is_open(self, at: datetime | None = None) -> bool:
        return self.regular_open

    def regular_market_gate(self, at: datetime | None = None):
        return SimpleNamespace(allowed=self.regular_open, reason="open" if self.regular_open else "closed")


class _StaticBarPriceStub:
    def __init__(self, bars: dict[str, list[DailyBar]]) -> None:
        self._bars = bars

    async def get_daily_bars(self, symbols, *, sessions=60, start_date=None, end_date=None):
        return {
            sym: [
                b for b in self._bars.get(sym, [])
                if (start_date is None or b.session_date >= start_date)
                and (end_date is None or b.session_date <= end_date)
            ]
            for sym in symbols
        }

    async def get_quotes(self, symbols, *, purpose="brief", max_age_s=None):
        return SimpleNamespace(quotes={}, missing={sym: "unavailable" for sym in symbols})


def _record_usage(
    store,
    *,
    request_id: str,
    created_at: datetime,
    settled_microusd: int,
    exit_work: int = 0,
) -> None:
    with store.transaction() as tx:
        tx.execute(
            """
            INSERT INTO research_usage (
                request_id, week, reserved_microusd, settled_microusd, exit_work, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                week_key(created_at),
                settled_microusd,
                settled_microusd,
                exit_work,
                created_at.astimezone(timezone.utc).isoformat(),
            ),
        )


def test_research_cost_primary_evidence_and_insufficient_evidence_by_job(tmp_path: Path) -> None:
    broker = StubBroker(cash=100_000.0)
    now_holder = [datetime(2026, 11, 25, 16, 15, tzinfo=ET)]
    ledger = make_ledger(tmp_path, broker, clock=lambda: now_holder[0])
    cfg = Config(DATA_DIR=tmp_path)
    ResearchStore(ledger.store, cfg)
    ledger.store.apply_migrations()

    try:
        t_today_1 = datetime(2026, 11, 25, 14, 5, tzinfo=timezone.utc)
        t_today_2 = datetime(2026, 11, 25, 17, 10, tzinfo=timezone.utc)
        t_prev = datetime(2026, 11, 23, 15, 10, tzinfo=timezone.utc)

        # Spend includes ordinary jobs ($1.20) AND shadow jobs ($0.30) when enabled (by design = $1.50 today),
        # while shadow decision rows (`shadow:` prefix) are excluded from trading decision counts.
        _record_usage(ledger.store, request_id="req-1", created_at=t_today_1, settled_microusd=1_200_000)
        _record_usage(ledger.store, request_id="req-shadow-1", created_at=t_today_1, settled_microusd=300_000)
        _record_usage(ledger.store, request_id="req-prev-1", created_at=t_prev, settled_microusd=2_500_000)

        # 1) Prescan validated entry thesis WITH dated & cited SEC filing primary evidence
        now_holder[0] = t_today_1
        ledger.record_thesis(
            "AAA",
            {
                "job": "prescan",
                "inputs_as_of": t_today_1.isoformat(),
                "evidence": [
                    {
                        "evidence_id": "sec:filing:0000320193-26-000010",
                        "url": "https://www.sec.gov/Archives/edgar/data/0000320193/000032019326000010/aaa-20260930.htm",
                        "published_at": "2026-11-24T21:30:00+00:00",
                        "title": "AAA Form 10-Q",
                        "snippet": "Form 10-Q revenue increased 18% year over year to $4.2B.",
                    }
                ],
                "draft": {
                    "direction": "BUY",
                    "claims": [
                        {
                            "text": "Q3 revenue grew 18% YoY.",
                            "evidence_ids": ["sec:filing:0000320193-26-000010"],
                        }
                    ],
                },
            },
            ThesisRole.ENTRY_CANDIDATE,
            source="openai/gpt-5-mini",
            decision_id="prescan:2026-11-25:AAA",
            generated_at=t_today_1,
        )

        # 2) Prescan INSUFFICIENT_EVIDENCE entry thesis with ONLY undated / future / XBRL / index-stub / commentary
        ledger.record_thesis(
            "BBB",
            {
                "job": "prescan",
                "inputs_as_of": t_today_1.isoformat(),
                "evidence": [
                    {
                        # Undated SEC filing -> MUST fail closed
                        "evidence_id": "sec:filing:undated",
                        "url": "https://www.sec.gov/Archives/edgar/data/0000789019/000078901926000012/bbb-10q.htm",
                        "published_at": None,
                        "snippet": "Undated filing snippet.",
                    },
                    {
                        # Future-dated wire item -> MUST fail closed
                        "evidence_id": "wire:future",
                        "url": "https://www.businesswire.com/news/home/202611260001/en/BBB-Update",
                        "published_at": "2026-11-26T12:00:00+00:00",
                        "snippet": "Future timestamp.",
                    },
                    {
                        # XBRL ratio rollup -> MUST fail closed
                        "evidence_id": "sec:xbrl:BBB:Revenues",
                        "url": "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/Revenues.json",
                        "published_at": "2026-11-24T20:00:00+00:00",
                        "snippet": "XBRL concept rollup.",
                    },
                    {
                        # Bare SEC index stub -> MUST fail closed
                        "evidence_id": "web:sec-index",
                        "url": "https://www.sec.gov/Archives/edgar/data/0000789019/0000789019-26-000012-index.htm",
                        "published_at": "2026-11-24T20:00:00+00:00",
                        "snippet": "SEC EDGAR Form 10-Q filed on 2026-11-24",
                    },
                    {
                        # Secondary market commentary -> NOT issuer/SEC primary
                        "evidence_id": "news:benzinga:1",
                        "url": "https://www.benzinga.com/markets/26/11/1000/bbb-rumor",
                        "published_at": "2026-11-25T13:00:00+00:00",
                        "snippet": "Analyst commentary on sector multiples.",
                    },
                ],
                "draft": {"direction": "INSUFFICIENT_EVIDENCE", "claims": []},
            },
            ThesisRole.ENTRY_CANDIDATE,
            source="openai/gpt-5-mini",
            decision_id="prescan:2026-11-25:BBB",
            generated_at=t_today_1,
        )

        # 3) Midweek held-position review WITH dated PR Newswire primary evidence (present, uncited)
        now_holder[0] = t_today_2
        ledger.record_thesis(
            "CCC",
            {
                "job": "midweek",
                "inputs_as_of": t_today_2.isoformat(),
                "evidence": [
                    {
                        "evidence_id": "fmp:press:ccc-1",
                        "url": "https://www.prnewswire.com/news-releases/ccc-announces-guidance-300000001.html",
                        "published_at": "2026-11-25T13:30:00+00:00",
                        "snippet": "CCC raises FY2027 revenue guidance by $150M.",
                    }
                ],
                "draft": {"direction": "HOLD", "claims": []},
            },
            ThesisRole.HELD_REVIEW,
            source="openai/gpt-5-mini",
            decision_id="midweek:2026-11-25:CCC",
            generated_at=t_today_2,
        )

        # 4) News entry thesis ending INSUFFICIENT_EVIDENCE -> counted in news job, NOT in scheduled review share
        ledger.record_thesis(
            "DDD",
            {
                "job": "news",
                "inputs_as_of": t_today_2.isoformat(),
                "evidence": [],
                "draft": {"direction": "INSUFFICIENT_EVIDENCE", "claims": []},
            },
            ThesisRole.ENTRY_CANDIDATE,
            source="openai/gpt-5-mini",
            decision_id="news:2026-11-25:DDD",
            generated_at=t_today_2,
        )

        # 5) Shadow audit-only thesis (`shadow:` prefix) & legacy unslashed source -> MUST be excluded from decisions
        ledger.record_thesis(
            "AAA",
            {"job": "prescan", "draft": {"direction": "BUY", "claims": []}},
            ThesisRole.ENTRY_CANDIDATE,
            source="anthropic/claude-sonnet-4-5",
            decision_id="shadow:prescan:2026-11-25:AAA",
            generated_at=t_today_2,
        )
        ledger.record_thesis(
            "ZZZ",
            {"job": "prescan", "draft": {"direction": "BUY", "claims": []}},
            ThesisRole.ENTRY_CANDIDATE,
            source="v0",
            decision_id="legacy:ZZZ",
            generated_at=t_today_2,
        )

        # 6) Earlier in 7d window: weekly entry thesis with issuer IR primary evidence
        now_holder[0] = t_prev
        ledger.record_thesis(
            "FFF",
            {
                "job": "weekly",
                "inputs_as_of": t_prev.isoformat(),
                "evidence": [
                    {
                        "evidence_id": "web:ir-fff",
                        "url": "https://investor.example.com/news-releases/q3-results",
                        "published_at": "2026-11-23T12:00:00+00:00",
                        "snippet": "Quarter ended September 30 GAAP diluted EPS was $1.42.",
                    }
                ],
                "draft": {"direction": "BUY", "claims": [{"evidence_ids": ["web:ir-fff"]}]},
            },
            ThesisRole.ENTRY_CANDIDATE,
            source="openai/gpt-5-mini",
            decision_id="weekly:2026-11-23:FFF",
            generated_at=t_prev,
        )

        as_of = datetime(2026, 11, 25, 16, 30, tzinfo=ET)
        metrics = collect_trial_metrics(store=ledger.store, ledger=ledger, as_of=as_of)

        # Metric 1: Research cost per validated decision
        rc_s = metrics["research_cost_per_validated_decision"]["session"]
        assert rc_s["spend_usd"] == pytest.approx(1.50)
        assert rc_s["validated_decisions"] == 4  # AAA, BBB, CCC, DDD (shadow + v0 excluded)
        assert rc_s["grounded_decisions"] == 2   # AAA (BUY) + CCC (HOLD)
        assert rc_s["cost_per_validated_decision_usd"] == pytest.approx(1.50 / 4, rel=1e-4)
        assert rc_s["cost_per_grounded_decision_usd"] == pytest.approx(1.50 / 2, rel=1e-4)

        rc_7 = metrics["research_cost_per_validated_decision"]["rolling_7d"]
        assert rc_7["spend_usd"] == pytest.approx(4.00)
        assert rc_7["validated_decisions"] == 5
        assert rc_7["cost_per_validated_decision_usd"] == pytest.approx(0.80)

        # Metric 2: Share of scheduled reviews with dated primary (issuer/SEC) evidence
        # Today scheduled reviews: AAA (yes, cited), BBB (no), CCC (yes, uncited) -> 2/3 (66.7%), 1 cited
        pe_s = metrics["scheduled_primary_evidence"]["session"]
        assert pe_s["scheduled_reviews"] == 3
        assert pe_s["with_dated_primary"] == 2
        assert pe_s["with_dated_primary_cited"] == 1
        assert pe_s["share_pct"] == pytest.approx(66.7, abs=0.1)

        # Metric 3: Share of entry theses ending INSUFFICIENT_EVIDENCE by job
        ie_s = metrics["entry_insufficient_evidence_by_job"]["session"]
        assert ie_s["by_job"]["prescan"] == {"total": 2, "insufficient_evidence": 1, "share_pct": 50.0}
        assert ie_s["by_job"]["news"] == {"total": 1, "insufficient_evidence": 1, "share_pct": 100.0}
        assert "midweek" not in ie_s["by_job"]  # CCC was HELD_REVIEW, not an entry thesis
        assert ie_s["scheduled"] == {"total": 2, "insufficient_evidence": 1, "share_pct": 50.0}
        assert ie_s["overall"] == {"total": 3, "insufficient_evidence": 2, "share_pct": pytest.approx(66.7, abs=0.1)}
    finally:
        ledger.store.close()


def test_stock_quote_coverage_counts_regular_hours_only(monkeypatch, tmp_path: Path) -> None:
    async def _run() -> None:
        # Regular hours Wednesday 2026-11-25 10:30 ET
        world = FakeWorld()
        world.install(monkeypatch, data_dir=tmp_path)
        world.clock.set_et(2026, 11, 25, 10, 30)
        world.price("AAA", 100.0)
        # BBB has no price set in FakeAlpaca or FakeFMP -> missing quote

        sched = world.scheduler()
        try:
            await sched.startup()
            # Ensure calendar knows Wednesday 10:30 ET is regular open
            await sched.calendar.refresh()

            # 1) Regular-hours batch: 2 symbols requested -> 1 fresh (AAA), 1 missing (BBB)
            res_open = await sched.prices.get_quotes(["AAA", "BBB"], purpose="order_sizing")
            assert "AAA" in res_open.quotes
            assert "BBB" in res_open.missing

            # 2) Advance to after-hours 18:00 ET and Saturday 11:00 ET -> must NOT be counted in regular-hours rate
            world.clock.set_et(2026, 11, 25, 18, 0)
            await sched.calendar.refresh()
            await sched.prices.get_quotes(["AAA"], purpose="brief")

            metrics = collect_trial_metrics(
                store=sched.store,
                price_service=sched.prices,
                config=sched.config,
                as_of=world.clock.now(),
            )
            sq = metrics["stock_quote_coverage_regular_hours"]["session"]
            assert sq["requested"] == 2
            assert sq["fresh"] == 1
            assert sq["fresh_rate_pct"] == pytest.approx(50.0)
        finally:
            await sched._cleanup()

    asyncio.run(_run())


def test_option_leg_fresh_two_sided_share_and_quote_rejections_broker_neutral(tmp_path: Path) -> None:
    async def _run() -> None:
        broker_stub = StubBroker()
        now_utc = datetime(2026, 11, 25, 15, 45, tzinfo=timezone.utc)  # 10:45 ET regular hours
        ledger = make_ledger(tmp_path, broker_stub, clock=lambda: now_utc)
        journal = ledger.journal
        opt_store = KvOptionsStore(ledger.store)
        cfg = Config(DATA_DIR=tmp_path, OPTIONS_QUOTE_MAX_AGE_SEC=90)
        cal = _StubCalendar(regular_open=True)

        try:
            class _HybridOptionBroker:
                async def get_option_snapshots(self, symbols, *args, **kwargs):
                    return {
                        # 1) Fresh standard latestQuote snapshot -> PASS
                        "AAA261218C00100000": {
                            "latestQuote": {
                                "bp": 2.40,
                                "ap": 2.55,
                                "t": (now_utc - timedelta(seconds=15)).isoformat(),
                            }
                        },
                        # 2) Fresh IBKR-style live (market_data_type=1 + gateway clocks) -> PASS
                        "BBB261218P00050000": {
                            "latestQuote": {"bp": 1.10, "ap": 1.20},
                            "ibkr_meta": {
                                "market_data_type": 1,
                                "bid_gateway_observed_at": (now_utc - timedelta(seconds=12)).isoformat(),
                                "ask_gateway_observed_at": (now_utc - timedelta(seconds=8)).isoformat(),
                            },
                        },
                        # 3) IBKR delayed market_data_type=3 -> FAIL CLOSED
                        "CCC261218C00075000": {
                            "latestQuote": {"bp": 1.50, "ap": 1.65},
                            "ibkr_meta": {
                                "market_data_type": 3,
                                "bid_gateway_observed_at": (now_utc - timedelta(seconds=5)).isoformat(),
                                "ask_gateway_observed_at": (now_utc - timedelta(seconds=5)).isoformat(),
                            },
                        },
                        # 4) Zero bid -> FAIL CLOSED
                        "DDD261218P00040000": {
                            "latestQuote": {
                                "bp": 0.0,
                                "ap": 0.25,
                                "t": (now_utc - timedelta(seconds=5)).isoformat(),
                            }
                        },
                        # 5) Stale quote (> 90s) -> FAIL CLOSED
                        "EEE261218C00120000": {
                            "latestQuote": {
                                "bp": 3.10,
                                "ap": 3.30,
                                "t": (now_utc - timedelta(seconds=300)).isoformat(),
                            }
                        },
                    }

            opt_broker = _HybridOptionBroker()
            install_broker_quote_observer(
                opt_broker,
                store=ledger.store,
                config=cfg,
                calendar=cal,
                clock=lambda: now_utc,
            )
            # Request 6 legs (FFF missing completely -> 2 fresh two-sided out of 6 checked = 33.3%)
            await opt_broker.get_option_snapshots(
                [
                    "AAA261218C00100000",
                    "BBB261218P00050000",
                    "CCC261218C00075000",
                    "DDD261218P00040000",
                    "EEE261218C00120000",
                    "FFF261218C00090000",
                ]
            )

            # Quote-caused option rejections vs non-quote business skips
            journal.append("options.scan_skip", underlying="CCC", reason="stale_quote age=310s")
            journal.append("options.scan_skip", underlying="DDD", reason="spread 28.0% > max 12.0%")
            journal.append("options.scan_skip", underlying="GGG", reason="iv_hv_ratio 1.45 > max 1.25")
            journal.append(
                "options.mark_rejected",
                structure_id="str-1",
                problems={"AAA261218C00100000": "missing_quote"},
            )
            journal.append("options.close_stopped", structure_id="str-1", source="intrinsic")

            st = Structure(
                id="str-1",
                underlying="AAA",
                strategy="long_call",
                direction="bullish",
                qty=1,
                expiry="2026-12-18",
                legs=[
                    OptBookLeg(
                        symbol="AAA261218C00100000",
                        side="buy",
                        type="call",
                        strike=100.0,
                        expiry="2026-12-18",
                    )
                ],
                entry_debit=2.50,
                max_loss=250.0,
                opened_at="2026-11-24T15:00:00+00:00",
                notes=[f"{now_utc.isoformat()} close skipped: no usable quote"],
            )
            opt_store.save([st], {}, {})

            metrics = collect_trial_metrics(
                store=ledger.store,
                journal=journal,
                options_store=opt_store,
                config=cfg,
                as_of=now_utc,
            )

            ol = metrics["option_legs_fresh_two_sided"]["session"]
            assert ol["legs_checked"] == 6
            assert ol["fresh_two_sided"] == 2
            assert ol["fresh_two_sided_share_pct"] == pytest.approx(33.3, abs=0.1)

            qr = metrics["quote_caused_option_rejections"]["session"]
            assert qr["scan_skips"] == 2  # iv_hv_ratio non-quote skip excluded!
            assert qr["mark_rejections"] == 1
            assert qr["close_quote_failures"] == 2  # close_stopped:intrinsic + note "close skipped: no usable quote"
            assert qr["total"] == 5
        finally:
            ledger.store.close()

    asyncio.run(_run())


def test_commissions_and_slippage_fail_closed_when_unreported_and_exact_on_ibkr(tmp_path: Path) -> None:
    async def _run() -> None:
        now_utc = datetime(2026, 11, 25, 16, 5, tzinfo=timezone.utc)

        # Case A: Broker does NOT report per-fill commission (Alpaca-shaped FILL activities)
        broker_a = StubBroker(cash=100_000.0)
        ledger_a = make_ledger(tmp_path / "alpaca_like", broker_a, clock=lambda: now_utc)
        try:
            # Enqueue, claim, and fill an equity BUY order: ref=100.00, fill=100.20, qty=10 -> +20.0 bps / +$2.00 adverse
            intents = ledger_a.enqueue_intents(
                [
                    NewIntent(
                        symbol="AAA",
                        side="buy",
                        dedupe_key="prescan:2026-11-25:AAA:buy",
                        kind="equity",
                        reference_price=Decimal("100.00"),
                        session_date=date(2026, 11, 25),
                    )
                ],
                job="prescan",
                window_key="2026-11-25",
            )
            claimed = ledger_a.claim_intent(intents[0], check_thesis=False)
            assert claimed is not None
            ledger_a.ack_intent(
                claimed.intent_id,
                IntentStatus.FILLED,
                broker_order_id="o1",
                meta={"filled_qty": "10", "avg_price": "100.20"},
            )
            broker_a.fill("AAA", "buy", 10, 100.20, ts="2026-11-25T15:37:00+00:00")
            snap_a = await ledger_a.refresh("test")

            metrics_a = collect_trial_metrics(
                store=ledger_a.store,
                ledger=ledger_a,
                snapshot=snap_a,
                as_of=now_utc,
            )
            comm_a = metrics_a["trade_commissions_and_slippage"]["session"]["commissions"]
            assert comm_a["total_fills"] == 1
            assert comm_a["fills_with_reported_commission"] == 0
            assert comm_a["reported"] is False
            assert comm_a["status"] == "not_reported_by_broker"
            assert comm_a["total_commission_usd"] is None  # NEVER $0.00 when unreported!

            slip_a = metrics_a["trade_commissions_and_slippage"]["session"]["slippage"]
            assert slip_a["trades_measured"] == 1
            assert slip_a["equity_trades_measured"] == 1
            assert slip_a["total_slippage_usd"] == pytest.approx(2.00)
            assert slip_a["mean_slippage_bps"] == pytest.approx(20.0, abs=0.1)

            rendered_a = format_trial_metrics_field(metrics_a)
            assert "fees not reported by broker (1 fills)" in rendered_a
            assert "+$2.00 (+20.0 bps avg, 1 trades)" in rendered_a
            for word in ("Alpaca", "IBKR", "Interactive Brokers"):
                assert word not in rendered_a
        finally:
            ledger_a.store.close()

        # Case B: Broker reports per-fill commissions + IBKR/desk `opt_orders` table
        broker_b = StubBroker(cash=100_000.0)
        ledger_b = make_ledger(tmp_path / "ibkr_like", broker_b, clock=lambda: now_utc)
        try:
            # Equity SELL order: ref=50.00, fill=49.90, qty=10 -> +20.0 bps / +$1.00 adverse
            sell_intents = ledger_b.enqueue_intents(
                [
                    NewIntent(
                        symbol="BBB",
                        side="sell",
                        dedupe_key="stop:2026-11-25:BBB:sell",
                        kind="stop",
                        reference_price=Decimal("50.00"),
                        session_date=date(2026, 11, 25),
                    )
                ],
                job="stop",
                window_key="2026-11-25-stop",
            )
            claimed_sell = ledger_b.claim_intent(sell_intents[0], check_thesis=False)
            assert claimed_sell is not None
            ledger_b.ack_intent(
                claimed_sell.intent_id,
                IntentStatus.FILLED,
                broker_order_id="ibkr-ord-sell",
                meta={"filled_qty": "10", "avg_price": "49.90"},
            )

            # IBKR-shaped FILL activities with settled USD fee, provisional fee, and non-USD fee
            broker_b.act(
                "FILL",
                "BBB",
                "2026-11-25T15:10:00Z",
                side="sell",
                qty="10",
                price="49.90",
                order_id="ibkr-ord-sell",
                commission="1.00",
                commission_currency="USD",
                provisional_commission=False,
            )
            broker_b.act(
                "FILL",
                "AAA261218C00100000",
                "2026-11-25T15:20:00Z",
                side="buy",
                qty="1",
                price="2.52",
                order_id="ibkr-opt-ord-1",
                commission="0.65",
                commission_currency="USD",
                provisional_commission=False,
            )
            broker_b.act(
                "FILL",
                "PROV",
                "2026-11-25T15:22:00Z",
                side="buy",
                qty="5",
                price="20.00",
                order_id="ibkr-ord-prov",
                commission="0.50",
                commission_currency="USD",
                provisional_commission=True,
            )
            broker_b.act(
                "FILL",
                "CADSYM",
                "2026-11-25T15:25:00Z",
                side="buy",
                qty="5",
                price="10.00",
                order_id="ibkr-ord-cad",
                commission="1.50",
                commission_currency="CAD",
            )
            await ledger_b.refresh("test")

            # Simulate IBKR/desk `opt_orders` schema from `options_ledger.py`
            with ledger_b.store.transaction() as tx:
                tx.execute(
                    """
                    CREATE TABLE opt_orders (
                        order_id TEXT PRIMARY KEY,
                        structure_id TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        status TEXT NOT NULL,
                        broker_order_id TEXT,
                        filled_qty INTEGER NOT NULL DEFAULT 0,
                        mid_net TEXT,
                        avg_net TEXT,
                        actual_fees_usd TEXT,
                        quote_grade TEXT,
                        leg_quotes_json TEXT NOT NULL DEFAULT '{}',
                        last_error TEXT,
                        reason TEXT,
                        prepared_at TEXT NOT NULL,
                        submitted_at TEXT,
                        terminal_at TEXT
                    )
                    """
                )
                # Same broker_order_id as ibkr-opt-ord-1 -> fee deduplicated, option debit slippage (+80 bps, +$2.00) recorded
                tx.execute(
                    """
                    INSERT INTO opt_orders VALUES (
                        'opt-1', 'str-ibkr-1', 'open', 'filled', 'ibkr-opt-ord-1',
                        1, '2.50', '2.52', '0.65', 'realtime',
                        '{"AAA261218C00100000": [2.45, 2.55, 2.50, "2026-11-25T15:19:58+00:00"]}',
                        NULL, NULL,
                        '2026-11-25T15:19:59+00:00', '2026-11-25T15:20:00+00:00', '2026-11-25T15:20:01+00:00'
                    )
                    """
                )
                # Second option credit order not in broker_activities -> adds $1.30 fee and +100 bps / +$1.00 slippage
                tx.execute(
                    """
                    INSERT INTO opt_orders VALUES (
                        'opt-2', 'str-ibkr-2', 'close', 'filled', 'ibkr-opt-ord-2',
                        1, '-1.00', '-0.99', '1.30', 'realtime', '{}',
                        NULL, NULL,
                        '2026-11-25T15:30:00+00:00', '2026-11-25T15:30:01+00:00', '2026-11-25T15:30:02+00:00'
                    )
                    """
                )

            metrics_b = collect_trial_metrics(store=ledger_b.store, ledger=ledger_b, as_of=now_utc)
            comm_b = metrics_b["trade_commissions_and_slippage"]["session"]["commissions"]
            assert comm_b["reported"] is True
            # $1.00 (sell) + $0.65 (opt-1, deduplicated!) + $1.30 (opt-2) = $2.95 (provisional & CAD excluded from USD sum)
            assert comm_b["total_commission_usd"] == pytest.approx(2.95)
            assert comm_b["provisional_fills"] == 1
            assert comm_b["non_usd_commissions"] == {"CAD": 1.5}

            slip_b = metrics_b["trade_commissions_and_slippage"]["session"]["slippage"]
            assert slip_b["equity_trades_measured"] == 1
            assert slip_b["option_trades_measured"] == 2
            assert slip_b["trades_measured"] == 3
            assert slip_b["total_slippage_usd"] == pytest.approx(4.00)  # $1.00 + $2.00 + $1.00
            # (+20 + +80 + +100) / 3 = +66.7 bps
            assert slip_b["mean_slippage_bps"] == pytest.approx(66.7, abs=0.1)

            # Also verify opt_orders.leg_quotes_json fallback populates option_legs_fresh_two_sided when KV is empty
            ol_b = metrics_b["option_legs_fresh_two_sided"]["session"]
            assert ol_b["legs_checked"] == 1
            assert ol_b["fresh_two_sided"] == 1

            rendered_b = format_trial_metrics_field(metrics_b)
            assert "fees $2.95" in rendered_b
            for word in ("Alpaca", "IBKR", "Interactive Brokers"):
                assert word not in rendered_b
        finally:
            ledger_b.store.close()

    asyncio.run(_run())


def test_daily_summary_and_runtime_health_surface_trial_metrics(monkeypatch, tmp_path: Path) -> None:
    async def _run() -> None:
        world = FakeWorld()
        world.install(monkeypatch, data_dir=tmp_path)
        world.clock.set_et(2026, 11, 25, 16, 20)
        sched = world.scheduler()
        try:
            await sched.startup()
            sched._publish_runtime()

            health_path = tmp_path / "runtime-health.json"
            payload = json.loads(health_path.read_text())
            assert "trial_metrics" in payload
            tm = payload["trial_metrics"]
            assert tm["session_date"] == "2026-11-25"
            for section in (
                "research_cost_per_validated_decision",
                "scheduled_primary_evidence",
                "entry_insufficient_evidence_by_job",
                "stock_quote_coverage_regular_hours",
                "option_legs_fresh_two_sided",
                "quote_caused_option_rejections",
                "trade_commissions_and_slippage",
            ):
                assert section in tm

            d_close = date(2026, 11, 25)
            bar_stub = _StaticBarPriceStub(
                {
                    "QQQ": [DailyBar("QQQ", d_close, Decimal("500"), Decimal("500"), Decimal("500"), Decimal("500"), 1000)],
                    "SPY": [DailyBar("SPY", d_close, Decimal("600"), Decimal("600"), Decimal("600"), Decimal("600"), 1000)],
                }
            )
            rb = ReportBuilder(
                config=sched.config,
                ledger=sched.ledger,
                price_service=bar_stub,
                benchmark_tracker=BenchmarkTracker(bar_stub),
                calendar=sched.calendar,
                options_store=sched.options.store,
                clock=lambda: datetime(2026, 11, 25, 16, 20, tzinfo=ET),
            )

            as_of_close = datetime(2026, 11, 25, 16, 20, tzinfo=ET)
            # 1) Missed-close / no-snapshot fallback includes Trial & Data-Quality Metrics
            missed_embeds = await rb.build_daily_summary(as_of=as_of_close)
            missed_fields = {f["name"]: f["value"] for f in missed_embeds[0]["fields"]}
            assert "Trial & Data-Quality Metrics" in missed_fields
            assert len(missed_fields["Trial & Data-Quality Metrics"]) <= 1024

            # 2) Normal close summary with recorded close mark
            snap = await sched.ledger.refresh("test")
            sched.ledger.record_equity_mark(d_close, "close", snap)
            embeds = await rb.build_daily_summary(snapshot=snap, as_of=as_of_close)
            fields = {f["name"]: f["value"] for f in embeds[0]["fields"]}
            assert "Trial & Data-Quality Metrics" in fields
            val = fields["Trial & Data-Quality Metrics"]
            assert len(val) <= 1024
            for expected_label in (
                "Research Cost / Decision:",
                "Scheduled Primary Evidence:",
                "Entry INSUFFICIENT_EVIDENCE:",
                "Stock Quotes (Regular Hours):",
                "Option Legs Two-Sided:",
                "Option Quote Rejections:",
                "Commissions & Slippage:",
            ):
                assert expected_label in val
            for forbidden_broker_word in ("Alpaca", "IBKR", "Interactive Brokers"):
                assert forbidden_broker_word not in val
        finally:
            await sched._cleanup()

    asyncio.run(_run())


def test_heartbeat_ping_url_end_to_end_and_redaction(monkeypatch, tmp_path: Path, caplog) -> None:
    async def _run() -> None:
        received: list[tuple[str, str, str]] = []

        async def _handle_ping(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                raw_head = await reader.readuntil(b"\r\n\r\n")
                head_text = raw_head.decode("latin1")
                lines = head_text.split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                content_len = 0
                for line in lines[1:]:
                    if line.lower().startswith("content-length:"):
                        content_len = int(line.split(":", 1)[1].strip())
                body = (await reader.readexactly(content_len)).decode("utf-8", errors="replace") if content_len else ""
                received.append((method, path, body))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(_handle_ping, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        synthetic_uuid = "00000000-0000-4000-8000-000000000001"
        ping_url = f"http://127.0.0.1:{port}/ping/{synthetic_uuid}"

        now_holder = [datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)]
        monkeypatch.setattr("stock_agent.scheduler._now_utc", lambda: now_holder[0])

        # 1) Empty HEARTBEAT_PING_URL (current server default) -> clean no-op
        settings_empty = load_settings(
            environ={"STOCK_DATA_DIR": str(tmp_path / "empty_hb"), "HEARTBEAT_PING_URL": ""},
            load_dotenv_file=False,
        )
        cfg_empty = Config(**{k: getattr(settings_empty, k) for k in settings_empty.__class__.model_fields})
        assert cfg_empty.HEARTBEAT_PING_URL == ""
        assert cfg_empty.HEARTBEAT_INTERVAL_SEC == 300
        hb_empty = HeartbeatClient(cfg_empty, clock=lambda: now_holder[0])
        await hb_empty.tick()
        await hb_empty.close()
        assert received == []

        # 2) Configured HEARTBEAT_PING_URL -> Scheduler._notification_tick() sends POST end to end,
        #    rate-limits within 300s, sends again at +300s, and hits /fail on explicit failure.
        settings_live = load_settings(
            environ={
                "STOCK_DATA_DIR": str(tmp_path / "live_hb"),
                "HEARTBEAT_PING_URL": ping_url,
                "HEARTBEAT_INTERVAL_SEC": "300",
            },
            load_dotenv_file=False,
        )
        cfg_live = Config(**{k: getattr(settings_live, k) for k in settings_live.__class__.model_fields})
        assert cfg_live.HEARTBEAT_PING_URL == ping_url
        assert ping_url not in repr(cfg_live)  # Secret-masked in Config.__repr__

        stub_broker = StubBroker()
        sched = StockAgentScheduler(
            config=cfg_live,
            services=SimpleNamespace(broker=stub_broker),
        )
        try:
            await sched._notification_tick()
            assert len(received) == 1
            assert received[0][0] == "POST"
            assert received[0][1] == f"/ping/{synthetic_uuid}"
            assert received[0][2].startswith("ok mode=PAPER ts=2026-11-26T02:00:00+00:00")

            # Within 300s interval -> rate-limited, no duplicate ping
            now_holder[0] = now_holder[0] + timedelta(seconds=120)
            await sched._notification_tick()
            assert len(received) == 1

            # At +300s -> next scheduled heartbeat fires
            now_holder[0] = now_holder[0] + timedelta(seconds=180)
            await sched._notification_tick()
            assert len(received) == 2

            # Explicit failure signal posts to Healthchecks.io `/fail` suffix
            await sched.heartbeat.send_fail("reconcile_stalled")
            assert len(received) == 3
            assert received[2][0] == "POST"
            assert received[2][1] == f"/ping/{synthetic_uuid}/fail"
            assert received[2][2] == "fail ts=2026-11-26T02:05:00+00:00 reconcile_stalled"

            # Close server and verify transport errors never leak the secret ping UUID into logs
            server.close()
            await server.wait_closed()
            caplog.set_level(logging.WARNING)
            now_holder[0] = now_holder[0] + timedelta(seconds=301)
            await sched._notification_tick()
            joined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
            assert synthetic_uuid not in joined_logs
            assert ping_url not in joined_logs
        finally:
            await sched._cleanup()

    asyncio.run(_run())
