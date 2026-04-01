"""Shared helpers — utility functions used across all agents and modules."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def parse_utc(s: str) -> datetime:
    """Parse an ISO-ish datetime string into a UTC-aware datetime."""
    s = s.strip()
    # Handle Polymarket formats like "2026-03-22T16:00:00Z" or "2026-03-22 21:15:00+00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S+00",
        "%Y-%m-%d %H:%M:%S+00:00",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Last resort: strip trailing timezone info and treat as UTC
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        raise ValueError(f"Cannot parse datetime: {s}")


def atomic_json_write(path: Path, data: Any) -> None:
    """Write JSON atomically — write to temp file then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON file, returning default if it doesn't exist."""
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return default if default is not None else {}


def format_price(price: float) -> str:
    """Format a probability price as cents."""
    return f"{price * 100:.0f}¢"


def format_dollars(amount: float) -> str:
    """Format a dollar amount."""
    return f"${amount:,.2f}"


def format_pct(pct: float) -> str:
    """Format a percentage."""
    return f"{pct * 100:.1f}%"


def format_edge(edge: float) -> str:
    """Format an edge value."""
    return f"{edge * 100:.1f}%"
