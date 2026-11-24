"""Entry-path readers of DrawdownState after the pause-cap fix (FOLLOWUP_BRIEF Stream A).

The execution engine reads the persisted drawdown record itself when no controls
callable is wired (execution.py ``_risk_controls``); the options manager judges
the throttle from whatever RiskControls it is handed. These tests drive the real
``update_drawdown`` and check, end to end on the fake broker, that
- a stock buy is refused while a pause is latched,
- a stock buy is allowed (at throttle size) once the 21-session cap clears it,
- an expired, unpaused record above 20% is readable (no new can-never-buy path),
- option entries stay blocked whenever the throttle applies, paused or not,
- a corrupt record still fails closed.
"""
import asyncio
from datetime import date, timedelta
from decimal import Decimal

from stock_agent.config import Config
from stock_agent.ledger import IntentStatus
from stock_agent.options_manager import _drawdown_halts_options
from stock_agent.research_schemas import Direction
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState, RiskControls, update_drawdown
from tests.exec_helpers import enqueue, make_engine
from tests.options_support import StubGate, make_manager, seed_chain

D = Decimal


def run(c):
    return asyncio.run(c)


def _sessions(n, start=date(2026, 3, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _series(closes, config=Config):
    """States after each close, fed through the production rule."""
    st, out = DrawdownState(), []
    for day, c in zip(_sessions(len(closes)), closes):
        st = update_drawdown(st, day, D(str(c)), config=config)
        out.append(st)
    return out


def _trap(flat=40):
    """Brief test 1: 100 -> 79, then flat closes."""
    return _series([100, 79] + [79] * flat)


def _released_index(states):
    return next(i for i, s in enumerate(states) if i > 1 and not s.paused)


def _buy(world, tmp_path, monkeypatch, record, symbol="AAA"):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price(symbol, 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    if record is not None:
        eng.ledger.risk_state_set(DRAWDOWN_STATE_KEY, record)
    it = enqueue(eng, symbol, "buy", ref=50)
    return eng, run(eng.execute_intent(it))


# ---------------------------------------------------------------- the rule as the readers see it
def test_trap_series_releases_at_21st_governed_close_and_stays_throttled():
    states = _trap()
    assert states[1].paused  # 21% fresh crossing latches
    i = _released_index(states)
    assert i == 1 + Config.DRAWDOWN_PAUSE_MAX_SESSIONS  # 21 governed closes after the latch
    assert all(s.paused for s in states[1:i])
    after = states[i:]
    # a standing breach does not re-latch; the throttle (half size, no borrowing, no options) stays
    assert not any(s.paused for s in after)
    assert all(s.is_throttled(Config) for s in after)


def test_exactly_twenty_percent_does_not_pause_but_throttles():
    st = _series([100, 80])[-1]
    assert not st.paused and st.is_throttled(Config)


# ---------------------------------------------------------------- execution.py reader (stocks)
def test_stock_buy_refused_while_pause_latched(world, tmp_path, monkeypatch):
    eng, res = _buy(world, tmp_path, monkeypatch, _trap()[5].to_record())
    assert res.status == IntentStatus.SKIPPED and res.code == "risk_size_zero", res
    assert "drawdown_pause" in res.reason
    world.assert_no_orders()


def test_stock_buy_allowed_at_throttle_size_after_pause_expires(world, tmp_path, monkeypatch):
    states = _trap()
    released = states[_released_index(states) + 3]
    base_dir, dd_dir = tmp_path / "base", tmp_path / "dd"
    base_dir.mkdir()
    dd_dir.mkdir()
    _, base = _buy(world, base_dir, monkeypatch, None)
    assert base.status == IntentStatus.FILLED, base
    eng, res = _buy(world, dd_dir, monkeypatch, released.to_record(), symbol="BBB")
    controls = eng._risk_controls()
    assert not controls.drawdown.paused and controls.drawdown.is_throttled(eng.config)
    assert res.status == IntentStatus.FILLED, res
    assert 1 <= res.filled_qty < base.filled_qty  # throttle scale applies


def test_expired_unpaused_record_above_pause_is_readable(world, tmp_path, monkeypatch):
    rec = _trap()[-1].to_record()
    eng = make_engine(world, tmp_path, monkeypatch)
    eng.ledger.risk_state_set(DRAWDOWN_STATE_KEY, rec)
    dd = eng._risk_controls().drawdown
    assert dd.drawdown > D("0.20") and not dd.paused
    assert DrawdownState.from_record(rec, config=Config) == dd


def test_corrupt_record_still_fails_closed_for_stock_buys(world, tmp_path, monkeypatch):
    rec = dict(_trap()[5].to_record())
    rec["paused"] = "yes"
    _, res = _buy(world, tmp_path, monkeypatch, rec)
    assert res.status == IntentStatus.SKIPPED and res.code == "risk_controls_unavailable", res
    world.assert_no_orders()


# ---------------------------------------------------------------- options_manager reader
def test_option_halt_follows_the_throttle_not_the_pause_latch():
    states = _trap()
    i = _released_index(states)
    assert _drawdown_halts_options(RiskControls(drawdown=states[5]), Config)  # paused
    assert _drawdown_halts_options(RiskControls(drawdown=states[i]), Config)  # released, still >=15%
    assert _drawdown_halts_options(RiskControls(drawdown=_series([100, 80])[-1]), Config)
    assert not _drawdown_halts_options(RiskControls(drawdown=_series([100, 90])[-1]), Config)
    assert not _drawdown_halts_options(RiskControls(), Config)


def test_option_halt_uses_running_config_thresholds():
    class Tight(Config):
        DRAWDOWN_ALERT_PCT = .03
        DRAWDOWN_THROTTLE_PCT = .05
        DRAWDOWN_PAUSE_PCT = .12

    st = _series([100, 94], config=Tight)[-1]
    assert _drawdown_halts_options(RiskControls(drawdown=st), Tight)
    assert not _drawdown_halts_options(RiskControls(drawdown=st), Config)


def _scan_with(world, tmp_path, drawdown):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    m._risk_controls = lambda: RiskControls(drawdown=drawdown)
    return run(m.scan())


def _mleg_posts(world):
    return [r for r in world.orders_sent() if r.json and r.json.get("order_class") == "mleg"]


def test_option_entry_blocked_after_pause_expires_while_throttled(world, tmp_path):
    states = _trap()
    out = _scan_with(world, tmp_path, states[_released_index(states)])
    assert out[0].opened is None
    assert not _mleg_posts(world)


def test_option_entry_opens_when_rolling_drawdown_below_throttle(world, tmp_path):
    out = _scan_with(world, tmp_path, _series([100, 90])[-1])
    assert out[0].opened, out
