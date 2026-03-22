"""Shared Polymarket Gamma API + CLOB client with caching and rate limiting."""

import json
import time
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from agents.common.config import (
    GAMMA_API_BASE,
    CLOB_API_BASE,
    REQUEST_DELAY,
)

logger = logging.getLogger(__name__)

# ── In-memory cache ─────────────────────────────────────────
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 120  # seconds


class PolymarketClient:
    """Lightweight client around Gamma & CLOB APIs with caching + rate limiting."""

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=30, follow_redirects=True)
        self._last_request_ts = 0.0

    # ── helpers ──────────────────────────────────────────────
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_request_ts = time.time()

    def _get(self, url: str, params: dict | None = None) -> Any:
        cache_key = url + ("?" + urlencode(params) if params else "")
        now = time.time()
        if cache_key in _cache:
            ts, data = _cache[cache_key]
            if now - ts < CACHE_TTL:
                return data

        self._throttle()
        try:
            resp = self._http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            _cache[cache_key] = (now, data)
            return data
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP %s for %s", exc.response.status_code, url)
            return None
        except Exception:
            logger.exception("Request failed: %s", url)
            return None

    # ── public API ───────────────────────────────────────────
    def fetch_events(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        order: str = "volume24hr",
        ascending: bool = False,
        tag_slug: str | None = None,
        text_query: str | None = None,
    ) -> list[dict]:
        """Fetch events from Gamma API with optional filters."""
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        if text_query:
            params["_q"] = text_query

        data = self._get(f"{GAMMA_API_BASE}/events", params)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", data.get("events", []))

    def fetch_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        order: str = "volume24hr",
        ascending: bool = False,
        tag_slug: str | None = None,
    ) -> list[dict]:
        """Fetch individual markets from Gamma API."""
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if tag_slug:
            params["tag_slug"] = tag_slug

        data = self._get(f"{GAMMA_API_BASE}/markets", params)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))

    def fetch_event_by_id(self, event_id: str) -> dict | None:
        """Fetch a single event by ID."""
        return self._get(f"{GAMMA_API_BASE}/events/{event_id}")

    def get_clob_price(self, token_id: str) -> float | None:
        """Get the current mid-price from the CLOB order book."""
        data = self._get(f"{CLOB_API_BASE}/price", {"token_id": token_id})
        if data and "price" in data:
            try:
                return float(data["price"])
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def parse_outcome_prices(market: dict) -> dict[str, float]:
        """Parse outcomePrices JSON string → {outcome_label: price}."""
        prices_raw = market.get("outcomePrices", "[]")
        outcomes_raw = market.get("outcomes", "[]")
        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            return {}

        result = {}
        for i, outcome in enumerate(outcomes):
            if i < len(prices):
                try:
                    result[outcome] = float(prices[i])
                except (ValueError, TypeError):
                    pass
        return result

    @staticmethod
    def parse_clob_token_ids(market: dict) -> list[str]:
        """Parse clobTokenIds JSON string → list of token IDs."""
        raw = market.get("clobTokenIds", "[]")
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            return [str(tid) for tid in ids] if isinstance(ids, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def build_event_url(event: dict) -> str:
        slug = event.get("slug", event.get("id", ""))
        return f"https://polymarket.com/event/{slug}"

    @staticmethod
    def build_market_url(market: dict) -> str:
        slug = market.get("slug") or market.get("conditionId", "")
        group_slug = market.get("groupItemSlug") or market.get("eventSlug", "")
        if group_slug:
            return f"https://polymarket.com/event/{group_slug}"
        return f"https://polymarket.com/event/{slug}"

    def get_market_yes_price(self, market: dict) -> float | None:
        """Get YES price for a market, trying outcomePrices first then CLOB."""
        prices = self.parse_outcome_prices(market)
        if "Yes" in prices:
            return prices["Yes"]

        token_ids = self.parse_clob_token_ids(market)
        if token_ids:
            return self.get_clob_price(token_ids[0])
        return None

    def close(self) -> None:
        self._http.close()
