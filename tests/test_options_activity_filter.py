"""Live defect 2026-11-24: Alpaca rejects OPXRC as a server-side activity filter
(422 "invalid activity type: OPXRC"), which made every options monitor cycle
stop at reconcile, so no exits, repairs or scans ran. The manager now reads
activities unfiltered and filters locally."""

import asyncio

import pytest

from stock_agent.broker import AlpacaClient, BrokerError
from stock_agent.options_book import StructState
from stock_agent.options_manager import OPTION_ACTIVITY_TYPES
from stock_agent.research_schemas import Direction
from tests.options_support import StubGate, make_manager, seed_chain


def run(coro):
    return asyncio.run(coro)


def _activity_reads(world):
    return world.net.requests(service="alpaca", method="GET", path="/v2/account/activities*")


def test_fake_rejects_opxrc_filter_like_the_live_api(world):
    client = AlpacaClient("https://paper-api.alpaca.markets", "k", "s")
    with pytest.raises(BrokerError, match="422.*invalid activity type: OPXRC"):
        run(client.get_activities(("FILL", "OPXRC")))
    assert run(client.get_activities(None)) is not None


def test_monitor_reconciles_and_reaches_exits_without_server_side_type_filter(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    out = run(m.scan())
    st = m.get(out[0].opened)
    assert st is not None
    world.clock.set_et(2026, 12, 22, 10, 0)  # T-2: time exit due
    seed_chain(world)
    n0 = len(_activity_reads(world))
    res = run(m.monitor())
    assert not res.skipped, res.skipped
    assert res.reconcile is not None
    assert res.exits and res.exits[0][1] == "time_exit"
    assert st.st is StructState.CLOSED
    reads = _activity_reads(world)[n0:]
    assert reads
    for r in reads:
        assert "activity_types" not in r.params
        assert r.path.rstrip("/") == "/v2/account/activities"


def test_both_exercise_spellings_still_parsed_locally():
    assert {"OPEXC", "OPXRC", "OPASN", "OPEXP", "OPTRD", "FILL"} <= set(OPTION_ACTIVITY_TYPES)
