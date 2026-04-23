"""Finnhub analyst recommendation trends + insider transactions.

Endpoints:
  /stock/recommendation?symbol=X           -> monthly analyst rec snapshots
  /stock/insider-transactions?symbol=X     -> recent insider buys/sells (90d window by default)

We collapse each into a compact per-symbol dict plus a context string for the
LLM prompt. Conviction is only nudged by strong, unambiguous signals.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from stock_agent.config import Config

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
HTTP_TIMEOUT = 15.0
INSIDER_LOOKBACK_DAYS = 90
MAX_INSIDER_ROWS = 10


async def _fetch(path: str, params: dict) -> Any:
    api_key = Config.FINNHUB_API_KEY
    if not api_key:
        return None
    params = {**params, "token": api_key}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"{FINNHUB_BASE}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Finnhub %s fetch failed (%s): %s", path, params.get("symbol"), exc)
        return None


def _normalise_recs(raw: Any) -> dict:
    """Finnhub returns a list of monthly snapshots, newest first."""
    if not isinstance(raw, list) or not raw:
        return {}
    latest = raw[0] or {}
    prev = raw[1] if len(raw) > 1 else {}

    def _int(x: Any) -> int:
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    latest_counts = {
        "strong_buy": _int(latest.get("strongBuy")),
        "buy": _int(latest.get("buy")),
        "hold": _int(latest.get("hold")),
        "sell": _int(latest.get("sell")),
        "strong_sell": _int(latest.get("strongSell")),
        "period": latest.get("period"),
    }
    prev_counts = {
        "strong_buy": _int(prev.get("strongBuy")),
        "buy": _int(prev.get("buy")),
        "hold": _int(prev.get("hold")),
        "sell": _int(prev.get("sell")),
        "strong_sell": _int(prev.get("strongSell")),
        "period": prev.get("period"),
    }
    # Delta in net-bullish analysts (strong_buy+buy) - (sell+strong_sell)
    def _net(c: dict) -> int:
        return c["strong_buy"] + c["buy"] - c["sell"] - c["strong_sell"]

    return {
        "latest": latest_counts,
        "previous": prev_counts,
        "net_bullish_delta": _net(latest_counts) - _net(prev_counts),
    }


def _normalise_insiders(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    rows = raw.get("data") or []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=INSIDER_LOOKBACK_DAYS)).date()
    filtered = []
    buys_shares = 0
    sells_shares = 0
    for row in rows:
        tx_date = row.get("transactionDate")
        try:
            d = datetime.strptime(tx_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if d < cutoff:
            continue
        change = row.get("change") or 0
        try:
            change_int = int(change)
        except (TypeError, ValueError):
            change_int = 0
        if change_int > 0:
            buys_shares += change_int
        elif change_int < 0:
            sells_shares += -change_int
        filtered.append(
            {
                "name": row.get("name"),
                "date": tx_date,
                "change": change_int,
                "code": row.get("transactionCode"),
                "price": row.get("transactionPrice"),
            }
        )
    filtered.sort(key=lambda r: r.get("date") or "", reverse=True)
    return {
        "rows": filtered[:MAX_INSIDER_ROWS],
        "buys_shares": buys_shares,
        "sells_shares": sells_shares,
        "lookback_days": INSIDER_LOOKBACK_DAYS,
    }


async def _fetch_one(symbol: str) -> dict:
    rec_raw, ins_raw = await asyncio.gather(
        _fetch("/stock/recommendation", {"symbol": symbol.upper()}),
        _fetch("/stock/insider-transactions", {"symbol": symbol.upper()}),
    )
    recs = _normalise_recs(rec_raw)
    insiders = _normalise_insiders(ins_raw)
    if not recs and not insiders.get("rows"):
        return {}
    return {"recommendations": recs, "insiders": insiders}


async def build_analyst_lookup(symbols: list[str]) -> dict[str, dict]:
    if not symbols or not Config.FINNHUB_API_KEY:
        return {}
    symbols = [s.upper() for s in symbols if s]
    results = await asyncio.gather(
        *[_fetch_one(s) for s in symbols], return_exceptions=True
    )
    lookup: dict[str, dict] = {}
    for sym, res in zip(symbols, results):
        if isinstance(res, Exception) or not res:
            continue
        lookup[sym] = res
    return lookup


def format_analyst_context(symbol: str, data: dict | None) -> str:
    if not data:
        return ""
    lines: list[str] = []
    recs = data.get("recommendations") or {}
    latest = recs.get("latest") or {}
    if latest:
        period = latest.get("period") or "?"
        lines.append(
            f"ANALYST RATINGS ({symbol}, {period}): "
            f"Strong Buy {latest['strong_buy']}, Buy {latest['buy']}, "
            f"Hold {latest['hold']}, Sell {latest['sell']}, "
            f"Strong Sell {latest['strong_sell']}"
        )
        delta = recs.get("net_bullish_delta")
        if isinstance(delta, int) and delta != 0:
            sign = "+" if delta > 0 else ""
            lines.append(f"- Net bullish change vs prior month: {sign}{delta}")

    insiders = data.get("insiders") or {}
    rows = insiders.get("rows") or []
    if rows:
        buys = insiders.get("buys_shares", 0)
        sells = insiders.get("sells_shares", 0)
        lb = insiders.get("lookback_days", INSIDER_LOOKBACK_DAYS)
        lines.append(
            f"INSIDER ACTIVITY ({symbol}, last {lb}d): "
            f"{buys:,} shares bought / {sells:,} shares sold"
        )
        for r in rows[:5]:
            name = (r.get("name") or "?")[:40]
            d = r.get("date") or "?"
            ch = r.get("change") or 0
            code = r.get("code") or ""
            price = r.get("price")
            price_str = f" @ ${price:.2f}" if isinstance(price, (int, float)) else ""
            direction = "BUY" if ch > 0 else "SELL"
            lines.append(
                f"  - {d} {direction} {abs(ch):,} sh{price_str} ({name}, {code})"
            )
    return "\n".join(lines)


def adjust_conviction_with_analysts(conviction: int, data: dict | None) -> int:
    """Small conviction nudge from analyst rec delta + insider net buying.

    Rules (additive, each clamped):
      - net_bullish_delta >= +3              -> +1
      - net_bullish_delta <= -3              -> -1
      - insider net buying share count > 0   -> +1 (only if recs not already -1)
      - insider net selling ratio > 5:1      -> -1
    Final clamped to [1, 10].
    """
    if not data:
        return conviction
    delta = 0
    recs = data.get("recommendations") or {}
    nbd = recs.get("net_bullish_delta")
    if isinstance(nbd, int):
        if nbd >= 3:
            delta += 1
        elif nbd <= -3:
            delta -= 1

    insiders = data.get("insiders") or {}
    buys = insiders.get("buys_shares") or 0
    sells = insiders.get("sells_shares") or 0
    if buys > 0 and sells == 0 and delta >= 0:
        delta += 1
    elif sells > 0 and (buys == 0 or sells / max(buys, 1) >= 5.0):
        delta -= 1

    return max(1, min(10, conviction + delta))
