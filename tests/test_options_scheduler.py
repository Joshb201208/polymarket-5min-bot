"""The scheduler's two options hooks delegate to OptionsManager only."""
import asyncio
from types import SimpleNamespace

from stock_agent.scheduler import StockAgentScheduler


class FakeOptions:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def scan(self, now, candidates=None):
        self.calls.append(("scan", now))
        if self.fail:
            raise RuntimeError("boom")
        return [SimpleNamespace(opened="os-1", reason="opened")]

    async def monitor(self, now):
        self.calls.append(("monitor", now))
        if self.fail:
            raise RuntimeError("boom")


def _sched(opts):
    s = StockAgentScheduler.__new__(StockAgentScheduler)
    s.options = opts
    s._options_scan_done_today = None
    return s


def test_scan_and_monitor_delegate_to_manager():
    opts = FakeOptions()
    s = _sched(opts)
    out = asyncio.run(s._run_options_scan())
    asyncio.run(s._check_options_positions())
    assert out[0].opened == "os-1"
    assert [c[0] for c in opts.calls] == ["scan", "monitor"]
    assert all(c[1].tzinfo is not None for c in opts.calls)
    assert s._options_scan_done_today is not None


def test_failures_propagate_to_the_caller():
    import pytest
    s = _sched(FakeOptions(fail=True))
    with pytest.raises(RuntimeError):
        asyncio.run(s._run_options_scan())
    assert s._options_scan_done_today is None  # a failed scan is not marked done
    with pytest.raises(RuntimeError):
        asyncio.run(s._check_options_positions())


def test_no_manager_places_nothing(caplog):
    s = _sched(None)
    asyncio.run(s._run_options_scan())
    asyncio.run(s._check_options_positions())
    assert "no OptionsManager wired" in caplog.text
