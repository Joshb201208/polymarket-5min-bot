"""Test setup.

`stock_agent.config.Config` reads the environment when it is imported, so the
environment has to be cleaned before any test module imports it. That is why
this happens at module level and not in a fixture.

Tests never touch the network and never read the real `.env`.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Settings that would change the limits or the trading mode under test.
_OVERRIDES = (
    "STOCK_AGENT_MODE",
    "ALPACA_BASE_URL",
    "MAX_POSITION_PCT",
    "MAX_TOTAL_EXPOSURE_PCT",
    "MAX_CLUSTER_PCT",
    "PRIORITY_SYMBOLS",
    "OPTIONS_ENABLED",
    "TIPRANKS_ENABLED",
    "TIPRANKS_MIN_SMART_SCORE",
    "TIPRANKS_UNIVERSE_LIMIT",
    "STOCK_LOG_LEVEL",
)
# Credentials are blanked so no test can reach a real account by accident.
_CREDENTIALS = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "PERPLEXITY_API_KEY",
    "OPENAI_API_KEY",
    "FMP_API_KEY",
    "FINNHUB_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_BOT_TOKEN",
)

for _name in _OVERRIDES:
    os.environ.pop(_name, None)
for _name in _CREDENTIALS:
    os.environ[_name] = ""

# State files go to a throwaway directory, never to data/stock_agent.
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="stock_agent_tests_")


# ---------------------------------------------------------------- fakes
import pytest  # noqa: E402


@pytest.fixture
def world(request, monkeypatch, tmp_path):
    """The simulated outside world (clock, broker, providers), installed.

    Starts Monday 23 Nov 2026 09:00 ET, or at the aware datetime given by
    `@pytest.mark.world_start(dt)`. Every HTTP request the code makes is
    answered by a fake; state files go to this test's own tmp_path.
    """
    from tests.fakes.world import FakeWorld

    mark = request.node.get_closest_marker("world_start")
    w = FakeWorld(start=mark.args[0]) if mark else FakeWorld()
    w.install(monkeypatch, data_dir=tmp_path)
    yield w
    w.assert_no_unexpected_hosts()
