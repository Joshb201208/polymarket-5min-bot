"""Scheduler orchestration tests; all brokers, research and time are offline."""

import asyncio
import shutil
import signal
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_agent import __main__ as entrypoint
from stock_agent import scheduler as module
from stock_agent.asset_registry import SymbolRenameEvent
from stock_agent.broker import OrderRequest
from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, CashFlow, Holding, IntentStatus, JobStatus, NewIntent
from stock_agent.market_calendar import ET, GateDecision, MarketSource, MarketState, MarketStatus, Session
from stock_agent.news_service import NewsService
from stock_agent.research_pipeline import JobOutcome, OptionIdea
from stock_agent.research_schemas import Direction
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState, TradeIntent, TradeKind, update_drawdown
from stock_agent.state_store import StateStore
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
        denied = await sched.market_gate("entry")
        assert not denied.allowed and "first_run_normalise" in denied.reason and "broker_reconcile" in denied.reason
        locks = {lock["code"]: lock for lock in sched.get_entry_locks()}
        assert "corrective orders" in locks["first_run_normalise"]["clears_when"]
        assert "fresh read" in locks["broker_reconcile"]["clears_when"]
        assert (await sched.market_gate("exit")).allowed
        assert (await sched.market_gate("exit", rule="cover_short")).allowed
        assert (await sched.market_gate("protection")).allowed
        sched._normalised = sched._reconciled = True
        assert (await sched.market_gate("entry")).allowed
        sched.pause_file.touch()
        assert not (await sched.market_gate("entry")).allowed
        assert "pause-trading file" in next(
            lock["clears_when"] for lock in sched.get_entry_locks() if lock["code"] == "operator_pause"
        )
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


def test_entry_lock_alert_names_required_health_and_cleanup(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 10, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        reported = []
        sched.alerts = SimpleNamespace(alert_entries_locked=lambda locks, *, session_date: reported.append(
            (locks, session_date),
        ))
        sched.llm = SimpleNamespace(is_healthy=lambda: True)
        try:
            sched.health.record_news_poll(("AAPL",), now, source="secondary", degraded=True)
            await sched._alert_entry_locks()
            locks, day = reported[-1]
            assert day == now.date()
            assert {lock["code"] for lock in locks} == {"first_run_normalise", "broker_reconcile"}

            sched._normalised = sched._reconciled = True
            await sched._alert_entry_locks()
            assert len(reported) == 1

            sched.health.record_news_poll(("AAPL",), now, source="failed", ok=False)
            sched.llm.is_healthy = lambda: False
            await sched._alert_entry_locks()
            locks = {lock["code"]: lock for lock in reported[-1][0]}
            assert set(locks) == {"news_health", "llm_health"}
            assert "successful fresh news poll" in locks["news_health"]["clears_when"]
            assert "model route" in locks["llm_health"]["clears_when"]
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_monday_evening_catchup_waits_for_tuesday_then_retries_at_open(tmp_path, monkeypatch):
    async def check():
        monday = datetime(2026, 11, 23, 19, tzinfo=ET)
        tuesday = monday + timedelta(days=1)
        current = monday
        events = []
        fresh = False

        async def research_run(kind, *, work_scope):
            nonlocal fresh
            events.append((kind, work_scope, current))
            fresh = work_scope == "catchup-regular"
            intents = (NewIntent(
                symbol="AAPL", side="buy", dedupe_key="catchup:AAPL", origin="scheduled", session_date=current.date(),
                next_attempt_at=current + timedelta(minutes=5),
            ),) if fresh else ()
            return JobOutcome(JobStatus.DONE, intents=intents)

        async def check_regime():
            events.append("regime")

        sched, _ = build(tmp_path, monkeypatch, monday, research=SimpleNamespace(
            has_fresh_thesis_set=lambda now: fresh, run=research_run,
        ))
        sched.store.open()
        monkeypatch.setattr(module, "_now_et", lambda: current)
        monkeypatch.setattr(module, "_now_utc", lambda: current)
        monkeypatch.setattr(sched, "_check_for_market_event", check_regime)
        sched._reconciled = sched._normalised = True

        try:
            sched._schedule_research_catchup(current)
            assert not sched._job_tasks
            session = Session(tuesday.date(), tuesday.replace(hour=9, minute=30), tuesday.replace(hour=16))
            sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, session)
            current = tuesday.replace(hour=6, minute=29)
            sched._schedule_research_catchup(current)
            assert not sched._job_tasks

            current += timedelta(minutes=1)
            sched._schedule_research_catchup(current)
            await sched._job_tasks["catchup"]
            await sched._collect_finished_jobs()
            assert events == [("weekly", "catchup-preopen", tuesday.replace(hour=6, minute=30))]
            sched._schedule_research_catchup(current)
            assert not sched._job_tasks  # persisted preopen job cannot replay

            current = session.opens_at
            sched.calendar.state = MarketState(MarketStatus.OPEN, MarketSource.LIVE, session)
            sched._schedule_research_catchup(current)
            assert not sched._should_run_options_scan(current + timedelta(minutes=15))
            await sched._job_tasks["catchup"]
            await sched._collect_finished_jobs()
            assert events[1:] == ["regime", ("weekly", "catchup-regular", current)]
            assert sched.ledger.job_status("catchup", f"{tuesday.date()}:regular").status is JobStatus.DONE
            assert not sched.ledger.due_intents(current + timedelta(minutes=4, seconds=59))
            purchase, = sched.ledger.due_intents(current + timedelta(minutes=5))
            assert purchase.symbol == "AAPL" and purchase.side == "buy" and purchase.job == "catchup"
            sched._schedule_research_catchup(current + timedelta(minutes=15))
            assert not sched._job_tasks
            assert sched._should_run_options_scan(current + timedelta(minutes=15))
        finally:
            await sched._cleanup()

    asyncio.run(check())


@pytest.mark.parametrize("at_close", [False, True])
def test_margin_trim_persists_and_continues_to_target(tmp_path, monkeypatch, at_close):
    async def check():
        now = datetime(2026, 11, 30, 16 if at_close else 10, tzinfo=ET)
        holding = Holding("AAPL", Decimal(50), Decimal(50), Decimal(110), Decimal(100), True,
                          Decimal(5000), Decimal(100))
        snap = BookSnapshot(now, "margin-book", Decimal(1000), Decimal(-4000), Decimal(5000), Decimal(0),
                            Decimal(0), None, None, (holding,), (), (), (), quality="indicative")
        excess = Decimal(".14")
        enabled = True
        plans = []
        alerts = []

        def status(snapshot):
            if enabled:
                return SimpleNamespace(needs_trim=excess < Decimal(".15"), margin_call=False, excess_pct=excess)
            return None

        def plan(snapshot, *, controls, force):
            plans.append((snapshot.as_of, force))
            return [TradeIntent(TradeKind.SELL, "AAPL", Decimal(5), Decimal(100), reason="margin_buffer")]

        def open_scheduler(t):
            scheduler, _ = build(tmp_path, monkeypatch, t)
            scheduler.store.open()
            monkeypatch.setattr(scheduler.config, "MARGIN_MIN_EXCESS_PCT", .25, raising=False)
            monkeypatch.setattr(scheduler.risk, "margin_status", status, raising=False)
            monkeypatch.setattr(scheduler.risk, "plan_margin_trim", plan, raising=False)
            scheduler.alerts = SimpleNamespace(alert=lambda severity, title, body, **kw: alerts.append(
                (severity, title, body),
            ))
            tomorrow = now + timedelta(days=1) if at_close else now
            session = Session(tomorrow.date(), tomorrow.replace(hour=9, minute=30), tomorrow.replace(hour=16))
            scheduler.calendar.state = MarketState(
                MarketStatus.CLOSED if at_close else MarketStatus.OPEN, MarketSource.LIVE, session,
            )
            return scheduler, session

        sched, session = open_scheduler(now)
        try:
            enabled = False
            await sched._evaluate_margin_guard(snap, now, at_close=at_close)
            assert not sched.ledger.list_intents() and not alerts
            enabled = True
            sched.pause_file.touch()
            await sched._evaluate_margin_guard(snap, now, at_close=at_close)
            first, = sched.ledger.list_intents()
            assert first.status is IntentStatus.QUEUED and first.origin == "margin_buffer"
            assert first.meta["qty"] == "5" and first.meta["rule"] == "margin_buffer"
            assert first.next_attempt_at == (session.opens_at + timedelta(minutes=5) if at_close else now)
            assert alerts[0][0] == "critical" and "14.0%" in alerts[0][2]
        finally:
            await sched._cleanup()

        tick = now + timedelta(seconds=30)
        sched, _ = open_scheduler(tick)
        try:
            await sched._evaluate_margin_guard(snap, tick, at_close=at_close)
            assert [row.intent_id for row in sched.ledger.list_intents()] == [first.intent_id]
            assert plans == [(now, True)]
            if at_close:
                return
            sched.ledger.claim_intent(first.intent_id, check_thesis=False)
            sched.ledger.ack_intent(first.intent_id, IntentStatus.FILLED, broker_order_id="first-fill")
            excess = Decimal(".20")
            await sched._evaluate_margin_guard(snap, tick)
            assert len(plans) == 1  # no second leg planned off a pre-fill book
            tick += timedelta(seconds=1)
            monkeypatch.setattr(module, "_now_utc", lambda: tick)
            reduced = replace(holding, qty=Decimal(45))
            fresh = replace(snap, as_of=tick, holdings=(reduced,))
            await sched._evaluate_margin_guard(fresh, tick)
            second, = [row for row in sched.ledger.list_intents() if not row.terminal]
            assert second.meta["margin_episode"] == first.meta["margin_episode"]
            assert plans[-1] == (tick, True)
            excess = Decimal(".25")
            await sched._evaluate_margin_guard(fresh, tick)
            assert sched.ledger.risk_state_get("a13.margin_trim_episode") == {}
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_tick_checks_fresh_margin_at_close_before_daily_report(tmp_path, monkeypatch):
    async def check():
        close = datetime(2026, 11, 30, 16, tzinfo=ET)
        now = close + timedelta(seconds=30)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        sched._engine_resumed = sched._reconciled = True
        sched._last_reconcile_at = now
        session = Session(now.date(), close.replace(hour=9, minute=30), close)
        sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, session)
        snapshot = SimpleNamespace(as_of=now)
        calls = []

        async def evaluate(snap, when, *, at_close=False):
            calls.append((snap, when, at_close))

        async def drive(now):
            return None

        monkeypatch.setattr(sched.ledger, "last_snapshot", lambda: snapshot)
        monkeypatch.setattr(sched, "_evaluate_margin_guard", evaluate)
        monkeypatch.setattr(sched, "_drive_order_lane", drive)
        try:
            await sched._tick(now)
            assert calls == [(snapshot, now, True)]
            assert "drawdown_close" not in sched._job_tasks
        finally:
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
        for symbol, quantity, cost in (("AAA", 10, 100), ("BBB", 20, 50), ("HHH", 5, 200)):
            world.price(symbol, cost)
            world.alpaca.seed_position(symbol, quantity, cost)
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


def test_startup_binds_unacknowledged_fill_before_reconciling(world):
    async def check():
        world.clock.set_et(2026, 11, 23, 10, 20)
        world.price("ALFA", 100)
        first = world.scheduler()
        first.store.open()
        intent = enqueue(first.engine, "ALFA", "buy", ref=100)
        claimed = first.ledger.claim_intent(intent.intent_id, check_thesis=False)
        order = await first.broker.submit_order(OrderRequest(
            symbol="ALFA", side="buy", type="limit", time_in_force="day", qty=5, limit_price=101,
            client_order_id=claimed.client_order_id, position_intent="buy_to_open",
        ))
        await first._cleanup()  # process stopped before it linked the broker reply

        restarted = world.scheduler()
        try:
            await restarted.startup()
            holding = restarted.ledger.snapshot().holding("ALFA")
            assert holding.meta.opened_source == "bot"
            assert holding.meta.entry_intent_id == intent.intent_id
            persisted = restarted.ledger.get_intent(intent.intent_id)
            assert persisted.broker_order_id == order.id and persisted.status.value == "filled"
            assert not any("imported ALFA" in str(row) for row in restarted.ledger.journal.read(kinds={"alert"}))
        finally:
            await restarted._cleanup()

    asyncio.run(check())


def test_scheduler_owns_one_broker_book_risk_gate_and_capital_lock(world, monkeypatch):
    async def check():
        def unexpected_read(*args, **kwargs):
            pytest.fail("a production service tried to read the unopened state store")

        with monkeypatch.context() as patch:
            patch.setattr(StateStore, "kv_get", unexpected_read)
            sched = world.scheduler()
        assert sched.ledger.broker is sched.calendar.broker is sched.broker
        assert sched.engine.broker is sched.options.broker is sched.broker
        assert sched.engine.ledger is sched.ledger
        assert sched.engine.risk is sched.options.risk is sched.risk
        assert sched.engine.prices is sched.prices
        assert sched.engine.options is sched.options
        assert sched.engine._capital_lock is sched.options._capital_lock is sched.capital_lock
        assert sched.engine._research_gate is sched.options.gate is sched.research_gate
        assert sched.options_store.store is sched.ledger.store is sched.research_store.store
        assert isinstance(sched.news, NewsService)
        assert sched.news.http is sched.http
        assert sched.news.assets is sched.assets
        assert sched.news.store is sched.health.store is sched.macro.store is sched.store
        assert sched.news.health is sched.llm.health is sched.research_sources.health is sched.health
        assert sched.news is sched.research_sources.news_service
        assert sched.prices is sched.macro.prices is sched.research_sources.prices
        assert sched.research.sources is sched.research_sources
        assert sched.research.gate is sched.research_gate
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


def test_prescan_does_not_block_morning_brief_or_reconcile(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 8, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now, engine=EngineStub([]), research=object())
        sched.store.open()
        sched._normalised = sched._reconciled = sched._engine_resumed = True
        sched._last_reconcile_at = now
        started, release, brief_ran = asyncio.Event(), asyncio.Event(), asyncio.Event()
        reconcile = []
        timeouts = {}
        schedule = sched._schedule_job

        def record(name, key, callback, *, timeout, **kwargs):
            timeouts[name] = timeout
            return schedule(name, key, callback, timeout=timeout, **kwargs)

        async def prescan():
            started.set()
            await release.wait()

        async def brief():
            brief_ran.set()

        async def reconcile_tick(reason):
            reconcile.append(reason)

        monkeypatch.setattr(sched, "_schedule_job", record)
        monkeypatch.setattr(sched, "_run_daily_prescan", prescan)
        monkeypatch.setattr(sched, "_send_morning_brief", brief)
        monkeypatch.setattr(sched, "_reconcile_scheduler", reconcile_tick)
        monkeypatch.setattr(sched, "_should_run_weekly", lambda _: True)
        monkeypatch.setattr(sched, "_should_run_earnings_check", lambda _: False)
        monkeypatch.setattr(sched, "_should_run_macro_check", lambda _: False)
        monkeypatch.setattr(sched, "_should_refresh_assets", lambda _: False)
        try:
            await sched._tick(now)
            await asyncio.wait_for(started.wait(), 1)
            assert timeouts == {"prescan": 2100}
            later = now + timedelta(minutes=15)
            monkeypatch.setattr(module, "_now_et", lambda: later)
            monkeypatch.setattr(module, "_now_utc", lambda: later)
            await sched._tick(later)
            await asyncio.wait_for(brief_ran.wait(), 1)
            assert not sched._job_tasks["prescan"].done()
            assert reconcile == ["scheduler tick"]
            assert timeouts["morning_brief"] == 90 and "weekly" not in timeouts
        finally:
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
        assert events[:3] == ["resume", "startup", "READY=1"]
        sched.shutdown()
        await asyncio.wait_for(task, timeout=2)
        assert events[-2:] == ["STOPPING=1", "drain"]

    asyncio.run(check())


def test_half_day_summary_uses_calendar_close(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 13, 4, tzinfo=ET)
    sched, _ = build(tmp_path, monkeypatch, now)
    assert not sched._should_send_daily_summary(now)
    assert sched._should_send_daily_summary(now + timedelta(minutes=1))


def test_close_performance_and_drawdown_share_trusted_mark_persisted_once(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 13, 5, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        old = DrawdownState(high_water=Decimal(1000), last_session="2026-11-25")
        sched.ledger.risk_state_set(DRAWDOWN_STATE_KEY, old.to_record())
        snapshot = BookSnapshot(
            as_of=now, run_id="close-fixture", equity=Decimal(790), cash=Decimal(790),
            long_mv=Decimal(0), short_mv=Decimal(0), options_mv=Decimal(0),
            last_equity=Decimal(1), buying_power=Decimal(790),
            holdings=(), legs=(), open_orders=(), diffs=(), quality="indicative",
        )

        async def refresh(reason):
            return snapshot

        monkeypatch.setattr(sched.ledger, "refresh", refresh)
        assert (await sched._close_drawdown()).detail == {"reason": "close_clock_unverified"}
        sess = sched.calendar.state.session
        sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, sess)
        assert (await sched._close_drawdown()).detail["reason"] == "close_snapshot_indicative"
        assert sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY) == old.to_record()
        assert not sched.ledger.equity_marks("close")
        snapshot = replace(snapshot, quality="live")
        await sched._close_drawdown()
        state = DrawdownState.from_record(sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY))
        assert state.last_session == "2026-11-27"
        assert state.paused and state.drawdown == Decimal("0.21")
        assert sched.ledger.equity_marks("close")[0].equity == Decimal(790)
        snapshot = replace(snapshot, equity=Decimal(1000))
        await sched._close_drawdown()
        assert sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY) == state.to_record()
        assert sched.ledger.equity_marks("close")[0].equity == Decimal(790)
        await sched._cleanup()

    asyncio.run(check())


def test_drawdown_timeout_close_alert_reports_remaining_throttle_and_does_not_repeat(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 13, 5, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        monkeypatch.setattr(sched.config, "DRAWDOWN_PAUSE_MAX_SESSIONS", 5)
        state = update_drawdown(DrawdownState(), "2026-11-18", Decimal(1000), config=sched.config)
        for day in ("19", "20", "23", "24", "25"):
            state = update_drawdown(state, f"2026-11-{day}", Decimal(790), config=sched.config)
        sched.ledger.risk_state_set(DRAWDOWN_STATE_KEY, state.to_record())
        assert "1 more trusted session close is recorded" in sched._entry_safety_locks()["drawdown_pause"][1]
        sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, sched.calendar.state.session)
        snapshot = BookSnapshot(
            as_of=now, run_id="timeout-close-fixture", equity=Decimal(790), cash=Decimal(790),
            long_mv=Decimal(0), short_mv=Decimal(0), options_mv=Decimal(0),
            last_equity=Decimal(790), buying_power=Decimal(790),
            holdings=(), legs=(), open_orders=(), diffs=(), quality="live",
        )

        async def refresh(reason):
            return snapshot

        sent = []

        async def alert(severity, title, body, *, key=None):
            sent.append((severity, body, key))

        monkeypatch.setattr(sched.ledger, "refresh", refresh)
        monkeypatch.setattr(sched, "_alert", alert)
        try:
            await sched._close_drawdown()
            assert "drawdown_pause" not in sched._entry_safety_locks()
            assert sched._risk_controls().drawdown.is_throttled(sched.config)
            assert len(sent) == 1
            severity, body, key = sent[0]
            assert severity == "warning" and key == "drawdown:2026-11-27"
            assert "Rolling decision drawdown: 21.0%; all-time drawdown: 21.0%" in body
            assert "pause lifted because its session cap was reached" in body
            assert "half-size stock entries when allowed, no new borrowing or options" in body
            await sched._close_drawdown()
            assert len(sent) == 1
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_close_drawdown_adjusts_only_external_flows_since_last_close(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 25, 13, 5, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        flows = [
            CashFlow("prior-deposit", now.date(), Decimal(1000), "CSD"),
            CashFlow("withdrawal", now.date() + timedelta(days=1), Decimal(-320), "CSW"),
            CashFlow("deposit", now.date() + timedelta(days=2), Decimal(100), "CSD"),
            CashFlow("dividend", now.date() + timedelta(days=2), Decimal(12), "DIV"),
            CashFlow("future-deposit", now.date() + timedelta(days=3), Decimal(500), "CSD"),
        ]
        monkeypatch.setattr(sched.ledger, "cash_flows", lambda: flows)
        equity = Decimal(1000)

        async def refresh(reason):
            return BookSnapshot(
                as_of=now, run_id="flow-fixture", equity=equity, cash=equity,
                long_mv=Decimal(0), short_mv=Decimal(0), options_mv=Decimal(0),
                last_equity=Decimal(1), buying_power=equity,
                holdings=(), legs=(), open_orders=(), diffs=(), quality="live",
            )

        monkeypatch.setattr(sched.ledger, "refresh", refresh)
        try:
            first = sched.calendar.state.session
            sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, first)
            assert await sched._close_drawdown() is None
            initial = sched._drawdown_state()
            assert initial.high_water == Decimal(1000)

            now += timedelta(days=2)
            monkeypatch.setattr(module, "_now_et", lambda: now)
            monkeypatch.setattr(module, "_now_utc", lambda: now)
            next_session = Session(now.date(), first.opens_at + timedelta(days=2),
                                   first.closes_at + timedelta(days=2))
            sched.calendar.state = MarketState(MarketStatus.CLOSED, MarketSource.LIVE, next_session)
            equity = Decimal(730)  # $220 net withdrawn and $50 of investment loss.
            assert await sched._close_drawdown() is None
            final = sched._drawdown_state()
            assert final.high_water == Decimal(1000)
            assert final.drawdown == Decimal("0.05") and not final.paused
            assert sched.ledger.equity_marks("close")[-1].equity == Decimal(730)
            assert await sched._close_drawdown() is None
            assert sched._drawdown_state() == final
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_scheduler_two_trusted_closes_execute_one_next_session_trim(world):
    async def check():
        sched = module.StockAgentScheduler(services=SimpleNamespace(prices=FakePrices(world)))
        sched.store.open()
        sched.options.load_state()
        world.alpaca.cash = 80_000
        world.price("ALFA", 100)
        world.alpaca.seed_position("ALFA", 200, 100)

        async def close(day):
            world.clock.set_et(2026, 11, day, 16, 5)
            key = world.clock.now_et().date().isoformat()
            sched._schedule_job("drawdown_close", key, sched._close_drawdown, timeout=5)
            if task := sched._job_tasks.get("drawdown_close"):
                await task
                await sched._collect_finished_jobs()
            assert sched.ledger.job_status("drawdown_close", key).status is JobStatus.DONE

        try:
            await close(23)
            await close(23)
            assert sched.ledger.risk_state_get("a6.close_drift")["counts"] == [["issuer_cap", "ALFA", 1]]
            assert not sched.ledger.list_intents()
            await close(24)
            (trim,) = sched.ledger.list_intents()
            assert trim.kind == "trim" and trim.origin == "limit_drift" and trim.meta["qty"] == "70"
            assert not await sched.engine.execute_due()
            world.clock.set_et(2026, 11, 25, 9, 34)
            assert not await sched.engine.execute_due()
            world.clock.advance(minutes=1)
            (result,) = await sched.engine.execute_due()
            assert result.filled_qty == 70
            assert world.alpaca.position("ALFA").qty == 130
            (stop,) = [order for order in world.alpaca.open_orders("ALFA") if order.type == "stop"]
            assert stop.qty == 130 and stop.time_in_force == "gtc"
            assert not await sched.engine.execute_due()
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_scheduler_option_delivery_trims_next_session_without_waiting_two_closes(world):
    async def check():
        sched = module.StockAgentScheduler(services=SimpleNamespace(prices=FakePrices(world)))
        sched.store.open()
        sched.options.load_state()
        world.clock.set_et(2026, 11, 23, 16, 20)
        world.alpaca.cash = 90_000
        world.price("ALFA", 100)
        world.alpaca.seed_position("ALFA", 100, 100)
        expiry = world.clock.now_et().date() + timedelta(days=18)
        contract = world.alpaca.add_option_contract("ALFA", expiry, "put", 100)
        world.alpaca.seed_position(contract.symbol, -1, 2)
        world.alpaca.assign_early(contract.symbol)
        try:
            await sched.calendar.refresh()
            await sched.ledger.refresh("after option delivery")
            await sched._queue_delivery_trims(world.clock.now_et())
            assert not sched.ledger.list_intents()
            world.clock.set_et(2026, 11, 24, 9, 35)
            await sched.calendar.refresh()
            await sched._queue_delivery_trims(world.clock.now_et())
            await sched._queue_delivery_trims(world.clock.now_et())
            (trim,) = sched.ledger.list_intents()
            assert trim.kind == "trim" and trim.origin == "option_delivery"
            assert trim.meta["qty"] == "50"
            (result,) = await sched.engine.execute_due()
            assert result.filled_qty == 50
            assert world.alpaca.position("ALFA").qty == 150
            assert sched.ledger.risk_state_get("a6.close_drift") is None
            assert not await sched.engine.execute_due()
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_scheduler_asset_refresh_tracks_focus_and_drops_dead_poll_symbols(world, monkeypatch):
    async def check():
        sched = world.scheduler()
        sched.store.open()
        monkeypatch.setattr(module, "focus_symbols", lambda **kw: ("DEAD", "FORMER", "ALFA"))
        world.price("ALFA", 100)
        world.price("CURRENT", 100)
        try:
            await sched._refresh_assets()
            assert "ALFA" in sched._option_universe()
            assert "DEAD" not in sched._option_universe()
            assert "FORMER" not in sched._option_universe()
            assert "CURRENT" in sched._option_universe()
            day = world.clock.now_et().date()
            await sched._on_asset_rename(SymbolRenameEvent("FORMER", "CURRENT", None, day))
            alias = sched.store.kv_get("symbol_aliases")["FORMER"]
            assert alias["new"] == "CURRENT" and alias["effective"] == day.isoformat()
        finally:
            await sched._cleanup()

    asyncio.run(check())


def test_live_option_story_replayed_until_manager_confirms_durable_handling(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 12, 40, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        sched._normalised = sched._reconciled = True
        idea = OptionIdea("AAA", Direction.BEARISH, "story-1", "thesis-1", 8, 9, now, ("evidence",))
        sched.research_store.record_option_idea(idea.story_id, idea, now + timedelta(minutes=10))
        calls = []
        handled = set()

        async def monitor(now):
            calls.append("monitor")

        async def scan(now, *, candidates):
            calls.append(candidates)

        sched.options = SimpleNamespace(monitor=monitor, scan=scan,
                                        handled_story_ids=lambda ids: handled.intersection(ids))
        await sched._run_options_lane(now)
        assert sched.research_store.pending_option_ideas(now)
        assert calls[0] == "monitor"
        assert calls[1][0].direction_hint == "BEARISH"
        handled.add(idea.story_id)
        await sched._run_options_lane(now)
        assert sched.research_store.pending_option_ideas(now) == []
        await sched._run_options_lane(now)
        assert len(calls) == 5  # monitor runs even when candidate already acknowledged
        await sched._cleanup()

    asyncio.run(check())
