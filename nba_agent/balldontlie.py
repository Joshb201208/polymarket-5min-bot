"""BallDontLie API integration — advanced stats, injuries, box scores.

GOAT tier ($40/mo) provides: season averages, advanced stats, injuries,
box scores, standings, betting odds, player props, and lineups.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from nba_agent.config import Config

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.balldontlie.io"


class BDLClient:
    """BallDontLie API client with caching."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.api_key = self.config.BALLDONTLIE_API_KEY

        # Caches
        self._injuries_cache: list[dict] | None = None
        self._injuries_ts: datetime | None = None
        self._standings_cache: list[dict] | None = None
        self._standings_ts: datetime | None = None
        self._team_averages_cache: dict[int, dict] | None = None
        self._team_averages_ts: datetime | None = None
        self._cache_ttl = timedelta(hours=2)
        self._injuries_ttl = timedelta(minutes=30)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """Make an authenticated GET request."""
        if not self.is_configured:
            return None

        try:
            resp = httpx.get(
                f"{_BASE_URL}{path}",
                headers={"Authorization": self.api_key},
                params=params or {},
                timeout=20.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("BDL API error on %s: %s", path, e)
            return None

    # ── Injuries ───────────────────────────────────────────────────

    def get_injuries(self) -> list[dict]:
        """Get current NBA injury reports. Cached for 30 min."""
        now = datetime.now(timezone.utc)
        if self._injuries_cache is not None and self._injuries_ts and (now - self._injuries_ts) < self._injuries_ttl:
            return self._injuries_cache

        data = self._get("/nba/v1/player_injuries")
        if not data:
            return self._injuries_cache or []

        injuries = data.get("data", [])
        self._injuries_cache = injuries
        self._injuries_ts = now
        logger.info("BDL: loaded %d injury reports", len(injuries))
        return injuries

    def get_team_injuries(self, team_abbr: str) -> list[dict]:
        """Get injuries for a specific team."""
        all_injuries = self.get_injuries()
        return [
            inj for inj in all_injuries
            if inj.get("team", {}).get("abbreviation", "").upper() == team_abbr.upper()
        ]

    def count_team_out(self, team_abbr: str) -> tuple[int, list[str]]:
        """Count players OUT for a team. Returns (count, [player names])."""
        injuries = self.get_team_injuries(team_abbr)
        out_players = []
        for inj in injuries:
            status = inj.get("status", "").lower()
            if status in ("out", "doubtful"):
                player = inj.get("player", {})
                name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
                if name:
                    out_players.append(name)
        return len(out_players), out_players

    # ── Team Season Averages ───────────────────────────────────────

    def get_team_season_averages(self) -> dict[int, dict]:
        """Get team season averages (advanced stats). Cached for 2 hours.
        Returns {bdl_team_id: stats_dict}."""
        now = datetime.now(timezone.utc)
        if self._team_averages_cache and self._team_averages_ts and (now - self._team_averages_ts) < self._cache_ttl:
            return self._team_averages_cache

        data = self._get(
            "/nba/v1/team_season_averages/general",
            params={
                "season": 2025,  # BDL uses start year of season
                "season_type": "regular",
                "type": "advanced",
                "per_page": 30,
            },
        )
        if not data:
            return self._team_averages_cache or {}

        result = {}
        for entry in data.get("data", []):
            team = entry.get("team", {})
            tid = team.get("id")
            if tid:
                result[tid] = {
                    "team_name": f"{team.get('city', '')} {team.get('name', '')}".strip(),
                    "team_abbr": team.get("abbreviation", ""),
                    "off_rating": entry.get("off_rating", 0.0),
                    "def_rating": entry.get("def_rating", 0.0),
                    "net_rating": entry.get("net_rating", 0.0),
                    "pace": entry.get("pace", 0.0),
                    "ts_pct": entry.get("ts_pct", 0.0),     # True shooting %
                    "efg_pct": entry.get("efg_pct", 0.0),   # Effective FG%
                    "ast_pct": entry.get("ast_pct", 0.0),
                    "reb_pct": entry.get("reb_pct", 0.0),
                    "pie": entry.get("pie", 0.0),            # Player Impact Estimate
                }

        self._team_averages_cache = result
        self._team_averages_ts = now
        logger.info("BDL: loaded advanced stats for %d teams", len(result))
        return result

    # ── Standings ──────────────────────────────────────────────────

    def get_standings(self) -> list[dict]:
        """Get current NBA standings. Cached for 2 hours."""
        now = datetime.now(timezone.utc)
        if self._standings_cache and self._standings_ts and (now - self._standings_ts) < self._cache_ttl:
            return self._standings_cache

        data = self._get(
            "/nba/v1/standings",
            params={"season": 2025},
        )
        if not data:
            return self._standings_cache or []

        standings = data.get("data", [])
        self._standings_cache = standings
        self._standings_ts = now
        logger.info("BDL: loaded standings for %d teams", len(standings))
        return standings

    # ── Games ─────────────────────────────────────────────────────

    def get_todays_games(self) -> list[dict]:
        """Get today's NBA games."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = self._get("/nba/v1/games", params={"dates[]": today})
        if not data:
            return []
        return data.get("data", [])

    # ── Mapping helper ─────────────────────────────────────────────

    def find_team_advanced_stats(self, team_abbr: str) -> dict | None:
        """Look up advanced stats for a team by abbreviation."""
        averages = self.get_team_season_averages()
        for tid, stats in averages.items():
            if stats.get("team_abbr", "").upper() == team_abbr.upper():
                return stats
        return None
