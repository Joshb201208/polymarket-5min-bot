"""Scheduled research and material-news reactions; returns proposals, never orders."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlsplit

from stock_agent.analyst import EXIT_PROMPT_VERSION, Analyst, ResearchInput
from stock_agent.ledger import BookSnapshot, IntentStatus, JobStatus, NewIntent, StaleBook, ThesisRole
from stock_agent.llm_client import ModelRoute, ProviderUnavailable, SCHEDULED_REVIEW_TRANSPORT_SECONDS
from stock_agent.research_focus import FocusItem, focus_symbols, owner_backed
from stock_agent.research_notes import load_notes, research_notes_payload
from stock_agent.research_schemas import Direction, Evidence, OwnerThesisDraft, ResearchTask, utc_datetime
from stock_agent.research_store import NY, ResearchStore
from stock_agent.screener import Candidate, Screener

_RETRYABLE = {"auth", "quota", "model_unavailable", "rate_limit", "budget", "transport", "upstream"}
_INPUT_BLOCKERS = {"broker_book_blocked", "broker_book_unavailable", "quote_stale_or_unavailable",
                   "news_stale_or_unavailable", "llm_unavailable", "research_health_unavailable"}
_FRESH_SET = "research:fresh_scheduled_set"
_SCHEDULED = {"prescan", "weekly", "midweek"}
PRESCAN_MAX_RUNTIME = 2_100
_PRESCAN_OPEN_BUFFER = timedelta(minutes=10)
_PRESCAN_PRICE_TIMEOUT = 15
_PRESCAN_REVIEW_BUDGET = SCHEDULED_REVIEW_TRANSPORT_SECONDS + _PRESCAN_PRICE_TIMEOUT + 10
# At most two configured model choices per provider, all ungrounded (30s).
_PRESCAN_CONFIRM_BUDGET = 190
_HANDOFF_TRANSIENT = {"broker_book_unavailable",
                      "quote_stale_or_unavailable", "news_stale_or_unavailable", "llm_unavailable"}


def _edgar_filing(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"sec.gov", "www.sec.gov"}
                and parsed.path.lower().startswith("/archives/edgar/data/"))
    except ValueError:
        return False


def _edgar_index_only(url: str) -> bool:
    return _edgar_filing(url) and urlsplit(url).path.lower().endswith(("-index.htm", "-index.html"))


def prescan_runtime_seconds(now: datetime, opens_at: datetime) -> float:
    return max(0.0, min(PRESCAN_MAX_RUNTIME, (opens_at - _PRESCAN_OPEN_BUFFER - now).total_seconds()))


class _PrescanCutoff(Exception):
    """Prescan may not publish a decision after its time budget closes."""


class SourceUnavailable(RuntimeError):
    """A required data request failed rather than returning a healthy empty result."""


class OwnerReviewUnavailable(RuntimeError):
    """A directive cannot be safely reviewed with the current evidence."""

@dataclass(frozen=True)
class NewsEvent:
    symbol: str
    story_id: str
    evidence: Evidence
    materiality: int
    issuer_specific: bool = True

    def payload(self) -> dict:
        return {"symbol": self.symbol, "story_id": self.story_id, "materiality": self.materiality,
                "issuer_specific": self.issuer_specific,
                "evidence": self.evidence.model_dump(mode="json")}

    @classmethod
    def from_payload(cls, payload: dict) -> NewsEvent:
        return cls(payload["symbol"], payload["story_id"],
                   Evidence.model_validate_json(json.dumps(payload["evidence"])), payload["materiality"],
                   payload.get("issuer_specific", True) is True)


@dataclass(frozen=True)
class SecurityInputs:
    symbol: str
    name: str
    sector: str
    price: Decimal | None = None
    quote_at: datetime | None = None
    tradable: bool = False
    metrics: dict = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    metric_provenance: dict = field(default_factory=dict)
    priority_evidence: tuple[Evidence, ...] = ()
    analysis_price: Decimal | None = None
    analysis_quote_at: datetime | None = None


class Sources(Protocol):
    async def universe(self, now: datetime) -> tuple[str, ...]: ...

    async def securities(self, symbols: tuple[str, ...], now: datetime,
                         *, preopen: bool = False, prescan: bool = False) -> dict[str, SecurityInputs]: ...

    async def news(self, symbols: tuple[str, ...], now: datetime) -> tuple[NewsEvent, ...]: ...

    async def releases(self, symbols: tuple[str, ...], now: datetime) -> tuple[NewsEvent, ...]: ...

    async def market(self, now: datetime) -> tuple[SecurityInputs, ...]: ...

    async def background(self, symbols: tuple[str, ...]) -> dict[str, tuple[Evidence, ...]]: ...


@dataclass(frozen=True)
class OptionIdea:
    symbol: str
    direction: Direction
    story_id: str
    thesis_id: str
    raw_conviction: int
    effective_conviction: int
    generated_at: datetime
    evidence_ids: tuple[str, ...]
    source: str = "news"
    owner_directive_added: str | None = None

    @classmethod
    def from_payload(cls, payload: dict):
        return cls(
            payload["symbol"], Direction(payload["direction"]), payload["story_id"], payload["thesis_id"],
            payload["raw_conviction"], payload["effective_conviction"],
            datetime.fromisoformat(payload["generated_at"]), tuple(payload["evidence_ids"]),
            payload.get("source", "news"), payload.get("owner_directive_added"),
        )


@dataclass(frozen=True)
class JobOutcome:
    status: JobStatus
    intents: tuple[NewIntent, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    option_candidates: tuple[OptionIdea, ...] = ()


@dataclass
class _EntryFunnel:
    screened: int = 0
    thesis_written: int = 0
    healthy_unheld_theses: int = 0  # Internal readiness signal, not a reported counter.
    passed_conviction: int = 0
    buy_candidates: int = 0
    option_ideas: int = 0
    handoff_staged: int = 0
    handoff_superseded: int = 0
    rejected_by_reason: Counter[str] = field(default_factory=Counter)

    def payload(self) -> dict:
        fields = {
            "screened": self.screened, "thesis_written": self.thesis_written,
            "passed_conviction": self.passed_conviction, "buy_candidates": self.buy_candidates,
            "option_ideas": self.option_ideas, "rejected_by_reason": dict(self.rejected_by_reason),
        }
        if self.handoff_staged:
            fields["handoff_staged"] = self.handoff_staged
        if self.handoff_superseded:
            fields["handoff_superseded"] = self.handoff_superseded
        return fields


def _safe_previous(thesis) -> dict | None:
    if thesis is None:
        return None
    draft = thesis.payload.get("draft")
    if not isinstance(draft, dict):
        return None
    return {key: draft.get(key) for key in (
        "direction", "summary", "thesis_break", "bull_case", "bear_case",
    )}


def _reconstitute(payload: dict) -> NewIntent:
    values = dict(payload)
    for key in ("expires_at", "next_attempt_at"):
        if values[key]:
            values[key] = datetime.fromisoformat(values[key])
    if values["session_date"]:
        values["session_date"] = date.fromisoformat(values["session_date"])
    if values["reference_price"] is not None:
        values["reference_price"] = Decimal(values["reference_price"])
    for key in ("thesis_ids", "evidence_ids"):
        values[key] = tuple(values[key])
    values["status"] = IntentStatus(values["status"])
    return NewIntent(**values)


class ResearchPipeline:
    def __init__(self, config, ledger, calendar, analyst: Analyst, store: ResearchStore, sources: Sources, gate,
                 *, clock=None, context=None, focus=None, daily_focus=None, directives=owner_backed, alerts=None):
        self.config = config
        self.ledger = ledger
        self.calendar = calendar
        self.analyst = analyst
        self.store = store
        self.sources = sources
        self.gate = gate
        self.clock = clock or ledger.now
        self.context = context or (lambda: {})
        self.focus = focus or (lambda *, now: focus_symbols(now=now, cadence="live"))
        self.daily_focus = daily_focus or (lambda *, now: focus_symbols(now=now, cadence="daily"))
        self.directives = directives
        self.alerts = alerts
        self._analysis_lock = asyncio.Lock()

    def has_fresh_thesis_set(self, now: datetime) -> bool:
        stamp = utc_datetime(self.store.store.kv_get(_FRESH_SET))
        return stamp is not None and timedelta() <= now - stamp <= timedelta(
            hours=self.config.RESEARCH_THESIS_MAX_AGE_H,
        )

    async def run(self, kind: str, snapshot: BookSnapshot | None = None, *, work_scope: str | None = None,
                  prescan_deadline: tuple[float, datetime] | None = None) -> JobOutcome:
        # Avoid overlapping scheduled/news windows superseding a thesis
        # during its independent review. Broker protection has its own lane.
        started_at = self.clock() if kind == "prescan" and work_scope is None else None
        started = asyncio.get_running_loop().time() if started_at is not None else None
        async with self._analysis_lock:
            funnel = _EntryFunnel()
            session = self._session(self.clock())
            if session is None:
                return JobOutcome(JobStatus.SKIPPED, detail={"reason": "no_execution_session"})
            deadline = prescan_deadline
            if deadline is None and started_at is not None and started_at < session.opens_at:
                runtime = prescan_runtime_seconds(started_at, session.opens_at)
                deadline = started + runtime, started_at + timedelta(seconds=runtime)
            try:
                outcome = await self._run(kind, snapshot, session, funnel, work_scope, deadline=deadline)
            except SourceUnavailable:
                intents, ideas = self._recover()
                funnel.rejected_by_reason["required_source_unavailable"] += 1
                outcome = JobOutcome(JobStatus.FAILED, intents, {"reason": "required_source_unavailable"}, ideas)
            info = funnel.payload()
            universe_sources = getattr(self.sources, "last_universe_sources", None)
            extra = {"universe_sources": dict(universe_sources)} if universe_sources else {}
            self.ledger.journal.append("research.entry_funnel", job=kind,
                                       session_date=session.date.isoformat(), **info, **extra)
            detail = {**outcome.detail, "entry_funnel": info}
            if universe_sources:
                detail["universe_sources"] = dict(universe_sources)
            return replace(outcome, detail=detail)

    async def _run(self, kind, snapshot, session, funnel, work_scope, *, deadline=None) -> JobOutcome:
        now = self.clock()
        held = {row.symbol: row for row in (snapshot or self.ledger.snapshot()).holdings if row.qty > 0}
        for pending in self.store.due_confirmations(now):
            await self._confirm(pending["work_key"], held.get(pending["symbol"]), deadline=deadline)
        focus = tuple(self.focus(now=now))
        daily = tuple(dict.fromkeys(self.daily_focus(now=now)))
        preopen = now.astimezone(NY).date() == session.date and now < session.opens_at
        prescan = kind == "prescan" and work_scope is None and preopen
        directives = self.directives(now=now) if prescan else {}
        universe = await self.sources.universe(now)
        symbols = tuple(dict.fromkeys((*held, *focus, *universe)))
        if kind in {"news", "earnings"}:
            retried = tuple(NewsEvent.from_payload(row) for row in self.store.retry_story_payloads(now, kind))
            try:
                live = getattr(self.sources, "live_news", None)
                if kind == "news" and live is not None:
                    gathered = await live(tuple(held), focus, universe, now)
                else:
                    gathered = await (self.sources.news(symbols, now) if kind == "news"
                                      else self.sources.releases(symbols, now))
            except SourceUnavailable:
                if not retried:
                    raise
                gathered = ()
            as_of = self.clock()
            seen = set()
            stories = []
            for item in sorted((*gathered, *retried), key=lambda event: (
                event.symbol not in held, -event.materiality, event.symbol, event.story_id,
            )):
                if item.symbol not in symbols or item.story_id in seen or not self._fresh_story(item, as_of):
                    continue
                seen.add(item.story_id)
                stories.append(item)
            # A live pass must remain bounded even with an exceptionally busy tape.
            selected = []
            for item in stories:
                key = self._story_key(item)
                if not self.store.ready(key, as_of):
                    continue
                if kind == "news" and item.symbol not in held and not item.issuer_specific:
                    day = as_of.astimezone(NY).date().isoformat()
                    if not self.store.reserve_indirect_news(day, item.symbol, item.story_id):
                        # Count the suppressed story once per symbol/day without
                        # consuming its ordinary work key (e.g. if it becomes held).
                        skip_key = f"indirect-news-skip:{day}:{item.symbol}:{key}"
                        if self.store.claim_work(skip_key, item.symbol, as_of):
                            code = "indirect_news_symbol_cap"
                            self.store.finish_work(skip_key, "no_action", detail={"reason": code})
                            funnel.rejected_by_reason[code] += 1
                        continue
                selected.append(item)
                if len(selected) == 4:
                    break
            stories = selected
            securities = await self.sources.securities(tuple(dict.fromkeys(item.symbol for item in stories)), now)
            work = [(item.symbol, self._story_key(item), item) for item in stories]
        else:
            if prescan:
                symbols = tuple(dict.fromkeys((*held, *directives, *focus, *daily, *symbols)))
            securities = await self.sources.securities(
                symbols, now, **({"preopen": True} if preopen else {}), **({"prescan": True} if prescan else {}),
            )
            ranked = Screener.rank([
                Candidate(row.symbol, row.name, row.sector, row.tradable, row.metrics, row.symbol in focus)
                for row in securities.values()
            ], include_relative_strength=kind not in _SCHEDULED or work_scope is not None)
            funnel.screened = len(ranked)
            if not ranked:
                funnel.rejected_by_reason["no_tradable_universe"] += 1
            ranked_symbols = [row["symbol"] for row in ranked]
            screen_at = self.clock()
            if session.opens_at <= screen_at < session.closes_at:
                # Keep factor order within each group, but don't crowd out
                # usable candidates with names that have no execution quote.
                ranked_symbols.sort(key=lambda symbol: not _fresh_quote(
                    securities[symbol], screen_at, max_age=timedelta(seconds=90),
                ))
            order = ((*directives, *held, *daily, *ranked_symbols[:8], *focus) if prescan
                     else (*held, *directives, *focus, *ranked_symbols[:8]))
            chosen = tuple(dict.fromkeys(order))
            background = getattr(self.sources, "background", None)
            if background is not None:
                stories = await background(chosen)
                for symbol in chosen:
                    if symbol in securities:
                        row = securities[symbol]
                        securities[symbol] = replace(row, evidence=(*stories.get(symbol, ()), *row.evidence))
            work = [(sym, f"{work_scope or kind}:{now.astimezone(NY).date()}:{sym}", None) for sym in chosen]

        failed = attempted = deferred = time_deferred = 0
        for symbol, key, story in work:
            if (work_scope and work_scope.startswith("catchup-")
                    and self._has_scheduled_entry(symbol, session.date)):
                # A fallback retry for some other symbol must not redraw this
                # symbol once its original handoff or queued BUY has resolved.
                if self.store.claim_work(key, symbol, self.clock()):
                    self.store.finish_work(key, "no_action", detail={"reason": "scheduled_entry_already_current"})
                continue
            item = securities.get(symbol, SecurityInputs(symbol, symbol, "Unknown"))
            as_of = self.clock()
            directive = directives.get(symbol)
            owner = None
            held_owner_option = None
            if (directive and as_of < session.opens_at
                    and not self.ledger.owner_directive_vetoed(symbol, directive.added)
                    and not self.ledger.owner_directive_exited(symbol, directive.added)
                    and not self.ledger.owner_directive_held(symbol, session.date)):
                option_used = self.ledger.owner_directive_used(symbol, directive.added, lane="option")
                if symbol in held:
                    held_owner_option = None if option_used else directive
                elif not option_used or not self.ledger.owner_directive_used(symbol, directive.added):
                    owner = directive
            if directive and symbol not in held and owner is None:
                funnel.rejected_by_reason["owner_directive_inactive"] += 1
                continue
            if self._prescan_remaining(deadline, as_of) <= _PRESCAN_REVIEW_BUDGET:
                # Do not claim or make a reservation for an unstarted tail.
                # An in-flight provider can bill even when its caller cancels.
                if self.store.ready(key, as_of):
                    time_deferred += 1
                continue
            if not self.store.claim_work(key, symbol, as_of):
                state = self.store.work_state(key)
                if state is not None and state["status"] == "retry":
                    deferred += 1
                continue
            attempted += 1
            try:
                await self._review(kind, key, item, held.get(symbol), story, session, as_of, funnel,
                                   owner=owner, held_owner_option=held_owner_option, deadline=deadline)
            except (_PrescanCutoff, OwnerReviewUnavailable, ProviderUnavailable) as exc:
                if isinstance(exc, _PrescanCutoff) or self._prescan_remaining(deadline) <= 0:
                    attempted -= 1
                    time_deferred += 1
                    self.store.finish_work(key, "retry", detail={"reason": "prescan_time_budget"},
                                           retry_at=self.clock() + timedelta(minutes=2))
                    continue
                failed += 1
                if isinstance(exc, OwnerReviewUnavailable):
                    await self._owner_unavailable(key, owner, str(exc), funnel)
                    continue
                failures = {error.kind for error in exc.attempts}
                funnel.rejected_by_reason.update(f"llm_{reason}" for reason in failures or {"no_available_route"})
                owner_review = owner or held_owner_option
                if owner_review:
                    await self._owner_unavailable(
                        key, owner_review, ", ".join(sorted(failures)) or "no_available_route", funnel,
                        failure_codes=exc.failure_codes,
                    )
                    continue
                state = self.store.work_state(key)
                can_retry = state["attempts"] < 3 and _can_retry(exc)
                if can_retry:
                    deferred += 1
                self.store.finish_work(
                    key, "retry" if can_retry else "unavailable",
                    detail={
                        "failures": [f"{err.provider}:{err.kind}" for err in exc.attempts], "kind": kind,
                        "story": story.payload() if can_retry and story else None,
                        **exc.failure_codes,
                    },
                    retry_at=self.clock() + timedelta(minutes=2 if kind == "news" else 5) if can_retry else None,
                )
        collected, ideas = self._recover()
        incomplete = (attempted and failed == attempted and not collected and not ideas) or (deferred and kind != "news")
        if time_deferred:
            funnel.rejected_by_reason["prescan_time_budget"] += time_deferred
        status = JobStatus.FAILED if incomplete else JobStatus.DONE
        if kind in _SCHEDULED and funnel.screened and funnel.healthy_unheld_theses and not funnel.handoff_staged and (
            not failed and not deferred and not time_deferred and not
            ((_INPUT_BLOCKERS | {"target_session_closed"}) & funnel.rejected_by_reason.keys())
        ):
            self.store.store.kv_set(_FRESH_SET, now.isoformat())
        return JobOutcome(status, collected, {
            "reviewed": attempted, "unavailable": failed, "deferred": deferred + time_deferred,
        }, ideas)

    def _prescan_remaining(self, deadline, now=None) -> float:
        if deadline is None:
            return math.inf
        return min(deadline[0] - asyncio.get_running_loop().time(), (deadline[1] - (now or self.clock())).total_seconds())

    def _require_prescan_time(self, deadline, minimum=0) -> None:
        if self._prescan_remaining(deadline) <= minimum:
            raise _PrescanCutoff

    def _has_scheduled_entry(self, symbol: str, session_date: date) -> bool:
        if self.store.protects_scheduled_buy(symbol, session_date.isoformat()):
            return True
        current = self.ledger.current_thesis(symbol)
        return current is not None and any(
            i.side == "buy" and i.origin in _SCHEDULED and i.session_date == session_date
            and current.thesis_id in i.thesis_ids
            for i in self.ledger.list_intents((IntentStatus.QUEUED,), symbol=symbol)
        )

    def _recover(self) -> tuple[tuple[NewIntent, ...], tuple[OptionIdea, ...]]:
        collected = []
        now = self.clock()
        for proposal in self.store.unqueued_proposals():
            intent = _reconstitute(proposal)
            if intent.expires_at and intent.expires_at > now:
                collected.append(intent)
            else:
                # Record the expired opportunity as a refused intent so recovery
                # never rescans the same expired proposal forever.
                collected.append(NewIntent(intent.symbol, intent.side, intent.dedupe_key,
                                           status=IntentStatus.SKIPPED, last_error="research_proposal_expired"))
        ideas = tuple(OptionIdea.from_payload(row) for row in self.store.pending_option_ideas(now))
        return tuple(collected), ideas

    def acknowledge_options(self, story_ids: tuple[str, ...]) -> None:
        """Scheduler calls after the options manager has durably handled each story."""
        self.store.acknowledge_options(story_ids)

    async def observe_regime(self) -> tuple[str, str] | None:
        """Quote-only posture calculation; no model, candidate scan, or intent."""
        rows = await self.sources.market(self.clock())
        now = self.clock()
        changes = {}
        for row in rows:
            change = row.metrics.get("session_return")
            if (row.symbol in {"SPY", "QQQ"} and _fresh_quote(row, now, max_age=timedelta(seconds=90))
                    and type(change) in (int, float)
                    and math.isfinite(change)):
                changes[row.symbol] = change
        if changes.keys() != {"SPY", "QQQ"}:
            return None
        regime = "cautious" if changes["SPY"] <= -.01 and changes["QQQ"] <= -.014 else "normal"
        old = self.store.store.kv_get("research_market_regime") or {}
        self.store.store.kv_set("research_market_regime", {"regime": regime, "as_of": now.isoformat()})
        if old.get("regime") and old["regime"] != regime:
            return old["regime"], regime
        return None

    @staticmethod
    def _story_key(event: NewsEvent) -> str:
        return "news:" + hashlib.sha256(event.story_id.encode()).hexdigest()

    @staticmethod
    def _fresh_story(event: NewsEvent, now: datetime) -> bool:
        stamp = event.evidence.published_at
        return stamp is not None and now - timedelta(hours=24) <= stamp <= now and event.evidence.retrieved_at <= now

    def _session(self, now: datetime):
        day = now.astimezone(NY).date()
        today = self.calendar.session_on(day)
        if today is not None and now < today.closes_at:
            return today
        return self.calendar.next_session(day)

    def entry_handoff_due(self, now: datetime) -> bool:
        return bool(self.store.pending_buys(now))

    async def run_entry_handoff(self) -> JobOutcome:
        funnel = _EntryFunnel()
        outcomes = Counter()
        if not self._analysis_lock.locked():
            async with self._analysis_lock:
                await self._sweep_handoffs(funnel, outcomes)
        info = funnel.payload()
        if outcomes:
            # Handoff waits and refusals are observable but not independent
            # research rejections. A terminal refusal has one skipped intent.
            self.ledger.journal.append("research.entry_funnel", job="entry_handoff",
                                       session_date=self.clock().astimezone(NY).date().isoformat(),
                                       handoff_outcomes=dict(outcomes), **info)
        return JobOutcome(JobStatus.DONE, detail={"entry_funnel": info, "handoff_outcomes": dict(outcomes)})

    async def _sweep_handoffs(self, funnel, outcomes):
        elapsed = asyncio.get_running_loop().time
        deadline = elapsed() + 60
        for row in self.store.pending_buys(self.clock()):
            remaining = deadline - elapsed()
            if remaining <= 0:
                break
            details = json.loads(row["details_json"])
            intent = _reconstitute(details["intent"])
            key = row["work_key"]
            try:
                async with asyncio.timeout(min(remaining, 55)):
                    code = await self._validate_handoff(key, intent, funnel, last_hold=details.get("last_hold"))
            except (TimeoutError, SourceUnavailable, StaleBook) as exc:
                if isinstance(exc, TimeoutError):
                    reason = "handoff_input_timeout"
                elif isinstance(exc, StaleBook):
                    reason = "broker_book_unavailable"
                else:
                    reason = "handoff_input_unavailable"
                review = self.store.work_state(f"{key}:reanchor")
                if review is not None and review["status"] == "started" and self.clock() < intent.expires_at:
                    code = self._finish_handoff(key, intent, "handoff_reanchor_unavailable", reasons=(reason,))
                else:
                    code = self._retry_handoff(key, intent, reason)
            outcomes[code] += 1
            if code == "handoff_promoted":
                funnel.buy_candidates += 1

    def _finish_handoff(self, key, intent, code, *, reasons=(), hold_owner=False):
        with self.store.store.transaction():
            if self.store.pending_buy(key)["status"] != "waiting":
                return "handoff_already_processed"
            if hold_owner:
                self.ledger.hold_owner_directive_for_session(intent.symbol, intent.session_date, reasons[0])
            intent_id = None
            if code != "handoff_reanchored":
                queued = intent if code == "handoff_promoted" else replace(
                    intent, status=IntentStatus.SKIPPED, last_error=code,
                    meta={**intent.meta, "handoff_reasons": reasons, "skip_code": code},
                )
                (intent_id,) = self.ledger.enqueue_intents((queued,), job="entry_handoff")
            self.store.finish_pending_buy(key, code, intent_id=intent_id)
        if code not in {"handoff_promoted", "handoff_reanchored", "handoff_owner_news_deferred"}:
            alert = getattr(self.alerts, "alert_blocked_buy", None)
            if alert is not None:
                alert(intent.symbol, code, session_date=intent.session_date, risk_reasons=reasons)
        return code

    def _retry_handoff(self, key, intent, reason):
        now = self.clock()
        if now >= intent.expires_at:
            return self._finish_handoff(key, intent, "handoff_expired", reasons=(reason,))
        self.store.finish_pending_buy(key, "waiting",
                                      retry_at=min(now + timedelta(minutes=1), intent.expires_at), hold_reason=reason)
        return reason

    async def _validate_handoff(self, key, intent, funnel, *, last_hold=None):
        symbol = intent.symbol
        reanchor_key = f"{key}:reanchor"

        def refuse(code, *reasons):
            if code == "handoff_expired" and not reasons and last_hold:
                reasons = (last_hold,)
            return self._finish_handoff(key, intent, code, reasons=reasons)

        def check_identity():
            now = self.clock()
            if now >= intent.expires_at:
                return "handoff_expired", None
            open_intents = [i for i in self.ledger.list_intents(symbol=symbol) if not i.terminal]
            if (self.store.has_pending_confirmation(symbol, now) or self.store.has_unqueued_exit(symbol, now)
                    or any(i.side == "sell" for i in open_intents)):
                return "handoff_exit_pending", None
            if any(i.side == "buy" and i.dedupe_key != intent.dedupe_key for i in open_intents):
                return "handoff_buy_pending", None
            current = self.ledger.current_thesis(symbol)
            if current is None or current.thesis_id != intent.thesis_ids[0]:
                return "handoff_thesis_changed", None
            holding = next((h for h in self.ledger.snapshot().holdings if h.symbol == symbol and h.qty > 0), None)
            position_id = holding.meta.position_id if holding and holding.meta else None
            if position_id != intent.position_id or (holding is not None and position_id is None):
                return "handoff_position_changed", None
            return None, holding

        def gate_rejection():
            gate = self.gate.is_entry_eligible(
                symbol, self.clock(), **({"allow_owner": True} if owner else {}),
            )
            if not gate.ok:
                if owner and "owner_directive_session_hold" in gate.reasons:
                    return refuse("handoff_owner_news_deferred", "owner_directive_session_hold")
                if gate.reasons and set(gate.reasons) <= _HANDOFF_TRANSIENT:
                    return self._retry_handoff(key, intent, gate.reasons[0])
                return refuse("handoff_gate_refused", *gate.reasons)
            if (gate.thesis_id != intent.thesis_ids[0] or gate.direction != Direction.BUY
                    or type(gate.effective_conviction) is not int
                    or gate.effective_conviction < intent.meta["minimum_conviction"]):
                return refuse("handoff_gate_refused", "buy_thesis_or_conviction_changed")
            if intent.position_id is not None:
                grade, may_initialize = self.ledger.top_up_baseline(intent.position_id)
                if not may_initialize and (grade is None or gate.effective_conviction < grade):
                    return refuse("handoff_gate_refused", "top_up_baseline_changed")
            return None

        generation = intent.meta.get("owner_directive_added")
        owner = self.directives(now=self.clock()).get(symbol) if generation else None
        if generation and (owner is None or generation != owner.added.isoformat()):
            return refuse("handoff_gate_refused", "owner_directive_changed")
        if self.store.pending_buy(reanchor_key) is not None:
            return refuse("handoff_reanchored")
        code, holding = check_identity()
        if code:
            return refuse(code)
        if self.store.work_state(reanchor_key) is not None:
            return refuse("handoff_reanchor_unavailable", "targeted_review_already_attempted")
        now = self.clock()
        session = self.calendar.session_on(intent.session_date)
        if session is None or now >= session.closes_at - timedelta(minutes=10):
            self.store.finish_pending_buy(key, "waiting", retry_at=intent.expires_at)
            return "handoff_execution_window_closed"
        refresh = getattr(self.sources, "refresh_entry_inputs", None)
        if refresh is not None:
            await refresh(symbol, now)
        code, holding = check_identity()
        if code:
            return refuse(code)
        if rejection := gate_rejection():
            return rejection
        material_since = getattr(self.sources, "handoff_material_news", None)
        current = self.ledger.current_thesis(symbol)
        inputs_as_of = utc_datetime(current.payload.get("inputs_as_of"))
        material = await material_since(
            symbol, inputs_as_of, self.clock(), **({"owner": True} if owner else {}),
        ) if material_since and inputs_as_of else ()
        code, holding = check_identity()
        if code:
            return refuse(code)
        if self.clock() >= session.closes_at - timedelta(minutes=10):
            self.store.finish_pending_buy(key, "waiting", retry_at=intent.expires_at)
            return "handoff_execution_window_closed"
        if rejection := gate_rejection():
            return rejection
        if owner:
            if material:
                code = self._finish_handoff(
                    key, intent, "handoff_owner_news_deferred", hold_owner=True,
                    reasons=("new_material_issuer_news_after_owner_review",),
                )
                if code == "handoff_owner_news_deferred":
                    self._owner_event(owner, "handoff_deferred", evidence_ids=tuple(item.evidence_id for item in material))
                    await self._owner_session_hold_alert(
                        symbol, intent.session_date,
                        "New material issuer news paused both owner lanes for this session. "
                        "A fresh pre-open review may reassess the active directive next session.\n"
                        + "\n".join(f"{item.title}: {item.url}" for item in material[:3]),
                    )
                return code
            if intent.reference_price is None:
                return refuse("handoff_reanchor_unavailable", "missing_owner_reference_price")
            return refuse("handoff_promoted")
        if material and intent.meta.get("handoff_reviewed"):
            return refuse("handoff_reanchor_unavailable", "new_material_news_after_single_review")
        if intent.reference_price is not None and not material:
            return refuse("handoff_promoted")
        now = self.clock()
        security = (await self.sources.securities((symbol,), now)).get(symbol)
        if security is None or not _fresh_quote(security, self.clock(), max_age=timedelta(seconds=90)):
            return self._retry_handoff(key, intent, "quote_stale_or_unavailable")
        stories = await self.sources.background((symbol,))
        security = replace(security, evidence=(*material, *stories.get(symbol, ()), *security.evidence))
        if not _fresh_quote(security, self.clock(), max_age=timedelta(seconds=90)):
            return self._retry_handoff(key, intent, "quote_stale_or_unavailable")
        code, holding = check_identity()
        if code:
            return refuse(code)
        if rejection := gate_rejection():
            return rejection
        if not self.store.claim_work(reanchor_key, symbol, self.clock()):
            return refuse("handoff_reanchor_unavailable", "targeted_review_already_attempted")
        try:
            await self._review(intent.origin, reanchor_key, security, holding, None, session,
                               self.clock(), funnel, reanchor=intent)
        except ProviderUnavailable as exc:
            reasons = tuple(sorted({error.kind for error in exc.attempts})) or ("no_available_route",)
            self.store.finish_work(reanchor_key, "unavailable", detail={"reason": reasons})
            return refuse("handoff_reanchor_unavailable", *reasons)
        if self.store.pending_buy(reanchor_key) is not None:
            return refuse("handoff_reanchored")
        return refuse("handoff_reanchor_unavailable", "targeted_review_did_not_qualify")

    async def _review(self, kind, key, security, holding, story, session, now, funnel, *,
                      reanchor=None, owner: FocusItem | None = None, held_owner_option: FocusItem | None = None,
                      deadline=None) -> None:
        symbol = security.symbol
        position_id = holding.meta.position_id if holding and holding.meta else None
        entry = self.ledger.entry_thesis(position_id) if position_id else None
        baseline = self.ledger.last_filled_thesis(position_id) if position_id else None
        current = self.ledger.current_thesis(symbol)
        evidence = {
            item.evidence_id: item for item in security.evidence
            if item.retrieved_at <= now and (item.published_at is None or item.published_at <= now)
        }
        if story:
            evidence = {story.evidence.evidence_id: story.evidence, **evidence}
        baseline_evidence = tuple(evidence.values())[:24]
        grounding_evidence = tuple(
            item for item in baseline_evidence if item.published_at is not None and not _edgar_index_only(item.url)
        )
        recent_primary = next((
            item for item in grounding_evidence
            if now - item.published_at <= timedelta(days=120) and _edgar_filing(item.url)
        ), None)
        filing_evidence = tuple({
            item.evidence_id: item for item in security.priority_evidence
            if item.evidence_id not in evidence and item.retrieved_at <= now
            and (item.published_at is None or item.published_at <= now)
        }.values())[:3]
        # Metadata-only filings get reserved slots, but cannot suppress search.
        # Keep both kinds of baseline grounding signals in the visible evidence.
        anchors = {
            item.evidence_id for item in (next(iter(grounding_evidence), None), recent_primary) if item is not None
        }
        kept = list(baseline_evidence)
        for index in reversed(range(len(kept))):
            if len(kept) + len(filing_evidence) <= 24:
                break
            if kept[index].evidence_id not in anchors:
                del kept[index]
        presented_evidence = (*kept, *filing_evidence)
        context = self.context()
        market = self.store.store.kv_get("research_market_regime") or {}
        market_at = utc_datetime(market.get("as_of"))
        if market_at is not None and timedelta() <= now - market_at <= timedelta(days=1):
            context = {**context, **market}
        approved_notes = research_notes_payload(symbol, load_notes(
            enabled=getattr(self.config, "RESEARCH_SHARE_NOTES", False),
            today=now.astimezone(NY).date(),
        ))
        if approved_notes:
            context = {**context, "unverified_owner_notes": approved_notes}
        price = security.price
        quote_at = security.quote_at
        # Catch-up uses the weekly prompt but is a fallback, not an original
        # scheduled decision. Keep its preopen/regular recovery semantics.
        scheduled_job = kind in _SCHEDULED and key.startswith(f"{kind}:") and story is None
        scheduled = scheduled_job and now < session.opens_at
        fresh_quote = _fresh_quote(security, now, max_age=timedelta(seconds=90) if scheduled else timedelta(minutes=20))
        anchor_kind = "quote" if fresh_quote else None
        if (scheduled or owner) and not fresh_quote:
            price = None
            previous_session = getattr(self.calendar, "previous_session", None)
            previous = previous_session(session.date) if previous_session is not None else None
            completed = getattr(self.sources, "completed_close", None)
            if previous is not None and completed is not None:
                if kind == "prescan" and reanchor is None:
                    try:
                        async with asyncio.timeout(_PRESCAN_PRICE_TIMEOUT):
                            price = await completed(symbol, previous.date)
                    except TimeoutError:
                        price = None
                else:
                    price = await completed(symbol, previous.date)
                if price is not None:
                    anchor_kind = "completed_split_adjusted_close"
        if anchor_kind is None and scheduled and now.astimezone(NY).date() == session.date:
            brief = replace(security, price=security.analysis_price, quote_at=security.analysis_quote_at)
            if _fresh_quote(brief, now, max_age=timedelta(minutes=20)):
                price, quote_at = brief.price, brief.quote_at
                anchor_kind = "recent_research_quote"
        metrics = dict(security.metrics)
        if anchor_kind:
            metrics["reference_price_kind"] = anchor_kind
        if security.metric_provenance:
            metrics["point_in_time_provenance"] = security.metric_provenance
        data = ResearchInput(
            symbol, now, security.name, security.sector, float(price) if anchor_kind else None,
            quote_at if anchor_kind in {"quote", "recent_research_quote"} else None,
            metrics, presented_evidence,
            (story.evidence.evidence_id,) if story else (), _safe_previous(entry),
            _safe_previous(baseline or current), {"held": True, "opened_at": holding.meta.opened_at.isoformat()
                                               if holding.meta and holding.meta.opened_at else None}
            if holding else None, context,
        )
        task = ResearchTask.NEWS if story else ResearchTask.REVIEW if holding else ResearchTask.THESIS
        # Dated feed evidence can ground this evaluation directly; external
        # web search is useful when it fills a gap, not a second mandatory feed.
        needs_search = not grounding_evidence
        best_ideas = task is ResearchTask.THESIS and (scheduled_job or (reanchor is not None and kind in _SCHEDULED))
        search_policy = ({"long_search": True} if (reanchor is None and story is None
                         and kind in {"prescan", "weekly", "market_move"} and key.startswith(f"{kind}:")) else {})
        self._require_prescan_time(deadline, SCHEDULED_REVIEW_TRANSPORT_SECONDS)
        if owner:
            if data.price is None:
                raise OwnerReviewUnavailable("no_quote_or_completed_close")
            result = await self.analyst.evaluate(data, task=task, grounded=True, owner=owner, **search_policy)
        elif best_ideas:
            # A dated market story or provider metric does not replace filing
            # research. Do not infer official issuer domains from ticker symbols.
            result = await self.analyst.evaluate(data, task=task, grounded=recent_primary is None, best_ideas=True,
                                                 **search_policy)
        else:
            result = await self.analyst.evaluate(
                data, task=task, grounded=bool(held_owner_option) or needs_search,
                **({"single_review": True} if held_owner_option else {}),
                **search_policy,
            )
        self._require_prescan_time(deadline)
        record = result.record
        direction = record.draft.direction
        entry_direction = direction in {Direction.BUY, Direction.BEARISH}
        if not holding or entry_direction:
            funnel.thesis_written += 1
        # An all-HOLD set can be valid, but not when nobody had a usable
        # quote and the analyst may simply have stood aside for that reason.
        news_ok = getattr(self.sources, "news_ok", None)
        if (not holding and security.tradable and _fresh_quote(security, now, max_age=timedelta(seconds=90))
                and (news_ok is None or news_ok(symbol, now))):
            funnel.healthy_unheld_theses += 1
        floor = 8 if story or direction is Direction.BEARISH or (
            str(context.get("regime", "")).lower() in {"cautious", "risk_off", "bear"}
        ) else 7
        if holding and direction is Direction.BUY:
            floor = max(floor, 8)
        if reanchor is not None:
            floor = max(floor, reanchor.meta["minimum_conviction"])
        conviction_ok = (
            (direction is Direction.BUY if owner else entry_direction and record.raw_conviction >= 7)
            and record.effective_conviction >= floor
        )
        if conviction_ok:
            funnel.passed_conviction += 1
            refresh = getattr(self.sources, "refresh_entry_inputs", None)
            current_time = self.clock()
            if (refresh is not None and fresh_quote and security.tradable
                    and session.opens_at <= current_time < session.closes_at):
                await refresh(symbol, current_time)
        role = ThesisRole.HELD_REVIEW if holding else ThesisRole.ENTRY_CANDIDATE
        payload = record.model_dump(mode="json") | {
            "inputs": result.inputs, "job": kind, "fallbacks": [
                f"{err.provider}:{err.kind}" for err in result.result.attempts
            ],
        }
        if owner:
            payload.update(
                owner_directive_added=owner.added.isoformat(), owner_conviction=owner.owner_conviction,
                model_effective_conviction=record.raw_conviction, owner_review_stop=result.owner_stop_price,
            )
        evidence_ids = tuple(dict.fromkeys(ref for claim in record.draft.claims for ref in claim.evidence_ids))
        option_from_held = (
            held_owner_option is not None and direction in {Direction.BUY, Direction.HOLD}
            and record.draft.thesis_change in {"unchanged", "strengthened"}
            and any(item.evidence_id in evidence_ids and item.published_at for item in record.evidence)
        )
        if option_from_held:
            payload.update(
                owner_option_directive_added=held_owner_option.added.isoformat(),
                owner_option_conviction=held_owner_option.owner_conviction,
            )
        if owner and direction is not Direction.BUY:
            assert isinstance(record.draft, OwnerThesisDraft) and record.draft.veto_claim_index is not None
            claim = record.draft.claims[record.draft.veto_claim_index]
            payload.update(owner_veto_claim_index=record.draft.veto_claim_index,
                           owner_veto_evidence_ids=claim.evidence_ids)
            with self.store.store.transaction():
                self._require_prescan_time(deadline)
                thesis_id = self.ledger.record_thesis(
                    symbol, payload, role, source=f"{record.provider}/{record.model}", decision_id=key,
                    generated_at=record.generated_at,
                )
                first = self.ledger.latch_owner_directive_veto(symbol, owner.added)
                self.store.finish_work(key, "no_action", thesis_id=thesis_id, detail={"reason": "owner_directive_veto"})
                self._require_prescan_time(deadline)
            funnel.rejected_by_reason["owner_directive_veto"] += 1
            self._owner_event(owner, "vetoed", raw_conviction=record.raw_conviction,
                              thesis_id=thesis_id, evidence_ids=claim.evidence_ids)
            if first:
                cited = [
                    item.url for item in record.evidence
                    if item.evidence_id in claim.evidence_ids
                ]
                await self._owner_alert(owner, "veto", f"{claim.text}\n" + "\n".join(cited[:3]))
            return
        entry_blockers = ()
        confirmation_requested = False
        new_owner_hold = False
        with self.store.store.transaction():
            self._require_prescan_time(deadline)
            checked_at = self.clock()
            exit_pending = self.store.has_unqueued_exit(symbol, now) or self.store.has_pending_confirmation(symbol, now)
            waiting_owner = (
                owner is None and self.store.has_waiting_owner_buy(symbol, session.date.isoformat(), checked_at)
            )
            protect_held_option = (
                holding is not None and held_owner_option is None and direction is Direction.HOLD
                and record.draft.thesis_change in {"unchanged", "strengthened"}
                and self._has_pending_held_owner_option(symbol, session.date, checked_at)
            )
            if (waiting_owner and kind in {"news", "earnings"}
                    and direction in {Direction.AVOID, Direction.BEARISH, Direction.EXIT_PROPOSED}):
                new_owner_hold = self.ledger.hold_owner_directive_for_session(
                    symbol, session.date, f"{kind}:{direction.value}",
                )
            thesis_id = self.ledger.record_thesis(
                symbol, payload, role, source=f"{record.provider}/{record.model}", decision_id=key,
                generated_at=record.generated_at,
                activate=not (exit_pending or (waiting_owner and not holding) or protect_held_option),
            )
            if option_from_held and not exit_pending and security.tradable:
                self._qualify_owner_option(
                    held_owner_option, thesis_id, evidence_ids, record, session, funnel,
                )
            if direction is not Direction.EXIT_PROPOSED or not position_id:
                rejected = self._record_entry(
                    key, symbol, kind, security, holding, position_id, baseline, story, session,
                    thesis_id, record, fresh_quote, price, evidence_ids, floor, conviction_ok, funnel,
                    scheduled=scheduled and not exit_pending, anchor_kind=anchor_kind, reanchor=reanchor,
                    owner=owner, owner_stop=result.owner_stop_price if owner else None,
                    deadline=deadline,
                )
                if entry_direction or not holding:
                    funnel.rejected_by_reason.update(rejected)
                if conviction_ok and (owner or session.opens_at <= self.clock() < session.closes_at):
                    entry_blockers = tuple(reason for reason in rejected if reason in _INPUT_BLOCKERS)
                if story:
                    self.ledger.journal.append(
                        "news_reaction", story_id=story.story_id, symbol=symbol, thesis_id=thesis_id,
                        direction=direction, raw_conviction=record.raw_conviction,
                        effective_conviction=record.effective_conviction,
                    )
            elif kind == "news" and self.clock() >= session.closes_at:
                self.store.finish_work(key, "no_action", thesis_id=thesis_id, detail={"reason": "live_session_closed"})
            elif not exit_pending and reanchor is None:
                current = self.clock()
                expiration = current + timedelta(minutes=30)
                if story and story.evidence.published_at:
                    expiration = min(expiration, story.evidence.published_at + timedelta(hours=24))
                if kind == "news":
                    expiration = min(expiration, session.closes_at)
                self.store.schedule_confirmation(
                    key, symbol, position_id, current, expiration, {
                        "thesis_id": thesis_id, "inputs": result.inputs, "provider": result.result.route.provider,
                        "model": result.result.route.model, "evidence_ids": evidence_ids, "kind": kind,
                        "story_id": story.story_id if story else None,
                    },
                )
                confirmation_requested = True
            else:
                self.store.finish_work(key, "no_action", thesis_id=thesis_id, detail={"reason": "exit_already_pending"})
            self._require_prescan_time(deadline)
        if new_owner_hold:
            self.ledger.journal.append(
                "research.owner_directive", symbol=symbol, reason="owner_directive", outcome="session_held",
                session_date=session.date.isoformat(), direction=direction, source=kind, thesis_id=thesis_id,
            )
            await self._owner_session_hold_alert(
                symbol, session.date,
                f"A {kind} review returned {direction.value}. Both owner lanes are paused for this session "
                "without using either shot; the next pre-open review may reassess the directive.",
            )
        if direction is not Direction.EXIT_PROPOSED or not position_id:
            if owner and entry_blockers:
                await self._owner_unavailable(key, owner, ", ".join(entry_blockers), funnel)
                return
            if entry_blockers and self.alerts is not None:
                alert = self.alerts.alert(
                    "warning", "Research entry input unavailable", f"{symbol}: {', '.join(entry_blockers)}",
                    key=f"research_entry_inputs:{symbol}:{':'.join(entry_blockers)}",
                )
                if inspect.isawaitable(alert):
                    await alert
            return
        if confirmation_requested:
            await self._confirm(key, holding, deadline=deadline)

    def _owner_event(self, owner: FocusItem, outcome: str, **detail) -> None:
        self.ledger.journal.append(
            "research.owner_directive", symbol=owner.symbol, owner_directive_added=owner.added.isoformat(),
            owner_conviction=owner.owner_conviction, reason="owner_directive", outcome=outcome, **detail,
        )

    async def _owner_alert(self, owner: FocusItem, kind: str, body: str) -> None:
        now = self.clock()
        suffix = f":{now.astimezone(NY).date()}" if kind == "unavailable" else ""
        await self._publish_owner_alert(
            owner.symbol, kind, body, f"owner_directive:{kind}:{owner.symbol}:{owner.added}{suffix}", now,
        )

    async def _owner_session_hold_alert(self, symbol: str, session: date, body: str) -> None:
        await self._publish_owner_alert(
            symbol, "handoff deferred", body, f"owner_directive:session_hold:{symbol}:{session}", self.clock(),
        )

    async def _publish_owner_alert(self, symbol: str, kind: str, body: str, key: str, now: datetime) -> None:
        alert = getattr(self.alerts, "alert", None)
        if alert is None:
            return
        if not self.store.claim_work(key, symbol, now):
            return
        result = alert("warning", f"Owner directive {kind}", f"{symbol}: {body}", key=key)
        if inspect.isawaitable(result):
            await result
        self.store.finish_work(key, "no_action")

    async def _owner_unavailable(self, key, owner: FocusItem, reason: str, funnel, *, failure_codes=None) -> None:
        self.store.finish_work(key, "unavailable", detail={
            "reason": "owner_directive_unavailable", "input": reason, **(failure_codes or {}),
        })
        funnel.rejected_by_reason["owner_directive_unavailable"] += 1
        self._owner_event(owner, "unavailable", input=reason)
        await self._owner_alert(owner, "unavailable", reason)

    def _defer_prescan_confirmation(self, key, thesis_id, attempts) -> None:
        with self.store.store.transaction():
            if attempts < 3:
                self.store.finish_confirmation(key, "waiting", retry_at=self.clock() + timedelta(minutes=2))
            else:
                self.store.finish_confirmation(key, "unavailable")
                self.store.finish_work(key, "confirmation_unavailable", thesis_id=thesis_id,
                                       detail={"reason": "prescan_time_budget"})

    async def _confirm(self, key: str, holding, *, deadline=None) -> None:
        if self._prescan_remaining(deadline) <= _PRESCAN_CONFIRM_BUDGET:
            return  # Leave for a later lane without claiming or starting a POST.
        row = self.store.claim_confirmation(key, self.clock())
        if row is None:
            return
        request = json.loads(row["payload_json"])
        thesis_id, symbol, position_id = request["thesis_id"], row["symbol"], row["position_id"]
        if not holding or not holding.meta or holding.meta.position_id != position_id:
            with self.store.store.transaction():
                self.store.finish_confirmation(key, "abandoned")
                self.store.finish_work(key, "no_action", thesis_id=thesis_id, detail={"reason": "position_changed"})
            return
        data = ResearchInput.from_payload(request["inputs"])
        primary = ModelRoute(request["provider"], request["model"])
        if self._prescan_remaining(deadline) <= 0:
            self._defer_prescan_confirmation(key, thesis_id, row["attempts"])
            return
        try:
            confirmation = await self.analyst.confirm_exit(data, primary)
        except ProviderUnavailable as exc:
            with self.store.store.transaction():
                if self._prescan_remaining(deadline) <= 0:
                    self._defer_prescan_confirmation(key, thesis_id, row["attempts"])
                elif row["attempts"] < 3 and _can_retry(exc):
                    self.store.finish_confirmation(key, "waiting", retry_at=self.clock() + timedelta(minutes=2))
                else:
                    self.store.finish_confirmation(key, "unavailable")
                    self.store.finish_work(key, "confirmation_unavailable", thesis_id=thesis_id, detail={
                        "failures": [f"{err.provider}:{err.kind}" for err in exc.attempts],
                        **exc.failure_codes,
                    })
            return

        try:
            with self.store.store.transaction():
                self._require_prescan_time(deadline)
                conf_id = self.ledger.record_thesis(
                    symbol, confirmation.result.value.model_dump(mode="json") | {
                        "inputs": confirmation.inputs,
                        "degraded": confirmation.result.degraded_confirmation,
                        "prompt_version": EXIT_PROMPT_VERSION,
                        "input_hash": confirmation.result.input_hash,
                    },
                    ThesisRole.EXIT_CONFIRMATION,
                    source=f"{confirmation.result.route.provider}/{confirmation.result.route.model}",
                    decision_id=key, generated_at=confirmation.result.completed_at,
                )
                execution_session = self._session(self.clock())
                self._require_prescan_time(deadline)
                if (confirmation.approved and execution_session
                        and self.store.reserve_exit(execution_session.date.isoformat(), position_id)):
                    ids = tuple(dict.fromkeys((*request["evidence_ids"], *confirmation.result.value.evidence_ids)))
                    price = Decimal(str(data.price)) if data.price is not None else None
                    intent = self._intent(key, symbol, "sell", position_id, (thesis_id, conf_id),
                                          price, ids, request["kind"], execution_session)
                    self.store.record_proposal(key, thesis_id, intent)
                else:
                    self.store.finish_work(key, "no_action", thesis_id=thesis_id)
                self.store.finish_confirmation(key, "complete")
                self._require_prescan_time(deadline)
        except _PrescanCutoff:
            self._defer_prescan_confirmation(key, thesis_id, row["attempts"])
            return
        if request["story_id"]:
            self.ledger.journal.append(
                "news_reaction", story_id=request["story_id"], symbol=symbol,
                thesis_id=thesis_id, confirmation_thesis_id=conf_id, direction=Direction.EXIT_PROPOSED,
                confirmed=confirmation.approved, degraded=confirmation.result.degraded_confirmation,
            )

    def _record_entry(self, key, symbol, kind, security, holding, position_id, baseline, story, session,
                      thesis_id, record, fresh_quote, price, evidence_ids, floor, conviction_ok, funnel, *,
                      scheduled=False, anchor_kind=None, reanchor=None, owner=None, owner_stop=None, deadline=None):
        direction = record.draft.direction
        rejected = []
        now = self.clock()
        if direction not in {Direction.BUY, Direction.BEARISH}:
            rejected.append("non_entry_direction")
        elif not conviction_ok:
            rejected.append("conviction_below_floor")
        else:
            if now >= session.closes_at:
                rejected.append("target_session_closed")
            if not security.tradable:
                rejected.append("not_tradable")
            if not fresh_quote:
                rejected.append("quote_stale_or_unavailable")
            if not rejected or (scheduled and direction is Direction.BUY
                                and rejected == ["quote_stale_or_unavailable"]):
                gate = self.gate.is_entry_eligible(symbol, now, **({"allow_owner": True} if owner else {}))
                rejected.extend(gate.reasons)
                if gate.thesis_id != thesis_id:
                    rejected.append("active_thesis_changed")
                elif not gate.ok and not gate.reasons:
                    rejected.append("research_gate_refused")
                elif gate.direction != direction:
                    rejected.append("research_direction_changed")
        qualifies = not rejected
        defer = scheduled and set(rejected) == {"quote_stale_or_unavailable"}
        stock_used = bool(owner and self.ledger.owner_directive_used(symbol, owner.added))
        self._require_prescan_time(deadline)
        # Options own their underlying/chain quote and execution checks. They
        # can use this priced research anchor even while stock awaits a quote.
        if owner and conviction_ok and security.tradable and price is not None:
            self._qualify_owner_option(owner, thesis_id, evidence_ids, record, session, funnel)
        if stock_used:
            self.store.finish_work(key, "no_action", thesis_id=thesis_id,
                                   detail={"reason": "owner_directive_stock_already_used"})
            return ()
        baseline_grade, may_initialize = self.ledger.top_up_baseline(position_id) if position_id else (None, False)
        reaffirmed = (
            position_id is not None and record.draft.thesis_change in {"strengthened", "unchanged"}
            and record.effective_conviction >= 8
            and (may_initialize or (baseline_grade is not None and record.effective_conviction >= baseline_grade))
        )
        if (qualifies or defer) and direction is Direction.BUY and (not holding or reaffirmed):
            if reanchor is None:
                # A newer, qualified BUY replaces (rather than falsely "blocking")
                # an older waiting BUY for the same session. HOLDs remain refusals.
                funnel.handoff_superseded += self.store.supersede_pending_buys(symbol, session.date.isoformat())
            intent = self._intent(key, symbol, "buy", position_id, (thesis_id,), price, evidence_ids, kind, session,
                                  {"raw_conviction": record.raw_conviction,
                                   "effective_conviction": record.effective_conviction,
                                   "minimum_conviction": floor,
                                   "last_filled_conviction": baseline_grade,
                                   "first_reaffirmation": may_initialize,
                                   "sector": security.sector, "thesis_change": record.draft.thesis_change,
                                   "signal_at": record.generated_at.isoformat(),
                                   "baseline_thesis_id": baseline.thesis_id if baseline else None})
            if owner:
                intent = replace(intent, meta={
                    **intent.meta, "adjustment_reason": "owner_directive",
                    "owner_directive_added": owner.added.isoformat(), "owner_conviction": owner.owner_conviction,
                    "owner_review_stop": owner_stop,
                })
            if defer or reanchor is not None:
                reviewed = {"handoff_reviewed": True} if reanchor is not None else {}
                if reanchor is not None and reanchor.reference_price is not None:
                    intent = replace(intent, reference_price=reanchor.reference_price)
                    anchor_kind = reanchor.meta["reference_price_kind"]
                intent = replace(intent, dedupe_key=f"handoff:{key}",
                                 next_attempt_at=session.opens_at + timedelta(minutes=5),
                                 meta={**intent.meta, **reviewed, "scheduled_handoff": True,
                                       "reference_price_kind": anchor_kind})
                self.store.stage_pending_buy(key, thesis_id, intent, anchor_kind or "none")
                funnel.handoff_staged += 1
                if defer:
                    rejected.clear()  # A successfully deferred quote gap is not a rejection.
            else:
                self.store.record_proposal(key, thesis_id, intent)
                funnel.buy_candidates += 1
            if owner:
                self._owner_event(owner, "proposed", raw_conviction=record.raw_conviction,
                                  thesis_id=thesis_id, staged=defer or reanchor is not None)
        else:
            self.store.finish_work(key, "no_action", thesis_id=thesis_id)
            if (qualifies or defer) and direction is Direction.BUY:
                rejected.append("add_thesis_not_reaffirmed")
        if qualifies and reanchor is None and (story or direction is Direction.BEARISH):
            idea_id = story.story_id if story else f"research:{key}"
            idea = OptionIdea(symbol, direction, idea_id, thesis_id, record.raw_conviction,
                              record.effective_conviction, record.generated_at, evidence_ids,
                              "news" if story else "research")
            self.store.record_option_idea(idea_id, idea, session.closes_at)
            funnel.option_ideas += 1
        return tuple(dict.fromkeys(rejected))

    def _has_pending_held_owner_option(self, symbol: str, session: date, now: datetime) -> bool:
        # Look up the exact current thesis after the review, inside the same
        # transaction as activation, rather than trusting the earlier inputs.
        current = self.ledger.current_thesis(symbol)
        if current is None or current.role is not ThesisRole.HELD_REVIEW:
            return False
        added = current.payload.get("owner_option_directive_added")
        if not isinstance(added, str) or current.payload.get("job") != "prescan":
            return False
        return self.store.has_pending_option_idea(f"owner:{symbol}:{added}:{session}", current.thesis_id, now)

    def _qualify_owner_option(self, owner, thesis_id, evidence_ids, record, session, funnel):
        now = self.clock()
        if now >= session.closes_at:
            return
        gate = self.gate.is_entry_eligible(owner.symbol, now, allow_owner=True, owner_lane="option")
        if gate.thesis_id != thesis_id:
            return
        if gate.ok and gate.owner_entry is not None:
            grade = gate.owner_entry.effective_conviction
        elif set(gate.reasons) == {"quote_stale_or_unavailable"}:
            # Persist an idea, not authorization. The option lane must fetch
            # a fresh underlying quote and obtain its own typed proof.
            grade = owner.owner_conviction
        else:
            return
        added = owner.added.isoformat()
        idea_id = f"owner:{owner.symbol}:{added}:{session.date}"
        idea = OptionIdea(
            owner.symbol, Direction.BUY, idea_id, thesis_id, record.raw_conviction,
            grade, record.generated_at, evidence_ids, "owner", added,
        )
        self.store.record_option_idea(idea_id, idea, session.closes_at)
        funnel.option_ideas += 1

    @staticmethod
    def _intent(key, symbol, side, position_id, thesis_ids, price, evidence_ids, kind, session, meta=None):
        eligible_at = session.opens_at + (timedelta(minutes=5) if side == "sell" else timedelta())
        return NewIntent(
            symbol, side, key, decision_id=key, position_id=position_id, thesis_ids=thesis_ids,
            reference_price=price, evidence_ids=evidence_ids, origin=kind, sleeve="core",
            session_date=session.date, expires_at=session.closes_at, next_attempt_at=eligible_at, meta=meta or {},
        )


def _fresh_quote(row: SecurityInputs, now: datetime, *, max_age: timedelta = timedelta(minutes=20)) -> bool:
    price, quoted = row.price, row.quote_at
    return (
        price is not None and math.isfinite(price) and price > 0 and quoted is not None
        and quoted.utcoffset() is not None and now - max_age <= quoted <= now
    )


def _can_retry(error: ProviderUnavailable) -> bool:
    return all(attempt.kind in _RETRYABLE for attempt in error.attempts)
