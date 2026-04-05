"""Gamma API market discovery and filtering for non-sports events."""

from __future__ import annotations

import logging
import os
import re
from datetime import timezone

import httpx

from events_agent.config import EventsConfig
from events_agent.models import EventCategory, EventMarket
from intelligence.sports_filter import is_sports_market
from shared.utils import utcnow, parse_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sports keywords — ANY match in slug or title → reject
# ---------------------------------------------------------------------------
_SPORTS_KEYWORDS = {
    # Leagues
    "nba", "nfl", "nhl", "mlb", "mls", "wnba", "cfl", "xfl",
    "ncaa", "cbb", "cfb", "cwbb",
    # Sports
    "soccer", "football", "basketball", "baseball", "hockey",
    "tennis", "golf", "cricket", "rugby", "boxing",
    "mma", "ufc", "f1", "formula-1", "formula1", "nascar",
    # Esports
    "csgo", "dota", "valorant", "lol-", "league-of-legends",
    # Leagues (international)
    "premier-league", "premierleague", "champions-league", "championsleague",
    "world-cup", "worldcup", "olympics", "olympic",
    "fifa", "atp", "wta", "pga", "lpga",
    "serie-a", "seriea", "la-liga", "laliga",
    "bundesliga", "ligue-1", "ligue1", "ipl",
    "eredivisie", "super-bowl", "superbowl",
    "stanley-cup", "stanleycup", "world-series", "worldseries",
    "march-madness", "marchmadness",
    # Europa / Turkish / French / misc leagues and teams
    "europa-league", "europa league", "europa",
    "fenerbahce", "super-lig", "süper lig", "süper-lig",
    "french-open", "grand-slam", "roland-garros",
    "champions", "celta",
    # Generic sports terms in slug context
    "moneyline", "spread-away", "spread-home", "total-over", "total-under",
    "-points-", "-rebounds-", "-assists-", "-touchdowns-",
    "-goals-", "-saves-", "-strikeouts-",
    # Player props
    "-1h-", "-1q-", "-halftime-",
    # Additional sports terms
    "o-u-", "over-under", "game-total", "points-total", "match-winner",
    "first-half", "second-half", "corners", "penalty", "red-card",
    "yellow-card", "wickets", "innings",
}

# ---------------------------------------------------------------------------
# Category detection keywords
# ---------------------------------------------------------------------------
_CATEGORY_KEYWORDS: dict[EventCategory, list[str]] = {
    EventCategory.POLITICS: [
        "president", "election", "senate", "congress", "governor", "mayor",
        "republican", "democrat", "gop", "white-house", "whitehouse",
        "trump", "biden", "political", "vote", "ballot", "primary",
        "cabinet", "impeach", "legislation", "bill-pass", "executive-order",
        "approval-rating", "poll",
    ],
    EventCategory.GEOPOLITICS: [
        "war", "conflict", "nato", "sanction", "treaty", "ceasefire",
        "invasion", "territory", "diplomatic", "embassy", "nuclear",
        "tariff", "trade-war", "un-security", "missile",
    ],
    EventCategory.ECONOMICS: [
        "fed", "interest-rate", "inflation", "gdp", "unemployment",
        "recession", "stock-market", "s-p-500", "sp500", "dow-jones",
        "nasdaq", "treasury", "bond", "housing", "jobs-report",
        "cpi", "ppi", "fomc", "rate-cut", "rate-hike",
    ],
    EventCategory.CRYPTO: [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
        "defi", "nft", "solana", "sol-", "altcoin", "binance",
        "coinbase", "sec-crypto", "stablecoin", "halving",
    ],
    EventCategory.SCIENCE: [
        "climate", "nasa", "space", "vaccine", "pandemic", "fda",
        "drug-approval", "ai-safety", "agi", "scientific",
        "asteroid", "earthquake", "hurricane", "wildfire",
    ],
    EventCategory.ENTERTAINMENT: [
        "oscar", "grammy", "emmy", "golden-globe", "box-office",
        "movie", "tv-show", "streaming", "netflix", "disney",
        "celebrity", "album", "concert", "billboard",
    ],
    EventCategory.TECHNOLOGY: [
        "apple", "google", "microsoft", "meta", "amazon", "tesla",
        "openai", "chatgpt", "ai-model", "iphone", "android",
        "antitrust", "tech-regulation", "ipo", "merger",
    ],
    EventCategory.CULTURE: [
        "tiktok", "social-media", "viral", "meme", "influencer",
        "controversy", "scandal", "lawsuit", "supreme-court",
        "roe-v-wade", "gun-control", "immigration",
    ],
    EventCategory.COMMODITIES: [
        "oil", "crude", "wti", "brent", "gold", "silver", "copper",
        "natural gas", "commodity", "opec", "barrel", "precious metal",
    ],
    EventCategory.MACRO_ECONOMICS: [
        "gdp", "cpi", "inflation", "unemployment", "interest rate", "fed rate",
        "federal reserve", "pce", "ppi", "jobs report", "nonfarm", "payroll",
        "recession", "treasury", "yield", "bond", "debt ceiling",
    ],
    EventCategory.FOREX: [
        "usd", "eur", "gbp", "jpy", "dollar", "euro", "pound", "yen",
        "exchange rate", "currency",
    ],
    EventCategory.CLIMATE: [
        "climate change", "carbon", "emissions", "renewable", "solar",
        "wind energy", "drought", "flooding", "wildfire", "sea level",
        "temperature record", "paris agreement", "cop2",
    ],
    EventCategory.TECH_INDUSTRY: [
        "ai", "agi", "gpt", "artificial intelligence", "tech regulation",
        "antitrust", "earnings", "revenue", "market cap", "ipo",
    ],
    EventCategory.FUTURES: [
        "futures", "contract", "expiry", "settlement", "forward",
        "derivative", "margin", "open interest",
    ],
}

# Secondary scan keywords — commodity/macro markets often phrase questions this way
_SECONDARY_SCAN_KEYWORDS = [
    "oil price", "gold price", "federal reserve", "interest rate",
    "inflation", "CPI", "GDP", "tariff", "sanctions",
]

# ---------------------------------------------------------------------------
# Crypto daily price markets — no informational edge on spot-price predictions
# ---------------------------------------------------------------------------

# Sports markets — filter out completely
SPORTS_RE = re.compile(
    r'(?:'
    r'(?:vs\.?|versus)\s'  # "X vs Y" pattern (common in sports)
    r'|O/U\s+\d'  # Over/Under lines
    r'|\bO/U\b'  # standalone O/U
    r'|\bover[/-]under\b'
    r'|(?:Games? Total|Total (?:Points|Goals|Runs))'
    r'|(?:NBA|NFL|NHL|MLB|UEFA|FIFA|EPL|LaLiga|Serie A|Bundesliga|Ligue 1)'
    r'|(?:Moneyline|Spread|Point Spread|Handicap)'
    r'|(?:Will .+ win (?:the |their )?(?:game|match|series))'
    r'|(?:first|second)\s+half'
    r'|(?:total|combined)\s+(?:points|goals|runs|score)'
    r'|(?:home|away)\s+(?:team|side)'
    r')',
    re.IGNORECASE
)

CRYPTO_DAILY_PRICE_RE = re.compile(
    r'(?:'
    r'Will (?:the price of )?(?:Bitcoin|BTC|Ethereum|ETH|XRP|Solana|SOL|Dogecoin|DOGE|Cardano|ADA|Avalanche|AVAX|Polkadot|DOT|Chainlink|LINK|Litecoin|LTC|BNB|MATIC|Polygon).*?(?:be above|be between|reach|dip to|hit|drop|fall|close above|close below).*?\$[\d,]+'
    r'|(?:Bitcoin|BTC|Ethereum|ETH|XRP|Solana|SOL)\s+(?:Up|Down)\s+(?:or|Or)'
    r'|Will (?:Bitcoin|BTC|Ethereum|ETH)\s+(?:reach|hit|dip|pump|dump|crash)'
    r'|(?:Bitcoin|BTC|Ethereum|ETH|XRP|Solana|SOL)\s+(?:price range|daily|hourly)'
    r')',
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Market priority tiers (by 24h volume)
# ---------------------------------------------------------------------------
PRIORITY_TIERS = {
    "TIER_1": 100_000,  # >$100K — always process, all modules
    "TIER_2": 50_000,   # >$50K  — process with GDELT + all specialist modules
    "TIER_3": 10_000,   # >$10K  — only if specialist modules have data
}

# ---------------------------------------------------------------------------
# Category targeting — keyword matching → priority boost multiplier
# ---------------------------------------------------------------------------
TARGET_CATEGORIES: dict[str, dict] = {
    "us_politics": {
        "keywords": ["trump", "president", "congress", "senate", "house",
                      "election", "democrat", "republican", "2028",
                      "presidential", "governor"],
        "priority_boost": 1.5,
    },
    "geopolitics_conflict": {
        "keywords": ["iran", "russia", "ukraine", "war", "military",
                      "ceasefire", "invasion", "nato", "china", "taiwan",
                      "israel", "gaza", "houthi", "sanctions", "nuclear"],
        "priority_boost": 1.5,
    },
    "macro_economics": {
        "keywords": ["fed", "rate", "interest", "inflation", "cpi", "gdp",
                      "recession", "tariff", "trade war", "treasury",
                      "yield", "employment", "unemployment"],
        "priority_boost": 1.5,
    },
    "commodities": {
        "keywords": ["oil", "crude", "wti", "gold", "silver", "commodity",
                      "brent", "natural gas"],
        "priority_boost": 1.5,
    },
    "foreign_elections": {
        "keywords": ["peru", "brazil", "canada", "australia", "germany",
                      "france", "uk", "mexico", "india", "south korea"],
        "priority_boost": 0.7,
    },
}


def _assign_priority_tier(volume_24h: float) -> str:
    """Return the priority tier for a given 24h volume."""
    if volume_24h >= PRIORITY_TIERS["TIER_1"]:
        return "TIER_1"
    if volume_24h >= PRIORITY_TIERS["TIER_2"]:
        return "TIER_2"
    if volume_24h >= PRIORITY_TIERS["TIER_3"]:
        return "TIER_3"
    return "UNTIERED"


def _assign_target_category(question: str) -> tuple[str, float]:
    """Return (category_name, boost) for a market question. Default ('other', 1.0).

    When two categories tie on keyword hits, prefer the one whose keywords are
    more specific (lower boost means more restrictive category).
    """
    q_lower = question.lower()
    best_cat = "other"
    best_boost = 1.0
    best_hits = 0
    for cat_name, cat_info in TARGET_CATEGORIES.items():
        hits = sum(1 for kw in cat_info["keywords"] if kw in q_lower)
        if hits > best_hits or (hits == best_hits and hits > 0
                                and cat_info["priority_boost"] < best_boost):
            best_hits = hits
            best_cat = cat_name
            best_boost = cat_info["priority_boost"]
    return best_cat, best_boost


class EventsScanner:
    """Scans Polymarket Gamma API for non-sports events markets."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()
        self.base_url = self.config.GAMMA_API_BASE

    async def scan(self) -> list[EventMarket]:
        """Fetch and filter events markets from Gamma API."""
        raw_events = await self._fetch_events()
        markets: list[EventMarket] = []

        for event in raw_events:
            event_slug = event.get("slug", "")
            event_title = event.get("title", "")

            # Filter: must NOT be sports
            if self._is_sports_event(event_slug, event_title):
                continue

            for raw_market in event.get("markets", []):
                try:
                    market = EventMarket.from_api(raw_market, event_slug, event_title)

                    # Double-check: reject if market question/slug contains sports keywords
                    if self._is_sports_event(market.slug, market.question):
                        continue

                    # Comprehensive sports check (includes regex patterns, tags, category)
                    if is_sports_market(market):
                        logger.debug("Skipping sports market: %s", market.id)
                        continue

                    market.category = self._detect_category(
                        market.slug, market.question, event_slug, event_title
                    )

                    if self._passes_filters(market):
                        markets.append(market)
                except Exception as e:
                    logger.warning("Failed to parse market %s: %s", raw_market.get("id"), e)

        # Market quality gate — minimum volume and liquidity
        min_vol = float(os.getenv("EVENTS_MIN_VOLUME_24H", "5000"))
        min_liq = float(os.getenv("EVENTS_MIN_LIQUIDITY", "10000"))
        pre_quality = len(markets)
        filtered_markets = []
        for m in markets:
            if m.volume_24h < min_vol or m.liquidity < min_liq:
                logger.debug(
                    "Quality gate: skipping %s (vol=$%.0f, liq=$%.0f)",
                    m.slug, m.volume_24h, m.liquidity,
                )
                continue
            filtered_markets.append(m)
        markets = filtered_markets
        logger.info(
            "Quality gate: %d → %d markets (min_vol=$%.0f, min_liq=$%.0f)",
            pre_quality, len(markets), min_vol, min_liq,
        )

        # ---- Priority tiers + category targeting ----
        # Sort by volume descending so highest-value markets are processed first
        markets.sort(key=lambda m: m.volume_24h, reverse=True)

        for m in markets:
            m.priority_tier = _assign_priority_tier(m.volume_24h)
            m.target_category, m.category_boost = _assign_target_category(m.question)

        tier_counts = {}
        for m in markets:
            tier_counts[m.priority_tier] = tier_counts.get(m.priority_tier, 0) + 1
        logger.info("Priority tiers: %s", tier_counts)

        logger.info("Scanner found %d events markets after filtering", len(markets))
        return markets

    async def _fetch_events(self) -> list[dict]:
        """Fetch active events from Gamma API sorted by liquidity.

        Runs a primary liquidity-sorted fetch, then a secondary keyword-based
        fetch for commodity/macro markets that use "price", "rate", "above",
        "below", "over", "under" in the question.
        """
        all_events: list[dict] = []
        seen_ids: set[str] = set()
        offset = 0
        limit = 100

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Primary fetch: liquidity-sorted
            while True:
                try:
                    resp = await client.get(
                        f"{self.base_url}/events",
                        params={
                            "active": "true",
                            "closed": "false",
                            "limit": limit,
                            "offset": offset,
                            "order": "liquidityClob",
                            "ascending": "false",
                        },
                    )
                    resp.raise_for_status()
                    events = resp.json()
                    if not events:
                        break
                    for ev in events:
                        eid = ev.get("id", "")
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            all_events.append(ev)
                    if len(events) < limit:
                        break
                    offset += limit
                    # Paginate deeper for events — more diverse markets
                    if offset >= 1000:
                        break
                except httpx.HTTPError as e:
                    logger.error("Gamma API request failed (offset=%d): %s", offset, e)
                    break

            # Secondary fetch: keyword-based for commodity/macro markets
            for keyword in _SECONDARY_SCAN_KEYWORDS:
                try:
                    resp = await client.get(
                        f"{self.base_url}/events",
                        params={
                            "active": "true",
                            "closed": "false",
                            "limit": 50,
                            "offset": 0,
                            "title": keyword,
                        },
                    )
                    resp.raise_for_status()
                    events = resp.json()
                    for ev in events:
                        eid = ev.get("id", "")
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            all_events.append(ev)
                except httpx.HTTPError as e:
                    logger.debug("Secondary scan for '%s' failed: %s", keyword, e)

        logger.info("Fetched %d events from Gamma API (primary + secondary scan)", len(all_events))
        return all_events

    def _is_sports_event(self, slug: str, title: str) -> bool:
        """Check if this event is sports-related — reject if so.

        Uses the shared ``is_sports_market()`` utility for comprehensive
        detection (keywords, regex patterns, category/tags fields).
        """
        proxy = {"slug": slug, "question": title, "event_slug": slug, "event_title": title}
        if is_sports_market(proxy):
            return True

        # Keep legacy keyword check as a safety net
        combined = slug.lower() + " " + title.lower()
        for keyword in _SPORTS_KEYWORDS:
            if keyword in combined:
                return True

        return False

    def _detect_category(
        self, slug: str, question: str, event_slug: str, event_title: str,
    ) -> EventCategory:
        """Detect the category of an events market."""
        combined = (slug + " " + question + " " + event_slug + " " + event_title).lower()

        best_category = EventCategory.OTHER
        best_matches = 0

        for category, keywords in _CATEGORY_KEYWORDS.items():
            matches = sum(1 for kw in keywords if kw in combined)
            if matches > best_matches:
                best_matches = matches
                best_category = category

        return best_category

    def _passes_filters(self, market: EventMarket) -> bool:
        """Apply all filtering rules to a market."""
        now = utcnow()

        # Block crypto daily price markets (no edge on spot-price predictions)
        if SPORTS_RE.search(market.question):
            logger.info("Filtered sports market: %s", market.question[:80])
            return False
        if CRYPTO_DAILY_PRICE_RE.search(market.question):
            logger.info("Filtered crypto daily price market: %s", market.question[:80])
            return False

        # Must be active and not closed
        if not market.active or market.closed:
            return False

        # Must accept orders
        if not market.accepting_orders:
            return False

        # Liquidity floor
        if market.liquidity < self.config.MIN_LIQUIDITY:
            logger.debug("Rejected %s: liquidity %.0f < %.0f",
                         market.slug, market.liquidity, self.config.MIN_LIQUIDITY)
            return False

        # Price bounds: each outcome price between 0.05 and 0.85
        for price in market.outcome_prices:
            if price < 0.05 or price > 0.85:
                logger.debug("Rejected %s: price %.2f out of bounds [0.05, 0.85]", market.slug, price)
                return False

        # End date must be in the future
        if market.end_date:
            try:
                end_dt = parse_utc(market.end_date)
                if end_dt <= now:
                    return False
            except ValueError:
                pass

        # Don't enter markets expiring within 4 hours — not enough time for signals
        if market.end_date:
            try:
                end_dt = parse_utc(market.end_date)
                hours_remaining = (end_dt - now).total_seconds() / 3600
                if hours_remaining < 4:
                    logger.debug("Rejected %s: expires in %.1f hours (min 4h)", market.slug, hours_remaining)
                    return False
            except ValueError:
                pass

        # Must have at least 2 outcomes
        if len(market.outcomes) < 2 or len(market.clob_token_ids) < 2:
            return False

        return True

    async def get_market_price(self, token_id: str) -> float | None:
        """Get current midpoint price for a token."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(self.config.CLOB_API_BASE)
            mid = client.get_midpoint(token_id)
            return float(mid.get("mid", 0))
        except Exception as e:
            logger.warning("Failed to get midpoint for %s: %s", token_id, e)
            return None

    async def get_order_book(self, token_id: str) -> dict | None:
        """Get order book for a token."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(self.config.CLOB_API_BASE)
            book = client.get_order_book(token_id)
            return {
                "bids": [(b.price, b.size) for b in book.bids] if book.bids else [],
                "asks": [(a.price, a.size) for a in book.asks] if book.asks else [],
            }
        except Exception as e:
            logger.warning("Failed to get order book for %s: %s", token_id, e)
            return None
