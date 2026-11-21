# Audit of the stock agent

Audited commit: `941f2ca` (code identical to `master` at `e92e1cd`; the two extra
commits only add the revamp setup). Date: 2026-11-20.

Eleven areas were audited independently. Every Critical and High finding was
then re-checked by a second reviewer whose job was to prove it wrong. No
finding was refuted; three are marked "Unclear" because they depend on facts
only the server or a data provider can supply.

**Updated 2026-11-21 with live evidence.** The audit was first written
without server data. Three evidence files are now in
[`evidence/`](evidence/), built from a read-only server snapshot and the
Alpaca order history: **E1** =
[`2026-11-20-live-performance.md`](evidence/2026-11-20-live-performance.md)
**E2** =
[`2026-11-21-answers-to-audit-unknowns.md`](evidence/2026-11-21-answers-to-audit-unknowns.md)
and **E3** =
[`2026-11-21b-stop-orders-and-telegram.md`](evidence/2026-11-21b-stop-orders-and-telegram.md).
"Seen in production" below means the evidence shows it happened, with the
file and section. No area was re-audited; the code findings are unchanged.
Figures are percentages and counts only.

## 1. Bottom line

The bot is not sound enough to trust with real money, and the live evidence
now shows several of the audit's worst findings have already happened. On
paper it is up 24.0% since launch against 14.7% for QQQ, but all of that came
before late July; since then it has lost 12.6%, twice QQQ's fall. Research
has produced nothing since late July because Perplexity, the price feed,
OpenAI and TipRanks have all been refusing requests, and every daily brief
looked normal. Twice a stop-out was sold a second time, and both resulting
short positions have been open for about six months without the bot knowing;
its own reports overstate the return by about 12 points. The risk limits did
not hold: the largest position was above the 15% cap on 122 of 164 trading
days, because shares delivered by options bypass every limit. No position
has a stop order at the broker today, and the bot's own stop check depends on a
price feed that is partly refused. Credentials for Telegram, TipRanks
and a Perplexity proxy are committed in this public repository and need
rotating. My recommendation is unchanged: revamp in place, safety first, with
options switched off and rebuilt only if Josh still wants them. What should
not wait for the plan: rotating credentials, closing the two short positions,
and deciding whether the bot should keep running with no working research.

## 2. How the bot actually works

One process wakes about once a minute, looks at New York time, and runs
whatever is due. Only one job runs at a time.

```
Sunday 18:00 (and Wednesday 18:00, a full repeat)
  reconcile ledger with Alpaca
  build universe: FMP screen + TipRanks names (~50-150 symbols)
  OpenAI gpt-4.1-nano ranks them from a table of numbers -> top 20
  Perplexity sonar-pro writes a thesis with a 1-10 conviction for each
  bot adds up to +5 for TipRanks, sentiment, insider data
  BUY with adjusted conviction >= 7 (6 in a sell-off) -> queued IN MEMORY
  held positions re-analysed; a SELL reply sells at once (market closed)

Weekdays
  07:00-08:00  earnings-reaction scan (can sell before the open)
  08:00        pre-market scan (can queue buys; its sells are dropped)
  08:15        morning brief, which also reconciles with Alpaca
  09:30        queued buys executed:
                 risk check on the local ledger
                 size from conviction tier and the analysis-time price
                 market buy -> wait 3 seconds -> stop order for full size
                 position written to the ledger as filled at that old price
  every 30 min monitoring: FMP quotes, the bot's own stop check (market
               sell if breached), material-news check (a SELL reply sells),
               macro regime check (a regime change starts a full cycle),
               options ledger reconcile
  once a day   options scan: covered calls, cash-secured puts, spreads
```

Where this differs from what is written down:

- The stop-loss is a flat 7%. The "volatility-adjusted 7–12%" stop can never
  engage because the field it reads does not exist (F-02-09).
- There are two stops per position, the Alpaca order and the bot's own
  30-minute check, at different prices, and neither knows about the other.
- "Alpaca is the source of truth" holds once a day at best. There is no
  reconcile at start-up, before an order, or during monitoring.
- Minimum conviction is 7 only in normal markets. In a sell-off it drops to 6.
- The models are OpenAI and Perplexity. The README names Anthropic, which the
  code never calls.
- `STOCK_AGENT_MODE` is only a label on messages. `ALPACA_BASE_URL` alone
  decides paper or live.

## 3. Top findings

Every Critical and High finding that survived verification, with duplicates
across areas merged into one row. "Bot's side" means the code path is
confirmed but the final outcome depends on how Alpaca responds, which could
not be tested.

### Exits and stop-losses

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-03-01](findings/03-execution-and-broker-reconciliation.md) (also F-02-01) | Critical | After Alpaca's stop fills, the bot's 30-minute check can sell the same shares again from its stale ledger. That may open a short position, which the bot's sync silently ignores. **Seen in production twice**, 1 and 27 May; both shorts are still open (E2 §1, E1 §2). |
| [F-03-02](findings/03-execution-and-broker-reconciliation.md) | High | The bot never cancels its resting stop, so its own "thesis broken" sells are probably refused by Alpaca, with a log line and no alert. Bot's side. |
| [F-03-05](findings/03-execution-and-broker-reconciliation.md) (also F-08-04) | High | The stop order is sent blind 3 seconds after the buy. If it is rejected, the position has no stop, yet the alert and ledger still show one. **Seen in production:** of 30 buys since the stop code arrived, the 11 that filled within three seconds all got a stop and the 19 that took longer got none. Every buy made at the open, which is when the bot normally buys, has gone without a broker stop (E3 §2; see F-11-15). The broker's refusal itself was not captured. |
| [F-06-01](findings/06-data-sources.md) (also F-08-05) | High | When price quotes fail, the bot's stop check runs on old prices, reports success and alerts nobody. **Seen in production:** the feed refused about 12,000 requests in two months, including quotes for held symbols (E1 §3). |
| [F-05-01](findings/05-research-and-llm.md) | High | One model reply of "SELL" sells a whole position, and the same news is put to the model up to six times a day. |
| [F-01-02](findings/01-scheduler-and-lifecycle.md) (also F-03-08, F-05-02) | High | A sell decided in the 08:00 scan is logged as "queued" and then forgotten. |

### Books versus broker

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-07-01](findings/07-state-and-accounting.md) | Critical | If the state file is missing or unreadable, the bot silently starts with an empty $100,000 book and can buy what it already holds. A new model field in the revamp could trigger this on deploy. |
| [F-03-04](findings/03-execution-and-broker-reconciliation.md) (also F-07-03, F-08-06, F-10-04) | High | One failed "list positions" request is read as "the account is empty" and wipes the ledger, losing every thesis, entry date and sector. |
| [F-03-03](findings/03-execution-and-broker-reconciliation.md) (also F-02-02) | High | Risk checks use the bot's own ledger. The only weekday reconcile is a side effect of the 08:15 morning brief. **Seen in production:** ledger 6 positions and +36.0%, broker 9 positions and +24.0% (E1 §2). |
| [F-03-06](findings/03-execution-and-broker-reconciliation.md) | High | A timed-out order request is treated as "not placed", with no order ID to check, so an order can exist that the bot does not know about. |
| [F-03-07](findings/03-execution-and-broker-reconciliation.md) (also F-02-06, F-06-02) | High | Share count, stop price and recorded entry use the price from analysis time, up to 65 hours old. After a gap, the stop can sit far from the real entry or above the market. |
| [F-07-05](findings/07-state-and-accounting.md) (also F-06-12) | High | A zero or null price from the quote feed is written into the ledger and can hide a position from the stop check. Mechanism confirmed; whether the feed ever sends one is unknown. |

### Risk limits

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-02-03](findings/02-risk-and-sizing.md) | High | Sector, cluster and exposure caps test the portfolio before the order. A sector at 39.6% can go to 49.6%; the "soft block" does nothing. |
| [F-02-04](findings/02-risk-and-sizing.md) | High | Top-ups skip the sector, cluster, exposure and cash checks and are not taken out of the ledger's cash. |
| [F-02-05](findings/02-risk-and-sizing.md) | High | There is no working cash or total-exposure limit (the default is 100%, the comment says 50%), so the bot can buy on margin. **Seen in production:** cash was negative on 40 days, on a margin account with 4x buying power (E1 §5, E2 §2). |
| [F-06-04](findings/06-data-sources.md) | High | Sectors are named two different ways ("Financials" and "Financial Services"), so the 40% cap under-counts. |
| [F-02-07](findings/02-risk-and-sizing.md) | High | In a sell-off the minimum conviction drops to 6. Deliberate, but it contradicts the recorded decision. Needs Josh's call. |
| [F-05-03](findings/05-research-and-llm.md) | High | Position size follows the model's score plus up to five bonus points for data the model had already seen. A model 5 can become a full-size 10. |

### Options

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-04-01](findings/04-options.md) | Critical | A spread is two separate market orders and is recorded as opened even if one or both fail. One live leg can be left at the broker untracked. |
| [F-04-02](findings/04-options.md) | Critical | Nothing ever closes an option. Positions run to expiry and can turn into stock positions, long or short, that no limit covers. **Seen in production:** exercised spreads delivered shares that took one stock to 71.5% of equity and total exposure to 154% (E1 §5). On three expiry days the broker itself closed a spread at 15:30; it does not do so reliably (E3 §4). |
| [F-04-03](findings/04-options.md) | Critical | The 15% options cap measures premium. Ten puts that could oblige $70,000 read as 1.5%. Cash behind a put is neither checked nor reserved. **Seen in production** in its effect: exposure above 100% on 49 days (E1 §5). |
| [F-04-04](findings/04-options.md) (also F-07-02, F-10-04) | High, two verifiers suggest Critical | One failed positions request closes every option in the ledger with invented losses and never restores them. The limits then read zero and the same run can open more. **Consistent with production:** the broker holds five option legs and the ledger holds none (E1 §2). |
| [F-04-05](findings/04-options.md) | High | Covered calls are checked once against the local share count. The shares can later be sold or stopped out, leaving the call uncovered. |
| [F-04-06](findings/04-options.md) | High | Option orders go at market with no limit price, and the fill is never read back. **Seen in production:** all 91 option orders were market orders (E2 §4). |
| [F-04-07](findings/04-options.md) (also F-10-03, F-01-05) | High | `OPTIONS_ENABLED` and every other `OPTIONS_*` setting are read by nothing. There is no off switch, and the real limits differ from `config.py`. |
| [F-04-08](findings/04-options.md) | High | The two legs of a spread can have different expiry dates. |
| [F-04-09](findings/04-options.md) | High | Puts are sold at conviction 6, including on stocks with no thesis at all. |

### Timing and restarts

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-01-03](findings/01-scheduler-and-lifecycle.md) (also F-03-09, F-02-08) | High | The bot has no market calendar. It treats holidays and early closes as open, and orders sent while the market is closed are booked as filled. **Next holiday: Thursday 26 November 2026.** **Seen in production:** two buys sent on Good Friday were cancelled by the broker and still recorded as bought (E2 §3). |
| [F-01-01](findings/01-scheduler-and-lifecycle.md) (also F-07-04) | High | Buys waiting for the open exist only in memory. Any restart, including every merge to `master`, drops them silently. |
| [F-01-04](findings/01-scheduler-and-lifecycle.md) | High | Shutdown is not clean. The bot keeps placing orders after being told to stop and can be killed between a buy and its stop. |
| [F-01-06](findings/01-scheduler-and-lifecycle.md) (also F-06-05) | High | A change in the macro "regime" starts a full research-and-trade cycle in market hours. One missing quote can cause the change. |
| [F-05-04](findings/05-research-and-llm.md) | High | A Perplexity or OpenAI outage is read as "nothing to do". The weekly run is marked done and reports normally. **Seen in production:** no successful Perplexity call in two months, 36 cycles with "0 signals", no alert (E1 §3). |

### Alerts, deploy and security

| ID | Sev. | What it means |
| --- | --- | --- |
| [F-09-01](findings/09-deploy-security-ops.md) (also F-06-03) | Critical | Real-looking credentials are on public `master`: Telegram (`.env.example:2-3`), TipRanks (`config.py:194-195`, `tipranks_client.py:18-19`), a Perplexity proxy token (`.mcp.json:13`). A Telegram-token-shaped value is also in the history of a removed file (E2 §8). |
| [F-09-03](findings/09-deploy-security-ops.md) | Critical once live | Nothing checks that the mode label and the broker address agree. Real orders could go out labelled "PAPER". Not triggered: the server's label and address are both paper (E2 §5). |
| [F-08-01](findings/08-alerts-and-observability.md) | High | Every sell the broker accepts is announced as "Sell failed" and the real sell alert is never sent (a one-line bug at `scheduler.py:1349`, present since 2026-04-23). **Seen in production** on 1 October, 26 October and 5 November (E1 §4). |
| [F-08-02](findings/08-alerts-and-observability.md) | High | Nothing watches the bot. A dead or mis-configured bot is simply silent. **Seen in production:** research was dead for about four months unnoticed (E1 §3). |
| [F-08-03](findings/08-alerts-and-observability.md) | High | The Telegram token and the FMP and Finnhub keys are written to the server log on every request. **Seen in production** for FMP (about 25,700 lines) and Finnhub (about 1,400); no Telegram request appears at all (E2 §7). |
| [F-09-04](findings/09-deploy-security-ops.md) | High | The deploy script reports "Update complete" even when it failed, and a crash on start loops silently. |
| [F-09-05](findings/09-deploy-security-ops.md) | High | The bot cannot start unless GitHub answers at that moment. The update log shows 25 failed fetches in July; no outage resulted (E1 §7). |
| [F-09-06](findings/09-deploy-security-ops.md) | High | Stopping the service does not stick: the next merge restarts it. There is no "no new orders" switch. |
| [F-09-07](findings/09-deploy-security-ops.md) | High | Every change to `master` restarts the bot as root at an arbitrary moment, with no test gate. |
| [F-10-01](findings/10-code-health-and-testability.md) | High | No test covers any code that places an order, records a trade or reconciles with the broker. |

### Settled or still unclear after the evidence

| ID | Status |
| --- | --- |
| [F-09-09](findings/09-deploy-security-ops.md) | **Settled, partly true.** The old bot's web server is still installed, enabled at boot and running with one site, and the page it serves returns an error. No other old service is enabled; the stock agent is the only application service. The settings file still holds the old bot's settings (E2 §6, E1 §7). Suggested severity: Medium. It is an unneeded open web server on the trading machine and should be removed, but no old endpoint is answering. This also removes the worry that old services were resetting the checkout. |
| [F-06-08](findings/06-data-sources.md) | Still unclear. The log holds status codes only, not FMP responses (E2 §9). |
| [F-06-12](findings/06-data-sources.md) | Still unclear, same reason. |

**The F-03-01 / F-03-02 puzzle is resolved for F-03-01.** On both occasions
the broker's stop had already filled, 22 minutes and about two and a half
hours before the bot's own check sold the same shares again. Alpaca did not
refuse the second sale; it booked it as a short (E2 §1). So F-03-01 is real
and has happened twice. F-03-02 (the bot selling while its stop still rests)
remains open: no market sell filled while a stop was open, an order refused
at submission leaves no record, and the log does not reach back to the
months when stop orders existed.

Two other broker behaviours are now known (E2 §3): market sells sent before
the open were held and filled within five minutes of the open, and market
buys sent on a holiday were cancelled later that day. In both cases the
bot's ledger recorded the trade at the moment it was sent (F-03-09).

### Other findings seen in production

| ID | Sev. | What the evidence shows |
| --- | --- | --- |
| [F-07-14](findings/07-state-and-accounting.md) | Low | The sync ignores short and fractional positions. This is why both shorts and a fractional remainder are missing from the ledger (E1 §2). Rated Low before the evidence; it deserves more. |
| [F-11-04](findings/11-live-performance.md) (also F-07-08) | Medium | The bot's own account value is about 12 points too high (E1 §2). |
| [F-11-01](findings/11-live-performance.md) | Medium | 13 exits were broker stop fills that the ledger never recorded; 56 trades in the ledger against 166 filled orders (E1 §2, §6). |
| [F-02-17](findings/02-risk-and-sizing.md) | Note | The 15% cap limits buying, not holding. The largest position was above 15% on 122 of 164 days and one stock is 33.6% of equity today (E1 §5). |
| [F-02-03](findings/02-risk-and-sizing.md) | High | One sector was above the 40% cap on 121 of 164 days. The evidence attributes the pattern mainly to shares delivered through options (E1 §5). |
| [F-04-07](findings/04-options.md) | High | `OPTIONS_ENABLED` is not set on the server and the scan runs (E2 §5). |
| [F-11-13](findings/11-live-performance.md) | High, new | Research and price inputs failing for months with normal-looking reports (E1 §3). |

### What no existing finding explains

These came out of the evidence and no code finding accounts for them. Each
is recorded as a new finding in area 11 so the plan can pick it up.

| ID | Observation |
| --- | --- |
| [F-11-14](findings/11-live-performance.md) | **Explained and withdrawn.** Both Telegram lines are in the server's settings file with empty values, so "Telegram not configured" is the code working as written. Nothing has gone to Telegram in two months; Discord is the only working channel (E3 §1). |
| [F-11-15](findings/11-live-performance.md) | **Mostly explained.** The stop is sent three seconds after the buy. Fast fills (11, all on 23 April, mid-session) got a stop; slow fills (19, all at the open) got none. This is F-03-05 in production (E3 §2). Still unexplained: eight stops sent together at 04:01 on 27 April for positions already held, through the API key, by something outside the bot's normal run (E3 §3). |
| [F-11-15](findings/11-live-performance.md) | Six stops were cancelled, five of them 88 to 90 days after placement. The bot never cancels a stop, so the broker most likely expired them. Nothing reported it (E2 §4). |
| [F-11-16](findings/11-live-performance.md) | The broker's portfolio-history figure has left out one long position, about 7% of equity, since 18 August. Cause unknown, on the broker's side (E1 §2). |
| [F-11-17](findings/11-live-performance.md) | The options scan has traded the same four symbols since April and retries a ticker renamed in June (E1 §3, §6). The verifier traced the likely cause to two existing findings acting together: a refused screen falls back to four hard-coded symbols (F-06-09), and stored theses of any age trigger the spreads (F-04-09). Not confirmed against the server, and it does not explain the April date. |
| none | Covered calls were sold far below the market price on 11 May. Commit `e92e1cd` fixed the strike rule, but no test covers it (E1 §4). |

## 4. Themes

1. **A failed request looks the same as "nothing there".** Almost every
   network wrapper returns an empty list or `None` on error, and callers act
   on it. This is the root of the ledger wipes, the blind stop check and the
   silent research outages.
2. **The bot trusts its own notes over the broker.** It records orders as
   filled when they are accepted, at a price it guessed, and reconciles
   rarely. It never reads a fill, an open order or a stop's status.
3. **Limits are checked before the trade, not after, and only on some paths.**
   Top-ups, options, imported positions and the event-triggered scan each skip
   some checks.
4. **Settings and documents describe a different bot.** Switches that switch
   nothing (`OPTIONS_ENABLED`, `STOCK_AGENT_MODE`, the TipRanks variables),
   comments with the wrong numbers, a README for an older design, and an
   `.env.example` that is 88% another bot's settings.
5. **Important state lives only in memory.** Queued buys and the "already ran
   today" markers vanish on restart, and restarts happen on every merge.
6. **Serious problems reach only the server log.** Rejected orders, a missing
   stop, a failed sync and a failed research run produce no alert.
7. **Nothing was tested until this week.** The first tests arrived with the
   revamp setup and cover the risk arithmetic only.

## 5. What is solid

- The main loop is simple and survives errors; jobs cannot overlap; daylight
  saving is handled correctly.
- Conviction-tier sizing (7/8/9/10 to 5/7/8/10%) and the per-symbol check for
  new positions do what the recorded decision says.
- Garbled, refused or truncated model replies are dropped, never traded on.
- TipRanks is never a sole buy trigger.
- Covered calls target out-of-the-money strikes only.
- Alpaca keys travel in headers and are not logged.
- A failing alert cannot crash the trading path.
- When both the account and positions calls fail, the sync is correctly
  skipped (the gap is when only the positions call fails).
- State is saved through a temp file and rename, with a backup copy.
- The whole package is about 9,250 lines with one orchestrator. It is small
  enough to fix in place.

## 6. Unknowns

Answered by the evidence and removed from this list: results and uptime
(area 11), whether a short position exists (yes, two), the account's
settings (margin with 4x buying power, short selling enabled, options level
3), the server's exposure and options settings (`MAX_TOTAL_EXPOSURE_PCT` is
1.00, `OPTIONS_ENABLED` is not set, mode and address are both paper),
leftovers from the old bot (F-09-09), and wallet keys in git history (a
pattern search over all 251 commits found none).

Still unknown:

| Unknown | What would answer it |
| --- | --- |
| Whether the exposed credentials still work | Provider dashboards. Nobody tested them. Rotate regardless. |
| Secrets of an unusual shape in git history | The search was by pattern and can miss them (E2 §8). Rotating everything the old bot used closes it. |
| How Alpaca answers a sell while a stop order rests (F-03-02) | A deliberate test on the paper account, or Alpaca's documentation |
| Real FMP field names and whether prices can be null or zero (F-06-08, F-06-12) | One saved response per endpoint |
| Anything before 20 September 2026 in the logs | Nothing; the log does not reach back. Longer retention is a plan item. |
| Holding periods, stop fill quality, whether widening the stop to 7% helped | The fills joined to stop prices (not measured) |
| Rejected orders, and duplicates other than the two double sales | Not recorded by the broker; not checked |
| What the broker answers to a stop sent before the buy has filled | One saved response (E3 §5) |
| What sent eight stop orders at 04:01 on 27 April | Josh may remember (E3 §3) |
| Why portfolio history omits a position | Broker side; see F-11-16 |
| API cost per month | Provider billing pages |

## 7. Decisions for Josh

**Urgent, before the plan**

1. **Rotate the exposed credentials?** Recommended: yes, now. Telegram and
   the Perplexity proxy token can be rotated at the provider straight away.
   TipRanks needs a code change, because the client ignores `.env`. Do not
   make the repository private as a quick fix: the droplet pulls without
   credentials and the bot would fail to start at its next restart.
2. **Thanksgiving, Thursday 26 November (and the 13:00 close on Friday 27).**
   The evidence lowers the urgency. On Good Friday the broker cancelled the
   bot's holiday buys, though the ledger still recorded them (E2 §3), and
   with research down no buy has been queued for two months. The remaining
   risk is a wrong ledger entry, not a wrong fill. Options: (a) stop the
   service and the cron updater over the holiday; (b) leave it. Recommended:
   (b) is acceptable as things stand; choose (a) if you restore the research
   inputs before Thursday.
3. **Snapshot and order history: done.** Still useful from you: the
   providers' billing pages, one saved FMP response per endpoint, and an
   export of the Discord trade-history channel.

**New from the live evidence**

22. **Two short stock positions are open and the bot cannot see them.**
    Options: (a) buy them back by hand now; (b) leave them until the bot can
    track shorts. Recommended: (a), then switch short selling off at Alpaca
    (decision 6). They have cost about 1.3 points so far and nothing manages
    them.
23. **No position has a stop order at the broker.** The cause is now
    known: a stop sent three seconds after a buy at the open is too early,
    and every such buy has gone without one (E3 §2). The April stops that
    did exist filled or expired after about 90 days, and the bot's own check
    depends on a price feed that is partly refused. Options: (a) place stops by
    hand now, knowing they expire again; (b) accept no stops while on paper;
    (c) stop the bot until decision 5 is built. Recommended: decide it
    knowingly. On paper (b) costs nothing real, but the record it produces
    says little. Whichever you choose, the bot's buy alerts have been showing
    a stop price that did not exist at the broker.
24. **Should the bot keep running with no working research?** Options:
    (a) restore the Perplexity, FMP, OpenAI and TipRanks accounts now;
    (b) stop the service until the revamp has input health checks;
    (c) leave it. Recommended: (b), or (a) if you want the paper record to
    continue. Under (c) the options scan keeps repeating losing spreads on
    the same four symbols.
25. **One stock is 33.6% of equity.** Trim it by hand to the 15% cap, or
    leave it? The bot has no code that trims. Recommended: your call; the
    plan needs to know whether the cap applies to holding or only to buying.
26. **Telegram is switched off on the server.** Both settings are empty
    there, so nothing has been sent for at least two months and Discord is
    your only channel (E3 §1). Options: (a) leave it off and make Discord the
    one must-act channel; (b) switch Telegram back on with a new token.
    Recommended: (a) unless you watch Telegram more closely than Discord;
    this folds into decision 16. Either way, revoke the token in
    `.env.example` if that bot still exists.
27. **Entry bar.** Score 7 entries lost money on average, on six trades.
    Recommended: no change on so small a sample, but it strengthens the case
    in decision 7 for never dropping to 6.

**Shape of the bot**

4. **Options while the revamp runs:** (a) off entirely; (b) covered calls
   only; (c) keep all four strategies. Recommended: (a), with a working off
   switch as the first options change, and any open positions closed by hand.
   Evidence: options made about +15.6 points in April and have lost about
   8.0 since; 5 of the last 17 structures were profitable (E1 §6).
5. **Who owns the stop-loss:** (a) the Alpaca order, with the bot only
   watching it; (b) the bot's 30-minute check; (c) both, with the bot
   cancelling one before using the other. Recommended: (a). It works while
   the bot is down.
6. **Should the account ever be short stock?** Recommended: no, and switch
   short selling off in the Alpaca account settings as a backstop. Now
   urgent: shorting and 4x margin are enabled and two shorts exist (E2 §1, §2).
7. **Minimum conviction in a sell-off: 7 always, or 6 as now?** Recommended:
   7 always, matching the recorded decision. Evidence: score 9 entries
   averaged +22.2%, score 8 +8.0%, score 7 -1.5% (E1 §6).
8. **Bonus conviction points after the model has answered:** (a) none;
   (b) at most +1; (c) up to +5 as now. Recommended: (a) or (b).
9. **Total exposure and margin:** is the cap 50% or 100%, and may the bot
   borrow? Recommended: no borrowing, and a cash floor you choose.
10. **Should the sector and cluster caps hold after the buy, and apply to
    top-ups?** Recommended: yes to both.
11. **One model reply sells a whole position.** Options: (a) keep; (b) require
    a second confirming call; (c) alert you and wait. Recommended: (b).
12. **Sells decided while the market is closed:** (a) send at the open;
    (b) re-check at the open first. Recommended: (a), held by the bot and sent
    in market hours, never booked before it fills.
13. **Stale prices:** how far may a price move between research and order
    before the buy is skipped, and how old may a signal be? Suggested
    starting point: skip beyond 3% or 4 trading days.
14. **Should a market-regime change trade on its own, or only alert you?**
    Recommended: alert only.
15. **Volatility-adjusted stop (7–12%) as documented, or the flat 7% the bot
    really uses?** Either is defensible; the documents should match the code.
    Evidence: of 25 stop exits the price was lower 20 trading days later in
    only 10, and the stops cost about 5 points against holding (E1 §6). A
    small sample from one rally, but worth weighing.

**Operations**

16. **Which channel do you watch, Telegram or Discord, and how long may the
    bot be silent before something outside it messages you?** Recommended:
    one must-act channel and an outside heartbeat check.
17. **Deploys:** (a) automatic on every merge as now; (b) automatic but only
    outside market hours and only if tests pass; (c) manual. Recommended: (b).
18. **A "pause new buys, keep exits" switch?** Recommended: yes.
19. **If a state file cannot be read, refuse to trade until rebuilt from
    Alpaca?** Recommended: yes.
20. **Make the repository private?** Recommended: yes, after a read-only
    deploy key is on the droplet.
21. **Mostly answered.** The "Sell failed" error is in the log on three
    dates and two short positions exist (E1 §4, E2 §1). Of 174 orders, 170
    came through the bot's API key, 3 were placed by the broker to close
    expiring spreads, and 1 did not come through the API key: a
    fractional-share buy on 16 April (E3 §4). Still needed from you: do you
    remember what sent eight stop orders at 04:01 on 27 April (a one-off
    script?), and did you place the fractional buy?

## 8. Suggested order of work

A suggestion for the plan phase, not the plan.

1. **Stop the bleeding (no code):** rotate credentials; close the two
   shorts and turn off shorting at Alpaca; decide whether the bot keeps
   running without research; remove the old web server.
2. **Safety net first:** tests around the order, ledger and reconcile paths
   using a fake broker, so later changes can be checked. Fix the false "Sell
   failed" alert as the first small proof.
3. **Make failures visible:** a failed request must not look like an empty
   result; alerts for rejected orders, a missing stop and a failed sync; a
   heartbeat.
4. **Trust the broker:** reconcile at start-up and before orders; read fills;
   refuse to trade on an unreadable state file; one owner for the stop.
5. **Market calendar and persistence:** ask Alpaca whether the market is
   open; save queued orders to disk; clean shutdown.
6. **Risk limits that hold:** check after the trade, on every buy path,
   including top-ups; a real cash limit; one sector vocabulary; fresh prices.
7. **Options:** a working off switch first, then rebuild or remove.
8. **Deploy and configuration:** tests gate the deploy; no restart for
   documentation; one setting for paper or live with a start-up check; pinned
   dependencies; a clean `.env.example`.
9. **Research quality:** store every thesis, cap the adjustments, confirm
   sells, and measure whether conviction predicts results.
10. **Clean-up:** dead code, duplicated clients, README.
11. **A paper test period** with working inputs and written pass or fail
    criteria before the strategy is judged or real money is considered
    (E1, "What the plan has to cover").

## 9. Index

| # | Area | Critical | High | Medium | Low | Note | File |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | Scheduler and lifecycle | 0 | 6 | 5 | 2 | 0 | [findings](findings/01-scheduler-and-lifecycle.md) |
| 02 | Risk and sizing | 1 | 7 | 7 | 1 | 1 | [findings](findings/02-risk-and-sizing.md) |
| 03 | Execution and broker reconciliation | 1 | 8 | 5 | 1 | 1 | [findings](findings/03-execution-and-broker-reconciliation.md) |
| 04 | Options | 3 | 6 | 3 | 3 | 0 | [findings](findings/04-options.md) |
| 05 | Research and LLM pipeline | 0 | 4 | 11 | 1 | 1 | [findings](findings/05-research-and-llm.md) |
| 06 | Data sources | 1 | 6 | 9 | 2 | 0 | [findings](findings/06-data-sources.md) |
| 07 | State and accounting | 2 | 3 | 7 | 3 | 0 | [findings](findings/07-state-and-accounting.md) |
| 08 | Alerts and observability | 0 | 6 | 5 | 2 | 1 | [findings](findings/08-alerts-and-observability.md) |
| 09 | Deploy, security, operations | 2 | 5 | 5 | 1 | 0 | [findings](findings/09-deploy-security-ops.md) |
| 10 | Code health and testability | 0 | 3 | 8 | 2 | 2 | [findings](findings/10-code-health-and-testability.md) |
| 11 | Live performance (code, then live evidence) | 0 | 2 | 10 | 1 | 3 | [findings](findings/11-live-performance.md) |
| | **Total** | **10** | **56** | **75** | **19** | **9** | 169 findings, plus F-11-14 withdrawn |

Counts are as rated by each area's auditor. Many findings describe the same
defect from different areas; the tables in section 3 merge them into about
forty distinct problems. Verifiers suggested three severity changes that are
left for Josh and the plan to settle: F-04-04 up to Critical, F-01-05 down to
Medium, and F-06-08 and F-06-12 down to Medium. Five findings (F-11-13 to
F-11-17) were added on 2026-11-21 from the live evidence and then checked by
a second reviewer: F-11-13 and F-11-15 confirmed, F-11-14 withdrawn, F-11-16
confirmed with Note suggested, and F-11-17 unclear with High suggested, its
cause corrected to F-06-09 plus F-04-09. The evidence also suggests F-09-09 down to Medium
and F-07-14 up from Low.
