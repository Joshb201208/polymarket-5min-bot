"""
Agent 1 Scanner — Find HIGH QUALITY event markets on Polymarket.
Targets: politics, geopolitics, crypto, major world events.
Explicitly EXCLUDES all sports (handled by agents 2 and 3).
Uses a WHITELIST approach: market must match at least one allowed topic.
"""

import logging
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import polymarket_api as pm

logger = logging.getLogger(__name__)

# Tags to search — politics, crypto, world events only
EVENT_TAGS = [
    "politics", "crypto", "science",
    "business", "world",
]

# ── MASSIVE EXCLUSION LIST ──────────────────────────────────────
# If ANY of these appear in the question, slug, or event title → SKIP
EXCLUDED_KEYWORDS = [
    # NBA / Basketball
    "nba", "basketball", "lakers", "celtics", "warriors", "bucks", "suns",
    "nuggets", "heat", "76ers", "sixers", "knicks", "nets", "clippers",
    "mavericks", "thunder", "timberwolves", "cavaliers", "pacers", "magic",
    "spurs", "rockets", "grizzlies", "pelicans", "kings", "pistons",
    "hornets", "wizards", "blazers", "jazz", "bulls", "hawks", "raptors",
    # College
    "ncaa", "college", "march madness", "crimson", "bruins", "huskies",
    "red raiders", "fighting illini", "rams vs", "bulldogs", "wildcats",
    "seminoles", "wolverines", "buckeyes", "tar heels", "blue devils",
    "cardinal", "hoosiers", "boilermakers", "jayhawks", "longhorns",
    "tigers vs", "bears vs", "cbb-", "sweet 16", "elite eight", "final four",
    # Soccer/Football
    "premier league", "champions league", "la liga", "serie a", "bundesliga",
    "ligue 1", "world cup", "europa league", "arsenal", "liverpool",
    "manchester", "chelsea", "tottenham", "barcelona", "real madrid",
    "bayern", "psg", "juventus", "inter milan", "ac milan", "dortmund",
    "copa america", "euro 2026", "mls", "fifa",
    # Other Sports
    "nfl", "mlb", "nhl", "ufc", "mma", "boxing", "tennis", "golf", "f1",
    "formula 1", "cricket", "rugby", "afl",
    # Esports / Gaming
    "lol", "league of legends", "dota", "csgo", "cs2", "valorant",
    "overwatch", "fortnite", "esports", "e-sports", "gaming",
    "g2 esports", "bilibili", "fnatic", "t1", "gen.g", "cloud9",
    # Sports terms
    "spread", "o/u", "moneyline", "over/under", "handicap",
    "vs.", "1h moneyline", "total-", "spread-",
    # General sports
    "game", "match", "playoff", "playoffs", "championship",
    "conference finals", "mvp", "rookie of the year",
    # Junk
    "5-minute", "1-minute", "next 5", "next 1",
    "meme", "tiktok", "youtube", "subscriber",
]

# ── SLUG PREFIX EXCLUSIONS ──────────────────────────────────────
EXCLUDED_SLUG_PREFIXES = [
    "nba-", "cbb-", "lol-", "csgo-", "dota-", "val-",
    "epl-", "ucl-", "laliga-", "seriea-", "bund-",
    "nfl-", "mlb-", "nhl-", "ufc-", "mma-",
]

# ── WHITELIST — market MUST match at least one of these ─────────
WHITELIST_KEYWORDS = [
    "president", "election", "congress", "senate", "trump", "biden",
    "fed", "interest rate", "inflation", "bitcoin", "ethereum", "crypto",
    "war", "ceasefire", "treaty", "sanction", "tariff", "impeach",
    "supreme court", "legislation", "bill", "act", "regulation",
    "elon musk", "spacex", "tesla", "ipo", "stock", "gdp", "recession",
    "ai", "openai", "border", "immigration", "nato", "china", "russia",
    "iran", "north korea", "ukraine", "israel", "palestine",
    "climate", "carbon", "merger", "acquisition",
]


def _is_excluded(question: str, slug: str, event_title: str) -> bool:
    """Check if market should be excluded based on keywords and slug."""
    # Check slug prefixes first (fast)
    if any(slug.startswith(prefix) for prefix in EXCLUDED_SLUG_PREFIXES):
        return True

    # Check all text fields against exclusion keywords (case-insensitive)
    combined = f"{question} {slug} {event_title}".lower()
    for keyword in EXCLUDED_KEYWORDS:
        if keyword in combined:
            return True

    return False


def _is_whitelisted(question: str) -> bool:
    """Check if market matches at least one whitelist keyword."""
    question_lower = question.lower()
    for keyword in WHITELIST_KEYWORDS:
        if keyword in question_lower:
            return True
    return False


def scan_event_markets() -> list[dict]:
    """Scan Polymarket for high-quality event markets.

    Filters:
    - Resolves within MAX_RESOLUTION_DAYS
    - Not resolving in less than MIN_RESOLUTION_HOURS
    - Market has not already ended
    - Minimum liquidity ($10k)
    - Price between 5c-95c (hard reject <=3c or >=97c)
    - No sports/esports/gaming markets (exclusion list + slug check)
    - Must match whitelist topic
    - Must be accepting orders
    """
    all_markets = []
    now = datetime.now(timezone.utc)

    for tag in EVENT_TAGS:
        try:
            events = pm.search_events(tag=tag, limit=30)
            for event in events:
                event_title = event.get("title", "") or ""
                markets = event.get("markets", [])
                if not markets:
                    continue

                for market_data in markets:
                    # Skip markets not accepting orders
                    if not market_data.get("acceptingOrders", False):
                        continue

                    market = pm.parse_market_data(market_data)
                    question = market.get("question", "")
                    slug = market.get("slug", "") or market_data.get("slug", "")

                    # EXCLUSION CHECK — block all sports/esports/gaming
                    if _is_excluded(question, slug, event_title):
                        continue

                    # WHITELIST CHECK — must match an allowed topic
                    if not _is_whitelisted(question):
                        continue

                    if _passes_filters(market, now):
                        market["url"] = pm.get_market_url(market_data)
                        market["source_tag"] = tag
                        all_markets.append(market)
        except Exception as e:
            logger.error(f"Error scanning tag {tag}: {e}")

    # Deduplicate by market ID
    seen = set()
    unique = []
    for m in all_markets:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)

    logger.info(f"Found {len(unique)} event markets passing filters")
    return unique


def _passes_filters(market: dict, now: datetime) -> bool:
    """Check if a market passes all filters."""
    # Must be active and accepting orders
    if not market.get("active") or market.get("closed"):
        return False
    if not market.get("accepting_orders", True):
        return False

    # HARD REJECT — price at 0c or near-resolved
    price = market.get("yes_price")
    if price is None or price <= 0.03 or price >= 0.97:
        return False
    if not (config.PRICE_RANGE[0] <= price <= config.PRICE_RANGE[1]):
        return False

    # Liquidity check
    if market.get("liquidity", 0) < config.MIN_LIQUIDITY:
        return False

    # Volume check
    if market.get("volume_24h", 0) < config.MIN_VOLUME_24H:
        return False

    # Check if market has already ended
    end_date_str = market.get("end_date", "")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

            # REJECT if market already ended
            if end_date < now:
                return False

            hours_until = (end_date - now).total_seconds() / 3600
            days_until = hours_until / 24

            if days_until > config.MAX_RESOLUTION_DAYS:
                return False
            if hours_until < config.MIN_RESOLUTION_HOURS:
                return False
        except Exception:
            return False  # Can't parse date = skip
    else:
        return False  # No end date = skip

    return True


def scan_volume_surges() -> list[dict]:
    """Find markets with unusual volume activity (potential news catalyst)."""
    markets = []
    try:
        events = pm.search_events(limit=50)
        for event in events:
            event_title = event.get("title", "") or ""
            for market_data in event.get("markets", []):
                # Skip markets not accepting orders
                if not market_data.get("acceptingOrders", False):
                    continue

                market = pm.parse_market_data(market_data)
                question = market.get("question", "")
                slug = market.get("slug", "") or market_data.get("slug", "")

                # EXCLUSION CHECK — block all sports/esports/gaming
                if _is_excluded(question, slug, event_title):
                    continue

                # WHITELIST CHECK — must match an allowed topic
                if not _is_whitelisted(question):
                    continue

                # HARD REJECT — price at 0c or near-resolved
                price = market.get("yes_price")
                if price is None or price <= 0.03 or price >= 0.97:
                    continue

                # Check if market has already ended
                end_date_str = market.get("end_date", "")
                if end_date_str:
                    try:
                        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        if end_date < datetime.now(timezone.utc):
                            continue
                    except Exception:
                        pass

                volume_24h = market.get("volume_24h", 0)
                total_volume = market.get("volume", 0)

                # Volume surge: 24h volume is >20% of all-time volume
                if total_volume > 0 and volume_24h > 0:
                    volume_ratio = volume_24h / total_volume
                    if volume_ratio > 0.20 and volume_24h > config.MIN_VOLUME_24H:
                        market["volume_surge_ratio"] = volume_ratio
                        market["url"] = pm.get_market_url(market_data)
                        markets.append(market)
    except Exception as e:
        logger.error(f"Error scanning volume surges: {e}")

    return sorted(markets, key=lambda x: x.get("volume_surge_ratio", 0), reverse=True)[:10]
