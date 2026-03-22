"""Web search + heuristic analysis engine — uses only free APIs (no paid keys)."""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import feedparser
import httpx

from agents.common.config import GOOGLE_NEWS_RSS_BASE, DUCKDUCKGO_URL

logger = logging.getLogger(__name__)

_http = httpx.Client(timeout=20, follow_redirects=True)

# ── Sentiment keywords ──────────────────────────────────────
POSITIVE_KEYWORDS = {
    "surge", "surges", "rally", "rallies", "soar", "soars", "jump", "jumps",
    "gain", "gains", "win", "wins", "victory", "success", "approve", "approved",
    "breakthrough", "record", "high", "beat", "beats", "strong", "bullish",
    "optimistic", "confirm", "confirmed", "agree", "agreement", "pass", "passed",
    "boost", "boosted", "accelerate", "momentum", "upgrade",
}
NEGATIVE_KEYWORDS = {
    "crash", "crashes", "plunge", "plunges", "drop", "drops", "fall", "falls",
    "loss", "losses", "lose", "defeat", "fail", "fails", "failure", "reject",
    "rejected", "block", "blocked", "weak", "bearish", "pessimistic", "deny",
    "denied", "collapse", "decline", "risk", "warn", "warning", "downgrade",
    "suspend", "suspended", "cancel", "cancelled", "delay", "delayed",
}


# ── News search ──────────────────────────────────────────────

def search_google_news(query: str, max_results: int = 10) -> list[dict[str, str]]:
    """Search Google News RSS for articles related to a query."""
    url = f"{GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = _http.get(url)
        feed = feedparser.parse(resp.text)
        results = []
        for entry in feed.entries[:max_results]:
            results.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": entry.get("source", {}).get("title", ""),
            })
        return results
    except Exception:
        logger.exception("Google News RSS search failed for: %s", query)
        return []


def search_duckduckgo(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """Search DuckDuckGo HTML endpoint for results (no API key)."""
    try:
        resp = _http.post(
            DUCKDUCKGO_URL,
            data={"q": query, "b": ""},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        results = []
        # Simple regex extraction of result titles and snippets
        blocks = re.findall(
            r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
            r'class="result__snippet"[^>]*>(.*?)</(?:a|span|td)',
            resp.text,
            re.DOTALL,
        )
        for link, title, snippet in blocks[:max_results]:
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            clean_snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            results.append({
                "title": clean_title,
                "snippet": clean_snippet,
                "link": link,
            })
        return results
    except Exception:
        logger.exception("DuckDuckGo search failed for: %s", query)
        return []


# ── Sentiment analysis ───────────────────────────────────────

def analyze_sentiment(texts: list[str]) -> tuple[float, list[str]]:
    """Simple keyword-based sentiment scorer. Returns (score -1..+1, reasoning_notes)."""
    pos_count = 0
    neg_count = 0
    notes: list[str] = []

    for text in texts:
        words = set(re.findall(r"\w+", text.lower()))
        pos_hits = words & POSITIVE_KEYWORDS
        neg_hits = words & NEGATIVE_KEYWORDS
        pos_count += len(pos_hits)
        neg_count += len(neg_hits)

    total = pos_count + neg_count
    if total == 0:
        return 0.0, ["No strong sentiment signals in news"]

    score = (pos_count - neg_count) / total  # -1 to +1
    if score > 0.3:
        notes.append(f"News sentiment is positive ({pos_count} positive vs {neg_count} negative signals)")
    elif score < -0.3:
        notes.append(f"News sentiment is negative ({neg_count} negative vs {pos_count} positive signals)")
    else:
        notes.append(f"News sentiment is mixed ({pos_count} positive, {neg_count} negative signals)")

    return score, notes


# ── Market data analysis ─────────────────────────────────────

def analyze_market_signals(market: dict) -> tuple[float, list[str]]:
    """Analyze price momentum, volume, and timing from Gamma API market data.

    Returns (adjustment -0.15..+0.15, reasoning_notes).
    """
    adjustment = 0.0
    notes: list[str] = []

    # Price momentum
    one_day_change = _safe_float(market.get("oneDayPriceChange"))
    one_week_change = _safe_float(market.get("oneWeekPriceChange"))

    if one_day_change is not None:
        if abs(one_day_change) > 0.10:
            # Big 1-day move — possible overshoot (mean reversion bias)
            direction = "up" if one_day_change > 0 else "down"
            adjustment -= one_day_change * 0.3  # partial reversion
            notes.append(
                f"Large 1-day price move ({one_day_change:+.1%} {direction}) — "
                f"possible overshoot, adjusting against momentum"
            )
        elif abs(one_day_change) > 0.03:
            notes.append(f"Moderate 1-day price change: {one_day_change:+.1%}")

    if one_week_change is not None and abs(one_week_change) > 0.15:
        notes.append(f"Significant 1-week trend: {one_week_change:+.1%}")

    # Volume surge detection
    vol_24h = _safe_float(market.get("volume24hr", market.get("volume24Hr")))
    vol_total = _safe_float(market.get("volume"))
    if vol_24h and vol_total and vol_total > 0:
        # Rough daily average: total volume / max(days_active, 7)
        end_date_str = market.get("endDate", "")
        created_str = market.get("createdAt", "")
        days_active = _days_between(created_str, datetime.now(timezone.utc).isoformat())
        if days_active and days_active > 0:
            avg_daily = vol_total / max(days_active, 7)
            if avg_daily > 0:
                surge_ratio = vol_24h / avg_daily
                if surge_ratio > 3:
                    notes.append(
                        f"Volume surge (+{surge_ratio:.0f}x vs avg) suggests new information"
                    )
                    adjustment += 0.02  # slight positive bias (smart money entering)

    # Time to resolution
    end_date_str = market.get("endDate", "")
    if end_date_str:
        days_left = _days_between(datetime.now(timezone.utc).isoformat(), end_date_str)
        if days_left is not None:
            if days_left < 1:
                notes.append("⚠️ Resolves within 24 hours — high uncertainty")
            elif days_left < 7:
                notes.append(f"Resolves in {days_left:.0f} days — near-term")
            elif days_left > 180:
                notes.append(f"Long-dated market ({days_left:.0f} days) — higher uncertainty discount")
                adjustment -= 0.02

    return max(-0.15, min(0.15, adjustment)), notes


def calculate_fair_probability(
    market_prob: float,
    sentiment_score: float,
    market_adjustment: float,
    volume_usd: float,
) -> float:
    """Combine signals into a fair probability estimate.

    High-volume markets are considered more efficient (crowd wisdom),
    so we dampen our adjustment for them.
    """
    # Crowd wisdom dampening: high volume → trust market price more
    if volume_usd > 100_000:
        efficiency_factor = 0.3  # very liquid — our edge is small
    elif volume_usd > 10_000:
        efficiency_factor = 0.6
    else:
        efficiency_factor = 1.0  # illiquid — more mispricing likely

    # Our total adjustment to the market price
    total_adj = (sentiment_score * 0.08 + market_adjustment) * efficiency_factor

    fair = market_prob + total_adj
    return max(0.01, min(0.99, fair))


def determine_confidence(edge: float, notes_count: int, volume: float) -> str:
    """Rate confidence level based on edge magnitude and data quality."""
    if abs(edge) > 0.15 and notes_count >= 3:
        return "HIGH"
    if abs(edge) > 0.08 or notes_count >= 2:
        return "MEDIUM"
    return "LOW"


def suggested_bet_size(edge: float, confidence: str) -> float:
    """Kelly-inspired paper bet sizing: more edge + confidence = larger size."""
    base = 10.0
    edge_mult = min(abs(edge) / 0.05, 3.0)  # cap at 3x
    conf_mult = {"HIGH": 2.5, "MEDIUM": 1.5, "LOW": 1.0}.get(confidence, 1.0)
    return round(base * edge_mult * conf_mult, 0)


# ── Sport-specific research ──────────────────────────────────

def research_soccer(query: str) -> dict[str, Any]:
    """Search for soccer/football context. Returns news articles + sentiment."""
    search_terms = [
        query,
        f"{query} Premier League",
        f"{query} Champions League",
        f"{query} injury",
    ]
    all_articles: list[dict[str, str]] = []
    for term in search_terms:
        all_articles.extend(search_google_news(term, max_results=5))
        time.sleep(0.3)

    texts = [a.get("title", "") for a in all_articles]
    sentiment, notes = analyze_sentiment(texts)

    return {
        "articles": all_articles[:15],
        "sentiment": sentiment,
        "notes": notes,
        "sources": list({a.get("source", "") for a in all_articles if a.get("source")}),
    }


def research_nba(query: str) -> dict[str, Any]:
    """Search for NBA context. Returns news articles + sentiment."""
    search_terms = [
        query,
        f"{query} NBA",
        f"{query} injury report",
    ]
    all_articles: list[dict[str, str]] = []
    for term in search_terms:
        all_articles.extend(search_google_news(term, max_results=5))
        time.sleep(0.3)

    texts = [a.get("title", "") for a in all_articles]
    sentiment, notes = analyze_sentiment(texts)

    return {
        "articles": all_articles[:15],
        "sentiment": sentiment,
        "notes": notes,
        "sources": list({a.get("source", "") for a in all_articles if a.get("source")}),
    }


def research_event(query: str) -> dict[str, Any]:
    """General event/news research. Combines Google News + DuckDuckGo."""
    google_articles = search_google_news(query, max_results=8)
    time.sleep(0.3)
    ddg_results = search_duckduckgo(query, max_results=5)

    texts = (
        [a.get("title", "") for a in google_articles]
        + [r.get("title", "") + " " + r.get("snippet", "") for r in ddg_results]
    )
    sentiment, notes = analyze_sentiment(texts)

    sources = list({a.get("source", "") for a in google_articles if a.get("source")})

    return {
        "articles": google_articles,
        "ddg_results": ddg_results,
        "sentiment": sentiment,
        "notes": notes,
        "sources": sources,
    }


# ── Helpers ──────────────────────────────────────────────────

def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _days_between(start_iso: str, end_iso: str) -> float | None:
    """Return fractional days between two ISO date strings."""
    try:
        fmt_patterns = ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"]
        s = e = None
        for fmt in fmt_patterns:
            try:
                s = datetime.strptime(start_iso[:26].rstrip("Z") + "Z", fmt.replace("%z", "Z"))
                break
            except ValueError:
                continue
        for fmt in fmt_patterns:
            try:
                e = datetime.strptime(end_iso[:26].rstrip("Z") + "Z", fmt.replace("%z", "Z"))
                break
            except ValueError:
                continue
        if s and e:
            return (e - s).total_seconds() / 86400
    except Exception:
        pass
    return None
