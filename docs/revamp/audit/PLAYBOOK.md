# Audit playbook

This is the method for the audit phase. It applies to every session, thread,
and subagent doing audit work, however it was started.

## Purpose

Josh built this bot with AI help and wants to revamp it. Before anything
changes he wants to see what is actually there. The audit answers three
questions, in this order:

1. What does the bot really do today? (Not what the README says.)
2. What is wrong, risky, fragile, or missing, and how sure are we?
3. What does Josh need to decide before a revamp plan can be written?

## Rules

- **Read-only.** Do not modify `stock_agent/`, `deploy/`, or
  `requirements.txt`. Do not fix what you find, even if the fix is one line.
  Write it down.
- **Only write under `docs/revamp/audit/`.** One exception: a small pure-logic
  test under `tests/` is allowed when running it is the only way to prove a
  finding. It must not call any network API.
- **Do not run the agent** and do not call broker, data, or LLM APIs. Reading
  code, running `pytest`, and running pure functions in a Python shell with
  made-up inputs is fine.
- **No secrets and no account data in anything you write.** This repository is
  public. Refer to a secret by its variable name and location, never its
  value. No account numbers, positions, balances, or trade history in
  committed files; describe them in general terms ("one position above the
  15% cap") instead.
- **Stay in your area.** If you notice something outside it, add one line to
  "Handoffs" and move on.
- **Report gaps honestly.** If you could not check something, say so and say
  what would let you check it.

## Evidence standard

A finding is only as good as its evidence. For every finding:

- Cite `path/to/file.py:line` (or a line range) for each claim, and quote the
  few lines that matter.
- Trace the real call path. A function that looks wrong but is never called is
  dead code, not a live bug. Say which one it is.
- State what you verified and how: read the code path end to end, ran a test,
  ran the function with sample inputs, or inferred it.
- Mark confidence: **Confirmed** (traced or demonstrated), **Likely** (strong
  reading, one link untested), or **Suspected** (worth checking, not shown).
- Keep facts and opinions apart. "The stop order is sent 3 seconds after the
  buy without checking the fill" is a fact. "This should use bracket orders"
  is a recommendation and goes in its own line.

The hints in each area brief are leads, not conclusions. Some will turn out to
be wrong or harmless. Check them, and say so when they are fine.

## Severity

Rate by what could happen to Josh's money and to the bot's ability to run.
The account is paper today; rate as if it were live, and say when a finding
only matters once real money is used.

| Level | Meaning |
| --- | --- |
| Critical | Can lose money beyond the intended limits, place unintended orders, or expose credentials. |
| High | Can make the bot miss trades or exits, act on wrong data, or stay down unnoticed. |
| Medium | Wrong numbers in reports, wasted API spend, fragile behaviour that has not bitten yet. |
| Low | Clutter, naming, dead code, minor inconsistencies. |
| Note | Not a problem. Useful context for the revamp. |

## Findings file format

Each area writes exactly one file: `docs/revamp/audit/findings/NN-area-name.md`,
matching its brief's number and name.

```markdown
# NN. Area name

Audited commit: <short sha>    Date: <YYYY-MM-DD>

## Plain-language summary
Five to ten sentences for Josh. What this part of the bot does, what state it
is in, and the two or three things that matter most. No jargon without a
half-sentence explanation.

## How it actually works
The real flow, step by step, with file:line references. Include a short
diagram or table if it helps. Note where reality differs from the README,
comments, or docstrings.

## Findings

### F-NN-01: Short title
- Severity: Critical | High | Medium | Low | Note
- Confidence: Confirmed | Likely | Suspected
- Where: `stock_agent/file.py:120-141`
- What happens: one or two sentences, plain language.
- Evidence: quoted lines and the call path that reaches them.
- Why it matters: the concrete consequence for the account or the bot.
- How verified: what you did to check.
- Direction (not a fix): one or two sentences on the kind of change needed.

(repeat)

## Checked and fine
Things you looked at that are correct. One line each with a reference. This
matters: it tells Josh what does not need to change.

## Could not verify
What you could not check and exactly what is needed (for example "the droplet
snapshot", "an Alpaca order history export").

## Questions for Josh
Decisions only he can make. Phrase each so it can be answered in a sentence.

## Handoffs
One line each for things noticed outside this area, with the area number.
```

Finding IDs are `F-<area number>-<sequence>`, for example `F-03-02`. They are
used later in the plan, so do not renumber them once committed.

## The eleven areas

| # | Area | Brief |
| --- | --- | --- |
| 01 | Scheduler and lifecycle | `areas/01-scheduler-and-lifecycle.md` |
| 02 | Risk and sizing | `areas/02-risk-and-sizing.md` |
| 03 | Execution and broker reconciliation | `areas/03-execution-and-broker-reconciliation.md` |
| 04 | Options | `areas/04-options.md` |
| 05 | Research and LLM pipeline | `areas/05-research-and-llm.md` |
| 06 | Data sources | `areas/06-data-sources.md` |
| 07 | State and accounting | `areas/07-state-and-accounting.md` |
| 08 | Alerts and observability | `areas/08-alerts-and-observability.md` |
| 09 | Deploy, security, operations | `areas/09-deploy-security-ops.md` |
| 10 | Code health and testability | `areas/10-code-health-and-testability.md` |
| 11 | Live performance | `areas/11-live-performance.md` (needs a droplet snapshot) |

## Running the audit

There are two ways to run it. Both produce the same files.

### Mode A: Claude Code Project (parallel threads)

Used when the work is started from a Claude Code Project. The lead thread
plans; each area runs in its own thread with its own copy of the repository.

1. The lead assigns one thread per area. Each thread's instructions are: read
   `CLAUDE.md`, this playbook, and its area brief; audit; write its one
   findings file.
2. Each area thread works on a branch named `audit/NN-area-name`, commits only
   its findings file, pushes the branch, and **does not open a pull request**.
   It reports back a five-line summary: counts by severity and the top three
   findings.
3. When all areas are done, one synthesis thread creates `audit/full-dig` from
   `master`, brings in the eleven findings files (for example
   `git checkout origin/audit/NN-area-name -- docs/revamp/audit/findings/`),
   and runs the verification and synthesis steps below.
4. The synthesis thread opens **one draft pull request** from `audit/full-dig`
   titled `docs(audit): full audit of the stock agent`, then stops.

### Mode B: single session with subagents

Used in a single cloud session or a local session, where `/audit` and the
subagents in `.claude/agents/` are available.

1. Create branch `audit/full-dig` from `master`, or from the setup branch
   if the setup is not merged to `master` yet.
2. Launch one `auditor` subagent per area brief, in parallel where possible.
   Each writes its own findings file and returns a short summary.
3. Run the verification and synthesis steps below in the main session.
4. Commit, push, open one draft pull request, then stop.

If parallel work is not available at all, do the areas one at a time in the
order 02, 03, 01, 07, 04, 09, 06, 05, 08, 10, 11 (money-critical first).

### Verification

Every Critical and High finding gets a second, independent look before it
reaches Josh. The verifier starts from the cited lines, not from the
auditor's reasoning, and tries to prove the finding wrong: is the code
reachable, is there a guard elsewhere, does the cited behaviour really follow?

The verdict is added to the finding as one line:

`- Verification: Confirmed | Refuted | Unclear: <one-sentence reason>`

Refuted findings stay in the file, marked, so the reasoning is not lost.

### Synthesis: `docs/revamp/audit/AUDIT.md`

Written last, for Josh, in plain language. Sections:

1. **Bottom line.** Ten sentences at most. Is the bot sound? What are the
   biggest risks? Is it worth revamping in place or rebuilding parts?
2. **How the bot actually works.** One page, end to end, from the weekly scan
   to an order and a stop, with a diagram.
3. **Top findings.** A table of every verified Critical and High finding: ID,
   title, plain-language consequence, link to the findings file.
4. **Themes.** Patterns that show up across areas (for example "errors are
   logged and swallowed everywhere").
5. **What is solid.** The parts that work and should be kept.
6. **Unknowns.** What could not be checked and what is needed to check it.
7. **Decisions for Josh.** A numbered list of questions with the options and a
   recommendation for each. These drive the plan.
8. **Suggested order of work.** A rough sequence, safety first. This is a
   suggestion for the plan phase, not the plan.
9. **Index.** Counts by severity per area with links.

Do not start the plan and do not change the phase. The audit ends when the
draft pull request is open.

## Using a droplet snapshot

Area 11, and parts of 03, 07, 08, and 09, need facts from the running server.
Josh can produce them by running `scripts/vps_snapshot.sh` on the droplet and
uploading the resulting archive to the session.

- A snapshot is evidence, not something to commit. Never add it to git.
- Summarise what it shows; do not paste raw positions, balances, order IDs, or
  log lines containing them into findings.
- If no snapshot is available, write area 11 as far as the code allows, list
  exactly what is missing under "Could not verify", and carry on.
