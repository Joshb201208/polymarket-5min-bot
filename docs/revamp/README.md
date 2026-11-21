# Revamp process

The stock agent is being revamped in three phases. The rule behind all of it:
**look first, decide second, change third.** Nothing in `stock_agent/` changes
until Josh has read the audit and approved a plan.

The current phase is the single word in [`PHASE`](PHASE). Only Josh changes it.

| Phase | Goal | Output | Ends when |
| --- | --- | --- | --- |
| `audit` | Understand exactly what the bot does today and what is wrong, risky, or missing. | `audit/findings/*.md`, then `audit/AUDIT.md` | Josh has read `AUDIT.md` and answered its questions. |
| `plan` | Turn the audit and Josh's decisions into an ordered list of small changes. | `PLAN.md` | Josh approves the plan. |
| `build` | Ship the plan one small pull request at a time, each with tests. | Pull requests | The plan is done or Josh stops. |

## How each phase runs

**Audit.** Eleven independent areas, each with a brief in `audit/areas/`. Each
area is investigated on its own and produces one findings file. A final pass
re-checks the serious findings, removes duplicates, and writes `audit/AUDIT.md`
with a plain-language summary and a list of decisions for Josh. The method,
evidence standard, and file formats are in [`audit/PLAYBOOK.md`](audit/PLAYBOOK.md).

**Plan.** After Josh answers the open questions, the plan is written from
[`PLAN_TEMPLATE.md`](PLAN_TEMPLATE.md). Each item is one pull request, traces
back to audit findings, and says how it will be tested and rolled back. Safety
and correctness come before features.

**Build.** Josh sets `PHASE` to `build`. Each plan item becomes one branch and
one pull request. Josh merges; merging to `master` deploys to the droplet
within ten minutes and restarts the bot.

## File map

```
CLAUDE.md                         Always-loaded project rules and context
docs/revamp/
  PHASE                           audit | plan | build
  README.md                       This file
  PLAN_TEMPLATE.md                Shape of the plan
  PLAN.md                         Created in the plan phase
  audit/
    PLAYBOOK.md                   How to audit: rules, evidence, formats
    areas/NN-name.md              One brief per audit area
    findings/NN-name.md           One findings file per area (created by the audit)
    AUDIT.md                      Synthesis for Josh (created by the audit)
  claude-project/
    SETUP.md                      Click-by-click setup for Josh
    PROJECT_INSTRUCTIONS.md       Text to paste into the Claude Code Project
    KICKOFF_PROMPTS.md            Prompts that start each phase
.claude/
  settings.json                   Permissions and hooks
  hooks/                          Push guard, phase lock, session start
  agents/                         auditor, finding-verifier, change-reviewer
  skills/                         /audit, /revamp-plan, /revamp-step, /preflight
  rules/                          Extra rules loaded when matching files are opened
scripts/
  vps_snapshot.sh                 Read-only evidence collector Josh runs on the droplet
  alpaca_readonly_report.py       GET-only Alpaca account report used by the snapshot
tests/                            Smoke tests and risk tripwires
```

## Where the rules are enforced

Claude Code runs this repository in more than one way, and they do not all
load the same files:

| Surface | Reads `CLAUDE.md` | Runs hooks and permissions | Loads skills and agents |
| --- | --- | --- | --- |
| Project threads (claude.ai/code, Projects) | Yes | No | No |
| Single cloud session on this repo | Yes | Yes | Yes |
| Claude Code on your own computer | Yes | Yes | Yes |

So every rule that matters is written in `CLAUDE.md` and the playbooks, which
all three read. The hooks are a second layer for the surfaces that run them.
The skills and agents are shortcuts to the same playbooks, not separate logic.
