"""Odds comparison snapshots — records Polymarket price vs Vegas line vs
model fair value for every game at each scan cycle.

Stores data in data/odds_snapshots.json. Keeps last 48 hours per game,
auto-prunes older entries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_ODDS_SNAPSHOTS_PATH = _DATA_DIR / "odds_snapshots.json"


def _load() -> dict[str, Any]:
    if not _ODDS_SNAPSHOTS_PATH.exists():
        return {"snapshots": []}
    try:
        data = json.loads(_ODDS_SNAPSHOTS_PATH.read_text())
        if "snapshots" not in data:
            data["snapshots"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"snapshots": []}


def _save(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ODDS_SNAPSHOTS_PATH.write_text(json.dumps(data, indent=2, default=str))


def record(
    sport: str,
    game: str,
    game_date: str,
    game_slug: str,
    polymarket_home: float,
    polymarket_away: float,
    vegas_home: float | None = None,
    vegas_away: float | None = None,
    model_home: float | None = None,
    model_away: float | None = None,
    our_fair_home: float | None = None,
    our_fair_away: float | None = None,
) -> None:
    """Record an odds snapshot for a game."""
    data = _load()
    now = datetime.now(timezone.utc).isoformat()

    edge_home = None
    edge_away = None
    if our_fair_home is not None:
        edge_home = round(our_fair_home - polymarket_home, 4)
    if our_fair_away is not None:
        edge_away = round(our_fair_away - polymarket_away, 4)

    snapshot = {
        "time": now,
        "sport": sport,
        "game": game,
        "game_date": game_date,
        "game_slug": game_slug,
        "polymarket_home": round(polymarket_home, 4),
        "polymarket_away": round(polymarket_away, 4),
        "vegas_home": round(vegas_home, 4) if vegas_home is not None else None,
        "vegas_away": round(vegas_away, 4) if vegas_away is not None else None,
        "model_home": round(model_home, 4) if model_home is not None else None,
        "model_away": round(model_away, 4) if model_away is not None else None,
        "our_fair_home": round(our_fair_home, 4) if our_fair_home is not None else None,
        "our_fair_away": round(our_fair_away, 4) if our_fair_away is not None else None,
        "edge_home": edge_home,
        "edge_away": edge_away,
    }

    data["snapshots"].append(snapshot)
    _save(data)


def record_from_evaluation(
    sport: str,
    market_question: str,
    market_slug: str,
    game_date: str,
    outcome_prices: list[float],
    our_fair_price: float | None = None,
    side_index: int = 0,
    vegas_price: float | None = None,
) -> None:
    """Convenience method to record from an edge evaluation.

    Takes the evaluation data as the agents have it and maps to the
    home/away format.
    """
    if len(outcome_prices) < 2:
        return

    poly_home = outcome_prices[0]
    poly_away = outcome_prices[1]

    fair_home = None
    fair_away = None
    vegas_home = None
    vegas_away = None

    if our_fair_price is not None:
        if side_index == 0:
            fair_home = our_fair_price
            fair_away = round(1.0 - our_fair_price, 4)
        else:
            fair_away = our_fair_price
            fair_home = round(1.0 - our_fair_price, 4)

    if vegas_price is not None:
        if side_index == 0:
            vegas_home = vegas_price
            vegas_away = round(1.0 - vegas_price, 4)
        else:
            vegas_away = vegas_price
            vegas_home = round(1.0 - vegas_price, 4)

    record(
        sport=sport,
        game=market_question,
        game_date=game_date,
        game_slug=market_slug,
        polymarket_home=poly_home,
        polymarket_away=poly_away,
        vegas_home=vegas_home,
        vegas_away=vegas_away,
        our_fair_home=fair_home,
        our_fair_away=fair_away,
    )


def prune_old_data() -> int:
    """Remove snapshots older than 48 hours per game. Returns count removed."""
    data = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    original_count = len(data["snapshots"])

    data["snapshots"] = [
        s for s in data["snapshots"]
        if _parse_time(s.get("time", "")) >= cutoff
    ]

    removed = original_count - len(data["snapshots"])
    if removed > 0:
        _save(data)

    return removed


def _parse_time(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def get_latest_snapshots() -> list[dict]:
    """Return the most recent snapshot for each active game."""
    data = _load()
    # Group by game_slug, take the latest
    latest: dict[str, dict] = {}
    for snap in data["snapshots"]:
        slug = snap.get("game_slug", "")
        existing = latest.get(slug)
        if not existing or snap.get("time", "") > existing.get("time", ""):
            latest[slug] = snap

    return list(latest.values())


def get_game_history(game_slug: str) -> list[dict]:
    """Return all snapshots for a specific game."""
    data = _load()
    return [
        s for s in data["snapshots"]
        if s.get("game_slug") == game_slug
    ]
