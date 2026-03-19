"""
fee_manager.py - Dynamic fee rate management for Polymarket CLOB.

Since Feb 2026, orders MUST include feeRateBps in the signature payload.
This module queries live fee rates per token and caches them.

Fee formula: fee = C × p × 0.25 × (p × (1-p))^2
- Max effective fee: ~0.44% at p=0.50 (for the new all-crypto fee structure)
- Makers pay 0% and get 20% rebate
- Fee rates can change anytime — never hardcode
"""

import time
import logging
import threading
from typing import Dict, Optional, Tuple
import httpx

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
FEE_CACHE_TTL = 60  # Re-fetch every 60 seconds


class FeeManager:
    """Manages dynamic fee rate lookups for Polymarket orders."""

    def __init__(self):
        self._cache: Dict[str, Tuple[int, float]] = {}  # token_id -> (fee_bps, timestamp)
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=10)
        logger.info("FeeManager initialized — dynamic fee querying active")

    def get_fee_rate_bps(self, token_id: str) -> int:
        """
        Get the current fee rate in basis points for a token.
        Returns cached value if fresh, otherwise fetches from API.

        Args:
            token_id: The Polymarket token ID

        Returns:
            Fee rate in basis points (e.g., 150 = 1.5%)
        """
        with self._lock:
            if token_id in self._cache:
                bps, ts = self._cache[token_id]
                if time.time() - ts < FEE_CACHE_TTL:
                    return bps

        # Fetch fresh rate
        bps = self._fetch_fee_rate(token_id)
        with self._lock:
            self._cache[token_id] = (bps, time.time())
        return bps

    def _fetch_fee_rate(self, token_id: str) -> int:
        """Fetch fee rate from CLOB API."""
        try:
            resp = self._http.get(
                f"{CLOB_BASE}/fee-rate",
                params={"tokenID": token_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                # API returns fee rate — extract basis points
                # The response format may vary; handle both structures
                if isinstance(data, dict):
                    # Could be {"fee_rate_bps": 150} or {"feeRateBps": "150"}
                    bps = data.get("fee_rate_bps") or data.get("feeRateBps") or data.get("fee") or 0
                    bps = int(bps)
                    logger.debug("Fee rate for %s: %d bps", token_id[:8], bps)
                    return bps
                elif isinstance(data, (int, float, str)):
                    return int(data)

            logger.warning("Fee rate fetch failed (status=%d), using default 0 (maker)", resp.status_code)
            return 0  # Default to 0 for maker orders

        except Exception as exc:
            logger.warning("Fee rate fetch error: %s, defaulting to 0 (maker)", exc)
            return 0  # Maker orders have 0 fee

    def estimate_taker_fee(self, price: float) -> float:
        """
        Estimate the taker fee percentage at a given price level.
        Formula: fee = 0.25 × (p × (1-p))^2

        Args:
            price: Token price between 0 and 1

        Returns:
            Estimated fee as a decimal (e.g., 0.0044 = 0.44%)
        """
        p = max(0.01, min(0.99, price))
        return 0.25 * (p * (1 - p)) ** 2

    def estimate_maker_rebate(self, price: float) -> float:
        """
        Estimate maker rebate (20% of taker fees collected).

        Returns:
            Estimated rebate as a decimal
        """
        return self.estimate_taker_fee(price) * 0.20

    def clear_cache(self):
        """Clear all cached fee rates."""
        with self._lock:
            self._cache.clear()
        logger.info("Fee rate cache cleared")

    def close(self):
        """Clean up HTTP client."""
        self._http.close()
