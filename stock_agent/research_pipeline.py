"""Scheduled research and material-news reactions; returns proposals, never orders."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from stock_agent.analyst import EXIT_PROMPT_VERSION, Analyst, ResearchInput
from stock_agent.ledger import BookSnapshot, IntentStatus, JobStatus, NewIntent, ThesisRole
from stock_agent.llm_client import ModelRoute, ProviderUnavailable
from stock_agent.research_focus import focus_symbols
from stock_agent.research_notes import load_notes, research_notes_payload
from stock_agent.research_schemas import Direction, Evidence, ResearchTask, utc_datetime
from stock_agent.research_store import NY, ResearchStore
from stock_agent.screener import Candidate, Screener

_RETRYABLE = {"auth", "quota", "model_unavailable", "rate_limit", "budget", "transport", "upstream"}


class SourceUnavailable(RuntimeError):
    """A required data request failed rather than returning a healthy empty result."""

@dataclass(frozen=True)
class NewsEvent:
    symbol: str
    story_id: str
    evidence: Evidence
    materiality: int

    def payload(self) -> dict:
        return {"symbol": self.symbol, "story_id": self.story_id, "materiality": self.materiality,
                "evidence": self.evidence.model_dump(mode="json")}

    @classmethod
    def from_payload(cls, payload: dict) -> NewsEvent:
        return cls(payload["symbol"], payload["story_id"],
                   Evidence.model_validate_json(json.dumps(payload["evidence"])), payload["materiality"])


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


class Sources(Protocol):
    async def universe(self, now: datetime) -> tuple[str, ...]: ...

    async def securities(self, symbols: tuple[str, ...], now: datetime) -> dict[str, SecurityInputs]: ...

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

    @classmethod
    def from_payload(cls, payload: dict):
        return cls(
            payload["symbol"], Direction(payload["direction"]), payload["story_id"], payload["thesis_id"],
            payload["raw_conviction"], payload["effective_conviction"],
            datetime.fromisoformat(payload["generated_at"]), tuple(payload["evidence_ids"]),
        )


@dataclass(frozen=True)
class JobOutcome:
    status: JobStatus
    intents: tuple[NewIntent, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    option_candidates: tuple[OptionIdea, ...] = ()


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
                 *, clock=None, context=None, focus=focus_symbols, alerts=None):
        self.config = config
        self.ledger = ledger
        self.calendar = calendar
        self.analyst = analyst
        self.store = store
        self.sources = sources
        self.gate = gate
        self.clock = clock or ledger.now
        self.context = context or (lambda: {})
        self.focus = focus
        self.alerts = alerts
        self._analysis_lock = asyncio.Lock()

    async def run(self, kind: str, snapshot: BookSnapshot | None = None) -> JobOutcome:
        # Avoid overlapping scheduled/news windows superseding a thesis
        # during its independent review. Broker protection has its own lane.
        async with self._analysis_lock:
            try:
                return await self._run(kind, snapshot)
            except SourceUnavailable:
                intents, ideas = self._recover()
                return JobOutcome(JobStatus.FAILED, intents, {"reason": "required_source_unavailable"}, ideas)

    async def _run(self, kind: str, snapshot: BookSnapshot | None = None) -> JobOutcome:
        now = self.clock()
        session = self._session(now)
        if session is None:
            return JobOutcome(JobStatus.SKIPPED, detail={"reason": "no_execution_session"})
        held = {row.symbol: row for row in (snapshot or self.ledger.snapshot()).holdings if row.qty > 0}
        for pending in self.store.due_confirmations(now):
            await self._confirm(pending["work_key"], held.get(pending["symbol"]))
        focus = tuple(self.focus(now=now))
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
            stories = [item for item in stories if self.store.ready(self._story_key(item), as_of)][:4]
            securities = await self.sources.securities(tuple(dict.fromkeys(item.symbol for item in stories)), now)
            work = [(item.symbol, self._story_key(item), item) for item in stories]
        else:
            securities = await self.sources.securities(symbols, now)
            ranked = Screener.rank([
                Candidate(row.symbol, row.name, row.sector, row.tradable, row.metrics, row.symbol in focus)
                for row in securities.values()
            ])
            chosen = tuple(dict.fromkeys((*held, *focus, *(r["symbol"] for r in ranked[:8]))))
            background = getattr(self.sources, "background", None)
            if background is not None:
                stories = await background(chosen)
                for symbol in chosen:
                    if symbol in securities:
                        row = securities[symbol]
                        securities[symbol] = replace(row, evidence=(*stories.get(symbol, ()), *row.evidence))
            work = [(sym, f"{kind}:{now.astimezone(NY).date()}:{sym}", None) for sym in chosen]

        failed = attempted = deferred = 0
        for symbol, key, story in work:
            item = securities.get(symbol, SecurityInputs(symbol, symbol, "Unknown"))
            as_of = self.clock()
            if not self.store.claim_work(key, symbol, as_of):
                state = self.store.work_state(key)
                if state is not None and state["status"] == "retry":
                    deferred += 1
                continue
            attempted += 1
            try:
                await self._review(kind, key, item, held.get(symbol), story, session, as_of)
            except ProviderUnavailable as exc:
                failed += 1
                state = self.store.work_state(key)
                can_retry = state["attempts"] < 3 and _can_retry(exc)
                if can_retry:
                    deferred += 1
                self.store.finish_work(
                    key, "retry" if can_retry else "unavailable",
                    detail={
                        "failures": [f"{err.provider}:{err.kind}" for err in exc.attempts], "kind": kind,
                        "story": story.payload() if can_retry and story else None,
                    },
                    retry_at=self.clock() + timedelta(minutes=2 if kind == "news" else 5) if can_retry else None,
                )
        collected, ideas = self._recover()
        incomplete = (attempted and failed == attempted and not collected and not ideas) or (deferred and kind != "news")
        status = JobStatus.FAILED if incomplete else JobStatus.DONE
        return JobOutcome(status, collected, {"reviewed": attempted, "unavailable": failed, "deferred": deferred}, ideas)

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
        return stamp is not None and now - timedelta(hours=24) <= stamp <= now

    def _session(self, now: datetime):
        day = now.astimezone(NY).date()
        today = self.calendar.session_on(day)
        if today is not None and now < today.closes_at:
            return today
        return self.calendar.next_session(day)

    async def _review(self, kind, key, security, holding, story, session, now) -> None:
        symbol = security.symbol
        position_id = holding.meta.position_id if holding and holding.meta else None
        entry = self.ledger.entry_thesis(position_id) if position_id else None
        baseline = self.ledger.last_filled_thesis(position_id) if position_id else None
        current = self.ledger.current_thesis(symbol)
        evidence = {item.evidence_id: item for item in security.evidence}
        if story:
            evidence = {story.evidence.evidence_id: story.evidence, **evidence}
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
        fresh_quote = _fresh_quote(security, now)
        metrics = dict(security.metrics)
        if security.metric_provenance:
            metrics["point_in_time_provenance"] = security.metric_provenance
        data = ResearchInput(
            symbol, now, security.name, security.sector, float(price) if fresh_quote else None,
            quote_at if fresh_quote else None, metrics, tuple(evidence.values())[:24],
            (story.evidence.evidence_id,) if story else (), _safe_previous(entry),
            _safe_previous(baseline or current), {"held": True, "opened_at": holding.meta.opened_at.isoformat()
                                               if holding.meta and holding.meta.opened_at else None}
            if holding else None, context,
        )
        task = ResearchTask.NEWS if story else ResearchTask.REVIEW if holding else ResearchTask.THESIS
        result = await self.analyst.evaluate(data, task=task)
        record = result.record
        direction = record.draft.direction
        role = ThesisRole.HELD_REVIEW if holding else ThesisRole.ENTRY_CANDIDATE
        payload = record.model_dump(mode="json") | {
            "inputs": result.inputs, "job": kind, "fallbacks": [
                f"{err.provider}:{err.kind}" for err in result.result.attempts
            ],
        }
        evidence_ids = tuple(dict.fromkeys(ref for claim in record.draft.claims for ref in claim.evidence_ids))
        entry_blockers = ()
        confirmation_requested = False
        with self.store.store.transaction():
            exit_pending = self.store.has_unqueued_exit(symbol, now) or self.store.has_pending_confirmation(symbol, now)
            thesis_id = self.ledger.record_thesis(
                symbol, payload, role, source=f"{record.provider}/{record.model}", decision_id=key,
                generated_at=record.generated_at,
                activate=not exit_pending,
            )
            if direction is not Direction.EXIT_PROPOSED or not position_id:
                entry_blockers = self._record_entry(
                    key, symbol, kind, security, holding, position_id, baseline, story, session,
                    thesis_id, record, fresh_quote, price, evidence_ids, context,
                )
                if story:
                    self.ledger.journal.append(
                        "news_reaction", story_id=story.story_id, symbol=symbol, thesis_id=thesis_id,
                        direction=direction, raw_conviction=record.raw_conviction,
                        effective_conviction=record.effective_conviction,
                    )
            elif not exit_pending:
                current = self.clock()
                expiration = current + timedelta(minutes=30)
                if story and story.evidence.published_at:
                    expiration = min(expiration, story.evidence.published_at + timedelta(hours=24))
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
        if direction is not Direction.EXIT_PROPOSED or not position_id:
            if entry_blockers and self.alerts is not None:
                alert = self.alerts.alert(
                    "warning", "Research entry input unavailable", f"{symbol}: {', '.join(entry_blockers)}",
                    key=f"research_entry_inputs:{symbol}:{':'.join(entry_blockers)}",
                )
                if inspect.isawaitable(alert):
                    await alert
            return
        if confirmation_requested:
            await self._confirm(key, holding)

    async def _confirm(self, key: str, holding) -> None:
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
        try:
            confirmation = await self.analyst.confirm_exit(data, primary)
        except ProviderUnavailable as exc:
            with self.store.store.transaction():
                if row["attempts"] < 3 and _can_retry(exc):
                    self.store.finish_confirmation(key, "waiting", retry_at=self.clock() + timedelta(minutes=2))
                else:
                    self.store.finish_confirmation(key, "unavailable")
                    self.store.finish_work(key, "confirmation_unavailable", thesis_id=thesis_id, detail={
                        "failures": [f"{err.provider}:{err.kind}" for err in exc.attempts],
                    })
            return

        with self.store.store.transaction():
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
        if request["story_id"]:
            self.ledger.journal.append(
                "news_reaction", story_id=request["story_id"], symbol=symbol,
                thesis_id=thesis_id, confirmation_thesis_id=conf_id, direction=Direction.EXIT_PROPOSED,
                confirmed=confirmation.approved, degraded=confirmation.result.degraded_confirmation,
            )

    def _record_entry(self, key, symbol, kind, security, holding, position_id, baseline, story, session,
                      thesis_id, record, fresh_quote, price, evidence_ids, context):
        direction = record.draft.direction
        gate = self.gate.is_entry_eligible(symbol, self.clock()) if fresh_quote else None
        floor = 8 if story or str(context.get("regime", "")).lower() in {"cautious", "risk_off", "bear"} else 7
        qualifies = (
            gate is not None and gate.ok and gate.thesis_id == thesis_id and security.tradable
            and record.raw_conviction >= 7 and record.effective_conviction >= floor
        )
        baseline_grade = baseline.payload.get("effective_conviction") if baseline else None
        strengthened = (
            position_id is not None and baseline is not None and type(baseline_grade) is int
            and record.effective_conviction >= baseline_grade and record.draft.thesis_change == "strengthened"
        )
        if qualifies and direction is Direction.BUY and (not holding or strengthened):
            intent = self._intent(key, symbol, "buy", position_id, (thesis_id,), price, evidence_ids, kind, session,
                                  {"raw_conviction": record.raw_conviction,
                                   "effective_conviction": record.effective_conviction,
                                   "minimum_conviction": floor,
                                   "last_filled_conviction": baseline_grade,
                                   "sector": security.sector, "thesis_change": record.draft.thesis_change,
                                   "signal_at": record.generated_at.isoformat(),
                                   "baseline_thesis_id": baseline.thesis_id if baseline else None})
            self.store.record_proposal(key, thesis_id, intent)
        else:
            self.store.finish_work(key, "no_action", thesis_id=thesis_id)
        if qualifies and story and direction in {Direction.BEARISH, Direction.BUY}:
            idea = OptionIdea(symbol, direction, story.story_id, thesis_id, record.raw_conviction,
                              record.effective_conviction, record.generated_at, evidence_ids)
            self.store.record_option_idea(story.story_id, idea, session.closes_at)
        if not qualifies and direction in {Direction.BUY, Direction.BEARISH} and record.effective_conviction >= floor:
            health_reasons = {"broker_book_blocked", "broker_book_unavailable", "quote_stale_or_unavailable",
                              "news_stale_or_unavailable", "llm_unavailable", "research_health_unavailable"}
            if not fresh_quote:
                return ("quote_stale_or_unavailable",) if session.opens_at <= self.clock() < session.closes_at else ()
            if gate is not None:
                return tuple(reason for reason in gate.reasons if reason in health_reasons)
        return ()

    @staticmethod
    def _intent(key, symbol, side, position_id, thesis_ids, price, evidence_ids, kind, session, meta=None):
        return NewIntent(
            symbol, side, key, decision_id=key, position_id=position_id, thesis_ids=thesis_ids,
            reference_price=price, evidence_ids=evidence_ids, origin=kind, sleeve="core",
            session_date=session.date, expires_at=session.closes_at, next_attempt_at=session.opens_at, meta=meta or {},
        )


def _fresh_quote(row: SecurityInputs, now: datetime, *, max_age: timedelta = timedelta(minutes=20)) -> bool:
    price, quoted = row.price, row.quote_at
    return (
        price is not None and math.isfinite(price) and price > 0 and quoted is not None
        and quoted.utcoffset() is not None and now - max_age <= quoted <= now
    )


def _can_retry(error: ProviderUnavailable) -> bool:
    return all(attempt.kind in _RETRYABLE for attempt in error.attempts)
