"""Trusted-close gap (TRUSTED_CLOSE_FIX_BRIEF item 4), run through the real scheduler, reconcile and ledger.

On 23 Nov two legacy long calls marked 0.0 made the reconcile snapshot "indicative". _close_drawdown then refused the
close as close_equity_unverified on every attempt, so no close mark or drawdown record was written. The brakes were
starved while entries stayed open. The fix accepts the close for the drawdown job only when the ONLY invalid marks
are option legs. It records broker equity minus a conservative haircut: 0 for long legs, and intrinsic value for short
legs on a valid underlying. Everything else stays untrusted.

The book below is illustrative: the leg symbols are the real ones, but the quantities and cash are not the owner's
account. Field names that belong to the fix are read through the small accessors at the top, so a rename is a
one-line change.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from stock_agent.ledger import JobStatus
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState
from stock_agent.scheduler import MAX_JOB_ATTEMPTS

GS_CALL = "GS261127C00920000"
JPM_CALL = "JPM261127C00345000"
ALERT_TITLE = "Trusted close not recorded"


# --------------------------------------------------------------------------- accessors for the fix's contract
def close_mark(sched, day: date):
    return next((m for m in sched.ledger.equity_marks("close", since=day) if m.session_date == day), None)


def drawdown_session(sched) -> str | None:
    raw = sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY)
    return DrawdownState.from_record(raw).last_session if raw else None


def job(sched, day: date):
    return sched.ledger.job_status("drawdown_close", day.isoformat())


def detail_text(row) -> str:
    return repr(dict(row.detail or {}))


def missed_alerts(world) -> list[str]:
    return [t for t in world.alert_texts() if ALERT_TITLE in t]


# --------------------------------------------------------------------------- book and job helpers
GS_SHORT = "GS261127C00940000"
JPM_SHORT = "JPM261127C00365000"
JPM_COVERED = "JPM261211C00340000"


def seed_23nov(world, *, gs_price: float | None = 820.0, jpm_price: float = 310.0) -> None:
    """The 20/23 Nov position shape (options_engineer, from the 20 Nov broker snapshot). Prices and cash are
    illustrative. Two debit call verticals whose long legs are marked 0.0 (so reconcile sees them crossed), a covered
    call on held JPM, and legacy short stock in AMGN and PDD."""
    world.alpaca.cash = 60_000
    world.price("JPM", jpm_price)
    world.alpaca.seed_position("JPM", 130, 250, price=jpm_price)
    world.price("AMGN", 300)
    world.alpaca.seed_position("AMGN", -10, 280, price=300)
    world.price("PDD", 120)
    world.alpaca.seed_position("PDD", -85, 110, price=120)
    if gs_price is not None:
        world.price("GS", gs_price)  # GS is not held: only market data knows it
    for sym, qty, cost, px in ((GS_CALL, 1, 12.0, 0.0), (GS_SHORT, -1, 6.0, 4.30),
                               (JPM_CALL, 1, 4.0, 0.0), (JPM_SHORT, -1, 1.0, 0.05),
                               (JPM_COVERED, -1, 2.5, 1.98)):
        world.alpaca.seed_position(sym, qty, cost, price=px)


def add_short_leg(world, sym: str, qty: float, underlying: str, und_px: float | None, *, held: bool) -> None:
    if und_px is not None:
        world.price(underlying, und_px)
        if held:
            world.alpaca.seed_position(underlying, 100 * abs(qty), und_px, price=und_px)
    world.alpaca.seed_position(sym, qty, 5.0, price=0.0)


async def run_attempts(sched, world, day: int, attempts: int = 1, *, hour: int = 16, minute: int = 5):
    """Run the scheduler's own job wrapper (retry clock, attempt counting, terminal alerting) for one session."""
    world.clock.set_et(2026, 11, day, hour, minute)
    key = world.clock.now_et().date().isoformat()
    for _ in range(attempts):
        sched._schedule_job("drawdown_close", key, sched._close_drawdown, timeout=10)
        if task := sched._job_tasks.get("drawdown_close"):
            await task
            await sched._collect_finished_jobs()
        world.clock.advance(minutes=16)
    return sched.ledger.job_status("drawdown_close", key)


def broker_equity(world) -> Decimal:
    return Decimal(str(world.alpaca.account_json()["equity"]))


def _run(world, body):
    async def check():
        sched = world.scheduler()
        sched.store.open()
        sched.options.load_state()
        try:
            await body(sched)
        finally:
            await sched._cleanup()

    asyncio.run(check())


# --------------------------------------------------------------------------- 1. the 23 Nov reproduction
@pytest.mark.parametrize("gs_price", [820.0, 935.0], ids=["gs_far", "gs_near_short_strike"])
def test_23nov_position_set_records_the_close(world, gs_price):
    """Fails on 82b56e5 (FAILED close_equity_unverified, no mark, no drawdown record); records the close on the fix.

    Expected under the fix (options_engineer): trusted, haircut 0. The long legs carry mv 0 at the broker, and each
    crossed debit vertical is bounded by max(0, pair net mv) = 0."""
    seed_23nov(world, gs_price=gs_price)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        snap = await sched.ledger.refresh("fixture")
        assert snap.quality == "indicative"  # the trigger is still present: reconcile is unchanged
        assert sched.ledger.last_reconcile()["clean"]
        assert {GS_CALL, JPM_CALL} <= {leg.occ_symbol for leg in snap.legs if not leg.price_ok}
        assert all(h.price_ok for h in snap.holdings)
        equity = broker_equity(world)
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.DONE, row
        mark = close_mark(sched, date(2026, 11, 23))
        assert mark is not None and mark.equity == equity
        assert drawdown_session(sched) == "2026-11-23"
        assert GS_CALL in detail_text(row) and JPM_CALL in detail_text(row)  # unpriced legs are on record
        assert not missed_alerts(world)
        after = sched.ledger.last_snapshot()  # the adjusted copy is never written back
        assert after.quality == "indicative" and after.equity == equity

    _run(world, body)


def test_23nov_replay_and_restart_keep_one_mark(world):
    seed_23nov(world)

    async def body(sched):
        await run_attempts(sched, world, 23)
        first = close_mark(sched, date(2026, 11, 23))
        state = sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY)
        assert first is not None
        world.alpaca.cash += 5_000  # a later re-run must not rewrite tonight's mark or the drawdown record
        await sched._close_drawdown()
        assert close_mark(sched, date(2026, 11, 23)) == first
        assert sched.ledger.risk_state_get(DRAWDOWN_STATE_KEY) == state

    async def restart(_):
        fresh = world.scheduler()
        fresh.store.open()
        try:
            row = await run_attempts(fresh, world, 23)
            assert row.status is JobStatus.DONE and row.attempts == 1  # DONE is not re-run after restart
        finally:
            await fresh._cleanup()

    _run(world, body)
    _run(world, restart)


# --------------------------------------------------------------------------- 2. long legs: only a zero broker mv
def test_unpriced_long_leg_with_nonzero_market_value_stays_untrusted(world, monkeypatch):
    seed_23nov(world)
    original = world.alpaca.position_json

    def position_json(p):
        out = original(p)
        if p.symbol == JPM_CALL:
            out["market_value"] = "45.0"  # invalid current_price but a nonzero broker value: inconsistent
        return out

    monkeypatch.setattr(world.alpaca, "position_json", position_json)

    async def body(sched):
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None and drawdown_session(sched) is None

    _run(world, body)


def test_crossed_credit_vertical_stays_untrusted(world):
    seed_23nov(world)
    # short the lower strike, long the higher: the long leg is marked ABOVE the short leg (crossed credit spread)
    world.alpaca.seed_position("JPM261127C00320000", -1, 3.0, price=1.00)
    world.alpaca.seed_position("JPM261127C00330000", 1, 1.0, price=3.00)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        snap = await sched.ledger.refresh("fixture")
        assert not next(leg for leg in snap.legs if leg.occ_symbol == "JPM261127C00320000").price_ok
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None

    _run(world, body)


# --------------------------------------------------------------------------- 3. short option legs
@pytest.mark.parametrize("right, und_px, intrinsic, held", [
    ("C", 1100.0, 180, True),    # deep ITM call (16% from strike), underlying held
    ("C", 1100.0, 180, False),   # same, underlying only in market data
    ("P", 800.0, 120, True),     # ITM put, 15% from strike
    ("C", 800.0, 0, False),      # 15% OTM call: no haircut
])
def test_short_leg_far_from_strike_applies_intrinsic_haircut(world, right, und_px, intrinsic, held):
    sym = f"NVDA261127{right}00920000"
    seed_23nov(world)
    add_short_leg(world, sym, -2, "NVDA", und_px, held=held)

    async def body(sched):
        equity = broker_equity(world)
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.DONE, row
        mark = close_mark(sched, date(2026, 11, 23))
        haircut = Decimal(intrinsic * 100 * 2)
        assert mark is not None and mark.equity == equity - haircut <= equity
        text = detail_text(row)
        assert sym in text and "conservative estimate" in text
        if haircut:
            assert any(f in text for f in (str(haircut), f"{haircut:,}", f"{haircut:.2f}", f"{haircut:,.2f}"))
        assert drawdown_session(sched) == "2026-11-23"
        assert sched.ledger.last_snapshot().equity == equity  # raw snapshot untouched

    _run(world, body)


@pytest.mark.parametrize("und_px", [950.0, 900.0, 1000.0], ids=["itm_3pct", "otm_2pct", "itm_8pct"])
def test_short_leg_near_the_money_stays_untrusted(world, und_px):
    sym = "NVDA261127C00920000"
    seed_23nov(world)
    add_short_leg(world, sym, -1, "NVDA", und_px, held=True)

    async def body(sched):
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None and drawdown_session(sched) is None

    _run(world, body)


def test_short_leg_with_unpriced_underlying_stays_untrusted(world):
    seed_23nov(world)
    add_short_leg(world, "XYZ261127C00050000", -1, "XYZ", None, held=False)  # no XYZ price anywhere

    async def body(sched):
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None and drawdown_session(sched) is None

    _run(world, body)


# --------------------------------------------------------------------------- 4-6. still untrusted
@pytest.mark.parametrize("symbol", ["JPM", "PDD"], ids=["long_stock", "short_stock"])
def test_unpriced_stock_stays_untrusted(world, symbol):
    seed_23nov(world)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        world.alpaca.set_price(symbol, 0.0)
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None and drawdown_session(sched) is None

    _run(world, body)


def test_trading_block_stays_untrusted(world):
    seed_23nov(world)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        await sched.ledger.refresh("fixture")
        sched.store.set_trading_block("test: operator block")
        assert sched.ledger.trading_block()
        row = await run_attempts(sched, world, 23)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None and drawdown_session(sched) is None

    _run(world, body)


@pytest.mark.parametrize("shift", [timedelta(minutes=-10), timedelta(seconds=-120)], ids=["pre_close", "stale"])
def test_pre_close_or_stale_snapshot_stays_untrusted(world, monkeypatch, shift):
    seed_23nov(world)

    async def body(sched):
        real = sched.ledger.refresh

        async def refresh(reason):
            snap = await real(reason)
            return replace(snap, as_of=snap.as_of + shift)

        monkeypatch.setattr(sched.ledger, "refresh", refresh)
        row = await run_attempts(sched, world, 23, minute=1)  # 16:01-10m is before the close; 16:01-2m is stale
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None and drawdown_session(sched) is None

    _run(world, body)


def test_market_not_closed_stays_untrusted(world):
    seed_23nov(world)

    async def body(sched):
        row = await run_attempts(sched, world, 23, hour=15, minute=0)
        assert row.status is JobStatus.FAILED
        assert close_mark(sched, date(2026, 11, 23)) is None

    _run(world, body)


# --------------------------------------------------------------------------- 7. final-attempt critical alert
def test_final_attempt_alert_fires_once_with_cause_and_streak(world):
    seed_23nov(world)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        world.alpaca.set_price("PDD", 0.0)  # an unpriced STOCK keeps the close untrusted
        row = await run_attempts(sched, world, 23, attempts=MAX_JOB_ATTEMPTS - 1)
        assert row.attempts == MAX_JOB_ATTEMPTS - 1 and not missed_alerts(world)  # nothing before the last try
        row = await run_attempts(sched, world, 23, attempts=1, minute=40)
        assert row.attempts == MAX_JOB_ATTEMPTS and row.status is JobStatus.FAILED
        assert row.detail.get("reason") not in (None, "close_equity_unverified")  # a distinct cause code
        (msg,) = missed_alerts(world)
        assert "PDD" in msg and "1" in msg.replace("2026", "")
        await run_attempts(sched, world, 23, attempts=2, minute=59)  # attempts exhausted: no rerun, no repeat
        assert len(missed_alerts(world)) == 1

        world.clock.set_et(2026, 11, 24, 16, 5)
        world.alpaca.set_price("PDD", 0.0)
        await run_attempts(sched, world, 24, attempts=MAX_JOB_ATTEMPTS)
        msgs = missed_alerts(world)
        assert len(msgs) == 2 and "2" in msgs[-1].replace("2026", "")  # second trading day in a row

    _run(world, body)


def test_final_attempt_alert_names_the_trading_block(world):
    seed_23nov(world)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        await sched.ledger.refresh("fixture")
        sched.store.set_trading_block("test: operator block")
        row = await run_attempts(sched, world, 23, attempts=MAX_JOB_ATTEMPTS)
        (msg,) = missed_alerts(world)
        assert "block" in msg.lower()
        assert row.detail.get("reason") not in (None, "close_equity_unverified")

    _run(world, body)


def test_streak_resets_after_a_recorded_close(world):
    seed_23nov(world)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        world.alpaca.set_price("PDD", 0.0)
        await run_attempts(sched, world, 23, attempts=MAX_JOB_ATTEMPTS)
        world.clock.set_et(2026, 11, 24, 16, 5)
        world.alpaca.set_price("PDD", 120.0)
        assert (await run_attempts(sched, world, 24)).status is JobStatus.DONE
        world.clock.set_et(2026, 11, 25, 16, 5)
        world.alpaca.set_price("PDD", 0.0)
        await run_attempts(sched, world, 25, attempts=MAX_JOB_ATTEMPTS)
        msgs = missed_alerts(world)
        assert len(msgs) == 2
        assert not any(f"{n} trading days" in msgs[-1] or f"{n} consecutive" in msgs[-1] for n in (2, 3))

    _run(world, body)


# --------------------------------------------------------------------------- 8. other consumers unchanged
def test_other_snapshot_quality_consumers_unchanged(world, monkeypatch):
    seed_23nov(world)

    async def body(sched):
        from stock_agent import reporting

        await run_attempts(sched, world, 23)
        snap = sched.ledger.last_snapshot()
        assert snap.quality == "indicative"  # what "indicative" means elsewhere is untouched
        assert reporting._return_equity(snap, date(2026, 11, 23), world.clock.now_et()) is None
        calls = []

        async def refresh(reason):
            calls.append(reason)
            return snap

        real_query = sched.store.query
        monkeypatch.setattr(sched.ledger, "refresh", refresh)
        monkeypatch.setattr(sched.risk, "plan_remediation",
                            lambda *a, **k: pytest.fail("delivery trims must not run on indicative marks"))
        monkeypatch.setattr(sched.store, "query", lambda q, p=(): (
            [{"activity_id": "a1", "symbol": "JPM", "ts": "2026-11-23"}] if "OPTRD" in q else real_query(q, p)))
        world.clock.set_et(2026, 11, 24, 10, 0)
        await sched.calendar.refresh()
        await sched._queue_delivery_trims(world.clock.now_et())
        assert calls  # it refreshed and then declined on quality, exactly as before

    _run(world, body)
