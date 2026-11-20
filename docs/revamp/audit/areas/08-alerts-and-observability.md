# 08. Alerts and observability

Output: `docs/revamp/audit/findings/08-alerts-and-observability.md`

## Scope

`stock_agent/telegram_alerts.py`, `stock_agent/discord_alerts.py`, logging
setup in `__main__.py` and across the package, and every `except` block in
`stock_agent/` (there are roughly a hundred broad ones).

## Why it matters

Josh is not watching a terminal. Telegram and Discord are how he knows what
the bot did, and the absence of messages is how he would notice it is down.
The bot has already been down for a stretch without anyone being told.

## Questions to answer

1. Inventory every message the bot sends: channel, trigger, and content. Which
   are for Josh to act on and which are noise?
2. If the process dies, hangs, or gets stuck in a restart loop, what tells
   Josh? Is there any heartbeat or any outside check? How long could the bot
   be down before anything notices?
3. If an alert fails to send, is it retried, queued, or lost? Can a failing
   alert break the trading path that triggered it? Can alert sending slow the
   trading path down?
4. Where do logs go on the server, how long are they kept, and how big can
   they get? Is the configured `LOG_LEVEL` actually applied?
5. Can secrets reach logs or messages: API keys in logged URLs, tokens in
   exception text, full error bodies forwarded to Discord or Telegram?
6. Go through the broad `except` blocks. Sort them into: fine; hides a
   failure that should stop the action; hides a failure that should alert.
   Report the ones around orders, stops, sync, and state saving individually,
   and the rest as counts by file.
7. Can an error loop spam alerts every minute? Is there any de-duplication or
   rate limit?
8. Are message length limits, formatting escapes, and Discord and Telegram
   rate limits handled?
9. From the alerts alone, could Josh reconstruct what the bot did in a day and
   why? What is missing?

## Leads to check (unverified)

- The Telegram API puts the bot token in the request URL. Check whether the
  HTTP client's request logging writes that URL to the journal at INFO level.
- `__main__.py` sets the log level directly. Compare with `Config.LOG_LEVEL`.
- The main loop sends an error alert on every failed tick. Check what a
  persistent failure does.
- Check whether "stop-loss order failed, position is unprotected" in the
  executor reaches Josh or only the log.

## Could need a snapshot

Redacted recent journal logs from the droplet snapshot show real error
frequency and whether URLs with keys are being logged.

## Out of scope

Server-level monitoring setup (09), beyond stating what the bot itself emits.
