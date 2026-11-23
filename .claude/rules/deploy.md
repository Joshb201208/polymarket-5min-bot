---
paths:
  - "deploy/**"
  - "requirements.txt"
---

# Deploy files

- The existing root cron invokes `deploy/auto_update.sh` every ten minutes.
  It pulls `master` only when that exact commit's CI test passed, the broker
  confirms the market is closed at least 30 minutes before the next open,
  and the service is already running or explicitly `failed`. Documentation-only
  changes do not restart a running service. There is no staging checkout.
- The first cutover itself is still performed by the predecessor updater,
  which does not check CI or market hours. Publish that cutover only after
  CI is green and the regular market is confirmed closed. If that updater
  advances the checkout but leaves a stale installed or loaded unit, the new
  updater repairs it using the same CI, market, and readiness checks.
- The updater preserves `.env`, `data/` and unrelated untracked files.
  `data/deploy-hold` defers update attempts. `data/pause-trading` blocks
  new entries but not exits or stop work. Neither file requires a change
  to the repository. A deliberately stopped service stays stopped.
- A `failed` service can be recovered only by a newer, CI-green, unquarantined
  commit at a broker-confirmed closed-market check. Cron checks the broker
  directly, clears systemd's failure state, and starts the new commit once.
  An unsuccessful recovery is quarantined and recorded without restarting the
  prior failed release.
- `deploy/stock-agent.service` starts local code without any GitHub request.
  The updater installs unit changes with candidate code and restores the
  prior unit and checkout if the replacement does not report readiness.
- A new dependency is installed on a 1 vCPU, 1 GB machine. Check its size and
  its own dependencies first.
- Never add a step that prints environment variables or the contents of
  `.env`.
- Say in the pull request exactly what will happen on the server when it is
  merged, step by step.
