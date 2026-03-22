"""Paper trading tracker — persists all recommended bets to JSON and tracks P&L."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.common.config import PAPER_TRADES_FILE
from agents.common.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


class PaperTracker:
    """Records paper trades, checks resolutions, and computes performance stats."""

    def __init__(self, file_path: str | None = None) -> None:
        self._path = Path(file_path or PAPER_TRADES_FILE)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._trades: list[dict[str, Any]] = self._load()
        self._client = PolymarketClient()

    # ── persistence ──────────────────────────────────────────
    def _load(self) -> list[dict[str, Any]]:
        if self._path.exists():
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt paper trades file — starting fresh")
        return []

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._trades, f, indent=2, default=str)
            tmp.replace(self._path)
        except OSError:
            logger.exception("Failed to save paper trades")

    # ── recording ────────────────────────────────────────────
    def record_trade(
        self,
        *,
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
        end_date: str = "",
        condition_id: str = "",
    ) -> dict[str, Any]:
        """Record a new paper trade. Returns the trade dict."""
        trade: dict[str, Any] = {
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
            "end_date": end_date,
            "condition_id": condition_id,
            "resolved": False,
            "outcome": None,
            "pnl": 0.0,
        }
        self._trades.append(trade)
        self._save()
        logger.info("Paper trade recorded: %s %s @ %.2f", direction, market_slug, entry_price)
        return trade

    # ── resolution checking ──────────────────────────────────
    def check_resolutions(self) -> list[dict[str, Any]]:
        """Check unresolved trades against the Gamma API. Returns newly resolved."""
        newly_resolved = []
        for trade in self._trades:
            if trade.get("resolved"):
                continue

            slug = trade.get("market_slug", "")
            condition_id = trade.get("condition_id", "")
            if not slug and not condition_id:
                continue

            try:
                markets = self._client.fetch_markets(active=False, closed=True, limit=10)
                for mkt in markets:
                    mkt_slug = mkt.get("slug") or mkt.get("conditionId", "")
                    mkt_cond = mkt.get("conditionId", "")
                    if mkt_slug == slug or mkt_cond == condition_id:
                        prices = self._client.parse_outcome_prices(mkt)
                        if prices:
                            self._resolve_trade(trade, prices)
                            newly_resolved.append(trade)
                        break
                time.sleep(0.1)
            except Exception:
                logger.exception("Error checking resolution for %s", slug)

        if newly_resolved:
            self._save()
        return newly_resolved

    def _resolve_trade(self, trade: dict, final_prices: dict[str, float]) -> None:
        """Mark a trade as resolved and calculate P&L."""
        direction = trade.get("direction", "YES").upper()
        entry = trade.get("entry_price", 0.5)
        size = trade.get("recommended_size", 10)

        yes_price = final_prices.get("Yes", 0.0)
        no_price = final_prices.get("No", 0.0)

        # Determine if outcome was YES (final YES price ~1.0) or NO
        outcome = "YES" if yes_price > 0.9 else "NO" if no_price > 0.9 else "AMBIGUOUS"

        if outcome == "AMBIGUOUS":
            trade["resolved"] = True
            trade["outcome"] = "AMBIGUOUS"
            trade["pnl"] = 0.0
            return

        # P&L calculation: if we bet YES at entry_price and YES wins → profit = (1-entry)*size
        if direction == "YES":
            if outcome == "YES":
                trade["pnl"] = round((1.0 - entry) * size, 2)
            else:
                trade["pnl"] = round(-entry * size, 2)
        else:  # direction == NO
            if outcome == "NO":
                trade["pnl"] = round((1.0 - (1.0 - entry)) * size, 2)
            else:
                trade["pnl"] = round(-(1.0 - entry) * size, 2)

        trade["resolved"] = True
        trade["outcome"] = outcome

    # ── statistics ───────────────────────────────────────────
    def get_stats(self) -> dict[str, Any]:
        """Compute aggregate performance statistics."""
        total = len(self._trades)
        resolved = [t for t in self._trades if t.get("resolved")]
        unresolved = [t for t in self._trades if not t.get("resolved")]
        wins = [t for t in resolved if t.get("pnl", 0) > 0]
        pnl = sum(t.get("pnl", 0) for t in resolved)
        edges = [abs(t.get("edge", 0)) for t in self._trades if t.get("edge")]
        avg_edge = (sum(edges) / len(edges) * 100) if edges else 0

        # Best/worst
        best = max(resolved, key=lambda t: t.get("pnl", 0), default=None)
        worst = min(resolved, key=lambda t: t.get("pnl", 0), default=None)

        best_str = "N/A"
        worst_str = "N/A"
        if best:
            best_str = f"+${best['pnl']:.0f} ({best.get('market_slug', 'unknown')[:30]})"
        if worst:
            worst_str = f"${worst['pnl']:.0f} ({worst.get('market_slug', 'unknown')[:30]})"

        return {
            "total_bets": total,
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": (len(wins) / len(resolved) * 100) if resolved else 0,
            "pnl": pnl,
            "avg_edge": avg_edge,
            "best_bet": best_str,
            "worst_bet": worst_str,
            "active_positions": len(unresolved),
        }

    def get_agent_stats(self, agent_name: str) -> dict[str, int]:
        """Get per-agent alert/scan counts for daily summary."""
        agent_trades = [t for t in self._trades if t.get("agent_name") == agent_name]
        return {
            "alerts_sent": len(agent_trades),
            "markets_scanned": 0,  # updated externally
        }
