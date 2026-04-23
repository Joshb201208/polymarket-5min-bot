"""Finnhub news + social sentiment scores.

Endpoints:
  /news-sentiment?symbol=X        -> aggregate news sentiment (bearishPercent / bullishPercent,
                                     sentiment.bullishPercent, companyNewsScore, buzz metrics)

We fold the two scores into a single conviction delta, mirroring the
TipRanks pattern so the scheduler can apply it uniformly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from stock_agent.config import Config

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
HTTP_TIMEOUT = 15.0


async def _fetch_news_sentiment(symbol: str) -> dict | None:
    api_key = Config.FINNHUB_API_KEY
    if not api_key:
        return None
    params = {"symbol": symbol.upper(), "token": api_key}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"{FINNHUB_BASE}/news-sentiment", params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Finnhub news-sentiment fetch failed for %s: %s", symbol, exc)
        return None


def _normalise_news(raw: dict | None) -> dict:
    if not raw:
        return {}
    sentiment = raw.get("sentiment") or {}
    buzz = raw.get("buzz") or {}
    return {
        "bullish_pct": sentiment.get("bullishPercent"),
        "bearish_pct": sentiment.get("bearishPercent"),
        "company_news_score": raw.get("companyNewsScore"),
        "sector_avg_news_score": raw.get("sectorAverageNewsScore"),
        "weekly_articles": buzz.get("articlesInLastWeek"),
        "buzz": buzz.get("buzz"),
    }


async def build_sentiment_lookup(symbols: list[str]) -> dict[str, dict]:
    """Fetch news sentiment for each symbol concurrently."""
    if not symbols or not Config.FINNHUB_API_KEY:
        return {}
    symbols = [s.upper() for s in symbols if s]
    news_task = asyncio.gather(
        *[_fetch_news_sentiment(s) for s in symbols], return_exceptions=True
    )
    news_results = await news_task

    lookup: dict[str, dict] = {}
    for sym, news_raw in zip(symbols, news_results):
        news_clean = (
            _normalise_news(news_raw) if not isinstance(news_raw, Exception) else {}
        )
        if not news_clean:
            continue
        lookup[sym] = news_clean
    return lookup


def _pct(v: Any) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v) * 100:.1f}%" if float(v) <= 1.0 else f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "?"


def format_sentiment_context(symbol: str, data: dict | None) -> str:
    if not data:
        return ""
    lines = [f"SENTIMENT ({symbol}):"]
    bullish = data.get("bullish_pct")
    bearish = data.get("bearish_pct")
    if bullish is not None or bearish is not None:
        lines.append(
            f"- News: {_pct(bullish)} bullish / {_pct(bearish)} bearish"
        )
    cns = data.get("company_news_score")
    sas = data.get("sector_avg_news_score")
    if cns is not None:
        sector_note = f" (sector avg {sas:.2f})" if isinstance(sas, (int, float)) else ""
        try:
            lines.append(f"- Company news score: {float(cns):.2f}{sector_note}")
        except (TypeError, ValueError):
            pass
    buzz = data.get("buzz")
    weekly = data.get("weekly_articles")
    if weekly is not None or buzz is not None:
        buzz_str = f"{float(buzz):.2f}" if isinstance(buzz, (int, float)) else "?"
        lines.append(f"- Buzz: {weekly or '?'} articles/wk (index {buzz_str})")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def adjust_conviction_with_sentiment(conviction: int, data: dict | None) -> int:
    """Additive sentiment nudge, clamped to [1, 10].

    Uses bullish-bearish spread:
      spread >= +30pp  -> +1
      spread <= -30pp  -> -1
      else              0
    """
    if not data:
        return conviction
    bullish = data.get("bullish_pct")
    bearish = data.get("bearish_pct")
    if bullish is None or bearish is None:
        return conviction
    try:
        b = float(bullish)
        r = float(bearish)
    except (TypeError, ValueError):
        return conviction
    # Handle already-percent vs 0-1 fraction
    if b > 1.0 or r > 1.0:
        spread_pp = b - r
    else:
        spread_pp = (b - r) * 100.0
    delta = 0
    if spread_pp >= 30.0:
        delta = 1
    elif spread_pp <= -30.0:
        delta = -1
    return max(1, min(10, conviction + delta))
