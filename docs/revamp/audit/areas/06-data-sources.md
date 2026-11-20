# 06. Data sources

Output: `docs/revamp/audit/findings/06-data-sources.md`

## Scope

`stock_agent/data_feed.py` (FMP, Finnhub, SEC, Alpaca market data),
`tipranks_client.py`, `news_feed.py`, `sentiment.py`, `analyst_trends.py`,
`macro_monitor.py` (its data fetching), and `options_data.py` (its HTTP
layer only).

## Why it matters

Prices drive stop-losses and sizing; fundamentals drive the research. Wrong,
stale, or silently missing data produces confident wrong decisions. Several
of these providers change or retire endpoints, and the bot has run largely
unattended.

## Questions to answer

1. Inventory every external endpoint: provider, URL path, what it is used
   for, how often it is called, and what the bot does when it fails. A table
   is the right format.
2. For each provider, how are errors handled: HTTP status, timeouts, rate
   limits, empty lists, error objects returned with status 200? Can callers
   tell "no data" from "the request failed"?
3. Which price is used for what (last trade, quote, previous close, delayed)?
   How stale can the price behind a stop-loss check or a position size be?
   What happens outside market hours and for a halted symbol?
4. What happens when a quote is missing or zero for a held position? Does the
   position get skipped, valued at zero, or valued at its last known price?
5. Are any calls blocking (synchronous HTTP) inside the async loop, and for
   how long could they stall it?
6. Are there retries, backoff, caching, or request budgets? How many calls
   does a weekly run make to each provider compared with plan limits?
7. Are any endpoints deprecated, legacy, or undocumented? What is the bot's
   behaviour if one starts returning errors tomorrow?
8. Where are API keys placed in requests (headers or URL query string), and
   could they end up in logs through request logging or error messages?
9. How are symbols normalised across providers (class shares such as BRK.B,
   ADRs)? How are sectors assigned, and how often does the fallback map get
   used?

## Leads to check (unverified)

- FMP calls pass the key as a query parameter, and Finnhub passes a token the
  same way. Check whether the HTTP client logs full URLs at the configured log
  level.
- Check which FMP API version the paths use and whether those paths are
  current.
- `tipranks_client.py` appears to use a synchronous HTTP library. Check
  whether it is called from async code without being moved off the loop.
- `tipranks_client.py` and `config.py` contain default credential values in
  source. Report the location and variable names only, never the values.
- Check whether the SEC requests send the contact header the SEC requires.

## Out of scope

Prompt content (05). Order endpoints (03, 04).
