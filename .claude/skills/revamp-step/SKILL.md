---
name: revamp-step
description: Implement exactly one approved item from docs/revamp/PLAN.md as one pull request, with tests.
argument-hint: "[plan item id such as R-03]"
disable-model-invocation: true
---

Implement plan item: $ARGUMENTS

## Checks before starting

1. Read `docs/revamp/PHASE`. It must say `build`, or Josh must have said in
   this conversation that the plan is approved and the build phase starts
   (then set the file to `build` on your branch as part of this change).
   Otherwise stop; only Josh changes the phase.
2. Read `CLAUDE.md` and the item in `docs/revamp/PLAN.md`. If no item ID was
   given, or the item does not exist, or it depends on an item that has not
   been merged, or it has an open question, stop and say which.
3. Read the audit findings the item refers to, in
   `docs/revamp/audit/findings/`.
4. Start from an up-to-date `master`: create branch
   `<type>/<item-id>-<short-name>`, for example `fix/r-03-stop-after-fill`.

## Steps

1. **Understand first.** Read the code the item touches and everything that
   calls it. Write down, in a few lines, what the bot does today on this path
   and what it will do after.
2. **Pin current behaviour.** If the path has no tests, add tests that pass
   against the current code for the behaviour that must not change.
3. **Write the failing test** for the new behaviour, where the item changes
   behaviour.
4. **Make the change.** Only what the item says. No drive-by clean-ups; if
   you find something else, note it for the pull request under "Noticed, not
   changed".
5. **Run the checks:** `python3 -m compileall -q stock_agent` and
   `python3 -m pytest -q`. If `tests/test_risk_tripwires.py` fails, stop: you
   have changed trading behaviour. Only update that test if the plan item
   says the limit changes, and say so at the top of the pull request.
6. **Independent review.** Launch the `change-reviewer` subagent with the
   item ID. Fix what it finds, or explain in the pull request why not.
7. **Commit and push the branch.** Open one pull request titled
   `<type>(<scope>): <summary> [<item-id>]`.

## Pull request description

In this order:

1. **Risk-limit or trading-mode changes:** list them, or "None".
2. **What the bot will do differently** on its next cycle, in plain language.
3. **Why:** the finding IDs this fixes.
4. **Tests:** what was added and the pytest summary line.
5. **Before merging:** any new environment variable Josh must add to the
   droplet first, or "Nothing".
6. **Rollback:** how to undo it.
7. **Noticed, not changed.**

Then stop. Do not merge. Merging to `master` deploys to the live bot within
ten minutes and restarts it; that decision is Josh's.
