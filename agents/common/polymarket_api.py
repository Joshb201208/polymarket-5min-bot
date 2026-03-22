"""
Polymarket API client — Gamma (events/markets) + CLOB (prices/trading).
All public endpoints, no authentication needed for read-only.
"""

import json
import logging
import httpx
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

TIMEOUT = 15.0


def _get(url: str, params: dict = None) -> dict | list | None:
    """Generic GET with error handling."""
    try:
        resp = httpx.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"API error {url}: {e}")
        return None


# ── Gamma API (events, markets, search) ───────────────────────

def search_events(query: str = "", tag: str = "", limit: int = 50,
                  active: bool = True, closed: bool = False,
                  order: str = "volume24hr",
                  ascending: bool = False) -> list[dict]:
    """Search events from Gamma API."""
    params = {"limit": limit, "order": order, "ascending": str(ascending).lower()}
    if query:
        params["title"] = query
    if tag:
        params["tag_slug"] = tag
    if closed:
        params["closed"] = "true"
        params.pop("active", None)
    else:
        params["active"] = "true"
        params["closed"] = "false"
    data = _get(f"{config.GAMMA_API}/events", params)
    return data if isinstance(data, list) else []


def get_event(event_slug: str) -> dict | None:
    """Get single event by slug."""
    return _get(f"{config.GAMMA_API}/events/{event_slug}")


def get_markets(event_id: str = None, active: bool = True,
                limit: int = 100) -> list[dict]:
    """Get markets, optionally filtered by event."""
    params = {"limit": limit, "active": str(active).lower()}
    if event_id:
        params["event_id"] = event_id
    data = _get(f"{config.GAMMA_API}/markets", params)
    return data if isinstance(data, list) else []


def search_markets(query: str, limit: int = 50) -> list[dict]:
    """Search markets by text query."""
    params = {"query": query, "limit": limit}
    data = _get(f"{config.GAMMA_API}/markets", params)
    return data if isinstance(data, list) else []


def fetch_resolved_events(tag_slug: str = None, limit: int = 100) -> list[dict]:
    """Fetch resolved (closed) events for backtesting."""
    params = {
        "closed": "true",
        "limit": limit,
        "order": "volume",
        "ascending": "false",
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    data = _get(f"{config.GAMMA_API}/events", params)
    return data if isinstance(data, list) else []


# ── CLOB API (prices, trading) ────────────────────────────────

def get_price_history(token_id: str, interval: str = "1w",
                      fidelity: int = 60) -> list[dict]:
    """Get price history from CLOB."""
    params = {"market": token_id, "interval": interval, "fidelity": fidelity}
    data = _get(f"{config.CLOB_API}/prices-history", params)
    return data if isinstance(data, list) else []


def get_midpoint(token_id: str) -> float | None:
    """Get current mid price for a token."""
    data = _get(f"{config.CLOB_API}/midpoint", {"token_id": token_id})
    if data and "mid" in data:
        try:
            return float(data["mid"])
        except (ValueError, TypeError):
            return None
    return None


def get_spread(token_id: str) -> dict | None:
    """Get spread (best bid/ask) for a token."""
    data = _get(f"{config.CLOB_API}/spread", {"token_id": token_id})
    return data


def get_book(token_id: str) -> dict | None:
    """Get order book for a token."""
    data = _get(f"{config.CLOB_API}/book", {"token_id": token_id})
    return data


def get_market_price(token_id: str) -> float | None:
    """Get current market price (tries midpoint first, falls back to book)."""
    mid = get_midpoint(token_id)
    if mid is not None:
        return mid
    book = get_book(token_id)
    if book:
        try:
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                return (best_bid + best_ask) / 2
        except (IndexError, KeyError, ValueError):
            pass
    return None


# ── Data API (trades, positions) ──────────────────────────────

def get_trades(condition_id: str, limit: int = 50) -> list[dict]:
    """Get recent trades for a market."""
    params = {"asset_id": condition_id, "limit": limit}
    data = _get(f"{config.CLOB_API}/trades", params)
    return data if isinstance(data, list) else []


# ── Helpers ───────────────────────────────────────────────────

def get_token_ids(market_raw: dict) -> tuple[str, str]:
    """Return (yes_token_id, no_token_id) from a raw Gamma market."""
    raw = market_raw.get("clobTokenIds", "[]")
    if isinstance(raw, str):
        try:
            tokens = json.loads(raw)
        except Exception:
            tokens = []
    else:
        tokens = list(raw)
    yes_id = tokens[0] if len(tokens) > 0 else ""
    no_id = tokens[1] if len(tokens) > 1 else ""
    return yes_id, no_id


def parse_market_data(market: dict) -> dict:
    """Extract key fields from a Gamma market object."""
    try:
        outcome_prices = market.get("outcomePrices", "[]")
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)
        yes_price = float(outcome_prices[0]) if outcome_prices else None
        no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else None
    except (ValueError, IndexError, TypeError):
        yes_price = no_price = None

    # If market is closed, mark prices as None (untradeable)
    if market.get("closed") == True or market.get("closed") == "true":
        yes_price = None
        no_price = None

    # If market is explicitly NOT accepting orders, mark as None
    accepting = market.get("acceptingOrders")
    if accepting is False or accepting == "false" or accepting == "False":
        yes_price = None
        no_price = None

    # Reject effectively resolved markets (price at 0/1)
    if yes_price is not None and (yes_price <= 0.02 or yes_price >= 0.98):
        yes_price = None  # Mark as untradeable
        no_price = None

    tokens = market.get("clobTokenIds", "[]")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            tokens = []

    yes_token = tokens[0] if len(tokens) > 0 else ""
    no_token = tokens[1] if len(tokens) > 1 else ""

    return {
        "id": market.get("id", ""),
        "question": market.get("question", ""),
        "description": market.get("description", ""),
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": float(market.get("volume", 0) or 0),
        "volume_24h": float(market.get("volume24hr", 0) or 0),
        "liquidity": float(market.get("liquidity", 0) or 0),
        "end_date": market.get("endDate", ""),
        "active": market.get("active", False),
        "closed": market.get("closed", False),
        "accepting_orders": market.get("acceptingOrders", True),
        "slug": market.get("slug", ""),
        "event_slug": market.get("eventSlug", ""),
        "condition_id": market.get("conditionId", ""),
        "token_ids": tokens,
        "yes_token": yes_token,
        "no_token": no_token,
        "neg_risk": market.get("negRisk", False),
        "tick_size": str(market.get("orderPriceMinTickSize", 0.01)),
        "one_day_change": float(market.get("oneDayPriceChange", 0) or 0),
        "one_week_change": float(market.get("oneWeekPriceChange", 0) or 0),
        "tags": market.get("tags", []),
    }


def get_market_url(market: dict) -> str:
    """Generate Polymarket URL for a market."""
    event_slug = market.get("event_slug") or market.get("eventSlug", "")
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    slug = market.get("slug", "")
    return f"https://polymarket.com/event/{slug}"
