# 03. Execution and broker reconciliation

Output: `docs/revamp/audit/findings/03-execution-and-broker-reconciliation.md`

## Scope

`stock_agent/executor.py`, `Portfolio.sync_from_alpaca` in
`stock_agent/portfolio.py`, and the scheduler functions that call them
(`_execute_signals`, `_execute_sell`, `_sync_portfolio_from_alpaca`,
`_run_market_monitoring`). Equities only; options orders are area 04.

## Why it matters

This is where decisions become orders. The design decision on record is that
Alpaca is the source of truth and the local ledger is reconciled against it
before risk checks. The audit should establish whether that is true on every
path, and what happens when an order does not go as expected.

## Questions to answer

1. Trace a buy from signal to resting stop order, step by step. After each
   step, what does the bot know for certain and what does it assume? Where is
   the fill price and filled quantity taken from?
2. What happens when: the buy is rejected; it fills partially; it has not
   filled when the stop is sent; the stop order is rejected; the process is
   restarted between the two orders; the market is closed?
3. Trace a sell the same way. Is the resting stop order cancelled first? Can
   the bot end up with a sell and a stop both live for the same shares, or
   with a stop order left behind for a position it no longer holds?
4. After a top-up, is the old stop replaced with one covering the new share
   count? After a partial sell?
5. Is there any idempotency: client order IDs, a record of orders in flight,
   or a check for an existing open order before sending another? Can a retry
   or a repeated job submit the same order twice?
6. When does reconciliation with Alpaca run relative to risk checks and
   orders? What does it do with positions the broker has and the ledger does
   not, and the reverse? What local information is lost or invented during a
   sync (thesis, entry date, stop, sector)?
7. How are Alpaca errors handled: HTTP errors, timeouts, rate limits, an empty
   response? Note what `_request` returns on failure and whether callers can
   tell "no positions" from "the request failed".
8. Which order types and time-in-force values are used, and are they sensible
   for the situations they are used in (pre-market, a gap down through a
   stop, low liquidity)?
9. Is there anything that would behave differently on a live account than on
   paper?

## Leads to check (unverified)

- `place_buy` sends a market buy, waits a fixed three seconds, then sends a
  separate stop for the full requested quantity. Check every way that can go
  wrong.
- `get_positions` returns an empty list on failure. Check whether a failed
  call can make the sync think the account is empty.
- `cancel_all_orders` exists. Check who calls it and when.
- Check whether stop-losses are enforced twice (resting broker stop and the
  monitoring loop) and whether the two can both act on the same position.

## Could need a snapshot

Open orders and order history from the droplet snapshot show whether orphan or
duplicate stop orders exist in practice. Without it, report what the code
allows and list the check under "Could not verify".

## Out of scope

Whether a trade should be allowed (02). Local file format (07).
