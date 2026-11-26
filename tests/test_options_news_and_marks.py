"""Options scans vs the news freshness stamp, and display-only intrinsic marks for unpriced legs.

(1) The live news poll stamps only held + focus + research-universe names; option underlyings that a
TipRanks slice displaced from the research universe never get a stamp, so every scan of them was
refused as news_stale_or_unavailable. The fix polls such a name for real (once) before giving up.
(2) An unpriced leg in the daily close carries a flagged intrinsic estimate instead of a bare 0; the
haircut, close equity and anything entries read are unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from stock_agent import close_trust as ct
from stock_agent.config import Config
from stock_agent.research_gate import Eligibility
from stock_agent.research_schemas import Direction
from tests.options_support import StubGate, make_manager, seed_chain
from tests.test_close_trust import leg, snap, stock
from tests.test_tipranks_ingest import ET, _row, _write_csv

D = Decimal


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ (1) cause, end to end
@pytest.mark.world_start(datetime(2026, 11, 23, 10, 15, tzinfo=ET))
def test_displaced_option_underlying_has_no_news_stamp_until_really_polled(world, monkeypatch):
    monkeypatch.setattr(Config, "PRIORITY_SYMBOLS", ["ANET"])
    monkeypatch.setattr(Config, "UNIVERSE_SIZE", 5)
    monkeypatch.setattr(Config, "TIPRANKS_UNIVERSE_SLICE", 2)
    monkeypatch.setattr(Config, "TIPRANKS_MIN_SMART_SCORE", 8)
    monkeypatch.setattr(Config, "OPTIONS_ENABLED", True)
    downstream = ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL"]
    for sym in ("ANET", "SPY", "QQQ", *downstream, "TRQA", "TRQB"):
        world.alpaca.add_asset(sym)
        world.price(sym, 100.0, spread=0.02)

    async def body():
        sched = world.scheduler()
        try:
            await sched.startup()
            await sched._refresh_assets()
            now = world.clock.now()
            _write_csv(world.data_dir, [
                _row("TRQA", smart_score="10", consensus="Strong Buy", market_cap="$90,000,000,000"),
                _row("TRQB", smart_score="9", consensus="Strong Buy", market_cap="$80,000,000,000"),
            ], mtime=now - timedelta(hours=2))

            async def fixed():
                return tuple(downstream)

            monkeypatch.setattr(sched.fundamentals, "list_universe", fixed)
            sources = sched.research.sources
            eq = await sources.universe(now)
            assert "NVDA" not in eq and "NVDA" in sched._option_universe()
            await sources.live_news((), (), eq, now)  # what the live news job polls
            assert sources.news_ok("AAPL", now)
            assert not sources.news_ok("NVDA", now)  # the cause of the options skips
            before = len(world.net.recorded)
            await sources.refresh_entry_inputs("NVDA", now)  # the hook the options manager now uses
            polled = [r for r in world.net.recorded[before:] if "NVDA" in str(r.url)]
            assert polled, "the stamp must come from a real poll of NVDA"
            assert sources.news_ok("NVDA", world.clock.now())
        finally:
            await sched._cleanup()

    run(body())


# ------------------------------------------------------------------ (1) manager behaviour
class StaleNewsGate(StubGate):
    """Refuses as news-stale until a refresh really ran (or with fixed extra reasons)."""

    def __init__(self, extra=(), refresh_works=True):
        super().__init__()
        self.extra = tuple(extra)
        self.refresh_works = refresh_works
        self.refreshes: list[str] = []
        self.fresh = False

    async def refresh(self, symbol, now):
        self.refreshes.append(symbol)
        if self.refresh_works == "raise":
            raise RuntimeError("synthetic poll failure")
        self.fresh = bool(self.refresh_works)

    def is_entry_eligible(self, symbol, now):
        if not self.fresh or self.extra:
            return Eligibility(False, ("news_stale_or_unavailable", *self.extra))
        return super().is_entry_eligible(symbol, now)


def _scan(world, tmp_path, gate, hook=True):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    m._research_refresh = gate.refresh if hook else None
    return run(m.scan())


def test_news_stale_underlying_is_polled_once_then_opens(world, tmp_path):
    gate = StaleNewsGate()
    out = _scan(world, tmp_path, gate)
    assert gate.refreshes == ["AAA"]
    assert out[0].opened, out  # without the fix: "research gate: news_stale_or_unavailable"


@pytest.mark.parametrize("extra", [("missing_thesis",), ("unverified_thesis",)])
def test_missing_or_unverified_thesis_never_triggers_a_poll(world, tmp_path, extra):
    gate = StaleNewsGate(extra=extra)
    out = _scan(world, tmp_path, gate)
    assert gate.refreshes == [] and not out[0].opened
    assert out[0].reason == "research gate: news_stale_or_unavailable," + extra[0]
    assert not world.orders_sent()


@pytest.mark.parametrize("works", [False, "raise"], ids=["no_stamp", "raises"])
def test_failed_poll_stays_closed(world, tmp_path, works):
    gate = StaleNewsGate(refresh_works=works)
    out = _scan(world, tmp_path, gate)
    assert gate.refreshes == ["AAA"] and not out[0].opened
    assert "news_stale_or_unavailable" in out[0].reason and not world.orders_sent()


def test_without_refresh_hook_stays_closed(world, tmp_path):
    gate = StaleNewsGate()
    out = _scan(world, tmp_path, gate, hook=False)
    assert not out[0].opened and "news_stale_or_unavailable" in out[0].reason


# ------------------------------------------------------------------ (2) intrinsic estimate
EXP = date(2026, 11, 27)


def _rows(trust):
    return {row["symbol"]: row for row in trust.legs}


def test_unpriced_lone_long_gets_flagged_intrinsic_estimate_without_changing_close():
    itm = leg("AAA", 90, 2)  # unpriced long call, underlying held at 100 -> intrinsic 10
    otm = leg("BBB", 120, 1)  # unpriced long call, OTM at 100 -> 0
    book = snap(holdings=[stock("AAA", 10), stock("BBB", 10)], legs=[itm, otm])
    trust = ct.assess_close(book)
    assert trust.trusted and trust.conservative
    rows = _rows(trust)
    a = rows[itm.occ_symbol]
    assert a["estimated"] is True and a["estimate_basis"] == "intrinsic_from_underlying"
    assert D(a["estimate_value"]) == D(2000) and a["estimate_underlying_source"] == "held_stock_mark"
    assert D(rows[otm.occ_symbol]["estimate_value"]) == 0
    assert D(trust.detail()["estimated_option_value"]) == D(2000)
    # the estimate is display only: haircut and close equity ignore it
    assert trust.haircut == 0 and trust.equity == book.equity


def test_unpriced_short_estimate_is_a_liability_from_session_close():
    short = leg("CCC", 95, -1, right="put")
    book = snap(legs=[short])
    row = ct.leg_estimate(book, short, {"CCC": D(90)})
    assert D(row["estimate_value"]) == D(-500) and row["estimate_underlying_source"] == "session_close"


def test_no_underlying_price_means_no_estimate_not_zero():
    lone = leg("EEE", 90, 1)
    trust = ct.assess_close(snap(legs=[lone]))
    row = _rows(trust)[lone.occ_symbol]
    assert row["estimate_value"] is None and row["estimated"] is False
    assert row["estimate_basis"] == "no_underlying_price"
    assert trust.detail()["estimated_option_value"] is None


# ------------------------------------------------------------------ (2) no display I/O in the close job
def test_close_job_queues_remediation_with_no_display_fetch_for_unpriced_lone_long(world, monkeypatch):
    """A lone long leg on an unheld, unpriced underlying: the drawdown_close job makes no bar call
    for it, returns DONE, and persists the issuer-cap trim; the estimate is None, flagged."""
    from stock_agent.ledger import IntentStatus, JobStatus
    from tests.fakes import occ_symbol

    lone = occ_symbol("ZZZ", date(2026, 12, 18), "call", 50)

    async def check():
        sched = world.scheduler()
        sched.store.open()
        ledger = sched.ledger
        world.alpaca.cash = 80_000
        world.price("ALFA", 100)
        world.alpaca.seed_position("ALFA", 200, 100)
        world.alpaca.seed_position(lone, 1, 1.0, price=0.0)  # unpriced long, ZZZ not held
        calls = []

        async def stalled(symbols, **kw):
            calls.append(tuple(symbols))
            await asyncio.sleep(3600)

        monkeypatch.setattr(sched.prices, "get_daily_bars", stalled)

        async def close(day):
            world.clock.set_et(2026, 11, day, 16, 5)
            key = world.clock.now_et().date().isoformat()
            sched._schedule_job("drawdown_close", key, sched._close_drawdown, timeout=10)
            if task := sched._job_tasks.get("drawdown_close"):
                await asyncio.wait_for(task, timeout=5)
                await sched._collect_finished_jobs()
            return ledger.job_status("drawdown_close", key)

        try:
            world.clock.set_et(2026, 11, 23, 10, 0)
            await sched.calendar.refresh()
            await ledger.refresh("seed")
            first = await close(23)
            assert first.status == JobStatus.DONE, first
            second = await close(24)
            assert second.status == JobStatus.DONE, second
            (trim,) = ledger.list_intents([IntentStatus.QUEUED])
            assert trim.symbol == "ALFA"
            assert calls == []  # no bar fetch at all: no display-only await in the job
            row = next(r for r in second.detail["legs"] if r["symbol"] == lone)
            assert row["estimate_value"] is None and row["estimate_basis"] == "no_underlying_price"
        finally:
            await sched._cleanup()

    run(check())


def test_known_short_leg_estimate_survives_a_second_pass():
    short = leg("CCC", 95, -1, right="put")
    book = snap(legs=[short])
    trust = ct.assess_close(book, {"CCC": D(80)})
    (row,) = trust.legs
    assert D(row["estimate_value"]) == D(-1500)
    again = ct._with_estimates([dict(row)], book, {"OTHER": D(10)})  # CCC price absent this pass
    assert D(again[0]["estimate_value"]) == D(-1500) and again[0]["estimated"] is True
    assert trust.haircut == D(1500)  # the estimate is not the haircut's input


# ------------------------------------------------------------------ (1) refresh is bounded under the lock
def test_stale_refresh_has_one_aggregate_budget_per_scan_and_a_cross_scan_cooldown(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    calls = []

    class Gate(StubGate):
        def is_entry_eligible(self, symbol, now):
            return Eligibility(False, ("news_stale_or_unavailable",))

    async def slow(symbol, now):
        calls.append(symbol)
        await asyncio.sleep(3600)

    from stock_agent.options_manager import OptionCandidate

    m = make_manager(world, tmp_path, gate=Gate())
    m._research_refresh = slow
    m._STALE_REFRESH_BUDGET_S = 0.3
    cands = [OptionCandidate(s, source="news", story_id=f"st-{i}") for i, s in enumerate(["AAA", "AAA", "BBB", "CCC"])]

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        out = await m.scan(candidates=cands)
        return out, loop.time() - t0

    out, took = run(go())
    assert took < 1.0 and calls == ["AAA"]  # one stalled poll used the whole budget; no per-candidate retries
    assert all(not o.opened for o in out)
    calls.clear()
    out, _ = run(go())
    assert "AAA" not in calls  # cooldown: AAA is not polled again in the next scan
