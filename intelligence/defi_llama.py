"""DeFiLlama Module — DeFi ecosystem health signals for crypto markets.

Tracks total DeFi TVL trends, stablecoin supply changes, and protocol-specific
metrics to generate signals for crypto-related prediction markets.

Signal weight: 0.10
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from shared.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.defi_llama")

LLAMA_PROTOCOLS_URL = "https://api.llama.fi/v2/protocols"
LLAMA_CHAINS_URL = "https://api.llama.fi/v2/chains"
LLAMA_STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins"

# Keywords that indicate a market is crypto-related
CRYPTO_KEYWORDS = frozenset({
    "bitcoin", "btc", "ethereum", "eth", "crypto", "defi", "nft",
    "solana", "sol", "cardano", "ada", "polygon", "matic", "avalanche",
    "avax", "chainlink", "link", "uniswap", "uni", "aave", "maker",
    "mkr", "coinbase", "binance", "stablecoin", "usdc", "usdt",
    "tether", "circle", "etf", "sec", "blockchain", "web3",
    "spot etf", "bitcoin etf", "ethereum etf",
})


class DeFiLlamaMonitor:
    """Monitors DeFi ecosystem health for crypto market signals."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._cache_path = self.config.DATA_DIR / "defi_llama_cache.json"
        self._tvl_history: list[float] = []
        self._stablecoin_supply: float = 0.0

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan DeFi metrics and generate signals for crypto-related markets."""
        # Only process crypto-related markets
        crypto_markets = [
            m for m in active_markets
            if self._is_crypto_related(m)
        ]

        if not crypto_markets:
            return []

        self._load_cache()

        # Fetch DeFi ecosystem data
        tvl_data = await self._fetch_tvl()
        stablecoin_data = await self._fetch_stablecoins()

        if not tvl_data and not stablecoin_data:
            logger.info("DeFiLlama: no data available")
            return []

        signals: list[Signal] = []

        for market in crypto_markets:
            try:
                signal = self._evaluate_market(market, tvl_data, stablecoin_data)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug("DeFiLlama error for %s: %s", getattr(market, "id", "?"), e)

        self._save_cache()
        logger.info("DeFiLlama scan: %d signals from %d crypto markets", len(signals), len(crypto_markets))
        return signals

    def _is_crypto_related(self, market) -> bool:
        """Check if a market is related to crypto/DeFi."""
        question = (getattr(market, "question", "") or "").lower()
        category = ""
        cat = getattr(market, "category", None)
        if cat:
            category = (cat.value if hasattr(cat, "value") else str(cat)).lower()

        if category in ("crypto", "cryptocurrency", "defi"):
            return True

        return any(kw in question for kw in CRYPTO_KEYWORDS)

    async def _fetch_tvl(self) -> dict:
        """Fetch total DeFi TVL and chain breakdown."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(LLAMA_CHAINS_URL)
                if resp.status_code != 200:
                    return {}

                chains = resp.json()
                total_tvl = sum(float(c.get("tvl", 0)) for c in chains)

                # Track TVL history
                self._tvl_history.append(total_tvl)
                self._tvl_history = self._tvl_history[-48:]  # Keep 48 data points

                # Calculate TVL trend
                tvl_change_pct = 0.0
                if len(self._tvl_history) >= 2:
                    prev = self._tvl_history[-2]
                    if prev > 0:
                        tvl_change_pct = ((total_tvl - prev) / prev) * 100

                # Top chains by TVL
                chains_sorted = sorted(chains, key=lambda c: float(c.get("tvl", 0)), reverse=True)
                top_chains = [
                    {"name": c.get("name"), "tvl": float(c.get("tvl", 0))}
                    for c in chains_sorted[:10]
                ]

                return {
                    "total_tvl": total_tvl,
                    "tvl_change_pct": tvl_change_pct,
                    "top_chains": top_chains,
                }
        except Exception as e:
            logger.debug("DeFiLlama TVL fetch failed: %s", e)
            return {}

    async def _fetch_stablecoins(self) -> dict:
        """Fetch stablecoin supply data."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(LLAMA_STABLECOINS_URL)
                if resp.status_code != 200:
                    return {}

                data = resp.json()
                stablecoins = data.get("peggedAssets", [])

                total_supply = sum(
                    float(s.get("circulating", {}).get("peggedUSD", 0) or 0)
                    for s in stablecoins
                )

                # Track supply changes
                prev_supply = self._stablecoin_supply
                self._stablecoin_supply = total_supply
                supply_change_pct = 0.0
                if prev_supply > 0:
                    supply_change_pct = ((total_supply - prev_supply) / prev_supply) * 100

                return {
                    "total_supply": total_supply,
                    "supply_change_pct": supply_change_pct,
                }
        except Exception as e:
            logger.debug("DeFiLlama stablecoin fetch failed: %s", e)
            return {}

    def _evaluate_market(self, market, tvl_data: dict, stablecoin_data: dict) -> Signal | None:
        """Generate signal for a crypto market based on DeFi health."""
        market_id = getattr(market, "id", "")
        question = getattr(market, "question", "")

        tvl_change = tvl_data.get("tvl_change_pct", 0) if tvl_data else 0
        supply_change = stablecoin_data.get("supply_change_pct", 0) if stablecoin_data else 0

        # Combine TVL and stablecoin signals
        ecosystem_health = (tvl_change * 0.6 + supply_change * 0.4)

        # Only signal on meaningful ecosystem changes (>1% combined)
        if abs(ecosystem_health) < 1.0:
            return None

        # Positive ecosystem health → bullish for crypto
        if ecosystem_health > 0:
            direction = "YES"  # Crypto-positive
        else:
            direction = "NO"  # Crypto-negative

        # Adjust direction for negative crypto markets (e.g., "will BTC fall below X")
        question_lower = question.lower()
        if any(w in question_lower for w in ["fall", "crash", "below", "decline", "drop"]):
            direction = "NO" if direction == "YES" else "YES"

        strength = min(abs(ecosystem_health) / 10.0, 1.0)
        confidence = 0.5  # DeFi metrics are lagging indicators

        now = utcnow()
        return Signal(
            source="defi_llama",
            market_id=market_id,
            market_question=question,
            signal_type="defi_ecosystem",
            direction=direction,
            strength=round(strength, 3),
            confidence=confidence,
            details={
                "total_tvl": tvl_data.get("total_tvl") if tvl_data else None,
                "tvl_change_pct": round(tvl_change, 2),
                "stablecoin_supply": stablecoin_data.get("total_supply") if stablecoin_data else None,
                "supply_change_pct": round(supply_change, 2),
                "ecosystem_health": round(ecosystem_health, 2),
            },
            timestamp=now,
            expires_at=now + timedelta(hours=4),
        )

    def _load_cache(self) -> None:
        data = load_json(self._cache_path, {})
        self._tvl_history = data.get("tvl_history", [])
        self._stablecoin_supply = data.get("stablecoin_supply", 0.0)

    def _save_cache(self) -> None:
        try:
            atomic_json_write(self._cache_path, {
                "tvl_history": self._tvl_history,
                "stablecoin_supply": self._stablecoin_supply,
                "updated_at": utcnow().isoformat(),
            })
        except Exception as e:
            logger.warning("Failed to save DeFiLlama cache: %s", e)
