# Live evidence: stop orders, Telegram, and who placed the orders

Collected: 2026-11-21    Source: the same read-only server snapshot of
20 Nov 2026 as the other evidence files    Mode: paper

## What this file is

A follow-up to the findings the audit added after reading the first two
evidence files (F-11-14 and F-11-15 in `findings/11-live-performance.md`),
and to decisions 21, 23, and 26 in `AUDIT.md`. Same rules as the other
evidence files: observations, percentages and counts only.

## 1. Correction: Telegram is switched off by its settings (F-11-14)

`2026-11-21-answers-to-audit-unknowns.md`, section 5, says the Telegram
settings "are present in the file". That wording was misleading.

- Both Telegram lines exist in the server's settings file, and both values
  are empty.
- `stock_agent/telegram_alerts.py:31` skips sending when either value is
  empty and logs "Telegram not configured". So the log line is the code
  working as written. There is nothing left to explain in F-11-14.
- No message has gone to Telegram in the two-month log window. Discord is
  the only channel that delivers anything (decision 26).
- The Telegram value in `.env.example` is therefore not in use on the
  server. It still needs revoking if that bot exists.

## 2. Why only some buys got a stop order (F-11-15, F-03-05)

The code that sends a stop order three seconds after each buy arrived on
6 Apr 2026 (commit `bfe42f0`; today at `stock_agent/executor.py:113-131`).
Since then 30 stock buys placed through the API key have filled.

| Buy filled within 3 seconds | Buys | With a stop order at the broker |
| --- | --- | --- |
| Yes | 11 | 11 |
| No (52 seconds to about 10 minutes) | 19 | 0 |

- The 11 fast fills were all on 23 Apr, when buys were sent in the middle
  of the session. Each has a stop order placed about three seconds later.
- The 19 slow fills were all sent in the first two minutes after the open,
  on nine different days. None has a stop order.
- The split is exact. The likeliest reading is that a stop sent before the
  buy has filled is refused by the broker, and a refused order leaves no
  record there. This is F-03-05 seen in production: every buy made at the
  open, which is when the bot normally buys, has gone without a broker stop.
- Not confirmed from the log. The log window has no buys in it, so the
  "Stop-loss order FAILED" line cannot be counted.

## 3. The other eight stop orders (F-11-15; decision 21)

- Eight stop orders were sent together at 04:01 New York time on 27 Apr,
  for positions that were already held. No buy came before them.
- They came through the API key, like every other stop order. None was
  placed on the broker's website.
- Nothing in the code places a stop except straight after a buy, on
  `master` today or at the commit that was live on 27 Apr. So these eight
  were sent by something outside the bot's normal run, most likely a
  one-off script. Josh may remember.

## 4. Who placed the orders (decision 21)

Of 174 orders in the broker's history:

- 170 came through the account's API key, which the bot uses.
- 3 were placed by the broker itself to close both legs of an option spread
  at 15:30 New York time on its expiry day (24 Apr, 21 Aug, and 11 Sep).
  Since nothing in the bot closes an option (F-04-02), the broker stepped in
  on those three. Other positions expired worthless or ended in exercise
  or assignment.
- 1 did not come through the API key: a buy of a fractional number of
  shares on 16 Apr. It is the source of the fractional remainder that the
  bot's ledger does not hold.

## 5. Still unknown

- What the broker answers to a stop order sent before the buy has filled.
  One saved response would settle section 2.
- What sent the eight stop orders on 27 Apr.
