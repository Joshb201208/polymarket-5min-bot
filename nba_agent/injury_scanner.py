"""Injury news scanner via Google News RSS."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import feedparser
import httpx

logger = logging.getLogger(__name__)

_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# Keywords that indicate a player is out or injured
_OUT_KEYWORDS = ("out", "ruled out", "sidelined", "injury", "injured", "miss", "absence", "doubtful", "questionable")
_POSITIVE_KEYWORDS = ("returns", "return", "cleared", "healthy", "available", "playing", "active")


@dataclass
class InjuryReport:
    """An injury news item."""
    headline: str
    team: str
    player: str
    status: str  # "out", "questionable", "healthy"
    source: str
    published: str


class InjuryScanner:
    """Scans Google News RSS for NBA injury updates."""

    async def scan_team(self, team_name: str) -> list[InjuryReport]:
        """Search for recent injury news for a team."""
        query = f"{team_name} NBA injury"
        reports: list[InjuryReport] = []

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    _GOOGLE_NEWS_RSS,
                    params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                )
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)

                for entry in feed.entries[:5]:  # Check top 5 results
                    title = entry.get("title", "").lower()
                    report = self._parse_headline(
                        entry.get("title", ""),
                        team_name,
                        entry.get("source", {}).get("title", ""),
                        entry.get("published", ""),
                    )
                    if report:
                        reports.append(report)

        except Exception as e:
            logger.warning("Injury scan failed for %s: %s", team_name, e)

        return reports

    def _parse_headline(
        self,
        headline: str,
        team_name: str,
        source: str,
        published: str,
    ) -> InjuryReport | None:
        """Parse an injury headline into a structured report."""
        headline_lower = headline.lower()

        # Must mention injury-related keywords
        is_negative = any(kw in headline_lower for kw in _OUT_KEYWORDS)
        is_positive = any(kw in headline_lower for kw in _POSITIVE_KEYWORDS)

        if not is_negative and not is_positive:
            return None

        status = "out" if is_negative else "healthy"
        if "questionable" in headline_lower:
            status = "questionable"
        elif "doubtful" in headline_lower:
            status = "doubtful"

        return InjuryReport(
            headline=headline,
            team=team_name,
            player="",  # Would need NLP to extract player name reliably
            status=status,
            source=source,
            published=published,
        )

    async def get_injury_summary(self, team_name: str) -> list[str]:
        """Get a list of injury headline strings for a team."""
        reports = await self.scan_team(team_name)
        return [f"{r.headline} ({r.status})" for r in reports]
