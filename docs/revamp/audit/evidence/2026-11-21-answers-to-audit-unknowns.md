# Live evidence: answers to the audit's open questions

Collected: 2026-11-21    Source: the same read-only server snapshot of
20 Nov 2026 as `2026-11-20-live-performance.md`    Mode: paper

## What this file is

The full audit (`AUDIT.md`, written 20 Nov 2026) ran without a snapshot and
listed what it could not know under "Unknowns", "Unclear after verification",
and "Decisions for Josh". This file answers those from the snapshot and the
broker's order history. Same rules as the other evidence file: observations,
not diagnoses; percentages and counts only; no account values or order IDs.
Finding IDs refer to `AUDIT.md`.

## 1. Does a short position exist? (F-03-01, F-02-01; decisions 3, 6, 21)

Yes. Two short stock positions are open at the broker, and neither is in the
bot's ledger.

- Both were opened the way F-03-01 describes. The broker's stop order filled
  just after the open. The bot's own check then sold the same number of
  shares again: 22 minutes later in one case (27 May) and about two and a
  half hours later in the other (1 May).
- The broker accepted the second sale as a short sale. It did not refuse it.
- This settles the puzzle at the end of section 3 of `AUDIT.md`: on those
  occasions the broker's stop had already filled.
- F-03-02 (the bot's sell while a stop still rests) is not settled. In the
  order history no market sell filled for a symbol while its stop order was
  still open, and the log does not reach back to the months when stop orders
  existed.

## 2. Broker account settings (unknown: approval level, margin, shorting)

- Margin account with a 4x buying-power multiplier.
- Short selling is enabled.
- Options approval level 3.
- Nothing at the broker stops the bot from borrowing or going short. Every
  such limit has to come from the bot, or from changing these settings.

## 3. Orders sent while the market was closed (F-01-03; decisions 2, 12)

- On Good Friday, 3 Apr 2026, a market holiday, the bot sent two market buys
  at 09:30 New York time. The broker cancelled both later that day and
  neither filled. The bot's ledger records both as bought at that moment.
  This is F-01-03 observed in production.
- Four market sells were sent before the open (between 04:00 and 07:01 New
  York time). The broker held them and filled each within five minutes of
  the open.
- In the two-month log window every research cycle ended with "queued 0
  signals" (18 lines), so no buy has been waiting for an open in that time.

## 4. What the order history contains (unknown: orphan stops, duplicates)

- 174 orders: 166 filled and 8 cancelled. None is recorded as rejected or
  expired. An order refused at submission leaves no record at the broker, so
  this does not show that no order was ever refused.
- All 91 option orders are market orders, good for the day (F-04-06).
- 19 stop orders, all good-till-cancelled, placed on two days in April:
  13 filled and 6 were cancelled. Five of the six were cancelled just after
  the close, 88 to 90 days after they were placed. The audit found that the
  bot never cancels a stop order, so the likeliest cause is an expiry at the
  broker. Either way, those positions lost their stop without any message.
- No stop order is open today. There are no orphan stops.
- Duplicate orders other than the two double sales were not checked.

## 5. Server settings (unknown: exposure and options settings)

- `MAX_TOTAL_EXPOSURE_PCT` is set to `1.00`.
- `OPTIONS_ENABLED` is not set. `ALPACA_BASE_URL` is not set, so the default
  paper address in `stock_agent/config.py` is used, and the log shows
  requests going to the paper address.
- `STOCK_AGENT_MODE` is `PAPER`.
- The Telegram settings are present in the file, yet the log reports
  "Telegram not configured" (77 lines). Cause not traced.

## 6. Leftovers from the earlier bot (F-09-09, marked "Unclear")

- A web server from the earlier bot is still installed, enabled at boot, and
  running, with one site configured. The page it serves returns an error.
- No other service from the earlier bot is enabled. The only application
  service is the stock agent.
- The settings file still holds the earlier bot's settings (see the other
  evidence file).

## 7. Secrets in the service log (F-08-03)

- Request addresses that contain API keys are written to the service log:
  about 25,700 lines for the FMP key and about 1,400 for the Finnhub key in
  two months.
- No Telegram request address appears in the log window.

## 8. Secrets in git history (unknown: history before 4 Apr 2026)

A pattern search over every branch's history (251 commits; the first is
dated 19 Mar 2026) looked for the usual shapes of wallet keys, private-key blocks,
and common API-key formats.

- No wallet-key-shaped value and no private-key block was found.
- A Telegram-token-shaped value also appears in the history of
  `agents/common/config.py`, a file that has since been removed.
- This is a search by shape. It can miss a secret with an unusual format, so
  it does not replace rotating everything the earlier bot used.

## 9. Still unknown

- Whether the exposed credentials still work. Nobody has tested them.
- What FMP returns for each endpoint, and whether a price can be zero or
  null (F-06-08, F-06-12). The log holds status codes only.
- How the broker answers a sell while a stop order rests (F-03-02).
- API cost per month.
