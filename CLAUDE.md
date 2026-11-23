# Stock Trading Agent

Thesis-driven equities and options agent owned by Josh. Python 3, asyncio, no
web framework. One long-lived process (`python -m stock_agent`) on a small
DigitalOcean droplet, trading through Alpaca. Paper mode is the default.

The repository name (`polymarket-5min-bot`) and the server path
(`/root/polymarket-bot`) are historical. Only `stock_agent/` is live.

Josh is the owner and decision-maker, not a professional developer. Explain
findings and proposals in plain language first, with the technical detail after.

## Current phase: read `docs/revamp/PHASE` first

This repo is in a staged revamp. The single word in `docs/revamp/PHASE` says
what is allowed right now:

| Phase | What you do | What you must not do |
| --- | --- | --- |
| `audit` | Read everything. Write findings under `docs/revamp/audit/`. | Change `stock_agent/`, `deploy/`, or `requirements.txt`. Fix anything. |
| `plan` | Turn the approved audit into `docs/revamp/PLAN.md`. | Same as above. |
| `build` | Implement one approved plan item per pull request, with tests. | Work outside the approved plan. |

Only Josh moves the phase forward. Never edit `docs/revamp/PHASE` unless he
says so in the current conversation. The process is in `docs/revamp/README.md`;
the audit method is in `docs/revamp/audit/PLAYBOOK.md`.

## Evidence from the running bot

`docs/revamp/audit/evidence/` holds measurements of what the bot actually did
in production (results against an index, ledger against broker, limits in
practice, failed inputs). Read it before auditing, planning, or changing
anything that touches money. It records what happened, not why. It follows
the public-repository rules: percentages and counts only.

## Deployment: read this before any git push

- The droplet runs `deploy/auto_update.sh` from cron every 10 minutes. When
  `origin/master` changes it runs `git reset --hard origin/master`, reinstalls
  requirements, and restarts the `stock-agent` systemd service.
- **Every push to `master` is a production deploy to a bot that places
  orders.** There is no staging environment. Even a documentation-only
  change restarts the service.
- Never commit to or push `master`. Work on a branch, open a pull request,
  and stop. Josh merges. Never merge a pull request yourself.
- Never force-push. Never rewrite history.
- Files edited on the droplet are wiped by the next reset. Only `.env` and
  `data/` (both git-ignored) persist there.

## Hard rules

- Never switch paper to live trading or change `STOCK_AGENT_MODE` /
  `ALPACA_BASE_URL` defaults.
- Never loosen a risk limit (position cap, cluster cap, sector cap, total
  exposure, stop-loss range, options exposure, minimum conviction) unless the
  approved plan item says so. Any diff touching those values is called out at
  the top of the PR description.
- Never run the live agent (`python -m stock_agent`) and never call Alpaca
  order endpoints while developing. Tests use fakes.
- Never read, print, or commit `.env`. Read credentials only through
  `stock_agent/config.py`. No secret values in code, commits, PR text, audit
  reports, or logs.
- **This repository is public.** Do not commit account numbers, portfolio
  snapshots, trade history, or server logs. Uploaded snapshots stay out of git.
- Do not connect to the droplet. You have no server access and do not need it.
- The droplet has 1 vCPU and 1 GB RAM. No heavy dependencies, no headless
  browsers, nothing that holds large data in memory.
- If something you need is missing (a secret, a snapshot, an API, a decision),
  say exactly what is missing and stop. Do not substitute, mock, or guess.

## Commands

Python 3.12 or later (the code uses 3.12 syntax; the droplet runs 3.12).

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m compileall -q stock_agent     # syntax check
python3 -m ruff check .                  # errors only, no style rules
python3 -m pytest -q                     # must pass before any PR
```

`tests/test_risk_tripwires.py` pins today's risk limits and sizing behaviour.
If it fails, you changed trading behaviour: stop and confirm that was intended
before touching the test.

Where skills load, the workflows are `/audit`, `/revamp-plan`,
`/revamp-step <id>`, and `/preflight`. Where they do not (Project threads),
follow the same steps from `docs/revamp/` by hand.

## Architecture

All code is in `stock_agent/`. `scheduler.py` (about 2,000 lines) is the
orchestrator; everything else is a collaborator it calls.

| Area | Files |
| --- | --- |
| Entry and loop | `__main__.py`, `scheduler.py` |
| Settings and models | `config.py`, `models.py`, `options_models.py` |
| Universe and data | `screener.py` (OpenAI), `data_feed.py` (FMP, Finnhub, SEC, Alpaca data), `tipranks_client.py` |
| Enrichment | `news_feed.py`, `sentiment.py`, `analyst_trends.py`, `macro_monitor.py`, `research_notes.py` |
| Thesis | `analyst.py` (Perplexity Sonar and Sonar Pro, JSON out) |
| Risk and ledger | `risk_manager.py`, `portfolio.py` |
| Execution | `executor.py`, `options_engine.py`, `options_executor.py`, `options_portfolio.py`, `options_data.py` |
| Alerts | `telegram_alerts.py`, `discord_alerts.py` |

Schedule, in US Eastern time (`config.py`): Sunday weekly deep analysis;
Wednesday midweek refresh; weekdays 08:00 pre-market scan; during market hours
a 30-minute monitoring loop plus options, macro, and earnings-reactive scans.

State is JSON under `data/stock_agent/` (`portfolio.json`,
`options_portfolio.json`, `macro_state.json`, `macro_history.json`).

## Decisions already made (keep unless the plan changes them)

- Alpaca is the source of truth for positions; local ledgers reconcile against
  it before risk checks. Stale local state once let one symbol reach 76% of
  the portfolio.
- Sizing is conviction-tiered: scores 7/8/9/10 target 5/7/8/10% of portfolio.
  The 15% per-symbol limit is a ceiling, not a target. Minimum conviction is 7.
- TipRanks Smart Score is a discovery and conviction input, never a sole buy
  trigger.
- Stop-loss is volatility-adjusted and clamped to 7–12%.
- Expired in-the-money option legs are valued at intrinsic value, not zero.
- Covered calls target out-of-the-money strikes only.

## Pull requests

- One purpose per PR. Conventional prefixes as in the git history:
  `feat(scope):`, `fix(scope):`, `chore(scope):`, `docs(scope):`.
- The description says, in plain language, what the bot will do differently on
  its next cycle and why, then lists the tests that prove it.
- A new environment variable goes into `.env.example` and is flagged in the
  PR, because Josh must add it to the droplet's `.env` by hand before merging.
