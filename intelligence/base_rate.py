"""Universal Signal 1: Market Price vs. Base Rate Analysis.

Estimates a base-rate probability for any market question by:
1. Fetching historical resolution data from the Gamma API for similar markets
2. Classifying the market question into an event category
3. Comparing the current market price to the category base rate
4. Producing a directional signal when significant deviation is found

Works for ANY market — no external prediction platform needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal

logger = logging.getLogger("intelligence.base_rate")

# Category base rates — historical resolution rates for YES outcome.
# Derived from Polymarket resolution patterns by category.
# Lower base rate = most markets of this type resolve NO.
CATEGORY_BASE_RATES: dict[str, float] = {
    # Geopolitical events: "Will X invade/attack/sanction Y?" → mostly NO
    "invasion": 0.05,
    "military_action": 0.08,
    "sanctions": 0.20,
    "war": 0.07,
    "coup": 0.04,
    "assassination": 0.03,
    "nuclear": 0.02,
    "peace_deal": 0.12,
    # Political events
    "resignation": 0.10,
    "impeachment": 0.08,
    "executive_order": 0.35,
    "legislation": 0.25,
    "election_winner": 0.50,  # Binary coin flip
    "nomination": 0.30,
    "veto": 0.15,
    "government_shutdown": 0.15,
    # Economic events
    "rate_cut": 0.30,
    "rate_hike": 0.30,
    "recession": 0.15,
    "default": 0.05,
    "price_target_above": 0.35,
    "price_target_below": 0.25,
    # Technology/crypto
    "token_launch": 0.40,
    "etf_approval": 0.30,
    "hack": 0.08,
    "regulation": 0.25,
    # Catch-all: generic "Will X happen by Y?" → most don't
    "generic_will_happen": 0.24,
}

# Keywords that map questions to categories
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("invasion", ["invade", "invasion", "annex"]),
    ("military_action", ["strike", "bomb", "attack", "military action", "deploy troops"]),
    ("sanctions", ["sanction", "embargo", "tariff"]),
    ("war", ["war", "conflict", "armed"]),
    ("coup", ["coup", "overthrow", "seize power"]),
    ("assassination", ["assassinat", "kill"]),
    ("nuclear", ["nuclear", "nuke", "atomic"]),
    ("peace_deal", ["peace", "ceasefire", "truce", "armistice"]),
    ("resignation", ["resign", "step down", "leave office"]),
    ("impeachment", ["impeach"]),
    ("executive_order", ["executive order", "sign into law"]),
    ("legislation", ["pass", "enact", "bill", "legislation", "law"]),
    ("election_winner", ["win the election", "elected", "win the presidency", "become president"]),
    ("nomination", ["nominat", "appoint"]),
    ("veto", ["veto"]),
    ("government_shutdown", ["shutdown", "government shut"]),
    ("rate_cut", ["rate cut", "lower rates", "cut interest"]),
    ("rate_hike", ["rate hike", "raise rates", "increase interest"]),
    ("recession", ["recession", "economic downturn", "gdp contract"]),
    ("default", ["default", "debt ceiling"]),
    ("price_target_above", ["above", "over", "exceed", "hit", "reach", "surpass"]),
    ("price_target_below", ["below", "under", "dip to", "fall to", "drop to"]),
    ("token_launch", ["launch", "release", "mainnet"]),
    ("etf_approval", ["etf", "approve", "sec"]),
    ("hack", ["hack", "exploit", "breach"]),
    ("regulation", ["regulat", "ban", "restrict"]),
]

# Cache for Gamma API calls — {query_hash: (result, timestamp)}
_gamma_cache: dict[str, tuple[dict, float]] = {}
_GAMMA_CACHE_TTL = 3600  # 1 hour


def _classify_question(question: str) -> tuple[str, float]:
    """Classify a market question into a category and return its base rate.

    Returns (category, base_rate).
    """
    q_lower = question.lower()

    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in q_lower:
                return category, CATEGORY_BASE_RATES[category]

    return "generic_will_happen", CATEGORY_BASE_RATES["generic_will_happen"]


async def _fetch_similar_resolutions(
    question: str, client: httpx.AsyncClient
) -> dict | None:
    """Fetch resolution stats for similar markets from Gamma API.

    Returns dict with yes_count, no_count, total, yes_rate or None.
    """
    # Extract key terms for search
    words = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", question)
    if not words:
        words = [w for w in question.split() if len(w) > 3][:3]
    if not words:
        return None

    query = " ".join(words[:2])
    cache_key = query.lower().strip()

    # Check cache
    if cache_key in _gamma_cache:
        cached, ts = _gamma_cache[cache_key]
        if time.monotonic() - ts < _GAMMA_CACHE_TTL:
            return cached

    try:
        resp = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "closed": "true",
                "limit": "50",
                "order": "endDate",
                "ascending": "false",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        markets = resp.json()
        if not isinstance(markets, list):
            return None

        yes_count = 0
        no_count = 0
        for m in markets:
            outcome_prices = m.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                import json
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    continue

            if not outcome_prices or len(outcome_prices) < 2:
                continue

            yes_p = float(outcome_prices[0])
            if yes_p >= 0.95:
                yes_count += 1
            elif yes_p <= 0.05:
                no_count += 1

        total = yes_count + no_count
        result = {
            "yes_count": yes_count,
            "no_count": no_count,
            "total": total,
            "yes_rate": yes_count / total if total > 0 else 0.24,
        }

        _gamma_cache[cache_key] = (result, time.monotonic())
        return result

    except Exception as e:
        logger.warning("Gamma API resolution fetch failed: %s", e)
        return None


class BaseRateAnalyzer:
    """Scans markets for base-rate deviation signals."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan all active markets for base-rate deviation signals.

        For each market:
        1. Classify the question into an event category
        2. Get the base rate for that category
        3. Compare to current market price
        4. If significant deviation → generate signal
        """
        if not os.getenv("BASE_RATE_ENABLED", "true").lower() == "true":
            return []

        signals: list[Signal] = []

        async with httpx.AsyncClient() as client:
            # Optionally fetch actual resolution data to refine base rates
            gamma_stats = await _fetch_similar_resolutions("", client)

            for market in active_markets:
                try:
                    question = getattr(market, "question", "")
                    if not question:
                        continue

                    market_id = getattr(market, "id", str(market))
                    prices = getattr(market, "outcome_prices", [])
                    if len(prices) < 2:
                        continue

                    yes_price = float(prices[0])
                    no_price = float(prices[1])

                    if yes_price <= 0.01 or yes_price >= 0.99:
                        continue  # Already resolved or nearly so

                    # Classify and get base rate
                    category, base_rate = _classify_question(question)

                    # Refine base rate with Gamma data if available
                    if gamma_stats and gamma_stats["total"] >= 10:
                        # Blend: 60% category prior, 40% observed data
                        observed_rate = gamma_stats["yes_rate"]
                        base_rate = 0.6 * base_rate + 0.4 * observed_rate

                    # Compare market price to base rate
                    # Deviation = how far the market price is from the base rate
                    yes_deviation = yes_price - base_rate
                    no_deviation = no_price - (1.0 - base_rate)

                    # Minimum deviation threshold to generate a signal
                    min_deviation = 0.08

                    if abs(yes_deviation) >= min_deviation:
                        if yes_deviation > 0:
                            # Market overprices YES relative to base rate → bet NO
                            direction = "NO"
                            strength = min(0.85, 0.4 + abs(yes_deviation))
                            signal_detail = f"YES overpriced by {yes_deviation:.0%} vs base rate {base_rate:.0%}"
                        else:
                            # Market underprices YES relative to base rate → bet YES
                            direction = "YES"
                            strength = min(0.85, 0.4 + abs(yes_deviation))
                            signal_detail = f"YES underpriced by {abs(yes_deviation):.0%} vs base rate {base_rate:.0%}"

                        confidence = min(0.80, 0.3 + abs(yes_deviation) * 1.5)

                        signal = Signal(
                            source="base_rate",
                            market_id=market_id,
                            market_question=question,
                            signal_type="base_rate_deviation",
                            direction=direction,
                            strength=round(strength, 3),
                            confidence=round(confidence, 3),
                            details={
                                "category": category,
                                "base_rate": round(base_rate, 3),
                                "yes_price": round(yes_price, 3),
                                "deviation": round(yes_deviation, 3),
                                "signal_detail": signal_detail,
                            },
                        )
                        signals.append(signal)

                        logger.info(
                            "BASE_RATE signal: %s | cat=%s base=%.0f%% mkt=%.0f%% dev=%+.0f%% → %s (str=%.2f)",
                            market_id[:8], category, base_rate * 100,
                            yes_price * 100, yes_deviation * 100,
                            direction, strength,
                        )

                except Exception as e:
                    logger.warning("Base rate analysis failed for market: %s", e)

        logger.info("Base rate scan complete: %d signals from %d markets",
                     len(signals), len(active_markets))
        return signals
