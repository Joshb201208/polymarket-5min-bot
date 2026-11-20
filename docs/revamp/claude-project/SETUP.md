# Setting up the Claude Code project

This is the click-by-click setup for Josh. It takes about fifteen minutes.
Nothing here changes the trading code.

There are two ways to run the revamp. Use the Project as the home base; use a
single session when you want the stricter guardrails.

| | A. Claude Code Project | B. Single cloud session |
| --- | --- | --- |
| What it is | One ongoing conversation that starts a separate thread per task and keeps memory across them | One session on the repository |
| Parallel work | A thread per audit area | Subagents inside the session |
| Reads `CLAUDE.md` | Yes | Yes |
| Runs this repo's hooks and permission rules | **No** | Yes |
| Uses this repo's `/audit` skill and subagents | **No** (it follows the playbook from the instructions) | Yes |
| Needs the setup merged to `master` first | Yes | No (you can pick the branch) |
| Where to upload the server snapshot | The project Library | See step B3 |

In a Project, the rules are enforced by instructions only. That is workable
for the audit, which only writes documents. For the build phase, where code
changes, prefer path B or keep a close eye on each pull request.

Projects are a beta feature on Pro, Max, and Ultra plans and are being rolled
out gradually. If you do not see **Projects** in the sidebar at claude.ai/code
(check under **More**), use path B.

## Before either path

### 1. Know what merging does

The server redeploys whenever `master` changes, and restarts the bot, even
for a documentation-only change. The setup pull request does not touch
`stock_agent/`, `deploy/`, or `requirements.txt`, so the bot's behaviour does
not change, but merging it will restart the service once.

**Merge when the US market is closed** (in Singapore time, that is any time
outside roughly 22:30 to 05:00 on weeknights, or the weekend).

- Path A needs the setup on `master`, because Project threads start from the
  default branch. Merge the setup pull request first.
- Path B does not need the merge. You can start the session on the
  `claude-code-setup` branch.

### 2. Connect GitHub

1. Go to [claude.ai/code](https://claude.ai/code) and sign in with GitHub when
   asked. Approve the authorization.
2. Install the **Claude GitHub App** on the `Joshb201208` account and give it
   access to `polymarket-5min-bot`. Projects require the app on the
   repository; it is also what lets Claude push branches and open pull
   requests.

### 3. Set up the cloud environment

Project threads do not run this repository's session hook, so the Python
packages need to be installed by the environment instead.

1. On the new-session screen at claude.ai/code, open the environment selector
   (the first control in the row under the message box).
2. Select **New environment** at the bottom of the menu and name it
   `stock-agent`. (To change an existing one instead, hover over its row
   under **Cloud** and select the settings icon.)
3. Network access: leave it at **Limited** with **Allow package managers**
   checked.
4. Select **Setup script** and paste:

   ```bash
   #!/bin/bash
   python3 -m pip install -q httpx requests pydantic python-dotenv aiohttp pytest ruff || true
   ```

   These are the packages in `requirements.txt` and `requirements-dev.txt`,
   listed by name so the script does not depend on which folder it runs in.

5. Do **not** add any environment variables or secrets. No session needs your
   Alpaca, Perplexity, OpenAI, FMP, Finnhub, Telegram, or Discord keys, and
   the tests are written to run without them.
6. Select **Add environment** (or **Save changes**).

### 4. Optional but recommended: take a server snapshot

The audit can read the code without it, but it cannot tell you how the bot
has actually performed, whether there are stray orders at the broker, or why
the service has been restarting. The snapshot gives it that.

It is read-only: it does not restart or change anything, and the broker part
only sends GET requests. It does not copy your `.env`; secret values are
replaced with `REDACTED` and the script refuses to produce an archive if any
secret value is still present afterwards.

If the setup pull request is merged (wait ten minutes for the server to pull
it), log in to the droplet and run:

```bash
cd /root/polymarket-bot && bash scripts/vps_snapshot.sh
```

If it is not merged, fetch the two scripts from the branch instead:

```bash
mkdir -p /root/snapshot-tools && cd /root/snapshot-tools
base=https://raw.githubusercontent.com/Joshb201208/polymarket-5min-bot/claude-code-setup/scripts
curl -fsSLO $base/vps_snapshot.sh && curl -fsSLO $base/alpaca_readonly_report.py
bash vps_snapshot.sh
```

The script prints the path of a file named
`/root/stock-agent-snapshot-<date>.tar.gz` and the `scp` command to download
it to your computer. After you have downloaded it, delete it from the server.

The archive contains your paper account's positions, orders, and balances.
Keep it out of the repository (the repository is public) and treat uploading
it to Claude as sharing that data with Anthropic.

## Path A: Claude Code Project

### A1. Create the project

1. At claude.ai/code, select **Projects** in the left sidebar (or under
   **More**), then **New project**. If the button says **Get started**
   instead, follow it first; it deals with any older claude.ai projects on
   your account.
2. **Name:** `Stock Agent Revamp`
3. **What do you want to work on?** paste:

   ```
   A careful, staged revamp of my stock and options trading bot: first a
   full read-only audit of everything, then a plan I approve, then small
   tested pull requests. The bot is live on a server that deploys the master
   branch automatically, so nothing may be pushed to master or merged
   without me.
   ```

4. **Context:** click **+** and add the GitHub repository
   `Joshb201208/polymarket-5min-bot`. If you took a snapshot, click **+**
   again and add the `.tar.gz` file.
5. Select **Create project**.

### A2. Add the project instructions

1. In the project header, open the **⋯** menu and select **Settings**, then
   go to **Memory > Project instructions**.
2. Open [`PROJECT_INSTRUCTIONS.md`](PROJECT_INSTRUCTIONS.md), copy everything
   below its horizontal line, paste, and save.
3. In **Project settings > Environment**, select the environment from step 3
   if it is not already selected.

### A3. Start the audit

Paste the first prompt from [`KICKOFF_PROMPTS.md`](KICKOFF_PROMPTS.md) into
the project conversation. It asks Claude to show you the list of threads
before starting them.

What to expect:

- Eleven area threads, then verification threads, then one synthesis thread.
- Branches named `audit/...` appear on GitHub. Pushing branches does not
  deploy anything; only `master` does.
- One draft pull request at the end, containing only files under
  `docs/revamp/audit/`.
- Threads run mostly without asking. If one needs approval, the prompt
  appears inside that thread, not in the project conversation.

If a thread starts editing anything under `stock_agent/` during the audit,
stop it and tell the project conversation. That is the rule the hooks would
have enforced in path B. The CI check on the pull request is a second net:
it runs the risk tripwire tests on whatever the thread pushed.

### A4. Read the audit

Read `docs/revamp/audit/AUDIT.md` in the draft pull request. Start with
"Bottom line" and "Decisions for Josh". Ask the project conversation to
explain anything; there are follow-up prompts in the kickoff file.

You can merge the audit pull request (documents only, restarts the bot once,
so do it with the market closed) or leave it open and let the plan build on
its branch.

Then use prompt 2 for the plan, and prompt 3 for each build step.

## Path B: single cloud session

### B1. Start the session

1. At claude.ai/code, click the repository selector under the message box and
   choose `Joshb201208/polymarket-5min-bot`.
2. In the branch control next to it, choose `claude-code-setup` (or `master`
   if the setup is merged).
3. Choose the environment from step 3.
4. Permission mode: **Auto** for the audit. For build steps, **Plan** makes
   Claude propose its approach and wait before editing.

### B2. Run the audit

Type:

```
/audit all
```

In this mode the repository's guardrails are active: the session cannot push
to `master`, merge a pull request, run the bot, read `.env`, or edit
`stock_agent/`, `deploy/`, or `requirements.txt` while the phase is `audit`.
Eleven `auditor` subagents run in parallel, `finding-verifier` subagents
re-check the serious findings, and the session writes `AUDIT.md` and opens
one draft pull request.

### B3. Giving a single session the snapshot

The cloud-session documentation does not describe attaching files to a
session. If the message box offers an attach control, use it and tell Claude
it is the droplet snapshot. If it does not, either run the audit from the
Project (which has a Library for files) or run Claude Code on your own
computer in a clone of the repository with the archive unpacked outside the
repository folder, and tell Claude the path.

## What is enforced, and where

| Rule | Project threads | Single cloud or local session |
| --- | --- | --- |
| No push to `master`, no force-push | Instructions | Hook blocks it |
| No merging pull requests | Instructions | Hook blocks it |
| No running the bot, no broker calls from the shell | Instructions (and no credentials exist in the session) | Hook blocks it |
| No reading `.env` | Instructions (and there is no `.env` in a fresh clone) | Hook and permission rules block it |
| Production code read-only until the build phase | Instructions | Hook blocks edits made with the editing tools |
| Risk limits unchanged | Instructions and the tripwire tests in CI | Same |

The hooks are a seatbelt, not a vault. They catch the commands Claude would
normally write; they are not designed to stop a determined attempt to get
around them. The real protections are that no session has your credentials,
and that only you merge.

Two things are worth doing on GitHub regardless, and both take a minute:

- In the repository's **Settings > Branches**, add a rule for `master` that
  requires a pull request before merging. Then nothing can reach the server
  without you pressing merge.
- Decide whether the repository should stay public. Audit findings will be
  readable by anyone. Making it private will break the server's automatic
  update until the server is given a read-only deploy key, so do not flip it
  without doing that first. The audit covers this in area 09.

## Checking the setup yourself

On any computer with Python 3.12 or later and a clone of the repository:

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest
```

This runs the import smoke tests, the risk tripwires, and tests that feed the
guard hooks the commands they are meant to block and allow.
