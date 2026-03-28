"""Gamma API market discovery and filtering for non-sports events."""

from __future__ import annotations

import logging
from datetime import timezone

import httpx

from events_agent.config import EventsConfig
from events_agent.models import EventCategory, EventMarket
from nba_agent.utils import utcnow, parse_utc

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
    # Generic sports terms in slug context
    "moneyline", "spread-away", "spread-home", "total-over", "total-under",
    "-points-", "-rebounds-", "-assists-", "-touchdowns-",
    "-goals-", "-saves-", "-strikeouts-",
    # Player props
    "-1h-", "-1q-", "-halftime-",
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
    "price", "rate", "above", "below", "over", "under",
]


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

                    market.category = self._detect_category(
                        market.slug, market.question, event_slug, event_title
                    )

                    if self._passes_filters(market):
                        markets.append(market)
                except Exception as e:
                    logger.warning("Failed to parse market %s: %s", raw_market.get("id"), e)

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
        """Check if this event is sports-related — reject if so."""
        slug_lower = slug.lower()
        title_lower = title.lower()
        combined = slug_lower + " " + title_lower

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
