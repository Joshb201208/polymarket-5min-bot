"""Tier 3C: Correlation Monitor.

Monitors correlation between open positions by theme. Warns when portfolio
is over-concentrated in a single area (e.g., >30% in US politics).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from intelligence.models import CorrelationReport
from nba_agent.utils import utcnow

logger = logging.getLogger("intelligence.correlation")

# Pre-defined correlation themes
CORRELATED_THEMES: dict[str, list[str]] = {
    "us_politics": [
        "trump", "republican", "democrat", "congress", "senate", "midterm",
        "election", "president", "governor", "gop", "white house", "ballot",
        "impeach", "cabinet", "legislation",
    ],
    "crypto_regulation": [
        "bitcoin", "ethereum", "sec", "crypto", "stablecoin", "defi",
        "blockchain", "btc", "eth", "digital asset", "coinbase", "binance",
    ],
    "fed_monetary": [
        "fed", "interest rate", "inflation", "recession", "treasury",
        "fomc", "rate cut", "rate hike", "cpi", "ppi", "unemployment",
        "gdp", "jobs report",
    ],
    "geopolitical": [
        "china", "russia", "ukraine", "taiwan", "nato", "sanctions",
        "tariff", "trade war", "missile", "ceasefire", "invasion",
    ],
    "tech_regulation": [
        "antitrust", "big tech", "ai regulation", "data privacy",
        "apple", "google", "meta", "microsoft", "openai",
    ],
}

# Concentration warning threshold
_CONCENTRATION_THRESHOLD = 0.30  # 30% of total exposure


class CorrelationMonitor:
    """Monitors portfolio concentration and theme-level correlation risk."""

    def analyze(self, open_positions: list) -> CorrelationReport:
        """Analyze correlation and concentration risk in open positions.

        Args:
            open_positions: List of Position objects (from events_agent.models)

        Returns:
            CorrelationReport with theme exposure, warnings, and suggestions.
        """
        if not open_positions:
            return CorrelationReport(
                theme_exposure={},
                concentration_warnings=[],
                diversification_score=100,
                suggested_actions=[],
                pairwise_correlations=[],
            )

        # 1. Classify each position into theme groups
        theme_positions: dict[str, list] = defaultdict(list)
        theme_exposure_usd: dict[str, float] = defaultdict(float)
        total_exposure = 0.0

        for pos in open_positions:
            cost = getattr(pos, "cost", 0.0)
            if cost <= 0:
                continue

            total_exposure += cost
            question = getattr(pos, "market_question", "").lower()
            slug = getattr(pos, "market_slug", "").lower()
            combined = question + " " + slug

            matched_themes = set()
            for theme, keywords in CORRELATED_THEMES.items():
                for kw in keywords:
                    if kw in combined:
                        matched_themes.add(theme)
                        break

            if not matched_themes:
                matched_themes.add("other")

            for theme in matched_themes:
                theme_positions[theme].append(pos)
                theme_exposure_usd[theme] += cost

        if total_exposure <= 0:
            return CorrelationReport(
                theme_exposure={},
                concentration_warnings=[],
                diversification_score=100,
                suggested_actions=[],
                pairwise_correlations=[],
            )

        # 2. Build theme exposure report
        theme_exposure: dict[str, dict] = {}
        for theme, positions in theme_positions.items():
            exposure = theme_exposure_usd[theme]
            pct = exposure / total_exposure
            theme_exposure[theme] = {
                "positions": [
                    {
                        "id": getattr(p, "id", ""),
                        "question": getattr(p, "market_question", "")[:60],
                        "cost": getattr(p, "cost", 0),
                    }
                    for p in positions
                ],
                "total_usd": round(exposure, 2),
                "pct": round(pct, 3),
            }

        # 3. Check for concentration warnings
        concentration_warnings: list[str] = []
        suggested_actions: list[str] = []

        for theme, info in theme_exposure.items():
            if info["pct"] > _CONCENTRATION_THRESHOLD:
                concentration_warnings.append(theme)
                excess = info["total_usd"] - (total_exposure * _CONCENTRATION_THRESHOLD)
                suggested_actions.append(
                    f"Reduce {theme} exposure by ${excess:.2f} "
                    f"({info['pct'] * 100:.0f}% > {_CONCENTRATION_THRESHOLD * 100:.0f}% limit)"
                )

        # 4. Calculate diversification score (0-100)
        # Based on Herfindahl-Hirschman Index (HHI)
        theme_pcts = [info["pct"] for info in theme_exposure.values()]
        hhi = sum(p ** 2 for p in theme_pcts)
        # HHI of 1.0 = perfectly concentrated, 1/n = perfectly diversified
        n_themes = len(theme_pcts)
        if n_themes <= 1:
            diversification_score = 0
        else:
            min_hhi = 1.0 / n_themes
            max_hhi = 1.0
            if max_hhi > min_hhi:
                normalized = (max_hhi - hhi) / (max_hhi - min_hhi)
                diversification_score = int(round(normalized * 100))
            else:
                diversification_score = 100

        diversification_score = max(0, min(100, diversification_score))

        # 5. Pairwise correlations (simplified — based on theme co-membership)
        pairwise: list[tuple] = []
        position_list = list(open_positions)
        for i in range(len(position_list)):
            for j in range(i + 1, len(position_list)):
                p1 = position_list[i]
                p2 = position_list[j]
                corr = self._estimate_pairwise_correlation(p1, p2)
                if corr > 0.3:
                    pairwise.append((
                        getattr(p1, "market_question", "")[:40],
                        getattr(p2, "market_question", "")[:40],
                        round(corr, 2),
                    ))

        # Sort by correlation descending
        pairwise.sort(key=lambda x: x[2], reverse=True)

        report = CorrelationReport(
            theme_exposure=theme_exposure,
            concentration_warnings=concentration_warnings,
            diversification_score=diversification_score,
            suggested_actions=suggested_actions,
            pairwise_correlations=pairwise[:20],  # Top 20
        )

        if concentration_warnings:
            logger.warning(
                "Concentration warnings: %s (diversification=%d/100)",
                concentration_warnings,
                diversification_score,
            )
        else:
            logger.info("Portfolio diversification score: %d/100", diversification_score)

        return report

    def _estimate_pairwise_correlation(self, p1, p2) -> float:
        """Estimate correlation between two positions based on theme overlap."""
        q1 = (getattr(p1, "market_question", "") + " " + getattr(p1, "market_slug", "")).lower()
        q2 = (getattr(p2, "market_question", "") + " " + getattr(p2, "market_slug", "")).lower()

        themes_1 = set()
        themes_2 = set()

        for theme, keywords in CORRELATED_THEMES.items():
            for kw in keywords:
                if kw in q1:
                    themes_1.add(theme)
                    break
            for kw in keywords:
                if kw in q2:
                    themes_2.add(theme)
                    break

        if not themes_1 or not themes_2:
            return 0.0

        overlap = len(themes_1 & themes_2)
        total = len(themes_1 | themes_2)

        return overlap / total if total > 0 else 0.0
