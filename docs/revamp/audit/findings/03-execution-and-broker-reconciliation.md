# 03. Execution and broker reconciliation

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

This part of the bot turns a decision into Alpaca orders and keeps the bot's
own notebook (the "ledger", `portfolio.json`) in line with what Alpaca really
holds. A buy is two separate orders: a market buy, then, three seconds later,
a stop-loss sell order that rests at Alpaca. The bot never checks what either
order actually did. It records the price it saw during research rather than
the price it paid, and it writes down a stop even when Alpaca refused the stop
order. It never cancels a stop order and never looks at its open orders.

Reconciling the ledger with Alpaca happens on Sunday and Wednesday evenings
and as a side effect of the weekday morning brief. It does not happen at
startup, before orders are sent, or in the 30-minute monitoring loop that
sells on stop-losses.

Three things matter most:

1. **A stop-out can be sold twice.** Alpaca's resting stop sells the shares,
   the ledger does not find out, and the monitoring loop then sends its own
   market sell for the same shares. On an account that allows short selling
   that second sell opens a short position, and the reconciliation code
   throws away negative share counts, so the bot can never see it (F-03-01).
2. **The bot's own sells probably do not work while a stop order is
   resting.** Alpaca reserves shares for an open stop order, the bot never
   cancels the stop first, and a refused sell produces a log line and no
   alert. "Thesis broken" exits would silently not happen (F-03-02).
3. **One failed "list positions" request on a Sunday or Wednesday wipes the
   ledger**: every thesis, entry date and sector is lost and that cycle's
   review of existing holdings is skipped (F-03-04).

Several conclusions depend on how Alpaca answers particular orders. No broker
calls are allowed in the audit and no droplet snapshot was available, so those
parts are marked Likely or Suspected and listed under "Could not verify".

One dated item: the bot decides "market open" from the weekday and the clock
only. It will treat Thanksgiving (Thursday 26 November 2026) as a normal
trading day and send Wednesday evening's queued orders that morning (F-03-09).

## How it actually works

### Every Alpaca call the equity side makes

`scheduler.py` calls exactly four methods on the equity `Executor`
(`grep "self\.executor\."` gives lines 816, 817, 1264, 1323, 1716, 1717):

| Method | Request | Called from |
| --- | --- | --- |
| `get_account` | `GET /v2/account` | `_sync_portfolio_from_alpaca` (816), morning brief (1716) |
| `get_positions` | `GET /v2/positions` | `_sync_portfolio_from_alpaca` (817), morning brief (1717) |
| `place_buy` | `POST /v2/orders` twice | `_execute_signals` (1264) |
| `place_sell` | `POST /v2/orders` | `_execute_sell` (1323) |

`cancel_order`, `get_order`, `get_open_orders` and `cancel_all_orders`
(`executor.py:163-188`) have no callers. Nothing in `stock_agent/` sends a
`client_order_id`, reads `/v2/clock` or `/v2/calendar`, or reads an order back
after sending it.

### A buy, step by step

| # | Step | Where | Known for certain | Assumed |
| --- | --- | --- | --- | --- |
| 1 | A signal is created with `entry_price=company.price` | `scheduler.py:984-996`, `650-664`, `769-781` | The price at analysis time | That it is still the price when the order goes out |
| 2 | Market closed: the signal waits in `self._pending_signals` (memory only) | `scheduler.py:433-438`, `543-547`, `664`, `781` | Nothing is on disk | The process is not restarted before the open |
| 3 | First tick on a weekday between 09:30 and 16:00 ET runs the queue | `scheduler.py:192-197` | Weekday and wall clock | The exchange is open (no holiday or early-close check, `297-302`) |
| 4 | Risk check, share count and stop price, all from the signal price and the ledger | `scheduler.py:1201-1261` | Ledger contents | Ledger equals the broker; price is current |
| 5 | `POST` market buy, time-in-force `day` | `executor.py:95-104` | Alpaca returned 2xx with a JSON body (an order was accepted) | It filled, completely |
| 6 | `await asyncio.sleep(3)` | `executor.py:114-115` | Nothing | "Give the fill a moment" |
| 7 | `POST` stop sell, GTC, for the full requested quantity | `executor.py:117-127` | Accepted or not, written to the log only | The shares exist to protect |
| 8 | `place_buy` returns the buy order; the scheduler tests only that it is truthy | `executor.py:134`, `scheduler.py:1264-1267` | The buy was accepted | Buy filled and stop is live |
| 9 | Ledger: `shares=qty`, `entry_price=price` (signal price), `stop_loss=stop`; cash debited `price * qty` | `scheduler.py:1280-1292`, `portfolio.py:93-98` | | All of it |
| 10 | Trade record and alerts use the same assumed numbers | `scheduler.py:1295-1310` | | |

The fill price and filled quantity are never read at trade time. They first
reach the ledger at the next reconciliation, from Alpaca's position fields
`qty` and `avg_entry_price` (`portfolio.py:157-164`).

### A sell, step by step

1. Trigger: software stop in the monitoring loop (`scheduler.py:1046-1053`),
   material event (`1087-1091`), thesis turned SELL on re-analysis
   (`1022-1026`), post-earnings re-analysis (`725-729`).
2. `_execute_sell` fetches an FMP quote as the assumed sale price
   (`1320-1321`).
3. It sends a market sell, `day`, for the ledger's share count (`1323`,
   `executor.py:145-154`). The resting stop is not cancelled first; open
   orders and the broker position are not consulted.
4. If the request comes back empty the function logs and returns (`1324-1326`).
5. Otherwise it books P&L from the quote, removes the position, credits cash
   (`1328-1346`), then raises on line 1349 (F-03-10).

There are no partial sells anywhere in the code; every sell is for the whole
ledger position.

### When reconciliation runs

| When (ET) | Where | Condition |
| --- | --- | --- |
| Sunday 18:00-20:59, top of weekly cycle | `scheduler.py:371` | Skipped only if the account call failed and positions came back empty (`818-820`) |
| Wednesday 18:00-20:59, top of midweek cycle | `scheduler.py:491` | Same |
| Weekdays 08:15-08:45, inside the morning brief | `scheduler.py:1716-1733` | Only if the account call succeeded and the positions list is non-empty |

It does not run at process start (`run`, `109-124`), before
`_execute_signals`, in `_run_market_monitoring`, before `_execute_sell`, in
the pre-market or earnings scans, or in the event-triggered scan
(`1578-1696`).

What a sync does (`portfolio.py:120-225`):

- Ledger position that Alpaca also has: shares, average entry, price and P&L
  are overwritten from Alpaca; thesis, sector, entry date and the stored
  `stop_loss` are kept.
- Ledger position Alpaca does not have: dropped with a WARNING log line
  (`149-156`). No trade record, no realised P&L, no alert.
- Alpaca position the ledger does not have: imported with an invented thesis
  ("Imported from Alpaca — awaiting re-analysis", direction HOLD, conviction
  7), `entry_date` set to now, `stop_loss` set to 5% under average entry, and
  sector from `Config.SECTOR_MAP` or "Unknown" (`175-208`).
- Short positions (negative quantity) and positions under one share are
  ignored (`160-162`, `180-181`). Option symbols are skipped by design.
- Cash is taken from the account when the account call succeeded (`215-219`).

### Where reality differs from the text

- `place_buy` docstring: "Stop sell (GTC) — placed after fill confirmed"
  (`executor.py:88`). No fill is confirmed.
- `scheduler.py:1046`: "belt-and-suspenders with Alpaca bracket orders". There
  are no bracket orders; there are two independent orders plus the loop.
- `_sync_portfolio_from_alpaca` docstring: "so risk checks see real weights"
  (`scheduler.py:811-813`). The risk check runs in `_execute_signals` at the
  next open, at least 12.5 hours after that sync.
- `scheduler.py:613`: "If market is open, execute immediately; otherwise
  queue". Nothing is queued (F-03-08).
- README: "live mode flips a single config flag". `STOCK_AGENT_MODE` is only
  printed (F-03-14).

### What would differ on a live account

- Nothing in the code branches on paper versus live; only the endpoint in
  `ALPACA_BASE_URL` differs (F-03-14).
- The two-order design exists because of a paper-account quirk: "Bracket
  orders on Alpaca paper have been unreliable" (`executor.py:85-86`). That
  reason has not been tested against a live account.
- Paper fills are simulated. The three-second wait before the stop (F-03-05)
  and the unlimited market orders at the open (F-03-15) meet real auctions,
  halts, partial fills and slippage only when live.
- Whether the second sell in F-03-01 becomes a short or is refused depends on
  the account's short-selling permission, which can differ between the paper
  account and a live one.
- Every finding rated here costs paper money today and real money once live;
  F-03-14 is the only one that matters solely after the switch.

## Findings

How the demonstrations were run: throwaway scripts outside the repository,
with `STOCK_DATA_DIR` pointed at a temporary directory and all credentials
blanked. `Portfolio.sync_from_alpaca` was called directly with made-up broker
rows. `Executor` was driven through `httpx.MockTransport` or a replaced
`_request`. Scheduler paths were run on `StockAgentScheduler.__new__(...)`
with fake executor, data feed, analyst and alert objects that record calls.
No network request was made and the agent was not run.

### F-03-01: A stop-out can be sold twice, leaving a short position the ledger cannot see
- Severity: Critical
- Confidence: Likely
- Where: `stock_agent/scheduler.py:1032-1053`, `stock_agent/scheduler.py:1316-1326`, `stock_agent/executor.py:117-127`, `stock_agent/portfolio.py:157-162`, `stock_agent/portfolio.py:175-181`
- What happens: Each position has two stop-losses at about the same price: a
  resting stop order at Alpaca and a check in the 30-minute loop. When
  Alpaca's stop sells the shares the ledger is not told, so the loop still
  sees the position, sees the price under the stop, and sends its own market
  sell for the full share count. With no shares left, that order is a short
  sale.
- Evidence:
  - Broker stop: `executor.py:117-124` sends `"side": "sell", "type": "stop",
    "time_in_force": "gtc"` at `get_stop_loss_price(price, beta)`
    (`scheduler.py:1259`).
  - Software stop: `check_position_health` recomputes the same formula from
    the ledger's entry price (`risk_manager.py:215-216`), and the loop acts on
    it:
    ```python
    alerts = self.risk_manager.check_position_health(self.portfolio.state)
    for alert in alerts:
        ...
            await self._execute_sell(pos, reason=alert["reason"])
    ```
    (`scheduler.py:1047-1053`).
  - `_execute_sell` goes straight to `place_sell(position.symbol,
    position.shares)` (`scheduler.py:1323`). Nothing reads the broker
    position or open orders first, and no reconciliation runs in the loop
    (see the table above).
  - After the second sell the ledger removes the position
    (`scheduler.py:1345-1346`). At the next sync the broker reports a negative
    quantity, and both branches discard it: `if shares <= 0: dropped += 1;
    continue` (`portfolio.py:160-162`) and `if shares <= 0 or avg_entry <= 0:
    continue` (`portfolio.py:180-181`). No log line is written for it.
  - Example: 100 shares, signal price 100.00, both stops at 93.00. 11:07 the
    price trades at 92.90 and Alpaca's stop sells 100. 11:30 the loop reads
    92.50 from FMP, `92.50 <= 93.00`, and sends a market sell for 100. Alpaca
    now shows -100 shares; the ledger shows none.
  - Corroboration, not proof: `executor.py` has not changed since it was added
    on 2026-04-06 (`git log -- stock_agent/executor.py` shows one commit), and
    the comment added on 2026-04-25 at `config.py:157-161` describes every
    sell up to then as a stop-out, quoting them in the "price vs stop" form
    that only the monitoring loop produces (`risk_manager.py:223`). So the
    loop's sells were being accepted while broker stops were also being
    placed.
- Why it matters: A short position has no upper limit on loss, has no stop,
  counts toward no risk limit, and stays open until Josh notices it in the
  Alpaca dashboard. If short selling is not permitted on the account the
  second sell is refused instead and the only effect is repeated error lines.
- How verified: Read both paths end to end. Ran `_run_market_monitoring` with
  a fake executor and a ledger position priced under its stop: the only
  broker call was `place_sell AAA qty=100`. Ran `sync_from_alpaca` with rows
  of quantity -30 (known symbol) and -10 (unknown symbol): the ledger ended
  empty. Not verified: that Alpaca accepts the second sell as a short. That
  is Alpaca's documented handling of a sell with no position on a
  shorting-enabled margin account; it was not exercised.
- Direction (not a fix): Decide on one owner for the stop. Before any sell,
  ask the broker what is held and what orders are open. Treat a negative
  quantity at the broker as an alarm, never as "nothing there".

### F-03-02: The bot never cancels its resting stop, so its own sells are expected to be refused while the stop is live
- Severity: High
- Confidence: Likely
- Where: `stock_agent/scheduler.py:1316-1326`, `stock_agent/executor.py:136-161`, `stock_agent/executor.py:163-188`
- What happens: When the bot decides to sell (thesis broken, bad earnings,
  material event, software stop), it sends a market sell for all the shares
  while a stop order for those same shares is still open at Alpaca. Alpaca
  reserves shares for open sell orders, so the new sell is expected to be
  refused. The bot logs the refusal and moves on.
- Evidence:
  - No cancel anywhere: `cancel_order`, `cancel_all_orders` and
    `get_open_orders` have zero callers; the stop's order ID is logged
    (`executor.py:130`) and then discarded, and `Position` has no field for it
    (`models.py:86-99`).
  - `_execute_sell` sends the sell with nothing before it
    (`scheduler.py:1320-1323`), and on an empty reply does only this:
    ```python
    if not order:
        logger.error("Sell order failed for %s", position.symbol)
        return
    ```
    (`scheduler.py:1324-1326`).
  - `_request` turns an HTTP 403 into `None` (`executor.py:55-63`).
- Why it matters: Every exit other than "price hit the stop" may be
  ineffective for any position whose stop was placed successfully. The
  position stays, the ledger stays, and nothing reaches Telegram or Discord
  (F-03-13). Software-stop sells are retried every 30 minutes and refused each
  time until the broker stop fires, after which F-03-01 applies.
- How verified: Read the code path; confirmed the zero callers by search. Ran
  the monitoring loop twice with a fake executor whose `place_sell` returns
  `None`: two identical sell attempts, no alert calls, ledger unchanged. Not
  verified: the refusal itself. Alpaca documents an available quantity per
  position that excludes shares held for open orders and rejects larger sells
  as "insufficient qty available"; the audit could not exercise it.
- Direction (not a fix): A sell needs to deal with the resting stop first and
  then confirm what happened. The stop's order ID has to be kept somewhere.

### F-03-03: Reconciliation with Alpaca does not run before orders are sent
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:371`, `stock_agent/scheduler.py:491`, `stock_agent/scheduler.py:808-823`, `stock_agent/scheduler.py:1731-1733`, `stock_agent/portfolio.py:147-156`
- What happens: The decision on record is that the ledger is reconciled with
  Alpaca before risk checks. In the code that is true only in the sense that
  a sync runs at the top of the Sunday and Wednesday evening cycles. The risk
  check and the orders happen the next morning, and every other order path
  uses whatever the ledger last said.
- Evidence:
  - `sync_from_alpaca` has three call sites in total: lines 371 and 491 (via
    the helper) and line 1733 (morning brief).
  - `_execute_signals` is reached from `_tick` at the open (`196`) and from
    the event-triggered scan during market hours (`1680`); neither syncs
    first. The weekly and midweek cycles call it only `if
    self._is_market_open(...)` (`433-434`, `543-544`), which is never true on
    a Sunday or at 18:00.
  - `_execute_sell` and `_run_market_monitoring` never sync.
  - `run()` does not sync at startup (`109-124`), and the service restarts on
    every push to `master`.
  - The morning-brief sync is conditional (`if alpaca_account:` then `if
    alpaca_positions:`) and sits inside a function whose errors are swallowed
    (`1934-1935`).
  - A position that vanished at the broker is dropped with one WARNING and
    nothing else (`portfolio.py:149-156`): no `Trade`, no realised P&L, no
    alert. A broker stop-out is therefore never announced and never appears
    in trade history or statistics.
  - History: the message of commit `4fa68de` describes a real incident in
    which the ledger and Alpaca disagreed on a share count and repeated
    top-ups followed.
- Why it matters: Between syncs the ledger can hold positions Alpaca has
  already sold (F-03-01), or miss positions Alpaca holds (F-03-05, F-03-06,
  F-03-09). Risk checks for intraday buys, the software stop, and the share
  counts passed to the options scan (`scheduler.py:1406-1408`) all read that
  ledger.
- How verified: Listed every caller of `sync_from_alpaca`,
  `_sync_portfolio_from_alpaca`, `_execute_signals` and `_execute_sell` by
  search and read each one.
- Direction (not a fix): Reconcile immediately before anything that sends an
  order and at startup, and turn "dropped" into a recorded, announced event.

### F-03-04: A failed "list positions" request is treated as "the account is empty" and wipes the ledger
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/executor.py:75-80`, `stock_agent/scheduler.py:815-821`, `stock_agent/portfolio.py:147-156`, `stock_agent/portfolio.py:175-208`
- What happens: `get_positions` returns an empty list both when the account
  has no positions and when the request failed. If the account request
  succeeds and the positions request fails, the Sunday or Wednesday sync
  deletes every position from the ledger.
- Evidence:
  - `executor.py:77-80`: `if isinstance(data, list): return data` then
    `return []`.
  - The guard only covers a double failure:
    ```python
    if account is None and not positions:
        ...
        return
    self.portfolio.sync_from_alpaca(positions or [], account)
    ```
    (`scheduler.py:818-821`).
  - `sync_from_alpaca([], account)` then drops each position
    (`portfolio.py:147-156`).
  - The morning brief resolves the same ambiguity the other way: `if
    alpaca_positions:` (`scheduler.py:1732`) skips the sync when the list is
    empty, so an account that really is all cash keeps its stale ledger rows
    until Sunday or Wednesday.
- Why it matters:
  - Permanent loss: when positions are re-imported later they carry the
    placeholder thesis, conviction 7, today's date as entry date, and sector
    "Unknown" unless the symbol is in `SECTOR_MAP`.
  - That cycle's `_re_analyze_existing_positions` loops over an empty list
    (`scheduler.py:1005`), so no holding is reviewed and no broken thesis is
    sold.
  - Signals for symbols already held are queued as if they were new. The
    usual rescue is the next morning brief re-importing the positions before
    the open. If that sync is skipped or fails too, the open-time risk check
    sees an empty book and buys full-size positions on top of existing ones.
- How verified: Ran `Executor.get_positions` through a mock transport: 403,
  429, 500, a timeout, a connection error, a non-JSON 200 and an empty 200 all
  return `[]`, identical to a genuine empty list. Ran `sync_from_alpaca([],
  {"cash": ...})` on a ledger with two positions: both dropped. Ran a second
  sync with the positions present: both came back as HOLD / conviction 7 /
  "Imported from Alpaca" / entry date today / sector "Unknown".
- Direction (not a fix): A failed request must be distinguishable from an
  empty result, and a sync that would remove everything deserves a second
  look before it is applied.

### F-03-05: The stop order is sent blind three seconds after the buy, and a failed stop is not reported or recorded
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/executor.py:82-134`, `stock_agent/scheduler.py:1264-1310`
- What happens: After the buy is accepted the code waits three seconds and
  sends a stop for the full requested quantity, without asking whether the
  buy filled, partly filled, or is still waiting. If the stop is refused the
  only trace is one WARNING log line; the ledger and the trade alert still
  show a stop.
- Evidence:
  - `await asyncio.sleep(3)  # Give the fill a moment` (`executor.py:115`),
    then `"qty": str(qty)` in the stop body (`executor.py:119`). The buy
    response's `status`, `filled_qty` and `filled_avg_price` are not read;
    `get_order` is never called.
  - Stop failure:
    ```python
    else:
        logger.warning("Stop-loss order FAILED for %s — position is unprotected", symbol)

    return result
    ```
    (`executor.py:131-134`). The caller receives the buy order either way and
    writes `stop_loss=stop_loss` into the position (`scheduler.py:1287`) and
    into the thesis shown in the Telegram alert (`scheduler.py:1260-1261`).
  - Restart window: the ledger entry is written only after `place_buy`
    returns (`scheduler.py:1264-1292`). If the process is killed between the
    two requests, Alpaca holds shares with no stop and the ledger has no
    record. Shutdown only sets a flag (`__main__.py:16-17`,
    `scheduler.py:105-107`); the unit file sets no stop timeout, so systemd's
    default kill applies to a tick that is still running; and
    `deploy/auto_update.sh` restarts the service whenever `master` changes.
    Startup does not reconcile (`scheduler.py:109-124`).
- Why it matters: Cases the code does not handle:
  - Buy not filled within three seconds (opening auction, halt, exchange
    closed): a sell stop is sent for shares the account does not hold.
  - Partial fill: the stop is for more shares than are held.
  - Stop refused for any reason: the position is covered only by the
    30-minute loop during market hours, and Josh is not told.
  - Restart between the two orders: no stop, no ledger entry, until the next
    reconciliation imports it as a placeholder (and no stop order is ever
    created for an imported position, F-03-12).
- How verified: Replaced `_request` with a scripted fake and patched the
  sleep. With the buy reply `status: accepted, filled_qty: 0`, and with
  `partially_filled, filled_qty: 40`, the stop request was identical: sell
  100, stop, GTC. With the stop reply `None`, `place_buy` returned the buy
  order unchanged. Not verified: how Alpaca answers a stop sent before the
  fill (refusal, or acceptance as an order to sell shares not held).
- Direction (not a fix): The stop should follow a confirmed fill and cover the
  filled quantity, and "no stop in place" should be a state the bot records
  and announces.

### F-03-06: A timed-out or unreadable broker reply is treated as "the order was not placed", and there is no order ID to check or de-duplicate with
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/executor.py:34-66`, `stock_agent/executor.py:104-108`, `stock_agent/scheduler.py:1264-1267`, `stock_agent/scheduler.py:1323-1326`
- What happens: Every failure of an Alpaca request is flattened to `None`: a
  refusal, a rate limit, a server error, a 30-second timeout, a reply that
  is not valid JSON. For an order, "refused" and "sent but the answer was
  lost" are opposite situations, and the code treats both as "nothing
  happened". No request carries a `client_order_id`, there is no record of
  orders in flight, and nothing checks for an existing open order.
- Evidence:
  - `except Exception as e: ... return None` (`executor.py:64-66`); client
    timeout 30 s (`executor.py:17`); no retry or backoff anywhere in the
    file.
  - Buy: `if not result: ... return None` (`executor.py:106-108`) returns
    before the stop is attempted; the scheduler then skips the ledger write
    (`scheduler.py:1265-1267`).
  - Sell: `scheduler.py:1324-1326` returns with the ledger unchanged, and the
    monitoring loop repeats the attempt 30 minutes later.
  - Order bodies contain only symbol, qty, side, type, time_in_force (and
    stop_price) (`executor.py:95-101`, `117-124`, `145-151`).
- Why it matters:
  - Buy lost in transit but accepted by Alpaca: a position with no stop and
    no ledger entry. A later signal for the same symbol sees "not held" and
    buys again at full size.
  - Sell lost in transit but accepted: the loop sells the same shares again
    half an hour later (a short, as in F-03-01).
  - The only protection against a repeated job sending the same buy twice is
    the ledger entry written after a successful reply. That works when the
    first order was recorded (see "Checked and fine") and not otherwise.
- How verified: Mock transport: 403, 429, 500, read timeout, connect error,
  non-JSON 200 and empty 200 all produce `_request(...) is None`. Fake
  executor with `place_buy` returning `None`, same signal run twice: two
  identical buy requests, nothing in the ledger, no alert calls.
- Direction (not a fix): Give every order an ID the bot chooses, and after an
  uncertain reply look the order up before deciding it failed.

### F-03-07: Share count, stop price and recorded entry all use the price from analysis time, not a current quote or the fill
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1201-1208`, `stock_agent/scheduler.py:1252-1261`, `stock_agent/scheduler.py:1280-1303`
- What happens: A signal carries the price seen when the research ran
  (Sunday evening: Friday's close). At the open the bot uses that old price
  for the risk check, the number of shares, the stop price, and the entry
  price in the ledger. A fresh quote is fetched only when the signal has no
  price at all.
- Evidence:
  ```python
  price = signal.entry_price or 0
  if price <= 0:
      quote = await self.data_feed.get_stock_quote(sym)
  ```
  (`scheduler.py:1201-1203`), then `calculate_position_size(pv, price, ...)`
  (`1252`), `get_stop_loss_price(price, beta)` (`1259`),
  `entry_price=price` (`1283`).
- Why it matters, with numbers (conviction 10, target 10% of 100,000,
  analysis price 100.00, so 100 shares and a stop at 93.00):
  - Opens at 108: cost 10,800, which is 10.8% rather than 10%; the stop at
    93.00 is 13.9% under the fill, outside the 7-12% range on record. The
    ledger shows an 8% gain that does not exist until the next sync.
  - Opens at 91: the stop price is above the market. The ledger entry is
    100, so the monitoring pass in the same tick sees `91 <= 93` and sends a
    sell for the shares just bought.
  - Any signal can also sit in the queue across a gap with no check of how
    far the price has moved or how old the signal is.
- How verified: Ran `_execute_signals` with a fake executor and a signal
  priced 100: the broker call was `place_buy NEW qty=100 stop=93.0` and the
  ledger recorded entry 100.00. Then, with the fake quote feed at 91, ran
  `_execute_signals` followed by `_run_market_monitoring` as `_tick` does
  (`scheduler.py:196-201`): the broker calls were `place_buy GAP qty=100
  stop=93.0` and then `place_sell GAP qty=100`, and the ledger showed a BUY
  at 100 and a SELL at 91.
- Direction (not a fix): Price the order at the moment it is sent, set the
  stop from the actual fill, and decide how much movement since the analysis
  cancels a signal.

### F-03-08: A sell decided in the pre-market scan is logged as "queued" but nothing is queued
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:605-624`
- What happens: When the 08:00 scan finds a severe negative event on a
  holding and the re-analysis says SELL, the code logs "Queuing sell for ...
  at market open" and does nothing else. No sell is stored, and the new SELL
  thesis is not saved either.
- Evidence:
  ```python
  if self._is_market_open(now):
      await self._execute_sell(...)
  else:
      logger.info("Queuing sell for %s at market open: %s", ...)
  ```
  (`scheduler.py:614-622`). `now` is taken at the start of the scan, which
  only runs between 08:00 and 09:00 (`274-283`), and the market opens at
  09:30 (`60`, `297-302`), so the first branch cannot run. `update_thesis` is
  only in the `elif new_thesis:` branch (`623-624`). `_pending_signals` holds
  BUY signals only; `_execute_signals` skips anything else (`1188-1189`).
- Why it matters: The case this branch exists for, bad news overnight on a
  stock the bot owns, produces no exit. The position leaves only if the
  price reaches the stop, or if a later intraday event check (even hours
  only, `1103-1108`) happens to reach the same conclusion again.
- How verified: Read the branch. Evaluated `_should_run_daily_prescan` and
  `_is_market_open` for 08:00, 08:30 and 09:00 ET on a weekday: scan window
  True, market open False in all three.
- Direction (not a fix): Either hold pending sells the same way pending buys
  are held, or stop logging that it was queued.

### F-03-09: Orders sent while the exchange is closed are recorded as if they had filled
- Severity: High
- Confidence: Confirmed for the bot's side; Alpaca's handling of such orders was not exercised
- Where: `stock_agent/scheduler.py:297-302`, `stock_agent/scheduler.py:725-729`, `stock_agent/scheduler.py:1022-1026`, `stock_agent/scheduler.py:1593`, `stock_agent/scheduler.py:1679-1680`, `stock_agent/scheduler.py:1328-1346`
- What happens: Several paths send orders when the exchange is closed, and
  the ledger books each one immediately at the last quote.
- Evidence:
  - Holidays and early closes: `_is_market_open` is `weekday < 5` and
    `09:30 <= time <= 16:00` and nothing else. Evaluated for Thursday 26
    November 2026 10:00 (Thanksgiving), Friday 27 November 2026 14:00 (after
    the 13:00 close) and Friday 25 December 2026 10:00: True each time.
    Signals queued by the Wednesday midweek cycle are sent on the first such
    tick (`192-197`).
  - Sells out of hours: `_re_analyze_existing_positions` calls `_execute_sell`
    from the Sunday and Wednesday evening cycles (`1022-1026`), and the
    earnings scan does so between 07:00 and 08:00 (`725-729`). Neither checks
    market hours.
  - Event-triggered scan: `now` is captured at line 1593, a full
    re-analysis and deep analysis follow, and `self._is_market_open(now)` is
    tested with that old timestamp at 1679. A scan that starts at 15:45 can
    send buys after the close.
  - In each case `_execute_sell` removes the position and logs a SELL at the
    quote (`1328-1346`), and `_execute_signals` adds the position at the
    signal price (`1280-1303`), as soon as the request is accepted.
- Why it matters: If Alpaca holds such an order for the next open, which is
  its documented handling of a plain day market order, the ledger and the
  broker disagree in the meantime:
  - A sell sent on Sunday evening: the morning brief re-imports the still-held
    position as a placeholder, the real sale happens at 09:30, and the
    ledger then holds a ghost position that the monitoring loop can try to
    sell again (F-03-01).
  - Buys sent on a holiday: the three-second stop is sent with no shares to
    protect (F-03-05); the next morning brief drops the not-yet-filled
    positions from the ledger; they then fill at the open, untracked, until
    the next sync.
  - The sale price and P&L recorded are a stale quote, not the opening
    price actually received.
- How verified: Read each caller; evaluated `_is_market_open` on the three
  dates above. Not verified: that Alpaca queues rather than refuses these
  orders, and how it treats the accompanying stop.
- Direction (not a fix): Ask the broker whether the market is open before
  sending, and record an order as pending until a fill is seen.

### F-03-10: Every sell the broker accepts ends with a "Sell failed" alert and no trade alert
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1348-1350`, `stock_agent/scheduler.py:1364-1370`
- What happens: After the sell order is accepted and the ledger is updated,
  `_execute_sell` reads `self._tipranks_lookup`, which does not exist. The
  error handler then sends "Sell failed for SYMBOL: ... has no attribute
  '_tipranks_lookup'" to Telegram and Discord. The normal sell alerts are
  never sent.
- Evidence: `self._tipranks_lookup.get(position.symbol.upper())`
  (`scheduler.py:1349`) is the only occurrence of that attribute in the
  repository; `__init__` (`73-103`) does not set it. The line was added on
  2026-04-23 (commit `7a8ac46`). The handler at `1364-1370` words every
  exception as a failed sell.
- Why it matters: The message says the opposite of what happened. Someone
  acting on it would sell by hand shares that are already sold. A real
  refusal, by contrast, sends nothing (F-03-13), so the alert appears only
  for sells that worked.
- How verified: Ran `_execute_sell` with a fake executor that accepts the
  order. Recorded calls, in order: `place_sell AAA qty=100`,
  `telegram.send_error_alert("Sell failed for AAA: 'StockAgentScheduler'
  object has no attribute '_tipranks_lookup'")`, `discord.send_error(...)`.
  No `send_trade_alert`. Ledger: position removed, SELL recorded.
- Direction (not a fix): Remove the dependency on a value that is never set,
  and make the handler distinguish "order not sent" from "something failed
  after the order".

### F-03-11: A top-up leaves two stop orders at different prices and does not update the ledger's stop, cash or average cost
- Severity: Medium
- Confidence: Confirmed for the code; Suspected for a broker refusal of the top-up itself
- Where: `stock_agent/scheduler.py:1227-1250`, `stock_agent/scheduler.py:1264`, `stock_agent/scheduler.py:1271-1278`
- What happens: A top-up calls the same `place_buy` with only the additional
  shares and a stop computed from the current signal price. The original
  stop order is left as it was. The ledger adds the shares and changes
  nothing else.
- Evidence: `existing.shares += qty` is the only ledger change
  (`scheduler.py:1275-1278`): `stop_loss` and `entry_price` are untouched and
  cash is not debited (`add_position`, which debits cash, is only called for
  new positions, `1292`).
- Why it matters: The position is covered by two stops at two levels, so a
  fall sells it in two pieces, each of which the bot would discover only at
  the next sync. Ledger cash is overstated until then. Separately, a market
  buy sent while a sell stop for the same symbol is open may be refused by
  Alpaca's protection against trading with yourself; if so, top-ups fail
  with no alert.
- How verified: Ran `_execute_signals` on a ledger holding 40 shares at 4%
  weight with a conviction-10 signal priced 110: broker call `place_buy OLDY
  qty=50 stop=102.3`; ledger afterwards `shares=90 entry_price=100.00
  stop_loss=93.00`, cash identical before and after. The broker refusal is
  not verified.
- Direction (not a fix): After any change in share count there should be one
  known stop covering the new total, replaced deliberately.

### F-03-12: Nothing ever checks that a position still has a stop order, and imported positions never get one
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/executor.py:178-183`, `stock_agent/portfolio.py:193-206`, `stock_agent/models.py:96`, `stock_agent/risk_manager.py:205-216`
- What happens: A stop order is created once, at purchase. The bot never
  looks at open orders afterwards, so it cannot notice a stop that was
  refused, cancelled by hand, removed by a corporate action, or aged out by
  the broker. A position that enters the ledger through reconciliation gets
  a `stop_loss` number written into the ledger and no order at Alpaca.
- Evidence: `get_open_orders` has no callers. `Position.stop_loss` is written
  in two places (`scheduler.py:1287`, `portfolio.py:202`) and read nowhere in
  `stock_agent/`; the monitoring loop recomputes its stop from the entry
  price instead (`risk_manager.py:215-216`). The import writes
  `stop_loss=round(avg_entry * 0.95, 2)` (`portfolio.py:202`), 5% and so
  outside the 7-12% range on record.
- Why it matters: A position without a broker stop is protected only by a
  check every 30 minutes, in market hours, that depends on an FMP quote
  arriving. The ledger gives no way to tell which positions are in that
  state.
- How verified: Search for every read of `stop_loss` and every caller of
  `get_open_orders`; read the import branch; saw the 0.95 value in the sync
  demonstration (average entry 50 gave 47.5).
- Direction (not a fix): Make "does each position have exactly one live stop
  for the right quantity" a routine check with an alert.

### F-03-13: Broker refusals are written to the log only; nothing reaches Telegram or Discord
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/executor.py:106-108`, `stock_agent/executor.py:131-132`, `stock_agent/executor.py:158-159`, `stock_agent/scheduler.py:1265-1267`, `stock_agent/scheduler.py:1324-1326`
- What happens: A refused buy, a refused sell and a refused stop each
  produce `logger.error` or `logger.warning` lines and no alert. A trade
  blocked by the bot's own risk check does post to Discord
  (`scheduler.py:1216-1219`); a trade blocked by the broker does not.
- Evidence: The lines cited above contain only logging calls followed by
  `return` or `continue`.
- Why it matters: Josh cannot tell from his phone that a planned trade did
  not happen, that an exit was refused, or that a position has no stop. It
  is the reason F-03-02 and F-03-05 would go unnoticed.
- How verified: In the fake-object runs with `place_buy` or `place_sell`
  returning `None`, the list of alert calls was empty.
- Direction (not a fix): A refused or uncertain order is an event the owner
  should see.

### F-03-14: `STOCK_AGENT_MODE` changes nothing; paper or live is decided only by `ALPACA_BASE_URL`
- Severity: Medium (matters only once real money is used)
- Confidence: Confirmed
- Where: `stock_agent/config.py:9`, `stock_agent/config.py:44`, `stock_agent/scheduler.py:111-117`, `stock_agent/executor.py:31-32`
- What happens: `Config.MODE` is read in three places, all of them startup
  messages. Orders go to whatever `ALPACA_BASE_URL` says.
- Evidence: Search for `.MODE` in `stock_agent/` returns only
  `scheduler.py:111`, `115`, `117`. `Executor._url` builds every request
  from `ALPACA_BASE_URL` (`executor.py:31-32`).
- Why it matters: With a live URL and the mode left at its default, the bot
  would announce "started in PAPER mode" while trading real money, and the
  reverse. The README's "live mode flips a single config flag" is not what
  the code does. The default URL is the paper endpoint, which is correct.
- How verified: Search and reading.
- Direction (not a fix): One setting should decide the endpoint, and the
  startup message should report the endpoint actually in use.

### F-03-15: Order types and time-in-force in use
- Severity: Note
- Confidence: Confirmed
- Where: `stock_agent/executor.py:95-101`, `stock_agent/executor.py:117-124`, `stock_agent/executor.py:145-151`
- What happens: Three order shapes exist: market buy / `day`, stop (market)
  sell / `gtc`, market sell / `day`. There are no limit orders, no
  stop-limit orders, no extended-hours flag and no fractional or
  dollar-amount orders.
- Evidence: The three request bodies cited.
- Why it matters:
  - Buys from the queue go out in the first minute after 09:30, the least
    orderly minute of the day, with no price limit.
  - A stop (market) order that is gapped through sells at the opening price,
    however far below the stop that is. Broker stops act in regular hours
    only, so overnight moves are not covered until the open.
  - Out-of-hours sells are plain day orders and take the opening price.
  - The universe is restricted to large companies, which limits the cost of
    all of this.
  - Paper fills are simulated; the three-second assumption in F-03-05 is
    weaker on a live account, where opening auctions, halts and partial
    fills are real.
- How verified: Read the request bodies; ran `place_buy` against a scripted
  fake and printed them.
- Direction (not a fix): Context for the order-handling decisions in the
  plan.

### F-03-16: Unused broker functions and comments that describe behaviour the code does not have
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/executor.py:163-188`, `stock_agent/executor.py:83-89`, `stock_agent/scheduler.py:149-154`, `stock_agent/scheduler.py:1046`
- What happens: `cancel_order`, `get_order`, `get_open_orders` and
  `cancel_all_orders` are never called. The first-run block in `_tick` can
  never find pending signals, because the list is created empty in
  `__init__` and that block runs on the first tick. Three comments describe
  fill confirmation, bracket orders and a sell queue that do not exist.
- Evidence: Caller search; `scheduler.py:95` and `149-154`.
- Why it matters: The comments make the order handling look safer than it
  is. Being unused, `cancel_all_orders` cannot remove stops by accident,
  which answers that lead.
- How verified: Search for each name across the repository.
- Direction (not a fix): Bring comments in line with behaviour when the
  order path is reworked.

## Checked and fine

- Credentials are not logged. `_request` logs method, path, status and the
  first 500 characters of the reply body, never headers (`executor.py:55-66`).
- The default endpoint is Alpaca paper (`config.py:9`); nothing in the code
  switches it.
- The sync guard skips when both broker calls fail (`scheduler.py:818-820`),
  and the morning brief skips on an empty list (`scheduler.py:1732`), so a
  double failure does not wipe the ledger.
- A failed account call with a good positions call is handled: positions
  sync, cash is left alone (`portfolio.py:215-219`).
- Option symbols are kept out of the equity ledger and dotted tickers are
  kept in: `_is_equity` gave True for a five-character dotted ticker and
  False for an OCC option symbol and a crypto pair (`portfolio.py:131-134`).
- Duplicate ledger rows for one symbol collapse to one on sync
  (`portfolio.py:148`).
- Negative cash (margin) is carried into the ledger as is (`portfolio.py:217`).
- Zero or negative quantities never reach Alpaca (`executor.py:90-92`,
  `141-143`); quantities are whole shares.
- The stop price is rounded to cents (`executor.py:122`), valid for stocks at
  or above one dollar.
- On ordinary weekdays buys are held until 09:30 rather than sent pre-market
  (`scheduler.py:192-197`, `433-438`).
- Two queued signals for the same symbol do not double the position when the
  first buy was recorded: the second meets "Already holding"
  (`risk_manager.py:58-62`) or tops up to target only.
- Order paths cannot overlap: `_tick` awaits each job in turn in a single
  task (`scheduler.py:127-142`).
- No code path cancels all orders, at shutdown or anywhere else
  (`scheduler.py:1972-1990`).
- Partial sells do not exist, so "stop after a partial sell" does not arise.

## Could not verify

No droplet snapshot was available. With one, these can be settled from the
files `scripts/vps_snapshot.sh` produces:

- **Orphan, missing, duplicate or mis-sized stop orders in practice.** Needs
  `alpaca/summary.md` (its "Stop-order cross-check" section),
  `alpaca/orders.json` and `alpaca/positions.json`.
- **Whether a short position exists or has existed (F-03-01).** Needs
  `alpaca/positions.json` (any negative `qty`), `alpaca/account.json`
  (`shorting_enabled`, `short_market_value`), and `alpaca/orders.json`
  searched for a filled stop sell followed by a filled market sell of the
  same symbol and quantity.
- **Whether the bot's sells are refused while a stop rests (F-03-02) and
  whether top-up buys are refused (F-03-11).** Needs `logs/journal.log`
  searched for `Alpaca POST /v2/orders failed`, `Sell order failed`,
  `BUY order FAILED` and `Stop-loss order FAILED`, with the status code and
  message of each.
- **How often the ledger has been wiped or drifted (F-03-03, F-03-04).** Needs
  `logs/journal.log` (runs of `not found at Alpaca, dropping`) and
  `state/portfolio.json` (how many positions carry the "Imported from Alpaca"
  placeholder).
- **How far recorded prices are from real fills (F-03-07).** Needs
  `alpaca/activities.json` compared with the trade history in
  `state/portfolio.json`.
- **Which endpoint the droplet uses (F-03-14).** Needs the snapshot's `.env`
  summary lines for `ALPACA_BASE_URL` and `STOCK_AGENT_MODE`.

Broker behaviour the findings rely on and the audit could not exercise,
because no Alpaca calls are allowed. Each needs Alpaca's documentation read
by the verifier, or matching lines in the snapshot logs:

- A sell with no position is accepted as a short sale on a shorting-enabled
  margin account (F-03-01).
- Shares held for an open stop order are unavailable to a second sell
  (F-03-02).
- A plain day market order sent while the exchange is closed is held for the
  next open (F-03-09).
- What happens to a sell stop sent before the buy has filled, or priced above
  the current market (F-03-05, F-03-07).
- Whether a market buy is refused while a sell stop for the same symbol is
  open (F-03-11).
- Whether Alpaca cancels GTC orders after a fixed age (F-03-12).

## Questions for Josh

1. Should this account ever hold a short stock position? If not, do you want
   short selling switched off in the Alpaca account settings as a backstop?
2. Who should own the stop-loss: the resting order at Alpaca, the bot's
   30-minute check, or the Alpaca order with the bot only watching it?
3. When research says "sell" while the market is closed, should the bot sell
   at the next open whatever the price, or look again at the open first?
4. If a stock has moved between the analysis and the open, how big a move
   should cancel the planned buy (for example 3%)?
5. After a top-up, do you want one stop for the whole position or a separate
   stop for each purchase?
6. If a position's broker stop is missing, is the 30-minute check acceptable
   cover, or should that alert you immediately?
7. Have you seen a negative share count in the Alpaca dashboard, or "Sell
   failed for ... _tipranks_lookup" messages in Telegram or Discord?
8. Do you want the service stopped over Thanksgiving (Thursday 26 November
   2026, and the 13:00 close on Friday 27 November), given F-03-09?
9. Can you run `scripts/vps_snapshot.sh` on the droplet and upload the
   archive, so the items under "Could not verify" can be closed?

## Handoffs

- 01: `_is_market_open` has no holiday or early-close calendar (`scheduler.py:297-302`); `_pending_signals` lives in memory and is lost on restart (`scheduler.py:95`); shutdown only sets a flag and the unit file has no stop timeout (`__main__.py:16-17`, `deploy/stock-agent.service`).
- 01: The event-triggered scan tests market hours with a timestamp taken before a long analysis (`scheduler.py:1593`, `1679`, `1686`).
- 02: `Thesis` has no `beta` field, so `getattr(thesis, "beta", None)` is always `None` and every stop is the 7% floor; the 7-12% volatility adjustment never applies (`models.py:58-72`, `scheduler.py:1258`, `risk_manager.py:215`).
- 02: The cash check requires enough for one share only, and top-ups have no cash check (`risk_manager.py:120`, `scheduler.py:1227-1250`); risk checks use the stale signal price.
- 02: The PDT check counts ledger trades, which never include broker stop-outs (`risk_manager.py:166-198`).
- 04: Equity sells and stops do not check for covered calls written against the shares, and the options scan takes share counts from the unreconciled ledger (`scheduler.py:1406-1408`).
- 05: Imported positions carry an empty placeholder thesis that is then passed to re-analysis as the "previous thesis" (`portfolio.py:182-192`, `scheduler.py:1013-1015`).
- 06: The software stop depends on one FMP quote per symbol; a missing quote silently leaves the old price in place (`data_feed.py:243-262`, `portfolio.py:107-118`).
- 07: Top-ups do not debit cash or re-average the entry price; sell P&L uses a quote, not the fill; broker stop-outs never enter trade history; imports set `entry_date` to the import time (`scheduler.py:1271-1278`, `1320-1330`, `portfolio.py:149-156`, `197`).
- 08: False "Sell failed" alert (F-03-10) and no alert on broker refusals (F-03-13); the weekly report's "executed" list is always empty because execution is deferred to the open (`scheduler.py:433-438`, `451`).
- 09: `STOCK_AGENT_MODE` is cosmetic (F-03-14); `config.py:194-195` commits default values for `TIPRANKS_API_KEY` and `TIPRANKS_API_TOKEN` in a public repository; Alpaca error bodies are written to the journal (`executor.py:56-62`).
- 10: Four unused executor methods and three misleading comments (F-03-16).
