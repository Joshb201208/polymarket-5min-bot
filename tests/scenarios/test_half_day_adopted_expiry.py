"""Friday 27 Nov 2026 half day (13:00 close) with the two inherited deep out-of-the-money structures,
through the real scheduler ticks, options lane, alert service and fake Alpaca.

* GS 920/940 call spread (long 1 @ 11.00, short 1 @ 0.00), spot 780; adopted from broker positions.
* JPM lone long 345 call (1 @ 4.96), spot 312.
Both expire 27 Nov. No option quote exists all day: Alpaca option snapshots were empty for these
contracts, and Alpaca rejects a market order for a contract without a quote with HTTP 403 "order has been
rejected due to no available quote for symbol. please reenter with a limit order" (observed live on the
paper account, reported by options_engineer; not taken from Alpaca's docs).
Whether Alpaca accepts an mleg limit of 0.00 is unverified, so both answers are replayed.

Expected (options_engineer 55f798d): limit orders only, never a debit; at most six tries per structure per
session with growing back-off; nothing sent after the 13:00 close; one "Options left to expire" info alert
per structure and no critical alert; after the broker expires the contracts, silence.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import httpx
import pytest

from stock_agent.config import Config
from tests.fakes import ET, occ_symbol
from tests.fakes.base import request_json
from tests.scenarios.v1_restore_support import Harness

FRI = date(2026, 11, 27)
GS_LONG, GS_SHORT = occ_symbol("GS", FRI, "call", 920), occ_symbol("GS", FRI, "call", 940)
JPM_CALL = occ_symbol("JPM", FRI, "call", 345)
CONTRACTS = (GS_LONG, GS_SHORT, JPM_CALL)
NO_QUOTE = ("order has been rejected due to no available quote for symbol. please reenter with a limit order")


def et(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


def _reject(status: int, message: str, code: int = 40310000) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message})


def drive(h, scenario):
    async def wrapper():
        try:
            await scenario()
        finally:
            await h.stop()

    asyncio.run(wrapper())


@pytest.mark.parametrize("zero_mleg", ["accepted", "rejected", "both_rejected"])
@pytest.mark.world_start(et(2026, 11, 25, 15, 0))
def test_friday_half_day_adopted_deep_otm_structures_close_bounded_and_quietly(world, monkeypatch, zero_mleg):
    h = Harness(world, monkeypatch, symbol="GS", others=("JPM",)).install()
    monkeypatch.setattr(Config, "UNIVERSE_SIZE", 2)
    a = world.alpaca
    a.behaviour.quotes_stay_fresh_in_session = True   # stock quotes stream in session
    a.behaviour.expiry_day_liquidation_time = None    # no broker liquidation sweep before the close
    for und, k in (("GS", 920), ("GS", 940), ("JPM", 345)):
        a.add_option_contract(und, FRI, "call", k, open_interest=100)
    a.seed_position(GS_LONG, 1, 11.0)
    a.seed_position(GS_SHORT, -1, 0.0)
    a.seed_position(JPM_CALL, 1, 4.96)
    for sym in CONTRACTS:
        a.quotes.pop(sym, None)  # empty option snapshots all day

    sent: list[tuple[datetime, dict, int]] = []
    handle = a.handle

    def broker(request):
        if request.method == "POST" and request.url.path == "/v2/orders":
            body = request_json(request) or {}
            legs = {lg.get("symbol") for lg in body.get("legs") or []}
            if body.get("symbol") in CONTRACTS or legs & set(CONTRACTS):
                if body.get("type") == "market":
                    sent.append((world.clock.now_et(), body, 403))
                    return _reject(403, NO_QUOTE)
                price = float(body.get("limit_price", "nan"))
                if body.get("order_class") == "mleg" and (
                        (zero_mleg != "accepted" and price == 0.0) or (zero_mleg == "both_rejected" and price < 0)):
                    sent.append((world.clock.now_et(), body, 422))
                    return _reject(422, "invalid limit_price", code=40010001)
                response = handle(request)
                sent.append((world.clock.now_et(), body, response.status_code))
                return response
        return handle(request)

    monkeypatch.setattr(a, "handle", broker)

    async def scenario():
        world.clock.set_et(2026, 11, 27, 9, 0)
        world.price("GS", 780.0, spread=0.05)
        world.price("JPM", 312.0, spread=0.02)
        await h.start()
        structures = [x for x in h.scheduler.options.structures if x.underlying in ("GS", "JPM")]
        assert sorted(x.underlying for x in structures) == ["GS", "JPM"], structures
        await h.advance(27, 9, 30)
        await h.execute(2 * 60 * 4)  # 09:30 -> 13:30, a real scheduler tick every 30 s
        await h.scheduler.alerts.flush()

        assert sent, "the bounded close tries are made"
        assert all(b.get("type") == "limit" for _, b, _ in sent), [b for _, b, _ in sent]
        assert all(t.hour < 13 for t, _, _ in sent), [t for t, _, _ in sent]  # nothing after the 13:00 close
        gs = [(t, b, s) for t, b, s in sent if b.get("order_class") == "mleg"]
        jpm = [(t, b, s) for t, b, s in sent if b.get("symbol") == JPM_CALL]
        assert len(gs) + len(jpm) == len(sent)
        for _, b, _ in gs:
            assert float(b["limit_price"]) <= 0, b  # a credit or zero, never a debit
            assert {lg["position_intent"] for lg in b["legs"]} == {"sell_to_close", "buy_to_close"}, b
        for _, b, _ in jpm:
            assert b["side"] == "sell" and b["position_intent"] == "sell_to_close" and b["limit_price"] == "0.01", b
        assert len(jpm) == 6, [t for t, _, _ in jpm]  # six unfilled tries, then it is left to expire
        prices = [b["limit_price"] for _, b, _ in gs]
        if zero_mleg == "accepted":
            assert prices == ["0.00"] * 6, prices
            tries = [t for t, _, _ in gs]
        elif zero_mleg == "rejected":
            assert prices == ["0.00"] + ["-0.01"] * 6, prices  # the 0.01-credit form, once per try
            tries = [t for t, _, _ in gs[1:]]
        else:
            assert prices == ["0.00", "-0.01"], prices  # both forms refused: GS stops for the session
            assert [x for x in h.scheduler.options.structures if x.underlying == "GS"][0].close_zero_form == "stopped"
            tries = []
        for times in ([t for t, _, _ in jpm], tries):
            gaps = [(y - x).total_seconds() for x, y in zip(times, times[1:], strict=False)]
            assert all(g >= 60 for g in gaps), gaps  # back-off between tries, no hammering

        texts = h.world.discord.texts()
        for und in ("GS", "JPM"):
            notes = [t for t in texts if f"Options left to expire: {und}" in t]
            assert len(notes) == 1, (und, texts)
        assert not [t for t in texts if "Options close not filled: GS" in t or "Options close not filled: JPM" in t], texts

        n_sent, n_texts = len(sent), len(texts)
        await h.advance(27, 16, 30)  # the broker expires the worthless contracts after the close
        await h.execute(6)
        await h.scheduler.alerts.flush()
        assert len(sent) == n_sent, sent[n_sent:]
        assert not any(sym in a.positions for sym in CONTRACTS), {s: a.positions.get(s) for s in CONTRACTS}
        after = h.world.discord.texts()[n_texts:]
        assert not [t for t in after if "GS" in t or "JPM" in t], after

    drive(h, scenario)
