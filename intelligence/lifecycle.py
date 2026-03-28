"""Event Lifecycle Manager — classifies markets into lifecycle stages.

Detects where a market is in its lifecycle and adapts strategy parameters.
A market 6 months from resolution is fundamentally different from one 3 days away.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from enum import Enum

from intelligence.models import LifecycleAssessment

logger = logging.getLogger("intelligence.lifecycle")


class Stage(Enum):
    EARLY = "early"              # >60 days to resolution
    DEVELOPING = "developing"    # 14-60 days
    MATURE = "mature"            # 3-14 days
    LATE = "late"                # 1-3 days
    TERMINAL = "terminal"        # <24 hours to resolution
    UNKNOWN = "unknown"          # No end date available


# Stage-specific parameter configurations
# Format: {min_edge, max_bet_pct, hold_strategy, take_profit, stop_loss, signal_weight_overrides}
STAGE_CONFIGS = {
    Stage.EARLY: {
        "min_edge": float(os.getenv("LIFECYCLE_EARLY_MIN_EDGE", "0.08")),
        "max_bet_pct": 0.01,
        "hold_strategy": "accumulate",
        "take_profit": 0.40,
        "stop_loss": 0.20,
        "signal_weight_overrides": {
            "metaculus": 1.4,
            "cross_market": 1.3,
            "x_scanner": 0.7,
            "orderbook": 0.6,
            "whale_tracker": 0.8,
            "google_trends": 0.9,
            "congress": 1.1,
        },
    },
    Stage.DEVELOPING: {
        "min_edge": float(os.getenv("LIFECYCLE_DEVELOPING_MIN_EDGE", "0.05")),
        "max_bet_pct": 0.015,
        "hold_strategy": "hold",
        "take_profit": 0.30,
        "stop_loss": 0.25,
        "signal_weight_overrides": {
            "metaculus": 1.0,
            "cross_market": 1.0,
            "x_scanner": 1.0,
            "orderbook": 1.0,
            "whale_tracker": 1.0,
            "google_trends": 1.0,
            "congress": 1.0,
        },
    },
    Stage.MATURE: {
        "min_edge": float(os.getenv("LIFECYCLE_MATURE_MIN_EDGE", "0.04")),
        "max_bet_pct": 0.02,
        "hold_strategy": "active",
        "take_profit": 0.20,
        "stop_loss": 0.15,
        "signal_weight_overrides": {
            "metaculus": 0.8,
            "cross_market": 0.9,
            "x_scanner": 1.3,
            "orderbook": 1.4,
            "whale_tracker": 1.1,
            "google_trends": 1.0,
            "congress": 0.7,
        },
    },
    Stage.LATE: {
        "min_edge": float(os.getenv("LIFECYCLE_LATE_MIN_EDGE", "0.03")),
        "max_bet_pct": 0.02,
        "hold_strategy": "hold_to_resolution",
        "take_profit": 0.15,
        "stop_loss": 0.10,
        "signal_weight_overrides": {
            "metaculus": 0.6,
            "cross_market": 0.7,
            "x_scanner": 0.9,
            "orderbook": 1.5,
            "whale_tracker": 1.4,
            "google_trends": 0.5,
            "congress": 0.5,
        },
    },
    Stage.TERMINAL: {
        "min_edge": float(os.getenv("LIFECYCLE_TERMINAL_MIN_EDGE", "0.02")),
        "max_bet_pct": 0.02,
        "hold_strategy": "hold_to_resolution",
        "take_profit": 0.10,
        "stop_loss": 0.05,
        "signal_weight_overrides": {
            "metaculus": 0.3,
            "cross_market": 0.3,
            "x_scanner": 0.5,
            "orderbook": 1.6,
            "whale_tracker": 1.2,
            "google_trends": 0.3,
            "congress": 0.3,
        },
    },
    Stage.UNKNOWN: {
        "min_edge": 0.06,
        "max_bet_pct": 0.01,
        "hold_strategy": "hold",
        "take_profit": 0.30,
        "stop_loss": 0.25,
        "signal_weight_overrides": {},
    },
}


class EventLifecycle:
    """Classifies markets into lifecycle stages and adjusts parameters."""

    def classify(self, market) -> LifecycleAssessment:
        """Determine market stage and return stage-adjusted parameters.

        Args:
            market: An EventMarket or similar object with an `end_date` attribute.
                    end_date can be a datetime, ISO string, or None.

        Returns:
            LifecycleAssessment with stage-specific adjustments.
        """
        try:
            days = self._days_remaining(market)
            stage = self._determine_stage(days)
        except Exception as e:
            logger.warning("Lifecycle classification failed: %s", e)
            days = -1.0
            stage = Stage.UNKNOWN

        cfg = STAGE_CONFIGS[stage]

        return LifecycleAssessment(
            stage=stage.value,
            days_remaining=round(days, 2),
            min_edge=cfg["min_edge"],
            max_bet_pct=cfg["max_bet_pct"],
            signal_weight_overrides=dict(cfg["signal_weight_overrides"]),
            hold_strategy=cfg["hold_strategy"],
            take_profit=cfg["take_profit"],
            stop_loss=cfg["stop_loss"],
        )

    def _days_remaining(self, market) -> float:
        """Calculate days until market resolution."""
        end_date = getattr(market, "end_date", None)
        if end_date is None:
            return -1.0

        if isinstance(end_date, str):
            if not end_date:
                return -1.0
            try:
                end_date = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return -1.0

        if not isinstance(end_date, datetime):
            return -1.0

        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        delta = end_date - now
        return max(delta.total_seconds() / 86400, 0.0)

    def _determine_stage(self, days: float) -> Stage:
        """Map days remaining to lifecycle stage."""
        if days < 0:
            return Stage.UNKNOWN
        if days < 1:
            return Stage.TERMINAL
        if days < 3:
            return Stage.LATE
        if days < 14:
            return Stage.MATURE
        if days < 60:
            return Stage.DEVELOPING
        return Stage.EARLY
