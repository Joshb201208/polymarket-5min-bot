"""Fault injection, request recording, client patching and provider fakes."""

import asyncio

import httpx
import pytest
import requests

from tests.fakes import FakeAlpaca, FakeInternet, SimClock
from tests.fakes.providers import FakeDiscord, FakeFMP, FakeOpenAI, FakeTelegram, FakeTipRanks

BASE = "https://paper-api.alpaca.markets"


@pytest.fixture
def net():
    clock = SimClock.at_et(2026, 11, 23, 10, 0)
    alpaca = FakeAlpaca(clock)
    alpaca.set_price("AAA", 100)
    return FakeInternet([alpaca, FakeDiscord(clock), FakeFMP(clock, price_source=alpaca.price),
                         FakeTelegram(clock), FakeOpenAI(clock), FakeTipRanks(clock)])


def client(net):
    return httpx.Client(transport=net.transport)


@pytest.mark.parametrize("kind,check", [
    ("5xx", lambda r: r.status_code == 500),
    ("429", lambda r: r.status_code == 429 and r.headers["Retry-After"] == "1"),
    ("empty_list", lambda r: r.json() == []),
    ("empty_object", lambda r: r.json() == {}),
])
def test_status_faults(net, kind, check):
    alpaca = net.service("alpaca")
    alpaca.fail("/v2/positions", kind)
    c = client(net)
    assert check(c.get(f"{BASE}/v2/positions"))
    assert c.get(f"{BASE}/v2/positions").status_code == 200  # times=1 by default


def test_malformed_json(net):
    net.service("alpaca").fail("/v2/account", "malformed_json")
    with pytest.raises(ValueError):
        client(net).get(f"{BASE}/v2/account").json()


def test_timeout_does_not_reach_the_service(net):
    alpaca = net.service("alpaca")
    alpaca.fail("/v2/orders", "timeout", method="POST")
    with pytest.raises(httpx.ReadTimeout):
        client(net).post(f"{BASE}/v2/orders", json={"symbol": "AAA", "qty": "1", "side": "buy", "type": "market",
                                                    "time_in_force": "day"})
    assert alpaca.orders == {}
    assert net.recorded[-1].processed is False


def test_lost_response_still_places_the_order(net):
    alpaca = net.service("alpaca")
    alpaca.fail("/v2/orders", "lost_response", method="POST")
    with pytest.raises(httpx.ReadTimeout):
        client(net).post(f"{BASE}/v2/orders", json={"symbol": "AAA", "qty": "1", "side": "buy", "type": "market",
                                                    "time_in_force": "day", "client_order_id": "k1"})
    assert alpaca.position("AAA").qty == 1
    assert net.recorded[-1].processed is True and net.recorded[-1].fault == "lost_response"


def test_fault_forever_and_by_method(net):
    alpaca = net.service("alpaca")
    alpaca.fail("/v2/*", "5xx", method="GET", times=None)
    c = client(net)
    assert all(c.get(f"{BASE}/v2/account").status_code == 500 for _ in range(3))
    alpaca.clear_faults()
    assert c.get(f"{BASE}/v2/account").status_code == 200


def test_unknown_fault_kind_rejected(net):
    with pytest.raises(ValueError):
        net.service("alpaca").fail("/v2/account", "gremlins")


def test_unknown_host_fails_loudly_and_is_recorded(net):
    with pytest.raises(httpx.ConnectError):
        client(net).get("https://example.com/")
    assert net.unexpected and net.unexpected[0].host == "example.com"


def test_install_patches_async_and_sync_httpx_and_requests(net, monkeypatch):
    net.install(monkeypatch)

    async def go():
        async with httpx.AsyncClient(timeout=5) as c:
            return (await c.get(f"{BASE}/v2/account")).status_code

    assert asyncio.run(go()) == 200
    assert httpx.Client().get(f"{BASE}/v2/clock").status_code == 200
    r = requests.get("https://api.tipranks.com/api/stocks/screener", params={"x": "1"}, timeout=5)
    assert r.status_code == 200 and r.json() == {"data": []}
    net.service("tipranks").fail("/api/stocks/screener", "timeout")
    with pytest.raises(requests.exceptions.Timeout):
        requests.get("https://api.tipranks.com/api/stocks/screener", timeout=5)
    assert [r.service for r in net.recorded] == ["alpaca", "alpaca", "tipranks", "tipranks"]


def test_recorder_redacts_credentials(net):
    client(net).get(f"{BASE}/v2/account", headers={"APCA-API-SECRET-KEY": "s3cret", "Authorization": "Bearer x"})
    h = net.recorded[-1].headers
    assert h["apca-api-secret-key"] == "<redacted>" and h["authorization"] == "<redacted>"


def test_requests_filter(net):
    c = client(net)
    c.get(f"{BASE}/v2/account")
    c.post(f"{BASE}/v2/orders", json={"symbol": "AAA", "qty": "1", "side": "buy", "type": "market",
                                      "time_in_force": "day"})
    assert len(net.requests(service="alpaca", method="POST", path="/v2/orders")) == 1
    assert net.requests(method="POST")[0].json["symbol"] == "AAA"


def test_discord_records_messages_and_rate_limits(net):
    d = net.service("discord")
    c = client(net)
    d.rate_limit(retry_after=0.5)
    r = c.post("https://discord.com/api/v10/channels/42/messages", json={"content": "hi"})
    assert r.status_code == 429 and r.json()["retry_after"] == 0.5
    c.post("https://discord.com/api/v10/channels/42/messages",
           json={"embeds": [{"title": "STOP", "fields": [{"name": "Symbol", "value": "AAA"}]}]})
    assert d.messages[0]["channel"] == "42" and "STOP\nSymbol\nAAA" in d.texts()[0]


def test_telegram_and_openai(net):
    c = client(net)
    c.post("https://api.telegram.org/bot123:abc/sendMessage", json={"chat_id": 1, "text": "hello"})
    assert net.service("telegram").texts() == ["hello"]
    oa = net.service("openai")
    oa.reply({"picks": []})
    r = c.post("https://api.openai.com/v1/chat/completions", json={"model": "m", "messages": []}).json()
    assert r["choices"][0]["message"]["content"] == '{"picks": []}'
    assert oa.prompts[0]["model"] == "m"


def test_fmp_quote_follows_broker_price(net):
    c = client(net)
    j = c.get("https://financialmodelingprep.com/stable/quote", params={"symbol": "AAA"}).json()
    assert j[0]["price"] == 100
    assert c.get("https://financialmodelingprep.com/stable/quote", params={"symbol": "ZZZ"}).json() == []
    assert c.get("https://financialmodelingprep.com/stable/ratios", params={"symbol": "AAA"}).json() == []


def test_world_fixture_runs_the_real_executor(world):
    """The bot's own Executor, unchanged, against the fake broker."""
    from stock_agent.config import Config
    from stock_agent.executor import Executor

    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50)
    ex = Executor(Config())

    async def go():
        acct = await ex.get_account()
        res = await ex.place_buy("AAA", 10, 46.5)
        return acct, res, await ex.get_positions(), await ex.get_open_orders()

    acct, res, positions, open_orders = asyncio.run(go())
    assert acct["equity"] == "100000"
    assert positions[0]["symbol"] == "AAA" and positions[0]["qty"] == "10"
    assert [o["type"] for o in open_orders] == ["stop"]
    assert len(world.orders_sent()) == 2
    assert world.clock.sleeps == [3]


def test_llm_shapes():
    from tests.fakes.providers import FakeAnthropic, FakePerplexity, LLMReply

    clock = SimClock.at_et(2026, 11, 23, 10, 0)
    oa, px, an = FakeOpenAI(clock), FakePerplexity(clock), FakeAnthropic(clock)
    c = httpx.Client(transport=FakeInternet([oa, px, an]).transport)
    src = [{"url": "https://example.invalid/a", "title": "A", "date": "2026-11-23"}]
    oa.reply(LLMReply(text='{"ok": true}', sources=src))
    j = c.post("https://api.openai.com/v1/responses", json={"tools": [{"type": "web_search"}]}).json()
    assert [o["type"] for o in j["output"]] == ["web_search_call", "message"]
    assert j["output"][1]["content"][0]["annotations"][0]["type"] == "url_citation"
    oa.reply(LLMReply(refusal="no", status="incomplete"))
    j = c.post("https://api.openai.com/v1/responses", json={}).json()
    assert j["status"] == "incomplete" and j["output"][0]["content"][0]["type"] == "refusal"
    px.reply(LLMReply(text="x", sources=src, cost_usd=0.002))
    j = c.post("https://api.perplexity.ai/v1/agent", json={}).json()
    assert j["output"][0]["results"][0]["id"] == 1 and j["usage"]["cost"]["total_cost"] == 0.002
    an.reply(LLMReply(text="y", tool_error="max_uses_exceeded"))
    j = c.post("https://api.anthropic.com/v1/messages", json={"tools": [{"type": "web_search_20260318"}]}).json()
    assert j["content"][1]["content"]["error_code"] == "max_uses_exceeded" and j["content"][-1]["text"] == "y"


def test_world_news_reaches_both_feeds(world):
    from datetime import timedelta

    world.news(["AAA"], "AAA guidance cut", at=world.clock.now() + timedelta(minutes=5))
    c = httpx.Client()
    url = "https://finnhub.io/api/v1/company-news"
    assert c.get(url, params={"symbol": "AAA"}).json() == []
    world.clock.advance(minutes=5)
    assert c.get(url, params={"symbol": "AAA"}).json()[0]["headline"] == "AAA guidance cut"
    assert c.get("https://data.alpaca.markets/v1beta1/news", params={"symbols": "AAA"}).json()["news"]
