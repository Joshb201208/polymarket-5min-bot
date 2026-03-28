"""Cross-agent bankroll coordination.

All agents share one bankroll. This module provides read-only queries
for total exposure across all agents so each can check limits before
placing a bet. File-based locking is used since agents run in the
same process via the orchestrator.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from nba_agent.utils import load_json

logger = logging.getLogger(__name__)

# File-based lock for bankroll reads (same process, different coroutines)
_bankroll_lock = threading.Lock()


def get_total_exposure(data_dir: Path) -> float:
    """Calculate total open exposure across ALL agents.

    Reads NBA positions + events positions and sums up open costs.
    """
    with _bankroll_lock:
        total = 0.0

        # NBA positions
        nba_positions = load_json(data_dir / "positions.json", {"positions": []})
        for p in nba_positions.get("positions", []):
            if p.get("status") == "open":
                total += float(p.get("cost", 0))

        # Events positions
        events_positions = load_json(data_dir / "events_positions.json", {"positions": []})
        for p in events_positions.get("positions", []):
            if p.get("status") == "open":
                total += float(p.get("cost", 0))

        return total


def get_agent_exposure(data_dir: Path, agent: str) -> float:
    """Get open exposure for a specific agent."""
    with _bankroll_lock:
        if agent == "nba":
            filename = "positions.json"
        elif agent == "events":
            filename = "events_positions.json"
        else:
            return 0.0

        positions = load_json(data_dir / filename, {"positions": []})
        return sum(
            float(p.get("cost", 0))
            for p in positions.get("positions", [])
            if p.get("status") == "open"
        )


def check_exposure_available(data_dir: Path, bankroll: float, max_pct: float, proposed_bet: float) -> bool:
    """Check if placing a bet would exceed the total exposure limit."""
    current = get_total_exposure(data_dir)
    max_exposure = bankroll * max_pct
    return (current + proposed_bet) <= max_exposure
