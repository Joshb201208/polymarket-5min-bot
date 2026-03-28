"""Signal Deduplication & Decay — clusters related signals and applies time decay.

Multiple signal sources often fire on the same underlying event (e.g., breaking news).
Without dedup, the composite scorer overcounts. Signals also lose value as the market
digests them, so we apply exponential decay.
"""

from __future__ import annotations

import logging
import math
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from intelligence.models import Signal, SignalCluster

logger = logging.getLogger("intelligence.dedup")

# Decay rates by source type (lambda for exponential decay)
DECAY_LAMBDAS: dict[str, float] = {
    "x_scanner": float(os.getenv("DECAY_FAST_LAMBDA", "0.3")),
    "orderbook": 0.5,
    "metaculus": float(os.getenv("DECAY_SLOW_LAMBDA", "0.05")),
    "google_trends": float(os.getenv("DECAY_MEDIUM_LAMBDA", "0.15")),
    "congress": 0.08,
    "whale_tracker": float(os.getenv("DECAY_MEDIUM_LAMBDA", "0.15")),
    "cross_market": 0.03,
}

# Minimum strength after decay before dropping the signal
MIN_STRENGTH_THRESHOLD = 0.05

# Time window (hours) for clustering signals together
CLUSTER_TIME_WINDOW_HOURS = 2.0

# Common stop words to exclude from keyword comparison
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "and", "but", "or", "not", "no", "nor",
    "so", "yet", "both", "either", "neither", "each", "every", "all",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "we", "us", "our", "you", "your", "he", "him", "his",
    "she", "her", "if", "then", "than", "when", "what", "which", "who",
    "how", "where", "why", "up", "about", "over", "between",
})


class SignalDeduplicator:
    """Cluster related signals and apply time-based decay."""

    def deduplicate(self, signals: list[Signal]) -> list[Signal]:
        """Cluster signals about the same underlying event.

        Groups signals by market_id, then within each market group clusters
        by temporal proximity (within 2 hours) AND content similarity.
        Keeps the strongest signal per cluster, boosted by log(N) for
        multi-source confirmation.

        Returns:
            Deduplicated signal list with boosted confidence for multi-source clusters.
        """
        if not signals:
            return []

        try:
            # Group signals by market_id
            by_market: dict[str, list[Signal]] = defaultdict(list)
            for sig in signals:
                by_market[sig.market_id].append(sig)

            result: list[Signal] = []
            clusters: list[SignalCluster] = []

            for market_id, market_signals in by_market.items():
                market_clusters = self._cluster_signals(market_signals)
                for cluster in market_clusters:
                    primary = cluster["primary"]
                    supporting = cluster["supporting"]
                    source_count = len(cluster["sources"])

                    # Boost confidence by log(N) for multi-source confirmation
                    if source_count > 1:
                        boost = 1.0 + math.log(source_count) * 0.1
                        primary.confidence = min(primary.confidence * boost, 1.0)

                    # Tag with contributing sources
                    primary.details = dict(primary.details) if primary.details else {}
                    primary.details["dedup_sources"] = list(cluster["sources"])
                    primary.details["dedup_cluster_size"] = len(supporting) + 1
                    primary.details["dedup_confidence_boost"] = round(
                        1.0 + math.log(source_count) * 0.1 if source_count > 1 else 1.0, 3
                    )

                    result.append(primary)

                    clusters.append(SignalCluster(
                        primary_signal=primary.to_dict(),
                        supporting_signals=[s.to_dict() for s in supporting],
                        source_count=source_count,
                        confidence_boost=primary.details["dedup_confidence_boost"],
                        cluster_id=str(uuid.uuid4())[:8],
                    ))

            self._last_clusters = clusters
            logger.info(
                "Dedup: %d signals → %d (clusters: %d)",
                len(signals), len(result), len(clusters),
            )
            return result

        except Exception as e:
            logger.error("Dedup failed, returning original signals: %s", e)
            return signals

    def apply_decay(self, signals: list[Signal]) -> list[Signal]:
        """Reduce signal strength based on age. Older signals have less edge.

        Decay model: strength *= exp(-lambda * hours_since_signal)

        Returns:
            Signals with decayed strength. Signals below MIN_STRENGTH_THRESHOLD are dropped.
        """
        if not signals:
            return []

        try:
            now = datetime.now(timezone.utc)
            result: list[Signal] = []

            for sig in signals:
                hours_old = self._hours_since(sig.timestamp, now)
                lam = DECAY_LAMBDAS.get(sig.source, 0.15)

                decay_factor = math.exp(-lam * hours_old)
                sig.strength = sig.strength * decay_factor

                if sig.strength >= MIN_STRENGTH_THRESHOLD:
                    sig.details = dict(sig.details) if sig.details else {}
                    sig.details["decay_factor"] = round(decay_factor, 4)
                    sig.details["hours_old"] = round(hours_old, 2)
                    result.append(sig)

            dropped = len(signals) - len(result)
            if dropped:
                logger.info("Decay: dropped %d expired signals (below %.2f)", dropped, MIN_STRENGTH_THRESHOLD)

            return result

        except Exception as e:
            logger.error("Decay failed, returning original signals: %s", e)
            return signals

    def get_cluster_stats(self) -> list[dict]:
        """Return cluster info from the last dedup run for dashboard."""
        return [c.to_dict() for c in getattr(self, "_last_clusters", [])]

    def _cluster_signals(self, signals: list[Signal]) -> list[dict]:
        """Cluster signals by temporal proximity and content similarity."""
        if not signals:
            return []

        # Sort by effective score (strength * confidence) descending
        sorted_sigs = sorted(
            signals,
            key=lambda s: s.strength * s.confidence,
            reverse=True,
        )

        clusters: list[dict] = []
        assigned: set[int] = set()

        for i, sig in enumerate(sorted_sigs):
            if i in assigned:
                continue

            cluster = {
                "primary": sig,
                "supporting": [],
                "sources": {sig.source},
            }
            assigned.add(i)

            for j, other in enumerate(sorted_sigs):
                if j in assigned:
                    continue
                if other.source == sig.source:
                    # Same source — only cluster if very similar (different signal_type)
                    if other.signal_type == sig.signal_type:
                        continue

                if self._should_cluster(sig, other):
                    cluster["supporting"].append(other)
                    cluster["sources"].add(other.source)
                    assigned.add(j)

            clusters.append(cluster)

        return clusters

    def _should_cluster(self, a: Signal, b: Signal) -> bool:
        """Check if two signals should be in the same cluster."""
        # Must be about the same market (already grouped by market_id)
        # Check temporal proximity
        try:
            time_a = datetime.fromisoformat(a.timestamp.replace("Z", "+00:00"))
            time_b = datetime.fromisoformat(b.timestamp.replace("Z", "+00:00"))
            hours_apart = abs((time_a - time_b).total_seconds()) / 3600
            if hours_apart > CLUSTER_TIME_WINDOW_HOURS:
                return False
        except (ValueError, AttributeError):
            pass  # If timestamps are bad, allow clustering based on content

        # Check content similarity via keyword overlap
        keywords_a = self._extract_keywords(a)
        keywords_b = self._extract_keywords(b)

        if not keywords_a or not keywords_b:
            # If no keywords, cluster by same direction
            return a.direction == b.direction

        overlap = keywords_a & keywords_b
        union = keywords_a | keywords_b
        similarity = len(overlap) / len(union) if union else 0

        return similarity >= 0.3 or a.direction == b.direction

    def _extract_keywords(self, sig: Signal) -> set[str]:
        """Extract meaningful keywords from signal details."""
        text_parts = []

        # Gather text from various fields
        if sig.market_question:
            text_parts.append(sig.market_question)
        details = sig.details or {}
        for key in ("summary", "text", "description", "keywords", "title", "query"):
            val = details.get(key, "")
            if isinstance(val, str) and val:
                text_parts.append(val)
            elif isinstance(val, list):
                text_parts.extend(str(v) for v in val)

        text = " ".join(text_parts).lower()
        words = set(re.findall(r"[a-z]{3,}", text))
        return words - _STOP_WORDS

    def _hours_since(self, timestamp_str: str, now: datetime) -> float:
        """Calculate hours since a timestamp string."""
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            return max((now - ts).total_seconds() / 3600, 0.0)
        except (ValueError, AttributeError):
            return 0.0  # If can't parse, treat as fresh
