# 09. Deploy, security, operations

Output: `docs/revamp/audit/findings/09-deploy-security-ops.md`

## Scope

`deploy/` (all files), `requirements.txt`, `.env.example`, `.gitignore`,
`README.md` setup instructions, credential handling across `stock_agent/`,
and the git history as far as secrets are concerned.

## Why it matters

The path from a merged pull request to a restarted trading bot is fully
automatic and has no checks. The repository is public. The server is small
and runs the bot as root.

## Questions to answer

1. Describe the deploy path exactly: what triggers it, what it runs, as which
   user, and what happens when a step fails half-way. Is there any health
   check after a restart, or any rollback?
2. What does a bad merge do? If new code crashes on import, how does the
   service behave and who finds out?
3. What does `pip install -r requirements.txt` on every deploy mean with
   unpinned versions? Could a library release change or break the bot without
   any change in this repo? Is there a lock file?
4. The unit resets the checkout on every start as well as the cron job. What
   are the consequences (network down at boot, GitHub unreachable, a
   half-fetched repository)?
5. Secrets: search the working tree **and the git history** for credentials.
   Report each as file, line or commit, and variable name, never the value.
   For each, say whether it needs rotating.
6. Compare `.env.example` with what the code actually reads. List variables
   that are missing, stale, or misleading. Is there a risk someone following
   it ends up with the wrong mode or URL?
7. What is the full list of ways the bot could switch from paper to live
   trading? Which settings control it, and do `MODE` and `ALPACA_BASE_URL`
   have to agree? What happens if they do not?
8. Server posture, as far as the repo shows: running as root, firewall, open
   ports, SSH setup, unattended upgrades, disk and memory headroom, anything
   left over from the earlier Polymarket bot.
9. The repository is public and the server pulls it without credentials. What
   would be needed to make it private without breaking deploys?
10. Is there a documented way to stop the bot quickly, and to stop it placing
    new orders without stopping exits?

## Leads to check (unverified)

- `.env.example` is mostly settings for a different, older bot.
- Default credential values appear in `config.py` and `tipranks_client.py`.
- An earlier committed file in this repo's history contained a token.
- `deploy/deploy_log.txt` is committed. Check what it contains.
- Something other than the bot has answered on the server's web port. Check
  whether `deploy/setup.sh` installs or configures a web server.
- `master` has no branch protection, and the only CI is the smoke-test
  workflow added with the revamp setup. Nothing stops a failing commit from
  deploying.
- Every change to `master` restarts the bot, including documentation-only
  merges.

## Could need a snapshot

Service status, cron entries, the auto-update log, listening ports, and disk
and memory use from the droplet snapshot.

## Out of scope

Code structure (10). Alert content (08).
