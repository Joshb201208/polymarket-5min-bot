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
3. Make sure you are on branch `audit/full-dig`. Create it from an
   up-to-date `master` if `docs/revamp/audit/PLAYBOOK.md` exists there. If it
   does not (the setup pull request is not merged yet), create it from the
   branch this session started on and remember that branch name: it is the
   base for the audit pull request. Never work on `master`.
4. Note the commit being audited (`git rev-parse --short HEAD`) so every
   findings file records it.
5. List `docs/revamp/audit/evidence/`. If it has files, read them. They are
   measurements of the running bot (see "Evidence on file" in the playbook).
6. List `docs/revamp/audit/findings/`. If the branch exists on GitHub, fetch
   it first so you see what an earlier session pushed. An area that already
   has a complete findings file is done: do not run it again unless Josh
   asked for that area by number.

## Save as you go

A session can stop without warning when a usage limit is reached. Nothing
that exists only in the session survives that.

- Commit and push each findings file as soon as its auditor returns and the
  file passes your format check. One commit per area:
  `docs(audit): findings for area NN`.
- Never hold more than one finished, unpushed findings file.
- If you are told the usage limit is close, stop launching work, commit and
  push what is complete, and tell Josh which areas remain.

## One area

If an area number was given:

1. Find its brief in `docs/revamp/audit/areas/`.
2. Launch one `auditor` subagent with the brief's path.
3. When it returns, launch one `finding-verifier` subagent on the new
   findings file if it contains any Critical or High findings.
4. Commit the findings file on `audit/full-dig`, push the branch, and report
   the summary. Do not write `AUDIT.md` for a single area.

## All areas

1. Launch one `auditor` subagent per brief in `docs/revamp/audit/areas/`
   that is not already done, at most four at a time, in the playbook's
   money-critical order (02, 03, 01, 07, 04, 09, 06, 05, 08, 10, 11). Each
   gets only its brief's path. Start the next one as each finishes.
2. As they return, check that each wrote exactly one findings file in the
   playbook's format. If one is thin or missed questions from its brief, send
   it back once with the specific gaps. Then commit and push that file (see
   "Save as you go").
3. Launch `finding-verifier` subagents on every findings file that has
   Critical or High findings. These can run in parallel, one per file.
4. Read all eleven findings files and the evidence files yourself and write
   `docs/revamp/audit/AUDIT.md` with the sections the playbook lists under
   "Synthesis". Leave refuted findings out of the top-findings table. Remove
   duplicates across areas by pointing to one finding ID. Every observation
   in the evidence files must appear under the finding that explains it or
   under "Unknowns".
5. Commit everything under `docs/revamp/audit/` on `audit/full-dig`, push,
   and open one **draft** pull request titled
   `docs(audit): full audit of the stock agent`. The description is the
   "Bottom line" and "Decisions for Josh" sections of `AUDIT.md`. The base
   is `master`, or the setup branch noted in the checks above if the setup is
   not merged yet, so the pull request shows only audit documents.
6. Stop. Tell Josh the audit is ready to read, give him the pull request
   link, and list the decisions that need his answer. Do not start a plan, do
   not change the phase, and do not fix anything.

## If a droplet snapshot was uploaded

Tell the area 03, 07, 08, 09, and 11 auditors where the unpacked snapshot is.
It is evidence only. It must never be committed, and its raw contents must
not be pasted into findings. A snapshot adds to the evidence on file; it does
not replace it.
