"""A stand-in agent built from the NEW components, for the end-to-end week.

The new scheduler is not wired yet, so this module composes what exists on
revamp/build the way DECISIONS.md describes (state store + ledger, market
calendar, ExecutionEngine, research pipeline with the OpenAI route) and drives
it once per simulated minute. When scheduler_deploy's loop lands, replace
`WeekAgent` with a `build=` that returns the real scheduler and keep the
assertions in test_e2e_week.py.

Only test adapters live here: `HttpPrices` and `HttpSources` read the fake
Alpaca data API over HTTP (stand-ins until news_data's services merge), and
`research_settings()` is the OpenAI-only research configuration from
research_pipeline_engineer (no Perplexity/Anthropic key).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.fakes.clock import ET

RESEARCH_SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
SECTORS = {"AAA": "Technology", "BBB": "Industrials", "CCC": "Technology", "DDD": "Industrials",
           "EEE": "Health Care"}
OPTION_UNIVERSE = ("CCC",)


def research_settings(**overrides):
    values = {
        "RESEARCH_PROVIDER_ORDER": ("perplexity", "openai", "anthropic"),
        "PERPLEXITY_API_KEY": "", "OPENAI_API_KEY": "placeholder-not-a-key", "ANTHROPIC_API_KEY": "",
        "RESEARCH_PERPLEXITY_MODEL": "anthropic/claude-sonnet-5-5",
        "RESEARCH_OPENAI_MODEL": "gpt-6-luna", "RESEARCH_OPENAI_CONFIRM_MODEL": "gpt-5.6-luna",
        "RESEARCH_THESIS_MAX_AGE_H": 96, "RESEARCH_MAX_ADJUSTMENT": 1,
        "RESEARCH_WEEKLY_BUDGET_USD": 15, "RESEARCH_EXIT_RESERVE_PCT": 20,
        "RESEARCH_MAX_MODEL_EXITS_PER_SESSION": 4,
    }
    return SimpleNamespace(**(values | overrides))


@dataclass
class _Q:
    price: float
    bid: float | None
    ask: float | None
    as_of: datetime
    source: str = "alpaca-fake"


@dataclass
class _Batch:
    quotes: dict
    missing: dict


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class HttpPrices:
    """`prices.get_quotes(...)` for ExecutionEngine, via the data API snapshots."""

    def __init__(self, broker):
        self.broker = broker

    async def get_quotes(self, symbols, *, purpose, max_age_s=None):
        data = await self.broker._get_data("/v2/stocks/snapshots", {"symbols": ",".join(symbols)})
        out, missing = {}, {}
        for s in symbols:
            snap = (data or {}).get(s)
            if not snap or not snap.get("latestQuote"):
                missing[s] = "no quote"
                continue
            q, t = snap["latestQuote"], snap.get("latestTrade") or {}
            out[s] = _Q(float(t.get("p") or q["ap"]), q.get("bp"), q.get("ap"), _ts(q["t"]))
        return _Batch(out, missing)


class HttpSources:
    """research_pipeline `Sources` over the fake Alpaca news + snapshots."""

    def __init__(self, broker, symbols=RESEARCH_SYMBOLS):
        self.broker = broker
        self.symbols = tuple(symbols)

    async def universe(self, now):
        return self.symbols

    async def news(self, symbols, now):
        from stock_agent.research_pipeline import NewsEvent
        from stock_agent.research_schemas import Evidence

        data = await self.broker._get_data("/v1beta1/news", {
            "symbols": ",".join(symbols), "start": (now - timedelta(hours=24)).isoformat(),
            "end": now.isoformat(), "limit": 50})
        out = []
        for n in (data or {}).get("news", []):
            for sym in n["symbols"]:
                if sym not in symbols:
                    continue
                ev = Evidence(evidence_id=f"news-{n['id']}", url=f"https://example.org/news/{n['id']}",
                              title=n["headline"], published_at=_ts(n["created_at"]), retrieved_at=now,
                              publisher=n["source"], snippet=n["summary"] or n["headline"])
                out.append(NewsEvent(sym, f"alpaca-{n['id']}", ev, 8))
        return tuple(out)

    async def releases(self, symbols, now):
        return ()

    async def market(self, now):
        return ()

    async def securities(self, symbols, now):
        from stock_agent.research_pipeline import SecurityInputs

        if not symbols:
            return {}
        data = await self.broker._get_data("/v2/stocks/snapshots", {"symbols": ",".join(symbols)})
        out = {}
        for s in symbols:
            snap = (data or {}).get(s)
            if not snap:
                continue
            t = snap["latestTrade"]
            out[s] = SecurityInputs(s, f"{s} Inc.", SECTORS.get(s, "Technology"), Decimal(str(t["p"])),
                                    _ts(t["t"]), True)
        return out


@dataclass
class CloseCheck:
    session: str
    ledger: dict[str, Decimal]
    broker: dict[str, Decimal]
    ledger_cash: Decimal | None
    broker_cash: Decimal
    diffs: tuple = ()

    @property
    def bad_diffs(self) -> list:
        return [d for d in self.diffs if str(getattr(d, "severity", "")) in ("error", "critical")]

    @property
    def ok(self) -> bool:
        return self.ledger == self.broker and self.ledger_cash == self.broker_cash and not self.bad_diffs


@dataclass
class WeekAgent:
    world: Any
    data_dir: Path
    news_every_min: int = 5
    alerts: list[tuple[str, str, str]] = field(default_factory=list)
    closes: list[CloseCheck] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def __post_init__(self):
        from stock_agent.analyst import Analyst
        from stock_agent.broker import AlpacaClient
        from stock_agent.config import Config
        from stock_agent.execution import ExecutionEngine
        from stock_agent.journal import Journal
        from stock_agent.ledger import Ledger
        from stock_agent.llm_client import LLMClient
        from stock_agent.market_calendar import MarketCalendar
        from stock_agent.research_gate import ResearchGate
        from stock_agent.research_pipeline import ResearchPipeline
        from stock_agent.research_store import ResearchStore
        from stock_agent.risk_manager import RiskManager
        from stock_agent.state_store import StateStore

        now = self.world.clock.now
        self.journal = Journal(self.data_dir / "journal", clock=now)
        self.store = StateStore(self.data_dir / "state.db", self.journal, clock=now)
        self.rconfig = research_settings()
        self.research_store = ResearchStore(self.store, self.rconfig)
        self.store.open()
        self.broker = AlpacaClient("https://paper-api.alpaca.markets", "placeholder", "placeholder")
        self.ledger = Ledger(self.store, self.journal, broker=self.broker, clock=now, alert=self._alert_sync)
        self.calendar = MarketCalendar(self.broker, self.data_dir / "calendar.json", now=now)
        self.engine = ExecutionEngine(self.broker, self.ledger, RiskManager(Config), self.calendar,
                                      HttpPrices(self.broker), alert=self._alert, now=now)
        self.analyst = Analyst(LLMClient(self.rconfig, spend=self.research_store, now=now))
        gate = ResearchGate(self.ledger, SimpleNamespace(research_entry_blockers=lambda *_: ()), self.rconfig)
        self.gate = gate
        self.pipeline = ResearchPipeline(self.rconfig, self.ledger, self.calendar, self.analyst,
                                         self.research_store, HttpSources(self.broker), gate,
                                         clock=now, focus=lambda **_: ())
        self._wire_options(now)

    def _wire_options(self, now):
        """OptionsManager wired the way StockAgentScheduler.__init__ does (scheduler.py:140-150)."""
        import asyncio

        from stock_agent.options_book import KvOptionsStore
        from stock_agent.options_manager import OptionsManager
        from stock_agent.risk_manager import RiskControls

        self.prices = HttpPrices(self.broker)
        self.capital_lock = asyncio.Lock()
        self.engine._capital_lock = self.capital_lock
        self._options_scanned: set = set()

        async def snapshot():
            return await self.ledger.refresh("option entry recheck")

        async def spot(symbol):
            return (await self.prices.get_quotes([symbol], purpose="order_sizing")).quotes.get(symbol)

        def controls():
            return RiskControls(option_commitments=self.options.risk_commitments())

        self.options = OptionsManager(
            broker=self.broker, store=KvOptionsStore(self.store), gate=self.gate, risk=self.engine.risk,
            snapshot=snapshot, calendar=self.calendar, market_gate=self.calendar.market_gate, spot=spot,
            cover_short=getattr(self.engine, "cover_short", None), universe=lambda: OPTION_UNIVERSE,
            risk_controls=controls, journal=self.journal, alerts=SimpleNamespace(alert=self._alert),
            now=now, capital_lock=self.capital_lock, defer_load=True,
        )
        self.engine.options = self.options

    # alerts from every component land here (and in the fake Discord? not yet wired)
    def _alert_sync(self, severity, title, body, key=None):
        self.alerts.append((severity, title, body))

    async def _alert(self, severity, title, body, key=None):
        self._alert_sync(severity, title, body, key)

    async def startup(self):
        """Same order as StockAgentScheduler.startup (scheduler.py:312-350)."""
        self.options.load_state()
        self.ledger.mark_submitting_ambiguous()
        await self.calendar.refresh()
        await self.ledger.refresh("startup")
        await self.engine.resume()
        await self.options.reconcile(self.world.clock.now())

    async def options_tick(self, now_et, session):
        """scheduler._run_options_lane + once-per-session _run_options_scan (open+15 .. 15:00)."""
        from stock_agent.options_manager import OptionCandidate
        from stock_agent.research_pipeline import OptionIdea

        now = self.world.clock.now()
        await self.options.monitor(now)
        ideas = [OptionIdea.from_payload(row) for row in self.research_store.pending_option_ideas(now)]
        if ideas and (await self.calendar.market_gate("entry")).allowed:
            outs = await self.options.scan(now, candidates=[OptionCandidate(
                i.symbol, source="news", story_id=i.story_id, direction_hint=i.direction.value) for i in ideas])
            self.log.append(f"{now_et:%a %H:%M} options news scan {[(o.symbol, o.reason) for o in outs]}")
            handled = self.options.handled_story_ids(i.story_id for i in ideas)
            if handled:
                self.research_store.acknowledge_options(tuple(sorted(handled)))
        day = now_et.date()
        if (day not in self._options_scanned and session.opens_at + timedelta(minutes=15) <= now_et
                < min(now_et.replace(hour=15, minute=0), session.closes_at - timedelta(minutes=10))):
            self._options_scanned.add(day)
            outs = await self.options.scan(now)
            self.log.append(f"{now_et:%a %H:%M} options scan {[(o.symbol, o.reason) for o in outs]}")

    async def step(self, now_et: datetime):
        market = await self.calendar.refresh()
        session = self.calendar.session_on(now_et.date())
        in_session = session is not None and session.opens_at <= now_et < session.closes_at
        if in_session or self.engine._norm_state().get("state") is None:
            await self.engine.monitor_tick()
        if in_session and self.engine.normalised:
            await self.options_tick(now_et, session)
        if in_session and now_et.minute % self.news_every_min == 0:
            out = await self.pipeline.run("news", await self.ledger.refresh("news"))
            if out.intents:
                self.ledger.enqueue_intents(list(out.intents))
                self.log.append(f"{now_et:%a %H:%M} intents {[(i.symbol, i.side) for i in out.intents]}")
                await self.engine.execute_due()
        if session is not None and now_et == session.closes_at:
            await self.check_close(session)
        return market

    async def check_close(self, session):
        snap = await self.ledger.refresh("close")
        acct = await self.broker.get_account()
        broker = {p.symbol: Decimal(str(p.qty)) for p in await self.broker.get_positions()}
        ledger = {h.symbol: Decimal(str(h.qty)) for h in snap.holdings if h.qty != 0}
        ledger.update({leg.occ_symbol: Decimal(str(leg.qty)) for leg in snap.legs if leg.qty != 0})
        self.closes.append(CloseCheck(session.date.isoformat(), ledger, broker,
                                      Decimal(str(snap.cash)) if snap.cash is not None else None,
                                      Decimal(str(acct.cash)), tuple(snap.diffs)))

    async def shutdown(self):
        try:
            await self.analyst.close()
        finally:
            await self.broker.close()
            self.store.close()


def build(world):
    """Alerts and close checks survive restarts: they live on the world."""
    shared = world.__dict__.setdefault("e2e_shared", {"alerts": [], "closes": [], "log": []})
    return WeekAgent(world, world.data_dir, alerts=shared["alerts"], closes=shared["closes"], log=shared["log"])


async def startup(agent):
    await agent.startup()


async def step(agent, now_et):
    await agent.step(now_et)


async def shutdown(agent):
    await agent.shutdown()


def runner_hooks() -> dict:
    return {"build": build, "startup": startup, "step": step, "shutdown": shutdown}


def et(*a) -> datetime:
    return datetime(*a, tzinfo=ET)
