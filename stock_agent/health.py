"""Provider and input health registry (Decision A12).

Tracks per-provider/endpoint health, consecutive failures, HTTP 402/403 plan-block
suppression (persisted across restarts via StateStore.kv so blocked endpoints are
not retried every loop), per-symbol quote/news freshness, and ResearchGate's
`research_entry_blockers(symbol, now)` contract.

Supports `defer_load=True` and lazy `load_state()` so constructing the singleton
before `StateStore.open()` never reads SQLite prematurely.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from stock_agent.config import Config
from stock_agent.http_safety import scrub_secrets
from stock_agent.research_schemas import utc_datetime
from stock_agent.state_store import StateStore, StateUnavailable

logger = logging.getLogger(__name__)

_HTTP_STATUS_RE = re.compile(r"\b(401|402|403|404|422|429|5\d\d)\b")
_KV_HEALTH_KEY = "health:provider_registry:v1"

DEFAULT_PLAN_BLOCK_TTL = timedelta(hours=24)
DEFAULT_QUOTE_MAX_AGE_S = 300.0
DEFAULT_NEWS_MAX_AGE_S = 900.0
UNHEALTHY_FAILURE_THRESHOLD = 3


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    PLAN_BLOCKED = "plan_blocked"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InputHealthStatus:
    provider: str
    capability: str
    state: HealthState
    required_for_trading: bool
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    last_status_code: int | None = None
    last_error_summary: str | None = None
    blocked_until: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.capability}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capability": self.capability,
            "state": self.state.value,
            "required_for_trading": self.required_for_trading,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "consecutive_failures": self.consecutive_failures,
            "last_status_code": self.last_status_code,
            "last_error_summary": self.last_error_summary,
            "blocked_until": self.blocked_until.isoformat() if self.blocked_until else None,
        }


@dataclass(frozen=True)
class SymbolFreshness:
    symbol: str
    as_of: datetime
    source: str
    fresh: bool = True


TransitionCallback = Callable[[InputHealthStatus, HealthState], Any]

_REQUIRED_CAPABILITIES: set[tuple[str, str]] = {
    ("alpaca", "broker"),
    ("prices", "quotes"),
    ("news", "poll"),
    ("llm", "research"),
}


def _parse_iso(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return utc_datetime(raw)
    except ValueError:
        return None


class ProviderHealthRegistry:
    """Central input-health registry queried by ResearchGate, services, and briefs."""

    def __init__(
        self,
        config: Any = Config,
        *,
        store: StateStore | None = None,
        defer_load: bool = False,
        now: Callable[[], datetime] | None = None,
        plan_block_ttl: timedelta = DEFAULT_PLAN_BLOCK_TTL,
        quote_max_age_s: float = DEFAULT_QUOTE_MAX_AGE_S,
        news_max_age_s: float = DEFAULT_NEWS_MAX_AGE_S,
        on_transition: TransitionCallback | None = None,
        llm_route_healthy: Callable[[datetime], bool] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._now = now or (lambda: datetime.now(UTC))
        self.plan_block_ttl = plan_block_ttl
        self.quote_max_age_s = float(quote_max_age_s)
        self.news_max_age_s = float(news_max_age_s)
        self._listeners: list[TransitionCallback] = []
        if on_transition is not None:
            self._listeners.append(on_transition)
        self._llm_route_healthy = llm_route_healthy

        self._statuses: dict[tuple[str, str], InputHealthStatus] = {}
        self._symbol_blocks: dict[tuple[str, str, str], datetime] = {}
        self._quotes: dict[str, SymbolFreshness] = {}
        self._news: dict[str, SymbolFreshness] = {}
        self._state_loaded: bool = store is None

        self._seed_defaults()
        if not defer_load:
            self.load_state()

    def register_listener(self, callback: TransitionCallback) -> None:
        """Register a state-transition listener for alerts_reporting."""
        self._listeners.append(callback)

    def _seed_defaults(self) -> None:
        alpaca_configured = bool(
            getattr(self.config, "ALPACA_API_KEY", "") and getattr(self.config, "ALPACA_SECRET_KEY", "")
        )
        finnhub_configured = bool(getattr(self.config, "FINNHUB_API_KEY", ""))
        fmp_configured = bool(getattr(self.config, "FMP_API_KEY", ""))
        tipranks_configured = any(
            bool(getattr(self.config, attr, ""))
            for attr in ("TIPRANKS_API_KEY", "TIPRANKS_API_TOKEN")
        ) or bool(
            getattr(self.config, "TIPRANKS_EMAIL", "") and getattr(self.config, "TIPRANKS_PASSWORD", "")
        )
        llm_configured = any(
            bool(getattr(self.config, attr, ""))
            for attr in ("PERPLEXITY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
        )

        defaults: list[tuple[str, str, bool, bool]] = [
            ("alpaca", "broker", True, alpaca_configured),
            ("alpaca", "quotes", True, alpaca_configured),
            ("alpaca", "bars", False, alpaca_configured),
            ("alpaca", "news", True, alpaca_configured),
            ("prices", "quotes", True, alpaca_configured or finnhub_configured or fmp_configured),
            ("news", "poll", True, alpaca_configured or finnhub_configured),
            ("llm", "research", True, llm_configured),
            ("finnhub", "company_news", False, finnhub_configured),
            ("finnhub", "earnings_calendar", False, finnhub_configured),
            ("finnhub", "fundamentals", False, finnhub_configured),
            ("fmp", "quotes", False, fmp_configured),
            ("fmp", "fundamentals", False, fmp_configured),
            ("fmp", "screener", False, fmp_configured),
            ("sec", "edgar", False, True),
            ("tipranks", "screener", False, tipranks_configured),
        ]
        for provider, capability, required, enabled in defaults:
            state = HealthState.UNKNOWN if enabled else HealthState.DISABLED
            self._statuses[(provider, capability)] = InputHealthStatus(
                provider=provider,
                capability=capability,
                state=state,
                required_for_trading=required,
            )

    def load_state(self) -> bool:
        """Hydrate persisted plan-blocks and statuses from StateStore once open."""
        if self.store is None:
            self._state_loaded = True
            return True
        try:
            raw = self.store.kv_get(_KV_HEALTH_KEY)
        except StateUnavailable:
            return False
        self._state_loaded = True
        if not isinstance(raw, dict):
            return True
        now = utc_datetime(self._now())
        for item in raw.get("statuses", ()):
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider", "")).strip()
            capability = str(item.get("capability", "")).strip()
            if not provider or not capability:
                continue
            blocked_until = _parse_iso(item.get("blocked_until"))
            state_raw = str(item.get("state", HealthState.UNKNOWN.value))
            try:
                state = HealthState(state_raw)
            except ValueError:
                state = HealthState.UNKNOWN
            if state is HealthState.PLAN_BLOCKED and (blocked_until is None or blocked_until <= now):
                state = HealthState.UNKNOWN
                blocked_until = None
            existing = self._statuses.get((provider, capability))
            required = bool(
                item.get(
                    "required_for_trading",
                    existing.required_for_trading
                    if existing
                    else (provider, capability) in _REQUIRED_CAPABILITIES,
                )
            )
            if existing and existing.state is HealthState.DISABLED:
                continue
            if existing and (existing.last_ok_at is not None or existing.last_error_at is not None):
                continue
            self._statuses[(provider, capability)] = InputHealthStatus(
                provider=provider,
                capability=capability,
                state=state,
                required_for_trading=required,
                last_ok_at=_parse_iso(item.get("last_ok_at")),
                last_error_at=_parse_iso(item.get("last_error_at")),
                consecutive_failures=int(item.get("consecutive_failures", 0) or 0),
                last_status_code=item.get("last_status_code"),
                last_error_summary=item.get("last_error_summary"),
                blocked_until=blocked_until,
            )

        sym_blocks = raw.get("symbol_blocks", {})
        if isinstance(sym_blocks, dict):
            for composite, until_raw in sym_blocks.items():
                parts = str(composite).split(":", 2)
                until = _parse_iso(until_raw)
                if len(parts) == 3 and until is not None and until > now:
                    self._symbol_blocks.setdefault((parts[0], parts[1], parts[2]), until)
        return True

    _load_persisted = load_state

    def _ensure_loaded(self) -> None:
        if not self._state_loaded:
            self.load_state()

    def _expire_if_due(self, key: tuple[str, str], now_utc: datetime) -> InputHealthStatus | None:
        st = self._statuses.get(key)
        if st is None:
            return None
        if st.state is HealthState.PLAN_BLOCKED and st.blocked_until is not None and st.blocked_until <= now_utc:
            restored = HealthState.HEALTHY if st.last_ok_at is not None else HealthState.UNKNOWN
            updated = InputHealthStatus(
                provider=st.provider,
                capability=st.capability,
                state=restored,
                required_for_trading=st.required_for_trading,
                last_ok_at=st.last_ok_at,
                last_error_at=st.last_error_at,
                consecutive_failures=0,
                last_status_code=st.last_status_code,
                last_error_summary=st.last_error_summary,
                blocked_until=None,
            )
            self._statuses[key] = updated
            return updated
        return st

    def _persist(self) -> None:
        if self.store is None:
            return
        self._ensure_loaded()
        now = utc_datetime(self._now())
        active_symbol_blocks = {
            f"{p}:{c}:{s}": until.isoformat()
            for (p, c, s), until in list(self._symbol_blocks.items())
            if until > now
        }
        if len(active_symbol_blocks) > 500:
            sorted_items = sorted(active_symbol_blocks.items(), key=lambda kv: kv[1], reverse=True)[:500]
            active_symbol_blocks = dict(sorted_items)
        payload = {
            "statuses": [st.to_dict() for st in self._statuses.values()],
            "symbol_blocks": active_symbol_blocks,
        }
        try:
            self.store.kv_set(_KV_HEALTH_KEY, payload)
        except StateUnavailable:
            return
        except Exception as exc:
            logger.warning("Failed to persist provider health registry: %s", scrub_secrets(str(exc)))

    def _emit_if_changed(self, previous: HealthState, updated: InputHealthStatus) -> None:
        if previous == updated.state:
            return
        for cb in tuple(self._listeners):
            try:
                cb(updated, previous)
            except Exception as exc:
                logger.warning("Health transition callback error: %s", scrub_secrets(str(exc)))

    def is_required(self, provider: str, capability: str) -> bool:
        existing = self._statuses.get((provider, capability))
        if existing is not None:
            return existing.required_for_trading
        return (provider, capability) in _REQUIRED_CAPABILITIES

    def is_blocked(
        self,
        provider: str,
        capability: str,
        symbol: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return True when endpoint or specific symbol is plan-blocked (402/403) or disabled."""
        self._ensure_loaded()
        at = utc_datetime(now or self._now())
        st = self._expire_if_due((provider, capability), at)
        if st is not None:
            if st.state is HealthState.DISABLED:
                return True
            if st.state is HealthState.PLAN_BLOCKED:
                if st.blocked_until is None or st.blocked_until > at:
                    return True
        if symbol:
            sym = symbol.strip().upper()
            until = self._symbol_blocks.get((provider, capability, sym))
            if until is not None:
                if until > at:
                    return True
                self._symbol_blocks.pop((provider, capability, sym), None)
        return False

    def record_ok(
        self,
        provider: str,
        capability: str,
        *,
        required: bool | None = None,
        at: datetime | None = None,
    ) -> InputHealthStatus:
        """Record a successful provider/capability call."""
        self._ensure_loaded()
        ts = utc_datetime(at or self._now())
        key = (provider, capability)
        prev = self._statuses.get(key)
        prev_state = prev.state if prev else HealthState.UNKNOWN
        req = prev.required_for_trading if (prev and required is None) else bool(
            required if required is not None else key in _REQUIRED_CAPABILITIES
        )
        updated = InputHealthStatus(
            provider=provider,
            capability=capability,
            state=HealthState.HEALTHY,
            required_for_trading=req,
            last_ok_at=ts,
            last_error_at=prev.last_error_at if prev else None,
            consecutive_failures=0,
            last_status_code=200,
            last_error_summary=None,
            blocked_until=None,
        )
        self._statuses[key] = updated
        if prev_state is HealthState.PLAN_BLOCKED or (prev and prev.consecutive_failures > 0):
            self._persist()
        self._emit_if_changed(prev_state, updated)
        return updated

    def record_failure(
        self,
        provider: str,
        capability: str,
        *,
        status_code: int | None = None,
        summary: str = "",
        symbol: str | None = None,
        required: bool | None = None,
        force_unhealthy: bool = False,
        at: datetime | None = None,
    ) -> InputHealthStatus:
        """Record a failed call; HTTP 402/403 triggers plan-block suppression."""
        self._ensure_loaded()
        ts = utc_datetime(at or self._now())
        key = (provider, capability)
        prev = self._statuses.get(key)
        prev_state = prev.state if prev else HealthState.UNKNOWN
        req = prev.required_for_trading if (prev and required is None) else bool(
            required if required is not None else key in _REQUIRED_CAPABILITIES
        )
        clean_summary = scrub_secrets(summary)[:240] if summary else (
            f"HTTP {status_code}" if status_code is not None else "provider error"
        )
        failures = (prev.consecutive_failures if prev else 0) + 1
        blocked_until: datetime | None = None

        if status_code in (402, 403):
            until = ts + self.plan_block_ttl
            if symbol:
                # Symbol-scoped 402/403 blocks ONLY that symbol so healthy unrelated symbols continue!
                sym = symbol.strip().upper()
                self._symbol_blocks[(provider, capability, sym)] = until
                new_state = HealthState.DEGRADED
                blocked_until = None
            else:
                new_state = HealthState.PLAN_BLOCKED
                blocked_until = until
        elif status_code == 401 or force_unhealthy:
            new_state = HealthState.UNHEALTHY
        else:
            new_state = (
                HealthState.UNHEALTHY
                if failures >= UNHEALTHY_FAILURE_THRESHOLD
                else HealthState.DEGRADED
            )

        updated = InputHealthStatus(
            provider=provider,
            capability=capability,
            state=new_state,
            required_for_trading=req,
            last_ok_at=prev.last_ok_at if prev else None,
            last_error_at=ts,
            consecutive_failures=failures,
            last_status_code=status_code,
            last_error_summary=clean_summary,
            blocked_until=blocked_until,
        )
        self._statuses[key] = updated
        if status_code in (401, 402, 403) or new_state != prev_state:
            self._persist()
        self._emit_if_changed(prev_state, updated)
        return updated

    def report(
        self,
        provider: str,
        capability: str,
        ok: bool,
        error: str | None = None,
        *,
        status_code: int | None = None,
        symbol: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """`HealthReport` callback compatible with PriceService and AssetRegistry."""
        if ok:
            self.record_ok(provider, capability, at=at)
            return
        inferred_code = status_code
        if inferred_code is None and error:
            match = _HTTP_STATUS_RE.search(error)
            if match:
                inferred_code = int(match.group(1))
        self.record_failure(
            provider,
            capability,
            status_code=inferred_code,
            summary=error or "",
            symbol=symbol,
            at=at,
        )

    # ------------------------------------------------- Domain convenience hooks

    def record_quote(
        self,
        symbol: str,
        as_of: datetime,
        *,
        source: str,
        fresh: bool = True,
        now: datetime | None = None,
    ) -> None:
        sym = symbol.strip().upper()
        quote_ts = utc_datetime(as_of)
        observed = utc_datetime(now or self._now())
        self._quotes[sym] = SymbolFreshness(symbol=sym, as_of=quote_ts, source=source, fresh=fresh)
        if fresh:
            self.record_ok("prices", "quotes", required=True, at=observed)

    def record_news_poll(
        self,
        symbols: Iterable[str],
        as_of: datetime,
        *,
        source: str,
        ok: bool = True,
        degraded: bool = False,
        include_market: bool = False,
    ) -> None:
        poll_ts = utc_datetime(as_of)
        sym_list = [str(raw).strip().upper() for raw in symbols if raw and str(raw).strip()]
        if not ok:
            for sym in sym_list:
                self._news.pop(sym, None)
            self.record_failure(
                "news",
                "poll",
                summary="all news providers failed",
                required=True,
                force_unhealthy=True,
                at=poll_ts,
            )
            return

        if degraded:
            prev = self._statuses.get(("news", "poll"))
            prev_state = prev.state if prev else HealthState.UNKNOWN
            updated = InputHealthStatus(
                provider="news",
                capability="poll",
                state=HealthState.DEGRADED,
                required_for_trading=True,
                last_ok_at=poll_ts,
                last_error_at=prev.last_error_at if prev else None,
                consecutive_failures=0,
                last_status_code=200,
                last_error_summary="primary news degraded; secondary succeeded",
            )
            self._statuses[("news", "poll")] = updated
            self._emit_if_changed(prev_state, updated)
        else:
            self.record_ok("news", "poll", required=True, at=poll_ts)

        for sym in sym_list:
            self._news[sym] = SymbolFreshness(symbol=sym, as_of=poll_ts, source=source, fresh=True)

    def record_broker_ok(self, *, at: datetime | None = None) -> InputHealthStatus:
        return self.record_ok("alpaca", "broker", required=True, at=at)

    def record_broker_failure(
        self,
        *,
        summary: str = "broker check failed",
        status_code: int | None = None,
        at: datetime | None = None,
    ) -> InputHealthStatus:
        return self.record_failure(
            "alpaca",
            "broker",
            status_code=status_code,
            summary=summary,
            required=True,
            force_unhealthy=True,
            at=at,
        )

    def record_llm_ok(self, provider: str, *, at: datetime | None = None) -> InputHealthStatus:
        self.record_ok(f"llm_{provider}", "route", required=False, at=at)
        return self.record_ok("llm", "research", required=True, at=at)

    def record_llm_failure(
        self,
        provider: str,
        *,
        summary: str = "",
        status_code: int | None = None,
        at: datetime | None = None,
    ) -> InputHealthStatus:
        return self.record_failure(
            f"llm_{provider}",
            "route",
            status_code=status_code,
            summary=summary,
            required=False,
            at=at,
        )

    def _has_healthy_llm_route(self, now_utc: datetime) -> bool:
        if self._llm_route_healthy is not None:
            return bool(self._llm_route_healthy(now_utc))
        overall = self._expire_if_due(("llm", "research"), now_utc)
        if overall and overall.state in (
            HealthState.UNHEALTHY,
            HealthState.DISABLED,
            HealthState.PLAN_BLOCKED,
        ):
            return False

        provider_key_attrs = (
            ("perplexity", "PERPLEXITY_API_KEY"),
            ("openai", "OPENAI_API_KEY"),
            ("anthropic", "ANTHROPIC_API_KEY"),
        )
        configured = [
            prov for prov, attr in provider_key_attrs if bool(getattr(self.config, attr, ""))
        ]
        if not configured:
            return bool(overall and overall.state is HealthState.HEALTHY)

        for prov in configured:
            st = self._expire_if_due((f"llm_{prov}", "route"), now_utc)
            if st is None:
                return True
            if st.state in (HealthState.HEALTHY, HealthState.UNKNOWN):
                return True
        return False

    # ------------------------------------------------- ResearchGate A12 contract

    def research_entry_blockers(
        self,
        symbol: str,
        now: datetime | None = None,
        *,
        quote_max_age_s: float | None = None,
        news_max_age_s: float | None = None,
    ) -> tuple[str, ...]:
        """Return A12 entry blockers for `symbol` at `now` (empty tuple = healthy).

        Required inputs for a NEW ENTRY (Decision A12):
        1. Verified healthy broker (`alpaca:broker` observed OK)
        2. Fresh positive quote for `symbol`
        3. Fresh healthy news poll explicitly covering `symbol`
        4. At least one healthy configured LLM route
        """
        self._ensure_loaded()
        now_utc = utc_datetime(now or self._now())
        sym = symbol.strip().upper()
        max_q_age = self.quote_max_age_s if quote_max_age_s is None else float(quote_max_age_s)
        max_n_age = self.news_max_age_s if news_max_age_s is None else float(news_max_age_s)
        blockers: list[str] = []

        broker = self._expire_if_due(("alpaca", "broker"), now_utc)
        if broker is None or broker.state is not HealthState.HEALTHY:
            blockers.append("broker_unhealthy")

        quote_stamp = self._quotes.get(sym)
        if (
            quote_stamp is None
            or not quote_stamp.fresh
            or (now_utc - quote_stamp.as_of).total_seconds() > max_q_age
        ):
            blockers.append("quote_missing_or_stale")

        news_status = self._expire_if_due(("news", "poll"), now_utc)
        sym_news = self._news.get(sym)
        if (
            (
                news_status is not None
                and news_status.state
                in (HealthState.UNHEALTHY, HealthState.PLAN_BLOCKED, HealthState.DISABLED)
            )
            or sym_news is None
            or not sym_news.fresh
            or (now_utc - sym_news.as_of).total_seconds() > max_n_age
        ):
            blockers.append("news_missing_or_stale")

        if not self._has_healthy_llm_route(now_utc):
            blockers.append("llm_unhealthy")

        return tuple(blockers)

    # ------------------------------------------------- Reporting & inspection

    def status(self, provider: str, capability: str) -> InputHealthStatus | None:
        self._ensure_loaded()
        now_utc = utc_datetime(self._now())
        return self._expire_if_due((provider, capability), now_utc)

    def get_input_health(self) -> dict[str, InputHealthStatus]:
        """Return snapshot keyed by 'provider:capability' for alerts_reporting."""
        self._ensure_loaded()
        now_utc = utc_datetime(self._now())
        for key in list(self._statuses):
            self._expire_if_due(key, now_utc)
        return {st.key: st for st in self._statuses.values()}

    def summary_rows(self) -> list[dict[str, Any]]:
        """Return deterministic list of status dicts for morning brief and status dumps."""
        self.get_input_health()
        ordered = sorted(
            self._statuses.values(),
            key=lambda s: (not s.required_for_trading, s.provider, s.capability),
        )
        return [st.to_dict() for st in ordered]

    def format_brief_lines(self) -> list[str]:
        """Human-readable summary lines for Discord/Telegram morning brief."""
        self.get_input_health()
        lines: list[str] = []
        for st in sorted(
            self._statuses.values(),
            key=lambda s: (not s.required_for_trading, s.provider, s.capability),
        ):
            tier = "req" if st.required_for_trading else "opt"
            detail = (
                f" ({st.last_error_summary})"
                if st.last_error_summary and st.state is not HealthState.HEALTHY
                else ""
            )
            lines.append(f"[{tier}] {st.provider}.{st.capability}: {st.state.value}{detail}")
        return lines


InputHealthRegistry = ProviderHealthRegistry
