"""
Paper trading tracker with JSON persistence.

Tracks all recommended bets, checks resolutions, and computes performance stats.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.common.config import PAPER_TRADES_FILE
from agents.common.polymarket_client import fetch_markets

logger = logging.getLogger(__name__)


class PaperTracker:
    """Thread-safe paper trading tracker backed by a JSON file."""

    def __init__(self, filepath: str | None = None):
        self.filepath = filepath or PAPER_TRADES_FILE
        self._trades: list[dict[str, Any]] = []
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        """Load trades from disk."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self._trades = json.load(f)
                logger.info("Loaded %d paper trades from %s", len(self._trades), self.filepath)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load paper trades: %s", exc)
                self._trades = []
        else:
            self._trades = []

    def _save(self) -> None:
        """Persist trades to disk."""
        try:
            Path(self.filepath).parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w") as f:
                json.dump(self._trades, f, indent=2, default=str)
        except OSError as exc:
            logger.error("Failed to save paper trades: %s", exc)

    # ── Recording ─────────────────────────────────────────────

    def record_trade(
        self,
        market_slug: str,
        market_question: str,
        direction: str,
        entry_price: float,
        recommended_size: float,
        fair_prob: float,
        market_prob: float,
        edge: float,
        confidence: str,
        agent_name: str,
        reasoning: str,
    ) -> dict:
        """Record a new paper trade recommendation."""
        trade = {
            "id": f"{agent_name}_{int(time.time())}_{market_slug[:30]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_slug": market_slug,
            "market_question": market_question,
            "direction": direction,
            "entry_price": entry_price,
            "recommended_size": recommended_size,
            "fair_prob": fair_prob,
            "market_prob": market_prob,
            "edge": edge,
            "confidence": confidence,
            "agent_name": agent_name,
            "reasoning": reasoning,
            "resolved": False,
            "outcome": None,
            "pnl": 0.0,
        }
        self._trades.append(trade)
        self._save()
        logger.info("Recorded paper trade: %s %s on %s", direction, entry_price, market_slug)
        return trade

    # ── Resolution checking ───────────────────────────────────

    def check_resolutions(self) -> list[dict]:
        """Check unresolved trades against Gamma API. Returns newly resolved."""
        newly_resolved = []
        unresolved = [t for t in self._trades if not t["resolved"]]
        if not unresolved:
            return []

        # Group by slug to avoid duplicate API calls
        slugs_checked: dict[str, dict | None] = {}
        for trade in unresolved:
            slug = trade["market_slug"]
            if slug not in slugs_checked:
                markets = fetch_markets(slug=slug, closed="true")
                slugs_checked[slug] = markets[0] if markets else None

            market = slugs_checked[slug]
            if not market:
                continue

            # Check if market is closed/resolved
            is_closed = (
                str(market.get("closed", "")).lower() == "true"
                or market.get("closed") is True
            )
            if not is_closed:
                continue

            # Determine outcome
            outcome_prices_raw = market.get("outcomePrices")
            if not outcome_prices_raw:
                continue
            try:
                prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
                yes_price = float(prices[0])
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                continue

            # Resolved: YES wins if price ~1.0, NO wins if price ~0.0
            if yes_price > 0.9:
                winner = "YES"
            elif yes_price < 0.1:
                winner = "NO"
            else:
                continue  # Not fully resolved yet

            trade["resolved"] = True
            trade["outcome"] = winner

            # Calculate P&L
            if trade["direction"].upper() == winner:
                # Won: payout is (1 - entry_price) * size
                trade["pnl"] = round(
                    (1.0 - trade["entry_price"]) * trade["recommended_size"], 2
                )
            else:
                # Lost: lose entry_price * size
                trade["pnl"] = round(
                    -trade["entry_price"] * trade["recommended_size"], 2
                )

            newly_resolved.append(trade)

        if newly_resolved:
            self._save()
            logger.info("Resolved %d paper trades", len(newly_resolved))

        return newly_resolved

    # ── Stats ─────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Compute aggregate performance stats."""
        resolved = [t for t in self._trades if t["resolved"]]
        wins = [t for t in resolved if t["pnl"] > 0]
        total_pnl = sum(t["pnl"] for t in resolved)
        edges = [abs(t["edge"]) for t in self._trades if t.get("edge")]

        best_trade = max(resolved, key=lambda t: t["pnl"], default=None)
        worst_trade = min(resolved, key=lambda t: t["pnl"], default=None)

        return {
            "total": len(self._trades),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": (len(wins) / len(resolved) * 100) if resolved else 0.0,
            "pnl": round(total_pnl, 2),
            "avg_edge": (sum(edges) / len(edges) * 100) if edges else 0.0,
            "best_bet": (
                f"+${best_trade['pnl']:.0f} ({best_trade['market_question'][:40]})"
                if best_trade and best_trade["pnl"] > 0 else None
            ),
            "worst_bet": (
                f"-${abs(worst_trade['pnl']):.0f} ({worst_trade['market_question'][:40]})"
                if worst_trade and worst_trade["pnl"] < 0 else None
            ),
        }

    def get_active_count(self) -> int:
        """Number of unresolved positions."""
        return sum(1 for t in self._trades if not t["resolved"])

    def get_agent_stats(self, agent_name: str) -> dict[str, int]:
        """Per-agent alert/scan counts for daily summary."""
        agent_trades = [t for t in self._trades if t["agent_name"] == agent_name]
        return {
            "alerts": len(agent_trades),
            "today_alerts": sum(
                1 for t in agent_trades
                if t.get("timestamp", "")[:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ),
        }
