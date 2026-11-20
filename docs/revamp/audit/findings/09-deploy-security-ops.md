# 09. Deploy, security, operations

Audited commit: 941f2ca    Date: 2026-11-20

Branch `audit/full-dig`. `deploy/`, `requirements.txt`, `stock_agent/`,
`.env.example` and `README.md` are byte-identical to `origin/master`
(`e92e1cd`), checked with `git diff --stat origin/master HEAD -- <paths>`, so
everything below describes what the droplet deploys today. No droplet snapshot
was available; server-state items are under "Could not verify".

## Plain-language summary

The bot gets onto the server by a timer: every ten minutes the server looks at
GitHub, and if `master` has changed in any way it downloads the new code,
restarts the bot, and writes "Update complete" in a log, whether or not the
bot actually came back up. Nothing checks the new code first, nothing checks
the bot afterwards, nothing rolls back, and nobody is told if it is now
crashing. All of it runs as the server's administrator account (root).

Three things matter most. First, real-looking credentials are sitting in the
public repository right now: a Telegram bot token and chat ID in the example
settings file, a TipRanks key and token written into two source files, and an
access token for an outside AI agent service in a file that is still on
`master`. They need revoking whatever else happens. Second, the setting that
looks like the paper/live switch (`STOCK_AGENT_MODE`) is only a label on
messages. Where share orders really go is decided by a different setting (the
broker web address), and nothing checks that the two agree, so the bot can
place real orders while every alert says "PAPER". Third, there is no reliable
way to stop the bot: stopping the service is undone by the next change to
`master`, and there is no switch for "no new buys, keep the exits".

The bot also cannot start unless GitHub is reachable at that moment, the
example settings file is 88% leftovers from the old Polymarket bot (one of
which silently changes a stock risk limit), and the old bot's web server and
services were deleted from the repository but nothing ever removed them from
the server.

## How it actually works

### The deploy path

| Step | What | Where |
| --- | --- | --- |
| Trigger 1 | root's crontab, every 10 minutes, runs `auto_update.sh`, output appended to `/var/log/auto_update.log` | `deploy/setup.sh:43` |
| Trigger 2 | every start of the service (deploy, crash, reboot) pulls `master` itself before starting Python | `deploy/stock-agent.service:11-12` |
| 1 | delete the local record of where `origin/master` is, prune, `git fetch --all`; on fetch failure log "git fetch failed" (reason discarded by `2>/dev/null`) and exit | `deploy/auto_update.sh:5-7` |
| 2 | compare `HEAD` with `origin/master`; if equal, do nothing | `deploy/auto_update.sh:9-11` |
| 3 | `git reset --hard origin/master` | `deploy/auto_update.sh:13` |
| 4 | `venv/bin/pip install -r requirements.txt --quiet` | `deploy/auto_update.sh:14` |
| 5 | copy the unit file from the repo over the live one, `daemon-reload` | `deploy/auto_update.sh:16-17` |
| 6 | `systemctl restart stock-agent`, then log "Update complete" | `deploy/auto_update.sh:18-19` |
| 7 | systemd stops the bot (SIGTERM; the handler only sets a flag, `stock_agent/__main__.py:16-20`, `scheduler.py:105-107`, checked once per 60-second loop, `scheduler.py:127-142`), then runs fetch, reset, and `python -m stock_agent` | `deploy/stock-agent.service:11-15` |

Everything runs as root: `User=root` (`stock-agent.service:8`), root's
crontab, checkout under `/root/polymarket-bot`. The script has no `set -e`, no
lock against overlapping runs, no health check and no rollback. `.env`,
`data/` and `venv/` survive because they are untracked and git-ignored
(`.gitignore:1-2,7`) and nothing runs `git clean`.

Settings reach the bot only through systemd's `EnvironmentFile=`
(`stock-agent.service:10`). `python-dotenv` is in `requirements.txt:4` but is
never imported, so the README Quick Start (`README.md:7-11`, "cp .env.example
.env ... python -m stock_agent") runs with no settings loaded at all.

### Where reality differs from the documents

- `README.md:17`: "live mode flips a single config flag." There is no such
  flag (F-09-03).
- `stock_agent/tipranks_client.py:17`: "env vars still win via config." They
  do not; the client never reads `Config` (F-09-01).
- `stock_agent/config.py:51`: default `"1.00"` with the comment `# 50%`.
- `deploy/setup.sh:36` says "fill in your API keys" and then starts the bot
  anyway (`setup.sh:47-50`).
- Changes to `auto_update.sh` are rolled out by the *previous* version of the
  script: bash keeps reading the old file after `git reset` replaces it
  (demonstrated in a sandbox repository).

## Findings

### F-09-01: Credentials are committed in the public repository and are on `master` now
- Severity: Critical
- Confidence: Confirmed (that they are committed); Likely (that they are still valid)
- Where: `.env.example:2-3`; `stock_agent/config.py:194-195`; `stock_agent/tipranks_client.py:18-19`; `.mcp.json:13` on `origin/master`
- What happens: Three sets of credentials are readable by anyone.

  | Variable | Location | State | Rotate? |
  | --- | --- | --- | --- |
  | `TELEGRAM_BOT_TOKEN` (value has the exact shape of a real bot token) and `TELEGRAM_CHAT_ID` | `.env.example:2-3`, present in every local commit back to `81fd93f` | on `master` and in this branch | Yes. Revoke the token in BotFather. The chat ID cannot be rotated; remove it. |
  | `TIPRANKS_API_KEY`, `TIPRANKS_API_TOKEN` | default values at `stock_agent/config.py:194-195`, literal constants at `stock_agent/tipranks_client.py:18-19`; added in commits `8f50387` and `edcdb0e` (2026-04-23) | on `master` and in this branch | Yes, if the key is Josh's. See Questions. |
  | `AUTH_TOKEN` for `agent-proxy.perplexity.ai` (a bearer token for an outside AI agent service) | `.mcp.json:13` | on `master`; deleted only in `ff6fdc7`, which is on the unmerged setup branch | Yes, or confirm it has expired. |

- Evidence: `.env.example:1-3` is a `# Telegram` heading followed by the two
  assignments with non-empty values. The stock agent reads those same names
  (`config.py:17-18`). `deploy/setup.sh:34-37` copies the file to `.env` when
  none exists, so a server set up from this repo runs on the published token.
  For TipRanks, `tipranks_client.py:25-31` builds its headers from the module
  constants and the module does not import `Config`; running it with
  `TIPRANKS_API_KEY` and `TIPRANKS_API_TOKEN` set in the environment still
  sent the hard-coded pair. `git ls-tree origin/master` lists `.mcp.json`.
- Why it matters: With the Telegram token anyone can send messages to Josh's
  chat as the bot (fake trade alerts, fake "all fine"), read what is sent to
  the bot, or flood it so real alerts are dropped. Telegram is how Josh learns
  about problems. The TipRanks pair can be used up or get revoked, and
  TipRanks fails soft, so the bot would silently pick stocks without it. The
  TipRanks pair cannot be changed without a code change and a deploy.
- How verified: Read the files; pattern-scanned every blob in the 50 locally
  available commits with values masked; ran the TipRanks header function with
  made-up overrides. Did not call any provider to test validity.
- Direction (not a fix): Revoke all three first; removing them from the files
  does not un-publish them. Then credentials come only from `.env`, with empty
  placeholders in the example file.

### F-09-02: Older credentials and server details remain in the public history
- Severity: Medium
- Confidence: Confirmed for the 50 commits available locally
- Where: history before commit `bfabf49` (2026-04-23)
- What happens: Files deleted from the tree are still retrievable.

  | Item | Location | Rotate? |
  | --- | --- | --- |
  | `FRED_API_KEY` default value | `intelligence/fred.py:45` at `bfabf49^` | Yes (free key) or accept |
  | `DASHBOARD_PASSKEY` default (6 characters), the login for the old web dashboard | `dashboard/api.py:62` at `bfabf49^` | Cannot rotate; make sure the dashboard is gone (F-09-09) |
  | The droplet's public address | `deploy/setup.sh:74` at `88e7b5e^`; `shared/telegram_digest.py:90` at `bfabf49^` | Not a credential; assume it is known |
  | Old Polymarket wallet balance figures | subjects of commits `150075d`, `9da9745`, `0425f3d`, `2df3ea4`; `.env.example:16` | Private data, not a credential |

- Evidence: masked scan output; `git show` of each file.
- Why it matters: The passkey plus the address is a working login if the old
  dashboard is still running. The rest is low impact.
- How verified: Scanned all 442 objects in the local clone. Found no Alpaca,
  OpenAI, Perplexity, FMP, Finnhub or Discord secrets, no wallet private key,
  no `.env`, no data or log files. The clone is shallow, so nothing before
  2026-04-04 was searched (see Could not verify).
- Direction (not a fix): Rotate what can be rotated. Decide whether a history
  rewrite or a private repository is worth it once the full history is scanned.

### F-09-03: `STOCK_AGENT_MODE` does not control trading; the broker address alone does, and nothing checks they agree
- Severity: Critical (only matters once live keys are on the server)
- Confidence: Confirmed
- Where: `stock_agent/config.py:9,44`; `stock_agent/executor.py:31-32`; `stock_agent/options_executor.py:27`; `stock_agent/options_data.py:28`; `README.md:17`
- What happens: `MODE` is read in three places, all cosmetic: the start-up log
  line and messages (`scheduler.py:111-117`) and Discord footers
  (`discord_alerts.py:50-52,89-94`). Share orders go to whatever
  `ALPACA_BASE_URL` says. Options calls ignore that setting and always go to a
  hard-coded paper address.
- Evidence: `executor.py:32` `return f"{self.config.ALPACA_BASE_URL}{path}"`.
  `options_executor.py:27` `_TRADING_BASE = "https://paper-api.alpaca.markets"`,
  used for every options order and position call (lines 221-448). Importing
  `Config` with made-up settings:

  | Settings | `MODE` | Share order address | Options order address |
  | --- | --- | --- | --- |
  | none | PAPER | paper | paper |
  | `STOCK_AGENT_MODE=PAPER`, `ALPACA_BASE_URL=https://api.alpaca.markets` | PAPER | **live** | paper |
  | `STOCK_AGENT_MODE=LIVE` only | LIVE | paper | paper |

  Three Discord footers say "Paper Mode" regardless
  (`discord_alerts.py:197,243,311`). There is no start-up check of the account.
- Why it matters: Setting the mode back to PAPER as a safety step would not
  stop real orders. In the mixed state the options side sends live keys to the
  paper address, is refused, and treats the refusal as "no positions"
  (`options_executor.py:283-291`).
- Ways the bot can end up live: (1) `ALPACA_BASE_URL` plus live keys in the
  droplet's `.env`; (2) a merge that changes the default at `config.py:9` or
  `executor.py:31-32`, effective only if `.env` does not set the address; the
  tripwire test pins the default (`tests/test_risk_tripwires.py:69-70`) but
  nothing makes the server wait for tests; (3) a merge adding the setting to
  `deploy/stock-agent.service`, which `auto_update.sh:16` installs as root
  (`.env` wins if it also sets it); (4) anyone who can push to `master`, since
  that code runs as root and can read or rewrite `.env`; (5) the old
  dashboard's `set_env` endpoint if it is still running (F-09-09). Options can
  only go live by a code change.
- How verified: grep of every use of `MODE` and every Alpaca address; ran
  `Config`, `Executor._url` and the options constants under each setting.
- Direction (not a fix): One explicit switch, a refusal to start when the mode,
  address and account type disagree, and one address used by shares and options.

### F-09-04: A deploy is never checked; failures are logged as success and a crash loop is silent
- Severity: High
- Confidence: Confirmed (script behaviour demonstrated; systemd behaviour from its documentation)
- Where: `deploy/auto_update.sh:11-19`; `deploy/stock-agent.service:11-15`
- What happens: Every step runs regardless of whether the one before worked,
  and the last line always prints "Update complete". If the new code crashes
  on import, systemd restarts it every 30 seconds forever (`Restart=always`,
  `RestartSec=30`), and no message is sent because the start-up alert is in
  `scheduler.run()` (`scheduler.py:111-119`), which is never reached.
- Evidence: I ran the real script, with only its two absolute paths changed
  and `pip`/`systemctl` replaced by logging stubs, against a local sandbox
  repository:
  - `pip` exits 1: the script still restarts and prints "Update complete".
    On the next run `HEAD` equals the remote, so `pip` is never retried.
  - `git reset` fails on a stale `.git/index.lock`: the script still runs
    `pip`, restarts the bot, prints "Update complete", and repeats this every
    run while `HEAD` stays on the old commit.
  - The service's own fetch and reset run first: the cron script then sees no
    difference and never runs `pip` or copies the unit file for that commit.
- Why it matters: A bad merge takes the bot down with no alert; the only sign
  is that the daily messages stop. Worse, a bot that is crash-looping pulls
  every new commit within about 30 seconds, ahead of the 10-minute timer in
  roughly 19 cases out of 20, so a fix that adds or upgrades a library cannot
  be installed by the normal route.
- How verified: Sandbox runs as above; read the unit file; systemd manual for
  restart behaviour.
- Direction (not a fix): One deploy path that stops on the first error,
  confirms the bot is running afterwards, goes back to the last good commit if
  not, and tells Josh either way.

### F-09-05: The bot cannot start unless GitHub answers at that moment
- Severity: High
- Confidence: Confirmed by reading; one case demonstrated
- Where: `deploy/stock-agent.service:11-12`; `deploy/auto_update.sh:5-11`
- What happens: Each start first runs `git fetch --all` and `git reset --hard
  origin/master`. If either fails, Python is not started and systemd retries
  every 30 seconds indefinitely.
- Evidence and cases:
  - Network or DNS not ready at boot, or a GitHub outage: the fetch fails; the
    bot stays down for the whole outage although the code on disk is fine.
  - A git process killed mid-operation (out of memory, power loss) leaves a
    lock file; every later reset fails until someone deletes it by hand. With
    a new commit waiting, the timer itself then stops the running bot and it
    cannot come back.
  - The default branch is renamed: demonstrated. `git rev-parse origin/master`
    prints the unresolved name before failing, so `REMOTE` becomes
    `origin/master` plus a newline plus `unknown`, the guard at
    `auto_update.sh:11` passes, and every 10 minutes the script restarts the
    bot, whose own reset then fails.
  - The repository is made private without credentials on the server: the
    running bot carries on with old code until its next restart, then never
    starts again.
  - `auto_update.sh:5` deletes the tracking record before fetching. After a
    failed fetch, `origin/master` resolved in the sandbox to the commit from
    the original clone. Neither script resets to it in that state, but a
    manual `git reset --hard origin/master` during an outage would roll the
    code back.
- Why it matters: Any restart at the wrong moment leaves the bot down, with no
  alert, for reasons unrelated to its own code.
- How verified: Read the unit; sandbox runs for the rename, the unreachable
  remote and the stale lock.
- Direction (not a fix): Starting the bot should not depend on the network;
  updating the code should be a separate step from starting it.

### F-09-06: Stopping the bot does not stick, and there is no "no new orders" switch
- Severity: High
- Confidence: Confirmed
- Where: `deploy/auto_update.sh:18`; `deploy/setup.sh:52-55`; `README.md`; `stock_agent/config.py:201`
- What happens: Nothing documents how to stop the bot; `setup.sh` prints only
  how to check status and read logs. `systemctl stop stock-agent` works but
  takes up to 60 seconds when idle, or is force-killed at systemd's default 90
  seconds mid-cycle. The next change to `master`, including a documentation
  merge, runs `systemctl restart stock-agent`, which starts a stopped service.
  A reboot also starts it.
- Evidence: `auto_update.sh:18`. A search of `stock_agent/` for pause, halt,
  kill switch, dry run and read-only finds nothing. `OPTIONS_ENABLED` is
  defined at `config.py:201` and read nowhere else in the package, so setting
  it to false changes nothing. `MODE` gates nothing (F-09-03).
- Why it matters: In an incident Josh stops the bot, and the fix being merged
  starts it again before he is ready. He cannot keep stop-loss handling while
  blocking new buys; the only choices are everything or nothing.
- How verified: Read both scripts and the README; grep for every reader of the
  two flags.
- Direction (not a fix): A documented stop that also holds off the updater, and
  a real pause for new entries that leaves exits running.

### F-09-07: Every change to `master` restarts the bot as root at an arbitrary moment, unreviewed
- Severity: High
- Confidence: Confirmed (repository side); branch protection itself not verifiable
- Where: `deploy/auto_update.sh:9-18`; `stock_agent/scheduler.py:88-103,432-442`; `.github/workflows/ci.yml:3-5`
- What happens: Any difference between `HEAD` and `origin/master` triggers a
  restart, whatever changed. The bot keeps its schedule markers and its queue
  of pending buys only in memory, so each restart loses them.
- Evidence: `scheduler.py:432-442` queues Sunday's weekly buy list in
  `self._pending_signals` "for next open"; that list is created empty at
  `scheduler.py:95` and is never saved. The "done today" markers at
  `scheduler.py:90-98` are the same. `origin/master` has no `.github/`
  directory and no tests; the workflow exists only on the unmerged setup
  branch and says of itself that the droplet "does not wait for this to pass".
  The local history of `master` has 48 commits, no merge commits, no pull
  request numbers, and six committer identities, four of them automated
  agents. Four commits are dated 14:19 to 15:11 UTC on Thursday 2026-04-23,
  which is inside US market hours.
- Why it matters: A merge between Sunday evening and Monday's open discards
  the week's buys with no message. A restart inside a schedule window repeats
  that cycle and its paid API calls. Anyone or anything able to push to
  `master` runs code as root next to the broker keys within ten minutes.
  Merging this audit will itself restart the bot.
- How verified: Read the script and scheduler; `git log` on `origin/master`.
- Direction (not a fix): Deploys at a chosen time, gated on checks, with the
  pending work surviving a restart (area 01).

### F-09-08: `.env.example` is 88% another bot's settings, and one leftover changes a stock risk limit
- Severity: Medium
- Confidence: Confirmed
- Where: `.env.example:1-154`; `stock_agent/config.py:50-57`; `deploy/setup.sh:34-37`
- What happens: Of 89 variables in the file, 11 are read by the stock agent
  and 78 belong to the removed Polymarket bot. Thirty variables the code does
  read are missing from it.
- Evidence (script comparing names in the file with every `os.environ.get`):
  - Collision: `.env.example:19` sets `MAX_TOTAL_EXPOSURE_PCT=0.50` under the
    old "Bankroll" heading. `config.py:51` reads that same name as the stock
    agent's total exposure cap, default `1.00`. With the line present the
    limit loads as 0.5, not the 1.00 the tripwire test pins.
  - Misleading: `TRADING_MODE=paper` (line 6), `EVENTS_TRADING_MODE=live`
    (47), `EVENTS_PAUSED=false` (144), `LOG_LEVEL=INFO` (42) are read by
    nothing. `STOCK_AGENT_MODE=PAPER` (154) is a label only.
  - Missing: `DISCORD_BOT_TOKEN`, 17 `DISCORD_CH_*`, `DISCORD_GUILD_ID`,
    `MAX_POSITION_PCT`, `MAX_CLUSTER_PCT`, `PRIORITY_SYMBOLS`,
    `STOCK_DATA_DIR`, `STOCK_LOG_LEVEL`, `OPTIONS_ENABLED`, five `TIPRANKS_*`.
  - Dead settings: `STOCK_LOG_LEVEL` (`__main__.py:10` hard-codes INFO),
    `OPTIONS_ENABLED`, `DISCORD_GUILD_ID`, `TIPRANKS_API_KEY`/`_TOKEN`.
  - No validation: `MAX_POSITION_PCT=15` loads as 15.0, a 1500% cap;
    `MAX_POSITION_PCT=15%` raises `ValueError` at import, a crash loop.
- Why it matters: The three risk limits that can be set from `.env` are
  whatever the server's file says, with no record in the repository and no
  test. The mode and address lines in the example are the safe paper values,
  so the wrong-mode risk comes from the misleading names, not the defaults.
- How verified: Name comparison script; imported `Config` under each value.
- Direction (not a fix): An example file containing only what the code reads,
  and range checks on limits at start-up.

### F-09-09: The old bot's web server and services were removed from the repository but nothing removes them from the server
- Severity: High
- Confidence: Confirmed (repository); Suspected (server state)
- Where: `deploy/setup.sh` and `dashboard/` before commits `bfabf49` and `88e7b5e`; current `deploy/setup.sh`, `deploy/auto_update.sh`
- What happens: The earlier `setup.sh` installed nginx on port 80, made
  `/root` readable by other accounts (`chmod 755 /root`, line 54), and enabled
  two services; the dashboard's own deploy endpoint could create two more.
  Commit `88e7b5e` changed only repository files. Nothing in the current
  scripts stops, disables or deletes any of it.
- Evidence: `git show 88e7b5e -- deploy/`. The old units ran `git fetch` and
  `git reset --hard origin/master` before each start with `Restart=always` at
  10 and 30 seconds, and their programs no longer exist on `master`. The old
  dashboard (`dashboard/api.py` at `bfabf49^`) had `POST /api/events/set_env`
  (lines 1739-1755), which writes any name and value into the server's `.env`,
  and `POST /api/deploy` (164-287), behind a passkey whose default is in
  public history (F-09-02), over plain HTTP with `allow_origins=["*"]`.
- Why it matters: If the old units are still enabled they are crash-looping,
  resetting the same checkout every 10 to 30 seconds. That would explain the
  "unable to update local ref" errors `auto_update.sh:4` works around, and it
  would make the timer see no change and skip the restart and `pip` (F-09-04).
  If an old dashboard process has survived since April, the `.env` can be
  rewritten from the internet; that would be Critical. The brief's lead that
  something answers on the web port fits nginx still running.
- How verified: Read the historic files. Server state not checked.
- Direction (not a fix): Inspect the server, remove what is left, restore
  `/root` permissions, close port 80.

### F-09-10: API keys are written to the server log on every request
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/__main__.py:9-12`; `telegram_alerts.py:42`; `data_feed.py:40-50,59-69`; `macro_monitor.py:138,155`; `news_feed.py:37-45`; `sentiment.py:31`; `analyst_trends.py:34`; `options_portfolio.py:319`
- What happens: Logging is set to INFO for everything, and the HTTP library
  logs each request's full address at INFO. Telegram puts the token in the
  address path; FMP and Finnhub take the key as an address parameter.
- Evidence: With the same `basicConfig` call and a fake transport (no
  network), the log contained the made-up Telegram token, FMP key and Finnhub
  key in `httpx INFO HTTP Request: ...` lines, and the FMP key again in the
  message built at `data_feed.py:50`. Alpaca keys, sent as headers, did not
  appear. Every `httpx` version `requirements.txt:1` allows does this.
- Why it matters: Any log excerpt copied off the server carries working keys.
  `scripts/vps_snapshot.sh:236-244` redacts these patterns, so the planned
  snapshot route is covered; an ad-hoc paste is not.
- How verified: Offline run described above; grep confirmed nothing lowers the
  `httpx` log level.
- Direction (not a fix): Keep keys out of addresses where the provider allows,
  and quiet or filter the HTTP library's logger.

### F-09-11: No pinned versions and no lock file; the server's libraries are whatever was newest on first install
- Severity: Medium
- Confidence: Confirmed (repository); installed versions unknown
- Where: `requirements.txt:1-5`; `deploy/auto_update.sh:14`; `deploy/setup.sh:7,26-27`; `.github/workflows/ci.yml:35`
- What happens: All five requirements are lower bounds only. There is no lock
  or constraints file.
- Evidence: An offline `pip install --dry-run --no-index -r requirements.txt`
  reports each one "already satisfied", so the per-deploy `pip` does not
  upgrade anything. A library release therefore cannot change the running
  server on its own. It can on a rebuilt server, in CI (which installs the
  newest each run), or when a lower bound is raised; `httpx>=0.24` and
  `pydantic>=2.0` would accept a future major version. `aiohttp` and
  `python-dotenv` are imported nowhere; `requests` only in
  `tipranks_client.py:13`. `pip` never uninstalls, so the old bot's packages
  are presumably still in the venv. `setup.sh:7` installs the system
  `python3` unchecked: `stock_agent/macro_monitor.py:456` fails to compile on
  Python 3.11 ("f-string expression part cannot include a backslash") and
  compiles on 3.12.
- Why it matters: Tests cannot be run against what the server runs, and a
  rebuild after a failure may not produce the same bot.
- How verified: Dry-run; import grep; compiled each module with 3.11 and 3.12.
- Direction (not a fix): Exact pinned versions, the same in CI and on the
  server, and a Python version check in setup.

### F-09-12: The bot runs as root with no limits, and the server has no hardening or backups in the repository
- Severity: Medium
- Confidence: Confirmed (repository); server state unknown
- Where: `deploy/stock-agent.service:6-16`; `deploy/setup.sh:1-55`
- What happens: The unit sets `User=root` and no memory limit or sandboxing.
  `EnvironmentFile=` hands every line of `.env` to the process, including any
  old Polymarket secrets still there (`.env.example:9-13` names a wallet
  private key). `setup.sh` configures no firewall, swap, log rotation for
  `/var/log/auto_update.log`, or backup of `.env` and `data/`. It creates
  `.env` with default permissions, and starts the bot before keys are filled in.
- Why it matters: A flaw in any library, or a bad commit, has the whole
  server. Losing the droplet loses the bot's state and settings.
- How verified: Read both files in full.
- Direction (not a fix): A dedicated unprivileged account, basic limits, and
  an off-server copy of state and settings.

### F-09-13: Leftover clutter from the old bot
- Severity: Low
- Confidence: Confirmed
- Where: `deploy/deploy_log.txt:1`; `.gitignore:9`; `stock_agent/config.py:22-40,185`; `README.md:25,48`
- What happens: `deploy_log.txt` holds one comment line about a March 2026
  dashboard deploy and no secrets. `.gitignore` still ignores
  `dashboard/data/`. Discord server and channel IDs are hard-coded defaults
  (identifiers, not secrets). The README names Anthropic and a
  `POSITION_SIZE_PCT_BY_CONVICTION` setting, neither of which exists.
- Why it matters: Clutter that misleads the next reader.
- How verified: Read each file; grep.
- Direction (not a fix): Remove alongside the `.env.example` clean-up.

## Checked and fine

- `git reset --hard` does not touch `.env`, `data/` or `venv/` (`.gitignore:1-2,7`).
- Alpaca, OpenAI, Perplexity and Discord credentials travel as headers and did not appear in logs (`executor.py:24-29`, `screener.py:33`, `analyst.py:33`, `discord_alerts.py:29`).
- Defaults are paper: `config.py:9,44`, pinned by `tests/test_risk_tripwires.py:65-70`.
- No real `.env`, state file, log or snapshot in the 50 local commits; no credentials in commit messages.
- The removed TipRanks login scraper had empty email and password defaults (`edcdb0e`).
- The bot opens no listening port and runs no shell commands (grep of `stock_agent/`).
- Nothing prints the environment or `Config` wholesale.
- Time zones fall back correctly when legacy names are missing (`scheduler.py:50-58`).
- State files are written atomically (`portfolio.py:50-65`, `options_portfolio.py:90-106`).
- The cron line is installed idempotently (`setup.sh:43`).
- `ci.yml:12-13` limits the workflow to read-only permissions.
- An interrupted fetch does not leave a half-updated branch pointer; it fails cleanly.

## Could not verify

- **Server state (needs the droplet snapshot):** service status and restart
  count, crontab, `/var/log/auto_update.log`, listening ports, nginx, firewall,
  SSH settings, unattended upgrades, disk, memory and swap, `pip freeze`,
  Python version, the installed unit file, and which names are set in `.env`.
  Three things the snapshot script does not collect:
  `stat -c '%a %U %n' /root /root/polymarket-bot/.env`;
  `systemctl list-units --type=service --all | grep -E 'nba|events|dashboard|nginx'`;
  `ls /root/polymarket-bot/.git/*.lock`.
- **Git history before 2026-04-04.** The checkout is a shallow clone (50
  commits, boundary `81fd93f`). I did not fetch more, because that is a
  network call and changes the shared checkout. Needed: `git fetch --unshallow`
  and a rerun of the scan. The old bot handled a wallet private key, so this
  is the most important gap.
- **GitHub settings:** branch protection on `master`, who and what has write
  access (collaborators, apps, tokens, deploy keys), secret-scanning alerts,
  forks. Needs Josh's view of the repository settings.
- **Whether the published credentials still work.** Needs the provider
  dashboards; I called no API.
- **systemd behaviour** (restart after a failed pre-start step, 90-second stop
  timeout, `restart` starting a stopped unit, `.env` overriding
  `Environment=`) is from its documentation, not run here.

## Questions for Josh

1. Is the Telegram token in `.env.example` the one the live bot uses?
2. Whose TipRanks key and token are in the code: yours, or one supplied by the tool that wrote the integration?
3. Do Perplexity Computer or other agents still have push access to this repository, and should they?
4. Does the droplet's `.env` still hold the old Polymarket wallet key, and does that wallet still hold funds?
5. Did you ever remove nginx and the old `nba-agent`, `nba-dashboard`, `events-agent` or `dashboard` services by hand?
6. Is `MAX_TOTAL_EXPOSURE_PCT` set in the droplet's `.env`, and should the total cap be 50% or 100%?
7. Should risk limits be changeable from `.env` at all?
8. Should deploys stay automatic, or happen only at a time you choose?
9. Do you want a "pause new buys, keep exits" switch, and where: a setting, a file on the server, or a Telegram command?
10. Do you want to be told when the bot is down?
11. Should the repository become private? It needs a read-only deploy key on the server first, and branch protection on a private repository may need a paid GitHub plan.

## Handoffs

- 01: schedule markers and `_pending_signals` are memory-only and lost on every restart (`scheduler.py:88-103,432-442`); shutdown waits up to 60 seconds (`scheduler.py:127-142`).
- 03: a restart inside the 3-second gap between buy and stop leaves an unprotected position (`executor.py:104-127`).
- 04: options addresses are hard-coded to paper (`options_executor.py:27`, `options_data.py:28`); failed calls return an empty list (`options_executor.py:283-291`); `OPTIONS_ENABLED` is never read.
- 05: README names Anthropic; the code uses OpenAI and Perplexity only.
- 06: the TipRanks client makes a blocking `requests` call inside the async loop (`scheduler.py:830`, `tipranks_client.py:54`); the SEC contact address is a placeholder (`data_feed.py:78`).
- 07: no backup of `data/stock_agent/`.
- 08: exception text is forwarded verbatim to Telegram and Discord and could carry an address containing a key (`scheduler.py:135-136,471-472`); "Paper Mode" footers are hard-coded (`discord_alerts.py:197,243,311`); no heartbeat.
- 10: dead settings (`STOCK_LOG_LEVEL`, `OPTIONS_ENABLED`, `DISCORD_GUILD_ID`, `TIPRANKS_API_*` in config); unused `aiohttp` and `python-dotenv`; the tripwire tests do not pin the options addresses or `.env` overrides; `options_portfolio.py:315` reads a key from the environment directly.
