"""TipRanks Screener API client — discovery + conviction-enrichment layer.

Additive module. The existing strategy pipeline is not changed structurally —
callers opt in to the helpers exposed here. All functions fail soft (log and
return safe defaults) so TipRanks outages never block the core trading loop.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

# Credentials embedded directly (per integration spec) — env vars still win via config.
TIPRANKS_API_KEY = "TR_SilverArrow"
TIPRANKS_API_TOKEN = "f8ed6170-a853-42a6-a76d-a3c244560c17"
TIPRANKS_SCREENER_URL = "https://api.tipranks.com/api/stocks/screener"

_DEFAULT_TIMEOUT = 15


def _headers() -> dict[str, str]:
    return {
        "X-APIKey": TIPRANKS_API_KEY,
        "X-APIToken": TIPRANKS_API_TOKEN,
        "Accept": "application/json",
        "User-Agent": "stock-agent/1.0 (+tipranks-client)",
    }


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present (and non-None) key from d."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _call_screener(params: dict) -> list[dict]:
    """Raw GET against the TipRanks screener endpoint.

    Handles multiple response shapes defensively:
      - {"stocks":   [...]}
      - {"data":     [...]}
      - {"results":  [...]}
      - bare list    [...]
    Returns an empty list on any error or unrecognised payload.
    """
    try:
        resp = requests.get(
            TIPRANKS_SCREENER_URL,
            headers=_headers(),
            params=params,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("TipRanks screener call failed: %s", exc)
        return []

    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if isinstance(payload, dict):
        for key in ("stocks", "data", "results"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]

    logger.warning(
        "TipRanks screener returned unrecognised payload shape: %s",
        type(payload).__name__,
    )
    return []


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    v = _to_float(value)
    if v is None:
        return None
    try:
        return int(round(v))
    except (TypeError, ValueError):
        return None


def _normalise(raw: dict) -> dict:
    """Map a raw screener row to a stable internal shape.

    Tries multiple field-name variants so changes in the upstream schema do
    not silently drop fields.
    """
    if not isinstance(raw, dict):
        return {}

    symbol = _first(raw, "ticker", "symbol", "Ticker", "Symbol", "stockTicker")
    smart_score = _to_int(
        _first(raw, "smartScore", "smart_score", "SmartScore", "tipranksSmartScore")
    )
    sector = _first(raw, "sector", "Sector", "sectorName")
    market_cap = _to_float(
        _first(raw, "marketCap", "market_cap", "MarketCap", "marketCapitalization")
    )
    analyst_consensus = _first(
        raw,
        "analystConsensus",
        "analyst_consensus",
        "AnalystConsensus",
        "stockAnalystConsensus",
        "consensus",
    )
    analyst_price_target_upside = _to_float(
        _first(
            raw,
            "analystPriceTargetUpside",
            "analyst_price_target_upside",
            "priceTargetUpside",
            "analystTargetUpside",
            "priceTargetUpsidePercent",
        )
    )
    hedge_fund_signal = _first(
        raw,
        "hedgeFundSignal",
        "hedge_fund_signal",
        "HedgeFundSignal",
        "hedgeFundTrendAction",
        "hfSignal",
    )
    insider_signal = _first(
        raw,
        "insiderSignal",
        "insider_signal",
        "InsiderSignal",
        "insiderTrendAction",
        "insidersSignal",
    )
    news_sentiment = _first(
        raw,
        "newsSentiment",
        "news_sentiment",
        "NewsSentiment",
        "newsSentimentLabel",
        "bloggerSentiment",
    )

    return {
        "symbol": str(symbol).upper() if symbol else None,
        "smart_score": smart_score,
        "sector": sector,
        "market_cap": market_cap,
        "analyst_consensus": analyst_consensus,
        "analyst_price_target_upside": analyst_price_target_upside,
        "hedge_fund_signal": hedge_fund_signal,
        "insider_signal": insider_signal,
        "news_sentiment": news_sentiment,
    }


def fetch_high_smart_score_us_stocks(
    min_score: int = 8,
    limit: int = 100,
) -> list[dict]:
    """Fetch US stocks with Smart Score >= min_score.

    Never raises. Returns [] on any error.
    """
    try:
        params = {
            "minSmartScore": int(min_score),
            "country": "US",
            "limit": int(limit),
        }
        rows = _call_screener(params)
        normalised: list[dict] = []
        for row in rows:
            n = _normalise(row)
            if n.get("symbol"):
                normalised.append(n)
        logger.info(
            "TipRanks fetch: %d rows (min_score=%d, limit=%d)",
            len(normalised), min_score, limit,
        )
        return normalised
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_high_smart_score_us_stocks failed: %s", exc)
        return []


def build_tipranks_lookup(
    min_score: int = 8,
    limit: int = 100,
) -> dict[str, dict]:
    """Return a {symbol: normalised_row} lookup for downstream enrichment."""
    rows = fetch_high_smart_score_us_stocks(min_score=min_score, limit=limit)
    lookup: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            lookup[sym] = row
    return lookup


def adjust_conviction_with_tipranks(
    base_conviction: int | float | None,
    tipranks_data: dict | None,
) -> int:
    """Adjust conviction score using TipRanks Smart Score.

    Rules:
        Smart Score 10    -> +2
        Smart Score  9    -> +1
        Smart Score  8    ->  0
        Smart Score  6-7  -> -1
        Smart Score <=5   -> -2
    Result is clamped to [1, 10]. Missing data returns the base unchanged.
    """
    try:
        base = int(round(float(base_conviction))) if base_conviction is not None else 0
    except (TypeError, ValueError):
        base = 0

    if not tipranks_data:
        return max(1, min(10, base))

    score = tipranks_data.get("smart_score")
    if score is None:
        return max(1, min(10, base))

    try:
        s = int(score)
    except (TypeError, ValueError):
        return max(1, min(10, base))

    if s >= 10:
        delta = 2
    elif s == 9:
        delta = 1
    elif s == 8:
        delta = 0
    elif s in (6, 7):
        delta = -1
    else:  # s <= 5
        delta = -2

    return max(1, min(10, base + delta))


def _fmt_upside(value: Any) -> str:
    v = _to_float(value)
    if v is None:
        return "n/a"
    return f"{v:+.1f}%"


def _fmt_market_cap(value: Any) -> str:
    v = _to_float(value)
    if v is None:
        return "n/a"
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def format_tipranks_context(symbol: str, tipranks_data: dict | None) -> str:
    """Plain-text block suitable for injection into a Perplexity/LLM prompt.

    Returns '' when no TipRanks data is available so callers can safely
    concatenate without conditionals.
    """
    if not tipranks_data:
        return ""

    score = tipranks_data.get("smart_score")
    score_str = f"{score}/10" if score is not None else "n/a"

    lines = [
        f"TIPRANKS DATA ({symbol}):",
        f"- Smart Score: {score_str}",
        f"- Sector: {tipranks_data.get('sector') or 'n/a'}",
        f"- Market Cap: {_fmt_market_cap(tipranks_data.get('market_cap'))}",
        f"- Analyst Consensus: {tipranks_data.get('analyst_consensus') or 'n/a'}",
        f"- Analyst Price Target Upside: "
        f"{_fmt_upside(tipranks_data.get('analyst_price_target_upside'))}",
        f"- Hedge Fund Signal: {tipranks_data.get('hedge_fund_signal') or 'n/a'}",
        f"- Insider Signal: {tipranks_data.get('insider_signal') or 'n/a'}",
        f"- News Sentiment: {tipranks_data.get('news_sentiment') or 'n/a'}",
    ]
    return "\n".join(lines) + "\n"


_SMART_SCORE_LABELS = {
    10: "Outperform",
    9: "Outperform",
    8: "Outperform",
    7: "Neutral",
    6: "Neutral",
    5: "Neutral",
    4: "Underperform",
    3: "Underperform",
    2: "Underperform",
    1: "Underperform",
}


def format_tipranks_telegram_clause(tipranks_data: dict | None) -> str:
    """Short inline clause for Telegram trade alerts.

    Example:
        " | TipRanks Smart Score: 10 (Outperform), Analyst: Strong Buy, HF: Positive"

    Returns '' when no data is available.
    """
    if not tipranks_data:
        return ""

    parts: list[str] = []
    score = tipranks_data.get("smart_score")
    if score is not None:
        try:
            s = int(score)
            label = _SMART_SCORE_LABELS.get(s, "")
            parts.append(f"Smart Score: {s} ({label})" if label else f"Smart Score: {s}")
        except (TypeError, ValueError):
            pass

    consensus = tipranks_data.get("analyst_consensus")
    if consensus:
        parts.append(f"Analyst: {consensus}")

    hf = tipranks_data.get("hedge_fund_signal")
    if hf:
        parts.append(f"HF: {hf}")

    if not parts:
        return ""

    return " | TipRanks " + ", ".join(parts)


__all__ = [
    "_call_screener",
    "_normalise",
    "fetch_high_smart_score_us_stocks",
    "build_tipranks_lookup",
    "adjust_conviction_with_tipranks",
    "format_tipranks_context",
    "format_tipranks_telegram_clause",
]
