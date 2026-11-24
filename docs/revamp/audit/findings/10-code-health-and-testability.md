# 10. Code health and testability

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

This area looks at the bot as a piece of software rather than as a trader: how
the code is organised, what is left over from earlier versions, and how safely
it can be changed. The code is about 9,250 lines in 24 files. One file,
`scheduler.py`, makes almost every decision and calls everything else; the
other files do not depend on each other in tangled ways, which is good news
for a step-by-step revamp.

The biggest weakness is that nothing checks the code that handles money. The
136 tests in the repository were all added on the day the audit was set up;
79 of them test the Claude Code guard rails, 24 only check that files load,
and the 33 that test the bot cover four risk functions. No test places a
pretend order, records a trade, or reconciles with the broker. The cost of
that is visible: since 23 April every sell has ended in a coding error right
after the order was sent, so the bot reports "Sell failed" for sells that went
through and never sends the real sell alert. A standard checking tool finds
that bug in seconds, but none is run.

Settings are the second weakness. 18 of the 56 settings in `config.py` are
read by nothing, including the options on/off switch and every options limit
(the options limits that really apply live in another file, and two of them
differ). The third is copy-and-paste: there are three separate Alpaca clients,
two of which ignore the configured address, and every network helper reports a
failed request as "nothing there". I showed with made-up data that this makes
the options ledger close every open position after one failed request.

Finally, the README, many comments, and `.env.example` describe an earlier bot
(a 5% stop, "bracket orders", "live mode flips a single flag", Anthropic, and
83 Polymarket-era settings).

## How it actually works

### Size and shape

| Part | Files | Lines | Share |
| --- | --- | --- | --- |
| Orchestrator | `scheduler.py` | 1,990 | 22% |
| Options | `options_engine.py`, `options_portfolio.py`, `options_executor.py`, `options_data.py`, `options_models.py` | 2,683 | 29% |
| Data and enrichment | `data_feed.py`, `tipranks_client.py`, `macro_monitor.py`, `analyst_trends.py`, `research_notes.py`, `sentiment.py`, `news_feed.py` | 2,060 | 22% |
| Alerts | `discord_alerts.py`, `telegram_alerts.py` | 877 | 9% |
| Risk and ledger | `risk_manager.py`, `portfolio.py` | 591 | 6% |
| LLM callers | `analyst.py`, `screener.py` | 481 | 5% |
| Settings and models | `config.py`, `models.py` | 357 | 4% |
| Equity broker client | `executor.py` | 188 | 2% |
| Entry | `__main__.py`, `__init__.py` | 26 | <1% |

277 functions, 130 of them `async`. There is no `pyproject.toml`, no package
metadata, and no declared Python version; the package runs from the repository
root with `python -m stock_agent`.

### Module dependency map (question 1)

Built by parsing every `import` in `stock_agent/` with Python's `ast` module.

```
__main__ ─▶ scheduler ─▶ all 21 other modules
                │
                ├─ analyst, data_feed, screener, portfolio, risk_manager,
                │  discord_alerts, telegram_alerts      ─▶ config, models
                ├─ executor, macro_monitor, news_feed, sentiment,
                │  analyst_trends, research_notes        ─▶ config
                ├─ options_data, options_executor        ─▶ config, options_models
                ├─ options_portfolio                     ─▶ config, options_data, options_models
                ├─ options_engine                        ─▶ config, macro_monitor, options_data, options_models
                └─ tipranks_client                       ─▶ (nothing internal)
```

- **No import cycles.** The comment "local import to avoid cycle" at
  `stock_agent/portfolio.py:129` is wrong; `models.py` imports nothing from
  the package.
- **Modules everything depends on:** `config` (imported by 18 modules),
  `models` (8), `options_models` (5). Every other module is imported only by
  `scheduler.py`, except `options_data` (3 importers) and `macro_monitor` (2).
- **Third-party libraries:** `httpx` in 14 modules, `pydantic` in 2,
  `requests` in 1 (`tipranks_client.py:13`). `aiohttp` and `python-dotenv`
  are imported nowhere.

The import graph is clean, but there are couplings it does not show:

- The scheduler reads `self.portfolio.state.…` 35 times, changes a position's
  share count directly (`scheduler.py:1275`), and calls private methods
  `self.portfolio._save()` (`scheduler.py:1278`, `1967`) and
  `self.discord._send_multi_embed()` (`scheduler.py:1930`).
- Modules pass plain dictionaries to each other and disagree about the keys
  (F-10-09).
- Settings reach code three ways: the `Config()` object handed to a class,
  the `Config` class read directly (`news_feed.py:29`, `sentiment.py:28`,
  `analyst_trends.py:31`, `research_notes.py:103`, `portfolio.py:204`), and
  not at all (constants in `tipranks_client.py:18-19`,
  `options_executor.py:27`, `options_data.py:28-29`,
  `options_engine.py:52-73`, and `os.environ.get("FMP_API_KEY")` at
  `options_portfolio.py:315`).

### What `scheduler.py` does (question 2)

One class, `StockAgentScheduler`, 35 methods, 1,919 lines. The constructor
builds all 13 collaborators itself (`scheduler.py:73-87`). `run()` loops
forever: call `_tick()`, sleep 60 seconds (`scheduler.py:127-142`). `_tick()`
runs every job that is due, one after another, in the same task
(`scheduler.py:146-210`); nothing runs concurrently, so a long job delays
every job behind it, including the stop-loss check.

| # | Job | Where | Lines | Does I/O? |
| --- | --- | --- | --- | --- |
| 1 | Process lifecycle: build collaborators, start-up and shut-down messages, main loop, clean-up | `73-144`, `1972-1990` | ~90 | yes |
| 2 | Schedule rules: 12 `_should_…` checks, `_is_market_open`, and 9 "done today" flags | `212-320`, `1103-1108` | ~115 | no (pure) |
| 3 | Research cycles: weekly, mid-week, daily pre-market, earnings-reactive, event-triggered | `361-804`, `1578-1696` | ~560 | yes |
| 4 | Shared research helpers: TipRanks load, fundamentals fetch, deep analysis, re-analysis | `825-1028` | ~200 | yes |
| 5 | Equity execution and ledger updates: `_execute_signals`, `_execute_sell` | `1173-1370` | ~200 | yes |
| 6 | Position monitoring: prices, stop check, material events | `1032-1108` | ~75 | yes |
| 7 | Options: reconcile, build candidates, call engine, place orders, record, expiry alerts | `1374-1574` | ~200 | yes |
| 8 | Macro check and market-event detection | `324-359`, `1578-1640` | ~100 | yes |
| 9 | Reports: daily-summary arithmetic and the morning brief (236 lines of Discord formatting) | `1112-1169`, `1700-1935` | ~295 | yes |
| 10 | Broker reconcile wrapper and sector backfill | `808-823`, `1939-1968` | ~45 | yes |

Error handling inside it: 47 `except Exception` blocks, 12 of which are
`except Exception: pass`.

### How a BUY signal is created: five paths that differ

| Path | Where | Extra research fed to the LLM | Conviction threshold | Broker reconcile in the same cycle |
| --- | --- | --- | --- | --- |
| Weekly | `scheduler.py:361-474` | TipRanks, Finnhub news, sentiment, analyst trends, research notes; conviction then adjusted | macro-adjusted minimum (6, 7 or 8) | yes (`:371`) |
| Mid-week | `scheduler.py:478-561` | same | same | yes (`:491`) |
| Event-triggered | `scheduler.py:1663-1688` | Finnhub and notes; **no TipRanks** (`:1677`) | same | **no** |
| Daily pre-market | `scheduler.py:628-672` | research notes only (`:646-649`) | hard-coded `>= 8` (`:650`) | **no** |
| Earnings-reactive | `scheduler.py:738-794` | research notes only (`:765-768`) | hard-coded `>= 8` (`:769`) | **no** |

All five end in `_execute_signals` (`scheduler.py:1173`), either at once or
through the in-memory `_pending_signals` list when the market opens
(`scheduler.py:194-197`).

### Tests and tooling today

- `python3 -m pytest -q`: 136 pass. `tests/test_claude_hooks.py` 79 (tests
  the `.claude/hooks` scripts, not the bot), `tests/test_smoke_imports.py` 24
  (each module imports), `tests/test_risk_tripwires.py` 33 (config constants
  and four `RiskManager` methods).
- `tests/` was first added in commit `ff6fdc7` on 2026-11-20. No commit to
  `stock_agent/` has been made since, so no test has yet guarded a change.
- `ruff.toml:10` selects only `E9, F63, F7, F82` (syntax errors and undefined
  names). With that configuration `ruff check stock_agent` reports nothing.
- No type checker is configured or run. No formatter is in use.
- CI (`.github/workflows/ci.yml`) runs compile, ruff, and pytest on pull
  requests; its own comment (`ci.yml:4-5`) notes the droplet deploys `master`
  without waiting for it.

## Findings

### F-10-01: No test covers any code that places an order, records a trade, or reconciles with the broker
- Severity: High
- Confidence: Confirmed
- Where: `tests/` (whole directory); untested: `stock_agent/scheduler.py`, `executor.py`, `portfolio.py`, `options_*.py`, `data_feed.py`, both alert modules
- What happens: The bot ran from April to November with no tests. The tests
  added with the audit set-up exercise four risk-manager methods and some
  constants. Everything between "a signal exists" and "an order is at Alpaca
  and written to the ledger" is untested.
- Evidence:
  - `git log --diff-filter=A -- tests` shows one commit, `ff6fdc7`
    (2026-11-20). `git log ff6fdc7..HEAD -- stock_agent` is empty.
  - `tests/test_risk_tripwires.py:17-20` imports only `options_engine`,
    `options_portfolio` (for constants), `Config`, three models, and
    `RiskManager`. Methods called: `calculate_position_size`,
    `can_open_position`, `get_stop_loss_price`, `should_stop_loss`.
    `check_pdt_compliance` and `check_position_health` are not called.
  - Three things the tripwire file pins give more comfort than they should:
    `tests/test_risk_tripwires.py:98-105` pins `Config.OPTIONS_*` values that
    no code reads (F-10-03); `:150-161` pins stop prices for betas 2.0 and
    3.0, which the running bot can never supply (F-10-07); `:82` pins
    `MIN_CONVICTION == 7` although `macro_monitor.py:337,342` lowers the
    effective minimum to 6 in two regimes.
  - `tests/conftest.py:33-49` blanks credentials "so no test can reach a real
    account by accident", but the TipRanks key and token are constants in
    `stock_agent/tipranks_client.py:18-19`, so a future test that reaches
    `build_tipranks_lookup()` would call the real API.
  - `.github/workflows/ci.yml:4-5`: "the droplet deploys master on its own
    timer and does not wait for this to pass."
- Why it matters: The revamp will change this code one pull request at a
  time, and every merge is live within ten minutes. Without tests on the
  order and ledger paths, a change can break exits or sizing and nobody
  finds out until an alert looks odd. F-10-02 is a bug of exactly that kind
  that has been live for seven months.
- How verified: Ran `python3 -m pytest -q` (136 passed) and counted tests per
  file with `--collect-only`; read all three test files; checked git history.
- Direction (not a fix): Before any behaviour change, add tests with a fake
  broker around the five functions named in F-10-15, and decide whether a
  failing check should be able to stop a deploy.
- Verification: Confirmed: the three test files import only `Config`, three models, `RiskManager` and options constants (136 tests pass, none touches `scheduler.py`, `executor.py`, `portfolio.py` methods or any order path), tests first appear in `ff6fdc7`, and `ci.yml:4-5` states the deploy does not wait for CI; High holds only because the untested sell path already carries a live bug (`scheduler.py:1349` reads an attribute that is never set).

### F-10-02: Every sell ends in an `AttributeError` after the order is sent; the bot then reports "Sell failed"
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1316-1370`
- What happens: `_execute_sell` places the sell, removes the position, logs
  the trade, and then reads `self._tipranks_lookup`, an attribute that is
  never created anywhere. The error is caught by the function's own handler,
  which sends "Sell failed for <symbol>" to Telegram and an error to Discord.
  The three normal sell alerts are never sent.
- Evidence:
  ```python
  # scheduler.py:1344-1350
  self.portfolio.remove_position(position.symbol)
  self.portfolio.log_trade(trade)

  tr_clause = format_tipranks_telegram_clause(
      self._tipranks_lookup.get(position.symbol.upper())
  )
  ```
  ```python
  # scheduler.py:1364-1367
  except Exception as e:
      logger.exception("Sell execution failed for %s: %s", position.symbol, e)
      await asyncio.gather(
          self.telegram.send_error_alert(f"Sell failed for {position.symbol}: {e}"),
  ```
  `_tipranks_lookup` appears once in the package (`scheduler.py:1349`); the
  constructor (`scheduler.py:73-103`) does not set it and
  `_load_tipranks_data` returns the lookup without storing it
  (`scheduler.py:825-838`). It arrived in commit `7a8ac46` (2026-04-23),
  whose message says "Cache lookup on self._tipranks_lookup", but the
  assignment was never written. Callers: stop-loss (`:1053`), material event
  (`:1088`, `:615`), thesis broken (`:1024`), post-earnings (`:727`).
  A second defect sits behind the first: the success log at
  `scheduler.py:1359-1362` uses `"$%+,.0f"`, which `%`-formatting rejects
  (`ValueError: unsupported format character ','`).
- Why it matters: On every exit Josh is told the opposite of what happened,
  and gets no record of the price or P&L. The ledger itself is correct (the
  position is removed and the trade logged before the error).
- How verified: Built a real `StockAgentScheduler` offline, replaced
  `data_feed`, `executor`, `telegram`, `discord` with recording fakes, and
  ran `_execute_sell`. Calls made, in order: `get_stock_quote`, `place_sell`,
  `send_error_alert`, `send_error`. No `send_trade_alert`. `mypy` reports the
  same line: `"StockAgentScheduler" has no attribute "_tipranks_lookup"`.
  Ran `"P&L: $%+,.0f" % 12.0` and got the `ValueError`.
- Direction (not a fix): Repair with a test that drives `_execute_sell`
  through a fake broker and asserts which alerts go out.

### F-10-03: The options on/off switch and all twelve options settings in `config.py` are read by nothing
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/config.py:200-212`; real limits at `stock_agent/options_engine.py:52-73`, `stock_agent/options_portfolio.py:43-45`, `stock_agent/options_models.py:182-195`
- What happens: Setting `OPTIONS_ENABLED=false` changes nothing; the options
  scan still runs and still places orders. The other eleven `OPTIONS_*`
  values are also unused. The limits that actually apply are separate
  constants, and two of them disagree with `config.py`.
- Evidence: A scan of every `config.X`, `Config.X`, and
  `getattr(config, "X")` in `stock_agent/` finds no reader for any
  `OPTIONS_*` name. `_should_run_options_scan` (`scheduler.py:224-236`)
  checks weekday, a done-today flag, and the time, nothing else.
  `git log -S'OPTIONS_ENABLED' -- stock_agent` shows only the commit that
  added the line, so it has never been read.

  | Setting in `config.py` | Value | What the code uses | Value |
  | --- | --- | --- | --- |
  | `OPTIONS_ENABLED` (`:201`) | env, default true | nothing | n/a |
  | `OPTIONS_MIN_RR_RATIO` (`:212`) | 2.0 | `MIN_REWARD_TO_RISK` (`options_engine.py:55`) | **1.5** |
  | `OPTIONS_SPREAD_WIDTH_MIN/MAX` (`:210-211`) | 5.0 / 10.0 | `SPREAD_WIDTH_NARROW/WIDE` (`options_engine.py:72-73`) | **10.0 / 20.0** |
  | `OPTIONS_MAX_DTE` (`:208`) | 45 | `CC_MAX_DTE` / `SPREAD_MAX_DTE` (`options_engine.py:62,66`) | **35** / 45 |
  | `OPTIONS_MAX_POSITIONS` (`:202`) | 10 | `MAX_OPTIONS_POSITIONS` (`options_engine.py:52`) | 10 |
  | `OPTIONS_MAX_SINGLE_TRADE_PCT` (`:203`) | 0.07 | `MAX_SINGLE_TRADE_PCT` (`options_engine.py:53`) | 0.07 |
  | `OPTIONS_MAX_TOTAL_EXPOSURE_PCT` (`:204`) | 0.15 | `MAX_TOTAL_OPTIONS_EXPOSURE_PCT` (`options_engine.py:54`) | 0.15 |
  | `OPTIONS_AUTO_CLOSE_PROFIT_PCT`, `_DTE` (`:205-206`) | 0.50, 3 | auto-close is switched off (`scheduler.py:1526-1529`) | n/a |

  The comments beside the dead values are also wrong: `config.py:203` says
  "Max 5% of portfolio per trade" next to `0.07`; `config.py:209` says
  "Minimum 1% monthly premium" next to `0.005`.
- Why it matters: Josh, or an assistant acting for him, can "turn options
  off" or "tighten a limit" in the obvious place and the bot carries on
  unchanged. The tripwire test pins both copies (`test_risk_tripwires.py:98-119`)
  but not the two that disagree. Today the unintended orders land in the
  paper account. With real money the effect depends on F-10-09: the options
  client is fixed to the paper address, so live keys would be rejected there;
  if that address is ever made configurable without fixing this switch, the
  bot would trade real options with no way to turn them off short of a code
  change.
- How verified: Wrote a script that lists every `Config` attribute and the
  modules that read it; read the scheduler paths that start an options scan
  (`scheduler.py:182-184`, `1685-1688`).
- Direction (not a fix): One source of truth for each limit, and a switch
  that is checked where the options scan starts, with a test that proves it.
- Verification: Confirmed: an independent search finds no reader of any `OPTIONS_*` name outside `config.py` and the tripwire test, `_should_run_options_scan` (`scheduler.py:224-236`) and both call sites (`:182-184`, `:1685-1688`) have no enabled check, and the engine's own constants (`options_engine.py:52-73`) are what apply; the write-up says two settings disagree but its own table correctly shows three (reward-to-risk, spread widths, covered-call maximum DTE).

### F-10-04: A failed network call returns the same thing as "nothing there", and callers act on it
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/options_executor.py:264-291`, `293-332`; `stock_agent/executor.py:34-80`; `stock_agent/data_feed.py:36-103`; `stock_agent/scheduler.py:808-823`, `1386-1400`, `1537-1546`; `stock_agent/options_portfolio.py:416-551`
- What happens: Every HTTP wrapper catches all errors, logs, and returns
  `None`, `[]`, or `{}`. Callers cannot tell "Alpaca says you hold nothing"
  from "the request timed out". The options reconcile treats an empty list as
  truth and closes every open position in the ledger.
- Evidence:
  ```python
  # options_executor.py:288-291
  except Exception as exc:
      logger.error("get_option_positions error: %s", exc)

  return []
  ```
  `scheduler.py:1538-1544` passes that list straight to
  `options_portfolio.sync_from_alpaca`. There, a position whose symbol is not
  in the list is closed; with no expiry and no fill it takes this branch:
  ```python
  # options_portfolio.py:507-510
  else:
      # Missing with no clear reason — assume expired/assigned to zero
      per_share_exit_value = 0.0
      close_reason = "missing at Alpaca (assumed expired OTM)"
  ```
  This runs every 30 minutes in market hours whenever an options position is
  open (`scheduler.py:199-203`, `1532-1546`).
  The equity side has the same shape: `Executor.get_positions` returns `[]`
  on failure (`executor.py:75-80`), and `_sync_portfolio_from_alpaca` skips
  the sync only when the account call failed too (`scheduler.py:818`);
  otherwise `Portfolio.sync_from_alpaca` drops every local position it does
  not find (`portfolio.py:147-156`).
  None of the Alpaca, FMP, Finnhub, Perplexity, or OpenAI wrappers retries.
  Only Discord (`discord_alerts.py:58-80`) and Telegram
  (`telegram_alerts.py:55-61`) do.
- Why it matters: One timeout or rate-limit response from Alpaca rewrites
  the options ledger: positions vanish from "open", a made-up realised loss
  or gain is booked, and the next options scan sees no existing positions on
  those underlyings and has room to open them again. This breaks the project
  rule "A failed broker call is not an empty result"
  (`.claude/rules/money-paths.md`).
- How verified: Offline, with a temporary data directory and made-up data: opened one
  bull call spread expiring in 30 days at a $3.00 debit, then called
  `sync_from_alpaca(alpaca_option_positions=[], alpaca_option_activities=[])`.
  Result: `{'closed': 1, 'realized_pnl': -300.0, 'still_open': 0}`, close
  reason "missing at Alpaca (assumed expired OTM)". Read both call sites and
  all wrappers. Overlaps areas 03 and 04, which own the money consequence.
- Direction (not a fix): Broker reads need a result that says whether the
  call worked; reconcile must refuse to run on a failed read.
- Verification: Confirmed: no guard exists between `get_option_positions` returning `[]` on any error (`options_executor.py:283-291`) and `sync_from_alpaca` closing every ledger position (reproduced offline with two made-up positions: both closed, a full loss booked, count and exposure reset to 0), and the equity sync skips only when the account call also failed (`scheduler.py:818`). Suggested severity: Critical, because the options reconcile never re-imports broker positions and the position-count and exposure caps are computed from the wiped ledger (`options_portfolio.py:817-824`, `options_engine.py:128-145`), so later scans can open positions beyond the intended limits; rate consistently with F-03-04 and F-04-04, which describe the same defect.

### F-10-05: `Config` is read once at import, checks nothing, and its `MODE` setting is only a label
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/config.py:5-214`; `stock_agent/scheduler.py:111-117`; `stock_agent/discord_alerts.py:50,89,197,243,311`; `stock_agent/executor.py:31-32`
- What happens: `Config` is a class whose attributes are computed from the
  environment when the module is first imported. Nothing validates them.
  `STOCK_AGENT_MODE` is printed in messages but decides nothing; where orders
  go is decided by `ALPACA_BASE_URL` alone (equities) or is hard-coded
  (options).
- Evidence (each line is a fresh interpreter with only that variable set):

  | Environment | Result |
  | --- | --- |
  | `MAX_POSITION_PCT=15` | accepted; cap becomes 1500%, so the per-symbol hard cap never triggers |
  | `MAX_POSITION_PCT=15%` or empty | `ValueError` at import; the process cannot start |
  | `TIPRANKS_MIN_SMART_SCORE=8.5` | `ValueError` at import |
  | `TIPRANKS_ENABLED=1` | parsed as **False** (`config.py:196` accepts only the word "true") |
  | `STOCK_AGENT_MODE=live`, paper URL | messages say "live"; orders still go to paper |
  | `STOCK_AGENT_MODE=PAPER`, `ALPACA_BASE_URL` set to the live address | messages say "PAPER"; equity orders go to the live account |

  `MODE` is read at `scheduler.py:111`, `115`, `117` and
  `discord_alerts.py:50`, `89`, all for display. Three Discord footers
  hard-code the words "Paper Mode" (`discord_alerts.py:197`, `243`, `311`).
  18 of 56 public attributes are read by nothing: the twelve `OPTIONS_*`
  (F-10-03), `DISCORD_GUILD_ID` (`:22`), `DAILY_PRESCAN_HOUR` (`:175`),
  `MARKET_CLOSE_HOUR` (`:176`), `TIPRANKS_API_KEY`, `TIPRANKS_API_TOKEN`
  (`:194-195`), and `LOG_LEVEL` (`:214`; `__main__.py:10` hard-codes `INFO`,
  so the six failure messages logged at debug level, for example
  `scheduler.py:792` and `sentiment.py:38`, can never be seen).
  `STOP_LOSS_PCT` (`:164`) is only printed (`discord_alerts.py:137`).
  With systemd's `Restart=always` and `RestartSec=30`
  (`deploy/stock-agent.service:14-15`), an import-time `ValueError` means a
  restart loop.
- Why it matters: A typo in `.env` either stops the bot or silently removes
  a cap, and no message says which. Going live is two separate settings plus
  new keys, and the label Josh sees can disagree with where the money is.
  For testing, values are frozen at import, so `tests/conftest.py:46-52` has
  to clean the environment at module level before anything imports `Config`,
  and a test cannot give one object a different setting without patching
  the shared class.
- How verified: Ran `from stock_agent.config import Config` in sub-processes
  with each environment above and printed the parsed values.
- Direction (not a fix): Settings need range checks at start-up, a refusal
  to start when mode and endpoint disagree, and one way of reaching code.

### F-10-06: `.env.example` is the Polymarket bot's file, and one of its old names sets a live risk limit
- Severity: Medium
- Confidence: Confirmed (file contents); the value in force on the droplet is not verified
- Where: `.env.example:1-154`; `stock_agent/config.py:51`; `deploy/setup.sh:34-37`; `README.md:8`, `43`
- What happens: Of the 89 variables in `.env.example`, 78 are read by
  nothing (Polymarket, NBA, "Events Agent", "Intelligence Modules"). Of the
  41 variables the bot does read, 30 are missing from the file (all Discord
  settings, TipRanks, `MAX_POSITION_PCT`, `MAX_CLUSTER_PCT`,
  `PRIORITY_SYMBOLS`, `OPTIONS_ENABLED`, `STOCK_DATA_DIR`). One leftover
  line collides with a current setting.
- Evidence: `.env.example:19` sits in the old "Bankroll" block and reads
  `MAX_TOTAL_EXPOSURE_PCT=0.50`. The stock agent reads that exact name:
  ```python
  # config.py:51
  MAX_TOTAL_EXPOSURE = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", "1.00"))  # 50%
  ```
  Commit `7d4e34a` (2026-04-16) changed the default from `"0.50"` to
  `"1.00"` and left the `# 50%` comment. `deploy/setup.sh:34-36` creates
  `.env` by copying `.env.example`, and the service loads `.env`
  (`deploy/stock-agent.service:10`). Other traps in the file:
  `.env.example:6` `TRADING_MODE=paper` and `:42` `LOG_LEVEL=INFO` look like
  stock-agent settings but are not read (the real names are
  `STOCK_AGENT_MODE` and `STOCK_LOG_LEVEL`).
- Why it matters: If the droplet's `.env` still carries that line, total
  exposure is capped at 50% there while the code, the tripwire test
  (`test_risk_tripwires.py:78`), and `CLAUDE.md` all say 100%. Either way,
  nobody can tell from the repository which limit is in force. A new
  environment variable is supposed to be added to `.env.example`
  (`CLAUDE.md`), but the file gives no usable picture of what the bot needs.
- How verified: Script comparing every `os.environ.get("…")` in
  `stock_agent/` with the names in `.env.example`; ran `Config` with
  `MAX_TOTAL_EXPOSURE_PCT=0.50` and read back `0.5`; read git history of
  the line.
- Direction (not a fix): Replace the file with the variables the bot reads,
  and confirm the droplet's value before anything depends on it.

### F-10-07: The "volatility-adjusted" stop can never engage; every stop is a flat 7%
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1257-1261`; `stock_agent/risk_manager.py:151-164`, `215`; `stock_agent/models.py:58-72`, `96`; `stock_agent/portfolio.py:202`
- What happens: The stop distance is meant to scale with the stock's beta
  and be clamped to 7–12%. No beta exists anywhere in the program, so the
  code always takes the "no beta" branch and uses the 7% floor.
- Evidence:
  ```python
  # scheduler.py:1258-1259
  beta = getattr(signal.thesis, "beta", None)
  stop_loss = self.risk_manager.get_stop_loss_price(price, beta)
  ```
  `Thesis` (`models.py:58-72`) has no `beta` field, `CompanyData`
  (`models.py:24-55`) has none, the LLM prompt does not ask for one
  (`analyst.py:133-145`), and `git log -S'beta'` over `models.py`,
  `analyst.py`, `data_feed.py` returns nothing: it has never existed.
  `getattr(..., None)` hides the missing field from every tool. The same
  pattern is at `risk_manager.py:215`. Related leftovers: the stop stored on
  each position (`Position.stop_loss`, `models.py:96`) is written
  (`scheduler.py:1287`) but never read; imported positions get a stored
  stop of 5% (`portfolio.py:202`) that also is never read; the docstring at
  `risk_manager.py:154-155` still says "clamped to [5%, 10%] … defaults to
  5%".
- Why it matters: `CLAUDE.md` lists "Stop-loss is volatility-adjusted and
  clamped to 7–12%" as a decision already made. What runs is a fixed 7% for
  every stock, so high-volatility names get the tight stop the 2026-04-25
  change was meant to relax. The tripwire test passes because it feeds beta
  values by hand.
- How verified: `'beta' in Thesis.model_fields` is `False`;
  `get_stop_loss_price(100.0, None)` returns `93.0` (with beta 2.0 it would
  be `90.0`). Searched the package for `beta` and for readers of
  `.stop_loss`. The package has 53 `getattr`/`hasattr` calls, 43 of them in
  `scheduler.py` and `discord_alerts.py`, many on the bot's own typed
  objects.
- Direction (not a fix): Either supply a real volatility input or state
  plainly that the stop is flat; a decision for area 02 and for Josh.

### F-10-08: The research-to-order cycle exists in five drifted copies, and the scheduler edits the ledger behind its back
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:361-474`, `478-561`, `628-672`, `738-794`, `1663-1688`, `1227-1292`
- What happens: The weekly cycle was copied to make the mid-week cycle, and
  again, in part, for the event-triggered, daily, and earnings paths. The
  copies no longer do the same things (table under "How a BUY signal is
  created"). Separately, `_execute_signals` carries its own sizing sums and
  changes position records directly instead of asking `Portfolio`.
- Evidence:
  - Weekly and mid-week share about 40 near-identical lines (TipRanks merge
    `scheduler.py:387-400` and `503-517`; screen, analyse, queue). The
    event-triggered copy calls `_deep_analyze(top_symbols, companies)` with
    no TipRanks data (`:1677`) and executes without a broker reconcile
    (`:1680`). `_sync_portfolio_from_alpaca` is called at `:371` and `:491`
    only.
  - The block that builds a `Signal` appears three times (`:653-663`,
    `:770-780`, `:985-995`). Each fills `position_size_pct` with a fallback
    of `0.03`; that field is never read. The fallback for a conviction with
    no tier is written four different ways (`scheduler.py:659` 0.03,
    `scheduler.py:1235-1237` 0.05, `risk_manager.py:45-47` tier 7 else 0.05,
    `risk_manager.py:143-145` tier 7 else 0.03).
  - The material-event block appears twice (`:583-626`, `:1057-1095`). In
    the pre-market copy the closed-market branch only logs "Queuing sell"
    (`:619-622`); nothing is queued.
  - Add-on buys:
    ```python
    # scheduler.py:1273-1278
    existing = self.portfolio.get_position(sym)
    if existing:
        existing.shares += qty
        existing.market_value = existing.current_price * existing.shares
        existing.last_updated = now
        self.portfolio._save()
    ```
    Cash is not reduced (only `Portfolio.add_position` does that,
    `portfolio.py:93-98`), and the entry price is not re-averaged.
- Why it matters: A fix made in one copy does not reach the others, which
  is how the reconcile step and TipRanks went missing from the event path.
  After an add-on the local ledger overstates cash and portfolio value by
  the cost of the add-on until the next broker sync, and later sizing in
  the same batch uses the inflated figure.
- How verified: Read all five paths side by side. Offline run of
  `_execute_signals` with a fake broker and one add-on signal on a made-up
  $100,000 portfolio: shares 20 → 50, cash unchanged at 98,000, portfolio
  value 100,000 → 103,000.
- Direction (not a fix): One implementation of "analyse and queue" with
  parameters, and ledger changes made only through `Portfolio`.

### F-10-09: API clients are copied many times and the copies disagree
- Severity: Medium
- Confidence: Confirmed (code); effect on FMP and Finnhub responses not verified
- Where: listed per item
- What happens: There is no shared HTTP layer. Thirteen places build an
  `httpx` client, plus one synchronous `requests` call. Each service has
  several independent clients with different rules.
- Evidence:
  - **Alpaca, three clients.** `executor.py:31-32` builds URLs from
    `config.ALPACA_BASE_URL`. `options_executor.py:27` and
    `options_data.py:28` hard-code `_TRADING_BASE` to the paper address and
    never read the setting. A fourth, `DataFeed._alpaca_data_get`
    (`data_feed.py:90-103`), is never called.
  - **Finnhub, four clients.** `data_feed.py:55-70` holds a semaphore and
    sleeps 1.1 s per call, citing "60/min". `news_feed.py:28-49`,
    `sentiment.py:27-39`, and `analyst_trends.py:30-42` each open a new
    connection per request with no pacing; `_deep_analyze` launches them
    together (`scheduler.py:890-895`), which at the normal depth of 20
    symbols is 20 + 20 + 40 = 80 simultaneous requests.
  - **FMP, three clients.** `data_feed.py:36-51` (paced),
    `macro_monitor.py:132-169` (not paced), and a one-off in
    `options_portfolio.py:313-322` that reads the key from `os.environ`
    directly. `FMP_BASE` is defined twice and written out a third time.
  - **Same field, different names.** `get_batch_quotes` reads
    `changesPercentage` or `changePercentage` (`data_feed.py:256`);
    `get_stock_quote` (`data_feed.py:238`) and six lines in
    `macro_monitor.py:188-229` read only the first.
  - **Three float parsers.** `data_feed._safe_float("nan")` returns `None`;
    `analyst._safe_float("nan")` and `tipranks_client._to_float("nan")`
    return `nan`.
  - **Dictionary keys that do not match.** `screener.py:125` returns rows
    keyed `composite`; `discord_alerts.py:453-454` looks for `score`,
    `composite_score`, and `name`, so the Discord screener table shows
    `0.0` and a blank name for every row. `portfolio.py:342` returns
    `sharpe`; `discord_alerts.py:342` looks for `sharpe_ratio`, so Sharpe is
    never shown.
  - **Three tests for "is this an option".** `asset_class == "us_option"`
    (`options_executor.py:280`), symbol length over 5
    (`options_executor.py:328`, `portfolio.py:134`), and a letters-prefix
    regex (`scheduler.py:1417`).
  - **Timeouts:** 120, 60, 30 (six times), 20, 15 (four times), 10 seconds.
  - Also copied: `_split_message` / `_split_text`
    (`telegram_alerts.py:176-195`, `discord_alerts.py:662-677`), the atomic
    save (`portfolio.py:44-75`, `options_portfolio.py:85-116`; the macro
    state at `macro_monitor.py:389-393` is not atomic), and portfolio value
    (`portfolio.py:227-230`, `risk_manager.py:38-40`,
    `discord_alerts.py:584`, plus Alpaca's own equity figure in the morning
    brief, `scheduler.py:1720`).
- Why it matters: Switching `ALPACA_BASE_URL` to live would move equity
  orders to the live account while options calls keep hitting the paper
  address with live keys and fail, and those failures then read as "no
  positions" (F-10-04). Any change to retries, pacing, or error handling
  has to be made in up to four places.
- How verified: Listed every client construction and base-URL constant with
  `grep`; ran the three float parsers on the same inputs; called
  `send_screener_results` with rows shaped as the screener prompt defines
  them and a fake `send_embed`, and read the table it built.
- Direction (not a fix): One client per external service, one definition of
  each record passed between modules.

### F-10-10: The README, comments, and docstrings describe a bot that no longer exists
- Severity: Medium
- Confidence: Confirmed
- Where: listed per row
- What happens: Statements a reader would rely on are false today. The ones
  that matter:

  | Claim | Where | Reality |
  | --- | --- | --- |
  | "live mode flips a single config flag" | `README.md:17` | `MODE` is a label (F-10-05); live needs a new URL and keys, and options stay on paper (F-10-09) |
  | `cp .env.example .env` … `python -m stock_agent` | `README.md:8-10` | Nothing loads `.env` (no `dotenv` call anywhere); only the systemd unit does. Run this way the bot starts with empty keys |
  | "Perplexity + Anthropic" | `README.md:25` | No Anthropic call exists; screening uses OpenAI (`screener.py:37`) |
  | "Options data — IV, skew, flow" | `README.md:24` | The engine uses price, strike, and expiry only; greeks are stored for display |
  | "news/social sentiment" | `README.md:22`, `sentiment.py:1-9` | Only `/news-sentiment` is fetched |
  | "portfolio rebalance" on Sunday | `README.md:15` | No trimming or rebalancing code exists |
  | Cadence lists Sunday and Wednesday only | `README.md:13-17`, `56` | Also daily pre-market, earnings, options, macro, morning brief, 30-minute monitoring, and event-triggered full scans |
  | `POSITION_SIZE_PCT_BY_CONVICTION` | `README.md:48` | The name is `CONVICTION_SIZE_MAP` |
  | "belt-and-suspenders with Alpaca bracket orders" | `scheduler.py:1046` | No bracket orders (`executor.py:85-88`) |
  | "Stop sell (GTC) — placed after fill confirmed" | `executor.py:88` | It sleeps 3 seconds and never checks the fill (`executor.py:114-115`) |
  | "a soft-block so a single new full-size buy can't slam into the ceiling" | `risk_manager.py:90-98` | The outer `if` does nothing; only `sector_pct >= cap` blocks. A sector at 39% accepts a new 10% position |
  | "Queuing sell for %s at market open" | `scheduler.py:619-622` | Nothing is queued |
  | "Widening scan to 80 stocks" | `macro_monitor.py:470`, `338` | `scan_depth_override` is only displayed (`scheduler.py:1867`); screening always uses `config.UNIVERSE_SIZE` (`data_feed.py:291,330`) |
  | "If the stock drops 5%, the bot will automatically sell" | `discord_alerts.py:550` | 7% |
  | "Starting Capital" on the start-up post | `discord_alerts.py:134` | Shows current cash |
  | "Max 5 open options positions", "Max 5% … per trade", covered calls "(future use)", "Auto-close at 50% profit or 3 DTE (enforced…)", "Minimum 2:1 reward-to-risk" | `options_engine.py:10-20`, `509-511`; `options_portfolio.py:10-12` | 10 positions, 7%, covered calls are live, auto-close is off (`scheduler.py:1526-1529`), 1.5 |
  | "env vars still win via config" | `tipranks_client.py:17` | The module never reads `Config` |
  | "every other monitoring cycle" | `scheduler.py:1104` | Every cycle in even-numbered hours |
  | Macro "7-8 AM", options "9:00-9:30 … before market open" | `scheduler.py:156`, `181` | 6:30–7:30 (`:222`) and 9:45–14:00 (`:236`) |
  | "Run TipRanks scraper" | `scheduler.py:373` | The scraper was deleted in `bfabf49`; it is an API call |

  `CLAUDE.md` itself repeats three of these as settled facts: the 7–12%
  volatility stop (F-10-07), "Minimum conviction is 7" (6 in two macro
  regimes, `macro_monitor.py:337,342`), and reconciling "before risk checks"
  (two of five buy paths, F-10-08).
- Evidence: the table above; each row cites the line making the claim and
  the line that contradicts it. No Anthropic, `dotenv`, or rebalancing code
  is found by searching `stock_agent/`.
- Why it matters: Josh reads these to understand his bot, and every AI
  session reads them as ground truth before changing code. The ELI5 Discord
  post tells readers a safety net that is not the one in force.
- How verified: Each row checked against the cited code. The sector row was
  run: five made-up holdings totalling 39% in one sector, new conviction-10
  buy in the same sector, `can_open_position` returned `(True, 'OK')`.
- Direction (not a fix): Rewrite the README from this audit once behaviour
  is settled; delete comments rather than maintain them where code is clear.

### F-10-11: Dependencies are unpinned, two are unused, and one line sets the Python floor
- Severity: Medium
- Confidence: Confirmed (repository); server versions not verified
- Where: `requirements.txt:1-5`; `deploy/auto_update.sh:14`; `deploy/setup.sh:7,23-27`; `stock_agent/macro_monitor.py:456`; `stock_agent/tipranks_client.py:54-59`
- What happens: All five requirements are lower bounds with no upper limit
  and no lock file. Two are not imported anywhere. The code needs Python
  3.12 because of a single f-string.
- Evidence:
  - `requirements.txt`: `httpx>=0.24`, `requests>=2.31`, `pydantic>=2.0`,
    `python-dotenv>=1.0`, `aiohttp>=3.9.0`. A search of every `.py`, `.sh`,
    and service file finds no `aiohttp`, `dotenv`, or `load_dotenv`.
  - No imports are missing: the only third-party imports are `httpx`,
    `pydantic`, `requests`.
  - `pip install -r requirements.txt` without `--upgrade`
    (`auto_update.sh:14`) leaves installed versions alone, so the droplet
    runs whatever was current when it was first set up; a rebuilt droplet
    and every CI run install the newest releases instead.
  - `macro_monitor.py:456` puts a backslash escape inside an f-string
    expression (`{'⚠ Yields elevated' if … }`). Parsing every module
    with Python 3.11, 3.12, and 3.13 gives exactly one error, under 3.11, at
    that line. Without it the floor would be 3.10 (`X | None` in evaluated
    signatures such as `executor.py:34`).
  - No file declares the Python version. `setup.sh:7` installs the
    distribution's `python3`.
  - `datetime.utcnow()` (`options_models.py:175`, `234`;
    `options_portfolio.py:160`) is deprecated from 3.12.
  - `requests.get(..., timeout=15)` (`tipranks_client.py:54-59`) is a
    blocking call made from inside the async loop
    (`scheduler.py:825-838`); the whole bot pauses for up to 15 seconds.
    Harmless today because the loop does one thing at a time.
- Why it matters: The versions tested in CI are not the versions running on
  the droplet, and rebuilding the server changes them with no code change.
  A major release of `pydantic` or `httpx` would arrive unannounced.
- How verified: Import scan with `ast`; `ast.parse` of all 24 files under
  three interpreters; read deploy scripts.
- Direction (not a fix): Pin exact versions from the droplet's
  `pip freeze`, drop unused packages, and declare the Python version.

### F-10-12: About 5% of the package is dead code
- Severity: Low
- Confidence: Confirmed
- Where: listed per item
- What happens: 27 functions (367 lines) are referenced nowhere in
  `stock_agent/`, `tests/`, or `scripts/`. Roughly 110 more lines are
  reachable only from those.
- Evidence (name-reference scan with `ast`, then each checked by `grep`):
  - **Broker client, half unused.** `Executor.cancel_order`, `get_order`,
    `get_open_orders`, `cancel_all_orders` (`executor.py:163-188`). The bot
    never looks up an order's status and never cancels one.
  - **Options close machinery, all unused.**
    `OptionsExecutor.close_option_position`, `_close_option_position_legacy`,
    `close_spread_position`, `get_option_position`, `get_open_orders`,
    `cancel_order`, `exercise_option` (`options_executor.py:182-441`);
    `OptionsPortfolio.close_position`, `get_auto_close_candidates`,
    `get_near_expiry_positions`, `format_portfolio_summary_embed`,
    `get_portfolio_greeks_summary`, `get_strategy_breakdown`,
    `find_position_by_contract`, properties `open_positions` and
    `closed_positions` (`options_portfolio.py:187-237`, `603-619`,
    `659-678`, `750-814`); `OptionsPosition.should_auto_close`,
    `close_trigger_reason` (`options_models.py:178-196`); constants
    `PROFIT_TAKE_PCT`, `LOSS_ALERT_PCT` (`options_portfolio.py:44-45`).
  - **Data feed, half unused.** `get_earnings_calendar`,
    `get_company_news`, `get_analyst_recommendations`, `get_sec_filings`,
    `_alpaca_data_get` (`data_feed.py:90-103`, `264-269`, `346-363`).
    `get_historical_prices` (`data_feed.py:420-432`) is reachable only
    through `getattr(self.data, "data_feed", None)` at
    `options_portfolio.py:298`, and `OptionsDataFeed` has no such attribute,
    so that branch never runs.
  - **TipRanks scraper leftovers.** `TipRanksStock` (`models.py:6-21`) and
    the "legacy list" branch in `_deep_analyze` (`scheduler.py:865-883`);
    `_load_tipranks_data` always returns a dict.
  - **Unreachable start-up block.** `scheduler.py:149-154` runs "pending
    signals from pre-market scan" on the first tick, but `_pending_signals`
    is created empty (`:95`) and nothing fills it before the first tick.
  - **Fields written, never read.** `Signal.position_size_pct`
    (`models.py:82`), `Position.stop_loss` (`models.py:96`),
    `PortfolioState.last_daily_check` and `last_universe_refresh`
    (`models.py:139,143`), `DailySummary.signals` (always `[]`,
    `scheduler.py:1151`).
  - **Others.** `Portfolio.get_sector_exposure`, `get_equity_curve`
    (`portfolio.py:251-273`); `DiscordReporter.send_macro_update`
    (`discord_alerts.py:498-505`); `news_feed.count_headlines`
    (`news_feed.py:103-104`); `OptionsDataFeed.get_option_quote`
    (`options_data.py:317-327`); unused parameters `min_market_cap` and
    `sectors` of `screen_universe` (`data_feed.py:271-275`).
- Why it matters: Mostly clutter. It also misleads: a reader sees
  `cancel_order`, `close_position`, and `should_auto_close` and assumes the
  bot cancels stops and closes options. It does neither.
- How verified: Script listing every function whose name is never
  referenced; second-order cases followed by hand.
- Direction (not a fix): Delete in a behaviour-neutral pull request after
  the money-path tests exist, so the deletion can be checked.

### F-10-13: Lint is set to the minimum, no type checker runs, and the type checker finds real bugs
- Severity: Low
- Confidence: Confirmed
- Where: `ruff.toml:1-10`; `requirements-dev.txt`; `.github/workflows/ci.yml:37-44`; whole package
- What happens: The configured lint run is clean because it checks almost
  nothing. Wider checks report hundreds of items, most cosmetic, a few real.
- Evidence (ruff 0.17.6, mypy 2.3.1, Python 3.13; counts by rule):

  | Check | Result |
  | --- | --- |
  | `ruff check stock_agent` (project config: `E9,F63,F7,F82`) | 0 |
  | Pyflakes only (`--select F`) | 29: `F401` unused import 19, `F841` unused variable 6, `F541` f-string without placeholders 3, `F811` redefinition 1 |
  | Ruff's own defaults (`--isolated`, py312) | 340: `UP045` 139, `BLE001` blind except 83, `S110` try/except/pass 20, `F401` 19, `UP017` 17, `TRY401` 12, `I001` 9, `UP037` 6, `F841` 6, `RUF012` 4, fifteen other rules 25 |
  | Line length over 120 (`E501`) | 42 |
  | `ruff format --check` | 21 of 24 files would be reformatted |
  | `mypy stock_agent --ignore-missing-imports` | 20 errors in 8 files |
  | same with `--check-untyped-defs` | 24 errors in 8 files |
  | same with `--strict` | 265 errors in 21 files (116 are bare `dict`/`list`) |

  Type hints: 200 of 277 functions (72%) are fully annotated; the gaps are
  `discord_alerts.py` (5 of 23), `telegram_alerts.py` (3 of 11),
  `scheduler.py` (17 of 37), `portfolio.py` (10 of 21).
  Real defects in the mypy output: `scheduler.py:1349` (F-10-02);
  `scheduler.py:1478` "expression has type dict | None, variable has type
  tuple[...]", which points at `if order:` on `scheduler.py:1486` being
  always true for a spread because `place_spread_order` returns a tuple
  (`options_executor.py:124`, `178`) and `bool((None, None))` is `True`;
  `options_engine.py:487,493` multiplying a possibly-`None` price.
  Clutter: ten imports inside functions (`executor.py:114`,
  `scheduler.py:1414`, `1896`, `portfolio.py:129`, `315`,
  `data_feed.py:373`, `options_data.py:368`, `options_portfolio.py:300`,
  `314`, `833`) and four `__import__("datetime")` expressions
  (`macro_monitor.py:232`, `options_portfolio.py:160`, `288`, `827`).
- Why it matters: The cheapest available check would have caught the sell
  bug on the day it was written.
- How verified: Ran each command above with caches outside the repository.
- Direction (not a fix): Decide which rule set and which type-check level
  gate pull requests; expect one clean-up pull request that changes no
  behaviour.

### F-10-14: What could be lifted out of `scheduler.py` without changing behaviour, and in what order
- Severity: Note
- Confidence: Likely (reading only; nothing was moved)
- Where: `stock_agent/scheduler.py` (line ranges in the job table above)
- What happens: Not a defect. Ordered from least to most risk:
  1. **Schedule rules** (`212-320`, `1103-1108`). Thirteen small methods
     that take the current time and return yes or no. No I/O. They read nine
     flags on `self`. Testable today with fixed datetimes.
  2. **Morning brief and embed formatting** (`1700-1935`, plus the embeds at
     `340-351`, `1555-1569`, `1646-1661`). 236 lines that turn an account
     dict, a position list, and a macro snapshot into Discord text. One side
     effect to keep: it also syncs the ledger (`1732-1733`).
  3. **Daily-summary arithmetic** (`1118-1152`). Pure sums over ledger
     state.
  4. **Repeated blocks inside the research cycles**: the TipRanks merge
     (twice), the `Signal` builder (three times), the material-event handler
     (twice). Same-file helpers first.
  5. **Add-on sizing** (`1227-1250`). Pure arithmetic that belongs beside
     `calculate_position_size`; needs tests first because it decides order
     size.
  6. **Options orchestration** (`1374-1574`). Touches the equity ledger only
     to read share counts, the universe, and stored theses.
  7. **Equity execution** (`1173-1370`). Last, and only behind fake-broker
     tests.
- Evidence: job table and method sizes measured with `ast`; largest methods
  are `_send_morning_brief` 236 lines, `_deep_analyze` 149,
  `_run_options_scan` 148, `_execute_signals` 142, `_run_daily_prescan` 122
  (nesting depth 9).
- Why it matters: Gives the plan a safe sequence.
- How verified: Read the file in full; measured sizes and nesting.
- Direction (not a fix): None; context for the plan.

### F-10-15: What can be tested today, what cannot, and where the seams are
- Severity: Note
- Confidence: Confirmed
- Where: whole package
- What happens: More is testable than the test suite suggests.
  - **Testable now with no network and no fakes:** all of `risk_manager.py`;
    the thirteen schedule checks; `analyst._extract_json`, `_parse_thesis`,
    `_format_financial_data`; everything in `tipranks_client.py` except
    `_call_screener`; the normalisers, formatters, and conviction adjusters
    in `sentiment.py`, `analyst_trends.py`, `news_feed.py`; all of
    `research_notes.py` (`load_notes` takes a directory);
    `MacroMonitor._compute_regime` and `format_discord_brief`;
    `OptionsPortfolio._leg_intrinsic_value`, `_expiry_from_contract_symbol`,
    `open_position`, `close_position`, `_recalculate_summary`;
    `OptionsDataFeed._parse_snapshot`; `Portfolio` arithmetic and
    `sync_from_alpaca` (dictionaries in, ledger out; needs a temp data
    directory).
  - **Testable now with simple fakes:** a `StockAgentScheduler` can be
    constructed offline (no constructor touches the network; HTTP clients
    are created lazily) and its collaborators replaced by assignment. I did
    this for `_execute_sell` and `_execute_signals`. `OptionsEngine` and
    `OptionsPortfolio` already take their data feed as a constructor
    argument (`options_engine.py:91-99`, `options_portfolio.py:59-63`).
  - **Not testable without patching internals,** because fetching and
    deciding share one function: `DataFeed.get_company_fundamentals`
    (`data_feed.py:107-227`, ten HTTP calls and the field mapping together);
    `DataFeed.screen_universe` (`271-344`); `Executor.place_buy`
    (`executor.py:82-134`, builds its own client and sleeps 3 s);
    `OptionsPortfolio.sync_from_alpaca` when a position has expired
    (`options_portfolio.py:463-484` calls FMP inline); the module-level
    Finnhub functions and `tipranks_client._call_screener`, which create
    their own clients.
  - **The five functions where a test buys the most safety:**
    1. `StockAgentScheduler._execute_signals` (`scheduler.py:1173-1314`):
       the only route from a signal to a buy order.
    2. `StockAgentScheduler._execute_sell` (`scheduler.py:1316-1370`): every
       exit; broken today.
    3. `Portfolio.sync_from_alpaca` with its wrapper
       (`portfolio.py:120-225`, `scheduler.py:808-823`): decides what the
       bot believes it owns.
    4. `OptionsPortfolio.sync_from_alpaca` with the order-recording block
       (`options_portfolio.py:389-551`, `scheduler.py:1464-1510`).
    5. `Executor.place_buy` (`executor.py:82-134`): buy, wait, stop, and
       what is returned when the stop fails.
  - **Seams that exist or are missing:**
    - *Broker.* The scheduler uses eight methods across two classes:
      `get_account`, `get_positions`, `place_buy`, `place_sell`
      (`Executor`), and `get_option_positions`, `get_option_activities`,
      `place_option_order`, `place_spread_order` (`OptionsExecutor`). That
      list is the broker interface in practice. It is not written down, the
      scheduler constructs both classes itself (`scheduler.py:80,86`), and
      the return types cannot express failure (F-10-04) or a fill.
    - *Clock.* The scheduler funnels 26 reads through `_now_et` and
      `_now_utc` (`scheduler.py:64-69`) and passes the time into its
      checks. The other 30 reads in 12 modules call the system clock
      directly, with three meanings of "today": Eastern
      (`scheduler.py`), UTC (`options_models.py:175`, ledger timestamps),
      and the server's local zone (`date.today()` ten times in
      `options_engine.py`, `data_feed.py:183,375-376`,
      `research_notes.py:56`).
    - *Data provider.* Five `DataFeed` methods are used
      (`get_company_fundamentals` nine times, `screen_universe` four,
      `get_batch_quotes`, `get_recent_earnings`, `get_stock_quote` twice
      each). The Finnhub and TipRanks helpers are module functions imported
      by name into the scheduler (`scheduler.py:29-46`), so a test must
      patch `stock_agent.scheduler.<name>`.
    - *LLM.* `Analyst` (three methods) and `Screener` (one). The JSON
      parsing is already separate from the HTTP call.
    - *Notifier.* Two classes, about twenty methods, called about seventy
      times from the scheduler.
    - *State.* Ledgers load in their constructors and save on almost every
      change to `Config.DATA_DIR`; the `STOCK_DATA_DIR` variable
      (`config.py:186`) is the one seam and `tests/conftest.py:52` uses it.
- Evidence: the offline runs described in F-10-02, F-10-04, and F-10-08.
- Why it matters: The first money-path tests need no restructuring, only
  fakes. Restructuring can follow the tests rather than precede them.
- How verified: Constructed the scheduler with a cleaned environment and a
  temporary data directory; listed every collaborator method it calls.
- Direction (not a fix): None; context for the plan.

## Checked and fine

- No import cycles anywhere in the package (`ast` import graph).
- Every module imports with no credentials set
  (`tests/test_smoke_imports.py`, 24 tests pass).
- No constructor touches the network; all nine cached HTTP clients are
  created on first use (`_get_client` in each class).
- No third-party import is missing from `requirements.txt`.
- Lead "f-string that needs 3.12": true, and it is the only version-specific
  syntax (`macro_monitor.py:456`). The code parses cleanly on 3.12 and 3.13.
- Lead "inline imports": ten found (F-10-13). All import the standard
  library or a module already loaded; none works around a cycle. Harmless.
- Lead "`getattr(self.config, …, default)` defaults differ": ten such calls.
  One default differs from `config.py` (`risk_manager.py:103` uses `0.45`,
  `config.py:57` is `0.48`). Harmless at run time because the attribute
  always exists, so the default is never used.
- Lead "requests, aiohttp, and httpx": `httpx` is the working client,
  `requests` is used in one module, `aiohttp` in none (F-10-11).
- Lead "Polymarket leftovers": the server path `/root/polymarket-bot`
  (`config.py:185`, `deploy/auto_update.sh:2`, `deploy/setup.sh:11-19,43`,
  `deploy/stock-agent.service:9-13`, `scripts/vps_snapshot.sh:24`), the
  repository name in `deploy/setup.sh:12`, `dashboard/data/` in
  `.gitignore:9`, `deploy/deploy_log.txt:1` ("Dashboard overhaul"), and 83
  lines of `.env.example` (F-10-06). No Polymarket code remains in
  `stock_agent/`.
- The atomic save in `portfolio.py:44-75` and `options_portfolio.py:85-116`
  writes a temp file and renames it; a failed save raises rather than being
  swallowed.
- `tests/conftest.py` cleans the environment before `Config` is imported and
  points state at a throwaway directory, which is the right approach given
  import-time settings.
- `README.md:39` (deploy script installs the unit and a 10-minute cron) and
  the architecture list `README.md:53-76` are accurate, apart from omitting
  `options_models.py` and `research_notes.py`.
- `MIN_CONVICTION`, `CONVICTION_SIZE_MAP`, the position, sector, and cluster
  caps, and the stop range are each defined once, in `config.py`.

## Could not verify

- **Python and package versions on the droplet.** Needed: the snapshot's
  `deploy/python-version.txt` and `deploy/pip-freeze.txt`
  (`scripts/vps_snapshot.sh:114-118`).
- **Which settings the droplet's `.env` contains.** Needed: variable names,
  and the non-secret values of `MAX_TOTAL_EXPOSURE_PCT`, `OPTIONS_ENABLED`,
  `STOCK_AGENT_MODE`, `TRADING_MODE`, `MAX_POSITION_PCT`, `MAX_CLUSTER_PCT`,
  from the snapshot's redacted environment listing (F-10-06).
- **Whether production shows F-10-02.** Needed: service logs from the
  snapshot, searched for "Sell execution failed" and "_tipranks_lookup".
- **Whether FMP returns `changePercentage` or `changesPercentage`, and
  whether Finnhub rejects the 80-request burst** (F-10-09). Needed: logs, or
  one sample response per endpoint supplied by Josh.
- **The 3.10 floor without the f-string line.** No 3.10 interpreter was
  available; that part is reasoning from the syntax used.
- **Whether CI has run and passed on GitHub.** Needed: the Actions history.

## Questions for Josh

1. Did you ever set `OPTIONS_ENABLED=false` (or expect options to be off)?
   It has no effect today.
2. Is options trading meant to stay on while the revamp is under way?
3. Have you seen Telegram messages saying "Sell failed … _tipranks_lookup"?
   That would confirm F-10-02 on the live bot.
4. When the bot goes live, should one setting select both the label and the
   broker address, with a refusal to start if they disagree?
5. Should total exposure be capped at 100% (code default) or 50% (the old
   `.env.example` value)?
6. Should a failing test or type check be able to block a deploy, or only
   warn?
7. Are you willing to take one clean-up pull request that deletes dead code
   and changes no behaviour, once tests exist?
8. Do you still read Telegram, or is Discord the one that matters? Every
   alert is written twice.
9. Should the README be rewritten now or at the end of the revamp? A
   documentation-only merge still restarts the bot.

## Handoffs

- Area 09: `.env.example:2-3` holds what looks like a real Telegram bot token and chat ID in a public repository; treat as exposed until rotated.
- Area 09: TipRanks API key and token are hard-coded as defaults in `stock_agent/config.py:194-195` and again in `stock_agent/tipranks_client.py:18-19`.
- Area 09: `stock_agent/config.py:157-161` (comment) contains specific past trade prices and losses, and `.env.example:16` an old bankroll figure; Discord server and channel IDs are hard-coded at `config.py:22-40`.
- Area 09: the droplet deploys `master` without waiting for CI (`.github/workflows/ci.yml:4-5`, `deploy/auto_update.sh:11-18`); requirements are unpinned (F-10-11).
- Area 03: `Executor.get_positions` returns `[]` on failure and `_sync_portfolio_from_alpaca` skips only when the account call also failed, so one failed positions request drops every local position (`executor.py:75-80`, `scheduler.py:816-821`, `portfolio.py:147-156`).
- Area 03: `cancel_order`, `get_order`, `get_open_orders`, `cancel_all_orders` are never called: fills are never checked and the GTC stop is never cancelled after a sell (`executor.py:163-188`, `scheduler.py:1316-1346`).
- Area 03: each add-on buy places a second stop order for the added shares only (`scheduler.py:1264`, `executor.py:117-127`).
- Area 04: a failed `get_option_positions` closes every open option in the ledger (F-10-04, demonstrated).
- Area 04: `if order:` at `scheduler.py:1486` is always true for spreads because `place_spread_order` returns a tuple; a spread whose legs both failed is recorded as open.
- Area 04: options limits marked "paper trading" are plain constants with no link to mode (`options_engine.py:52-55`, `195-196`, `216`).
- Area 04: the recorded options entry price is the signal's estimate, not the fill (`scheduler.py:1487-1489`).
- Area 02: no beta exists, so the stop is a flat 7% (F-10-07).
- Area 02: the sector "soft block" does nothing (`risk_manager.py:90-98`); add-ons return before the exposure, sector, cluster, and cash checks (`risk_manager.py:51-63`).
- Area 02: effective minimum conviction is 6 in two macro regimes and such buys are sized as tier 7 (`macro_monitor.py:337,342`, `risk_manager.py:143-145`); `MIN_CONVICTION` is not checked at execution.
- Area 01: the pre-market "Queuing sell" branch queues nothing (`scheduler.py:612-622`).
- Area 01: `_pending_signals` lives in memory only and every deploy restarts the process (`scheduler.py:95`, `149-154`).
- Area 01: an event-triggered full scan runs inline in the monitoring branch and delays the next stop check (`scheduler.py:199-205`, `1663-1688`).
- Area 07: add-on buys do not reduce local cash (`scheduler.py:1271-1278`, demonstrated in F-10-08).
- Area 07: local portfolio value excludes options while synced cash includes option premiums (`portfolio.py:131-138`, `227-230`); `starting_capital` is a hard-coded default (`models.py:137-138`); macro state is saved non-atomically (`macro_monitor.py:389-393`).
- Area 08: sell alerts are never sent (F-10-02); the Discord screener table shows 0.0 for every score and Sharpe is never shown (F-10-09); three footers hard-code "Paper Mode" (`discord_alerts.py:197,243,311`).
- Area 08: six failure messages are logged at debug level and the log level is fixed at INFO (`__main__.py:10`, `config.py:214`).
- Area 06: the field named `forward_pe` is filled from a PEG-ratio key and three `*_ttm` fields from per-share keys (`data_feed.py:201`, `215-217`), then shown to the LLM as "Forward P/E" (`analyst.py:74`).
- Area 06: `changesPercentage` versus `changePercentage` (`data_feed.py:238,256`, `macro_monitor.py:188-229`); 80 simultaneous unpaced Finnhub requests (`scheduler.py:890-895`).
- Area 06: `get_company_fundamentals` (ten HTTP calls) is called only to read a sector (`scheduler.py:1195`) or a price (`scheduler.py:1431`, up to 30 symbols per options scan).
- Area 05: with `TIPRANKS_MIN_SMART_SCORE=8` the TipRanks adjustment can only raise conviction; the penalty branches are unreachable (`tipranks_client.py:302-306`, `365-374`).
- Area 05: the daily and earnings paths skip all enrichment (`scheduler.py:646-649`, `765-768`); LLM `severity` is compared as a number without parsing (`scheduler.py:606`, `1081`).
