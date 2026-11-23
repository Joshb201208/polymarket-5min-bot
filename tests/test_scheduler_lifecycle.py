"""Scheduler orchestration tests; all brokers, research and time are offline."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from stock_agent import scheduler as module
from stock_agent.config import Config
from stock_agent.ledger import JobStatus, NewIntent
from stock_agent.market_calendar import ET, GateDecision, MarketSource, MarketState, MarketStatus, Session


class CalendarStub:
    def __init__(self, now, state):
        self.now = now
        self.state = state
        self.calls = []

    def session_on(self, day):
        session = self.state.session
        if session and session.date == day:
            return session
        return None

    def next_session(self, day):
        session = self.state.session
        if session and session.date > day:
            return session
        return None

    def is_open(self, now):
        session = self.session_on(now.date())
        return bool(session and session.opens_at <= now < session.closes_at)

    async def market_gate(self, purpose, *, rule=None):
        self.calls.append(purpose)
        return GateDecision(True, "fixture")

    async def refresh(self):
        return self.state


class EngineStub:
    normalised = True

    def __init__(self, calls):
        self.calls = calls

    async def resume(self):
        self.calls.append("resume")

    async def monitor_tick(self):
        self.calls.append("protection")

    async def drain(self, timeout):
        self.calls.append("drain")
        return True


def build(tmp_path, monkeypatch, time, *, engine=None, research=None, with_session=True):
    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "_now_et", lambda: time)
    monkeypatch.setattr(module, "_now_utc", lambda: time)
    session = Session(time.date(), time.replace(hour=9, minute=30), time.replace(hour=13, minute=0))
    market = MarketState(MarketStatus.OPEN, MarketSource.LIVE, session if with_session else None)
    calls = []
    calendar = CalendarStub(time, market)
    runtime = SimpleNamespace(publish=lambda *a, **k: calls.append(("runtime", k)))
    sched = module.StockAgentScheduler(services=SimpleNamespace(
        calendar=calendar, pause_file=tmp_path / "pause-trading", runtime=runtime,
        engine=engine, research=research,
    ))
    return sched, calls


def test_pause_and_normalise_only_block_entries(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 10, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        assert not (await sched.market_gate("entry")).allowed
        assert (await sched.market_gate("exit")).allowed
        sched._normalised = sched._reconciled = True
        assert (await sched.market_gate("entry")).allowed
        sched.pause_file.touch()
        assert not (await sched.market_gate("entry")).allowed
        assert (await sched.market_gate("exit")).allowed
        sched.shutdown()
        assert not (await sched.market_gate("exit")).allowed
        assert (await sched.market_gate("protection")).allowed

    asyncio.run(check())


def test_durable_job_completion_and_intent_are_not_repeated(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 10, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        calls = []

        async def research():
            calls.append("research")
            return SimpleNamespace(status=JobStatus.DONE, intents=(
                NewIntent("AAA", "buy", "nov-27:aaa", origin="news", session_date=now.date()),
            ))

        sched._schedule_job("live_news", "slot", research, timeout=1)
        await sched._job_tasks["live_news"]
        await sched._collect_finished_jobs()
        assert sched.ledger.job_status("live_news", "slot").status is JobStatus.DONE
        assert len(sched.ledger.due_intents(now)) == 1
        sched.store.close()

        restarted, _ = build(tmp_path, monkeypatch, now)
        restarted.store.open()
        restarted._schedule_job("live_news", "slot", research, timeout=1)
        assert restarted._job_tasks == {}
        assert calls == ["research"]
        assert len(restarted.ledger.due_intents(now)) == 1
        restarted.store.close()

    asyncio.run(check())


def test_protection_continues_while_live_news_waits(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 10, tzinfo=ET)
        events = []
        sched, _ = build(tmp_path, monkeypatch, now, engine=EngineStub(events), research=object())
        sched.store.open()
        sched._normalised = sched._reconciled = True
        sched._engine_resumed = True
        sched._last_reconcile_at = now
        news_started, release = asyncio.Event(), asyncio.Event()

        async def news():
            news_started.set()
            await release.wait()

        async def no_op():
            pass

        monkeypatch.setattr(sched, "_run_news_slot", news)
        monkeypatch.setattr(sched, "_check_for_market_event", no_op)
        monkeypatch.setattr(sched, "_send_morning_brief", no_op)
        await sched._tick(now)
        await news_started.wait()
        assert events == ["protection"]
        assert not sched._job_tasks["live_news"].done()
        later = now + timedelta(minutes=1)
        monkeypatch.setattr(module, "_now_et", lambda: later)
        monkeypatch.setattr(module, "_now_utc", lambda: later)
        await sched._tick(later)
        await asyncio.sleep(0)
        assert events == ["protection", "protection"]
        release.set()
        await sched._cleanup()

    asyncio.run(check())


def test_readiness_follows_reconcile_and_shutdown_wakes_idle_loop(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 22, tzinfo=ET)
        events = []
        ready = asyncio.Event()
        sched, _ = build(tmp_path, monkeypatch, now, engine=EngineStub(events), with_session=False)

        def sd_notify(payload):
            events.append(payload.splitlines()[0])
            if payload.startswith("READY"):
                ready.set()

        async def reconcile(reason, **kwargs):
            events.append(reason)

        async def tick(now):
            events.append("tick")

        monkeypatch.setattr(module, "notify_systemd", sd_notify)
        monkeypatch.setattr(sched.ledger, "refresh", reconcile)
        monkeypatch.setattr(sched, "_tick", tick)
        task = asyncio.create_task(sched.run())
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert events[:3] == ["startup", "resume", "READY=1"]
        sched.shutdown()
        await asyncio.wait_for(task, timeout=2)
        assert events[-2:] == ["STOPPING=1", "drain"]

    asyncio.run(check())


def test_half_day_summary_uses_calendar_close(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 13, 4, tzinfo=ET)
    sched, _ = build(tmp_path, monkeypatch, now)
    assert not sched._should_send_daily_summary(now)
    assert sched._should_send_daily_summary(now + timedelta(minutes=1))
