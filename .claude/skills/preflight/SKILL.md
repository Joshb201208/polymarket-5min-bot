---
name: preflight
description: Run the checks that must pass before opening a pull request in this repo (syntax, tests, secrets, risk-file changes, phase rules). Use before pushing a branch or opening a PR.
---

Run these checks on the current branch and report the result as a short
table. Do not fix anything unless asked; report first.

1. **Branch.** `git branch --show-current` must not be `master` or `main`.
2. **Phase rules.** Read `docs/revamp/PHASE`, then list changed files with
   `git diff --name-only origin/master...HEAD`. In `audit` or `plan`, nothing
   under `stock_agent/`, `deploy/`, or `requirements.txt` may be changed.
3. **Syntax.** `python3 -m compileall -q stock_agent`
4. **Tests.** `python3 -m pytest -q`
5. **Risk files.** If the diff touches any of these, list each changed hunk
   and say in one line what it changes:
   `stock_agent/config.py`, `stock_agent/risk_manager.py`,
   `stock_agent/executor.py`, `stock_agent/options_executor.py`,
   `stock_agent/options_engine.py`, `stock_agent/portfolio.py`,
   `tests/test_risk_tripwires.py`, anything in `deploy/`.
6. **Secrets and private data.** Look through the added lines of
   `git diff origin/master...HEAD` for anything that looks like a key, token,
   password, account number, balance, or pasted server log. Confirm that no
   `.env` file, snapshot archive, or file from `data/` is staged or
   committed.
7. **New environment variables.** Any new `os.environ` name in the diff must
   also appear in `.env.example`.
8. **Size.** Report files changed and lines added and removed. Flag a build
   pull request that touches more than about five source files.

Finish with one line: `Preflight: PASS` or `Preflight: FAIL` followed by the
failing checks.
