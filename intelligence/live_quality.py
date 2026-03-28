"""Live Signal Quality Scorer — rolling 7-day signal quality tracker.

Tracks real-time signal source accuracy on a rolling window. Automatically
reduces weight of underperforming sources and boosts well-performing ones
without waiting for the full calibrator threshold.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("intelligence.live_quality")

_project_root = Path(__file__).resolve().parent.parent
try:
    DATA_DIR = Path("/root/polymarket-bot/data") if Path("/root/polymarket-bot/data").exists() else _project_root / "data"
except (PermissionError, OSError):
    DATA_DIR = _project_root / "data"

QUALITY_LOG_FILE = "live_quality_log.json"


class LiveQualityScorer:
    """Rolling 7-day signal quality tracker with automatic weight adjustments."""

    LOOKBACK_DAYS = int(os.getenv("QUALITY_LOOKBACK_DAYS", "7"))
    PENALTY_THRESHOLD = float(os.getenv("QUALITY_PENALTY_THRESHOLD", "0.40"))
    REWARD_THRESHOLD = float(os.getenv("QUALITY_REWARD_THRESHOLD", "0.65"))
    MAX_PENALTY = 0.5       # Reduce weight by at most 50%
    MAX_REWARD = 1.3        # Boost weight by at most 30%
    MIN_SIGNALS_FOR_EVAL = 3

    def update(self, signal_source: str, market_id: str, direction: str, outcome: str):
        """Record a signal outcome (correct/incorrect/pending).

        Args:
            signal_source: e.g. "x_scanner", "metaculus"
            market_id: Polymarket condition_id or slug
            direction: Signal direction ("YES"/"NO")
            outcome: "correct", "incorrect", or "pending"
        """
        try:
            entry = {
                "source": signal_source,
                "market_id": market_id,
                "direction": direction,
                "outcome": outcome,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / QUALITY_LOG_FILE

            log = self._load_log()
            log.append(entry)

            # Trim entries older than 2x lookback (keep some extra history)
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS * 2)
            log = [
                e for e in log
                if self._parse_ts(e.get("timestamp", "")) > cutoff
            ]

            path.write_text(json.dumps({"log": log}, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to update quality log: %s", e)

    def get_weight_adjustments(self) -> dict[str, float]:
        """Calculate per-source weight multipliers based on recent accuracy.

        Returns:
            {source: multiplier} where MAX_PENALTY <= multiplier <= MAX_REWARD
        """
        try:
            log = self._load_log()
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)

            # Filter to lookback window and resolved signals only
            recent = [
                e for e in log
                if self._parse_ts(e.get("timestamp", "")) > cutoff
                and e.get("outcome") in ("correct", "incorrect")
            ]

            # Group by source
            by_source: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
            for entry in recent:
                source = entry.get("source", "")
                if not source:
                    continue
                by_source[source]["total"] += 1
                if entry["outcome"] == "correct":
                    by_source[source]["correct"] += 1

            adjustments: dict[str, float] = {}
            for source, stats in by_source.items():
                if stats["total"] < self.MIN_SIGNALS_FOR_EVAL:
                    adjustments[source] = 1.0
                    continue

                accuracy = stats["correct"] / stats["total"]

                if accuracy < self.PENALTY_THRESHOLD:
                    # Penalize: scale from MAX_PENALTY (worst) to 1.0 (at threshold)
                    multiplier = max(
                        self.MAX_PENALTY,
                        accuracy / self.PENALTY_THRESHOLD,
                    )
                elif accuracy > self.REWARD_THRESHOLD:
                    # Reward: scale from 1.0 (at threshold) to MAX_REWARD (best)
                    multiplier = min(
                        self.MAX_REWARD,
                        accuracy / self.REWARD_THRESHOLD,
                    )
                else:
                    multiplier = 1.0

                adjustments[source] = round(multiplier, 4)

            return adjustments

        except Exception as e:
            logger.error("Failed to compute quality adjustments: %s", e)
            return {}

    def get_health_report(self) -> dict[str, dict]:
        """Return current health of each signal source for dashboard.

        Returns:
            {source: {accuracy_7d, signals_7d, streak, status, multiplier}}
        """
        try:
            log = self._load_log()
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
            adjustments = self.get_weight_adjustments()

            # Filter to lookback window
            recent = [
                e for e in log
                if self._parse_ts(e.get("timestamp", "")) > cutoff
            ]

            by_source: dict[str, list[dict]] = defaultdict(list)
            for entry in recent:
                source = entry.get("source", "")
                if source:
                    by_source[source].append(entry)

            report: dict[str, dict] = {}
            for source, entries in by_source.items():
                resolved = [e for e in entries if e.get("outcome") in ("correct", "incorrect")]
                total = len(resolved)
                correct = sum(1 for e in resolved if e["outcome"] == "correct")
                accuracy = correct / total if total > 0 else 0.0

                # Calculate streak (consecutive correct/incorrect)
                streak = 0
                if resolved:
                    sorted_entries = sorted(
                        resolved,
                        key=lambda e: e.get("timestamp", ""),
                        reverse=True,
                    )
                    last_outcome = sorted_entries[0]["outcome"]
                    for e in sorted_entries:
                        if e["outcome"] == last_outcome:
                            streak += 1
                        else:
                            break
                    if last_outcome == "incorrect":
                        streak = -streak

                # Status
                if total < self.MIN_SIGNALS_FOR_EVAL:
                    status = "insufficient_data"
                elif accuracy > self.REWARD_THRESHOLD:
                    status = "hot"
                elif accuracy < self.PENALTY_THRESHOLD:
                    status = "cold"
                else:
                    status = "normal"

                report[source] = {
                    "accuracy_7d": round(accuracy, 4),
                    "signals_7d": total,
                    "pending_7d": len(entries) - total,
                    "streak": streak,
                    "status": status,
                    "multiplier": adjustments.get(source, 1.0),
                }

            return report

        except Exception as e:
            logger.error("Failed to generate health report: %s", e)
            return {}

    def _load_log(self) -> list[dict]:
        """Load quality log from disk."""
        path = DATA_DIR / QUALITY_LOG_FILE
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else data.get("log", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load quality log: %s", e)
            return []

    def _parse_ts(self, ts_str: str) -> datetime:
        """Parse an ISO timestamp string to datetime."""
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.min.replace(tzinfo=timezone.utc)
