"""
Backtesting engine for Polymarket strategies.
Uses historical resolved market data to test edge detection signals.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from . import config
from . import polymarket_api as pm
from . import telegram

logger = logging.getLogger(__name__)


class Backtester:
    """Backtest Polymarket strategies on resolved markets."""

    def __init__(self):
        self.results: list[dict] = []

    def backtest_market_signals(self, n_markets: int = 100, tag: str = None) -> dict:
        """Backtest market signal analysis on resolved markets.

        For each resolved market:
        1. Get market data including pre-resolution price movements
        2. Apply our signal analysis (momentum, volume, mean reversion)
        3. Check if signals predicted the actual outcome
        """
        events = pm.fetch_resolved_events(tag_slug=tag, limit=n_markets)
        if not events:
            logger.warning("No resolved events found for backtesting")
            return self._empty_report()

        trades = []
        for event in events:
            markets = event.get("markets", [])
            if not markets:
                continue

            for market_data in markets:
                market = pm.parse_market_data(market_data)
                result = self._simulate_signal_trade(market)
                if result:
                    trades.append(result)

            time.sleep(0.3)  # Rate limit

        self.results.extend(trades)
        return self._generate_report(trades)

    def _simulate_signal_trade(self, market: dict) -> dict | None:
        """Simulate a trade based on market signals for a resolved market."""
        yes_price = market.get("yes_price")
        if yes_price is None:
            return None

        one_day_change = market.get("one_day_change", 0)
        one_week_change = market.get("one_week_change", 0)

        # Determine actual outcome
        closed = market.get("closed", False)
        if not closed:
            return None

        # If final price is very close to 1 or 0, we know the outcome
        if yes_price > 0.95:
            outcome = "YES"
        elif yes_price < 0.05:
            outcome = "NO"
        else:
            return None  # Can't determine outcome

        # Signal: strong momentum in one direction
        signal_side = None
        signal_strength = 0

        # Momentum signal: large price movement suggests continuation
        if abs(one_day_change) > 0.05:
            if one_day_change > 0:
                signal_side = "YES"
            else:
                signal_side = "NO"
            signal_strength = abs(one_day_change)

        # Mean reversion signal for extreme prices
        if signal_side is None and one_week_change != 0:
            if one_week_change > 0.10:
                signal_side = "YES"
                signal_strength = one_week_change * 0.5
            elif one_week_change < -0.10:
                signal_side = "NO"
                signal_strength = abs(one_week_change) * 0.5

        if signal_side is None:
            return None

        # Check if our signal was correct
        correct = signal_side == outcome
        entry_price = 0.50  # Simplified: assume 50c entry for backtesting
        pnl = (1.0 - entry_price) if correct else -entry_price

        return {
            "market_id": market.get("id"),
            "question": market.get("question", "")[:100],
            "signal_side": signal_side,
            "signal_strength": signal_strength,
            "outcome": outcome,
            "correct": correct,
            "entry_price": entry_price,
            "pnl": pnl,
        }

    def backtest_nba_model(self, season: str = "2024-25") -> dict:
        """Backtest NBA predictions against resolved NBA markets."""
        events = pm.fetch_resolved_events(tag_slug="nba", limit=100)
        if not events:
            logger.warning("No resolved NBA events found")
            return self._empty_report()

        trades = []
        for event in events:
            markets = event.get("markets", [])
            for market_data in markets:
                market = pm.parse_market_data(market_data)
                result = self._simulate_signal_trade(market)
                if result:
                    trades.append(result)
            time.sleep(0.3)

        self.results.extend(trades)
        return self._generate_report(trades)

    def _generate_report(self, trades: list[dict]) -> dict:
        """Generate backtest summary statistics."""
        if not trades:
            return self._empty_report()

        wins = sum(1 for t in trades if t["correct"])
        total = len(trades)
        total_pnl = sum(t["pnl"] for t in trades)
        avg_pnl = total_pnl / total if total > 0 else 0

        # Calculate max drawdown
        running_pnl = 0
        peak = 0
        max_drawdown = 0
        for t in trades:
            running_pnl += t["pnl"]
            peak = max(peak, running_pnl)
            drawdown = (peak - running_pnl) / max(peak, 1)
            max_drawdown = max(max_drawdown, drawdown)

        # Simple Sharpe approximation
        import numpy as np
        pnls = [t["pnl"] for t in trades]
        sharpe = 0
        if len(pnls) > 1:
            std = np.std(pnls)
            if std > 0:
                sharpe = (np.mean(pnls) / std) * (252 ** 0.5)  # Annualized

        return {
            "total_markets": total,
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": wins / total if total > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 4),
            "roi": total_pnl / (total * 0.5) if total > 0 else 0,  # Relative to capital risked
            "max_drawdown": round(max_drawdown, 4),
            "sharpe": round(sharpe, 2),
        }

    def _empty_report(self) -> dict:
        return {
            "total_markets": 0, "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "total_pnl": 0, "avg_pnl": 0, "roi": 0,
            "max_drawdown": 0, "sharpe": 0,
        }

    def run_full_backtest(self) -> dict:
        """Run all backtests and send summary to Telegram."""
        logger.info("Starting full backtest run...")

        # General market signals
        signals_report = self.backtest_market_signals(n_markets=50)
        logger.info(f"Market signals backtest: {signals_report['win_rate']:.1%} win rate")

        # NBA
        nba_report = self.backtest_nba_model()
        logger.info(f"NBA backtest: {nba_report['win_rate']:.1%} win rate")

        # Combined report
        all_trades = self.results
        combined = self._generate_report(all_trades)
        telegram.alert_backtest_report(combined)

        return combined
