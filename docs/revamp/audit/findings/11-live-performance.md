# 11. Live performance

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

**Updated 2026-11-21 with live evidence.** The first version of this file was
written without a server snapshot. Two evidence files now sit in
`docs/revamp/audit/evidence/`, built from a read-only snapshot of the server
and the broker's order history. This summary and the next section use them;
the findings below are unchanged except where a note says otherwise.

On paper the bot is up 24.0% since launch (30 March to 19 November 2026),
against 14.7% for QQQ and 12.9% for SPY. That headline hides three things.
First, all of the gain came before late July: the bot was up 41.8% by 24 July
and has lost 12.6% since, twice the fall in QQQ. Second, research has almost
certainly produced nothing since late July: Perplexity has refused every call
for lack of quota, the price feed refuses many requests, and the bot kept
sending normal-looking daily briefs. Third, the bot's own reports say +36.0%,
about 12 points too high, mainly because two accidental short positions,
opened when a stop-out was sold twice, are missing from its books. The risk
limits did not hold in practice: the largest position was above the 15% cap on
122 of 164 trading days, because shares delivered by option exercise bypass
every limit. No stock position has a stop order at the broker today, and the
bot's own stop check depends on a price feed that is partly refused. The
sample is small (38 stock entries, 43 option structures, one strong technology
rally), so none of this proves the strategy good or bad.

## Answers from the live evidence (added 2026-11-21)

Sources: E1 = `evidence/2026-11-20-live-performance.md`, E2 = `evidence/2026-11-21-answers-to-audit-unknowns.md`. Section numbers refer to those files. Everything
here is an observation from them; nothing was re-measured in this session.

| # | Question | Answer | Source |
| --- | --- | --- | --- |
| 1 | Uptime and restarts | Server up 229 days. Six service restarts in the two-month log window, each back within seconds, no crash loop. 25 "git fetch failed" lines in the update log, in July. The log starts on 20 September, so earlier restarts are unknown. | E1 §7 |
| 2 | Return and drawdown against an index | +24.0% against QQQ +14.7% and SPY +12.9%. Launch to 24 July: +41.8% against +21.6% and +16.5%. 24 July to 19 November: -12.6% against -5.7% and -3.1%. Largest fall from a peak 15.6% (QQQ 15.1%, SPY 8.1%). Volatility 30.4% a year against 23.9% and 14.6%. Peaked 22 June; 15.3% below that now. | E1 §1 |
| 3 | Trades, win rate, how closed | 38 thesis stock entries, 21 profitable (55%). Average winner +21.7%, average loser -6.5%. Three entries made 67% of the stock profit. Technology won 11 of 12 and made about 95% of it; Healthcare won 1 of 8. 25 stop exits. Holding periods were not measured. | E1 §6 |
| 4 | Stops | 25 stop exits: 13 by broker order, 12 by the bot's own check. The price was lower 20 trading days later in only 10 of 25; against holding 20 more days the stops cost about 5 points in total. How close fills were to the stop price was not measured, and the effect of the 5% to 7% widening was not separated out. | E1 §6 |
| 5 | Did conviction work | Yes, on a small sample. Score 9: 5 winners of 7, average +22.2%. Score 8: 14 of 25, average +8.0%. Score 7: 2 of 6, average -1.5%. | E1 §6 |
| 6 | Limits in practice | Not held. Largest position above 15% on 122 of 164 days; one stock is 33.6% of equity today. One sector above 40% on 121 days. Long plus short stock above 100% of equity on 49 days, peaking at 154% with one stock at 71.5%. Cash negative on 40 days. Position count stayed inside its limit. Limits held for stock buys and were not applied to shares arriving through options. | E1 §5 |
| 7 | Orphan, duplicate, rejected orders | 174 orders: 166 filled, 8 cancelled, none recorded as rejected (an order refused at submission leaves no record). Two double sales, each of which opened a short. No stop order is open today, so there are no orphan stops and no position has a broker stop. Six April stops were cancelled, five of them 88 to 90 days after placement. Other duplicates were not checked. | E2 §1, §4; E1 §4 |
| 8 | Options | 43 structures, 19 profitable, mostly bull call spreads. Those expiring in April made about +15.6 points; everything since lost about 8.0. The scan repeats the same spread on a list frozen at four symbols since April; on one symbol it has lost seven times in a row. Exercise and assignment delivered shares that made +3.6 points and caused the limit breaches in question 6. All 91 option orders were market orders. | E1 §1, §6; E2 §4 |
| 9 | Ledger against broker | They disagree. Ledger: 6 positions, 56 trades, return +36.0%. Broker: 9 stock positions, 5 open option legs, 166 filled orders, return +24.0%. The two shorts, a fractional remainder and every open option leg are missing from the ledger. | E1 §2 |
| 10 | Most frequent errors | Two months of log: FMP "payment required" about 12,000 and "too many requests" about 7,900; Perplexity quota exceeded 2,756 with no successful call; "Empty re-analysis response" 235; "Telegram not configured" 77; options "invalid underlying symbols" 51; "0 signals" 36; OpenAI model deprecated 29; TipRanks 401 on 21. | E1 §3 |
| 11 | API cost | Not measured. | E1 limits |

### Findings in this file seen in production

| Finding | What the evidence shows | Source |
| --- | --- | --- |
| F-11-01 | 13 exits were broker stop fills, which the ledger does not record; the ledger holds 56 trades against 166 filled orders. | E1 §2, §6 |
| F-11-02 | The `_tipranks_lookup` error appears on 1 October, 26 October and 5 November (9 log lines). The sale goes through and the steps after it do not run. | E1 §4 |
| F-11-04 | The ledger's value is about 12 points too high and leaves out shorts and options. | E1 §2 |
| F-11-07 | No open option leg is in the local ledger, while the broker holds five. | E1 §2 |
| F-11-08 | The conviction and index comparisons above had to be rebuilt from broker fills outside the bot; nothing the bot stores could produce them. | E1 intro |
| F-11-09 | Broker stop orders exist for two April days only: 11 placed by the bot after fast fills and 8 sent together by something outside its normal run. Since then stops have been software-only. | E1 §4; `2026-11-21b-stop-orders-and-telegram.md` §2, §3 |
| F-11-12 | The concentration incident is confirmed in outline: one stock reached 71.5% of equity on 22 April after exercised call spreads delivered shares. | E1 §5 |

The first two predictions at the end of this file ("fewer sells in the ledger
than at the broker", "one error per recorded sell") are borne out. The
prediction of rejected sells while a stop rests could not be tested: an order
refused at submission leaves no record, and the log does not reach back to
the months when stop orders existed.

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

### F-11-13: Research and price inputs have been failing for months and every report looked normal
- Severity: High
- Confidence: Confirmed (observed in production)
- Where: `docs/revamp/audit/evidence/2026-11-20-live-performance.md` section 3; code causes are in F-05-04, F-06-01, F-08-05 and F-08-11.
- What happens: Since at least 20 September no Perplexity call has succeeded, FMP has refused about 20,000 requests, the OpenAI model has been reported as deprecated since 25 October, and TipRanks returns "unauthorized". The last new stock entry was 17 July and the newest stored thesis is from late July. That research stopped in late July, before the log begins, is the evidence file's most likely reading, not a logged fact.
- Evidence: 2,756 quota errors, about 12,000 "payment required" and 7,900 "too many requests" responses, 235 empty re-analysis replies and 36 research cycles ending in "0 signals" in two months. Entries per month fell from 24 in April to none after July.
- Why it matters: The bot has been holding positions for four months with no working research, and its software stop depends on a price feed that is partly refused (software stops did still sell on three days in the log window). Nobody was told. Results since late July say little about the strategy.
- How verified: Read from the evidence file; not re-measured. Added 2026-11-21.
- Direction (not a fix): A health check on every input, an alert when one fails, and no new trades while research or prices are down.
- Verification: Confirmed: every count and date matches section 3 of the evidence file and High fits, with three wording caveats: the evidence gives refusal counts but never says "most" FMP requests are refused, the OpenAI deprecation notice starts on 25 October rather than 20 September, and "a software stop that mostly cannot see prices" goes beyond the evidence (section 4 records software stops selling on three days inside the same log window), while the period before 20 September is the evidence file's "most likely reading", not a logged fact.

### F-11-14: Telegram reports "not configured" although its settings are present on the server
- Severity: Withdrawn (was High)
- Confidence: Explained
- Where: `stock_agent/telegram_alerts.py:31-33`; evidence file `docs/revamp/audit/evidence/2026-11-21b-stop-orders-and-telegram.md` section 1.
- What happens: **Withdrawn 2026-11-21.** Both Telegram lines exist in the server's settings file and both values are empty. The code skips sending when either is empty and logs "Telegram not configured", so the log line is the code working as written. There is nothing unexplained.
- Evidence: The earlier evidence file said the settings "are present in the file"; the follow-up corrects that wording.
- Why it matters: What remains is a fact for Josh, not a defect: no message has gone to Telegram in the two-month log window, and Discord is the only channel that delivers anything. The Telegram value in `.env.example` is not the one in use on the server; it still needs revoking if that bot exists (F-09-01).
- How verified: Read from the follow-up evidence file and the three code lines. The ID is kept so the numbering does not change.
- Direction (not a fix): None for this finding. Whether Telegram should be switched back on is decision 26 in `AUDIT.md`.
- Verification: Confirmed: the withdrawal is justified, because `stock_agent/config.py:17-18` reads both Telegram settings with an empty default, `stock_agent/telegram_alerts.py:31-33` logs "Telegram not configured" and returns when either is empty, and section 1 of the follow-up evidence file says both values on the server are empty, so the 77 log lines are the code working as written.

### F-11-15: Every buy made at the open has gone without a broker stop order
- Severity: High
- Confidence: Likely (the pattern is exact; the broker's refusal itself was not captured)
- Where: `stock_agent/executor.py:113-131`; evidence file `docs/revamp/audit/evidence/2026-11-21b-stop-orders-and-telegram.md` sections 2 and 3; earlier evidence files, section 4 of each.
- What happens: Updated 2026-11-21 (the title was "Broker stop orders were placed on only two days, although the code sends one after every buy"). The stop is sent three seconds after the buy. Of the 30 stock buys filled since that code arrived on 6 April, the 11 that filled within three seconds all have a stop at the broker, and the 19 that took longer (52 seconds to about ten minutes) have none.
- Evidence: The 11 fast fills were all on 23 April, sent in the middle of the session. The 19 slow fills were all sent in the first two minutes after the open, on nine different days. The split is exact. The other eight of the 19 stop orders in the history were sent together at 04:01 New York time on 27 April for positions already held, through the API key, with no buy before them. Nothing in the code places a stop except straight after a buy, so those eight came from outside the bot's normal run, most likely a one-off script. Six stops were later cancelled, five of them 88 to 90 days after placement, most likely expired by the broker.
- Why it matters: This is F-03-05 seen in production. The bot normally buys at the open, which is exactly when the fill is slow, so in normal running a new position gets no broker stop, and the buy alert still shows a stop price. No stop order is open today.
- How verified: Read from the evidence files, plus `git log` for the stop code (commit `bfe42f0`). The likeliest cause, that the broker refuses a stop sent before the buy has filled and keeps no record of it, is not confirmed: the log window holds no buys, so the "Stop-loss order FAILED" line could not be counted.
- Direction (not a fix): Send the stop only after the fill is confirmed, read it back, and alert when a position has none. One saved broker response to an early stop would confirm the cause.
- Verification: Confirmed: every number matches sections 2 and 3 of the follow-up evidence file, and the code agrees (`stock_agent/executor.py:113-132` sends the stop after a fixed three-second sleep with no fill check and only logs a warning if it fails, it is the only place a stop order is built, its only caller is `stock_agent/scheduler.py:1264`, and queued buys are released as soon as the market opens at `scheduler.py:194-196`); the broker refusing an early stop remains the likeliest cause and is still unproven, so "Likely" and High are both right.

### F-11-16: The broker's portfolio-history figure has left out one long position since 18 August
- Severity: Medium
- Confidence: Confirmed (observed), cause unknown
- Where: `docs/revamp/audit/evidence/2026-11-20-live-performance.md` section 2.
- What happens: The broker's portfolio-history endpoint under-reports equity by about 7% from 18 August, while its account and positions endpoints include the position.
- Evidence: A rebuild from every fill, dividend and fee matches the broker's share counts and cash exactly and is within 0.1% of the corrected equity.
- Why it matters: Portfolio history cannot be used as the source of truth for equity or drawdown. No code finding explains it; it is on the broker's side.
- How verified: Read from the evidence file. Added 2026-11-21.
- Direction (not a fix): Report equity from the live account figures.
- Verification: Confirmed: the statement matches section 2 of the evidence file, but nothing in `stock_agent/` calls the portfolio-history endpoint (the only caller is the read-only report script, `scripts/alpaca_readonly_report.py:106`), so this affects how performance is measured and not what the bot does or reports. Suggested severity: Note

### F-11-17: The options scan trades a list frozen in April and retries a renamed ticker
- Severity: Medium
- Confidence: Confirmed (observed), code path not traced
- Where: `docs/revamp/audit/evidence/2026-11-20-live-performance.md` sections 3 and 6.
- What happens: Corrected 2026-11-21 after verification. The evidence says the last successful TipRanks file is dated 14 April, the options universe has been four symbols since, and the scan repeats the same bull call spread on them about every three weeks. The cause first written here (a saved list with no age check) does not match the code. The scan takes its symbols from the universe saved in `portfolio.json` (`scheduler.py:1426-1427`), which is rewritten at every screen. When the FMP screener is refused, the screen returns exactly the four hard-coded priority symbols (`data_feed.py:337-344`, `config.py:63-68`), which is F-06-09. The spreads are then triggered by stored theses of any age, which is F-04-09. Whether the server's four symbols are that priority list is not confirmed, and it does not explain the 14 April date. One ticker renamed on 9 June has produced 51 "invalid underlying symbols" errors.
- Evidence: Seven losses in a row on one symbol since July; a second symbol net negative over eight structures.
- Why it matters: A failed screen quietly shrinks the universe to four names, and months-old theses keep triggering real option orders on them. F-06-09 and F-04-09 cover the two halves; this finding records that together they have been trading in production.
- How verified: Read from the evidence file; the code that loads the list was not traced in this pass. Added 2026-11-21.
- Direction (not a fix): Treat a failed screen as a failure, not as a four-symbol universe, and refuse to open options on a thesis older than a set age. An age check on the universe alone would not fire, because the list is re-saved each week.
- Verification: Unclear: the observations match sections 3 and 6 of the evidence file, but the traced code contradicts the explanation: no code reads a saved TipRanks file (`scheduler.py:825-838` calls the API each time and saves nothing, and that client only arrived on 23 April), the options scan takes its symbols from the first 30 of the universe saved in `portfolio.json` (`scheduler.py:1426-1427`), that list is rewritten by every weekly, midweek and event screen (`scheduler.py:402`, `519`, `1672`), and when the FMP screener is refused `screen_universe` returns exactly the four hard-coded priority symbols (`data_feed.py:337-344`, `config.py:63-68`, run here with a failing fake, already F-06-09), so the list is most likely re-saved fresh each week rather than frozen and an age check on it would not fire; no age check exists anywhere (`last_universe_refresh` is written at `portfolio.py:365` and never read, and the conviction that triggers a bull call spread comes from a stored thesis of any age at `scheduler.py:1435-1440`, already F-04-09); still unknown are whether the server's four symbols are that priority list and why the evidence dates the freeze to 14 April, ten days before priority symbols were added. Suggested severity: High

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

Updated 2026-11-21. Most of this section's original table is answered above.
What is still open:

- **Before 20 September 2026.** The service log starts there. Restart history,
  error counts and the cause of anything earlier cannot be read from logs.
- **Holding periods** and **how close stop fills were to the stop price.** Not
  measured. Needs the fills joined to stop prices.
- **Whether widening the stop floor from 5% to 7% helped.** The evidence gives
  one total for all 25 stop exits, not a before-and-after.
- **Rejected orders.** An order refused at submission leaves no record at the
  broker, and the log covers two months only.
- **Duplicate orders** other than the two double sales. Not checked.
- **API cost.** Needs the providers' billing pages.
- **Why the broker's portfolio-history figure leaves out one long position
  since 18 August.** Cause unknown; see F-11-16.
- **Total-return comparison.** Index figures are price change only, without
  dividends.

## Questions for Josh

1. Answered 2026-11-21: the snapshot was run and summarised in
   `docs/revamp/audit/evidence/`.
2. When did the stock agent first trade on this Alpaca account, with what
   starting balance, and has the paper account ever been reset?
3. Mostly answered 2026-11-21: of 174 orders, 170 came through the API key,
   3 were placed by the broker to close expiring spreads, and 1 (a
   fractional-share buy on 16 April) did not come through the API key. Still
   open: do you remember what sent the eight stop orders at 04:01 on
   27 April, and did you place the fractional buy?
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
