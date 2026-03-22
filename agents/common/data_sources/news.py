"""
News data source — Google News RSS, DuckDuckGo search, Reddit search.
Time-aware sentiment with recent news weighted higher.
"""

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 10.0


def search_google_news(query: str, max_results: int = 10,
                       hours_back: int = 48) -> list[dict]:
    """Search Google News RSS for recent articles.

    Args:
        query: Search terms
        max_results: Max articles to return
        hours_back: Only return articles from last N hours
    """
    encoded = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    try:
        feed = feedparser.parse(url)
        results = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        for entry in feed.entries[:max_results * 2]:  # Fetch extra, filter by date
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass

            # Skip old articles if we can determine the date
            if published and published < cutoff:
                continue

            results.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "source": entry.get("source", {}).get("title", "Unknown"),
                "published": published.isoformat() if published else "",
                "summary": entry.get("summary", "")[:500],
            })

            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        logger.error(f"Google News search error: {e}")
        return []


def search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo for general web results."""
    try:
        resp = httpx.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1},
            timeout=TIMEOUT,
        )
        data = resp.json()
        results = []

        # Abstract text (instant answer)
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", "DuckDuckGo Answer"),
                "text": data["AbstractText"][:500],
                "source": data.get("AbstractSource", ""),
                "url": data.get("AbstractURL", ""),
            })

        # Related topics
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:100],
                    "text": topic.get("Text", "")[:500],
                    "url": topic.get("FirstURL", ""),
                    "source": "DuckDuckGo",
                })

        return results
    except Exception as e:
        logger.error(f"DuckDuckGo search error: {e}")
        return []


def search_reddit(query: str, max_results: int = 5,
                  subreddit: str = None) -> list[dict]:
    """Search Reddit for relevant discussions."""
    try:
        if subreddit:
            url = f"https://www.reddit.com/r/{subreddit}/search.json"
            params = {"q": query, "limit": max_results, "sort": "new", "restrict_sr": "on"}
        else:
            url = "https://www.reddit.com/search.json"
            params = {"q": query, "limit": max_results, "sort": "relevance", "t": "week"}

        resp = httpx.get(url, params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "PolymarketBot/2.0"})
        data = resp.json()

        results = []
        for post in data.get("data", {}).get("children", []):
            post_data = post.get("data", {})
            results.append({
                "title": post_data.get("title", ""),
                "subreddit": post_data.get("subreddit", ""),
                "score": post_data.get("score", 0),
                "url": f"https://reddit.com{post_data.get('permalink', '')}",
                "text": post_data.get("selftext", "")[:300],
                "created": datetime.fromtimestamp(
                    post_data.get("created_utc", 0), tz=timezone.utc
                ).isoformat(),
            })
        return results
    except Exception as e:
        logger.error(f"Reddit search error: {e}")
        return []


def extract_keywords(text: str) -> list[str]:
    """Extract key search terms from a market question."""
    # Remove common Polymarket phrasing
    stop_phrases = [
        "will", "the", "be", "by", "in", "on", "at", "to", "of", "a", "an",
        "this", "that", "who", "what", "when", "how", "is", "are", "was",
        "before", "after", "during", "yes", "no", "win", "over", "under",
    ]
    words = re.findall(r'\b[A-Za-z][a-z]{2,}\b', text)
    keywords = [w for w in words if w.lower() not in stop_phrases]

    # Also extract proper nouns (capitalized words)
    proper_nouns = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
    keywords = proper_nouns + keywords

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for k in keywords:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            unique.append(k)

    return unique[:10]


def get_comprehensive_news(query: str, max_articles: int = 15) -> dict:
    """Get news from multiple sources with time-weighted relevance.

    Returns combined results from Google News, DuckDuckGo, and Reddit
    with a simple sentiment signal.
    """
    google_results = search_google_news(query, max_results=max_articles, hours_back=48)
    time.sleep(0.3)
    ddg_results = search_duckduckgo(query, max_results=5)
    time.sleep(0.3)
    reddit_results = search_reddit(query, max_results=5)

    # Simple sentiment from titles
    positive_words = {"win", "wins", "lead", "surge", "gain", "rise", "positive",
                      "success", "approve", "pass", "victory", "strong", "ahead"}
    negative_words = {"lose", "loss", "fall", "drop", "fail", "reject", "decline",
                      "crisis", "concern", "risk", "weak", "behind", "injury"}

    pos_count = 0
    neg_count = 0
    for article in google_results:
        title_lower = article["title"].lower()
        for w in positive_words:
            if w in title_lower:
                pos_count += 1
        for w in negative_words:
            if w in title_lower:
                neg_count += 1

    total = pos_count + neg_count
    sentiment = 0
    if total > 0:
        sentiment = (pos_count - neg_count) / total  # -1 to 1

    return {
        "google_news": google_results,
        "duckduckgo": ddg_results,
        "reddit": reddit_results,
        "total_articles": len(google_results),
        "sentiment": sentiment,
        "sentiment_label": "positive" if sentiment > 0.2 else ("negative" if sentiment < -0.2 else "neutral"),
    }
