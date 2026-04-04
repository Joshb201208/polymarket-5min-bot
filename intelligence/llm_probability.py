"""Universal Signal 3: LLM-Powered Probability Estimation.

Uses an LLM (Claude or OpenAI-compatible) to estimate the probability of
any market question resolving YES, then compares to the current market price.

Features:
- Works for literally ANY market question
- Caches results for 4-6 hours to control API costs
- Rate-limited to avoid excessive spend
- Falls back gracefully if no API key is configured
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from intelligence.sports_filter import is_sports_market

logger = logging.getLogger("intelligence.llm_probability")

# Cache configuration
_LLM_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL_SECONDS", str(5 * 3600)))  # 5 hours default
_LLM_CACHE: dict[str, tuple[dict, float]] = {}  # {cache_key: (result, monotonic_time)}

# Rate limiting
_MAX_LLM_CALLS_PER_CYCLE = int(os.getenv("LLM_MAX_CALLS_PER_CYCLE", "15"))
_MIN_CALL_INTERVAL = 2.0  # seconds between calls
_last_call_time: float = 0.0

# Persistent cache on disk
_project_root = Path(__file__).resolve().parent.parent
try:
    _CACHE_DIR = Path("/root/polymarket-bot/data") if Path("/root/polymarket-bot/data").exists() else _project_root / "data"
except (PermissionError, OSError):
    _CACHE_DIR = _project_root / "data"
_CACHE_FILE = _CACHE_DIR / "llm_probability_cache.json"


def _cache_key(question: str, yes_price: float) -> str:
    """Generate a cache key from question + price bucket."""
    # Bucket price to nearest 5% to avoid cache misses on tiny price changes
    price_bucket = round(yes_price * 20) / 20
    raw = f"{question.strip().lower()}|{price_bucket:.2f}"
    return hashlib.md5(raw.encode()).hexdigest()


def _load_disk_cache() -> None:
    """Load persistent cache from disk into memory."""
    global _LLM_CACHE
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            now = time.monotonic()
            # Disk cache stores wall-clock timestamps; convert to monotonic
            wall_now = time.time()
            for key, entry in data.items():
                wall_ts = entry.get("wall_time", 0)
                age = wall_now - wall_ts
                if age < _LLM_CACHE_TTL:
                    # Convert to monotonic time
                    _LLM_CACHE[key] = (entry.get("result", {}), now - age)
            logger.info("Loaded %d LLM cache entries from disk", len(_LLM_CACHE))
    except Exception as e:
        logger.warning("Failed to load LLM disk cache: %s", e)


def _save_disk_cache() -> None:
    """Save in-memory cache to disk for persistence across restarts."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        now_mono = time.monotonic()
        now_wall = time.time()
        data = {}
        for key, (result, mono_ts) in _LLM_CACHE.items():
            age = now_mono - mono_ts
            if age < _LLM_CACHE_TTL:
                data[key] = {
                    "result": result,
                    "wall_time": now_wall - age,
                }
        _CACHE_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to save LLM disk cache: %s", e)


# Load cache at import time
_load_disk_cache()


def _get_api_config() -> tuple[str, str, str] | None:
    """Get LLM API configuration from environment.

    Supports:
    - ANTHROPIC_API_KEY → Claude API
    - OPENAI_API_KEY → OpenAI-compatible API

    Returns (api_key, base_url, model) or None if not configured.
    """
    anthropic_key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        return (
            anthropic_key,
            "https://api.anthropic.com/v1/messages",
            os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"),
        )

    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        return (
            openai_key,
            "https://api.openai.com/v1/chat/completions",
            os.getenv("LLM_MODEL", "gpt-4o-mini"),
        )

    return None


async def _call_claude(
    api_key: str,
    model: str,
    question: str,
    yes_price: float,
    end_date: str,
    client: httpx.AsyncClient,
) -> dict | None:
    """Call Claude API for probability estimation."""
    prompt = f"""You are a calibrated prediction market analyst. Estimate the probability that the following market question resolves YES.

Market question: {question}
Current market price (YES): {yes_price:.1%}
Market end date: {end_date}
Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Analyze this step by step:
1. What is the base rate for this type of event?
2. What current evidence supports YES or NO?
3. How does the timeframe affect the probability?
4. Is the current market price reasonable?

Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{"probability": 0.XX, "confidence": 0.XX, "direction": "YES or NO", "reasoning": "one sentence"}}

Where:
- probability: your calibrated estimate (0.0 to 1.0) that the market resolves YES
- confidence: how confident you are in your estimate (0.0 to 1.0)
- direction: whether you'd bet YES or NO given the current price
"""

    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )

        if resp.status_code == 429:
            logger.warning("LLM rate limited — backing off")
            return None
        if resp.status_code != 200:
            logger.warning("LLM API error: %d %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "")
        return _parse_llm_response(text)

    except Exception as e:
        logger.warning("Claude API call failed: %s", e)
        return None


async def _call_openai(
    api_key: str,
    base_url: str,
    model: str,
    question: str,
    yes_price: float,
    end_date: str,
    client: httpx.AsyncClient,
) -> dict | None:
    """Call OpenAI-compatible API for probability estimation."""
    prompt = f"""You are a calibrated prediction market analyst. Estimate the probability that the following market question resolves YES.

Market question: {question}
Current market price (YES): {yes_price:.1%}
Market end date: {end_date}
Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Respond with ONLY a JSON object:
{{"probability": 0.XX, "confidence": 0.XX, "direction": "YES or NO", "reasoning": "one sentence"}}
"""

    try:
        resp = await client.post(
            base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.3,
            },
            timeout=30,
        )

        if resp.status_code == 429:
            logger.warning("LLM rate limited — backing off")
            return None
        if resp.status_code != 200:
            logger.warning("LLM API error: %d", resp.status_code)
            return None

        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return _parse_llm_response(text)

    except Exception as e:
        logger.warning("OpenAI API call failed: %s", e)
        return None


def _parse_llm_response(text: str) -> dict | None:
    """Parse LLM JSON response into structured result."""
    try:
        # Try to extract JSON from the response
        # Handle cases where LLM wraps in markdown code blocks
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"```(?:json)?\s*", "", text)
            text = text.rstrip("`").strip()

        result = json.loads(text)

        probability = float(result.get("probability", 0))
        confidence = float(result.get("confidence", 0))
        direction = str(result.get("direction", "")).upper()
        reasoning = str(result.get("reasoning", ""))

        # Validate
        if not (0 <= probability <= 1):
            return None
        if direction not in ("YES", "NO"):
            direction = "YES" if probability > 0.5 else "NO"

        confidence = max(0.0, min(1.0, confidence))

        return {
            "probability": probability,
            "confidence": confidence,
            "direction": direction,
            "reasoning": reasoning,
        }

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse LLM response: %s — text: %s", e, text[:100])
        return None


class LLMProbabilityEstimator:
    """Uses LLM to estimate probabilities for any market."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._calls_this_cycle = 0

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan markets using LLM probability estimation.

        Rate-limited and cached to control costs.
        """
        if not os.getenv("LLM_PROBABILITY_ENABLED", "true").lower() == "true":
            return []

        api_config = _get_api_config()
        if api_config is None:
            logger.debug("LLM probability module disabled — no API key configured")
            return []

        api_key, base_url, model = api_config

        signals: list[Signal] = []
        self._calls_this_cycle = 0

        # Sort markets by liquidity (highest first) to prioritize important markets
        sorted_markets = sorted(
            active_markets,
            key=lambda m: getattr(m, "liquidity", 0),
            reverse=True,
        )

        async with httpx.AsyncClient() as client:
            for market in sorted_markets:
                if self._calls_this_cycle >= _MAX_LLM_CALLS_PER_CYCLE:
                    logger.info("LLM call limit reached (%d) — stopping", _MAX_LLM_CALLS_PER_CYCLE)
                    break

                try:
                    signal = await self._evaluate_market(
                        market, api_key, base_url, model, client,
                    )
                    if signal:
                        signals.append(signal)
                except Exception as e:
                    logger.warning("LLM evaluation failed: %s", e)

        # Persist cache to disk after each cycle
        _save_disk_cache()

        logger.info(
            "LLM probability scan: %d signals, %d API calls, %d cache hits",
            len(signals), self._calls_this_cycle,
            len(active_markets) - self._calls_this_cycle,
        )
        return signals

    async def _evaluate_market(
        self,
        market,
        api_key: str,
        base_url: str,
        model: str,
        client: httpx.AsyncClient,
    ) -> Signal | None:
        """Evaluate a single market using LLM."""
        global _last_call_time

        if is_sports_market(market):
            logger.debug("Skipping sports market: %s", getattr(market, "id", ""))
            return None

        question = getattr(market, "question", "")
        market_id = getattr(market, "id", str(market))
        end_date = getattr(market, "end_date", "")
        prices = getattr(market, "outcome_prices", [])

        if not question or len(prices) < 2:
            return None

        yes_price = float(prices[0])
        if yes_price <= 0.02 or yes_price >= 0.98:
            return None  # Already decided

        # Check cache
        key = _cache_key(question, yes_price)
        if key in _LLM_CACHE:
            cached_result, cached_at = _LLM_CACHE[key]
            if time.monotonic() - cached_at < _LLM_CACHE_TTL:
                return self._result_to_signal(cached_result, market_id, question, yes_price)

        # Rate limit
        now = time.monotonic()
        elapsed = now - _last_call_time
        if elapsed < _MIN_CALL_INTERVAL:
            await __import__("asyncio").sleep(_MIN_CALL_INTERVAL - elapsed)

        # Call LLM
        _last_call_time = time.monotonic()
        self._calls_this_cycle += 1

        if "anthropic" in base_url:
            result = await _call_claude(api_key, model, question, yes_price, end_date, client)
        else:
            result = await _call_openai(api_key, base_url, model, question, yes_price, end_date, client)

        if result is None:
            return None

        # Cache the result
        _LLM_CACHE[key] = (result, time.monotonic())

        return self._result_to_signal(result, market_id, question, yes_price)

    def _result_to_signal(
        self,
        result: dict,
        market_id: str,
        question: str,
        yes_price: float,
    ) -> Signal | None:
        """Convert LLM result to a Signal if edge is significant."""
        probability = result["probability"]
        confidence = result["confidence"]
        reasoning = result.get("reasoning", "")

        # Compare LLM estimate to market price
        deviation = probability - yes_price

        # Minimum deviation to generate signal
        min_deviation = 0.06

        if abs(deviation) < min_deviation:
            return None  # LLM agrees with market — no edge

        if deviation > 0:
            # LLM thinks YES is more likely than market → bet YES
            direction = "YES"
        else:
            # LLM thinks YES is less likely than market → bet NO
            direction = "NO"

        # Strength scales with deviation magnitude
        strength = min(0.85, 0.35 + abs(deviation) * 1.5)

        # Scale confidence with LLM's own confidence
        adj_confidence = min(0.80, confidence * 0.7 + abs(deviation) * 0.5)

        return Signal(
            source="llm_probability",
            market_id=market_id,
            market_question=question,
            signal_type="llm_estimate",
            direction=direction,
            strength=round(strength, 3),
            confidence=round(adj_confidence, 3),
            details={
                "llm_probability": round(probability, 3),
                "market_price": round(yes_price, 3),
                "deviation": round(deviation, 3),
                "llm_confidence": round(confidence, 3),
                "reasoning": reasoning[:200],
            },
        )
