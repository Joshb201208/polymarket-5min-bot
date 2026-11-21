---
paths:
  - "stock_agent/options_*.py"
---

# Options code

- A covered call needs at least 100 shares per contract held at the broker at
  the moment of the order, and only out-of-the-money strikes.
- A cash-secured put needs the full assignment cost in cash, not buying power.
- Multi-leg positions are opened and closed as one order where the broker
  allows it. If legs can fill separately, the code must handle one filling
  without the other.
- Expired in-the-money legs are valued at intrinsic value, not zero.
- Options exposure and equity exposure share one account. A change here must
  say what it does to the equity side's cash and share counts.
