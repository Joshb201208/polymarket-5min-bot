"""Tier 2C: Cross-Market Arbitrage Detector.

Finds pricing inconsistencies within Polymarket (logical contradictions between
related markets) and across platforms. Detects internal, cross-platform, and
temporal arbitrage opportunities.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import utcnow

logger = logging.getLogger("intelligence.cross_market")


class CrossMarketArbitrage:
    """Detects pricing inconsistencies within and across prediction markets."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()

    async def scan(self, all_markets: list) -> list[Signal]:
        """Scan for arbitrage opportunities. Returns Signal list."""
        if not self.config.is_enabled("cross_market"):
            logger.debug("Cross-market arbitrage disabled")
            return []

        signals: list[Signal] = []

        # 1. Internal arbitrage (within Polymarket)
        try:
            internal = await asyncio.wait_for(
                self._scan_internal_arbitrage(all_markets),
                timeout=self.config.MODULE_TIMEOUT,
            )
            signals.extend(internal)
        except asyncio.TimeoutError:
            logger.warning("Internal arbitrage scan timed out")
        except Exception as e:
            logger.error("Internal arbitrage scan failed: %s", e)

        # 2. Temporal arbitrage (different time horizons)
        try:
            temporal = self._scan_temporal_arbitrage(all_markets)
            signals.extend(temporal)
        except Exception as e:
            logger.error("Temporal arbitrage scan failed: %s", e)

        logger.info("Cross-market scan: %d signals", len(signals))
        return signals

    async def _scan_internal_arbitrage(self, markets: list) -> list[Signal]:
        """Find logical contradictions between related Polymarket markets."""
        signals: list[Signal] = []
        now = utcnow()

        # Cluster related markets by topic
        clusters = self._cluster_related_markets(markets)

        for cluster_name, cluster_markets in clusters.items():
            if len(cluster_markets) < 2:
                continue

            # Check: mutually exclusive outcomes should sum to ~100%
            # e.g., "Who wins 2026 election?" — all candidates should sum to ~1.0
            event_groups = self._group_by_event(cluster_markets)

            for event_key, group in event_groups.items():
                # Check if these look like mutually exclusive outcomes
                total_yes = sum(
                    getattr(m, "outcome_prices", [0.5])[0] for m in group
                )

                if len(group) >= 3 and total_yes > 0:
                    # For multi-outcome events, total should be ~1.0
                    overround = total_yes - 1.0

                    if abs(overround) > 0.05:
                        # Significant deviation from 100%
                        direction = "YES" if overround < 0 else "NO"
                        questions = [getattr(m, "question", "")[:50] for m in group[:3]]

                        signals.append(Signal(
                            source="cross_market",
                            market_id=getattr(group[0], "id", ""),
                            market_question=f"Cluster: {cluster_name}",
                            signal_type="internal_arbitrage",
                            direction=direction,
                            strength=min(abs(overround) / 0.15, 1.0),
                            confidence=0.7,
                            details={
                                "cluster": cluster_name,
                                "total_probability": round(total_yes, 3),
                                "overround": round(overround, 3),
                                "market_count": len(group),
                                "markets": questions,
                            },
                            timestamp=now,
                            expires_at=now + timedelta(hours=1),
                        ))

            # Check pairwise contradictions
            # e.g., "Fed cut June" at 40% but "Fed cut 2026" at 35% is contradictory
            pairwise_signals = self._check_pairwise_contradictions(
                cluster_markets, cluster_name,
            )
            signals.extend(pairwise_signals)

        return signals

    def _scan_temporal_arbitrage(self, markets: list) -> list[Signal]:
        """Find temporal contradictions — shorter timeframe can't be more likely than longer."""
        signals: list[Signal] = []
        now = utcnow()

        # Group markets by topic, then compare time horizons
        topic_groups = defaultdict(list)
        for market in markets:
            question = getattr(market, "question", "").lower()
            # Extract core topic (remove date/time references)
            topic = re.sub(r"\b(by|before|in|during|after)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s*\d*", "", question)
            topic = re.sub(r"\b(20\d{2}|q[1-4]|january|february|march|april|may|june|july|august|september|october|november|december)\b", "", topic)
            topic = re.sub(r"\s+", " ", topic).strip()

            if len(topic) > 10:  # Meaningful topic left
                topic_groups[topic].append(market)

        for topic, group in topic_groups.items():
            if len(group) < 2:
                continue

            # Sort by end date
            sorted_markets = sorted(
                group,
                key=lambda m: getattr(m, "end_date", "") or "",
            )

            # Compare adjacent pairs
            for i in range(len(sorted_markets) - 1):
                earlier = sorted_markets[i]
                later = sorted_markets[i + 1]

                earlier_price = getattr(earlier, "outcome_prices", [0.5])[0]
                later_price = getattr(later, "outcome_prices", [0.5])[0]

                # Earlier deadline should have lower or equal probability
                if earlier_price > later_price + self.config.CROSS_MARKET_DIVERGENCE_THRESHOLD:
                    signals.append(Signal(
                        source="cross_market",
                        market_id=getattr(earlier, "id", ""),
                        market_question=getattr(earlier, "question", ""),
                        signal_type="temporal_arbitrage",
                        direction="NO",  # Earlier is overpriced relative to later
                        strength=min(
                            (earlier_price - later_price) / 0.10, 1.0,
                        ),
                        confidence=0.8,
                        details={
                            "earlier_question": getattr(earlier, "question", "")[:60],
                            "later_question": getattr(later, "question", "")[:60],
                            "earlier_price": round(earlier_price, 3),
                            "later_price": round(later_price, 3),
                            "divergence": round(earlier_price - later_price, 3),
                        },
                        timestamp=now,
                        expires_at=now + timedelta(hours=2),
                    ))

        return signals

    def _cluster_related_markets(self, markets: list) -> dict[str, list]:
        """Group markets by topic using keyword overlap."""
        clusters: dict[str, list] = defaultdict(list)

        for market in markets:
            question = getattr(market, "question", "").lower()
            slug = getattr(market, "slug", "").lower()
            event_slug = getattr(market, "event_slug", "").lower()
            combined = question + " " + slug + " " + event_slug

            # Classify into clusters by topic keywords
            topic_keywords = {
                "fed_rate": ["fed", "interest rate", "rate cut", "rate hike", "fomc"],
                "bitcoin": ["bitcoin", "btc", "crypto"],
                "trump": ["trump"],
                "election": ["election", "primary", "nominee", "electoral"],
                "ukraine": ["ukraine", "russia", "ceasefire"],
                "china": ["china", "taiwan", "trade war", "tariff"],
                "ai": ["artificial intelligence", "ai ", "openai", "chatgpt"],
                "inflation": ["inflation", "cpi", "ppi"],
                "recession": ["recession", "gdp", "economic"],
            }

            for cluster_name, keywords in topic_keywords.items():
                for kw in keywords:
                    if kw in combined:
                        clusters[cluster_name].append(market)
                        break

        return dict(clusters)

    def _group_by_event(self, markets: list) -> dict[str, list]:
        """Group markets by their parent event."""
        groups: dict[str, list] = defaultdict(list)
        for market in markets:
            event_slug = getattr(market, "event_slug", "") or "unknown"
            groups[event_slug].append(market)
        return dict(groups)

    def _check_pairwise_contradictions(
        self, markets: list, cluster_name: str,
    ) -> list[Signal]:
        """Check for pairwise logical contradictions."""
        signals: list[Signal] = []
        now = utcnow()

        for i in range(len(markets)):
            for j in range(i + 1, len(markets)):
                m1 = markets[i]
                m2 = markets[j]

                q1 = getattr(m1, "question", "").lower()
                q2 = getattr(m2, "question", "").lower()
                p1 = getattr(m1, "outcome_prices", [0.5])[0]
                p2 = getattr(m2, "outcome_prices", [0.5])[0]

                # Check if one is a subset condition of the other
                # e.g., "by June" is subset of "by December"
                # The subset can't be MORE likely
                if self._is_subset_condition(q1, q2):
                    if p1 > p2 + self.config.CROSS_MARKET_DIVERGENCE_THRESHOLD:
                        signals.append(Signal(
                            source="cross_market",
                            market_id=getattr(m1, "id", ""),
                            market_question=getattr(m1, "question", ""),
                            signal_type="logical_contradiction",
                            direction="NO",
                            strength=min((p1 - p2) / 0.10, 1.0),
                            confidence=0.75,
                            details={
                                "market_a": getattr(m1, "question", "")[:60],
                                "market_b": getattr(m2, "question", "")[:60],
                                "price_a": round(p1, 3),
                                "price_b": round(p2, 3),
                                "cluster": cluster_name,
                            },
                            timestamp=now,
                            expires_at=now + timedelta(hours=1),
                        ))

        return signals

    def _is_subset_condition(self, q1: str, q2: str) -> bool:
        """Check if q1 is a stricter condition than q2 (subset)."""
        # Simple heuristic: if q1 has earlier date/lower threshold than q2
        # This catches patterns like "by June" vs "by December"
        months_order = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
        month_shorts = [
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec",
        ]

        def _find_month_index(text: str) -> int:
            for i, (full, short) in enumerate(zip(months_order, month_shorts)):
                if full in text or short in text:
                    return i
            return -1

        m1_idx = _find_month_index(q1)
        m2_idx = _find_month_index(q2)

        if m1_idx >= 0 and m2_idx >= 0:
            # Both have months — earlier month is the subset
            return m1_idx < m2_idx

        return False
