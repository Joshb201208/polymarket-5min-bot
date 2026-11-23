"""Simple fakes for every non-broker service the bot calls.

Each fake answers from a route table of canned responses. Tests override a
route with `svc.on("GET", "/path*", data_or_callable)`; the callable gets the
`httpx.Request` and returns JSON data or an `httpx.Response`. Every fake has
the same fault injection as the broker (`svc.fail(...)`, see base.py), and
every request is recorded on the `FakeInternet`.

Hosts (taken from the constants in stock_agent/*.py):
- OpenAI      api.openai.com/v1/chat/completions            (screener.py)
- Perplexity  api.perplexity.ai/chat/completions            (analyst.py)
- Anthropic   api.anthropic.com/v1/messages                 (not used by the code today)
- FMP         financialmodelingprep.com/stable/...          (data_feed.py, options_portfolio.py)
- Finnhub     finnhub.io/api/v1/...                         (data_feed, news_feed, sentiment, analyst_trends)
- SEC EDGAR   data.sec.gov / www.sec.gov                    (data_feed)
- TipRanks    api.tipranks.com/api/stocks/screener (requests) (tipranks_client.py)
- Discord     discord.com/api/v10/channels/{id}/messages    (discord_alerts.py)
- Telegram    api.telegram.org/bot{token}/sendMessage       (telegram_alerts.py)
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any, Callable

import httpx

from tests.fakes.base import FakeService, json_response, request_json

Handler = Any  # data | Callable[[httpx.Request], data | httpx.Response]


class CannedService(FakeService):
    def __init__(self, clock=None):
        super().__init__()
        self.clock = clock
        self.routes: list[tuple[str, str, Handler]] = []
        self.defaults()

    def now(self):
        return self.clock.now() if self.clock else None

    def defaults(self) -> None:
        """Subclasses register their default routes here."""

    def on(self, method: str, path: str, handler: Handler) -> None:
        """Answer `method path` (fnmatch) with `handler`. Later calls win."""
        self.routes.insert(0, (method.upper(), path, handler))

    def handle(self, request: httpx.Request) -> httpx.Response:
        for method, pattern, handler in self.routes:
            if method not in ("*", request.method) or not fnmatch.fnmatchcase(request.url.path, pattern):
                continue
            data = handler(request) if callable(handler) else handler
            if isinstance(data, httpx.Response):
                return data
            return json_response(data)
        return json_response({"error": f"fake {self.name}: no route for {request.method} {request.url.path}"}, 404)


# --------------------------------------------------------------------- LLMs
class _LLM(CannedService):
    """Replies are taken from `replies` in order, then `default_reply`."""

    default_reply: str = "{}"

    def __init__(self, clock=None):
        self.replies: list[str] = []
        self.prompts: list[Any] = []
        super().__init__(clock)

    def reply(self, *texts: str | dict | list) -> None:
        for t in texts:
            self.replies.append(t if isinstance(t, str) else json.dumps(t))

    def _next(self, request: httpx.Request) -> str:
        self.prompts.append(request_json(request))
        return self.replies.pop(0) if self.replies else self.default_reply


class FakeOpenAI(_LLM):
    name = "openai"
    hosts = ("api.openai.com",)

    def defaults(self):
        self.on("POST", "/v1/chat/completions", self._chat)

    def _chat(self, request):
        text = self._next(request)
        return {
            "id": "chatcmpl-fake", "object": "chat.completion", "model": (request_json(request) or {}).get("model", "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class FakePerplexity(_LLM):
    name = "perplexity"
    hosts = ("api.perplexity.ai",)

    def defaults(self):
        self.on("POST", "/chat/completions", self._chat)

    def _chat(self, request):
        text = self._next(request)
        return {
            "id": "pplx-fake", "model": (request_json(request) or {}).get("model", "sonar"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "citations": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class FakeAnthropic(_LLM):
    name = "anthropic"
    hosts = ("api.anthropic.com",)

    def defaults(self):
        self.on("POST", "/v1/messages", self._messages)

    def _messages(self, request):
        text = self._next(request)
        return {
            "id": "msg_fake", "type": "message", "role": "assistant",
            "model": (request_json(request) or {}).get("model", "fake"),
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


# ---------------------------------------------------------------- data APIs
class FakeFMP(CannedService):
    """Financial Modeling Prep (stable API).

    `/quote` prices come from `price_source(symbol)` (for example
    `alpaca.price`) so the bot's FMP view and the broker agree; `profiles`
    supplies `/profile` rows. Everything else returns an empty list.
    """

    name = "fmp"
    hosts = ("financialmodelingprep.com",)

    def __init__(self, clock=None, price_source: Callable[[str], float | None] | None = None):
        self.price_source = price_source
        self.prices: dict[str, float] = {}
        self.profiles: dict[str, dict] = {}
        super().__init__(clock)

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def set_profile(self, symbol: str, **fields) -> dict:
        p = {
            "symbol": symbol, "companyName": f"{symbol} Inc.", "price": self._price(symbol) or 100.0,
            "marketCap": 50_000_000_000, "beta": 1.0, "sector": "Technology", "industry": "Software",
            "exchange": "NASDAQ", "country": "US", "isEtf": False, "isActivelyTrading": True,
            "volAvg": 5_000_000, "averageVolume": 5_000_000,
        }
        p.update(fields)
        self.profiles[symbol] = p
        return p

    def _price(self, symbol: str) -> float | None:
        if self.price_source:
            v = self.price_source(symbol)
            if v is not None:
                return v
        return self.prices.get(symbol)

    def defaults(self):
        self.on("GET", "/stable/*", [])
        self.on("GET", "/stable/quote", self._quote)
        self.on("GET", "/stable/profile", self._profile)

    def _quote(self, request):
        sym = request.url.params.get("symbol", "")
        px = self._price(sym)
        if px is None:
            return []
        return [{"symbol": sym, "price": px, "change": 0.0, "changePercentage": 0.0, "volume": 1_000_000,
                 "dayLow": px, "dayHigh": px, "open": px, "previousClose": px, "timestamp": 0}]

    def _profile(self, request):
        sym = request.url.params.get("symbol", "")
        if sym in self.profiles:
            p = dict(self.profiles[sym])
            p["price"] = self._price(sym) or p["price"]
            return [p]
        return []


class FakeFinnhub(CannedService):
    name = "finnhub"
    hosts = ("finnhub.io",)

    def defaults(self):
        self.on("GET", "/api/v1/*", [])
        self.on("GET", "/api/v1/calendar/earnings", {"earningsCalendar": []})
        self.on("GET", "/api/v1/news-sentiment", {})
        self.on("GET", "/api/v1/stock/insider-transactions", {"data": []})


class FakeSEC(CannedService):
    name = "sec"
    hosts = ("data.sec.gov", "www.sec.gov", "efts.sec.gov")

    def defaults(self):
        self.on("GET", "*", {})
        self.on("GET", "/submissions/*", {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": []}}})


class FakeTipRanks(CannedService):
    """Reached through `requests.get` (tipranks_client.py)."""

    name = "tipranks"
    hosts = ("api.tipranks.com", "www.tipranks.com")

    def __init__(self, clock=None):
        self.rows: list[dict] = []
        super().__init__(clock)

    def defaults(self):
        self.on("GET", "/api/stocks/screener", lambda r: {"data": self.rows})


# ----------------------------------------------------------------- alerting
class FakeDiscord(CannedService):
    name = "discord"
    hosts = ("discord.com", "discordapp.com")

    def __init__(self, clock=None):
        self.messages: list[dict] = []  # {"channel": id, "content": ..., "embeds": [...]}
        super().__init__(clock)

    def defaults(self):
        self.on("*", "*", {})
        self.on("POST", "/api/v10/channels/*/messages", self._post)

    def rate_limit(self, times: int = 1, retry_after: float = 1.0) -> None:
        """Discord-shaped 429 (body carries retry_after, which the code reads)."""
        self.fail("/api/v10/channels/*/messages", "status", method="POST", times=times, status=429,
                  body={"message": "You are being rate limited.", "retry_after": retry_after, "global": False})

    def _post(self, request):
        body = request_json(request) or {}
        channel = request.url.path.split("/")[4]
        msg = {"channel": channel, **body}
        self.messages.append(msg)
        return {"id": str(len(self.messages)), "channel_id": channel, **body}

    def texts(self) -> list[str]:
        """Flattened text of every message: content + embed titles/descriptions/fields."""
        out = []
        for m in self.messages:
            parts = [m.get("content") or ""]
            for e in m.get("embeds") or []:
                parts += [e.get("title") or "", e.get("description") or ""]
                for f in e.get("fields") or []:
                    parts += [str(f.get("name", "")), str(f.get("value", ""))]
            out.append("\n".join(p for p in parts if p))
        return out


class FakeTelegram(CannedService):
    name = "telegram"
    hosts = ("api.telegram.org",)

    def __init__(self, clock=None):
        self.messages: list[dict] = []
        super().__init__(clock)

    def defaults(self):
        self.on("*", "*", {"ok": True, "result": True})
        self.on("POST", "/bot*/sendMessage", self._send)

    def _send(self, request):
        body = request_json(request) or {}
        self.messages.append(body)
        return {"ok": True, "result": {"message_id": len(self.messages), "text": body.get("text", "")}}

    def texts(self) -> list[str]:
        return [m.get("text", "") for m in self.messages]


ALL_PROVIDERS = (FakeOpenAI, FakePerplexity, FakeAnthropic, FakeFMP, FakeFinnhub, FakeSEC, FakeTipRanks,
                 FakeDiscord, FakeTelegram)
