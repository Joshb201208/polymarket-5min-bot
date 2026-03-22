"""NBA stats engine — standings, game logs, H2H, team stats.

Data sources (tried in order):
1. NBA CDN schedule (cdn.nba.com) — full season game-by-game data
2. ESPN API (site.api.espn.com) — standings + team stats fallback

Both are public APIs accessible from any IP including cloud/VPS servers.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from nba_api.stats.static import teams as nba_teams_static

from nba_agent.config import Config
from nba_agent.models import TeamStats, H2HRecord

logger = logging.getLogger(__name__)

# ── Data source URLs ───────────────────────────────────────────────
_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
_ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"

# Browser headers — NBA CDN needs these to avoid 403 on cloud IPs
_HTTP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
}

# ── Static team data (no HTTP, bundled in nba_api package) ─────────
_ALL_TEAMS = nba_teams_static.get_teams()
_TEAM_BY_ABBR: dict[str, dict] = {t["abbreviation"].upper(): t for t in _ALL_TEAMS}
_TEAM_BY_NAME: dict[str, dict] = {}
for _t in _ALL_TEAMS:
    _TEAM_BY_NAME[_t["full_name"].lower()] = _t
    _TEAM_BY_NAME[_t["nickname"].lower()] = _t
    _TEAM_BY_NAME[_t["city"].lower() + " " + _t["nickname"].lower()] = _t

# ESPN team ID → nba_api team ID mapping
_ESPN_ABBR_TO_NBA_ID: dict[str, int] = {t["abbreviation"]: t["id"] for t in _ALL_TEAMS}

# Polymarket abbreviation normalization
_ABBR_MAP: dict[str, str] = {
    "BKN": "BKN", "BRK": "BKN",
    "CHA": "CHA", "CHO": "CHA",
    "GS": "GSW", "GSW": "GSW",
    "NY": "NYK", "NYK": "NYK",
    "NO": "NOP", "NOP": "NOP",
    "SA": "SAS", "SAS": "SAS",
    "PHX": "PHX", "PHO": "PHX",
    "WSH": "WAS", "WAS": "WAS",
    "UTA": "UTA", "UTAH": "UTA",
    "OKC": "OKC", "POR": "POR",
    "LAL": "LAL", "LAC": "LAC",
    "MEM": "MEM", "DEN": "DEN",
    "MIL": "MIL", "IND": "IND",
    "ATL": "ATL", "BOS": "BOS",
    "CHI": "CHI", "CLE": "CLE",
    "DAL": "DAL", "DET": "DET",
    "HOU": "HOU", "MIA": "MIA",
    "MIN": "MIN", "ORL": "ORL",
    "PHI": "PHI", "SAC": "SAC",
    "TOR": "TOR",
}


def resolve_team_abbr(abbr: str) -> str:
    """Map a Polymarket slug abbreviation to the nba_api abbreviation."""
    return _ABBR_MAP.get(abbr.upper(), abbr.upper())


def find_team_by_abbr(abbr: str) -> dict | None:
    """Look up an nba_api team by abbreviation."""
    resolved = resolve_team_abbr(abbr)
    return _TEAM_BY_ABBR.get(resolved)


def find_team_by_name(name: str) -> dict | None:
    """Look up an nba_api team by name (fuzzy)."""
    name_lower = name.lower().strip()
    if name_lower in _TEAM_BY_NAME:
        return _TEAM_BY_NAME[name_lower]
    for key, team in _TEAM_BY_NAME.items():
        if name_lower in key or key in name_lower:
            return team
    return None


# ═══════════════════════════════════════════════════════════════════
# ESPN standings fetcher (fallback — always works from cloud IPs)
# ═══════════════════════════════════════════════════════════════════

def _parse_espn_record(val: str) -> tuple[int, int]:
    """Parse '26-8' into (26, 8)."""
    try:
        parts = val.split("-")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 0, 0


def _fetch_espn_standings() -> dict[int, dict]:
    """Fetch standings from ESPN API — returns {nba_team_id: stats_dict}."""
    try:
        resp = httpx.get(_ESPN_STANDINGS_URL, params={"season": "2026"}, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("ESPN standings fetch failed: %s", e)
        return {}

    result: dict[int, dict] = {}
    for conference in data.get("children", []):
        for entry in conference.get("standings", {}).get("entries", []):
            team_info = entry.get("team", {})
            espn_abbr = team_info.get("abbreviation", "")
            team_name = team_info.get("displayName", "")

            # Map ESPN abbreviation to nba_api team ID
            nba_team = find_team_by_name(team_name) or find_team_by_abbr(espn_abbr)
            if not nba_team:
                logger.debug("Could not map ESPN team %s (%s)", team_name, espn_abbr)
                continue

            tid = nba_team["id"]
            stats_raw = {s["name"]: s for s in entry.get("stats", [])}

            wins = int(stats_raw.get("wins", {}).get("value", 0))
            losses = int(stats_raw.get("losses", {}).get("value", 0))
            ppg = float(stats_raw.get("avgPointsFor", {}).get("value", 0))
            opp_ppg = float(stats_raw.get("avgPointsAgainst", {}).get("value", 0))
            diff = float(stats_raw.get("differential", {}).get("value", 0))
            streak_val = int(stats_raw.get("streak", {}).get("value", 0))
            win_pct = float(stats_raw.get("winPercent", {}).get("value", 0))

            home_str = stats_raw.get("Home", {}).get("displayValue", "0-0")
            road_str = stats_raw.get("Road", {}).get("displayValue", "0-0")
            l10_str = stats_raw.get("Last Ten Games", {}).get("displayValue", "0-0")
            home_w, home_l = _parse_espn_record(home_str)
            road_w, road_l = _parse_espn_record(road_str)
            l10_w, l10_l = _parse_espn_record(l10_str)

            result[tid] = {
                "team_name": team_name,
                "team_abbr": nba_team["abbreviation"],
                "wins": wins,
                "losses": losses,
                "win_pct": win_pct,
                "ppg": ppg,
                "opp_ppg": opp_ppg,
                "diff": diff,
                "streak": streak_val,
                "home_record": home_str,
                "road_record": road_str,
                "last_10": l10_str,
                "home_wins": home_w, "home_losses": home_l,
                "road_wins": road_w, "road_losses": road_l,
                "l10_wins": l10_w, "l10_losses": l10_l,
            }

    logger.info("ESPN standings: loaded %d teams", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════
# Main research class
# ═══════════════════════════════════════════════════════════════════

class NBAResearch:
    """Fetches and caches NBA statistics. Tries NBA CDN first, ESPN fallback."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.season = self.config.NBA_SEASON
        self._data_source: str = "none"

        # Build proxy URL if configured
        self._proxy_url: str | None = None
        if self.config.PROXY_HOST and self.config.PROXY_USER:
            self._proxy_url = (
                f"http://{self.config.PROXY_USER}:{self.config.PROXY_PASS}"
                f"@{self.config.PROXY_HOST}:{self.config.PROXY_PORT}"
            )
            logger.info("Residential proxy configured: %s:%s",
                        self.config.PROXY_HOST, self.config.PROXY_PORT)

        # NBA CDN caches
        self._schedule_raw: dict | None = None
        self._schedule_ts: datetime | None = None
        self._cdn_failed_ts: datetime | None = None  # Track CDN failures
        self._team_games: dict[int, list[dict]] | None = None
        self._team_records: dict[int, dict] | None = None

        # ESPN caches
        self._espn_standings: dict[int, dict] | None = None
        self._espn_ts: datetime | None = None

        self._cache_ttl = timedelta(hours=2)
        self._cdn_fail_cooldown = timedelta(minutes=30)  # Don't retry CDN for 30 min after failure

    # ── NBA CDN data fetching ──────────────────────────────────────

    def _fetch_schedule(self) -> dict | None:
        """Download the full season schedule from NBA CDN, cached for 2 hours."""
        now = datetime.now(timezone.utc)
        if self._schedule_raw and self._schedule_ts and (now - self._schedule_ts) < self._cache_ttl:
            return self._schedule_raw

        # Don't keep retrying CDN if it recently failed
        if self._cdn_failed_ts and (now - self._cdn_failed_ts) < self._cdn_fail_cooldown:
            return self._schedule_raw  # None on first failure, stale data otherwise

        for attempt in range(2):
            try:
                client_kwargs = {"headers": _HTTP_HEADERS, "timeout": 30.0}
                if self._proxy_url:
                    client_kwargs["proxy"] = self._proxy_url
                resp = httpx.get(_SCHEDULE_URL, **client_kwargs)
                resp.raise_for_status()
                data = resp.json()
                self._schedule_raw = data
                self._schedule_ts = now
                self._cdn_failed_ts = None  # Clear failure flag on success
                self._team_games = None
                self._team_records = None
                self._data_source = "nba_cdn"
                logger.info("Fetched NBA schedule from CDN (%d game dates)",
                            len(data.get("leagueSchedule", {}).get("gameDates", [])))
                return data
            except Exception as e:
                logger.warning("CDN schedule attempt %d/2 failed: %s", attempt + 1, e)
                time.sleep(2)

        self._cdn_failed_ts = now
        logger.warning("NBA CDN unavailable — using ESPN only (will retry CDN in 30 min)")
        return None

    def _build_team_games(self) -> dict[int, list[dict]]:
        """Parse the CDN schedule into per-team game logs."""
        if self._team_games is not None:
            return self._team_games

        schedule = self._fetch_schedule()
        if not schedule:
            return {}

        team_games: dict[int, list[dict]] = defaultdict(list)
        dates = schedule.get("leagueSchedule", {}).get("gameDates", [])

        for dt in dates:
            for g in dt.get("games", []):
                if g.get("gameStatus") != 3:
                    continue

                ht = g["homeTeam"]
                at = g["awayTeam"]
                game_date = g.get("gameDateEst", g.get("gameDateUTC", ""))[:10]

                team_games[ht["teamId"]].append({
                    "date": game_date,
                    "is_home": True,
                    "pts": int(ht.get("score", 0)),
                    "opp_pts": int(at.get("score", 0)),
                    "opp_id": at["teamId"],
                    "opp_tricode": at.get("teamTricode", ""),
                    "won": int(ht.get("score", 0)) > int(at.get("score", 0)),
                })
                team_games[at["teamId"]].append({
                    "date": game_date,
                    "is_home": False,
                    "pts": int(at.get("score", 0)),
                    "opp_pts": int(ht.get("score", 0)),
                    "opp_id": ht["teamId"],
                    "opp_tricode": ht.get("teamTricode", ""),
                    "won": int(at.get("score", 0)) > int(ht.get("score", 0)),
                })

        for tid in team_games:
            team_games[tid].sort(key=lambda x: x["date"])

        self._team_games = dict(team_games)
        logger.info("Built game logs for %d teams from CDN", len(self._team_games))
        return self._team_games

    def _build_team_records(self) -> dict[int, dict]:
        """Build latest W-L records from the CDN schedule."""
        if self._team_records is not None:
            return self._team_records

        schedule = self._fetch_schedule()
        if not schedule:
            return {}

        records: dict[int, dict] = {}
        dates = schedule.get("leagueSchedule", {}).get("gameDates", [])

        for dt in reversed(dates):
            for g in dt.get("games", []):
                if g.get("gameStatus") != 3:
                    continue
                for side in ("homeTeam", "awayTeam"):
                    t = g[side]
                    tid = t["teamId"]
                    if tid not in records:
                        records[tid] = {
                            "TeamID": tid,
                            "TeamName": t.get("teamName", ""),
                            "TeamCity": t.get("teamCity", ""),
                            "TeamSlug": t.get("teamTricode", ""),
                            "WINS": int(t.get("wins", 0)),
                            "LOSSES": int(t.get("losses", 0)),
                        }
            if len(records) >= 30:
                break

        self._team_records = records
        return records

    # ── ESPN fallback ──────────────────────────────────────────────

    def _get_espn_standings(self) -> dict[int, dict]:
        """Get ESPN standings (cached for 2 hours)."""
        now = datetime.now(timezone.utc)
        if self._espn_standings and self._espn_ts and (now - self._espn_ts) < self._cache_ttl:
            return self._espn_standings

        standings = _fetch_espn_standings()
        if standings:
            self._espn_standings = standings
            self._espn_ts = now
            self._data_source = "espn"
        return self._espn_standings or {}

    # ── Public API ─────────────────────────────────────────────────

    def get_standings(self) -> list[dict]:
        """Return standings as a list of dicts."""
        records = self._build_team_records()
        if records:
            return list(records.values())

        # Fallback to ESPN
        espn = self._get_espn_standings()
        return [
            {"TeamID": tid, "TeamName": s["team_name"], "TeamSlug": s["team_abbr"],
             "WINS": s["wins"], "LOSSES": s["losses"]}
            for tid, s in espn.items()
        ]

    def get_team_stats(self, team_id: int) -> TeamStats | None:
        """Build TeamStats — uses CDN game data if available, ESPN fallback."""
        # Try CDN-based detailed stats first
        team_games = self._build_team_games()
        if team_games and team_id in team_games:
            return self._build_stats_from_cdn(team_id, team_games[team_id])

        # Fallback to ESPN standings
        espn = self._get_espn_standings()
        if team_id in espn:
            return self._build_stats_from_espn(team_id, espn[team_id])

        logger.warning("Team ID %d not found in any data source", team_id)
        return None

    def _build_stats_from_cdn(self, team_id: int, games: list[dict]) -> TeamStats:
        """Build TeamStats from CDN schedule game data."""
        records = self._build_team_records()
        record = records.get(team_id, {})

        n = len(games)
        total_pts = sum(g["pts"] for g in games)
        total_opp = sum(g["opp_pts"] for g in games)

        home_games = [g for g in games if g["is_home"]]
        road_games = [g for g in games if not g["is_home"]]
        home_w = sum(1 for g in home_games if g["won"])
        home_l = len(home_games) - home_w
        road_w = sum(1 for g in road_games if g["won"])
        road_l = len(road_games) - road_w

        last_10 = games[-10:]
        l10_w = sum(1 for g in last_10 if g["won"])
        l10_l = len(last_10) - l10_w

        # Streak
        streak_count = 0
        streak_type = "W" if games[-1]["won"] else "L"
        for g in reversed(games):
            if (g["won"] and streak_type == "W") or (not g["won"] and streak_type == "L"):
                streak_count += 1
            else:
                break

        wins = int(record.get("WINS", sum(1 for g in games if g["won"])))
        losses = int(record.get("LOSSES", sum(1 for g in games if not g["won"])))
        total = wins + losses
        win_pct = wins / total if total > 0 else 0.0

        team_info = next((t for t in _ALL_TEAMS if t["id"] == team_id), None)
        team_name = (
            f"{record.get('TeamCity', '')} {record.get('TeamName', '')}".strip()
            if record
            else (team_info["full_name"] if team_info else f"Team {team_id}")
        )
        team_abbr = record.get("TeamSlug", "").upper() or (
            team_info["abbreviation"] if team_info else ""
        )

        ppg = total_pts / n if n > 0 else 0.0
        opp_ppg = total_opp / n if n > 0 else 0.0

        ts = TeamStats(
            team_id=team_id,
            team_name=team_name,
            team_abbr=team_abbr,
            wins=wins, losses=losses, win_pct=win_pct,
            home_record=f"{home_w}-{home_l}",
            road_record=f"{road_w}-{road_l}",
            last_10=f"{l10_w}-{l10_l}",
            points_pg=round(ppg, 1),
            opp_points_pg=round(opp_ppg, 1),
            diff_points_pg=round(ppg - opp_ppg, 1),
            current_streak=f"{streak_type}{streak_count}",
            last_10_wins=l10_w, last_10_losses=l10_l,
            home_wins=home_w, home_losses=home_l,
            road_wins=road_w, road_losses=road_l,
        )

        ts.off_rating = round(ppg * (100 / 98), 1)
        ts.def_rating = round(opp_ppg * (100 / 98), 1)
        ts.net_rating = round(ts.off_rating - ts.def_rating, 1)
        ts.pace = 98.0
        return ts

    def _build_stats_from_espn(self, team_id: int, espn: dict) -> TeamStats:
        """Build TeamStats from ESPN standings data."""
        streak_val = espn.get("streak", 0)
        # ESPN streak is positive for wins, negative for losses
        streak_str = f"W{abs(streak_val)}" if streak_val >= 0 else f"L{abs(streak_val)}"

        ppg = espn.get("ppg", 0.0)
        opp_ppg = espn.get("opp_ppg", 0.0)

        ts = TeamStats(
            team_id=team_id,
            team_name=espn.get("team_name", ""),
            team_abbr=espn.get("team_abbr", ""),
            wins=espn.get("wins", 0),
            losses=espn.get("losses", 0),
            win_pct=espn.get("win_pct", 0.0),
            home_record=espn.get("home_record", "0-0"),
            road_record=espn.get("road_record", "0-0"),
            last_10=espn.get("last_10", "0-0"),
            points_pg=round(ppg, 1),
            opp_points_pg=round(opp_ppg, 1),
            diff_points_pg=round(espn.get("diff", 0.0), 1),
            current_streak=streak_str,
            last_10_wins=espn.get("l10_wins", 0),
            last_10_losses=espn.get("l10_losses", 0),
            home_wins=espn.get("home_wins", 0),
            home_losses=espn.get("home_losses", 0),
            road_wins=espn.get("road_wins", 0),
            road_losses=espn.get("road_losses", 0),
        )

        ts.off_rating = round(ppg * (100 / 98), 1) if ppg else 0.0
        ts.def_rating = round(opp_ppg * (100 / 98), 1) if opp_ppg else 0.0
        ts.net_rating = round(ts.off_rating - ts.def_rating, 1)
        ts.pace = 98.0
        return ts

    def get_team_game_log(self, team_id: int, last_n: int = 10) -> list[dict]:
        """Get recent game log for a team (CDN only — empty if ESPN-only mode)."""
        team_games = self._build_team_games()
        if not team_games:
            return []  # ESPN mode — no game-level data
        games = team_games.get(team_id, [])
        return games[-last_n:] if games else []

    def get_h2h(self, team_a_id: int, team_b_id: int) -> H2HRecord:
        """Get head-to-head record between two teams this season."""
        h2h = H2HRecord(team_a_id=team_a_id, team_b_id=team_b_id)

        team_games = self._build_team_games()
        if not team_games:
            return h2h  # ESPN mode — no H2H data available

        games_a = team_games.get(team_a_id, [])
        matchups = [g for g in games_a if g["opp_id"] == team_b_id]
        for g in matchups:
            if g["won"]:
                h2h.team_a_wins += 1
            else:
                h2h.team_b_wins += 1
            h2h.team_a_avg_pts += g["pts"]

        n = len(matchups)
        if n > 0:
            h2h.team_a_avg_pts /= n

        return h2h

    def get_rest_days(self, team_id: int, game_date: str | None = None) -> int:
        """Calculate rest days for a team before a given date."""
        team_games = self._build_team_games()
        if not team_games:
            return 1  # ESPN mode — assume 1 day rest
        games = team_games.get(team_id, [])
        if not games:
            return 1

        try:
            last_game_date_str = games[-1]["date"]
            last_game_dt = datetime.strptime(last_game_date_str[:10], "%Y-%m-%d")

            if game_date:
                target_dt = datetime.strptime(game_date[:10], "%Y-%m-%d")
            else:
                target_dt = datetime.now()

            rest = (target_dt - last_game_dt).days
            return max(0, rest)
        except Exception as e:
            logger.warning("Failed to calculate rest days for %d: %s", team_id, e)
            return 1

    def build_research(
        self,
        home_team_id: int,
        away_team_id: int,
        game_date: str | None = None,
    ) -> tuple[TeamStats | None, TeamStats | None, H2HRecord | None]:
        """Build full research package for a game matchup."""
        home_stats = self.get_team_stats(home_team_id)
        away_stats = self.get_team_stats(away_team_id)

        if home_stats and game_date:
            rest = self.get_rest_days(home_team_id, game_date)
            home_stats.rest_days = rest
            home_stats.is_b2b = rest == 0

        if away_stats and game_date:
            rest = self.get_rest_days(away_team_id, game_date)
            away_stats.rest_days = rest
            away_stats.is_b2b = rest == 0

        h2h = None
        if home_stats and away_stats:
            h2h = self.get_h2h(home_team_id, away_team_id)

        return home_stats, away_stats, h2h
