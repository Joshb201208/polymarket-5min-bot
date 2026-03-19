"""
market_finder.py - Discovers active Polymarket 5-minute crypto up/down markets.

Strategy:
  1. Construct the slug based on the current 5-minute window timestamp.
  2. Query Gamma API by slug: GET /markets/slug/{slug}
  3. Fall back to public-search if slug lookup fails.
  4. Cache results to avoid redundant API calls within the same window.
  5. Retry the next window if Gamma has indexing lag.

Returns structured market dicts with everything the strategy needs.
"""

import json
import math
import time
import logging
from typing import Dict, List, Optional

import httpx

from utils import timestamp_to_5min_window, current_window, sync_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
ASSETS = ["BTC", "ETH", "SOL"]
WINDOW_SECONDS = 300


# ---------------------------------------------------------------------------
# Market schema returned by find_active_5min_markets()
# ---------------------------------------------------------------------------
# {
#   "slug":           "btc-updown-5m-1700000100",
#   "condition_id":   "0x...",
#   "token_id_yes":   "52114...",     # YES = Up token
#   "token_id_no":    "98765...",     # NO  = Down token
#   "asset":          "BTC",
#   "window_start":   1700000100,     # Unix timestamp
#   "window_end":     1700000400,
#   "question":       "Will BTC be higher or lower …",
# }


class MarketFinder:
    """
    Discovers active Polymarket 5-minute up/down markets for BTC, ETH, SOL.
    """

    def __init__(
        self,
        assets: Optional[List[str]] = None,
        gamma_base: str = GAMMA_BASE,
        request_timeout: float = 10.0,
    ):
        self._assets = [a.upper() for a in (assets or ASSETS)]
        self._gamma_base = gamma_base.rstrip("/")
        self._timeout = request_timeout

        # Cache: slug → market dict (valid within the same 5-min window)
        self._cache: Dict[str, dict] = {}
        self._cache_window: int = 0  # window_start of cached data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_active_5min_markets(self) -> List[dict]:
        """
        Return a list of active market dicts for the current 5-minute window.

        Tries current window first; if Gamma hasn't indexed it yet, tries
        constructing from the previous window as a fallback.  Results are
        cached for the duration of the window.

        Returns:
            List of market dicts (one per asset, if found).
        """
        window_start, window_end = current_window()

        # If cache is still valid for this window, return it
        if self._cache_window == window_start and self._cache:
            logger.debug("MarketFinder: returning cached markets for window %d", window_start)
            return list(self._cache.values())

        # Cache miss — fetch fresh data
        markets = self._fetch_markets_for_window(window_start)

        # If nothing found, Gamma may have indexing lag — try previous window
        if not markets:
            prev_window = window_start - WINDOW_SECONDS
            logger.info(
                "MarketFinder: no markets at window %d, trying previous window %d",
                window_start,
                prev_window,
            )
            markets = self._fetch_markets_for_window(prev_window)

        # Update cache
        self._cache = {m["slug"]: m for m in markets}
        self._cache_window = window_start

        logger.info(
            "MarketFinder: found %d markets for window %d",
            len(markets),
            window_start,
        )
        return markets

    def get_market_by_asset(self, asset: str) -> Optional[dict]:
        """
        Return the market dict for a specific asset (using the current cache
        or fetching fresh data if needed).
        """
        asset = asset.upper()
        markets = self.find_active_5min_markets()
        for m in markets:
            if m.get("asset") == asset:
                return m
        return None

    def get_upcoming_market(self, asset: str) -> Optional[dict]:
        """
        Try to look up the market for the NEXT 5-minute window.
        Useful for pre-loading data before a new window opens.
        """
        _, next_window_start = current_window()
        slug = self._build_slug(asset, next_window_start)
        return self._fetch_by_slug(slug, asset, next_window_start)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_markets_for_window(self, window_start: int) -> List[dict]:
        """Fetch market data for all configured assets at `window_start`."""
        markets = []
        for asset in self._assets:
            market = self._fetch_market_for_asset(asset, window_start)
            if market:
                markets.append(market)
        return markets

    def _fetch_market_for_asset(self, asset: str, window_start: int) -> Optional[dict]:
        """Fetch a single asset's market for the given window start."""
        slug = self._build_slug(asset, window_start)

        # Primary: direct slug lookup
        market = self._fetch_by_slug(slug, asset, window_start)
        if market:
            return market

        # Secondary: public search
        market = self._search_market(asset, window_start)
        if market:
            return market

        return None

    @staticmethod
    def _build_slug(asset: str, window_start: int) -> str:
        """
        Build the Polymarket slug for a 5-min up/down market.
        Pattern: {asset.lower()}-updown-5m-{unix_timestamp}
        """
        return f"{asset.lower()}-updown-5m-{window_start}"

    @sync_retry(max_retries=3, delay=0.5, exceptions=(httpx.RequestError, httpx.HTTPStatusError))
    def _fetch_by_slug(
        self, slug: str, asset: str, window_start: int
    ) -> Optional[dict]:
        """Query Gamma API by slug and parse the result."""
        url = f"{self._gamma_base}/markets/slug/{slug}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url)
                if resp.status_code == 404:
                    logger.debug("MarketFinder: slug not found: %s", slug)
                    return None
                resp.raise_for_status()
                data = resp.json()
                return self._parse_gamma_market(data, asset, window_start)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.warning("MarketFinder: HTTP error for slug %s: %s", slug, exc)
            return None
        except Exception as exc:
            logger.warning("MarketFinder: error fetching slug %s: %s", slug, exc)
            return None

    def _search_market(self, asset: str, window_start: int) -> Optional[dict]:
        """
        Use Gamma public-search endpoint to find the market by keyword.
        Falls back if the slug lookup returned nothing.
        """
        query = f"{asset.upper()} up or down 5 minutes"
        url = f"{self._gamma_base}/public-search"
        params = {"q": query, "limit": 20}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                results = resp.json()
        except Exception as exc:
            logger.warning("MarketFinder: search error for %s: %s", asset, exc)
            return None

        # Results may be under a "markets" key or be a direct list
        if isinstance(results, dict):
            items = results.get("markets", results.get("results", []))
        else:
            items = results

        for item in items:
            slug = item.get("slug", "")
            # Match slugs that belong to the correct window or are very recent
            if (
                f"{asset.lower()}-updown-5m" in slug
                and str(window_start) in slug
            ):
                logger.info("MarketFinder: found market via search: %s", slug)
                return self._parse_gamma_market(item, asset, window_start)

        # Last resort: take the most recent matching market
        for item in items:
            slug = item.get("slug", "")
            if f"{asset.lower()}-updown-5m" in slug:
                logger.info(
                    "MarketFinder: using nearby search result %s for window %d",
                    slug, window_start,
                )
                return self._parse_gamma_market(item, asset, window_start)

        return None

    def _parse_gamma_market(
        self, data: dict, asset: str, window_start: int
    ) -> Optional[dict]:
        """
        Parse a Gamma API market response into our standard market dict.

        The Gamma response for a binary market has:
          - conditionId
          - tokens: [{tokenId, outcome, ...}, ...]
          - slug
          - question
          - active / closed flags
        """
        if not data:
            return None

        condition_id = data.get("conditionId") or data.get("condition_id", "")
        slug = data.get("slug", self._build_slug(asset, window_start))

        # Parse token IDs for YES (Up) and NO (Down) outcomes
        token_id_yes = ""
        token_id_no = ""

        # Parse tokens - try structured tokens array first
        tokens = data.get("tokens")
        if isinstance(tokens, list):
            for token in tokens:
                if isinstance(token, dict):
                    outcome = str(token.get("outcome", "")).lower()
                    token_id = str(token.get("tokenId", token.get("token_id", "")))
                    if outcome in ("up", "yes", "higher"):
                        token_id_yes = token_id
                    elif outcome in ("down", "no", "lower"):
                        token_id_no = token_id

        # Gamma API often returns clobTokenIds as a JSON string, not a list
        # e.g., '["tokenid1", "tokenid2"]' — parse it
        if not token_id_yes and not token_id_no:
            clob_ids_raw = data.get("clobTokenIds", [])
            if isinstance(clob_ids_raw, str):
                try:
                    clob_ids_raw = json.loads(clob_ids_raw)
                except (json.JSONDecodeError, ValueError):
                    clob_ids_raw = []
            if isinstance(clob_ids_raw, list) and len(clob_ids_raw) >= 2:
                token_id_yes = str(clob_ids_raw[0])
                token_id_no = str(clob_ids_raw[1])

        if not condition_id and not token_id_yes:
            logger.debug("MarketFinder: could not parse market data: %s", data)
            return None

        # Determine window end
        window_end = window_start + WINDOW_SECONDS

        # Check if market is still active
        # Gamma marks closed markets; skip if already resolved
        if data.get("closed", False) or data.get("archived", False):
            logger.debug("MarketFinder: market %s is already closed", slug)
            return None

        return {
            "slug": slug,
            "condition_id": condition_id,
            "token_id_yes": token_id_yes,
            "token_id_no": token_id_no,
            "asset": asset.upper(),
            "window_start": window_start,
            "window_end": window_end,
            "question": data.get("question", f"Will {asset} be up or down in 5 minutes?"),
            "active": data.get("active", True),
            "volume": float(data.get("volume", 0) or 0),
            "liquidity": float(data.get("liquidity", 0) or 0),
        }

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Force re-fetch on next call to find_active_5min_markets()."""
        self._cache.clear()
        self._cache_window = 0

    def list_recent_markets(self, asset: str, count: int = 5) -> List[dict]:
        """
        List the most recent completed markets for `asset`.
        Useful for backtesting / verifying slugs.
        """
        now = int(time.time())
        window_start = (now // WINDOW_SECONDS) * WINDOW_SECONDS
        results = []
        for i in range(count):
            ws = window_start - i * WINDOW_SECONDS
            slug = self._build_slug(asset, ws)
            market = self._fetch_by_slug(slug, asset, ws)
            if market:
                results.append(market)
        return results
