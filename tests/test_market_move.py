import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from stock_agent import scheduler as scheduler_module
from stock_agent.ledger import JobStatus
from stock_agent.market_calendar import ET
from stock_agent.market_move import Reading, observe, valid_reading
from stock_agent.research_pipeline import JobOutcome
from tests.test_scheduler_lifecycle import EngineStub, build

OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)


def step(state, slot, *, spy=None, previous_spy=None, vix=None, previous_vix=None, regime=None):
    now = OPEN + timedelta(minutes=30 * slot)
    readings = {key: Reading(Decimal(str(value)), now) for key, value in (("spy", spy), ("vix", vix))
                if value is not None}
    closes = {key: Decimal(str(value)) for key, value in (("spy", previous_spy), ("vix", previous_vix))
              if value is not None}
    return observe(state, slot=slot, now=now, readings=readings, closes=closes, regime=regime)


def test_exact_thresholds_and_simultaneous_triggers_coalesce():
    state, hits = step({}, 0, spy=100, vix=20, regime="normal")
    assert not hits
    state, hits = step(state, 1, spy=101.5, vix=23, regime="cautious")
    assert [h["signal"] for h in hits] == ["spy_up_slot", "vix_up_slot", "regime_change"]
    assert len(state["claims"]) == 1


def test_prior_close_crosses_each_direction_once_even_without_previous_sample():
    state, hits = step({}, 0, spy=101.5, previous_spy=100)
    assert [h["signal"] for h in hits] == ["spy_up_close"]
    state, hits = step(state, 1, spy=101.5, previous_spy=100)
    assert not hits
    state, hits = step(state, 2, spy=98.5, previous_spy=100)
    assert {h["signal"] for h in hits} == {"spy_down_slot", "spy_down_close"}
    state, hits = step(state, 3, spy=98.5, previous_spy=100)
    assert not hits


def test_unavailable_vix_does_not_disable_spy_or_compare_across_missing_slot():
    state, _ = step({}, 0, vix=20, spy=100)
    state, hits = step(state, 1, spy=98.5)
    assert [h["signal"] for h in hits] == ["spy_down_slot"]
    state, hits = step(state, 2, vix=35)
    assert not hits
    assert len(state["claims"]) == 1


def test_macro_requires_adjacent_valid_sample_or_previous_sessions_final_window():
    state, _ = step({}, 0, regime="normal")
    state, _ = step(state, 1)
    state, hits = step(state, 2, regime="cautious")
    assert not hits
    state, hits = step(state, 3, regime="normal")
    assert [h["signal"] for h in hits] == ["regime_change"]
    state, hits = step({"regime_anchor": "normal"}, 0, regime="cautious")
    assert [h["signal"] for h in hits] == ["regime_change"]
    state, hits = step(state, 1, regime="cautious")
    assert not hits


def test_cooldown_daily_cap_and_repeated_window():
    state, hits = step({}, 0, spy=102, previous_spy=100)
    assert hits
    state, hits = observe(
        state, slot=1, now=OPEN + timedelta(minutes=29), readings={
            "spy": Reading(Decimal(100), OPEN + timedelta(minutes=29)),
        }, closes={}, regime=None,
    )
    assert not hits
    state, hits = step(state, 1, spy=200, previous_spy=100)
    assert not hits  # Never reprocess an already observed window.
    state, hits = step(state, 2, spy=102)
    assert hits
    state, hits = step(state, 3, spy=100)
    assert hits
    state, hits = step(state, 4, spy=102)
    assert not hits
    assert len(state["claims"]) == 3


def test_invalid_quotes_never_enter_the_detector():
    assert valid_reading("100", OPEN, now=OPEN, opens_at=OPEN) == Reading(Decimal(100), OPEN)
    for value in ("0", "-1", "NaN", "Infinity", None):
        assert valid_reading(value, OPEN, now=OPEN, opens_at=OPEN) is None
    for stamp in (None, OPEN - timedelta(seconds=91), OPEN + timedelta(seconds=1), OPEN.replace(tzinfo=None)):
        assert valid_reading(100, stamp, now=OPEN, opens_at=OPEN - timedelta(minutes=3)) is None
    assert valid_reading(100, OPEN - timedelta(seconds=1), now=OPEN, opens_at=OPEN) is None


def test_rescan_claim_precedes_work_protection_continues_and_restart_does_not_repeat(tmp_path, monkeypatch):
    async def check():
        now = OPEN.astimezone(ET) + timedelta(minutes=1)
        entered = asyncio.Event()
        orders, scans = [], []

        async def run(kind, *, work_scope):
            scans.append((kind, work_scope))
            entered.set()
            await asyncio.Future()

        async def macro_check():
            return SimpleNamespace(regime="normal", degraded=False, timestamp=now.isoformat())

        async def get_quotes(symbols, *, purpose, max_age_s):
            assert (symbols, purpose, max_age_s) == (["SPY"], "brief", 90)
            return SimpleNamespace(quotes={"SPY": SimpleNamespace(price=Decimal(102), as_of=now,
                                                                  is_market_hours=True)})

        async def get_daily_bars(symbols, *, start_date, end_date):
            assert symbols == ["SPY"] and start_date == end_date
            return {"SPY": [SimpleNamespace(session_date=end_date, close=Decimal(100))]}

        def make_scheduler():
            sched, _ = build(tmp_path, monkeypatch, now, engine=EngineStub(orders),
                             research=SimpleNamespace(run=run))
            monkeypatch.setattr(scheduler_module, "_now_et", lambda: now)
            monkeypatch.setattr(scheduler_module, "_now_utc", lambda: now.astimezone(UTC))
            sched.macro = SimpleNamespace(check_conditions=macro_check, get_last_snapshot=lambda: None)
            sched.prices = SimpleNamespace(get_quotes=get_quotes, get_daily_bars=get_daily_bars)
            sched.store.open()
            return sched

        sched = make_scheduler()
        key = f"{now.date()}:0"
        sample_job = "market_move_sample"
        rescan_job = "market_move_rescan"
        try:
            sched._schedule_market_move_sample(now)
            await sched._job_tasks[sample_job]
            await asyncio.wait_for(entered.wait(), 1)
            assert scans == [("market_move", f"market_move:{key}")]
            assert sched.ledger.job_status(rescan_job, key).status is JobStatus.STARTED
            assert len(sched.store.kv_get(scheduler_module.MARKET_MOVE_STATE_KEY)["claims"]) == 1
            sched._last_reconcile_at = now
            sched._engine_resumed = True
            await sched._drive_order_lane(now)
            await sched._order_task
            assert orders.count("protection") == 1
            now += timedelta(seconds=30)
            await sched._drive_order_lane(now)
            await sched._order_task
            assert orders.count("protection") == 2
            assert not sched._job_tasks[rescan_job].done()
        finally:
            await sched._cleanup()

        restarted = make_scheduler()
        try:
            assert restarted.ledger.job_status(rescan_job, key).status is JobStatus.STARTED
            restarted._schedule_market_move_sample(now)
            assert restarted._job_tasks == {}
            now += timedelta(minutes=30)
            restarted._schedule_market_move_sample(now)
            await restarted._job_tasks[sample_job]
            assert rescan_job not in restarted._job_tasks
            assert len(restarted.store.kv_get(scheduler_module.MARKET_MOVE_STATE_KEY)["claims"]) == 1
            assert len(scans) == 1
        finally:
            await restarted._cleanup()

    asyncio.run(check())


def test_rescan_does_not_consume_research_marker_and_requests_options_after_research(tmp_path, monkeypatch):
    async def check():
        now = OPEN.astimezone(ET) + timedelta(minutes=31)
        events = []

        async def run(kind, *, work_scope):
            events.append(("research", kind, work_scope))
            return JobOutcome(JobStatus.DONE, detail={"reviewed": 2})

        def options(window):
            events.append(("options", window))

        sched, _ = build(tmp_path, monkeypatch, now, research=SimpleNamespace(
            run=run, has_fresh_thesis_set=lambda _: True,
        ))
        sched.store.open()
        sched.request_options_rescan = options
        try:
            key = f"{now.date()}:1"
            hits = [{"signal": "spy_up_close", "text": "SPY +1.50% from the prior regular close"}]
            outcome = await sched._run_market_move_rescan(key, hits)
            assert outcome.status is JobStatus.DONE
            assert outcome.detail["triggers"] == hits
            assert events == [("research", "market_move", f"market_move:{key}"), ("options", key)]
        finally:
            await sched._cleanup()

    asyncio.run(check())
