"""Cross-agent bankroll coordination.

Provides read-only queries for total exposure so the events agent
can check limits before placing a bet. File-based locking is used
since agents run in the same process via the orchestrator.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from shared.utils import load_json

logger = logging.getLogger(__name__)

# File-based lock for bankroll reads (same process, different coroutines)
_bankroll_lock = threading.Lock()


def get_total_exposure(data_dir: Path) -> float:
    """Calculate total open exposure across all agents.

    Reads events positions and sums up open costs.
    """
    with _bankroll_lock:
        total = 0.0

        # Events positions
        events_positions = load_json(data_dir / "events_positions.json", {"positions": []})
        for p in events_positions.get("positions", []):
            if p.get("status") == "open":
                total += float(p.get("cost", 0))

        return total


def get_agent_exposure(data_dir: Path, agent: str) -> float:
    """Get open exposure for a specific agent."""
    with _bankroll_lock:
        if agent == "events":
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
