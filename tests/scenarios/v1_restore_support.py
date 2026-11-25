"""Shared world harness for the V1-intent restore scenarios (V1_RESTORE_BRIEF quality bar).

It is generalised from tests/scenarios/test_scheduled_buy_handoff.py (the red repro). The real scheduler,
research pipeline, ledger, execution engine and fake broker run on simulated time. The LLM's reply is
scripted per scenario (direction, conviction, thesis_change). Prices are synthetic round numbers only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

from stock_agent.config import Config
from stock_agent.ledger import JobStatus
from tests.fakes.providers import LLMReply

SYMBOL = "AAPL"


def with_grounding(body, draft):
    """The canned provider returns tool results only when the request asks for them."""
    if body.get("tools"):
        return LLMReply(text=json.dumps(draft), sources=[
            {"url": "https://example.com/fixture/issuer-filing", "title": "Synthetic issuer filing"},
        ])
    return draft


@dataclass
class Reply:
    direction: str = "BUY"
    conviction: int = 9
    thesis_change: str = "new"


@dataclass
class Harness:
    world: object
    monkeypatch: object
    symbol: str = SYMBOL
    others: tuple = ()  # extra focus symbols (same scripted reply, same synthetic price)
    reply: Reply = field(default_factory=Reply)
    calls: list = field(default_factory=list)  # (et_time, direction, reference_price) per LLM draft
    scheduler: object = None

    def install(self, *, price: float = 100.0) -> Harness:
        w = self.world
        self.monkeypatch.setattr(Config, "PRIORITY_SYMBOLS", (self.symbol, *self.others))
        self.monkeypatch.setattr(Config, "UNIVERSE_SIZE", 1)
        # Out-of-hours quotes are stale at every provider except the broker.
        w.finnhub.fail("/api/v1/quote", "status", status=503, times=None)
        w.fmp.fail("/stable/quote", "status", status=503, times=None)
        w.perplexity.fail("/v1/agent", "status", status=401, times=None)
        for symbol in (self.symbol, *self.others):
            w.price(symbol, price, spread=0.02)
        w.openai.respond_with(self._draft)
        return self

    def _draft(self, body):
        text = body.get("input") or body["messages"][0]["content"]
        data = json.loads(text.split("INPUT_JSON:\n", 1)[1])
        self.calls.append((self.world.clock.now_et(), self.reply.direction, data.get("reference_price")))
        evidence = next((item for item in data["evidence"] if item["published_at"]), None)
        best_ideas = "No discrete catalyst is required" in body.get("instructions", "")
        if evidence is None:  # nothing dated to cite
            draft = {
                "symbol": data.get("symbol") or self.symbol,
                "direction": "INSUFFICIENT_EVIDENCE" if best_ideas else "HOLD",
                "conviction": 1, "summary": "No new evidence.",
                "bull_case": "None.", "bear_case": "None.", "claims": [], "catalysts": [], "risks": [],
                "thesis_break": "Not applicable.", "target_price": None, "thesis_change": "unchanged",
                "horizon_days": 15,
            }
        else:
            direction = "AVOID" if best_ideas and self.reply.direction == "HOLD" else self.reply.direction
            draft = {
                "symbol": data.get("symbol") or self.symbol, "direction": direction, "conviction": self.reply.conviction,
                "summary": "The issuer updated its outlook.", "bull_case": "Improving demand.",
                "bear_case": "Demand may weaken.", "claims": [
                    {"text": "The issuer issued updated guidance.", "evidence_ids": [evidence["evidence_id"]]},
                ],
                "catalysts": [], "risks": [], "thesis_break": "Next release contradicts the update.",
                "target_price": None, "thesis_change": self.reply.thesis_change, "horizon_days": 15,
            }
        return with_grounding(body, draft)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, scheduler=None):
        self.scheduler = scheduler or self.world.scheduler()
        await self.scheduler.startup()
        await self.scheduler._refresh_assets()
        await self.scheduler._run_execution_cycle()
        return self.scheduler

    async def restart(self):
        """A process restart: a fresh scheduler on the same state DB (a crash would skip _cleanup)."""
        await self.scheduler._cleanup()
        return await self.start()

    async def stop(self):
        if self.scheduler is not None:
            await self.scheduler._cleanup()

    async def advance(self, day: int, hh: int, mm: int = 0):
        self.world.clock.set_et(2026, 11, day, hh, mm)
        await self.scheduler.calendar.refresh()
        assert await self.scheduler._reconcile_scheduler("v1 restore scenario")

    async def drain(self, name: str):
        s = self.scheduler
        if name in s._job_tasks:
            await s._job_tasks[name]
            await s._collect_finished_jobs()

    def story(self, minutes_ago: int = 5):
        now = self.world.clock.now()
        self.world.news([self.symbol, *self.others], f"Issuer updates outlook {now.isoformat()}",
                        summary="The issuer publishes new guidance.", at=now - timedelta(minutes=minutes_ago))

    def quotes_go_stale(self):
        """Broker quotes age normally in session (the fake keeps them fresh by default)."""
        self.world.alpaca.behaviour.quotes_stay_fresh_in_session = False

    def fresh_quote(self, price: float = 100.0):
        for symbol in (self.symbol, *self.others):
            self.world.price(symbol, price, spread=0.02)

    async def run_research(self, kind: str):
        """Run one scheduled research job (prescan | midweek | weekly) now and return its job row."""
        s = self.scheduler
        now = self.world.clock.now_et()
        if kind == "prescan":
            key, callback = now.date().isoformat(), s._run_daily_prescan
        else:
            key, callback = s._research_week_key(now, kind), getattr(s, f"_run_{kind}_analysis")
        s._schedule_job(kind, key, callback, timeout=120)
        await self.drain(kind)
        row = s.ledger.job_status(kind, key)
        assert row is not None and row.status is JobStatus.DONE, row
        return row

    async def catchup(self):
        self.scheduler._schedule_research_catchup(self.world.clock.now_et())
        await self.drain("catchup")

    async def execute(self, cycles: int = 1):
        """Run real scheduler ticks (the 30-second lane plus whatever background jobs are due, such as the
        handoff promotion), 30 simulated seconds apart, and wait for the jobs they started."""
        s = self.scheduler
        for _ in range(cycles):
            await s._tick(self.world.clock.now_et())
            for task in tuple(s._job_tasks.values()):
                await task
            await s._collect_finished_jobs()
            if s._order_task is not None and not s._order_task.done():
                await s._order_task  # the order lane runs as its own task; let this pass finish
            self.world.clock.advance(seconds=30)

    # ------------------------------------------------------------------ observations
    def buys(self):
        return [r for r in self.world.orders_sent() if r.method == "POST" and r.json.get("side") == "buy"
                and r.json.get("symbol") == self.symbol]

    def intents(self):
        return self.scheduler.ledger.list_intents(symbol=self.symbol)

    def intent_states(self):
        return [(i.status.value, i.origin, i.dedupe_key, i.last_error) for i in self.intents()]


async def monday_hold_catchup(h: Harness):
    """Mon 16 Nov 09:35: the catch-up writes a healthy HOLD set, so Tuesday's prescan is the only BUY source."""
    h.reply = Reply(direction="HOLD")
    await h.advance(16, 9, 35)
    h.fresh_quote()
    await h.scheduler._run_execution_cycle()  # the lane only: the catch-up must see the story below
    assert h.scheduler.engine.normalised
    h.story()
    await h.catchup()
    assert h.scheduler.research.has_fresh_thesis_set(h.world.clock.now())
    h.reply = Reply()


async def tuesday_prescan_with_stale_quote(h: Harness):
    """Tue 17 Nov 08:00: the prescan analyses a BUY 9, but the broker quote is hours old (the 23 Nov shape)."""
    await h.advance(17, 8, 0)
    # No new story: Monday's 09:30 story is the evidence. From Tue 09:35 it is over 24 h old, so the live
    # news lane cannot buy it on its own and every buy seen later comes from the handoff.
    row = await h.run_research("prescan")
    assert row.detail["entry_funnel"]["passed_conviction"] == 1
    h.world.assert_no_orders()
    return row
