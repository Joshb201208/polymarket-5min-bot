# 04. Options

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

The options side of the bot can open four kinds of trade: covered calls
(selling a call against shares it already holds), cash-secured puts (selling a
put on a stock it would accept buying), and two capped-risk bets called bull
call spreads and bear put spreads. It looks for trades once each weekday
between 9:45 and 14:00 Eastern, again after any large market move, and sends
every order "at market" (at whatever price is available). After a trade is
opened the bot only watches: automatic closing was switched off in April 2026,
so every option is held until it expires and the broker decides the outcome.

The trade-picking code runs, but most of the safety net around it is missing
or not connected. Three things matter most:

1. A spread is sent as two separate orders with no check that either one went
   through. The bot can end up holding half a spread (which is no longer
   capped-risk), or record and announce a trade that never happened.
2. Because nothing is closed before expiry, an option that finishes "in the
   money" becomes 100 shares bought or sold at the strike price, and no size
   limit is applied to that. For an expensive stock this can be half the
   account or more. If it arrives as a short stock position, the equity side
   of the bot does not see it at all.
3. The limits are weaker than they read. The "15% options exposure" cap
   measures the current price of the options, not the money at stake: ten puts
   that could force $70,000 of stock purchases register as 1.5%. The cash
   behind a put is never checked or set aside. And the bot's record of its
   options is wiped if a single broker request fails.

The `OPTIONS_ENABLED` switch and every other `OPTIONS_*` setting in
`config.py` do nothing; the numbers actually used are constants inside the
options files. Some things are right: covered calls are restricted to
out-of-the-money strikes, the options code can only talk to the paper account
today, and expired options are valued at their real worth when the price
lookup succeeds. The profit-and-loss figures in the options ledger are not
reliable enough to judge whether options have made or lost money.

## How it actually works

### Flow

```
_tick (every 60 s)
 ├─ weekday 09:45-14:00 ET, not yet run today ──► _run_options_scan
 │     1. reconcile options ledger with Alpaca      scheduler.py:1386-1400
 │     2. refresh option prices                     scheduler.py:1403
 │     3. build inputs from the LOCAL equity ledger scheduler.py:1406-1451
 │     4. OptionsEngine.scan_opportunities          options_engine.py:103-262
 │     5. send market orders, record as open        scheduler.py:1464-1510
 └─ market open, every 30 min ──► _check_options_positions
       reconcile, refresh prices, send "3 days to expiry" alert
       (no closing of any kind)                     scheduler.py:1523-1574
_check_for_market_event ──► clears the "done today" flag and runs the scan
       again                                        scheduler.py:1685-1688
```

1. **Trigger.** `_should_run_options_scan` allows one scan per weekday between
   09:45 and 14:00 ET (`scheduler.py:224-236`). The "done today" marker is an
   in-memory attribute (`scheduler.py:98`, set at `:1378`). The comment above
   the call says "9:00-9:30 AM ET ... before market open" (`scheduler.py:181`);
   the code does not do that.
2. **Reconcile.** `get_option_positions()` and `get_option_activities()` are
   fetched and passed to `OptionsPortfolio.sync_from_alpaca`
   (`scheduler.py:1387-1393`). This only ever removes positions from the local
   options ledger (F-04-04).
3. **Inputs.** Share counts come from the local equity ledger
   (`scheduler.py:1406-1408`); the equity ledger is not reconciled with Alpaca
   here. Candidates are the first 30 symbols of the stored universe, each with
   a full fundamentals fetch to get a price, and a conviction taken from the
   stored thesis or defaulted to 6 (`scheduler.py:1426-1443`). Underlyings
   that already have an option position in the local ledger are skipped
   (`scheduler.py:1414-1429`, `:1448-1451`).
4. **Selection.** See the strategy table below.
5. **Execution.** Every order is a market, day order
   (`scheduler.py:1472`, `:1482-1483`). Spreads are two separate orders
   (`options_executor.py:160-178`). If the call returns something truthy the
   position is written to the ledger at an estimated price
   (`scheduler.py:1486-1496`) and a Discord embed is posted.
6. **Monitoring.** Every 30 minutes in market hours, and only if the local
   ledger is non-empty (`scheduler.py:1532-1533`): reconcile, refresh prices,
   and post an alert for anything with 3 or fewer days left. The alert text
   says "positions will run to expiry" (`scheduler.py:1562-1563`).
7. **Expiry.** The bot does nothing. When a leg disappears from Alpaca after
   its expiry date, the next reconcile moves the position to
   `closed_positions` and values it from the underlying's closing price
   (`options_portfolio.py:463-484`).

State is `data/stock_agent/options_portfolio.json`: open positions, every
closed position ever, and running totals (`options_models.py:244-254`).

### Q1. What it can open, and the real entry rules

| Strategy | Real entry rules (code) | What comments / README say |
| --- | --- | --- |
| Covered call | Local ledger shows 100+ shares; no option already on that underlying; a price is available. Calls with strike between 0.1% and 25% above the price, 14-35 days out, premium at least 0.5% of strike per month. Picks the largest dollar premium, which is the lowest strike and the longest expiry. Quantity = shares // 100. Any regime, any conviction. `options_engine.py:155-189`, `:266-375` | Module docstring: "(future use)" (`options_engine.py:10`). It is live and runs first. |
| Cash-secured put | Universe symbol not held, conviction 6, 7 or 8 (6 is the default when there is no thesis). Strike between 90% and 101% of the "desired entry" (thesis target if below the price, otherwise 5% below the price), 14-35 days, strike x 100 no more than 7% of portfolio value, premium at least 0.5% per month. One contract. Any regime, including CAUTIOUS. `options_engine.py:192-213`, `:377-495` | "stocks the bot wants to buy"; "We verify the account has adequate capital headroom" (`options_engine.py:389-391`). No cash figure is read. |
| Bull call spread | Regime is anything except CAUTIOUS. First five candidates with conviction 8+, held or not. Buy the call nearest to price x 1.03 rounded to $5, sell the call $10 higher (price under $150) or $20 higher. 14-45 days. Reward-to-risk at least 1.5. Debit x 100 no more than 7% of portfolio. One spread. `options_engine.py:218-239`, `:497-608` | "AGGRESSIVE_DEPLOY / DIP_OPPORTUNITY", "30-45 DTE", "Width: $5-$10", "Minimum 2:1" (`options_engine.py:509-511`). |
| Bear put spread | Regime is CAUTIOUS. First three candidates in universe order, whatever their conviction. Buy the put nearest price x 0.99 rounded to $5, sell $10 / $20 lower. Same checks. `options_engine.py:241-256`, `:610-708` | "as portfolio hedges" (`options_engine.py:242`). They are not tied to anything the bot holds. |

`LONG_CALL` and `LONG_PUT` exist in the enum (`options_models.py:22-23`) but
nothing produces them.

### Q5. The limits as enforced

| Limit | Value used | Measured as | When |
| --- | --- | --- | --- |
| Open positions | 10 (`options_engine.py:52`) | Count of positions in the local ledger; a spread counts once | Start of scan, then one slot per signal |
| Per trade | 7% (`:53`) | Put: strike x 100. Spread: estimated debit x 100. Covered call: not checked | Per candidate, against local portfolio value |
| Total exposure | 15% (`:54`) | Absolute value of (long option prices minus short option prices), over portfolio value | Once, at the start of the scan; not updated for the trades about to be placed (F-04-03) |
| Days to expiry | 14-35 calls/puts, 14-45 spreads (`:61-66`) | Calendar days from the server's local date | Chain request filter |
| Minimum premium | 0.5% of strike per 30 days (`:69`) | Mid, else last trade, else yesterday's close | Per contract; calls and puts only |
| Reward to risk | 1.5 (`:55`) | (width - debit) / debit at mid prices | Spreads only |
| One position per underlying | - | Symbol prefix of contracts in the local ledger | Built once before the scan |

Not checked anywhere: account cash or buying power, the account's options
approval level, bid-ask width, open interest or volume, quote age, earnings or
ex-dividend dates inside the option's life, that both spread legs share an
expiry.

### Answers to the remaining questions

- **Q2 (covered calls).** Cover is checked only against the local share count,
  once, at selection time. Nothing ties the call to the shares afterwards.
  See F-04-05.
- **Q3 (cash-secured puts).** The check is strike x 100 against 7% of local
  portfolio value (cash plus stock). No cash figure is used and nothing is
  reserved from the equity side. See F-04-03.
- **Q4 (spreads).** Two separate market orders, long leg first, short leg sent
  regardless. A one-sided fill is not detected. See F-04-01.
- **Q6 (closing).** There is no automatic close of any kind: no profit target,
  no days-to-expiry close, no stop. Expiry, assignment and exercise are left
  to the broker. The equity ledger learns about shares that arrive only at its
  own next reconcile, and never learns about a short stock position. See
  F-04-02.
- **Q7 (reconcile, expired legs).** One-way: positions missing at the broker
  are closed locally; nothing is ever imported. Expired legs are valued at
  intrinsic value when the closing-price lookup works and at zero when it
  does not. See F-04-04 and F-04-10.
- **Q8 (quotes and greeks).** Alpaca option snapshots on the `indicative` feed;
  price is mid, else last trade, else the contract's previous close. Quote
  timestamps are parsed and never read. Greeks are stored for display and
  never used in a decision. An empty chain means no signal; a wide or stale
  quote is used as-is. See F-04-06.
- **Q9 (equity risk manager).** It has no knowledge of options. `option` does
  not appear in `risk_manager.py`. See F-04-03.

- **Lead: missing options approval level.** No code reads the account's
  approval level. A refused order comes back as an HTTP error;
  `_submit_order` logs it and returns `None` (`options_executor.py:459-467`).
  For a single-leg order nothing is recorded and no alert is sent, and the
  same order is tried again on the next scan. For a spread the trade is
  recorded as opened anyway (F-04-01).

## Findings

### F-04-01: A spread is two separate market orders, and it is recorded as opened even when one or both orders fail
- Severity: Critical
- Confidence: Confirmed (the code path and the always-true check were run; what the broker then does with a stray leg was not checked)
- Where: `stock_agent/options_executor.py:160-178`, `stock_agent/scheduler.py:1466-1496`, `stock_agent/options_portfolio.py:438-453`
- What happens: A spread is meant to be two options that limit each other's
  risk. The bot sends the buy, then sends the sell without looking at what
  happened to the buy, and then treats the pair as a success no matter what
  came back.
- Evidence:
  `options_executor.py:160-178` — the short leg is submitted unconditionally and a pair is returned:
  ```python
  long_order = await self.place_option_order(contract_symbol=long_symbol, ..., side=OptionSide.BUY, ...)
  short_order = await self.place_option_order(contract_symbol=short_symbol, ..., side=OptionSide.SELL, ...)
  return long_order, short_order
  ```
  `_submit_order` returns `None` on any rejection or network error
  (`options_executor.py:459-467`). The caller tests the pair itself
  (`scheduler.py:1468-1473`, `:1486-1496`):
  ```python
  order = await self.options_executor.place_spread_order(..., order_type=OptionOrderType.MARKET)
  ...
  if order:
      fill_price = getattr(signal, 'max_debit_per_contract', None) or ... or 0.0
      pos = self.options_portfolio.open_position(signal=signal, filled_price=fill_price, ...)
  ```
  A two-item tuple is always true in Python, including `(None, None)`.
  What follows in each case:

  | Long leg | Short leg | At the broker | In the bot's ledger |
  | --- | --- | --- | --- |
  | rejected | rejected | nothing | Spread "opened", Discord trade embed posted, counted in "N trades executed". At the next 30-minute check it is closed as "missing at Alpaca (assumed expired OTM)" with a loss equal to the whole estimated debit. The underlying is then free, so the same thing can repeat on the next scan. |
  | accepted | rejected | one long option, alone | Spread "opened". At the next check, because both legs are not live, the whole position is closed (`options_portfolio.py:445-453`); the long option stays at the broker untracked. |
  | rejected | accepted | one short option, alone | Same. The short option stays at the broker untracked. |
  | accepted | accepted, fills later or never | depends | Recorded as a full spread at an estimated price either way. |

  The docstring gives the reason for two orders as "Alpaca paper accounts
  process spread legs individually" (`options_executor.py:127`).
- Why it matters: The third row is the dangerous one. For a bear put spread
  the stray leg is a short put, which the broker can accept on its own as an
  ordinary cash-secured put. Its assignment cost is strike x 100 and was never
  held to the 7% rule, because for spreads only the debit is checked
  (`options_engine.py:672-675`). Example with made-up numbers: stock at $480,
  spread is long 475 put / short 455 put; if only the short fills, the account
  owes up to $45,500 on a $100,000 portfolio, and the ledger forgets the leg
  within 30 minutes. The first row matters differently: with an account that
  cannot trade spreads, the ledger would record a fictitious trade and a
  fictitious loss every scan.
- How verified: Read the path end to end. Ran `place_spread_order` with
  `_submit_order` replaced by a stub (no network):
  ```bash
  STOCK_DATA_DIR=$(mktemp -d) ALPACA_API_KEY= ALPACA_SECRET_KEY= python3 - <<'EOF'
  import asyncio
  from stock_agent.config import Config
  from stock_agent.options_executor import OptionsExecutor
  from stock_agent.options_models import OptionOrderType
  async def main():
      ex = OptionsExecutor(Config())
      async def rejected(payload, label=""):   # stands in for a broker rejection
          return None
      ex._submit_order = rejected
      order = await ex.place_spread_order("LONG", "SHORT", qty=1, order_type=OptionOrderType.MARKET)
      print(order, bool(order))
  asyncio.run(main())
  EOF
  # prints: (None, None) True
  ```
  With the stub rejecting only the buy, the sell was still submitted. Ran
  `sync_from_alpaca` on a spread with only leg 1 live: the position was closed
  and `open_position_count` went to 0.
- Direction (not a fix): Send a spread as one multi-leg order if the broker
  supports it; otherwise confirm the first leg is filled before sending the
  second and unwind if the second fails. Judge success per leg, from the
  order's final status.

### F-04-02: Nothing ever closes an option; positions run to expiry and can become stock positions that no limit covers
- Severity: Critical
- Confidence: Likely (the bot's side is Confirmed; what Alpaca does at expiry was not checked in this session)
- Where: `stock_agent/scheduler.py:1523-1574`, `stock_agent/options_engine.py:572-575`, `:672-675`, `stock_agent/portfolio.py:157-162`, `:175-181`
- What happens: The profit-target and days-to-expiry closing rules exist in
  the code but are not called. Every option is held to expiry. An option that
  expires in the money is exercised or assigned by the broker, which means 100
  shares bought or sold at the strike, and nothing in the bot sizes or checks
  that.
- Evidence: The monitoring function says so itself (`scheduler.py:1526-1529`):
  ```
  Note: Auto-close via order submission is disabled because Alpaca paper
  trading rejects SELL orders on long options as 'uncovered'. Positions
  will be managed to expiry or closed manually.
  ```
  The closing call was removed in commit `602aa96` (2026-04-08).
  `get_auto_close_candidates`, `close_option_position`, `close_spread_position`
  and `OptionsPortfolio.close_position` have no callers (repo-wide search).
  For spreads the size check is on the debit only
  (`options_engine.py:572-575`):
  ```python
  total_debit = net_debit * SHARES_PER_CONTRACT
  position_pct = total_debit / portfolio_value
  if position_pct > MAX_SINGLE_TRADE_PCT:
      return None
  ```
  Worked example, made-up numbers, $100,000 portfolio:

  | Position | Underlying at expiry | Result if the broker exercises | Size |
  | --- | --- | --- | --- |
  | Bull call spread 495 / 515 (stock was $480; debit about $600, 0.6%) | $505, between the strikes | Long call exercised, short call expires: buy 100 shares at $495 | $49,500, about half the account; the per-symbol ceiling is 15% |
  | Bear put spread 475 / 455 | $465, between the strikes | Long put exercised, short put expires: sell 100 shares at $475 that the bot does not own | Short 100 shares |
  | Cash-secured put, strike $60 | $55 | Assigned: buy 100 shares at $60 | $6,000 (within the 7% rule) |
  | Covered call, strike $55 | $61 | Assigned: 100 shares leave at $55 | - |

  What the equity ledger does with the result (`portfolio.py:175-181`):
  ```python
  shares    = int(float(ap.get("qty", 0) or 0))
  ...
  if shares <= 0 or avg_entry <= 0:
      continue
  ```
  A short stock position has a negative quantity, so it is never recorded.
  Shares that arrive are imported with a placeholder thesis ("HOLD",
  conviction 7) and a local stop 5% below entry (`portfolio.py:182-203`); no
  stop order is sent to the broker for them (stop orders are only sent from
  `Executor.place_buy`, `executor.py:117-127`). Shares that leave are dropped
  with a log line and no trade record (`portfolio.py:149-156`). The equity
  reconcile runs only in the Sunday and Wednesday cycles and the 08:15-08:45
  morning brief (`scheduler.py:371`, `:491`, `:1732-1733`).
- Why it matters: This is the normal path, not a failure path. A spread that
  ends between its strikes is a common outcome, and for most large-cap stocks
  100 shares is well above the 15% per-symbol ceiling. A short stock position
  has no loss limit and would be invisible to the bot.
- How verified: Read `_check_options_positions` and searched for callers of
  every close function. Read commit `602aa96`. Arithmetic above. The broker's
  expiry handling (automatic exercise, any forced liquidation for lack of
  buying power) is from memory of Alpaca's documentation and is listed under
  "Could not verify".
- Direction (not a fix): Decide an exit policy (close or roll before expiry,
  or accept assignment on purpose). If shares can arrive through options, that
  arrival has to pass the same size limits as a buy, and the equity ledger has
  to handle negative quantities.

### F-04-03: The 15% exposure cap measures option prices, not money at risk; put cash is neither checked nor reserved; the equity side is unaware
- Severity: Critical
- Confidence: Confirmed
- Where: `stock_agent/options_portfolio.py:623-657`, `stock_agent/options_engine.py:135-150`, `:428-445`, `stock_agent/scheduler.py:1385`, `:1445`, `stock_agent/risk_manager.py:14-123`
- What happens: "Exposure" is the net market price of the options held, with
  sold options subtracting from bought ones. A sold put that could cost $6,000
  counts as about $150, and adding it can make the exposure number go down.
  The cap is checked once before the scan and not again as trades are added.
  No code reads the account's cash or buying power for options, and the equity
  risk checks do not know options exist.
- Evidence: `options_portfolio.py:631-637`, `:654-655`:
  ```python
  if pos.side == OptionSide.BUY:
      position_value = pos.current_price * pos.qty * pos.multiplier
  else:
      position_value = -pos.current_price * pos.qty * pos.multiplier
  total_value += position_value
  ...
  self.state.total_options_exposure_pct = abs(total_value) / portfolio_value
  ```
  The gate (`options_engine.py:135`) is
  `if current_options_exposure >= MAX_TOTAL_OPTIONS_EXPOSURE_PCT: return`.
  The headroom is computed and only logged (`options_engine.py:145-150`); no
  evaluator receives it. The put check (`options_engine.py:429`, `:439-440`):
  ```python
  max_trade_value = portfolio_value * MAX_SINGLE_TRADE_PCT
  ...
  cash_required = contract.strike_price * SHARES_PER_CONTRACT
  if cash_required > max_trade_value:
  ```
  `portfolio_value` is `Portfolio.get_portfolio_value()`, local cash plus
  stock value (`scheduler.py:1385`, `portfolio.py:227-230`). `buying_power`
  appears nowhere in `stock_agent/`. `risk_manager.py` contains no reference
  to options; its cash test is `state.cash < price` (`:120`). Equity exposure
  is allowed up to 100% (`config.py:51`).
- Why it matters: Ten slots at up to 7% each is up to 70% of the account in
  put obligations while the exposure figure reads about 1.5%, on top of an
  equity book that may itself use all the cash. In a market fall those puts
  are assigned together, on the same kind of stocks the bot already holds.
  The cap named in the hard rules ("options exposure") does not constrain
  this.
- How verified: Ran the real `OptionsPortfolio` with made-up positions and a
  $100,000 portfolio value (no network): one spread alone showed 0.450%;
  adding a put that secures $6,000 lowered it to 0.300%; ten puts securing
  $70,000 in total showed 1.50%. Ran `scan_opportunities` with exposure
  passed as 14.9%: three signals were returned whose own size fields summed
  to 11.7%.
- Direction (not a fix): Define exposure as the worst-case cash a position can
  demand (assignment cost for puts, maximum loss for spreads), check it
  including the order being proposed, and make the equity side's cash and
  position checks see that reserved amount.

### F-04-04: One failed broker request erases the options ledger, and the reconcile never adds anything back
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/options_executor.py:264-291`, `:330-332`, `stock_agent/options_portfolio.py:416-551`, `stock_agent/scheduler.py:1386-1400`, `:1532-1546`
- What happens: When the request for broker positions fails, the function
  returns an empty list. The reconcile reads an empty list as "the broker
  holds no options" and closes every position in the ledger, each with an
  invented profit or loss. Nothing re-imports them afterwards.
- Evidence: `options_executor.py:283-291`:
  ```python
  except httpx.HTTPStatusError as exc:
      logger.error("get_option_positions failed: %s — %s", ...)
  except Exception as exc:
      logger.error("get_option_positions error: %s", exc)
  return []
  ```
  No exception reaches the caller, so the `try` around the reconcile in the
  scheduler does not help (`scheduler.py:1386-1400`). In
  `sync_from_alpaca`, `live_syms` is built from that list
  (`options_portfolio.py:416`) and every position not in it is closed
  (`:455-533`). The reconcile only iterates over local positions; a broker
  position the ledger does not know is never added (`:436`, `:540`). Once the
  ledger is empty the 30-minute check returns at its first line
  (`scheduler.py:1532-1533`), so nothing looks again. The equity side has a
  partial guard for the same situation (`scheduler.py:818-820`); the options
  side has none.
- Why it matters: After a wipe the position count is 0 and the
  one-per-underlying guard is empty, so the next scan can open up to ten more
  positions on top of the real ones, including a second put or a second
  covered call on the same underlying. The real positions get no expiry
  alerts. Realized P&L in the ledger gains invented entries. The reconcile
  runs about 13 times a trading day, so a timeout or a 5xx at the wrong moment
  is a matter of time.
- How verified: Ran the real `sync_from_alpaca` on a made-up ledger (a put, a
  spread, a covered call, all 25 days from expiry) with
  `alpaca_option_positions=[]` and no activities, in a temporary data
  directory: result `{'closed': 3, 'still_open': 0}`, reasons all "missing at
  Alpaca (assumed expired OTM)", realized +$150, -$315, +$90. Separately
  passed a broker option position with an empty ledger: nothing imported.
  Minimal reproduction:
  ```bash
  STOCK_DATA_DIR=$(mktemp -d) ALPACA_API_KEY= ALPACA_SECRET_KEY= FMP_API_KEY= python3 - <<'EOF'
  import asyncio
  from datetime import date, timedelta
  from stock_agent.config import Config
  from stock_agent.options_models import OptionSide, OptionType, OptionsSignal, OptionsStrategy
  from stock_agent.options_portfolio import OptionsPortfolio
  exp = date.today() + timedelta(days=25)
  sig = OptionsSignal(strategy=OptionsStrategy.CASH_SECURED_PUT, underlying_symbol="AAA", underlying_price=63.0,
                      contract_symbol=f"AAA{exp:%y%m%d}P00060000", option_type=OptionType.PUT, side=OptionSide.SELL,
                      target_strike=60.0, min_credit_per_contract=1.50, estimated_max_loss=6000)
  pf = OptionsPortfolio(Config(), data_feed=None)
  pf.open_position(sig, 1.50, portfolio_value=100_000)
  print("exposure:", pf.total_exposure_pct)
  print(asyncio.run(pf.sync_from_alpaca(alpaca_option_positions=[], alpaca_option_activities=[])))
  print(pf.open_position_count, pf.closed_positions[0].close_reason, pf.closed_positions[0].realized_pnl)
  EOF
  # exposure: 0.0015
  # {'closed': 1, 'realized_pnl': 150.0, 'still_open': 0}
  # 0 missing at Alpaca (assumed expired OTM) 150.0
  ```
- Direction (not a fix): A failed request must be distinguishable from an
  empty account and must skip the reconcile. The reconcile should run both
  ways, per leg, with quantities, and should flag broker positions the ledger
  does not know.

### F-04-05: Covered-call cover is checked once against the local share count, and nothing links the call to the shares afterwards
- Severity: High
- Confidence: Likely (the code path is Confirmed; whether the broker refuses the uncovered outcome was not checked)
- Where: `stock_agent/scheduler.py:1406-1408`, `:1448-1451`, `stock_agent/options_engine.py:158-159`, `:282`, `stock_agent/scheduler.py:1316-1346`, `stock_agent/executor.py:117-124`
- What happens: The number of calls to sell is worked out from the bot's own
  record of shares, not from the broker at the moment of the order. After the
  call is sold, the equity side can still sell those shares, and a stop order
  for them is already resting at the broker.
- Evidence: `scheduler.py:1406-1408`:
  ```python
  stock_positions = {
      p.symbol: p.shares for p in self.portfolio.state.positions
  }
  ```
  `options_engine.py:159` (`if shares >= SHARES_PER_CONTRACT ...`) and `:282`
  (`contracts_to_sell = shares // SHARES_PER_CONTRACT`). The scan reconciles
  the options ledger but not the equity ledger. The last equity reconcile is
  the 08:15-08:45 brief, and only when the positions list came back non-empty
  (`scheduler.py:1732-1733`). Between then and the scan the local count can
  be wrong: an add-on buy increases it as soon as the order is accepted
  (`scheduler.py:1273-1278`), a broker-side stop can fire, and a failed sell
  leaves the position in place (`scheduler.py:1323-1326`). On the other side,
  `_execute_sell` sells the whole position with no look at the options ledger
  (`scheduler.py:1323`), and every bot purchase leaves a good-till-cancelled
  stop sell for the full quantity (`executor.py:117-124`).
- Why it matters: Either order of events ends with a short call that has no
  shares behind it, which has no loss limit. The only thing standing in the
  way is the broker refusing the order. The engine's own statement "No naked
  options" (`options_engine.py:18`) is enforced only at entry, from a local
  count. If the options ledger has been wiped (F-04-04), the duplicate guard
  that stops a second call on the same shares is gone too.
- How verified: Read both paths end to end; searched `scheduler.py` for any
  options reference in the sell path (none).
- Direction (not a fix): Read share count and existing short calls from the
  broker immediately before the order, and make the equity sell and stop
  paths aware of open short calls on the same symbol.

### F-04-06: Orders go at market, priced from mid, last trade or yesterday's close, and the fill is never read back
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1472`, `:1482-1489`, `stock_agent/options_models.py:106-115`, `stock_agent/options_data.py:208-212`, `:245-253`, `:280-283`, `:362-377`
- What happens: The checks (minimum premium, reward-to-risk, 7% size) are
  made on an estimated price. The order then goes out with no price limit,
  and the ledger records the estimate as if it were the fill.
- Evidence: Orders: `order_type=OptionOrderType.MARKET` with
  `limit_price=None` (`scheduler.py:1472`, `:1482-1483`). The engine does
  compute a limit (`max_debit_per_contract`, `min_credit_per_contract`,
  `options_engine.py:364`, `:592`), but it is used only as the pretend fill
  (`scheduler.py:1487-1489`):
  ```python
  # Use max_debit as fill price proxy; fall back to 0.0
  fill_price = getattr(signal, 'max_debit_per_contract', None) or \
               getattr(signal, 'min_credit_per_contract', None) or 0.0
  ```
  Price used for decisions (`options_models.py:106-115`): mid if positive,
  else last trade, else close. If the snapshot request fails the batch is
  skipped with `continue` (`options_data.py:245-253`) and each contract then
  gets a quote built from its previous close (`options_data.py:366-372`).
  The quote time is parsed into `updated_at` (`options_data.py:296-301`) and
  read nowhere. `open_interest`, `volume`, `bid` and `ask` are not read by the
  engine. The feed is `indicative` (`options_data.py:211`). The order's
  status is never fetched; an order that is accepted but not filled is
  recorded as an open position.
- Why it matters: With a wide market the real fill can be far from the
  estimate. Selling a put whose bid is $0.05 and ask is $3.00 passes the
  premium test at $1.525 and sells for about $0.05. Buying a spread at the
  ask and selling at the bid can cost more than the "maximum loss" the size
  check used. A data outage does not stop trading; it silently switches the
  bot to yesterday's prices. And every options P&L number starts from an
  entry price that is not the real one.
- How verified: Read the path. Ran `OptionsQuote.fair_value` and
  `_parse_snapshot` with made-up quotes: bid 0.05 / ask 3.00 gives 1.525; bid
  0 with a seven-week-old last trade of 1.90 gives 1.90. Ran
  `sync_from_alpaca` on a just-recorded put with no broker position and no
  fill: closed as "assumed expired OTM", +$150.
- Direction (not a fix): Use limit orders at the price the checks were made
  on, confirm the fill from the order, record the real fill price, and refuse
  to trade on quotes that are missing, stale or too wide.

### F-04-07: `OPTIONS_ENABLED` does not switch options off, and none of the other `OPTIONS_*` settings are read
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/config.py:201-212`, `stock_agent/scheduler.py:181-184`, `:224-236`, `stock_agent/options_engine.py:52-73`, `stock_agent/options_models.py:179-196`
- What happens: There are twelve options settings in `config.py`. A search of
  the whole repository finds no code that reads any of them. The live values
  are module-level constants in `options_engine.py`, and several differ.
- Evidence: The only matches for `OPTIONS_` outside `config.py` are the
  tripwire test, `tests/conftest.py:27` and `scripts/vps_snapshot.sh:177`.
  `_should_run_options_scan` tests weekday, time and the done-today flag and
  nothing else (`scheduler.py:224-236`). `OptionsEngine` stores `self.config`
  (`options_engine.py:97`) and never reads it.

  | Setting in `config.py` | Value | Its comment | What the code really uses |
  | --- | --- | --- | --- |
  | `OPTIONS_ENABLED` | env, default true | - | Nothing. Options always run. |
  | `OPTIONS_MAX_POSITIONS` | 10 | "Max open options positions" | `MAX_OPTIONS_POSITIONS = 10` (`options_engine.py:52`) |
  | `OPTIONS_MAX_SINGLE_TRADE_PCT` | 0.07 | "Max 5% of portfolio per trade" | `MAX_SINGLE_TRADE_PCT = 0.07`: 7% (`:53`) |
  | `OPTIONS_MAX_TOTAL_EXPOSURE_PCT` | 0.15 | "Max 15%" | `MAX_TOTAL_OPTIONS_EXPOSURE_PCT = 0.15` (`:54`), measured as in F-04-03 |
  | `OPTIONS_AUTO_CLOSE_PROFIT_PCT` | 0.50 | "Auto-close at 50% profit" | Nothing closes. A literal `50.0` sits in unused code (`options_models.py:182`, `:192`) |
  | `OPTIONS_AUTO_CLOSE_DTE` | 3 | "Auto-close at 3 DTE" | Nothing closes. `EXPIRY_ALERT_DTE = 3` drives an alert only (`options_portfolio.py:43`) |
  | `OPTIONS_MIN_DTE` | 14 | - | `CC_MIN_DTE = 14`, `SPREAD_MIN_DTE = 14` (`:61`, `:65`) |
  | `OPTIONS_MAX_DTE` | 45 | - | 35 for calls and puts (`CC_MAX_DTE`), 45 for spreads (`:62`, `:66`) |
  | `OPTIONS_MIN_PREMIUM_PCT` | 0.005 | "Minimum 1% monthly premium" | `MIN_PUT_PREMIUM_MONTHLY_PCT = 0.005`: 0.5% per month (`:69`; its own comment says "annualised") |
  | `OPTIONS_SPREAD_WIDTH_MIN` | 5.0 | "$5" | `SPREAD_WIDTH_NARROW = 10.0` (`:72`) |
  | `OPTIONS_SPREAD_WIDTH_MAX` | 10.0 | "$10" | `SPREAD_WIDTH_WIDE = 20.0` (`:73`) |
  | `OPTIONS_MIN_RR_RATIO` | 2.0 | "Minimum 2:1" | `MIN_REWARD_TO_RISK = 1.5` (`:55`) |

- Why it matters: Setting `OPTIONS_ENABLED=false` on the droplet would not
  stop options orders; there is no off switch short of a code change. Editing
  any limit in `config.py` changes nothing. `tests/test_risk_tripwires.py:98-118`
  pins both sets of numbers, so the two look deliberately in step when one of
  them is inert.
- How verified: Repository-wide search for each name; read every options
  module in full.
- Direction (not a fix): One source for each limit, read from config, with
  the enable flag checked before any options work starts.

### F-04-08: Spread legs are chosen by strike alone and can have different expiry dates; the fixed $5 rounding gives odd strikes on cheap stocks
- Severity: High
- Confidence: Confirmed for the selection logic; how often real chains trigger it was not checked
- Where: `stock_agent/options_engine.py:513-545`, `:623-651`, `:712-732`
- What happens: The bot asks for all contracts 14-45 days out, then picks
  "the contract nearest strike A" and "the contract nearest strike B"
  independently. Nothing checks that the two share an expiry date. Target
  strikes are rounded to the nearest $5 whatever the share price.
- Evidence: `options_engine.py:729-732`:
  ```python
  return min(chain_with_quotes, key=lambda cq: abs(cq[0].strike_price - target_strike))
  ```
  called once per leg (`:537-538`, `:644-645`). There is no comparison of
  `long_contract.expiration_date` with `short_contract.expiration_date`. The
  list is sorted by expiry then strike (`options_data.py:174`), so the
  earliest expiry wins ties, and a later expiry wins when only it lists the
  wanted strike. Rounding (`:719`):
  `round(round(price / increment) * increment, 2)` with `increment=5.0`.
  Output of the real function:

  | Price | Bull spread buy / sell | Bear spread buy / sell |
  | --- | --- | --- |
  | $2 | 0 / 10 | 0 / -10 |
  | $7 | 5 / 15 (buy is 29% in the money) | 5 / -5 |
  | $12 | 10 / 20 | 10 / 0 |
  | $18 | 20 / 30 (11% out, not 3%) | 20 / 10 |
  | $30 | 30 / 40 (sell is 33% away) | 30 / 20 |
  | $98 | 100 / 110 | 95 / 85 |
  | $480 | 495 / 515 | 475 / 455 |

- Why it matters: If the short leg expires after the long leg, then once the
  long leg is gone the account holds an uncovered short call. The ledger
  stores one expiry (from leg 1), and the maximum profit and reward-to-risk
  were computed as if both legs expired together, so the trade passed a test
  that does not describe it. On low-priced stocks the "spread" is in effect a
  single long option with a far-away, nearly worthless short leg, and still
  passes the reward-to-risk test.
- How verified: Ran `_evaluate_bull_call_spread` against a fake chain (no
  network) where the nearer expiry lists strikes 160-175 and the later one
  165, 175, 185, price $160: the signal bought the 165 call in the near
  expiry and sold the 185 call in the later one, and reported reward-to-risk
  7.0. Ran `_round_to_strike` for the table.
- Direction (not a fix): Choose an expiry first, then both strikes within it,
  from the strikes that are actually listed, with a width that scales with
  the share price.

### F-04-09: Entry rules are looser than documented: puts are sold on stocks with no thesis, and thesis direction and age are ignored
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1426-1443`, `stock_agent/options_engine.py:192-256`, `:344-347`, `:404-412`
- What happens: Options candidates are the first 30 universe symbols. A
  symbol with no stored thesis is given conviction 6, and conviction 6 is
  enough for a cash-secured put. Only the stored conviction number is read;
  whether the thesis said BUY, HOLD or SELL, and how old it is, is not.
- Evidence: `scheduler.py:1435-1441`:
  ```python
  thesis = self.portfolio.state.active_theses.get(sym)
  conviction = 6  # default
  target_price = None
  if isinstance(thesis, dict):
      conviction = thesis.get("conviction", 6)
      target_price = thesis.get("target_price")
  ```
  `options_engine.py:196`: `if conviction < 6 or conviction > 8: continue`.
  Theses of every direction are stored (`scheduler.py:964`, before the BUY
  test at `:984`) and entries are never removed (`portfolio.py:350-353` is the
  only writer). Other gaps between stated and real rules:
  - A conviction-8 symbol qualifies for both a put and a bull call spread in
    the same scan; the one-per-underlying guard is built before the scan
    (`scheduler.py:1414-1419`).
  - Bull call spreads run in every regime except CAUTIOUS
    (`options_engine.py:218`), and on stocks already held; the per-symbol
    ceiling does not count them.
  - Bear put spreads use `screener_candidates[:3]` with no conviction test
    (`options_engine.py:243`), so they can be bought on the bot's own
    strongest buy ideas.
  - The covered call keeps the contract with the largest dollar premium
    (`options_engine.py:344-347`), which is the lowest allowed strike (as
    little as 0.1% above the price) at the longest expiry. There is no
    comparison with the position's entry price.
  - The put's strike may be up to 1% above the "desired entry"
    (`options_engine.py:411`), so when a thesis target sits just under the
    price the put can be slightly in the money.
- Why it matters: The bot's stated floor is conviction 7 and it describes
  itself as thesis-driven. Here it takes on an obligation to buy 100 shares
  of a stock it has not analysed, or one the analyst rated HOLD or AVOID
  months ago. The covered-call rule caps the upside of a conviction holding
  at about 1% for up to five weeks, and can lock in a loss on a position that
  is under water.
- How verified: Ran `scan_opportunities` with a fake chain and two
  candidates, one at conviction 6 with no target and one at conviction 8:
  output was a put on each plus a bull call spread on the second. Ran
  `_evaluate_covered_call` with spot $50.00 and strikes 50.5 / 52.5 / 55 / 60
  across four expiries: it chose the 50.5 strike at the longest expiry.
- Direction (not a fix): Josh decides which strategies and thresholds he
  wants (see Questions). Whatever the rules are, they should require a
  current thesis with the right direction.

### F-04-10: The options ledger's profit and loss is wrong in several independent ways
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/options_portfolio.py:463-521`, `:579-586`, `:287-347`, `stock_agent/options_executor.py:308-312`, `stock_agent/scheduler.py:1487-1489`
- What happens: The numbers stored for realized and unrealized options P&L
  do not match what happened at the broker, and for some cases have the wrong
  sign.
- Evidence:
  1. Closing price is taken from the opening fill. Activities are requested
     newest first (`"direction": "desc"`, `options_executor.py:311`), grouped
     per symbol without regard to buy or sell, and the code takes
     `last = fills[-1]` (`options_portfolio.py:490`), which is the oldest.
  2. For a sold single option the buy-back cost is added instead of
     subtracted: `per_share_exit_value = leg2_exit - leg1_exit` is negative
     (`:505`), then `realized = (pos.entry_price - per_share_exit_value)`
     (`:521`).
  3. A position that vanishes with no fill is "assumed expired OTM"
     (`:507-510`): full premium booked as profit, or full debit as loss.
  4. An expired position whose closing-price lookup fails is valued at zero,
     once, permanently (`:473-476`). The lookup always takes the direct-FMP
     branch because `OptionsDataFeed` has no `data_feed` attribute (`:298`).
  5. Open spreads are marked from leg 1 only: both symbols are requested
     (`:567-572`) but only `quotes.get(pos.contract_symbol)` is used
     (`:580`).
  6. The entry price is an estimate, 5% above the mid-price debit for
     spreads (`options_engine.py:592`, `scheduler.py:1487-1489`).
  7. On assignment the options ledger books the gap between strike and
     closing price as a realized loss, and the equity ledger then imports the
     shares at the strike and shows the same gap as an unrealized loss.
- Why it matters: `total_realized_pnl` and every per-position figure in
  `options_portfolio.json` are unreliable. Any judgement about whether
  options have helped or hurt has to come from the broker's records, not from
  this file.
- How verified: Ran the real `sync_from_alpaca` and `refresh_positions` with
  made-up data in a temporary directory. A spread opened for a net 3.00 and
  closed by hand for a net 7.00 (true result +$400) was recorded as -$15. A
  put sold at 1.50 and bought back at 5.00 (true result -$350) was recorded
  as +$650. A spread worth 3.00 (legs 4.50 and 1.50) was marked at 4.50,
  +$135. With the closing price stubbed, expired legs came out at intrinsic
  value as intended; with no price they came out at zero.
- Direction (not a fix): Take entry and exit prices from the broker's fills
  for each leg, by side and time, and keep a position open (and flagged) when
  its outcome cannot be determined.

### F-04-11: The options scan can run several times a day and on market holidays, and each run makes about 300 data calls to read 30 prices
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:98`, `:224-236`, `:1378`, `:1685-1688`, `:1426-1443`, `stock_agent/data_feed.py:107-184`
- What happens: "Already scanned today" is kept in memory only. A restart
  between 09:45 and 14:00 ET runs the scan again, and every merge to `master`
  restarts the service. A market event also clears the flag and re-runs the
  scan. The schedule tests weekday and clock time only, so it also runs on
  exchange holidays.
- Evidence: `self._options_scan_done_today: str | None = None`
  (`scheduler.py:98`); `self._options_scan_done_today = None  # Reset so it
  runs again` (`:1687`). No holiday, clock or calendar lookup exists in
  `stock_agent/` (search). For each of up to 30 symbols the scan calls
  `get_company_fundamentals` (`:1431`), which makes nine FMP requests and one
  Finnhub request (`data_feed.py:109-118`, `:139`, `:148`, `:181`), and uses
  only `company.price`.
- Why it matters: Each extra run can open positions on underlyings not
  already used, up to the cap of ten, on a day that was volatile enough to
  trigger the re-scan. The data calls are wasted spend and rate-limit
  pressure.
- How verified: Read the scheduler paths and `get_company_fundamentals`.
- Direction (not a fix): Persist the once-a-day marker, use the broker's
  market calendar, and fetch prices with a quote call.

### F-04-12: The options modules always call the paper endpoint, whatever `ALPACA_BASE_URL` says
- Severity: Medium (a safety net today; a trap once real money is used)
- Confidence: Confirmed
- Where: `stock_agent/options_executor.py:27`, `stock_agent/options_data.py:28-29`, compare `stock_agent/executor.py:31-32`
- What happens: Equity orders go to `config.ALPACA_BASE_URL`. Options orders,
  positions, activities and contract lookups go to a hard-coded
  `https://paper-api.alpaca.markets`.
- Evidence: `_TRADING_BASE = "https://paper-api.alpaca.markets"` in both
  files; every options URL is built from it. `executor.py:32` is
  `return f"{self.config.ALPACA_BASE_URL}{path}"`.
- Why it matters: Today this means options orders cannot reach a live
  account, which is good. If the equity side were pointed at live with live
  keys, every options call would fail against the paper host. A failing
  positions call wipes the ledger (F-04-04), and the daily scan would keep
  trying. The README's "live mode flips a single config flag" is not true for
  options. Anyone "fixing" the URL later would switch on everything in this
  report at once.
- How verified: Read both modules; printed the constants with
  `ALPACA_BASE_URL` overridden on a `Config` subclass.
- Direction (not a fix): Make the options endpoint an explicit, separate
  decision rather than an accident of two constants.

### F-04-13: Docstrings, comments and the README describe numbers and features the code does not have
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/options_engine.py:9-21`, `:195`, `:509-511`, `:519`, `:621`, `stock_agent/options_executor.py:136-138`, `:152-153`, `stock_agent/scheduler.py:181`, `:202`, `:225`, `README.md:24`
- What happens: Beyond the config comments in F-04-07:
  - Module docstring: "Max 5 open options positions", "Max 5% of portfolio
    per trade", "Auto-close at 50% profit or 3 DTE" (`options_engine.py:16-19`).
    Real: 10, 7%, no auto-close.
  - "allow CSPs on conviction 6-8 (not just 6-7)" is right about the code but
    the default-6 rule is not mentioned (`:195`).
  - Bull call spread: "30-45 DTE", "$5 for stocks < $150, $10 for stocks >=
    $150", "Minimum 2:1" (`:509-511`, `:519`). Real: 14-45 days, $10 / $20,
    1.5.
  - `place_spread_order` docstring says limits at 1.05 / 0.95 of net debit;
    code uses 1.10 / 0.90 (`options_executor.py:136-138`, `:152-153`).
  - Scheduler comments: "Options scan: Mon-Fri, 9:00-9:30 AM ET ... before
    market open" (`:181`) and "Check options positions for auto-close
    triggers" (`:202`). Real: 09:45-14:00, alerts only.
  - README: "Options data — IV, skew, flow for the options engine"
    (`README.md:24`). The engine reads no implied volatility, skew, flow or
    greeks; greeks are stored for display (`options_portfolio.py:641-647`).
- Why it matters: Anyone reading the comments, including an AI assistant
  asked to change this code, would believe tighter limits are in force.
- How verified: Compared each statement with the code.
- Direction (not a fix): Bring the text into line once the rules are decided.

### F-04-14: A large share of the options code is unreachable, and some of it would be harmful if switched back on
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/options_executor.py:182-260`, `:334-441`, `stock_agent/options_portfolio.py:187-237`, `:603-619`, `:750-788`, `stock_agent/options_models.py:178-196`
- What happens: No caller exists for `close_option_position`,
  `_close_option_position_legacy`, `close_spread_position`,
  `get_option_position`, `get_open_orders`, `cancel_order`, `exercise_option`
  (executor); `close_position`, `get_auto_close_candidates`,
  `get_near_expiry_positions`, `find_position_by_contract`,
  `format_portfolio_summary_embed` (portfolio); `should_auto_close`,
  `close_trigger_reason` (models); `get_option_quote` (data);
  `LOSS_ALERT_PCT`, `PROFIT_TAKE_PCT`.
- Evidence and hazards:
  - Neither close variant is live. `close_spread_position` calls
    `close_option_position` for both legs with the default
    `position_side="long"` (`options_executor.py:258-259`), so it would sell
    the short leg again rather than buy it back, and with its default
    `qty=None` it raises `TypeError` at `abs(qty)` (`:209`).
  - The limit-order path of `place_spread_order` sets each leg's limit from
    the net debit (`:152-153`): for legs worth 4.50 and 1.50 it would bid
    3.30 for the long and offer the short at 2.70.
  - The 50% profit trigger divides by `max_loss`, which for a put is
    strike x 100 (`options_portfolio.py:262-263`): a put at full profit shows
    about 2%, so the trigger could never fire for puts.
  - The auto-close removed in `602aa96` chose the side with
    `"long" if pos.qty > 0 else "short"`, and `qty` is positive for every
    position, so sold options were "closed" with another sell. The stated
    reason for disabling it ("Alpaca paper trading rejects SELL orders on
    long options as 'uncovered'", `scheduler.py:1526-1527`) is consistent
    with that bug and with positions that never existed at the broker
    (F-04-01). Suspected, not shown: it needs the April logs.
- Why it matters: Clutter today. The risk is someone re-enabling auto-close
  by calling what is already there.
- How verified: Repository-wide search for each name; ran
  `close_spread_position` and the limit path with a stubbed submit; ran
  `update_position_price` on a put at price 0 (2.14%).
- Direction (not a fix): Remove or rewrite as part of deciding the exit
  policy in F-04-02.

### F-04-15: Smaller defects in monitoring and parsing
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1414-1419`, `:1503`, `:1552-1571`, `stock_agent/options_portfolio.py:588-595`, `:826-851`, `stock_agent/options_models.py:172-176`, `stock_agent/options_engine.py:573`
- What happens:
  - The expiry alert has no "already sent" memory: every position with 3 or
    fewer days left is re-announced every 30 minutes of market time
    (`scheduler.py:1552-1571`).
  - The open alert is requested with type `"OPENED"`; the formatter knows
    `"open"` (`options_portfolio.py:693-700`, `:736-742`), so the embed gets
    the generic title and grey colour. Any error posting it is swallowed
    (`scheduler.py:1507-1508`).
  - The underlying for the duplicate guard is taken from the contract symbol
    with `([A-Z]+)` (`scheduler.py:1417`) instead of the stored
    `underlying_symbol`; a class share such as `BRK.B` yields `BRKB` and
    never matches the universe symbol.
  - `_expiry_from_contract_symbol` starts at the first digit, so a root that
    contains a digit (adjusted contracts) fails to parse and the expiry falls
    back to "today plus the maximum window" (`options_portfolio.py:157-161`).
  - `refresh_positions` logs "updated N positions" and stamps `last_updated`
    even when the snapshot request returned nothing (`:588-595`).
  - Days to expiry use the UTC date (`options_models.py:175`) while the
    engine uses the server's local date (`options_engine.py:297`).
  - A portfolio value of zero raises `ZeroDivisionError` in the spread
    evaluators (`options_engine.py:573`, `:673`); the scan's outer `except`
    catches it and the whole scan is abandoned.
- Why it matters: Alert noise and small wrong numbers; none moves money on
  its own.
- How verified: Read the code; ran the two parsers on sample symbols and the
  formatter with `"OPENED"`.
- Direction (not a fix): Fold into the rewrite of monitoring.

## Checked and fine

- Covered calls can only target out-of-the-money strikes. The chain request
  asks for strikes from price x 1.001 up (`options_engine.py:303-314`), each
  contract is re-checked with `strike_price <= current_price` rejected
  (`:331-333`), and a holding with no usable price is skipped
  (`:160-178`). Caveat: the price is FMP's profile price fetched earlier in
  the same scan, or Alpaca's latest trade, and is not re-read at order time.
- Covered-call quantity cannot exceed the local share count: 99 shares gives
  none, 250 gives 2 (`options_engine.py:159`, `:282`; ran it).
- `exercise_option` is never called (`options_executor.py:412`; search).
- `_close_option_position_legacy` is never called. The non-legacy variant is
  not called either (F-04-14).
- Expired in-the-money legs are valued at intrinsic value when a closing
  price is available, matching the decision on record
  (`options_portfolio.py:270-285`, `:349-387`, `:463-484`). Ran it with
  stubbed closes: 105/115 call spread at $112 gives 7.00; short 60 put at $55
  gives 5.00. The failure case is in F-04-10.
- Weekend and holiday expiries: the lookup takes the latest daily row on or
  before the expiry date within seven days (`options_portfolio.py:299-302`,
  `:330-339`).
- The position cap of 10 holds within one scan (`options_engine.py:128-133`,
  `:144`, and each `slots_remaining -= 1`).
- The 7% per-trade rule is applied to a put's assignment cost and to a
  spread's estimated debit (`options_engine.py:429-445`, `:572-575`,
  `:672-675`).
- A spread with zero or negative debit, or no possible profit, is rejected;
  that also covers both legs resolving to the same contract
  (`options_engine.py:552-561`, `:658-666`).
- `limit=50` on chain requests is a page size, not a cut-off; pagination
  follows `next_page_token` (`options_data.py:122-168`).
- The equity ledger does not import option legs as stocks
  (`portfolio.py:131-138`).
- Alpaca keys are sent in headers only by the options modules
  (`options_executor.py:62-67`, `options_data.py:60-65`).
- Options ledger writes are write-then-rename with a backup copy
  (`options_portfolio.py:85-116`); mechanics belong to area 07.
- Because of the hard-coded paper host, options orders cannot reach a live
  account at this commit (F-04-12 covers the other side of that).
- `python3 -m pytest -q` passes at this commit; the tripwire test pins the
  live constants listed in F-04-07.

## Could not verify

- **Whether any of this has already happened.** Needs the droplet snapshot:
  `data/stock_agent/options_portfolio.json` (counts of closed positions by
  `close_reason`, especially "missing at Alpaca (assumed expired OTM)" closed
  within an hour of opening, and "no EOD price"), and service log lines
  matching "Order submission failed", "Options reconcile", "get_option_positions
  failed".
- **The account's options approval level, whether it is a margin account, and
  its options buying power.** `scripts/alpaca_readonly_report.py:189-190`
  already prints these fields; its output is needed.
- **What the broker did with each options order.** Needs an Alpaca order
  history export for option symbols: statuses, rejection reasons, whether
  spread legs filled separately, and any option or short stock position at
  the broker that the ledger does not show.
- **Broker rules this report leans on.** Not checked against Alpaca's
  documentation or sandbox in this session (no external calls allowed):
  whether a single multi-leg order is available on paper and live (the code
  says legs are processed individually); whether selling the shares or a
  resting stop order is refused while a short call is open, and whether a
  covered call is accepted while a stop order holds the shares; what happens
  on expiry day to in-the-money options (automatic exercise, forced
  liquidation when buying power is short); whether a short put needs cash or
  is accepted against margin; how delayed the `indicative` feed is; how
  quickly a paper market order shows up in positions.
- **How often real chains list a strike in one expiry and not another**
  (F-04-08). Needs a sample of real chains for a few universe symbols.
- **The droplet's timezone**, which decides what `date.today()` means in
  `options_engine.py`. Needs the snapshot.

## Questions for Josh

1. Do you want options trading running at all while the revamp is under way?
   Today it cannot be switched off with `OPTIONS_ENABLED`.
2. Which of the four strategies do you actually want: covered calls,
   cash-secured puts, bull call spreads, bear put spreads?
3. What should "15% options exposure" mean: premium paid, maximum loss, or
   the cash and shares that assignment could demand?
4. Should the cash behind a sold put be set aside so the equity side cannot
   spend it?
5. Should options be closed before expiry (for example at a profit target or
   a few days out), or are you content to be assigned and exercised? Do you
   ever want to end up with shares, or short shares, through an option?
6. Does the minimum conviction of 7 apply to options? Puts are currently sold
   at 6, including on stocks with no thesis.
7. Do you want covered calls sold on thesis positions at all, given they cap
   the upside just above today's price?
8. Have you closed option positions by hand in the Alpaca dashboard? It
   decides how much of the ledger's history can be trusted.
9. What options approval level does the Alpaca account have, and is it a
   margin account?
10. When the bot goes live for equities, should options stay on paper, go
    live too, or be off?

## Handoffs

- 01: the options "done today" flag is in memory, a market event re-runs the scan, and there is no market-holiday check (`scheduler.py:98`, `:224-236`, `:1685-1688`); see F-04-11.
- 02: `risk_manager.py` has no notion of options; put obligations and spread exposure are outside every equity cap; options trade at conviction 6 (`scheduler.py:1436`).
- 03: the equity reconcile skips any position with quantity <= 0, so a short stock position is invisible (`portfolio.py:160-162`, `:180-181`); imported positions get a 5% local stop and no broker stop (`portfolio.py:202`); dropped positions get no trade record (`portfolio.py:149-156`); the morning-brief reconcile is skipped when the positions list is empty (`scheduler.py:1732`).
- 03: `_execute_sell` and the resting stop order do not consider open short calls on the same symbol (`scheduler.py:1323`, `executor.py:117-124`).
- 06: `_run_options_scan` uses `get_company_fundamentals` (ten requests) per symbol only to read a price (`scheduler.py:1431`); `options_portfolio.py:313-322` calls FMP directly with its own HTTP client, bypassing `DataFeed`.
- 07: `closed_positions` grows without limit and `total_realized_pnl` contains invented entries (F-04-10); on assignment the same loss is in both ledgers.
- 08: options positions and P&L appear in no daily or weekly report (`format_portfolio_summary_embed` has no caller); expiry alerts repeat every 30 minutes; the morning brief counts option legs in "N / 20 slots" (`scheduler.py:1744`); a trade embed is posted for spreads that did not execute (F-04-01).
- 09: `options_portfolio.py:315` reads `FMP_API_KEY` from `os.environ` rather than through `config.py` and sends it as a URL query parameter (`:319`); `config.py:194-195` carry default values for `TIPRANKS_API_KEY` and `TIPRANKS_API_TOKEN` in a public repository; `scripts/vps_snapshot.sh:177` reports `OPTIONS_ENABLED` as if it had an effect.
- 10: no test exercises any options logic beyond constant values; `tests/test_risk_tripwires.py:98-118` pins both the unused config values and the live constants.
