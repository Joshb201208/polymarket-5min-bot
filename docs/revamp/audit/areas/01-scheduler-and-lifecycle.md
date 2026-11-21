# 01. Scheduler and lifecycle

Output: `docs/revamp/audit/findings/01-scheduler-and-lifecycle.md`

## Scope

`stock_agent/__main__.py`, `stock_agent/scheduler.py` (the `run` loop, `_tick`,
every `_should_*` gate, and how each `_run_*` job is triggered), plus the
schedule constants in `stock_agent/config.py`. Read `deploy/auto_update.sh`
and `deploy/stock-agent.service` only to understand when the process restarts.

The inside of each job (analysis, execution, options) belongs to other areas.
This area is about **when things run, in what order, and what happens when the
process stops and starts**.

## Why it matters

The bot is one process that wakes every 60 seconds and decides what is due.
It is restarted on every deploy and after every crash. A job that runs twice
can double an order; a job that never runs can miss an exit.

## Questions to answer

1. Draw the real timetable: every job, its trigger condition, its time zone,
   and what stops it running twice. Which of those "already ran" markers are
   saved to disk and which live only in memory?
2. What happens on a restart at each point in the day? For every job: can it
   be skipped, and can it run a second time? Pay most attention to jobs that
   place orders.
3. What happens if a job takes longer than its interval, or longer than the
   60-second tick? Can two jobs overlap, and do they share state safely?
4. How are market hours, weekends, US market holidays, half-days, and daylight
   saving handled? Is "market open" computed locally or asked of the broker?
5. If one job raises an exception, what else in that tick is skipped? Is
   anything retried? Could a failing job repeat every minute and send an alert
   every time?
6. What does shutdown do on SIGTERM? Can it interrupt an order sequence
   half-way (for example after a buy but before its stop is placed)?
7. Which jobs are gated by `MODE`, `OPTIONS_ENABLED`, or the macro regime, and
   are those gates applied consistently?

## Leads to check (unverified)

- The service unit and the cron script both reset the code and restart the
  process. Check what a restart in the middle of market hours does to the
  monitoring loop and to any job that was running.
- Check whether the Sunday and Wednesday analyses can re-run after a restart on
  the same day, and whether the weekday pre-market scan can.
- Check whether any long job blocks the 30-minute monitoring loop that enforces
  stop-losses.
- Check whether there is any protection against two copies of the process
  running at once.

## Out of scope

Order placement details (03), risk maths (02), deploy hardening (09).
