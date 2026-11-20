# 01. Scheduler and lifecycle

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

This part of the bot is the clock. One loop wakes about once a minute, looks at
the time in New York, and decides which job is due. The loop itself is simple
and hard to crash: each job catches its own errors, daylight saving is handled
correctly, and two jobs can never run on top of each other because only one
thing runs at a time.

The weaknesses are about memory and timing:

1. **Decisions waiting to be acted on are kept only in the running program.**
   Buy orders decided on Sunday evening wait in memory until Monday 09:30. Any
   restart in between (every merge to `master` causes one) throws them away,
   while the note that says "this week's analysis is done" is saved to disk.
   The week's buys then never happen and nothing tells you.
2. **A "sell" decided before the market opens is never carried out.** The
   08:00 scan writes "Queuing sell ... at market open" in the log and then does
   nothing.
3. **The bot does not know the real market calendar.** It treats every weekday
   09:30 to 16:00 as open. It will send and record orders on market holidays
   (the next one is Thursday 26 November 2026) and after an early close, and
   three jobs send sell orders at times the market is certainly closed.
4. **Stopping the bot is not clean.** After being told to stop it carries on,
   including placing orders, and is killed by the server if it has not finished
   within about 90 seconds. When it starts again it does not ask Alpaca what
   it actually holds.
5. **Two switches do nothing.** `OPTIONS_ENABLED` is never read, so setting it
   to `false` does not stop options trading. `STOCK_AGENT_MODE` only changes
   the label on messages.

I could not measure how long jobs really take or how often restarts have landed
at a bad moment. That needs the server logs (see "Could not verify").

## How it actually works

### Start, loop, stop

1. systemd starts the process. Before Python runs, the unit fetches and
   hard-resets the checkout to `origin/master`
   (`deploy/stock-agent.service:11-12`). So every start, including a restart
   after a crash, pulls the newest code.
2. `stock_agent/__main__.py:14-22` builds one `StockAgentScheduler`, registers a
   handler for SIGTERM and SIGINT that only sets a flag, and runs
   `scheduler.run()`.
3. The constructor loads `portfolio.json`, `options_portfolio.json` and
   `macro_state.json` from disk and sets every "already ran" marker to empty
   (`stock_agent/scheduler.py:88-103`).
4. `run()` sends a "started" message to Telegram and Discord, fills in missing
   sectors, then loops: take the New York time, call `_tick`, sleep 60 seconds
   (`scheduler.py:109-144`). It does **not** reconcile with Alpaca at start.
5. `_tick` (`scheduler.py:146-210`) checks each job's gate in a fixed order and
   awaits the job inline. Nothing runs in the background: a search for
   `create_task`, `ensure_future`, threads and executors in `stock_agent/`
   finds none. The next tick starts 60 seconds after the previous one
   *finishes*, so a 40-minute job means no tick for 41 minutes.
6. A restart happens when: a commit lands on `master` (cron runs
   `deploy/auto_update.sh` every 10 minutes and calls `systemctl restart`,
   lines 11-19); the process exits for any reason (`Restart=always`,
   `RestartSec=30`); or the server reboots.

Order of checks inside one tick:

```
queued-signals-on-first-tick (dead code, see F-01-12)
 -> macro check -> weekly -> mid-week -> morning brief -> pre-market scan
 -> options scan -> earnings scan
 -> if "market open": queued buys -> [monitoring -> options position check -> event check]
 -> daily summary
```

### The real timetable (question 1)

All times are US Eastern, taken from `datetime.now(ET)` (`scheduler.py:50-65`).
"Marker" is what stops the job running twice. Only one marker is on disk.

| Job | Runs when | Marker | Marker is set | On disk? | Orders it can send |
| --- | --- | --- | --- | --- | --- |
| Macro check | Mon-Fri 06:30-07:30 (`:214-222`) | `_macro_check_done_today` | at start (`:328`) | No | none |
| Earnings scan | Mon-Fri 07:00-08:00 (`:285-295`) | `_earnings_check_done_today` | at start (`:697`) | No | sells at once, before the open (`:727`); buys queued (`:781`) |
| Pre-market scan | Mon-Fri 08:00-09:00 (`:274-283`) | `_daily_prescan_done_today` | at start (`:571`) | No | buys queued (`:664`); sells never (F-01-02) |
| Morning brief | Mon-Fri 08:15-08:45 (`:238-246`) | `_morning_brief_done_today` | at start (`:1708`) | No | none; this is the only weekday reconcile with Alpaca (`:1716-1733`) |
| Queued buys | first tick Mon-Fri 09:30-16:00 with a non-empty queue (`:192-197`) | the queue is emptied afterwards (`:197`) | after | No | market buys, each followed by a stop order |
| Monitoring | "market open" and 30 min since last (`:199`, `:304-309`) | `_last_monitor_time` | at start (`:1035`) | No | stop-loss and event sells (`:1053`, `:1088`) |
| Options position check | right after each monitoring run (`:203`) | none of its own | - | - | none (auto-close is disabled, `:1526-1529`) |
| Event check | right after that (`:205`); full rescan if SPX moved 1.5%, VIX moved 3 points, or the regime label changed since the previous check (`:1609-1629`) | `_event_scan_cooldown`, 30 min (`:1594-1597`) | when an event fires (`:1640`) | No | sells, buys, and a second options scan (`:1663-1688`) |
| Options scan | Mon-Fri 09:45-14:00 (`:224-236`) | `_options_scan_done_today` | at start (`:1378`); cleared by the event check (`:1687`) | No | option orders (`:1464-1484`) |
| Daily summary | Mon-Fri 16:05-16:30 (`:311-320`) | `_daily_summary_sent_today` | at start (`:1116`) | No | none |
| Mid-week analysis | Wed 18:00-20:59 (`:262-272`) | `_midweek_analysis_done_this_week` | at start (`:485-486`) | No | sells at once, after the close (`:1024`); buys queued (`:546`) |
| Weekly analysis | Sun 18:00-20:59 (`:248-260`) | `portfolio.state.last_weekly_analysis`, must be 5+ days old | at the very end, only on success (`:465`) | **Yes** (`portfolio.py:355-357`) | sells at once, market closed (`:1024`); buys queued (`:437`) |

Also held only in memory: the queue of buy signals (`_pending_signals`, `:95`)
and the event check's last-seen SPX, VIX and regime (`:100-103`).
`last_daily_check` and `last_universe_refresh` are written to disk
(`:1097`, `portfolio.py:365`) but nothing reads them.

Within monitoring, the "has anything material happened" LLM check runs only
when the clock hour is even (`:1103-1108`), which in practice means the runs
near 10:00, 10:30, 12:00, 12:30, 14:00 and 14:30.

### Where reconciliation with Alpaca sits in the day

The decision on record is "local ledgers reconcile against Alpaca before risk
checks". In the schedule that means three places only: the start of the Sunday
analysis (`:371`), the start of the Wednesday analysis (`:491`), and the
morning brief (`:1733`, only if Alpaca returned an account and a non-empty
position list). There is no reconcile at process start, none in the tick that
executes the queued buys at 09:30, none before the event check buys during the
day, and none in the monitoring loop. Area 03 owns the detail; F-01-09 covers
the timing.

### What a restart does at each point (question 2)

Checked by building a scheduler, marking every job as done, then building a
second scheduler on the same data directory (run D2 below). After the
"restart" every gate except the weekly one was open again.

| Restart lands | What happens |
| --- | --- |
| Sun 18:00-20:59, weekly still running | Killed part-way (F-01-04). Marker not yet saved, so the weekly starts again from scratch on the first tick. Sells already sent stay sent. |
| After the weekly finished, any time before Mon 09:30 | Queued buys are gone. Weekly will not run again (marker on disk). No message (F-01-01). |
| 06:30-07:30 | Macro check repeats. Harmless. |
| 07:00-08:00 | Earnings scan repeats: same LLM calls, sell attempts repeated. |
| 08:00-09:00 | Pre-market scan repeats (LLM spend). Its queue is rebuilt. |
| 09:00-09:30 | Buys queued by the earnings and pre-market scans are gone and are not rebuilt. |
| 09:30-16:00 | Any job in flight is killed. Queue lost if not yet executed. Monitoring runs on the first tick. Options scan runs again if between 09:45 and 14:00. Event check baseline is reset, so a move that straddles the restart is not seen. |
| 16:05-16:30 | Daily summary sent and stored a second time. |
| Wed 18:00-20:59 | Mid-week analysis repeats in full. |
| Wed 21:00 to Thu 09:30 | Mid-week buys are gone and are not rebuilt. |

A job is skipped for the day (week, for the Sunday job) if the bot is down or
busy for the whole of its window. Nothing catches up later.

### Where each brief question is answered

| Question | Answer |
| --- | --- |
| 1 Timetable and markers | "The real timetable" above |
| 2 Restarts | "What a restart does at each point" above; F-01-01, F-01-04, F-01-08 |
| 3 Long jobs and overlap | Jobs cannot overlap (one coroutine, "Start, loop, stop" step 5). A long job delays every later job and can push others past their windows: F-01-06, F-01-08, F-01-09 |
| 4 Market hours, holidays, half-days, daylight saving | Computed locally, never asked of the broker: F-01-03. Daylight saving is correct ("Checked and fine") |
| 5 Exceptions, retries, alert loops | F-01-07 and F-01-11 |
| 6 SIGTERM | F-01-04 |
| 7 `MODE`, `OPTIONS_ENABLED`, regime gates | F-01-05 |

### Differences from comments, docstrings and README

Listed in F-01-12.

## Findings

Method for the items marked "ran": the real `StockAgentScheduler` methods were
executed against fake collaborators that record calls and return canned values
(no network, no broker), with a real `Portfolio` and `RiskManager` pointed at a
scratch directory and the clock function `_now_et` replaced by fixed times.
Runs are labelled D1 to D10. The shutdown test ran the real `run()` loop and
the signal wiring from `__main__.py` in a child process and sent it SIGTERM.
Nothing was added to the repository.

### F-01-01: Buy orders waiting for the open are lost on any restart, and the weekly job will not recreate them
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:95`, `:433-442`, `:465`, `:192-197`, `:1178-1185`, `:1264-1267`
- What happens: Buy decisions from the Sunday analysis, the Wednesday analysis, the 07:00 earnings scan and the 08:00 scan sit in a Python list until the first tick after 09:30. The list is not saved anywhere. A restart empties it. The Sunday job's "done" marker is saved, so it does not run again. Separately, the queue is emptied after one attempt whatever the outcome.
- Evidence:
  - The queue is a plain attribute: `self._pending_signals: list[Signal] = []` (`:95`).
  - Sunday evening the market is closed, so the weekly always takes the queue branch: `self._pending_signals.extend(signals)` (`:437`), then saves the marker: `self.portfolio.set_last_weekly_analysis(_now_utc())` (`:465`).
  - The gate refuses a second run for five days: `if (now_et - last_et).days < 5: return False` (`:258-259`).
  - One attempt only: `executed = await self._execute_signals(self._pending_signals)` then `self._pending_signals.clear()` (`:196-197`). Inside, a pattern-day-trader stop returns early (`:1178-1185`), a risk block does `continue` (`:1214-1222`), and a failed order does `logger.error("Order execution failed for %s", sym); continue` (`:1265-1267`). None is retried.
  - The first block of `_tick` is commented "First run: execute any pending signals from pre-market scan" (`:148-154`). It can never find any, because the list is always empty on a new process. The author appears to have expected the queue to survive a restart.
- Why it matters: The weekly analysis is the bot's main source of new positions. Sunday's buys wait about 15 hours. A merge, a crash or a reboot in that time means the bot does the research, reports "N signals", and buys nothing, with no alert. Deploys do land in these windows: of the 48 commits on `origin/master` (2026-04-04 to 2026-05-16), by commit time 20 fell in weekday market hours (4 separate days), 9 between Wednesday 18:00 and Thursday 09:30 (1 day) and 1 between Sunday 18:00 and Monday 09:30. Commit time is not exactly push time, so treat these as indicative.
- How verified: Ran D3. Weekly analysis on a fake Sunday queued three BUY signals and sent no orders; the marker was on disk. A new scheduler on the same data directory had an empty queue, `_should_run_weekly` returned False for the rest of Sunday, and the Monday 09:31 tick sent no buy orders. Commit-time counts came from `git log origin/master --format=%ct` converted to Eastern time.
- Direction (not a fix): Queued decisions need to be stored on disk with their age, and "analysis done" should not be recorded separately from "its orders were handled". A signal that fails for a temporary reason needs an explicit rule: retry, expire, or alert.
- Verification: Confirmed: read independently, the queue is only the in-memory list at `scheduler.py:95` with no write to disk anywhere, the weekly marker is saved (`portfolio.py:355-357`) and blocks a re-run for five days, and `:196-197` clears the queue after a single attempt; the 08:00 scan can add its own new signals after a restart but nothing rebuilds the lost ones.

### F-01-02: A sell decided by the pre-market scan is logged as "queued" and then dropped
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:605-624`
- What happens: At 08:00 the scan asks the LLM about overnight news for each holding. For a severe negative event it asks for a fresh verdict. If the verdict is SELL, the code checks whether the market is open; it never is at that hour, so it only writes a log line. No sell is stored, nothing is sent at 09:30, and the saved thesis is not updated.
- Evidence:
  ```python
  if new_thesis and new_thesis.direction == "SELL":
      # If market is open, execute immediately; otherwise queue
      if self._is_market_open(now):
          await self._execute_sell(pos, reason=f"Pre-market event: {event_desc}")
      else:
          logger.info("Queuing sell for %s at market open: %s", pos.symbol, event_desc)
  elif new_thesis:
      self.portfolio.update_thesis(pos.symbol, new_thesis)
  ```
  `now` is taken at the start of the scan (`:570`) and the scan only starts between 08:00 and 09:00 (`:283`), so `_is_market_open(now)` is always False here and the first branch is dead. The queue only ever holds BUY signals: every `Signal(` constructed in the file has `action="BUY"` (`:655`, `:772`, `:987`), and `_execute_signals` skips anything else (`:1188-1189`).
- Why it matters: This is the one job designed to react to bad overnight news before the open, and its exit path does not exist. The position stays until the price hits the stop, or until a later monitoring run happens to raise the same event again. Those event checks only run in even clock hours (`:1103-1108`), so the earliest is about 10:00.
- How verified: Ran D4. With a fake "severity 9, negative" event and a fake SELL verdict at 08:02: the verdict was requested, zero sell orders were sent, the queue was empty, the stored thesis was unchanged, and after the 09:31 tick the position was still held with no sell order.
- Direction (not a fix): Either give sells a real queue that is executed at the open, or make this path an explicit alert to Josh. The log line should not claim something that does not happen.
- Verification: Confirmed: the path is live (`_tick` `:177-179`), `now` is fixed at `:570` inside the 08:00-09:00 window so the sell branch at `:614` cannot run, the else branch only logs, and no later guard picks the sell up (the queue executor skips anything that is not a BUY at `:1188`); the only fallbacks are the stop-loss and a later even-hour event check that depends on the LLM flagging the same event again.

### F-01-03: The bot guesses when the market is open; holidays, early closes and after-hours order paths are not handled
- Severity: High
- Confidence: Confirmed for what the bot sends and records; what Alpaca does with those orders is not verified
- Where: `stock_agent/scheduler.py:60-61`, `:297-302`, `:192-197`, `:1022-1026`, `:725-729`, `:1593`, `:1679-1688`; `stock_agent/executor.py:95-101`, `:145-151`
- What happens: "Market open" is `weekday < 5 and 09:30 <= time <= 16:00`. There is no holiday list, no early-close list, and no call to the broker's clock or calendar (a search for `v2/clock`, `v2/calendar` and `holiday` in `stock_agent/` finds only a comment in `options_portfolio.py:293`). Three more paths place orders without even this check.
- Evidence:
  - `MARKET_OPEN = time(9, 30)`, `MARKET_CLOSE = time(16, 0)` (`:60-61`); `_is_market_open` (`:297-302`).
  - Holidays: on a closed weekday the queue is executed as normal (`:194-197`). `place_buy` posts a `market` / `day` order, sleeps 3 seconds, then posts a stop-sell for the same quantity (`executor.py:95-127`). If the POST returns anything, the scheduler books the position at the signal's price and debits cash (`:1280-1303`).
  - Early closes: after 13:00 on a half day the monitoring loop keeps running on prices that no longer move, and any sell it decides is sent to a closed market.
  - Sells outside hours: `_re_analyze_existing_positions` calls `_execute_sell` whenever the verdict is SELL (`:1022-1026`). It is called from the weekly (Sunday 18:00 onward, `:430`) and mid-week (Wednesday 18:00 onward, `:540`) jobs. The earnings scan does the same between 07:00 and 08:00 (`:725-729`). `place_sell` is `market` / `day` (`executor.py:145-151`). On a non-empty response `_execute_sell` removes the position and credits cash at the last quote straight away (`:1345-1346`, `portfolio.py:84-89`).
  - Stale clock in the event check: `now = _now_et()` is taken once at the top (`:1593`) and reused after the full rescan to decide `if self._is_market_open(now): executed = await self._execute_signals(signals)` (`:1679-1680`) and to re-run the options scan (`:1686-1688`). An event found at 15:40 whose analysis ends after 16:00 still sends orders. The weekly and mid-week jobs call `_now_et()` afresh at the same point (`:433`, `:543`).
- Why it matters: On a holiday the local ledger shows positions and spent cash that do not exist at the broker until at least the next session, and every risk check that day uses that false picture. Sunday's signals are the usual victims because several holidays are Mondays. Sells sent on Sunday evening are booked as done about 15 hours before they can fill, at a price that will not be the fill price. Upcoming dates: Thursday 2026-11-26 (closed), Friday 2026-11-27 (closes 13:00), Thursday 2026-12-24 (closes 13:00), Friday 2026-12-25 and Friday 2027-01-01 (closed).
- How verified: Ran D1: `_is_market_open` returned True for 2026-11-26 10:00, 2026-11-27 14:30 and 2026-12-25 11:00, and the pre-market, brief, options-scan and summary gates were all open on 2026-11-26. Ran D6: a queued BUY at 09:31 on 2026-11-26 produced `place_buy('AAA', 70, 93.0)`, a ledger position of 70 shares at $100 and cash down by $7,000. The sell paths and the stale clock were traced by reading.
- Direction (not a fix): Ask the broker whether the market is open and when it next opens and closes, and make every order path go through that one answer. Decide deliberately what a sell decided outside hours should do.
- Verification: Confirmed: no broker clock, calendar or holiday check exists anywhere in `stock_agent/`, the open-hours rule returned True for 2026-11-26 10:00, 2026-11-27 14:30 and 2026-12-25 11:00 when re-run, and the three unguarded order paths (`:1022-1026`, `:725-729`, the stale `now` at `:1679-1688`) read as described; what Alpaca does with orders sent to a closed market is still untested, and the first affected date (Thursday 2026-11-26, the morning after a Wednesday analysis) is days away.

### F-01-04: Shutdown is not graceful: the bot keeps placing orders after SIGTERM and is killed mid-job; nothing reconciles at start
- Severity: High
- Confidence: Confirmed for the mechanism; the 90-second limit is the systemd default and the droplet's actual value is not verified
- Where: `stock_agent/__main__.py:16-22`; `stock_agent/scheduler.py:105-107`, `:126-144`, `:109-124`; `deploy/stock-agent.service:6-16`; `stock_agent/executor.py:104-127`; `stock_agent/options_executor.py:160-176`
- What happens: SIGTERM only sets `self._shutdown = True`. The flag is read in exactly one place, the `while` condition, which is reached only after the current tick has finished **and** a further fixed 60-second sleep has ended. Until then the bot behaves normally. The unit file sets no stop timeout, so systemd's default applies (90 seconds unless changed on the server), after which the process is killed outright.
- Evidence:
  ```python
  while not self._shutdown:
      now = _now_et()
      try:
          await self._tick(now)
      ...
      await asyncio.sleep(60)
  ```
  (`scheduler.py:127-142`). `_shutdown` appears nowhere else except `:88` and `:107`. `deploy/stock-agent.service` has `Restart=always` and `RestartSec=30` but no `TimeoutStopSec`. So exit takes (work left in this tick) + up to 60 seconds, and any job with more than about 30 seconds left is killed.
  Places where a kill leaves broker and ledger disagreeing:
  - Share buy: POST buy (`executor.py:104`), `await asyncio.sleep(3)` (`:115`), POST stop (`:127`), then the ledger write back in the scheduler (`scheduler.py:1280-1303`). A kill in between leaves shares with no stop order and no ledger entry.
  - Share sell: POST sell (`scheduler.py:1323`), ledger update later (`:1345-1346`).
  - Option spread: two separate orders, long leg then short leg (`options_executor.py:160-176`).
  After the restart `run()` goes straight from the startup message to `_backfill_sectors()` and the loop (`scheduler.py:109-127`). There is no call to `_sync_portfolio_from_alpaca` on start, so the gap stays until the next morning brief or the next Sunday or Wednesday job.
- Why it matters: Every merge to `master` triggers this within 10 minutes, at any time of day. Cron fires on the :00/:10/:20/:30 marks, so a deploy picked up at 09:30 arrives while the queued buys are being placed. A position bought but not recorded has no stop at the broker, is not watched by the monitoring loop, and is invisible to the risk check; a later buy signal for the same symbol would be treated as a brand-new position. The clean "shutting down" message is only sent on a graceful exit (`:143-144`, `:1972-1982`), so a kill is silent.
- How verified: Ran the real `run()` loop with the handlers from `__main__.py` in a child process. Idle case: SIGTERM at 2.7 s, exit at 60.0 s (57.3 s later). Busy case, with a tick standing in for a 40-second job that "sends" something every 5 seconds: all eight sends happened after the flag was already True, and the process exited 97.4 s after SIGTERM, past the 90-second default. The kill windows were traced by reading.
- Direction (not a fix): Make the stop request take effect quickly and at safe points only: never between an order and its stop or its ledger entry, and no new orders once stop has been requested. Reconcile with the broker on every start. Consider whether deploys should wait for a quiet moment (question for Josh below).
- Verification: Confirmed: re-ran the real `run()` loop with every collaborator stubbed and SIGTERM at 2 s (idle: exit at 60 s; a 30-second job: all six later "sends" happened with the flag already set, exit at 90 s), and `run()` has no reconcile before the loop; the unprotected-position outcome additionally needs the kill to land in the few seconds between a buy and its stop or ledger write, so it is real but should be rare per deploy.

### F-01-05: `OPTIONS_ENABLED` and `STOCK_AGENT_MODE` switch nothing, and the macro regime gate is applied to some buy paths only
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/config.py:44`, `:201`; `stock_agent/scheduler.py:111-117`, `:224-236`, `:203`, `:1686-1688`, `:650`, `:769`, `:984`
- What happens (answers question 7):
  - `OPTIONS_ENABLED` is defined at `config.py:201` and read nowhere. A search of `stock_agent/` finds only that line. The options scan gate (`scheduler.py:224-236`) checks weekday, marker and time; the options position check (`:203`) and the event-triggered second scan (`:1686-1688`) have no switch either. Setting `OPTIONS_ENABLED=false` changes nothing.
  - `MODE` (`STOCK_AGENT_MODE`) is used only for the startup log and startup messages (`scheduler.py:111`, `:115`, `:117`) and the Discord footer (`discord_alerts.py:50`, `:89`). No job, order path or limit reads it. Paper or live is decided only by `ALPACA_BASE_URL` and the keys. The README says "live mode flips a single config flag" (`README.md:17`); that flag is a label.
  - Macro regime, by path:

    | Buy path | Minimum conviction used | Source |
    | --- | --- | --- |
    | Weekly, mid-week, event rescan (`_deep_analyze`) | regime value: 6 in the two "dip" regimes, 7 normal, 8 cautious | `scheduler.py:984`, `macro_monitor.py:335-354`, `:369-373` |
    | Pre-market scan | fixed 8 | `scheduler.py:650` |
    | Earnings scan | fixed 8 | `scheduler.py:769` |
    | Execution of the queue | none; the regime is not looked at again | `scheduler.py:1173-1314` |

    The regime's "scan depth" (40 to 80) is only printed in the morning brief (`scheduler.py:1867`, `:1873`); the screener always uses `UNIVERSE_SIZE` (`data_feed.py:291`, `:330`). The cautious regime's description says "tighten stops, reduce new entries" (`macro_monitor.py:41`); no code tightens a stop.
  - Other switches: `EARNINGS_CHECK_ENABLED` is honoured (`scheduler.py:289`) but is hard-coded `True` with no environment override (`config.py:180`). `TIPRANKS_ENABLED` is honoured (`scheduler.py:827`).
- Why it matters: There is no working way to turn options trading off short of stopping the whole bot. If Josh ever sets `OPTIONS_ENABLED=false` expecting options orders to stop, they will continue; from that moment this is a Critical-class problem (unintended orders). Today the default is `true`, so behaviour matches the default. A mode flag that only relabels messages means the messages can say PAPER while the base URL is live, or the reverse.
- How verified: `grep -rn OPTIONS_ENABLED stock_agent/` returns only `config.py:201`; `grep -rn "\.MODE\b"` returns only the three scheduler lines; the two `getattr(self.config, "MODE", ...)` uses are in `discord_alerts.py`. Read every gate. Ran `MacroMonitor._compute_regime` with made-up inputs to confirm the 6 / none / 8 overrides.
- Direction (not a fix): Every switch in `config.py` should either gate something or be removed. Decide which switches Josh actually wants (options on/off; no new buys but exits continue; paper/live with a check that the label and the URL agree).
- Verification: Confirmed: `OPTIONS_ENABLED` is read nowhere outside `config.py:201` and `MODE` only feeds log lines and message labels, but nothing misbehaves while the defaults stand, and the fixed conviction of 8 on the pre-market and earnings paths is never looser than the regime gate (which tops out at 8), so that part is an inconsistency rather than a bypass. Suggested severity: Medium, rising to Critical if the droplet's `.env` already sets `OPTIONS_ENABLED=false` (not checkable from the repository; the variable is not in `.env.example`).

### F-01-06: The event-triggered rescan runs a full research-and-trade cycle inline during market hours, can be set off by missing quotes, and has no daily limit
- Severity: High
- Confidence: Likely (mechanics confirmed by reading and by running the regime function; how often it fires and how long it takes need the logs)
- Where: `stock_agent/scheduler.py:199-205`, `:1578-1696`; `stock_agent/macro_monitor.py:132-146`, `:186-229`, `:264-356`
- What happens: After every monitoring run the bot fetches index quotes and compares them with the previous run. If the S&P moved 1.5%, VIX moved 3 points, or the regime label is different, it immediately re-analyses every holding (which can sell), re-screens the universe, runs deep analysis on the top names, executes any buys, and re-runs the options scan. All of that is awaited inside the same tick.
- Evidence:
  - Inline: `await self._check_for_market_event()` (`:205`), which awaits `_fetch_fundamentals`, `_re_analyze_existing_positions`, `screen_universe`, `screen_candidates`, `_deep_analyze`, `_execute_signals` and `_run_options_scan` in sequence (`:1663-1688`). No other tick can start until it returns, so no monitoring, no stop-loss check and no daily summary happen meanwhile.
  - Triggered by data gaps: a failed quote returns `None` (`macro_monitor.py:132-146`), the snapshot field stays `0.0` (`:186-229`), and the regime is computed anyway. A regime label that differs from last time is an event (`scheduler.py:1626-1629`).
  - Repeats: the only limit is a 30-minute cooldown (`:1594-1597`), which is the monitoring interval anyway. Each firing clears the options scan marker and runs it again: `self._options_scan_done_today = None` (`:1687`).
  - It uses the time it started to decide whether the market is still open (F-01-03).
  - It calls `_deep_analyze(top_symbols, companies)` without the TipRanks data the weekly passes (`:1677` against `:427`), and does not reconcile with Alpaca before buying.
- Why it matters: This is the only path where the bot decides on its own, mid-session, to buy and sell across the whole book, and it is set off by a comparison that can be wrong for data reasons. On a genuinely volatile day it can fire on consecutive monitoring runs, each costing a full set of LLM calls and each able to open more options positions up to the caps. While it runs, the 30-minute loop is not running; the resting stop orders at the broker are the only protection in that time.
- How verified: Read the path end to end. Ran `_compute_regime` with made-up numbers: a "fear day" snapshot scored AGGRESSIVE_DEPLOY; the same snapshot with the VIX quote missing scored DIP_OPPORTUNITY; with every quote missing it scored NORMAL. Each change would count as an event. Lower bound on duration from hard-coded waits: each fundamentals fetch includes one Finnhub call followed by a 1.1-second sleep (`data_feed.py:66`, `:181-184`), so about 50 universe symbols alone take roughly a minute before any LLM call (timeouts 60 s for screening, 120 s for analysis: `screener.py:22`, `analyst.py:22`).
- Direction (not a fix): Decide whether the bot should trade on intraday events at all. If yes, the trigger needs to ignore missing data, the work needs to stay out of the way of the exit loop, and there should be a daily cap.
- Verification: Confirmed: the rescan is awaited inline from `_tick` (`:205`) with only the 30-minute cooldown, and re-running `_compute_regime` showed a single missing VIX or S&P quote changes the label (CAUTIOUS to NORMAL in a calm market above the hard-coded 6,653 level; AGGRESSIVE_DEPLOY to DIP_OPPORTUNITY on a fear day), and so does VIX merely crossing 15 with no data fault at all; how often it fires and how long it blocks the loop still need the droplet logs.

### F-01-07: A failed weekly analysis starts again every minute for three hours; every other job gives up after one failure
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:248-260`, `:361-474`, `:485-486`
- What happens: The weekly job records "done" only on its last line. If it raises, or leaves through either early `return`, the marker stays unset and the gate is true again on the next tick. That repeats until 21:00. Each attempt sends a "Weekly Analysis Starting" message and a failure or warning message to both Telegram and Discord. Every other job sets its marker before doing any work, so one failure means it is skipped for the day (or week) with no retry.
- Evidence: Early exits before the marker: `return` at `:385` (no symbols) and `:413` (no fundamentals), and the `except` at `:468-474`. Marker at `:465`. Compare `self._midweek_analysis_done_this_week = week_key` as the second statement of the mid-week job (`:485-486`). When the data provider is down the screener still returns the four priority symbols (`data_feed.py:337-344`), so the path taken is "No fundamental data retrieved" (`:407-413`). A late failure is costlier: an unvalidated LLM row makes the ranking raise (`r["symbol"]` at `:417`, or the sort at `screener.py:99`) after the full fundamentals fetch has been paid for.
- Why it matters: A provider outage or quota limit on a Sunday evening turns into a few hundred messages and a repeated full fetch each time, on a 1 vCPU server with metered APIs. The opposite behaviour elsewhere means a single timeout at 18:00 on Wednesday cancels that week's mid-week review with one Discord message.
- How verified: Ran D5. With fundamentals returning nothing and ticks 90 seconds apart (an assumed pace), the weekly ran 120 times between 18:00 and 20:59 and sent 240 Telegram and 240 Discord messages. The mid-week job with a forced failure ran once and never again. Confirmed in a Python shell that sorting rows where one `composite` is a string raises `TypeError`, and a row without `symbol` raises `KeyError`.
- Direction (not a fix): One rule for all jobs: a bounded number of retries with a growing gap, and one alert per failure episode rather than one per attempt.

### F-01-08: "Already ran today" markers live in memory, so a restart inside a job's window runs it again, and a busy or stopped bot skips it
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:89-98` and the marker lines in the timetable above; `:1112-1160`; `stock_agent/portfolio.py:345-348`
- What happens: Eight of the nine markers are attributes reset by the constructor. After a restart inside the window the job runs again. If the bot is down, or one long job holds the single loop for the whole window, the job is skipped with no catch-up.
- Evidence and consequences per job:
  - Options scan (09:45-14:00): runs again and can send new option orders. It skips underlyings already in the local options file (`:1415-1429`) and the engine applies its count and exposure caps, so a re-run adds positions up to the caps rather than duplicating one.
  - Pre-market scan, earnings scan, mid-week analysis: all LLM and data calls are paid for again; the earnings and mid-week jobs can attempt their sells again.
  - Daily summary: `self.portfolio.add_daily_summary(summary)` (`:1154`) appends a second row for the same day. The second message compares against the first (`:1122-1126`) and shows a day P&L of about zero, and the stored history gets an extra flat day.
  - Skips: the summary window is 25 minutes and the brief window 30 minutes. A monitoring run near 15:55 that sets off the event rescan (F-01-06) can outlast 16:30, and then no summary is stored, so the next day's "day P&L" covers two days.
  - The brief's "Bot Activity" recap is written from the day of the week, not from what ran (`:1828-1837`), so it reports jobs as completed even when they were skipped.
- Why it matters: Wasted API spend, duplicated or missing reports, and extra option orders after a mid-day deploy. The weekly job shows the opposite failure (F-01-01): its marker survives but its output does not.
- How verified: Ran D2 (table in "What a restart does"). Read each job's marker line.
- Direction (not a fix): Keep one small saved record of what ran and when, and decide per job whether a missed run should be caught up.

### F-01-09: The only weekday reconcile with Alpaca is a side effect of the morning brief, and there is none at start-up or before orders
- Severity: Medium
- Confidence: Confirmed for where the calls are; whether the brief is ever crowded out needs the logs
- Where: `stock_agent/scheduler.py:109-124`, `:238-246`, `:1716-1733`, `:192-197`, `:1680`, `:1032-1053`
- What happens: Outside the Sunday and Wednesday jobs, the ledger is only reconciled inside `_send_morning_brief`, a Discord report with a 30-minute window (08:15-08:45), and only when Alpaca returned an account and a non-empty position list. The 09:30 execution of queued buys, the intraday event buys, and every monitoring run use the ledger as it stands.
- Evidence: `if alpaca_positions: self.portfolio.sync_from_alpaca(alpaca_positions, alpaca_account)` (`:1732-1733`) is the only weekday call. The brief's gate is `time(8, 15) <= current_time <= time(8, 45)` (`:246`). The pre-market scan starts at 08:00 and is awaited inline; if it is still running at 08:45, or the bot restarts after 08:45, the brief and its reconcile do not happen that day. `run()` has no sync before the loop (`:109-127`).
- Why it matters: The recorded lesson from the 76% incident is that risk checks must see broker-reconciled positions. On most paths the freshest reconcile is 75 minutes old at best, and on days the brief does not run it is the previous day's. Anything that changed at the broker in between (a stop order filling, an order from before a kill) is unknown to the risk check.
- How verified: Listed every call to `sync_from_alpaca` and `_sync_portfolio_from_alpaca` (`:371`, `:491`, `:1733`) and read the paths that place orders.
- Direction (not a fix): Reconcile as its own step: at start, and immediately before any batch of orders. Area 03 should own the design.

### F-01-10: Queued buys are sized, stopped and recorded at the price seen during analysis, and the same tick can then stop them out
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:990`, `:1201-1208`, `:1252-1264`, `:1280-1303`, `:192-205`
- What happens: A signal carries `entry_price=company.price` from when it was created (Sunday evening means Friday's close). At 09:30 the executor uses that number as long as it is above zero; a fresh quote is fetched only when it is missing. Share count, stop price and the ledger's entry price all come from it. The signal's `generated_at` is never read, so there is no age limit. Straight after the buys, the same tick runs monitoring with live quotes.
- Evidence: `price = signal.entry_price or 0` / `if price <= 0: quote = await self.data_feed.get_stock_quote(sym)` (`:1201-1203`); `qty = self.risk_manager.calculate_position_size(pv, price, signal.conviction)` (`:1252`); `stop_loss = self.risk_manager.get_stop_loss_price(price, beta)` (`:1259`); `entry_price=price` (`:1283`). Monitoring follows in the same tick (`:199-201`).
- Worked numbers (conviction 8, $100,000 portfolio, analysis price $100): 70 shares, stop $93.00.
  - Opens at $92: the bot buys 70 and sends a stop at $93.00, above the market. The ledger says entry $100. Monitoring then sees $92 <= $93 and sells all 70 in the same minute. Booked result: a $560 loss and one day trade.
  - Opens at $106: 70 shares cost $7,420 (7.4%, not 7%), and the $93 stop is 12.3% below the fill, outside the 7-12% band.
- Why it matters: Every buy at the open ignores the overnight move; after a weekend that move can be large. The gap-down case produces a pointless round trip and a stop order the broker may refuse.
- How verified: Ran D7 with the real `_tick`, `_execute_signals`, `RiskManager` and `Portfolio`: orders were `place_buy('AAA', 70, 93.0)` then `place_sell('AAA', 70)`, and the trades booked were BUY 70 at 100.0 and SELL 70 at 92.0 with reason "Stop-loss triggered".
- Direction (not a fix): Re-price and re-check a queued signal at the moment of execution, and give signals a maximum age. Sizing and stop details belong to areas 02 and 03.

### F-01-11: An error while placing the queued buys repeats every minute, alerts every time, and holds up the stop-loss loop
- Severity: Medium
- Confidence: Confirmed for the mechanism; the triggers found are uncommon
- Where: `stock_agent/scheduler.py:129-142`, `:192-205`
- What happens (answers question 5): Every scheduled job wraps its own body in `try/except`, so a failing job does not stop later jobs in the same tick. The exception is the execution of queued buys, which `_tick` calls bare. If it raises, the queue is not cleared, the rest of the tick (monitoring, options check, event check, summary) is skipped, `run()` sends an error alert to Telegram and Discord, and the same thing happens 60 seconds later. There is no back-off and no de-duplication.
- Evidence: `executed = await self._execute_signals(self._pending_signals)` then `self._pending_signals.clear()` (`:196-197`) with no `try`. The catch is in `run()` (`:131-140`), followed by `await asyncio.sleep(60)`. Things that can raise inside `_execute_signals`: a state save failing after a buy (`portfolio.py:65-75` re-raises), or a trade timestamp without a time zone in old state meeting the comparison in `risk_manager.py:176`.
- Why it matters: While it lasts, the software stop-loss check does not run during market hours and Josh gets an alert a minute. In the save-failure case each retry places one more real buy that is never written to trade history.
- How verified: Ran D10. Three queued signals with the state save forced to fail: ticks 1 to 3 each placed one new buy and raised; monitoring ran zero times until tick 4; trade history stayed empty. Ran D9 to confirm the retry does not double-buy while the process stays up: a symbol queued twice produced one order, the second being blocked with "Already holding AAA at 7.0% (target 7%)".
- Direction (not a fix): Treat order execution like every other job (contained failure, one alert), and never let a failure in one step keep the exit loop from running.

### F-01-12: Schedule comments, docstrings and settings do not match the code, and some schedule code is dead
- Severity: Low
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py` and `stock_agent/config.py`, lines below
- What happens: The written description of the timetable is wrong in several places, and two schedule settings do nothing.

  | Says | Does |
  | --- | --- |
  | `_tick` comment "Macro conditions check: Mon-Fri, 7-8 AM ET" (`:156`) | 06:30-07:30 (`:222`) |
  | `_tick` comment "Options scan: Mon-Fri, 9:00-9:30 AM ET (after prescan, before market open)" (`:181`) | 09:45-14:00 (`:236`) |
  | "between 6-8 PM ET" for weekly and mid-week (`:161`, `:166`, `:249`, `:263`) | `18 <= now_et.hour <= 20`, which is 18:00-20:59 (`:252`, `:266`) |
  | `_should_check_events`: "every other monitoring cycle" (`:1104`) | both runs in even clock hours, none in odd hours (`:1108`); its first branch can never be taken because `_last_monitor_time` was set at `:1035` |
  | "belt-and-suspenders with Alpaca bracket orders" (`:1046`) | no bracket orders are used (`executor.py:83-89`) |
  | `DAILY_PRESCAN_HOUR = 8`, `MARKET_CLOSE_HOUR = 16` (`config.py:175-176`) | never read; the times are literals in the scheduler |
  | `_first_run_done` block, "execute any pending signals from pre-market scan" (`:148-154`) | the queue is always empty on the first tick |
  | "If market is open, execute immediately" in the pre-market scan (`:613-617`) | unreachable (F-01-02) |
  | weekly and mid-week `if self._is_market_open(_now_et())` execute branch (`:433-434`, `:543-544`) | unreachable at 18:00 on a Sunday or Wednesday |
  | `CLAUDE.md` "during market hours ... options, macro, and earnings-reactive scans" | macro (06:30) and earnings (07:00) run before the open |

- Why it matters: Anyone changing the schedule from the comments or from `config.py` will change the wrong thing.
- How verified: Read each line; ran D1 for the window edges (18:00 True, 20:59 True, 21:00 False).
- Direction (not a fix): One table of job times in one place, with the comments generated from or checked against it.

### F-01-13: Nothing in the code stops two copies of the bot running against the same account
- Severity: Low
- Confidence: Confirmed that no guard exists; whether a second copy has ever run is not verified
- Where: `stock_agent/__main__.py:8-22`; `deploy/stock-agent.service`
- What happens: There is no lock file, PID file or other single-instance check (a search for `flock`, `fcntl`, `getpid`, `.lock` and `pidfile` in `stock_agent/` and `deploy/` finds nothing). On the droplet, systemd runs one instance of the unit and `systemctl restart` stops the old process before starting the new one, so the normal deploy path cannot overlap. A copy started by hand, or on another machine with the same keys, would trade the same account with its own queue and its own ledger.
- Why it matters: Two schedulers would each size positions as if they were alone. This needs a manual mistake, which is why it is Low.
- How verified: Search, and reading the unit file and `auto_update.sh`.
- Direction (not a fix): A cheap guard at start (a lock in the data directory) covers the same-machine case. The cross-machine case is a process rule.

## Checked and fine

- Daylight saving: times come from `datetime.now(ET)` with `zoneinfo` (`scheduler.py:50-65`). D1 showed 14:30 UTC in winter and 13:30 UTC in summer both map to 09:30 and "open". The server's own time zone does not affect the schedule.
- Jobs cannot overlap or race: there is a single coroutine and no background tasks or threads (search above). Loops that sell while iterating take a copy first (`:583`, `:704`, `:1005`, `:1057`).
- The weekly analysis cannot run twice in a week once it has finished: marker on disk, five-day guard (`:255-259`; D2).
- A restart during market hours does not delay monitoring: `_last_monitor_time` is empty on a new process, so the first tick runs it (`:306-307`; D2).
- After a restart the event check takes a baseline first and cannot fire on its first run (`:1610`, `:1618`, `:1626` all require a previous value).
- Lead "long jobs block the 30-minute loop": the weekly and mid-week jobs run after the close and do not block it. The options scan and the event rescan do (F-01-06).
- Lead "Sunday and Wednesday analyses re-run after a restart": the Sunday one does not once complete; the Wednesday one does (F-01-08). The pre-market scan does too.
- Lead "unit and cron both reset the code": confirmed and described in "Start, loop, stop". The effect on the process is one restart per new commit plus a reset on every start. Consequences for deploys are handed to area 09.
- A symbol queued twice (for example by the weekly and the pre-market scan) is bought once: the second is blocked at 90% of target (`risk_manager.py:58-62`; D9).
- Every window is at least 25 minutes wide, so the 60-second tick cannot step over one on its own.
- `portfolio.json` is written to a temporary file and renamed (`portfolio.py:44-65`), so a kill during a save does not leave a half-written file.
- A crash is followed by an automatic restart after 30 seconds (`deploy/stock-agent.service:14-15`), and the unit is enabled at boot (`deploy/setup.sh:49`).
- Each scheduled job contains its own errors (`:358`, `:468`, `:559`, `:684`, `:802`, `:1100`, `:1163`, `:1519`, `:1573`, `:1695`, `:1934`), so one failing job does not skip the others in that tick. The exception is F-01-11.

## Could not verify

- **How long each job really takes** (weekly, mid-week, pre-market, earnings, options scan, event rescan), and so how often the brief, the summary or the monitoring loop is crowded out. Needs: the service journal from the droplet snapshot, with the "Starting ..." and "... complete" lines and their timestamps.
- **How often restarts happen, when, and whether any ended in a kill.** Needs: `/var/log/auto_update.log` and journal lines for "Stock Agent starting" and systemd's "timed out. Killing" from the snapshot.
- **The real stop timeout on the server.** Needs: `TimeoutStopUSec` from `systemctl show stock-agent` (the snapshot's `service/show.txt`).
- **What Alpaca does with the orders this schedule produces while closed**: a market/day buy on a holiday, a stop-sell sent 3 seconds later for shares not yet held, a market/day sell on Sunday evening or at 07:00, and whether the account allows short selling. Needs: an Alpaca order-history export covering a holiday and a Sunday evening (statuses only), or confirmation from Alpaca's documentation by area 03.
- **Whether queued buys have actually been lost.** Needs: journal lines "queued N signals for next open" compared with the buys on the following session.
- **Whether `OPTIONS_ENABLED` or `STOCK_AGENT_MODE` is set to a non-default value in the droplet's `.env`.** Needs: Josh to say (names only, no values beyond true/false or PAPER/LIVE).
- **Whether more than one bot process, or a leftover service from the earlier bot, is running.** Needs: the snapshot's `system/python-processes.txt` and the enabled-services list.
- **Whether old trade records in `portfolio.json` have timestamps without a time zone** (one of the two triggers in F-01-11). Needs: the shape of the real state file from the snapshot.

## Questions for Josh

1. If the bot restarts between Sunday's analysis and Monday's open, should it still place Sunday's buys? If yes, how old may a buy decision be before it is thrown away?
2. When the 08:00 scan concludes "sell", do you want the bot to sell at the open by itself, or to alert you and wait?
3. Should the bot trade on its own in the middle of a volatile day (the event-triggered rescan), or should a big market move only raise an alert?
4. Is options trading meant to be on? Do you want a working off-switch for it, and a separate "no new buys, exits only" switch?
5. When a sell is decided on Sunday or Wednesday evening, or at 07:00, should the order go to the broker straight away or wait for the open?
6. Is it acceptable for a merge to restart the bot during market hours, or should deploys wait until the market is closed?
7. If a scheduled job is missed because the bot was down, should it catch up when the bot comes back, or wait for the next scheduled time?
8. For a repeating error, is one alert and then silence until it clears what you want?

## Handoffs

- 02: The two "dip" regimes lower the minimum conviction to 6 (`macro_monitor.py:337`, `:342`) while `MIN_CONVICTION` is 7; `calculate_position_size(100000, 100, 6)` returns 50 shares, a full 5% position.
- 02: `Thesis` has no `beta` field (`models.py:58-72`), so `getattr(signal.thesis, "beta", None)` (`scheduler.py:1258`, `risk_manager.py:215`) is always `None` and every stop is the 7% floor.
- 03: `_execute_sell` reads `self._tipranks_lookup` (`scheduler.py:1349`), which is never assigned. Ran D8: the sell order goes out, the ledger is updated, then an `AttributeError` turns every successful sell into a "Sell failed" alert and no trade alert is sent.
- 03: A software sell does not cancel the resting stop order (`executor.py:136-161`), and the monitoring loop can try to sell a position the broker's stop has already closed; `sync_from_alpaca` ignores quantities at or below zero (`portfolio.py:160-162`, `:180`), so an accidental short would not be seen. Suspected, not shown.
- 03: In monitoring, prices are saved to disk before the stop check (`scheduler.py:1044` then `:1047`; `portfolio.py:118`, `:75`), so a failed save cancels that run's stop-loss check with a log line only.
- 04: `place_spread_order` returns a two-item tuple (`options_executor.py:178`) and the scheduler tests `if order:` (`scheduler.py:1486`), which is true even when both legs failed.
- 04: The options reconcile marks a position closed when it is not yet at Alpaca (`options_portfolio.py:507-510`), which removes it from the "already have this underlying" guard that protects re-runs of the options scan.
- 04: The "Paper trading:" relaxations in `options_engine.py:195` and `:216` apply whatever `MODE` says.
- 05: LLM screener rows are returned unvalidated (`screener.py:143-145`); a row without `symbol` or with a non-numeric `composite` raises in the weekly job (`scheduler.py:417`, `screener.py:99`).
- 06: Macro quotes that fail are left at zero and the regime is still computed and saved (`macro_monitor.py:132-146`, `:186-229`).
- 07: `macro_state.json` and `macro_history.json` are written without a temporary file (`macro_monitor.py:393`, `:416`); the history keeps 90 entries but gains one every 30 minutes in market hours, so it spans about a week.
- 07: A restart between 16:05 and 16:30 stores a second `DailySummary` for the day (`portfolio.py:345-348`); "trades today" compares a UTC date with an Eastern date (`scheduler.py:1132-1136`), which misfiles the evening sells.
- 08: The weekly report is always given an empty signal list because the buys are queued, not executed (`scheduler.py:438`, `:451`; seen in D3). Failures in monitoring, macro check, options check, event check and the brief are log-only (`:1100`, `:358`, `:1573`, `:1695`, `:1934`). The per-tick error alert has no throttle (`:131-138`). A crash loop sends a "started" message every 30 seconds or so (`:113-119`).
- 09: Because the unit resets the code on every start (`deploy/stock-agent.service:11-12`), a crash-restart can pick up a new commit without `pip install`; cron then sees no difference and never installs. The service also cannot start while GitHub is unreachable. No `TimeoutStopSec` is set. `README.md:17` describes `MODE` as the live switch.
- 10: Unused: `DAILY_PRESCAN_HOUR`, `MARKET_CLOSE_HOUR`, `LOG_LEVEL` (`config.py:175-176`, `:214`), the `_first_run_done` block (`scheduler.py:148-154`), the three `REGIME_*` imports (`scheduler.py:23`), and the saved fields `last_daily_check` and `last_universe_refresh`.
