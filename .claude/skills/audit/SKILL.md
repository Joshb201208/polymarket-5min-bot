---
name: audit
description: Run the read-only audit of the stock agent, either one area or all eleven, following docs/revamp/audit/PLAYBOOK.md.
argument-hint: "[all | area number such as 03]"
disable-model-invocation: true
---

Run the audit for: $ARGUMENTS

If nothing was given above, treat it as `all`.

## Checks before starting

1. Read `docs/revamp/PHASE`. If it does not say `audit`, stop and tell Josh
   which phase the repo is in.
2. Read `CLAUDE.md` and `docs/revamp/audit/PLAYBOOK.md` in full. The playbook
   is the authority; this skill only sequences it.
3. Make sure you are on branch `audit/full-dig`, created from an up-to-date
   `master`. Never work on `master`.
4. Note the commit being audited (`git rev-parse --short HEAD`) so every
   findings file records it.

## One area

If an area number was given:

1. Find its brief in `docs/revamp/audit/areas/`.
2. Launch one `auditor` subagent with the brief's path.
3. When it returns, launch one `finding-verifier` subagent on the new
   findings file if it contains any Critical or High findings.
4. Commit the findings file on `audit/full-dig`, push the branch, and report
   the summary. Do not write `AUDIT.md` for a single area.

## All areas

1. Launch one `auditor` subagent per brief in `docs/revamp/audit/areas/`, all
   eleven, in parallel. Each gets only its brief's path.
2. As they return, check that each wrote exactly one findings file in the
   playbook's format. If one is thin or missed questions from its brief, send
   it back once with the specific gaps.
3. Launch `finding-verifier` subagents on every findings file that has
   Critical or High findings. These can run in parallel, one per file.
4. Read all eleven findings files yourself and write
   `docs/revamp/audit/AUDIT.md` with the sections the playbook lists under
   "Synthesis". Leave refuted findings out of the top-findings table. Remove
   duplicates across areas by pointing to one finding ID.
5. Commit everything under `docs/revamp/audit/` on `audit/full-dig`, push,
   and open one **draft** pull request titled
   `docs(audit): full audit of the stock agent`. The description is the
   "Bottom line" and "Decisions for Josh" sections of `AUDIT.md`.
6. Stop. Tell Josh the audit is ready to read, give him the pull request
   link, and list the decisions that need his answer. Do not start a plan, do
   not change the phase, and do not fix anything.

## If a droplet snapshot was uploaded

Tell the area 03, 07, 08, 09, and 11 auditors where the unpacked snapshot is.
It is evidence only. It must never be committed, and its raw contents must
not be pasted into findings.
