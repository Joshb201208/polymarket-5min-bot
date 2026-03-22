"""
Web research + heuristic analysis engine.

Uses ONLY free, keyless sources:
  - Google News RSS feeds
  - DuckDuckGo HTML search
  - Polymarket's own data (price momentum, volume trends)
  - Free sports APIs (balldontlie.io, football-data.org)

No paid API keys are required.
"""

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from agents.common.config import (
    BALLDONTLIE_API,
    DEFAULT_BET_SIZE,
    DUCKDUCKGO_URL,
    GOOGLE_NEWS_RSS,
    MAX_BET_SIZE,
    MIN_EDGE_THRESHOLD,
)

logger = logging.getLogger(__name__)

# ── Sentiment keywords ────────────────────────────────────────
POSITIVE_KEYWORDS = [
    "surge", "soar", "rally", "jump", "gain", "win", "victory", "success",
    "approve", "pass", "confirm", "boost", "rise", "record", "breakthrough",
    "agreement", "deal", "growth", "bullish", "strong", "beat", "exceed",
    "recover", "upgrade", "momentum", "support", "optimism", "favorable",
]
NEGATIVE_KEYWORDS = [
    "crash", "fall", "drop", "plunge", "lose", "defeat", "fail", "reject",
    "decline", "slump", "crisis", "risk", "warn", "cut", "bearish", "weak",
    "miss", "downgrade", "concern", "threat", "scandal", "collapse", "ban",
    "delay", "suspend", "cancel", "opposition", "unlikely", "pessimism",
]


@dataclass
class ResearchResult:
    """Aggregated research output for a single market."""
    fair_prob: float = 0.5
    edge: float = 0.0
    confidence: str = "LOW"
    direction: str = "YES"
    suggested_size: float = DEFAULT_BET_SIZE
    reasoning: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


# ── News fetching (free) ─────────────────────────────────────

def fetch_google_news(query: str, max_results: int = 10) -> list[dict]:
    """Fetch headlines from Google News RSS. Free, no key."""
    url = GOOGLE_NEWS_RSS.format(query=urllib.parse.quote_plus(query))
    try:
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:max_results]:
            results.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": entry.get("source", {}).get("title", "Google News"),
            })
        return results
    except Exception as exc:
        logger.warning("Google News fetch failed for '%s': %s", query, exc)
        return []


def fetch_duckduckgo(query: str, max_results: int = 8) -> list[dict]:
    """Fetch search results from DuckDuckGo HTML. Free, no key."""
    try:
        resp = httpx.post(
            DUCKDUCKGO_URL,
            data={"q": query, "b": ""},
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot)"},
            timeout=15,
            follow_redirects=True,
        )
        results = []
        # Extract result titles from HTML (simple regex extraction)
        for match in re.finditer(
            r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', resp.text, re.DOTALL
        ):
            title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if title:
                results.append({"title": title, "source": "DuckDuckGo"})
                if len(results) >= max_results:
                    break
        return results
    except Exception as exc:
        logger.warning("DuckDuckGo fetch failed for '%s': %s", query, exc)
        return []


# ── Sentiment analysis (keyword-based) ───────────────────────

def analyze_sentiment(texts: list[str]) -> tuple[float, list[str]]:
    """Simple keyword sentiment: returns (score in -1..+1, reasoning notes)."""
    pos_count = 0
    neg_count = 0
    notes = []

    combined = " ".join(texts).lower()
    for kw in POSITIVE_KEYWORDS:
        count = combined.count(kw)
        pos_count += count
    for kw in NEGATIVE_KEYWORDS:
        count = combined.count(kw)
        neg_count += count

    total = pos_count + neg_count
    if total == 0:
        return 0.0, ["No strong sentiment signal in news"]

    score = (pos_count - neg_count) / total
    if score > 0.3:
        notes.append(f"News sentiment strongly positive ({pos_count} pos vs {neg_count} neg mentions)")
    elif score > 0.1:
        notes.append(f"News sentiment mildly positive ({pos_count} pos vs {neg_count} neg)")
    elif score < -0.3:
        notes.append(f"News sentiment strongly negative ({neg_count} neg vs {pos_count} pos mentions)")
    elif score < -0.1:
        notes.append(f"News sentiment mildly negative ({neg_count} neg vs {pos_count} pos)")
    else:
        notes.append("News sentiment neutral/mixed")

    return score, notes


# ── Market data analysis ─────────────────────────────────────

def analyze_market_signals(market: dict) -> tuple[float, list[str]]:
    """Analyze Polymarket's own data for pricing signals.

    Returns (adjustment in -0.15..+0.15, reasoning notes).
    """
    adjustment = 0.0
    notes = []

    # Price momentum
    one_day = _safe_float(market.get("oneDayPriceChange"))
    one_week = _safe_float(market.get("oneWeekPriceChange"))

    if one_day is not None and abs(one_day) > 0.05:
        # Recency bias detection: rapid moves may overshoot
        if one_day > 0.10:
            adjustment -= 0.03  # likely overshot up
            notes.append(f"1-day price surged {one_day*100:+.1f}% — possible overshoot")
        elif one_day < -0.10:
            adjustment += 0.03  # likely overshot down
            notes.append(f"1-day price dropped {one_day*100:+.1f}% — possible overshoot")
        else:
            direction = "up" if one_day > 0 else "down"
            notes.append(f"1-day price moved {one_day*100:+.1f}% ({direction})")

    if one_week is not None and abs(one_week) > 0.08:
        if one_week > 0.15:
            adjustment -= 0.02
            notes.append(f"1-week price surged {one_week*100:+.1f}% — momentum may be overextended")
        elif one_week < -0.15:
            adjustment += 0.02
            notes.append(f"1-week price dropped {one_week*100:+.1f}% — may be oversold")

    # Volume analysis
    vol_24h = _safe_float(market.get("volume24hr"))
    vol_1w = _safe_float(market.get("volume1wk"))
    if vol_24h and vol_1w and vol_1w > 0:
        daily_avg = vol_1w / 7
        if daily_avg > 0:
            volume_ratio = vol_24h / daily_avg
            if volume_ratio > 3:
                notes.append(f"Volume surge: 24h volume {volume_ratio:.1f}x weekly average — new information likely")
                adjustment += 0.02  # high volume = meaningful move
            elif volume_ratio < 0.3:
                notes.append("Very low volume — price may be stale/unreliable")

    # Crowd wisdom: high-volume markets are usually better priced
    if vol_24h and vol_24h > 10000:
        adjustment *= 0.5  # reduce our edge estimate for well-traded markets
        notes.append("High-volume market — crowd pricing likely efficient")

    return adjustment, notes


def analyze_time_to_resolution(market: dict) -> tuple[float, list[str]]:
    """Analyze time decay and resolution date effects.

    Returns (adjustment, notes).
    """
    notes = []
    adjustment = 0.0

    end_date_str = market.get("endDate") or market.get("end_date_iso")
    if not end_date_str:
        notes.append("No resolution date — skipping time analysis")
        return 0.0, notes

    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0, ["Could not parse resolution date"]

    now = datetime.now(timezone.utc)
    days_to_resolution = (end_date - now).total_seconds() / 86400

    if days_to_resolution < 0:
        notes.append("Market past resolution date")
        return 0.0, notes
    elif days_to_resolution < 1:
        notes.append("Resolves within 24 hours — last-minute edge possible")
        adjustment += 0.02
    elif days_to_resolution < 7:
        notes.append(f"Resolves in {days_to_resolution:.0f} days — near-term catalyst")
    elif days_to_resolution > 180:
        notes.append(f"Resolves in {days_to_resolution:.0f} days — long-dated, high uncertainty")
        adjustment -= 0.01  # discount edge on very long-dated markets

    return adjustment, notes


# ── Core research functions ───────────────────────────────────

def research_event_market(market: dict, event: dict | None = None) -> ResearchResult:
    """Full research pipeline for a general event market."""
    result = ResearchResult()
    question = market.get("question") or market.get("title", "Unknown market")
    description = market.get("description", "")

    # 1. Get current market price
    from agents.common.polymarket_client import get_market_price
    market_prob = get_market_price(market)
    if market_prob is None:
        market_prob = 0.5
        result.reasoning.append("Could not get market price — defaulting to 50%")

    # 2. News research
    search_query = _extract_search_query(question)
    news = fetch_google_news(search_query)
    ddg_results = fetch_duckduckgo(search_query)

    all_titles = [n["title"] for n in news] + [r["title"] for r in ddg_results]
    result.sources = list(set(
        n.get("source", "News") for n in news
    ))[:5]
    if ddg_results:
        result.sources.append("DuckDuckGo")

    # 3. Sentiment from news
    sentiment_score, sentiment_notes = analyze_sentiment(all_titles)
    result.reasoning.extend(sentiment_notes)

    # 4. Market signal analysis
    signal_adj, signal_notes = analyze_market_signals(market)
    result.reasoning.extend(signal_notes)

    # 5. Time analysis
    time_adj, time_notes = analyze_time_to_resolution(market)
    result.reasoning.extend(time_notes)

    # 6. Calculate fair probability
    # Start from market price (crowd wisdom baseline)
    fair_prob = market_prob

    # Apply sentiment adjustment (max +/- 15%)
    sentiment_adj = sentiment_score * 0.15
    fair_prob += sentiment_adj

    # Apply market signal adjustments
    fair_prob += signal_adj

    # Apply time adjustments
    fair_prob += time_adj

    # Clamp to valid range
    fair_prob = max(0.02, min(0.98, fair_prob))

    # 7. Calculate edge
    edge = fair_prob - market_prob
    abs_edge = abs(edge)

    result.fair_prob = round(fair_prob, 4)
    result.edge = round(edge, 4)

    # Determine direction
    if edge > 0:
        result.direction = "YES"
    else:
        result.direction = "NO"

    # Confidence level
    if abs_edge >= 0.12 and len(all_titles) >= 5:
        result.confidence = "HIGH"
    elif abs_edge >= 0.08 or len(all_titles) >= 3:
        result.confidence = "MEDIUM"
    else:
        result.confidence = "LOW"

    # Suggested size based on confidence
    if result.confidence == "HIGH":
        result.suggested_size = min(MAX_BET_SIZE, DEFAULT_BET_SIZE * 2)
    elif result.confidence == "MEDIUM":
        result.suggested_size = DEFAULT_BET_SIZE
    else:
        result.suggested_size = DEFAULT_BET_SIZE * 0.5

    return result


def research_soccer_market(market: dict, event: dict | None = None) -> ResearchResult:
    """Research pipeline for soccer/football markets."""
    result = ResearchResult()
    question = market.get("question") or market.get("title", "Unknown market")

    from agents.common.polymarket_client import get_market_price
    market_prob = get_market_price(market) or 0.5

    # Extract team/tournament names from question
    search_query = _extract_search_query(question)

    # 1. News + injury reports
    news = fetch_google_news(f"{search_query} soccer football")
    injury_news = fetch_google_news(f"{search_query} injury lineup")

    all_titles = [n["title"] for n in news] + [n["title"] for n in injury_news]
    result.sources = list(set(n.get("source", "News") for n in news))[:5]

    # 2. Try football-data.org (free tier, limited)
    football_notes = _fetch_football_data(search_query)
    result.reasoning.extend(football_notes)

    # 3. Sentiment from news
    sentiment_score, sentiment_notes = analyze_sentiment(all_titles)
    result.reasoning.extend(sentiment_notes)

    # 4. Injury impact
    injury_titles = [n["title"] for n in injury_news]
    if injury_titles:
        injury_mentions = sum(1 for t in injury_titles if any(
            kw in t.lower() for kw in ["injury", "injured", "out", "miss", "doubt", "ruled out"]
        ))
        if injury_mentions > 0:
            result.reasoning.append(f"Found {injury_mentions} injury-related news items")

    # 5. Market signals
    signal_adj, signal_notes = analyze_market_signals(market)
    result.reasoning.extend(signal_notes)

    # 6. Time analysis
    time_adj, time_notes = analyze_time_to_resolution(market)
    result.reasoning.extend(time_notes)

    # 7. Calculate fair probability
    fair_prob = market_prob
    fair_prob += sentiment_score * 0.12  # slightly less weight for sports
    fair_prob += signal_adj
    fair_prob += time_adj
    fair_prob = max(0.02, min(0.98, fair_prob))

    edge = fair_prob - market_prob
    abs_edge = abs(edge)

    result.fair_prob = round(fair_prob, 4)
    result.edge = round(edge, 4)
    result.direction = "YES" if edge > 0 else "NO"

    if abs_edge >= 0.10 and len(all_titles) >= 4:
        result.confidence = "HIGH"
    elif abs_edge >= 0.07 or len(all_titles) >= 2:
        result.confidence = "MEDIUM"
    else:
        result.confidence = "LOW"

    result.suggested_size = (
        min(MAX_BET_SIZE, DEFAULT_BET_SIZE * 2) if result.confidence == "HIGH"
        else DEFAULT_BET_SIZE if result.confidence == "MEDIUM"
        else DEFAULT_BET_SIZE * 0.5
    )

    return result


def research_nba_market(market: dict, event: dict | None = None) -> ResearchResult:
    """Research pipeline for NBA markets."""
    result = ResearchResult()
    question = market.get("question") or market.get("title", "Unknown market")

    from agents.common.polymarket_client import get_market_price
    market_prob = get_market_price(market) or 0.5

    search_query = _extract_search_query(question)

    # 1. News + injury reports
    news = fetch_google_news(f"{search_query} NBA basketball")
    injury_news = fetch_google_news(f"{search_query} NBA injury")

    all_titles = [n["title"] for n in news] + [n["title"] for n in injury_news]
    result.sources = list(set(n.get("source", "News") for n in news))[:5]

    # 2. Try balldontlie API for team/player stats
    nba_notes = _fetch_nba_data(search_query)
    result.reasoning.extend(nba_notes)

    # 3. Sentiment
    sentiment_score, sentiment_notes = analyze_sentiment(all_titles)
    result.reasoning.extend(sentiment_notes)

    # 4. Injury analysis
    injury_titles = [n["title"] for n in injury_news]
    if injury_titles:
        injury_mentions = sum(1 for t in injury_titles if any(
            kw in t.lower() for kw in ["injury", "injured", "out", "miss", "questionable", "day-to-day"]
        ))
        if injury_mentions > 0:
            result.reasoning.append(f"Found {injury_mentions} injury-related news items")

    # 5. Market signals
    signal_adj, signal_notes = analyze_market_signals(market)
    result.reasoning.extend(signal_notes)

    # 6. Time analysis
    time_adj, time_notes = analyze_time_to_resolution(market)
    result.reasoning.extend(time_notes)

    # 7. Fair probability
    fair_prob = market_prob
    fair_prob += sentiment_score * 0.12
    fair_prob += signal_adj
    fair_prob += time_adj
    fair_prob = max(0.02, min(0.98, fair_prob))

    edge = fair_prob - market_prob
    abs_edge = abs(edge)

    result.fair_prob = round(fair_prob, 4)
    result.edge = round(edge, 4)
    result.direction = "YES" if edge > 0 else "NO"

    if abs_edge >= 0.10 and len(all_titles) >= 4:
        result.confidence = "HIGH"
    elif abs_edge >= 0.07 or len(all_titles) >= 2:
        result.confidence = "MEDIUM"
    else:
        result.confidence = "LOW"

    result.suggested_size = (
        min(MAX_BET_SIZE, DEFAULT_BET_SIZE * 2) if result.confidence == "HIGH"
        else DEFAULT_BET_SIZE if result.confidence == "MEDIUM"
        else DEFAULT_BET_SIZE * 0.5
    )

    return result


# ── Sports data helpers (free APIs) ──────────────────────────

def _fetch_football_data(query: str) -> list[str]:
    """Try to get football info from free sources. Gracefully degrades."""
    notes = []
    try:
        # football-data.org free tier: competitions, standings
        resp = httpx.get(
            "https://api.football-data.org/v4/competitions",
            headers={"X-Auth-Token": ""},  # empty = free anonymous tier
            timeout=10,
        )
        if resp.status_code == 200:
            notes.append("Football data API accessible")
    except Exception:
        notes.append("Football data API unavailable — using news analysis only")
    return notes


def _fetch_nba_data(query: str) -> list[str]:
    """Try to fetch NBA data from balldontlie.io. Gracefully degrades."""
    notes = []

    # Extract potential team name
    nba_teams = [
        "Lakers", "Celtics", "Warriors", "Bucks", "Nuggets", "Heat", "Suns",
        "76ers", "Sixers", "Knicks", "Nets", "Clippers", "Mavericks", "Mavs",
        "Thunder", "Timberwolves", "Cavaliers", "Cavs", "Pacers", "Hawks",
        "Bulls", "Raptors", "Kings", "Pelicans", "Grizzlies", "Spurs",
        "Trail Blazers", "Blazers", "Rockets", "Pistons", "Hornets",
        "Magic", "Wizards", "Jazz",
    ]
    found_team = None
    query_lower = query.lower()
    for team in nba_teams:
        if team.lower() in query_lower:
            found_team = team
            break

    if not found_team:
        notes.append("No specific NBA team identified — using news analysis")
        return notes

    try:
        resp = httpx.get(
            f"{BALLDONTLIE_API}/teams",
            params={"search": found_team},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            teams = data.get("data", [])
            if teams:
                team_info = teams[0]
                notes.append(
                    f"Team: {team_info.get('full_name', found_team)} "
                    f"({team_info.get('conference', '?')} Conference, "
                    f"{team_info.get('division', '?')} Division)"
                )
        else:
            notes.append(f"NBA API returned {resp.status_code} — using news only")
    except Exception as exc:
        logger.debug("balldontlie API error: %s", exc)
        notes.append("NBA data API unavailable — using news analysis only")

    return notes


# ── Utility ──────────────────────────────────────────────────

def _extract_search_query(question: str) -> str:
    """Extract a concise search query from a market question."""
    # Remove common question prefixes
    q = question
    for prefix in ["Will ", "Will the ", "Is ", "Does ", "Can ", "Has ", "Have "]:
        if q.startswith(prefix):
            q = q[len(prefix):]
            break
    # Remove trailing question mark and common suffixes
    q = q.rstrip("?").strip()
    # Truncate very long questions
    if len(q) > 100:
        q = q[:100]
    return q


def _safe_float(val: Any) -> float | None:
    """Safely convert to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
