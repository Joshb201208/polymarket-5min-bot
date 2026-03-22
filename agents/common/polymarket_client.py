"""
Shared Polymarket Gamma API + CLOB client.

Provides cached, rate-limited access to Polymarket event and market data.
"""

import json
import logging
import time
from typing import Any

import httpx

from agents.common.config import (
    API_REQUEST_DELAY,
    GAMMA_API_BASE,
    MIN_LIQUIDITY,
    MIN_VOLUME_24H,
)

logger = logging.getLogger(__name__)

# ── In-memory response cache ─────────────────────────────────
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 120  # seconds


def _cached_get(url: str, params: dict | None = None, ttl: int = CACHE_TTL) -> Any:
    """GET with simple TTL cache and rate-limiting delay."""
    key = f"{url}|{json.dumps(params or {}, sort_keys=True)}"
    now = time.time()
    if key in _cache:
        ts, data = _cache[key]
        if now - ts < ttl:
            return data

    time.sleep(API_REQUEST_DELAY)
    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        _cache[key] = (now, data)
        return data
    except httpx.HTTPStatusError as exc:
        logger.error("HTTP %s for %s: %s", exc.response.status_code, url, exc)
        return None
    except Exception as exc:
        logger.error("Request failed for %s: %s", url, exc)
        return None


# ── Public helpers ────────────────────────────────────────────

def fetch_active_events(
    tag_slug: str | None = None,
    limit: int = 100,
    order: str = "volume24hr",
    ascending: bool = False,
    extra_params: dict | None = None,
) -> list[dict]:
    """Fetch active, open events from the Gamma API."""
    params: dict[str, Any] = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": order,
        "ascending": str(ascending).lower(),
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    if extra_params:
        params.update(extra_params)

    data = _cached_get(f"{GAMMA_API_BASE}/events", params=params)
    if not data:
        return []
    if isinstance(data, list):
        return data
    # Some endpoints wrap in a dict
    return data.get("data", data.get("events", []))


def fetch_markets(event_id: str | None = None, **kwargs: Any) -> list[dict]:
    """Fetch markets, optionally filtered by event."""
    params: dict[str, Any] = {"active": "true", "closed": "false"}
    if event_id:
        params["event_id"] = event_id
    params.update(kwargs)
    data = _cached_get(f"{GAMMA_API_BASE}/markets", params=params)
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("data", data.get("markets", []))


def search_events(query: str, limit: int = 50) -> list[dict]:
    """Text-search events."""
    return fetch_active_events(extra_params={"q": query, "limit": limit})


def get_market_price(market: dict) -> float | None:
    """Extract the YES price from a market dict.

    outcomePrices is a JSON-encoded list like '["0.55","0.45"]' where
    index 0 = YES price.
    """
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(prices[0])
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        return None


def get_clob_token_ids(market: dict) -> list[str]:
    """Parse clobTokenIds (JSON string) from a market."""
    raw = market.get("clobTokenIds")
    if not raw:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else list(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def build_event_url(slug: str) -> str:
    """Build the Polymarket event URL."""
    return f"https://polymarket.com/event/{slug}"


def passes_filters(market: dict) -> bool:
    """Check that a market meets minimum liquidity/volume thresholds."""
    try:
        liquidity = float(market.get("liquidity", 0) or 0)
        volume_24h = float(market.get("volume24hr", 0) or 0)
    except (TypeError, ValueError):
        return False
    return liquidity >= MIN_LIQUIDITY and volume_24h >= MIN_VOLUME_24H


def is_market_tradeable(market: dict) -> bool:
    """Check that the market is accepting orders."""
    return (
        str(market.get("acceptingOrders", "")).lower() == "true"
        or market.get("acceptingOrders") is True
    ) and (
        str(market.get("enableOrderBook", "")).lower() == "true"
        or market.get("enableOrderBook") is True
    )


def get_event_markets(event: dict) -> list[dict]:
    """Return the markets list embedded in an event, or fetch separately."""
    markets = event.get("markets")
    if markets:
        return markets if isinstance(markets, list) else []
    event_id = event.get("id")
    if event_id:
        return fetch_markets(event_id=event_id)
    return []


def clear_cache() -> None:
    """Flush the in-memory cache."""
    _cache.clear()
