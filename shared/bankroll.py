"""Shared bankroll manager with file locking for NBA + NHL agents.

Both agents share one bankroll (data/bankroll.json). Total exposure
calculations include open positions from BOTH sports. All reads/writes
are atomic and use fcntl advisory locks to prevent corruption when
two agents access the file concurrently.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — same DATA_DIR logic as nba_agent/config.py
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Locked JSON helpers
# ---------------------------------------------------------------------------

def _locked_read(path: Path, default: Any = None) -> Any:
    """Read a JSON file under a shared (read) lock."""
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return default if default is not None else {}


def _locked_write(path: Path, data: Any) -> None:
    """Write JSON atomically under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2, default=str)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Shared bankroll state
# ---------------------------------------------------------------------------

_BANKROLL_PATH = _DATA_DIR / "bankroll.json"


def load_bankroll() -> dict:
    """Load the shared bankroll state."""
    return _locked_read(_BANKROLL_PATH, {})


def save_bankroll(state: dict) -> None:
    """Save the shared bankroll state atomically."""
    _ensure_data_dir()
    _locked_write(_BANKROLL_PATH, state)


def get_current_bankroll() -> float:
    """Quick read of current bankroll value."""
    state = load_bankroll()
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
    default_starting = float(os.getenv("STARTING_BANKROLL", "440.58"))
    return float(state.get("current_bankroll", default_starting))


# ---------------------------------------------------------------------------
# Cross-sport exposure
# ---------------------------------------------------------------------------

def get_total_open_exposure() -> float:
    """Sum open position costs across NBA and NHL."""
    total = 0.0
    for filename in ("positions.json", "nhl_positions.json"):
        path = _DATA_DIR / filename
        data = _locked_read(path, {"positions": []})
        for p in data.get("positions", []):
            if p.get("status") == "open":
                total += float(p.get("cost", 0))
    return total


def get_sport_open_exposure(sport: str) -> float:
    """Sum open position costs for a single sport ('nba' or 'nhl')."""
    filename = "nhl_positions.json" if sport == "nhl" else "positions.json"
    path = _DATA_DIR / filename
    data = _locked_read(path, {"positions": []})
    total = 0.0
    for p in data.get("positions", []):
        if p.get("status") == "open":
            total += float(p.get("cost", 0))
    return total


def check_total_exposure_ok(proposed_bet: float, max_pct: float = 0.50) -> bool:
    """Check if adding a bet would exceed total exposure limit (NBA + NHL)."""
    bankroll = get_current_bankroll()
    current_exposure = get_total_open_exposure()
    return (current_exposure + proposed_bet) <= (bankroll * max_pct)
