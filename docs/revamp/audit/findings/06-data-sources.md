# 06. Data sources

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

This part of the bot fetches everything it knows about the outside world: prices and company financials from Financial Modeling Prep (FMP), news, analyst and insider data from Finnhub, a "Smart Score" list from TipRanks, option chains and option quotes from Alpaca, and a handful of index and ETF prices that decide the "market regime". It works in the sense that the bot runs and nothing here can crash it: every fetch is wrapped so that a failure becomes "no data" and the bot carries on. That same design is the main weakness. The bot cannot tell "the request failed" from "there is nothing to report", so when a provider is down it keeps going on old or empty data and tells nobody.

The three things that matter most:

1. If FMP quotes stop arriving, the bot's own stop-loss check keeps running on the last price it had and reports the positions as "checked". No alert is sent (F-06-01).
2. The price used to size a buy and set its stop is the one captured when the stock was analysed, which is 17 to 65 or more hours before the order is sent, and it is never refreshed (F-06-02).
3. A TipRanks API key and token are written in the source code of this public repository, and the setting that is supposed to let `.env` override them is not connected to anything (F-06-03).

Beyond those: sector names come from two different word lists, so the 40% sector cap can under-count (F-06-04); a single failed index quote can flip the market regime, which changes the conviction bar and can trigger a full buy/sell cycle (F-06-05); several financial fields shown to the AI are mislabelled or probably empty (F-06-08); and the FMP and Finnhub keys are written to the server log on every request (F-06-07).

No droplet snapshot was available, so I could not check which provider plans the keys are on or how often these failures have actually happened. Those gaps are listed under "Could not verify".

## How it actually works

### The flow

1. **Universe.** `DataFeed.screen_universe` (`stock_agent/data_feed.py:271-344`) asks FMP's company screener for 10 names in each of 7 sectors, drops tickers that are not 1-5 plain letters, cuts the list to 50, then puts the `PRIORITY_SYMBOLS` (4 by default, `config.py:64-68`) at the front. On Sunday and Wednesday the scheduler also appends every TipRanks name with Smart Score 8 or more that is not already present (`scheduler.py:388-400`, `504-517`).
2. **Fundamentals.** For each symbol, `get_company_fundamentals` (`data_feed.py:107-227`) makes 9 FMP calls and 1 Finnhub call and builds one `CompanyData` record. Symbols are processed one after another (`scheduler.py:840-851`).
3. **Enrichment.** For the top 15-30 symbols, `_deep_analyze` (`scheduler.py:887-902`) builds three Finnhub lookups at once: news (`news_feed.py`), news sentiment (`sentiment.py`), analyst ratings plus insider trades (`analyst_trends.py`). After the thesis comes back, TipRanks, sentiment and analyst data each adjust the conviction number (`scheduler.py:950-962`).
4. **Prices while holding.** Every 30 minutes in market hours `get_batch_quotes` (`data_feed.py:243-262`) fetches one FMP quote per held symbol; `Portfolio.update_prices` writes it into the ledger; `RiskManager.check_position_health` compares it with the stop (`scheduler.py:1041-1053`).
5. **Macro.** `MacroMonitor.check_conditions` (`macro_monitor.py:173-262`) fetches six FMP quotes (`^GSPC`, `QQQ`, `^VIX`, `USO`, `TLT`, `UUP`) and 90 days of S&P history, scores them, and stores a regime. It runs at 06:30 ET and again after every 30-minute monitoring cycle (`scheduler.py:1600`).
6. **Options.** `OptionsDataFeed` (`options_data.py`) lists contracts from Alpaca's trading API and quotes from Alpaca's market-data API.

### Endpoint inventory (question 1)

"Fundamentals fetch" below means one call to `get_company_fundamentals`. It happens per universe symbol on Sunday, Wednesday and in any event-triggered scan; for 30 symbols in the 08:00 pre-scan; for up to 30 symbols in the 09:45 options scan (only to read the price); once per buy signal at execution (only to read the sector); and for held positions on re-analysis.

| Provider | Path | Code | Used for | When it fails |
| --- | --- | --- | --- | --- |
| FMP | `/company-screener` | `data_feed.py:294-302` | Universe, 7 calls per screen | Sector silently contributes nothing; priority symbols are still returned (F-06-09) |
| FMP | `/profile` | `data_feed.py:111` | Name, sector, market cap, and the price later used to size buys | Company dropped, WARNING log |
| FMP | `/key-metrics`, `/ratios` | `data_feed.py:112-113` | Valuation and margin fields | Fields left empty, no log beyond the request error |
| FMP | `/analyst-estimates` | `data_feed.py:114` | Nothing: the result is never read | n/a |
| FMP | `/discounted-cash-flow` | `data_feed.py:115` | DCF value | Field empty |
| FMP | `/stock-peers`, `/revenue-product-segmentation` | `data_feed.py:116-117` | Stored on the record, never shown to any prompt | Field empty |
| FMP | `/income-statement-growth` | `data_feed.py:139` | Revenue and earnings growth | Fields empty |
| FMP | `/price-target-summary` | `data_feed.py:148` | Analyst target price | Field empty |
| FMP | `/quote` | `data_feed.py:231`, `249`; `macro_monitor.py:137` | Stop-loss monitoring price, sell P&L price, macro inputs | Position keeps its old price (F-06-01); macro value stays 0 (F-06-05) |
| FMP | `/historical-price-eod/light` | `macro_monitor.py:153` | 90-day S&P high for drawdown | Drawdown stays 0 |
| FMP | `/historical-price-eod/full` | `options_portfolio.py:317` (direct call); `data_feed.py:425` (not reachable, see F-06-17) | Underlying close at option expiry | Returns no price (area 04) |
| FMP | `/news/stock` | `data_feed.py:348` | Dead code | n/a |
| Finnhub | `/calendar/earnings` | `data_feed.py:181-184`, `379-382` | Next earnings date; "did it just report" check at 07:00 ET | No date; treated as "no earnings" |
| Finnhub | `/company-news` | `news_feed.py:41` | Headlines for the thesis prompt | Empty list, WARNING log |
| Finnhub | `/news-sentiment` | `sentiment.py:34` | Prompt text and a conviction nudge of +1 or -1 | Nothing, DEBUG log only |
| Finnhub | `/stock/recommendation`, `/stock/insider-transactions` | `analyst_trends.py:130-131` | Prompt text and a conviction nudge of -1 to +2 | Nothing, DEBUG log only |
| TipRanks | `api.tipranks.com/api/stocks/screener` | `tipranks_client.py:54-59` | Extra universe names; conviction nudge of 0 to +2 | Empty list, WARNING log |
| Alpaca trading | `/v2/options/contracts` | `options_data.py:118-127` | Option chain | Loop stops; partial or empty list returned |
| Alpaca data | `/v1beta1/options/snapshots` (`feed=indicative`) | `options_data.py:239-242` | Option quotes and greeks; every 30 min for open option positions | That batch of 50 is skipped, ERROR log |
| Alpaca data | `/v2/stocks/{symbol}/snapshot` | `options_data.py:186-188` | Underlying price for covered calls when FMP gave none | `None`; caller skips the trade |
| Alpaca data | generic helper | `data_feed.py:90-103` | Dead code, never called | n/a |
| SEC | `data.sec.gov/submissions/CIK…json` | `data_feed.py:360-363` | Dead code, never called | n/a |

### Which price is used for what (question 3)

| Decision | Price | Source | How old it can be |
| --- | --- | --- | --- |
| Buy quantity, stop price, recorded entry price | `signal.entry_price` | FMP `/profile` `price` at analysis time (`scheduler.py:658`, `775`, `990`) | Previous close: about 17.5 h for weekday scans, 65.5 h for the Sunday run, longer over a holiday (F-06-02) |
| In-process stop-loss check | `pos.current_price` | FMP `/quote` `price`, refreshed every 30 min in market hours | 30 min plus any provider delay when healthy; unlimited when quotes fail (F-06-01) |
| Portfolio value (the base for sizing and caps) | cash + sum of `current_price × shares` | Last FMP quote or last Alpaca sync | Same as above |
| Recorded sell price and P&L | FMP `/quote` at the moment of selling, else `pos.current_price` (`scheduler.py:1320-1321`) | Not the fill | n/a |
| Option strike selection | FMP `/profile` `price` fetched during the options scan; Alpaca stock snapshot only as fallback (`scheduler.py:1431-1441`, `options_engine.py:155-166`) | Minutes, plus provider delay | |
| Option valuation | Alpaca "indicative" feed mid, else last, else yesterday's contract close (`options_data.py:211`, `362-376`) | See F-06-16 | |

Outside market hours the monitoring loop does not run. The quote has no timestamp kept (`data_feed.py:252-258` drops it), so a halted symbol or a holiday looks the same as a live price.

### Missing or zero quote for a held position (question 4)

- **Missing** (request failed or FMP returned an empty list): the position is skipped and keeps its last known price and its old `last_updated` (`portfolio.py:109-110`).
- **Zero**: the position is valued at zero, shows a 100% loss, and the stop-loss check skips it (`risk_manager.py:212-213`).
- **Null**: monitoring aborts for every position (F-06-12).

### Error handling by provider (question 2)

| Provider | HTTP error / timeout | Rate limit (429) | Empty list | Error object with status 200 |
| --- | --- | --- | --- | --- |
| FMP (`data_feed.py:36-51`) | Caught, ERROR log, returns `None` | Same as any error; no retry, and the 0.15 s courtesy sleep is skipped on the error path | Treated the same as `None` by every caller | Ignored by list-typed callers; on `/profile` it produces a record with price 0 and sector "Unknown" (F-06-08) |
| FMP (`macro_monitor.py:132-169`) | Caught, WARNING log | Same | Value stays 0 | Value stays 0 |
| Finnhub (`data_feed.py:55-70`) | Caught, ERROR log, `None` | Same | Same as `None` | `.get("earningsCalendar", [])` gives `[]` |
| Finnhub enrichment (3 modules) | Caught; WARNING for news, DEBUG for the other two | Same | Same | Dropped |
| TipRanks | Caught, WARNING, `[]` | Same | `[]` | "unrecognised payload shape" WARNING, `[]` |
| Alpaca options data | Caught, ERROR with status and body | Same | Empty | `.get(..., [])` gives empty |

In no case can a caller tell "no data" from "the request failed". The only helper whose contract says so is `fetch_stock_price` (`options_data.py:181-182`).

### Retries, caching, budgets, blocking (questions 5 and 6)

- No retry, backoff or cache exists in any data module (searched for `retry`, `backoff`, `cache`; the only hits are Discord's 429 handling and one local dict in `options_portfolio.py`).
- Pacing is a fixed sleep after each success: 0.15 s for FMP under a semaphore of 5, 1.1 s for Finnhub under a semaphore of 3 (`data_feed.py:21-23`, `47`, `66`). The three enrichment modules and the macro monitor do not use these and have no pacing.
- The only synchronous HTTP call is TipRanks (`requests.get`, `tipranks_client.py:54`), called directly from an `async def` (`scheduler.py:825-838`). All other calls use `httpx.AsyncClient`.
- Timeouts: 30 s (`data_feed.py:27`, `options_data.py:52`), 20 s (`macro_monitor.py:123`), 15 s (enrichment modules, TipRanks).

Estimated request counts (derived from the code; U = universe size, 54 without TipRanks additions and up to 154 with them; N = analysis depth, 20 by default; P = held positions):

| Run | FMP | Finnhub | Other |
| --- | --- | --- | --- |
| Sunday or Wednesday analysis | 7 + 9U, so about 490 to 1,390 | U + 4N, so about 134 to 234 | 1 TipRanks |
| Ordinary weekday | about 650 + 15P, so roughly 800 with 10 positions and 950 with 20 | about 90 + P | Alpaca option calls |
| Each buy signal executed | 9 | 1 | |

During a fundamentals sweep the rate is about 220 FMP calls a minute (measured with a fake client and 0.15 s simulated latency: 2.45 s per symbol).

### Keys in requests (question 8)

| Provider | Where the key goes | Code |
| --- | --- | --- |
| FMP | URL query `apikey=` | `data_feed.py:40`, `macro_monitor.py:138`, `155`, `options_portfolio.py:319` |
| Finnhub | URL query `token=` | `data_feed.py:59`, `news_feed.py:37`, `sentiment.py:31`, `analyst_trends.py:34` |
| Alpaca | Request headers | `options_data.py:60-65`, `data_feed.py:93-96` |
| TipRanks | Request headers, values hard-coded | `tipranks_client.py:25-31` |

### Where reality differs from the documentation

- `README.md` does not mention FMP at all, although it is the largest data dependency. It lists "news/social sentiment" (only news sentiment is fetched), "IV, skew, flow" for options (only chain, quotes and greeks are fetched), and "Perplexity + Anthropic" (the code calls Perplexity and OpenAI).
- `CLAUDE.md` says `data_feed.py` covers "FMP, Finnhub, SEC, Alpaca data". The SEC and Alpaca helpers there are never called.
- `tipranks_client.py:17` says "env vars still win via config". They do not (F-06-03).
- `tipranks_client.py:340-345` documents penalties for Smart Scores of 7 and below. Those rows cannot be reached (F-06-10).
- `data_feed.py:66` says "60/min → ~1s between calls". The semaphore allows 3 at once, and most Finnhub traffic bypasses it (F-06-11).
- `macro_monitor.py:86` names a field `spx_52w_high`; it holds a 90-day high (`macro_monitor.py:232-239`).
- `config.py:214` reads `STOCK_LOG_LEVEL`; nothing uses it (`__main__.py:9-12` hard-codes INFO).

## Findings

### F-06-01: When the quote feed fails, the bot's own stop-loss check runs on old prices and reports success
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/data_feed.py:36-51`, `243-262`; `stock_agent/portfolio.py:107-118`; `stock_agent/risk_manager.py:205-225`; `stock_agent/scheduler.py:1041-1053`, `1098`
- What happens: If an FMP quote request fails for any reason, that symbol is left out of the result. The position keeps the price it had, and the stop-loss check compares that old price with the stop. Nothing records that the price is old and no alert goes out.
- Evidence:
  ```python
  # data_feed.py:49-51
  except Exception as e:
      logger.error("FMP request failed for %s: %s", path, e)
      return None
  # data_feed.py:249-250 — a failed call and an empty answer both skip the symbol
  data = await self._fmp_get("/quote", {"symbol": sym})
  if isinstance(data, list) and data:
  # portfolio.py:109-110
  for pos in self.state.positions:
      if pos.symbol in prices:
  ```
  Call path: `_tick` → `_run_market_monitoring` → `get_batch_quotes` → `update_prices` → `check_position_health` (`scheduler.py:1043-1047`). The cycle always ends with `"Market monitoring complete — %d positions checked"` (`scheduler.py:1098`). `Position.last_updated` is never read by the risk check. The only other refresh of `current_price` is `sync_from_alpaca`, which runs Sunday evening, Wednesday evening and in the 08:15 ET brief (`scheduler.py:371`, `491`, `1733`), never during the session.
- Why it matters: With FMP unavailable (expired key, lapsed plan, daily quota used up, outage) the price behind every in-process stop check is the 08:15 pre-market price at best. The broker-side stop order becomes the only protection. Positions imported from Alpaca get a local stop value but no broker order from this code (`portfolio.py:202`), and a failed stop placement is only a log warning (`executor.py:132`); area 03 covers how reliable the broker stops are.
- How verified: Read the path end to end. Ran `get_batch_quotes` with `_fmp_get` stubbed to return `None`: result `{}`. `update_prices({})` left both test positions' prices and `last_updated` unchanged, and `check_position_health` returned `[]`.
- Direction (not a fix): Treat an unpriced held position as an event. Track price age, alert when it exceeds a limit, and consider using the broker's own position prices as the monitoring source.
- Verification: Confirmed: traced `_tick` → `_run_market_monitoring` independently and re-ran `update_prices({})` then `check_position_health` on a made-up position (price unchanged, no alert, while a real 20% drop does alert); no broker sync or price-age check exists on the monitoring path, so the separately placed broker stop order is the only protection during an FMP failure.

### F-06-02: Buy size and stop price are computed from the analysis-time price, which is never refreshed
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:658`, `775`, `990`, `1201-1208`, `1252`, `1259`, `1280-1300`; `stock_agent/data_feed.py:199`
- What happens: A signal stores the FMP profile price seen during analysis. At execution the bot only fetches a new quote if that stored price is zero. Quantity, stop price, the cash check and the recorded entry all use the stored price; the order itself is a market order.
- Evidence:
  ```python
  # scheduler.py:990
  entry_price=company.price,
  # scheduler.py:1201-1203
  price = signal.entry_price or 0
  if price <= 0:
      quote = await self.data_feed.get_stock_quote(sym)
  ```
  Sunday signals are queued (`scheduler.py:437`) and sent at the first open-market tick on Monday (`scheduler.py:194-197`). Earnings-reactive signals are created at 07:00 ET for stocks that just beat estimates by more than 10% (`scheduler.py:755`), which is when an overnight gap is most likely.
- Why it matters: Worked example with a 100,000 portfolio and conviction 8 (7% target): stored price 100 gives 70 shares and a stop at 93.00. If the stock opens at 115, the position costs 8,050 (8.05%, not 7%), the stop is 19.1% below the real entry (the intended range is 7-12%), and the ledger records an entry of 100. If it opens at 90, the stop price of 93 is above the market and the in-process check sees a breach at the next cycle.
- How verified: Read the path. Ran `RiskManager.calculate_position_size(100000, 100.0, 8)` → 70 and `get_stop_loss_price(100.0, None)` → 93.0, then did the arithmetic above.
- Direction (not a fix): Take a fresh price immediately before sizing, refuse the trade if it has moved beyond a tolerance, and base the stop on the fill.
- Verification: Confirmed: `_execute_signals` uses `signal.entry_price` whenever it is above zero, with no fresh quote, age limit or tolerance check before sizing and stop calculation, and signals queued on Sunday evening or pre-market reach it at the next open; the worked numbers check out (the recorded entry is later corrected by the next Alpaca sync, the size and the broker stop price are not).

### F-06-03: A TipRanks API key and token are committed in this public repository, and the `.env` override is not connected
- Severity: Critical (exposed credential, per the playbook scale; it is a data-vendor credential and cannot place orders)
- Confidence: Confirmed
- Where: `stock_agent/tipranks_client.py:18-19` (module constants `TIPRANKS_API_KEY`, `TIPRANKS_API_TOKEN`); `stock_agent/config.py:194-195` (the same two values as environment defaults); in git history since 2026-04-23 (commits `8f50387`, `edcdb0e`)
- What happens: Both values are in source in two files. The request headers are built from the module constants (`tipranks_client.py:25-31`). Nothing in the code reads `Config.TIPRANKS_API_KEY` or `Config.TIPRANKS_API_TOKEN`, so setting them in `.env` has no effect on what is sent.
- Evidence: `tipranks_client.py:17` reads `# Credentials embedded directly (per integration spec) — env vars still win via config.` A search for `TIPRANKS_API` finds only the two definitions and the two header lines.
- Why it matters: Anyone can use the credential. Replacing it requires a code change, and deleting the lines does not remove them from history, so the current values have to be treated as compromised.
- How verified: Search across the repository. Set both environment variables to test strings, reloaded `config` and `tipranks_client`: `Config` picked them up, `_headers()` did not.
- Direction (not a fix): Rotate the credential at TipRanks, load it only from the environment with an empty default, and disable the feature when it is absent.
- Verification: Confirmed: non-empty string literals are assigned to `TIPRANKS_API_KEY` and `TIPRANKS_API_TOKEN` at `stock_agent/tipranks_client.py:18-19` and repeated as the environment defaults at `stock_agent/config.py:194-195`, `_headers()` reads only the module constants, the client is reached from `scheduler.py` through `_load_tipranks_data`, and git history shows the names introduced in the two cited commits (location and names checked only; the values were not used or tested).

### F-06-04: Sector names come from two different vocabularies, so the 40% sector cap under-counts; a failed lookup skips the cap
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/config.py:102-146`; `stock_agent/data_feed.py:196`; `stock_agent/portfolio.py:204`; `stock_agent/scheduler.py:1192-1199`, `1945-1963`; `stock_agent/risk_manager.py:77-98`
- What happens: Positions the bot buys carry FMP's sector name ("Financial Services", "Consumer Cyclical"). Positions imported from Alpaca, and positions back-filled at start-up, carry the name from `SECTOR_MAP` ("Financials", "Consumer Discretionary"). The cap adds up positions by exact name, so one real sector is split into two buckets. Separately, if the FMP lookup fails at execution time and the symbol is not in the map, the sector is "Unknown" and the cap is not applied.
- Evidence:
  ```python
  # portfolio.py:204
  sector=Config.SECTOR_MAP.get(sym, "Unknown"),
  # scheduler.py:1193-1199
  company_sector = "Unknown"
  try:
      company_data = await self.data_feed.get_company_fundamentals(sym)
      ...
  except Exception:
      pass
  # risk_manager.py:90
  if resolved_sector != "Unknown" and sector_pct + ... >= self.config.MAX_SECTOR_PCT:
  ```
  The map also disagrees with FMP on membership: for example it puts GOOGL and META under "Technology".
- Why it matters: A sector limit meant to stop concentration can be passed without any rule firing. The cluster cap (`config.py:71-98`) is a partial backstop, but only for the symbols listed there.
- How verified: Ran `RiskManager.can_open_position` on a made-up 100,000 portfolio with two holdings labelled "Financials" and two labelled "Financial Services" (56% in total): a further "Financial Services" buy was allowed. With one consistent label it was blocked ("at 56.0% >= limit 40%"). With the sector at 70%, the same buy was blocked when passed "Financial Services" and allowed when passed "Unknown". `sync_from_alpaca` on four made-up rows assigned "Financials", "Consumer Discretionary", "Technology" and "Unknown".
- How often the fallback map is used: on every position imported by `sync_from_alpaca`, on every start-up for positions marked "Unknown" (map first, FMP second), and inside the risk check for "Unknown" positions. The actual count needs the droplet's ledger.
- Direction (not a fix): One sector vocabulary, one place that assigns it, and a blocked trade rather than a skipped check when the sector is not known.
- Verification: Confirmed: reproduced independently with `can_open_position` on made-up holdings (three positions labelled "Financials" at 42% of the portfolio: a new buy passed as "Financial Services" or as "Unknown" was allowed, the same buy passed as "Financials" was blocked), and the cluster cap did not catch it either because it sat below its own 48% limit; the split only arises once a position has been imported from Alpaca or back-filled from the map, which needs the droplet ledger to count.

### F-06-05: A failed macro quote is scored as zero, which can flip the market regime, change the conviction bar and trigger a full trading cycle
- Severity: High
- Confidence: Confirmed (mechanism); how often quotes fail on the droplet is not known
- Where: `stock_agent/macro_monitor.py:132-146`, `179-229`, `264-354`; `stock_agent/scheduler.py:984`, `1600-1688`
- What happens: Each macro input defaults to 0 and is only filled in if its quote arrives. The scoring rules skip zeros, so a missing input removes its points. The result is saved as the regime with nothing marking it incomplete.
- Evidence:
  ```python
  # macro_monitor.py:186-187
  if spx:
      snapshot.spx_price = spx.get("price", 0)
  # scheduler.py:1626-1629
  if self._last_event_regime and regime_now != self._last_event_regime:
      events_detected.append(f"Regime changed: ...")
  ```
  A detected event re-analyses every position (which can sell), screens and analyses the universe, executes buys if the market is open, and re-runs the options scan (`scheduler.py:1663-1688`). The regime also sets the minimum conviction: 8 in CAUTIOUS, 6 in DIP_OPPORTUNITY and AGGRESSIVE_DEPLOY (`macro_monitor.py:335-349`). The Sunday run uses whatever the last snapshot of Friday said.
- Why it matters: A provider hiccup on one symbol can loosen or tighten the buy threshold and start an unplanned cycle that spends LLM budget and places orders. When the quote returns 30 minutes later the regime flips back, which is a second event.
- How verified: Ran `_compute_regime` on hand-built snapshots. All zeros: NORMAL, score 0. S&P 6700, VIX 14, TLT 84: CAUTIOUS, score 4, minimum conviction 8; the same with the S&P quote missing: NORMAL, score 2. S&P 6250, drawdown 5.5%, VIX 20, TLT 84: NORMAL, score -2; the same with the TLT quote missing: DIP_OPPORTUNITY, score -3, minimum conviction 6.
- Direction (not a fix): A snapshot with a missing input should not replace the previous regime or count as a regime change.
- Verification: Confirmed: re-ran `_compute_regime` with my own inputs (a full set scoring AGGRESSIVE_DEPLOY became NORMAL with the S&P quote missing) and re-checked the auditor's examples, and traced `check_conditions` being called every 30-minute monitoring pass through `_check_for_market_event`, where any regime difference starts the full re-analysis, buy and options cycle with only a 30-minute cooldown; how often a macro quote actually fails is still unknown.

### F-06-06: The TipRanks clause on the sell path refers to a lookup that does not exist, so every completed sell is announced as a failed sell
- Severity: Medium (the order and the ledger are correct; the alerts are wrong)
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1348-1350`, `1364-1370`; `stock_agent/tipranks_client.py:444-476`
- What happens: After the sell order is placed and the ledger updated, `_execute_sell` reads `self._tipranks_lookup`, which is never created. The error is caught by the surrounding handler, which sends "Sell failed for SYMBOL" to Telegram and Discord. The normal sell alert is never sent. This is the only place `format_tipranks_telegram_clause` is called.
- Evidence: `self._tipranks_lookup.get(position.symbol.upper())` at `scheduler.py:1349`; a repository search finds no assignment.
- Why it matters: The owner is told the opposite of what happened on every exit, including stop-loss exits.
- How verified: Built a scheduler with a scratch data directory and fake executor, Telegram and Discord objects, and called `_execute_sell`. Calls recorded: `place_sell`, then `send_error_alert("Sell failed for AAA: 'StockAgentScheduler' object has no attribute '_tipranks_lookup'")`, then `send_error`. The position was gone from the ledger and one trade was logged.
- Direction (not a fix): Belongs with the execution and alert work in areas 03 and 08.

### F-06-07: The FMP key and Finnhub token are written to the server log on every request
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/__main__.py:9-12`; key placement at `data_feed.py:40`, `59`, `macro_monitor.py:138`, `155`, `news_feed.py:37`, `sentiment.py:31`, `analyst_trends.py:34`, `options_portfolio.py:319`; error logging at `data_feed.py:50`, `69`, `macro_monitor.py:145`, `168`, `news_feed.py:45`
- What happens: Logging is set to INFO for everything. At that level the HTTP library logs one line per request containing the full URL, and both providers take their key in the URL. HTTP error messages also contain the full URL and are logged by the bot's own handlers.
- Evidence: Output of a local run with a mock transport and fake keys, using the same `basicConfig` call:
  ```
  httpx INFO HTTP Request: GET https://…/stable/quote?apikey=FAKE_KEY_123456&symbol=AAA "HTTP/1.1 200 OK"
  stock_agent.data_feed ERROR Finnhub request failed for /bad: Client error '401 Unauthorized' for url 'https://…/bad?token=FAKE_TOKEN_987654&symbol=AAA'
  ```
  The service writes to the systemd journal (`deploy/stock-agent.service`, no log redirection). `Config.LOG_LEVEL` (`config.py:214`) is never used.
- Why it matters: Roughly a thousand journal lines a day carry the keys in clear text. Anyone who can read the journal, or any copy of it, has them.
- How verified: The mock run above (httpx 0.28.1 locally; `requirements.txt` allows any version from 0.24, which is where this log line was introduced).
- Direction (not a fix): Send keys in headers where the provider allows it and quiet the HTTP library's logger.

### F-06-08: Several financial fields given to the screener and thesis prompts are mislabelled, and most valuation fields are probably empty
- Severity: High
- Confidence: Confirmed for the mislabels and discarded calls; Likely for the empty fields (depends on FMP's response field names, which I could not fetch)
- Where: `stock_agent/data_feed.py:109`, `139-155`, `164-170`, `200-219`, `238`, `256`; `stock_agent/macro_monitor.py:188`, `206`, `217`, `221`, `225`, `229`; `stock_agent/screener.py:66-81`; `stock_agent/analyst.py:73-90`
- What happens:
  - Confirmed from code alone: "Forward P/E" is filled from a price/earnings-to-growth ratio (`data_feed.py:201`). `revenue_ttm`, `net_income_ttm` and `fcf_ttm` are filled from per-share figures (`215-217`). `analyst_rating` is always `None` (`150`, `219`). The `/analyst-estimates` result is never read (`109`). Peers and revenue segments are stored but no prompt uses them. So 3 of the 9 FMP calls per symbol contribute nothing.
  - Likely: the requests go to FMP's current `/stable` paths, but the fields read are the names used by FMP's older API (`peRatio`, `pbRatio`, `enterpriseValueOverEBITDA`, `roic`, `priceEarningsRatio`, `debtEquityRatio`, `targetConsensus`, `peersList`, `changesPercentage`). The code itself shows the rename happened: `get_batch_quotes` reads `changesPercentage or changePercentage` (`256`) while `get_stock_quote` (`238`) and all six macro inputs read only the old name.
  - Growth is requested with `period=quarterly` and labelled "YoY"; it may be quarter-on-quarter.
  - If FMP answers `/profile` with an error object and status 200, the bot builds a company with price 0, market cap 0 and sector "Unknown" instead of dropping it (`125-127`, `195-199`).
- Why it matters: Either the AI is shown a growth-adjusted ratio under the name "Forward P/E", or it is asked to score "Valuation" and "Analyst Sentiment" (`screener.py:117-119`) with those fields missing. Both shape which stocks reach deep analysis. Missing fields are omitted from the prompt, so the model cannot tell missing from not applicable. The macro brief would show every day-change as +0.0%.
- How verified: Ran `get_company_fundamentals` with stubbed responses. With old-style field names the screener block showed `P/E: 30.0` and `Forward P/E: 1.8` (the 1.8 was supplied as the PEG value). With the field names I believe `/stable` uses, these were empty: `pe_ratio, forward_pe, pb_ratio, ps_ratio, ev_ebitda, roe, roic, debt_to_equity, analyst_target_price`, and peers was `[]`. With an error object on `/profile` the result was `name='AAA', sector='Unknown', market_cap=0.0, price=0.0`. With `changePercentage: -2.5`, the macro snapshot and `get_stock_quote` reported 0 and `get_batch_quotes` reported -2.5.
- Direction (not a fix): Capture one real response per endpoint, map fields against it with tests, and drop the calls nothing uses.
- Verification: Unclear: the code-level claims hold, but only one mislabelled field ("Forward P/E" filled from a PEG ratio) can reach a prompt (`revenue_ttm`, `net_income_ttm`, `fcf_ttm` and `analyst_rating` are never read by `screener.py` or `analyst.py`), and that mislabel and the "valuation fields are empty" claim cannot both be true at once, so which one the bot actually suffers from, and the High rating, depend on real FMP `/stable` response field names that nobody has captured. Suggested severity: Medium until one saved response per endpoint settles it.

### F-06-09: A failed universe screen is invisible, and the screen never covers all the sectors it asks for
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/data_feed.py:291`, `303-307`, `330`, `337-344`; `stock_agent/scheduler.py:379-385`, `499-501`, `634`, `741`, `1427`
- What happens: (a) Priority symbols are added after screening, so `screen_universe` returns four names even when every FMP call failed. The scheduler's "screening returned no symbols" warning can therefore never fire with the default settings. (b) The screen asks for 10 names in each of 7 sectors (70), then keeps the first 50, which are the first five sectors requested. Consumer Defensive and Communication Services are cut. (c) The daily pre-scan, the earnings scan and the options scan use only the first 30 entries: the priority names, Technology, Healthcare and part of Financial Services.
- Evidence: `per_sector = max(self.config.UNIVERSE_SIZE // len(target_sectors), 10)` and `truncated = filtered[: self.config.UNIVERSE_SIZE]`.
- Why it matters: An FMP outage quietly reduces the weekly run to four hand-picked names plus whatever TipRanks adds. In normal operation the bot's daily attention is on about three sectors, which is not what the code appears to intend.
- How verified: Ran `screen_universe` with `_fmp_get` returning `None`: `['ANET', 'BK', 'GS', 'JPM']`. Ran it with 10 distinct tickers per sector: 54 symbols, none from the last two sectors; the first 30 were 4 priority, 10 Technology, 10 Healthcare, 6 Financial Services.
- Direction (not a fix): Report screening failure separately from the priority list, and decide deliberately how sectors are sampled.

### F-06-10: TipRanks can only raise conviction, its nudge stacks with two others, and its discovered names skip the universe filters
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/tipranks_client.py:54-59`, `290-309`, `334-376`; `stock_agent/scheduler.py:388-396`, `825-838`, `950-962`
- What happens:
  - The lookup only contains names scoring at least `TIPRANKS_MIN_SMART_SCORE` (8), so the adjustment is always 0, +1 or +2. The documented -1 and -2 cases cannot occur.
  - The three data-driven adjustments are applied one after another with no combined limit.
  - Names found only through TipRanks are added without the $10B market-cap floor or the ticker-shape filter that the FMP screen applies. Up to 100 names can be added, which can triple the size of the weekly run.
  - The call is synchronous inside the async loop.
  - The client guesses among five response shapes and six or seven names per field, which shows the response format is not pinned down.
- Evidence: `if score is not None and score < int(min_score): continue` (`tipranks_client.py:305-306`); `candidate_symbols.extend(tr_new_symbols)` (`scheduler.py:396`).
- Why it matters: A thesis the model scored 5 becomes a 7 (the buy threshold) on a Smart Score of 10 alone. In the best case for all three inputs, a 4 becomes a 9 and a 5 becomes a 10 (a 10% position). The project decision says TipRanks is "never a sole buy trigger"; it can be the deciding one.
- How verified: Ran `adjust_conviction_with_tipranks` over bases 4-7 and scores 10, 9, 8, 7, 5. Built a lookup from rows scoring 10, 7, 3, none, 9, 9 with a dotted ticker and an 8-letter ticker: the 7 and 3 were dropped, the dotted and long tickers were kept. Ran the three adjusters in scheduler order: `{2: 7, 3: 8, 4: 9, 5: 10}`. Patched `requests.get` to sleep 2 s: another task on the same loop paused for 2.05 s. No other task runs alongside the scheduler today, so the blocking call only delays the run.
- Direction (not a fix): Decide the intended role of TipRanks (see questions), cap the total adjustment, and apply the same universe rules to every source.

### F-06-11: Finnhub enrichment sends all its requests at once, hides its failures, and reports a misleading count
- Severity: Medium
- Confidence: Confirmed for the burst, the log levels and the count; Likely for the rate-limit effect and for `/news-sentiment` needing a paid plan
- Where: `stock_agent/news_feed.py:40-49`, `68-80`; `stock_agent/sentiment.py:33-39`, `57-75`; `stock_agent/analyst_trends.py:30-42`, `128-152`; `stock_agent/scheduler.py:888-902`
- What happens: For N symbols the three builders start 4N requests simultaneously, each on its own new connection, with no pacing. Failures in two of the three modules are logged at DEBUG, which is not shown at the configured level. A failed news request is stored as an empty list and still counted.
- Evidence: `asyncio.gather(*tasks, ...)` in each builder; `logger.debug(...)` at `sentiment.py:38` and `analyst_trends.py:41`; `lookup[sym] = _dedupe_and_trim(result)` at `news_feed.py:79`.
- Why it matters: Finnhub's free tier is documented as 60 calls a minute. Requests beyond the limit fail, and which symbols lose their data is arbitrary. Because the data feeds conviction adjustments, two equally good stocks can end up on different sides of the buy threshold depending on which requests got through. The summary line "Finnhub enrichment built: news=20 …" reads as healthy even when every news request failed.
- How verified: Replaced the fetch functions with counters. 15, 20 and 30 symbols gave 60, 80 and 120 simultaneous requests. With every fetch returning its failure value the lookup sizes were `[20, 0, 0]`.
- Direction (not a fix): Route all Finnhub traffic through one paced client, and distinguish "failed" from "nothing found" in the result.

### F-06-12: A zero or null price in a quote is written straight into the ledger
- Severity: High
- Confidence: Suspected (the code path is demonstrated; whether FMP ever sends a zero or null price is not)
- Where: `stock_agent/data_feed.py:252-258`; `stock_agent/portfolio.py:107-118`, `25-42`; `stock_agent/risk_manager.py:212-213`; `stock_agent/scheduler.py:1032-1101`
- What happens: The quote's `price` is copied without checking it. A zero values the position at nothing, and the stop check then skips it. A null makes `update_prices` raise part-way through, which aborts the whole monitoring cycle before any stop is checked. The null stays in memory; the next save writes it to `portfolio.json`; after a restart the file no longer loads.
- Evidence: `"price": q.get("price", 0)` (`data_feed.py:254`); `pos.current_price = quote.get("price", pos.current_price)` (`portfolio.py:112`), where the default only applies if the key is absent.
- Why it matters: In the null case every later valuation raises until a good quote arrives, and a restart in that window (any merge to `master` causes one) starts the bot from an empty ledger with the default 100,000 cash.
- How verified: Zero: position value 1,000 → 0, portfolio 1,500 → 500, no stop alert. Null: `update_prices` raised `TypeError`; `get_portfolio_value`, `check_position_health` and `can_open_position` then raised too. After two further saves, a fresh `Portfolio` logged "Failed to load portfolio state" and came up with 0 positions and cash 100000.
- Direction (not a fix): Validate a price before it enters the ledger; reject zero, null and non-numeric values and treat them as a missing quote.
- Verification: Unclear: every step of the consequence chain reproduces with made-up inputs (zero hides the position from the stop check; null raises in `update_prices`, aborts the monitoring pass with only a log line, and is written to `portfolio.json`), with the correction that the `.bak` copy still loads after the first bad save and the empty-ledger restart needs two saves in the bad state, but nothing shows that FMP `/quote` ever returns a zero or null `price` for a held symbol. Suggested severity: Medium until a real zero or null quote is evidenced.

### F-06-13: No retries, no cache and no request budget; whole bundles are re-fetched for one field; slow responses stall the single loop
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/data_feed.py:27`, `36-70`; `stock_agent/scheduler.py:840-851`, `1195`, `1431`, `1959`; `stock_agent/macro_monitor.py:123`
- What happens: One failed request means that data is gone for the cycle. The 10-request fundamentals bundle is fetched again to read only the sector at execution (`scheduler.py:1195`), only the price for up to 30 symbols in the options scan (`1431`), and only the sector at start-up (`1959`). Requests for held positions' quotes run one at a time with a 30-second timeout each.
- Why it matters: About 650 to 950 FMP calls on an ordinary weekday, most of them repeats. If FMP hangs rather than failing fast, 20 positions take up to 10 minutes before the stop check runs (on stale prices), and a 50-symbol fundamentals sweep takes far longer. The scheduler is one sequential task, so nothing else happens meanwhile. Timeout errors also log with an empty message (`str(httpx.ReadTimeout(""))` is `''`).
- How verified: Searched for retry and cache code; counted calls with a stub (10 per symbol); measured pacing with a fake client.
- Direction (not a fix): A small per-cycle cache, one bounded retry for transient errors, and an overall deadline for the monitoring fetch.

### F-06-14: The earnings check treats "scheduled but not yet reported" as "reported"
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/data_feed.py:386-418`; `stock_agent/scheduler.py:704-729`, `746-756`
- What happens: For a calendar entry with no actual figures, `get_recent_earnings` returns a non-empty record marked `pending`. For held positions the scheduler only tests whether the record is non-empty, announces "Earnings detected", and re-analyses the position, which can end in a sell. Because the window is today minus two days, the same event triggers again on the following mornings. Separately, the surprise percentage divides by the estimate, so 0.05 against 0.01 is a 400% "blowout". A failed request returns the same as "no earnings".
- Why it matters: A position can be re-judged, and sold, on "post-earnings data" before the company has reported, and up to three paid analyses are spent per event.
- How verified: Stubbed Finnhub with an entry dated today with no actual EPS: result `{'pending': True, 'beat': False, 'surprise_pct': 0}`, which is truthy. Ran the 0.05 versus 0.01 case: 400%.
- Direction (not a fix): Return distinct states for "reported", "pending" and "unknown", and remember which events were already handled.

### F-06-15: Insider "buying" counts any increase in shares, including stock awards and option exercises
- Severity: Medium
- Confidence: Likely (the code ignores the transaction type; I could not fetch live rows to show the mix)
- Where: `stock_agent/analyst_trends.py:101-109`, `219-225`
- What happens: Every positive share change is added to "bought" and every negative one to "sold". The transaction code is stored for display (`:115`) but not used. Any "buying" with no selling adds a conviction point; any selling with no buying removes one.
- Why it matters: Routine compensation events move conviction, and the prompt tells the model "29,000 shares bought" for what were grants.
- How verified: Fed two rows with codes `A` (award) and `M` (option exercise): `buys_shares=29000`, conviction 7 became 8.
- Direction (not a fix): Count only open-market purchase and sale codes.

### F-06-16: Option data is fetched from a hard-coded paper host and a hard-coded "indicative" feed, and partial results are returned silently
- Severity: Medium (the host matters only when the account goes live)
- Confidence: Confirmed for the code; the feed's quality is from Alpaca's documentation, not tested
- Where: `stock_agent/options_data.py:28`, `118`, `130-138`, `211`, `245-253`, `362-376`
- What happens: The contract list always comes from the paper trading host, regardless of `ALPACA_BASE_URL`. Quotes always request the free indicative feed even if the account has the official one. If a page of contracts or a batch of quotes fails, the loop stops or skips and returns what it has. A contract with no quote is given yesterday's close as its price.
- Why it matters: With live keys the paper host would refuse the request and the options scan would quietly find nothing. Before that, option prices used to pick trades are indicative or a day old.
- How verified: Read the file in full; searched for uses of `ALPACA_BASE_URL` (only `executor.py:32`).
- Direction (not a fix): Take hosts and feed from configuration and make incomplete data visible to the caller.

### F-06-17: Dead data code and documentation that no longer matches
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/data_feed.py:74-103`, `264-269`, `346-363`; `stock_agent/options_portfolio.py:298-306`; `stock_agent/config.py:194-195`, `214`; `stock_agent/models.py:6-21`
- What happens: Never called: `_sec_get`, `get_sec_filings`, `_alpaca_data_get`, `get_company_news`, `get_analyst_recommendations`, `get_earnings_calendar`. `get_historical_prices` is reached only through a lookup of attributes that `OptionsDataFeed` does not have, so the direct FMP call in `options_portfolio.py:313-325` always runs instead. The `TipRanksStock` model and three `Config` values are unused.
- On the SEC lead: the helper does send a `User-Agent` (`data_feed.py:78`), but the contact in it is a placeholder address at example.com, which would not meet the SEC's requirement. It is harmless today because the helper is never called.
- Why it matters: It suggests data sources the bot does not use and hides the ones it does.
- How verified: Repository search for each name.
- Direction (not a fix): Remove or wire up during the revamp.

### F-06-18: Class-share tickers are not translated between providers
- Severity: Low
- Confidence: Likely
- Where: `stock_agent/data_feed.py:249-258`; `stock_agent/portfolio.py:131-134`; `stock_agent/scheduler.py:390-396`
- What happens: Alpaca writes class shares with a dot. The FMP screen filters such tickers out, but positions imported from Alpaca and TipRanks-discovered names can carry them, and the quote request sends the ticker unchanged. FMP's own convention appears to be a hyphen (the exclusion list at `data_feed.py:313-317` contains both forms).
- Why it matters: Such a position would get no monitoring price from FMP (see F-06-01).
- How verified: Read the code; the import filter accepts dotted tickers and the TipRanks lookup kept one in the test for F-06-10. Not tested against FMP.
- Direction (not a fix): One canonical ticker form with a per-provider translation.

## Checked and fine

- Apart from TipRanks, every data call is asynchronous (`data_feed.py:44`, `63`, `79`; `macro_monitor.py:136`, `152`; `news_feed.py:40-41`; `sentiment.py:33-34`; `analyst_trends.py:36-37`; `options_data.py:127`, `188`, `242`).
- Every outbound call has a timeout (`data_feed.py:27`, `macro_monitor.py:123`, `news_feed.py:25`, `sentiment.py:24`, `analyst_trends.py:25`, `tipranks_client.py:22`, `options_data.py:52`).
- No data-layer exception can stop the main loop; each helper catches its own errors.
- Alpaca credentials travel in headers (`options_data.py:60-65`), so they are not in the request log line.
- FMP request paths use the current `/stable` base (`data_feed.py:12`, `macro_monitor.py:27`). The lead about API version is harmless for paths; the issue is response field names (F-06-08).
- Data-layer error text containing a key does not reach Telegram or Discord; those helpers log locally only.
- `scripts/vps_snapshot.sh:237` redacts `apikey=` and `token=` values, so an uploaded snapshot should not carry these keys.
- `fetch_stock_price` returns `None` on failure and the covered-call path skips the trade (`options_data.py:178-206`, `options_engine.py:173-178`).
- A TipRanks outage does not block the cycle (`tipranks_client.py:53-64`, `scheduler.py:825-838`).
- `_safe_float` returns `None` for missing, non-numeric and NaN values (`data_feed.py:435-444`); run with sample inputs.
- News de-duplication and the cap of 8 headlines work (`news_feed.py:52-65`); run with 15 items.
- The sentiment adjustment handles both fraction and percent inputs and needs a 30-point spread (`sentiment.py:115-144`); run with five cases.
- Earnings surprise guards against a zero estimate (`data_feed.py:393-395`).
- Option symbols are converted from hyphen to dot for Alpaca (`options_data.py:96`, `185`), and option quote batches of 50 stay under Alpaca's limit (`options_data.py:231`).
- The FMP screen removes tickers that are not 1-5 letters (`data_feed.py:318-324`).
- The semaphores created before the event loop starts (`data_feed.py:21-23`) are safe on Python 3.12.

## Could not verify

- **Provider plans.** Which FMP plan the key is on (rate limit, real-time or delayed quotes, whether index symbols and the screener are included); whether the Finnhub key is free or paid (`/news-sentiment`); which Alpaca market-data subscription is active. Needed: the plan names from Josh.
- **FMP response field names.** Needed: one saved response for each of `/profile`, `/key-metrics`, `/ratios`, `/quote`, `/price-target-summary`, `/stock-peers`, `/income-statement-growth`, or permission to make one read-only call per endpoint. This settles the "Likely" half of F-06-08, whether `period=quarterly` is accepted, and whether the growth figures are year-on-year.
- **How often these failures happen.** Needed: the droplet snapshot's journal, to count `FMP request failed`, `FMP quote failed`, HTTP 429 lines, `Finnhub enrichment built: news=… sentiment=… analyst=…`, `TipRanks fetch: N rows` and regime changes.
- **Whether FMP ever returns a zero or null price** (F-06-12). Needed: the same journal, or FMP's answer for a halted or delisted symbol.
- **How many positions carry "Unknown" or map-style sector names** (F-06-04). Needed: `portfolio.json` from the snapshot.
- **TipRanks contract.** Whether the endpoint honours `minSmartScore` and `limit`, and whether this API is an agreed product for the account. Needed: Josh's TipRanks agreement or a sample response.
- **Finnhub earnings call with an empty `to` date** (`data_feed.py:183`) and the order of its results. Needed: one sample response.
- **Alpaca's handling of a sell stop above the market** (F-06-02). Needed: area 03's order-history review.
- **The httpx version on the droplet.** Needed: `pip freeze` from the snapshot.

## Questions for Josh

1. Which FMP plan is the key on, and is the Finnhub key free or paid?
2. Is the TipRanks key and token in the source a personal paid credential, and can you rotate it now?
3. When prices cannot be fetched during market hours, should the bot alert you, stop opening positions, or both?
4. Would you accept Alpaca as the price source for held positions, keeping FMP for company financials only?
5. Should a TipRanks score be able to lift a thesis the model rated below 7 over the buy line, and should a low score ever count against a stock?
6. Should names found only through TipRanks have to meet the $10B minimum market cap?
7. Is it intended that the daily scans look mainly at Technology, Healthcare and Financials, and that Energy, Materials, Utilities and Real Estate are never screened?
8. Should a change of market regime be allowed to start a full buy/sell cycle on its own?

## Handoffs

- 01: The event-triggered scan and the 09:45 options scan run inside the monitoring task, so stop-loss monitoring pauses for their duration (`scheduler.py:1663-1688`).
- 01: `_is_market_open` has no holiday calendar (`scheduler.py:297-302`); `get_effective_universe_size()` is only used for display (`scheduler.py:1867`), so the regime's "wider scan" never happens.
- 02: `Thesis` has no `beta` field and `data_feed` never reads FMP's `beta`, so the stop is always the 7% floor (`models.py:58-72`, `risk_manager.py:157-164`).
- 02: The macro regime lowers minimum conviction to 6, below the documented 7 (`macro_monitor.py:337`, `342`); the stop check recomputes from entry price and ignores `pos.stop_loss` (`risk_manager.py:216`).
- 03: `_execute_sell` raises after the order is placed (F-06-06, `scheduler.py:1348-1350`).
- 03: Buys are recorded at the stored signal price and sells at an FMP quote, not at fills (`scheduler.py:1280-1300`, `1320-1328`).
- 04: `options_executor.py:27` hard-codes the paper host for option orders.
- 04: The covered-call out-of-the-money check uses the FMP profile price first (`options_engine.py:155-163`); a day-old close can stand in for a quote before a market order (`options_data.py:362-376`, `scheduler.py:1472`, `1482`).
- 04: `options_portfolio.py:315` reads `FMP_API_KEY` from the environment directly rather than through `Config`.
- 05: News headlines are inserted into the thesis prompt verbatim (`news_feed.py:83-100`); conviction stacking is described in F-06-10.
- 07: A null price can make `portfolio.json` unloadable after a restart (F-06-12); `macro_history.json` keeps 90 entries, about six trading days at up to 15 checks a day (`macro_monitor.py:407-418`).
- 08: No alert exists for data-feed failures; the Telegram bot token is part of the request URL (`telegram_alerts.py:42`) and is logged the same way as in F-06-07.
- 09: TipRanks credentials in source and history (F-06-03); Discord guild and channel IDs as defaults in `config.py:22-40`; `.env.example` describes the retired Polymarket bot and lists none of the stock agent's keys; no start-up check that required keys are set.
- 10: `aiohttp` and `python-dotenv` are in `requirements.txt` but never imported; `TipRanksStock` is unused.
