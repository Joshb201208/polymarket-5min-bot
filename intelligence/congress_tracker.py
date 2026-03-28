"""Tier 2B: Congress/Government Action Tracker.

Monitors Federal Register API (free, no key) for executive orders and
Congress API for bill activity. Matches government actions to political markets.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.congress_tracker")

# Keywords for matching government actions to markets
_POLITICAL_KEYWORDS = {
    "executive order", "tariff", "trade", "immigration", "border",
    "climate", "energy", "healthcare", "tax", "budget", "spending",
    "defense", "military", "sanction", "regulation", "deregulation",
    "crypto", "digital asset", "AI", "artificial intelligence",
    "antitrust", "tech", "gun", "abortion", "supreme court",
    "federal reserve", "interest rate", "inflation", "debt ceiling",
}


class CongressTracker:
    """Monitors government actions and matches them to prediction markets."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._actions_path = self.config.DATA_DIR / "congress_actions.json"
        self._seen_actions: set = set()

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan government sources for signals. Returns Signal list."""
        if not self.config.is_enabled("congress"):
            logger.debug("Congress tracker disabled")
            return []

        self._load_seen_actions()
        signals: list[Signal] = []
        logger.info("Congress tracker scanning %d active markets", len(active_markets))

        # 1. Fetch executive orders from Federal Register (free, no key)
        try:
            logger.info("Scanning Federal Register for executive orders (no API key needed)...")
            eo_signals = await asyncio.wait_for(
                self._scan_executive_orders(active_markets),
                timeout=self.config.MODULE_TIMEOUT,
            )
            signals.extend(eo_signals)
            logger.info("Executive order scan complete: %d signals generated", len(eo_signals))
        except asyncio.TimeoutError:
            logger.warning("Executive order scan timed out after %ds", self.config.MODULE_TIMEOUT)
        except Exception as e:
            logger.error("Executive order scan failed: %s", e, exc_info=True)

        # 2. Fetch bill activity from Congress API (optional key)
        if self.config.CONGRESS_API_KEY:
            try:
                logger.info("Scanning Congress API for bill activity (API key present)...")
                bill_signals = await asyncio.wait_for(
                    self._scan_bill_activity(active_markets),
                    timeout=self.config.MODULE_TIMEOUT,
                )
                signals.extend(bill_signals)
                logger.info("Bill activity scan complete: %d signals generated", len(bill_signals))
            except asyncio.TimeoutError:
                logger.warning("Bill activity scan timed out after %ds", self.config.MODULE_TIMEOUT)
            except Exception as e:
                logger.error("Bill activity scan failed: %s", e, exc_info=True)
        else:
            logger.info("Congress API key not set — skipping bill activity scan (executive orders still active)")

        self._save_seen_actions()
        logger.info("Congress tracker complete: %d total signals, %d seen actions tracked", len(signals), len(self._seen_actions))
        return signals

    async def _scan_executive_orders(self, active_markets: list) -> list[Signal]:
        """Fetch recent executive orders from Federal Register API."""
        url = f"{self.config.FEDERAL_REGISTER_API}/documents.json"
        params = {
            "conditions[type][]": "PRESDOCU",
            "conditions[presidential_document_type][]": "executive_order",
            "per_page": 20,
            "order": "newest",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                documents = data.get("results", [])
                logger.info("Federal Register returned %d documents", len(documents))
        except httpx.HTTPError as e:
            logger.error("Federal Register API failed: %s (url=%s)", e, url)
            return []

        signals: list[Signal] = []
        now = utcnow()
        skipped_old = 0
        skipped_seen = 0
        checked_markets = 0

        for doc in documents:
            doc_id = doc.get("document_number", "")
            if doc_id in self._seen_actions:
                skipped_seen += 1
                continue

            title = doc.get("title", "")
            abstract = doc.get("abstract", "") or ""
            pub_date = doc.get("publication_date", "")

            # Only process recent documents (last 48h)
            if pub_date:
                try:
                    pub_dt = datetime.strptime(pub_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if (now - pub_dt).days > 2:
                        skipped_old += 1
                        continue
                except ValueError:
                    pass

            checked_markets += 1

            # Match to active markets
            matched = self._match_to_markets(
                title + " " + abstract, active_markets,
            )

            if matched:
                logger.debug("EO '%s' matched %d markets", title[:60], len(matched))

            for market, relevance_score in matched:
                market_id = getattr(market, "id", "")
                question = getattr(market, "question", "")

                signals.append(Signal(
                    source="congress",
                    market_id=market_id,
                    market_question=question,
                    signal_type="executive_order",
                    direction="NEUTRAL",  # EOs don't inherently indicate YES/NO
                    strength=min(relevance_score, 1.0),
                    confidence=0.6,
                    details={
                        "document_type": "executive_order",
                        "title": title,
                        "document_number": doc_id,
                        "publication_date": pub_date,
                    },
                    timestamp=now,
                    expires_at=now + timedelta(hours=12),
                ))

            self._seen_actions.add(doc_id)

        logger.info(
            "Executive orders: %d fetched, %d already seen, %d too old, %d checked, %d signals",
            len(documents), skipped_seen, skipped_old, checked_markets, len(signals),
        )
        return signals

    async def _scan_bill_activity(self, active_markets: list) -> list[Signal]:
        """Fetch recent bill activity from Congress API."""
        url = f"{self.config.CONGRESS_API_BASE}/bill"
        since = (utcnow() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "fromDateTime": since,
            "sort": "updateDate+desc",
            "limit": 20,
            "api_key": self.config.CONGRESS_API_KEY,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                bills = data.get("bills", [])
        except httpx.HTTPError as e:
            logger.error("Congress API failed: %s", e)
            return []

        signals: list[Signal] = []
        now = utcnow()

        for bill in bills:
            bill_id = bill.get("number", "") + "-" + bill.get("congress", "")
            if bill_id in self._seen_actions:
                continue

            title = bill.get("title", "")
            latest_action = bill.get("latestAction", {})
            action_text = latest_action.get("text", "")

            # Determine if this is a significant action
            significance = self._assess_bill_significance(action_text)
            if significance < 0.3:
                continue

            # Match to active markets
            matched = self._match_to_markets(
                title + " " + action_text, active_markets,
            )

            for market, relevance_score in matched:
                market_id = getattr(market, "id", "")
                question = getattr(market, "question", "")

                # Determine direction based on action
                direction = self._infer_direction(action_text)

                signals.append(Signal(
                    source="congress",
                    market_id=market_id,
                    market_question=question,
                    signal_type="bill_activity",
                    direction=direction,
                    strength=min(relevance_score * significance, 1.0),
                    confidence=0.5,
                    details={
                        "bill_number": bill.get("number", ""),
                        "bill_title": title,
                        "latest_action": action_text,
                        "congress": bill.get("congress", ""),
                    },
                    timestamp=now,
                    expires_at=now + timedelta(hours=12),
                ))

            self._seen_actions.add(bill_id)

        logger.info("Bill activity: %d bills, %d signals", len(bills), len(signals))
        return signals

    def _match_to_markets(
        self, action_text: str, markets: list,
    ) -> list[tuple]:
        """Match government action text to relevant markets by keyword overlap."""
        action_lower = action_text.lower()
        matched: list[tuple] = []

        for market in markets:
            question = getattr(market, "question", "").lower()
            slug = getattr(market, "slug", "").lower()
            combined_market = question + " " + slug

            # Count keyword overlap
            overlap_count = 0
            for keyword in _POLITICAL_KEYWORDS:
                if keyword in action_lower and keyword in combined_market:
                    overlap_count += 1

            # Also check direct word overlap
            action_words = set(re.sub(r"[^\w\s]", "", action_lower).split())
            market_words = set(re.sub(r"[^\w\s]", "", combined_market).split())
            word_overlap = len(action_words & market_words)

            relevance = (overlap_count * 0.3) + (word_overlap * 0.05)
            if relevance >= 0.3:
                matched.append((market, min(relevance, 1.0)))

        return matched

    def _assess_bill_significance(self, action_text: str) -> float:
        """Score the significance of a bill action (0-1)."""
        text_lower = action_text.lower()
        high_significance = [
            "passed", "signed into law", "approved", "enacted",
            "vetoed", "overridden", "cloture", "conference report",
        ]
        medium_significance = [
            "reported", "committee", "hearing", "markup",
            "introduced", "referred", "amendment",
        ]

        for phrase in high_significance:
            if phrase in text_lower:
                return 0.8

        for phrase in medium_significance:
            if phrase in text_lower:
                return 0.5

        return 0.2

    def _infer_direction(self, action_text: str) -> str:
        """Infer YES/NO direction from bill action text."""
        text_lower = action_text.lower()
        positive = ["passed", "approved", "signed", "enacted", "agreed"]
        negative = ["vetoed", "rejected", "failed", "tabled", "withdrawn"]

        for word in positive:
            if word in text_lower:
                return "YES"
        for word in negative:
            if word in text_lower:
                return "NO"

        return "NEUTRAL"

    def _load_seen_actions(self) -> None:
        """Load seen action IDs from disk."""
        data = load_json(self._actions_path, {"seen": []})
        self._seen_actions = set(data.get("seen", []))

    def _save_seen_actions(self) -> None:
        """Persist seen action IDs to disk."""
        # Keep last 500 IDs
        seen_list = list(self._seen_actions)[-500:]
        try:
            atomic_json_write(self._actions_path, {"seen": seen_list})
        except Exception as e:
            logger.warning("Failed to save congress actions: %s", e)
