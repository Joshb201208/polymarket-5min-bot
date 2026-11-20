# 02. Risk and sizing

Output: `docs/revamp/audit/findings/02-risk-and-sizing.md`

## Scope

`stock_agent/risk_manager.py`, the risk settings in `stock_agent/config.py`,
and **every place in `stock_agent/scheduler.py` that decides how much to buy
or whether a buy is allowed** (start at `_execute_signals`, `_execute_sell`,
`_run_market_monitoring`, and anything that calls `RiskManager`). Also the
macro regime's effect on limits in `stock_agent/macro_monitor.py`
(`get_effective_min_conviction` and similar).

## Why it matters

These are the rules that stop the bot from putting too much money in one
place. A past incident let one symbol reach about three quarters of the
portfolio because the local view of positions was stale. The limits only
protect the account if every buy path goes through them with fresh numbers.

## Questions to answer

1. List every limit the bot is supposed to respect (per-symbol, per-sector,
   per-cluster, total exposure, position count, minimum conviction, cash,
   pattern-day-trader). For each: where is it defined, where is it enforced,
   and is there any buy path that skips it?
2. For each limit, does the check use the portfolio **before** the proposed
   trade or **after** it? Can a single order take a position, sector, or
   cluster over its cap?
3. Walk the sizing maths from a conviction score to a share count with real
   numbers. Do top-ups to an existing position use the same maths, and what
   stops a top-up from overshooting the target or the hard cap?
4. What portfolio value does sizing use, where does it come from, and how
   fresh is it at the moment of the order? Does it include options positions
   and their collateral?
5. How does the stop-loss price get computed, stored, and enforced? Is the
   stop the bot checks in its loop the same number as the stop order resting
   at the broker? What happens to a stop after a top-up changes the share
   count and average price?
6. Is the pattern-day-trader check actually called before orders? Is its
   window correct?
7. What do the limits do with missing data: unknown sector, a price of zero,
   a symbol in no cluster, a conviction score outside 7 to 10?
8. Do comments, docstrings, and the README agree with the numbers in code?
   List every mismatch.

## Leads to check (unverified)

- `get_stop_loss_price`: compare the docstring's stated range with the
  configured minimum and maximum.
- `MAX_TOTAL_EXPOSURE`: compare the default value with the comment beside it,
  and work out whether this limit can ever block anything at its default.
- The sector check in `can_open_position` has a two-step condition with a
  "soft block" comment. Work out what it really blocks, with numbers.
- The cluster and sector checks: do they count the proposed purchase?
- Existing positions return early as `ADD_TO_EXISTING`. Check which later
  checks (sector, cluster, exposure, cash) top-ups therefore skip.
- The fallback when a conviction score is not in `CONVICTION_SIZE_MAP`.
- Whether `MAX_POSITION_PCT` and the other environment overrides are validated
  at all (for example a typo setting a limit to 15 instead of 0.15).

## Out of scope

How orders are sent (03). Options limits (04).
