"""Backtester — replays historical signals against price data to evaluate quality."""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from intelligence.models import BacktestReport

logger = logging.getLogger(__name__)

_project_root = Path(__file__).resolve().parent.parent
try:
    DATA_DIR = Path("/root/polymarket-bot/data") if Path("/root/polymarket-bot/data").exists() else _project_root / "data"
except (PermissionError, OSError):
    DATA_DIR = _project_root / "data"


class Backtester:
    """Replay historical signals against actual Polymarket price movements."""

    def run(self, days: int = 30) -> BacktestReport:
        """Run a full backtest over the given period.

        1. Load historical signals from data/intelligence_signals_history.json
        2. Load historical price data from data/price_history/
        3. For each signal: calculate P&L as if we bet $100 on signal direction
        4. Aggregate by source and by composite tier
        """
        signals = self._load_signals(days)
        price_data = self._load_price_history()

        if not signals:
            return BacktestReport(
                period_days=days,
                total_signals=0,
                by_source={},
                by_tier={},
                equity_curve=[],
                best_source="",
                worst_source="",
            )

        # Track results per source and per tier
        source_results: dict[str, list] = defaultdict(list)
        tier_results: dict[str, list] = defaultdict(list)
        daily_pnl: dict[str, float] = defaultdict(float)
        bet_amount = 100.0

        for sig in signals:
            market_id = sig.get("market_id", "")
            direction = sig.get("direction", "")
            strength = sig.get("strength", 0)
            source = sig.get("source", "unknown")
            timestamp = sig.get("timestamp", "")

            # Need price at signal time and 24h later
            entry_price = self._get_price_at_time(price_data, market_id, timestamp)
            exit_time = self._add_hours(timestamp, 24)
            exit_price = self._get_price_at_time(price_data, market_id, exit_time)

            if entry_price is None or exit_price is None:
                continue

            # Calculate P&L
            if direction == "YES":
                # Bought YES at entry_price, sold at exit_price
                shares = bet_amount / entry_price if entry_price > 0 else 0
                pnl = shares * (exit_price - entry_price)
            elif direction == "NO":
                # Bought NO, which is 1 - YES price
                no_entry = 1.0 - entry_price
                no_exit = 1.0 - exit_price
                shares = bet_amount / no_entry if no_entry > 0 else 0
                pnl = shares * (no_exit - no_entry)
            else:
                continue

            pnl = round(pnl, 2)
            won = pnl > 0

            result = {"pnl": pnl, "won": won, "strength": strength}
            source_results[source].append(result)

            # Map strength to tier
            tier = self._strength_to_tier(strength)
            tier_results[tier].append(result)

            # Daily P&L
            try:
                day = timestamp[:10]
                daily_pnl[day] += pnl
            except (TypeError, IndexError):
                pass

        # Aggregate by source
        by_source = {}
        for source, results in source_results.items():
            wins = [r for r in results if r["won"]]
            total = len(results)
            total_pnl = sum(r["pnl"] for r in results)
            pnls = [r["pnl"] for r in results]
            avg_pnl = total_pnl / total if total > 0 else 0

            # Sharpe ratio (simplified)
            if len(pnls) > 1:
                mean = sum(pnls) / len(pnls)
                variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
                std = math.sqrt(variance) if variance > 0 else 1
                sharpe = round(mean / std, 2)
            else:
                sharpe = 0

            by_source[source] = {
                "signals": total,
                "win_rate": round(len(wins) / total * 100, 1) if total > 0 else 0,
                "avg_pnl": round(avg_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "sharpe": sharpe,
            }

        # Aggregate by tier
        by_tier = {}
        for tier, results in tier_results.items():
            wins = [r for r in results if r["won"]]
            total = len(results)
            total_pnl = sum(r["pnl"] for r in results)
            avg_pnl = total_pnl / total if total > 0 else 0

            by_tier[tier] = {
                "signals": total,
                "win_rate": round(len(wins) / total * 100, 1) if total > 0 else 0,
                "avg_pnl": round(avg_pnl, 2),
                "total_pnl": round(total_pnl, 2),
            }

        # Equity curve
        sorted_days = sorted(daily_pnl.keys())
        equity_curve = []
        cumulative = 0
        for day in sorted_days:
            cumulative += daily_pnl[day]
            equity_curve.append({
                "date": day,
                "pnl": round(daily_pnl[day], 2),
                "cumulative": round(cumulative, 2),
            })

        # Best / worst source
        best_source = ""
        worst_source = ""
        if by_source:
            best_source = max(by_source, key=lambda s: by_source[s]["total_pnl"])
            worst_source = min(by_source, key=lambda s: by_source[s]["total_pnl"])

        total_signals = sum(len(r) for r in source_results.values())

        return BacktestReport(
            period_days=days,
            total_signals=total_signals,
            by_source=by_source,
            by_tier=by_tier,
            equity_curve=equity_curve,
            best_source=best_source,
            worst_source=worst_source,
        )

    def backtest_signal_source(self, source: str, days: int = 30) -> dict:
        """Run a single-source backtest for comparison."""
        report = self.run(days)
        return report.by_source.get(source, {
            "signals": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0, "sharpe": 0,
        })

    def _load_signals(self, days: int) -> list[dict]:
        """Load historical signals from data files."""
        signals = []

        # Primary: intelligence_signals_history.json
        history_path = DATA_DIR / "intelligence_signals_history.json"
        if history_path.exists():
            try:
                data = json.loads(history_path.read_text())
                signals = data.get("signals", [])
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback: current signals file
        if not signals:
            current_path = DATA_DIR / "intelligence_signals.json"
            if current_path.exists():
                try:
                    data = json.loads(current_path.read_text())
                    signals = data.get("signals", [])
                except (json.JSONDecodeError, OSError):
                    pass

        # Filter by date range
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        filtered = []
        for sig in signals:
            ts = sig.get("timestamp", "")
            if ts >= cutoff:
                filtered.append(sig)

        return filtered

    def _load_price_history(self) -> dict:
        """Load price history data.

        Returns: {market_id: [(timestamp, price), ...]}
        """
        price_data: dict[str, list] = {}
        price_dir = DATA_DIR / "price_history"
        if not price_dir.exists():
            return price_data

        for path in price_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                market_id = data.get("market_id", path.stem)
                prices = data.get("prices", [])
                price_data[market_id] = prices
            except (json.JSONDecodeError, OSError):
                continue

        return price_data

    def _get_price_at_time(
        self,
        price_data: dict,
        market_id: str,
        timestamp: str,
    ) -> float | None:
        """Find the closest price for a market at a given timestamp."""
        prices = price_data.get(market_id, [])
        if not prices:
            return None

        # Find closest price by timestamp
        target_ts = timestamp
        closest = None
        min_diff = float("inf")

        for entry in prices:
            entry_ts = entry.get("timestamp", "") if isinstance(entry, dict) else ""
            price = entry.get("price") if isinstance(entry, dict) else None
            if not entry_ts or price is None:
                continue

            try:
                target = datetime.fromisoformat(target_ts.replace("Z", "+00:00"))
                entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                diff = abs((target - entry_dt).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    closest = float(price)
            except (ValueError, AttributeError):
                continue

        return closest

    @staticmethod
    def _add_hours(timestamp: str, hours: int) -> str:
        """Add hours to an ISO timestamp string."""
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return (dt + timedelta(hours=hours)).isoformat()
        except (ValueError, AttributeError):
            return timestamp

    @staticmethod
    def _strength_to_tier(strength: float) -> str:
        """Map signal strength to a confidence tier."""
        if strength >= 0.8:
            return "VERY_HIGH"
        elif strength >= 0.6:
            return "HIGH"
        elif strength >= 0.4:
            return "MEDIUM"
        else:
            return "LOW"
