# 07. State and accounting

Output: `docs/revamp/audit/findings/07-state-and-accounting.md`

## Scope

`stock_agent/portfolio.py`, `stock_agent/models.py`, the persistence and
P&L parts of `options_portfolio.py` and `options_models.py`, macro state
files written by `macro_monitor.py`, and every place the scheduler reads or
writes `self.portfolio.state`.

## Why it matters

The bot's memory is a handful of JSON files in `data/stock_agent/` on a
1 GB server. If a file is corrupted, grows without limit, or disagrees with
the broker, the bot either stops or acts on a false picture. The performance
numbers Josh sees come from here too.

## Questions to answer

1. List every file the bot writes: path, what is in it, when it is written,
   and roughly how it grows over a year.
2. Is every write atomic (temp file then rename)? What happens on a crash or
   a full disk mid-write? What happens on startup if a file is missing,
   empty, or invalid JSON: does the bot stop, start fresh, or overwrite the
   evidence?
3. Is there a schema version or any migration path? What happens when a model
   gains or loses a field and an old file is loaded?
4. Where can local state and broker state disagree, for how long, and which
   one wins in each case? After a sync, what local-only information survives?
5. How are cash, portfolio value, exposure, realised P&L, unrealised P&L, win
   rate, and the equity curve calculated? Check each formula. Which of them
   come from the broker and which are the bot's own arithmetic?
6. Is `starting_capital` right for computing total return if money is added
   or removed? Do the stats include options P&L, dividends, and fees?
7. How is a trade's P&L computed on a partial sell or after a top-up changed
   the average price?
8. Are times stored with time zones, consistently? Are there naive and aware
   datetimes mixed in comparisons?
9. Is any state held only in memory that should survive a restart?
10. Are there backups, and is there any way to rebuild state from the broker
    alone?

## Leads to check (unverified)

- `DailySummary` embeds full position and signal objects, and each position
  embeds its thesis. Estimate how `portfolio.json` grows and what loading and
  rewriting it on every save costs.
- `_save` is called from many small setters. Check how often the whole file
  is rewritten per cycle.
- Check `_load` error handling: what does a corrupt file lead to?
- `shares` is an integer in the models. Check what happens with fractional
  quantities from the broker.
- `trade_history` is an unbounded list in the same file.

## Could need a snapshot

File sizes and the shape of the real state files (not their contents) from
the droplet snapshot.

## Out of scope

The sync call sequence and order handling (03).
