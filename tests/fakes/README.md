# Test fakes: a simulated outside world

Tests run the real bot code against fakes at the HTTP boundary. No request
leaves the process: `httpx` (sync and async) and `requests` are routed to the
fakes, `aiohttp` is blocked, and any request to a host no fake serves fails
the test (`world.assert_no_unexpected_hosts()`, run automatically by the
`world` fixture). There is no network and no credential in any test; the
fixture puts obvious placeholder strings on `Config`.

## Quick start

```python
from tests.fakes import ET
from tests.fakes.bot import buy_signal          # builders for today's models

def test_buy_gets_a_stop(world):                # fixture: tests/conftest.py
    world.clock.set_et(2026, 11, 23, 10, 0)     # Monday, market open
    world.price("AAA", 50.0, spread=0.02)       # Alpaca quote + FMP quote
    sched = world.scheduler()                   # real StockAgentScheduler
    sched._pending_signals = [buy_signal("AAA")]
    asyncio.run(sched._tick(world.clock.now_et()))
    world.clock.advance(minutes=5)              # fills happen as time moves
    assert world.alpaca.position("AAA").qty > 0
    assert [o.type for o in world.alpaca.open_orders("AAA")] == ["stop"]
    assert world.alpaca.equity() > 0
    assert world.alert_texts()                  # Discord + Telegram bodies
```

Start the fixture at another time with `@pytest.mark.world_start(aware_dt)`.
Default: Monday 2026-11-23 09:00 ET. State files go to the test's `tmp_path`
(`Config.DATA_DIR`).

Without the fixture (e.g. a client unit test with an injected transport):

```python
clock = SimClock.at_et(2026, 11, 23, 9, 35)
alpaca = FakeAlpaca(clock)
net = FakeInternet([alpaca]).install(monkeypatch)   # or use alpaca.transport()
clock.install(monkeypatch)
```

## Modules

| Module | What it is |
|---|---|
| `clock.py` | `SimClock` (simulated now; `install` patches `datetime` in `stock_agent.*`, the scheduler's `_now_et/_now_utc`, and `asyncio.sleep` so sleeping advances simulated time instantly). `MarketCalendar`: NYSE sessions 2026–2028 incl. holidays and 13:00 early closes. Constants `THANKSGIVING_2026`, `DAY_AFTER_THANKSGIVING_2026`, `GOOD_FRIDAY_2026`, `DST_END_2026`, `DST_START_2027`. |
| `base.py` | `FakeService` (routing, fault injection), `FakeInternet` (installs the transports, records every request as `RecordedRequest`). |
| `alpaca.py` | `FakeAlpaca`: trading API + market data API, in memory. |
| `providers.py` | `FakeOpenAI`, `FakePerplexity`, `FakeAnthropic`, `FakeFMP`, `FakeFinnhub`, `FakeSEC`, `FakeTipRanks`, `FakeDiscord`, `FakeTelegram`. |
| `world.py` | `FakeWorld`: all of the above wired together, plus assertions. |
| `bot.py` | Builders for today's `Thesis`, `Signal`, `Position`. |
| `state.py` | `corrupt(path, how)`: garbage, truncate (torn write), empty, zero_bytes_tail (lost page), delete. `world.data_dir` is the test's state directory. |
| `../scenarios/` | `ScenarioRunner` and the scripted `E2EWeek`. |

## Recording and assertions

`world.net.recorded` holds every outbound request (service, method, url, path,
params, json, redacted headers, simulated time, status, fault, `processed`).
Filter with `world.net.requests(service, method, path_glob, where=fn)`.

- `world.orders_sent(include_failed=True)`, `world.cancels_sent()`,
  `world.assert_no_orders()`
- `world.alerts_sent()`, `world.alert_texts()` (Discord and Telegram)
- `world.alpaca.position(sym)`, `open_orders(sym)`, `orders_for(sym)`,
  `price(sym)`, `equity()`; LLM fakes keep `.prompts`.

## Fault injection (every fake)

```python
world.alpaca.fail("/v2/positions", "5xx", method="GET")           # once
world.alpaca.fail("/v2/orders", "lost_response", method="POST")    # broker acts, reply lost
world.fmp.fail("/stable/quote", "429", times=None)                 # every time
world.alpaca.fail("/v2/orders", "status", status=403, body={"code": 40310000, "message": "..."})
```

Kinds: `timeout`, `connect_error`, `5xx`, `429` (with `Retry-After`),
`malformed_json`, `empty_list`, `empty_object`, `status` (any status/body),
`lost_response` (the fake processes the request, then the client gets a
timeout). `path` is an fnmatch glob; `times=None` means forever;
`clear_faults()` resets.

## FakeAlpaca

Endpoints: account; account configurations GET/PATCH (`no_shorting` read
back); clock (v2, v3); calendar; assets; orders list/create/get/replace/
cancel/cancel-all (207 multi-status), `orders:by_client_order_id`; positions
list/get/close/close-all, option exercise; account activities (paged with
`page_token`, `page_size` ≤ 100; FILL, DIV, OPEXP, OPASN, OPEXC); portfolio
history; options contracts; data: stock quotes/trades/bars/snapshots (single
and multi), v1beta1 news (start/end/page_token; stories dated in the future
stay hidden until the clock reaches them), option snapshots (with greeks)/
quotes/trades. Orders: market, limit, stop, stop_limit, trailing_stop,
bracket/oco/oto, `mleg` with per-leg fills, extended hours, partial fills
(`script_partial_fills`).

Test helpers: `add_asset`, `set_price(sym, last, bid=, ask=, spread=)`,
`seed_position`, `submit(body)`, `script_partial_fills`, `add_news`,
`seed_daily_bars`, `pay_dividend`, `add_option_contract`, `add_option_chain`,
`set_option_quote`, `set_option_greeks`, `settle_option_expiry`,
`assign_early`, `corporate_action_cancel`.

Rejections modelled: insufficient buying power; insufficient qty (403, with
`held_for_orders`); potential wash trade (403, opposite side of an open order);
shorting disabled (`no_shorting` config or account flag); covered-call 403
"insufficient underlying qty available for covered call (required: X,
available: Y)"; options level; non-marketable/invalid params (422); duplicate
`client_order_id` (422).

### Behaviour switches (`AlpacaBehaviour`)

Where the docs are silent, behaviour is a switch with a stated status.

| Switch | Default | Status |
|---|---|---|
| `wash_trade_check` | True | documented ([user protection](https://docs.alpaca.markets/us/docs/user-protection)) |
| `allow_long_to_short_flip` | False | unverified; a sell above the long qty is rejected unless enabled |
| `shorting_disabled_message` | "account is not allowed to short" | unverified wording |
| `holiday_day_order_policy` | `cancel_at_close` (or `queue_next_session`) | evidence only (holiday orders cancelled unfilled) |
| `day_order_end_status` | `canceled` | documented ([orders](https://docs.alpaca.markets/docs/orders-at-alpaca)) |
| `gtc_max_age_days` | 90 | documented 90-day GTC; resulting status unverified |
| `fill_delay_seconds` | 0 | evidence (mid-session paper fills in seconds) |
| `opening_fill_delay_seconds` / `opening_window_seconds` | 300 / 120 | evidence (opening fills took ~1–10 min) |
| `extended_hours_fills` | False | simplification |
| `client_order_id_unique_scope` | `all` | unverified |
| `cancel_latency_seconds` | 0 | simplification (`pending_cancel` when > 0) |
| `require_auth` | False | when True, missing key headers get 401 |
| `auto_expire_options` | True | documented OPEXP activity ([option events](https://docs.alpaca.markets/us/docs/non-trade-activities-for-option-events)) |
| `expiry_day_liquidation_time` | None | evidence (broker closed expiring spreads at 15:30 ET); set `time(15, 30)` to model it |
| `covered_call_locks_shares` | True | documented (`qty_available` excludes shares locked for covered calls) |
| `short_sale_bp_factor` | 1.03 | documented range 102–103 % |

## Provider fakes

- LLMs: `reply(LLMReply(text, sources=[...], status=, incomplete_reason=,
  refusal=, tool_error=, input_tokens=, output_tokens=, cost_usd=))` queues a
  reply (or pass a function of the request). Shapes: OpenAI Chat
  Completions and Responses API with `web_search_call` + `url_citation`
  annotations; Perplexity Agent API `/v1/agent` (alias `/v1/responses`,
  `search_results`, `usage.cost.total_cost`) and legacy `/chat/completions`
  (`legacy_chat_status` switch); Anthropic Messages with
  `server_tool_use`/`web_search_tool_result` blocks and citations.
- `FakeFMP`: `/stable/quote` follows the Alpaca price; `set_profile`; other
  paths return `[]` unless you `on(...)` a handler.
- `FakeFinnhub.add_news(at=)` (timed), `FakeSEC`, `FakeTipRanks(rows)`.
- `FakeDiscord` (`messages`, `texts()`, `rate_limit()`), `FakeTelegram`.
- Any fake: `on(method, path_glob, handler)` overrides a route.

## Scenario runner

```python
from tests.scenarios import ScenarioRunner
from tests.scenarios.week import E2EWeek

@pytest.mark.world_start(datetime(2026, 11, 23, 7, 0, tzinfo=ET))
def test_week(world):
    r = ScenarioRunner(world)                 # build=, startup=, step= are overridable
    r.at(datetime(2026, 11, 24, 11, 0, tzinfo=ET), lambda w, r: w.price("BBB", 44.0), "BBB drop")
    r.at(datetime(2026, 11, 25, 10, 0, tzinfo=ET), r.restart)       # process restart
    r.restart_after_lost_order()              # crash right after an order whose reply is lost
    E2EWeek().run(r)                          # or r.run_day / r.run_days / r.run_week
    assert not r.errors, r.summary()
```

One step per simulated minute (07:00–16:30 by default; nights are skipped,
the broker catches up on the next request). A tick that raises is recorded
in `r.errors` and the run continues, like the production loop. A full
simulated week of today's scheduler runs in about 2 seconds.

## Characterisation tests

`tests/test_findings_today.py` states the correct behaviour for the audit's
worst findings and is marked `xfail(strict=True, raises=AssertionError)`.
When you fix a finding, its test XPASSes, strict mode fails the run, and you
delete the marker so it stays as a regression test. If your redesign renames
what a test drives (e.g. `_sync_portfolio_from_alpaca`), the test errors
rather than silently xfailing: port it to the new entry point in the same
change.

## Sources

Alpaca: [orders](https://docs.alpaca.markets/docs/orders-at-alpaca),
[paper trading](https://docs.alpaca.markets/docs/paper-trading),
[create order](https://docs.alpaca.markets/reference/postorder),
[trading OpenAPI](https://docs.alpaca.markets/us/openapi/trading-api.json),
[market data OpenAPI](https://docs.alpaca.markets/us/openapi/market-data-api.json),
[options trading](https://docs.alpaca.markets/docs/options-trading),
[options level 3](https://docs.alpaca.markets/docs/options-level-3-trading),
[options overview and errors](https://docs.alpaca.markets/us/docs/options-trading-overview),
[option events](https://docs.alpaca.markets/us/docs/non-trade-activities-for-option-events),
[account activities](https://docs.alpaca.markets/us/docs/account-activities),
[account plans](https://docs.alpaca.markets/us/docs/account-plans),
[common errors](https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca),
[user protection](https://docs.alpaca.markets/us/docs/user-protection),
[broker API OpenAPI (qty_available)](https://docs.alpaca.markets/eu/openapi/broker-api.json).
NYSE: [hours and calendars](https://www.nyse.com/markets/hours-calendars).
Perplexity: [Agent API quickstart](https://docs.perplexity.ai/docs/agent-api/quickstart),
[agent POST](https://docs.perplexity.ai/api-reference/agent-post.md).
OpenAI: [web search tool](https://developers.openai.com/api/docs/guides/tools-web-search).
Anthropic: [web search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool).
Fetched November 2026.
