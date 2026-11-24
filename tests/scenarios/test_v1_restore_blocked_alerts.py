"""V1 restore part 5 (blocked-buy #risk messages), end to end on the world harness.

Twelve stocks are queued as 08:00 prescan buys (conviction 7). At 09:34 the market regime turns cautious, so the
conviction floor rises to 8 and every buy is refused at the pre-buy recheck. #risk must then carry one plain-English
line per symbol and reason, at most 10 in the session, and after the close one "and 2 more" line, which is not
repeated by later ticks, a restart or the next session's rollover.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import pytest

from stock_agent.config import Config
from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, Reply

pytestmark = pytest.mark.world_start(datetime(2026, 11, 15, 17, tzinfo=ET))

OTHERS = ("MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V", "WMT", "XOM", "KO")


async def risk_lines(h: Harness) -> list[str]:
    await h.scheduler.alerts.flush()
    return [text for text in h.world.discord.texts() if "Buy blocked" in text or "Blocked buys" in text]


def test_twelve_blocked_buys_give_ten_lines_and_one_overflow_line(world, monkeypatch):
    h = Harness(world, monkeypatch, others=OTHERS).install()
    monkeypatch.setattr(Config, "UNIVERSE_SIZE", 1 + len(OTHERS))

    async def go():
        try:
            await h.start()
            # All twelve are focus names, so the prescan reviews every one (it ranks at most 8 others).
            monkeypatch.setattr(h.scheduler.research, "focus", lambda **_: (h.symbol, *OTHERS))
            await h.advance(16, 9, 35)
            h.fresh_quote()
            await h.scheduler._run_execution_cycle()  # Monday's first-run clean-up, as in monday_hold_catchup
            assert h.scheduler.engine.normalised
            h.story()
            h.reply = Reply(direction="HOLD")
            await h.catchup()
            await h.advance(17, 8, 0)
            h.reply = Reply(conviction=7)
            h.fresh_quote()
            await h.run_research("prescan")
            queued = [i for i in h.scheduler.ledger.list_intents() if i.status.value == "queued" and i.side == "buy"]
            assert len(queued) == 12, [(i.symbol, i.status.value) for i in h.scheduler.ledger.list_intents()]
            await h.advance(17, 9, 34)
            h.scheduler.store.kv_set("research_market_regime",
                                     {"regime": "cautious", "as_of": h.world.clock.now().isoformat()})
            await h.advance(17, 9, 35)
            h.fresh_quote()
            await h.execute(6)
            assert not [r for r in h.world.orders_sent() if r.method == "POST" and r.json.get("side") == "buy"]
            lines = await risk_lines(h)
            blocked = [text for text in lines if "Buy blocked" in text]
            assert len(blocked) == 10, lines
            assert len({re.search(r"Buy blocked — (\S+)", text).group(1) for text in blocked}) == 10
            assert not [text for text in lines if "Blocked buys" in text], "overflow waits for the close"
            for text in blocked:
                assert "{" not in text and "_" not in text, text  # plain English, no codes or JSON

            await h.restart()
            await h.advance(17, 16, 5)
            await h.execute(3)
            lines = await risk_lines(h)
            overflow = [text for text in lines if "Blocked buys" in text]
            assert len(overflow) == 1 and "and 2 more" in overflow[0], lines
            assert len([text for text in lines if "Buy blocked" in text]) == 10

            await h.restart()
            await h.advance(18, 9, 35)
            h.fresh_quote()
            await h.execute(3)
            lines = await risk_lines(h)
            assert len([text for text in lines if "Blocked buys" in text]) == 1, lines
            assert len([text for text in lines if "Buy blocked" in text]) == 10, lines
        finally:
            await h.stop()

    asyncio.run(go())
