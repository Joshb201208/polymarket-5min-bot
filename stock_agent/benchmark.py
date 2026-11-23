"""Benchmark comparison (QQQ & SPY) and flow-adjusted portfolio metrics.

Rules (DECISIONS & PAPER_TEST):
- Portfolio returns come strictly from trusted ``equity_marks('close')``, live
  reconciled ``BookSnapshot.equity``, and ``cash_flows`` — NEVER from Alpaca
  ``account.last_equity`` or ``/v2/account/portfolio/history`` (both omit a held
  position on this account).
- Benchmark closes come from ``PriceService.get_daily_bars(['QQQ', 'SPY'])``
  (split-adjusted daily close bars, price-return basis) with intraday/brief
  fallback to ``PriceService.get_quotes(['QQQ', 'SPY'], purpose='brief')``.
- Launch baseline: ``2026-03-30`` (pre-launch close ``2026-03-27``).
- Never invent a 0.0% return when a mark or bar is missing; set ``None`` and
  expose ``stale=True`` with a plain-English reason.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from stock_agent.ledger import CashFlow, EquityMark
from stock_agent.performance import DEPOSIT_KINDS, net_deposits, previous_close

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
LAUNCH_DATE = date(2026, 3, 30)
LAUNCH_BASELINE_DATE = date(2026, 3, 27)
BENCHMARK_SYMBOLS = ("QQQ", "SPY")


def _q4(val: Decimal | float) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"))


def _pct_change(cur: Decimal | None, base: Decimal | None) -> Decimal | None:
    if cur is None or base is None or base <= ZERO:
        return None
    return ((cur - base) / base * Decimal(100)).quantize(Decimal("0.01"))


def flow_adjusted_return_pct(
    marks: Sequence[EquityMark],
    flows: Sequence[CashFlow],
    *,
    start_date: date | None = None,
    as_of_date: date | None = None,
    live_equity: Decimal | None = None,
    fallback_starting_capital: Decimal | None = None,
) -> Decimal | None:
    """Compute flow-adjusted time-weighted return (%) over [start_date, as_of_date].

    Chains sub-period returns across ``close`` marks, adjusting each interval
    for external cash flows (`CSD`, `CSW`, `JNLC`, `JNL`). Appends ``live_equity``
    as the terminal point when ``as_of_date`` is after the latest close mark.
    """
    closes = sorted(
        (
            m
            for m in marks
            if m.quality == "close"
            and (as_of_date is None or m.session_date <= as_of_date)
        ),
        key=lambda m: m.session_date,
    )

    points: list[tuple[date, Decimal]] = []
    if start_date is not None:
        # Anchor on the latest close on or before start_date if available
        prior_or_on = [m for m in closes if m.session_date <= start_date]
        after = [m for m in closes if m.session_date > start_date]
        if prior_or_on:
            points.append((prior_or_on[-1].session_date, prior_or_on[-1].equity))
        points.extend((m.session_date, m.equity) for m in after)
    else:
        points = [(m.session_date, m.equity) for m in closes]

    if live_equity is not None and as_of_date is not None:
        if not points or points[-1][0] < as_of_date:
            points.append((as_of_date, live_equity))
        elif points[-1][0] == as_of_date:
            points[-1] = (as_of_date, live_equity)

    if len(points) >= 2:
        growth = Decimal(1)
        for (d0, eq0), (d1, eq1) in zip(points, points[1:], strict=False):
            if eq0 <= ZERO:
                return None
            interval_flow = sum(
                (f.amount for f in flows if f.kind in DEPOSIT_KINDS and d0 < f.date <= d1),
                ZERO,
            )
            growth *= (eq1 - interval_flow) / eq0
        return ((growth - Decimal(1)) * Decimal(100)).quantize(Decimal("0.01"))

    # Fewer than 2 close points (e.g. Day 1 after cut-over): compare terminal equity to net deposits
    terminal_eq = live_equity if live_equity is not None else (points[-1][1] if points else None)
    if terminal_eq is None:
        return None
    dep = net_deposits(flows, as_of_date)
    if dep > ZERO and (start_date is None or start_date <= LAUNCH_DATE):
        return _pct_change(terminal_eq, dep)
    if dep == ZERO and fallback_starting_capital and fallback_starting_capital > ZERO and (
        start_date is None or start_date <= LAUNCH_DATE
    ):
        return _pct_change(terminal_eq, fallback_starting_capital)
    return None


@dataclass(frozen=True)
class SymbolBenchmark:
    symbol: str
    current_price: Decimal | None
    prev_close: Decimal | None
    baseline_close: Decimal | None
    baseline_date: date | None
    week_ago_close: Decimal | None
    cutover_close: Decimal | None
    session_return_pct: Decimal | None
    week_return_pct: Decimal | None
    cutover_return_pct: Decimal | None
    since_launch_return_pct: Decimal | None
    mark_source: str  # daily_bar_close | live_quote | missing
    stale: bool
    stale_reason: str = ""


@dataclass(frozen=True)
class BenchmarkSnapshot:
    as_of_date: date
    portfolio_session_pct: Decimal | None
    portfolio_week_pct: Decimal | None
    portfolio_cutover_pct: Decimal | None
    portfolio_since_launch_pct: Decimal | None
    qqq: SymbolBenchmark
    spy: SymbolBenchmark
    active_vs_qqq_session_pct: Decimal | None
    active_vs_qqq_launch_pct: Decimal | None
    active_vs_spy_launch_pct: Decimal | None
    mark_basis: str
    any_stale: bool
    paired_sessions: int
    realized_vol_ann_pct: Decimal | None
    beta_to_qqq: Decimal | None
    information_ratio_vs_qqq: Decimal | None


class BenchmarkTracker:
    """Computes QQQ/SPY benchmark returns and active risk/return statistics."""

    def __init__(
        self,
        price_service: Any | None = None,
        *,
        launch_baseline_date: date = LAUNCH_BASELINE_DATE,
    ) -> None:
        self.price_service = price_service
        self.launch_baseline_date = launch_baseline_date

    async def compute(
        self,
        *,
        as_of_date: date,
        marks: Sequence[EquityMark],
        flows: Sequence[CashFlow],
        live_equity: Decimal | None = None,
        cutover_date: date | None = None,
        fallback_starting_capital: Decimal | None = None,
    ) -> BenchmarkSnapshot:
        bars_by_sym: dict[str, list[Any]] = {}
        quotes_by_sym: dict[str, Any] = {}
        missing_quotes: dict[str, str] = {}

        if self.price_service is not None:
            try:
                raw_bars = await self.price_service.get_daily_bars(
                    list(BENCHMARK_SYMBOLS),
                    sessions=260,
                    start_date=self.launch_baseline_date,
                    end_date=as_of_date,
                )
                bars_by_sym = dict(raw_bars or {})
            except Exception as e:
                logger.warning("Benchmark daily bars fetch failed (%s): %s", type(e).__name__, e)

            # Fetch live/brief quotes whenever today's daily close bar is not yet settled
            need_quotes = [
                sym
                for sym in BENCHMARK_SYMBOLS
                if not bars_by_sym.get(sym) or bars_by_sym[sym][-1].session_date < as_of_date
            ]
            if need_quotes and hasattr(self.price_service, "get_quotes"):
                try:
                    qres = await self.price_service.get_quotes(need_quotes, purpose="brief")
                    quotes_by_sym = dict(getattr(qres, "quotes", {}) or {})
                    missing_quotes = dict(getattr(qres, "missing", {}) or {})
                except Exception as e:
                    logger.warning("Benchmark quote fetch failed (%s): %s", type(e).__name__, e)
                    for sym in need_quotes:
                        missing_quotes[sym] = f"{type(e).__name__}"

        qqq_bm = self._build_symbol_benchmark(
            "QQQ",
            as_of_date=as_of_date,
            bars=bars_by_sym.get("QQQ", []),
            quote=quotes_by_sym.get("QQQ"),
            missing_reason=missing_quotes.get("QQQ", ""),
            cutover_date=cutover_date,
        )
        spy_bm = self._build_symbol_benchmark(
            "SPY",
            as_of_date=as_of_date,
            bars=bars_by_sym.get("SPY", []),
            quote=quotes_by_sym.get("SPY"),
            missing_reason=missing_quotes.get("SPY", ""),
            cutover_date=cutover_date,
        )

        # Portfolio session return vs previous trusted close mark
        prev_mark = previous_close(marks, as_of_date)
        today_close = next((m for m in marks if m.quality == "close" and m.session_date == as_of_date), None)
        effective_eq = today_close.equity if today_close is not None else live_equity
        port_session_pct = _pct_change(effective_eq, prev_mark.equity) if (effective_eq is not None and prev_mark is not None) else None

        # Week start: 5 close marks back
        sorted_closes = sorted((m for m in marks if m.quality == "close" and m.session_date <= as_of_date), key=lambda m: m.session_date)
        week_anchor_date: date | None = None
        if len(sorted_closes) >= 6:
            week_anchor_date = sorted_closes[-6].session_date
        elif len(sorted_closes) >= 2:
            week_anchor_date = sorted_closes[0].session_date

        port_week_pct = (
            flow_adjusted_return_pct(
                marks,
                flows,
                start_date=week_anchor_date,
                as_of_date=as_of_date,
                live_equity=effective_eq,
            )
            if week_anchor_date is not None
            else None
        )
        port_cutover_pct = (
            flow_adjusted_return_pct(
                marks,
                flows,
                start_date=cutover_date,
                as_of_date=as_of_date,
                live_equity=effective_eq,
            )
            if cutover_date is not None
            else None
        )
        port_launch_pct = flow_adjusted_return_pct(
            marks,
            flows,
            start_date=self.launch_baseline_date,
            as_of_date=as_of_date,
            live_equity=effective_eq,
            fallback_starting_capital=fallback_starting_capital,
        )

        active_sess = (
            (port_session_pct - qqq_bm.session_return_pct).quantize(Decimal("0.01"))
            if port_session_pct is not None and qqq_bm.session_return_pct is not None
            else None
        )
        active_qqq_launch = (
            (port_launch_pct - qqq_bm.since_launch_return_pct).quantize(Decimal("0.01"))
            if port_launch_pct is not None and qqq_bm.since_launch_return_pct is not None
            else None
        )
        active_spy_launch = (
            (port_launch_pct - spy_bm.since_launch_return_pct).quantize(Decimal("0.01"))
            if port_launch_pct is not None and spy_bm.since_launch_return_pct is not None
            else None
        )

        paired_n, vol_ann, beta_qqq, ir_qqq = self._compute_paired_stats(
            sorted_closes, flows, bars_by_sym.get("QQQ", [])
        )

        return BenchmarkSnapshot(
            as_of_date=as_of_date,
            portfolio_session_pct=port_session_pct,
            portfolio_week_pct=port_week_pct,
            portfolio_cutover_pct=port_cutover_pct,
            portfolio_since_launch_pct=port_launch_pct,
            qqq=qqq_bm,
            spy=spy_bm,
            active_vs_qqq_session_pct=active_sess,
            active_vs_qqq_launch_pct=active_qqq_launch,
            active_vs_spy_launch_pct=active_spy_launch,
            mark_basis="split_adjusted_price_close",
            any_stale=qqq_bm.stale or spy_bm.stale,
            paired_sessions=paired_n,
            realized_vol_ann_pct=vol_ann,
            beta_to_qqq=beta_qqq,
            information_ratio_vs_qqq=ir_qqq,
        )

    def _build_symbol_benchmark(
        self,
        symbol: str,
        *,
        as_of_date: date,
        bars: Sequence[Any],
        quote: Any | None,
        missing_reason: str,
        cutover_date: date | None,
    ) -> SymbolBenchmark:
        ordered = sorted(
            (b for b in bars if getattr(b, "session_date", None) is not None and b.session_date <= as_of_date),
            key=lambda b: b.session_date,
        )

        cur_price: Decimal | None = None
        prev_close: Decimal | None = None
        mark_source = "missing"
        stale = False
        stale_reason = ""

        if ordered and ordered[-1].session_date == as_of_date:
            cur_price = Decimal(str(ordered[-1].close))
            prev_close = Decimal(str(ordered[-2].close)) if len(ordered) >= 2 else None
            mark_source = "daily_bar_close"
        elif quote is not None and getattr(quote, "price", None) is not None and Decimal(str(quote.price)) > ZERO:
            cur_price = Decimal(str(quote.price))
            prev_close = (
                Decimal(str(quote.prev_close))
                if getattr(quote, "prev_close", None)
                else (Decimal(str(ordered[-1].close)) if ordered else None)
            )
            mark_source = f"live_quote:{getattr(quote, 'source', 'provider')}"
        elif ordered:
            cur_price = Decimal(str(ordered[-1].close))
            prev_close = Decimal(str(ordered[-2].close)) if len(ordered) >= 2 else None
            mark_source = "prior_bar_close"
            stale = True
            stale_reason = f"latest bar is {ordered[-1].session_date.isoformat()}" + (
                f" ({missing_reason})" if missing_reason else ""
            )
        else:
            stale = True
            stale_reason = missing_reason or ("price_service_unwired" if self.price_service is None else "no_bars_or_quote")

        # Launch baseline: bar on or closest on/before launch_baseline_date; never silently substitute a much later bar without flagging
        baseline_bar = None
        on_or_before = [b for b in ordered if b.session_date <= self.launch_baseline_date]
        if on_or_before:
            baseline_bar = on_or_before[-1]
        elif ordered and (ordered[0].session_date - self.launch_baseline_date).days <= 5:
            baseline_bar = ordered[0]

        baseline_close = Decimal(str(baseline_bar.close)) if baseline_bar is not None else None
        baseline_dt = baseline_bar.session_date if baseline_bar is not None else None
        if baseline_close is None and ordered:
            stale = True
            stale_reason = stale_reason or f"history starts {ordered[0].session_date.isoformat()}, after launch baseline {self.launch_baseline_date.isoformat()}"

        # Week-ago close (5 completed sessions back)
        week_bar = None
        prior_bars = [b for b in ordered if b.session_date < as_of_date]
        if len(prior_bars) >= 5:
            week_bar = prior_bars[-5]
        elif prior_bars:
            week_bar = prior_bars[0]
        week_close = Decimal(str(week_bar.close)) if week_bar is not None else None

        # Cut-over close
        cutover_close: Decimal | None = None
        if cutover_date is not None:
            cut_bars = [b for b in ordered if b.session_date <= cutover_date]
            if cut_bars:
                cutover_close = Decimal(str(cut_bars[-1].close))

        return SymbolBenchmark(
            symbol=symbol,
            current_price=cur_price,
            prev_close=prev_close,
            baseline_close=baseline_close,
            baseline_date=baseline_dt,
            week_ago_close=week_close,
            cutover_close=cutover_close,
            session_return_pct=_pct_change(cur_price, prev_close),
            week_return_pct=_pct_change(cur_price, week_close),
            cutover_return_pct=_pct_change(cur_price, cutover_close),
            since_launch_return_pct=_pct_change(cur_price, baseline_close),
            mark_source=mark_source,
            stale=stale,
            stale_reason=stale_reason,
        )

    @staticmethod
    def _compute_paired_stats(
        closes: Sequence[EquityMark],
        flows: Sequence[CashFlow],
        qqq_bars: Sequence[Any],
    ) -> tuple[int, Decimal | None, Decimal | None, Decimal | None]:
        if len(closes) < 2 or len(qqq_bars) < 2:
            return 0, None, None, None

        qqq_ordered = sorted(qqq_bars, key=lambda b: b.session_date)
        qqq_ret_by_date: dict[date, float] = {}
        for b0, b1 in zip(qqq_ordered, qqq_ordered[1:], strict=False):
            c0, c1 = float(b0.close), float(b1.close)
            if c0 > 0:
                qqq_ret_by_date[b1.session_date] = (c1 - c0) / c0

        paired: list[tuple[float, float]] = []
        for m0, m1 in zip(closes, closes[1:], strict=False):
            if m0.equity <= ZERO:
                continue
            f = sum(
                (x.amount for x in flows if x.kind in DEPOSIT_KINDS and m0.session_date < x.date <= m1.session_date),
                ZERO,
            )
            rp = float((m1.equity - f) / m0.equity - Decimal(1))
            rq = qqq_ret_by_date.get(m1.session_date)
            if rq is not None:
                paired.append((rp, rq))

        n = len(paired)
        if n < 5:
            return n, None, None, None

        rp_vals = [p[0] for p in paired]
        rq_vals = [p[1] for p in paired]
        mean_p = sum(rp_vals) / n
        mean_q = sum(rq_vals) / n
        var_p = sum((x - mean_p) ** 2 for x in rp_vals) / (n - 1)
        var_q = sum((x - mean_q) ** 2 for x in rq_vals) / (n - 1)
        cov_pq = sum((x - mean_p) * (y - mean_q) for x, y in paired) / (n - 1)

        vol_ann = _q4(math.sqrt(max(var_p, 0.0)) * math.sqrt(252.0) * 100.0)
        beta = _q4(cov_pq / var_q) if var_q > 1e-12 else None

        ir: Decimal | None = None
        if n >= 20:
            active = [x - y for x, y in paired]
            mean_act = sum(active) / n
            var_act = sum((a - mean_act) ** 2 for a in active) / (n - 1)
            if var_act > 1e-12:
                ir = _q4((mean_act / math.sqrt(var_act)) * math.sqrt(252.0))

        return n, vol_ann, beta, ir


__all__ = [
    "BENCHMARK_SYMBOLS",
    "LAUNCH_BASELINE_DATE",
    "LAUNCH_DATE",
    "BenchmarkSnapshot",
    "BenchmarkTracker",
    "SymbolBenchmark",
    "flow_adjusted_return_pct",
]
