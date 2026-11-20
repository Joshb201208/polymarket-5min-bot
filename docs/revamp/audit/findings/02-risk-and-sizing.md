# 02. Risk and sizing

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

This part of the bot decides whether a buy is allowed, how many shares to buy,
and where the stop-loss sits. The basic sizing sum is correct: a conviction
score of 7/8/9/10 buys 5/7/8/10% of the portfolio, rounded down to whole
shares, and the pinned tests pass. The limits around that sum are much weaker
than they read. Every cap (sector, group of related stocks, total invested)
looks at the portfolio *before* the order and ignores the order itself, so one
buy can land a sector at about 50% against a 40% cap. Top-ups to a stock the
bot already holds skip the sector, group, total-invested and cash checks
altogether. There is no real cash limit: the bot only checks it can afford one
share, so on a margin account it can buy with borrowed money. The most serious
problem is the stop-loss: there are two of them (an order resting at the
broker and a check in the bot's 30-minute loop), they use different prices,
and neither knows about the other, so the bot's own sell can be refused by the
broker or sent a second time after the broker has already sold. The numbers
the checks run on come from the bot's own notebook, which is matched to the
broker only at the start of the Sunday and Wednesday research runs and as a
side effect of the morning Discord brief, not at the moment of the order. Two
documented rules are not what the code does: the "volatility-adjusted 7-12%
stop" is always exactly 7%, and the "minimum conviction 7" drops to 6 when the
market falls.

## How it actually works

### The one buy path

Every stock buy goes through `StockAgentScheduler._execute_signals`
(`stock_agent/scheduler.py:1173-1314`); `executor.place_buy` has no other
caller (`scheduler.py:1264`). Signals reach it four ways:

| Source | Signal created | Conviction gate | When the order is sent |
| --- | --- | --- | --- |
| Weekly (Sun 18:00-20:59 ET) and midweek (Wed) `_deep_analyze` | `scheduler.py:984-996` | `macro.get_effective_min_conviction()` = 6, 7 or 8 | Queued in memory (`:437`, `:546`); sent at the next 09:30 tick (`:194-197`). The "market open" branches at `:433-434` and `:543-544` cannot be true at those hours. |
| Daily pre-market scan (08:00-09:00) | `:650-664` | hard-coded `>= 8` | Queued; sent at 09:30 |
| Earnings-reactive scan (07:00-08:00) | `:769-781` | hard-coded `>= 8` | Queued; sent at 09:30 |
| Market-event scan (during market hours) | `:1677` via `_deep_analyze` | macro value | Immediately (`:1679-1680`) |

Inside `_execute_signals`, per batch and then per signal:

1. `check_pdt_compliance` once for the whole batch (`:1178`).
2. Sector: a full fundamentals fetch, else `"Unknown"` (`:1193-1199`).
3. Price: `signal.entry_price`, the price seen at analysis time; a fresh quote
   is fetched only if that is zero (`:1201-1208`).
4. `RiskManager.can_open_position(state, sym, sector, price, conviction)`
   (`:1210`) on the local ledger `self.portfolio.state`.
5. Size: new position `calculate_position_size(pv, price, conviction)`
   (`:1252`); top-up `int((pv * min(target, cap) - current_value) / price)`
   (`:1235-1243`).
6. Stop price `get_stop_loss_price(price, beta)` (`:1258-1259`).
7. `executor.place_buy(sym, qty, stop_loss)` (`:1264`): market buy, 3 second
   sleep, then a separate GTC stop order (`executor.py:95-127`).
8. Ledger update: new position via `add_position` (cash debited at the signal
   price, `portfolio.py:93-98`); top-up only does `existing.shares += qty`
   (`scheduler.py:1271-1278`).

### Q1 and Q2. Every limit, where it lives, and what it counts

All checks are in `can_open_position` (`risk_manager.py:14-123`). None of them
adds the proposed order to the number it tests.

| Limit | Defined | Enforced | Counts the order? | Skipped by |
| --- | --- | --- | --- | --- |
| Per-symbol hard cap 15% | `config.py:50` (env `MAX_POSITION_PCT`) | Existing holdings only: `risk_manager.py:54-57`; top-up target `scheduler.py:1238-1239` | Top-up: yes (sized to target). New buy: not compared at all | New buys (`calculate_position_size` never reads it); any symbol missing from the local ledger |
| Conviction target 5/7/8/10% | `config.py:149-154` | `risk_manager.py:45-49`, `:58-62`, `:143-147`; `scheduler.py:1235-1243` | Yes, this is the sizing | - |
| Position count 20 | `config.py:52` | `risk_manager.py:66-67` | Effectively (blocks at 20, so max is 20) | Top-ups (intended) |
| Total exposure | `config.py:51` (env `MAX_TOTAL_EXPOSURE_PCT`, default 1.00) | `risk_manager.py:70-73` | No | Top-ups; cannot trigger while cash is positive (F-02-05) |
| Sector 40% | `config.py:53` | `risk_manager.py:77-98` | No | Top-ups; sector `"Unknown"`; differently-labelled positions (F-02-10) |
| Cluster 48% | `config.py:57` (env `MAX_CLUSTER_PCT`), lists `config.py:71-98` | `risk_manager.py:102-117` | No | Top-ups; symbols in no cluster |
| Cash | no setting | `risk_manager.py:120-121` | No: tests one share, not the order | Top-ups |
| Minimum conviction 7 | `config.py:167` | Not in `RiskManager`. `scheduler.py:984` (6/7/8 by regime), `:650`, `:769` (8) | n/a | Lowered to 6 by the macro regime (F-02-07) |
| Pattern day trader | no setting | `risk_manager.py:166-198`, called at `scheduler.py:1178` | n/a | Sells; options orders; fills the bot did not record |
| Stop-loss 7-12% | `config.py:162-163` | `risk_manager.py:151-164`; broker order `executor.py:117-127`; loop `scheduler.py:1047-1053` | n/a | See F-02-01, F-02-09 |

### Q3. Sizing, with numbers

Portfolio $100,000, share price $333 (run through the real functions):

| Conviction | Target | Shares | Cost | Stop sent |
| --- | --- | --- | --- | --- |
| 7 | 5% = $5,000 | 15 | $4,995 | $309.69 |
| 8 | 7% = $7,000 | 21 | $6,993 | $309.69 |
| 9 | 8% = $8,000 | 24 | $7,992 | $309.69 |
| 10 | 10% = $10,000 | 30 | $9,990 | $309.69 |

Top-ups use a second copy of the maths in the scheduler, not
`calculate_position_size`. A top-up is allowed while the holding is below 90%
of `min(conviction target, 15%)` (`risk_manager.py:58`). Holding 12 shares
($3,996, 4.0%) at conviction 8: `int((7,000 - 3,996) / 333)` = 9 more shares,
total 21. Rounding down and the `min(target, cap)` stop a top-up from passing
the target, provided the prices and the portfolio value are right. Two things
break that proviso: the divisor is the stale signal price (signal $300, real
$333 gives 10 shares and 7.33%), and an earlier top-up in the same batch
inflates the portfolio value (F-02-04).

Top-ups are generated automatically: `_deep_analyze` does not skip held
symbols, so any held name that scores BUY again and sits below 90% of target
(for example because its price fell) is bought back up to target.

### Q4. The portfolio value used for sizing

`Portfolio.get_portfolio_value()` = local cash + local share count x local
price (`portfolio.py:227-230`); `can_open_position` recomputes the same sum
(`risk_manager.py:38-40`). It is not Alpaca's equity figure. Cash is copied
from Alpaca's `cash` field at each reconcile (`portfolio.py:214-219`) and
then adjusted locally. Prices come from the 30-minute monitoring pass or the
reconcile. Options are filtered out (`portfolio.py:131-138`), so option
positions and collateral are not in it (F-02-12).

Freshness at the moment of an order: reconcile runs at the top of the weekly
and midweek runs (`scheduler.py:371`, `:491`), which is 12 to 37 hours before
those orders are sent, and inside the morning brief at 08:15-08:45 ET
(`:1731-1733`). So a normal 09:30 batch runs on numbers 45-75 minutes old;
the market-event path has no reconcile (F-02-02).

### Q5. Stop-loss: computed, stored, enforced

- Computed: `entry * (1 - pct)` rounded to cents, `pct = beta * 5%` clamped to
  7-12%, or 7% with no beta (`risk_manager.py:157-164`). Beta is never
  available, so it is always 7% (F-02-09).
- Stored: `Position.stop_loss` (`scheduler.py:1287`) and
  `thesis.stop_loss_price` (`:1260-1261`). Nothing reads `Position.stop_loss`;
  the thesis copy is only shown in alerts.
- Enforced twice: a GTC stop order at the broker per buy, priced off the
  signal price (`executor.py:117-124`); and the loop, which recomputes 7% off
  `pos.entry_price` every 30 minutes (`risk_manager.py:211-216`).
  `pos.entry_price` is the signal price until the next reconcile overwrites
  it with the broker's average fill (`portfolio.py:164`). They are the same
  number only until that first reconcile.
- After a top-up: a second stop order is placed for the added shares at 7%
  below the new signal price. The first stop order is untouched. The ledger's
  `entry_price` and `stop_loss` are not updated (proof run: after a top-up
  from 20 to 100 shares, `entry_price` and `stop_loss` were unchanged). After
  the next reconcile the loop uses 7% below the blended average. That leaves
  three different stop levels for one holding.

### Q6. Pattern day trader check

It is called once at the top of every equity buy batch (`scheduler.py:1178`).
It counts symbols that have both a BUY and a SELL on the same UTC date in the
local trade history over the last 7 calendar days and blocks buying at 3
(`risk_manager.py:171-195`). Seven calendar days covers five to six business
days, so the window errs on the safe side. See F-02-13 for what it misses.

### Q7. Missing data

| Input | What happens | Reference |
| --- | --- | --- |
| Sector unknown and symbol not in `SECTOR_MAP` | Sector check skipped | `risk_manager.py:83`, `:90` |
| Symbol in no cluster | Cluster check skipped (by design) | `:104-106` |
| Price zero, new position | Size 0, no order | `:139-140` |
| Price zero, top-up | `ZeroDivisionError`, whole batch aborts | `scheduler.py:1243` (F-02-15) |
| Held position with `current_price` 0 | Counts as 0% weight everywhere; stop check skips it | `risk_manager.py:52`, `:212-213` |
| Conviction not 7-10 | Sized as conviction 7 (5%); `RiskManager` has no floor | `:45-47`, `:143-145` |
| Portfolio value zero or negative | Buy blocked | `:41-42` |

### Q8. Comments and documents versus code

Listed in F-02-16.

### The macro regime

`MacroMonitor._compute_regime` (`macro_monitor.py:264-356`) adds up points
from the S&P 500 level against fixed numbers (`:48-52`), its drawdown from the
90-day high, VIX, oil, bonds and the dollar. The only thing it changes in this
area is the conviction gate for weekly, midweek and event scans: 6 in the two
"dip" regimes, 8 in "cautious", 7 otherwise (`:335-354`, `:369-373`). It does
not change any cap, any size, or any stop. The docstring's "tighten stops"
(`:11`, `:41`) has no code behind it, and `get_effective_universe_size` is
only displayed in the morning brief (`scheduler.py:1867`); the screener uses
`config.UNIVERSE_SIZE` directly (`data_feed.py:291`, `:330`).

## Findings

### F-02-01: Two stop-losses that do not know about each other; the bot's own sells never cancel the broker's stop or check the broker's position
- Severity: Critical
- Confidence: Likely
- Where: `stock_agent/executor.py:117-127`, `:136-161`, `:163-188`; `stock_agent/scheduler.py:1047-1053`, `:1316-1326`; `stock_agent/risk_manager.py:205-225`; `stock_agent/portfolio.py:160-162`, `:180-181`
- What happens: Each buy leaves a stop order resting at the broker. Separately, every 30 minutes the bot compares prices with its own stop level and, if breached, sends a plain market sell for the full share count. The bot also sells this way when a thesis breaks. Nothing cancels the resting stop first, and nothing asks the broker what is actually held.
- Evidence:
  - Stop at the broker, per buy, off the signal price: `executor.py:117-124` (`"type": "stop"`, `"time_in_force": "gtc"`, `"qty": str(qty)`).
  - Loop stop, off the ledger's entry price: `risk_manager.py:216` `if self.should_stop_loss(pos.entry_price, pos.current_price, beta)`, then `scheduler.py:1053` `await self._execute_sell(pos, reason=alert["reason"])`, then `scheduler.py:1323` `order = await self.executor.place_sell(position.symbol, position.shares)`.
  - `Executor.cancel_order`, `get_order`, `get_open_orders`, `cancel_all_orders` (`executor.py:163-188`) have no callers anywhere in `stock_agent/`.
  - The comment at `scheduler.py:1046` says "belt-and-suspenders with Alpaca bracket orders"; `executor.py:83-89` says bracket orders are not used.
  - Whatever the broker does, no branch is correct: (a) if the resting stop reserves the shares, the bot's sell is refused and `_execute_sell` just logs and returns (`scheduler.py:1324-1326`), so thesis-broken and material-event exits do not happen; (b) if the broker's stop has already sold, the ledger still shows the position until the next reconcile, the loop sees the same breach and sends a second sell for shares that are gone, which a margin account with shorting enabled accepts as a short sale; (c) if the bot's sell does go through while a stop rests, that stop is left behind and can fire later with no shares held.
  - A short would be invisible: reconcile drops or skips any position with `shares <= 0` (`portfolio.py:160-162`, `:180-181`). Proof run with a made-up broker position of -50 shares: local positions `[]`, local portfolio value 105,000 against a true 100,000.
- Why it matters: Either exits fail silently, or a stop-out can turn into an unmonitored short position with no stop. Both are outside any limit the bot has.
- How verified: Read every call path into `place_sell` and searched for callers of the cancel and open-order helpers. Ran `Portfolio.sync_from_alpaca` with a negative-quantity position. The broker's response to each case was not tested (no API calls allowed); that is the untested link.
- Direction (not a fix): One owner for the stop. Before any bot-initiated sell, read the broker's position and open orders for that symbol, cancel what rests, and sell only what is held.

### F-02-02: Risk checks run on the bot's own ledger, which is not matched to the broker at the moment of the order
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:194-197`, `:370-371`, `:433-442`, `:490-491`, `:808-823`, `:1210-1213`, `:1663-1680`, `:1716-1733`; `stock_agent/executor.py:75-80`
- What happens: The reconcile that was added after the 76% incident runs at the start of the Sunday and Wednesday research. Those orders are sent at 09:30 the next trading day. The only reconcile near the order is a side effect of the morning Discord brief. The market-event path buys with no reconcile. When a reconcile fails, the bot carries on with what it had.
- Evidence:
  - `scheduler.py:370` `# 0a. Reconcile with Alpaca (source of truth) BEFORE any risk decisions.` The risk decision is at `:1210`, reached from `:196` on a later day.
  - `_execute_signals` (`:1173-1314`) contains no broker read.
  - `:822-823` `except Exception as exc: logger.exception("Alpaca sync failed — continuing with local state: %s", exc)`.
  - Morning brief reconcile is conditional: `:1719 if alpaca_account:` and `:1732 if alpaca_positions:`.
  - `executor.get_positions` returns `[]` on any failure (`executor.py:77-80`); `_sync_portfolio_from_alpaca` only skips when the account call *also* failed (`scheduler.py:818`). Proof run: ledger with two positions, `sync_from_alpaca([], {"cash": "20000"})` leaves `positions=[]`, portfolio value 20,000 instead of 100,000, and `can_open_position` for a symbol really held at 40% returns `(True, 'OK')`.
  - Orders sent but not yet filled are not counted by any check.
- Why it matters: A symbol the ledger has lost is treated as new and bought again at full size. That is the mechanism of the past incident; it is narrower now but still open. In the normal schedule the morning brief repairs the ledger before 09:30, so a breach needs a second failure (brief skipped or its account call failing).
- How verified: Traced all five call sites of `_execute_signals` and all three reconcile sites; ran the wipe case with made-up data.
- Direction (not a fix): Reconcile inside the buy path, immediately before the checks, and refuse to buy when the broker read fails rather than falling back.

### F-02-03: Sector, cluster and total-exposure caps test the portfolio before the order; the sector "soft block" does nothing
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/risk_manager.py:70-73`, `:84-98`, `:107-117`
- What happens: Each cap asks "is it already at or over the limit?" and never adds the order being considered. A single buy can therefore land up to one full position size (10 points) above each cap.
- Evidence:
  - Sector: `:90 if resolved_sector != "Unknown" and sector_pct + (conviction_target or 0) * 0.5 >= self.config.MAX_SECTOR_PCT:` wraps `:94 if sector_pct >= self.config.MAX_SECTOR_PCT:`. Only the inner test returns False, and it implies the outer one, so the outer line has no effect.
  - Proof run, $100,000 portfolio, conviction-10 buy: sector at 36.0% allowed, 46.0% after; 39.6% allowed, 49.6% after; 40.0% blocked.
  - Cluster: `:113 if cluster_pct >= cluster_cap:`. Proof run: semiconductors at 47.5% allowed, 57.5% after; 48.0% blocked.
  - Exposure: `:72 if exposure_pct >= self.config.MAX_TOTAL_EXPOSURE:`. Proof run with the limit set to 0.50: 49.9% allowed, 59.9% after.
- Why it matters: The real ceilings are about 50% per sector and 58% per cluster, not 40% and 48%. The overshoot is bounded at one position per cap for new buys; top-ups add to it (F-02-04).
- How verified: Ran `can_open_position` and `calculate_position_size` with made-up portfolios either side of each cap.
- Direction (not a fix): Test the portfolio as it would be after the order, and either shrink the order to fit or reject it.

### F-02-04: Top-ups skip the sector, cluster, exposure and cash checks, and are not deducted from cash in the ledger
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/risk_manager.py:51-63`; `stock_agent/scheduler.py:1227-1250`, `:1271-1278`; `stock_agent/portfolio.py:81-83`
- What happens: When the symbol is already held, `can_open_position` returns before any portfolio-level check. After the order, the scheduler raises the share count but never reduces cash, so the bot's idea of its portfolio value grows by the cost of every top-up until the next reconcile.
- Evidence:
  - `risk_manager.py:63 return True, "ADD_TO_EXISTING"` sits above the count, exposure, sector, cluster and cash checks (`:65-121`).
  - Proof run: portfolio on margin (cash -5,000, exposure 105%, semiconductor cluster 105%, technology sector 105%); top-up of a held 5% position at conviction 10 returns `(True, 'ADD_TO_EXISTING')`; a new name in the same portfolio is blocked.
  - `scheduler.py:1275-1278` only does `existing.shares += qty`, `market_value`, `last_updated`, `_save()`. `portfolio.log_trade` does nothing for a BUY (`portfolio.py:81-83`).
  - Proof run through the real `_execute_signals` with fake broker and data feed: portfolio 100,000, top-up of 80 shares at $100, then a new conviction-10 buy in the same batch was sized at 108 shares instead of 100; ledger portfolio value ended at 108,000.
- Why it matters: Top-ups can push a sector or cluster further over its cap and can be made with no cash. Each one also makes every later order in the batch slightly too large.
- How verified: Read the path; ran both proofs above.
- Direction (not a fix): Run the same portfolio-level checks for top-ups, and make the ledger treat a top-up exactly like a new buy for cash.

### F-02-05: There is no working cash or total-exposure limit, so the bot can buy with borrowed money
- Severity: High
- Confidence: Confirmed (code); the account type and the live setting are unverified
- Where: `stock_agent/config.py:51`; `stock_agent/risk_manager.py:70-73`, `:119-121`; `.env.example:19`; `deploy/setup.sh:33-37`; `deploy/stock-agent.service:10`
- What happens: The cash check only asks whether one share is affordable. The total-exposure limit defaults to 100%, and because exposure is stocks divided by (cash + stocks) it can only reach 100% when cash is zero or negative. So neither stops an order that is larger than the cash available.
- Evidence:
  - `risk_manager.py:120 if state.cash < price:`.
  - `config.py:51 MAX_TOTAL_EXPOSURE = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", "1.00"))  # 50%`. The default moved from 0.50 to 1.00 in commit 7d4e34a; the comment did not.
  - Proof run: portfolio 99,150 with cash $150, price $100, conviction 10: allowed, order 99 shares ($9,900). Portfolio at 99.90% exposure with $100 cash: allowed, exposure after 109.8%, cash after -9,700. Blocked only when cash is exactly 0 or below.
  - Reconcile keeps negative balances on purpose: `portfolio.py:214 # 3. Sync cash — always, including negative (margin) balances.`
  - The live value may differ: `.env.example:19` carries `MAX_TOTAL_EXPOSURE_PCT=0.50` in its old Polymarket section, `deploy/setup.sh:35` creates the server's settings file by copying `.env.example`, and the service loads that file (`stock-agent.service:10`). If that line is still present on the droplet the limit there is 50%, not the 100% the tests pin.
- Why it matters: On a margin account the broker fills these orders with borrowed money, which is leverage nobody chose. On a cash account the broker rejects them instead. Either way the bot does not know its own limit. Once exposure passes 100% new positions stop, but top-ups do not (F-02-04).
- How verified: Ran the functions with made-up portfolios; read the deploy files; checked git history for the default.
- Direction (not a fix): Decide the intended maximum invested share and whether borrowing is ever allowed, then check the full order cost against cash (or broker buying power) after the trade.

### F-02-06: Queued buys use the price from analysis time for sizing, for the stop, and as the recorded entry price
- Severity: High
- Confidence: Confirmed (code path); the broker's handling of an above-market stop is untested
- Where: `stock_agent/scheduler.py:194-201`, `:658`, `:775`, `:990`, `:1201-1208`, `:1259`, `:1280-1286`
- What happens: A signal carries the price seen when the research ran (Sunday evening, or 07:00-09:00 on a weekday). At 09:30 the bot uses that old price to work out the share count and the stop, and writes it down as the entry price. It never re-quotes, and it never checks how old the signal is or whether the bot sold that symbol in between.
- Evidence:
  - `:1201 price = signal.entry_price or 0` then `:1202 if price <= 0:` is the only path to a fresh quote.
  - The 09:30 tick sends the queued buys (`:196`) and then runs the monitoring pass in the same tick (`:199-201`), which fetches live quotes and applies the stop check (`:1043-1053`).
  - Proof run through the real `_execute_signals` with fakes, signal priced at $100, stock actually at $91: order `('BUY', 50 shares, stop 93.0)`; ledger `entry_price=100.0 stop_loss=93.0`; the first monitoring pass returns `STOP_LOSS, "Stop-loss triggered: $91.00 <= $93.00", -9.0%`.
  - Same with the stock at $112: 100 shares cost $11,200 (11.2% against a 10% target) and the $93 stop is 17.0% below the real fill, outside the 7-12% range.
- Why it matters: After a gap down the bot buys and then tries to sell in the same minute, recording a loss it did not make and a same-day round trip (F-02-13). After a gap up it overspends and the stop is much looser than intended. The earnings-reactive scan is the most exposed, because it picks stocks that have just reported.
- How verified: Read the path; ran both cases.
- Direction (not a fix): Re-quote at order time, drop the signal if the price has moved beyond a set tolerance or the signal is too old, and base the stop on the actual fill.

### F-02-07: The macro regime lowers the minimum conviction to 6 during sell-offs
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/macro_monitor.py:48-52`, `:270-279`, `:335-344`, `:369-373`; `stock_agent/scheduler.py:984`; `stock_agent/risk_manager.py:45-47`, `:143-145`
- What happens: When the regime score is -3 or lower the gate for weekly, midweek and event-triggered buys becomes 6. A conviction-6 buy is sized like a 7 (5%). `CLAUDE.md` records "Minimum conviction is 7" as a decision and the tripwire test pins `MIN_CONVICTION == 7`, but that constant is only the fallback.
- Evidence:
  - `macro_monitor.py:337 snap.min_conviction_override = 6      # Lower bar — more stocks qualify` and `:342`.
  - `scheduler.py:984 if thesis.direction == "BUY" and thesis.conviction >= self.macro.get_effective_min_conviction():`.
  - Proof run of `_compute_regime` with made-up snapshots: drawdown 8.5% and VIX 26 gives score -3, `DIP_OPPORTUNITY`, minimum 6; S&P below the hard-coded 6,200 with a calm VIX of 18 also gives -3 and minimum 6; S&P above the hard-coded 6,653 with VIX 14 gives +3, `CAUTIOUS`, minimum 8; all quotes failing (zeros) gives `NORMAL`, minimum 7.
  - `calculate_position_size(100_000, 100.0, 6)` returns 50 shares, the same as conviction 7.
  - The index levels are constants in code (`:48-52`). If the index sits below 6,200 for a long period the bot stays in a dip regime, and the bar stays at 6, for as long as that lasts.
  - `RiskManager` has no conviction floor: `can_open_position(..., conviction=3)` on an empty portfolio returns `(True, 'OK')`.
  - The change is announced, not hidden: the Discord macro brief says "lowering conviction threshold to 6/10" (`macro_monitor.py:468-477`).
- Why it matters: The bar drops exactly when prices are falling, and with no effective exposure limit (F-02-05) the bot can fill up on weaker ideas in one morning. A data outage silently cancels the stricter "cautious" setting.
- How verified: Ran `_compute_regime` and the getters with made-up snapshots; ran sizing for conviction 6.
- Direction (not a fix): Josh decides whether 7 is a hard floor. If it is, enforce it in one place the regime cannot override.

### F-02-08: Exits decided while the market is closed are either dropped or booked as if already filled
- Severity: High
- Confidence: Confirmed (code); the broker queuing the order until the open is standard behaviour, not tested here
- Where: `stock_agent/scheduler.py:570`, `:606-624`, `:725-729`, `:1021-1026`, `:1320-1346`
- What happens: The pre-market scan (08:00-09:00) finds a severe negative event, re-analyses, gets SELL, and then only logs "Queuing sell"; nothing is queued. The weekly, midweek and earnings paths do call `_execute_sell` while the market is closed, and the ledger immediately removes the position and adds cash at the last quote.
- Evidence:
  - `:614 if self._is_market_open(now):` uses `now` captured at `:570`, always before 09:30, so the branch at `:615` cannot run. The `else` at `:618-622` is a log line only. Because the first `if` matched, the `elif new_thesis:` at `:623` is skipped, so the SELL thesis is not stored either.
  - `_execute_sell` is called with no market-hours test at `:727` (07:00-08:00), and at `:1024` from the Sunday and Wednesday evening runs (`:430`, `:540`).
  - `:1321 sell_price = quote["price"] if quote else position.current_price`, then `:1345-1346` remove the position and log the trade at that price.
- Why it matters: The exit the bot judged most urgent is missed; the next chance is a fresh pair of AI calls at 10:00 or later that may not agree. For the evening sells, the ledger shows cash the account does not have yet, and the next reconcile re-imports the position as a placeholder.
- How verified: Read each call site and the schedule windows.
- Direction (not a fix): A real pending-sell queue that is executed at the open, with the ledger updated from fills, not from intent.

### F-02-09: The stop-loss is never volatility-adjusted; it is always 7%
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/risk_manager.py:157-162`, `:215`; `stock_agent/scheduler.py:1258`; `stock_agent/models.py:58-72`; `stock_agent/analyst.py:271-285`
- What happens: The stop widens with a stock's beta, but beta is read from the thesis and the thesis has no such field. Every stop is therefore the 7% floor, and the 12% ceiling is unreachable.
- Evidence: `scheduler.py:1258 beta = getattr(signal.thesis, "beta", None)`; `Thesis` (`models.py:58-72`) declares no `beta`, and `_parse_thesis` does not set one. Proof run: `"beta" in Thesis.model_fields` is False; stop for a $100 entry is 93.0 with beta None, 0.5 or 1.0. `beta` appears nowhere else in `stock_agent/` outside the risk manager and that one line.
- Why it matters: `CLAUDE.md` lists "volatility-adjusted and clamped to 7-12%" as a decision already made. Volatile names get the same tight stop as defensive ones, which is the whipsaw problem the 5% to 7% change was meant to ease.
- How verified: Searched for every use of `beta`; ran `get_stop_loss_price`.
- Direction (not a fix): Either feed a real volatility measure in from market data or document the stop as a flat 7%.

### F-02-10: The sector cap cannot see positions filed under a different label or under "Unknown"
- Severity: Medium
- Confidence: Likely
- Where: `stock_agent/risk_manager.py:77-90`; `stock_agent/config.py:102-146`; `stock_agent/portfolio.py:204`; `stock_agent/data_feed.py:196`, `:281-289`; `stock_agent/scheduler.py:1193-1199`, `:1945-1963`
- What happens: A position bought by the bot is labelled with the data provider's sector name. A position imported from the broker, or back-filled at start-up, is labelled from the bot's own `SECTOR_MAP`. The two lists use different words for the same sector, and the check compares the words exactly.
- Evidence: `SECTOR_MAP` uses `"Financials"` and `"Consumer Discretionary"` (`config.py:115-137`); the provider names the code itself sends to the screener are `"Financial Services"` and `"Consumer Cyclical"` (`data_feed.py:281-289`). `SECTOR_MAP` also files some large names under Technology that the provider's list would put under Communication Services. Proof run: five banks labelled `"Financials"` at 45%; a new buy labelled `"Financial Services"` returns `(True, 'OK')`, while the same buy labelled `"Financials"` is blocked. A buy whose sector is unknown and unmapped is also allowed (`:83`, `:90`).
- Why it matters: After any re-import of the ledger (see F-02-02) most holdings carry map labels and new financial or consumer buys are not counted against them. The cluster cap catches the symbols it lists; anything else is uncapped by sector.
- How verified: Ran the check with both labels. The provider's exact label in a live profile response was not fetched; a look at the sector values in the droplet's `portfolio.json` would settle it.
- Direction (not a fix): One sector vocabulary, applied in one place, and a defined rule for unknown sectors.

### F-02-11: `_execute_sell` crashes after every accepted sell, sending a false "Sell failed" alert and no trade alert
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1345-1350`, `:1364-1370`
- What happens: After the sell order is accepted and the ledger is updated, the code reads `self._tipranks_lookup`, which is never created. The error handler then reports the sell as failed.
- Evidence: `:1348-1350 tr_clause = format_tipranks_telegram_clause(self._tipranks_lookup.get(position.symbol.upper()))`. `_tipranks_lookup` is assigned nowhere in the repository; it arrived in commit 7a8ac46 (2026-04-23). Proof run through the real `_execute_sell` with fakes: broker order `('SELL', 'OUT', 50)` sent, position removed, SELL trade logged, then the only messages sent were Telegram `send_error_alert("Sell failed for OUT: 'StockAgentScheduler' object has no attribute '_tipranks_lookup'")` and Discord `send_error(...)`. No `send_trade_alert`.
- Why it matters: Josh is told a sell failed when it went through, and gets no sell confirmation. Acting on that message by hand would sell twice.
- How verified: Search plus the run above.
- Direction (not a fix): Remove or define the lookup, and report from the broker's order result.

### F-02-12: The portfolio value used for sizing ignores options and is built from cash, not broker equity
- Severity: Medium
- Confidence: Confirmed (code); size of the effect needs account data
- Where: `stock_agent/portfolio.py:131-138`, `:214-219`, `:227-230`; `stock_agent/risk_manager.py:38-40`, `:120`; `stock_agent/scheduler.py:1225`, `:1385`, `:1720`
- What happens: Sizing uses local cash plus local stock positions. Option contracts are filtered out of the ledger, but the broker's cash figure already includes premiums paid and received. Cash set aside for a cash-secured put is not reserved. A short stock position would be left out while its sale proceeds stay in cash.
- Evidence: `_is_equity` drops option symbols (`portfolio.py:131-138`); `get_portfolio_value` sums cash and stock only (`:227-230`). The broker's `equity` is read only for display in the morning brief (`scheduler.py:1720`); `buying_power` is never read. The same stock-only figure is passed to the options engine as `portfolio_value` (`:1385`, `:1455`).
- Why it matters: Every percentage limit is measured against a number that drifts from the real account value as options activity grows, and stock buys can spend cash that backs an open put.
- How verified: Read the reconcile and every use of `get_portfolio_value`.
- Direction (not a fix): Take account value and available cash from the broker's account record at order time.

### F-02-13: The pattern-day-trader check works from the bot's own notes, blocks only buys, and can freeze buying for a week
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/risk_manager.py:166-198`; `stock_agent/scheduler.py:1178-1185`, `:196-197`
- What happens: The check counts same-day buy-and-sell pairs in the local trade history. It does not use the broker's own day-trade count or the account's size, it is not applied to sells or options orders, and it cannot see a stop order that filled at the broker.
- Evidence: `:195 compliant = day_trades < 3`; `:172 five_days_ago = now - timedelta(days=7)`. Proof run: three round trips six calendar days ago returns False; eight days ago returns True; two round trips in one symbol on one day count as one. When it trips, `_execute_signals` returns an empty list and the caller clears the queue (`scheduler.py:196-197`), so the signals are discarded; the alert says "no new trades today" (`:1181-1182`) although the block lasts until the trades age out of the 7-day window.
- Why it matters: The count can be too low (broker-side stop fills) or needlessly restrictive (the rule does not apply to accounts above the regulatory threshold, and a sell followed by a re-buy is counted). The same-minute buy and sell in F-02-06 is exactly the kind of trade that feeds it.
- How verified: Ran the function with made-up histories; read the call site.
- Direction (not a fix): Use the broker's day-trade count and account status, and decide whether the rule applies to this account at all.

### F-02-14: Limit settings are not validated, and the 15% hard cap is not applied to new buys
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/config.py:50-57`; `stock_agent/risk_manager.py:44-49`, `:139-149`
- What happens: The three limits that can be set from the environment are passed straight to `float()`. A value typed as a percentage number is accepted and silently switches the limit off. Separately, the hard cap is only consulted for top-ups.
- Evidence: Proof run importing `Config` with made-up environment values: `15` gives 15.0 for all three; `-1` gives -1.0; `15%` or an empty value raises `ValueError` at import, so the service cannot start. With `MAX_CLUSTER_PCT=48` a cluster at 90% passes. With `MAX_POSITION_PCT=0.03` a new conviction-10 buy is still allowed and sized at 10%, because `calculate_position_size` never reads the cap. With `MAX_POSITION_PCT=15` and a holding at 60%, the add is still blocked, but only by the conviction-target rule (`:58`).
- Why it matters: At today's values the 15% cap never decides anything for a buy: new buys are at most 10% and top-ups stop at 90% of target. Lowering it would not restrain new buys, and a typo in the other two removes the cap without any warning.
- How verified: Ran the import and the checks with made-up values.
- Direction (not a fix): Validate ranges at start-up and fail loudly; apply the cap inside sizing.

### F-02-15: One bad price aborts the whole buy batch, and the retry loop then blocks monitoring
- Severity: Medium
- Confidence: Confirmed (mechanism); the trigger is rare
- Where: `stock_agent/scheduler.py:194-201`, `:1187-1243`; `stock_agent/data_feed.py:236`; `stock_agent/risk_manager.py:52`
- What happens: The loop over signals has no per-signal error handling. A top-up whose price comes back as zero divides by zero. The exception leaves `_execute_signals`, skips the line that clears the queue, and the same batch is retried every minute. The stop-loss pass sits after that line in the same tick, so it stops running.
- Evidence: `:1243 qty = int(additional_value / price)`. Proof run: batch of a zero-priced top-up followed by a normal buy raises `ZeroDivisionError`; no orders placed. `:196-197` clear the queue only after a normal return; `:199-201` run monitoring afterwards. A quote can carry price 0 (`data_feed.py:236 "price": q.get("price", 0)`). A related case: a held position whose `current_price` is 0 weighs 0% in every check (`risk_manager.py:52`), so a top-up to full target is allowed.
- Why it matters: A single data glitch on one symbol could halt all buying and the 30-minute stop check for the rest of the session, with an error alert every minute.
- How verified: Ran the batch; read the tick ordering.
- Direction (not a fix): Handle each signal on its own, treat zero or missing prices as "no data", and make sure monitoring cannot be starved by the buy path.

### F-02-16: Comments, docstrings and documents disagree with the code
- Severity: Low
- Confidence: Confirmed
- Where: see table
- What happens: Several statements a reader would rely on are wrong.
- Evidence:

| Where | It says | The code does |
| --- | --- | --- |
| `risk_manager.py:154-155` | "clamped to [5%, 10%] ... defaults to 5%" | Clamp is 7-12%, default 7% (`config.py:162-163`) |
| `risk_manager.py:162` | `# Default 5%` | 7% |
| `risk_manager.py:142`, `:144` | "default to minimum 3% for conviction 7", fallback `0.03` | Falls back to the conviction-7 tier, 5%; `0.03` is unreachable |
| `risk_manager.py:91-93` | A soft block so "a single new full-size buy can't slam into the ceiling" | No effect (F-02-03) |
| `risk_manager.py:103` | Fallback cluster cap `0.45` | Setting is `0.48`; fallback unreachable |
| `config.py:51` | `# 50%` | Default is 1.00 |
| `config.py:48-49` | Headroom "before we force a trim" | No trim code exists anywhere |
| `config.py:164` | `STOP_LOSS_PCT` "used if beta unavailable" | Only shown in the Discord start-up message (`discord_alerts.py:137`) |
| `scheduler.py:370`, `:490` | Reconcile "BEFORE any risk decisions" | Risk decisions happen at 09:30 on a later day (F-02-02) |
| `scheduler.py:1046` | "Alpaca bracket orders" | Separate market and stop orders (`executor.py:83-89`) |
| `scheduler.py:1257` | "Volatility-adjusted stop-loss" | Always 7% (F-02-09) |
| `scheduler.py:659-661`, `:776-778`, `:991-993` | `position_size_pct` with fallback `0.03` | Field is never read |
| `portfolio.py:202` | Imported positions get `stop_loss = entry * 0.95` | Field is never read; loop uses 7% |
| `macro_monitor.py:11`, `:41` | Cautious regime will "tighten stops" | Only raises the conviction gate |
| `macro_monitor.py:470-476` | "Widening scan to 80 stocks" | Universe size is not changed by the regime |
| `README.md:46` | `MIN_CONVICTION` is a flag, floor 7 | Not settable from the environment; regime lowers it to 6 |
| `README.md:48` | `POSITION_SIZE_PCT_BY_CONVICTION` | No such name; it is `CONVICTION_SIZE_MAP` |
| `README.md:15-16` | Sunday "portfolio rebalance", Wednesday "risk review" | No rebalance or review step; both runs are scan, analyse, re-analyse |
| `CLAUDE.md` decisions | Stop "volatility-adjusted", "Minimum conviction is 7", ledgers "reconcile ... before risk checks" | F-02-09, F-02-07, F-02-02 |

- Why it matters: Josh and any future session will be misled about what protects the account.
- How verified: Compared each statement with the code path it describes.
- Direction (not a fix): Correct or delete each one when the related code is touched.

### F-02-17: The 15% cap is a limit on buying, not on holding; top-ups buy back positions that have shrunk
- Severity: Note
- Confidence: Confirmed
- Where: `stock_agent/risk_manager.py:54-63`; `stock_agent/config.py:47-50`; `stock_agent/scheduler.py:917-996`
- What happens: Nothing sells part of a position. A holding that grows past 15% stays there; the cap only stops further buying. In the other direction, a holding that falls below 90% of its target is topped up the next time it scores BUY, which in practice means buying more of what has gone down, up to the 7% stop.
- Evidence: No sell path other than `_execute_sell`, which sells everything (`scheduler.py:1316-1323`). `_deep_analyze` does not skip held symbols (`:917-996`), unlike the daily and earnings scans (`:641`, `:743`).
- Why it matters: Context for the plan; both behaviours may or may not be what Josh wants.
- How verified: Searched for any partial-sell or trim logic; read the top-up gate.
- Direction (not a fix): A decision for Josh (see questions).

## Checked and fine

- Conviction-tier sizing sum is right and rounds down to whole shares (`risk_manager.py:139-149`); `tests/test_risk_tripwires.py` passes (33 tests).
- Zero or negative price or portfolio value gives zero shares for a new position (`risk_manager.py:139-140`).
- No conviction tier exceeds the per-symbol cap; pinned by `test_no_conviction_tier_exceeds_the_per_symbol_cap`.
- Lead "fallback when a score is not in the map": it falls to the smallest tier (5%), never a larger one (`risk_manager.py:45-47`, `:143-145`). Harmless on its own; see F-02-07 for how a 6 gets there.
- Lead "typo setting `MAX_POSITION_PCT` to 15": adds stay blocked by the conviction-target rule, so that particular typo does little (F-02-14 covers the ones that do).
- Position-count limit is correct: blocks at 20, so the maximum is 20 (`risk_manager.py:66-67`).
- Top-up sizing cannot pass `min(target, cap)` when prices and portfolio value are consistent (`scheduler.py:1235-1243`).
- Conviction is clamped to 1-10 when parsed (`analyst.py:268-269`) and in each adjuster (`tipranks_client.py:376`, `sentiment.py:144`, `analyst_trends.py:227`); a non-numeric score rejects the thesis.
- The PDT check is called before every equity buy batch (`scheduler.py:1178`); its 7-calendar-day window covers 5-6 business days; grouping by UTC date is safe for regular US hours.
- Stop price arithmetic and clamp order are right; zero or negative beta is treated as missing (`risk_manager.py:157-164`).
- The loop does not stop-sell a position that has no price (`risk_manager.py:212-213`).
- A symbol in several clusters is tested against each (`risk_manager.py:104-117`).
- The `SECTOR_MAP` fallback is applied to both the candidate and to held positions marked Unknown (`risk_manager.py:77-83`).
- Negative cash from the broker is kept, so the exposure check does see leverage once it exists (`portfolio.py:214-219`).
- All equity buys pass through one function, which is a single place to fix (`scheduler.py:1264`).
- Paper mode and the paper endpoint are the defaults (`config.py:9`, `:44`), pinned by tests.

## Could not verify

- Whether the broker refuses the bot's sell while a stop order rests, or accepted second sells as short sales (F-02-01). Needed: the droplet snapshot's Alpaca report (`scripts/alpaca_readonly_report.py`) showing order history with rejections, current open orders, and whether any position has a negative quantity; plus the account's margin and shorting flags.
- Whether top-up buys are refused by the broker while a sell stop for the same symbol is open. Needed: the same order history.
- Whether stop orders sent 3 seconds after the buy are actually accepted, and what the broker does with a stop priced above the market (F-02-06). Needed: order history.
- The live values of `MAX_TOTAL_EXPOSURE_PCT`, `MAX_POSITION_PCT`, `MAX_CLUSTER_PCT` on the droplet (F-02-05). Needed: the snapshot's settings summary, or the "Max Exposure" field of the Discord start-up message.
- Whether the account is margin or cash, and above or below the day-trading threshold (F-02-05, F-02-13). Needed: the account section of the Alpaca report.
- The sector labels actually stored on positions (F-02-10). Needed: the sector field of each position in the droplet's `portfolio.json`.
- How often conviction-6 buys have happened and which regime is active now (F-02-07). Needed: `macro_state.json`, `macro_history.json` and the trade reasons in `portfolio.json`.
- How far real fills have been from signal prices (F-02-06). Needed: bot logs alongside order history.

## Questions for Josh

1. Should the bot ever buy with borrowed money? If not, how much cash should it always keep?
2. Is the maximum invested share meant to be 50% or 100%? What does the Discord start-up message show for "Max Exposure" today?
3. Is conviction 7 a hard floor, or may the bot drop to 6 in a sell-off as it does now?
4. Are the 40% sector and 48% cluster caps meant to hold after a buy (shrink or refuse the order), and should top-ups obey them?
5. Should the bot top up a position because its price fell, or only when conviction rises?
6. Which stop do you want: the order resting at the broker, the bot's 30-minute check, or both with the bot cancelling one before using the other?
7. Do you want stops that widen for volatile stocks (7-12%) as documented, or a flat 7%?
8. When a holding grows past 15% on its own, should the bot trim it or leave it?
9. How far may a price move between research and order before the bot should skip the buy, and how old may a signal be?
10. Should a sell decided while the market is closed be held by the bot and sent at the open?
11. Does the pattern-day-trader rule apply to this account, or can that check go?

## Handoffs

- 03: Confirm F-02-01 against broker order history; `place_buy` reports success before any fill and when the stop order fails (`executor.py:129-134`); no sell path cancels stops; unfilled orders are invisible to risk checks.
- 03: `_sync_portfolio_from_alpaca` treats a failed positions call as "no positions" and wipes the ledger (`scheduler.py:816-821`, `executor.py:75-80`).
- 01: An exception in `_execute_signals` skips `_pending_signals.clear()` and retries every tick (`scheduler.py:194-197`); the queue is memory-only; the first-run block (`:149-154`) and the direct-execute branches (`:433-434`, `:543-544`) cannot run; `_is_market_open` ignores holidays; the daily reconcile depends on the morning-brief window not being missed.
- 01: A regime that flips at a threshold re-triggers the full event scan and buy path every 30 minutes (`scheduler.py:1626-1680`).
- 04: Options orders pass no PDT or equity-limit check; an assignment can push a stock holding past 15%; put collateral is compared with 7% of portfolio value, not with cash (`options_engine.py:428-445`); `config.py:203`, `:209`, `:212` disagree with their comments or with the engine's own constants.
- 05: Non-AI nudges can add up to +5 to conviction (TipRanks +2, sentiment +1, analyst and insider +2), each step raising position size; a BUY from `_deep_analyze` and a SELL from `_re_analyze_existing_positions` can be issued for the same symbol in one run; `update_thesis` writes `active_theses` only, so `Position.thesis` is never refreshed.
- 07: Entry price and cash are recorded from the signal price, not the fill; closed-market sells are booked at the last quote; top-ups do not reduce cash; a null price from the quote feed could be saved into `portfolio.json`; `Position.stop_loss` and `Signal.position_size_pct` are written and never read.
- 08: False "Sell failed" alert on every sell (F-02-11); the PDT alert says "today" for a block that can last a week.
- 09: `.env.example:2-3` holds what look like real Telegram credentials and `config.py:194-195` holds default TipRanks credentials in a public repository (values not repeated here); `deploy/setup.sh:35` seeds the server settings from a file full of old Polymarket values, one of which the stock agent reads; a malformed limit value stops the service at import.
- 10: Sizing exists in two places with different fallbacks (`risk_manager.py:143-145`, `scheduler.py:1235-1237`); `Executor.cancel_order`, `get_order`, `get_open_orders`, `cancel_all_orders` are unused.
