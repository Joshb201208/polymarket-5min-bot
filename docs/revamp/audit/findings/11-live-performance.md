# 11. Live performance

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

No droplet snapshot was uploaded, so this file does **not** say how the bot has
performed. It contains no return, win rate, drawdown, or holdings, because none
of that can be known from the code, and the brief says not to estimate. What it
does cover is the bot's own scorekeeping: the numbers it sends in the daily,
weekly, and morning messages, and whether they can be trusted. The morning
brief is the only report read straight from the broker. The daily summary and
the weekly "Performance" block are the bot's own arithmetic on its own
notebook, and that notebook has gaps. The biggest one: when a stop order fills
at the broker, the bot records no trade and sends no alert, so the weekly win
rate and "Total P&L" only count the sells the bot placed itself (F-11-01).
Second: since 23 April every sell the bot does place is announced as "Sell
failed" and the real sell alert is never sent, because of a bug that fires
after the order has already gone through (F-11-02). Third: options results are
never reported at all, and the options notebook is wrong from the first minute
(a made-up spread shows a 95% profit at entry) (F-11-07). Fourth: nothing the
bot stores can answer "did high conviction do better?" or "did we beat an
index fund?" (F-11-08). The "Could not verify" section lists, question by
question, what the snapshot will answer, what it will still not answer as the
script stands today, and how to run it to get more.

## How it actually works

The production branch (`origin/master`, commit `e92e1cd`, 2026-05-16) has the
same `stock_agent/`, `deploy/`, and `requirements.txt` as the audited commit;
the two newer commits only add documentation and tooling. The visible git
history is a shallow clone starting 2026-04-04, so the date the bot first
traded is not known from the repository.

### What the bot writes down, and where the numbers come from

| Record | Stored in | Written when | Prices come from |
| --- | --- | --- | --- |
| Buy trade | `portfolio.json` → `trade_history` | Broker accepted the order (`scheduler.py:1264-1303`). The fill is never read back. | The signal's price from analysis time (`scheduler.py:1201`), not the fill |
| Sell trade | same | Broker accepted the bot's own market sell (`scheduler.py:1323-1346`). This is the only place a sell is recorded (`grep 'action="SELL"'` has one hit, `scheduler.py:1334`). | A data-vendor quote taken just before the order (`scheduler.py:1320-1321`) |
| Position | `portfolio.json` → `positions` | On buy; then overwritten from the broker at each sync | Broker after a sync (`portfolio.py:157-171`) |
| Daily summary | `portfolio.json` → `daily_summaries` | Once per weekday if the loop ticks between 16:05 and 16:30 Eastern (`scheduler.py:311-320`, `1112-1154`) | The local ledger, not the broker |
| Options position | `options_portfolio.json` | When the order call returns anything truthy (`scheduler.py:1486-1496`); closed only by reconciliation | An estimate (`scheduler.py:1488-1489`) |
| Logs | systemd journal only (`__main__.py:9-12`) | Always | n/a |

The ledger is replaced by broker truth in exactly three places: the Sunday
cycle (`scheduler.py:371`), the Wednesday cycle (`scheduler.py:491`), and the
weekday morning brief at 08:15–08:45 Eastern, and there only if the broker
returned at least one position (`scheduler.py:1732-1733`). It is not synced
before the open, before the options scan, or before the daily summary.

### Every statistic the bot computes for itself

| Statistic | Formula | Shown in | Verdict |
| --- | --- | --- | --- |
| Portfolio value | cash + Σ price × shares, stocks only (`portfolio.py:227-230`) | Daily, weekly | Leaves out options and shorts; cash can be stale (F-11-04) |
| Day P&L | value − previous summary's value (`scheduler.py:1122-1127`) | Daily | "Day" means "since the last summary", however long ago (F-11-05) |
| Total P&L, daily | value − `starting_capital` (`scheduler.py:1128-1129`) | Daily | Depends on the value above and a fixed constant (F-11-03) |
| Total P&L, weekly | Σ P&L of recorded sells + Σ open unrealised (`portfolio.py:301`, `337`) | Weekly | Not the account's result (F-11-01, F-11-03) |
| Total P&L, morning | broker equity − `starting_capital` (`scheduler.py:1725-1727`) | Morning brief | Sound if the constant is right |
| Win rate | winning recorded sells ÷ recorded sells (`portfolio.py:300`, `334`) | Weekly | Arithmetic fine; the population is incomplete (F-11-01) |
| Per-trade P&L, hold days | (quote − ledger entry) × shares; now − entry date (`scheduler.py:1328-1330`) | Sell alert, which is never sent (F-11-02) | Approximate (F-11-06) |
| Max drawdown | peak-to-trough over daily summaries (`portfolio.py:321-329`) | Weekly | Loop is correct; the series is not (F-11-05) |
| Sharpe | mean ÷ stdev of summary-to-summary changes × √252 (`portfolio.py:305-319`) | Nowhere | Computed, never displayed (F-11-05) |
| `total_return`, `total_return_pct`, `avg_pnl_pct`, `total_trades` | `portfolio.py:282-283`, `303`, `332` | Nowhere | Computed, never displayed |
| Exposure | Σ price × shares ÷ value (`portfolio.py:243-249`) | Daily | Stocks only |
| Position unrealised P&L | Broker's own fields after a sync (`portfolio.py:165-169`), else (price − entry) × shares (`portfolio.py:114-116`) | Daily, weekly | Fine |
| Options unrealised/realised P&L, exposure, delta, theta | `options_portfolio.py:256-265`, `515-531`, `623-657` | Nowhere except one embed at open and log lines | Unreliable (F-11-07) |

The bot computes no comparison with any index, no per-conviction breakdown,
and no cost or API usage figure.

### Where this differs from the documentation

- `README.md` lists "Telegram trade alerts"; sell alerts have not been
  deliverable since 2026-04-23 (F-11-02).
- `CLAUDE.md` says the stop-loss is "volatility-adjusted and clamped to
  7–12%". In practice every stop is exactly 7% (F-11-09).
- The module docstring at `options_portfolio.py:10-12` says auto-close rules
  are "enforced on each update cycle". Nothing closes an option; every
  position runs to expiry (`scheduler.py:1523-1574`; `close_option_position`
  and `close_spread_position` have no caller outside `options_executor.py`).

## Findings

### F-11-01: Exits that happen at the broker leave no trade record and no alert
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:147-156`, `275-277`; `stock_agent/scheduler.py:1316-1346`; `stock_agent/executor.py:117-124`
- What happens: Every buy places a resting stop order at the broker. When
  that stop fills (or a position disappears for any other reason: a covered
  call is assigned, an order is placed by hand), the next sync deletes the
  position from the ledger and writes one warning to the journal. No sell is
  recorded and nothing is sent to Telegram or Discord. The weekly win rate,
  closed-trade count, and "Total P&L" are built only from recorded sells.
- Evidence: the sync drops the position and moves on:
  ```python
  # portfolio.py:149-156
  if not ap:
      # Position on our books but not at Alpaca — treat as closed.
      logger.warning("sync_from_alpaca: %s not found at Alpaca, dropping from local state", pos.symbol)
      dropped += 1
      continue
  ```
  The statistics read only the trade list: `trades = [t for t in
  self.state.trade_history if t.action == "SELL" and t.pnl is not None]`
  (`portfolio.py:277`). The only code that appends a sell is `_execute_sell`
  (`scheduler.py:1332-1346`), reached from the bot's own 30-minute stop check
  and its thesis and event exits.
- Why it matters: The broker stop is the first line of defence, so by design
  many losing exits take this path. The scoreboard cannot see them. With
  made-up numbers: two round trips, one a −7% broker stop-out and one a +10%
  sale by the bot. The ledger reports 1 closed trade, 100% win rate, and
  "Total P&L" of +500 on an account that is really up 150. Until the next
  sync (next weekday 08:15, or Wednesday/Sunday evening) the daily summary
  also still lists the sold position as held.
- How verified: Read the path end to end. Ran `Portfolio.sync_from_alpaca`
  and `calculate_stats` in a temporary directory with made-up positions; the
  output matched the numbers above (`closed_trades: 0`, then `win_rate: 1.0`,
  `total_pnl: 500.0`, `total_return: 150.0`).
- Direction (not a fix): Performance records need to be built from the
  broker's fills, and an exit the bot did not initiate needs to be detected
  and announced, not silently dropped.

### F-11-02: Every sell the bot places is reported as "Sell failed", and the sell alert is never sent
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1345-1350`, `1364-1370`
- What happens: After the broker accepts a sell and the trade is saved,
  the code reads `self._tipranks_lookup`, an attribute that is never created.
  That raises an error, which is caught by the handler meant for failed
  sells. The owner gets "Sell failed for SYMBOL" on Telegram and an error on
  Discord. The real sell alert (price, P&L, days held, reason) is skipped.
- Evidence:
  ```python
  # scheduler.py:1345-1350
  self.portfolio.remove_position(position.symbol)
  self.portfolio.log_trade(trade)

  tr_clause = format_tipranks_telegram_clause(
      self._tipranks_lookup.get(position.symbol.upper())
  )
  ```
  `grep _tipranks_lookup stock_agent/` finds only this line; `__init__`
  (`scheduler.py:73-103`) does not set it. Introduced by commit `7a8ac46`
  (2026-04-23).
- Why it matters: For seven months the alert history has said the opposite
  of what happened on every bot-initiated sell. Anyone reading the error log
  or the Telegram history will count successful sells as failures, and the
  Discord trade-history channel has no sells after that date. With real
  money, a "Sell failed" message on a stop-out invites a manual sell of
  shares that are already gone.
- How verified: Ran `_execute_sell` on a scheduler object with fake broker,
  data, and alert objects (no network). Calls recorded, in order:
  `place_sell('AAA', 50)`, then `telegram.send_error_alert("Sell failed for
  AAA: 'StockAgentScheduler' object has no attribute '_tipranks_lookup'")`,
  then `discord.send_error(...)`. No trade alert call. The sell was in
  `trade_history` and the position was gone from the ledger.
- Direction (not a fix): Owned by areas 03 and 08. For this area: treat every
  "Sell execution failed" line in the logs as "sell placed, alert lost" until
  the broker history says otherwise.

### F-11-03: "Total P&L" means three different things, and the weekly one is not the account's result
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:282-283`, `301`, `337`; `stock_agent/scheduler.py:1128-1129`, `1725-1727`; `stock_agent/telegram_alerts.py:145-151`; `stock_agent/discord_alerts.py:335-343`; `stock_agent/models.py:138`
- What happens: Three reports print a figure called "Total P&L":

  | Report | What it actually is |
  | --- | --- |
  | Weekly | P&L of recorded sells + unrealised P&L of open stock positions (`portfolio.py:337`) |
  | Daily | Ledger value − `starting_capital` (`scheduler.py:1128`) |
  | Morning | Broker equity − `starting_capital` (`scheduler.py:1726`) |

  The weekly figure ignores unrecorded exits (F-11-01), options, dividends,
  and interest. The same weekly message also prints "Portfolio Value", so one
  message can imply two different results. The figure that does mean "value
  now minus value at the start" (`total_return`) is computed and never shown.
- Evidence: `"total_pnl": realized_pnl + unrealized_pnl` (`portfolio.py:337`)
  is what `telegram_alerts.py:148` and `discord_alerts.py:338` print.
  `starting_capital: float = 100000.0` (`models.py:138`) has no writer
  anywhere (`grep starting_capital` shows reads only), so every "total"
  percentage rests on a constant that is never checked against the broker.
- Why it matters: These are the headline numbers Josh sees. In the made-up
  case from F-11-01 the weekly "Total P&L" is more than three times the real
  gain. A decision to go live would rest on them.
- How verified: Read each formula and its display site; ran `calculate_stats`
  with made-up data (`total_pnl: 500.0` against `total_return: 150.0`).
- Direction (not a fix): One definition of total result, taken from the
  broker, used in every report; the starting amount and any deposits or
  withdrawals read from the broker's records.

### F-11-04: The bot's own account value leaves out options and shorts, is not refreshed before it is recorded, and jumps on a top-up
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:227-230`, `158-162`, `177-181`, `214-219`; `stock_agent/scheduler.py:1112-1129`, `1271-1278`
- What happens: The value used for the daily summary and the weekly
  "Portfolio Value" is cash plus stock positions from the ledger. Four things
  make it differ from the broker's equity:
  1. Options are not in it. Cash is copied from the broker, and broker cash
     falls when an option is bought and rises when one is sold, but the
     option itself is never added or subtracted.
  2. Short stock positions are discarded by the sync (`if shares <= 0`), so
     the cash from a short sale counts and the liability does not.
  3. A top-up of an existing position adds shares but never debits cash
     (`scheduler.py:1271-1278`; compare `add_position`, `portfolio.py:93-98`).
  4. Nothing syncs before the 16:05 summary. Cash is as of the 08:15 sync
     plus the bot's own adjustments at signal prices; stock prices are as of
     the last 30-minute check; a position stopped out during the day is still
     counted.
- Evidence: `return self.state.cash + market_val` where `market_val` sums
  `self.state.positions` only (`portfolio.py:229-230`). The add-on branch:
  ```python
  # scheduler.py:1274-1278
  if existing:
      existing.shares += qty
      existing.market_value = existing.current_price * existing.shares
      existing.last_updated = now
      self.portfolio._save()
  ```
- Why it matters: The daily "Portfolio Value", "Day P&L", and "Total P&L",
  and everything derived from the daily series, carry these errors. Made-up
  examples on a 100,000 account: a 2,000 top-up shows as a 2% gain that day
  and reverses after the next sync; a short of 5,000 shows the account at
  105,000 while broker equity is 100,000; buying a 300 call spread shows as a
  300 loss on day one.
- How verified: Ran the add-on steps on a temporary `Portfolio` (value went
  100,000 → 102,000 with cash unchanged). Ran `sync_from_alpaca` with a
  made-up negative-quantity position (ledger held one symbol, value 105,000,
  against stated equity 100,000). Items 1 and 4 by reading; the sync call
  sites are `scheduler.py:371`, `491`, `1733` only.
- Direction (not a fix): Report the broker's equity figure. Area 07 owns the
  ledger arithmetic; area 02 should note that the same value feeds position
  sizing (`scheduler.py:1225`, `risk_manager.py:38-40`).

### F-11-05: The daily series behind drawdown and Sharpe has gaps, duplicates, and holiday rows
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:90`, `311-320`, `1116`, `1122-1127`, `1139`; `stock_agent/portfolio.py:285-298`, `305-329`; `stock_agent/discord_alerts.py:342`
- What happens: One summary row is appended when the loop happens to tick
  between 16:05 and 16:30 Eastern on a weekday. The "already done today" flag
  lives in memory only.
  - If the bot is down, or a long job is running, during those 25 minutes,
    that day has no row, and the next "Day P&L" silently covers several days.
  - If the bot restarts inside the window after writing, it writes a second
    row for the same day.
  - Market holidays that fall on weekdays get a row (the bot has no holiday
    calendar; `scheduler.py:313` checks the weekday only).
  - The date is stored as display text (`"%b %d, %Y (%a)"`,
    `scheduler.py:1139`), not a sortable date.

  Drawdown and Sharpe are then computed row to row as if each step were one
  trading day. Separately, `calculate_stats` returns early with
  `max_drawdown: 0.0` and `sharpe: 0.0` whenever there is no recorded sell
  (`portfolio.py:285-298`), and the Sharpe value is never displayed because
  Discord looks up `sharpe_ratio` while the dictionary key is `sharpe`.
- Evidence: `if not trades: return {... "max_drawdown": 0.0, "sharpe": 0.0}`
  (`portfolio.py:285-298`); `if stats.get("sharpe_ratio") is not None:`
  (`discord_alerts.py:342`) against `"sharpe": sharpe` (`portfolio.py:342`).
  Sharpe uses no risk-free rate and sets the deviation to 1 when there is a
  single return (`portfolio.py:317`).
- Why it matters: The weekly "Max Drawdown" reads 0.0% until the first
  recorded sell, whatever the account has done. After that it is computed on
  a series that also carries the errors in F-11-04.
- How verified: Ran `calculate_stats` on four made-up rows (100k, 104k, 90k,
  95k): no sells gave `max_drawdown 0.0` against a true 13.5%; with one
  recorded sell it gave 0.1346. Recomputed the Sharpe formula on a made-up
  series: 9.34 clean, 6.72 with every row duplicated, 32.17 with two days
  missing.
- Direction (not a fix): Use the broker's daily equity history for any
  return, drawdown, or risk-adjusted figure.

### F-11-06: Trade records hold quoted prices, not fills, and lack the fields needed to analyse a trade
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1201`, `1295-1302`, `1320-1321`, `1328-1342`; `stock_agent/executor.py:95-111`, `117-124`, `145-151`, `171-176`; `stock_agent/models.py:102-112`
- What happens:
  - A buy is recorded at the signal's price, which is the data vendor's
    profile price when the analysis ran (`scheduler.py:658`, `775`, `990`).
    For the Sunday cycle that is Friday's close, used for Monday's open.
  - A sell is recorded at a vendor quote taken before the order is sent.
  - The fill is never fetched: `Executor.get_order` (`executor.py:171`) has
    no caller, and `place_buy` returns the "order accepted" response.
  - A trade record has symbol, action, shares, price, time, reason, P&L, P&L
    %, and hold days (`models.py:102-112`). It has no order ID, no conviction,
    no stop level, and no exit type.
  - Orders are sent without a `client_order_id` (`executor.py:95-101`,
    `117-124`, `145-151`), so in the broker's history a bot order cannot be
    told apart from one placed by hand. The message of commit `fc297ce` says
    stop orders were added by hand on eight positions, so hand-placed orders
    do exist in this account.
- Evidence: `price = signal.entry_price or 0` (`scheduler.py:1201`) flows
  into `Trade(... price=price ...)` (`scheduler.py:1295-1302`).
  `sell_price = quote["price"] if quote else position.current_price`
  (`scheduler.py:1321`) is set two lines before the order is placed.
- Why it matters: Per-trade P&L in the ledger is an approximation, and
  questions such as "how close to the stop did it fill?" or "which exits were
  the bot's?" cannot be answered from the bot's records. They need the
  broker's order and fill history plus the owner's memory of manual orders.
- How verified: Read the buy and sell paths end to end; grepped for callers
  of `get_order` and for `client_order_id` (none).
- Direction (not a fix): Record the broker's order ID and fill for every
  order, tag bot orders, and keep entry conviction and stop level on the
  trade record.

### F-11-07: Options results are never reported, and the options ledger's numbers are not reliable
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/options_portfolio.py:579-586`, `256-265`, `438-453`, `507-521`, `750-788`; `stock_agent/scheduler.py:1466-1496`, `1523-1574`; `stock_agent/options_engine.py:592`, `692`
- What happens:
  1. **Never reported.** The daily and weekly reports contain no options
     figure. `format_portfolio_summary_embed` (`options_portfolio.py:750`) has
     no caller. The expiry warning says so itself: "Unrealized P&L tracked
     internally only" (`scheduler.py:1562`).
  2. **Spreads are priced on one leg.** The refresh fetches both legs' quotes
     but uses only leg 1 (`options_portfolio.py:579-586`) and compares it with
     the net debit of the whole spread.
  3. **Entry price is an estimate.** It is the engine's mid-price estimate
     plus 5% (`options_engine.py:592`, `692`; `scheduler.py:1488-1489`), not a
     fill. The orders are market orders.
  4. **A failed or half-filled spread is booked as a total loss.**
     `place_spread_order` returns a pair, and `if order:` is true for any
     pair, including `(None, None)` (`scheduler.py:1486`). The position is
     recorded; at the next reconcile it is "not fully live", has not expired,
     and is closed as "missing at Alpaca (assumed expired OTM)" with a loss
     equal to the full debit (`options_portfolio.py:507-521`).
  5. **Nothing is closed before expiry** (see "How it actually works").
  6. **Assignment splits one result across two ledgers.** Made-up case: a put
     sold for 2.00 at strike 95 expires with the stock at 90. The options
     ledger books −300. The 100 assigned shares are then imported into the
     stock ledger at 95 and show −500 unrealised. Added together that is
     −800 for a real loss of −300. Nothing adds them today, which is the
     only reason this is not yet a wrong number in a report.
- Evidence: `quote = quotes.get(pos.contract_symbol)` then
  `current_price=quote.fair_value` (`options_portfolio.py:580-584`);
  `if order:` followed by `fill_price = getattr(signal,
  'max_debit_per_contract', None) or ...` (`scheduler.py:1486-1489`).
- Why it matters: Josh has no view of what options have contributed, and the
  one place that tracks it would mislead him. `total_realized_pnl` can
  contain losses on trades that never existed at the broker, and unrealised
  P&L on spreads is overstated from the first refresh.
- How verified: Ran `OptionsPortfolio` in a temporary directory with a fake
  quote source. A made-up 105/115 call spread bought for 3.00 with the long
  leg at 6.00 and the short leg at 3.00 showed, immediately: entry 3.15,
  current price 6.00, unrealised +285, 95% of maximum loss, auto-close reason
  "50% profit target reached", and options value 600 against a true 300.
  Reconciling the same spread against an empty broker position list gave
  realised −315 with reason "missing at Alpaca (assumed expired OTM)"; with
  only the long leg present at the broker the result was the same and the
  ledger no longer tracked the remaining leg. `bool((None, None))` is `True`.
- Direction (not a fix): Area 04 owns order handling. For performance,
  options results have to be rebuilt from broker fills and expiry,
  assignment, and exercise activities.

### F-11-08: Nothing the bot stores can show whether conviction worked or whether it beat an index
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:95`, `950-962`, `1151`, `1215-1222`; `stock_agent/portfolio.py:182-198`, `350-353`; `stock_agent/models.py:102-112`; `stock_agent/macro_monitor.py:407-416`
- What happens:
  - No index comparison exists anywhere in the code. The macro history keeps
    only the last 90 checks (`macro_monitor.py:415`), and a check runs every
    morning and every 30 minutes in market hours (`scheduler.py:330`,
    `1600`), so it covers about a week and a half.
  - Signals are never saved: the daily summary stores `signals=[]`
    (`scheduler.py:1151`), and the queue of pending buys is in memory
    (`scheduler.py:95`).
  - Blocked trades are a log line and a Discord message only
    (`scheduler.py:1215-1222`).
  - Entry conviction lives on the open position's thesis. The trade record
    has no conviction field, and the position is deleted on sale. The only
    surviving copy is inside whichever daily summaries captured the position.
  - The stored conviction is after the TipRanks, sentiment, and analyst
    adjustments (`scheduler.py:950-962`); the model's own score is not kept.
  - A position imported from the broker gets a placeholder thesis with
    conviction 7 and today's date as its entry date (`portfolio.py:182-198`).
  - `active_theses` keeps only the latest thesis per symbol
    (`portfolio.py:350-353`).
- Evidence: `conviction=7, summary="Imported from Alpaca — awaiting
  re-analysis."` and `entry_date=now` (`portfolio.py:185-186`, `198`).
- Why it matters: The strategy's central claim is that conviction scores
  carry information and that sizing by them pays. The bot keeps no record
  that could test this, and after any state loss or ledger drift every
  re-imported position looks like a conviction-7 trade opened that day.
  Commit `fc297ce` records at least eight imported positions.
- How verified: Read the models and every writer of the fields above.
- Direction (not a fix): A small append-only decision log (signal, scores
  before and after adjustment, risk verdict, order, fill, exit) and a daily
  index price would make these questions answerable.

### F-11-09: What the code says about stops, needed before reading any stop analysis
- Severity: Note
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1258-1259`; `stock_agent/risk_manager.py:151-164`, `205-225`; `stock_agent/models.py:58-72`, `96`; `stock_agent/portfolio.py:164`, `202`; `stock_agent/config.py:157-164`
- What happens:
  - **Every stop is exactly the floor.** The stop width depends on `beta`,
    read with `getattr(signal.thesis, "beta", None)`. The thesis model has no
    `beta` field and nothing sets one, so the floor is always used: 5% before
    the change on 2026-04-25, 7% after. The 12% ceiling cannot be reached.
  - **A position has three "stops" that can disagree.** The broker order is
    7% below the signal price (`scheduler.py:1259`). The bot's own 30-minute
    check uses 7% below the ledger entry price, which becomes the broker's
    average fill after a sync (`portfolio.py:164`, `risk_manager.py:215-216`).
    The `stop_loss` stored on the position is only ever written
    (`scheduler.py:1287`, `portfolio.py:202`) and never read.
  - **Widening the floor did not move stops already resting at the broker.**
    No code cancels or replaces an order (`executor.py:163-188` has no
    callers). The commit message of `fc297ce` says the same.
  - **Imported positions get no broker stop**, and their stored stop is 5%
    (`portfolio.py:202`), the old floor.
- Evidence: `Thesis` fields at `models.py:58-72`; `stop_pct =
  self.config.STOP_LOSS_PCT_MIN` in the no-beta branch
  (`risk_manager.py:161-162`).
- Why it matters: To judge whether the wider floor helped, trades must be
  split by entry date and by which stop actually took them out (broker order,
  the bot's check, or a hand-placed order), and bot-check exits compared with
  broker-stop exits. The ledger records only the bot-check kind.
- How verified: `get_stop_loss_price(100, None)` returns 93.0; a `Thesis`
  object has no `beta` attribute; with a signal price of 100 and a fill of
  103 the broker stop is 93.00 and the bot's check level is 95.79.
- Direction (not a fix): Areas 02 and 03 own this. The decision "volatility-
  adjusted, 7–12%" in `CLAUDE.md` is not what runs.

### F-11-10: Two P&L labels in the reports say something other than what is computed
- Severity: Low
- Confidence: Likely
- Where: `stock_agent/scheduler.py:1720-1724`, `1742`; `stock_agent/discord_alerts.py:255-262`, `270`
- What happens: The morning brief's "Yesterday P&L" is broker `equity` minus
  `last_equity`. At 08:15 Eastern that is the change since yesterday's close
  (overnight and pre-market), not yesterday's result. The Discord daily
  summary's "Trades Today" shows wins over all trades, and buys are in the
  total with no P&L, so three buys read "0/3".
- Evidence: `day_pnl = equity - last_equity` (`scheduler.py:1723`);
  `wins = sum(1 for t in trades if t.pnl and t.pnl > 0); total = len(trades)`
  (`discord_alerts.py:258-259`).
- Why it matters: Small, but both sit next to correct numbers and read as
  results.
- How verified: Read the code. The meaning of `last_equity` is from the
  broker's documentation and was not checked against the live API, hence
  "Likely".
- Direction (not a fix): Rename or recompute; area 08 owns the messages.

### F-11-11: The snapshot script, as written, cannot answer several of this area's questions
- Severity: Note
- Confidence: Confirmed
- Where: `scripts/vps_snapshot.sh:26`, `92-94`, `135-136`; `scripts/alpaca_readonly_report.py:36`, `104-107`, `139-162`
- What happens:
  - The scripts are on the setup branch only. `origin/master` is `e92e1cd`,
    so they are not on the droplet until that branch is merged (which
    restarts the bot) or the two files are copied over by hand.
  - The journal is collected for 21 days by default (`LOG_DAYS`, line 26) and
    capped at the last 250,000 lines (line 136). Start/stop history is fixed
    at 120 days (line 93). The bot has run since at least early April, so
    uptime, errors, and API counts cover only the recent part of its life.
  - No index prices are collected, so "against an index fund" has no data.
  - No price history is collected for sold symbols, so "what the price did
    afterwards" has no data.
  - No kernel log is collected, so an out-of-memory kill on the 1 GB server
    would show only as an unexplained restart.
  - Account activities stop at 60 pages of 100 with no warning that the list
    was cut (`alpaca_readonly_report.py:36`, `144`).
  - API cost in money cannot come from logs at all: the token `usage` block
    of each model response is discarded (`analyst.py:42-45`,
    `screener.py:44-47`).
- Evidence: `LOG_DAYS="${LOG_DAYS:-21}"`; `--since '120 days ago'`;
  `tail -n 250000`; `MAX_PAGES = 60`.
- Why it matters: Knowing this before the first run saves a round trip.
- How verified: Read both scripts. Ran `build_summary` with made-up data to
  confirm its arithmetic (see "Checked and fine").
- Direction (not a fix): See "Could not verify" for how to run it with wider
  windows and what else to bring.

### F-11-12: The repository's own history makes performance claims that are unverified and disagree with each other
- Severity: Note
- Confidence: Confirmed (that the claims exist), not verified (that they are true)
- Where: `stock_agent/config.py:157-161`; commit messages of `fc297ce` and `4fa68de`; `CLAUDE.md` ("Decisions already made")
- What happens: Three statements about live results are written into the
  repository. (1) As of 2026-04-25, five of five sells were stop-outs, each a
  small loss, one of them about 0.6% under its stop level. (2) Before
  2026-04-23 one symbol grew far past its cap because the ledger undercounted
  the shares held: the commit message says 45% of the portfolio, `CLAUDE.md`
  says 76%. (3) Eight positions had no broker stop until stops were placed by
  hand.
- Evidence: the comment at `config.py:157-161`; `git show fc297ce`;
  `git show 4fa68de`.
- Why it matters: These are the only performance facts available without a
  snapshot. They are leads to test, not findings. Note (1) counts only sells
  the ledger recorded (F-11-01), and the 45% and 76% figures cannot both
  describe the same moment. No code sells down a position that is over the
  cap, so whether that concentration still exists is a question only the
  snapshot answers. These texts also put trade details in a public
  repository.
- How verified: Read the comment and the two commit messages.
- Direction (not a fix): Check each against the broker history once the
  snapshot exists.

## Checked and fine

- Win-rate arithmetic and its zero guards are correct for the trades it is
  given (`portfolio.py:283`, `300`, `334`).
- The drawdown loop is a correct peak-to-trough calculation on whatever
  series it receives (`portfolio.py:321-329`); 100k/104k/90k/95k gives 13.46%.
- After a sync, per-position unrealised P&L and percentage are the broker's
  own fields (`portfolio.py:165-169`).
- The morning brief's equity, cash, and exposure come straight from the
  broker (`scheduler.py:1719-1729`).
- "Trades today" compares a UTC timestamp with an Eastern date
  (`scheduler.py:1132-1136`). US market hours never cross midnight UTC, so
  the dates agree for anything traded in the session.
- Expired option legs are valued at intrinsic value, as decided
  (`options_portfolio.py:269-285`, `463-484`, `515-521`). Made-up 105/115
  call spread bought at 3.00: −300, +200, +700 with the stock at 100, 110,
  120. Short 95 put sold at 2.00: +200 at 100, −300 at 90. All as expected,
  given a correct entry price (see F-11-07 item 3).
- The snapshot's Alpaca report sends GET requests only, through one function
  (`alpaca_readonly_report.py:74-87`), and blanks the account number and ID
  (`alpaca_readonly_report.py:37`, `89-95`).
- The report's own summary arithmetic is right: made-up equity series
  1000/1100/990/1045 gives "change over the period 4.50%" and "largest drop
  from a peak −10.00%", and the stop cross-check correctly flagged one stop
  with no position and one position with no stop
  (`alpaca_readonly_report.py:234-257`, `270-283`).
- The snapshot refuses to write inside the repository, redacts `.env` values,
  and fails closed if a secret survives (`vps_snapshot.sh:36-40`, `282-335`).
  `.gitignore` excludes snapshot names.
- `python3 -m pytest -q` passes on the audited commit.

## Could not verify

Nothing about actual performance could be verified. No snapshot was uploaded,
and no other source of account data was used. Item by item:

| # | Question | Status | What the snapshot would supply | Still missing with a default run |
| --- | --- | --- | --- | --- |
| 1 | Uptime, restarts, reasons | Not answered | `service/status.txt`, `service/show.txt`, `service/start-stop-history.txt`, `deploy/auto_update.log.tail`, `system/reboots.txt`, `logs/journal.log` | Anything before 120 days (start/stop) or 21 days (reasons); the kernel out-of-memory log; any sign of life outside market hours, because an idle tick logs nothing. In market hours "Running market monitoring" (`scheduler.py:200`) appears every 30 minutes and works as a heartbeat. The dates in `daily_summaries` give a whole-life proxy for "alive at 16:05–16:30 Eastern". |
| 2 | Return and drawdown against an index fund | Not answered | `alpaca/portfolio_history.json` (one year, daily), `alpaca/account.json`, non-trade rows of `alpaca/activities.json` | Daily closes of the chosen index fund for the same dates (not collected); the date and balance the bot started with; whether the paper account was ever reset. The bot's own daily series must not be used (F-11-04, F-11-05). |
| 3 | Trade count, win rate, average win and loss, holding periods, how closed | Not answered | `alpaca/orders.json`, fills in `alpaca/activities.json`, reasons in `state/portfolio.json` → `trade_history` | Which orders were placed by hand (F-11-06). The ledger's own trade list is a subset (F-11-01). |
| 4 | Stop-outs, fill against stop, price afterwards, did widening help | Not answered | Stop orders in `alpaca/orders.json` with stop price, fill price, fill time, and expiry | Daily prices of each stopped-out symbol for the weeks after the exit (not collected); when commit `fc297ce` reached the droplet, if `auto_update.log` no longer reaches back to April. Read with F-11-09. |
| 5 | Did higher conviction do better | Not answered | `state/portfolio.json`: `positions[].thesis.conviction` and the copies inside `daily_summaries[].positions`, joined to broker round trips | Conviction for positions opened and closed between two daily summaries; the model's score before adjustments; anything real for imported positions, which all read 7 (F-11-08). The sample may be too small to conclude anything. |
| 6 | Limits exceeded; concentration today | Not answered | `alpaca/positions.json`, `alpaca/account.json`, `env-summary.txt` for the limits in force; sectors and clusters from `config.py:71-146` | History of holdings by day: needs fills plus daily closes per symbol (not collected). The ledger's daily snapshots are an approximation and were wrong during the incident in F-11-12. |
| 7 | Orphan, duplicate, rejected orders; stops without positions; positions without stops | Not answered | `alpaca/orders.json`, the stop cross-check in `alpaca/summary.md`, `alpaca/positions.json` | Nothing further. |
| 8 | Options: opened, closed, assigned, expired unmanaged, result | Partly answered from code | Option orders and fills; expiry, assignment, and exercise rows in `alpaca/activities.json`; `state/options_portfolio.json`; option level fields in `alpaca/account.json` | Nothing further. From code alone: every option position has been left to run to expiry since commit `602aa96` (2026-04-08); whether any was assigned or exercised is unknown. |
| 9 | Ledger against broker now | Not answered | `state/portfolio.json` and `state/options_portfolio.json` against `alpaca/positions.json` and `alpaca/account.json` | Nothing, if the snapshot is taken outside market hours so the two sides describe the same moment. |
| 10 | Most frequent errors and warnings | Not answered | `logs/digest.txt`, `logs/journal.log` | Anything older than 21 days. The digest counts ERROR and WARNING only; option reconcile closures are INFO and need a search of the journal. |
| 11 | Monthly API cost | Not answered | Request lines in `logs/journal.log`, counted by host | Token counts are never logged, so money figures need the Perplexity and OpenAI usage pages and the plan prices for FMP, Finnhub, TipRanks, and the droplet. |

**How to run the snapshot for this area.** Run it after the market close or
at a weekend. Widen the journal window with the supported override, for
example `LOG_DAYS=240 bash scripts/vps_snapshot.sh` (`vps_snapshot.sh:26`);
the 250,000-line cap and the 120-day start/stop window are fixed in the
script. Getting the scripts onto the droplet by merging restarts the bot; the
queue of Sunday's buy signals exists only in memory until Monday's open
(`scheduler.py:95`, `437`), so that window is the wrong time to merge.

**What the code predicts the snapshot will show.** These are hypotheses to
test, not results.

- Fewer sells in `trade_history` than filled sell orders at the broker
  (F-11-01).
- One "Sell execution failed" error per recorded sell since 2026-04-23
  (F-11-02).
- Stop prices 5% below the signal price for buys before 2026-04-25 and 7%
  after, with no other widths except hand-placed orders (F-11-09).
- Days missing from `daily_summaries`, and possibly doubled days (F-11-05).
- Positions whose thesis summary begins "Imported from Alpaca", all with
  conviction 7 (F-11-08).
- Closed option positions with the reason "missing at Alpaca (assumed expired
  OTM)" dated within a day of opening (F-11-07).
- Rejected bot sells while a stop order was resting, and possibly stock
  positions with a negative quantity that are absent from the ledger. Both
  follow from the first handoff below and are the first thing to look for.

**How API volume can be counted (question 11).** With the bot's logging
set-up (`__main__.py:9-12`) the HTTP library writes one INFO line per request
with the full URL. Checked with a mock transport and made-up URLs on httpx
0.28.1; `requirements.txt` allows 0.24 or later and the droplet's version is
in `deploy/pip-freeze.txt`. TipRanks uses a different library and shows only
as "TipRanks fetch:" lines. The code's call pattern, to compare against the
counts (from reading the code, not measured):

| Unit | Calls |
| --- | --- |
| One company's fundamentals (`data_feed.py:107-190`) | 9 FMP + 1 Finnhub |
| One universe screen (`data_feed.py:281-302`) | 7 FMP |
| One macro check (`macro_monitor.py:179-184`, `234`) | 7 FMP |
| Screening n companies (`screener.py:91-96`) | n ÷ 5 OpenAI, rounded up |
| Deep analysis of k symbols (`scheduler.py:853-1001`) | k Perplexity (larger model) + 4k Finnhub |
| Each held position, each weekday | 1 Perplexity (smaller model) in the pre-market scan plus 1 in each 30-minute check that falls in an even Eastern hour (`scheduler.py:585`, `1056-1060`, `1103-1108`) |
| Each held position, Sunday and Wednesday | 1 Perplexity (larger model) (`scheduler.py:1013`) |

The pre-market scan and the options scan each fetch fundamentals for 30
symbols every weekday (`scheduler.py:634`, `1427-1431`). A restart inside a
job's time window repeats that job, because the "done today" flags are in
memory.

## Questions for Josh

1. Can you run the snapshot as described above and upload it? Every numbered
   question in this area waits on it.
2. When did the stock agent first trade on this Alpaca account, with what
   starting balance, and has the paper account ever been reset?
3. Do you place, change, or cancel orders by hand in the Alpaca dashboard?
   The commit history says stops were added by hand at least once.
4. Which yardstick do you want: the S&P 500, the total US market, or the
   Nasdaq-100?
5. Have you been receiving "Sell failed for …" messages on Telegram? If so,
   roughly how many? That would confirm F-11-02 in production.
6. Can you export the Discord trade-history and daily-P&L channels? They are
   the only long-run record of what the bot told you.
7. Should options be judged together with stocks or separately, and are
   options meant to continue at all?
8. Can you share last month's usage or invoice pages for Perplexity, OpenAI,
   FMP, Finnhub, and TipRanks? The logs cannot give a money figure.
9. Which is right for the concentration incident, 45% or 76%, and has that
   position since been reduced?

## Handoffs

- 03: A bot-initiated market sell never cancels the resting stop first (`executor.py:163` has no caller; sell at `scheduler.py:1323`). While the stop is open the broker is likely to reject the sell for lack of available shares; once the stop has filled, the bot's own stop check (`scheduler.py:1047-1053`) can sell the same quantity again, which would open a short that the sync then discards (`portfolio.py:158-162`, `177-181`). Not verified against the broker; potentially Critical.
- 03: Stop orders are never renewed or amended, and imported positions never get one (`portfolio.py:193-206`). Whether the broker expires long-lived stop orders was not checked.
- 03: The buy's fill is never read back; the stop is sent three seconds later for the ordered quantity at a level based on the signal price (`executor.py:104-134`).
- 03, 08: The missing `_tipranks_lookup` attribute at `scheduler.py:1349` (F-11-02).
- 02: `beta` is never set, so stops are always the 7% floor (F-11-09). Nothing sells down a position above the 15% cap; the "trim" in the comment at `config.py:47-49` does not exist. The day-trade check reads only the ledger (`risk_manager.py:166-198`), so broker stop-outs are not counted.
- 02, 07: Position sizing and risk weights use the ledger value criticised in F-11-04 (`scheduler.py:1225`, `risk_manager.py:38-40`).
- 04: `if order:` is always true for spreads (`scheduler.py:1486`). Legs are separate market orders (`options_executor.py:160-176`). A spread with one leg at the broker is closed in the ledger and the other leg is no longer tracked (`options_portfolio.py:438-453`, `507-510`). Nothing closes options before expiry, so an in-the-money long leg can become 100 shares per contract. Options endpoints are hard-coded to the paper URL (`options_executor.py:27`, `options_data.py:28`).
- 04, 07: On assignment one result is split across two ledgers (F-11-07 item 6).
- 07: A top-up does not debit cash (`scheduler.py:1271-1278`). A failed load starts a fresh state and loses the trade history and daily summaries (`portfolio.py:25-42`). `starting_capital` has no writer.
- 08: A broker-side exit produces only a journal warning (`portfolio.py:151-154`). The expiry alert repeats every 30 minutes for three days (`scheduler.py:1552-1571`). `STOCK_LOG_LEVEL` is read but never applied (`config.py:214`, `__main__.py:9-12`). Several embeds hard-code "Paper Mode" (`discord_alerts.py:197`, `243`, `311`), the buy explainer says 5% (`discord_alerts.py:550`), and the startup embed labels current cash "Starting Capital" (`discord_alerts.py:134`).
- 09: The HTTP library logs every request URL to the journal at INFO, which includes the `apikey` and `token` query parameters (`data_feed.py:40`, `59`; `macro_monitor.py:138`, `155`; `news_feed.py:37`; `sentiment.py:31`; `analyst_trends.py:34`) and the Telegram bot token in the URL path (`telegram_alerts.py:42`). Shown with a mock transport.
- 09: `.env.example:2-3` holds what look like real values for `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Default credential values are hard-coded at `config.py:194-195` and `tipranks_client.py:18-19`. Commit messages and `config.py:157-161` contain trade details.
- 01: Queued buy signals and all "done today" flags are in memory only (`scheduler.py:89-98`). The daily summary window is 25 minutes (`scheduler.py:311-320`). There is no market-holiday calendar (`scheduler.py:297-302`). `_should_check_events` means "even Eastern hours", not "every other cycle" (`scheduler.py:1103-1108`).
- 05, 06: Model token usage is discarded (`analyst.py:42-45`, `screener.py:44-47`). Fundamentals cost ten HTTP calls per symbol with no cache (`data_feed.py:107-190`). Recorded buy prices come from the vendor's profile price at analysis time (`data_feed.py:199`).
- 10: Unused code met on the way: `get_sector_exposure` and `get_equity_curve` (`portfolio.py:251-273`); `format_portfolio_summary_embed`, `get_auto_close_candidates`, and `close_position` in `options_portfolio.py`; the first-run block at `scheduler.py:149-154`.
