"""Honest morning brief, daily close summary, and weekly paper-test scorecard.

Design rules (DECISIONS & PAPER_TEST):
- Portfolio equity, cash, positions, and options come strictly from reconciled
  ``BookSnapshot``, ``ledger.equity_marks('close')``, and ``ledger.cash_flows()``.
  NEVER use Alpaca ``account.last_equity`` or ``portfolio/history`` (both omit a
  held position on this account).
- Benchmark comparison against ``QQQ`` and ``SPY`` uses ``BenchmarkTracker``
  (split-adjusted daily close bars from ``PriceService.get_daily_bars``).
- Options P&L and modeled max loss are always reported separately from stocks,
  and news-origin options are shown as an origin breakdown of ``OPTIONS`` without
  double-counting in ``NEWS_EQUITY``.
- Plain sentences for a non-technical owner: no raw tracebacks, no unexplained
  acronyms, and ``N/A`` (never ``0``) whenever a shadow or statistical series
  has no mature observations yet.
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_agent.alerts import format_entry_lock_line, redact_secrets_in_text
from stock_agent.benchmark import MIN_PAIRED_IR, BenchmarkSnapshot, BenchmarkTracker
from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, EquityMark, Holding, Ledger
from stock_agent.logging_setup import redact_text
from stock_agent.performance import (
    InterestSeries,
    margin_interest,
    margin_rate,
    SHADOW_POST_STOP_SESSIONS,
    day_pnl,
    drawdown,
    net_deposits,
    previous_close,
    realised_fifo,
    twr,
)
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")
ZERO = Decimal("0")


def _default_now() -> datetime:
    sched = sys.modules.get("stock_agent.scheduler")
    if sched is not None and hasattr(sched, "_now_utc"):
        return sched._now_utc()
    return datetime.now(timezone.utc)


def _fmt_money(val: Decimal | float | None, *, signed: bool = False, decimals: int = 0) -> str:
    if val is None:
        return "N/A"
    num = float(val)
    if signed:
        sign = "+" if num >= 0 else "-"
        return f"{sign}${abs(num):,.{decimals}f}"
    if num < 0:
        return f"-${abs(num):,.{decimals}f}"
    return f"${num:,.{decimals}f}"


def _fmt_pct(val: Decimal | float | None, *, signed: bool = True) -> str:
    if val is None:
        return "N/A"
    num = float(val)
    return f"{num:+.2f}%" if signed else f"{num:.2f}%"


def _safe_dec(val: Any) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        d = Decimal(str(val))
        return d if d.is_finite() else None
    except Exception:
        return None


def _safe_report_reason(exc: BaseException, config: Any | None = None) -> str:
    """Return a short, secret-scrubbed reason suitable for an owner fallback message."""
    raw = str(exc).strip()
    secrets = (
        config.secret_values()
        if config is not None and hasattr(config, "secret_values")
        else ()
    )
    cleaned = redact_text(raw, secrets) if raw else ""
    cleaned = redact_secrets_in_text(cleaned).strip()
    label = f"{type(exc).__name__}: {cleaned}" if cleaned else type(exc).__name__
    return label[:180]


@dataclass(frozen=True)
class _DrawdownReportSnapshot:
    decision_dd_pct: Decimal | None
    rolling_hwm: Decimal | None
    all_time_dd_pct: Decimal | None
    all_time_hwm: Decimal | None
    tier: str
    label: str
    paused: bool
    pause_sessions_remaining: int
    rolling_window_sessions: int


def _read_persisted_drawdown_checked(
    ledger: Any | None,
    config: Any = Config,
) -> tuple[DrawdownState | None, bool]:
    """Read ``risk_state[DRAWDOWN_STATE_KEY]``, returning ``(state, corrupt)``."""
    if ledger is None or not hasattr(ledger, "risk_state_get"):
        return None, False
    try:
        raw = ledger.risk_state_get(DRAWDOWN_STATE_KEY)
    except Exception:
        return None, True
    if raw is None:
        return None, False
    if not isinstance(raw, Mapping):
        return None, True
    try:
        st = DrawdownState.from_record(raw, config=config)
    except Exception:
        return None, True
    if (
        st.last_session is None
        and not st.paused
        and st.high_water <= ZERO
        and st.drawdown <= ZERO
        and st.decision_drawdown <= ZERO
        and not st.rolling_closes
    ):
        return None, False
    return st, False


def _read_persisted_drawdown(ledger: Any | None, config: Any = Config) -> DrawdownState | None:
    """Read the persisted DrawdownState record from risk_state[DRAWDOWN_STATE_KEY] if present."""
    st, _ = _read_persisted_drawdown_checked(ledger, config=config)
    return st


def _format_pause_lift_condition(st: DrawdownState, config: Any = Config) -> str:
    """Plain-English condition explaining both ways an active drawdown pause lifts."""
    rem = st.pause_sessions_remaining(config=config)
    close_lbl = "close is" if rem == 1 else "closes are"
    thr_pct = float(getattr(config, "DRAWDOWN_THROTTLE_PCT", 0.15)) * 100.0
    rec_target = int(getattr(config, "DRAWDOWN_RECOVERY_SESSIONS", 5))
    return (
        f"{rem} more trusted session {close_lbl} recorded, or rolling drawdown stays below "
        f"{thr_pct:.0f}% for {rec_target} consecutive closes ({st.recovery_closes}/{rec_target} recovery closes recorded)"
    )


def describe_drawdown_tier(
    drawdown_pct: Decimal | float | None,
    ledger: Ledger | None = None,
    *,
    config: Any = Config,
    persisted: DrawdownState | None = None,
) -> tuple[str, str]:
    """Map persisted drawdown state/latch (or fallback drawdown %) to control tier and label."""
    st = persisted if persisted is not None else _read_persisted_drawdown(ledger, config=config)
    rec_target = int(getattr(config, "DRAWDOWN_RECOVERY_SESSIONS", 5))
    max_pause_sessions = int(getattr(config, "DRAWDOWN_PAUSE_MAX_SESSIONS", 21))
    thr_pct = float(getattr(config, "DRAWDOWN_THROTTLE_PCT", 0.15)) * 100.0
    pause_frac = Decimal(str(getattr(config, "DRAWDOWN_PAUSE_PCT", "0.20")))
    if st is not None:
        if st.paused:
            lift_cond = _format_pause_lift_condition(st, config=config)
            return (
                "halt_20pct",
                f"20% Drawdown Stop — new entries paused; lifts when {lift_cond}",
            )
        if st.is_throttled(config=config):
            cap_note = (
                f" ({max_pause_sessions}-session pause cap elapsed; fresh crossing above {float(pause_frac) * 100:.0f}% required to re-pause)"
                if st.decision_drawdown > pause_frac
                else ""
            )
            return (
                "throttle_15pct",
                f"15% Drawdown Guard{cap_note} — new stock sizes cut in half, no new margin borrowing, and new option trades paused",
            )
        if st.should_alert(config=config):
            return (
                "alert_10pct",
                "10% Drawdown Watch — normal rules active with heightened monitoring",
            )
        return ("normal", "Normal — all safety rules clear")

    halted = False
    if ledger is not None and hasattr(ledger, "risk_state_get"):
        try:
            halted = bool(ledger.risk_state_get("drawdown_halted", False))
        except Exception:
            halted = False
    dd = float(drawdown_pct) if drawdown_pct is not None else 0.0
    if halted or dd < -float(pause_frac) * 100.0:
        return (
            "halt_20pct",
            f"20% Drawdown Stop — new entries paused for up to {max_pause_sessions} sessions or until rolling drawdown stays below {thr_pct:.0f}% for {rec_target} closes",
        )
    if dd <= -thr_pct:
        return (
            "throttle_15pct",
            "15% Drawdown Guard — new stock sizes cut in half, no new margin borrowing, and new option trades paused",
        )
    if dd <= -float(getattr(config, "DRAWDOWN_ALERT_PCT", 0.10)) * 100.0:
        return (
            "alert_10pct",
            "10% Drawdown Watch — normal rules active with heightened monitoring",
        )
    return ("normal", "Normal — all safety rules clear")


def format_input_health_table(health_registry: Any | None) -> tuple[str, list[dict[str, Any]]]:
    """Build a plain-English data-source health summary and structured rows."""
    if health_registry is None:
        return ("Data-source health monitor not connected.", [])

    rows: list[dict[str, Any]] = []
    try:
        if hasattr(health_registry, "summary_rows"):
            rows = list(health_registry.summary_rows() or [])
        elif hasattr(health_registry, "get_input_health"):
            statuses = health_registry.get_input_health() or {}
            rows = [
                st.to_dict() if hasattr(st, "to_dict") else dict(st)
                for st in statuses.values()
            ]
    except Exception as e:
        logger.warning("Could not read input health rows (%s)", type(e).__name__)
        return ("Could not read data-source health status.", [])

    if not rows:
        return ("All configured data feeds initialised; awaiting first check.", [])

    icon_map = {
        "healthy": "✅",
        "degraded": "⚠️",
        "unhealthy": "🚨",
        "disabled": "⏸️",
    }
    lines: list[str] = []
    for r in rows:
        prov = str(r.get("provider", "?"))
        cap = str(r.get("capability", "feed")).replace("_", " ")
        state = str(r.get("state", "unknown")).lower()
        req = bool(r.get("required_for_trading", False))
        err = redact_secrets_in_text(str(r.get("last_error_summary") or "")).strip()
        if state == "disabled" and not req:
            continue
        icon = icon_map.get(state, "•")
        req_tag = " (required)" if req else " (backup)"
        note = f" — {err[:90]}" if err and state in ("degraded", "unhealthy") else ""
        lines.append(f"{icon} **{prov} {cap}**{req_tag}: {state}{note}")

    if not lines:
        return ("All active data sources are healthy.", rows)
    return ("\n".join(lines[:16]), rows)


def _holding_stop_status(h: Holding, snap: BookSnapshot) -> tuple[str, bool]:
    """Return plain-English stop status and whether the long position is fully covered."""
    if h.is_short:
        return ("🚨 Unexpected short (buys blocked)", False)
    if h.qty <= ZERO:
        return ("—", True)

    open_stops = [
        o
        for o in snap.open_orders_for(h.symbol)
        if o.side == "sell" and ("stop" in o.type.lower() or o.stop_price is not None)
    ]
    covered_qty = sum((o.remaining_qty for o in open_stops), ZERO)
    stop_px = open_stops[0].stop_price if open_stops else (h.meta.stop_price if h.meta else None)

    whole_qty = h.qty.to_integral_value(rounding="ROUND_FLOOR")
    if covered_qty >= h.qty and stop_px is not None:
        return (f"✅ Stop @ ${float(stop_px):,.2f}", True)
    if whole_qty > ZERO and covered_qty >= whole_qty and h.is_fractional and stop_px is not None:
        frac = h.qty - whole_qty
        return (f"✅ Stop @ ${float(stop_px):,.2f} ({float(frac):.3f} fractional sh uncovered)", True)
    if covered_qty > ZERO and stop_px is not None:
        return (f"⚠️ Partial stop ({float(covered_qty):g}/{float(h.qty):g} sh @ ${float(stop_px):,.2f})", False)
    if h.is_fractional and whole_qty == ZERO:
        return ("ℹ️ Sub-1 fractional share (GTC stop unsupported; watched live)", True)
    return ("🚨 MISSING BROKER STOP", False)


def _parse_ts(x: Any) -> datetime | None:
    """ISO timestamp from SQLite/journal rows -> aware datetime (naive treated as UTC)."""
    if x is None or x == "":
        return None
    try:
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


SNAPSHOT_MAX_AGE = timedelta(minutes=90)


def _return_equity(snap: Any, session_date: date, now: datetime, *,
                   not_before: datetime | None = None) -> Decimal | None:
    """Equity usable as a return/drawdown terminal for ``session_date``, else None.

    Only a live (fully marked) snapshot taken during ``session_date`` (ET), no older
    than SNAPSHOT_MAX_AGE at report time, and (for the close summary) taken at or after
    ``not_before``. A cached snapshot from an earlier session is never today's value."""
    if snap is None or str(getattr(snap, "quality", "")) != "live":
        return None
    taken = getattr(snap, "as_of", None)
    if not isinstance(taken, datetime):
        return None
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    if taken.astimezone(ET).date() != session_date:
        return None
    age = now - taken
    if age < timedelta(minutes=-5) or age > SNAPSHOT_MAX_AGE:
        return None
    if not_before is not None and taken < not_before:
        return None
    return snap.equity


class ReportBuilder:
    """Builds honest morning briefs, daily summaries, and weekly scorecards."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        ledger: Ledger | None = None,
        price_service: Any | None = None,
        health_registry: Any | None = None,
        options_store: Any | None = None,
        calendar: Any | None = None,
        research_store: Any | None = None,
        benchmark_tracker: BenchmarkTracker | None = None,
        clock: Any | None = None,
    ) -> None:
        self.config = config or Config()
        self.ledger = ledger
        self.price_service = price_service
        self.health_registry = health_registry
        self.options_store = options_store
        self._options_store_failed = False
        self.calendar = calendar
        self.research_store = research_store
        self.benchmark = benchmark_tracker or BenchmarkTracker(price_service)
        self._clock = clock or _default_now

    # ------------------------------------------------------- margin (A13j)
    def _interest_series(self, marks: Sequence[EquityMark]) -> InterestSeries | None:
        if self.ledger is None or not hasattr(self.ledger, "store"):
            return None
        try:
            acts = [json.loads(r[0]) for r in self.ledger.store.query(
                "SELECT raw_json FROM broker_activities WHERE activity_type IN ('INT','INTNRA','INTTW')")]
            return margin_interest(marks, acts, rate=margin_rate(self.config))
        except Exception as e:  # reporting must not die on the cost estimate
            logger.warning("Margin interest series unavailable (%s)", type(e).__name__)
            return None

    def _margin_dict(self, snap: BookSnapshot | None, bm: BenchmarkSnapshot,
                     interest: InterestSeries | None, session_date: date) -> dict[str, Any]:
        def f(x: Decimal | None, nd: int = 2) -> float | None:
            return round(float(x), nd) if x is not None else None

        excess = snap.margin_excess_pct if snap is not None else None
        week_start = session_date - timedelta(days=7)
        out: dict[str, Any] = {
            "borrowed": f(snap.borrowed) if snap is not None else None,
            "leverage_now": f(snap.leverage, 3) if snap is not None else None,
            "avg_leverage_week": f(bm.avg_leverage_week, 3),
            "avg_leverage_since_launch": f(bm.avg_leverage_since_launch, 3),
            "margin_excess_pct": f(excess * 100) if excess is not None else None,
            "regt_buying_power": f(snap.regt_buying_power) if snap is not None else None,
            "interest_rate_pct": f(interest.rate * 100, 3) if interest is not None else None,
            "interest_basis": interest.basis if interest is not None else "none",
            "interest_accrued_unposted_est": f(interest.unposted(session_date)) if interest is not None else None,
            "interest_cost_week": f(interest.period_cost(week_start, session_date)) if interest is not None else None,
            "interest_posted_total": f(interest.actual_cost(None, session_date)) if interest is not None else None,
            "session_after_cost_pct": f(bm.portfolio_session_after_cost_pct),
            "week_after_cost_pct": f(bm.portfolio_week_after_cost_pct),
            "since_launch_after_cost_pct": f(bm.portfolio_since_launch_after_cost_pct),
        }
        out["display"] = self._margin_lines(snap, bm, interest, session_date, include_week=True)
        return out

    def _margin_lines(self, snap: BookSnapshot | None, bm: BenchmarkSnapshot, interest: InterestSeries | None,
                      session_date: date, *, include_week: bool = False) -> str:
        """Borrowed amount, leverage (now and average), margin excess and returns after
        ESTIMATED borrowing cost. Broker-reported equity is never adjusted."""
        if snap is None:
            return "N/A (no verified account snapshot)"

        def lev(x: Decimal | None) -> str:
            return f"{float(x):.2f}x" if x is not None else "N/A"

        avg = bm.avg_leverage_week if include_week else bm.avg_leverage_since_launch
        avg_lbl = "this week" if include_week else "since launch"
        excess = snap.margin_excess_pct
        lines = [
            f"Borrowed {_fmt_money(snap.borrowed)} | Leverage {lev(snap.leverage)} now, avg {lev(avg)} {avg_lbl} "
            "(QQQ/SPY are unlevered)",
            "Margin excess: " + (f"{float(excess) * 100:.1f}% of equity (equity above broker maintenance)"
                                 if excess is not None else "N/A (broker maintenance figure not reported)"),
        ]
        if interest is not None:
            rate = f"{float(interest.rate) * 100:.2f}%/yr"
            lines.append(
                f"Interest: est. accrued not yet posted {_fmt_money(interest.unposted(session_date), decimals=2)} "
                f"({rate}, /360) | posted by broker {_fmt_money(interest.actual_cost(None, session_date), decimals=2)}")
            parts = [f"today {_fmt_pct(bm.portfolio_session_after_cost_pct)}"]
            if include_week:
                parts.append(f"week {_fmt_pct(bm.portfolio_week_after_cost_pct)}")
            parts.append(f"since launch {_fmt_pct(bm.portfolio_since_launch_after_cost_pct)}")
            lines.append("After est. borrowing cost: " + " | ".join(parts))
        return "\n".join(lines)

    def _session_close(self, session_date: date) -> datetime:
        """Regular close of ``session_date`` (market calendar when covered, else 16:00 ET)."""
        cal = self.calendar
        if cal is not None and hasattr(cal, "session_on"):
            try:
                sess = cal.session_on(session_date)
                if sess is not None and getattr(sess, "closes_at", None) is not None:
                    return sess.closes_at
            except Exception:
                pass
        return datetime.combine(session_date, time(16, 0), tzinfo=ET)

    def _now_et(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(ET)

    def _load_options_structures(self) -> list[Any]:
        self._options_store_failed = False
        if self.options_store is None:
            return []
        try:
            if hasattr(self.options_store, "load"):
                loaded = self.options_store.load()
                if isinstance(loaded, tuple) and loaded and isinstance(loaded[0], list):
                    return list(loaded[0])
                if isinstance(loaded, list):
                    return list(loaded)
                structs = getattr(loaded, "structures", None)
                if isinstance(structs, dict):
                    return list(structs.values())
                if isinstance(structs, list):
                    return list(structs)
        except Exception as e:
            logger.warning("Could not read options structures for report (%s)", type(e).__name__)
            self._options_store_failed = True
        return []

    # ---- options helpers (structure maths lives in options_book)
    @staticmethod
    def _option_origin(s: Any) -> str:
        src = str(getattr(s, "source", "") or "")
        if src == "adopted":
            return "ADOPTED"
        return "NEWS" if src == "news" else "SCHEDULED"

    @staticmethod
    def _option_origin_label(s: Any) -> str:
        return {"ADOPTED": "adopted/legacy", "NEWS": "news"}.get(ReportBuilder._option_origin(s), "scheduled")

    @staticmethod
    def _option_mismatch(structs: Sequence[Any], snap: BookSnapshot | None) -> set[str]:
        """Contract symbols where the broker's signed quantity differs from what the live
        structures in the book expect (book lag, repair, store failure, or unbooked legs)."""
        expected: dict[str, float] = defaultdict(float)
        for s in structs:
            if getattr(s, "st", None) is None or not s.st.live or str(getattr(s, "state", "")) == "pending":
                continue
            try:
                for sym, q in s.expected_positions().items():
                    expected[sym] += q
            except Exception:
                for leg in getattr(s, "legs", ()) or ():
                    expected[getattr(leg, "symbol", "")] = float("nan")
        held = {leg.occ_symbol: float(leg.qty) for leg in snap.legs} if snap is not None else {}
        if snap is None:
            return set()
        return {sym for sym in set(expected) | set(held) if expected.get(sym, 0.0) != held.get(sym, 0.0)}

    @classmethod
    def _option_marks(cls, snap: BookSnapshot | None, structs: Sequence[Any] = ()) -> dict[str, float]:
        """Per-share marks for legs whose held quantity matches the book; a mismatched
        symbol has no mark, so its structures report P&L as unknown."""
        if snap is None:
            return {}
        bad = cls._option_mismatch(structs, snap)
        return {leg.occ_symbol: float(leg.price) for leg in snap.legs
                if leg.price_ok and leg.price is not None and leg.occ_symbol not in bad}

    def _option_risk(self, structs: Sequence[Any], snap: BookSnapshot | None = None) -> tuple[float, int]:
        """(sum of known max loss, count of positions whose max loss is unknown).
        Unknown counts live structures without a max loss plus each underlying whose
        broker legs the book does not explain. Unknown risk is never counted as zero."""
        total, unknown = 0.0, 0
        for s in structs:
            ml = getattr(s, "max_loss", None)
            if ml is None:
                unknown += 1
            else:
                total += float(ml)
        if snap is not None and snap.legs:
            bad = self._option_mismatch(structs, snap)
            held_bad = {leg.underlying for leg in snap.legs if leg.occ_symbol in bad}
            if self._options_store_failed:
                held_bad |= {leg.underlying for leg in snap.legs}
            unknown += len(held_bad)
        return total, unknown

    @staticmethod
    def _fmt_option_risk(total: float, unknown: int) -> str:
        if not unknown:
            return _fmt_money(total)
        if total <= 0:
            return f"unknown ({unknown} structure(s); no known maximum)"
        return f"{_fmt_money(total)} known + {unknown} structure(s) with unknown max loss"

    def _option_total_limit_pct(self) -> float:
        try:
            from stock_agent.options_settings import load_options_settings

            return float(load_options_settings(self.config).max_loss_total_pct) * 100.0
        except Exception:
            return 10.0

    def _fallback_starting_capital(self) -> Decimal:
        raw = getattr(self.config, "STARTING_CAPITAL", None)
        d = _safe_dec(raw)
        return d if d and d > ZERO else Decimal("100000")

    def _resolve_cutover_date(self, explicit: date | None, marks: Sequence[EquityMark]) -> date | None:
        if explicit is not None:
            return explicit
        for attr in ("CUTOVER_DATE", "REVAMP_CUTOVER_DATE"):
            val = getattr(self.config, attr, None)
            if isinstance(val, date):
                return val
            if isinstance(val, str) and val.strip():
                try:
                    return date.fromisoformat(val.strip()[:10])
                except ValueError:
                    pass
        if self.ledger is not None and hasattr(self.ledger, "risk_state_get"):
            try:
                stored = self.ledger.risk_state_get("cutover_date")
                if isinstance(stored, str) and stored.strip():
                    return date.fromisoformat(stored.strip()[:10])
            except Exception:
                pass
        close_dates = sorted(m.session_date for m in marks if m.quality == "close")
        return close_dates[0] if close_dates else None

    def _drawdown_snapshot(
        self,
        marks: Sequence[EquityMark],
        ret_eq: Decimal | None,
    ) -> _DrawdownReportSnapshot:
        """Read persisted DrawdownState (both rolling decision & all-time); fall back to marks."""
        rolling_window = int(getattr(self.config, "DRAWDOWN_ROLLING_SESSIONS", 252))
        st = _read_persisted_drawdown(self.ledger, config=self.config)
        calc_dd, calc_hwm = drawdown(marks, ret_eq)
        if st is not None:
            all_time_dd_pct = (-st.drawdown * Decimal(100)).quantize(Decimal("0.01"))
            decision_dd_pct = (-st.decision_drawdown * Decimal(100)).quantize(Decimal("0.01"))
            all_time_hwm = st.high_water if st.high_water > ZERO else calc_hwm
            rolling_hwm = st.rolling_high_water if st.rolling_high_water > ZERO else all_time_hwm
            tier, label = describe_drawdown_tier(
                decision_dd_pct,
                self.ledger,
                config=self.config,
                persisted=st,
            )
            return _DrawdownReportSnapshot(
                decision_dd_pct=decision_dd_pct,
                rolling_hwm=rolling_hwm,
                all_time_dd_pct=all_time_dd_pct,
                all_time_hwm=all_time_hwm,
                tier=tier,
                label=label,
                paused=bool(st.paused),
                pause_sessions_remaining=st.pause_sessions_remaining(config=self.config),
                rolling_window_sessions=rolling_window,
            )

        close_marks = sorted((m for m in marks if m.quality == "close"), key=lambda m: m.session_date)
        rolling_marks = close_marks[-rolling_window:] if len(close_marks) > rolling_window else close_marks
        roll_dd, roll_hwm = drawdown(rolling_marks, ret_eq) if rolling_marks or ret_eq is not None else (calc_dd, calc_hwm)
        decision_dd_pct = roll_dd if roll_dd is not None else calc_dd
        rolling_hwm = roll_hwm if roll_hwm is not None else calc_hwm
        tier, label = describe_drawdown_tier(decision_dd_pct, self.ledger, config=self.config)
        return _DrawdownReportSnapshot(
            decision_dd_pct=decision_dd_pct,
            rolling_hwm=rolling_hwm,
            all_time_dd_pct=calc_dd,
            all_time_hwm=calc_hwm,
            tier=tier,
            label=label,
            paused=tier == "halt_20pct",
            pause_sessions_remaining=0,
            rolling_window_sessions=rolling_window,
        )

    def _persisted_or_computed_drawdown(
        self,
        marks: Sequence[EquityMark],
        ret_eq: Decimal | None,
    ) -> tuple[Decimal | None, Decimal | None, str, str]:
        """Read persisted DrawdownState (risk_manager.DRAWDOWN_STATE_KEY) first; fall back to marks."""
        snap = self._drawdown_snapshot(marks, ret_eq)
        return snap.all_time_dd_pct, snap.all_time_hwm, snap.tier, snap.label

    @staticmethod
    def _format_dual_drawdown_detail(dd_snap: _DrawdownReportSnapshot) -> str:
        if dd_snap.all_time_dd_pct is None and dd_snap.decision_dd_pct is None:
            return ""
        roll_part = (
            f"Rolling {dd_snap.rolling_window_sessions}-session decision drawdown {_fmt_pct(dd_snap.decision_dd_pct)}"
            + (f" (window peak {_fmt_money(dd_snap.rolling_hwm)})" if dd_snap.rolling_hwm is not None else "")
        )
        all_part = (
            f"All-time fall from peak {_fmt_pct(dd_snap.all_time_dd_pct)}"
            + (f" ({_fmt_money(dd_snap.all_time_hwm)} peak)" if dd_snap.all_time_hwm is not None else "")
        )
        return f"\n{roll_part} | {all_part}"

    def _unavailable_embed(self, title: str, exc: BaseException) -> list[dict[str, Any]]:
        reason = _safe_report_reason(exc, self.config)
        logger.warning("Report degraded to unavailable (%s)", type(exc).__name__)
        return [
            {
                "title": title,
                "description": f"report unavailable: {reason}",
                "color": 0xEF4444,
            }
        ]

    def _benchmark_lines(
        self,
        bm: BenchmarkSnapshot,
        *,
        include_week: bool = False,
        include_cutover: bool = False,
        cutover_date: date | None = None,
    ) -> str:
        parts: list[str] = []
        qqq_s = _fmt_pct(bm.qqq.session_return_pct)
        spy_s = _fmt_pct(bm.spy.session_return_pct)
        port_s = _fmt_pct(bm.portfolio_session_pct)
        parts.append(f"**Today:** Portfolio {port_s} vs QQQ {qqq_s} | SPY {spy_s}")

        if include_week:
            parts.append(
                f"**This Week:** Portfolio {_fmt_pct(bm.portfolio_week_pct)} "
                f"vs QQQ {_fmt_pct(bm.qqq.week_return_pct)} | SPY {_fmt_pct(bm.spy.week_return_pct)}"
            )

        if include_cutover:
            cut_dt = cutover_date or getattr(bm, "cutover_date", None)
            cut_lbl = f" ({cut_dt.strftime('%b %d')})" if isinstance(cut_dt, date) else ""
            parts.append(
                f"**Since Cut-Over{cut_lbl}:** Portfolio {_fmt_pct(bm.portfolio_cutover_pct)} "
                f"vs QQQ {_fmt_pct(bm.qqq.cutover_return_pct)} | SPY {_fmt_pct(bm.spy.cutover_return_pct)}"
            )

        parts.append(
            f"**Since Launch (Mar 30):** Portfolio {_fmt_pct(bm.portfolio_since_launch_pct)} "
            f"vs QQQ {_fmt_pct(bm.qqq.since_launch_return_pct)} | SPY {_fmt_pct(bm.spy.since_launch_return_pct)}"
        )
        basis_note = "Basis: split-adjusted close prices (flow-adjusted portfolio return)."
        if bm.any_stale:
            reasons = ", ".join(
                f"{x.symbol}: {x.stale_reason}"
                for x in (bm.qqq, bm.spy)
                if x.stale and x.stale_reason
            )
            basis_note += f" ⚠️ Benchmark data note: {reasons or 'using latest available close'}."
        parts.append(basis_note)
        return "\n".join(parts)

    async def build_morning_brief(
        self,
        *,
        snapshot: BookSnapshot | None = None,
        as_of: datetime | None = None,
        macro_embed: dict[str, Any] | None = None,
        entry_locks: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build plain-English morning brief embeds from reconciled state; never raises."""
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        title = f"☕ Morning Brief — {now_et.strftime('%A, %B %d, %Y')}"
        try:
            return await self._build_morning_brief_inner(
                snapshot=snapshot,
                as_of=as_of,
                macro_embed=macro_embed,
                entry_locks=entry_locks,
            )
        except Exception as e:
            return self._unavailable_embed(title, e)

    async def _build_morning_brief_inner(
        self,
        *,
        snapshot: BookSnapshot | None = None,
        as_of: datetime | None = None,
        macro_embed: dict[str, Any] | None = None,
        entry_locks: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        session_date = now_et.date()
        today_label = now_et.strftime("%A, %B %d, %Y")
        sgt_label = now_et.astimezone(SGT).strftime("%I:%M %p SGT")

        snap = snapshot
        if snap is None and self.ledger is not None:
            snap = self.ledger.last_snapshot()

        if snap is None:
            return [
                {
                    "title": f"☕ Morning Brief — {today_label}",
                    "description": (
                        f"Could not read a verified account snapshot yet ({sgt_label}). "
                        "The bot will hold off on new trades until the broker connection syncs cleanly."
                    ),
                    "color": 0xEF4444,
                }
            ]

        marks = self.ledger.equity_marks("close") if self.ledger else []
        flows = self.ledger.cash_flows() if self.ledger else []
        prev_mark = previous_close(marks, session_date)
        # returns/drawdown only from a live (reconciled, marked) snapshot; an indicative
        # pre-market snapshot is shown as a value but never turned into a % change
        ret_eq = _return_equity(snap, session_date, now_et)
        overnight_pnl, overnight_pct = (
            day_pnl(marks, ret_eq, session_date, flows) if ret_eq is not None else (None, None)
        )
        dd_snap = self._drawdown_snapshot(marks, ret_eq)
        interest = self._interest_series(marks)

        bm = await self.benchmark.compute(
            as_of_date=session_date,
            marks=marks,
            flows=flows,
            live_equity=ret_eq,
            fallback_starting_capital=self._fallback_starting_capital(),
            interest=interest,
        )

        long_holdings = [h for h in snap.holdings if h.qty > ZERO]
        short_holdings = [h for h in snap.holdings if h.is_short]
        equity_f = float(snap.equity)
        cash_f = float(snap.cash)
        cash_pct = (cash_f / equity_f * 100.0) if equity_f > 0 else 0.0
        stock_pct = (float(snap.long_mv) / equity_f * 100.0) if equity_f > 0 else 0.0
        opt_mv_f = float(snap.options_mv)

        # Detect pre-market / indicative option or stock marks
        zero_or_bad_legs = [leg for leg in snap.legs if not leg.price_ok or leg.price is None or leg.price <= ZERO]
        quality_note = ""
        if snap.quality != "live" or zero_or_bad_legs:
            quality_note = (
                "\n\nℹ️ **Pre-market pricing note:** Outside 9:30 AM–4:00 PM ET, option quotes often show $0 or "
                "prior marks. Treat overnight totals as indicative until the market opens."
            )

        prev_label = (
            f"{_fmt_money(overnight_pnl, signed=True, decimals=2)} ({_fmt_pct(overnight_pct)}) vs {prev_mark.session_date.isoformat()} close"
            if prev_mark is not None and overnight_pnl is not None
            else (
                f"Not shown: snapshot is {snap.quality}, waiting for live marks"
                if prev_mark is not None
                else "First recorded session (no prior close mark yet)"
            )
        )

        # 1. Account & Benchmark Embed
        header_fields = [
            {"name": "Account Value", "value": f"**{_fmt_money(snap.equity, decimals=2)}**", "inline": True},
            {"name": "Cash Reserve" if snap.cash >= ZERO else "Cash (Debit Balance)",
             "value": f"{_fmt_money(snap.cash)} ({cash_pct:.1f}%)", "inline": True},
            {"name": "Stock Exposure", "value": f"{_fmt_money(snap.long_mv)} ({stock_pct:.1f}%)", "inline": True},
            {"name": "Options Value", "value": f"{_fmt_money(opt_mv_f, decimals=0)} ({len(snap.legs)} leg(s))", "inline": True},
            {"name": "Change Since Prior Close", "value": prev_label, "inline": True},
            {
                "name": "Stock Slots",
                "value": f"{len(long_holdings)} / {self.config.MAX_POSITIONS} long stocks",
                "inline": True,
            },
            {
                "name": "Safety & Drawdown Status",
                "value": f"{dd_snap.label}{self._format_dual_drawdown_detail(dd_snap)}",
                "inline": False,
            },
            {
                "name": "Performance vs Market (QQQ & SPY)",
                "value": self._benchmark_lines(bm),
                "inline": False,
            },
            {"name": "Borrowing & Leverage", "value": self._margin_lines(snap, bm, interest, session_date),
             "inline": False},
        ]

        embeds: list[dict[str, Any]] = [
            {
                "title": f"☕ Morning Brief — {today_label}",
                "description": (
                    f"Good {'morning' if now_et.hour < 12 else 'day'}. Here is your verified account check-in "
                    f"({sgt_label}).{quality_note}"
                ),
                "color": 0x3B82F6 if not short_holdings else 0xDC2626,
                "fields": header_fields,
            }
        ]

        # 2. Positions, Concentration Caps & Stop-Loss Protection Embed
        pos_fields: list[dict[str, Any]] = []
        if long_holdings:
            sector_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
            pos_lines: list[str] = []
            for h in sorted(long_holdings, key=lambda x: x.market_value, reverse=True):
                wt = float(h.market_value / snap.equity * 100) if snap.equity > ZERO else 0.0
                upnl = (h.price - h.avg_cost) * h.qty if (h.price is not None and h.price_ok) else None
                stop_txt, _ = _holding_stop_status(h, snap)
                cap_flag = " ⚠️ >15% cap" if wt > 15.0 else ""
                sec = (h.meta.sector if h.meta and h.meta.sector else "Unclassified")
                sector_totals[sec] += h.market_value
                px_txt = f"${float(h.price):,.2f}" if (h.price is not None and h.price_ok) else "mark unavailable"
                pos_lines.append(
                    f"• **{h.symbol}** ({float(h.qty):g} sh @ {px_txt}) — {wt:.1f}% of account{cap_flag} | "
                    f"P&L {_fmt_money(upnl, signed=True)} | {stop_txt}"
                )
            pos_fields.append(
                {"name": "Stock Holdings & Stop-Loss Coverage", "value": "\n".join(pos_lines), "inline": False}
            )
            sec_parts = [
                f"{sec}: {float(mv / snap.equity * 100):.1f}%"
                + (" ⚠️ >40% cap" if snap.equity > ZERO and (mv / snap.equity) > Decimal("0.40") else "")
                for sec, mv in sorted(sector_totals.items(), key=lambda kv: kv[1], reverse=True)
            ]
            pos_fields.append(
                {"name": "Sector Concentration (40% Limit)", "value": " | ".join(sec_parts), "inline": False}
            )
        else:
            pos_fields.append(
                {"name": "Stock Holdings", "value": "No stock positions currently held (100% cash).", "inline": False}
            )

        if short_holdings:
            short_lines = [
                f"🚨 **{h.symbol}**: {float(h.qty):g} shares short (market value {_fmt_money(h.market_value)}) — new buys blocked until covered"
                for h in short_holdings
            ]
            pos_fields.append({"name": "Unexpected Short Positions", "value": "\n".join(short_lines), "inline": False})

        # Options Sleeve Summary (separate from stocks!)
        structs = [s for s in self._load_options_structures() if getattr(s, "st", None) and s.st.live]
        if structs or snap.legs:
            total_max_loss, unknown_risk = self._option_risk(structs, snap)
            max_loss_pct = (total_max_loss / equity_f * 100.0) if equity_f > 0 else 0.0
            opt_lines: list[str] = []
            if structs:
                for s in structs:
                    strat_label = str(getattr(s, "strategy", "option")).replace("_", " ")
                    ml = getattr(s, "max_loss", None)
                    origin = self._option_origin_label(s)
                    ml_txt = _fmt_money(ml) if ml is not None else "unknown"
                    opt_lines.append(
                        f"• **{s.underlying}** {strat_label} x{s.qty} (exp {s.expiry}, {origin}) — "
                        f"max possible loss {ml_txt}"
                    )
            else:
                for leg in snap.legs:
                    opt_lines.append(
                        f"• **{leg.occ_symbol}** ({float(leg.qty):+g} contracts) — value {_fmt_money(leg.market_value)}"
                    )
            opt_lines.append(
                f"**Total Modeled Option Max Loss:** {self._fmt_option_risk(total_max_loss, unknown_risk)} "
                f"({max_loss_pct:.1f}% of account vs {self._option_total_limit_pct():.1f}% limit"
                + (", excluding unknown" if unknown_risk else "") + ")"
            )
            pos_fields.append({"name": "Options Sleeve (Separate from Stocks)", "value": "\n".join(opt_lines), "inline": False})
        else:
            pos_fields.append({"name": "Options Sleeve", "value": "No open option structures.", "inline": False})

        embeds.append(
            {
                "title": "💼 Holdings, Risk Caps & Options",
                "color": 0x2563EB,
                "fields": pos_fields,
            }
        )

        if macro_embed:
            embeds.append(macro_embed)

        # 3. Actual Yesterday Activity, Today's Calendar/Queue & Input Health
        ops_fields: list[dict[str, Any]] = []
        ops_fields.append(
            {
                "name": "Yesterday's Actual Bot Activity",
                "value": self._yesterday_activity_summary(session_date),
                "inline": False,
            }
        )
        ops_fields.append(
            {
                "name": "Today's Market Schedule & Queued Orders",
                "value": self._today_plan_summary(session_date, snap),
                "inline": False,
            }
        )
        health_txt, _ = format_input_health_table(self.health_registry)
        ops_fields.append({"name": "Data Sources & API Health", "value": health_txt, "inline": False})
        locks = self._compile_entry_locks(snap, entry_locks)
        if locks:
            ops_fields.append(
                {
                    "name": "Entry Lock Status",
                    "value": "\n".join(locks["lines"]),
                    "inline": False,
                }
            )

        embeds.append(
            {
                "title": "📋 Yesterday's Recap, Today's Plan & Data Health",
                "color": 0x6B7280,
                "fields": ops_fields,
            }
        )
        return embeds

    def _yesterday_activity_summary(self, today: date) -> str:
        if self.ledger is None:
            return "Ledger not connected."
        yday = today - timedelta(days=1)
        # Find most recent exchange day or yesterday with job_runs
        rows = self.ledger.store.query(
            "SELECT job, status, detail_json FROM job_runs WHERE window_key LIKE ? ORDER BY started_at",
            (f"{yday.isoformat()}%",),
        )
        lines: list[str] = []
        if rows:
            done_jobs = [str(r["job"]) for r in rows if r["status"] == "done"]
            bad_jobs = [f"{r['job']} ({r['status']})" for r in rows if r["status"] != "done"]
            if done_jobs:
                lines.append(f"• Completed steps ({yday.isoformat()}): {', '.join(done_jobs)}")
            if bad_jobs:
                lines.append(f"• Skipped/failed steps: {', '.join(bad_jobs)}")
        else:
            lines.append(f"• No scheduled jobs recorded for {yday.isoformat()} (weekend or holiday).")

        # Yesterday fills from broker_activities
        acts = [
            json.loads(r[0])
            for r in self.ledger.store.query("SELECT raw_json FROM broker_activities")
        ]
        yday_fills = [
            a
            for a in acts
            if a.get("activity_type") in ("FILL", "OPTRD")
            and str(a.get("transaction_time") or a.get("date") or "")[:10] == yday.isoformat()
        ]
        if yday_fills:
            for a in yday_fills[:6]:
                lines.append(
                    f"• Filled {str(a.get('side', '')).upper()} {a.get('qty')} {a.get('symbol')} @ ${float(a.get('price', 0)):,.2f}"
                )
        else:
            lines.append("• No orders filled yesterday.")
        return "\n".join(lines)

    def _today_plan_summary(self, today: date, snap: BookSnapshot) -> str:
        lines: list[str] = []
        if self.calendar is not None and hasattr(self.calendar, "session_on"):
            try:
                sess = self.calendar.session_on(today)
                if sess is None:
                    lines.append("• **US Market Closed Today** (weekend or exchange holiday) — monitoring only.")
                else:
                    open_str = sess.opens_at.astimezone(ET).strftime("%I:%M %p ET")
                    close_str = sess.closes_at.astimezone(ET).strftime("%I:%M %p ET")
                    early = " (early close)" if getattr(sess, "early_close", False) else ""
                    lines.append(f"• **Market Hours:** {open_str} – {close_str}{early}")
            except Exception:
                pass

        if self.ledger is not None:
            block = self.ledger.trading_block()
            if block:
                lines.append(f"• ⚠️ **New buys currently paused:** {block}")
            queued = self.ledger.list_intents(statuses=("queued", "submitting", "submitted"))
            if queued:
                lines.append(f"• **{len(queued)} queued order intent(s):**")
                for it in queued[:5]:
                    lines.append(f"  - {it.side.upper()} {it.symbol} ({it.sleeve}, {it.status})")
            else:
                lines.append("• No pre-queued orders waiting at the open.")

        long_count = sum(1 for h in snap.holdings if h.qty > ZERO)
        open_slots = max(0, self.config.MAX_POSITIONS - long_count)
        lines.append(f"• **Capacity:** {open_slots} stock slot(s) open; {_fmt_money(snap.cash)} cash available.")
        return "\n".join(lines)

    async def build_daily_summary(
        self,
        *,
        snapshot: BookSnapshot | None = None,
        close_report: Any | None = None,
        as_of: datetime | None = None,
        entry_locks: Sequence[Any] | None = None,
        entry_funnel: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build honest end-of-day summary embeds from close mark & reconciled book; never raises."""
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        title = f"📊 Daily Close Summary — {now_et.strftime('%b %d, %Y (%a)')}"
        try:
            return await self._build_daily_summary_inner(
                snapshot=snapshot,
                close_report=close_report,
                as_of=as_of,
                entry_locks=entry_locks,
                entry_funnel=entry_funnel,
            )
        except Exception as e:
            return self._unavailable_embed(title, e)

    async def _build_daily_summary_inner(
        self,
        *,
        snapshot: BookSnapshot | None = None,
        close_report: Any | None = None,
        as_of: datetime | None = None,
        entry_locks: Sequence[Any] | None = None,
        entry_funnel: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        session_date = now_et.date()
        date_label = now_et.strftime("%b %d, %Y (%a)")

        snap = snapshot or (self.ledger.last_snapshot() if self.ledger else None)
        marks = self.ledger.equity_marks("close") if self.ledger else []
        flows = self.ledger.cash_flows() if self.ledger else []
        today_mark: EquityMark | None = next((m for m in marks if m.session_date == session_date), None)

        if today_mark is None and snap is None:
            locks_empty = self._compile_entry_locks(None, entry_locks)
            funnel_empty = self._compile_entry_funnel(session_date, week=False, explicit_funnel=entry_funnel)
            empty_fields: list[dict[str, Any]] = []
            if locks_empty:
                empty_fields.append({"name": "Entry Lock Status", "value": "\n".join(locks_empty["lines"]), "inline": False})
            empty_fields.append(
                {
                    "name": "Entry Funnel",
                    "value": self._format_entry_funnel_text(funnel_empty, locked=bool(locks_empty)),
                    "inline": False,
                }
            )
            return [
                {
                    "title": f"📊 Daily Summary — {date_label}",
                    "description": "Could not produce end-of-day summary because no verified broker snapshot was available.",
                    "color": 0xEF4444,
                    "fields": empty_fields,
                }
            ]

        equity = today_mark.equity if today_mark is not None else snap.equity  # type: ignore[union-attr]
        cash = (
            today_mark.cash
            if (today_mark is not None and today_mark.cash is not None)
            else (snap.cash if snap is not None else None)
        )
        long_mv = (
            today_mark.long_mv
            if (today_mark is not None and today_mark.long_mv is not None)
            else (snap.long_mv if snap is not None else None)
        )
        short_mv = (
            today_mark.short_mv
            if (today_mark is not None and today_mark.short_mv is not None)
            else (snap.short_mv if snap is not None else None)
        )
        opt_mv = (
            today_mark.options_mv
            if (today_mark is not None and today_mark.options_mv is not None)
            else (snap.options_mv if snap is not None else None)
        )
        mark_quality = "trusted close" if today_mark is not None else f"{snap.quality} snapshot (close mark pending)"  # type: ignore[union-attr]

        # equity used for any return/drawdown: today's trusted close, else a LIVE snapshot only
        ret_eq = (
            today_mark.equity
            if today_mark is not None
            else _return_equity(snap, session_date, now_et, not_before=self._session_close(session_date))
        )
        sess_pnl, sess_pct = (
            (close_report.day_pnl, close_report.day_pnl_pct)
            if close_report is not None
            and getattr(close_report, "day_pnl", None) is not None
            and getattr(close_report, "session_date", None) == session_date
            and ret_eq is not None
            else (day_pnl(marks, ret_eq, session_date, flows) if ret_eq is not None else (None, None))
        )
        dd_snap = self._drawdown_snapshot(marks, ret_eq)
        twr_pct = twr(marks, flows)
        dep = net_deposits(flows, session_date)
        total_ret_pct = (
            ((ret_eq - dep) / dep * Decimal(100)).quantize(Decimal("0.01"))
            if dep > ZERO and ret_eq is not None
            else None
        )

        interest = self._interest_series(marks)
        bm = await self.benchmark.compute(
            as_of_date=session_date,
            marks=marks,
            flows=flows,
            live_equity=ret_eq,
            fallback_starting_capital=self._fallback_starting_capital(),
            interest=interest,
        )

        # Separate Stock vs Option P&L
        stock_unreal = ZERO
        if snap is not None:
            for h in snap.holdings:
                if h.qty > ZERO and h.price is not None and h.price_ok:
                    stock_unreal += (h.price - h.avg_cost) * h.qty

        structs = self._load_options_structures()
        live_structs = [s for s in structs if getattr(s, "st", None) and s.st.live]
        closed_today_structs = [
            s
            for s in structs
            if str(getattr(s, "closed_at", "") or "")[:10] == session_date.isoformat()
        ]
        opt_realized_today = sum(float(getattr(s, "realized_pnl", 0.0) or 0.0) for s in closed_today_structs)
        opt_open_max_loss, opt_unknown_risk = self._option_risk(live_structs, snap)

        # Today's stock trades & closed win count (sells only — never count buys as wins!)
        did_lines, passed_lines = self._daily_decisions_summary(session_date, closed_today_structs)

        color = 0x16A34A if (sess_pnl is None or sess_pnl >= ZERO) else 0xDC2626
        eq_f = float(equity)
        has_mv_breakdown = any(v is not None for v in (long_mv, short_mv, opt_mv))
        exp_pct: float | None = (
            float((long_mv or ZERO) + abs(short_mv or ZERO) + abs(opt_mv or ZERO)) / eq_f * 100.0
            if eq_f > 0 and has_mv_breakdown
            else None
        )
        exp_str = f"{exp_pct:.1f}%" if exp_pct is not None else "N/A"
        net_ret_val = total_ret_pct if total_ret_pct is not None else bm.portfolio_since_launch_pct

        summary_fields = [
            {"name": "Closing Account Value", "value": f"**{_fmt_money(equity, decimals=2)}** ({mark_quality})", "inline": True},
            {
                "name": "Today's Change",
                "value": f"{_fmt_money(sess_pnl, signed=True, decimals=2)} ({_fmt_pct(sess_pct)})" if sess_pnl is not None else (
                    "Unavailable until a trusted close mark (snapshot is indicative)"
                    if previous_close(marks, session_date) is not None
                    else "First close baseline recorded"
                ),
                "inline": True,
            },
            {
                "name": "Flow-Adjusted Return",
                "value": f"TWR {_fmt_pct(twr_pct)} | Net vs Deposits {_fmt_pct(net_ret_val)}",
                "inline": True,
            },
            {"name": "Cash & Exposure", "value": f"Cash {_fmt_money(cash)} | Invested {exp_str}", "inline": True},
            {
                "name": "Stocks Sleeve",
                "value": f"Value {_fmt_money(long_mv)} | Open Unrealized {_fmt_money(stock_unreal, signed=True)}",
                "inline": True,
            },
            {
                "name": "Options Sleeve (Separate)",
                "value": (
                    f"Value {_fmt_money(opt_mv)} | Open Max Risk {self._fmt_option_risk(opt_open_max_loss, opt_unknown_risk)} | "
                    f"Closed Today {_fmt_money(opt_realized_today, signed=True)}"
                ),
                "inline": True,
            },
            {
                "name": "Portfolio vs QQQ & SPY",
                "value": self._benchmark_lines(bm),
                "inline": False,
            },
            {
                "name": "Drawdown & Safety Tier",
                "value": f"{dd_snap.label}{self._format_dual_drawdown_detail(dd_snap)}",
                "inline": False,
            },
            {"name": "Borrowing & Leverage", "value": self._margin_lines(snap, bm, interest, session_date),
             "inline": False},
            {
                "name": "What We Did Today & Why",
                "value": "\n".join(did_lines),
                "inline": False,
            },
            {
                "name": "What We Passed On & Why",
                "value": "\n".join(passed_lines),
                "inline": False,
            },
        ]

        if snap is not None and snap.holdings:
            hold_lines: list[str] = []
            for h in sorted(snap.holdings, key=lambda x: x.market_value, reverse=True)[:12]:
                wt = float(h.market_value / equity * 100) if equity > ZERO else 0.0
                stop_txt, _ = _holding_stop_status(h, snap)
                hold_lines.append(f"• **{h.symbol}** ({wt:.1f}%) — {_fmt_money(h.market_value)} | {stop_txt}")
            summary_fields.append({"name": "Current Stock Holdings", "value": "\n".join(hold_lines), "inline": False})

        health_txt, _ = format_input_health_table(self.health_registry)
        summary_fields.append({"name": "Data Sources & API Health", "value": health_txt, "inline": False})
        locks_info = self._compile_entry_locks(snap, entry_locks)
        funnel_info = self._compile_entry_funnel(session_date, week=False, explicit_funnel=entry_funnel)
        if locks_info:
            summary_fields.append(
                {
                    "name": "Entry Lock Status",
                    "value": "\n".join(locks_info["lines"]),
                    "inline": False,
                }
            )
        summary_fields.append(
            {
                "name": "Entry Funnel",
                "value": self._format_entry_funnel_text(funnel_info, locked=bool(locks_info)),
                "inline": False,
            }
        )

        return [
            {
                "title": f"📊 Daily Close Summary — {date_label}",
                "description": "End-of-day results based on reconciled broker holdings and verified close marks.",
                "color": color,
                "fields": summary_fields,
            }
        ]

    def _daily_decisions_summary(
        self, session_date: date, closed_opt_structs: Sequence[Any]
    ) -> tuple[list[str], list[str]]:
        did: list[str] = []
        passed: list[str] = []
        if self.ledger is None:
            return (["No trades recorded (ledger offline)."], ["No skipped candidates recorded."])

        day_iso = session_date.isoformat()
        acts = [
            json.loads(r[0])
            for r in self.ledger.store.query("SELECT raw_json FROM broker_activities")
        ]
        real_all = realised_fifo(acts)

        # Stock & option intents updated today
        intents = [
            it
            for it in self.ledger.list_intents()
            if (it.session_date == session_date)
            or ((it.updated_at or it.created_at).astimezone(ET).date() == session_date)
        ]

        closed_stock_wins = 0
        closed_stock_total = 0
        sold_seen: set[str] = set()
        day_start = datetime.combine(session_date, time.min, tzinfo=ET)
        day_end = datetime.combine(session_date, time.max, tzinfo=ET)

        for it in intents:
            if it.status == "filled":
                reason = str(it.meta.get("reason") or it.meta.get("rule") or it.origin).replace("_", " ")
                conv = it.meta.get("effective_conviction") or it.meta.get("conviction")
                conv_txt = f" (conviction {conv}/10)" if conv else ""
                if it.side == "buy":
                    did.append(f"• **Bought {it.symbol}** ({it.sleeve}){conv_txt} — {reason}.")
                else:
                    # realised lots of this symbol booked during this ET session, counted once per symbol
                    sym_pnl = None if it.symbol in sold_seen else real_all.between(it.symbol, day_start, day_end)
                    sold_seen.add(it.symbol)
                    if sym_pnl is not None:
                        closed_stock_total += 1
                        if sym_pnl > ZERO:
                            closed_stock_wins += 1
                    pnl_note = f" (realized {_fmt_money(sym_pnl, signed=True)})" if sym_pnl is not None else ""
                    did.append(f"• **Sold {it.symbol}**{pnl_note} — {reason}.")
            elif it.status in ("skipped", "rejected", "cancelled", "expired"):
                why = redact_secrets_in_text(it.last_error or str(it.meta.get("reason") or it.status))
                passed.append(f"• Passed on **{it.side.upper()} {it.symbol}** ({it.sleeve}): {why}.")

        for s in closed_opt_structs:
            rpnl = getattr(s, "realized_pnl", None)
            why = str(getattr(s, "exit_reason", None) or "closed").replace("_", " ")
            strat = str(getattr(s, "strategy", "option")).replace("_", " ")
            did.append(
                f"• **Closed {s.underlying} {strat}** ({_fmt_money(rpnl, signed=True)}) — {why}."
            )

        # Also check journal for declined news reactions today
        for ev in self.ledger.journal.read(kinds={"news_reaction"}):
            ts_str = str(ev.get("ts") or "")[:10]
            if ts_str == day_iso:
                direction = str(ev.get("direction") or "")
                eff = ev.get("effective_conviction")
                sym = ev.get("symbol", "?")
                if direction in ("NO_ACTION", "HOLD", "NEUTRAL") or (isinstance(eff, int) and eff < 8):
                    passed.append(
                        f"• Declined news trade on **{sym}** (direction {direction or 'neutral'}, conviction {eff}/10)."
                    )

        if closed_stock_total > 0:
            did.append(f"• **Closed Stock Exits Today:** {closed_stock_wins}/{closed_stock_total} profitable.")
        if not did:
            did.append("• Held current positions steady today — no orders filled.")
        if not passed:
            passed.append("• No candidate orders were skipped or blocked today.")
        return (did[:10], passed[:10])

    async def build_weekly_scorecard(
        self,
        *,
        snapshot: BookSnapshot | None = None,
        as_of: datetime | None = None,
        build_id: str = "revamp",
        cutover_date: date | None = None,
        entry_locks: Sequence[Any] | None = None,
        entry_funnel: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build the weekly paper-test scorecard embeds and structured breakdown dict; never raises."""
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        title = f"📈 Weekly Paper-Test Scorecard — {now_et.strftime('%b %d, %Y')}"
        try:
            return await self._build_weekly_scorecard_inner(
                snapshot=snapshot,
                as_of=as_of,
                build_id=build_id,
                cutover_date=cutover_date,
                entry_locks=entry_locks,
                entry_funnel=entry_funnel,
            )
        except Exception as e:
            embeds = self._unavailable_embed(title, e)
            return embeds, {
                "error": _safe_report_reason(e, self.config),
                "account": {
                    "mode": str(getattr(self.config, "MODE", "PAPER")),
                    "build_id": build_id,
                    "as_of_date": now_et.date().isoformat(),
                },
            }

    async def _build_weekly_scorecard_inner(
        self,
        *,
        snapshot: BookSnapshot | None = None,
        as_of: datetime | None = None,
        build_id: str = "revamp",
        cutover_date: date | None = None,
        entry_locks: Sequence[Any] | None = None,
        entry_funnel: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        session_date = now_et.date()
        snap = snapshot or (self.ledger.last_snapshot() if self.ledger else None)
        marks = self.ledger.equity_marks("close") if self.ledger else []
        flows = self.ledger.cash_flows() if self.ledger else []

        live_eq = snap.equity if snap is not None else (marks[-1].equity if marks else ZERO)
        ret_live_eq = _return_equity(snap, session_date, now_et) if snap is not None else None
        if ret_live_eq is not None and ret_live_eq <= ZERO:
            ret_live_eq = None
        cash_now: Decimal | None = snap.cash if snap is not None else (marks[-1].cash if marks else None)
        gross_exp_pct: float | None
        if snap is not None:
            gross_exp_pct = float(snap.gross_exposure_pct * 100)
        elif (
            marks
            and marks[-1].equity > ZERO
            and marks[-1].long_mv is not None
            and marks[-1].short_mv is not None
        ):
            gross_exp_pct = float(
                (marks[-1].long_mv + abs(marks[-1].short_mv) + abs(marks[-1].options_mv or ZERO))
                / marks[-1].equity
                * 100
            )
        else:
            gross_exp_pct = None

        resolved_cutover = self._resolve_cutover_date(cutover_date, marks)
        interest = self._interest_series(marks)
        bm = await self.benchmark.compute(
            as_of_date=session_date,
            marks=marks,
            flows=flows,
            live_equity=ret_live_eq,
            cutover_date=resolved_cutover,
            fallback_starting_capital=self._fallback_starting_capital(),
            interest=interest,
        )

        # Persisted drawdown state/latch first; fallback to marks
        dd_snap = self._drawdown_snapshot(marks, ret_live_eq)
        dd_now, hwm, dd_tier, dd_label = (
            dd_snap.all_time_dd_pct,
            dd_snap.all_time_hwm,
            dd_snap.tier,
            dd_snap.label,
        )
        hwm_date: str | None = None
        max_dd_pct: Decimal | None = None
        max_dd_date: str | None = None
        running_peak = ZERO
        peak_cash_pct: float | None = None
        min_cash_pct: float | None = None
        peak_exp_pct: float | None = None

        for m in sorted((x for x in marks if x.quality == "close"), key=lambda x: x.session_date):
            if m.equity > running_peak:
                running_peak = m.equity
                hwm_date = m.session_date.isoformat()
            if running_peak > ZERO:
                cur_dd = ((m.equity - running_peak) / running_peak * Decimal(100)).quantize(Decimal("0.01"))
                if max_dd_pct is None or cur_dd < max_dd_pct:
                    max_dd_pct = cur_dd
                    max_dd_date = m.session_date.isoformat()
            if m.equity > ZERO:
                if m.cash is not None:
                    cp = float(m.cash / m.equity * 100)
                    peak_cash_pct = cp if peak_cash_pct is None else max(peak_cash_pct, cp)
                    min_cash_pct = cp if min_cash_pct is None else min(min_cash_pct, cp)
                if m.long_mv is not None and m.short_mv is not None and m.options_mv is not None:
                    ep = float((m.long_mv + abs(m.short_mv) + abs(m.options_mv)) / m.equity * 100)
                    peak_exp_pct = ep if peak_exp_pct is None else max(peak_exp_pct, ep)

        # Sleeves & Orders breakdown
        sleeves, options_by_origin, by_conviction, orders_stats = self._compile_sleeves_and_orders(snap)
        news_inputs = self._compile_news_and_inputs(session_date)
        learned = self._compile_what_we_learned(bm)

        structs = [s for s in self._load_options_structures() if getattr(s, "st", None) and s.st.live]
        open_opt_max_loss, open_opt_unknown_risk = self._option_risk(structs, snap)
        open_opt_max_loss_pct = (open_opt_max_loss / float(live_eq) * 100.0) if live_eq > ZERO else 0.0

        invalid_marks = 0
        breaches = 0
        if snap is not None:
            invalid_marks = sum(1 for h in snap.holdings if not h.price_ok) + sum(1 for leg in snap.legs if not leg.price_ok)
            for h in snap.holdings:
                if h.is_short or (live_eq > ZERO and (h.market_value / live_eq) > Decimal("0.15")):
                    breaches += 1
            if open_opt_max_loss_pct > self._option_total_limit_pct():
                breaches += 1
        breaches += open_opt_unknown_risk  # a live structure with no known max loss is uncapped risk

        scorecard: dict[str, Any] = {
            "account": {
                "mode": str(getattr(self.config, "MODE", "PAPER")),
                "build_id": build_id,
                "as_of_date": session_date.isoformat(),
                "sessions_recorded": len([m for m in marks if m.quality == "close"]),
                "equity": float(live_eq),
                "cash": float(cash_now) if cash_now is not None else None,
                "cash_pct": round(float(cash_now / live_eq * 100), 2) if (cash_now is not None and live_eq > ZERO) else None,
                "gross_exposure_pct": round(gross_exp_pct, 2) if gross_exp_pct is not None else None,
                "net_deposits": float(net_deposits(flows, session_date)),
                "cutover_date": resolved_cutover.isoformat() if resolved_cutover else None,
                "week_return_pct": float(bm.portfolio_week_pct) if bm.portfolio_week_pct is not None else None,
                "cutover_return_pct": float(bm.portfolio_cutover_pct) if bm.portfolio_cutover_pct is not None else None,
                "since_launch_return_pct": float(bm.portfolio_since_launch_pct) if bm.portfolio_since_launch_pct is not None else None,
                "qqq_week_pct": float(bm.qqq.week_return_pct) if bm.qqq.week_return_pct is not None else None,
                "qqq_cutover_pct": float(bm.qqq.cutover_return_pct) if bm.qqq.cutover_return_pct is not None else None,
                "qqq_since_launch_pct": float(bm.qqq.since_launch_return_pct) if bm.qqq.since_launch_return_pct is not None else None,
                "spy_week_pct": float(bm.spy.week_return_pct) if bm.spy.week_return_pct is not None else None,
                "spy_cutover_pct": float(bm.spy.cutover_return_pct) if bm.spy.cutover_return_pct is not None else None,
                "spy_since_launch_pct": float(bm.spy.since_launch_return_pct) if bm.spy.since_launch_return_pct is not None else None,
                "benchmark_mark_basis": bm.mark_basis,
                "benchmark_stale": bm.any_stale,
                "hwm": float(hwm) if hwm is not None else None,
                "hwm_date": hwm_date,
                "rolling_hwm": float(dd_snap.rolling_hwm) if dd_snap.rolling_hwm is not None else None,
                "drawdown_pct": float(dd_now) if dd_now is not None else None,
                "all_time_drawdown_pct": float(dd_now) if dd_now is not None else None,
                "decision_drawdown_pct": (
                    float(dd_snap.decision_dd_pct) if dd_snap.decision_dd_pct is not None else None
                ),
                "drawdown_rolling_sessions": dd_snap.rolling_window_sessions,
                "drawdown_paused": dd_snap.paused,
                "pause_sessions_remaining": dd_snap.pause_sessions_remaining,
                "max_drawdown_pct": float(max_dd_pct) if max_dd_pct is not None else None,
                "max_drawdown_date": max_dd_date,
                "drawdown_tier": dd_tier,
                "peak_cash_pct": round(peak_cash_pct, 2) if peak_cash_pct is not None else None,
                "min_cash_pct": round(min_cash_pct, 2) if min_cash_pct is not None else None,
                "peak_exposure_pct": round(peak_exp_pct, 2) if peak_exp_pct is not None else None,
                "open_option_max_loss": round(open_opt_max_loss, 2),
                "open_option_max_loss_pct": round(open_opt_max_loss_pct, 2),
                "open_option_unknown_risk_count": open_opt_unknown_risk,
                "breach_count": breaches,
                "invalid_mark_count": invalid_marks,
            },
            "sleeves": sleeves,
            "options_by_origin": options_by_origin,
            "by_conviction": by_conviction,
            "orders_and_stops": orders_stats,
            "news_and_inputs": news_inputs,
            "what_we_learned": learned,
            "entry_locks": self._compile_entry_locks(snap, entry_locks) or {"locked": False, "locks": [], "lines": []},
            "entry_funnel": self._compile_entry_funnel(session_date, week=True, explicit_funnel=entry_funnel),
            "margin": self._margin_dict(snap, bm, interest, session_date),
        }

        embeds = self._format_weekly_embeds(scorecard, bm, dd_label, now_et)
        return embeds, scorecard

    def _compile_sleeves_and_orders(
        self, snap: BookSnapshot | None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
        def _empty_sleeve() -> dict[str, Any]:
            return {
                "candidates": 0,
                "fills": 0,
                "skips_by_reason": {},
                "open_count": 0,
                "closed_count": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "fees": 0.0,
                "wins": 0,
                "win_denominator": 0,
                "win_summary": "N/A (0 closed)",
                "largest_loss": None,
                "median_hold_days": None,
            }

        sleeves = {
            "CORE_EQUITY": _empty_sleeve(),
            "NEWS_EQUITY": _empty_sleeve(),
            "OPTIONS": _empty_sleeve(),
            "LEGACY_REPAIR": _empty_sleeve(),
        }
        options_by_origin = {
            "SCHEDULED": _empty_sleeve(),
            "NEWS": _empty_sleeve(),
            "ADOPTED": _empty_sleeve(),  # legacy/adopted: kept out of the OPTIONS strategy stats
        }
        by_conviction: dict[str, dict[str, Any]] = {
            str(k): {"count": 0, "closed_count": 0, "net_pnl": None, "display": "N/A (0 closed)"}
            for k in (7, 8, 9, 10)
        }
        conv_pnls: dict[str, list[float]] = defaultdict(list)

        orders_stats: dict[str, Any] = {
            "filled_count": 0,
            "cancelled_count": 0,
            "rejected_count": 0,
            "median_fill_latency_s": None,
            "worst_fill_latency_s": None,
            "median_arrival_slippage_bps": None,
            "protected_long_count": 0,
            "unprotected_long_count": 0,
            "atr_fallback_count": 0,
            "option_t_minus_2_missed_count": 0,
            "reconcile_repairs_count": 0,
            "drift_trims_count": 0,
            "unresolved_diffs_count": 0,
        }

        if self.ledger is None:
            return sleeves, options_by_origin, by_conviction, orders_stats

        acts = [
            json.loads(r[0])
            for r in self.ledger.store.query("SELECT raw_json FROM broker_activities")
        ]
        real_fifo = realised_fifo(acts)

        # Intents -> candidates, fills, skips, latencies, slippage
        latencies: list[float] = []
        intents = self.ledger.list_intents()
        sym_to_sleeve: dict[str, str] = {}
        sym_to_conv: dict[str, str] = {}

        for it in intents:
            sl = it.sleeve if it.sleeve in sleeves else (
                "NEWS_EQUITY" if it.origin == "news" else (
                    "LEGACY_REPAIR" if it.origin in ("normalise", "limit_drift", "migration") else "CORE_EQUITY"
                )
            )
            if it.side == "buy":
                sleeves[sl]["candidates"] += 1
                sym_to_sleeve[it.symbol] = sl
                conv_val = it.meta.get("effective_conviction") or it.meta.get("conviction")
                if conv_val is not None and str(conv_val) in by_conviction:
                    by_conviction[str(conv_val)]["count"] += 1
                    sym_to_conv[it.symbol] = str(conv_val)

            if it.status == "filled":
                orders_stats["filled_count"] += 1
                if it.side == "buy":
                    sleeves[sl]["fills"] += 1
                if it.kind == "trim":
                    orders_stats["drift_trims_count"] += 1
                if it.updated_at is not None and it.created_at is not None:
                    dt_s = (it.updated_at - it.created_at).total_seconds()
                    if dt_s >= 0:
                        latencies.append(dt_s)
            elif it.status == "rejected":
                orders_stats["rejected_count"] += 1
                reason = str(it.last_error or "rejected")[:40]
                skips = sleeves[sl]["skips_by_reason"]
                skips[reason] = skips.get(reason, 0) + 1
            elif it.status == "cancelled":
                orders_stats["cancelled_count"] += 1
            elif it.status in ("skipped", "expired"):
                reason = str(it.last_error or it.status)[:40]
                skips = sleeves[sl]["skips_by_reason"]
                skips[reason] = skips.get(reason, 0) + 1

        if latencies:
            orders_stats["median_fill_latency_s"] = round(statistics.median(latencies), 2)
            orders_stats["worst_fill_latency_s"] = round(max(latencies), 2)

        # ATR fallbacks & reconcile diffs from journal
        for ev in self.ledger.journal.read(kinds={"entry.filled", "reconcile_diff"}):
            if ev.get("kind") == "entry.filled" and ev.get("stop_fallback"):
                orders_stats["atr_fallback_count"] += 1
            elif ev.get("kind") == "reconcile_diff":
                orders_stats["reconcile_repairs_count"] += 1

        # Open stock holdings & stop protection
        if snap is not None:
            orders_stats["unresolved_diffs_count"] = sum(
                1 for d in snap.diffs if str(d.severity) in ("warning", "critical")
            )
            for h in snap.holdings:
                if h.qty <= ZERO:
                    continue
                _, covered = _holding_stop_status(h, snap)
                if covered:
                    orders_stats["protected_long_count"] += 1
                else:
                    orders_stats["unprotected_long_count"] += 1
                opened_src = h.meta.opened_source if h.meta else "bot"
                sl = (
                    "LEGACY_REPAIR"
                    if opened_src in ("import", "migration", "assignment", "exercise", "manual")
                    else sym_to_sleeve.get(h.symbol, "CORE_EQUITY")
                )
                sleeves[sl]["open_count"] += 1
                if h.price is not None and h.price_ok:
                    sleeves[sl]["unrealized_pnl"] = round(
                        sleeves[sl]["unrealized_pnl"] + float((h.price - h.avg_cost) * h.qty), 2
                    )

        # Closed stock positions from SQLite positions_meta + FIFO realized P&L
        closed_rows = self.ledger.store.query(
            "SELECT symbol, opened_at, closed_at, opened_source FROM positions_meta WHERE closed_at IS NOT NULL"
        )
        hold_days_by_sleeve: dict[str, list[float]] = defaultdict(list)
        losses_by_sleeve: dict[str, list[float]] = defaultdict(list)

        for r in closed_rows:
            sym = str(r["symbol"])
            src = str(r["opened_source"] or "bot")
            sl = (
                "LEGACY_REPAIR"
                if src in ("import", "migration", "assignment", "exercise", "manual")
                else sym_to_sleeve.get(sym, "CORE_EQUITY")
            )
            sleeves[sl]["closed_count"] += 1
            # only the lots closed during THIS position (a symbol traded twice has two positions)
            pnl_dec = real_fifo.between(sym, _parse_ts(r["opened_at"]), _parse_ts(r["closed_at"]))
            if pnl_dec is not None:
                pnl_f = float(pnl_dec)
                sleeves[sl]["realized_pnl"] = round(sleeves[sl]["realized_pnl"] + pnl_f, 2)
                sleeves[sl]["win_denominator"] += 1
                if pnl_f > 0:
                    sleeves[sl]["wins"] += 1
                if pnl_f < 0:
                    losses_by_sleeve[sl].append(pnl_f)
                ck = sym_to_conv.get(sym)
                if ck in by_conviction:
                    conv_pnls[ck].append(pnl_f)
            try:
                t0 = datetime.fromisoformat(str(r["opened_at"]).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(r["closed_at"]).replace("Z", "+00:00"))
                hold_days_by_sleeve[sl].append(max(0.0, (t1 - t0).total_seconds() / 86400.0))
            except Exception:
                pass

        # Options structures -> OPTIONS sleeve (new strategy only) + SCHEDULED/NEWS origin
        # subsets; adopted/legacy structures go only to ADOPTED. Never in NEWS_EQUITY.
        from stock_agent.options_book import open_pnl, structure_mark

        all_structs = self._load_options_structures()
        marks = self._option_marks(snap, all_structs)
        for b in (sleeves["OPTIONS"], *options_by_origin.values()):
            b["unrealized_unknown_count"] = 0
        for s in all_structs:
            origin_key = self._option_origin(s)
            buckets = ((options_by_origin[origin_key],) if origin_key == "ADOPTED"
                       else (sleeves["OPTIONS"], options_by_origin[origin_key]))
            st_val = str(getattr(s, "state", ""))
            live = getattr(s, "st", None) is not None and s.st.live
            upnl = None
            if live and st_val != "pending":
                try:
                    upnl = open_pnl(s, structure_mark(s, marks))
                except Exception:
                    upnl = None
            for b in buckets:
                if live and st_val != "pending":
                    if upnl is None:
                        b["unrealized_unknown_count"] += 1
                    else:
                        b["unrealized_pnl"] = round(b["unrealized_pnl"] + upnl, 2)
                    partial = float(getattr(s, "partial_pnl", 0.0) or 0.0)
                    if partial:
                        b["realized_pnl"] = round(b["realized_pnl"] + partial, 2)
            for b in buckets:
                b["candidates"] += 1
                if st_val in ("open", "closing", "repair", "unknown"):
                    b["fills"] += 1
                    b["open_count"] += 1
                elif st_val == "closed":
                    b["fills"] += 1
                    b["closed_count"] += 1
                    rpnl = getattr(s, "realized_pnl", None)
                    if rpnl is not None:
                        rf = float(rpnl)
                        b["realized_pnl"] = round(b["realized_pnl"] + rf, 2)
                        b["win_denominator"] += 1
                        if rf > 0:
                            b["wins"] += 1
                        if rf < 0 and origin_key != "ADOPTED":
                            losses_by_sleeve["OPTIONS"].append(rf)
            conv_s = getattr(s, "conviction", None)
            if origin_key != "ADOPTED" and conv_s is not None and str(conv_s) in by_conviction:
                by_conviction[str(conv_s)]["count"] += 1
                if st_val == "closed" and getattr(s, "realized_pnl", None) is not None:
                    conv_pnls[str(conv_s)].append(float(s.realized_pnl))

        sleeves["CORE_EQUITY"]["fees"] = round(float(real_fifo.fees), 2)

        for sl_name, sl_dict in list(sleeves.items()) + list(options_by_origin.items()):
            den = sl_dict["win_denominator"]
            wins = sl_dict["wins"]
            sl_dict["win_summary"] = f"{wins}/{den} ({wins / den * 100:.0f}%)" if den > 0 else "N/A (0 closed)"
            if sl_name in losses_by_sleeve and losses_by_sleeve[sl_name]:
                sl_dict["largest_loss"] = round(min(losses_by_sleeve[sl_name]), 2)
            if sl_name in hold_days_by_sleeve and hold_days_by_sleeve[sl_name]:
                sl_dict["median_hold_days"] = round(statistics.median(hold_days_by_sleeve[sl_name]), 1)

        for ck, bucket in by_conviction.items():
            vals = conv_pnls.get(ck, [])
            bucket["closed_count"] = len(vals)
            if vals:
                tot = round(sum(vals), 2)
                bucket["net_pnl"] = tot
                bucket["display"] = f"{len(vals)} closed, net {_fmt_money(tot, signed=True)} ({bucket['count']} total signals)"
            else:
                bucket["net_pnl"] = None
                bucket["display"] = f"N/A (0 closed of {bucket['count']} signals)"

        return sleeves, options_by_origin, by_conviction, orders_stats

    def _compile_news_and_inputs(self, session_date: date) -> dict[str, Any]:
        _, rows = format_input_health_table(self.health_registry)
        req_rows = [r for r in rows if r.get("required_for_trading")]
        healthy_req = [r for r in req_rows if str(r.get("state")).lower() == "healthy"]
        mandatory_pct = round(len(healthy_req) / len(req_rows) * 100.0, 1) if req_rows else None
        stale_missing = sum(1 for r in rows if str(r.get("state")).lower() in ("degraded", "unhealthy"))

        stories_seen = 0
        stories_confirmed = 0
        degraded_confirm = 0
        news_declined = 0
        unhealthy_transitions = 0

        if self.ledger is not None:
            for ev in self.ledger.journal.read(kinds={"news_reaction", "alert"}):
                if ev.get("kind") == "news_reaction":
                    stories_seen += 1
                    if ev.get("confirmed"):
                        stories_confirmed += 1
                    if ev.get("degraded"):
                        degraded_confirm += 1
                    eff = ev.get("effective_conviction")
                    if str(ev.get("direction", "")) in ("NO_ACTION", "HOLD", "NEUTRAL") or (
                        isinstance(eff, int) and eff < 8
                    ):
                        news_declined += 1
                elif ev.get("kind") == "alert" and ev.get("category") == "input_health" and not ev.get("resolved"):
                    unhealthy_transitions += 1

        news_opt_count = sum(
            1 for s in self._load_options_structures() if getattr(s, "source", "") == "news"
        )
        news_eq_count = (
            sum(1 for it in self.ledger.list_intents() if it.origin == "news" and it.status == "filled")
            if self.ledger is not None
            else 0
        )

        llm_spent: float | None = None
        if self.research_store is not None and hasattr(self.research_store, "daily_spend"):
            try:
                total = 0.0
                for d_back in range(7):
                    total += float(self.research_store.daily_spend(session_date - timedelta(days=d_back)) or 0.0)
                llm_spent = round(total, 4)
            except Exception:
                llm_spent = None

        return {
            "mandatory_healthy_pct": mandatory_pct,
            "unhealthy_transitions": unhealthy_transitions,
            "stale_or_missing_count": stale_missing,
            "stories_seen": stories_seen,
            "stories_deduped": stories_seen,
            "stories_material": stories_seen,
            "stories_confirmed": stories_confirmed,
            "degraded_confirmations": degraded_confirm,
            "news_equity_trades": news_eq_count,
            "news_option_trades": news_opt_count,
            "news_declined": news_declined,
            "latency_stages": {
                "story_to_detection_s": None,
                "detection_to_decision_s": None,
                "decision_to_order_s": None,
                "order_to_fill_s": None,
            },
            "llm_budget_spent_7d_usd": llm_spent,
            "input_health_rows": rows,
        }

    def _sessions_after_stop(self, result: dict[str, Any], row: dict[str, Any]) -> int:
        """Completed trading sessions after a shadow stop's exit session (0 if unknown).

        Prefers the count journalled by ``performance.shadow_stops`` (from the daily-bar
        session calendar); else asks the market calendar; unknown is treated as immature."""
        n = result.get("sessions_after_exit")
        if isinstance(n, int) and not isinstance(n, bool):
            return n
        try:
            exit_d = date.fromisoformat(str(result.get("exit_session"))[:10])
            thru_d = date.fromisoformat(str(result.get("through_session") or row.get("through_session"))[:10])
        except ValueError:
            return 0
        cal = self.calendar
        if cal is not None and hasattr(cal, "trading_days_between"):
            try:
                got = cal.trading_days_between(exit_d, thru_d)
            except Exception:
                got = None
            if isinstance(got, int):
                return got
        return 0

    def _compile_what_we_learned(self, bm: BenchmarkSnapshot) -> dict[str, Any]:
        """Aggregate shadow-stop, sizing, and news outcomes from the journal.

        Never prints ``0`` when an observation series is empty/immature; uses
        ``None`` in numeric fields and ``N/A`` in display text.
        """
        variant_map = {
            "fixed_7pct": "stop_7pct",
            "7pct": "stop_7pct",
            "0.07": "stop_7pct",
            "fixed_10pct": "stop_10pct",
            "10pct": "stop_10pct",
            "0.10": "stop_10pct",
            "fixed_12pct": "stop_12pct",
            "12pct": "stop_12pct",
            "0.12": "stop_12pct",
            "no_stop": "no_stop",
            "none": "no_stop",
            "live": "live_atr_stop",
            "atr": "live_atr_stop",
            "entry_stop": "live_atr_stop",
        }
        latest_by_anchor: dict[str, dict[str, Any]] = {}
        if self.ledger is not None:
            for ev in self.ledger.journal.read(kinds={"shadow.stop"}):
                anc = str(ev.get("anchor") or "")
                if not anc:
                    continue
                prev = latest_by_anchor.get(anc)
                if prev is None or str(ev.get("through_session") or "") >= str(prev.get("through_session") or ""):
                    latest_by_anchor[anc] = ev

        buckets: dict[str, list[float]] = {
            "stop_7pct": [],
            "stop_10pct": [],
            "stop_12pct": [],
            "no_stop": [],
            "live_atr_stop": [],
        }
        stopped_count: Counter[str] = Counter()
        post_stop_20d: list[float] = []

        for ev in latest_by_anchor.values():
            for res in ev.get("results") or []:
                raw_v = str(res.get("variant") or "").lower()
                canon = variant_map.get(raw_v)
                if canon is None:
                    if "7" in raw_v:
                        canon = "stop_7pct"
                    elif "10" in raw_v:
                        canon = "stop_10pct"
                    elif "12" in raw_v:
                        canon = "stop_12pct"
                    elif "no" in raw_v:
                        canon = "no_stop"
                    elif "live" in raw_v or "atr" in raw_v or "actual" in raw_v:
                        canon = "live_atr_stop"
                if canon in buckets and res.get("pnl") is not None:
                    pnl_f = float(res["pnl"])
                    buckets[canon].append(pnl_f)
                    if res.get("stopped"):
                        stopped_count[canon] += 1

            # Post-stop check: mature only once 20 completed TRADING sessions have passed
            # after the live stop's exit session (not calendar days from entry).
            try:
                res_list = ev.get("results") or []
                live_r = next((x for x in res_list if "live" in str(x.get("variant", "")).lower() or "atr" in str(x.get("variant", "")).lower()), None)
                nostop_r = next((x for x in res_list if "no" in str(x.get("variant", "")).lower()), None)
                if (live_r and nostop_r and live_r.get("stopped") and live_r.get("pnl") is not None
                        and nostop_r.get("pnl") is not None
                        and self._sessions_after_stop(live_r, ev) >= SHADOW_POST_STOP_SESSIONS):
                    post_stop_20d.append(float(live_r["pnl"]) - float(nostop_r["pnl"]))
            except Exception:
                pass

        shadow_stops_out: dict[str, dict[str, Any]] = {}
        for k, vals in buckets.items():
            if vals:
                tot = round(sum(vals), 2)
                worst = round(min(vals), 2)
                shadow_stops_out[k] = {
                    "count": len(vals),
                    "stopped_count": stopped_count[k],
                    "total_pnl": tot,
                    "worst_loss": worst,
                    "display": f"{len(vals)} trade(s), net {_fmt_money(tot, signed=True)}, stopped {stopped_count[k]}x (worst {_fmt_money(worst, signed=True)})",
                }
            else:
                shadow_stops_out[k] = {
                    "count": 0,
                    "stopped_count": 0,
                    "total_pnl": None,
                    "worst_loss": None,
                    "display": "N/A (no mature trades yet)",
                }

        post_20d_summary = (
            {
                "mature_count": len(post_stop_20d),
                "net_saved_vs_hold": round(sum(post_stop_20d), 2),
                "display": f"{len(post_stop_20d)} stopped trade(s) 20+ sessions after the stop: stop saved {_fmt_money(sum(post_stop_20d), signed=True)} vs holding without a stop (no-stop marked at the latest replayed close, not a fixed day-20 mark)",
            }
            if post_stop_20d
            else {
                "mature_count": 0,
                "net_saved_vs_hold": None,
                "display": "N/A (no stopped trades with 20+ trading sessions after the stop yet)",
            }
        )

        return {
            "shadow_stops": shadow_stops_out,
            "sizing_comparison": {
                "conviction_sized": {"total_pnl": None, "display": "N/A (accumulating paired sample)"},
                "equal_weight": {"total_pnl": None, "display": "N/A (accumulating paired sample)"},
            },
            "stopped_out_20d_check": post_20d_summary,
            "active_return_vs_qqq_pct": float(bm.active_vs_qqq_launch_pct) if bm.active_vs_qqq_launch_pct is not None else None,
            "realized_vol_ann_pct": float(bm.realized_vol_ann_pct) if bm.realized_vol_ann_pct is not None else None,
            "beta_to_qqq": float(bm.beta_to_qqq) if bm.beta_to_qqq is not None else None,
            "information_ratio_vs_qqq": float(bm.information_ratio_vs_qqq) if bm.information_ratio_vs_qqq is not None else None,
            "information_ratio_display": (
                f"{float(bm.information_ratio_vs_qqq):.2f}"
                if bm.information_ratio_vs_qqq is not None
                else f"N/A (early sample: {bm.paired_sessions}/{MIN_PAIRED_IR} paired sessions)"
            ),
            "paired_sessions": bm.paired_sessions,
            "stats_early": bm.stats_early,
            "risk_stats_label": (
                f"early sample: {bm.paired_sessions} paired sessions, treat as indicative"
                if bm.stats_early
                else f"{bm.paired_sessions} paired sessions"
            ),
        }

    def _format_weekly_embeds(
        self,
        sc: dict[str, Any],
        bm: BenchmarkSnapshot,
        dd_label: str,
        now_et: datetime,
    ) -> list[dict[str, Any]]:
        acc = sc["account"]
        sl = sc["sleeves"]
        opt_orig = sc["options_by_origin"]
        ords = sc["orders_and_stops"]
        ni = sc["news_and_inputs"]
        wl = sc["what_we_learned"]

        # Embed 1: Account, Benchmarks & Drawdown
        cash_pct_s = f"{acc['cash_pct']:.1f}%" if acc.get("cash_pct") is not None else "N/A"
        exp_pct_s = f"{acc['gross_exposure_pct']:.1f}%" if acc.get("gross_exposure_pct") is not None else "N/A"
        cut_iso = acc.get("cutover_date")
        cut_dt = date.fromisoformat(cut_iso) if isinstance(cut_iso, str) and cut_iso else None
        e1 = {
            "title": f"📈 Weekly Paper-Test Scorecard — {now_et.strftime('%b %d, %Y')}",
            "description": (
                f"Mode: **{acc['mode']}** (`{acc['build_id']}`) | Sessions recorded: **{acc['sessions_recorded']}**. "
                "All returns adjust for cash deposits/withdrawals and use verified close marks."
            ),
            "color": 0x2563EB,
            "fields": [
                {"name": "Account Value & Cash", "value": f"**{_fmt_money(acc['equity'], decimals=2)}** (Cash {cash_pct_s}, Gross Exp {exp_pct_s})", "inline": False},
                {"name": "Returns vs QQQ & SPY", "value": self._benchmark_lines(bm, include_week=True, include_cutover=True, cutover_date=cut_dt), "inline": False},
                {"name": "Borrowing & Leverage", "value": sc["margin"]["display"] if sc.get("margin") else "N/A",
                 "inline": False},
                {
                    "name": "Drawdown & Risk Controls",
                    "value": (
                        f"{dd_label}\n"
                        f"Rolling {acc.get('drawdown_rolling_sessions', 252)}-Session Decision Drawdown: "
                        f"{_fmt_pct(acc.get('decision_drawdown_pct'))} | "
                        f"All-Time Fall from Peak: {_fmt_pct(acc['drawdown_pct'])} | "
                        f"Worst Close Drawdown: {_fmt_pct(acc['max_drawdown_pct'])}"
                        + (f" on {acc['max_drawdown_date']}" if acc["max_drawdown_date"] else "")
                        + f" | Breaches: {acc['breach_count']} | Invalid Marks: {acc['invalid_mark_count']}"
                    ),
                    "inline": False,
                },
            ],
        }

        # Embed 2: Sleeves, Options & Execution Quality
        sleeve_lines = [
            f"• **Core Stocks:** {sl['CORE_EQUITY']['fills']}/{sl['CORE_EQUITY']['candidates']} filled | "
            f"Wins {sl['CORE_EQUITY']['win_summary']} | Realized {_fmt_money(sl['CORE_EQUITY']['realized_pnl'], signed=True)} | "
            f"Open {_fmt_money(sl['CORE_EQUITY']['unrealized_pnl'], signed=True)}",
            f"• **News Stocks:** {sl['NEWS_EQUITY']['fills']}/{sl['NEWS_EQUITY']['candidates']} filled | "
            f"Wins {sl['NEWS_EQUITY']['win_summary']} | Realized {_fmt_money(sl['NEWS_EQUITY']['realized_pnl'], signed=True)} | "
            f"Open {_fmt_money(sl['NEWS_EQUITY']['unrealized_pnl'], signed=True)}",
            f"• **Options (Separate Sleeve):** {sl['OPTIONS']['fills']}/{sl['OPTIONS']['candidates']} structures | "
            f"Wins {sl['OPTIONS']['win_summary']} | Realized {_fmt_money(sl['OPTIONS']['realized_pnl'], signed=True)} | "
            f"Open {_fmt_money(sl['OPTIONS']['unrealized_pnl'], signed=True)}"
            + (f" (+{sl['OPTIONS']['unrealized_unknown_count']} unmarked)" if sl['OPTIONS'].get('unrealized_unknown_count') else "")
            + f" | Open Max Loss {self._fmt_option_risk(acc['open_option_max_loss'], acc.get('open_option_unknown_risk_count', 0))} "
            f"({acc['open_option_max_loss_pct']:.1f}% of equity)\n"
            f"  ↳ Scheduled origin: {opt_orig['SCHEDULED']['fills']} | News origin: {opt_orig['NEWS']['fills']} (not double-counted in News Stocks)"
            + (f"\n  ↳ Adopted/legacy (excluded from strategy stats): {opt_orig['ADOPTED']['fills']} | "
               f"Realized {_fmt_money(opt_orig['ADOPTED']['realized_pnl'], signed=True)} | "
               f"Open {_fmt_money(opt_orig['ADOPTED']['unrealized_pnl'], signed=True)}"
               if opt_orig.get('ADOPTED', {}).get('fills') else ""),
        ]
        if sl["LEGACY_REPAIR"]["open_count"] or sl["LEGACY_REPAIR"]["closed_count"]:
            sleeve_lines.append(
                f"• **Inherited / Clean-Up Bucket:** {sl['LEGACY_REPAIR']['open_count']} open, "
                f"{sl['LEGACY_REPAIR']['closed_count']} closed | Realized {_fmt_money(sl['LEGACY_REPAIR']['realized_pnl'], signed=True)}"
            )

        conv_lines = [
            f"• Score **{k}/10**: {v['display']}" for k, v in sc["by_conviction"].items()
        ]
        lat_txt = f"{ords['median_fill_latency_s']}s" if ords["median_fill_latency_s"] is not None else "N/A"
        ord_txt = (
            f"Filled {ords['filled_count']} | Cancelled {ords['cancelled_count']} | Rejected {ords['rejected_count']} | "
            f"Median Fill Time {lat_txt}\n"
            f"Stop Coverage: {ords['protected_long_count']} protected, {ords['unprotected_long_count']} unprotected | "
            f"ATR Fallbacks: {ords['atr_fallback_count']} | Drift Trims: {ords['drift_trims_count']}"
        )
        ni_txt = (
            f"Mandatory Feeds Healthy: {ni['mandatory_healthy_pct'] if ni['mandatory_healthy_pct'] is not None else 'N/A'}% | "
            f"News Stories Evaluated: {ni['stories_seen']} (Stock fills {ni['news_equity_trades']}, Option fills {ni['news_option_trades']}, Declined {ni['news_declined']})"
            + (f" | 7d AI Spend: ${ni['llm_budget_spent_7d_usd']:.2f}" if ni["llm_budget_spent_7d_usd"] is not None else "")
        )

        e2_fields: list[dict[str, Any]] = [
            {"name": "Results by Strategy Sleeve", "value": "\n".join(sleeve_lines), "inline": False},
            {"name": "Results by Conviction Tier (Sample Size, Not Probability)", "value": "\n".join(conv_lines), "inline": False},
            {"name": "Order & Stop-Loss Reliability", "value": ord_txt, "inline": False},
            {"name": "Live News & Input Reliability", "value": ni_txt, "inline": False},
        ]
        wk_locks = sc.get("entry_locks") or {}
        if wk_locks.get("locked") and wk_locks.get("lines"):
            e2_fields.append(
                {"name": "Entry Lock Status", "value": "\n".join(wk_locks["lines"]), "inline": False}
            )
        wk_funnel = sc.get("entry_funnel")
        if isinstance(wk_funnel, Mapping):
            e2_fields.append(
                {
                    "name": "Entry Funnel (This Week)",
                    "value": self._format_entry_funnel_text(wk_funnel, locked=bool(wk_locks.get("locked"))),
                    "inline": False,
                }
            )

        e2 = {
            "title": "🧩 Results by Sleeve, Execution & News Health",
            "color": 0x0D9488,
            "fields": e2_fields,
        }

        # Embed 3: What We Learned (Plain-English Shadow Comparisons)
        ss = wl["shadow_stops"]
        shadow_lines = [
            f"• **Live ATR Stop (7–12%):** {ss['live_atr_stop']['display']}",
            f"• **What if 7% Fixed Stop:** {ss['stop_7pct']['display']}",
            f"• **What if 10% Fixed Stop:** {ss['stop_10pct']['display']}",
            f"• **What if 12% Fixed Stop:** {ss['stop_12pct']['display']}",
            f"• **What if No Stop-Loss:** {ss['no_stop']['display']}",
            f"• **20-Day Post-Stop Check:** {wl['stopped_out_20d_check']['display']}",
        ]
        risk_adj_txt = (
            f"Active vs QQQ (since launch): {_fmt_pct(wl['active_return_vs_qqq_pct'])} | "
            f"Annualized Volatility: {_fmt_pct(wl['realized_vol_ann_pct'], signed=False)} | "
            f"Beta to QQQ: {wl['beta_to_qqq'] if wl['beta_to_qqq'] is not None else 'N/A'} | "
            f"Information Ratio: {wl['information_ratio_display']}"
            + (f" ({wl['risk_stats_label']})" if wl.get("risk_stats_label") else "")
        )

        e3 = {
            "title": "🔬 What We Learned This Week (Shadow Comparisons)",
            "description": (
                "Background 'what-if' checks run on the exact same entries without risking extra capital. "
                "Items show N/A until enough trades finish to make a fair comparison."
            ),
            "color": 0x7C3AED,
            "fields": [
                {"name": "Stop-Loss Comparison", "value": "\n".join(shadow_lines), "inline": False},
                {"name": "Risk-Adjusted Comparison vs QQQ", "value": risk_adj_txt, "inline": False},
            ],
        }

        return [e1, e2, e3]

    # ── A14c / A14d: Entry Locks & Entry Funnel ──────────────────────────────

    def _compile_entry_locks(
        self,
        snap: BookSnapshot | None,
        explicit_locks: Sequence[Any] | None = None,
    ) -> dict[str, Any] | None:
        """Collect all active global buy-entry locks and their clear conditions (DECISIONS A14c)."""
        collected: list[dict[str, str]] = []
        seen_codes: set[str] = set()

        def _add(code: str, lock: str, clears_when: str, detail: str = "") -> None:
            norm = code.strip().lower() or lock.strip().lower()
            if not norm or norm in seen_codes:
                return
            seen_codes.add(norm)
            item = {
                "code": norm,
                "lock": lock.strip(),
                "detail": detail.strip(),
                "clears_when": clears_when.strip(),
            }
            collected.append(item)

        def _ingest_items(items: Sequence[Any] | None) -> None:
            for it in items or ():
                if not it:
                    continue
                if isinstance(it, str):
                    txt = it.strip()
                    if txt:
                        _add(txt[:32], txt, "the blocking condition resolves")
                elif isinstance(it, Mapping):
                    c = str(it.get("code") or it.get("lock") or "safety_lock")
                    lk = str(it.get("lock") or c)
                    cl = str(it.get("clears_when") or it.get("clear_condition") or "the safety check passes")
                    dt = str(it.get("detail") or "")
                    _add(c, lk, cl, dt)
                else:
                    c = str(getattr(it, "code", None) or getattr(it, "lock", None) or "safety_lock")
                    lk = str(getattr(it, "lock", None) or c)
                    cl = str(
                        getattr(it, "clears_when", None)
                        or getattr(it, "clear_condition", None)
                        or "the safety check passes"
                    )
                    dt = str(getattr(it, "detail", None) or "")
                    _add(c, lk, cl, dt)

        _ingest_items(explicit_locks)

        if self.ledger is not None and hasattr(self.ledger, "risk_state_get"):
            try:
                persisted_locks = self.ledger.risk_state_get("entry_locks")
                if isinstance(persisted_locks, Sequence) and not isinstance(persisted_locks, (str, bytes)):
                    _ingest_items(persisted_locks)
            except Exception:
                pass

        # Fallback live state checks so locks are reported accurately even without scheduler override
        try:
            data_dir = getattr(self.config, "DATA_DIR", None)
            if data_dir and (Path(data_dir) / "pause-trading").exists():
                _add(
                    "pause_file",
                    "operator pause switch (pause-trading file)",
                    "the data/pause-trading file is removed",
                )
        except Exception:
            pass

        if self.ledger is not None:
            try:
                block = self.ledger.trading_block() if hasattr(self.ledger, "trading_block") else None
                if block:
                    _add(
                        "books_untrusted",
                        f"broker reconciliation not clean ({block})",
                        "the next broker reconciliation finishes with zero blocking discrepancies",
                    )
            except Exception:
                pass

            try:
                store = getattr(self.ledger, "store", None)
                if store is not None and hasattr(store, "kv_get"):
                    norm_rec = store.kv_get("execution.normalise")
                    if isinstance(norm_rec, Mapping) and norm_rec.get("state") not in (None, "done"):
                        _add(
                            "cleanup_pending",
                            f"first-run account clean-up ({norm_rec.get('state')})",
                            "initial protective stops and legacy position repairs complete cleanly",
                        )
            except Exception:
                pass

        st_dd, dd_corrupt = _read_persisted_drawdown_checked(self.ledger, config=self.config)
        if dd_corrupt:
            _add(
                "risk_state_unavailable",
                "invalid persisted drawdown record",
                "the risk state is readable and valid and the broker reconciles cleanly",
            )
        elif st_dd is not None and st_dd.paused:
            _add(
                "drawdown_pause",
                "20% drawdown safety stop",
                _format_pause_lift_condition(st_dd, config=self.config),
            )

        if snap is not None:
            flags = getattr(snap, "account_flags", None) or {}
            active_flags = [
                k for k in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
                if flags.get(k)
            ]
            if active_flags:
                _add(
                    "broker_blocked",
                    f"broker account flag ({', '.join(active_flags)})",
                    "Alpaca clears the account trading restriction",
                )

        if not collected:
            return None

        return {
            "locked": True,
            "locks": collected,
            "lines": [format_entry_lock_line(it) for it in collected],
        }

    def _compile_entry_funnel(
        self,
        session_date: date,
        *,
        week: bool = False,
        explicit_funnel: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile the 6-stage entry funnel and skip counts by reason & sleeve (DECISIONS A14d)."""
        if explicit_funnel is not None:
            return dict(explicit_funnel)

        start_date = (session_date - timedelta(days=6)) if week else session_date
        start_iso = start_date.isoformat()
        end_iso = session_date.isoformat()

        screened = 0
        thesis_written = 0
        passed_conviction = 0
        research_skips: Counter[str] = Counter()
        exec_skips: Counter[str] = Counter()
        held_counts: Counter[str] = Counter()
        by_sleeve: dict[str, dict[str, Any]] = {
            "CORE_EQUITY": {"candidates": 0, "passed_risk": 0, "ordered": 0, "filled": 0, "skips": Counter()},
            "NEWS_EQUITY": {"candidates": 0, "passed_risk": 0, "ordered": 0, "filled": 0, "skips": Counter()},
            "OPTIONS": {"candidates": 0, "passed_risk": 0, "ordered": 0, "filled": 0, "skips": Counter()},
        }

        has_research_funnel_events = False
        exec_stage_by_intent: dict[str, set[str]] = defaultdict(set)
        exec_code_by_intent: dict[str, tuple[str, str, str]] = {}

        if self.ledger is not None and hasattr(self.ledger, "journal"):
            try:
                for ev in self.ledger.journal.read(kinds={"research.entry_funnel", "execution.buy_funnel"}):
                    kind = ev.get("kind")
                    if kind == "research.entry_funnel":
                        ev_day = str(ev.get("session_date") or str(ev.get("ts") or "")[:10])[:10]
                        if start_iso <= ev_day <= end_iso:
                            has_research_funnel_events = True
                            screened += int(ev.get("screened") or 0)
                            thesis_written += int(ev.get("thesis_written") or 0)
                            passed_conviction += int(ev.get("passed_conviction") or 0)
                            rej = ev.get("rejected_by_reason")
                            if isinstance(rej, Mapping):
                                for r_key, r_cnt in rej.items():
                                    if r_cnt:
                                        research_skips[str(r_key)] += int(r_cnt)
                    elif kind == "execution.buy_funnel":
                        ts_et = self._iso_to_et_date(ev.get("ts"))
                        ev_day = ts_et.isoformat() if ts_et else str(ev.get("ts") or "")[:10]
                        if start_iso <= ev_day <= end_iso:
                            iid = str(ev.get("intent_id") or "")
                            stg = str(ev.get("stage") or "")
                            if iid and stg:
                                exec_stage_by_intent[iid].add(stg)
                                if stg in ("skipped", "held"):
                                    exec_code_by_intent[iid] = (
                                        stg,
                                        str(ev.get("code") or "other"),
                                        str(ev.get("origin") or ""),
                                    )
            except Exception:
                pass

        # Fallback to job_runs.detail_json['entry_funnel'] if journal had no research.entry_funnel rows
        if not has_research_funnel_events and self.ledger is not None and hasattr(self.ledger, "store"):
            try:
                rows = self.ledger.store.query(
                    "SELECT window_key, detail_json FROM job_runs WHERE detail_json IS NOT NULL"
                )
                for r in rows:
                    wk_key = str(r["window_key"] or "")[:10]
                    if start_iso <= wk_key <= end_iso:
                        det = json.loads(r["detail_json"]) if r["detail_json"] else {}
                        ef = det.get("entry_funnel") if isinstance(det, Mapping) else None
                        if isinstance(ef, Mapping):
                            has_research_funnel_events = True
                            screened += int(ef.get("screened") or 0)
                            thesis_written += int(ef.get("thesis_written") or 0)
                            passed_conviction += int(ef.get("passed_conviction") or 0)
                            rej = ef.get("rejected_by_reason")
                            if isinstance(rej, Mapping):
                                for r_key, r_cnt in rej.items():
                                    if r_cnt:
                                        research_skips[str(r_key)] += int(r_cnt)
            except Exception:
                pass

        # Aggregate buy OrderIntents in the window (distinct intent_id; partial fill counts once as filled)
        passed_risk_ids: set[str] = set()
        ordered_ids: set[str] = set()
        filled_ids: set[str] = set()
        partial_fill_count = 0
        seen_intents: set[str] = set()

        if self.ledger is not None and hasattr(self.ledger, "list_intents"):
            filled_order_ids: set[str] = set()
            filled_ledger_intents: set[str] = set()
            try:
                f_rows = self.ledger.store.query(
                    "SELECT broker_order_id, intent_id FROM fills WHERE side='buy'"
                )
                for fr in f_rows:
                    if fr["broker_order_id"]:
                        filled_order_ids.add(str(fr["broker_order_id"]))
                    if fr["intent_id"]:
                        filled_ledger_intents.add(str(fr["intent_id"]))
            except Exception:
                pass
            try:
                for it in self.ledger.list_intents():
                    if str(getattr(it, "side", "")).lower() != "buy":
                        continue
                    sleeve = str(getattr(it, "sleeve", "CORE_EQUITY") or "CORE_EQUITY")
                    if sleeve == "LEGACY_REPAIR":
                        continue
                    cr_dt = it.created_at.astimezone(ET).date()
                    it_dt = (it.updated_at.astimezone(ET).date() if it.updated_at is not None else cr_dt)
                    sess_dt = getattr(it, "session_date", None)
                    in_window = (
                        start_date <= it_dt <= session_date
                        or start_date <= cr_dt <= session_date
                        or (isinstance(sess_dt, date) and start_date <= sess_dt <= session_date)
                    )
                    if not in_window:
                        continue

                    iid = str(it.intent_id)
                    seen_intents.add(iid)
                    sl_bucket = sleeve if sleeve in by_sleeve else "CORE_EQUITY"
                    by_sleeve[sl_bucket]["candidates"] += 1

                    meta = it.meta if isinstance(getattr(it, "meta", None), Mapping) else {}
                    j_stages = exec_stage_by_intent.get(iid, set())
                    status_s = str(getattr(it.status, "value", it.status)).lower()
                    f_qty = getattr(it, "filled_qty", None) or ZERO
                    boid = str(getattr(it, "broker_order_id", "") or "")
                    has_fill = (
                        status_s in ("filled", "partial", "partially_filled")
                        or f_qty > ZERO
                        or "filled" in j_stages
                        or iid in filled_ledger_intents
                        or (bool(boid) and boid in filled_order_ids)
                    )
                    has_order = (
                        bool(boid)
                        or status_s in ("submitted", "partial", "partially_filled", "filled")
                        or "ordered" in j_stages
                        or has_fill
                    )
                    skip_code = str(meta.get("skip_code") or "")
                    has_risk_pass = (
                        has_fill
                        or has_order
                        or bool(meta.get("passed_risk"))
                        or status_s == "submitting"
                        or skip_code in ("broker_rejected", "unfilled_cancelled", "insufficient_buying_power")
                        or "passed_risk" in j_stages
                    )

                    if has_risk_pass:
                        passed_risk_ids.add(iid)
                        by_sleeve[sl_bucket]["passed_risk"] += 1
                    if has_order:
                        ordered_ids.add(iid)
                        by_sleeve[sl_bucket]["ordered"] += 1
                    if has_fill:
                        filled_ids.add(iid)
                        by_sleeve[sl_bucket]["filled"] += 1
                        if (
                            status_s in ("partial", "partially_filled", "cancelled", "expired")
                            or (getattr(it, "qty", None) and ZERO < f_qty < it.qty)
                        ):
                            partial_fill_count += 1
                        continue

                    # Unfilled intent: bucket skip or active hold once per distinct intent_id
                    code = skip_code
                    if not code and iid in exec_code_by_intent:
                        code = exec_code_by_intent[iid][1]

                    if status_s in ("queued", "submitting", "submitted"):
                        hold_key = code or ("awaiting_fill" if has_order else "queued")
                        held_counts[hold_key] += 1
                    else:
                        reason_key = self._classify_intent_skip_key(code, meta, it.last_error, status_s)
                        exec_skips[reason_key] += 1
                        by_sleeve[sl_bucket]["skips"][reason_key] += 1
            except Exception:
                pass

        # Include any execution.buy_funnel journal events whose intent_id wasn't in list_intents
        for iid, stgs in exec_stage_by_intent.items():
            if iid in seen_intents:
                continue
            if "passed_risk" in stgs or "ordered" in stgs or "filled" in stgs:
                passed_risk_ids.add(iid)
            if "ordered" in stgs or "filled" in stgs:
                ordered_ids.add(iid)
            if "filled" in stgs:
                filled_ids.add(iid)
            elif iid in exec_code_by_intent:
                stg, code, _ = exec_code_by_intent[iid]
                if stg == "held":
                    held_counts[code or "held"] += 1
                else:
                    exec_skips[code or "other"] += 1

        # Include Options structures opened/attempted in the window in the OPTIONS sleeve summary
        for s in self._load_options_structures():
            if self._option_origin(s) == "ADOPTED":
                continue
            created_iso = str(getattr(s, "created_at", "") or getattr(s, "opened_at", "") or "")[:10]
            if start_iso <= created_iso <= end_iso:
                by_sleeve["OPTIONS"]["candidates"] += 1
                st_name = str(getattr(getattr(s, "st", None), "value", getattr(s, "state", ""))).lower()
                if st_name not in ("rejected", "skipped", "failed"):
                    by_sleeve["OPTIONS"]["passed_risk"] += 1
                    by_sleeve["OPTIONS"]["ordered"] += 1
                if st_name in ("open", "live", "closed", "closing", "repair"):
                    by_sleeve["OPTIONS"]["filled"] += 1
                elif st_name in ("cancelled", "expired", "rejected", "skipped"):
                    by_sleeve["OPTIONS"]["skips"][f"options_{st_name}"] += 1

        # If research.entry_funnel hasn't run/emitted yet, keep funnel monotonic from observed theses/intents
        if not has_research_funnel_events and self.ledger is not None:
            try:
                th_rows = self.ledger.store.query(
                    "SELECT created_at FROM theses WHERE role != 'EXIT_CONFIRMATION'"
                )
                for r in th_rows:
                    th_dt = self._iso_to_et_date(r["created_at"])
                    if th_dt and start_date <= th_dt <= session_date:
                        thesis_written += 1
            except Exception:
                pass
            passed_conviction = max(passed_conviction, len(seen_intents))
            thesis_written = max(thesis_written, passed_conviction)
            screened = max(screened, thesis_written)

        passed_risk = len(passed_risk_ids)
        ordered = len(ordered_ids)
        filled = len(filled_ids)

        combined_skips: dict[str, int] = {}
        for k, v in research_skips.items():
            combined_skips[k] = combined_skips.get(k, 0) + v
        for k, v in exec_skips.items():
            combined_skips[k] = combined_skips.get(k, 0) + v

        return {
            "window": "week" if week else "day",
            "start_date": start_iso,
            "as_of_date": end_iso,
            "research_recorded": has_research_funnel_events,
            "screened": screened,
            "thesis_written": thesis_written,
            "passed_conviction": passed_conviction,
            "passed_risk": passed_risk,
            "ordered": ordered,
            "filled": filled,
            "partial_fill_count": partial_fill_count,
            "research_skips_by_reason": dict(research_skips),
            "execution_skips_by_reason": dict(exec_skips),
            "held_by_reason": dict(held_counts),
            "skips_by_reason": combined_skips,
            "by_sleeve": {
                k: {
                    "candidates": v["candidates"],
                    "passed_risk": v["passed_risk"],
                    "ordered": v["ordered"],
                    "filled": v["filled"],
                    "skips_by_reason": dict(v["skips"]),
                }
                for k, v in by_sleeve.items()
            },
        }

    @staticmethod
    def _iso_to_et_date(raw_ts: Any) -> date | None:
        if not raw_ts:
            return None
        try:
            dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ET).date()
        except Exception:
            return None

    @staticmethod
    def _classify_intent_skip_key(
        skip_code: str,
        meta: Mapping[str, Any],
        last_error: str | None,
        status_s: str,
    ) -> str:
        code = (skip_code or "").strip()
        if code == "intent_expired":
            held_by = str(meta.get("held_by") or "").strip()
            return f"intent_expired:{held_by}" if held_by else "intent_expired"
        if code in ("risk_refused", "risk_size_zero"):
            r_list = meta.get("risk_reasons")
            if isinstance(r_list, Sequence) and not isinstance(r_list, (str, bytes)) and r_list:
                first_r = str(r_list[0]).split(":")[0].strip()
                if first_r:
                    return f"{code}:{first_r}"
        if code:
            return code

        # Fallback classification from legacy text in last_error / meta['reason']
        raw = str(last_error or meta.get("reason") or status_s).lower()
        if "clean-up" in raw or "normalis" in raw:
            return "cleanup_pending"
        if "trading blocked" in raw or "reconcil" in raw:
            return "books_untrusted"
        if "market gate" in raw or "closed" in raw:
            return "market_closed"
        if "ran more than" in raw or "drift" in raw:
            return "price_ran_away"
        if "quote" in raw:
            return "quote_unavailable"
        if "margin_buffer" in raw or "excess" in raw:
            return "risk_refused:margin_buffer"
        if "buying_power_floor" in raw or "buying power" in raw:
            return "risk_refused:buying_power_floor"
        if "gross_cap" in raw or "gross" in raw:
            return "risk_refused:gross_cap"
        if "drawdown_no_new_borrowing" in raw or "borrowing" in raw:
            return "risk_refused:drawdown_no_new_borrowing"
        if "sector" in raw:
            return "risk_refused:sector_cap"
        if "issuer" in raw:
            return "risk_refused:issuer_cap"
        if "less than one share" in raw:
            return "risk_size_zero"
        if raw.startswith("risk:"):
            return "risk_refused"
        if status_s == "cancelled":
            return "unfilled_cancelled"
        if status_s == "expired":
            return "intent_expired"
        if status_s == "rejected":
            return "broker_rejected"
        return status_s or "other"

    @staticmethod
    def _humanize_funnel_reason(reason_key: str) -> str:
        labels = {
            # Research reasons
            "no_tradable_universe": "no tradable stock list loaded",
            "required_source_unavailable": "mandatory quote/news feed unavailable",
            "llm_budget": "AI research budget reached",
            "llm_no_available_route": "AI model routes cooling down/unavailable",
            "llm_upstream": "AI provider temporary error",
            "llm_no_grounding": "AI thesis lacked grounded sources",
            "llm_invalid_output": "AI output failed schema check",
            "non_entry_direction": "rated Hold / non-buy",
            "conviction_below_floor": "conviction below minimum floor",
            "target_session_closed": "market closed for target session",
            "not_tradable": "symbol not tradable",
            "quote_stale_or_unavailable": "live quote stale or unavailable",
            "add_thesis_not_strengthened": "held stock thesis did not strengthen",
            "active_thesis_changed": "superseded by newer thesis",
            "research_gate_refused": "research freshness gate refused",
            # Execution & risk reasons
            "cleanup_pending": "first-run account clean-up pending",
            "books_untrusted": "broker reconciliation lock",
            "market_closed": "outside regular market entry window",
            "price_ran_away": "price moved past limit from analysis",
            "quote_unavailable": "fresh quote unavailable at order time",
            "symbol_blocked": "symbol blocked by reconcile mismatch",
            "short_open": "existing short must be covered first",
            "research_stale": "thesis/inputs went stale before order",
            "position_changed": "position changed before order",
            "risk_size_zero": "sized below 1 share under risk caps",
            "risk_refused": "portfolio risk cap",
            "risk_controls_unavailable": "risk controls unavailable",
            "insufficient_buying_power": "insufficient overnight buying power",
            "broker_rejected": "rejected by broker",
            "unfilled_cancelled": "limit order unfilled by close",
            "intent_expired": "expired before entry window opened",
            "duplicate": "duplicate candidate skipped",
            "awaiting_fill": "open order waiting at broker",
            "queued": "waiting for entry window",
            # Risk sub-reasons
            "buying_power_floor": "2% overnight Reg T buying-power floor",
            "margin_buffer": "25% margin safety cushion floor",
            "margin_call": "broker margin call flag",
            "gross_cap": "max gross stock exposure cap",
            "drawdown_no_new_borrowing": "15% drawdown guard (no new borrowing)",
            "drawdown_pause": "20% drawdown stop",
            "options_buying_power": "options buying-power limit",
            "cash_secured_put_unfunded": "cash-secured put unfunded",
            "sector_cap": "40% sector cap",
            "issuer_cap": "15% single-stock cap",
            "cluster_cap": "48% theme cluster cap",
            "max_positions": "20-stock slot limit",
        }
        if ":" in reason_key:
            head, sub = reason_key.split(":", 1)
            head_lbl = labels.get(head, head.replace("_", " "))
            sub_lbl = labels.get(sub, sub.replace("_", " "))
            if head == "intent_expired":
                return f"expired while held by {sub_lbl}"
            return f"{head_lbl} ({sub_lbl})"
        if reason_key.startswith("llm_"):
            suffix = reason_key[4:].replace("_", " ")
            return labels.get(reason_key, f"AI model {suffix}")
        return labels.get(reason_key, redact_secrets_in_text(reason_key.replace("_", " ")))

    @classmethod
    def _format_entry_funnel_text(cls, funnel: Mapping[str, Any], *, locked: bool = False) -> str:
        sc_n = int(funnel.get("screened") or 0)
        th_n = int(funnel.get("thesis_written") or 0)
        cv_n = int(funnel.get("passed_conviction") or 0)
        rk_n = int(funnel.get("passed_risk") or 0)
        or_n = int(funnel.get("ordered") or 0)
        fl_n = int(funnel.get("filled") or 0)
        pt_n = int(funnel.get("partial_fill_count") or 0)
        fill_note = f" (incl. {pt_n} partial)" if pt_n > 0 else ""

        chain = (
            f"**Screened {sc_n}** → **Thesis Written {th_n}** → **Passed Conviction {cv_n}** → "
            f"**Passed Risk {rk_n}** → **Ordered {or_n}** → **Filled {fl_n}**{fill_note}"
        )
        lines = [chain]

        r_skips = funnel.get("research_skips_by_reason") or {}
        e_skips = funnel.get("execution_skips_by_reason") or {}
        held = funnel.get("held_by_reason") or {}
        all_skips = funnel.get("skips_by_reason") or {}

        if r_skips:
            top_r = sorted(r_skips.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:6]
            lines.append(
                "• **Research skips:** "
                + ", ".join(f"{cls._humanize_funnel_reason(k)} ({v})" for k, v in top_r)
            )
        if e_skips:
            top_e = sorted(e_skips.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:6]
            lines.append(
                "• **Order/risk skips:** "
                + ", ".join(f"{cls._humanize_funnel_reason(k)} ({v})" for k, v in top_e)
            )
        elif all_skips and not r_skips:
            top_a = sorted(all_skips.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:6]
            lines.append(
                "• **Skips by reason:** "
                + ", ".join(f"{cls._humanize_funnel_reason(k)} ({v})" for k, v in top_a)
            )
        if held:
            top_h = sorted(held.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:4]
            lines.append(
                "• **Still waiting / held:** "
                + ", ".join(f"{cls._humanize_funnel_reason(k)} ({v})" for k, v in top_h)
            )

        by_sl = funnel.get("by_sleeve") or {}
        sl_parts: list[str] = []
        for sl_key, sl_lbl in (
            ("CORE_EQUITY", "Core Stocks"),
            ("NEWS_EQUITY", "News Stocks"),
            ("OPTIONS", "Options"),
        ):
            b = by_sl.get(sl_key)
            if isinstance(b, Mapping) and (b.get("candidates") or b.get("filled")):
                sl_parts.append(
                    f"{sl_lbl}: {b.get('filled', 0)}/{b.get('candidates', 0)} filled"
                )
        if sl_parts:
            lines.append("• **By sleeve:** " + " | ".join(sl_parts))

        if sc_n == 0 and th_n == 0 and cv_n == 0 and rk_n == 0 and or_n == 0 and fl_n == 0 and not all_skips and not held:
            if locked:
                lines.append("• Zero buys attempted because new entries are currently locked (see Entry Lock Status above).")
            else:
                lines.append("• Zero buys: no scheduled or live-news research pass completed for this window yet.")
        elif fl_n == 0 and not all_skips and not held:
            if locked:
                lines.append("• Zero fills: new entries are currently held by an active safety lock.")
            else:
                lines.append("• Zero fills: no candidate cleared all entry gates in this window.")

        return "\n".join(lines)[:1024]


__all__ = [
    "ReportBuilder",
    "describe_drawdown_tier",
    "format_input_health_table",
]
