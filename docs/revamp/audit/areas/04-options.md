# 04. Options

Output: `docs/revamp/audit/findings/04-options.md`

## Scope

`stock_agent/options_engine.py`, `options_executor.py`,
`options_portfolio.py`, `options_data.py`, `options_models.py`, the
`OPTIONS_*` settings in `config.py`, and the scheduler's `_run_options_scan`
and `_check_options_positions`.

## Why it matters

Options are the part of the bot where a small mistake costs the most:
uncovered short calls, assignment, multi-leg orders that fill on one side, and
positions that expire while the bot is not looking. This module is about
2,700 lines and has had the least scrutiny.

## Questions to answer

1. Which strategies can the bot open (covered calls, cash-secured puts, bull
   call spreads, bear put spreads, anything else)? For each, what are the
   entry rules, and are they the ones in the comments and README?
2. For covered calls: how does the bot confirm it holds at least 100 shares
   per contract at the moment of the order, from the broker and not just the
   local ledger? Can it sell more calls than it has shares for? What happens
   to the cover if the equity side later sells or stops out?
3. For cash-secured puts: how is the cash requirement checked, and against
   which cash figure? Is the cash then treated as reserved by the equity side?
4. For spreads: are they sent as one multi-leg order or as separate legs? What
   happens if one leg fills and the other does not?
5. How are the limits enforced: position count, per-trade size, total options
   exposure, days-to-expiry window, minimum premium, reward-to-risk? What is
   "exposure" measured as (premium, max loss, notional)? Check before or after
   the trade?
6. How are positions closed: profit target, days to expiry, stop? What happens
   at expiry, on assignment, and on early exercise? Does the equity ledger
   learn about shares that arrive or leave through assignment?
7. How are option positions reconciled with the broker, and how are expired
   legs valued? (The decision on record: expired in-the-money legs are valued
   at intrinsic value, not zero.)
8. Where do quotes and greeks come from, how stale can they be, and what
   happens when the chain is empty or wide?
9. Does the equity risk manager know about options exposure at all?

## Leads to check (unverified)

- A search finds no use of the `OPTIONS_MAX_*`, `OPTIONS_MIN_*`,
  `OPTIONS_AUTO_*`, or `OPTIONS_SPREAD_*` settings outside `config.py`, while
  `options_engine.py` and `options_portfolio.py` define their own module-level
  constants, some with different values. Establish which set is live and
  whether the config values do anything.
- Several config comments do not match their values (for example the comment
  beside `OPTIONS_MAX_SINGLE_TRADE_PCT` and beside `OPTIONS_MIN_PREMIUM_PCT`).
  List each mismatch and say which number the code really uses.
- `_round_to_strike` uses a fixed increment. Check what that does for cheap
  and expensive underlyings.
- `close_option_position` has a legacy variant. Check which is live.
- `exercise_option` exists. Check whether anything calls it.
- Check that covered calls can only target out-of-the-money strikes, as the
  decision on record says.
- Check what the options side does when the account lacks the required
  options approval level.

## Out of scope

Equity execution (03). File persistence mechanics (07), beyond noting what the
options ledger stores.
