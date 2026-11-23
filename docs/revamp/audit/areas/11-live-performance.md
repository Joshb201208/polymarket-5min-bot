# 11. Live performance

Output: `docs/revamp/audit/findings/11-live-performance.md`

## Scope

What the bot has actually done since it started: its trades, its results, and
its behaviour in production, compared with what the code and the strategy say
it should do.

**This area needs facts from the running server.** There are two sources:

1. **Evidence on file.** Read every file in `docs/revamp/audit/evidence/`
   first. Those files hold measurements already taken from a droplet snapshot
   and the broker's records, written to this repository's privacy rules. Use
   them to answer the questions below, cite the file and section, and mark
   each such answer "Observed (evidence on file)". You cannot re-derive them,
   so do not restate them with more precision than the file gives.
2. **A droplet snapshot uploaded to the session.** Josh produces it by running
   `scripts/vps_snapshot.sh` on the server. If one is available, use it to
   check and extend the evidence on file, and say where the two disagree.

If neither exists, do the code-only parts below, list exactly what is missing
under "Could not verify", and stop. Do not estimate results and do not use any
other source for account data.

For every observation in the evidence files, say which part of the code
explains it (with a handoff to the owning area) or that nothing in the code
explains it.

## Why it matters

The other ten areas say what the code could do. This one says what it did.
A strategy that has been losing for reasons the code explains is a different
revamp from a strategy that works but runs on fragile plumbing.

## Privacy

This repository is public. In the findings file, use percentages, counts, and
ratios. Do not write account numbers, dollar balances, order IDs, or a
position-by-position listing. Symbols may be named where a specific trade
illustrates a finding.

## Questions to answer

With a snapshot:

1. Uptime: from service status, the auto-update log, and the journal, when was
   the bot running and when was it not? How many restarts, and why?
2. Results: total return and drawdown from the broker's portfolio history,
   set against simply holding a broad index fund over the same dates.
3. Trades: how many, win rate, average win against average loss, holding
   periods, and how each position was closed (stop, thesis change, other).
4. Stops: how many exits were stop-outs, how close to the stop they filled,
   and what the price did afterwards. The stop floor was widened once already
   because of whipsaws; did it help?
5. Conviction: did higher-conviction positions do better than lower ones? If
   the sample is too small to tell, say so.
6. Limits in practice: was any per-symbol, sector, cluster, or exposure limit
   exceeded at any point? Is the account concentrated today?
7. Orders: are there orphan, duplicate, or rejected orders in the history?
   Open stop orders with no matching position, or positions with no stop?
8. Options: what was opened, how it closed, whether anything was assigned or
   expired unmanaged, and the result separate from equities.
9. Ledger against broker: does the local state agree with the broker's
   positions and cash right now?
10. Errors: the most frequent errors and warnings in the logs, by count.
11. Cost: a rough monthly figure for API usage from call counts in the logs.

Without a snapshot (code only):

- What performance statistics does the bot compute for itself, and are the
  formulas sound (cross-reference area 07)?
- What would be needed to answer questions 1 to 11, item by item.

## Out of scope

Explaining why the code allows what happened; hand that to the relevant area
with a one-line note.
