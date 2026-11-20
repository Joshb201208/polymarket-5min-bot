---
paths:
  - "stock_agent/risk_manager.py"
  - "stock_agent/config.py"
  - "stock_agent/executor.py"
  - "stock_agent/portfolio.py"
  - "stock_agent/scheduler.py"
---

# Money paths

These files decide what gets bought, how much, and when it is sold.

- Before changing a function here, find every caller and read each one.
- A risk check must use broker-reconciled positions and must account for the
  order being proposed, not only the portfolio before it.
- Never change a limit value, a default mode, or the Alpaca base URL as a
  side effect of another change. `tests/test_risk_tripwires.py` pins them.
- A failed broker call is not an empty result. Do not treat `None` or `[]`
  from a failed request as "no positions" or "no orders".
- Every order path must say what happens on a rejection, a partial fill, and
  a restart half-way through.
- Describe any behaviour change in plain language at the top of the pull
  request.
