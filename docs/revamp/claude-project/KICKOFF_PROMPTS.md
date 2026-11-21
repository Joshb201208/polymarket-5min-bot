# Kickoff prompts

Copy the prompt for the step you are on. Each one is written to work whether
or not the session can use this repository's skills and subagents.

## 1. Start the audit

### In the Claude Code Project conversation

```
Start the full audit of the stock agent.

The repository is in the audit phase (see docs/revamp/PHASE). Follow
docs/revamp/audit/PLAYBOOK.md, Mode A:

1. Start one thread for each of the eleven area briefs in
   docs/revamp/audit/areas/. Give each thread only its brief and the
   instruction to follow the playbook. Each one commits a single findings
   file on its own branch (audit/NN-area-name) and does not open a pull
   request.
2. When they have all reported, have every Critical and High finding
   independently re-checked by a thread that did not write it.
3. Then have one thread collect everything on branch audit/full-dig, write
   docs/revamp/audit/AUDIT.md, and open one draft pull request.

Nothing under stock_agent/, deploy/, or requirements.txt may change. Do not
fix anything you find; write it down.

[Delete whichever line does not apply]
The droplet snapshot is in the project Library as stock-agent-snapshot-*.tar.gz. Use it for areas 03, 07, 08, 09 and 11. It is private: summarise it, never commit it or paste its contents.
I have not uploaded a droplet snapshot. Do what the code allows and list what is missing.

Before you start the threads, show me the list of threads you intend to run
and wait for my go-ahead. When the audit is finished, give me the pull
request link, the bottom line in plain language, and the decisions you need
from me. Then stop.
```

### In a single cloud session or a local session

```
/audit all
```

If the snapshot is available, add a line saying where it is, for example:
`The droplet snapshot is unpacked at /tmp/snapshot. It is private.`

To audit one area on its own, for example before committing to the full run:

```
/audit 02
```

### If the audit stopped part-way

A session stops when a usage limit is reached. Open the same session again
once the limit has reset (do not start a new one, or unsaved work is lost)
and send:

```
The audit was interrupted. Continue it, do not start again.

1. Show me which findings files already exist in
   docs/revamp/audit/findings/ and whether each one is complete.
2. Commit and push the complete ones to audit/full-dig now, before doing
   anything else.
3. Read every file in docs/revamp/audit/evidence/. If that folder is missing
   on this branch, bring it in with:
   git fetch origin
   git checkout origin/master -- docs/revamp/audit/evidence
   (if it is not on master yet, tell me and I will paste the evidence.)
4. Run the remaining areas at most two at a time, money-critical first.
   Commit and push each findings file as soon as it is done.
5. Then verify the Critical and High findings, write AUDIT.md, open the
   draft pull request, and stop.

If the usage limit gets close again, stop starting new work, push what is
finished, and tell me which areas are left.
```

## 2. From audit to plan

Use this after you have read `docs/revamp/audit/AUDIT.md`. Replace the
bracketed parts.

```
I have read the audit. Move the revamp to the plan phase: set
docs/revamp/PHASE to "plan" on the plan branch as part of this work.

My answers to the decisions in AUDIT.md:
1. [answer]
2. [answer]
...

What I want from the revamp, in order of importance:
- [for example: never exceed its limits or leave a position without a stop]
- [for example: tell me when it is down]
- [for example: then improve the research quality]

Write docs/revamp/PLAN.md from docs/revamp/PLAN_TEMPLATE.md. One item per
pull request, small enough to review in one sitting, safety first. Every
verified Critical and High finding must appear either in an item or under
"Not doing" with a reason. If any of my answers above is unclear or you
disagree with one, ask before writing the plan. Open one draft pull request
and stop. Do not write any code.
```

In a session where skills are available you can start with `/revamp-plan`
followed by the same answers.

## 3. Build one step

Use this after you have approved and merged the plan. Run it once per plan
item, in order.

First time only:

```
The plan is approved. Move the revamp to the build phase: set
docs/revamp/PHASE to "build" as part of the first build pull request.
```

For each item:

```
Implement plan item R-01 from docs/revamp/PLAN.md, and only that item.

- Branch from an up-to-date master.
- First add tests that pin the current behaviour you must not change, then
  make the change, with tests for the new behaviour.
- python3 -m compileall -q stock_agent and python3 -m pytest -q must pass.
  If tests/test_risk_tripwires.py fails, stop and tell me.
- Have the change reviewed independently before you open the pull request,
  looking only at the diff, for unintended changes to trading behaviour.
- Open one pull request. Start the description with any risk-limit or
  trading-mode change (or "None"), then what the bot will do differently on
  its next cycle, in plain language, then what I must do before merging.

Do not merge. Tell me when it is ready and what you are least sure about.
```

In a session where skills are available: `/revamp-step R-01`.

## Useful follow-ups

```
Explain finding F-03-02 to me as if I had never seen the code. What exactly
could go wrong, how likely is it, and what would it cost?
```

```
Re-check finding F-02-01 from scratch without reading the original
reasoning. Try to prove it wrong.
```

```
Before I merge this pull request: what will the bot do differently the
first time it runs with this change, and what is the worst case if the
change is wrong?
```
