"""Scheduler orchestration tests; all brokers, research and time are offline."""

import asyncio
import shutil
import signal
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_agent import __main__ as entrypoint
from stock_agent import scheduler as module
from stock_agent.config import Config
from stock_agent.ledger import JobStatus, NewIntent
from stock_agent.market_calendar import ET, GateDecision, MarketSource, MarketState, MarketStatus, Session
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState
from tests.exec_helpers import FakePrices, enqueue


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

    def previous_session(self, day):
        session = self.state.session
        if session:
            return Session(day - timedelta(days=2), session.opens_at - timedelta(days=2),
                           session.closes_at - timedelta(days=2))
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
        sched.store.open()
        assert not (await sched.market_gate("entry")).allowed
        assert (await sched.market_gate("exit")).allowed
        assert (await sched.market_gate("exit", rule="cover_short")).allowed
        assert (await sched.market_gate("protection")).allowed
        sched._normalised = sched._reconciled = True
        assert (await sched.market_gate("entry")).allowed
        sched.pause_file.touch()
        assert not (await sched.market_gate("entry")).allowed
        assert (await sched.market_gate("exit")).allowed
        sched.pause_file.unlink()
        monkeypatch.setattr(sched.config, "STOCK_TRADING_PAUSED", True, raising=False)
        assert not (await sched.market_gate("entry")).allowed
        assert (await sched.market_gate("exit", rule="hard_stop")).allowed
        sched.shutdown()
        assert not (await sched.market_gate("exit")).allowed
        assert (await sched.market_gate("protection")).allowed
        await sched._cleanup()

    asyncio.run(check())


def test_entrypoint_flock_excludes_second_scheduler_and_sigterm_stops_first(tmp_path, monkeypatch):
    async def check():
        started = asyncio.Event()
        stopped = asyncio.Event()
        instances = []

        class Agent:
            def __init__(self, config):
                instances.append(self)

            async def run(self):
                started.set()
                await stopped.wait()

            def shutdown(self):
                stopped.set()

        monkeypatch.setattr(entrypoint, "StockAgentScheduler", Agent)
        config = SimpleNamespace(DATA_DIR=tmp_path)
        first = asyncio.create_task(entrypoint._serve(config))
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(SystemExit, match="state directory lock"):
            await entrypoint._serve(config)
        assert len(instances) == 1
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(first, 1)
        assert stopped.is_set()
        await entrypoint._serve(config)
        assert len(instances) == 2

    asyncio.run(check())


def test_sigterm_drain_retains_stop_for_inflight_real_buy(world, monkeypatch):
    async def check():
        world.clock.set_et(2026, 11, 23, 11, 0)
        world.price("AAA", 50, spread=0.02)
        watchdog = asyncio.Event()
        submitted, release = asyncio.Event(), asyncio.Event()
        sched = module.StockAgentScheduler(services=SimpleNamespace(prices=FakePrices(world)))
        wait_terminal = sched.broker.wait_terminal

        async def delayed_confirmation(*args, **kwargs):
            submitted.set()
            await release.wait()
            return await wait_terminal(*args, **kwargs)

        def sd_notify(message):
            if message == "WATCHDOG=1":
                watchdog.set()

        monkeypatch.setattr(module, "notify_systemd", sd_notify)
        monkeypatch.setattr(sched.broker, "wait_terminal", delayed_confirmation)
        await sched.startup()
        sched.store.kv_set(sched.engine.NORMALISE_KEY, {"state": "done"})
        sched._normalised = True
        intent = enqueue(sched.engine, "AAA", "buy", ref=50)
        task = asyncio.create_task(sched.run())
        await asyncio.wait_for(submitted.wait(), 2)
        await asyncio.wait_for(watchdog.wait(), 2)
        sched.shutdown()
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 3)
        buys = [order for order in world.alpaca.orders.values() if order.side == "buy"]
        stops = [order for order in world.alpaca.open_orders("AAA") if order.type == "stop"]
        assert len(buys) == len(stops) == 1
        assert buys[0].client_order_id == intent.client_order_id
        assert stops[0].qty == world.alpaca.position("AAA").qty
        assert stops[0].time_in_force == "gtc"

    asyncio.run(check())


def test_unit_uses_existing_root_path_watchdog_and_bounded_stop():
    unit = (Path(__file__).resolve().parents[1] / "deploy/stock-agent.service").read_text()
    assert "User=root" in unit
    assert "WorkingDirectory=/root/polymarket-bot" in unit
    assert "EnvironmentFile=/root/polymarket-bot/.env" in unit
    assert "ExecStart=/root/polymarket-bot/venv/bin/python -m stock_agent" in unit
    assert "ExecStartPre" not in unit
    assert "Type=notify" in unit and "WatchdogSec=180" in unit
    stop = next(line for line in unit.splitlines() if line.startswith("TimeoutStopSec="))
    assert int(stop.split("=", 1)[1]) > module.SHUTDOWN_DRAIN_SECONDS + 10


def test_closed_first_start_imports_legacy_files_without_order_or_unlock(world):
    async def check():
        legacy = Path(__file__).parent / "fixtures/state_v0/portfolio.json"
        dest = world.data_dir / "portfolio.json"
        shutil.copyfile(legacy, dest)
        world.clock.set_et(2026, 11, 26, 11, 0)  # Thanksgiving
        sched = world.scheduler()
        await sched.startup()
        assert sched._state_ready and sched._reconciled
        assert sched.calendar.state.status is MarketStatus.CLOSED
        assert not sched.engine.normalised and not (await sched.market_gate("entry")).allowed
        assert sched.store.get_meta("migrated_v0_at")
        assert (world.data_dir / "legacy_v0/portfolio.json").read_bytes() == legacy.read_bytes()
        assert dest.read_bytes() == legacy.read_bytes()
        world.assert_no_orders()
        await sched._cleanup()

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


def test_drawdown_uses_only_trusted_close_and_persists_once(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 13, 5, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        old = DrawdownState(high_water=Decimal(1000), last_session="2026-11-25")
        sched.ledger.risk_state_set(DRAWDOWN_STATE_KEY, old.to_record())
        snapshot = SimpleNamespace(quality="indicative", equity=Decimal(790), as_of=now)

        async def refresh(reason):
            return snapshot

        monkeypatch.setattr(sched.ledger, "refresh", refresh)
        assert (await sched._close_drawdown()).detail == {"reason": "close_clock_unverified"}
        sess = sched.calendar.state.session
        sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, sess)
        assert (await sched._close_drawdown()).detail == {"reason": "close_equity_unverified"}
        assert sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY) == old.to_record()
        snapshot.quality = "live"
        await sched._close_drawdown()
        state = DrawdownState.from_record(sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY))
        assert state.last_session == "2026-11-27"
        assert state.paused and state.drawdown == Decimal("0.21")
        snapshot.equity = Decimal(1000)
        await sched._close_drawdown()
        assert sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY) == state.to_record()
        await sched._cleanup()

    asyncio.run(check())
