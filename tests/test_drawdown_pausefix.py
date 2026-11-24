"""Drawdown pause fix (rolling 252-close peak, pause capped at 21 governed closes).

Contract: rolling decision drawdown drives alert/throttle/pause; `.drawdown` and
`.high_water` stay all-time for reporting. A pause latches only on a fresh
crossing (previous decision drawdown <= 20%, new one > 20%; exactly 20% does
not latch), governs at most 21 trusted closes (the latching close not counted),
can release early after five consecutive closes strictly below 15% without
cancelling the cooldown, and never queues a crossing.

On the shipped code (4fed519) the trap, boundary, migration and flow tests
below fail: there a 20% fall from the all-time peak pauses entries until five
closes below 15%, which an all-cash account can never produce.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot
from stock_agent.risk_manager import DrawdownState, RiskControls, RiskManager, TradeIntent, TradeKind, update_drawdown

D = Decimal
NOW = datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc)
CFG = Config


# ── helpers ──────────────────────────────────────────────────────────────

def sessions(n: int, start: date = date(2025, 1, 6)) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class Path:
    """Feed trusted closes one session at a time, keeping every state."""

    def __init__(self, state: DrawdownState | None = None, start: date = date(2025, 1, 6)):
        self.state = state or DrawdownState()
        self.labels = iter(sessions(2000, start))
        self.states: list[DrawdownState] = []
        if self.state.last_session:
            # skip labels up to and including the state's last session
            for label in self.labels:
                if label > self.state.last_session:
                    self._pending = label
                    break
        else:
            self._pending = None

    def _label(self) -> str:
        if self._pending:
            label, self._pending = self._pending, None
            return label
        return next(self.labels)

    def close(self, equity, flow=0) -> DrawdownState:
        self.state = update_drawdown(self.state, self._label(), D(str(equity)),
                                     net_external_flow=D(str(flow)), config=CFG)
        self.states.append(self.state)
        return self.state

    def closes(self, equity, n: int) -> list[DrawdownState]:
        return [self.close(equity) for _ in range(n)]


def latched(first=100, low=79) -> Path:
    p = Path()
    p.close(first)
    s = p.close(low)
    assert s.paused, "fresh crossing above 20% must latch"
    return p


def cash_book(equity: float) -> BookSnapshot:
    e = D(str(equity))
    return BookSnapshot(
        as_of=NOW, run_id="pausefix", equity=e, cash=e, long_mv=D(0), short_mv=D(0), options_mv=D(0),
        last_equity=None, buying_power=e, holdings=(), legs=(), open_orders=(), diffs=(),
    )


def stock_entry(price=100, stop=".07") -> TradeIntent:
    return TradeIntent(TradeKind.BUY, "ALFA", D(0), D(price), mark_price=D(price), sector="Industrials",
                       raw_conviction=10, effective_conviction=10, stop_distance=D(stop),
                       analysis_price=D(price), signal_at=NOW, quote_at=NOW)


def option_entry() -> TradeIntent:
    return TradeIntent(TradeKind.OPTION_OPEN, "ALFA", D(1), raw_conviction=8, effective_conviction=8,
                       signal_at=NOW, option_max_loss=D(500), option_cash_debit=D(500), structure_id="opt-1")


def stock_qty(state: DrawdownState, equity: float) -> int:
    risk = RiskManager(Config(MARGIN_ENABLED=False))
    return int(risk.propose_equity_qty(cash_book(equity), stock_entry(), now=NOW,
                                       controls=RiskControls(drawdown=state)))


def reasons(state: DrawdownState, equity: float, intent: TradeIntent) -> tuple[str, ...]:
    risk = RiskManager(Config(MARGIN_ENABLED=False))
    if intent.kind == TradeKind.BUY:
        intent = replace(intent, qty=D(1))
    return risk.evaluate_trade(cash_book(equity), intent, now=NOW, controls=RiskControls(drawdown=state)).reasons


# ── 1. the trap ──────────────────────────────────────────────────────────

def test_trap_all_cash_after_21pct_fall_releases_on_21st_governed_close():
    p = latched(100, 79)
    flat = p.closes(79, 40)
    assert all(s.paused for s in flat[:20]), [s.paused for s in flat]
    assert not flat[20].paused, "pause must clear at the 21st governed close"
    assert not any(s.paused for s in flat[20:]), "a still-standing breach must not re-latch"
    assert all(s.is_throttled(CFG) for s in flat)
    assert flat[-1].drawdown == D("0.21") and flat[-1].decision_drawdown == D("0.21")


def test_trap_entries_blocked_while_paused_then_allowed_at_throttle_terms():
    p = latched(100, 79)
    flat = p.closes(79, 21)
    paused, released = flat[19], flat[20]
    assert stock_qty(paused, 79_000) == 0
    assert "drawdown_pause" in reasons(paused, 79_000, stock_entry())
    # Released: stock entries resume at half size (10% tier x 0.5 of equity, $100 shares).
    qty = stock_qty(released, 79_000)
    assert qty == int(D("0.10") * D(CFG.DRAWDOWN_THROTTLE_SCALE) * 79_000 / 100) == 39
    unthrottled = stock_qty(DrawdownState(), 79_000)
    assert qty < unthrottled
    # Options stay blocked while the throttle applies.
    assert "drawdown_pause" in reasons(released, 79_000, option_entry())


def test_pause_sessions_remaining_counts_down_to_release():
    p = latched(100, 79)
    assert p.state.pause_sessions_remaining(CFG) == CFG.DRAWDOWN_PAUSE_MAX_SESSIONS == 21
    flat = p.closes(79, 21)
    assert [s.pause_sessions_remaining(CFG) for s in flat[:20]] == list(range(20, 0, -1))
    assert flat[19].pause_sessions_governed == 20
    assert not flat[20].paused


# ── 3. boundaries ────────────────────────────────────────────────────────

def test_exactly_20pct_does_not_latch_but_throttles():
    p = Path()
    p.close(100)
    s = p.close(80)
    assert s.decision_drawdown == D("0.2")
    assert not s.paused
    assert s.is_throttled(CFG)


def test_just_above_20pct_latches():
    p = Path()
    p.close(100)
    assert p.close("79.99").paused


def test_exactly_15pct_is_throttled_inclusive_and_just_below_is_not():
    p = Path()
    p.close(100)
    assert p.close(85).is_throttled(CFG)
    assert not p.close("85.01").is_throttled(CFG)


def test_early_recovery_on_fifth_close_strictly_below_15pct():
    p = latched(100, 79)
    rec = p.closes("85.01", 5)
    assert [s.paused for s in rec] == [True, True, True, True, False]
    assert [s.recovery_closes for s in rec[:4]] == [1, 2, 3, 4]


def test_closes_at_exactly_15pct_do_not_count_toward_recovery():
    p = latched(100, 79)
    assert all(s.paused and s.recovery_closes == 0 for s in p.closes(85, 5))


def test_recovery_streak_resets_when_drawdown_returns_to_15pct():
    p = latched(100, 79)
    p.closes("85.01", 3)
    assert p.close(85).recovery_closes == 0
    rec = p.closes("85.01", 5)
    assert rec[3].paused and not rec[4].paused


def test_fresh_crossing_on_the_expiry_bar_starts_a_new_episode():
    p = latched(100, 79)
    first_start = p.state.pause_start_session
    p.closes(81, 20)                       # 19%: still paused, not recovering
    assert p.state.paused and p.state.pause_sessions_governed == 20
    s = p.close(79)                        # 21st governed close is itself a fresh crossing
    assert s.paused
    assert s.pause_start_session != first_start and s.pause_start_session == s.last_session
    assert s.pause_sessions_governed == 0
    nxt = p.closes(79, 21)
    assert nxt[19].paused and not nxt[20].paused


def test_crossing_during_an_episode_is_not_queued():
    p = latched(100, 79)
    p.close(81)
    p.close(79)                            # crossing inside the episode
    p.closes(79, 18)
    assert p.state.paused and p.state.pause_sessions_governed == 20
    s = p.close(79)
    assert not s.paused, "the in-episode crossing must not extend or re-arm the pause"
    assert not any(x.paused for x in p.closes(79, 10))


def test_crossing_after_early_release_inside_cooldown_does_not_relatch():
    p = latched(100, 79)
    p.closes(86, 5)
    assert not p.state.paused
    s = p.close(79)                        # governed close 6, cooldown still running
    assert not s.paused and s.is_throttled(CFG)
    p.closes(79, 14)                       # through governed close 20
    p.close(81)                            # 21: cooldown ends, 19%
    assert not p.state.paused
    assert p.close(79).paused, "a fresh crossing after the cooldown latches a new episode"


def test_rolling_window_slides_at_close_253():
    p = Path()
    p.close(100)                           # the peak is close 1
    window = p.closes(84, 251)             # closes 2..252: peak still inside the 252-close window
    assert window[-1].decision_drawdown == D("0.16") and window[-1].is_throttled(CFG)
    s = p.close(84)                        # close 253: the 100 peak leaves the window
    assert s.decision_drawdown == 0
    assert not s.is_throttled(CFG)
    assert s.rolling_high_water == D(84)
    assert s.drawdown == D("0.16") and s.high_water == D(100), "all-time figures are still reported"


# ── 4. migration and persistence ─────────────────────────────────────────

V1 = {"version": 1, "high_water": "100", "drawdown": "0.1", "paused": False, "recovery_closes": 0,
      "last_session": "2026-11-20"}
V2_UNPAUSED = {"version": 2, "high_water": "100", "drawdown": "0.16", "paused": False, "recovery_closes": 0,
               "last_session": "2026-11-20", "last_equity": "84000"}
V2_PAUSED = {"version": 2, "high_water": "100", "drawdown": "0.22", "paused": True, "recovery_closes": 0,
             "last_session": "2026-11-20", "last_equity": "78000"}


def test_none_record_is_empty_state():
    s = DrawdownState.from_record(None)
    assert s == DrawdownState() and not s.paused and not s.is_throttled(CFG)


def test_v1_migrates_without_loosening():
    s = DrawdownState.from_record(V1)
    assert s.drawdown == D("0.1") and s.decision_drawdown == D("0.1")
    assert s.rolling_closes == ((None, D(100)), ("2026-11-20", D(90)))
    assert not s.paused and not s.is_throttled(CFG)
    assert json.loads(json.dumps(s.to_record()))["version"] == 3


def test_v2_unpaused_throttle_preserved_on_day_one():
    s = DrawdownState.from_record(V2_UNPAUSED)
    assert s.is_throttled(CFG) and not s.paused
    assert stock_qty(s, 84_000) < stock_qty(DrawdownState(), 84_000)


def test_v2_paused_starts_a_21_session_episode_at_migration():
    s = DrawdownState.from_record(V2_PAUSED)
    assert s.paused and s.pause_start_session == "2026-11-20" and s.pause_sessions_governed == 0
    assert s.pause_sessions_remaining(CFG) == 21
    p = Path(s, start=date(2026, 11, 20))
    flat = p.closes(78_000, 21)
    assert all(x.paused for x in flat[:20]) and not flat[20].paused


def test_v2_paused_recovery_streak_is_preserved():
    s = DrawdownState.from_record({**V2_PAUSED, "drawdown": "0.21", "recovery_closes": 0})
    p = Path(s, start=date(2026, 11, 20))
    # adjusted index continues from 79 (100 x (1-0.21)); raw equity 78,000 maps to it
    rec = [p.close(D(78_000) * D("0.8501") / D("0.79")) for _ in range(5)]
    assert [x.paused for x in rec] == [True, True, True, True, False]


def test_migrated_synthetic_peak_ages_out_of_the_window():
    p = Path(DrawdownState.from_record(V1), start=date(2026, 11, 20))
    # window = synthetic 100 + real 90 + 250 more = 252 closes
    states = p.closes(90, 250)
    assert states[-1].decision_drawdown == D("0.1")
    assert p.close(90).decision_drawdown == 0


@pytest.mark.parametrize("record", [
    {"version": 3},
    {"version": 99, "high_water": "100"},
    {"version": "2", "high_water": "100", "drawdown": "0.1", "paused": False, "recovery_closes": 0,
     "last_session": "2026-11-20"},
    {**V2_PAUSED, "paused": "yes"},
    {**V1, "drawdown": "1.5"},
    "not a record",
])
def test_corrupt_records_fail_closed(record):
    with pytest.raises(ValueError):
        DrawdownState.from_record(record)


def test_corrupt_v3_record_fails_closed():
    p = latched(100, 79)
    p.closes(79, 3)
    good = p.state.to_record()
    for bad in ({**good, "pause_sessions_governed": -1},
                {**good, "rolling_closes": [["2025-01-08", "79"], ["2025-01-07", "100"]]},
                {**good, "rolling_closes": "x"},
                {**good, "pause_start_session": "not-a-date"}):
        with pytest.raises(ValueError):
            DrawdownState.from_record(bad)


def test_v3_round_trip_and_restart_mid_episode_matches_uninterrupted_run():
    straight = latched(100, 79)
    straight.closes(79, 25)

    p = latched(100, 79)
    p.closes(79, 10)
    restored = DrawdownState.from_record(json.loads(json.dumps(p.state.to_record())))
    assert restored == p.state
    resumed = Path(restored, start=date.fromisoformat(restored.last_session))
    resumed.closes(79, 15)
    assert resumed.state == straight.state
    assert not resumed.state.paused


def test_v3_unpaused_above_20pct_after_timeout_is_a_legal_record():
    p = latched(100, 79)
    p.closes(79, 21)
    assert not p.state.paused and p.state.decision_drawdown > D("0.2")
    assert DrawdownState.from_record(p.state.to_record()) == p.state


def test_replayed_session_is_idempotent_and_does_not_advance_the_episode():
    p = latched(100, 79)
    p.closes(79, 3)
    s = p.state
    again = update_drawdown(s, s.last_session, D(79), config=CFG)
    assert again == s and again.pause_sessions_governed == 3
    older = update_drawdown(s, "2025-01-06", D(50), config=CFG)
    assert older == s


# ── 5. flows ─────────────────────────────────────────────────────────────

def test_deposit_during_an_episode_neither_clears_nor_relatches():
    p = latched(100, 79)
    s = p.close(99, flow=20)                    # +20 deposit, market flat
    assert s.paused and s.decision_drawdown == D("0.21")
    assert s.rolling_closes[-1][1] == D(79)     # window stores the flow-adjusted index
    rest = [p.close(99) for _ in range(20)]
    assert rest[18].paused and not rest[19].paused


def test_withdrawal_during_an_episode_is_not_a_new_loss():
    p = latched(100, 79)
    s = p.close(69, flow=-10)                   # -10 withdrawal, market flat
    assert s.paused and s.decision_drawdown == D("0.21")
    rest = [p.close(69) for _ in range(20)]
    assert rest[18].paused and not rest[19].paused
    assert not any(x.paused for x in [p.close(69) for _ in range(5)])


def test_deposit_after_release_does_not_hide_a_fresh_crossing():
    p = latched(100, 79)
    p.closes(81, 20)
    p.close(81)                                  # 21st: clears at 19%
    assert not p.state.paused
    p.closes(81, 1)
    s = p.close(D(81) * D("0.97") + 50, flow=50) # market -3% (to 21.4%), deposit 50
    assert s.paused
