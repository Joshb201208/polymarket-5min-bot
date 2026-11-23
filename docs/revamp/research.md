# Research and live news

The deterministic screener ranks tradable companies by available financial factors. A rank only chooses what to research; it cannot authorize an order. Scheduled deep research prioritizes held names, then owner-focus names, then the highest-ranked remaining securities. A material, timestamped news item may prompt a fresh held review or a new thesis for a focus or universe name.

## Owner focus list

The versioned `stock_agent/owner_focus.json` starts as `[]`. Add an entry such as `{"symbol":"NVDA","note":"Monitor earnings revisions","added":"2026-11-23","expires":"2026-12-15"}` inside the array. Use ISO dates and adjust them for your review period. Symbols on the active list are polled for news and included in the next scheduled research pass; they do not bypass quote freshness, broker checks, risk, or conviction requirements. The change belongs in version control and takes effect once the file is updated on the running instance.

## Decision and spend controls

- **Equity entries**: News-triggered buys and adds require effective conviction 8 or higher and raw conviction 7 or higher, plus the usual data and risk gates. An add also needs a fresh thesis strengthened relative to the last filled thesis. A bearish view can only go to defined-risk options checks, not to an equity short.
- **Model-driven exits**: A blind second review uses a separate provider when available, otherwise a different model on the same provider, with a degraded flag. A cap of four model-driven exits per regular market session is independent of protective stop processing.
- **Weekly spending**: The default budget is $15 per New York week, with 20% held back for exit confirmations. Requests reserve a conservative configured estimate before they are sent. Unknown usage retains the reservation. A story seen on multiple feeds is evaluated once; completed equity proposals can be recovered after a restart without another model call.
- **Market posture**: The research posture switches to cautious when fresh SPY and QQQ returns are down at least 1% and 1.4% for the session, respectively. A posture switch can send an alert and raise the conviction floor, not start a trading job. Missing benchmarks do not trigger a switch.
- **Owner notes**: Provider sharing is off by default. If enabled, only explicitly approved, unexpired files with the prescribed front matter in `data/research_notes` are included as bounded, unverified context. They are not evidence, and the note directory should never include private account data or credentials.
