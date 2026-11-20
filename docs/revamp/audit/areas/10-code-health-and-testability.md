# 10. Code health and testability

Output: `docs/revamp/audit/findings/10-code-health-and-testability.md`

## Scope

The whole `stock_agent/` package as a codebase: structure, dependencies
between modules, duplication, dead code, typing, and how hard it is to test.
Also `README.md` versus reality, and `tests/`.

## Why it matters

The revamp will change this code one small pull request at a time. This area
tells the plan where changes are safe, where they are dangerous, and what has
to be put in place first so that later changes can be checked.

## Questions to answer

1. Draw the module dependency map. Which modules know about which? Where are
   the cycles and the modules everything depends on?
2. `scheduler.py` is about 2,000 lines. What distinct jobs does it do? Which
   pieces could be lifted out without changing behaviour, and in what order?
3. What is dead: unused functions, unused config values, unreachable branches,
   leftovers from earlier designs? Give evidence that each is unused.
4. What is duplicated: HTTP client setup, retry-less request wrappers,
   `_safe_float` helpers, alert formatting, portfolio-value arithmetic? Where
   do the copies disagree with each other?
5. `Config` is a class whose attributes are read from the environment at
   import time. What does that do to testing and to configuration mistakes?
   Is anything validated?
6. What can be unit-tested today without the network, and what cannot,
   because I/O and decisions are mixed in one function? Name the five
   functions where a test would buy the most safety.
7. What seams would make the money paths testable (a broker interface, a
   clock, a data provider interface)? Describe them; do not build them.
8. Are dependencies in `requirements.txt` all used? Are any imports missing
   from it? What Python version does the code require, and what runs on the
   server?
9. Type hints, linting, formatting: what is the current state, and what does
   `ruff check stock_agent` report (counts by rule, not the full list)?
10. Where do the README, comments, and docstrings describe a bot that no
    longer exists? List the claims that are false today.

## Leads to check (unverified)

- Both `requests` and `aiohttp` and `httpx` are dependencies. Check which are
  really used and where.
- The repository and server path are named after an earlier Polymarket bot.
  List what else still refers to it.
- `stock_agent/macro_monitor.py` uses an f-string form that only parses on
  Python 3.12 or later. Check for other version-specific syntax and confirm
  the minimum version.
- Inline imports inside functions (for example `import asyncio` inside
  `place_buy`).
- `getattr(self.config, ..., default)` calls whose defaults differ from the
  values in `config.py`.

## Out of scope

Proposing the new architecture. That is the plan phase. Describe what is
there and what makes it hard to change.
