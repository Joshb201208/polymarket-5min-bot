"""Self-Learning Calibrator — auto-recalibrates composite scorer weights.

After N resolved event trades, analyzes which signal sources actually predicted
correctly and adjusts weights accordingly. Runs once per 24h.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from intelligence.models import CalibrationResult

logger = logging.getLogger("intelligence.calibrator")

_project_root = Path(__file__).resolve().parent.parent
try:
    DATA_DIR = Path("/root/polymarket-bot/data") if Path("/root/polymarket-bot/data").exists() else _project_root / "data"
except (PermissionError, OSError):
    DATA_DIR = _project_root / "data"

# Default weights from composite_scorer
DEFAULT_WEIGHTS: dict[str, float] = {
    "metaculus": 0.25,
    "x_scanner": 0.20,
    "orderbook": 0.15,
    "whale_tracker": 0.15,
    "google_trends": 0.10,
    "congress": 0.08,
    "cross_market": 0.07,
}


class SignalCalibrator:
    """Auto-recalibrates composite scorer weights from resolved trade outcomes."""

    CALIBRATION_THRESHOLD = int(os.getenv("CALIBRATOR_THRESHOLD", "30"))
    RECALIBRATION_INTERVAL_HOURS = int(os.getenv("CALIBRATOR_INTERVAL_HOURS", "24"))
    MIN_SIGNALS_PER_SOURCE = 5
    SMOOTHING_FACTOR = float(os.getenv("CALIBRATOR_SMOOTHING", "0.7"))

    def __init__(self):
        self._last_calibration: datetime | None = None
        self._current_weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        self._last_result: CalibrationResult | None = None
        self._load_last_calibration()

    def should_calibrate(self) -> bool:
        """Check if enough time has passed and enough data exists."""
        if self._last_calibration is not None:
            hours_since = (datetime.now(timezone.utc) - self._last_calibration).total_seconds() / 3600
            if hours_since < self.RECALIBRATION_INTERVAL_HOURS:
                return False

        resolved = self._load_resolved_trades()
        return len(resolved) >= self.CALIBRATION_THRESHOLD

    def calibrate(self) -> CalibrationResult:
        """Analyze resolved trades and recalibrate weights.

        Returns:
            CalibrationResult with new weights and per-source metrics.
        """
        try:
            resolved_trades = self._load_resolved_trades()
            if len(resolved_trades) < self.CALIBRATION_THRESHOLD:
                logger.info(
                    "Not enough resolved trades for calibration (%d/%d)",
                    len(resolved_trades), self.CALIBRATION_THRESHOLD,
                )
                return self._empty_result(len(resolved_trades))

            signal_history = self._load_signal_history()
            source_metrics = self._compute_source_metrics(resolved_trades, signal_history)
            new_weights = self._compute_new_weights(source_metrics)
            smoothed = self._apply_smoothing(new_weights)

            result = CalibrationResult(
                calibrated_weights=smoothed,
                default_weights=dict(DEFAULT_WEIGHTS),
                source_metrics=source_metrics,
                resolved_trades=len(resolved_trades),
                smoothing_factor=self.SMOOTHING_FACTOR,
            )

            self._current_weights = smoothed
            self._last_calibration = datetime.now(timezone.utc)
            self._last_result = result
            self._save_calibration(result)

            logger.info(
                "Calibration complete: %d trades, weights=%s",
                len(resolved_trades),
                {k: round(v, 3) for k, v in smoothed.items()},
            )
            return result

        except Exception as e:
            logger.error("Calibration failed: %s", e)
            return self._empty_result(0)

    def get_current_weights(self) -> dict[str, float]:
        """Return the current calibrated (or default) weights."""
        return dict(self._current_weights)

    def get_source_report(self) -> dict:
        """Return per-source performance for dashboard display."""
        if self._last_result and self._last_result.source_metrics:
            return dict(self._last_result.source_metrics)
        return {}

    def _load_resolved_trades(self) -> list[dict]:
        """Load resolved trades from events_trades.json."""
        path = DATA_DIR / "events_trades.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            trades = data if isinstance(data, list) else data.get("trades", [])
            return [
                t for t in trades
                if t.get("pnl") is not None or t.get("action") == "SELL"
            ]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load trades: %s", e)
            return []

    def _load_signal_history(self) -> list[dict]:
        """Load signal history for matching signals to trades."""
        for name in ("intelligence_signals_history.json", "intelligence_signals.json"):
            path = DATA_DIR / name
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    return data if isinstance(data, list) else data.get("signals", [])
                except (json.JSONDecodeError, OSError):
                    continue
        return []

    def _compute_source_metrics(
        self, trades: list[dict], signals: list[dict]
    ) -> dict[str, dict]:
        """Compute accuracy, profitability, lift, and Brier score per source."""
        # Index signals by market_id for lookup
        signals_by_market: dict[str, list[dict]] = defaultdict(list)
        for sig in signals:
            mid = sig.get("market_id", "")
            if mid:
                signals_by_market[mid].append(sig)

        # Track per-source outcomes
        source_stats: dict[str, dict] = defaultdict(lambda: {
            "correct": 0, "incorrect": 0, "total_pnl": 0.0,
            "brier_sum": 0.0, "count": 0,
        })

        # Baseline win rate (all trades)
        total_wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        baseline_win_rate = total_wins / len(trades) if trades else 0.5

        for trade in trades:
            market_id = trade.get("market_id", "")
            pnl = trade.get("pnl", 0) or 0
            won = pnl > 0
            trade_direction = trade.get("side", trade.get("direction", ""))

            market_signals = signals_by_market.get(market_id, [])
            sources_present = set()

            for sig in market_signals:
                source = sig.get("source", "")
                if not source or source in sources_present:
                    continue
                sources_present.add(source)

                sig_direction = sig.get("direction", "")
                sig_confidence = sig.get("confidence", 0.5)

                # Check if signal direction matched trade outcome
                direction_correct = (sig_direction == trade_direction and won) or \
                                    (sig_direction != trade_direction and not won)

                stats = source_stats[source]
                stats["count"] += 1
                if direction_correct:
                    stats["correct"] += 1
                else:
                    stats["incorrect"] += 1
                stats["total_pnl"] += pnl

                # Brier score: (confidence - outcome)^2
                outcome = 1.0 if won else 0.0
                stats["brier_sum"] += (sig_confidence - outcome) ** 2

        # Build metrics
        metrics: dict[str, dict] = {}
        for source in set(list(source_stats.keys()) + list(DEFAULT_WEIGHTS.keys())):
            stats = source_stats.get(source)
            if not stats or stats["count"] < self.MIN_SIGNALS_PER_SOURCE:
                metrics[source] = {
                    "accuracy": 0.5,
                    "profitability": 0.0,
                    "lift": 1.0,
                    "brier": 0.25,
                    "sample_size": stats["count"] if stats else 0,
                    "status": "insufficient_data",
                }
                continue

            accuracy = stats["correct"] / stats["count"]
            profitability = stats["total_pnl"] / stats["count"]
            brier = stats["brier_sum"] / stats["count"]
            lift = accuracy / baseline_win_rate if baseline_win_rate > 0 else 1.0

            if accuracy > 0.55:
                status = "trusted"
            elif accuracy >= 0.45:
                status = "neutral"
            else:
                status = "degraded"

            metrics[source] = {
                "accuracy": round(accuracy, 4),
                "profitability": round(profitability, 4),
                "lift": round(lift, 4),
                "brier": round(brier, 4),
                "sample_size": stats["count"],
                "status": status,
            }

        return metrics

    def _compute_new_weights(self, metrics: dict[str, dict]) -> dict[str, float]:
        """Compute new weights from accuracy * lift, normalized."""
        raw: dict[str, float] = {}
        for source, m in metrics.items():
            if m["status"] == "insufficient_data":
                raw[source] = DEFAULT_WEIGHTS.get(source, 0.05)
            else:
                raw[source] = max(m["accuracy"] * m["lift"], 0.01)

        total = sum(raw.values())
        if total <= 0:
            return dict(DEFAULT_WEIGHTS)

        return {source: val / total for source, val in raw.items()}

    def _apply_smoothing(self, calibrated: dict[str, float]) -> dict[str, float]:
        """Blend calibrated weights with defaults to prevent wild swings."""
        smoothed: dict[str, float] = {}
        all_sources = set(list(calibrated.keys()) + list(DEFAULT_WEIGHTS.keys()))

        for source in all_sources:
            cal = calibrated.get(source, 0.0)
            default = DEFAULT_WEIGHTS.get(source, 0.05)
            smoothed[source] = self.SMOOTHING_FACTOR * cal + (1 - self.SMOOTHING_FACTOR) * default

        # Re-normalize
        total = sum(smoothed.values())
        if total > 0:
            smoothed = {k: v / total for k, v in smoothed.items()}

        return smoothed

    def _save_calibration(self, result: CalibrationResult):
        """Append calibration result to history."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "calibration_history.json"

            history = []
            if path.exists():
                try:
                    history = json.loads(path.read_text())
                    if not isinstance(history, list):
                        history = history.get("history", [])
                except (json.JSONDecodeError, OSError):
                    history = []

            history.append(result.to_dict())

            # Keep last 100 calibrations
            history = history[-100:]

            path.write_text(json.dumps({"history": history}, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to save calibration history: %s", e)

    def _load_last_calibration(self):
        """Load the most recent calibration from history."""
        try:
            path = DATA_DIR / "calibration_history.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            history = data if isinstance(data, list) else data.get("history", [])
            if history:
                last = history[-1]
                self._last_result = CalibrationResult.from_dict(last)
                weights = last.get("calibrated_weights", {})
                if weights:
                    self._current_weights = weights
                ts = last.get("timestamp", "")
                if ts:
                    self._last_calibration = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    )
        except Exception as e:
            logger.warning("Failed to load calibration history: %s", e)

    def _empty_result(self, resolved_count: int) -> CalibrationResult:
        return CalibrationResult(
            calibrated_weights=dict(DEFAULT_WEIGHTS),
            default_weights=dict(DEFAULT_WEIGHTS),
            source_metrics={},
            resolved_trades=resolved_count,
            smoothing_factor=self.SMOOTHING_FACTOR,
        )
