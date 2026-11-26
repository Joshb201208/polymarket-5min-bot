"""Read-only, broker-neutral trial and data-quality measurements.

Produces the seven diagnostics used to judge paid data and research tools
without altering any trading, risk, research, or reconciliation behaviour:

1. Research cost per validated decision (from ``research_usage`` and ``theses``).
2. Share of scheduled reviews with dated primary (issuer/SEC) evidence.
3. Share of entry theses ending ``INSUFFICIENT_EVIDENCE`` by job.
4. Stock quote coverage (fresh quote rate during regular US equity hours).
5. Share of option legs with a fresh two-sided price.
6. Count of quote-caused option rejections.
7. Per-trade commissions and slippage where the broker reports them.

Fail-closed rules:
- Undated items, future-dated items, XBRL ratio rollups, blank snippets, and
  bare SEC ``-index.htm`` index stubs never count as dated primary evidence.
- When a broker omits commission fields on fills (for example Alpaca), fees are
  reported as ``None`` (``not_reported_by_broker``), never ``$0.00``.
- Slippage is computed only when both a fill price and an arrival/reference/mid
  price exist; trades missing either benchmark are counted separately.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from stock_agent.market_calendar import MarketStatus

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

STOCK_QUOTES_KV_KEY = "trial_metrics.stock_quotes.v1"
OPTION_LEG_QUOTES_KV_KEY = "trial_metrics.option_leg_quotes.v1"
_KV_RETENTION_DAYS = 14

SCHEDULED_JOBS: frozenset[str] = frozenset({"prescan", "weekly", "midweek", "market_move"})
CANONICAL_ENTRY_JOBS: tuple[str, ...] = (
    "prescan",
    "weekly",
    "midweek",
    "market_move",
    "news",
    "earnings",
)

_OFFICIAL_WIRE_DOMAINS: tuple[str, ...] = (
    "businesswire.com",
    "prnewswire.com",
    "globenewswire.com",
    "accesswire.com",
    "newsfilecorp.com",
    "eqs-news.com",
)
_OFFICIAL_WIRE_PUBLISHERS: frozenset[str] = frozenset({
    "business wire",
    "businesswire",
    "pr newswire",
    "prnewswire",
    "globenewswire",
    "globe newswire",
    "accesswire",
    "newsfile",
    "sec edgar",
    "sec",
})
_REGULATOR_DOMAINS: tuple[str, ...] = (
    "sec.gov",
    "fda.gov",
    "ftc.gov",
    "justice.gov",
    "bis.gov",
    "commerce.gov",
    "federalregister.gov",
    "treasury.gov",
    "federalreserve.gov",
)
_ISSUER_IR_HOST_PREFIXES: tuple[str, ...] = (
    "ir.",
    "investor.",
    "investors.",
    "newsroom.",
    "press.",
)
_ISSUER_IR_PATH_TOKENS: tuple[str, ...] = (
    "/investor-relations",
    "/investors/",
    "/investor/",
    "/newsroom/",
    "/press-release",
    "/press-releases",
    "/financial-releases",
    "/earnings-release",
)
_API_DOC_URL_MARKERS: tuple[str, ...] = (
    "finnhub.io/docs/",
    "financialmodelingprep.com/developer/docs",
    "news.alpaca.invalid/",
)
_PRIMARY_EVIDENCE_ID_PREFIXES: tuple[str, ...] = (
    "fmp:press",
    "fmp:release",
    "fmp:transcript",
    "issuer:",
    "press_release:",
    "transcript:",
    "sec:filing:",
    "edgar:",
)
_EXPLICIT_FEE_ACTIVITY_TYPES: frozenset[str] = frozenset({
    "FEE",
    "CFEE",
    "REG",
    "TAF",
    "ORF",
    "CAT",
})
_QUOTE_OPTION_REASON_SUBSTRINGS: tuple[str, ...] = (
    "no quote",
    "no_quote",
    "missing quote",
    "missing_quote",
    "no usable quote",
    "no usable option quote",
    "no ask",
    "no_ask",
    "no bid",
    "no_bid",
    "crossed quote",
    "crossed_quote",
    "quote has no timestamp",
    "stale quote",
    "stale_quote",
    "delayed quote",
    "delayed_quote",
    "market_data_type",
    "realtime option leg evidence expired",
    "quote_stale_or_unavailable",
    "bid-ask spread",
    "wide spread",
    "wide_spread",
    "spread too wide",
    "spread has no positive debit",
    "underlying quote",
    "underlying moved",
    "no underlying price",
    "option_quote_",
    "cross_leg_skew",
    "unpriced",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_iso(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return _as_utc(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    txt = raw.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return _as_utc(dt)


def _et_day_iso(raw_ts: Any) -> str | None:
    dt = _parse_iso(raw_ts)
    if dt is not None:
        return dt.astimezone(ET).date().isoformat()
    if isinstance(raw_ts, str) and len(raw_ts) >= 10:
        head = raw_ts[:10]
        try:
            return date.fromisoformat(head).isoformat()
        except ValueError:
            return None
    return None


def _finite_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _store_ready(store: Any) -> bool:
    if store is None:
        return False
    # StateStore raises StateUnavailable when _conn is None (before open or after close).
    if hasattr(store, "_conn") and getattr(store, "_conn") is None:
        return False
    return True


def _store_in_tx(store: Any) -> bool:
    conn = getattr(store, "_conn", None)
    return bool(conn is not None and getattr(conn, "in_transaction", False))


def _has_table(store: Any, name: str) -> bool:
    if not _store_ready(store) or not hasattr(store, "query"):
        return False
    try:
        rows = store.query(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return bool(rows)
    except Exception:
        return False


def is_regular_hours_open(
    now: datetime,
    *,
    calendar: Any = None,
    market_open: bool | None = None,
) -> bool:
    """True only during regular US equity session hours (09:30–16:00 ET, open session).

    Fail-closed against off-hours/weekend/holiday noise: if ``calendar`` knows
    the session is closed, returns ``False``; if ``calendar`` is unknown or
    absent, requires ``market_open is True`` AND weekday 09:30 <= ET < 16:00.
    """
    now_et = _as_utc(now).astimezone(ET)
    if now_et.weekday() >= 5:
        return False
    if calendar is not None:
        try:
            sess = calendar.session_on(now_et.date()) if hasattr(calendar, "session_on") else None
            if sess is not None:
                opens_at = getattr(sess, "opens_at", None)
                closes_at = getattr(sess, "closes_at", None)
                if isinstance(opens_at, datetime) and isinstance(closes_at, datetime):
                    if not (_as_utc(opens_at) <= _as_utc(now) < _as_utc(closes_at)):
                        return False
            state = getattr(calendar, "state", None)
            status = getattr(state, "status", None)
            if status is MarketStatus.CLOSED:
                return False
            if status is MarketStatus.OPEN and sess is not None:
                return True
        except Exception:
            pass
    if market_open is False:
        return False
    t = now_et.time()
    if not (time(9, 30) <= t < time(16, 0)):
        return False
    return market_open is not False


def _prune_day_buckets(buckets: Mapping[str, Any], anchor_day: date) -> dict[str, Any]:
    floor_iso = (anchor_day - timedelta(days=_KV_RETENTION_DAYS)).isoformat()
    return {
        str(k): dict(v)
        for k, v in buckets.items()
        if isinstance(k, str) and len(k) == 10 and k >= floor_iso and isinstance(v, Mapping)
    }


# ---------------------------------------------------------------------------
# Passive quote telemetry hooks (stock quotes & option leg snapshots)
# ---------------------------------------------------------------------------
def record_stock_quote_batch(
    store: Any,
    now: datetime,
    *,
    requested: int,
    fresh: int,
    missing_reasons: Mapping[str, str] | None = None,
    in_memory_sink: dict[str, Any] | None = None,
) -> None:
    """Passively record one regular-hours stock quote batch; never raises."""
    if requested <= 0:
        return
    fresh_clamped = max(0, min(int(fresh), int(requested)))
    req_count = int(requested)
    day_et = _as_utc(now).astimezone(ET).date()
    day_iso = day_et.isoformat()

    reason_counts: Counter[str] = Counter()
    if missing_reasons:
        for reason in missing_reasons.values():
            key = str(reason or "missing").strip()[:80] or "missing"
            reason_counts[key] += 1

    if in_memory_sink is not None:
        cur = in_memory_sink.setdefault(day_iso, {"requested": 0, "fresh": 0, "missing_by_reason": {}})
        cur["requested"] = int(cur.get("requested", 0)) + req_count
        cur["fresh"] = int(cur.get("fresh", 0)) + fresh_clamped
        mbr = dict(cur.get("missing_by_reason") or {})
        for r_key, r_cnt in reason_counts.items():
            mbr[r_key] = int(mbr.get(r_key, 0)) + r_cnt
        cur["missing_by_reason"] = mbr

    if not _store_ready(store) or _store_in_tx(store):
        return
    kv_get = getattr(store, "kv_get", None)
    kv_set = getattr(store, "kv_set", None)
    if not (callable(kv_get) and callable(kv_set)):
        return
    try:
        raw = kv_get(STOCK_QUOTES_KV_KEY, {})
        buckets = _prune_day_buckets(raw if isinstance(raw, Mapping) else {}, day_et)
        entry = dict(buckets.get(day_iso) or {})
        entry["requested"] = int(entry.get("requested", 0)) + req_count
        entry["fresh"] = int(entry.get("fresh", 0)) + fresh_clamped
        mbr = dict(entry.get("missing_by_reason") or {})
        for r_key, r_cnt in reason_counts.items():
            mbr[r_key] = int(mbr.get(r_key, 0)) + r_cnt
        entry["missing_by_reason"] = mbr
        buckets[day_iso] = entry
        kv_set(STOCK_QUOTES_KV_KEY, buckets)
    except Exception:
        logger.debug("Stock quote metric persistence skipped", exc_info=True)


def is_fresh_two_sided_option_snapshot(
    snap: Any,
    now: datetime,
    *,
    max_quote_age_s: int = 300,
) -> bool:
    """Broker-neutral check for a fresh two-sided option leg snapshot.

    Supports both Alpaca option snapshots (``latestQuote`` ``bp``/``ap``/``t``)
    and IBKR option snapshots (``latestQuote`` + ``ibkr_meta`` gateway clocks
    and ``market_data_type``). Delayed/frozen market data types (3 or 4),
    missing/zero bids or asks, crossed quotes, missing timestamps, and quotes
    older than ``max_quote_age_s`` fail closed.
    """
    if not isinstance(snap, Mapping):
        return False
    quote = snap.get("latestQuote")
    if not isinstance(quote, Mapping):
        return False
    bid = _finite_float(quote.get("bp") if "bp" in quote else quote.get("bid"))
    ask = _finite_float(quote.get("ap") if "ap" in quote else quote.get("ask"))
    if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or ask < bid:
        return False

    now_utc = _as_utc(now)
    meta = snap.get("ibkr_meta")
    if isinstance(meta, Mapping):
        mdt = meta.get("market_data_type")
        if isinstance(mdt, int) and not isinstance(mdt, bool) and mdt in {3, 4}:
            return False
        bid_at = _parse_iso(meta.get("bid_gateway_observed_at"))
        ask_at = _parse_iso(meta.get("ask_gateway_observed_at"))
        if bid_at is not None and ask_at is not None:
            for side_ts in (bid_at, ask_at):
                age_s = (now_utc - side_ts).total_seconds()
                if age_s < -5.0 or age_s > max_quote_age_s:
                    return False
            return True

    quote_at = _parse_iso(quote.get("t") or quote.get("timestamp") or quote.get("quote_at"))
    if quote_at is None:
        return False
    age_s = (now_utc - quote_at).total_seconds()
    return -5.0 <= age_s <= max_quote_age_s


def record_option_leg_quotes(
    store: Any,
    now: datetime,
    *,
    requested_symbols: Sequence[str],
    snapshots: Mapping[str, Any] | None,
    max_quote_age_s: int = 300,
    in_memory_sink: dict[str, Any] | None = None,
) -> None:
    """Passively record option leg quote freshness across requested contracts."""
    unique_syms = [str(s).strip() for s in dict.fromkeys(requested_symbols) if str(s).strip()]
    if not unique_syms:
        return
    snaps = snapshots if isinstance(snapshots, Mapping) else {}
    checked = len(unique_syms)
    fresh = sum(
        1
        for sym in unique_syms
        if is_fresh_two_sided_option_snapshot(snaps.get(sym), now, max_quote_age_s=max_quote_age_s)
    )
    day_et = _as_utc(now).astimezone(ET).date()
    day_iso = day_et.isoformat()

    if in_memory_sink is not None:
        cur = in_memory_sink.setdefault(day_iso, {"legs_checked": 0, "fresh_two_sided": 0})
        cur["legs_checked"] = int(cur.get("legs_checked", 0)) + checked
        cur["fresh_two_sided"] = int(cur.get("fresh_two_sided", 0)) + fresh

    if not _store_ready(store) or _store_in_tx(store):
        return
    kv_get = getattr(store, "kv_get", None)
    kv_set = getattr(store, "kv_set", None)
    if not (callable(kv_get) and callable(kv_set)):
        return
    try:
        raw = kv_get(OPTION_LEG_QUOTES_KV_KEY, {})
        buckets = _prune_day_buckets(raw if isinstance(raw, Mapping) else {}, day_et)
        entry = dict(buckets.get(day_iso) or {})
        entry["legs_checked"] = int(entry.get("legs_checked", 0)) + checked
        entry["fresh_two_sided"] = int(entry.get("fresh_two_sided", 0)) + fresh
        buckets[day_iso] = entry
        kv_set(OPTION_LEG_QUOTES_KV_KEY, buckets)
    except Exception:
        logger.debug("Option leg quote metric persistence skipped", exc_info=True)


def _resolve_max_option_quote_age_s(config: Any) -> int:
    try:
        from stock_agent.options_settings import load_options_settings

        return int(load_options_settings(config).max_quote_age_s)
    except Exception:
        return 300


def install_broker_quote_observer(
    broker: Any,
    *,
    store: Any,
    config: Any = None,
    calendar: Any = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Idempotently wrap ``broker.get_option_snapshots`` to observe leg quotes.

    Broker-neutral: works for both ``AlpacaClient`` and ``IbkrClient`` without
    altering return values, exceptions, or signatures (including ``feed=...``).
    """
    if broker is None or getattr(broker, "_trial_quote_observer_installed", False):
        return
    orig = getattr(broker, "get_option_snapshots", None)
    if not callable(orig):
        return
    now_fn = clock or _utc_now

    @functools.wraps(orig)
    async def _observed(symbols: list[str], *args: Any, **kwargs: Any) -> Any:
        result = await orig(symbols, *args, **kwargs)
        try:
            now_obs = now_fn()
            # Record during regular session hours (or when no calendar is wired in tests).
            if calendar is None or is_regular_hours_open(now_obs, calendar=calendar, market_open=True):
                record_option_leg_quotes(
                    store,
                    now_obs,
                    requested_symbols=symbols,
                    snapshots=result if isinstance(result, Mapping) else {},
                    max_quote_age_s=_resolve_max_option_quote_age_s(config),
                )
        except Exception:
            logger.debug("Option snapshot observer skipped", exc_info=True)
        return result

    try:
        setattr(broker, "get_option_snapshots", _observed)
        setattr(broker, "_trial_quote_observer_installed", True)
    except Exception:
        logger.debug("Could not attach option quote observer on broker", exc_info=True)


# ---------------------------------------------------------------------------
# Metric 2: Dated primary (issuer / SEC) evidence classification
# ---------------------------------------------------------------------------
def is_dated_primary_evidence(item: Mapping[str, Any], *, as_of: datetime | None = None) -> bool:
    """True only when ``item`` is dated, non-blank primary (issuer/SEC) evidence.

    Fail-closed exclusions:
    - Missing, unparseable, or future ``published_at`` relative to ``as_of``.
    - Blank snippet (or snippet that merely echoes URL/title on a blank web page).
    - ``sec:xbrl:`` / ``/api/xbrl/companyconcept/`` accounting ratio rollups.
    - Bare SEC EDGAR ``-index.htm`` / ``-index.html`` links whose snippet is only
      the one-line ``SEC EDGAR Form ... filed on ...`` index stub.
    - API documentation placeholder URLs (Finnhub / FMP docs, ``.invalid``).
    """
    if not isinstance(item, Mapping):
        return False
    pub_at = _parse_iso(item.get("published_at"))
    if pub_at is None:
        return False
    if as_of is not None and pub_at > _as_utc(as_of) + timedelta(minutes=5):
        return False

    ev_id = str(item.get("evidence_id") or "").strip().lower()
    url = str(item.get("url") or "").strip()
    url_lower = url.lower()
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    publisher = str(item.get("publisher") or "").strip().lower()

    if not snippet or snippet.lower() in {url_lower, title.lower()}:
        return False
    if ev_id.startswith("sec:xbrl:") or "/api/xbrl/companyconcept/" in url_lower:
        return False
    if any(marker in url_lower for marker in _API_DOC_URL_MARKERS):
        return False

    try:
        parsed = urlparse(url_lower)
        host = (parsed.hostname or "").lstrip(".")
        path = parsed.path or ""
    except ValueError:
        host, path = "", ""

    is_edgar_index = host.endswith("sec.gov") and (
        path.endswith("-index.htm") or path.endswith("-index.html") or path.endswith("/index.json")
    )
    if is_edgar_index:
        # A bare index wrapper with only the form/date stub is NOT filing text.
        stub_prefix = snippet.lower().startswith("sec edgar form ")
        if stub_prefix and len(snippet) < 220:
            return False

    if ev_id.startswith(_PRIMARY_EVIDENCE_ID_PREFIXES):
        return True

    if host and any(host == dom or host.endswith("." + dom) for dom in _REGULATOR_DOMAINS):
        if host.endswith("sec.gov"):
            if "/archives/edgar/data/" in path or "/litigation/" in path or "/news/press-release" in path:
                return True
            # Other sec.gov pages (generic search/docs) require explicit filing text.
            return not is_edgar_index and ("form " in snippet.lower() or "item " in snippet.lower())
        return True

    if host and any(host == dom or host.endswith("." + dom) for dom in _OFFICIAL_WIRE_DOMAINS):
        return True

    if publisher in _OFFICIAL_WIRE_PUBLISHERS and not is_edgar_index:
        return True

    if host and (
        host.startswith(_ISSUER_IR_HOST_PREFIXES)
        or any(tok in path for tok in _ISSUER_IR_PATH_TOKENS)
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Metric 6: Quote-caused option rejection classification
# ---------------------------------------------------------------------------
def is_quote_caused_option_reason(reason: Any) -> bool:
    """True when an option scan/mark/close/order rejection was caused by missing,
    stale, one-sided, crossed, or wide quotes."""
    if not isinstance(reason, str) or not reason.strip():
        return False
    low = reason.strip().lower()
    if any(sub in low for sub in _QUOTE_OPTION_REASON_SUBSTRINGS):
        return True
    if "quote is " in low and "s old" in low:
        return True
    if low.startswith("spread ") and "% > max" in low:
        return True
    return False


# ---------------------------------------------------------------------------
# Core read-only collectors
# ---------------------------------------------------------------------------
def _collect_research_and_evidence(
    store: Any,
    session_iso: str,
    start_7d_iso: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    def _empty_cost() -> dict[str, Any]:
        return {
            "spend_usd": 0.0,
            "validated_decisions": 0,
            "grounded_decisions": 0,
            "cost_per_validated_decision_usd": None,
            "cost_per_grounded_decision_usd": None,
        }

    def _empty_primary() -> dict[str, Any]:
        return {
            "scheduled_reviews": 0,
            "with_dated_primary": 0,
            "with_dated_primary_cited": 0,
            "share_pct": None,
            "cited_share_pct": None,
        }

    def _empty_ie() -> dict[str, Any]:
        return {
            "by_job": {},
            "scheduled": {"total": 0, "insufficient_evidence": 0, "share_pct": None},
            "overall": {"total": 0, "insufficient_evidence": 0, "share_pct": None},
        }

    if not _store_ready(store) or not hasattr(store, "query"):
        return (
            {"session": _empty_cost(), "rolling_7d": _empty_cost()},
            {"session": _empty_primary(), "rolling_7d": _empty_primary()},
            {"session": _empty_ie(), "rolling_7d": _empty_ie()},
        )

    spend_day_micro = 0
    spend_7d_micro = 0
    if _has_table(store, "research_usage"):
        try:
            floor_utc = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            for row in store.query(
                "SELECT COALESCE(settled_microusd, reserved_microusd) AS charge, created_at "
                "FROM research_usage WHERE created_at >= ?",
                (floor_utc,),
            ):
                charge = int(row["charge"] or 0)
                r_day = _et_day_iso(row["created_at"])
                if r_day is None or r_day < start_7d_iso or r_day > session_iso:
                    continue
                spend_7d_micro += charge
                if r_day == session_iso:
                    spend_day_micro += charge
        except Exception:
            logger.debug("Could not query research_usage for trial metrics", exc_info=True)

    buckets = {
        "session": {
            "validated": 0,
            "grounded": 0,
            "sched_total": 0,
            "sched_primary": 0,
            "sched_primary_cited": 0,
            "jobs": {},  # job -> [total, ie]
        },
        "rolling_7d": {
            "validated": 0,
            "grounded": 0,
            "sched_total": 0,
            "sched_primary": 0,
            "sched_primary_cited": 0,
            "jobs": {},
        },
    }

    if _has_table(store, "theses"):
        try:
            floor_utc = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            rows = store.query(
                "SELECT role, source, decision_id, created_at, generated_at, payload_json "
                "FROM theses WHERE created_at >= ?",
                (floor_utc,),
            )
            for row in rows:
                source = str(row["source"] or "")
                decision_id = str(row["decision_id"] or "")
                # Exclude legacy non-model imports and audit-only shadow runs.
                if "/" not in source or decision_id.startswith("shadow:"):
                    continue
                ts_day = _et_day_iso(row["generated_at"] or row["created_at"])
                if ts_day is None or ts_day < start_7d_iso or ts_day > session_iso:
                    continue
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except ValueError:
                    continue
                if not isinstance(payload, Mapping):
                    continue

                role = str(row["role"] or "")
                draft = payload.get("draft") if isinstance(payload.get("draft"), Mapping) else payload
                direction = str(
                    (draft.get("direction") if isinstance(draft, Mapping) else None)
                    or payload.get("verdict")
                    or ""
                ).upper()
                is_ie = direction == "INSUFFICIENT_EVIDENCE"
                job = str(payload.get("job") or "").strip().lower()
                if not job and ":" in decision_id:
                    job = decision_id.split(":", 1)[0].strip().lower()

                as_of_dt = _parse_iso(
                    payload.get("inputs_as_of")
                    or (payload.get("inputs") or {}).get("AS_OF")
                    if isinstance(payload.get("inputs"), Mapping)
                    else payload.get("inputs_as_of")
                ) or _parse_iso(row["generated_at"] or row["created_at"])

                raw_ev = payload.get("evidence")
                if not isinstance(raw_ev, list):
                    inp = payload.get("inputs")
                    raw_ev = inp.get("evidence") if isinstance(inp, Mapping) else []
                ev_list = [e for e in (raw_ev or []) if isinstance(e, Mapping)]
                primary_ids = {
                    str(e.get("evidence_id") or "")
                    for e in ev_list
                    if is_dated_primary_evidence(e, as_of=as_of_dt)
                }
                has_primary = bool(primary_ids)
                cited_ids: set[str] = set()
                if isinstance(draft, Mapping) and isinstance(draft.get("claims"), list):
                    for claim in draft["claims"]:
                        if isinstance(claim, Mapping) and isinstance(claim.get("evidence_ids"), list):
                            cited_ids.update(str(cid) for cid in claim["evidence_ids"] if cid)
                has_primary_cited = bool(primary_ids & cited_ids)

                targets = ["rolling_7d"]
                if ts_day == session_iso:
                    targets.append("session")
                for win in targets:
                    b = buckets[win]
                    b["validated"] += 1
                    if not is_ie:
                        b["grounded"] += 1
                    if job in SCHEDULED_JOBS and role in {"entry_candidate", "held_review", "position_review"}:
                        b["sched_total"] += 1
                        if has_primary:
                            b["sched_primary"] += 1
                        if has_primary_cited:
                            b["sched_primary_cited"] += 1
                    if role == "entry_candidate":
                        j_key = job or "unknown"
                        counts = b["jobs"].setdefault(j_key, [0, 0])
                        counts[0] += 1
                        if is_ie:
                            counts[1] += 1
        except Exception:
            logger.debug("Could not query theses for trial metrics", exc_info=True)

    def _build_cost(spend_micro: int, b: dict[str, Any]) -> dict[str, Any]:
        spend_usd = round(spend_micro / 1_000_000.0, 4)
        val_n = int(b["validated"])
        grd_n = int(b["grounded"])
        return {
            "spend_usd": spend_usd,
            "validated_decisions": val_n,
            "grounded_decisions": grd_n,
            "cost_per_validated_decision_usd": round(spend_usd / val_n, 4) if val_n > 0 else None,
            "cost_per_grounded_decision_usd": round(spend_usd / grd_n, 4) if grd_n > 0 else None,
        }

    def _build_primary(b: dict[str, Any]) -> dict[str, Any]:
        tot = int(b["sched_total"])
        prim = int(b["sched_primary"])
        cited = int(b["sched_primary_cited"])
        return {
            "scheduled_reviews": tot,
            "with_dated_primary": prim,
            "with_dated_primary_cited": cited,
            "share_pct": _pct(prim, tot),
            "cited_share_pct": _pct(cited, tot),
        }

    def _build_ie(b: dict[str, Any]) -> dict[str, Any]:
        jobs_map: dict[str, list[int]] = b["jobs"]
        ordered_keys = [k for k in CANONICAL_ENTRY_JOBS if k in jobs_map] + sorted(
            k for k in jobs_map if k not in CANONICAL_ENTRY_JOBS
        )
        by_job: dict[str, dict[str, Any]] = {}
        sched_tot = sched_ie = all_tot = all_ie = 0
        for k in ordered_keys:
            tot, ie = jobs_map[k]
            by_job[k] = {
                "total": tot,
                "insufficient_evidence": ie,
                "share_pct": _pct(ie, tot),
            }
            all_tot += tot
            all_ie += ie
            if k in SCHEDULED_JOBS:
                sched_tot += tot
                sched_ie += ie
        return {
            "by_job": by_job,
            "scheduled": {
                "total": sched_tot,
                "insufficient_evidence": sched_ie,
                "share_pct": _pct(sched_ie, sched_tot),
            },
            "overall": {
                "total": all_tot,
                "insufficient_evidence": all_ie,
                "share_pct": _pct(all_ie, all_tot),
            },
        }

    cost_out = {
        "session": _build_cost(spend_day_micro, buckets["session"]),
        "rolling_7d": _build_cost(spend_7d_micro, buckets["rolling_7d"]),
    }
    primary_out = {
        "session": _build_primary(buckets["session"]),
        "rolling_7d": _build_primary(buckets["rolling_7d"]),
    }
    ie_out = {
        "session": _build_ie(buckets["session"]),
        "rolling_7d": _build_ie(buckets["rolling_7d"]),
    }
    return cost_out, primary_out, ie_out


def _collect_stock_quote_coverage(
    store: Any,
    price_service: Any,
    snapshot: Any,
    session_iso: str,
    start_7d_iso: str,
) -> dict[str, Any]:
    buckets: dict[str, Any] = {}
    if _store_ready(store) and hasattr(store, "kv_get"):
        try:
            raw = store.kv_get(STOCK_QUOTES_KV_KEY, {})
            if isinstance(raw, Mapping):
                buckets = {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}
        except Exception:
            buckets = {}
    if not buckets and price_service is not None:
        mem = getattr(price_service, "_trial_quote_stats", None)
        if isinstance(mem, Mapping):
            buckets = {str(k): v for k, v in mem.items() if isinstance(v, Mapping)}

    day_entry = buckets.get(session_iso) if isinstance(buckets.get(session_iso), Mapping) else {}
    day_req = int(day_entry.get("requested", 0) or 0)
    day_fresh = int(day_entry.get("fresh", 0) or 0)
    day_missing = {
        str(k): int(v)
        for k, v in (day_entry.get("missing_by_reason") or {}).items()
        if int(v or 0) > 0
    }

    req_7d = fresh_7d = 0
    for d_key, val in buckets.items():
        if start_7d_iso <= d_key <= session_iso and isinstance(val, Mapping):
            req_7d += int(val.get("requested", 0) or 0)
            fresh_7d += int(val.get("fresh", 0) or 0)

    holdings = getattr(snapshot, "holdings", None) if snapshot is not None else None
    hold_total = len(holdings) if isinstance(holdings, Sequence) else 0
    hold_priced = (
        sum(1 for h in holdings if getattr(h, "price_ok", False) is True)
        if isinstance(holdings, Sequence)
        else 0
    )

    return {
        "session": {
            "requested": day_req,
            "fresh": day_fresh,
            "fresh_rate_pct": _pct(day_fresh, day_req),
            "missing_by_reason": day_missing,
            "snapshot_holdings_total": hold_total,
            "snapshot_holdings_priced": hold_priced,
        },
        "rolling_7d": {
            "requested": req_7d,
            "fresh": fresh_7d,
            "fresh_rate_pct": _pct(fresh_7d, req_7d),
        },
    }


def _collect_option_leg_coverage(
    store: Any,
    snapshot: Any,
    session_iso: str,
    start_7d_iso: str,
    max_quote_age_s: int,
) -> dict[str, Any]:
    buckets: dict[str, Any] = {}
    if _store_ready(store) and hasattr(store, "kv_get"):
        try:
            raw = store.kv_get(OPTION_LEG_QUOTES_KV_KEY, {})
            if isinstance(raw, Mapping):
                buckets = {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}
        except Exception:
            buckets = {}

    day_entry = buckets.get(session_iso) if isinstance(buckets.get(session_iso), Mapping) else {}
    day_checked = int(day_entry.get("legs_checked", 0) or 0)
    day_fresh = int(day_entry.get("fresh_two_sided", 0) or 0)

    checked_7d = fresh_7d = 0
    for d_key, val in buckets.items():
        if start_7d_iso <= d_key <= session_iso and isinstance(val, Mapping):
            checked_7d += int(val.get("legs_checked", 0) or 0)
            fresh_7d += int(val.get("fresh_two_sided", 0) or 0)

    # Fallback/supplement from IBKR/desk opt_orders.leg_quotes_json when passive KV has no observations yet.
    if checked_7d == 0 and _has_table(store, "opt_orders"):
        try:
            floor_utc = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            for row in store.query(
                "SELECT prepared_at, quote_grade, leg_quotes_json FROM opt_orders WHERE prepared_at >= ?",
                (floor_utc,),
            ):
                p_day = _et_day_iso(row["prepared_at"])
                prep_dt = _parse_iso(row["prepared_at"])
                if p_day is None or prep_dt is None or not (start_7d_iso <= p_day <= session_iso):
                    continue
                grade = str(row["quote_grade"] or "").lower()
                lq = json.loads(row["leg_quotes_json"] or "{}")
                if not isinstance(lq, Mapping):
                    continue
                for vals in lq.values():
                    bid = ask = None
                    q_at = prep_dt
                    if isinstance(vals, Sequence) and not isinstance(vals, (str, bytes)) and len(vals) >= 2:
                        bid, ask = _finite_float(vals[0]), _finite_float(vals[1])
                        if len(vals) >= 4:
                            parsed_q = _parse_iso(vals[3])
                            if parsed_q is not None:
                                q_at = parsed_q
                    elif isinstance(vals, Mapping):
                        bid = _finite_float(vals.get("bid") if "bid" in vals else vals.get("bp"))
                        ask = _finite_float(vals.get("ask") if "ask" in vals else vals.get("ap"))
                        q_at = _parse_iso(vals.get("quote_at") or vals.get("t")) or prep_dt
                    age_ok = q_at is not None and -5.0 <= (prep_dt - q_at).total_seconds() <= max_quote_age_s
                    ok = (
                        grade == "realtime"
                        and bid is not None
                        and ask is not None
                        and bid > 0.0
                        and ask >= bid
                        and age_ok
                    )
                    checked_7d += 1
                    if ok:
                        fresh_7d += 1
                    if p_day == session_iso:
                        day_checked += 1
                        if ok:
                            day_fresh += 1
        except Exception:
            logger.debug("Could not inspect opt_orders.leg_quotes_json", exc_info=True)

    legs = getattr(snapshot, "legs", None) if snapshot is not None else None
    snap_total = len(legs) if isinstance(legs, Sequence) else 0
    snap_priced = (
        sum(1 for lg in legs if getattr(lg, "price_ok", False) is True)
        if isinstance(legs, Sequence)
        else 0
    )

    return {
        "session": {
            "legs_checked": day_checked,
            "fresh_two_sided": day_fresh,
            "fresh_two_sided_share_pct": _pct(day_fresh, day_checked),
            "snapshot_legs_total": snap_total,
            "snapshot_legs_priced": snap_priced,
        },
        "rolling_7d": {
            "legs_checked": checked_7d,
            "fresh_two_sided": fresh_7d,
            "fresh_two_sided_share_pct": _pct(fresh_7d, checked_7d),
        },
    }


_JOURNAL_TRIAL_KINDS: frozenset[str] = frozenset(
    {
        "options.scan_skip",
        "options.mark_rejected",
        "options.preopen_veto",
        "options.open_skipped",
        "options.desk_stress",
        "options.close_stopped",
        "options.exit_submitted",
    }
)


def _iter_journal_since(journal: Any, since_utc: datetime) -> list[dict[str, Any]]:
    if journal is None:
        return []
    try:
        if hasattr(journal, "read"):
            return [
                ev
                for ev in journal.read(kinds=set(_JOURNAL_TRIAL_KINDS), since=since_utc)
                if isinstance(ev, Mapping)
            ]
        if hasattr(journal, "read_since"):
            return [ev for ev in journal.read_since(since_utc) if isinstance(ev, Mapping)]
    except Exception:
        logger.debug("Could not read journal for trial metrics", exc_info=True)
    return []


def _load_option_structures_safe(store: Any, options_store: Any) -> list[Any]:
    if options_store is not None and hasattr(options_store, "load"):
        try:
            loaded = options_store.load()
            if isinstance(loaded, tuple) and loaded and isinstance(loaded[0], list):
                return list(loaded[0])
            if isinstance(loaded, list):
                return loaded
        except Exception:
            pass
    if _store_ready(store) and hasattr(store, "kv_get"):
        try:
            raw = store.kv_get("options.book.v1")
            if isinstance(raw, Mapping) and isinstance(raw.get("structures"), list):
                return list(raw["structures"])
        except Exception:
            pass
    return []


def _collect_quote_option_rejections(
    store: Any,
    journal_events: Sequence[Mapping[str, Any]],
    structures: Sequence[Any],
    session_iso: str,
    start_7d_iso: str,
) -> dict[str, Any]:
    def _new_bucket() -> dict[str, Any]:
        return {
            "total": 0,
            "scan_skips": 0,
            "mark_rejections": 0,
            "close_quote_failures": 0,
            "order_rejections": 0,
            "by_reason": Counter(),
        }

    wins = {"session": _new_bucket(), "rolling_7d": _new_bucket()}

    def _bump(day_str: str | None, field: str, reason_key: str, count: int = 1) -> None:
        if day_str is None or not (start_7d_iso <= day_str <= session_iso):
            return
        for w_name in ("rolling_7d", "session") if day_str == session_iso else ("rolling_7d",):
            b = wins[w_name]
            b[field] += count
            b["total"] += count
            b["by_reason"][reason_key] += count

    for ev in journal_events:
        ev_day = _et_day_iso(ev.get("ts"))
        kind = str(ev.get("kind") or "")
        if kind == "options.scan_skip":
            reason = str(ev.get("reason") or "")
            if is_quote_caused_option_reason(reason):
                _bump(ev_day, "scan_skips", reason[:80])
        elif kind == "options.mark_rejected":
            probs = ev.get("problems")
            first_prob = (
                next((str(v) for v in probs.values() if v), "mark_rejected")
                if isinstance(probs, Mapping) and probs
                else "mark_rejected"
            )
            _bump(ev_day, "mark_rejections", f"mark: {first_prob}"[:80])
        elif kind in {"options.preopen_veto", "options.open_skipped", "options.desk_stress"}:
            reason = str(ev.get("reason") or ev.get("why") or ev.get("message") or "")
            if is_quote_caused_option_reason(reason):
                _bump(ev_day, "scan_skips", reason[:80])
            elif isinstance(ev.get("reasons"), Sequence) and not isinstance(ev.get("reasons"), (str, bytes)):
                q_reasons = [str(r) for r in ev["reasons"] if is_quote_caused_option_reason(str(r))]
                if q_reasons:
                    _bump(ev_day, "scan_skips", q_reasons[0][:80])
        elif kind == "options.close_stopped":
            src = str(ev.get("source") or "").lower()
            if src in {"none", "intrinsic"}:
                _bump(ev_day, "close_quote_failures", f"close_stopped:{src}")
        elif kind == "options.exit_submitted":
            src = str(ev.get("price_source") or "").lower()
            if src in {"none", "intrinsic"}:
                _bump(ev_day, "close_quote_failures", f"exit_fallback:{src}")

    # Structure notes capture skipped closes that did not submit an order ("close skipped: no usable quote").
    for st in structures:
        notes = getattr(st, "notes", None)
        if notes is None and isinstance(st, Mapping):
            notes = st.get("notes")
        if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)):
            continue
        for note in notes:
            txt = str(note or "")
            # Format written by Structure.note(): "2026-11-25T15:00:00+00:00 close skipped: no usable quote"
            parts = txt.split(" ", 1)
            if len(parts) != 2:
                continue
            n_day = _et_day_iso(parts[0])
            msg = parts[1].strip()
            msg_low = msg.lower()
            if msg_low.startswith(("close skipped:", "close not sent:")) and is_quote_caused_option_reason(msg):
                _bump(n_day, "close_quote_failures", msg[:80])

    # IBKR / options-desk rejected orders in opt_orders caused by quote issues.
    if _has_table(store, "opt_orders"):
        try:
            floor_utc = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            for row in store.query(
                "SELECT COALESCE(terminal_at, submitted_at, prepared_at) AS ts, last_error, reason "
                "FROM opt_orders WHERE status IN ('rejected', 'failed') AND prepared_at >= ?",
                (floor_utc,),
            ):
                err = str(row["last_error"] or row["reason"] or "")
                if is_quote_caused_option_reason(err):
                    _bump(_et_day_iso(row["ts"]), "order_rejections", err[:80])
        except Exception:
            logger.debug("Could not inspect opt_orders rejections", exc_info=True)

    def _finalise(b: dict[str, Any]) -> dict[str, Any]:
        return {
            "total": int(b["total"]),
            "scan_skips": int(b["scan_skips"]),
            "mark_rejections": int(b["mark_rejections"]),
            "close_quote_failures": int(b["close_quote_failures"]),
            "order_rejections": int(b["order_rejections"]),
            "by_reason": dict(b["by_reason"].most_common(8)),
        }

    return {
        "session": _finalise(wins["session"]),
        "rolling_7d": _finalise(wins["rolling_7d"]),
    }


def _extract_activity_commission(raw: Mapping[str, Any]) -> tuple[bool, float | None, str, bool]:
    """Return ``(has_field, usd_or_raw_amount, currency, provisional)`` for one activity."""
    provisional = bool(raw.get("provisional_commission") is True or raw.get("commission_provisional") is True)
    currency = str(raw.get("commission_currency") or raw.get("currency") or "USD").strip().upper() or "USD"
    for key in ("commission", "fee", "fees", "reg_fee"):
        if key in raw and raw.get(key) is not None and str(raw.get(key)).strip() != "":
            val = _finite_float(raw.get(key))
            if val is not None:
                return True, abs(val), currency, provisional
    return False, None, currency, provisional


def _collect_commissions_and_slippage(
    store: Any,
    journal_events: Sequence[Mapping[str, Any]],
    structures: Sequence[Any],
    session_iso: str,
    start_7d_iso: str,
) -> dict[str, Any]:
    def _new_comm() -> dict[str, Any]:
        return {
            "total_fills": 0,
            "reported_fills": 0,
            "provisional_fills": 0,
            "explicit_fee_events": 0,
            "usd_sum": 0.0,
            "non_usd": {},
            "seen_order_ids": set(),
        }

    def _new_slip() -> dict[str, Any]:
        return {
            "equity_bps": [],
            "option_bps": [],
            "usd_list": [],
            "missing_benchmark": 0,
        }

    comm_wins = {"session": _new_comm(), "rolling_7d": _new_comm()}
    slip_wins = {"session": _new_slip(), "rolling_7d": _new_slip()}

    fill_by_order_id: dict[str, list[tuple[float, float]]] = {}  # order_id -> [(qty, price)]

    if _has_table(store, "broker_activities"):
        try:
            floor_day = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            for row in store.query(
                "SELECT activity_type, ts, net_amount, raw_json FROM broker_activities WHERE ts >= ?",
                (floor_day,),
            ):
                act_type = str(row["activity_type"] or "").upper()
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except ValueError:
                    raw = {}
                if not isinstance(raw, Mapping):
                    raw = {}
                act_day = (
                    _et_day_iso(raw.get("transaction_time") or raw.get("date"))
                    or _et_day_iso(row["ts"])
                )
                if act_day is None or not (start_7d_iso <= act_day <= session_iso):
                    continue
                targets = ("rolling_7d", "session") if act_day == session_iso else ("rolling_7d",)

                if act_type == "FILL":
                    oid = str(raw.get("order_id") or "").strip()
                    q_val = _finite_float(raw.get("qty"))
                    p_val = _finite_float(raw.get("price"))
                    if oid and q_val and q_val > 0 and p_val and p_val > 0:
                        fill_by_order_id.setdefault(oid, []).append((q_val, p_val))

                    has_comm, amt, ccy, prov = _extract_activity_commission(raw)
                    for w in targets:
                        cw = comm_wins[w]
                        cw["total_fills"] += 1
                        if oid:
                            cw["seen_order_ids"].add(oid)
                        if has_comm and amt is not None:
                            if prov:
                                cw["provisional_fills"] += 1
                            elif ccy == "USD":
                                cw["reported_fills"] += 1
                                cw["usd_sum"] += amt
                            else:
                                cw["non_usd"][ccy] = round(float(cw["non_usd"].get(ccy, 0.0)) + amt, 4)
                elif act_type in _EXPLICIT_FEE_ACTIVITY_TYPES:
                    net_amt = _finite_float(row["net_amount"])
                    if net_amt is None:
                        net_amt = _finite_float(raw.get("net_amount"))
                    ccy = str(raw.get("currency") or "USD").strip().upper() or "USD"
                    if net_amt is not None:
                        fee_mag = abs(net_amt)
                        for w in targets:
                            cw = comm_wins[w]
                            cw["explicit_fee_events"] += 1
                            if ccy == "USD":
                                cw["usd_sum"] += fee_mag
                            else:
                                cw["non_usd"][ccy] = round(float(cw["non_usd"].get(ccy, 0.0)) + fee_mag, 4)
        except Exception:
            logger.debug("Could not inspect broker_activities for commissions", exc_info=True)

    # Equities slippage from filled order_intents.
    if _has_table(store, "order_intents"):
        try:
            floor_utc = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            for row in store.query(
                "SELECT side, kind, reference_price, session_date, broker_order_id, updated_at, meta_json "
                "FROM order_intents WHERE status='filled' AND updated_at >= ?",
                (floor_utc,),
            ):
                if str(row["kind"] or "") == "option_structure":
                    continue
                try:
                    meta = json.loads(row["meta_json"] or "{}")
                except ValueError:
                    meta = {}
                if not isinstance(meta, Mapping):
                    meta = {}
                i_day = (
                    str(meta.get("new_stock_fill_session") or "")[:10]
                    or _et_day_iso(row["updated_at"])
                    or _et_day_iso(row["session_date"])
                )
                if not i_day or not (start_7d_iso <= i_day <= session_iso):
                    continue
                targets = ("rolling_7d", "session") if i_day == session_iso else ("rolling_7d",)

                ref_px = _finite_float(row["reference_price"])
                avg_px = _finite_float(meta.get("avg_price"))
                qty = _finite_float(meta.get("filled_qty"))
                oid = str(row["broker_order_id"] or "").strip()
                if (avg_px is None or qty is None or qty <= 0) and oid in fill_by_order_id:
                    fills = fill_by_order_id[oid]
                    tot_q = sum(q for q, _ in fills)
                    if tot_q > 0:
                        avg_px = sum(q * p for q, p in fills) / tot_q
                        qty = tot_q

                if ref_px is None or ref_px <= 0 or avg_px is None or avg_px <= 0 or qty is None or qty <= 0:
                    for w in targets:
                        slip_wins[w]["missing_benchmark"] += 1
                    continue

                side = str(row["side"] or "").lower()
                adverse_per_share = (avg_px - ref_px) if side == "buy" else (ref_px - avg_px)
                bps = (adverse_per_share / ref_px) * 10_000.0
                usd = adverse_per_share * qty
                for w in targets:
                    sw = slip_wins[w]
                    sw["equity_bps"].append(bps)
                    sw["usd_list"].append(usd)
        except Exception:
            logger.debug("Could not inspect order_intents for slippage", exc_info=True)

    # Options commissions & slippage from opt_orders (when OPTIONS_DESK_LEDGER is active on IBKR/desk).
    handled_opt_structures: set[tuple[str, str]] = set()
    if _has_table(store, "opt_orders"):
        try:
            floor_utc = (date.fromisoformat(start_7d_iso) - timedelta(days=2)).isoformat()
            for row in store.query(
                "SELECT structure_id, purpose, broker_order_id, filled_qty, mid_net, avg_net, "
                "actual_fees_usd, COALESCE(terminal_at, submitted_at, prepared_at) AS ts "
                "FROM opt_orders WHERE status IN ('filled', 'partially_filled') "
                "AND filled_qty > 0 AND prepared_at >= ?",
                (floor_utc,),
            ):
                o_day = _et_day_iso(row["ts"])
                if not o_day or not (start_7d_iso <= o_day <= session_iso):
                    continue
                targets = ("rolling_7d", "session") if o_day == session_iso else ("rolling_7d",)
                handled_opt_structures.add((str(row["structure_id"] or ""), str(row["purpose"] or "")))

                oid = str(row["broker_order_id"] or "").strip()
                fee_usd = _finite_float(row["actual_fees_usd"])
                for w in targets:
                    cw = comm_wins[w]
                    # Avoid double-counting fills already seen in broker_activities.
                    if not oid or oid not in cw["seen_order_ids"]:
                        cw["total_fills"] += 1
                        if fee_usd is not None:
                            cw["reported_fills"] += 1
                            cw["usd_sum"] += abs(fee_usd)

                mid_net = _finite_float(row["mid_net"])
                avg_net = _finite_float(row["avg_net"])
                f_qty = _finite_float(row["filled_qty"])
                if mid_net is None or abs(mid_net) <= 0 or avg_net is None or f_qty is None or f_qty <= 0:
                    for w in targets:
                        slip_wins[w]["missing_benchmark"] += 1
                else:
                    # Signed order net (+debit paid, -credit received): avg_net - mid_net is always adverse.
                    adverse_net = avg_net - mid_net
                    bps = (adverse_net / abs(mid_net)) * 10_000.0
                    usd = adverse_net * 100.0 * f_qty
                    for w in targets:
                        sw = slip_wins[w]
                        sw["option_bps"].append(bps)
                        sw["usd_list"].append(usd)
        except Exception:
            logger.debug("Could not inspect opt_orders for fees/slippage", exc_info=True)

    # KvOptionsStore structures (legacy / Alpaca / mirrored book) for option slippage when not in opt_orders.
    exit_mid_by_struct: dict[str, float] = {}
    for ev in journal_events:
        if str(ev.get("kind") or "") == "options.exit_submitted" and str(ev.get("price_source") or "") == "quotes":
            sid = str(ev.get("structure_id") or "")
            mid_v = _finite_float(ev.get("mid"))
            if sid and mid_v is not None and mid_v > 0:
                exit_mid_by_struct[sid] = mid_v

    for st in structures:
        def _g(attr: str, default: Any = None) -> Any:
            if isinstance(st, Mapping):
                return st.get(attr, default)
            return getattr(st, attr, default)

        sid = str(_g("id") or "")
        qty = _finite_float(_g("qty")) or 0.0
        if qty <= 0:
            continue

        opened_day = _et_day_iso(_g("opened_at"))
        if (
            opened_day
            and start_7d_iso <= opened_day <= session_iso
            and (sid, "open") not in handled_opt_structures
        ):
            targets = ("rolling_7d", "session") if opened_day == session_iso else ("rolling_7d",)
            entry_debit = _finite_float(_g("entry_debit"))
            eg = _g("entry_greeks")
            mid_debit = _finite_float(eg.get("mid_debit")) if isinstance(eg, Mapping) else None
            if entry_debit is not None and entry_debit > 0 and mid_debit is not None and mid_debit > 0:
                adverse = entry_debit - mid_debit
                for w in targets:
                    slip_wins[w]["option_bps"].append((adverse / mid_debit) * 10_000.0)
                    slip_wins[w]["usd_list"].append(adverse * 100.0 * qty)
            elif entry_debit is not None:
                for w in targets:
                    slip_wins[w]["missing_benchmark"] += 1

        closed_day = _et_day_iso(_g("closed_at"))
        if (
            closed_day
            and start_7d_iso <= closed_day <= session_iso
            and (sid, "close") not in handled_opt_structures
        ):
            targets = ("rolling_7d", "session") if closed_day == session_iso else ("rolling_7d",)
            exit_credit = _finite_float(_g("exit_credit"))
            mid_credit = exit_mid_by_struct.get(sid)
            if exit_credit is not None and mid_credit is not None and mid_credit > 0:
                adverse = mid_credit - exit_credit
                for w in targets:
                    slip_wins[w]["option_bps"].append((adverse / mid_credit) * 10_000.0)
                    slip_wins[w]["usd_list"].append(adverse * 100.0 * qty)
            elif exit_credit is not None:
                for w in targets:
                    slip_wins[w]["missing_benchmark"] += 1

    def _finalise_window(cw: dict[str, Any], sw: dict[str, Any]) -> dict[str, Any]:
        tot_fills = int(cw["total_fills"])
        rep_fills = int(cw["reported_fills"])
        fee_events = int(cw["explicit_fee_events"])
        prov_fills = int(cw["provisional_fills"])
        non_usd = dict(cw["non_usd"])

        if rep_fills > 0 or fee_events > 0:
            reported = True
            status = "reported"
            total_comm = round(float(cw["usd_sum"]), 4)
            denom = rep_fills if rep_fills > 0 else max(tot_fills, fee_events)
            mean_comm = round(total_comm / denom, 4) if denom > 0 else None
        elif prov_fills > 0 or non_usd:
            reported = False
            status = "provisional_or_non_usd_only"
            total_comm = None
            mean_comm = None
        elif tot_fills > 0:
            reported = False
            status = "not_reported_by_broker"
            total_comm = None
            mean_comm = None
        else:
            reported = False
            status = "no_trades"
            total_comm = None
            mean_comm = None

        all_bps = [*sw["equity_bps"], *sw["option_bps"]]
        usd_list = sw["usd_list"]
        n_meas = len(all_bps)
        tot_slip = round(sum(usd_list), 2) if n_meas > 0 else None
        mean_slip_usd = round(sum(usd_list) / n_meas, 2) if n_meas > 0 else None
        mean_bps = round(sum(all_bps) / n_meas, 1) if n_meas > 0 else None
        med_bps = round(float(statistics.median(all_bps)), 1) if n_meas > 0 else None

        return {
            "commissions": {
                "reported": reported,
                "status": status,
                "total_fills": tot_fills,
                "fills_with_reported_commission": rep_fills,
                "provisional_fills": prov_fills,
                "explicit_fee_events": fee_events,
                "total_commission_usd": total_comm,
                "mean_commission_per_trade_usd": mean_comm,
                "non_usd_commissions": non_usd,
            },
            "slippage": {
                "trades_measured": n_meas,
                "equity_trades_measured": len(sw["equity_bps"]),
                "option_trades_measured": len(sw["option_bps"]),
                "fills_missing_benchmark": int(sw["missing_benchmark"]),
                "total_slippage_usd": tot_slip,
                "mean_slippage_usd": mean_slip_usd,
                "mean_slippage_bps": mean_bps,
                "median_slippage_bps": med_bps,
            },
        }

    return {
        "session": _finalise_window(comm_wins["session"], slip_wins["session"]),
        "rolling_7d": _finalise_window(comm_wins["rolling_7d"], slip_wins["rolling_7d"]),
    }


def collect_trial_metrics(
    *,
    store: Any = None,
    ledger: Any = None,
    journal: Any = None,
    snapshot: Any = None,
    options_store: Any = None,
    price_service: Any = None,
    config: Any = None,
    calendar: Any = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Collect all seven read-only trial measurements into a JSON-safe dict."""
    del calendar  # Reserved for caller symmetry; read-only aggregation uses ET dates.
    now_utc = _as_utc(as_of) if as_of is not None else _utc_now()
    session_day = now_utc.astimezone(ET).date()
    session_iso = session_day.isoformat()
    start_7d_day = session_day - timedelta(days=6)
    start_7d_iso = start_7d_day.isoformat()

    resolved_store = store if store is not None else getattr(ledger, "store", None)
    resolved_journal = journal if journal is not None else getattr(ledger, "journal", None)
    resolved_snap = snapshot
    if resolved_snap is None and ledger is not None and hasattr(ledger, "last_snapshot"):
        try:
            resolved_snap = ledger.last_snapshot()
        except Exception:
            resolved_snap = None

    since_utc = datetime.combine(start_7d_day - timedelta(days=1), time.min, tzinfo=ET).astimezone(UTC)
    journal_events = _iter_journal_since(resolved_journal, since_utc)
    structures = _load_option_structures_safe(resolved_store, options_store)
    max_opt_age_s = _resolve_max_option_quote_age_s(config)

    cost_info, primary_info, ie_info = _collect_research_and_evidence(
        resolved_store, session_iso, start_7d_iso
    )
    stock_quotes = _collect_stock_quote_coverage(
        resolved_store, price_service, resolved_snap, session_iso, start_7d_iso
    )
    option_legs = _collect_option_leg_coverage(
        resolved_store, resolved_snap, session_iso, start_7d_iso, max_opt_age_s
    )
    opt_rejects = _collect_quote_option_rejections(
        resolved_store, journal_events, structures, session_iso, start_7d_iso
    )
    comm_slip = _collect_commissions_and_slippage(
        resolved_store, journal_events, structures, session_iso, start_7d_iso
    )

    return {
        "session_date": session_iso,
        "window_7d_start": start_7d_iso,
        "as_of": now_utc.isoformat(),
        "research_cost_per_validated_decision": cost_info,
        "scheduled_primary_evidence": primary_info,
        "entry_insufficient_evidence_by_job": ie_info,
        "stock_quote_coverage_regular_hours": stock_quotes,
        "option_legs_fresh_two_sided": option_legs,
        "quote_caused_option_rejections": opt_rejects,
        "trade_commissions_and_slippage": comm_slip,
    }


# ---------------------------------------------------------------------------
# Broker-neutral daily report formatting
# ---------------------------------------------------------------------------
def _fmt_rate(pct_val: float | None, num: int, den: int, *, suffix: str = "") -> str:
    if pct_val is None or den <= 0:
        return f"N/A (0/0{suffix})"
    return f"{pct_val:.1f}% ({num}/{den}{suffix})"


def _fmt_signed_usd(val: float | None) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val > 0 else ("-" if val < 0 else "")
    return f"{sign}${abs(val):,.2f}"


def _fmt_signed_bps(val: float | None) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.1f} bps"


def format_trial_metrics_field(metrics: Mapping[str, Any]) -> str:
    """Format the 7 trial metrics for ``ReportBuilder.build_daily_summary``.

    Uses strictly broker-neutral wording and stays well under Discord's
    1,024-character embed field limit.
    """
    cost_s = ((metrics.get("research_cost_per_validated_decision") or {}).get("session")) or {}
    cost_7 = ((metrics.get("research_cost_per_validated_decision") or {}).get("rolling_7d")) or {}
    c_today = cost_s.get("cost_per_validated_decision_usd")
    c_7d = cost_7.get("cost_per_validated_decision_usd")
    today_cost_txt = (
        f"${c_today:.2f} (${cost_s.get('spend_usd', 0.0):.2f} / {cost_s.get('validated_decisions', 0)}, "
        f"{cost_s.get('grounded_decisions', 0)} grounded)"
        if c_today is not None
        else f"N/A (${cost_s.get('spend_usd', 0.0):.2f} / 0)"
    )
    wk_cost_txt = (
        f"${c_7d:.2f} (${cost_7.get('spend_usd', 0.0):.2f} / {cost_7.get('validated_decisions', 0)})"
        if c_7d is not None
        else f"N/A (${cost_7.get('spend_usd', 0.0):.2f} / 0)"
    )

    prim_s = ((metrics.get("scheduled_primary_evidence") or {}).get("session")) or {}
    prim_7 = ((metrics.get("scheduled_primary_evidence") or {}).get("rolling_7d")) or {}
    prim_today_txt = _fmt_rate(
        prim_s.get("share_pct"),
        int(prim_s.get("with_dated_primary", 0) or 0),
        int(prim_s.get("scheduled_reviews", 0) or 0),
        suffix=f", {int(prim_s.get('with_dated_primary_cited', 0) or 0)} cited",
    )
    prim_7d_txt = _fmt_rate(
        prim_7.get("share_pct"),
        int(prim_7.get("with_dated_primary", 0) or 0),
        int(prim_7.get("scheduled_reviews", 0) or 0),
    )

    ie_s = ((metrics.get("entry_insufficient_evidence_by_job") or {}).get("session")) or {}
    ie_7 = ((metrics.get("entry_insufficient_evidence_by_job") or {}).get("rolling_7d")) or {}
    sched_s = ie_s.get("scheduled") or {}
    sched_7 = ie_7.get("scheduled") or {}
    by_job_s = ie_s.get("by_job") or {}
    job_parts = [
        f"{j} {v.get('insufficient_evidence', 0)}/{v.get('total', 0)}"
        + (f" ({v['share_pct']:.0f}%)" if v.get("share_pct") is not None else "")
        for j, v in list(by_job_s.items())[:6]
        if isinstance(v, Mapping) and int(v.get("total", 0) or 0) > 0
    ]
    job_breakdown = f" [{', '.join(job_parts)}]" if job_parts else ""
    ie_today_txt = _fmt_rate(
        sched_s.get("share_pct"),
        int(sched_s.get("insufficient_evidence", 0) or 0),
        int(sched_s.get("total", 0) or 0),
    )
    ie_7d_txt = _fmt_rate(
        sched_7.get("share_pct"),
        int(sched_7.get("insufficient_evidence", 0) or 0),
        int(sched_7.get("total", 0) or 0),
    )

    sq_s = ((metrics.get("stock_quote_coverage_regular_hours") or {}).get("session")) or {}
    sq_7 = ((metrics.get("stock_quote_coverage_regular_hours") or {}).get("rolling_7d")) or {}
    sq_snap = (
        f", book {sq_s.get('snapshot_holdings_priced', 0)}/{sq_s.get('snapshot_holdings_total', 0)}"
        if int(sq_s.get("snapshot_holdings_total", 0) or 0) > 0
        else ""
    )
    sq_today_txt = _fmt_rate(
        sq_s.get("fresh_rate_pct"),
        int(sq_s.get("fresh", 0) or 0),
        int(sq_s.get("requested", 0) or 0),
        suffix=sq_snap,
    )
    sq_7d_txt = _fmt_rate(
        sq_7.get("fresh_rate_pct"),
        int(sq_7.get("fresh", 0) or 0),
        int(sq_7.get("requested", 0) or 0),
    )

    ol_s = ((metrics.get("option_legs_fresh_two_sided") or {}).get("session")) or {}
    ol_7 = ((metrics.get("option_legs_fresh_two_sided") or {}).get("rolling_7d")) or {}
    ol_snap = (
        f", book {ol_s.get('snapshot_legs_priced', 0)}/{ol_s.get('snapshot_legs_total', 0)}"
        if int(ol_s.get("snapshot_legs_total", 0) or 0) > 0
        else ""
    )
    ol_today_txt = _fmt_rate(
        ol_s.get("fresh_two_sided_share_pct"),
        int(ol_s.get("fresh_two_sided", 0) or 0),
        int(ol_s.get("legs_checked", 0) or 0),
        suffix=ol_snap,
    )
    ol_7d_txt = _fmt_rate(
        ol_7.get("fresh_two_sided_share_pct"),
        int(ol_7.get("fresh_two_sided", 0) or 0),
        int(ol_7.get("legs_checked", 0) or 0),
    )

    qr_s = ((metrics.get("quote_caused_option_rejections") or {}).get("session")) or {}
    qr_7 = ((metrics.get("quote_caused_option_rejections") or {}).get("rolling_7d")) or {}
    qr_txt = (
        f"{int(qr_s.get('total', 0) or 0)} today "
        f"(scan {int(qr_s.get('scan_skips', 0) or 0)}, "
        f"mark {int(qr_s.get('mark_rejections', 0) or 0)}, "
        f"close {int(qr_s.get('close_quote_failures', 0) or 0) + int(qr_s.get('order_rejections', 0) or 0)}) "
        f"| 7d {int(qr_7.get('total', 0) or 0)}"
    )

    cs_s = ((metrics.get("trade_commissions_and_slippage") or {}).get("session")) or {}
    cs_7 = ((metrics.get("trade_commissions_and_slippage") or {}).get("rolling_7d")) or {}
    comm_s = cs_s.get("commissions") or {}
    slip_s = cs_s.get("slippage") or {}
    comm_7 = cs_7.get("commissions") or {}
    slip_7 = cs_7.get("slippage") or {}

    if comm_s.get("reported"):
        mean_c = comm_s.get("mean_commission_per_trade_usd")
        mean_c_txt = f", ${mean_c:.2f}/fill" if mean_c is not None else ""
        comm_txt = f"fees ${comm_s.get('total_commission_usd', 0.0):.2f} ({comm_s.get('fills_with_reported_commission', 0)} fills{mean_c_txt})"
    elif comm_s.get("status") == "not_reported_by_broker":
        comm_txt = f"fees not reported by broker ({comm_s.get('total_fills', 0)} fills)"
    elif comm_s.get("status") == "provisional_or_non_usd_only":
        comm_txt = "fees pending final USD settlement"
    else:
        comm_txt = "fees N/A (0 fills)"

    if int(slip_s.get("trades_measured", 0) or 0) > 0:
        slip_txt = (
            f"slippage {_fmt_signed_usd(slip_s.get('total_slippage_usd'))} "
            f"({_fmt_signed_bps(slip_s.get('mean_slippage_bps'))} avg, {slip_s.get('trades_measured')} trades)"
        )
    elif int(slip_s.get("fills_missing_benchmark", 0) or 0) > 0:
        slip_txt = f"slippage benchmark unavailable ({slip_s.get('fills_missing_benchmark')} fills)"
    else:
        slip_txt = "slippage N/A (0 trades)"

    wk_fee_txt = (
        f"${comm_7.get('total_commission_usd', 0.0):.2f}"
        if comm_7.get("reported")
        else ("unreported" if comm_7.get("status") == "not_reported_by_broker" else "N/A")
    )
    wk_slip_txt = _fmt_signed_bps(slip_7.get("mean_slippage_bps"))

    lines = [
        f"• **Research Cost / Decision:** {today_cost_txt} today | 7d {wk_cost_txt}",
        f"• **Scheduled Primary Evidence:** {prim_today_txt} today | 7d {prim_7d_txt}",
        f"• **Entry INSUFFICIENT_EVIDENCE:** scheduled {ie_today_txt} today{job_breakdown} | 7d scheduled {ie_7d_txt}",
        f"• **Stock Quotes (Regular Hours):** {sq_today_txt} today | 7d {sq_7d_txt}",
        f"• **Option Legs Two-Sided:** {ol_today_txt} today | 7d {ol_7d_txt}",
        f"• **Option Quote Rejections:** {qr_txt}",
        f"• **Commissions & Slippage:** {comm_txt}; {slip_txt} | 7d fees {wk_fee_txt}, slip {wk_slip_txt}",
    ]
    out = "\n".join(lines)
    return out if len(out) <= 1020 else out[:1017] + "..."


__all__ = [
    "OPTION_LEG_QUOTES_KV_KEY",
    "SCHEDULED_JOBS",
    "STOCK_QUOTES_KV_KEY",
    "collect_trial_metrics",
    "format_trial_metrics_field",
    "install_broker_quote_observer",
    "is_dated_primary_evidence",
    "is_fresh_two_sided_option_snapshot",
    "is_quote_caused_option_reason",
    "is_regular_hours_open",
    "record_option_leg_quotes",
    "record_stock_quote_batch",
]
