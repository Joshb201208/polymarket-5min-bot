# Audit of the stock agent

Audited commit: `941f2ca` (code identical to `master` at `e92e1cd`; the two extra
commits only add the revamp setup). Date: 2026-11-20.

Eleven areas were audited independently. Every Critical and High finding was
then re-checked by a second reviewer whose job was to prove it wrong. No
finding was refuted; three are marked "Unclear" because they depend on facts
only the server or a data provider can supply.

**No droplet snapshot was available.** Everything here is what the code does
and allows. What actually happened in the account (results, orders, uptime) is
not known and is listed under "Unknowns".

## 1. Bottom line

The bot is not sound enough to trust with real money, and several of its
safety features do not work the way the documentation and `CLAUDE.md` say.
The research-to-order loop itself runs and is hard to crash, and the sizing
tiers and the basic per-symbol check are correct. The biggest risks are in
how it exits and how it keeps its books. The bot's own sells never cancel the
stop order resting at Alpaca, so a stop-out can be sold twice, which may
open a short position the bot cannot see. Its records are checked against
Alpaca only about once a day and never before an order, and one failed
"list positions" request makes it believe the account is empty. Options are
the weakest part: nothing ever closes an option, the "15% exposure" cap
measures premium and not money at risk, a half-filled spread is recorded as
opened, and `OPTIONS_ENABLED=false` does nothing. Credentials for Telegram,
TipRanks and a Perplexity proxy are committed in this public repository and
need rotating now. Nothing watches the bot, most serious failures reach only
the server log, and every accepted sell is announced as "Sell failed".
My recommendation is to revamp in place, safety first, with one exception:
switch options off and rebuild that module only if Josh still wants it.
Two things should not wait for the plan: rotating the exposed credentials,
and deciding what to do about the market holiday on Thursday 26 November.

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
| [F-03-01](findings/03-execution-and-broker-reconciliation.md) (also F-02-01) | Critical | After Alpaca's stop fills, the bot's 30-minute check can sell the same shares again from its stale ledger. That may open a short position, which the bot's sync silently ignores. Bot's side. |
| [F-03-02](findings/03-execution-and-broker-reconciliation.md) | High | The bot never cancels its resting stop, so its own "thesis broken" sells are probably refused by Alpaca, with a log line and no alert. Bot's side. |
| [F-03-05](findings/03-execution-and-broker-reconciliation.md) (also F-08-04) | High | The stop order is sent blind 3 seconds after the buy. If it is rejected, the position has no stop, yet the alert and ledger still show one. |
| [F-06-01](findings/06-data-sources.md) (also F-08-05) | High | When price quotes fail, the bot's stop check runs on old prices, reports success and alerts nobody. |
| [F-05-01](findings/05-research-and-llm.md) | High | One model reply of "SELL" sells a whole position, and the same news is put to the model up to six times a day. |
| [F-01-02](findings/01-scheduler-and-lifecycle.md) (also F-03-08, F-05-02) | High | A sell decided in the 08:00 scan is logged as "queued" and then forgotten. |

### Books versus broker

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-07-01](findings/07-state-and-accounting.md) | Critical | If the state file is missing or unreadable, the bot silently starts with an empty $100,000 book and can buy what it already holds. A new model field in the revamp could trigger this on deploy. |
| [F-03-04](findings/03-execution-and-broker-reconciliation.md) (also F-07-03, F-08-06, F-10-04) | High | One failed "list positions" request is read as "the account is empty" and wipes the ledger, losing every thesis, entry date and sector. |
| [F-03-03](findings/03-execution-and-broker-reconciliation.md) (also F-02-02) | High | Risk checks use the bot's own ledger. The only weekday reconcile is a side effect of the 08:15 morning brief. |
| [F-03-06](findings/03-execution-and-broker-reconciliation.md) | High | A timed-out order request is treated as "not placed", with no order ID to check, so an order can exist that the bot does not know about. |
| [F-03-07](findings/03-execution-and-broker-reconciliation.md) (also F-02-06, F-06-02) | High | Share count, stop price and recorded entry use the price from analysis time, up to 65 hours old. After a gap, the stop can sit far from the real entry or above the market. |
| [F-07-05](findings/07-state-and-accounting.md) (also F-06-12) | High | A zero or null price from the quote feed is written into the ledger and can hide a position from the stop check. Mechanism confirmed; whether the feed ever sends one is unknown. |

### Risk limits

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-02-03](findings/02-risk-and-sizing.md) | High | Sector, cluster and exposure caps test the portfolio before the order. A sector at 39.6% can go to 49.6%; the "soft block" does nothing. |
| [F-02-04](findings/02-risk-and-sizing.md) | High | Top-ups skip the sector, cluster, exposure and cash checks and are not taken out of the ledger's cash. |
| [F-02-05](findings/02-risk-and-sizing.md) | High | There is no working cash or total-exposure limit (the default is 100%, the comment says 50%), so the bot can buy on margin. |
| [F-06-04](findings/06-data-sources.md) | High | Sectors are named two different ways ("Financials" and "Financial Services"), so the 40% cap under-counts. |
| [F-02-07](findings/02-risk-and-sizing.md) | High | In a sell-off the minimum conviction drops to 6. Deliberate, but it contradicts the recorded decision. Needs Josh's call. |
| [F-05-03](findings/05-research-and-llm.md) | High | Position size follows the model's score plus up to five bonus points for data the model had already seen. A model 5 can become a full-size 10. |

### Options

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-04-01](findings/04-options.md) | Critical | A spread is two separate market orders and is recorded as opened even if one or both fail. One live leg can be left at the broker untracked. |
| [F-04-02](findings/04-options.md) | Critical | Nothing ever closes an option. Positions run to expiry and can turn into stock positions, long or short, that no limit covers. Bot's side. |
| [F-04-03](findings/04-options.md) | Critical | The 15% options cap measures premium. Ten puts that could oblige $70,000 read as 1.5%. Cash behind a put is neither checked nor reserved. |
| [F-04-04](findings/04-options.md) (also F-07-02, F-10-04) | High, two verifiers suggest Critical | One failed positions request closes every option in the ledger with invented losses and never restores them. The limits then read zero and the same run can open more. |
| [F-04-05](findings/04-options.md) | High | Covered calls are checked once against the local share count. The shares can later be sold or stopped out, leaving the call uncovered. |
| [F-04-06](findings/04-options.md) | High | Option orders go at market with no limit price, and the fill is never read back. |
| [F-04-07](findings/04-options.md) (also F-10-03, F-01-05) | High | `OPTIONS_ENABLED` and every other `OPTIONS_*` setting are read by nothing. There is no off switch, and the real limits differ from `config.py`. |
| [F-04-08](findings/04-options.md) | High | The two legs of a spread can have different expiry dates. |
| [F-04-09](findings/04-options.md) | High | Puts are sold at conviction 6, including on stocks with no thesis at all. |

### Timing and restarts

| ID | Sev. | What it means for the account |
| --- | --- | --- |
| [F-01-03](findings/01-scheduler-and-lifecycle.md) (also F-03-09, F-02-08) | High | The bot has no market calendar. It treats holidays and early closes as open, and orders sent while the market is closed are booked as filled. **Next holiday: Thursday 26 November 2026.** Bot's side. |
| [F-01-01](findings/01-scheduler-and-lifecycle.md) (also F-07-04) | High | Buys waiting for the open exist only in memory. Any restart, including every merge to `master`, drops them silently. |
| [F-01-04](findings/01-scheduler-and-lifecycle.md) | High | Shutdown is not clean. The bot keeps placing orders after being told to stop and can be killed between a buy and its stop. |
| [F-01-06](findings/01-scheduler-and-lifecycle.md) (also F-06-05) | High | A change in the macro "regime" starts a full research-and-trade cycle in market hours. One missing quote can cause the change. |
| [F-05-04](findings/05-research-and-llm.md) | High | A Perplexity or OpenAI outage is read as "nothing to do". The weekly run is marked done and reports normally. |

### Alerts, deploy and security

| ID | Sev. | What it means |
| --- | --- | --- |
| [F-09-01](findings/09-deploy-security-ops.md) (also F-06-03) | Critical | Real-looking credentials are on public `master`: Telegram (`.env.example:2-3`), TipRanks (`config.py:194-195`, `tipranks_client.py:18-19`), a Perplexity proxy token (`.mcp.json:13`). |
| [F-09-03](findings/09-deploy-security-ops.md) | Critical once live | Nothing checks that the mode label and the broker address agree. Real orders could go out labelled "PAPER". |
| [F-08-01](findings/08-alerts-and-observability.md) | High | Every sell the broker accepts is announced as "Sell failed" and the real sell alert is never sent (a one-line bug at `scheduler.py:1349`, present since 2026-04-23). |
| [F-08-02](findings/08-alerts-and-observability.md) | High | Nothing watches the bot. A dead or mis-configured bot is simply silent. |
| [F-08-03](findings/08-alerts-and-observability.md) | High | The Telegram token and the FMP and Finnhub keys are written to the server log on every request. |
| [F-09-04](findings/09-deploy-security-ops.md) | High | The deploy script reports "Update complete" even when it failed, and a crash on start loops silently. |
| [F-09-05](findings/09-deploy-security-ops.md) | High | The bot cannot start unless GitHub answers at that moment. |
| [F-09-06](findings/09-deploy-security-ops.md) | High | Stopping the service does not stick: the next merge restarts it. There is no "no new orders" switch. |
| [F-09-07](findings/09-deploy-security-ops.md) | High | Every change to `master` restarts the bot as root at an arbitrary moment, with no test gate. |
| [F-10-01](findings/10-code-health-and-testability.md) | High | No test covers any code that places an order, records a trade or reconciles with the broker. |

### Unclear after verification

| ID | Why it is unclear |
| --- | --- |
| [F-09-09](findings/09-deploy-security-ops.md) | Leftovers from the old bot (nginx, a dashboard, old services) may still be running on the droplet. Only a snapshot can tell. Low if gone, Critical if an old endpoint is still reachable. |
| [F-06-08](findings/06-data-sources.md) | Some financial fields in the prompts may be mislabelled or empty. Needs one saved FMP response per endpoint. Verifier suggests Medium. |
| [F-06-12](findings/06-data-sources.md) | Same mechanism as F-07-05; depends on whether FMP ever returns a zero or null price. |

One puzzle the verifiers raised: F-03-01 and F-03-02 cannot both describe the
same sell. A comment at `config.py:157-161` records five past stop-out sells
by the bot's own loop as completed. So either Alpaca's stop had already
filled (and a short may have been opened), or the stop orders were never
live, or Alpaca does not refuse such sells. The Alpaca order history settles
this and is the single most useful thing to check.

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

| Unknown | What would answer it |
| --- | --- |
| Everything in area 11: results, drawdown, win rate, uptime, whether limits were breached, real error counts | A droplet snapshot from `scripts/vps_snapshot.sh`. The script is not on `master` yet, so it is not on the droplet; it would have to be copied there or the setup merged first. |
| Whether a short position, orphan stops or duplicate orders exist | Alpaca order history and current positions (in the snapshot) |
| How Alpaca responds to: a sell while a stop rests, a sell with no shares, orders sent while closed, a stop sent before the buy fills | Order history, or Alpaca's order documentation |
| Options approval level, margin or cash account, shorting enabled | Alpaca account settings |
| What the droplet's `.env` sets for `MAX_TOTAL_EXPOSURE_PCT` and `OPTIONS_ENABLED` | Josh, or the snapshot's redacted settings list |
| Whether old services and nginx are still on the droplet (F-09-09) | Snapshot |
| Secrets in git history before 2026-04-04 | This checkout holds only the last 50 commits. A full-history scan is needed; the older bot handled a wallet private key. |
| Whether the exposed credentials still work | Provider dashboards. Nobody tested them. |
| Real FMP field names and whether prices can be null or zero | One saved response per endpoint |
| API cost per month | Provider billing pages |

## 7. Decisions for Josh

**Urgent, before the plan**

1. **Rotate the exposed credentials?** Recommended: yes, now. Telegram and
   the Perplexity proxy token can be rotated at the provider straight away.
   TipRanks needs a code change, because the client ignores `.env`. Do not
   make the repository private as a quick fix: the droplet pulls without
   credentials and the bot would fail to start at its next restart.
2. **Thanksgiving, Thursday 26 November (and the 13:00 close on Friday 27).**
   Options: (a) stop the service and disable the cron updater from Wednesday
   evening to Monday; (b) leave it and accept that Wednesday's queued buys are
   sent on a closed market and booked as filled. Recommended: (a). Note that
   stopping the service alone does not stick if anything is merged.
3. **Can you provide a droplet snapshot and the Alpaca order history?**
   Recommended: yes. It closes most of the unknowns, starting with whether a
   short position exists.

**Shape of the bot**

4. **Options while the revamp runs:** (a) off entirely; (b) covered calls
   only; (c) keep all four strategies. Recommended: (a), with a working off
   switch as the first options change, and any open positions closed by hand.
5. **Who owns the stop-loss:** (a) the Alpaca order, with the bot only
   watching it; (b) the bot's 30-minute check; (c) both, with the bot
   cancelling one before using the other. Recommended: (a). It works while
   the bot is down.
6. **Should the account ever be short stock?** Recommended: no, and switch
   short selling off in the Alpaca account settings as a backstop.
7. **Minimum conviction in a sell-off: 7 always, or 6 as now?** Recommended:
   7 always, matching the recorded decision.
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
21. **Have you seen "Sell failed … _tipranks_lookup" messages, a negative
    share count in Alpaca, or placed orders by hand?** These are facts only
    you have, and they tell us which findings have already bitten.

## 8. Suggested order of work

A suggestion for the plan phase, not the plan.

1. **Stop the bleeding (no code):** rotate credentials; holiday handling by
   hand; snapshot and order history; turn off shorting at Alpaca.
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
| 11 | Live performance (code only) | 0 | 0 | 8 | 1 | 3 | [findings](findings/11-live-performance.md) |
| | **Total** | **10** | **54** | **73** | **19** | **9** | 165 findings |

Counts are as rated by each area's auditor. Many findings describe the same
defect from different areas; the tables in section 3 merge them into about
forty distinct problems. Verifiers suggested three severity changes that are
left for Josh and the plan to settle: F-04-04 up to Critical, F-01-05 down to
Medium, and F-06-08 and F-06-12 down to Medium.
