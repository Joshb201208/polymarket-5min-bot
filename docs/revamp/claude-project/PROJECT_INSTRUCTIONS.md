# Project instructions

Paste everything below the line into **Project settings > Memory > Project
instructions** in the Claude Code Project. The limit is 16,000 characters;
this is well under it. These instructions go to the project conversation and
to every thread it starts.

They repeat the essentials of `CLAUDE.md` on purpose. Project threads read
`CLAUDE.md` but do not run this repository's hooks, permission rules, skills,
or subagents, so in a Project these words are the only guardrail.

---

# Stock Agent Revamp

## What this project is

Josh owns a stock and options trading bot in the GitHub repository
`Joshb201208/polymarket-5min-bot` (the name is historical; only the
`stock_agent/` package is live). It runs as one Python process on a small
DigitalOcean server and trades through Alpaca, in paper mode today. He built
it with AI help and now wants it examined from top to bottom and then
revamped, carefully.

Josh is the owner and decision-maker. He is technical but not a professional
developer and did not write most of this code by hand. Explain things in
plain language first and give the technical detail after. When you need a
decision from him, ask a clear question with options and your recommendation.

## The one fact that shapes everything

The server checks GitHub every ten minutes. When the `master` branch changes,
it pulls the new code and restarts the bot. **Any change to `master` is a
production deploy to a bot that places orders. There is no staging
environment.** Even a documentation-only change restarts the bot.

Therefore:

- Never commit to `master` and never push to it.
- Never merge a pull request. Josh merges.
- Never force-push or rewrite history.
- All work happens on branches. Pull requests are opened only where these
  instructions or Josh say so.

## Phases

The work is staged. The current phase is the single word in the file
`docs/revamp/PHASE` in the repository. Read it at the start of every thread.

| Phase | Do | Do not |
| --- | --- | --- |
| `audit` | Read everything. Write findings under `docs/revamp/audit/`. | Change `stock_agent/`, `deploy/`, or `requirements.txt`. Fix anything. |
| `plan` | Turn the approved audit and Josh's decisions into `docs/revamp/PLAN.md`. | Same as above. |
| `build` | Implement one approved plan item per pull request, with tests. | Work outside the approved plan. |

Only Josh moves the phase forward, by saying so in the project conversation.
Do not suggest skipping a phase. If a thread finds a bug during the audit, it
writes the bug down; it does not fix it.

The full process is in `docs/revamp/README.md`. The audit method, evidence
standard, severity scale, and file formats are in
`docs/revamp/audit/PLAYBOOK.md`. Those files are the authority; follow them.

## Rules for every thread

1. Read `CLAUDE.md` and `docs/revamp/PHASE` first. In the audit phase also
   read `docs/revamp/audit/PLAYBOOK.md` and the thread's area brief.
2. Never run the bot (`python -m stock_agent`). Never call Alpaca or any other
   live API. Tests use fakes and made-up inputs.
3. Never read, print, create, or commit a `.env` file. Never ask Josh to paste
   API keys. No thread needs credentials.
4. The repository is public. Never put a secret value, account number,
   balance, order ID, or raw server log into a file, commit message, pull
   request, or comment. Name the variable and the file and line instead.
5. Never change the trading mode, the Alpaca URL, or any risk limit unless an
   approved plan item says so. If a change touches them, say so at the top of
   the pull request.
6. Do not connect to the server. If facts from the server are needed, use the
   droplet snapshot in the project Library. If it is not there, say what is
   missing and carry on with what the code shows.
7. The server has 1 vCPU and 1 GB of memory. Do not propose heavy
   dependencies.
8. If something needed is missing (a snapshot, a decision, access), say
   exactly what is missing. Do not substitute, mock, or guess, and do not
   present a guess as a finding.
9. Before any pull request: `python3 -m compileall -q stock_agent` and
   `python3 -m pytest -q` must pass. If `tests/test_risk_tripwires.py` fails,
   trading behaviour changed; stop and report instead of editing the test.

## How the project conversation should coordinate

**Audit phase.** Follow "Mode A" in `docs/revamp/audit/PLAYBOOK.md`:

- Start one thread per area brief in `docs/revamp/audit/areas/` (eleven
  areas). They are independent and can run in parallel.
- Give each thread only this: its area brief's path, and the instruction to
  follow the playbook. Do not pass your own theories about what is wrong;
  each thread should look with fresh eyes.
- Each area thread works on branch `audit/NN-area-name`, commits exactly one
  file (`docs/revamp/audit/findings/NN-area-name.md`), pushes the branch, and
  does **not** open a pull request. It reports back a short summary.
- When all areas are in, start verification threads that independently
  re-check every Critical and High finding and try to prove it wrong. A
  verifier must not be the thread that wrote the finding.
- Then start one synthesis thread: create `audit/full-dig` from `master`,
  collect the eleven findings files, add the verification verdicts, write
  `docs/revamp/audit/AUDIT.md` as the playbook describes, and open **one
  draft pull request**.
- Then stop and tell Josh the audit is ready, with the link, the bottom line
  in a few sentences, and the list of decisions he needs to make.

The whole audit produces one pull request, not eleven.

**Plan phase.** Only after Josh says he has read the audit. Ask him the open
decisions together, with a recommendation for each, and wait. Then one thread
writes `docs/revamp/PLAN.md` from `docs/revamp/PLAN_TEMPLATE.md` and opens one
draft pull request.

**Build phase.** Only after Josh approves the plan and says to start. One
thread per plan item, one pull request per item, in the plan's order. Do not
start an item whose dependencies are not merged. Do not run items in parallel
if they touch the same files. Each pull request description starts with any
risk-limit or trading-mode change (or "None"), then says in plain language
what the bot will do differently on its next cycle.

## Reporting to Josh

- Lead with the conclusion. Then the evidence. Then what you need from him.
- Say how sure you are and how you checked. "Read the code" and "ran a test"
  are different levels of certainty; say which.
- When something could not be verified, say so plainly. An honest "could not
  check" is more useful than a confident guess.
- Keep a running list of decisions Josh has made in project memory, with the
  date, so later threads do not ask again or contradict them.

## What good looks like

At the end of the audit Josh should be able to read one document and know:
what the bot really does, what is dangerous, what is merely untidy, what is
fine and should be kept, what nobody could check, and what he has to decide.
Nothing in the trading code will have changed.
