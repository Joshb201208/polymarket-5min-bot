"""A scripted end-to-end week for the lead: Mon 2026-11-23 .. Fri 2026-11-27.

The week contains Thanksgiving (Thu, closed) and the day after (Fri, 13:00
close). `E2EWeek.schedule(runner)` only changes the OUTSIDE world (broker
holdings, prices, news, option quotes, faults, restarts); it asserts nothing
about the bot. The lead writes assertions against `week.times` and the fakes,
because what "correct" means comes from DECISIONS.md, not from the harness.

Script (ET):
  Mon 07:00  first run. Broker already holds AAA 50 @ 100 that the ledger does
             not know, plus a stale GTC buy limit for ZZZ (first-run normalise).
  Mon 09:45  positive news for BBB (news-driven buy candidate).
  Tue 11:00  BBB falls 12 % through any reasonable stop (stop fill).
  Tue 13:00  negative news for AAA; Tue 13:20 a second, confirming story
             from another source (news-driven exit with confirmation).
  Mon..Wed   CCC option chain, expiry Mon 2026-11-30. Two sessions before
             expiry (T-2) is Wed 11-25, because Thu is a holiday and Fri is
             T-1: a spread opened Mon/Tue must be closed by Wed's close.
  Thu        holiday: the broker accepts orders but nothing fills.
  Fri 09:50  positive news for DDD; the next order's reply is lost and the
             agent restarts at the end of that step (restart mid-order).

LLM replies are NOT scripted here: set them with `world.openai.reply(...)`,
`world.perplexity.reply(...)`, `world.anthropic.reply(...)` in the shape the
research pipeline expects, or pass `on_news=` to react to each story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from tests.fakes.clock import ET
from tests.fakes.alpaca import occ_symbol


def _et(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


@dataclass
class E2EWeek:
    monday: date = date(2026, 11, 23)
    option_expiry: date = date(2026, 11, 30)
    on_news: Callable | None = None  # on_news(world, symbol, headline) after each story
    times: dict[str, datetime] = field(default_factory=lambda: {
        "first_run": _et(2026, 11, 23, 7, 0),
        "bbb_news": _et(2026, 11, 23, 9, 45),
        "bbb_drop": _et(2026, 11, 24, 11, 0),
        "aaa_bad_news": _et(2026, 11, 24, 13, 0),
        "aaa_confirm": _et(2026, 11, 24, 13, 20),
        "ccc_t_minus_2_close": _et(2026, 11, 25, 16, 0),
        "holiday": _et(2026, 11, 26, 9, 30),
        "ddd_news": _et(2026, 11, 27, 9, 50),
        "early_close": _et(2026, 11, 27, 13, 0),
    })
    prices: dict[str, float] = field(default_factory=lambda: {
        "AAA": 100.0, "BBB": 50.0, "CCC": 200.0, "DDD": 30.0, "ZZZ": 20.0,
    })

    def ccc_contracts(self) -> dict[str, str]:
        return {k: occ_symbol("CCC", self.option_expiry, "call", k_) for k, k_ in (("long", 200), ("short", 210))}

    def setup(self, world) -> None:
        """Broker and market state at Monday 07:00 (call before running)."""
        a = world.alpaca
        for sym, px in self.prices.items():
            world.price(sym, px, spread=0.02)
        a.seed_position("AAA", 50, 100.0)
        a.submit({"symbol": "ZZZ", "qty": "5", "side": "buy", "type": "limit", "limit_price": "15",
                  "time_in_force": "gtc"})
        a.add_option_chain("CCC", self.option_expiry, [190, 200, 210, 220])
        c = self.ccc_contracts()
        a.set_option_quote(c["long"], 6.0, 6.2)
        a.set_option_quote(c["short"], 2.4, 2.6)
        a.seed_daily_bars("AAA", [100.0] * 30, end=date(2026, 11, 20))
        a.seed_daily_bars("BBB", [50.0] * 30, end=date(2026, 11, 20))

    def _story(self, symbol: str, headline: str, source: str = "benzinga"):
        def ev(world, runner):
            world.news([symbol], headline)
            if self.on_news:
                self.on_news(world, symbol, headline)
        ev.__name__ = f"news {symbol}: {headline[:30]} ({source})"
        return ev

    def schedule(self, runner) -> "E2EWeek":
        t = self.times
        runner.at(t["bbb_news"], self._story("BBB", "BBB raises full-year guidance on record orders"))
        runner.at(t["bbb_drop"], lambda w, r: w.price("BBB", self.prices["BBB"] * 0.88, spread=0.02), "BBB -12%")
        runner.at(t["aaa_bad_news"], self._story("AAA", "AAA discloses regulator probe into accounting"))
        runner.at(t["aaa_confirm"], self._story("AAA", "Second report confirms AAA accounting probe", "reuters"))
        runner.at(t["ccc_t_minus_2_close"], lambda w, r: None, "CCC T-2 close (checkpoint)")
        runner.at(t["ddd_news"], self._story("DDD", "DDD wins multi-year government contract"))
        runner.at(t["ddd_news"], lambda w, r: r.restart_after_lost_order(), "arm lost order reply + restart")
        return self

    def run(self, runner) -> None:
        self.setup(runner.world)
        self.schedule(runner)
        runner.run_week(self.monday)
