# Revamp plan template

Copy this to `docs/revamp/PLAN.md` in the plan phase and fill it in. The plan
is written from `docs/revamp/audit/AUDIT.md` and Josh's answers to its
decisions. It contains no code.

## Goals

Three to five sentences agreed with Josh: what the revamped bot should be able
to do, and what "done" looks like.

## Decisions recorded

| # | Question from the audit | Josh's decision | Date |
| --- | --- | --- | --- |

## Principles for ordering

1. Protect the account first: anything that can place a wrong order, exceed a
   limit, or leave a position unprotected.
2. Then visibility: knowing when the bot is down or misbehaving.
3. Then a safety net: tests around behaviour that is about to change.
4. Then structure: untangling code so later changes are small.
5. Features last.

## Items

Each item is one pull request, small enough to review in one sitting. Use the
ID in the branch name and PR title.

### R-01: Short title

- Fixes: F-03-02, F-07-01 (audit finding IDs)
- Why: one or two plain sentences.
- Change: what will be different, described as behaviour, not as code.
- Files likely touched:
- Trading behaviour change: None | Yes: exactly what changes on the next cycle
- Risk limits touched: None | Yes: which, from what, to what
- Tests: what will prove it works and what guards against regressions
- New environment variables: None | names (Josh adds them to the droplet first)
- Rollback: how to undo it (normally "revert the PR")
- Depends on: R-xx
- Size: S | M | L

(repeat)

## Not doing

Findings deliberately left alone, with a one-line reason each.

## Open questions

Anything still undecided. An item that depends on an open question is not
ready to build.
