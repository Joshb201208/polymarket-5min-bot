"""V1 restore part 4 (market-move rescan), end to end on the world harness.

The real scheduler ticks on simulated time. SPY comes from a synthetic quote and prior-session bar (round numbers),
VIX from the `market_move_vix` seam (unset in production), and the macro regime from a scripted snapshot. The rescan
runs the real research pipeline against the fake LLM. Covered: an S&P move from the prior close triggers one journalled
rescan with one alert; zero, stale, off-hours and missing readings never trigger; missing or failing VIX leaves S&P
only; a VIX +3 move or a regime change triggers alone; the 30-minute cooldown; at most 3 rescans per session; a
restart mid-rescan does not fire it again; an exhausted research budget spends nothing on the LLM; and the 30-second
protection lane keeps placing stops while a rescan is still running.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from stock_agent.config import Config
from stock_agent.ledger import JobStatus
from stock_agent.market_move import Reading
from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness

pytestmark = pytest.mark.world_start(datetime(2026, 11, 15, 17, tzinfo=ET))

DAY = 17  # Tuesday 2026-11-17; the regular session opens 09:30 ET
RESCAN = "market_move_rescan"


@dataclass
class Tape:
    """What the market-move sampler sees. `spy` is the quote price; None means no SPY quote at all."""

    spy: float | None = 100.0
    spy_age_s: int = 0
    spy_in_hours: bool = True
    prior_close: float = 100.0
    vix: float | None = None  # None: the VIX seam returns nothing (production leaves it unset)
    vix_prior: float = 20.0
    vix_raises: bool = False
    regime: str | None = None  # None: degraded macro, no regime signal
    vix_calls: list = field(default_factory=list)

    def install(self, h: Harness) -> None:
        s, clock = h.scheduler, h.world.clock
        real_quotes, real_bars = s.prices.get_quotes, s.prices.get_daily_bars

        async def get_quotes(symbols, *args, **kw):
            if list(symbols) != ["SPY"]:
                return await real_quotes(symbols, *args, **kw)
            if self.spy is None:
                return SimpleNamespace(quotes={})
            quote = SimpleNamespace(price=Decimal(str(self.spy)), is_market_hours=self.spy_in_hours,
                                    as_of=clock.now() - timedelta(seconds=self.spy_age_s))
            return SimpleNamespace(quotes={"SPY": quote})

        async def get_daily_bars(symbols, *args, start_date=None, end_date=None, **kw):
            if list(symbols) != ["SPY"]:
                return await real_bars(symbols, *args, start_date=start_date, end_date=end_date, **kw)
            return {"SPY": [SimpleNamespace(session_date=end_date, close=Decimal(str(self.prior_close)))]}

        async def vix(session, previous):
            self.vix_calls.append(session.date)
            if self.vix_raises:
                raise TimeoutError("synthetic VIX outage")
            if self.vix is None:
                return None
            return Reading(Decimal(str(self.vix)), clock.now()), Decimal(str(self.vix_prior))

        async def check_conditions():
            if self.regime is None:
                return SimpleNamespace(regime=None, degraded=True, timestamp=clock.now().isoformat())
            return SimpleNamespace(regime=self.regime, degraded=False, timestamp=clock.now().isoformat())

        h.monkeypatch.setattr(s.prices, "get_quotes", get_quotes)
        h.monkeypatch.setattr(s.prices, "get_daily_bars", get_daily_bars)
        h.monkeypatch.setattr(s.macro, "check_conditions", check_conditions)
        h.monkeypatch.setattr(s.macro, "get_last_snapshot", lambda: None)
        s.market_move_vix = vix


def _run(world, monkeypatch, body, **tape):
    h = Harness(world, monkeypatch).install()
    t = Tape(**tape)

    async def go():
        try:
            await h.start()
            t.install(h)
            await body(h, t)
        finally:
            await h.stop()

    asyncio.run(go())


async def sample(h: Harness, hh: int, mm: int, ss: int = 0):
    """One real tick in the sample window (30-179 s after a 30-minute slot boundary), then wait for its jobs."""
    h.world.clock.set_et(2026, 11, DAY, hh, mm, ss)
    s = h.scheduler
    await s._tick(h.world.clock.now_et())
    while pending := [task for task in s._job_tasks.values() if not task.done()]:
        await asyncio.wait(pending)
    await s._collect_finished_jobs()


def rescans(h: Harness) -> list[str]:
    """Windows of every claimed rescan this session, from the job ledger (claims are durable)."""
    s = h.scheduler
    return [f"2026-11-{DAY}:{slot}" for slot in range(14)
            if s.ledger.job_status(RESCAN, f"2026-11-{DAY}:{slot}") is not None]


def market_move_funnels(h: Harness) -> list[dict]:
    return [row for row in h.scheduler.ledger.journal.read(kinds={"research.entry_funnel"})
            if row.get("job") == "market_move"]


async def rescan_alerts(h: Harness) -> list[str]:
    await h.scheduler.alerts.flush()
    return [text for text in h.world.discord.texts() if "Market-move rescan" in text]


def test_sp500_move_from_prior_close_triggers_one_journalled_rescan(world, monkeypatch):
    async def body(h, t):
        await sample(h, 9, 31)  # slot 0: SPY 100 vs prior close 100
        assert rescans(h) == []
        t.spy = 101.5  # exactly +1.5% from the prior close
        await sample(h, 10, 1)
        assert rescans(h) == [f"2026-11-{DAY}:1"]
        # It ran to completion (DONE, or FAILED when the fake model can't ground a thesis); never left STARTED.
        assert h.scheduler.ledger.job_status(RESCAN, f"2026-11-{DAY}:1").status in {JobStatus.DONE, JobStatus.FAILED}
        assert market_move_funnels(h), "the rescan must run the real research pipeline"
        assert len(await rescan_alerts(h)) == 1
        await sample(h, 10, 31)  # still +1.5% from the close: the first crossing per direction is consumed
        assert rescans(h) == [f"2026-11-{DAY}:1"]

    _run(world, monkeypatch, body)


def test_below_threshold_zero_stale_offhours_and_missing_readings_never_trigger(world, monkeypatch):
    async def body(h, t):
        steps = [
            (9, 31, dict(spy=101.4)),  # +1.4%: below the threshold
            (10, 1, dict(spy=0.0)),  # zero price
            (10, 31, dict(spy=103.0, spy_age_s=120)),  # stale (over 90 s old)
            (11, 1, dict(spy=103.0, spy_in_hours=False)),  # not a regular-hours print
            (11, 31, dict(spy=None)),  # no quote
            (12, 1, dict(spy=None, vix=0.0)),  # zero VIX
        ]
        for hh, mm, values in steps:
            for key, value in {"spy_age_s": 0, "spy_in_hours": True, "vix": None, **values}.items():
                setattr(t, key, value)
            await sample(h, hh, mm)
            assert rescans(h) == [], (hh, mm, values)
        assert not market_move_funnels(h)
        t.spy, t.vix = 101.5, None
        await sample(h, 12, 31)  # the detector is alive: a valid reading still triggers
        assert rescans(h) == [f"2026-11-{DAY}:6"]

    _run(world, monkeypatch, body)


def test_missing_or_failing_vix_falls_back_to_sp500_only(world, monkeypatch):
    async def body(h, t):
        t.vix_raises = True
        await sample(h, 9, 31)
        assert t.vix_calls, "the injected VIX seam must be consulted"
        assert rescans(h) == []
        t.spy = 98.5  # -1.5% from the prior close while VIX keeps failing
        await sample(h, 10, 1)
        assert rescans(h) == [f"2026-11-{DAY}:1"]

    _run(world, monkeypatch, body)


def test_vix_plus_three_points_alone_triggers(world, monkeypatch):
    async def body(h, t):
        await sample(h, 9, 31)  # VIX 20 = prior close
        assert rescans(h) == []
        t.vix = 23.0  # +3 points; SPY flat
        await sample(h, 10, 1)
        assert rescans(h) == [f"2026-11-{DAY}:1"]

    _run(world, monkeypatch, body, vix=20.0, vix_prior=20.0)


def test_regime_change_alone_triggers(world, monkeypatch):
    async def body(h, t):
        await sample(h, 9, 31)
        assert rescans(h) == []
        t.regime = "cautious"
        await sample(h, 10, 1)
        assert rescans(h) == [f"2026-11-{DAY}:1"]

    _run(world, monkeypatch, body, regime="normal")


def test_cooldown_suppresses_a_second_trigger_within_30_minutes_without_replay(world, monkeypatch):
    async def body(h, t):
        t.spy = 102.0
        await sample(h, 9, 32, 30)  # slot 0 claimed at 09:32:30
        assert rescans(h) == [f"2026-11-{DAY}:0"]
        t.spy = 99.0  # -2.9% over the window, but only 28 minutes after the claim
        await sample(h, 10, 0, 30)
        assert rescans(h) == [f"2026-11-{DAY}:0"]
        await sample(h, 10, 30, 30)  # flat: the suppressed signal is not replayed later
        assert rescans(h) == [f"2026-11-{DAY}:0"]
        t.spy = 101.0  # +2.0% over the window, past the cooldown
        await sample(h, 11, 0, 30)
        assert rescans(h) == [f"2026-11-{DAY}:0", f"2026-11-{DAY}:3"]

    _run(world, monkeypatch, body)


def test_at_most_three_rescans_per_session(world, monkeypatch):
    async def body(h, t):
        tape = [(9, 31, 102.0), (10, 1, 102.0), (10, 31, 99.0), (11, 1, 99.0), (11, 31, 101.0),
                (12, 1, 101.0), (12, 31, 99.0), (13, 1, 99.0), (13, 31, 101.0)]
        for hh, mm, price in tape:
            t.spy = price
            await sample(h, hh, mm)
        assert rescans(h) == [f"2026-11-{DAY}:{slot}" for slot in (0, 2, 4)]
        assert len(market_move_funnels(h)) == 3
        assert len(await rescan_alerts(h)) == 3

    _run(world, monkeypatch, body)


def test_restart_mid_rescan_does_not_fire_it_again(world, monkeypatch):
    async def body(h, t):
        s = h.scheduler
        entered, calls = asyncio.Event(), []
        real_run = s.research.run

        async def stuck(kind, *args, **kw):
            if kind != "market_move":
                return await real_run(kind, *args, **kw)
            calls.append(kw.get("work_scope"))
            entered.set()
            await asyncio.Future()  # the process dies before the rescan finishes

        monkeypatch.setattr(s.research, "run", stuck)
        t.spy = 102.0
        h.world.clock.set_et(2026, 11, DAY, 9, 31)
        await s._tick(h.world.clock.now_et())
        await asyncio.wait_for(s._job_tasks["market_move_sample"], 5)
        await asyncio.wait_for(entered.wait(), 5)
        assert calls == [f"market_move:2026-11-{DAY}:0"]
        await h.restart()
        t.install(h)
        await sample(h, 9, 32)  # the same slot again after the restart
        await sample(h, 10, 1)  # and the next slot, still +2% from the close
        assert calls == [f"market_move:2026-11-{DAY}:0"]
        assert rescans(h) == [f"2026-11-{DAY}:0"]
        assert not market_move_funnels(h)

    _run(world, monkeypatch, body)


def test_exhausted_research_budget_spends_nothing_on_the_llm(world, monkeypatch):
    monkeypatch.setattr(Config, "RESEARCH_WEEKLY_BUDGET_USD", 0.0)

    async def body(h, t):
        h.story()  # fresh evidence, so only the budget stands between the rescan and a model call
        t.spy = 102.0
        await sample(h, 9, 31)
        assert rescans(h) == [f"2026-11-{DAY}:0"]
        assert h.calls == []
        assert h.world.net.requests(service="openai") == []
        [funnel] = market_move_funnels(h)
        assert "llm_budget" in repr(funnel), funnel
        assert h.buys() == []

    _run(world, monkeypatch, body)


def test_protection_lane_keeps_placing_stops_while_a_rescan_runs(world, monkeypatch):
    async def body(h, t):
        s = h.scheduler
        release = asyncio.Event()
        real_run = s.research.run

        async def slow(kind, *args, **kw):
            if kind == "market_move":
                await release.wait()
            return await real_run(kind, *args, **kw)

        monkeypatch.setattr(s.research, "run", slow)
        t.spy = 102.0
        h.world.clock.set_et(2026, 11, DAY, 9, 31)
        await s._tick(h.world.clock.now_et())
        await asyncio.wait_for(s._job_tasks["market_move_sample"], 5)
        rescan = s._job_tasks[RESCAN]
        # An unprotected holding appears at the broker while the rescan is still running.
        h.world.alpaca.seed_position(h.symbol, 10, 100.0, price=100.0)
        for _ in range(6):
            h.world.clock.advance(seconds=30)
            await asyncio.wait_for(s._tick(h.world.clock.now_et()), 5)
            if s._order_task is not None:
                await asyncio.wait_for(s._order_task, 5)
            await s._collect_finished_jobs()
        assert not rescan.done(), "the rescan should still be running"
        stops = [r for r in h.world.orders_sent(include_failed=False)
                 if r.method == "POST" and (r.json or {}).get("type") in {"stop", "stop_limit"}]
        assert stops, "the 30-second protection lane must not wait for the rescan"
        release.set()
        await asyncio.wait_for(rescan, 30)

    _run(world, monkeypatch, body)
