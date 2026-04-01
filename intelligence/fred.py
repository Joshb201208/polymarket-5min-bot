"""FRED Macro Monitor — economic indicator monitoring for rate/CPI/employment markets.

Uses the FRED (Federal Reserve Economic Data) API to track key economic series
and generate signals for macro-sensitive prediction markets.

Signal weight: 0.15
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from shared.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.fred")

FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Key FRED series to track
FRED_SERIES = {
    "FEDFUNDS": {"name": "Federal Funds Rate", "keywords": ["fed", "rate", "interest", "fomc"]},
    "UNRATE": {"name": "Unemployment Rate", "keywords": ["unemployment", "jobs", "labor"]},
    "CPIAUCSL": {"name": "CPI (All Urban)", "keywords": ["cpi", "inflation", "prices"]},
    "GDP": {"name": "GDP", "keywords": ["gdp", "growth", "economic"]},
    "DGS10": {"name": "10-Year Treasury", "keywords": ["treasury", "10 year", "yield", "bond"]},
    "DGS2": {"name": "2-Year Treasury", "keywords": ["treasury", "2 year", "yield"]},
    "T10Y2Y": {"name": "10Y-2Y Spread", "keywords": ["yield curve", "inversion", "spread"]},
    "DCOILWTICO": {"name": "WTI Crude Oil", "keywords": ["oil", "crude", "wti", "petroleum"]},
    "GOLDAMGBD228NLBM": {"name": "Gold Price", "keywords": ["gold", "precious metal"]},
}


class FREDMonitor:
    """Monitors FRED economic indicators for macro trading signals."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._api_key = os.environ.get("FRED_API_KEY", "bbe655dfc916200f56a95e28c653a32f")
        self._cache_path = self.config.DATA_DIR / "fred_cache.json"
        self._series_cache: dict[str, dict] = {}  # series_id -> {value, prev_value, date}

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan FRED data and generate signals for relevant markets."""
        if not self._api_key:
            logger.debug("FRED API key not set — skipping FRED scan")
            return []

        self._load_cache()

        # Fetch latest values for all tracked series
        await self._refresh_series()

        signals: list[Signal] = []

        for market in active_markets:
            try:
                signal = self._evaluate_market(market)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug("FRED evaluation error for %s: %s", getattr(market, "id", "?"), e)

        self._save_cache()
        logger.info("FRED scan: %d signals from %d series", len(signals), len(self._series_cache))
        return signals

    async def _refresh_series(self) -> None:
        """Fetch latest observations for all tracked FRED series."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            for series_id in FRED_SERIES:
                try:
                    resp = await client.get(FRED_API_BASE, params={
                        "series_id": series_id,
                        "api_key": self._api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": 5,
                    })
                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    observations = data.get("observations", [])

                    # Filter out "." values (FRED uses "." for missing data)
                    valid_obs = [
                        o for o in observations
                        if o.get("value", ".") != "."
                    ]

                    if len(valid_obs) >= 2:
                        latest = float(valid_obs[0]["value"])
                        previous = float(valid_obs[1]["value"])
                        self._series_cache[series_id] = {
                            "value": latest,
                            "prev_value": previous,
                            "date": valid_obs[0].get("date", ""),
                            "change": latest - previous,
                            "change_pct": ((latest - previous) / previous * 100) if previous != 0 else 0,
                        }
                    elif len(valid_obs) == 1:
                        self._series_cache[series_id] = {
                            "value": float(valid_obs[0]["value"]),
                            "prev_value": None,
                            "date": valid_obs[0].get("date", ""),
                            "change": 0,
                            "change_pct": 0,
                        }

                except Exception as e:
                    logger.debug("FRED fetch failed for %s: %s", series_id, e)

    def _evaluate_market(self, market) -> Signal | None:
        """Check if any FRED series is relevant to a market and generate signal."""
        market_id = getattr(market, "id", "")
        question = getattr(market, "question", "")
        if not question:
            return None

        question_lower = question.lower()

        # Find relevant FRED series for this market
        relevant_series: list[tuple[str, dict, dict]] = []

        for series_id, meta in FRED_SERIES.items():
            if series_id not in self._series_cache:
                continue

            # Check if market question matches any keywords
            keywords = meta["keywords"]
            if any(kw in question_lower for kw in keywords):
                relevant_series.append((series_id, meta, self._series_cache[series_id]))

        if not relevant_series:
            return None

        # Use the most relevant series (first match)
        series_id, meta, data = relevant_series[0]
        change_pct = data.get("change_pct", 0)

        # Only signal on meaningful changes (>0.5% change)
        if abs(change_pct) < 0.5:
            return None

        # Determine signal direction based on the type of indicator and market question
        direction = self._infer_direction(question_lower, series_id, change_pct)

        strength = min(abs(change_pct) / 5.0, 1.0)
        confidence = 0.7  # FRED data is authoritative but may lag

        now = utcnow()
        return Signal(
            source="fred",
            market_id=market_id,
            market_question=question,
            signal_type="macro_indicator",
            direction=direction,
            strength=round(strength, 3),
            confidence=confidence,
            details={
                "series_id": series_id,
                "series_name": meta["name"],
                "current_value": data.get("value"),
                "previous_value": data.get("prev_value"),
                "change_pct": round(change_pct, 2),
                "observation_date": data.get("date", ""),
            },
            timestamp=now,
            expires_at=now + timedelta(hours=6),
        )

    def _infer_direction(self, question: str, series_id: str, change_pct: float) -> str:
        """Infer signal direction based on indicator type and market question.

        For rate markets: rising rates → YES for "will rates increase" type questions.
        For employment: falling unemployment → YES for "economy strong" type questions.
        """
        # Rate-related questions
        if series_id in ("FEDFUNDS", "DGS10", "DGS2"):
            if any(w in question for w in ["increase", "raise", "hike", "higher", "above"]):
                return "YES" if change_pct > 0 else "NO"
            if any(w in question for w in ["cut", "lower", "decrease", "below"]):
                return "YES" if change_pct < 0 else "NO"

        # Inflation questions
        if series_id == "CPIAUCSL":
            if any(w in question for w in ["increase", "rise", "above", "higher"]):
                return "YES" if change_pct > 0 else "NO"
            if any(w in question for w in ["decrease", "fall", "below", "lower"]):
                return "YES" if change_pct < 0 else "NO"

        # Unemployment
        if series_id == "UNRATE":
            if any(w in question for w in ["increase", "rise", "above", "higher"]):
                return "YES" if change_pct > 0 else "NO"
            if any(w in question for w in ["decrease", "fall", "below", "lower"]):
                return "YES" if change_pct < 0 else "NO"

        # Oil/commodities
        if series_id == "DCOILWTICO":
            if any(w in question for w in ["above", "increase", "rise", "higher"]):
                return "YES" if change_pct > 0 else "NO"
            if any(w in question for w in ["below", "decrease", "fall", "lower"]):
                return "YES" if change_pct < 0 else "NO"

        # Default: rising indicator = YES
        return "YES" if change_pct > 0 else "NO"

    def _load_cache(self) -> None:
        data = load_json(self._cache_path, {})
        self._series_cache = data.get("series", {})

    def _save_cache(self) -> None:
        try:
            atomic_json_write(self._cache_path, {
                "series": self._series_cache,
                "updated_at": utcnow().isoformat(),
            })
        except Exception as e:
            logger.warning("Failed to save FRED cache: %s", e)
