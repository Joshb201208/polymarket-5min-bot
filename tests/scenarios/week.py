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
  Mon 07:00  the broker also holds a bull call spread on EEE (+1 C100 / -1 C105)
             expiring Fri 2026-11-27 that the bot has never seen (adopted on
             first run). Trading days left: Tue, Wed, Fri, so T-2 is Tue 11-24:
             it must be closed as one mleg after Tue open + 5 minutes.
  Mon 10:30  bearish news for CCC; CCC has a liquid chain expiring Thu
             2026-12-24 (31 DTE, inside the 21-45 DTE entry window), so a
             conviction-8 bearish thesis can open a defined-risk put spread.
             Its own T-2 (Mon 12-21) is after this week; the test steps one
             tick on that date after the week to check the close.
  Thu        holiday: the broker accepts orders but nothing fills.
  Fri 09:50  positive news for DDD; the next order's reply is lost and the
             agent restarts at the end of that step (restart mid-order); the
             order lookup is down until then, so recovery is the new process's job.

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
from tests.options_support import seed_chain


def _et(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


@dataclass
class E2EWeek:
    monday: date = date(2026, 11, 23)
    option_expiry: date = date(2026, 12, 24)
    adopted_expiry: date = date(2026, 11, 27)
    on_news: Callable | None = None  # on_news(world, symbol, headline) after each story
    times: dict[str, datetime] = field(default_factory=lambda: {
        "first_run": _et(2026, 11, 23, 7, 0),
        "bbb_news": _et(2026, 11, 23, 9, 45),
        "bbb_drop": _et(2026, 11, 24, 11, 0),
        "aaa_bad_news": _et(2026, 11, 24, 13, 0),
        "aaa_confirm": _et(2026, 11, 24, 13, 20),
        "ccc_news": _et(2026, 11, 23, 10, 30),
        "eee_t_minus_2": _et(2026, 11, 24, 9, 35),
        "ccc_t_minus_2": _et(2026, 12, 22, 9, 35),
        "holiday": _et(2026, 11, 26, 9, 30),
        "ddd_news": _et(2026, 11, 27, 9, 50),
        "early_close": _et(2026, 11, 27, 13, 0),
    })
    prices: dict[str, float] = field(default_factory=lambda: {
        "AAA": 100.0, "BBB": 50.0, "CCC": 100.0, "EEE": 100.0, "DDD": 30.0, "ZZZ": 20.0,
    })

    def ccc_contracts(self) -> dict[str, str]:
        """Every CCC contract the bot could pick (puts and calls, 90..115)."""
        return {f"{t}{k}": occ_symbol("CCC", self.option_expiry, t, k)
                for t in ("call", "put") for k in (90, 95, 100, 105, 110, 115)}

    def eee_legs(self) -> dict[str, str]:
        return {"long": occ_symbol("EEE", self.adopted_expiry, "call", 100),
                "short": occ_symbol("EEE", self.adopted_expiry, "call", 105)}

    def setup(self, world) -> None:
        """Broker and market state at Monday 07:00 (call before running)."""
        a = world.alpaca
        for sym, px in self.prices.items():
            world.price(sym, px, spread=0.02)
        a.seed_position("AAA", 50, 100.0)
        a.submit({"symbol": "ZZZ", "qty": "5", "side": "buy", "type": "limit", "limit_price": "15",
                  "time_in_force": "gtc"})
        seed_chain(world, "CCC", self.prices["CCC"], self.option_expiry)
        seed_chain(world, "EEE", self.prices["EEE"], self.adopted_expiry)
        legs = self.eee_legs()
        a.seed_position(legs["long"], 1, 3.0)
        a.seed_position(legs["short"], -1, 2.4)  # debit 0.60 = current mid value: no P&L exit
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
        runner.at(t["ccc_news"], self._story("CCC", "CCC warns of demand slowdown in core market"))
        runner.at(t["ddd_news"], self._story("DDD", "DDD wins multi-year government contract"))
        runner.at(t["ddd_news"], lambda w, r: r.restart_after_lost_order(lookup_down=True),
                    "arm lost order reply (lookup down) + restart")
        return self

    def run(self, runner) -> None:
        self.setup(runner.world)
        self.schedule(runner)
        runner.run_week(self.monday)
