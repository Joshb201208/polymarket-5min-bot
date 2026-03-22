"""Self-learning calibration engine.

Tracks model accuracy over time and adjusts weights once enough
data has accumulated (200+ resolved bets). Until then, operates
in observation-only mode — logging everything but changing nothing.

Adjustments:
1. Edge accuracy — if the model consistently overestimates edges,
   apply a shrinkage factor
2. Bet type performance — shift capital toward types that actually win
3. Confidence calibration — verify HIGH/MEDIUM/LOW map to real outcomes
4. Vegas blend — adjust model vs Vegas weighting based on which is more accurate
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nba_agent.config import Config
from nba_agent.utils import load_json, atomic_json_write

logger = logging.getLogger(__name__)

# Minimum resolved bets before any adjustments activate
_MIN_BETS_FOR_LEARNING = 200
# Minimum bets in a subcategory before adjusting that category
_MIN_CATEGORY_BETS = 30


class Calibrator:
    """Tracks prediction accuracy and adjusts model parameters.

    Observation mode (< 200 bets): logs calibration data, no changes.
    Active mode (>= 200 bets): applies learned corrections.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self._path = self.config.DATA_DIR / "calibration.json"
        self._data = self._load()

    def _load(self) -> dict:
        default = {
            "version": 1,
            "total_resolved": 0,
            "active": False,  # False = observation mode
            "last_updated": None,

            # Edge accuracy tracking
            "edge_buckets": {
                # "4-6": {"bets": 0, "wins": 0, "total_edge": 0, "total_pnl": 0},
                # "6-10": {...},
                # "10+": {...},
            },

            # Bet type tracking
            "bet_types": {
                # "moneyline": {"bets": 0, "wins": 0, "pnl": 0},
                # "spread": {...}, "total": {...}, "futures": {...},
            },

            # Confidence calibration
            "confidence_tiers": {
                "HIGH": {"bets": 0, "wins": 0, "pnl": 0.0},
                "MEDIUM": {"bets": 0, "wins": 0, "pnl": 0.0},
                "LOW": {"bets": 0, "wins": 0, "pnl": 0.0},
            },

            # Home vs away
            "home_away": {
                "home": {"bets": 0, "wins": 0, "pnl": 0.0},
                "away": {"bets": 0, "wins": 0, "pnl": 0.0},
            },

            # Vegas accuracy (when available)
            "vegas_accuracy": {
                "bets_with_vegas": 0,
                "vegas_correct": 0,  # Vegas favorite won
                "model_correct": 0,  # Model favorite won (when disagreeing with Vegas)
                "both_correct": 0,   # Both agreed and were right
            },

            # Learned adjustments (only applied when active=True)
            "adjustments": {
                "edge_shrinkage": 1.0,       # Multiply detected edge by this (1.0 = no change)
                "min_edge_override": None,    # Override minimum edge threshold
                "vegas_weight_override": None, # Override Vegas vs model blend
                "type_multipliers": {},        # Bet size multipliers by market type
            },
        }
        saved = load_json(self._path, default)
        # Merge defaults for any missing keys
        for key in default:
            if key not in saved:
                saved[key] = default[key]
        for tier in ("HIGH", "MEDIUM", "LOW"):
            if tier not in saved.get("confidence_tiers", {}):
                saved.setdefault("confidence_tiers", {})[tier] = {"bets": 0, "wins": 0, "pnl": 0.0}
        return saved

    def save(self) -> None:
        self.config.ensure_data_dir()
        self._data["last_updated"] = datetime.now(timezone.utc).isoformat()
        atomic_json_write(self._path, self._data)

    @property
    def is_active(self) -> bool:
        """Whether learned adjustments are being applied."""
        return self._data.get("active", False)

    @property
    def total_resolved(self) -> int:
        return self._data.get("total_resolved", 0)

    # ── Record a resolved bet ─────────────────────────────────────

    def record_result(
        self,
        won: bool,
        edge: float,
        confidence: str,
        market_type: str,
        side: str,  # "home" or "away"
        pnl: float,
        had_vegas_line: bool = False,
        vegas_agreed: bool = False,
    ) -> None:
        """Record the outcome of a resolved bet for calibration tracking."""
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

        # Bet type
        types = self._data.setdefault("bet_types", {})
        t = types.setdefault(market_type, {"bets": 0, "wins": 0, "pnl": 0.0})
        t["bets"] += 1
        if won:
            t["wins"] += 1
        t["pnl"] += pnl

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

        # Check if we should activate learning
        if self._data["total_resolved"] >= _MIN_BETS_FOR_LEARNING and not self._data.get("active"):
            self._activate_learning()

        self.save()

        if self._data["total_resolved"] % 50 == 0:
            logger.info("Calibrator: %d bets resolved. Active: %s",
                        self._data["total_resolved"], self._data.get("active", False))

    # ── Activate learning ─────────────────────────────────────────

    def _activate_learning(self) -> None:
        """Compute and apply learned adjustments after 200+ bets."""
        logger.info("CALIBRATOR ACTIVATING: %d bets resolved — computing adjustments",
                     self._data["total_resolved"])

        adjustments = self._data.setdefault("adjustments", {})

        # 1. Edge shrinkage — if our edges overestimate, apply correction
        buckets = self._data.get("edge_buckets", {})
        total_predicted_edge = 0.0
        total_actual_return = 0.0
        total_bets = 0
        for bucket_data in buckets.values():
            n = bucket_data.get("bets", 0)
            if n > 0:
                avg_edge = bucket_data["total_edge"] / n
                actual_roi = bucket_data["total_pnl"] / (n * 15)  # rough avg bet size
                total_predicted_edge += avg_edge * n
                total_actual_return += actual_roi * n
                total_bets += n

        if total_bets > 0 and total_predicted_edge > 0:
            # Shrinkage = actual performance / predicted performance
            # If we predicted 7% edge but reality is 5%, shrinkage = 0.71
            shrinkage = min(1.2, max(0.5, total_actual_return / total_predicted_edge))
            adjustments["edge_shrinkage"] = round(shrinkage, 3)
            logger.info("Edge shrinkage: %.3f (predicted edges were %s accurate)",
                        shrinkage, "over" if shrinkage < 1 else "under")

        # 2. Bet type multipliers — reward profitable types, penalize losing ones
        types = self._data.get("bet_types", {})
        type_multipliers = {}
        for mtype, tdata in types.items():
            if tdata["bets"] >= _MIN_CATEGORY_BETS:
                wr = tdata["wins"] / tdata["bets"]
                # If win rate > 52%, increase sizing. If < 48%, decrease.
                if wr > 0.55:
                    type_multipliers[mtype] = 1.3  # Bet 30% more on this type
                elif wr > 0.52:
                    type_multipliers[mtype] = 1.1
                elif wr < 0.45:
                    type_multipliers[mtype] = 0.5  # Halve bets on this type
                elif wr < 0.48:
                    type_multipliers[mtype] = 0.7
                else:
                    type_multipliers[mtype] = 1.0

                logger.info("Type %s: %d bets, %.1f%% WR -> multiplier %.1fx",
                            mtype, tdata["bets"], wr * 100, type_multipliers[mtype])

        if type_multipliers:
            adjustments["type_multipliers"] = type_multipliers

        # 3. Edge threshold — if small edges lose money, raise minimum
        low_bucket = buckets.get("4-6", {})
        if low_bucket.get("bets", 0) >= _MIN_CATEGORY_BETS:
            low_wr = low_bucket["wins"] / low_bucket["bets"]
            if low_wr < 0.47 and low_bucket["total_pnl"] < 0:
                adjustments["min_edge_override"] = 0.06  # Raise to 6%
                logger.info("Low-edge bets (4-6%%) are losing money (%.1f%% WR, $%.2f P&L) — raising min edge to 6%%",
                            low_wr * 100, low_bucket["total_pnl"])

        # 4. Vegas weight — if pure Vegas outperforms blend, increase Vegas weight
        va = self._data.get("vegas_accuracy", {})
        if va.get("bets_with_vegas", 0) >= 100:
            model_only = va.get("model_correct", 0)
            total_v = va["bets_with_vegas"]
            model_pct = model_only / total_v if total_v > 0 else 0
            if model_pct < 0.10:
                # Model rarely disagrees AND wins — increase Vegas weight
                adjustments["vegas_weight_override"] = 0.80
                logger.info("Model rarely outperforms Vegas when disagreeing — increasing Vegas weight to 80%%")
            elif model_pct > 0.25:
                # Model often disagrees AND wins — increase model weight
                adjustments["vegas_weight_override"] = 0.50
                logger.info("Model frequently outperforms Vegas — increasing model weight to 50%%")

        self._data["active"] = True
        self.save()
        logger.info("Calibrator activated with adjustments: %s", adjustments)

    # ── Get current adjustments ───────────────────────────────────

    def get_edge_shrinkage(self) -> float:
        """Returns edge multiplier. 1.0 if not active."""
        if not self.is_active:
            return 1.0
        return self._data.get("adjustments", {}).get("edge_shrinkage", 1.0)

    def get_min_edge_override(self) -> float | None:
        """Returns overridden min edge, or None to use default."""
        if not self.is_active:
            return None
        return self._data.get("adjustments", {}).get("min_edge_override")

    def get_vegas_weight(self) -> float | None:
        """Returns overridden Vegas weight, or None to use default."""
        if not self.is_active:
            return None
        return self._data.get("adjustments", {}).get("vegas_weight_override")

    def get_type_multiplier(self, market_type: str) -> float:
        """Returns bet size multiplier for a market type. 1.0 if not active."""
        if not self.is_active:
            return 1.0
        mults = self._data.get("adjustments", {}).get("type_multipliers", {})
        return mults.get(market_type, 1.0)

    # ── Summary for dashboard ─────────────────────────────────────

    def get_summary(self) -> dict:
        """Return calibration summary for the dashboard."""
        return {
            "total_resolved": self.total_resolved,
            "active": self.is_active,
            "bets_until_active": max(0, _MIN_BETS_FOR_LEARNING - self.total_resolved),
            "edge_buckets": self._data.get("edge_buckets", {}),
            "bet_types": self._data.get("bet_types", {}),
            "confidence_tiers": self._data.get("confidence_tiers", {}),
            "home_away": self._data.get("home_away", {}),
            "vegas_accuracy": self._data.get("vegas_accuracy", {}),
            "adjustments": self._data.get("adjustments", {}),
        }
