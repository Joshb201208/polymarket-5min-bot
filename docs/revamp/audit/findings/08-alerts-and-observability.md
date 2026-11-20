# 08. Alerts and observability

Audited commit: 941f2ca    Date: 2026-11-20

No droplet snapshot was available. Everything below is what the code allows;
what actually happened on the server is listed under "Could not verify".

## Plain-language summary

The bot talks to Josh through Telegram (a thin channel: start, stop, buys, the
daily summary, the weekly report, a few errors) and Discord (seventeen
channels, far more detail). Everything else goes to the server's system log,
which nobody reads. Sending an alert can never crash trading, which is good,
but the alerts do not do the job Josh needs: telling him what happened and
letting him notice when something is wrong. Three things matter most.
First, every time the bot sells a position and the broker accepts the order,
Josh is sent "Sell failed" and the real sell alert is never sent, because of a
one-line bug that runs after the order is placed. Second, nothing watches the
bot: there is no heartbeat and no outside check, so a dead, stuck, or
mis-configured bot is simply silent, and a normal weekend already contains
about 50 hours of silence. The earlier unnoticed outage can happen again
unchanged. Third, the problems Josh most needs to hear about reach only the
system log: a rejected order, a stop-loss order that was not placed (the buy
alert still shows a stop price), the 30-minute stop-loss check failing, and a
failed broker sync. Separately, the Telegram bot token and two data-provider
keys are written to the system log in plain text on every request. Several
routine messages also say things that are not true: the weekly report always
says "no new signals", and the morning brief lists yesterday's scans as
completed from the calendar, not from what ran.

## How it actually works

### Where output goes

- Logging is configured once, in `stock_agent/__main__.py:9-12`, at a fixed
  INFO level, to standard error. The systemd unit
  (`deploy/stock-agent.service`) sets no output target, so it lands in the
  systemd journal. The bot writes no log files of its own. There are no
  `print` calls. Usage across the package: 20 debug, 90 info, 41 warning,
  64 error, 12 exception (with traceback), 0 critical.
- The HTTP library (`httpx`) logs one INFO line per request, including the
  full URL, through the same handler (see F-08-03).
- Telegram: `TelegramReporter` (`telegram_alerts.py`). One POST per message
  chunk with the bot token in the URL (line 42), 30-second timeout, one
  attempt. On an HTTP error it resends once without formatting (lines 53-61).
  On any other failure it logs and the message is lost (lines 62-63).
- Discord: `DiscordReporter` (`discord_alerts.py`). Bot token in a header.
  `send_embed` (lines 39-82) makes up to four attempts on network errors,
  sleeps for whatever Discord's `retry_after` says on a 429, makes no retry
  on any other HTTP error, and returns `False`. No caller checks the return
  value (grep for an assignment or `if` on any send: none).
- Every send is awaited in line by the scheduler, between trading steps.
  Nothing is queued.

### Message inventory (question 1)

Telegram:

| Message | Trigger | Where | Kind |
| --- | --- | --- | --- |
| "Stock Agent started in MODE mode" | every process start | `scheduler.py:114-116` | liveness |
| "Stock Agent shutting down" | loop exits after SIGTERM/SIGINT | `scheduler.py:1977` | liveness |
| "STOCK AGENT ERROR" + exception text | exception escaping `_tick` | `scheduler.py:131-137` | act |
| "Weekly Analysis Starting" | Sunday 18:00-20:59 ET, each attempt | `scheduler.py:365` | liveness / noise |
| "Universe screening returned no symbols" | unreachable: priority symbols are always injected (`data_feed.py:337-344`) | `scheduler.py:381` | dead |
| "No fundamental data retrieved" | weekly run got nothing from FMP | `scheduler.py:409` | act |
| Weekly report | end of weekly analysis | `scheduler.py:447-453` | read |
| Error: "Weekly analysis failed" | | `scheduler.py:471` | act |
| Daily summary | weekdays 16:05-16:30 ET | `scheduler.py:1156` | read |
| Error: "Daily summary failed" | | `scheduler.py:1166` | act |
| "PDT limit reached" | day-trade rule blocks a batch | `scheduler.py:1181` | read |
| BUY alert | buy order accepted | `scheduler.py:1306` | read |
| SELL alert, TipRanks line | never sent today (F-08-01) | `scheduler.py:1352-1357` | dead |
| Error: "Sell failed for X" | every accepted sell today, plus real exceptions | `scheduler.py:1367` | act, but false |

Discord, by channel key in `config.py:23-41`:

| Channel key | What is sent | Where |
| --- | --- | --- |
| `announcements` | online / offline; regime change; "Market Event, Immediate Scan Triggered"; morning brief (5 embeds, weekdays 08:15-08:45 ET) | `scheduler.py:117, 1978, 340, 1646, 1930` |
| `macro` | macro brief, weekdays 06:30-07:30 ET | `scheduler.py:334` |
| `system_logs` | start/complete lines for weekly, midweek, pre-market, earnings, options scans; "Trade blocked for X: reason"; red error embeds from eight handlers | `scheduler.py:366, 410, 488, 553, 573, 675-680, 699, 713, 786, 796, 1217, 1380, 1512`; errors `136, 472, 561, 686, 804, 1167, 1368, 1521` |
| `screener` | top-25 table (weekly, midweek) | `scheduler.py:422, 532` |
| `thesis` | one embed per analysed symbol | `scheduler.py:969, 732` |
| `analyst` | the same thesis text again, long form | `scheduler.py:970` |
| `trades` | BUY/SELL embed; options position embed | `scheduler.py:1307, 1353, 1504` |
| `trade_history` | identical copy of the `trades` embed | `discord_alerts.py:201-205` |
| `what_bot_did` | plain-English trade note and daily note | `scheduler.py:1308, 1354, 1158` |
| `weekly_eli5` | plain-English weekly note | `scheduler.py:461` |
| `daily_pnl` | daily summary | `scheduler.py:1157` |
| `portfolio` | weekly report | `scheduler.py:454` |
| `risk` | PDT warning; "Options Expiry Alert" | `scheduler.py:1182, 1555` |
| `market_monitor` | material event on a held position | `scheduler.py:599, 1074` |
| `research`, `investing_101`, `live_positions` | nothing, ever | `config.py:26, 38, 40` |

Volume: a quiet weekday is about 10 Discord messages and 1 Telegram message.
A weekly analysis is about 45 Discord messages (two or more per analysed
symbol) and 2 Telegram messages.

For Josh to act on: the error messages, "Trade blocked", material events,
and the red "Could not fetch Alpaca account data" morning brief. Noise or
duplicates: `trade_history` (copy of `trades`), `analyst` (copy of
`thesis`), INFO start/complete lines, the plain-English notes (static text,
partly wrong, F-08-10), and the options expiry alert (repeats every 30
minutes and says nothing can be done, F-08-08).

### What tells Josh the bot is alive (question 2)

Nothing is designed for it. The routine messages are the only signal:

- Telegram: daily summary on weekdays at 16:05 ET; "Weekly Analysis
  Starting" on Sunday at 18:00 ET; trade alerts when there are trades.
- Discord: weekdays 06:30 macro brief, 07:00 earnings scan, 08:00 pre-market
  scan, 08:15 morning brief, 09:45 options scan, 16:05 daily summary; Sunday
  18:00 weekly.

Longest normal silence: Friday about 16:30 ET to Sunday 18:00 ET, roughly
50 hours on both channels. On weekdays Telegram is silent for 24 hours
between summaries, and both channels are silent from about 09:45 to 16:05
unless there is a trade.

| Situation | What Josh receives |
| --- | --- |
| Clean stop, tick finishes inside systemd's stop timeout | "shutting down", then "started" |
| Killed (out of memory, SIGKILL, reboot) | nothing; then an unexplained "started" |
| Crash before `run()` (import error, a bad number in `.env` parsed at `config.py:50-57`, `git fetch` failing in `ExecStartPre`) | nothing, forever; systemd retries every 30 s with no limit |
| Crash after the startup message | "started" on both channels every 30-40 s, with no limit |
| Alive, alert tokens missing | nothing; one WARNING per skipped message in the journal (`telegram_alerts.py:31-33`, `discord_alerts.py:41-43`) |
| Alive, broker or data keys dead | daily summary keeps arriving, built from the local ledger |
| Stuck (long scan, waiting out a Discord rate limit) | nothing; no watchdog |

### What happens to errors (question 6)

`stock_agent/` has 144 exception handlers; 105 are `except Exception`, none
are bare `except:`. Eight handlers send an alert. Four of those reach
Telegram.

| Job | Handler | Telegram | Discord | Journal |
| --- | --- | --- | --- | --- |
| Main loop tick | `scheduler.py:131` | yes | yes | traceback |
| Weekly analysis | `scheduler.py:468` | yes | yes | traceback |
| Daily summary | `scheduler.py:1163` | yes | yes | traceback |
| Sell | `scheduler.py:1364` | yes | yes | traceback |
| Midweek analysis | `scheduler.py:559` | no | yes | traceback |
| Pre-market scan | `scheduler.py:684` | no | yes | traceback |
| Earnings scan | `scheduler.py:802` | no | yes | traceback |
| Options scan | `scheduler.py:1519` | no | yes | traceback |
| Macro check | `scheduler.py:358` | no | no | traceback |
| Market monitoring (stop-loss check) | `scheduler.py:1100` | no | no | traceback |
| Morning brief | `scheduler.py:1934` | no | no | traceback |
| Broker sync | `scheduler.py:822` | no | no | traceback |
| Options monitoring | `scheduler.py:1573` | no | no | one line |
| Event-triggered scan (can place orders) | `scheduler.py:1695` | no | no | one line |

The 105 broad handlers, sorted (A = fine, B = hides a failure that should
stop the action, C = hides a failure that should alert):

| File | Handlers | Broad | A | B | C |
| --- | --- | --- | --- | --- | --- |
| `analyst.py` | 9 | 3 | 1 | 0 | 2 |
| `analyst_trends.py` | 4 | 1 | 1 | 0 | 0 |
| `config.py` | 1 | 0 | 0 | 0 | 0 |
| `data_feed.py` | 8 | 7 | 6 (2 in dead code) | 0 | 1 |
| `discord_alerts.py` | 4 | 2 | 0 | 0 | 2 |
| `executor.py` | 2 | 1 | 0 | 1 | 0 |
| `macro_monitor.py` | 5 | 5 | 3 | 2 | 0 |
| `news_feed.py` | 2 | 2 | 2 | 0 | 0 |
| `options_data.py` | 8 | 6 | 5 | 0 | 1 |
| `options_engine.py` | 1 | 1 | 1 | 0 | 0 |
| `options_executor.py` | 14 | 8 | 5 (all in dead code) | 2 | 1 |
| `options_portfolio.py` | 11 | 8 | 4 | 2 | 2 |
| `portfolio.py` | 7 | 6 | 3 | 2 | 1 |
| `research_notes.py` | 2 | 2 | 2 | 0 | 0 |
| `scheduler.py` | 49 | 47 | 33 | 3 | 11 |
| `screener.py` | 3 | 1 | 0 | 1 | 0 |
| `sentiment.py` | 4 | 1 | 1 | 0 | 0 |
| `telegram_alerts.py` | 3 | 2 | 0 | 0 | 2 |
| `tipranks_client.py` | 7 | 2 | 2 | 0 | 0 |
| Total | 144 | 105 | 69 | 13 | 23 |

"A" includes per-symbol research failures that are logged and skipped, and
`pass` blocks around a Discord send. The handlers around orders, stops, sync
and state saving are reported one by one in F-08-01, F-08-04, F-08-05 and
F-08-06. The `scheduler.py` C handlers are lines 358, 625, 735, 1027, 1094,
1100, 1364, 1509, 1573, 1695, 1934 (625, 735, 1027, 1094 are exit reviews of
a held position that are skipped with a log line only).

## Findings

### F-08-01: Every sell the broker accepts is announced as "Sell failed"; the real sell alert is never sent
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1316-1370` (bug at 1348-1350)
- What happens: After the sell order is placed and the ledger is updated,
  the code reads an attribute that does not exist. The error is caught by
  the function's catch-all, which tells Josh on Telegram and Discord that the
  sell failed. The proper sell alert (price, profit or loss, reason) is
  skipped.
- Evidence:
  ```python
  # scheduler.py:1345-1350
  self.portfolio.remove_position(position.symbol)
  self.portfolio.log_trade(trade)

  tr_clause = format_tipranks_telegram_clause(
      self._tipranks_lookup.get(position.symbol.upper())
  )
  ```
  `_tipranks_lookup` is never assigned anywhere in the repository (this is
  its only occurrence). Commit `7a8ac46` (2026-04-23) added the line; its
  message says "Cache lookup on self._tipranks_lookup", but no assignment
  exists. The handler at 1364-1370 sends
  `f"Sell failed for {position.symbol}: {e}"`. Every exit goes through this
  function: lines 615, 727, 1024, 1053 (stop-loss), 1088. Run with a fake
  broker and fake alert channels:
  ```
  sell orders sent to broker : [('XYZ', 10)]
  position still in ledger   : False
  trades logged              : [('SELL', 'XYZ', 10)]
  telegram.send_error_alert("Sell failed for XYZ: 'StockAgentScheduler' object has no attribute '_tipranks_lookup'")
  discord.send_error(..., context='Sell execution for XYZ')
  ```
  The other branch is no better: if the broker rejects the sell,
  `scheduler.py:1324-1326` logs and returns with no alert (F-08-04). So no
  bot-initiated sell can currently produce a correct alert.
- Why it matters: Sells are the exits. Josh is told an exit failed when it
  was sent, never sees the result, and learns to ignore "Sell failed", which
  is also the wording of a real failure. Acting on the false alarm by hand
  could mean a second sell on shares already sold. No money is lost by the
  bug itself; it is rated High because the owner's only window says the
  opposite of what happened on every exit.
- How verified: Read the path end to end; grep for the attribute; ran
  `_execute_sell` with fakes (no network).
- Direction (not a fix): Bookkeeping and notification after an accepted
  order must not be able to turn it into a "failed" message. Report the
  order outcome and the notification outcome separately.
- Verification: Confirmed: `_tipranks_lookup` appears once in the repository (the read at `scheduler.py:1349`) with no assignment and no `__getattr__`, and an independent run of `_execute_sell` with fakes placed the sell, updated the ledger, then sent only the two "Sell failed" messages; all five call sites are live from the tick.

### F-08-02: Nothing watches the bot, so a dead, stuck, or mis-configured bot is silent
- Severity: High
- Confidence: Confirmed (code and unit file); server history not verified
- Where: `stock_agent/scheduler.py:109-144`, `stock_agent/__main__.py:8-22`,
  `deploy/stock-agent.service`, `stock_agent/telegram_alerts.py:31-33`,
  `stock_agent/discord_alerts.py:41-43`
- What happens: There is no heartbeat message, no watchdog, and no check
  from outside the server. The only liveness signal is the absence of a
  routine message, and the schedule has gaps of up to about 50 hours.
  Starting without credentials is not an error: the bot runs, logs warnings,
  and sends nothing.
- Evidence: Grep for heartbeat, watchdog, healthcheck, `sd_notify` in
  `stock_agent/` and `deploy/` finds nothing. The unit has `Restart=always`
  and `RestartSec=30`, with no `WatchdogSec`, `OnFailure`, or start limit;
  systemd's default limit (5 starts in 10 s) cannot trip with a 30-second
  delay, so a crash loop runs for ever. The startup message is sent inside
  `run()` (`scheduler.py:112-121`), after import and construction, so a
  crash before that point sends nothing. With an empty token:
  ```python
  # telegram_alerts.py:31-33
  if not self.config.TELEGRAM_BOT_TOKEN or not self.config.TELEGRAM_CHAT_ID:
      logger.warning("Telegram not configured — skipping message")
      return
  ```
  The startup send ignores its result (`scheduler.py:112-121`). This has
  already happened once; commit `ec027ec` says: "the service ran with an
  empty environment ... every channel silently no-op'd." Nothing added since
  would catch a repeat. When the bot returns after an outage it sends only
  "started": not how long it was down, nor what it missed (a weekly run
  missed on Sunday is skipped until the next Sunday, `scheduler.py:248-260`).
- Why it matters: The bot can be down, or alive and useless, for days with
  open positions and nobody told. Where a stop order was not placed
  (F-08-04) the bot's own check is the only protection, and it is not
  running.
- How verified: Read the loop, the unit file, both alert modules; computed
  the gaps from the schedule windows at `scheduler.py:214-320`.
- Direction (not a fix): A check that lives outside the process and messages
  Josh when the bot stops checking in, plus a startup self-test that refuses
  to run quietly without working broker and alert credentials.
- Verification: Confirmed: no heartbeat, watchdog or outside check exists in `stock_agent/`, `deploy/` or the unit file, both alert senders return quietly when unconfigured, and the only liveness sign is the "started" message sent after construction; whether an outage has gone unnoticed on the droplet is not checked here.

### F-08-03: The Telegram bot token and the FMP and Finnhub keys are written to the system log on every request
- Severity: High
- Confidence: Confirmed locally (httpx 0.28.1); droplet version not verified
- Where: `stock_agent/__main__.py:9-12`, `stock_agent/telegram_alerts.py:42`,
  `stock_agent/data_feed.py:40-51, 59-70`, `stock_agent/macro_monitor.py:136-146`,
  `stock_agent/news_feed.py:37-46`, `stock_agent/options_portfolio.py:319`
- What happens: Root logging is set to INFO, and the HTTP library logs every
  request URL at INFO. Telegram carries its token in the URL path; FMP and
  Finnhub carry their keys in the query string. On an HTTP error the bot's
  own ERROR line repeats the URL, because the library's exception text
  contains it.
- Evidence: Ran the real `TelegramReporter`, `DataFeed` and `MacroMonitor`
  against a local mock transport with fake credentials, using the same
  `basicConfig` call as `__main__.py`:
  ```
  httpx INFO HTTP Request: POST https://api.telegram.org/bot1111111111:FAKE_TELEGRAM_TOKEN_FOR_AUDIT_ONLY/sendMessage "HTTP/1.1 200 OK"
  httpx INFO HTTP Request: GET https://financialmodelingprep.com/stable/quote?apikey=FAKE_FMP_KEY_FOR_AUDIT&symbol=AAPL "HTTP/1.1 200 OK"
  stock_agent.data_feed ERROR FMP request failed for /quote: Client error '429 Too Many Requests' for url 'https://financialmodelingprep.com/stable/quote?apikey=FAKE_FMP_KEY_FOR_AUDIT&symbol=AAPL'
  httpx INFO HTTP Request: GET https://finnhub.io/api/v1/calendar/earnings?token=FAKE_FINNHUB_KEY_FOR_AUDIT&symbol=AAPL "HTTP/1.1 429 Too Many Requests"
  ```
  `requirements.txt` pins `httpx>=0.24`. The snapshot script already scrubs
  exactly these shapes (`scripts/vps_snapshot.sh:237-238`).
- Why it matters: Anyone who can read the journal, or any log excerpt shared
  for debugging, holds the token that can send messages as the bot into
  Josh's chat (fake alerts) and the two data keys. Changing a key later does
  not clean old log lines. The Alpaca, Perplexity, OpenAI, Discord and
  TipRanks credentials travel in headers and are not logged this way, which
  is why this is High and not Critical.
- How verified: Local run, no network. Read every call site that builds a
  URL with a credential.
- Direction (not a fix): Keep credentials out of anything that is logged
  (quiet the HTTP library's request lines, do not log exception text that
  embeds the URL), then rotate the three credentials, since they are already
  in the journal.
- Verification: Confirmed: an independent run with made-up keys and a mock transport, using the same `basicConfig` call, printed the Telegram token in the URL path and the FMP and Finnhub keys in the query string on every `httpx` INFO line and again in the bot's own ERROR lines, and nothing in the code lowers the `httpx` logger; the exposure is the droplet's root-only journal (the snapshot script scrubs these shapes), the broker keys travel in headers and are not logged, and the droplet's `httpx` version and journal contents are not checked.

### F-08-04: A rejected order or a rejected stop-loss order reaches only the system log, and the buy alert still shows a stop price
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/executor.py:34-66, 127-134`,
  `stock_agent/scheduler.py:1264-1267, 1323-1326, 1486, 1509-1510`,
  `stock_agent/options_executor.py:445-467`
- What happens: When the broker refuses a buy, a sell, an options order, or
  the stop-loss order that follows a buy, the bot writes a log line and
  moves on. For the stop-loss case the buy is still reported as a success
  and the alert shows the stop level.
- Evidence:
  ```python
  # executor.py:129-134
  if stop_result:
      logger.info("Stop-loss placed: ...")
  else:
      logger.warning("Stop-loss order FAILED for %s — position is unprotected", symbol)

  return result
  ```
  ```python
  # scheduler.py:1323-1326
  order = await self.executor.place_sell(position.symbol, position.shares)
  if not order:
      logger.error("Sell order failed for %s", position.symbol)
      return
  ```
  Run with a fake broker that accepts the buy and rejects the stop:
  ```
  orders sent: [('buy', 'market'), ('sell', 'stop')]
  signals reported executed: ['XYZ']
  telegram.send_trade_alert  thesis.stop_loss_price=93.0
  discord.send_trade_alert   thesis.stop_loss_price=93.0
  stock_agent.executor WARNING Stop-loss order FAILED for XYZ — position is unprotected
  ```
  A failed buy (`scheduler.py:1265-1267`) and a failed single-leg options
  order (`scheduler.py:1486`, no `else`) are also log-only. A timeout is
  logged with an empty reason: `str(httpx.ReadTimeout)` is `''`, so the line
  reads `Alpaca request POST /v2/orders failed: ` (checked against a
  localhost socket).
- Why it matters: A position can sit without its broker stop while the alert
  says it has one, and a stop-loss exit can fail every 30 minutes with
  nobody told.
- How verified: Read both executors and all call sites; ran
  `_execute_signals` with fakes.
- Direction (not a fix): Every order outcome that is not a clean success is
  a must-act alert on the channel Josh actually watches, and the buy alert
  states whether the stop is really in place. What the bot should do about
  the position is for area 03.
- Verification: Confirmed: read independently, `place_buy` returns the buy result whether or not the stop order was accepted, the buy, sell and options call sites only log on a refused order, the buy alert prints the computed stop level regardless, and nothing in the scheduler ever reads open orders to notice a missing stop.

### F-08-05: The 30-minute stop-loss check can fail or go blind with no alert
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1032-1101, 1573-1574, 1695-1696`,
  `stock_agent/data_feed.py:36-51, 243-262`, `stock_agent/portfolio.py:107-118`
- What happens: If anything inside the monitoring run raises, the handler
  writes a traceback to the journal and nothing else, and the next attempt
  is 30 minutes later. If the price source is down, no exception is raised
  at all: prices stay stale, no stop triggers, and the log says monitoring
  completed.
- Evidence:
  ```python
  # scheduler.py:1100-1101
  except Exception as e:
      logger.exception("Market monitoring failed: %s", e)
  ```
  Run with fakes:
  ```
  (a) quotes unavailable: price used for the stop check: 100.0 | sells triggered: [] | alerts sent: []
      scheduler log: Market monitoring complete — 1 positions checked
  (b) state save fails, real price $80 below the $93 stop: sells triggered: [] | alerts sent: []
      scheduler log: ERROR Market monitoring failed: [Errno 28] No space left on device
  ```
  In (b) the save inside `update_prices` (line 1044) raises before the
  stop check at 1047 is reached. The two neighbouring handlers log a single
  line with no traceback: options monitoring (1573-1574) and the
  event-triggered scan (1695-1696), which wraps `_execute_signals`, so an
  error after an order is placed there never reaches the main-loop alert.
- Why it matters: The comment at line 1046 calls this check
  "belt-and-suspenders". It is the only protection for a position whose
  broker stop was rejected (F-08-04), and it can be off for days unnoticed.
- How verified: Read the path; ran `_run_market_monitoring` with fakes.
- Direction (not a fix): The monitor should know the difference between
  "checked, all fine" and "could not check", and say so to Josh when it
  could not check.
- Verification: Confirmed: read independently, the monitoring handler at `scheduler.py:1100-1101` only logs so the main-loop alert is never reached, and a failed quote read returns `None`, leaves prices unchanged, triggers no stop and still logs "Market monitoring complete"; a broker stop order, where one was accepted, still works during such a gap, so the blind spot is the bot's own check and any position without a resting stop.

### F-08-06: Thirteen catch-alls turn a failed read into "nothing there" and the bot carries on
- Severity: High
- Confidence: Confirmed for items 1-4 (run with fakes); the rest read only
- Where: listed below
- What happens: Several handlers answer a failure with an empty result or a
  default, so the caller cannot tell "the broker says you hold nothing" from
  "the request failed". No alert is sent in any of these cases.
- Evidence, one by one:
  1. `executor.py:55-66` returns `None`; `get_positions` (75-80) turns that
     into `[]`. With the account read working and the positions read
     failing, `scheduler.py:816-821` calls the sync with an empty list and
     `portfolio.py:147-156` drops every local position. Run with a mock
     HTTP 503: `local equity positions after sync: [] | alerts sent: []`.
  2. `options_executor.py:283-291` returns `[]` on failure;
     `options_portfolio.py:416-551` then closes every open options position.
     Run: `{'closed': 1, 'realized_pnl': -600.0}`, reason
     `missing at Alpaca (assumed expired OTM)`.
  3. `options_executor.py:330-332` returns `[]` for fills, which feeds the
     same "assumed expired" branch.
  4. `scheduler.py:822-823`: "Alpaca sync failed — continuing with local
     state". The weekly and midweek cycles go on to risk checks and buys on
     unreconciled state.
  5. `scheduler.py:1399-1400`: options reconcile failed "(non-fatal)"; the
     options scan and its orders continue.
  6. `scheduler.py:1194-1199`: sector lookup fails with `pass`, no log; the
     sector becomes "Unknown", which `risk_manager.py:83-90` may not be able
     to check.
  7. `portfolio.py:31-42` and `options_portfolio.py:73-83` (four handlers):
     an unreadable state file and backup end in "Starting with fresh
     portfolio state" at INFO, with default cash, and no alert.
  8. `macro_monitor.py:144-146, 167-169`: failed quotes leave prices at 0,
     and `_compute_regime` (264-356) then scores the regime as NORMAL.
  9. `screener.py:51-53` with 136-138: a failed screening call gives every
     company a score of 0, so the "top" names sent to deep analysis are just
     the first ones in the list.
- Why it matters: These are the paths the project's own rule describes ("a
  failed broker call is not an empty result"). From the alerts' side the
  point is that each of them is invisible: the ledger can be emptied or the
  bot can trade on stale state and Josh hears nothing.
- How verified: Read each handler and its callers; ran items 1-3 with a mock
  transport and fakes.
- Direction (not a fix): Reads that feed money decisions need a distinct
  "failed" result that stops the action and raises an alert. The trading
  consequences belong to areas 03, 04 and 07 (see Handoffs).
- Verification: Confirmed: re-ran item 1 with a mock transport (account read succeeds, positions read returns 503, every local position is dropped with no alert; when both reads fail the sync is skipped correctly) and read items 2 to 9, which behave as described but were not re-run; item 1 is reachable only from the weekly and midweek cycles, item 2 from every options scan.

### F-08-07: Things that happen at the broker never produce an alert
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:147-156, 174-225`,
  `stock_agent/scheduler.py:808-823, 1394-1398`,
  `stock_agent/options_portfolio.py:523-551`, `stock_agent/discord_alerts.py:399-420`
- What happens: When a broker stop order fills, the bot does not know until
  a later sync, and the sync only logs "not found at Alpaca, dropping from
  local state". No trade is recorded, no profit or loss, no alert. The same
  holds for positions imported by a sync and for options that expire or are
  closed, whose realized result is logged only.
- Evidence: `get_order` and `get_open_orders` are never called (grep), so no
  fill is ever read. `sync_from_alpaca` returns nothing and
  `scheduler.py:821` ignores it. `send_risk_alert` supports `STOP_LOSS`,
  `THESIS_BREAK`, `EXPOSURE_WARNING` and `SECTOR_LIMIT`
  (`discord_alerts.py:401-407`) but is called once, for `PDT_WARNING`
  (`scheduler.py:1182`). `format_position_alert(..., "close")` is never
  called.
- Why it matters: The most important risk event, a stop-out at the broker,
  is invisible. The position just disappears from the next brief.
- How verified: Read the code paths; grep for callers.
- Direction (not a fix): Alerts for exits should come from what the broker
  reports, not only from what the bot itself initiated.

### F-08-08: No de-duplication or rate limit; three loops can flood both channels
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:127-142, 248-260, 361-474, 1551-1571`,
  `stock_agent/options_portfolio.py:599-601`, `deploy/stock-agent.service`
- What happens: No alert remembers that it was sent before.
  1. Weekly analysis: "done" is recorded only on success
     (`scheduler.py:465`). On an early return (385, 413) or an exception
     (468) the next tick starts it again, for the whole Sunday 18:00-20:59
     window. Simulated with fakes at one pass per 90 seconds:
     `weekly runs started=120, telegram messages=240, discord messages=240`.
     If the failure comes late in the run, the paid research is repeated
     each time.
  2. Options expiry alert: sent for every position with three days or fewer
     left, on every 30-minute monitoring pass (`scheduler.py:199-203,
     1551-1571`). About 13 passes a day for up to 4 trading days is about 50
     identical alerts per position in the `risk` channel, each saying the
     position "will run to expiry".
  3. Main loop: one alert per failed tick, every 60 seconds, no cap
     (`scheduler.py:131-142`). In practice few errors reach it, because each
     job swallows its own. One simulated case (state file not writable
     during buys) stopped by itself after one alert per pending signal, with
     the buy orders sent and no buy alert.
  4. A crash loop after startup sends "started" every 30-40 seconds
     (F-08-02). A restart inside a schedule window repeats that window's
     message, because the "done today" flags are in memory only
     (`scheduler.py:89-98`).
- Why it matters: Hundreds of identical messages bury the one that matters
  and teach Josh to mute the channel. Item 1 also wastes API money.
- How verified: Read; simulated the Sunday window and five loop passes with
  fakes.
- Direction (not a fix): Alerts need an identity and a memory (send once,
  then at most a reminder), and a failed job needs a retry rule that is not
  "again next minute".

### F-08-09: Alerts are sent in line with trading, can hold it up, and are lost when they fail
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/discord_alerts.py:58-82`, `stock_agent/telegram_alerts.py:50-63`,
  `stock_agent/scheduler.py:364-371, 1073-1091, 1216-1219, 1305-1310, 1646`
- What happens: The scheduler waits for each send before its next step.
  Simulated with a mock transport:
  ```
  Discord unreachable: attempts=4 backoff sleeps=[1, 2, 4] -> 4*30 + 7 = 127 s per embed
  Discord 429 retry_after=600: attempts=4 sleeps=[600.0, 600.0, 600.0, 600.0] total wait=2400.0 s
  Discord 502: attempts=1 returned=False
  Telegram unreachable: requests=1
  ```
  The wait on a 429 is whatever Discord says, with no upper bound. A
  material-event message is awaited (1073-1076) before the re-analysis and
  sell decision that follows it (1081-1091). A lost alert is not queued or
  retried later, the other channel is not told, and the `False` return is
  ignored everywhere. Where sends are grouped with
  `asyncio.gather(..., return_exceptions=True)` (for example 1305-1310), an
  exception while building a message is discarded without a log line.
- Why it matters: A Discord outage slows exits and buys by minutes per
  message, and the messages Josh most needs can vanish with only a journal
  line.
- How verified: Read; simulated retries locally.
- Direction (not a fix): Sending should be off the trading path, with a
  bounded wait, a small retry queue, and a fallback to the other channel.

### F-08-10: Several routine messages state things that are not true
- Severity: Medium
- Confidence: Confirmed
- Where: listed below
- What happens and evidence:
  1. Weekly report: the run is on Sunday, the market is closed, so
     `executed_signals = []` (`scheduler.py:433-442`) and that empty list is
     what is reported (447-463). Telegram always says "New Signals: None
     this week" (`telegram_alerts.py:165-166`) and the plain-English note
     says "No new stocks met the bar this week" (`discord_alerts.py:604`),
     even when buys are queued for Monday.
  2. Morning brief, "Bot Activity": lines such as "Daily pre-market scan
     completed" and "Market monitoring ran every 30 min" are chosen from the
     day of the week alone (`scheduler.py:1827-1837`). They appear even if
     the bot was down all day.
  3. Morning brief, "Yesterday P&L": it is current equity minus the last
     close, read at 08:15 (`scheduler.py:1722-1723, 1742`), which is the
     overnight move.
  4. Screener table: reads `score` or `composite_score`
     (`discord_alerts.py:453`); the screener returns `composite`
     (`screener.py:125`). Every row shows 0.0 and a blank name (run).
  5. Trade, thesis and daily-summary embeds hardcode "Paper Mode"
     (`discord_alerts.py:197, 243, 311`). With `STOCK_AGENT_MODE=LIVE` the
     footer still says Paper (run). This matters once real money is used.
  6. The plain-English buy note says the bot sells "if the stock drops 5%"
     (`discord_alerts.py:550`); the stop is 7-12% (`config.py:162-163`).
  7. Startup embed labels current cash as "Starting Capital"
     (`discord_alerts.py:134`, `scheduler.py:117`).
  8. Options alert: called with `"OPENED"` (`scheduler.py:1503`) but the
     lookup keys are lower-case `"open"` (`options_portfolio.py:693-742`),
     so the title is the generic "Options — XYZ" in grey. The entry price
     can show `$0.00` (`scheduler.py:1488-1489`). For a spread the alert is
     sent even if both legs were rejected, because the result is a tuple
     and always true (`scheduler.py:1486`, `options_executor.py:178`).
  9. Daily summary "Trades Today" shows wins/total (`discord_alerts.py:255-270`),
     so two buys read "0/2".
  10. The buy alert shows the planned price and quantity, not a fill; the
      bot never reads the fill (`scheduler.py:1295-1302`).
- Why it matters: Item 2 is false reassurance about exactly the thing the
  brief worries about. The rest erode trust in the numbers.
- How verified: Read; built the messages with made-up inputs and captured
  them.
- Direction (not a fix): Reports should be built from recorded facts (what
  ran, what filled, what is queued) and from the live settings, not from
  fixed text.

### F-08-11: A research or data outage looks the same as a quiet week
- Severity: Medium
- Confidence: Confirmed by reading; not run
- Where: `stock_agent/analyst.py:29-51, 155-198`,
  `stock_agent/scheduler.py:524-525, 937-947, 998-999`,
  `stock_agent/sentiment.py:37-39`, `stock_agent/analyst_trends.py:40-42`
- What happens: If Perplexity fails, `_call_perplexity` returns `None`, each
  symbol logs "Empty response" and is skipped, and the cycle finishes
  normally with zero signals. The material-event check returns "no event" on
  any failure, and on a response it cannot parse it does so with no log at
  all (`analyst.py:191-198`). Midweek returns silently if no fundamentals
  came back (`scheduler.py:524-525`). Finnhub failures are logged at DEBUG,
  which is never shown (F-08-12).
- Why it matters: An expired key or exhausted quota means no new ideas and
  no event-driven exits, while every report says all is calm.
- How verified: Read the paths end to end.
- Direction (not a fix): Count failures per cycle per provider and alert
  when a provider is effectively down.

### F-08-12: The log-level setting is ignored, and some failures are logged at a level that is never shown
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/__main__.py:9-12`, `stock_agent/config.py:214`, `.env.example:42`
- What happens: `__main__.py` hardcodes `level=logging.INFO`.
  `Config.LOG_LEVEL` reads `STOCK_LOG_LEVEL` and is referenced nowhere.
  `.env.example` documents a third name, `LOG_LEVEL`. Failure lines at DEBUG
  are therefore invisible: `scheduler.py:792`, `data_feed.py:416`,
  `sentiment.py:38`, `analyst_trends.py:41`, and
  `options_portfolio.py:308, 324` (after which an expired option is valued
  as "assumed OTM"). Most error lines print only the exception text with no
  type or traceback (64 `logger.error` against 12 `logger.exception`), and
  a timeout prints nothing after the colon.
- Why it matters: Diagnosis needs a code change, and some failures leave no
  trace.
- How verified: Grep for every reference; read the handlers.
- Direction (not a fix): One setting, actually applied, and failures logged
  at a visible level with their type.

### F-08-13: Message formatting edge cases
- Severity: Low
- Confidence: Likely (the Telegram and Discord responses were not tested)
- Where: `stock_agent/telegram_alerts.py:53-61, 82, 90, 164`,
  `stock_agent/risk_manager.py:223`, `stock_agent/discord_alerts.py:13, 224-225, 390-395`
- What happens: Telegram messages use HTML mode, but dynamic text is not
  escaped, apart from the error alert. Every stop-loss reason contains `<=`
  (`risk_manager.py:223`), and thesis summaries are free text. Telegram is
  expected to reject such a message; the fallback resends it with
  `parse_mode` set to an empty string, so at best it arrives with literal
  `<b>` tags. The fallback also fires at once on a 429. On Discord, an
  empty bull or bear case (the default at `analyst.py:276-277`) produces a
  field with an empty value, which Discord is expected to reject, losing
  the whole embed. `MAX_EMBED_LENGTH` is defined and never used, and the
  "split if needed" check compares the number of embeds (at most 3) with 10.
- Why it matters: Occasional lost or ugly messages.
- How verified: Built the messages locally; platform behaviour inferred
  from their documented rules.
- Direction (not a fix): Escape all dynamic text and validate a message
  against the platform limits before sending.

### F-08-14: Telegram is the thin channel; Discord carries duplicates and unused channels
- Severity: Note
- Confidence: Confirmed
- Where: inventory tables above
- What happens: Telegram receives 4 of the 14 error paths and none of the
  blocked-trade, material-event or expiry messages. Discord has three
  channels nothing writes to, two channels that are copies of others, and
  an alert type list that is mostly unused.
- Why it matters: Context for deciding which channel is the pager and what
  to cut.
- How verified: Traced every send call.
- Direction (not a fix): Decide the role of each channel first (question 1
  below).

## Checked and fine

- A failing alert cannot crash the trading path: both senders catch all
  errors (`telegram_alerts.py:50-63`, `discord_alerts.py:58-82`) and call
  sites use `gather(return_exceptions=True)` or a `try`.
- No path forwards a URL containing a key to Telegram or Discord: every HTTP
  call is wrapped where it is made, so the exception text sent on in
  `str(e)` never comes from the HTTP library.
- Alpaca, Perplexity, OpenAI, Discord and TipRanks credentials are sent in
  headers and do not appear in the request log lines.
- Morning brief fits Discord's 6,000-character message limit: 3,214
  characters with 20 positions, 4,331 with 40 (measured).
- Daily-summary position table splits correctly: all rows shown once for 20
  and 45 positions (`discord_alerts.py:274-295`, measured).
- Thesis embed: 2,680 characters typical; 5,894 with every capped field at
  its cap and a 600-character summary.
- Telegram splitter keeps chunks at or under 4,000 characters and loses
  nothing (`telegram_alerts.py:176-195`, measured).
- Telegram error alert escapes HTML and truncates to 1,500 characters
  (`telegram_alerts.py:170-173`).
- Discord 429 handling reads `retry_after` and retries
  (`discord_alerts.py:61-65`); the wait is unbounded, see F-08-09.
- No bare `except:`; cancellation and Ctrl-C are not swallowed.
- Log volume is small, an estimated 1,500 lines a day and 2,000 per weekly
  run, so the bot's own output is not a disk risk under journald defaults.
- SIGTERM and SIGINT set a flag and the loop sends a shutdown message when
  it exits (`__main__.py:16-20`, `scheduler.py:143-144`), provided the tick
  ends inside systemd's stop timeout.
- All four leads in the brief were checked. None was harmless: F-08-03
  (token in logged URL), F-08-12 (log level), F-08-08 (per-tick alert),
  F-08-04 (unprotected position).

## Could not verify

- Real error frequency and whether key-bearing URLs are in the droplet's
  journal. Needs the snapshot: `logs/journal.log`, `logs/digest.txt`, and
  the "pattern match" count in `redaction-report.txt`.
- The httpx version on the droplet. Needs `deploy/pip-freeze.txt`.
- Journal retention and size on the droplet. Needs
  `logs/journal-disk-usage.txt` and `/etc/systemd/journald.conf`, which the
  snapshot script does not collect.
- Restart and crash history. Needs `service/show.txt` (NRestarts) and
  `service/start-stop-history.txt`.
- How Telegram treats an unescaped `<=` and an empty `parse_mode`, and
  whether Discord rejects an empty field value. Needs one test message to a
  test chat and channel, or the platform documentation.
- Whether Alpaca rejects the bot's market sell while the stop order is open
  (which decides which branch of F-08-01 is hit in practice). Needs the
  Alpaca order history; area 03.
- Who can read the Discord `system_logs` channel, which receives raw error
  text.

## Questions for Josh

1. Which channel is the one you actually watch, Telegram or Discord? Should
   every must-act alert go there?
2. How long may the bot be silent before something outside it should
   message you: one hour, half a day?
3. Which Discord channels do you read? May `trade_history`, `analyst`, the
   plain-English channels and the INFO lines in `system_logs` be dropped?
4. When a stop-loss order cannot be placed, do you want an alert only, or an
   alert and the position sold?
5. Do you want an alert whenever the broker fills an order, including stop
   orders, taken from the broker's own record?
6. Are you content for the server log to hold balances and order IDs, given
   that you may share log excerpts?

## Handoffs

- 09: `.env.example:2-3` holds non-placeholder values for
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in a public repository, and
  `deploy/setup.sh:35-38` copies that file to `.env`; treat the token as
  exposed.
- 09: TipRanks credentials are hardcoded at `config.py:194-195` and
  `tipranks_client.py:18-19`; Discord guild and channel IDs at
  `config.py:22-40`.
- 09: `/var/log/auto_update.log` has no rotation (`deploy/setup.sh:43`);
  the service cannot start if `git fetch` fails (`ExecStartPre`).
- 03: a failed positions read with a working account read empties the local
  ledger (`scheduler.py:816-821`, `executor.py:75-80`); shown with fakes.
- 03: no fill is ever read; the stop is sent 3 seconds after the buy
  unchecked; the stop order is never cancelled before the bot's own market
  sell; after a broker stop fills, the bot's check can send a second sell.
- 03: an order POST that times out is treated as "failed" though it may
  have been accepted (`executor.py:64-66`).
- 03: positions imported by sync get a stop price in the ledger but no stop
  order (`portfolio.py:202`).
- 04: a failed options-positions read closes every open options position
  and books the loss (`options_executor.py:283-291`); shown with fakes.
- 04: a spread counts as opened even if both legs were rejected
  (`scheduler.py:1486`); entry price is a proxy, possibly 0.0 (1488-1489).
- 01: failed weekly analysis reruns every tick for three hours; "done
  today" flags and pending signals are in memory only; the event-triggered
  scan runs a full analysis inside market hours and pauses monitoring.
- 01: `build_tipranks_lookup` uses blocking `requests` inside the event
  loop (`scheduler.py:830`).
- 02: sector lookup failure is swallowed silently before the sector cap
  check (`scheduler.py:1194-1199`).
- 05: screener failure scores every company 0, so deep analysis runs on
  arbitrary names (`screener.py:136-138`).
- 06: failed macro quotes produce a NORMAL regime and can trigger a "regime
  changed" event scan (`macro_monitor.py:144-146`, `scheduler.py:1626`).
- 07: unreadable state files end in a silent fresh start with default cash
  (`portfolio.py:31-42`); a state-save failure during a buy leaves the
  order placed and the position in memory only.
- 10: dead code: `_sec_get`, `_alpaca_data_get`, five options-executor
  methods, `OptionsPortfolio.close_position`, `send_macro_update`;
  `aiohttp` and `python-dotenv` are required but never imported.
