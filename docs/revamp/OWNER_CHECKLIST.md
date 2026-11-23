# Owner Security & Operations Checklist

This checklist covers one-time follow-ups that live outside the codebase: provider API dashboards, GitHub repository settings, and optional droplet cleanup.

**No immediate action is required for the revamped bot to run.** With your existing server `.env` unchanged, the revamped service starts cleanly in `PAPER` mode (`https://paper-api.alpaca.markets`) with defined-risk options enabled (`OPTIONS_MODE=defined_risk`), ignores leftover variables from the retired Polymarket/NBA/Events bot, silences noisy HTTP URL logs, and redacts API keys before writing logs.

> **Public-repo safety note:** This document lists **setting names and file locations only** — never paste secret values into git commits, issues, or pull requests.

---

## 1. Revoke and Rotate Exposed Credentials (Browser Only)

Git history on `master` is **intentionally not rewritten**. Per [GitHub's leaked-secret remediation guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository), any credential that has ever appeared in a public repository must be considered compromised and rotated at the issuing provider; force-pushing rewritten history does not invalidate copies already indexed externally and would break the droplet's `git fetch` updater.

Rotate or revoke each item below in its provider dashboard:

1. **Telegram bot token (`TELEGRAM_BOT_TOKEN`)** — *previously committed in `.env.example` lines 2–3*
   - In Telegram, message `@BotFather`, send `/revoke`, and select the bot to invalidate the exposed token immediately.
   - If you use Telegram alerts, paste the replacement token into `TELEGRAM_BOT_TOKEN` on the server; otherwise leave it unset (Discord alerts run independently via `DISCORD_BOT_TOKEN`).
2. **Perplexity MCP proxy token** — *previously committed in `.mcp.json` line 13 (file deleted and gitignored)*
   - Revoke or delete that bearer token in the MCP proxy / provider console where it was generated.
3. **TipRanks API credentials (`TIPRANKS_API_KEY`, `TIPRANKS_API_TOKEN`)** — *previously committed in `stock_agent/config.py` lines 194–195 and the now-removed `stock_agent/tipranks_client.py` lines 18–19*
   - Rotate or revoke both credentials in your TipRanks account dashboard (`tipranks_client.py` has been retired from `stock_agent`, so no replacement key is needed on the server).
4. **Financial Modeling Prep & Finnhub API keys (`FMP_API_KEY`, `FINNHUB_API_KEY`)** — *previously echoed in request query URLs inside server logs*
   - Regenerate `FMP_API_KEY` in the Financial Modeling Prep dashboard and `FINNHUB_API_KEY` in the Finnhub dashboard.
   - Update both values in `/root/polymarket-bot/.env` on the droplet and run `systemctl restart stock-agent`.
5. **Legacy FRED API key (`FRED_API_KEY`)** — *present in an early pre-revamp commit on `master`*
   - No longer used by `stock_agent`. If still active in your St. Louis Fed FRED account, disable or regenerate it.

---

## 2. Check Provider Billing and Quotas (Browser Only)

- **`PERPLEXITY_API_KEY`**: Check your Perplexity API credit balance and auto-top-up settings (earlier runs hit HTTP 401 insufficient-credit responses).
- **`OPENAI_API_KEY` and `ANTHROPIC_API_KEY`**: Verify active credit balances and monthly spend caps in OpenAI and Anthropic billing settings. The revamped research pipeline reads all three keys (`RESEARCH_PROVIDER_ORDER=openai,perplexity,anthropic`) and enforces a built-in weekly spend cap (`RESEARCH_WEEKLY_BUDGET_USD=15.0`).
- **`FMP_API_KEY` and `FINNHUB_API_KEY`**: Confirm your active subscription / rate-limit tier. Endpoints outside your current FMP plan are now automatically circuit-broken instead of retrying every cycle.

---

## 3. Clean Up `/root/polymarket-bot/.env` on the Droplet (When Convenient)

The revamped configuration loader ignores unknown environment variables so existing deployments keep running, but 28 settings from the removed Polymarket/NBA/NHL/Events bot remain in `/root/polymarket-bot/.env` — including an unused wallet key.

1. **Before deleting `PRIVATE_KEY` and `FUNDER_ADDRESS`:** Verify that the old Polymarket wallet holds zero balance (or transfer any remaining funds to a fresh wallet), then delete both lines from `/root/polymarket-bot/.env`.
2. **Delete all 28 unused old-bot variable names** (note `TIPRANKS_EMAIL` and `TIPRANKS_PASSWORD` each appear twice in the file):
   - `TRADING_MODE`
   - `POLYMARKET_API_KEY`
   - `POLYMARKET_API_SECRET`
   - `POLYMARKET_API_PASSPHRASE`
   - `PRIVATE_KEY`
   - `FUNDER_ADDRESS`
   - `STARTING_BANKROLL`
   - `MAX_BET_PCT`
   - `MAX_GAME_EXPOSURE_PCT`
   - `SCAN_INTERVAL`
   - `EXIT_CHECK_INTERVAL`
   - `PRE_TIPOFF_MINUTES`
   - `MIN_GAME_EDGE`
   - `MIN_FUTURES_EDGE`
   - `ODDS_API_KEY`
   - `BALLDONTLIE_API_KEY`
   - `PROXY_HOST`
   - `PROXY_PORT`
   - `PROXY_USER`
   - `PROXY_PASS`
   - `NBA_SEASON`
   - `LOG_LEVEL` *(replaced by `STOCK_LOG_LEVEL`)*
   - `NHL_TRADING_MODE`
   - `TWITTER_BEARER_TOKEN`
   - `TIPRANKS_EMAIL` *(duplicated twice; remove if not using TipRanks login)*
   - `TIPRANKS_PASSWORD` *(duplicated twice; remove if not using TipRanks login)*
   - `EVENTS_STARTING_BANKROLL`
3. **Keep active `stock_agent` variables:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `STOCK_AGENT_MODE`, `DISCORD_BOT_TOKEN`, `OPENAI_API_KEY`, `PERPLEXITY_API_KEY`, `ANTHROPIC_API_KEY` *(now wired into the research fallback chain)*, `FMP_API_KEY`, `FINNHUB_API_KEY`, and `MAX_TOTAL_EXPOSURE_PCT`.
4. **Ensure strict file permissions:**
   ```bash
   chmod 600 /root/polymarket-bot/.env
   ```

---

## 4. Stop Leftover `nginx` on Port 80 (Droplet Cleanup)

`nginx` is still running and listening publicly on TCP port 80 from the retired Polymarket dashboard. `stock_agent` runs purely as an outbound worker (`stock-agent.service`) and needs no inbound web server.

When logged into the droplet, disable `nginx` and optionally close port 80 at the firewall:

```bash
systemctl disable --now nginx
```

In your DigitalOcean Cloud Firewall (or `ufw`), allow inbound SSH (`22/tcp`) only and remove inbound HTTP (`80/tcp`).

---

## 5. Optional New Settings (Defaults Work Out of the Box)

None of these need to be added unless you want to override a default:

| Setting Name | Default | What It Controls |
| :--- | :--- | :--- |
| `OPTIONS_MODE` | `defined_risk` | Valid values: `off`, `close_only`, `defined_risk`. `defined_risk` allows vertical debit spreads, protective puts, and covered calls against settled shares while blocking naked shorts and cash-secured puts; `close_only` manages and exits existing contracts without opening new ones; `off` disables options completely. |
| `STOCK_TRADING_PAUSED` | `false` | Entry-only kill switch: blocks new equity/option openings while exits, stop-loss repairs, reconciliation, and alerts continue. Creating `/root/polymarket-bot/data/PAUSED` (or `data/stock_agent/PAUSED`) pauses entries immediately without restarting the service. |
| `STOCK_LOG_LEVEL` | `INFO` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `httpx`/`httpcore` request logs remain clamped at `WARNING`, and rotating logs are written under `data/stock_agent/logs/stock_agent.log` (10 MB x 12 files). |
| `HEARTBEAT_PING_URL` | *(empty)* | Optional dead-man's-switch URL (e.g. Healthchecks.io or Uptime Kuma) pinged periodically so an external monitor alerts you if the droplet goes offline. |
| `SEC_USER_AGENT` | *(empty)* | Optional `AppName contact@example.com` header enabling SEC EDGAR filing checks. |
| `STOCK_AGENT_LIVE_CONFIRMED` | `false` | Second safety lock for live trading. Switching to live money in the future requires **all three** of `STOCK_AGENT_MODE=LIVE`, `ALPACA_BASE_URL=https://api.alpaca.markets`, and `STOCK_AGENT_LIVE_CONFIRMED=true`. |

---

## 6. GitHub Repository Hardening & Making the Repo Private

1. **Enable GitHub Push Protection now (while public):**
   - Go to **Settings -> Code security** and enable **Secret Protection** and **Push protection** ([GitHub Push Protection docs](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/prevent-future-leaks/enable-push-protection)).
   - CI also runs Gitleaks (`gitleaks/gitleaks-action@v3` with `.gitleaks.toml`) on every pull request and `master` push.
2. **Add a read-only SSH deploy key BEFORE switching repo visibility to Private:**
   - The droplet currently fetches over unauthenticated HTTPS (`https://github.com/Joshb201208/polymarket-5min-bot.git`). Making the repo Private first will cause `git fetch` in the 10-minute auto-updater to fail.
   - Follow this sequence on the droplet and GitHub ([GitHub Deploy Keys docs](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)):
     ```bash
     ssh-keygen -t ed25519 -C "stock-agent-droplet-readonly" -f /root/.ssh/id_ed25519 -N ""
     cat /root/.ssh/id_ed25519.pub
     ```
   - In GitHub **Settings -> Deploy keys -> Add deploy key**, paste the `.pub` output and **leave "Allow write access" unchecked** (read-only).
   - Switch the droplet checkout remote and verify fetching works:
     ```bash
     cd /root/polymarket-bot
     git remote set-url origin git@github.com:Joshb201208/polymarket-5min-bot.git
     git fetch origin master
     ```
   - Once `git fetch origin master` succeeds over SSH, switch the repository visibility to **Private** in GitHub **Settings -> General -> Danger Zone**.
