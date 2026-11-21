---
paths:
  - "deploy/**"
  - "requirements.txt"
---

# Deploy files

- These files run as root on the production server, automatically, within ten
  minutes of a merge to `master`. There is no staging copy.
- `deploy/auto_update.sh` copies `deploy/stock-agent.service` over the live
  unit file on every update. A mistake in the unit file stops the bot.
- A new dependency is installed on a 1 vCPU, 1 GB machine. Check its size and
  its own dependencies first.
- Never add a step that prints environment variables or the contents of
  `.env`.
- Say in the pull request exactly what will happen on the server when it is
  merged, step by step.
