"""Retrospective research audit. It never creates theses, proposals or intents."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from stock_agent.analyst import ResearchInput
from stock_agent.llm_client import ModelRoute, ProviderUnavailable
from stock_agent.research_schemas import Direction, Evidence
from stock_agent.research_store import week_key

logger = logging.getLogger(__name__)


async def run_shadow_research(analyst, store, now: datetime, *, buys: bool, weekly: bool) -> dict:
    if not (buys or weekly):
        return {}
    now = now.astimezone(UTC)
    # Catch weekend/off-session decisions as well as today's. The persistent
    # work key prevents replay across closes or a process restart.
    rows = store.store.query(
        """SELECT thesis_id, symbol, generated_at, payload_json FROM theses
        WHERE role IN ('entry_candidate','held_review') AND generated_at >= ? AND generated_at <= ?
        ORDER BY generated_at DESC, thesis_id""",
        ((now - timedelta(days=7)).isoformat(), now.isoformat()),
    )
    ideas = [(row, json.loads(row["payload_json"])) for row in rows]
    buy_ideas = [(row, payload) for row, payload in ideas
                 if payload.get("draft", {}).get("direction") == Direction.BUY]
    counts = {"reviewed": 0, "unavailable": 0}

    async def review(row, payload, *, deep):
        lane = "weekly_deep" if deep else "buy_second_opinion"
        key = (f"shadow:{lane}:{week_key(now)}:" if deep else f"shadow:{lane}:") + row["thesis_id"]
        if not store.claim_work(key, row["symbol"], now):
            return
        detail = {"kind": lane, "reference_thesis_id": row["thesis_id"]}
        try:
            source_time = datetime.fromisoformat(row["generated_at"])
            source = ResearchInput.from_payload(payload["inputs"])
            evidence = tuple(
                Evidence.model_validate_json(json.dumps(item)) for item in payload["evidence"]
            )
            # Preserve the original evidence IDs; exclude anything not in the
            # point-in-time snapshot for a blind second opinion.
            as_of = now if deep else source_time
            source = replace(
                source, as_of=as_of, evidence=tuple(
                    item for item in evidence if item.retrieved_at <= source_time
                    and (item.published_at is None or item.published_at <= source_time)
                ),
            )
            result = await analyst.shadow_review(
                source, ModelRoute(payload["provider"], payload["model"]), deep=deep,
            )
            detail.update(
                direction=result.record.draft.direction.value,
                conviction=result.record.raw_conviction,
                provider=result.record.provider, model=result.record.model,
                agrees=result.record.draft.direction == payload["draft"]["direction"],
                evidence_ids=sorted({key for claim in result.record.draft.claims for key in claim.evidence_ids}),
                metered_microusd=result.result.metered_microusd,
            )
            counts["reviewed"] += 1
            status = "shadow_reviewed"
        except ProviderUnavailable as error:
            detail["reason"] = error.attempts[-1].kind if error.attempts else "no_independent_route"
            detail.update(error.failure_codes)
            counts["unavailable"] += 1
            status = "shadow_unavailable"
        except (ValueError, KeyError, TypeError):
            detail["reason"] = "shadow_input_invalid"
            counts["unavailable"] += 1
            status = "shadow_unavailable"
        store.finish_work(key, status, detail=detail)
        logger.info("Research shadow lane=%s thesis=%s status=%s", lane, row["thesis_id"], status)

    # Freeze the weekly selection on first execution (including an empty set).
    # An interrupted audit is not repeated, and has no effect on trading.
    if weekly and store.claim_work(f"shadow:weekly_batch:{week_key(now)}", "_weekly", now):
        candidates = [(row, payload) for row, payload in ideas if
                      payload.get("draft", {}).get("direction") in {Direction.BUY, Direction.BEARISH}
                      and type(payload.get("effective_conviction")) is int
                      and payload["effective_conviction"] >= 7]
        candidates.sort(key=lambda pair: pair[1]["effective_conviction"], reverse=True)
        selected = {}
        for row, payload in candidates:
            selected.setdefault(row["symbol"], (row, payload))
            if len(selected) == 3:
                break
        for row, payload in selected.values():
            await review(row, payload, deep=True)
        store.finish_work(f"shadow:weekly_batch:{week_key(now)}", "shadow_reviewed",
                          detail={"selected": [row["thesis_id"] for row, _ in selected.values()]})
    if buys:
        for row, payload in buy_ideas:
            await review(row, payload, deep=False)
    return counts
