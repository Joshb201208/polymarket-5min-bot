"""Shared sports market detection.

Provides a single ``is_sports_market()`` function usable by both the
events scanner and every intelligence module to reject sports markets
before scoring or trading.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sports keywords — ANY match in slug / title / question → reject
# ---------------------------------------------------------------------------
_SPORTS_KEYWORDS: set[str] = {
    # North American leagues
    "nba", "nfl", "nhl", "mlb", "mls", "wnba", "cfl", "xfl",
    "ncaa", "cbb", "cfb", "cwbb",
    # International soccer leagues
    "serie a", "serie-a", "seriea",
    "la liga", "la-liga", "laliga",
    "bundesliga",
    "ligue 1", "ligue-1", "ligue1",
    "premier league", "premier-league", "premierleague",
    "champions league", "champions-league", "championsleague",
    "europa league", "europa-league",
    "chinese super league", "chinese-super-league", "csl",
    "j-league", "j league", "jleague",
    "k league", "k-league", "kleague",
    "a-league", "a league", "aleague",
    "eredivisie",
    "liga mx", "liga-mx",
    "primeira liga", "primeira-liga",
    "super lig", "super-lig", "süper lig", "süper-lig",
    "saudi pro league", "saudi-pro-league",
    "copa libertadores", "copa-libertadores",
    "afc champions", "concacaf",
    # Sports
    "soccer", "football match", "basketball", "baseball", "hockey",
    "tennis", "golf", "cricket", "rugby", "boxing",
    "mma", "ufc", "f1", "formula-1", "formula1", "formula 1", "nascar",
    # Esports
    "csgo", "dota", "valorant", "lol-", "league-of-legends",
    # International tournaments
    "world-cup", "worldcup", "world cup",
    "olympics", "olympic",
    "fifa", "atp", "wta", "pga", "lpga",
    # Specific events
    "super-bowl", "superbowl", "super bowl",
    "stanley-cup", "stanleycup", "stanley cup",
    "world-series", "worldseries", "world series",
    "march-madness", "marchmadness", "march madness",
    "grand-slam", "grand slam", "french-open", "french open",
    "roland-garros", "roland garros",
    # Betting / prop terms
    "moneyline", "spread-away", "spread-home",
    "total-over", "total-under",
    "-points-", "-rebounds-", "-assists-", "-touchdowns-",
    "-goals-", "-saves-", "-strikeouts-",
    "-1h-", "-1q-", "-halftime-",
    "o-u-", "over-under", "game-total", "points-total", "match-winner",
    "first-half", "second-half", "corners",
    "penalty", "red-card", "yellow-card",
    "red card", "yellow card",
    "wickets", "innings",
    "halftime", "goal",
}

# ---------------------------------------------------------------------------
# Sports tags — match against market/event ``tags`` field from Polymarket API
# ---------------------------------------------------------------------------
_SPORTS_TAGS: set[str] = {
    "sports", "soccer", "football", "basketball", "baseball", "hockey",
    "tennis", "mma", "ufc", "boxing", "cricket", "rugby",
    "f1", "formula 1", "nascar", "golf", "esports",
}

# ---------------------------------------------------------------------------
# Regex patterns that catch sports markets the keyword list misses
# ---------------------------------------------------------------------------
# "Will AS Roma win on 2026-04-05?"
_RE_WIN_ON_DATE = re.compile(
    r"Will .+ win on \d{4}-\d{2}-\d{2}", re.IGNORECASE
)
# "Will X beat Y" (sports match-up phrasing)
_RE_BEAT = re.compile(
    r"Will .+ beat .+", re.IGNORECASE
)
# "Will X win the game/match/series/cup/tournament/league"
_RE_WIN_MATCH = re.compile(
    r"Will .+ win (?:the |their )?(?:game|match|series|cup|tournament|league)",
    re.IGNORECASE,
)
# Standard Polymarket sports lines
_RE_SPORTS_LINES = re.compile(
    r"(?:"
    r"(?:vs\.?|versus)\s"
    r"|O/U\s+\d"
    r"|\bO/U\b"
    r"|\bover[/-]under\b"
    r"|(?:Games? Total|Total (?:Points|Goals|Runs))"
    r"|(?:NBA|NFL|NHL|MLB|UEFA|FIFA|EPL|LaLiga|Serie A|Bundesliga|Ligue 1)"
    r"|(?:Moneyline|Spread|Point Spread|Handicap)"
    r"|(?:first|second)\s+half"
    r"|(?:total|combined)\s+(?:points|goals|runs|score)"
    r"|(?:home|away)\s+(?:team|side)"
    r")",
    re.IGNORECASE,
)

_SPORTS_REGEXES = [_RE_WIN_ON_DATE, _RE_BEAT, _RE_WIN_MATCH, _RE_SPORTS_LINES]


# ---- public API -----------------------------------------------------------

def is_sports_market(market) -> bool:
    """Return True if *market* is a sports market that should be skipped.

    Accepts either:
    - An ``EventMarket`` dataclass (or any object with .question/.slug/.event_slug/.event_title)
    - A raw ``dict`` from the Polymarket Gamma API

    Detection layers (any match → True):
    1. ``category`` field == "sports"
    2. ``tags`` field contains a sports-related tag
    3. Keyword match in question / slug / event title / description
    4. Regex pattern match on question text
    """
    # --- extract text fields ------------------------------------------------
    if isinstance(market, dict):
        question = market.get("question", "") or ""
        slug = market.get("slug", "") or ""
        event_slug = market.get("event_slug", "") or ""
        event_title = market.get("event_title", "") or market.get("title", "") or ""
        description = market.get("description", "") or ""
        category = market.get("category", "") or ""
        tags_raw = market.get("tags", []) or []
    else:
        question = getattr(market, "question", "") or ""
        slug = getattr(market, "slug", "") or ""
        event_slug = getattr(market, "event_slug", "") or ""
        event_title = getattr(market, "event_title", "") or ""
        description = getattr(market, "description", "") or ""
        category = getattr(market, "category", "") or ""
        tags_raw = getattr(market, "tags", []) or []

    # --- 1. category field --------------------------------------------------
    if isinstance(category, str) and category.lower() == "sports":
        return True

    # --- 2. tags field ------------------------------------------------------
    if isinstance(tags_raw, str):
        # Could be a JSON string or comma-separated
        tags_lower = tags_raw.lower()
    elif isinstance(tags_raw, (list, tuple)):
        tags_lower = " ".join(str(t) for t in tags_raw).lower()
    else:
        tags_lower = str(tags_raw).lower()

    for stag in _SPORTS_TAGS:
        if stag in tags_lower:
            return True

    # --- 3. keyword match ---------------------------------------------------
    combined = f"{slug} {question} {event_slug} {event_title} {description}".lower()

    for keyword in _SPORTS_KEYWORDS:
        if keyword in combined:
            return True

    # --- 4. regex patterns on question --------------------------------------
    for pattern in _SPORTS_REGEXES:
        if pattern.search(question):
            return True

    return False
