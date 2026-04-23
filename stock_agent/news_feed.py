"""Finnhub company news feed.

Free-tier endpoint: /company-news?symbol=X&from=YYYY-MM-DD&to=YYYY-MM-DD

We fetch the last 7 days of headlines per symbol, dedupe by headline,
and return a compact context string for the LLM prompt.
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
MAX_HEADLINES_PER_SYMBOL = 8
LOOKBACK_DAYS = 7
HTTP_TIMEOUT = 15.0


async def _fetch_company_news(symbol: str) -> list[dict]:
    api_key = Config.FINNHUB_API_KEY
    if not api_key:
        return []
    today = datetime.now(timezone.utc).date()
    params = {
        "symbol": symbol.upper(),
        "from": (today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
        "to": today.isoformat(),
        "token": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"{FINNHUB_BASE}/company-news", params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Finnhub company news fetch failed for %s: %s", symbol, exc)
        return []
    if not isinstance(data, list):
        return []
    return data


def _dedupe_and_trim(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    cleaned: list[dict] = []
    # Finnhub returns newest-first already, but sort defensively.
    items = sorted(items, key=lambda x: x.get("datetime", 0), reverse=True)
    for item in items:
        headline = (item.get("headline") or "").strip()
        if not headline or headline.lower() in seen:
            continue
        seen.add(headline.lower())
        cleaned.append(item)
        if len(cleaned) >= MAX_HEADLINES_PER_SYMBOL:
            break
    return cleaned


async def build_news_lookup(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch recent company news for every symbol concurrently."""
    if not symbols or not Config.FINNHUB_API_KEY:
        return {}
    symbols = [s.upper() for s in symbols if s]
    tasks = [_fetch_company_news(s) for s in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    lookup: dict[str, list[dict]] = {}
    for sym, result in zip(symbols, results):
        if isinstance(result, Exception):
            continue
        lookup[sym] = _dedupe_and_trim(result)
    return lookup


def format_news_context(symbol: str, items: list[dict] | None) -> str:
    if not items:
        return ""
    lines = [f"RECENT NEWS ({symbol}, last {LOOKBACK_DAYS} days):"]
    for item in items:
        ts = item.get("datetime")
        try:
            when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            when = "?"
        source = (item.get("source") or "").strip() or "?"
        headline = (item.get("headline") or "").strip()
        if not headline:
            continue
        lines.append(f"- [{when} / {source}] {headline}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def count_headlines(items: list[dict] | None) -> int:
    return len(items) if items else 0
