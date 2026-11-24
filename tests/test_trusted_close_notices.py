"""Offline terminal trusted-close notices and restart behavior."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from stock_agent.ledger import JobStatus
from stock_agent.market_calendar import ET
from stock_agent.research_pipeline import JobOutcome
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState, update_drawdown
from stock_agent.scheduler import MAX_JOB_ATTEMPTS
from tests.test_scheduler_lifecycle import build


def _prepare_last_attempt(scheduler, day):
    for _ in range(MAX_JOB_ATTEMPTS - 1):
        scheduler.ledger.start_job("drawdown_close", day)
        scheduler.ledger.finish_job("drawdown_close", day, JobStatus.FAILED)
    return scheduler.ledger.start_job("drawdown_close", day).attempts


def test_terminal_notice_uses_prior_trading_streak_and_replays_notification_only(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 13, 30, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        wednesday, friday = "2026-11-25", "2026-11-27"
        try:
            mapping = {date.fromisoformat(friday): SimpleNamespace(date=date.fromisoformat(wednesday))}
            monkeypatch.setattr(sched.calendar, "previous_session", mapping.get)
            _prepare_last_attempt(sched, wednesday)
            sched.ledger.finish_job(
                "drawdown_close", wednesday, JobStatus.FAILED,
                detail={"reason": "close_equity_unverified", "missed_close_streak": 5, "close_mark_present": False},
            )
            attempt = _prepare_last_attempt(sched, friday)

            async def unpriced():
                return JobOutcome(JobStatus.FAILED, detail={
                    "reason": "close_unpriced_stock", "symbols": ["EXAMPLE"],
                })

            # Simulate a process dying after the final job committed but before dispatch.
            async def drop_before_dispatch(window_key, detail):
                pass

            monkeypatch.setattr(sched, "_warn_missed_close", drop_before_dispatch)
            await sched._run_scheduled_job("drawdown_close", friday, unpriced, 1, attempt)
            row = sched.ledger.job_status("drawdown_close", friday)
            assert row.detail["missed_close_streak"] == 6
            assert row.detail["close_mark_present"] is False
        finally:
            await sched._cleanup()

        restarted, _ = build(tmp_path, monkeypatch, now)
        restarted.store.open()
        sent = []

        async def replay_close_would_be_a_bug():
            raise AssertionError("terminal close must not be run again")

        async def alert(severity, title, body, *, key=None):
            sent.append((severity, title, body, key))

        monkeypatch.setattr(restarted, "_alert", alert)
        try:
            restarted._schedule_job("drawdown_close", friday, replay_close_would_be_a_bug, timeout=1)
            await restarted._job_tasks["drawdown_close"]
            await restarted._collect_finished_jobs()
            restarted._schedule_job("drawdown_close", friday, replay_close_would_be_a_bug, timeout=1)
            assert len(sent) == 1
            severity, title, body, key = sent[0]
            assert (severity, title, key) == ("critical", "Trusted close not recorded", f"trusted-close-missed:{friday}")
            assert "No trusted close was recorded today" in body
            assert "fall-from-peak brakes did not update" in body
            assert "stock prices could not be verified (EXAMPLE)" in body
            assert "missed drawdown update: 6" in body
            assert restarted.ledger.job_status("drawdown_close", friday).attempts == MAX_JOB_ATTEMPTS
        finally:
            await restarted._cleanup()

    asyncio.run(check())


def test_failed_close_limits_does_not_send_missed_close_notice_if_drawdown_applied(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 13, 30, tzinfo=ET)
        day = now.date().isoformat()
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        applied = update_drawdown(DrawdownState(), day, Decimal(100), config=sched.config)
        sched.ledger.risk_state_set(DRAWDOWN_STATE_KEY, applied.to_record())
        attempt = _prepare_last_attempt(sched, day)
        alerts = []

        async def alert(severity, title, body, *, key=None):
            alerts.append((severity, title, key))

        async def risk_check_failed():
            return JobOutcome(JobStatus.FAILED, detail={"reason": "close_risk_input_unverified"})

        monkeypatch.setattr(sched, "_alert", alert)
        try:
            await sched._run_scheduled_job("drawdown_close", day, risk_check_failed, 1, attempt)
            outcome = sched.ledger.job_status("drawdown_close", day)
            assert outcome.detail == {"reason": "close_risk_input_unverified"}
            assert all(severity != "critical" for severity, _, _ in alerts)
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_terminal_notice_counts_sessions_with_no_job_since_last_applied_close(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 13, 30, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        tuesday, wednesday, friday = date(2026, 11, 24), date(2026, 11, 25), now.date()
        mapping = {friday: SimpleNamespace(date=wednesday), wednesday: SimpleNamespace(date=tuesday)}
        monkeypatch.setattr(sched.calendar, "previous_session", mapping.get)
        applied = update_drawdown(DrawdownState(), tuesday.isoformat(), Decimal(100), config=sched.config)
        sched.ledger.risk_state_set(DRAWDOWN_STATE_KEY, applied.to_record())
        try:
            assert sched.ledger.job_status("drawdown_close", wednesday.isoformat()) is None
            detail = sched._terminal_close_failure(friday.isoformat(), {"reason": "close_unpriced_stock"})
            assert detail["missed_close_streak"] == 2
            assert detail["missed_close_streak_lower_bound"] is False
        finally:
            await sched._cleanup()

    asyncio.run(check())
