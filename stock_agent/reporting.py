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
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from stock_agent.alerts import redact_secrets_in_text
from stock_agent.benchmark import MIN_PAIRED_IR, BenchmarkSnapshot, BenchmarkTracker
from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, EquityMark, Holding, Ledger
from stock_agent.performance import (
    SHADOW_POST_STOP_SESSIONS,
    day_pnl,
    drawdown,
    net_deposits,
    previous_close,
    realised_fifo,
    twr,
)

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


def describe_drawdown_tier(drawdown_pct: Decimal | float | None, ledger: Ledger | None = None) -> tuple[str, str]:
    """Map current drawdown (%) to PAPER_TEST control tier and plain-English label."""
    halted = bool(ledger.risk_state_get("drawdown_halted", False)) if ledger is not None else False
    dd = float(drawdown_pct) if drawdown_pct is not None else 0.0
    if halted or dd <= -20.0:
        return (
            "halt_20pct",
            "20% Drawdown Stop — new entries paused until drawdown stays above -15% for 5 closes",
        )
    if dd <= -15.0:
        return (
            "throttle_15pct",
            "15% Drawdown Guard — new stock sizes cut in half; new option trades paused",
        )
    if dd <= -10.0:
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
        logger.warning("Could not read input health rows: %s", e)
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


def _return_equity(snap: Any) -> Decimal | None:
    """Equity usable as a return/drawdown terminal: only from a live snapshot."""
    if snap is None or str(getattr(snap, "quality", "")) != "live":
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
        self.calendar = calendar
        self.research_store = research_store
        self.benchmark = benchmark_tracker or BenchmarkTracker(price_service)
        self._clock = clock or _default_now

    def _now_et(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(ET)

    def _load_options_structures(self) -> list[Any]:
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
            logger.warning("Could not read options structures for report: %s", e)
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
    def _option_marks(snap: BookSnapshot | None) -> dict[str, float]:
        if snap is None:
            return {}
        return {leg.occ_symbol: float(leg.price) for leg in snap.legs if leg.price_ok and leg.price is not None}

    @staticmethod
    def _option_risk(structs: Sequence[Any]) -> tuple[float, int]:
        """(sum of known max loss, count of live structures whose max loss is unknown).
        Unknown risk is never counted as zero."""
        total, unknown = 0.0, 0
        for s in structs:
            ml = getattr(s, "max_loss", None)
            if ml is None:
                unknown += 1
            else:
                total += float(ml)
        return total, unknown

    @staticmethod
    def _fmt_option_risk(total: float, unknown: int) -> str:
        if not unknown:
            return _fmt_money(total)
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

    def _benchmark_lines(self, bm: BenchmarkSnapshot, *, include_week: bool = False) -> str:
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
    ) -> list[dict[str, Any]]:
        """Build plain-English morning brief embeds from reconciled state."""
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
        ret_eq = _return_equity(snap)
        overnight_pnl, overnight_pct = (
            day_pnl(marks, ret_eq, session_date, flows) if ret_eq is not None else (None, None)
        )
        dd_pct, hwm = drawdown(marks, ret_eq)
        _, dd_desc = describe_drawdown_tier(dd_pct, self.ledger)

        bm = await self.benchmark.compute(
            as_of_date=session_date,
            marks=marks,
            flows=flows,
            live_equity=ret_eq,
            fallback_starting_capital=self._fallback_starting_capital(),
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
            {"name": "Cash Reserve", "value": f"{_fmt_money(snap.cash)} ({cash_pct:.1f}%)", "inline": True},
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
                "value": f"{dd_desc}" + (f" (Drawdown {_fmt_pct(dd_pct)} from {_fmt_money(hwm)} peak)" if dd_pct is not None else ""),
                "inline": False,
            },
            {
                "name": "Performance vs Market (QQQ & SPY)",
                "value": self._benchmark_lines(bm),
                "inline": False,
            },
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
            total_max_loss, unknown_risk = self._option_risk(structs)
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
                opt_lines.append(
                    f"**Total Modeled Option Max Loss:** {self._fmt_option_risk(total_max_loss, unknown_risk)} "
                    f"({max_loss_pct:.1f}% of account vs {self._option_total_limit_pct():.1f}% limit"
                    + (", excluding unknown" if unknown_risk else "") + ")"
                )
            else:
                for leg in snap.legs:
                    opt_lines.append(
                        f"• **{leg.occ_symbol}** ({float(leg.qty):+g} contracts) — value {_fmt_money(leg.market_value)}"
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
    ) -> list[dict[str, Any]]:
        """Build honest end-of-day summary embeds from close mark & reconciled book."""
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        session_date = now_et.date()
        date_label = now_et.strftime("%b %d, %Y (%a)")

        snap = snapshot or (self.ledger.last_snapshot() if self.ledger else None)
        marks = self.ledger.equity_marks("close") if self.ledger else []
        flows = self.ledger.cash_flows() if self.ledger else []
        today_mark: EquityMark | None = next((m for m in marks if m.session_date == session_date), None)

        if today_mark is None and snap is None:
            return [
                {
                    "title": f"📊 Daily Summary — {date_label}",
                    "description": "Could not produce end-of-day summary because no verified broker snapshot was available.",
                    "color": 0xEF4444,
                }
            ]

        equity = today_mark.equity if today_mark is not None else snap.equity  # type: ignore[union-attr]
        cash = today_mark.cash if today_mark is not None else snap.cash  # type: ignore[union-attr]
        long_mv = today_mark.long_mv if today_mark is not None else snap.long_mv  # type: ignore[union-attr]
        opt_mv = today_mark.options_mv if today_mark is not None else snap.options_mv  # type: ignore[union-attr]
        mark_quality = "trusted close" if today_mark is not None else f"{snap.quality} snapshot (close mark pending)"  # type: ignore[union-attr]

        # equity used for any return/drawdown: today's trusted close, else a LIVE snapshot only
        ret_eq = today_mark.equity if today_mark is not None else _return_equity(snap)
        sess_pnl, sess_pct = (
            (close_report.day_pnl, close_report.day_pnl_pct)
            if close_report is not None and getattr(close_report, "day_pnl", None) is not None
            else (day_pnl(marks, ret_eq, session_date, flows) if ret_eq is not None else (None, None))
        )
        dd_pct, hwm = drawdown(marks, ret_eq)
        _, dd_desc = describe_drawdown_tier(dd_pct, self.ledger)
        twr_pct = twr(marks, flows)
        dep = net_deposits(flows, session_date)
        total_ret_pct = (
            ((ret_eq - dep) / dep * Decimal(100)).quantize(Decimal("0.01"))
            if dep > ZERO and ret_eq is not None
            else None
        )

        bm = await self.benchmark.compute(
            as_of_date=session_date,
            marks=marks,
            flows=flows,
            live_equity=ret_eq,
            fallback_starting_capital=self._fallback_starting_capital(),
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
        opt_open_max_loss, opt_unknown_risk = self._option_risk(live_structs)

        # Today's stock trades & closed win count (sells only — never count buys as wins!)
        did_lines, passed_lines = self._daily_decisions_summary(session_date, closed_today_structs)

        color = 0x16A34A if (sess_pnl is None or sess_pnl >= ZERO) else 0xDC2626
        eq_f = float(equity)
        exp_pct = (float(long_mv + abs(opt_mv)) / eq_f * 100.0) if eq_f > 0 else 0.0

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
                "value": f"TWR {_fmt_pct(twr_pct)} | Net vs Deposits {_fmt_pct(total_ret_pct or bm.portfolio_since_launch_pct)}",
                "inline": True,
            },
            {"name": "Cash & Exposure", "value": f"Cash {_fmt_money(cash)} | Invested {exp_pct:.1f}%", "inline": True},
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
                "value": f"{dd_desc}" + (f" (Current {_fmt_pct(dd_pct)}, Peak {_fmt_money(hwm)})" if dd_pct is not None else ""),
                "inline": False,
            },
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
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build the weekly paper-test scorecard embeds and structured breakdown dict."""
        now_et = as_of.astimezone(ET) if as_of else self._now_et()
        session_date = now_et.date()
        snap = snapshot or (self.ledger.last_snapshot() if self.ledger else None)
        marks = self.ledger.equity_marks("close") if self.ledger else []
        flows = self.ledger.cash_flows() if self.ledger else []

        live_eq = snap.equity if snap is not None else (marks[-1].equity if marks else ZERO)
        ret_live_eq = _return_equity(snap) if snap is not None else None
        if ret_live_eq is not None and ret_live_eq <= ZERO:
            ret_live_eq = None
        cash_now = snap.cash if snap is not None else (marks[-1].cash if marks else ZERO)
        gross_exp_pct = (
            float(snap.gross_exposure_pct * 100)
            if snap is not None
            else (
                float((marks[-1].long_mv + abs(marks[-1].short_mv)) / marks[-1].equity * 100)
                if marks and marks[-1].equity > ZERO
                else 0.0
            )
        )

        bm = await self.benchmark.compute(
            as_of_date=session_date,
            marks=marks,
            flows=flows,
            live_equity=ret_live_eq,
            cutover_date=cutover_date,
            fallback_starting_capital=self._fallback_starting_capital(),
        )

        # Drawdown & HWM dates
        dd_now, hwm = drawdown(marks, ret_live_eq)
        dd_tier, dd_label = describe_drawdown_tier(dd_now, self.ledger)
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
                cp = float(m.cash / m.equity * 100)
                ep = float((m.long_mv + abs(m.short_mv) + abs(m.options_mv)) / m.equity * 100)
                peak_cash_pct = cp if peak_cash_pct is None else max(peak_cash_pct, cp)
                min_cash_pct = cp if min_cash_pct is None else min(min_cash_pct, cp)
                peak_exp_pct = ep if peak_exp_pct is None else max(peak_exp_pct, ep)

        # Sleeves & Orders breakdown
        sleeves, options_by_origin, by_conviction, orders_stats = self._compile_sleeves_and_orders(snap)
        news_inputs = self._compile_news_and_inputs(session_date)
        learned = self._compile_what_we_learned(bm)

        structs = [s for s in self._load_options_structures() if getattr(s, "st", None) and s.st.live]
        open_opt_max_loss, open_opt_unknown_risk = self._option_risk(structs)
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
                "cash": float(cash_now),
                "cash_pct": round(float(cash_now / live_eq * 100), 2) if live_eq > ZERO else 0.0,
                "gross_exposure_pct": round(gross_exp_pct, 2),
                "net_deposits": float(net_deposits(flows, session_date)),
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
                "drawdown_pct": float(dd_now) if dd_now is not None else None,
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

        marks = self._option_marks(snap)
        for b in (sleeves["OPTIONS"], *options_by_origin.values()):
            b["unrealized_unknown_count"] = 0
        for s in self._load_options_structures():
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
        e1 = {
            "title": f"📈 Weekly Paper-Test Scorecard — {now_et.strftime('%b %d, %Y')}",
            "description": (
                f"Mode: **{acc['mode']}** (`{acc['build_id']}`) | Sessions recorded: **{acc['sessions_recorded']}**. "
                "All returns adjust for cash deposits/withdrawals and use verified close marks."
            ),
            "color": 0x2563EB,
            "fields": [
                {"name": "Account Value & Cash", "value": f"**{_fmt_money(acc['equity'], decimals=2)}** (Cash {acc['cash_pct']:.1f}%, Gross Exp {acc['gross_exposure_pct']:.1f}%)", "inline": False},
                {"name": "Returns vs QQQ & SPY", "value": self._benchmark_lines(bm, include_week=True), "inline": False},
                {
                    "name": "Drawdown & Risk Controls",
                    "value": (
                        f"{dd_label}\n"
                        f"Current: {_fmt_pct(acc['drawdown_pct'])} | Worst Close Drawdown: {_fmt_pct(acc['max_drawdown_pct'])}"
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

        e2 = {
            "title": "🧩 Results by Sleeve, Execution & News Health",
            "color": 0x0D9488,
            "fields": [
                {"name": "Results by Strategy Sleeve", "value": "\n".join(sleeve_lines), "inline": False},
                {"name": "Results by Conviction Tier (Sample Size, Not Probability)", "value": "\n".join(conv_lines), "inline": False},
                {"name": "Order & Stop-Loss Reliability", "value": ord_txt, "inline": False},
                {"name": "Live News & Input Reliability", "value": ni_txt, "inline": False},
            ],
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


__all__ = [
    "ReportBuilder",
    "describe_drawdown_tier",
    "format_input_health_table",
]
