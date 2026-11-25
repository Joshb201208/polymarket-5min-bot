"""World harness for the owner-directive FIX FIRST scenarios (plan_coverage_reviewer on 16483e0).

The real scheduler, research pipeline, research gate, ledger, execution engine and options lane run on
simulated time against the fake broker and providers. Only the LLM replies are scripted: the owner review
("Act as a risk reviewer") and ordinary reviews each follow a per-scenario policy. All prices are synthetic
round numbers. The owner focus file is written per test (the autouse fixture otherwise points it at an
empty file).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_agent import research_focus
from stock_agent.config import Config
from stock_agent.news_service import DEFAULT_CIK_MAP
from stock_agent.research_schemas import OwnerThesisDraft
from tests.fakes import ET, occ_symbol
from tests.fakes.providers import LLMReply
from tests.scenarios.v1_restore_support import Harness

SYMBOL = "NVDA"
SPOT = 100.0
ADDED = date(2026, 11, 24)        # the day before the scenario session, so 25 Nov evidence can veto
EXPIRES = date(2027, 1, 25)
OPTION_EXPIRY = date(2026, 12, 24)
STRIKES = (90, 95, 100, 105, 110, 115)
OWNER_NOTE = ("Bullish: Rubin shipments and AI demand support growth. Veto on dated issuer evidence of a "
              "Q4 guidance cut, a material Rubin delay or the CEO leaving.")
# research_pipeline_engineer is adding a required veto_claim_index; send it only once the schema has it.
_HAS_VETO_INDEX = "veto_claim_index" in OwnerThesisDraft.model_fields
ALPACA_NAMES = {
    "NVDA": "NVIDIA Corporation Common Stock", "AAPL": "Apple Inc. Common Stock",
    "MSFT": "Microsoft Corporation Common Stock", "AMD": "Advanced Micro Devices, Inc. Common Stock",
}
OWNER_PROMPT = "Act as a risk reviewer"
BREAK = "The issuer cut Q4 revenue guidance."  # a veto's break claim repeats thesis_break verbatim


def et(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


def write_focus(path: Path, *items: dict) -> Path:
    path.write_text(json.dumps(list(items)))
    return path


def owner_item(symbol: str = SYMBOL, *, added: date = ADDED, note: str = OWNER_NOTE, **extra) -> dict:
    return {"symbol": symbol, "note": note, "stance": "bullish", "owner_conviction": 8,
            "added": added.isoformat(), "expires": EXPIRES.isoformat(), **extra}


@dataclass
class OwnerHarness(Harness):
    """`owner` and `ordinary` are the scripted directions for owner and ordinary reviews. A callable may
    replace either: it receives (body, data) and returns a direction string."""

    symbol: str = SYMBOL
    owner: object = "BUY"
    ordinary: object = "HOLD"
    focus_path: Path | None = None
    _filings: dict = field(default_factory=dict)
    veto_cites: str = "latest"     # "latest" | "all" | "oldest": which dated evidence an owner veto cites
    ordinary_conviction: int = 8
    prompts: list = field(default_factory=list)  # (et, kind, direction, tools?, evidence ids)

    def install(self, *, price: float = SPOT, focus: tuple[dict, ...] | None = None, tmp_path: Path) -> OwnerHarness:
        # Alpaca asset names carry the share class, e.g. "Apple Inc. Common Stock"
        # (https://docs.alpaca.markets/us/openapi/trading-api.json, Assets schema example).
        for symbol in (self.symbol, *self.others):
            self.world.alpaca.add_asset(symbol, name=ALPACA_NAMES.get(symbol, f"{symbol} Inc. Common Stock"))
        super().install(price=price)
        # The owner review is a single review with no fallback; route it to the scripted provider.
        self.monkeypatch.setattr(Config, "RESEARCH_PROVIDER_ORDER", ("openai",))
        self.monkeypatch.setattr(Config, "SEC_USER_AGENT", "fixture-agent ops@example.com")
        self.focus_path = write_focus(tmp_path / "owner_focus.json", *(focus or (owner_item(self.symbol),)))
        self.monkeypatch.setitem(research_focus.load_focus.__kwdefaults__, "path", self.focus_path)
        self.world.openai.respond_with(self._owner_draft)
        a = self.world.alpaca
        for k in STRIKES:
            for kind in ("call", "put"):
                a.add_option_contract(self.symbol, OPTION_EXPIRY, kind, k, open_interest=1000)
        self.requote_chain(price)
        return self

    def requote_chain(self, spot: float):
        for k in STRIKES:
            for kind in ("call", "put"):
                intrinsic = max(spot - k, 0) if kind == "call" else max(k - spot, 0)
                mid = round(intrinsic + 3.0 * max(0.2, 1 - abs(spot - k) / 25), 2)
                self.world.alpaca.set_option_quote(occ_symbol(self.symbol, OPTION_EXPIRY, kind, k),
                                                   round(mid - 0.05, 2), round(mid + 0.05, 2))

    def edit_focus(self, *items: dict):
        write_focus(self.focus_path, *items)

    # ------------------------------------------------------------------ scripted model
    def _owner_draft(self, body):
        text = body.get("input") or body["messages"][0]["content"]
        data = json.loads(text.split("INPUT_JSON:\n", 1)[1])
        owner = OWNER_PROMPT in body.get("instructions", "")
        policy = self.owner if owner else self.ordinary
        direction = policy(body, data) if callable(policy) else policy
        dated = sorted((e for e in data.get("evidence", []) if e.get("published_at")),
                       key=lambda e: e["published_at"])
        self.prompts.append((self.world.clock.now_et(), "owner" if owner else "ordinary", direction,
                             bool(body.get("tools")), [e["evidence_id"] for e in dated]))
        symbol = data.get("symbol") or self.symbol
        if not dated:
            direction = "INSUFFICIENT_EVIDENCE" if not owner else "AVOID"
        cite = (dated[-1:] if self.veto_cites == "latest" or direction == "BUY"
                else dated[:1] if self.veto_cites == "oldest" else dated)
        draft = {
            "symbol": symbol, "direction": direction, "conviction": 3 if owner else self.ordinary_conviction,
            "summary": "Scripted review.", "bull_case": "Demand holds.", "bear_case": "Guidance may slip.",
            "claims": [{"text": BREAK, "evidence_ids": [e["evidence_id"] for e in cite]}]
            if cite else [],
            "catalysts": [], "risks": [], "thesis_break": BREAK, "target_price": None,
            "thesis_change": {"EXIT_PROPOSED": "broken", "HOLD": "unchanged"}.get(direction, "new"),
            "horizon_days": 15,
        }
        if owner:
            draft["stop_price"] = round(0.9 * data["reference_price"], 2) if direction == "BUY" and data.get("reference_price") else None
            if _HAS_VETO_INDEX:
                draft["veto_claim_index"] = 0 if direction in ("BEARISH", "AVOID") and cite else None
        if body.get("tools"):
            return LLMReply(text=json.dumps(draft), sources=[
                {"url": "https://example.com/fixture/issuer-filing", "title": "Synthetic issuer filing"},
            ])
        return draft

    # ------------------------------------------------------------------ helpers
    async def run_job(self, kind: str):
        """Like run_research, but returns the job row whatever its status (an owner abstention may retry)."""
        s = self.scheduler
        now = self.world.clock.now_et()
        key, callback = ((now.date().isoformat(), s._run_daily_prescan) if kind == "prescan" else
                         (s._research_week_key(now, kind), getattr(s, f"_run_{kind}_analysis")))
        s._schedule_job(kind, key, callback, timeout=120)
        await self.drain(kind)
        return s.ledger.job_status(kind, key)

    def filing(self, form: str, *, accepted: datetime, items: str = "", symbol: str | None = None, seq: int = 1):
        """Add one EDGAR submissions row for the issuer (synthetic accession number)."""
        cik = DEFAULT_CIK_MAP[symbol or self.symbol]
        rows = self._filings.setdefault(cik, [])
        filed = accepted.astimezone(UTC).date().isoformat()
        rows.insert(0, (form, filed, f"{int(cik):010d}-26-{seq:06d}", items,
                        accepted.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")))
        recent = {key: [row[i] for row in rows] for i, key in enumerate(
            ("form", "filingDate", "accessionNumber", "items", "acceptanceDateTime"))}
        self.world.sec.on("GET", f"/submissions/CIK{cik}.json", {"filings": {"recent": recent}})

    def intercept_broker(self, hook):
        """hook(request) -> httpx.Response | None (None = let the fake broker answer); it may raise."""
        handle = self.world.alpaca.handle

        def wrapped(request):
            return hook(request) or handle(request)

        self.monkeypatch.setattr(self.world.alpaca, "handle", wrapped)

    def headline(self, text: str, *, minutes_ago: int = 1, symbols: list[str] | None = None, summary: str = ""):
        now = self.world.clock.now()
        self.world.news(symbols or [self.symbol], text, summary=summary or text,
                        at=now - timedelta(minutes=minutes_ago))

    def owner_prompts(self):
        return [p for p in self.prompts if p[1] == "owner"]

    def option_orders(self):
        return [r for r in self.world.orders_sent() if r.method == "POST"
                and (r.json.get("order_class") == "mleg" or len(str(r.json.get("symbol", ""))) > 10)]

    def journal(self, *kinds: str):
        return list(self.scheduler.ledger.journal.read(set(kinds)))

    def owner_events(self):
        return [r for r in self.journal("research.owner_directive")]
