# 07. State and accounting

Audited commit: 941f2ca    Date: 2026-11-20

No droplet snapshot was available. Everything below is what the code allows,
proven by reading the code end to end and by running the ledger classes with
made-up inputs in a throwaway data directory (no network, no real account
data). All dollar figures in this file are invented test inputs.

## Plain-language summary

The bot's memory is four JSON files. The main one, `portfolio.json`, holds the
stock positions, cash, every trade, a daily summary per trading day, and the
research theses. A second file holds the options positions.

The file writing itself is mostly careful (write a temporary file, then swap it
in). The problems are in what happens around it:

1. **If a state file cannot be read, the bot silently starts over with an
   empty book and $100,000 of imaginary cash**, does not check the broker at
   startup, and overwrites the unreadable file and its backup within two
   saves. Any future change to the data models that adds a required field
   would trigger this on the next deploy.
2. **One failed "list my positions" call to Alpaca is treated as "you hold
   nothing".** For options this permanently empties the options ledger and
   books invented profits and losses; nothing ever re-imports those
   positions. For stocks it throws away each position's thesis and purchase
   date.
3. **Buy decisions made on Sunday or Wednesday evening live only in memory.**
   Any restart before the next market open (every merge to `master` causes
   one) discards them without a trace.
4. **The performance numbers are the bot's own guesses, not the broker's
   records.** Trades are logged at a quoted price rather than the real fill,
   exits done by the broker's stop orders are never logged at all, options
   are left out of portfolio value, and three different reports compute
   "total P&L" three different ways.
5. `portfolio.json` grows by roughly 10–20 MB per year because every daily
   summary carries a full copy of every position and its thesis, and the whole
   file is rewritten thirty or more times a day.

Backups are one previous copy on the same disk. Stock positions and cash can
be rebuilt from Alpaca automatically; trade history, the equity curve and the
whole options ledger cannot.

## How it actually works

### Q1. Files the bot writes

All under `Config.DATA_DIR` (`stock_agent/config.py:184-191`; on the droplet
`data/stock_agent/`, git-ignored).

| File | Contents | Written when | Growth per year |
| --- | --- | --- | --- |
| `portfolio.json` | `PortfolioState` (`models.py:132-143`): positions with embedded thesis, all trades, all daily summaries (each embedding all positions again), cash, `starting_capital`, universe, `active_theses`, three timestamps | Every `Portfolio._save()` (`portfolio.py:44-75`), called from 9 setters plus `scheduler.py:1278` and `:1967`. Quiet trading day with positions: about 31 full rewrites (14 monitoring runs x 2, pre-scan, morning sync, daily summary). A weekly analysis adds about 45 (one per analysed symbol at `scheduler.py:964`, one per held position at `:1019`). | Unbounded. Measured with synthetic data: 3.4 KB per held position per trading day, so about 10 MB at 10 positions, 20 MB at 20 positions. See F-07-10. |
| `portfolio.json.bak` | Copy of the previous `portfolio.json` | Every save, before the swap (`portfolio.py:58-63`) | Same as above |
| `portfolio_*.tmp` | Temp file for the swap | Every save; removed on error, left behind if the process is killed mid-save | Normally none |
| `options_portfolio.json`, `.bak`, `options_portfolio_*.tmp` | `OptionsPortfolioState` (`options_models.py:244-254`): open and all closed option positions, totals | `OptionsPortfolio._save()` (`options_portfolio.py:85-116`): on open, on reconcile and on price refresh, so twice per 30 minutes in market hours while positions exist | About 1 KB per position ever opened; small |
| `macro_state.json` | Latest `MacroSnapshot` | Every `check_conditions()` (`macro_monitor.py:251-254`): daily plus about 14 times a day from `scheduler.py:1600` | Constant, about 1 KB |
| `macro_history.json` | Last 90 snapshots (`macro_monitor.py:407-418`) | Same | Capped, about 70–150 KB. Nothing reads this file. |
| `research_notes/*.md` | Hand-written notes | Read only (`research_notes.py:100-130`) | n/a |

Logs go to stdout and so to journald (`__main__.py:9-12`,
`deploy/stock-agent.service`). The bot writes no log files itself.

### Q2. Atomicity, crashes, startup

Save (`portfolio.py:44-75`, same code in `options_portfolio.py:85-116`):
serialise the whole state, `mkstemp` in the same directory, one `os.write`,
close, copy the current file to `.bak` with `write_text`, `os.replace`. The
swap is atomic. Weak points: the `os.write` return value is ignored, there is
no `fsync`, the `.bak` copy is not atomic and its errors are swallowed, and a
failure re-raises into the caller. Macro files use plain `write_text`
(`macro_monitor.py:393`, `:416`), not atomic.

Load (`portfolio.py:25-42`):

| On disk at startup | Result (demonstrated) |
| --- | --- |
| Main file missing, `.bak` present | Fresh state. `.bak` is not tried: the fallback sits inside `if self._state_path.exists()`. |
| Main unreadable, `.bak` good | `.bak` loaded (one save old). ERROR log only. |
| Main unreadable, `.bak` unreadable or missing | Fresh state: no positions, no history, cash 100000. INFO log "Starting with fresh portfolio state". |
| Any single record fails validation | Whole file counts as unreadable. |

The bot never stops on a bad file and never alerts. There is no reconcile with
Alpaca at startup (`scheduler.py:109-144`). See F-07-01.

### Q3. Schema version and migration

None. Loading is `PortfolioState.model_validate_json(raw)`. Demonstrated with
pydantic 2.13: a new field with a default loads old files; a removed field is
silently dropped on the next save; a new required field, a changed type, or an
unknown enum value makes the whole file unreadable and leads to the fresh-start
path above.

### Q4. Local state versus broker

The stock ledger is reconciled in exactly three places: Sunday analysis
(`scheduler.py:371`), Wednesday analysis (`:491`), and the weekday morning
brief (`:1733`, only when the account call succeeded and the positions list is
non-empty). Not at startup, not before `_execute_signals`, not in the
30-minute monitoring, not before the daily summary.

| Item | Local value between syncs | At sync |
| --- | --- | --- |
| Shares | Order quantity, assumed fully filled (`scheduler.py:1275`, `:1282`) | Broker wins (`portfolio.py:163`) |
| Entry price | The price seen at analysis time, possibly days old (`scheduler.py:990`, `:1283`) | Broker average cost wins (`:164`) |
| Cash | 100000 default, minus price x qty on a new buy, unchanged on a top-up, plus quote x qty on a sell | Broker wins if the account call worked (`:215-219`) |
| Position exists | Added or removed when the order request is accepted, not when it fills | Dropped if absent at broker, imported if present |
| Thesis, entry date, sector, `stop_loss` | Local only | Survive only while the symbol is continuously present on both sides. A drop and re-import replaces them with a placeholder (`:182-206`). |
| Trade history, daily summaries | Local only | Never reconciled |
| Options positions | Created when the order call returns anything truthy, at an estimated entry price (`scheduler.py:1486-1496`) | Only ever closed when missing at broker; never imported, never corrected (`options_portfolio.py:389-551`) |

Typical disagreement window is one day. It is longer when the morning sync is
skipped (account call fails, account truly holds nothing, or the 08:15–08:45
window is missed), and after any restart it lasts until the next scheduled
sync.

### Q5. Formulas

| Number | Formula and source | Broker or bot |
| --- | --- | --- |
| Cash | `state.cash` | Broker at sync, bot arithmetic in between |
| Portfolio value | `cash + sum(current_price * shares)` over stock positions only (`portfolio.py:227-230`) | Bot. Excludes options (F-07-08). |
| Exposure | stock market value / portfolio value (`portfolio.py:243-249`) | Bot |
| Unrealised P&L | `sum((current - entry) * shares)` (`portfolio.py:232-241`) | Bot, on broker-synced inputs |
| Realised P&L, win rate | Sum and count over local SELL trades with a `pnl` (`portfolio.py:277`, `:300-301`, `:334`) | Bot. Misses every exit the bot did not place itself (F-07-06). |
| Total return | `portfolio value - starting_capital` (`portfolio.py:282-283`, `scheduler.py:1128`) | Bot; `starting_capital` is a fixed 100000 |
| Day P&L | `portfolio value - previous summary's value` (`scheduler.py:1122-1127`) | Bot |
| Equity curve, drawdown, Sharpe | From `daily_summaries[].portfolio_value` (`portfolio.py:305-329`) | Bot |
| Morning brief value and P&L | Alpaca `equity`, `last_equity` (`scheduler.py:1720-1729`) | Broker (minus the fixed 100000) |

### Q6. Starting capital, options, dividends, fees

`starting_capital` is the model default 100000 (`models.py:138`) and is never
assigned anywhere. Deposits and withdrawals are not tracked. Stats ignore the
options ledger entirely. Dividends and fees reach `cash` through the sync, so
they affect portfolio value and total return, but not realised P&L or win rate.

### Q7. Partial sells and top-ups

The bot never sells part of a position. A reduction made at the broker is
absorbed by the sync with no trade record. A top-up adds shares locally
without debiting cash or re-averaging the entry price (F-07-07); the next sync
replaces the entry price with the broker's average cost, after which a sell
logs `(quote - average cost) * shares`.

### Q8. Time zones

Every datetime the current code persists is timezone-aware UTC and survives a
round trip (checked). Mixed handling exists only in comparisons: two report
filters compare a UTC date string with an Eastern date string, and options
day counts use the UTC date (F-07-13).

### Q9. State held only in memory

`_pending_signals` and all the once-per-day flags (`scheduler.py:89-103`).
Broker order IDs are logged and discarded (`executor.py:110-111`, `:130`).
See F-07-04.

### Q10. Backups and rebuild

One generation of `.bak`, same directory, at most minutes old. No dated or
off-server copy; `deploy/` contains no backup step. From Alpaca alone the bot
can rebuild stock positions and cash (with placeholder theses). It has no code
to rebuild trade history, the equity curve, or any options position.

### Where reality differs from the comments

- `portfolio.py:45` says "Atomic write"; the swap is, the backup and the
  short-write handling are not.
- `options_portfolio.py:10-12` says auto-close rules are "enforced on each
  update cycle"; `scheduler.py:1526-1529` disables them and
  `get_auto_close_candidates` has no caller.
- `scheduler.py:148` "execute any pending signals from pre-market scan" on
  first run can never fire: the list is always empty at startup.
- `macro_monitor.py:408` "keep last 90 entries" reads like 90 days; at about
  15 checks a day it is about 6 trading days.

## Findings

### F-07-01: An unreadable or missing state file silently becomes an empty $100,000 book, with no broker check at startup
- Severity: Critical
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:25-42`, `stock_agent/options_portfolio.py:67-83`, `stock_agent/scheduler.py:109-144`, `stock_agent/models.py:137-138`, `stock_agent/config.py:184-191`
- What happens: If `portfolio.json` cannot be parsed or validated and the backup cannot either (or the main file is simply missing), the bot starts with no positions, no history and cash of 100000, logs one line, and carries on. The first two saves then overwrite both the unreadable file and the backup.
- Evidence:
  ```python
  # portfolio.py:27-42
  if self._state_path.exists():
      try: ... return PortfolioState.model_validate_json(raw)
      except Exception as e:
          logger.error("Failed to load portfolio state: %s", e)
          backup = self._state_path.with_suffix(".json.bak")
          if backup.exists():
              try: ... return PortfolioState.model_validate_json(raw)
              except Exception: pass
  logger.info("Starting with fresh portfolio state")
  return PortfolioState()
  ```
  `run()` (`scheduler.py:109-144`) sends a startup message, backfills sectors and enters the loop; it never calls the sync. The next sync is the next weekday 08:15–08:45 brief or Sunday/Wednesday 18:00. Until then every buy path that does not sync first uses the empty book: `_check_for_market_event` to `_execute_signals` (`scheduler.py:1680`), a pre-scan re-run after a restart between 08:45 and 09:00 followed by execution at the open (`:194-197`), and the options scan sized on `get_portfolio_value()` (`:1385`). In `can_open_position` (`risk_manager.py:32-123`) an empty book means no existing holding, 0% exposure, 0% sector and cluster weight and 100000 cash, so every cap passes for symbols already held at the broker.
  Ways to get an unreadable file, each demonstrated: (a) a model gains a required field (old file rejected, backup has the same shape); (b) one record has a wrong type, for example a null price (F-07-05); (c) a truncated write (F-07-11); (d) the main file is missing, in which case `.bak` is not even tried; (e) `DATA_DIR` resolves somewhere new, since `config.py:185` hard-codes `/root/polymarket-bot/data/stock_agent` and falls back to a path relative to the checkout.
  For the options file the same pattern is worse: nothing ever imports option positions from Alpaca, so they stay untracked for good.
- Why it matters: The bot would trade against a false picture with position, sector, cluster and exposure limits all reading zero, which is the same class of failure as the earlier 76% concentration incident. Trade history, daily summaries and theses are lost permanently. Nothing tells Josh; the startup message prints local cash under the label "Starting Capital" (`discord_alerts.py:134`), which would read $100,000 and look normal.
- How verified: Read the load and startup paths end to end; ran from the repo root:
  ```python
  import os, tempfile; os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp()
  from stock_agent.config import Config; from stock_agent.portfolio import Portfolio
  p = Portfolio(Config()); p.state.cash = 5.0; p._save(); p._save()
  main = Config.DATA_DIR / "portfolio.json"; bak = Config.DATA_DIR / "portfolio.json.bak"
  main.write_text(main.read_text()[:20]); bak.write_text("")       # both unreadable
  q = Portfolio(Config()); print(q.state.cash, len(q.state.positions))   # 100000.0 0
  q.set_last_daily_check(None); q.set_last_daily_check(None)             # two ordinary saves
  print('"cash": 100000.0' in main.read_text(), '"cash": 100000.0' in bak.read_text())  # True True
  ```
  Also ran: main missing with a good `.bak` gives fresh state; a subclass of `Thesis` with a required `beta: float` rejects a file written by today's models; one unknown strategy name in `options_portfolio.json` gives zero open option positions.
- Direction (not a fix): Treat an unreadable state file as a stop-and-alert condition, keep the bad file aside, and rebuild from the broker before any order can be placed. Add a schema version and a migration step before the revamp changes any model.
- Verification: Confirmed: reproduced independently (both files unreadable, or main file missing even with a good `.bak`, gives 0 positions and cash 100000, and `can_open_position` then returns OK for a symbol already held), and `run()` has no broker sync before the loop, so the event-triggered scan (`scheduler.py:1680`) can buy against the empty book the same day; in practice the exposure window is short because the next weekday 08:15 brief re-imports stock positions before the open, unless the broker list is empty or the brief fails.

### F-07-02: One failed positions call empties the options ledger for good and books invented P&L
- Severity: Critical
- Confidence: Confirmed
- Where: `stock_agent/options_portfolio.py:389-551`, `stock_agent/options_executor.py:264-291`, `stock_agent/scheduler.py:1385-1400`, `stock_agent/scheduler.py:1536-1546`
- What happens: `get_option_positions()` returns an empty list on any HTTP error. The reconcile then treats every open option position as gone, moves it to the closed list with a made-up result, and saves. There is no code that puts a position back.
- Evidence: `options_executor.py:283-291` logs the error and falls through to `return []`. The scheduler passes the result straight in (`scheduler.py:1387-1393`, `:1538-1544`). In the reconcile, `live_syms` is empty so no position is `fully_live` (`options_portfolio.py:416`, `:438-453`); unexpired positions land in the fill branch or in `:507-510`:
  ```python
  # Missing with no clear reason — assume expired/assigned to zero
  per_share_exit_value = 0.0
  close_reason = "missing at Alpaca (assumed expired OTM)"
  ```
  then `self.state.closed_positions.append(pos)`, `self.state.total_realized_pnl += realized`, `self.state.positions = remaining`, `self._save()` (`:530-542`). This runs every 30 minutes in market hours while any option position exists, and at the start of every options scan. Straight after it in `_run_options_scan`, the engine is given `open_position_count` 0, exposure 0 and no "existing underlyings" (`scheduler.py:1415-1460`).
- Why it matters: After a single timeout or 5xx the bot believes it holds no options, so the 10-position and 15% options limits and the one-position-per-underlying rule all reset while the real positions are still open at the broker and no longer monitored. Realised options P&L is permanently polluted.
- How verified: Read both call paths; ran with made-up positions (long call bought at 4.00, bull call spread recorded at 2.10, put sold at 1.50): one call with `[]` returned `{'closed': 3, 'realized_pnl': -460.0, 'still_open': 0}`; a following call listing all four legs as live returned `still_open: 0`. Minimal version:
  ```python
  import os, tempfile, asyncio; os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp()
  from stock_agent.config import Config; from stock_agent.options_portfolio import OptionsPortfolio
  from stock_agent.options_models import OptionsSignal, OptionsStrategy, OptionType, OptionSide
  op = OptionsPortfolio(Config(), data_feed=None)
  sig = OptionsSignal(strategy=OptionsStrategy.LONG_CALL, underlying_symbol="AAA", underlying_price=100,
      contract_symbol="AAA300118C00100000", option_type=OptionType.CALL, side=OptionSide.BUY, target_strike=100)
  op.open_position(sig, filled_price=4.00, portfolio_value=100000)
  print(asyncio.run(op.sync_from_alpaca([], [], 100000)))   # {'closed': 1, 'realized_pnl': -400.0, 'still_open': 0}
  print(asyncio.run(op.sync_from_alpaca([{"symbol": "AAA300118C00100000"}], [], 100000)))  # still_open: 0
  ```
- Direction (not a fix): A failed broker read must be distinguishable from an empty account and must leave the ledger untouched; the options ledger also needs an import path so it can be rebuilt from the broker.
- Verification: Confirmed: reproduced with made-up positions (an empty list closed both, count and exposure went to 0, a later good list restored nothing, and the only code that adds an open option position is `open_position` at `options_portfolio.py:177`), and no guard exists between `get_option_positions()` returning `[]` on error and the reconcile; additionally, when the fills call succeeds while the positions call fails, the position's own opening fill is read as its closing fill (`options_portfolio.py:485-506`), which booked +300 on my sample instead of -250.

### F-07-03: The stock sync treats an empty positions list as truth and throws away what only the bot knew
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:145-212`, `stock_agent/executor.py:75-80`, `stock_agent/scheduler.py:808-823`, `stock_agent/scheduler.py:1732-1733`
- What happens: `get_positions()` returns `[]` both for "no positions" and for a failed request. If the account call works and the positions call fails during the Sunday or Wednesday sync, every stock position is dropped locally. When they come back at the next sync they are re-imported with a placeholder thesis, today's date as entry date and a sector from a small hard-coded map.
- Evidence: `executor.py:78-80` returns `[]` when `_request` returned `None`. The guard only skips when both calls failed: `if account is None and not positions: ... return` (`scheduler.py:818-820`). In the sync, a local position not in the list is dropped (`portfolio.py:148-156`); an unknown broker position is imported with `direction="HOLD", conviction=7, summary="Imported from Alpaca — awaiting re-analysis."`, `entry_date=now`, `stop_loss=round(avg_entry * 0.95, 2)`, `sector=Config.SECTOR_MAP.get(sym, "Unknown")` (`:182-206`). The morning brief has the opposite guard, `if alpaca_positions:` (`scheduler.py:1732`), so an account that truly holds nothing is never synced in the morning and phantom local positions and stale cash persist until Sunday or Wednesday evening.
- Why it matters: Between the wipe and the next good sync the bot has no positions to monitor (no software stop-loss, no event checks, and the weekly re-analysis at `scheduler.py:1005` loops over nothing), and risk checks see no holdings. Afterwards the original thesis and purchase date are gone for good, `hold_days` is counted from the re-import, and sectors not in the map stay "Unknown" until the next restart because `_backfill_sectors` only runs at startup (`scheduler.py:124`).
- How verified: Read the paths; ran:
  ```python
  import os, tempfile; os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp()
  from datetime import datetime, timezone
  from stock_agent.config import Config; from stock_agent.portfolio import Portfolio
  from stock_agent.models import Position, Thesis
  now = datetime.now(timezone.utc)
  th = Thesis(symbol="AAA", direction="BUY", conviction=9, summary="real thesis", bull_case="", bear_case="", catalysts=[], risks=[], generated_at=now)
  p = Portfolio(Config())
  p.add_position(Position(symbol="AAA", shares=10, entry_price=100, entry_date=datetime(2026, 1, 5, tzinfo=timezone.utc), stop_loss=93, thesis=th, sector="Technology", last_updated=now))
  row = {"symbol": "AAA", "qty": "10", "avg_entry_price": "100", "current_price": "110", "market_value": "1100", "unrealized_pl": "100", "unrealized_plpc": "0.1"}
  p.sync_from_alpaca([], {"cash": "99000"}); print(len(p.state.positions))        # 0
  p.sync_from_alpaca([row], {"cash": "99000"}); x = p.state.positions[0]
  print(x.thesis.conviction, x.thesis.summary, x.entry_date.date() == now.date(), x.stop_loss)
  # 7 Imported from Alpaca — awaiting re-analysis. True 95.0
  ```
- Direction (not a fix): Make "the request failed" a different result from "the list is empty", and keep local-only facts (thesis, entry date) keyed by symbol so a drop and re-import does not erase them.
- Verification: Confirmed: reproduced (`sync_from_alpaca([], account)` dropped every position and the re-import came back as HOLD, conviction 7, placeholder summary, entry date now, sector "Unknown"), and the guard at `scheduler.py:818` only skips when the account call also failed; High is right rather than Critical because both syncs that can wipe run at 18:00 with the market closed and the 08:15 brief re-imports before queued buys execute at the open, though a re-imported position whose sector stays "Unknown" is no longer counted toward its sector's 40% cap (`risk_manager.py:78-88`).

### F-07-04: Queued buy signals and all once-a-day markers exist only in memory
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:89-103`, `:149-154`, `:194-197`, `:433-442`, `:465`, `:543-551`, `:1116`
- What happens: Signals produced when the market is closed are appended to `self._pending_signals` and executed at the next open. A restart in between loses them. For the Sunday run the "done" marker is persisted (`set_last_weekly_analysis`, `:465`), so the analysis is not repeated and that week's buys simply never happen.
- Evidence: `self._pending_signals: list[Signal] = []` (`:95`); the only writers are `.extend`/`.append` at `:437`, `:546`, `:664`, `:781`, `:1682`; no code saves or loads it. The first-run branch at `:149-154` tests a list that is always empty. The service restarts on every change to `master` (`deploy/auto_update.sh`), and `deploy/stock-agent.service` has `Restart=always` with no stop timeout override, so a restart during a long job is a hard kill after systemd's default wait.
  The other markers (`_daily_summary_sent_today`, `_daily_prescan_done_today`, `_options_scan_done_today`, `_midweek_analysis_done_this_week`, `_macro_check_done_today`, `_morning_brief_done_today`, `_earnings_check_done_today`) are also memory-only. A restart inside a window repeats the job: a second `DailySummary` for the same day between 16:05 and 16:30 (`:1116`, `:1154`), a second options scan between 09:45 and 14:00, a second full Wednesday analysis between 18:00 and 20:59.
- Why it matters: A merge on Sunday night or Monday before 09:30 ET silently cancels the week's buying after the research cost has been paid. Duplicate daily summaries put a zero-change point into the equity curve; repeated scans spend API budget and can place further option orders.
- How verified: Traced every read and write of `_pending_signals` and each flag; confirmed nothing in `PortfolioState` holds them.
- Direction (not a fix): Persist pending decisions and "done" markers with the rest of the state, with an expiry so stale signals are not executed late.
- Verification: Confirmed: `_pending_signals` and every once-a-day marker are plain attributes set in `__init__` with no save or load anywhere, the weekly marker alone is persisted, and every push to `master` restarts the service; one overstatement is "never happen", since the Wednesday run repeats the full analysis and the daily pre-scan can queue conviction 8+ names, so lost Sunday buys are delayed by days or partly replaced rather than certainly gone.

### F-07-05: A null or zero price from the quote source goes straight into the ledger
- Severity: High
- Confidence: Likely (mechanism demonstrated; whether FMP ever returns a null or zero price for a held symbol is the untested link)
- Where: `stock_agent/portfolio.py:107-118`, `stock_agent/data_feed.py:236`, `:254`, `stock_agent/scheduler.py:1044-1047`, `:1320-1329`
- What happens: `update_prices` assigns whatever the quote contains. The models do not validate on assignment. A `null` price raises inside `update_prices`, leaves `None` in memory, and the stop-loss check that follows is skipped for every position. The next save by any other setter writes the `null` to disk, the save after that copies it into `.bak`, and the next restart hits F-07-01.
- Evidence:
  ```python
  # portfolio.py:112-113
  pos.current_price = quote.get("price", pos.current_price)
  pos.market_value = pos.current_price * pos.shares
  ```
  `data_feed.py:254` builds the quote with `"price": q.get("price", 0)`, which yields `None` when the key is present with a null and `0` when it is absent. In `_run_market_monitoring` the exception from line 1044 jumps to the `except` at `:1100`, so `check_position_health` at `:1047` does not run. With `0`, the position is valued at nothing, portfolio value drops by its full value, and `check_position_health` skips it (`risk_manager.py:212`). In `_execute_sell` the quote is read at `:1320-1321`, the order is sent at `:1323`, and P&L is computed at `:1328`; a null price raises after the order is at the broker, a zero price logs a 100% loss.
- Why it matters: One bad quote for one symbol can switch off the 30-minute stop check for the whole book for as long as it lasts, and can leave a state file that wipes the ledger at the next restart.
- How verified: Ran:
  ```python
  # same setup as F-07-03, position AAA with current_price=100
  try: p.update_prices({"AAA": {"price": None}})
  except TypeError as e: print("update_prices:", e)   # unsupported operand type(s) for *: 'NoneType' and 'int'
  p.set_last_daily_check(now); p.set_last_daily_check(now)
  q = Portfolio(Config()); print(q.state.cash, len(q.state.positions))   # 100000.0 0
  ```
  With price `0` on a 100-share, $100 position: portfolio value went from 100000 to 90000.
- Direction (not a fix): Validate external numbers before they touch state, and never let a pricing error for one symbol stop the checks for the others.
- Verification: Confirmed: the mechanism reproduced end to end (a null price raised in `update_prices`, left `None` in memory, later positions in the list were not repriced, two ordinary saves wrote the null to both files without error, and a restart then loaded 0 positions and cash 100000; a zero price is skipped by the stop check and cuts portfolio value), but whether the quote source ever returns null or zero for a held symbol could not be tested without calling it, so the High rating holds only if that happens.

### F-07-06: The trade history is the bot's guess, not what happened at the broker
- Severity: Medium
- Confidence: Confirmed (code); Likely for the after-hours case, which depends on Alpaca accepting a market order while closed
- Where: `stock_agent/scheduler.py:1264-1303`, `:1320-1346`, `:1003-1028`, `stock_agent/portfolio.py:77-91`, `:148-156`, `stock_agent/executor.py:104-134`
- What happens: A BUY is recorded as soon as the order request is accepted, for the full quantity, at the price seen during analysis (for Sunday signals, Friday's price). A SELL is recorded at a quote taken before the order is sent. Neither is ever compared with a fill. Exits the bot did not place itself, in particular the broker-side stop order it attaches to every buy, are never recorded: the sync just drops the position.
- Evidence: `price = signal.entry_price or 0` (`:1201`) feeds `Position(entry_price=price, ...)` (`:1283`) and `Trade(price=price, ...)` (`:1299`). `sell_price = quote["price"] if quote else position.current_price` (`:1321`), then `pnl = (sell_price - position.entry_price) * position.shares` (`:1328`). Order IDs are only logged (`executor.py:110-111`, `:130`); `Position` and `Trade` have no order or fill fields. The drop at `portfolio.py:150-156` writes a warning and no `Trade`. `_re_analyze_existing_positions` calls `_execute_sell` during the Sunday and Wednesday evening runs with no market-hours check (`scheduler.py:1022-1026`), so a sell that is only queued for the next open is logged as done, and the Monday morning sync then re-imports the still-held shares as a new placeholder position.
- Why it matters: Realised P&L, win rate, closed-trade count and the day-trade count all leave out broker stop-outs, which by the comment at `config.py:157-161` have been the usual way positions close. A stopped-out loss shows up in total return but not in "Total P&L" or win rate, so the weekly report flatters the strategy.
- How verified: Read the paths. Ran a made-up case: position of 100 shares at $100, then a sync showing it gone and cash 99300: `closed_trades 0, realized_pnl 0.0, total_pnl 0`, while `total_return` was -700.
- Direction (not a fix): Build the trade record from broker fills and account activities, keyed by order ID, and treat a position that vanished at the broker as a closed trade to be explained.

### F-07-07: A top-up adds shares without taking cash or re-averaging the entry price
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1271-1278`, `:1295-1303`, `stock_agent/portfolio.py:81-83`
- What happens: On an add-on buy the code increases `shares` and saves. Cash is not reduced (`add_position` is not called and the BUY branch of `log_trade` is `pass`) and `entry_price` stays at the old value. Until the next sync, portfolio value is overstated by the full cost of the add-on.
- Evidence:
  ```python
  # scheduler.py:1274-1278
  if existing:
      existing.shares += qty
      existing.market_value = existing.current_price * existing.shares
      existing.last_updated = now
      self.portfolio._save()
  ```
- Why it matters: The daily summary on a top-up day shows a gain that does not exist and the next day shows the matching loss. Risk checks and sizing use the inflated value for up to a day. A sell before the sync logs the wrong P&L, and the software stop is measured from the old entry price.
- How verified: Replayed those lines with made-up numbers: 20 shares at $100, price now $120, add 40 shares at $120. Result: cash unchanged at 98000, portfolio value 105200 against a true 100400 (overstated by exactly the $4,800 spent), entry price still 100.0 against a true average of 113.33, unrealised P&L shown 1200 against a true 400.
- Direction (not a fix): Route every fill, new or add-on, through one function that moves cash and average cost together, or take both from the broker immediately after the order.

### F-07-08: Portfolio value and return figures are computed several different ways, and none includes options
- Severity: Medium
- Confidence: Confirmed (the "Yesterday P&L" item is Likely; it depends on Alpaca's definition of `last_equity`)
- Where: `stock_agent/portfolio.py:227-230`, `:275-343`, `stock_agent/models.py:138`, `stock_agent/scheduler.py:1118-1129`, `:1720-1727`, `stock_agent/discord_alerts.py:134`, `:335-343`, `:582-586`
- What happens:
  - Portfolio value is broker cash plus stock positions. Cash already reflects option premiums paid and received, but option positions are not added back. Made-up check: buying $3,000 of calls moved local value from 100000 to 97000 while broker equity stayed at 100000.
  - `starting_capital` is fixed at 100000 and never set. A $10,000 account shows a total return of -90000 (-90%); a $5,000 deposit shows as a +$5,000 day.
  - "Total P&L" means three things: Alpaca equity minus 100000 in the morning brief (`scheduler.py:1726`), local value minus 100000 in the daily summary (`:1128`), and realised plus unrealised from local trades in the weekly report (`portfolio.py:337`, shown at `discord_alerts.py:338`, `telegram_alerts.py:148`).
  - Until the first locally recorded SELL, `calculate_stats` returns `max_drawdown` 0.0 and `sharpe` 0.0 regardless of history (`portfolio.py:285-298`). Summaries of 100000, 110000, 88000 gave drawdown 0.0 against a true 20%.
  - The weekly Discord report looks for `stats.get("sharpe_ratio")` (`discord_alerts.py:342`) but the key is `sharpe` (`portfolio.py:342`), so Sharpe is computed and never shown.
  - The daily summary does not refresh prices or sync first; it uses prices from the last monitoring run, up to 30 minutes or more before the close.
  - The morning brief's "Yesterday P&L" is `equity - last_equity` taken at about 08:15 (`scheduler.py:1722-1723`), which is the overnight move since yesterday's close, not yesterday's result.
  - The startup message labels current local cash as "Starting Capital" (`discord_alerts.py:134`).
  - Options realised P&L is kept in `total_realized_pnl` but the only formatter that shows it, `format_portfolio_summary_embed`, has no caller.
- Why it matters: Josh cannot compare one report with another, and any decision to move to real money based on these numbers rests on figures that omit options, dividends as income, and broker-side exits. The deposit and starting-capital problems only matter once real money is used or the paper balance is reset to a different amount.
- How verified: Read each formula and its display site; ran `calculate_stats`, `get_portfolio_value` and `sync_from_alpaca` with the made-up inputs quoted above.
- Direction (not a fix): Take equity, cash and the equity curve from the broker's own account figures, define each headline number once, and record deposits and withdrawals explicitly.

### F-07-09: Options P&L is booked from the wrong prices in several cases
- Severity: Medium
- Confidence: Confirmed for the arithmetic; Likely for the fill-order item (it relies on Alpaca honouring `direction=desc`)
- Where: `stock_agent/options_portfolio.py:239-265`, `:473-476`, `:485-510`, `:555-590`, `stock_agent/options_executor.py:308-312`, `stock_agent/scheduler.py:1486-1496`, `stock_agent/options_engine.py:592`
- What happens:
  - Spreads are marked with leg 1 only. `refresh_positions` fetches both legs but uses `quotes.get(pos.contract_symbol)` (`:579-586`), while `entry_price` is the net debit. Made-up spread (long leg 5.00, short leg 3.00, recorded entry 2.10) with no price movement showed unrealised +$290 (+145%) and tripped the "50% profit target" flag.
  - An early close uses the opening fill as the exit price. Activities are requested newest first (`options_executor.py:311`) and the code takes `fills[-1]` (`options_portfolio.py:490`), the oldest. Made-up long call opened at 4.05 and sold at 9.00 was booked with exit 4.05 and realised +$5 instead of about +$500. The fill's side is not checked.
  - Entry price is an estimate that is never corrected: `max_debit_per_contract or min_credit_per_contract or 0.0` (`scheduler.py:1488-1489`), which for spreads is the mid net debit times 1.05 (`options_engine.py:592`) and can be 0.0.
  - A position missing at the broker for no known reason is booked as expired worthless (`:507-510`): full loss for a long, full premium as profit for a short.
  - At expiry, if the closing-price lookup fails the position is booked at zero and never revisited (`:473-476`), which contradicts the "in-the-money legs at intrinsic value" decision in that failure case.
- Why it matters: Options results cannot be trusted in either direction. If auto-close is ever re-enabled, every debit spread would look like a 50% winner on the first refresh.
- How verified: Ran `refresh_positions` with a fake quote feed and `sync_from_alpaca` with made-up positions and activities; outputs quoted above.
- Direction (not a fix): Take entry and exit prices from the broker's fills per leg and value a spread as the net of both legs.

### F-07-10: `portfolio.json` grows without limit and the copies it keeps are not real history
- Severity: Medium
- Confidence: Confirmed (growth rate measured on synthetic data; real size not verified)
- Where: `stock_agent/models.py:115-129`, `stock_agent/scheduler.py:1138-1154`, `stock_agent/portfolio.py:44-65`, `:350-353`
- What happens: Each `DailySummary` embeds every open position, and each position embeds its full thesis. `trade_history` and `active_theses` are also never trimmed. The whole file is serialised, the old file is read and copied to `.bak`, and the new file written, on every one of the 30-plus saves a day, synchronously on the event loop.
- Evidence: Measured with theses of realistic length: one position is about 3.4 KB of JSON.

  | Scenario | File size | Peak memory after load and 3 saves |
  | --- | --- | --- |
  | 1 year, 10 positions | 10.2 MB | 93 MB |
  | 1 year, 20 positions | 19.8 MB | 153 MB |
  | 2 years, 15 positions | 29.4 MB | 215 MB |

  Baseline with imports only was 27 MB. Save time was 0.07–0.16 s on a 4-core machine; the droplet has one vCPU and 1 GB.
  The only field ever read back from old summaries is `portfolio_value` (`portfolio.py:268-273`, `:307-329`, `scheduler.py:1124`). The embedded positions are also not snapshots: `positions=list(self.portfolio.state.positions)` (`scheduler.py:1150`) stores references to the live objects, so every summary created since the last restart is rewritten with current prices on each save.
- Why it matters: Memory and I/O climb steadily on a small server, and the data that causes the growth is both unused and wrong.
- How verified: Built synthetic states and timed `model_dump_json`, `model_validate_json` and `_save`. For the aliasing: three summaries at prices 100, 120, 80 all showed the embedded price as 80.0 on disk, and `summary.positions[0] is state.positions[0]` was True.
- Direction (not a fix): Store one small row per day and keep history out of the file that is rewritten on every price update.

### F-07-11: Save path and backups are weaker than the "atomic write" label suggests
- Severity: Medium
- Confidence: Confirmed for the code behaviour; Suspected for how often the conditions arise
- Where: `stock_agent/portfolio.py:44-75`, `stock_agent/options_portfolio.py:85-116`, `stock_agent/macro_monitor.py:389-418`, `deploy/auto_update.sh`, `deploy/setup.sh`
- What happens:
  - `os.write(fd, data.encode())` (`portfolio.py:54`) ignores the returned byte count. With `os.write` patched to write half the data, `_save` returned normally and `portfolio.json` was invalid JSON. A nearly full disk can do this for real.
  - No `fsync` before the rename.
  - `.bak` is one generation, written non-atomically with errors swallowed (`:58-63`). On the first save after a bad load it is overwritten with the unreadable file.
  - A save failure re-raises. In `update_prices` that happens after prices are set and before the stop check (`scheduler.py:1044-1047`); in `_execute_signals` it happens after the order is sent and before the trade is logged (`:1264`, `:1292`, `:1303`).
  - Temp files from a kill mid-save are never cleaned up.
  - Macro files are written in place. If `macro_history.json` is ever truncated, `_append_history` fails on every later call and the file never recovers (demonstrated). A removed dataclass field makes `macro_state.json` load as `None`, which silently resets the regime to NORMAL.
  - A macro snapshot built when every quote call failed is all zeros, scores 0, and is saved as a real NORMAL reading (demonstrated with `_compute_regime` on an empty snapshot).
  - There is no backup outside `data/` and no off-server copy. The bot cannot rebuild trade history, the equity curve or options positions from the broker.
- Why it matters: The state survives a clean crash well, but a full disk, a droplet loss or a bad deploy can still take the history with it.
- How verified: Read the code; ran the short-write, macro-history and macro-state cases in a throwaway directory.
- Direction (not a fix): Check and sync writes, keep dated backups off the server, and make every file rebuildable or disposable by design.

### F-07-12: A position's thesis is never updated, and stored theses never expire
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:350-353`, `stock_agent/scheduler.py:1013-1019`, `:609-624`, `:720-724`, `:1084-1093`, `:1435-1440`
- What happens: Re-analysis writes the new thesis to `active_theses[symbol]` only. No code assigns `position.thesis` after the position is created. So the position keeps its entry thesis (or the import placeholder) forever, reports show that old conviction, and every re-analysis hands the model the old thesis as "current". `active_theses` is never pruned and has no age check; the options scan reads `conviction` and `target_price` from it however old the entry is.
- Evidence: `self.state.active_theses[symbol] = thesis.model_dump()` (`portfolio.py:352`); a search for `.thesis =` in `stock_agent/` finds nothing. `re_analyze_position(pos.symbol, company, pos.thesis)` at each call site. Display sites use `p.thesis.conviction` (`discord_alerts.py:278`, `:355`).
- Why it matters: Imported positions stay "awaiting re-analysis" permanently, and the weekly re-check compares against a thesis that may be months old or empty.
- How verified: Searched for assignments and read every caller of `update_thesis` and `re_analyze_position`.
- Direction (not a fix): Keep one thesis store with a timestamp per symbol and decide explicitly how old a thesis may be before it is ignored.

### F-07-13: Dates are compared across time zones in two reports, and options count days in UTC
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1132-1136`, `:1811-1815`, `stock_agent/risk_manager.py:171-189`, `stock_agent/options_models.py:173-176`, `:234`, `stock_agent/options_portfolio.py:160`, `:426-427`, `:463`
- What happens: Trade timestamps are UTC; "today" and "yesterday" in the daily summary and morning brief are Eastern dates, compared as strings. A sell logged at 20:30 ET on Sunday is stamped Monday in UTC: it is missing from Monday's "yesterday" recap and appears in Tuesday's. The day-trade check also groups by UTC date. Option days-to-expiry use `datetime.utcnow().date()`, so they tick over at 19:00 or 20:00 ET, and `exp <= today` treats expiry day itself as expired. `DailySummary.date` is a display string such as "Nov 20, 2026 (Fri)", not a sortable date.
- Why it matters: Minor misattribution in reports today; it becomes relevant when history is used for analysis.
- How verified: Computed the Sunday 20:30 ET example in Python; confirmed that a timestamp stored without an offset loads as naive and makes `check_pdt_compliance` raise `TypeError` (see Could not verify).
- Direction (not a fix): Convert to market time before deriving a trading date, in one helper.

### F-07-14: The sync ignores short, fractional and long-symbol positions
- Severity: Low
- Confidence: Confirmed (handling); Suspected (whether such positions ever occur)
- Where: `stock_agent/portfolio.py:131-138`, `:158-162`, `:177-181`, `stock_agent/models.py:89`
- What happens: Quantity is parsed as `int(float(qty))` and anything `<= 0` is skipped. With made-up rows: 10.7 shares became 10, 0.5 shares and both short positions disappeared, and a six-letter symbol was ignored. Local value came out at 105700 against a broker equity of 102370 because cash included the short-sale proceeds with no matching liability.
- Why it matters: The bot only buys whole shares, so fractions are harmless in normal running. A short position, if one were ever created (see the hand-off to area 03 on double sells), would be invisible, unmonitored and would inflate portfolio value.
- How verified: Ran `sync_from_alpaca` with the rows described.
- Direction (not a fix): Surface anything at the broker that the ledger cannot represent instead of skipping it.

### F-07-15: Stored fields and code paths that nothing uses
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/models.py:96`, `:129`, `:139`, `:143`, `stock_agent/portfolio.py:251-273`, `stock_agent/options_portfolio.py:187-237`, `:603-619`, `:750-788`, `stock_agent/scheduler.py:149-154`
- What happens: `Position.stop_loss` is written and never read (the software stop is recomputed from `entry_price` each cycle; the import value of 5% is outside the 7–12% range but has no effect). `last_daily_check` and `last_universe_refresh` are saved (the first twice an hour) and never read. `DailySummary.signals` is always `[]`. `get_equity_curve`, `get_sector_exposure`, `OptionsPortfolio.close_position`, `get_auto_close_candidates`, `get_near_expiry_positions` and `format_portfolio_summary_embed` have no callers. `macro_history.json` is never read.
- Why it matters: Clutter that misleads a reader about what the bot tracks; `stop_loss` in particular looks authoritative and is not.
- How verified: Searched for every reader of each name.
- Direction (not a fix): Remove or wire up during the revamp.

## Checked and fine

- The main-file swap is a same-directory temp file plus `os.replace`; a crash mid-save cannot leave a half-written `portfolio.json` (`portfolio.py:50-65`).
- `deploy/auto_update.sh` uses `git reset --hard` with no `git clean`, so `data/` survives deploys.
- All persisted datetimes written by the current code are timezone-aware UTC and reload as aware (`scheduler.py:68-69`, `portfolio.py:140`, `analyst.py:284`, `options_portfolio.py:171`, `:211`).
- `_should_run_weekly` handles both naive and aware `last_weekly_analysis` (`scheduler.py:257`).
- Conviction stays an integer through all three adjusters (`tipranks_client.py:334-376`, `sentiment.py:115-144`, `analyst_trends.py:198-227`), so it cannot make the file unloadable.
- `unrealized_pnl_pct` is a fraction both from Alpaca and from `update_prices` (`portfolio.py:116`, `:169`).
- The mutable defaults in the pydantic models (`= []`, `= {}`) are copied per instance.
- Fractional share quantities do not crash the sync; they are truncated (F-07-14).
- `DATA_DIR` resolution works for an empty or unset `STOCK_DATA_DIR` (`config.py:186-191`); tests redirect it to a temp directory (`tests/conftest.py`).
- Single process, single event loop, synchronous saves: no interleaved writes within one process.
- Win rate, average P&L percent and max drawdown formulas are arithmetically correct on the inputs they are given (`portfolio.py:300-303`, `:321-329`); the problem is the inputs.
- The options `closed_positions` list and `macro_history.json` are small enough not to matter for size.

## Could not verify

All of these need the droplet snapshot from `scripts/vps_snapshot.sh`.

- Real size and shape of the state files: byte sizes, number of daily summaries, positions per summary, leftover `*.tmp` files, age of `.bak`. Needs `state/listing.txt`, `state/sizes.txt` and the copied JSON files.
- Whether any old record has a datetime without an offset. If so, `check_pdt_compliance` and `hold_days` would raise. Needs the state JSON.
- Whether F-07-01, F-07-02 or F-07-03 have already happened: positions carrying the placeholder thesis, closed option positions with reason "missing at Alpaca (assumed expired OTM)", duplicate daily-summary dates, and log lines "Failed to load portfolio state", "Starting with fresh portfolio state", "get_option_positions failed", "not found at Alpaca, dropping". Needs the state JSON and `logs/journal.log`.
- Whether FMP has returned a null or zero price (F-07-05): look for "Market monitoring failed" with a `TypeError`. Needs the journal.
- How far local trade history is from broker fills, and whether `starting_capital` matches the account's real starting equity. Needs the state JSON plus the Alpaca report in the snapshot.
- Alpaca behaviours assumed above: activities returned newest first, market orders accepted while the market is closed, definition of `last_equity`. Needs Alpaca documentation or the order history export.
- Save and load time and free disk on the real one-vCPU droplet.

## Questions for Josh

1. If a state file cannot be read, should the bot refuse to trade and alert you until it has rebuilt from Alpaca, rather than start fresh?
2. Should headline performance (equity, total return, equity curve) come from Alpaca's own account figures instead of the bot's arithmetic?
3. What is the real starting balance, and will money be added or withdrawn once live?
4. Should options results be part of the headline P&L and portfolio value?
5. Should buy decisions made on Sunday or Wednesday evening survive a restart before the next open, and for how long is such a decision still valid?
6. Do you need the full list of positions for every past day, or is one value per day enough?
7. Do you want daily off-server backups of `data/`, and how many days kept?
8. On a held position, should reports show the original entry thesis or the latest re-analysis?
9. Has anything ever been bought or sold in this account by hand, outside the bot?

## Handoffs

- 03: `get_positions()` and `get_option_positions()` return `[]` on failure (`executor.py:75-80`, `options_executor.py:283-291`); this is the trigger for F-07-02 and F-07-03.
- 03, 08: `_execute_sell` uses `self._tipranks_lookup` (`scheduler.py:1349`), which is never defined; every successful sell lands in the `except` and sends "Sell failed" after the trade was logged, and the normal sell alerts are never sent.
- 03: Broker stop orders are never cancelled before a bot sell and no order IDs are kept; check with order history whether bot sells are rejected for shares held by the stop order, or whether a late second sell can open a short.
- 03: Sells are placed during the Sunday and Wednesday evening runs with no market-hours check and recorded as done (`scheduler.py:1022-1026`); a kill during the 3-second wait in `place_buy` (`executor.py:115`) leaves a buy with no stop and no local record.
- 02: Risk checks use the same local portfolio value, which excludes options and is inflated after a top-up (F-07-07, F-07-08).
- 02: Sector labels come from two vocabularies, FMP ("Financial Services") for bought positions and `SECTOR_MAP` ("Financials") for imported ones (`config.py:102-146`, `data_feed.py:196`), so the sector cap can under-count.
- 02: `Thesis` has no `beta` field, so `getattr(pos.thesis, "beta", None)` is always `None` (`risk_manager.py:215`, `scheduler.py:1258`); the day-trade check sees only locally recorded trades.
- 04: `if order:` is always true for the tuple returned by `place_spread_order` (`scheduler.py:1468-1486`), so a spread whose two orders both failed is recorded as open.
- 04: Options exposure is the absolute value of longs minus shorts (`options_portfolio.py:631-655`); made-up long 5000 plus short 5000 gave 0%. A half-closed spread leaves one live leg untracked.
- 04: A symbol with no stored thesis gets conviction 6 (`scheduler.py:1436`), which qualifies it for cash-secured puts; stale `active_theses` entries drive candidates.
- 01: The weekly "done" marker is set only at the end (`scheduler.py:465`); the early return at `:413` lets the run restart every minute inside the window. "Queuing sell at market open" (`:618-622`) only logs.
- 06: On an FMP outage `screen_universe` returns just the priority symbols, which then overwrite the stored universe (`data_feed.py:337-344`, `scheduler.py:402`); the all-zero macro snapshot can register as a regime change and start an event scan.
- 08: No alert when state fails to load, none when an option position is closed or expires, and the expiry warning repeats every 30 minutes.
- 09: No backup of `data/`; `.env.example` is the old Polymarket template; `config.py:194-195` carries hard-coded defaults for `TIPRANKS_API_KEY` and `TIPRANKS_API_TOKEN` in a public repo.
- 10: No tests cover `portfolio.py` or `options_portfolio.py` persistence, sync or stats; the options ledger class makes HTTP calls and reads `os.environ` directly (`options_portfolio.py:287-347`).
- 11: Realised P&L, win rate and the equity curve in `portfolio.json` are not reliable inputs; use Alpaca activities and portfolio history.
