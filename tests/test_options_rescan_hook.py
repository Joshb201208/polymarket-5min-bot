"""Market-move options re-scan hook (V1 restore, Part 4).

The rescan job only *requests* an extra options scan; the scan runs later as the ordinary
"options" job in its own window. These tests pin that the existing entry rules still hold on
a re-scan (one structure per underlying, open-structure cap, restart safety) and that the
hook never runs beside the options protection lane or inside the stock stop lane.
All numbers are synthetic.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from stock_agent import scheduler as module
from stock_agent.ledger import JobStatus
from stock_agent.market_calendar import ET
from stock_agent.options_book import OptionsStore
from stock_agent.research_schemas import Direction
from tests.options_support import StubGate, make_manager, seed_chain
from tests.test_scheduler_lifecycle import EngineStub, build

KEY = module.OPTIONS_RESCAN_STATE_KEY


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ manager rules on a re-scan

def _orders(world):
    return [o for o in world.alpaca.orders.values()] if isinstance(world.alpaca.orders, dict) else list(
        world.alpaca.orders)


def test_rescan_never_opens_a_second_structure_on_the_same_underlying(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    first = run(m.scan())
    assert first[0].opened
    sent = len(_orders(world))
    world.clock.set_et(2026, 11, 23, 11, 0)  # market-move rescan later the same day
    again = run(m.scan())
    assert again[0].opened is None
    assert again[0].reason == "already holds a structure on this underlying"
    assert len(_orders(world)) == sent


def test_rescan_after_restart_reads_the_book_and_does_not_double_open(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    assert run(m.scan())[0].opened
    sent = len(_orders(world))
    restarted = make_manager(world, tmp_path, gate=gate, store=OptionsStore(tmp_path / "options_book.json"))
    out = run(restarted.scan())
    assert out[0].opened is None and "already holds" in out[0].reason
    assert len(_orders(world)) == sent


def test_rescan_respects_the_open_structure_cap(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate, max_open_structures=1)
    assert run(m.scan())[0].opened
    seed_chain(world, "BBB")
    gate.set("BBB", Direction.BUY, 8)
    m._universe = lambda: ["BBB"]
    sent = len(_orders(world))
    out = run(m.scan())
    assert out[0].opened is None and out[0].reason == "1 structures open"
    assert len(_orders(world)) == sent


# ------------------------------------------------------------------ scheduler hook

def _ready(sched, scans):
    sched._normalised = sched._reconciled = True

    async def scan(now, candidates=None):
        scans.append(now)
        return [SimpleNamespace(opened=None, reason="no idea", final=True)]

    async def monitor(now):
        scans.append("monitor")

    sched.options = SimpleNamespace(scan=scan, monitor=monitor, handled_story_ids=lambda ids: set())


def test_request_is_persisted_deduplicated_and_survives_restart(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 10, 30, tzinfo=ET)
    sched, _ = build(tmp_path, monkeypatch, now)
    sched.store.open()
    _ready(sched, [])
    assert sched.request_options_rescan("2026-11-27:spx-down-1", now) == "queued"
    assert sched.request_options_rescan("2026-11-27:spx-down-1", now) == "already_queued"
    assert sched.request_options_rescan("2026-11-27:vix-up-1", now) == "queued"
    assert sched.ledger.risk_state_get(KEY) == {
        "session": "2026-11-27", "keys": ["2026-11-27:spx-down-1", "2026-11-27:vix-up-1"]}
    sched.store.close()
    restarted, _ = build(tmp_path, monkeypatch, now)
    restarted.store.open()
    assert restarted.ledger.risk_state_get(KEY)["keys"] == ["2026-11-27:spx-down-1", "2026-11-27:vix-up-1"]
    restarted.store.close()


def test_request_without_a_manager_or_after_the_window_is_not_queued(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 10, 30, tzinfo=ET)
    sched, _ = build(tmp_path, monkeypatch, now)
    sched.store.open()
    sched.options = None
    assert sched.request_options_rescan("k1", now) == "no_options_manager"
    _ready(sched, [])
    late = datetime(2026, 11, 27, 12, 50, tzinfo=ET)  # build() session closes 13:00; window ends close - 10
    assert sched.request_options_rescan("k1", late) == "outside_options_window"
    assert sched.ledger.risk_state_get(KEY) is None
    sched.store.close()


def test_opening_gap_request_before_open_plus_15_is_refused_not_queued(tmp_path, monkeypatch):
    early = datetime(2026, 11, 27, 9, 35, tzinfo=ET)  # lead ruling: strict refusal before open + 15 min
    sched, _ = build(tmp_path, monkeypatch, early)
    sched.store.open()
    _ready(sched, [])
    assert sched.request_options_rescan("2026-11-27:0", early) == "outside_options_window"
    assert sched.ledger.risk_state_get(KEY) is None
    sched._drive_options_rescans(datetime(2026, 11, 27, 9, 46, tzinfo=ET))
    assert "options" not in sched._job_tasks  # nothing carried into the window
    sched.store.close()


def test_rescan_runs_as_the_options_job_after_the_daily_scan_and_only_once(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 10, 30, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        scans = []
        _ready(sched, scans)
        release = asyncio.Event()

        async def daily():
            await release.wait()

        sched._schedule_job("options", now.date().isoformat(), daily, timeout=5)
        assert sched.request_options_rescan("spx-up-1", now) == "queued"
        sched._drive_options_rescans(now)
        assert scans == []  # waits for the running daily scan: one options scan at a time
        release.set()
        await sched._job_tasks["options"]
        await sched._collect_finished_jobs()
        sched._drive_options_rescans(now)
        window = sched.options_rescan_window("2026-11-27", "spx-up-1")
        await sched._job_tasks["options"]
        await sched._collect_finished_jobs()
        assert scans == [now]
        assert sched.ledger.job_status("options", window).status is JobStatus.DONE
        sched._drive_options_rescans(now)  # DONE is never re-run
        assert "options" not in sched._job_tasks
        sched.store.close()

        restarted, _ = build(tmp_path, monkeypatch, now)
        restarted.store.open()
        rescans = []
        _ready(restarted, rescans)
        restarted._drive_options_rescans(now)
        assert "options" not in restarted._job_tasks and rescans == []  # no double fire after restart
        restarted.store.close()

    run(check())


def test_request_still_waiting_when_the_window_closes_is_journalled_as_skipped(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 10, 30, tzinfo=ET)
    sched, _ = build(tmp_path, monkeypatch, now)
    sched.store.open()
    scans = []
    _ready(sched, scans)
    assert sched.request_options_rescan("spx-down-1", now) == "queued"
    late = datetime(2026, 11, 27, 12, 55, tzinfo=ET)
    sched._drive_options_rescans(late)
    window = sched.options_rescan_window("2026-11-27", "spx-down-1")
    row = sched.ledger.job_status("options", window)
    assert row.status is JobStatus.SKIPPED and row.detail == {"reason": "outside_options_window"}
    assert "options" not in sched._job_tasks and scans == []
    sched.ledger.risk_state_set(KEY, {"session": "2026-11-26", "keys": ["old"]})
    sched._drive_options_rescans(now)
    assert "options" not in sched._job_tasks  # yesterday's request never carries over
    assert sched.ledger.job_status("options", sched.options_rescan_window("2026-11-27", "old")) is None
    sched.store.close()


def test_rescan_is_blocked_while_entries_are_locked(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 10, 30, tzinfo=ET)
    sched, _ = build(tmp_path, monkeypatch, now)
    sched.store.open()
    scans = []
    _ready(sched, scans)
    assert sched.request_options_rescan("spx-down-1", now) == "queued"
    sched._normalised = False
    sched._drive_options_rescans(now)
    assert "options" not in sched._job_tasks
    sched.store.close()


def test_stop_lane_runs_and_options_exits_wait_while_a_rescan_scan_is_in_flight(tmp_path, monkeypatch):
    async def check():
        now = datetime(2026, 11, 27, 10, 30, tzinfo=ET)
        events = []
        sched, _ = build(tmp_path, monkeypatch, now, engine=EngineStub(events))
        sched.store.open()
        sched._normalised = sched._reconciled = True
        sched._engine_resumed = True
        sched._last_reconcile_at = now
        started, release = asyncio.Event(), asyncio.Event()
        monitors = []

        async def scan(now, candidates=None):
            started.set()
            await release.wait()
            return [SimpleNamespace(opened=None, reason="x", final=True)]

        async def monitor(now):
            monitors.append(now)

        sched.options = SimpleNamespace(scan=scan, monitor=monitor, handled_story_ids=lambda ids: set())
        assert sched.request_options_rescan("spx-down-1", now) == "queued"
        sched._drive_options_rescans(now)
        await started.wait()
        await sched._drive_order_lane(now)
        await sched._order_task
        assert events == ["protection"]  # the stock stop/order lane is a separate task and still runs
        await sched._drive_options_monitor(now)
        assert sched._options_task is None and monitors == []  # same exclusion as the daily scan
        release.set()
        await sched._job_tasks["options"]
        await sched._collect_finished_jobs()
        await sched._drive_options_monitor(now)
        await sched._options_task
        assert len(monitors) == 1
        await sched._cleanup()

    run(check())


def test_hung_broker_read_inside_the_capital_lock_is_bounded_by_the_job_timeout(world, tmp_path, monkeypatch):
    """Lead bar: the worst case shared with the daily scan is bounded by the options job timeout."""
    async def check():
        world.clock.set_et(2026, 11, 23, 10, 0)
        seed_chain(world)
        gate = StubGate()
        gate.set("AAA", Direction.BUY, 8)
        m = make_manager(world, tmp_path / "book", gate=gate)
        lock = asyncio.Lock()
        m._capital_lock = lock
        real = m.broker.get_account
        calls = []

        async def hanging_account():
            calls.append(1)
            if len(calls) >= 2:  # the read under the capital lock (options buying power)
                await asyncio.Event().wait()
            return await real()

        m.broker.get_account = hanging_account
        now = datetime(2026, 11, 23, 10, 0, tzinfo=ET)
        sched, _ = build(tmp_path, monkeypatch, now)
        sched.store.open()
        sched.options = m
        sent = len(_orders(world))
        sched.ledger.start_job("options", "w")
        started = asyncio.get_running_loop().time()
        job = asyncio.create_task(sched._run_scheduled_job("options", "w", sched._run_options_job, 0.5, 1))
        while not lock.locked():
            await asyncio.sleep(0.01)
        bought = []

        async def stock_buy():  # ExecutionEngine.buy takes the same lock only around authorise + submit
            async with lock:
                bought.append(asyncio.get_running_loop().time())

        buy = asyncio.create_task(stock_buy())
        await asyncio.sleep(0.05)
        assert bought == []  # queued behind the hung options read
        await job
        await asyncio.wait_for(buy, 1)
        assert asyncio.get_running_loop().time() - started < 5
        assert len(calls) >= 2 and not lock.locked()  # cancellation released the shared lock
        assert len(bought) == 1  # the queued stock buy went through after the cancel
        assert sched.ledger.job_status("options", "w").status is JobStatus.FAILED
        assert m.live() == [] and len(_orders(world)) == sent  # nothing reserved, nothing sent
        sched.store.close()

    run(check())


def test_only_the_stock_buy_step_takes_the_shared_capital_lock():
    """Stops, exits and the protection tick never wait on the lock an options scan can hold."""
    import inspect

    from stock_agent.execution import ExecutionEngine

    import re

    takers = sorted(name for name, fn in inspect.getmembers(ExecutionEngine, inspect.isfunction)
                    if re.search(r"async with \(?self\._capital_lock", inspect.getsource(fn)))
    assert takers == ["buy"]
    # the locked section may be factored into a helper, but only buy() may call it
    src = inspect.getsource(ExecutionEngine)
    if "_authorise_and_submit_buy" in src:
        callers = sorted(name for name, fn in inspect.getmembers(ExecutionEngine, inspect.isfunction)
                         if "self._authorise_and_submit_buy(" in inspect.getsource(fn))
        assert callers == ["buy"]
