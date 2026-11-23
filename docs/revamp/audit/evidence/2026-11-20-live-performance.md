# Live evidence: what the running bot did, launch to 19 Nov 2026

Collected: 2026-11-20    Code on the server: `e92e1cd` (same as `master`)    Mode: paper

## What this file is

Facts measured from the running bot, outside any Claude Code session, from a
read-only snapshot of the server made with `scripts/vps_snapshot.sh` on
20 Nov 2026: the broker's own records (orders, fills, account activities,
positions, portfolio history), the bot's local ledger, and the service log.
Index comparisons use daily closing prices for QQQ and SPY.

The snapshot itself is private and is not in this repository. Josh holds the
full report and a data workbook with the underlying numbers. Following the
audit playbook, everything here is a percentage, a count, or a ratio. "Points"
means percentage points of the starting balance.

## How to use it

- Treat each statement as **observed in production**. It says what happened,
  not why. Tracing the cause in the code, with file and line, is the audit's
  job. An observation that the code cannot explain is itself a finding.
- Area 11 should use this file to answer its numbered questions instead of
  reporting "no snapshot". Areas 02, 03, 04, 05, 06, 07, 08 and 09 each have
  observations below that belong to them.
- The plan must address every observation under "What the plan has to cover",
  or list it under "Not doing" with a reason.
- Do not add account values, order IDs, or a position-by-position listing to
  this file or to anything that quotes it.

## Limits of this evidence

- Paper account, 164 trading days, one strong technology rally. Small sample:
  38 thesis-driven stock entries and 43 option structures.
- The service log only goes back to 20 Sep 2026. Nothing before that date can
  be explained from logs.
- Index figures are price change only, without dividends, from the close on
  the last trading day before launch.
- Not measured: holding periods, how close stop fills were to the stop price,
  rejected orders, and API cost.

## 1. Results (area 11, questions 2 and 3)

| Measure | Bot | QQQ | SPY |
| --- | --- | --- | --- |
| Return since launch (30 Mar to 19 Nov) | +24.0% | +14.7% | +12.9% |
| Launch to 24 Jul | +41.8% | +21.6% | +16.5% |
| 24 Jul to 19 Nov | -12.6% | -5.7% | -3.1% |
| Largest fall from a peak | -15.6% | -15.1% | -8.1% |
| Annualised volatility | 30.4% | 23.9% | 14.6% |

- Beta to QQQ is 0.95. The account peaked on 22 Jun and is 15.3% below that
  peak.
- By month, bot against QQQ: Apr +20.4 / +15.7, May +14.4 / +10.6,
  Jun +5.0 / -0.3, Jul -6.0 / -6.6, Aug +1.4 / +4.2, Sep +0.9 / +3.2,
  Oct -5.6 / -6.7, Nov to the 19th -6.3 / -6.6.
- Where the gain came from, in points: thesis stock entries +14.2, shares
  delivered by option exercise or assignment +3.6, options +7.5, accidental
  short positions -1.3.

## 2. The bot's own numbers are wrong (areas 03 and 07; area 11, question 9)

- The local ledger and the daily Discord brief report a return of +36.0%.
  The broker-based figure is +24.0%. The bot overstates its value by about
  12 points.
- Cause observed in the order history: on two occasions (1 May and 27 May) a
  broker stop order filled and the software stop then sold the same shares
  again. The second sale opened a short position. Neither short is in the
  local ledger: its cash includes the sale proceeds and nothing is subtracted
  for the shares owed. Both shorts have been open for about six months.
- Local ledger: 6 positions and 56 trades. Broker: 9 stock positions, 5 open
  option legs, and 166 filled orders. A fractional-share remainder and every
  open option leg are missing from the local ledger.
- The broker's portfolio-history endpoint has under-reported equity since
  18 Aug: it leaves out one long stock position worth about 7% of equity,
  while the live account and positions endpoints include it. Cause unknown.
  Portfolio history is not safe as a source of truth for equity or drawdown.
- A rebuild of the account from every fill, dividend, and fee matches the
  broker's current share counts and cash exactly, and is within 0.1% of the
  corrected equity figure used above.

## 3. Research inputs have failed, with no alert (areas 05, 06, 08; area 11, question 10)

Counts are from the service log, 20 Sep to 20 Nov.

| Input | What the log shows | Count |
| --- | --- | --- |
| Perplexity API | "You exceeded your current quota"; no successful call in the window | 2,756 |
| OpenAI | Model `gpt-4.1-nano` reported as deprecated, from 25 Oct | 29 |
| TipRanks screener | 401 Unauthorized | 21 |
| FMP | 402 Payment Required, including price quotes for held symbols and QQQ | about 12,000 |
| FMP | 429 Too Many Requests | about 7,900 |
| Research cycle | "0 signals" | 36 |
| Holdings review | "Empty re-analysis response" | 235 |
| Options scan | "invalid underlying symbols" for a ticker that was renamed on 9 Jun | 51 |
| Telegram | "Telegram not configured" | 77 |

- The last successful TipRanks file is dated 14 Apr. The options universe has
  been frozen at four symbols since then.
- The newest stored thesis is dated 26 to 29 Jul. The last new stock entry was
  on 17 Jul. Entries per month: Mar 8, Apr 24, May 3, Jun 1, Jul 2, none since.
- The most likely reading is that research has produced nothing since late
  July. The bot kept running, kept sending daily briefs, and reported nothing
  unusual.

## 4. Bugs seen in production (areas 01, 03, 04)

- `stock_agent/scheduler.py:1349` reads `self._tipranks_lookup`, which is not
  assigned anywhere. After a software stop sells, the bookkeeping step fails
  with `'StockAgentScheduler' object has no attribute '_tipranks_lookup'`.
  Seen on 1 Oct, 26 Oct, and 5 Nov (9 log lines). The sale goes through; the
  steps after it do not run.
- Broker stop orders and software stops are not coordinated (the double sale
  in section 2).
- Broker stop orders were placed on only two days, 23 and 27 Apr (19 orders:
  13 filled, 6 cancelled). No open stock position has a stop order at the
  broker today. Stops exist only in software, so they only work while the
  process is up and its price feed is healthy; see the FMP refusals above.
- On 11 May covered calls were sold at strikes far below the market price.
  Commit `e92e1cd` (16 May) addresses this. Check that the fix has a test.

## 5. Limits in practice (area 02; area 11, question 6)

Measured against the values in `stock_agent/config.py`: 15% per symbol, 40%
per sector, 100% total exposure, 20 positions.

- Per symbol: the largest position was above 15% of equity on 122 of 164
  trading days, and above 10% (the largest intended size) on 148. Today one
  stock is 33.6% of equity, almost all of it delivered through options.
- Per sector: one sector was above 40% of equity on 121 of 164 days
  (Technology on 82, Financial Services on 39).
- Total exposure: stock held long plus stock sold short was above 100% of
  equity on 49 days. It peaked at 154% on
  22 Apr, when exercised call spreads delivered shares: one stock was 71.5%
  of equity that day and cash was negative. Cash was negative on 40 days.
- Position count: 19 at most, inside the limit.
- Pattern: limits are respected when the bot buys stock and are not applied
  to shares that arrive through option exercise or assignment.

## 6. What worked and what did not (areas 04 and 05; area 11, questions 3, 4, 5, 8)

Stock entries made on a thesis:

- 38 entries, 21 profitable (55%). Average winner +21.7%, average loser -6.5%.
- The three best entries produced 67% of the profit from stock entries.
- Technology: 11 winners of 12 and about 95% of that profit. Healthcare:
  1 winner of 8. Consumer Cyclical: 2 of 8, negative overall. Financial
  Services: 4 of 6.
- Conviction at entry tracked results. Score 9: 5 winners of 7, average
  +22.2%. Score 8: 14 of 25, average +8.0%. Score 7: 2 of 6, average -1.5%.

Options:

- 43 structures, 19 profitable, mostly bull call spreads.
- Structures that expired in April made about +15.6 points. Everything since
  has lost about 8.0 points. Expiring after 31 Jul: 17 structures,
  5 profitable. Three structures were still open on 19 Nov and are counted
  at that day's value.
- The recurring scan repeats the same bull call spread on the frozen
  four-symbol list about every three weeks. On one of those symbols it has
  lost seven times in a row since July (six finished, one still open and
  nearly worthless). A second is net negative over eight structures. A third
  is roughly flat over ten.

Stops:

- 25 stop exits: 13 by broker stop order, 12 by software stop.
- The price was lower 20 trading days later in 10 of the 25. Compared with
  holding for 20 more trading days, the stops cost about 5 points in total.
  The two most recent exits have fewer than 20 days of later prices.

## 7. Operations (area 09; area 11, question 1)

- The server has been up for 229 days. The service restarted 6 times in the
  two-month log window, each time back within seconds, with no crash loop.
- The auto-update log shows 25 "git fetch failed" lines, in July.
- The server's settings file still carries unused settings from the removed
  Polymarket code. They should be removed there, and anything that was ever
  committed should be rotated.

## What the plan has to cover

1. One source of truth: reconcile the local ledger against the broker every
   cycle, including short positions and option legs, and alert on any
   difference. Report equity from the live account, not portfolio history.
2. One stop mechanism, so that two exits can never sell the same shares, and
   a fix with a test for the `_tipranks_lookup` failure.
3. Position, sector, and exposure limits that also apply to shares delivered
   by options, with option trades sized for the worst case of exercise.
4. Health checks on every research and price input. When one fails, alert and
   stop opening new trades. A stale universe must not keep trading.
5. An honest daily brief: true equity, the comparison with QQQ and SPY, and
   the state of each input.
6. A permanent trade journal (thesis, conviction, entry, exit, reason) and
   log retention longer than two months.
7. A decision from Josh on the recurring options scan, on the stop policy,
   and on the entry bar (score 7 entries lost money on average).
8. A paper-trading test period with working inputs and written pass or fail
   criteria before the strategy is judged or real money is considered. The
   period since late July says little about the strategy, because research
   was not running.
