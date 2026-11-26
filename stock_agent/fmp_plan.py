"""FMP subscription plan detector with startup + daily re-probing.

Detects at startup (and re-probes every 24 hours, or immediately if `FMP_API_KEY` changes)
which `/stable/` capabilities the configured Financial Modeling Prep key permits:
- Full-universe quotes (`/stable/quote?symbol=ANET`) vs free-plan sample-only quotes (`AAPL`).
- Multi-symbol batch quotes (`/stable/batch-quote`).
- Quarterly consensus analyst estimates (`/stable/analyst-estimates?period=quarter`).
- Corporate press releases (`/stable/news/press-releases`).

Invariants:
- Refused endpoints (`401`/`402`/`403` or FMP restricted-plan error payloads) are suppressed at
  runtime so free-plan keys do not waste quota or log spam on restricted endpoints/tickers.
- Empty `200 []` or transient `429`/`5xx`/network responses classify as `"unknown"`, neither
  enabling paid-only endpoints nor falsely blocking baseline quote fallbacks.
- Never triggers `ProviderHealthRegistry.record_failure` during probing (avoiding false operator
  degradation alerts on free plans), while automatically clearing prior FMP `PLAN_BLOCKED`
  entries when a live key upgrade to Starter/Premium is detected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import logging
from typing import Any, Literal

from stock_agent.config import Config
from stock_agent.health import HealthState, ProviderHealthRegistry
from stock_agent.http_safety import DataHttpClient, scrub_secrets

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com"
DEFAULT_REPROBE_INTERVAL = timedelta(hours=24)
DEFAULT_INCOMPLETE_BACKOFF = timedelta(minutes=15)
DEFAULT_INCOMPLETE_BACKOFFS: tuple[timedelta, ...] = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=4),
    timedelta(hours=24),
)
DEFAULT_MAX_PROBE_CALLS_PER_DAY = 25
DEFAULT_PROBE_TIMEOUT_S = 3.0

CapabilityStatus = Literal["unknown", "allowed", "refused"]

# Core US tickers permitted by FMP's free-plan `/stable/quote` sample showcase.
FMP_FREE_SAMPLE_SYMBOLS: frozenset[str] = frozenset(
    {
        "AAPL",
        "AMZN",
        "GOOGL",
        "META",
        "MSFT",
        "NVDA",
        "SPY",
        "TSLA",
    }
)


@dataclass(frozen=True)
class FmpPlanCapabilities:
    quote_all_symbols: CapabilityStatus = "unknown"
    sample_quotes: CapabilityStatus = "unknown"
    batch_quotes: CapabilityStatus = "unknown"
    quarterly_estimates: CapabilityStatus = "unknown"
    press_releases: CapabilityStatus = "unknown"
    probed_at: datetime | None = None

    @property
    def quote_all_symbols_allowed(self) -> bool:
        return self.quote_all_symbols == "allowed"

    @property
    def sample_quotes_allowed(self) -> bool:
        return self.sample_quotes == "allowed"

    @property
    def batch_quotes_allowed(self) -> bool:
        return self.batch_quotes == "allowed"

    @property
    def quarterly_estimates_allowed(self) -> bool:
        return self.quarterly_estimates == "allowed"

    @property
    def press_releases_allowed(self) -> bool:
        return self.press_releases == "allowed"


def _is_fmp_error_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("Error Message", "error", "message", "Message"):
        val = payload.get(key)
        if isinstance(val, str) and any(
            tok in val.lower()
            for tok in (
                "restricted",
                "exclusive",
                "subscription",
                "upgrade",
                "not available",
                "special endpoint",
                "invalid api key",
                "legacy",
                "plan",
            )
        ):
            return True
    return False


def _classify_list_probe(status_code: int, payload: Any, *, require_price: bool = False) -> CapabilityStatus:
    if status_code in {401, 402, 403}:
        return "refused"
    if status_code == 429 or status_code >= 500 or status_code >= 400:
        return "unknown"
    if _is_fmp_error_payload(payload):
        return "refused"
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            if require_price and first.get("price") is None:
                return "unknown"
            return "allowed"
    return "unknown"


class FmpPlanDetector:
    """Tracks FMP plan capabilities across startup and daily re-probes."""

    def __init__(
        self,
        http: DataHttpClient,
        *,
        config: Any = Config,
        health: ProviderHealthRegistry | None = None,
        now: Callable[[], datetime] | None = None,
        reprobe_interval: timedelta = DEFAULT_REPROBE_INTERVAL,
        incomplete_backoff: timedelta = DEFAULT_INCOMPLETE_BACKOFF,
        incomplete_backoffs: tuple[timedelta, ...] | None = None,
        max_probe_calls_per_day: int = DEFAULT_MAX_PROBE_CALLS_PER_DAY,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    ) -> None:
        self.http = http
        self.config = config
        self.health = health
        self._now = now or (lambda: datetime.now(UTC))
        self.reprobe_interval = reprobe_interval
        self.incomplete_backoff = incomplete_backoff
        if incomplete_backoffs is not None:
            self.incomplete_backoffs = tuple(incomplete_backoffs) or (incomplete_backoff,)
        elif incomplete_backoff != DEFAULT_INCOMPLETE_BACKOFF:
            self.incomplete_backoffs = (
                incomplete_backoff,
                timedelta(hours=1),
                timedelta(hours=4),
                timedelta(hours=24),
            )
        else:
            self.incomplete_backoffs = DEFAULT_INCOMPLETE_BACKOFFS
        self.max_probe_calls_per_day = max(1, int(max_probe_calls_per_day))
        self.probe_timeout_s = max(0.1, float(probe_timeout_s))

        self._capabilities = FmpPlanCapabilities()
        self._refused_quote_symbols: set[str] = set()
        self._last_key_fp: str = ""
        self._retry_after: datetime | None = None
        self._incomplete_streak: int = 0
        self._probe_call_times: list[datetime] = []
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    @property
    def capabilities(self) -> FmpPlanCapabilities:
        return self._capabilities

    def _api_key(self) -> str:
        return str(getattr(self.config, "FMP_API_KEY", "") or "").strip()

    def _key_fingerprint(self) -> str:
        key = self._api_key()
        if not key:
            return ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def can_quote_symbol(self, symbol: str) -> bool:
        if not self._api_key() or not bool(getattr(self.config, "FMP_QUOTES_ENABLED", True)):
            return False
        sym = (symbol or "").strip().upper()
        if not sym or sym in self._refused_quote_symbols:
            return False
        caps = self._capabilities
        if caps.quote_all_symbols in {"allowed", "unknown"}:
            return True
        # quote_all_symbols == "refused": only try free-tier sample tickers if sample_quotes isn't refused
        if caps.sample_quotes == "refused":
            return False
        return sym in FMP_FREE_SAMPLE_SYMBOLS

    def batch_quotes_enabled(self) -> bool:
        if not self._api_key() or not bool(getattr(self.config, "FMP_QUOTES_ENABLED", True)):
            return False
        return self._capabilities.batch_quotes == "allowed"

    def quarterly_estimates_enabled(self) -> bool:
        if not self._api_key():
            return False
        return self._capabilities.quarterly_estimates == "allowed"

    def press_releases_enabled(self) -> bool:
        if not self._api_key():
            return False
        return self._capabilities.press_releases == "allowed"

    def record_quote_symbol_refused(self, symbol: str) -> None:
        sym = (symbol or "").strip().upper()
        if sym:
            self._refused_quote_symbols.add(sym)

    def record_endpoint_refused(
        self,
        capability: Literal["quote_all_symbols", "sample_quotes", "batch_quotes", "quarterly_estimates", "press_releases"],
    ) -> None:
        cur = self._capabilities
        self._capabilities = FmpPlanCapabilities(
            quote_all_symbols="refused" if capability == "quote_all_symbols" else cur.quote_all_symbols,
            sample_quotes="refused" if capability == "sample_quotes" else cur.sample_quotes,
            batch_quotes="refused" if capability == "batch_quotes" else cur.batch_quotes,
            quarterly_estimates="refused" if capability == "quarterly_estimates" else cur.quarterly_estimates,
            press_releases="refused" if capability == "press_releases" else cur.press_releases,
            probed_at=cur.probed_at,
        )

    def _prune_probe_window(self, at: datetime) -> None:
        self._probe_call_times = [
            ts for ts in self._probe_call_times if timedelta() <= (at - ts) < timedelta(hours=24)
        ]

    async def _probe_endpoint(
        self,
        path: str,
        params: dict[str, Any],
        *,
        at: datetime,
        require_price: bool = False,
        fallback: CapabilityStatus = "unknown",
    ) -> CapabilityStatus:
        self._prune_probe_window(at)
        if len(self._probe_call_times) >= self.max_probe_calls_per_day:
            logger.debug(
                "FMP plan probe %s skipped: daily probe cap (%d) reached",
                path,
                self.max_probe_calls_per_day,
            )
            return fallback
        self._probe_call_times.append(at)
        try:
            resp = await asyncio.wait_for(
                self.http.get(f"{FMP_BASE}{path}", params=params, timeout=self.probe_timeout_s),
                timeout=self.probe_timeout_s,
            )
        except Exception as exc:
            logger.debug("FMP plan probe %s failed transiently: %s", path, scrub_secrets(str(exc)))
            return "unknown"
        if resp.status_code in {401, 402, 403}:
            return "refused"
        if resp.status_code == 429 or resp.status_code >= 400:
            return "unknown"
        try:
            payload = resp.json()
        except ValueError:
            return "unknown"
        return _classify_list_probe(resp.status_code, payload, require_price=require_price)

    def _clear_fmp_plan_blocks(self, at: datetime) -> None:
        """Clear stale FMP PLAN_BLOCKED entries in ProviderHealthRegistry after a plan upgrade."""
        if self.health is None:
            return
        changed = False
        symbol_blocks = getattr(self.health, "_symbol_blocks", None)
        if isinstance(symbol_blocks, dict):
            for key in [k for k in symbol_blocks if isinstance(k, tuple) and len(k) == 3 and k[0] == "fmp"]:
                symbol_blocks.pop(key, None)
                changed = True
        statuses = getattr(self.health, "_statuses", None)
        if isinstance(statuses, dict):
            for (prov, cap), st in list(statuses.items()):
                if prov == "fmp" and getattr(st, "state", None) is HealthState.PLAN_BLOCKED:
                    self.health.record_ok("fmp", cap, at=at)
                    changed = True
        if changed:
            persist_fn = getattr(self.health, "_persist", None)
            if callable(persist_fn):
                persist_fn()

    async def probe(self, *, now: datetime | None = None) -> FmpPlanCapabilities:
        """Probe FMP endpoints to detect active plan capabilities safely."""
        async with self._get_lock():
            at = now or self._now()
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            key_fp = self._key_fingerprint()
            if not key_fp:
                self._capabilities = FmpPlanCapabilities(
                    quote_all_symbols="refused",
                    sample_quotes="refused",
                    batch_quotes="refused",
                    quarterly_estimates="refused",
                    press_releases="refused",
                    probed_at=at,
                )
                self._last_key_fp = ""
                self._incomplete_streak = 0
                self._retry_after = at + self.reprobe_interval
                return self._capabilities

            same_key = key_fp == self._last_key_fp
            if not same_key:
                self._incomplete_streak = 0

            self._prune_probe_window(at)
            if len(self._probe_call_times) >= self.max_probe_calls_per_day:
                if not same_key:
                    self._capabilities = FmpPlanCapabilities(probed_at=at)
                    self._refused_quote_symbols.clear()
                self._last_key_fp = key_fp
                next_slot = self._probe_call_times[0] + timedelta(hours=24)
                step_idx = min(self._incomplete_streak, len(self.incomplete_backoffs) - 1)
                self._retry_after = max(at + self.incomplete_backoffs[step_idx], next_slot)
                return self._capabilities

            # Stamp exponential retry_after (15m -> 1h -> 4h -> 24h) and key fingerprint
            # BEFORE network I/O so hanging or incomplete probes back off safely.
            step_idx = min(self._incomplete_streak, len(self.incomplete_backoffs) - 1)
            cur_backoff = self.incomplete_backoffs[step_idx]
            self._incomplete_streak += 1
            self._last_key_fp = key_fp
            self._retry_after = at + cur_backoff

            prev = self._capabilities
            quote_all = await self._probe_endpoint(
                "/stable/quote",
                {"symbol": "ANET"},
                at=at,
                require_price=True,
                fallback=prev.quote_all_symbols if same_key else "unknown",
            )
            if quote_all == "allowed":
                sample_q: CapabilityStatus = "allowed"
            elif quote_all == "refused":
                sample_q = await self._probe_endpoint(
                    "/stable/quote",
                    {"symbol": "AAPL"},
                    at=at,
                    require_price=True,
                    fallback=prev.sample_quotes if same_key else "unknown",
                )
            else:
                sample_q = "unknown"

            batch_q, est_q, press_q = await asyncio.gather(
                self._probe_endpoint(
                    "/stable/batch-quote",
                    {"symbols": "AAPL,ANET"},
                    at=at,
                    require_price=True,
                    fallback=prev.batch_quotes if same_key else "unknown",
                ),
                self._probe_endpoint(
                    "/stable/analyst-estimates",
                    {"symbol": "ANET", "period": "quarter", "limit": 1},
                    at=at,
                    fallback=prev.quarterly_estimates if same_key else "unknown",
                ),
                self._probe_endpoint(
                    "/stable/news/press-releases",
                    {"symbols": "AAPL,ANET", "limit": 5},
                    at=at,
                    fallback=prev.press_releases if same_key else "unknown",
                ),
            )

            updated = FmpPlanCapabilities(
                quote_all_symbols=quote_all,
                sample_quotes=sample_q,
                batch_quotes=batch_q,
                quarterly_estimates=est_q,
                press_releases=press_q,
                probed_at=at,
            )
            self._capabilities = updated
            complete = "unknown" not in {quote_all, sample_q, batch_q, est_q, press_q}
            if complete:
                self._incomplete_streak = 0
                self._retry_after = at + self.reprobe_interval
            else:
                self._retry_after = at + cur_backoff
                self._prune_probe_window(at)
                if len(self._probe_call_times) >= self.max_probe_calls_per_day:
                    self._retry_after = max(
                        self._retry_after, self._probe_call_times[0] + timedelta(hours=24)
                    )

            if quote_all == "allowed":
                self._refused_quote_symbols.clear()
            if (
                quote_all == "allowed"
                or batch_q == "allowed"
                or est_q == "allowed"
                or press_q == "allowed"
            ):
                self._clear_fmp_plan_blocks(at)

            if (
                prev.quote_all_symbols != updated.quote_all_symbols
                or prev.batch_quotes != updated.batch_quotes
                or prev.quarterly_estimates != updated.quarterly_estimates
                or prev.press_releases != updated.press_releases
            ):
                logger.info(
                    "FMP plan probe: quote_all=%s sample_quotes=%s batch_quotes=%s "
                    "quarterly_estimates=%s press_releases=%s",
                    updated.quote_all_symbols,
                    updated.sample_quotes,
                    updated.batch_quotes,
                    updated.quarterly_estimates,
                    updated.press_releases,
                )
            return updated

    async def maybe_reprobe(self, now: datetime | None = None) -> FmpPlanCapabilities:
        """Re-probe if never probed, if `FMP_API_KEY` changed, or once `reprobe_interval` elapsed."""
        at = now or self._now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        cur_fp = self._key_fingerprint()
        if not cur_fp:
            return self._capabilities
        if (
            self._retry_after is None
            or cur_fp != self._last_key_fp
            or at >= self._retry_after
        ):
            return await self.probe(now=at)
        return self._capabilities
