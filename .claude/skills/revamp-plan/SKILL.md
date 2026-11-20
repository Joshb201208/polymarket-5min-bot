---
name: revamp-plan
description: Turn the approved audit and Josh's decisions into docs/revamp/PLAN.md, an ordered list of small pull requests.
disable-model-invocation: true
---

Write the revamp plan.

Extra direction from Josh, if any: $ARGUMENTS

## Checks before starting

1. Read `docs/revamp/PHASE`. If it says `audit`, Josh must have said in this
   conversation that he has read `docs/revamp/audit/AUDIT.md` and wants to
   move to the plan phase. If he has, you will set the file to `plan` on the
   plan branch in step 2 below. If he has not, stop and ask him.
2. `docs/revamp/audit/AUDIT.md` must exist, either on `master` (audit pull
   request merged) or on branch `audit/full-dig` (still open). If it is on
   neither, stop and say so.
3. Read `CLAUDE.md`, `AUDIT.md`, and `docs/revamp/PLAN_TEMPLATE.md`.

## Steps

1. List the "Decisions for Josh" from `AUDIT.md`. For each one that Josh has
   not answered in this conversation, ask him now. Ask them together, in
   plain language, with your recommendation for each. Wait for answers. Do
   not assume.
2. Create branch `plan/revamp` from `master` if the audit is merged there,
   otherwise from `audit/full-dig`. Set `docs/revamp/PHASE` to `plan` if Josh
   asked for the phase change.
3. Write `docs/revamp/PLAN.md` from the template:
   - Record the goals and every decision with its date.
   - One item per pull request. Each item names the finding IDs it fixes,
     says whether trading behaviour changes, whether any risk limit is
     touched, how it is tested, and how it is rolled back.
   - Order by the template's principles: protect the account, then
     visibility, then tests, then structure, then features.
   - Keep items small. An item that touches more than about five files or
     mixes a refactor with a behaviour change should be split.
   - Put every verified Critical and High finding either in an item or under
     "Not doing" with a reason. None may be silently dropped.
4. Commit, push, and open one draft pull request titled
   `docs(plan): revamp plan`. The description lists the items in order with
   one line each.
5. Stop. The plan phase ends when Josh approves the plan. Do not write code
   and do not change the phase.
