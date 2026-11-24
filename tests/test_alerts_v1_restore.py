"""Tests for V1-restore Part 5 blocked-buy alerts and plain-English handoff/rescan text."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from stock_agent.alerts import (
    AlertCategory,
    AlertEvent,
    AlertService,
    OutboxItem,
    format_handoff_outcome_text,
    format_market_rescan_text,
)
from stock_agent.config import Config

ET = ZoneInfo("America/New_York")


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.DATA_DIR = tmp_path
    cfg.DISCORD_BOT_TOKEN = "test-discord"
    cfg.DISCORD_CHANNEL_RISK = "1001"
    cfg.DISCORD_CHANNEL_ANNOUNCEMENTS = "1002"
    cfg.TELEGRAM_BOT_TOKEN = ""
    return cfg


def test_blocked_buy_plain_english_routing_and_scope_filter(world: Any, tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = _make_cfg(tmp_path)
        world.clock.set_et(2026, 11, 24, 9, 40)
        svc = AlertService(cfg, clock=world.clock.now)

        # Non-qualifying codes must return False, queue nothing, and not consume session cap.
        ignored_cases = [
            ("AAPL", "non_entry_direction", "rated HOLD"),
            ("AAPL", "conviction_below_floor", "effective conviction 6 < 7"),
            ("AAPL", "llm_budget", "weekly cap reached"),
            ("AAPL", "quote_stale_or_unavailable", "quote age 300s"),
            ("AAPL", "add_thesis_not_reaffirmed", "thesis weakened"),
            ("AAPL", "handoff_pending", "staged for open"),
            ("AAPL", "market_closed", "market gate: closed"),
            ("AAPL", "cleanup_pending", "account clean-up has not finished"),
            ("AAPL", "books_untrusted", "trading blocked: reconcile"),
            ("AAPL", "research_inputs_pending", "research gate refused at submit: quote_stale_or_unavailable (retrying)"),
            ("AAPL", "quote_unavailable", "no fresh quote"),
            ("AAPL", "risk_controls_unavailable", "drawdown state unreadable"),
            ("AAPL", "handoff_buy_pending", ""),
            ("AAPL", "handoff_superseded", ""),
            ("AAPL", "handoff_promoted", ""),
            ("AAPL", "handoff_reanchored", ""),
            ("AAPL", "duplicate", "already claimed"),
            ("AAPL", "top_up_already_today", "already topped up today"),
            ("AAPL", "risk_size_zero", "risk sizing gave less than one share: near_target"),
            ("AAPL", "risk_size_zero", "risk sizing gave less than one share: top_up_conviction"),
            ("AAPL", "broker_rejected", "HTTP 422 rejected"),
        ]
        for sym, code, raw_reason in ignored_cases:
            assert svc.alert_blocked_buy(sym, code, raw_reason, session_date="2026-11-24") is False
        assert len(svc.outbox) == 0

        # Approved order-stage & terminal handoff block reasons emit plain-English #risk alerts
        # without leaking raw codes, JSON, provider exceptions, account balances, or IDs.
        approved_cases = [
            ("AAPL", "risk_refused", "risk: sector_cap:Technology", (), "40%", "Technology"),
            ("NVDA", "risk_refused", "risk: cluster_cap:Semiconductors", (), "48%", "Semiconductors"),
            ("MSFT", "risk_size_zero", "risk sizing gave less than one share: position_count", (), "20", "slots"),
            ("AMZN", "price_ran_away", "price 104.00 ran more than 3% above the analysed 100.00", (), "3%", "price"),
            (
                "META",
                "insufficient_buying_power",
                "broker: insufficient buying power (account_id=acct-9000 buying_power=500.00)",
                (),
                "buying power",
                "META",
            ),
            ("GOOGL", "risk_refused", "risk: margin_buffer", (), "25%", "margin"),
            ("AVGO", "risk_refused", "risk: drawdown_no_new_borrowing", (), "15%", "drawdown"),
            ("LLY", "risk_refused", "risk: drawdown_pause", (), "20%", "drawdown"),
            (
                "JPM",
                "research_stale",
                "research gate and ledger thesis both unavailable: TimeoutError('upstream 504')",
                (),
                "recheck",
                "JPM",
            ),
            (
                "GS",
                "handoff_expired",
                "",
                ("quote_stale_or_unavailable",),
                "expired",
                "GS",
            ),
        ]
        for sym, code, raw_reason, risk_reasons, needle1, needle2 in approved_cases:
            assert (
                svc.alert_blocked_buy(
                    sym,
                    code,
                    raw_reason,
                    session_date="2026-11-24",
                    risk_reasons=risk_reasons,
                )
                is True
            )

        assert len(svc.outbox) == 10
        for item in svc.outbox:
            assert item.event.channel_key == "risk"
            assert item.event.category == AlertCategory.RISK_BLOCK

        await svc.flush()
        texts = world.discord.texts()
        assert len(texts) == 10
        joined = "\n".join(texts)

        for sym, _code, _raw, _rr, needle1, needle2 in approved_cases:
            matching = [t for t in texts if f"Buy blocked — {sym}" in t]
            assert len(matching) == 1
            assert needle1.lower() in matching[0].lower()
            assert needle2.lower() in matching[0].lower()

        # Never leak raw internal codes, provider exceptions, IDs, or balances
        for forbidden in (
            "sector_cap",
            "cluster_cap",
            "position_count",
            "price_ran_away",
            "insufficient_buying_power",
            "drawdown_no_new_borrowing",
            "quote_stale_or_unavailable",
            "TimeoutError",
            "upstream 504",
            "acct-9000",
            "500.00",
            "{",
        ):
            assert forbidden not in joined

    asyncio.run(_run())


def test_blocked_buy_session_dedupe_cap_10_overflow_restart_and_rollover(
    world: Any, tmp_path: Path
) -> None:
    async def _run() -> None:
        cfg = _make_cfg(tmp_path)
        world.clock.set_et(2026, 11, 24, 10, 0)
        svc = AlertService(cfg, clock=world.clock.now)

        symbols = [f"SYM{i:02d}" for i in range(1, 13)]
        for i, sym in enumerate(symbols, start=1):
            queued = svc.alert_blocked_buy(
                sym,
                "risk_refused",
                "risk: position_count",
                session_date="2026-11-24",
            )
            if i <= 10:
                assert queued is True
            else:
                # 11th and 12th unique blocks record overflow without emitting prematurely mid-session
                assert queued is False

        await svc.flush()
        assert len(world.discord.texts()) == 10
        assert not any("more" in t.lower() and "blocked" in t.lower() for t in world.discord.texts())

        # Duplicate of displayed (SYM01) and duplicate of overflowed (SYM11) do nothing
        assert svc.alert_blocked_buy("SYM01", "risk_refused", "risk: position_count", session_date="2026-11-24") is False
        assert svc.alert_blocked_buy("SYM11", "risk_refused", "risk: position_count", session_date="2026-11-24") is False

        # Restart mid-session: state reloads from alert_outbox.json and preserves both cap and seen set
        restarted = AlertService(cfg, clock=world.clock.now)
        assert (
            restarted.alert_blocked_buy("SYM02", "risk_refused", "risk: position_count", session_date="2026-11-24")
            is False
        )
        assert (
            restarted.alert_blocked_buy("SYM11", "risk_refused", "risk: position_count", session_date="2026-11-24")
            is False
        )
        # Same symbol (SYM01) with a NEW reason (gap drift vs max positions) counts once in overflow (-> 3)
        assert (
            restarted.alert_blocked_buy(
                "SYM01",
                "price_ran_away",
                "price 105.00 ran more than 3% above the analysed 100.00",
                session_date="2026-11-24",
            )
            is False
        )
        # Also verify terminal handoff_reanchor_unavailable with llm_unavailable counts cleanly (-> 4)
        assert (
            restarted.alert_blocked_buy(
                "SYM13",
                "handoff_reanchor_unavailable",
                "",
                session_date="2026-11-24",
                risk_reasons=("llm_unavailable",),
            )
            is False
        )

        # Rollback compatibility check on persisted alert_outbox.json before flush
        raw_json = json.loads(restarted.outbox_path.read_text(encoding="utf-8"))
        for entry in raw_json.get("outbox", []):
            reloaded = OutboxItem.from_dict(entry)
            assert isinstance(reloaded.event, AlertEvent)

        # Session close flush emits the single "and 4 more" line once
        assert restarted.flush_blocked_buy_overflow(session_date="2026-11-24") is True
        await restarted.flush()
        texts = world.discord.texts()
        assert len(texts) == 11
        assert "and 4 more" in texts[-1]

        # Calling flush again, restarting again, or rolling over to 2026-11-25 never repeats the overflow line
        assert restarted.flush_blocked_buy_overflow(session_date="2026-11-24") is False
        restarted2 = AlertService(cfg, clock=world.clock.now)
        assert restarted2.flush_blocked_buy_overflow(session_date="2026-11-24") is False
        assert restarted2.flush_blocked_buy_overflow(before_session="2026-11-25") is False

        world.clock.set_et(2026, 11, 25, 9, 40)
        assert (
            restarted2.alert_blocked_buy("SYM01", "risk_refused", "risk: position_count", session_date="2026-11-25")
            is True
        )
        await restarted2.flush()
        assert len(world.discord.texts()) == 12
        assert sum(1 for t in world.discord.texts() if "and 4 more" in t) == 1

    asyncio.run(_run())


def test_handoff_outcome_and_rescan_announcement_helpers(world: Any, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    world.clock.set_et(2026, 11, 24, 9, 36)
    svc = AlertService(cfg, clock=world.clock.now)

    staged_title, staged_body = format_handoff_outcome_text(
        stage="staged",
        session_date="2026-11-24",
        symbols=("AAPL", "NVDA"),
        source_job="prescan",
    )
    assert "AAPL" in staged_body and "NVDA" in staged_body
    assert "open" in staged_body.lower()

    resolved_title, resolved_body = format_handoff_outcome_text(
        stage="resolved",
        session_date="2026-11-24",
        outcomes={"handoff_promoted": 2, "handoff_reanchored": 1, "handoff_gate_refused": 1},
    )
    assert "promoted" in resolved_body.lower() or "queued" in resolved_body.lower()
    assert "handoff_promoted" not in resolved_body
    assert "handoff_gate_refused" not in resolved_body

    assert svc.send_handoff_outcome(
        stage="staged",
        session_date="2026-11-24",
        symbols=("AAPL", "NVDA"),
        source_job="prescan",
    ) is True
    # Deduped for the same session/job
    assert svc.send_handoff_outcome(
        stage="staged",
        session_date="2026-11-24",
        symbols=("AAPL", "NVDA"),
        source_job="prescan",
    ) is False

    rescan_with_vix = format_market_rescan_text(
        triggers=("S&P 500 +1.6% vs prior close", "VIX +3.2 pts over 30m"),
        sp500_source="SPY live quote",
        vix_source="CBOE cash VIX",
        vix_unavailable=False,
        rescan_number=1,
        max_rescans=3,
    )
    assert "SPY live quote" in rescan_with_vix
    assert "CBOE cash VIX" in rescan_with_vix
    assert "1 of 3" in rescan_with_vix

    rescan_no_vix = format_market_rescan_text(
        triggers=("S&P 500 -1.7% vs prior close",),
        sp500_source="SPY live quote",
        vix_unavailable=True,
        rescan_number=2,
        max_rescans=3,
    )
    assert "S&P-only" in rescan_no_vix
    assert "2 of 3" in rescan_no_vix


def test_scheduler_flushes_blocked_buy_overflow_only_after_close_and_handoff_completion(
    world: Any, tmp_path: Path
) -> None:
    from stock_agent.scheduler import StockAgentScheduler

    cfg = _make_cfg(tmp_path)
    world.clock.set_et(2026, 11, 24, 15, 50)
    svc = AlertService(cfg, clock=world.clock.now)
    for i in range(1, 13):
        svc.alert_blocked_buy(f"STK{i:02d}", "risk_refused", "risk: position_count", session_date="2026-11-24")

    closes_at = datetime(2026, 11, 24, 16, 0, tzinfo=ET)
    fake_session = SimpleNamespace(date=date(2026, 11, 24), closes_at=closes_at)
    handoff_due = {"flag": True}
    expired_intents = [
        SimpleNamespace(
            symbol="STK13",
            side="buy",
            job="prescan",
            session_date=date(2026, 11, 24),
            last_error="expired before submission",
        ),
        SimpleNamespace(
            symbol="STK14",
            side="buy",
            job="prescan",
            session_date=date(2026, 11, 24),
            last_error="expired before submission",
        ),
    ]

    sched = SimpleNamespace(
        alerts=svc,
        calendar=SimpleNamespace(session_on=lambda d: fake_session if str(d) == "2026-11-24" else None),
        _job_tasks={"entry_handoff": object()},
        research=SimpleNamespace(entry_handoff_due=lambda _now: handoff_due["flag"]),
        ledger=SimpleNamespace(
            expire_intents=lambda _now: len(expired_intents),
            list_intents=lambda statuses: expired_intents if "expired" in statuses else [],
        ),
    )

    # 1) Before 16:00 ET close: do not flush overflow
    StockAgentScheduler._flush_blocked_buy_overflow(sched, datetime(2026, 11, 24, 15, 50, tzinfo=ET))
    assert len(svc.outbox) == 10

    # 2) At 16:02 ET while entry_handoff is still in _job_tasks: defer so late handoff_expired counts in N
    world.clock.set_et(2026, 11, 24, 16, 2)
    StockAgentScheduler._flush_blocked_buy_overflow(sched, datetime(2026, 11, 24, 16, 2, tzinfo=ET))
    assert len(svc.outbox) == 10

    # 3) ExecutionEngine already emitted intent_expired after an all-day hold for STK13;
    #    STK14 expired silently via ledger.expire_intents() -> total 14 unique blocks (overflow = 4)
    svc.alert_blocked_buy(
        "STK13",
        "intent_expired",
        "research inputs pending; earlier held: llm_unavailable (retrying)",
        session_date=date(2026, 11, 24),
    )
    # Duplicate intent_expired with date/str/None on same session never adds a second count
    assert svc.alert_blocked_buy("STK13", "handoff_expired", "", session_date="2026-11-24") is False
    assert svc.alert_blocked_buy("STK13", "intent_expired", "expired", session_date=None) is False
    sched._job_tasks.clear()
    handoff_due["flag"] = False

    StockAgentScheduler._flush_blocked_buy_overflow(sched, datetime(2026, 11, 24, 16, 5, tzinfo=ET))
    assert len(svc.outbox) == 11
    assert "and 4 more" in svc.outbox[-1].event.body

    # 4) Subsequent close ticks and next-session rollover do not emit again
    StockAgentScheduler._flush_blocked_buy_overflow(sched, datetime(2026, 11, 24, 16, 10, tzinfo=ET))
    StockAgentScheduler._flush_blocked_buy_overflow(sched, datetime(2026, 11, 25, 9, 35, tzinfo=ET))
    assert len(svc.outbox) == 11
