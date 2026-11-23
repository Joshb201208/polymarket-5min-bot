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
- OpenAI Responses /v1/responses and Perplexity Agent /v1/agent for the new research client
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
from dataclasses import dataclass, field
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
@dataclass
class LLMReply:
    """One scripted model answer, rendered in each provider's own shape.

    text      the model's text (a JSON string when the caller asked for a schema)
    sources   web results the provider's search tool returned: dicts with
              url, title and optionally date / snippet
    status    "completed" or "incomplete" (with incomplete_reason, e.g.
              "max_output_tokens"); incomplete output may break the schema
    refusal   a refusal string instead of text (OpenAI refusal content part)
    tool_error  Anthropic: web_search_tool_result_error code inside HTTP 200
              (e.g. "max_uses_exceeded", "unavailable")
    """

    text: str = "{}"
    sources: list[dict] = field(default_factory=list)
    status: str = "completed"
    incomplete_reason: str | None = None
    refusal: str | None = None
    tool_error: str | None = None
    input_tokens: int = 1000
    output_tokens: int = 300
    cost_usd: float | None = None


def _as_reply(x) -> LLMReply:
    if isinstance(x, LLMReply):
        return x
    return LLMReply(text=x if isinstance(x, str) else json.dumps(x))


class _LLM(CannedService):
    """Replies are taken from `replies` in order, then `default_reply`.

    `reply("text")`, `reply({"json": 1})` or `reply(LLMReply(...))`.
    `respond_with(fn)`: fn(request_body: dict) -> reply, used when the queue is
    empty (pick the answer from the prompt, e.g. by model or evidence id).
    `prompts` keeps every request body for assertions.
    """

    default_reply: str = "{}"

    def __init__(self, clock=None):
        self.replies: list[LLMReply] = []
        self.prompts: list[Any] = []
        self.responder = None
        super().__init__(clock)

    def respond_with(self, fn) -> None:
        self.responder = fn

    def reply(self, *items) -> None:
        for t in items:
            self.replies.append(_as_reply(t))

    def _next(self, request: httpx.Request) -> LLMReply:
        body = request_json(request)
        self.prompts.append(body)
        if self.replies:
            return self.replies.pop(0)
        if self.responder is not None:
            return _as_reply(self.responder(body or {}))
        return LLMReply(text=self.default_reply)

    def _model(self, request, default: str) -> str:
        return (request_json(request) or {}).get("model", default)

    def _created(self) -> int:
        return int(self.now().timestamp()) if self.now() else 0


def _cite_annotations(text: str, sources: list[dict], kind: str = "url_citation") -> list[dict]:
    return [{"type": kind, "start_index": 0, "end_index": len(text), "url": s["url"], "title": s.get("title", "")}
            for s in sources]


class FakeOpenAI(_LLM):
    """Chat Completions (today's screener) and the Responses API.

    Responses (https://developers.openai.com/api/docs/guides/tools-web-search):
    output = [web_search_call (action.sources)] + [message with output_text +
    url_citation annotations | refusal]; status completed | incomplete.
    """

    name = "openai"
    hosts = ("api.openai.com",)

    def defaults(self):
        self.on("POST", "/v1/chat/completions", self._chat)
        self.on("POST", "/v1/responses", self._responses)

    def _chat(self, request):
        r = self._next(request)
        return {
            "id": "chatcmpl-fake", "object": "chat.completion", "created": self._created(),
            "model": self._model(request, "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": r.text, "refusal": r.refusal},
                         "finish_reason": "length" if r.status == "incomplete" else "stop"}],
            "usage": {"prompt_tokens": r.input_tokens, "completion_tokens": r.output_tokens,
                      "total_tokens": r.input_tokens + r.output_tokens},
        }

    def _responses(self, request):
        r = self._next(request)
        body = request_json(request) or {}
        searched = any(t.get("type", "").startswith("web_search") for t in body.get("tools") or [])
        output: list[dict] = []
        if searched or r.sources:
            output.append({"type": "web_search_call", "id": "ws_fake", "status": "completed",
                           "action": {"type": "search", "query": "fake query",
                                      "sources": [{"type": "url", "url": s["url"]} for s in r.sources]}})
        if r.refusal is not None:
            content = [{"type": "refusal", "refusal": r.refusal}]
        else:
            content = [{"type": "output_text", "text": r.text, "annotations": _cite_annotations(r.text, r.sources)}]
        output.append({"type": "message", "id": "msg_fake", "status": r.status, "role": "assistant",
                       "content": content})
        return {
            "id": f"resp_fake_{len(self.prompts)}", "object": "response", "created_at": self._created(),
            "status": r.status,
            "incomplete_details": {"reason": r.incomplete_reason or "max_output_tokens"} if r.status == "incomplete" else None,
            "error": None, "model": self._model(request, "fake"), "output": output,
            "usage": {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                      "total_tokens": r.input_tokens + r.output_tokens,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        }


class FakePerplexity(_LLM):
    """Agent API `POST /v1/agent` (alias `/v1/responses`) and legacy
    `/chat/completions` (used by today's analyst.py).

    Agent shape (https://docs.perplexity.ai/docs/agent-api/quickstart and the
    Create Agent Response reference): output = [search_results {queries,
    results[{id,title,url,snippet,date,source}]}] + [message output_text];
    usage.cost.total_cost. `legacy_chat_status` can make /chat/completions
    fail (Sonar Chat Completions support ended 27 Sep 2026; behaviour today
    unverified, default still answers).
    """

    name = "perplexity"
    hosts = ("api.perplexity.ai",)
    legacy_chat_status: int = 200

    def defaults(self):
        self.on("POST", "/chat/completions", self._chat)
        self.on("POST", "/v1/agent", self._agent)
        self.on("POST", "/v1/responses", self._agent)

    def _chat(self, request):
        if self.legacy_chat_status != 200:
            return json_response({"error": {"message": "fake: legacy endpoint unavailable"}}, self.legacy_chat_status)
        r = self._next(request)
        return {
            "id": "pplx-fake", "model": self._model(request, "sonar"), "created": self._created(),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": r.text}, "finish_reason": "stop"}],
            "citations": [s["url"] for s in r.sources],
            "search_results": [{"title": s.get("title", ""), "url": s["url"], "date": s.get("date")} for s in r.sources],
            "usage": {"prompt_tokens": r.input_tokens, "completion_tokens": r.output_tokens,
                      "total_tokens": r.input_tokens + r.output_tokens},
        }

    def _agent(self, request):
        r = self._next(request)
        output: list[dict] = []
        if r.sources:
            output.append({"type": "search_results", "queries": ["fake query"], "results": [
                {"id": i + 1, "title": s.get("title", ""), "url": s["url"], "snippet": s.get("snippet", ""),
                 "date": s.get("date"), "last_updated": s.get("last_updated"), "source": "web"}
                for i, s in enumerate(r.sources)]})
        output.append({"type": "message", "id": "msg_fake", "status": r.status, "role": "assistant",
                       "content": [{"type": "output_text", "text": r.text,
                                    "annotations": _cite_annotations(r.text, r.sources)}]})
        return {
            "id": f"resp_fake_{len(self.prompts)}", "object": "response", "created_at": self._created(),
            "status": r.status, "model": self._model(request, "perplexity/sonar"), "output": output,
            "usage": {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                      "total_tokens": r.input_tokens + r.output_tokens,
                      "cost": {"currency": "USD", "total_cost": r.cost_usd if r.cost_usd is not None else 0.001}},
        }


class FakeAnthropic(_LLM):
    """Messages API `POST /v1/messages`.

    With a web_search tool in the request (or sources on the reply) the
    content is server_tool_use + web_search_tool_result + text blocks with
    web_search_result_location citations; `tool_error` puts a
    web_search_tool_result_error inside an HTTP 200
    (https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool).
    """

    name = "anthropic"
    hosts = ("api.anthropic.com",)

    def defaults(self):
        self.on("POST", "/v1/messages", self._messages)

    def _messages(self, request):
        r = self._next(request)
        body = request_json(request) or {}
        searched = any(str(t.get("type", "")).startswith("web_search") for t in body.get("tools") or [])
        content: list[dict] = []
        n_search = 0
        if searched or r.sources or r.tool_error:
            n_search = 1
            content.append({"type": "server_tool_use", "id": "srvtoolu_fake", "name": "web_search",
                            "input": {"query": "fake query"}})
            if r.tool_error:
                result = {"type": "web_search_tool_result_error", "error_code": r.tool_error}
            else:
                result = [{"type": "web_search_result", "url": s["url"], "title": s.get("title", ""),
                           "page_age": s.get("page_age"), "encrypted_content": "fake"} for s in r.sources]
            content.append({"type": "web_search_tool_result", "tool_use_id": "srvtoolu_fake", "content": result})
        citations = [{"type": "web_search_result_location", "url": s["url"], "title": s.get("title", ""),
                      "cited_text": r.text[:150], "encrypted_index": "fake"} for s in r.sources]
        block = {"type": "text", "text": r.text}
        if citations:
            block["citations"] = citations
        content.append(block)
        return {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": self._model(request, "fake"),
            "content": content,
            "stop_reason": "max_tokens" if r.status == "incomplete" else ("refusal" if r.refusal else "end_turn"),
            "usage": {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                      "server_tool_use": {"web_search_requests": n_search}},
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
    """`/company-news` serves stories added with `add_news`, only once the
    simulated clock has reached their time (needs a clock)."""

    name = "finnhub"
    hosts = ("finnhub.io",)

    def __init__(self, clock=None):
        self.news: list[dict] = []
        super().__init__(clock)

    def add_news(self, symbol: str, headline: str, *, at=None, summary: str = "", source: str = "Reuters") -> dict:
        at = at or self.now()
        n = {"category": "company", "datetime": int(at.timestamp()), "headline": headline, "id": len(self.news) + 1,
             "image": "", "related": symbol, "source": source, "summary": summary,
             "url": f"https://example.invalid/finnhub/{len(self.news) + 1}"}
        self.news.append(n)
        return n

    def _company_news(self, request):
        sym = request.url.params.get("symbol", "")
        now = self.now().timestamp() if self.now() else float("inf")
        rows = [n for n in self.news if n["related"] == sym and n["datetime"] <= now]
        return sorted(rows, key=lambda n: n["datetime"], reverse=True)

    def defaults(self):
        self.on("GET", "/api/v1/*", [])
        self.on("GET", "/api/v1/company-news", self._company_news)
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
