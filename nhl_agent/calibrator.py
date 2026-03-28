"""Self-learning calibration engine for NHL bets.

Same pattern as nba_agent/calibrator.py but with separate tracking.
Activates after 200 resolved NHL bets.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from nhl_agent.config import NHLConfig
from nba_agent.utils import load_json, atomic_json_write

logger = logging.getLogger(__name__)

_MIN_BETS_FOR_LEARNING = 200
_MIN_CATEGORY_BETS = 30


class NHLCalibrator:
    """Tracks NHL prediction accuracy and adjusts model parameters."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()
        self._path = self.config.nhl_calibration_path
        self._data = self._load()

    def _load(self) -> dict:
        default = {
            "version": 1,
            "total_resolved": 0,
            "active": False,
            "last_updated": None,
            "edge_buckets": {},
            "confidence_tiers": {
                "HIGH": {"bets": 0, "wins": 0, "pnl": 0.0},
                "MEDIUM": {"bets": 0, "wins": 0, "pnl": 0.0},
                "LOW": {"bets": 0, "wins": 0, "pnl": 0.0},
            },
            "home_away": {
                "home": {"bets": 0, "wins": 0, "pnl": 0.0},
                "away": {"bets": 0, "wins": 0, "pnl": 0.0},
            },
            "vegas_accuracy": {
                "bets_with_vegas": 0,
                "vegas_correct": 0,
                "model_correct": 0,
                "both_correct": 0,
            },
            "adjustments": {
                "edge_shrinkage": 1.0,
                "min_edge_override": None,
                "vegas_weight_override": None,
            },
        }
        saved = load_json(self._path, default)
        for key in default:
            if key not in saved:
                saved[key] = default[key]
        return saved

    def save(self) -> None:
        self.config.ensure_data_dir()
        self._data["last_updated"] = datetime.now(timezone.utc).isoformat()
        atomic_json_write(self._path, self._data)

    @property
    def is_active(self) -> bool:
        return self._data.get("active", False)

    @property
    def total_resolved(self) -> int:
        return self._data.get("total_resolved", 0)

    def record_result(
        self,
        won: bool,
        edge: float,
        confidence: str,
        side: str,
        pnl: float,
        had_vegas_line: bool = False,
        vegas_agreed: bool = False,
    ) -> None:
        """Record the outcome of a resolved NHL bet."""
        self._data["total_resolved"] = self._data.get("total_resolved", 0) + 1

        # Edge bucket
        if edge < 0.06:
            bucket = "4-6"
        elif edge < 0.10:
            bucket = "6-10"
        else:
            bucket = "10+"
        buckets = self._data.setdefault("edge_buckets", {})
        b = buckets.setdefault(bucket, {"bets": 0, "wins": 0, "total_edge": 0.0, "total_pnl": 0.0})
        b["bets"] += 1
        if won:
            b["wins"] += 1
        b["total_edge"] += edge
        b["total_pnl"] += pnl

        # Confidence tier
        conf_key = confidence.upper()
        tiers = self._data.setdefault("confidence_tiers", {})
        tier = tiers.setdefault(conf_key, {"bets": 0, "wins": 0, "pnl": 0.0})
        tier["bets"] += 1
        if won:
            tier["wins"] += 1
        tier["pnl"] += pnl

        # Home/away
        ha = self._data.setdefault("home_away", {})
        h = ha.setdefault(side, {"bets": 0, "wins": 0, "pnl": 0.0})
        h["bets"] += 1
        if won:
            h["wins"] += 1
        h["pnl"] += pnl

        # Vegas tracking
        if had_vegas_line:
            va = self._data.setdefault("vegas_accuracy", {
                "bets_with_vegas": 0, "vegas_correct": 0,
                "model_correct": 0, "both_correct": 0,
            })
            va["bets_with_vegas"] += 1
            if vegas_agreed and won:
                va["both_correct"] += 1
            elif vegas_agreed:
                va["vegas_correct"] += 1
            elif won:
                va["model_correct"] += 1

        # Check activation
        if self._data["total_resolved"] >= _MIN_BETS_FOR_LEARNING and not self._data.get("active"):
            self._activate_learning()

        self.save()

    def _activate_learning(self) -> None:
        """Compute and apply learned adjustments after 200+ bets."""
        logger.info("NHL CALIBRATOR ACTIVATING: %d bets resolved", self._data["total_resolved"])

        adjustments = self._data.setdefault("adjustments", {})
        buckets = self._data.get("edge_buckets", {})

        total_predicted = 0.0
        total_actual = 0.0
        total_bets = 0
        for bd in buckets.values():
            n = bd.get("bets", 0)
            if n > 0:
                avg_edge = bd["total_edge"] / n
                actual_roi = bd["total_pnl"] / (n * 15)
                total_predicted += avg_edge * n
                total_actual += actual_roi * n
                total_bets += n

        if total_bets > 0 and total_predicted > 0:
            shrinkage = min(1.2, max(0.5, total_actual / total_predicted))
            adjustments["edge_shrinkage"] = round(shrinkage, 3)

        low_bucket = buckets.get("4-6", {})
        if low_bucket.get("bets", 0) >= _MIN_CATEGORY_BETS:
            low_wr = low_bucket["wins"] / low_bucket["bets"]
            if low_wr < 0.47 and low_bucket["total_pnl"] < 0:
                adjustments["min_edge_override"] = 0.06

        self._data["active"] = True
        self.save()
        logger.info("NHL Calibrator activated: %s", adjustments)

    def get_summary(self) -> dict:
        return {
            "total_resolved": self.total_resolved,
            "active": self.is_active,
            "bets_until_active": max(0, _MIN_BETS_FOR_LEARNING - self.total_resolved),
            "edge_buckets": self._data.get("edge_buckets", {}),
            "confidence_tiers": self._data.get("confidence_tiers", {}),
            "home_away": self._data.get("home_away", {}),
            "vegas_accuracy": self._data.get("vegas_accuracy", {}),
            "adjustments": self._data.get("adjustments", {}),
        }
