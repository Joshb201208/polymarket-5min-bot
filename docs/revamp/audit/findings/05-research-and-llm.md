# 05. Research and LLM pipeline

Audited commit: 941f2ca    Date: 2026-11-20

## Plain-language summary

This area is the bot's research desk. Twice a week, and in smaller ways every
weekday, it builds a list of roughly 50 to 150 large US stocks, has a small
OpenAI model score them from a table of numbers, sends the top 20 to
Perplexity's web-searching model for a written thesis with a 1-10 "conviction"
score, and buys whatever comes back as BUY with a high enough score. The same
Perplexity model re-reads every position the bot holds, and a single reply of
"SELL" sells the whole position.

The plumbing is sound in one respect: replies that are garbled, refused, or
cut off are dropped rather than traded on, and no path buys a stock on a
TipRanks score alone. As a basis for risking money, though, the research is
weak in four ways that matter:

1. **One model reply can sell an entire position**, with no second look, and
   the same piece of bad news is put to the model up to seven times a day. The
   first "SELL" wins.
2. **The conviction score that sets position size is not the model's score.**
   Up to five points are added afterwards for TipRanks, news sentiment, and
   insider data that the model had already been shown. A lukewarm 5 can become
   a maximum-size 10.
3. **When Perplexity or OpenAI is down, or answers in a slightly different
   shape, the bot treats that as "nothing to do"** and sends a normal-looking
   weekly report. Nobody is told the research did not happen.
4. **A sell decided before the market opens is logged as "queued" and then
   forgotten.** Nothing is queued.

Beyond those: nothing the model says is checked against numbers the bot
already holds, nothing is stored that would show whether high-conviction calls
did better than low ones, and the README names Anthropic as a model provider
when the code only calls OpenAI and Perplexity.

## How it actually works

### The models and how they are called

| Job | Provider and model | Parameters sent | Timeout | Retries | Code |
| --- | --- | --- | --- | --- | --- |
| Score companies (ranking) | OpenAI `gpt-4.1-nano` | `temperature: 0.3`, `response_format: json_object` | 60 s | none | `stock_agent/screener.py:22,36-41` |
| New-idea thesis | Perplexity `sonar-pro` | model and messages only | 120 s | none | `stock_agent/analyst.py:22,36-39,155` |
| Re-analysis of a held position | Perplexity `sonar-pro` | model and messages only | 120 s | none | `stock_agent/analyst.py:246` |
| "Anything material in the last 24 hours?" | Perplexity `sonar` | model and messages only | 120 s | none | `stock_agent/analyst.py:185` |

For Perplexity nothing else is set: no temperature, no output length limit, no
JSON mode, no restriction on which websites or dates the search may use
(`analyst.py:36-39`). No other model provider is called anywhere in
`stock_agent/` (searched for anthropic, claude, gemini, and other endpoints).

### The weekly funnel (Sunday 18:00-20:59 ET), with numbers

`_run_weekly_analysis`, `stock_agent/scheduler.py:361-474`.

| Stage | What happens | How many | Code |
| --- | --- | --- | --- |
| 0 | Reconcile positions with Alpaca; fetch TipRanks names with Smart Score 8 or more | up to 100 TipRanks rows | `scheduler.py:371-375`, `tipranks_client.py:282-331` |
| 1 | FMP screener: 7 sectors, 10 names each (cap $10B or more), filtered, cut to the first 50 | 50 at most. Because the cut keeps the first 50 of 70 in sector order, only the first five sectors survive; "Consumer Defensive" and "Communication Services" are fetched and thrown away | `data_feed.py:281-330` |
| 1b | Prepend priority symbols (default four names) | up to 54 | `data_feed.py:337-344`, `config.py:63-68` |
| 1c | Add every TipRanks name not already present | up to about 154. These skip the $10B floor and the symbol filter (F-05-14) | `scheduler.py:388-400` |
| 2 | Fetch fundamentals one symbol at a time: 9 FMP calls and 1 Finnhub call each | 54 symbols = 486 FMP calls; 154 = 1,386 | `scheduler.py:840-851`, `data_feed.py:107-184` |
| 3 | OpenAI scores companies in batches of 5; all are sorted by the model's own "composite" number | 11 to 31 OpenAI calls | `screener.py:85-106` |
| 4 | Top N go to deep analysis. N is 20 normally, 15 in the "cautious" regime, 25 or 30 in the two "dip" regimes | 20 `sonar-pro` calls, plus 80 Finnhub calls fired at once for news, sentiment, analyst and insider data | `scheduler.py:417,427,887-898`, `macro_monitor.py:335-354` |
| 5 | Conviction is adjusted by TipRanks, sentiment, and analyst/insider rules; BUY with conviction at or above the regime minimum (7, or 8 cautious, or 6 dip) becomes a signal | no fixed number | `scheduler.py:949-962,984-997` |
| 6 | Every held position is re-analysed; `SELL` sells it immediately | 1 `sonar-pro` call per position | `scheduler.py:430,1003-1028` |
| 7 | Market is closed on Sunday, so buy signals wait in memory until the next open | | `scheduler.py:433-442,192-197` |

Wednesday's "mid-week" run (`scheduler.py:478-561`) is the same cycle with the
same universe, depth, and call counts. Its docstring calls it "lighter"; it is
not.

### Every place a model is asked something, per week

P = positions held, U = universe size (54 to about 154), N = analysis depth (20
in the normal regime).

| Job | When | OpenAI nano | `sonar-pro` | `sonar` |
| --- | --- | --- | --- | --- |
| Weekly | Sun 18:00-20:59 ET | U/5 rounded up (11-31) | N + P | 0 |
| Mid-week | Wed 18:00-20:59 ET | same | N + P | 0 |
| Daily pre-market scan (x5) | 08:00-09:00 ET | 6 | up to 5, plus 1 per severe event | P |
| Earnings scan (x5) | 07:00-08:00 ET | 0 | 1 per holding with an earnings date in the last 2 days, plus 1 per big beat among the first 30 universe names | 0 |
| Market monitoring | every 30 min, 09:30-16:00 ET | 0 | 1 per severe negative event | P on each run that starts in an even hour (10:00, 10:30, 12:00, 12:30, 14:00, 14:30: six or seven runs a day) |
| Event-triggered scan | when the index, VIX, or regime moves | 11 | N + P | 0 |

Worked example, quiet week, U=54, N=20, P=15: about 52 OpenAI calls, up to
about 95 `sonar-pro` calls, and about 525 `sonar` calls (75 pre-market plus
450 during market hours). By count, the largest consumer is the
"anything material?" check, not the weekly run. The code does not read the
token-usage figures the providers return and has no budget cap, so the cost in
dollars cannot be established from the repository (see "Could not verify").

### Buy rules differ by path

| Path | Buy rule | Extra context in the prompt | Conviction adjusted afterwards |
| --- | --- | --- | --- |
| Weekly and mid-week (`_deep_analyze`) | BUY and conviction >= regime minimum (7 / 8 / 6) | TipRanks, news, sentiment, analyst and insider data, research notes | TipRanks, sentiment, analyst/insider |
| Event-triggered scan (`scheduler.py:1677`) | same | as above but no TipRanks (argument not passed) | sentiment, analyst/insider only |
| Daily pre-market scan (`scheduler.py:647-650`) | BUY and conviction >= 8, hard-coded | research notes only | none |
| Earnings beat (`scheduler.py:766-769`) | BUY and conviction >= 8, hard-coded | research notes only | none |

### How a SELL reaches the broker

| Path | Model replies needed | What happens | Code |
| --- | --- | --- | --- |
| Weekly, mid-week, event-triggered re-analysis | 1 | whole position sold at market straight away | `scheduler.py:1013-1026` |
| Earnings scan (also fires on the morning of an earnings day, before the report: F-05-01) | 1 | same | `scheduler.py:717-729` |
| Monitoring: material event | 2 (`sonar` says severity >= 7 and "negative", then `sonar-pro` says SELL) | same | `scheduler.py:1059-1091` |
| Pre-market: material event | 2 | **nothing** (F-05-02) | `scheduler.py:585-624` |

A HOLD on a held position does nothing except overwrite the stored thesis. A
BUY on re-analysis also does nothing. `_execute_sell` sends a market order for
`position.shares` (`scheduler.py:1323`, `executor.py:145-151`).

### The prompts, as sent

**OpenAI ranking** (`screener.py:114-134`). System: `You are a quantitative
stock screener. Return only valid JSON.` User:

```
You are a quantitative stock screener. For EACH of the following companies, score them 1-10 on each factor:
- Revenue Growth (YoY quarterly)
- Margin Quality (gross margin trend, operating margin trend)
- Valuation (P/E vs sector, P/FCF, EV/EBITDA)
- Balance Sheet (debt/equity, current ratio, FCF yield)
- Analyst Sentiment (consensus rating, price target upside)

COMPANIES:
<five blocks: symbol, name, sector, market cap, price, then whichever of 16 ratio lines are not empty>

Return JSON with a "results" array. Each element must have:
{"symbol": "...", "scores": {"revenue_growth": X, "margin_quality": X, "valuation": X, "balance_sheet": X, "analyst_sentiment": X}, "composite": X, "summary": "one sentence"}

The composite score should be the weighted average: revenue_growth (25%), margin_quality (20%), valuation (25%), balance_sheet (15%), analyst_sentiment (15%).

Return ONLY the JSON object with the "results" array.
```

Given: one snapshot of numbers per company. Left to guess: margin *trends*
(one data point is supplied), "P/E vs sector" (no sector figure), P/FCF (not
supplied), and "consensus rating" (the field is always empty,
`data_feed.py:150,219`). The model also does the weighted-average arithmetic
itself.

**New-idea thesis** (`analyst.py:119-153`). System: `You are a senior equity
research analyst. Provide structured JSON analysis.` User:

```
You are a senior equity analyst at a top hedge fund. Analyze {symbol} ({company name}) for a potential investment.

FINANCIAL DATA:
<up to 23 lines from FMP; empty ones are left out without comment>
<optional blocks, pasted as-is: TipRanks data, analyst ratings and insider rows, sentiment, up to 8 news headlines, research notes>

TASK: Form a comprehensive investment thesis. Research the following:
1. Recent earnings performance vs estimates — any surprises?
2. Management's forward guidance and commentary from the latest earnings call
3. Competitive positioning — who are the threats? Is the moat strengthening or weakening?
4. Key catalysts in the next 1-3 months (product launches, FDA approvals, earnings, macro events)
5. Sector/macro tailwinds or headwinds affecting this stock
6. Analyst consensus and any notable recent upgrades/downgrades

OUTPUT FORMAT (JSON):
{
  "direction": "BUY" or "HOLD" or "AVOID",
  "conviction": 1-10,
  "summary": "2-3 sentence thesis",
  "bull_case": "strongest bull argument",
  "bear_case": "strongest bear argument",
  "catalysts": ["catalyst 1", "catalyst 2"],
  "risks": ["risk 1", "risk 2"],
  "target_price": null or number,
  "time_horizon": "4-8 weeks",
  "key_metrics_to_watch": ["metric 1", "metric 2"]
}

Be specific and cite numbers. If you wouldn't put your own money on this, say AVOID.
Respond ONLY with the JSON object, no other text.
```

All six research items are left for the model to find on the web. The prompt
does not say what a conviction of 7 versus 9 means, or that the number sets
position size.

**Re-analysis of a held position** (`analyst.py:206-244`). Same system line.
User:

```
You are a senior equity analyst. Re-evaluate your thesis on {symbol} ({company name}).

CURRENT THESIS (written on {date}):
Direction: {direction} | Conviction: {conviction}/10
Summary: {summary}
Bull case: {bull_case}
Bear case: {bear_case}
Catalysts: {catalysts}
Target price: {target_price}

UPDATED FINANCIAL DATA:
<same FMP lines>

TASK: Has anything changed that would strengthen, weaken, or invalidate this thesis?
- Has the thesis played out? Should we take profits?
- Have risks materialized? Should we cut losses?
- Are catalysts still intact or have they passed?
- Has the competitive landscape shifted?

OUTPUT FORMAT (JSON):
{
  "direction": "BUY" or "HOLD" or "SELL",
  "conviction": 1-10,
  ... same fields ...,
  "thesis_change": "strengthened/unchanged/weakened/broken"
}

Respond ONLY with the JSON object.
```

Not given: the entry price, the profit or loss, how long the position has been
held, any news, TipRanks, sentiment, or research notes.

**Material-event check** (`analyst.py:167-183`). System: `You are a financial
news monitor. Respond only with JSON.` User:

```
Has anything material happened with {symbol} ({company_name}) in the last 24 hours that would affect an investment thesis? Check for:
- Earnings announcements or guidance changes
- Major analyst upgrades/downgrades
- Product launches, FDA decisions, or regulatory changes
- Management changes or insider trading
- Competitive threats or sector shifts
- Macro events affecting the stock

If nothing material, respond: {"material_event": false}
If something material, respond: {"material_event": true, "event": "description", "thesis_impact": "positive/negative/neutral", "severity": 1-10}

Respond ONLY with the JSON object.
```

Both callers pass the ticker as `company_name` (`scheduler.py:586,1060`), so
the model reads, for example, "LOW (LOW)". No data at all is supplied.

### How a reply becomes a `Thesis`

`_parse_thesis` and `_extract_json`, `analyst.py:256-323`. I ran both with
made-up replies. Results:

| Reply | Result |
| --- | --- |
| Clean JSON; JSON in a code fence; JSON with prose around it | parsed correctly |
| Refusal text; truncated JSON; two JSON objects; a JSON array; `{}` | dropped, no thesis |
| `conviction` = `"high"`, `"8/10"`, `"7.5"`, or `null` | dropped |
| `conviction` = 11 | **10** |
| `conviction` = 85 (a 0-100 scale) | **10** |
| `conviction` = 7.9 | 7 (cut, not rounded) |
| `conviction` missing | 5 |
| `direction` missing | HOLD |
| `direction` = `"Strong Buy"` or `"BUY "` (trailing space) | kept as written, so not a buy |
| `direction` = `"Sell"` | SELL |
| `direction` = `"STRONG SELL"` | kept as written, so not a sell |
| Only `{"direction":"BUY","conviction":9}` | **accepted** as BUY 9 with summary "No summary provided" |
| Reply wrapped as `{"analysis": {...SELL...}}` | **accepted** as HOLD, conviction 5, "No summary provided" |
| `target_price` = 0, -50, `NaN`, `"inf"` | accepted as written |
| `summary` null; `catalysts` a string | dropped |
| A `symbol` field naming a different ticker | ignored; the caller's ticker is used |

### Where reality differs from the README and comments

Collected in F-05-16.

## Findings

### F-05-01: One model reply sells a whole position, and the same news is re-asked until one reply says SELL
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:1003-1028`, `717-729`, `1059-1091`, `1316-1346`; `stock_agent/analyst.py:200-254,264-269`
- What happens: When the model re-reads a held position and its reply has `"direction": "SELL"`, the bot immediately sends a market order for every share. There is no second opinion, no minimum conviction, no look at the `thesis_change` field the prompt asks for, no limit on how many positions one pass may sell, and no check that the market is open.
- Evidence:
  - `scheduler.py:1013-1026`:
    ```python
    new_thesis = await self.analyst.re_analyze_position(pos.symbol, company, pos.thesis)
    if not new_thesis:
        continue
    self.portfolio.update_thesis(pos.symbol, new_thesis)
    # If thesis is now SELL, generate exit signal
    if new_thesis.direction == "SELL":
        logger.info("Thesis broken for %s — selling", pos.symbol)
        await self._execute_sell(pos, reason=f"Thesis broken: {new_thesis.summary[:100]}")
    ```
  - This loop runs over every position in the weekly run (`scheduler.py:430`, Sunday evening), the mid-week run (`:540`, Wednesday evening), and every event-triggered scan (`:1667`). `_execute_sell` places `"type": "market", "time_in_force": "day"` for `position.shares` (`scheduler.py:1323`, `executor.py:145-151`).
  - Repeated asks. The material-event check runs before the open and on six or seven monitoring runs a day (`scheduler.py:1103-1108`: `return _now_et().hour % 2 == 0`). Each asks about "the last 24 hours" (`analyst.py:167`), so one piece of bad news is reported on every check that day. Each severe-negative answer triggers a fresh `re_analyze_position` (`scheduler.py:1081-1091`). Nothing remembers that the event was already judged and the answer was "hold".
  - The earnings path sells on one reply (`scheduler.py:720-729`). It starts when `get_recent_earnings` returns anything, and that function also returns a `{"pending": True}` entry when the report has not been published yet (`data_feed.py:407-414`), so the "post-earnings" re-analysis also runs on the morning of earnings day and on each of the next two mornings.
  - The model deciding the exit sees less than the model deciding the entry: no news, TipRanks, sentiment or research notes, and no entry price, profit/loss or holding period, although the prompt asks "Should we take profits?" and "Should we cut losses?" (`analyst.py:206-223`).
  - For positions imported from Alpaca the "current thesis" shown to the model is a placeholder ("Imported from Alpaca — awaiting re-analysis.", `portfolio.py:182-192`). `Position.thesis` is never reassigned anywhere in `stock_agent/` (searched for `.thesis =`), so the placeholder, or the original entry thesis, is what every later re-analysis is anchored on.
- Why it matters: The exit decision for a position worth up to 15% of the account rests on a single sample from a web-searching model whose answer is not repeatable (F-05-08). Asking again and again and acting on the first SELL makes a sale far more likely than any single judgement implies. One odd pass could sell every holding, since nothing counts or limits sells per run.
- How verified: Read each path end to end. Ran `_parse_thesis` with made-up replies: `"SELL"` and `"Sell"` give SELL; `"STRONG SELL"` does not sell. Listed the monitoring slots that pass the `hour % 2` rule: 10:00, 10:30, 12:00, 12:30, 14:00, 14:30, 16:00.
- Direction (not a fix): Decide what evidence a thesis-based sale needs (for example agreement of two independent replies, a conviction or `thesis_change` threshold, or Josh's confirmation), remember which events were already judged, and cap sells per run.
- Verification: Confirmed: all four sell paths are reached from `_tick` and go from `direction == "SELL"` straight to `_execute_sell` with no conviction, `thesis_change`, count or market-hours test in between; the one caveat is that whether Alpaca then accepts the order while the stop is resting is F-03-02's open question, so this finding describes what the bot sends, not what is guaranteed to fill.

### F-05-02: A sell decided before the market opens is logged as "queued" and then dropped
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:565-624` (branch at `606-624`)
- What happens: The 08:00 pre-market scan asks the model about overnight events for each holding. If the event is severe and negative and the follow-up re-analysis says SELL, the code logs "Queuing sell for … at market open" and does nothing else. No order, no stored instruction, no thesis update, no alert.
- Evidence:
  ```python
  if new_thesis and new_thesis.direction == "SELL":
      # If market is open, execute immediately; otherwise queue
      if self._is_market_open(now):
          await self._execute_sell(pos, reason=f"Pre-market event: {event_desc}")
      else:
          logger.info("Queuing sell for %s at market open: %s", pos.symbol, event_desc)
  elif new_thesis:
      self.portfolio.update_thesis(pos.symbol, new_thesis)
  ```
  `now` is taken at the start of the scan (`scheduler.py:570`), and the scan only runs between 08:00 and 09:00 ET (`:274-283`). `_is_market_open` needs 09:30 or later (`:60,297-302`). So the first branch can never run and the `else` only writes a log line. `_pending_signals` holds buy signals only (`:1188`: `if signal.action != "BUY": continue`).
- Why it matters: The scan exists to react to overnight news, and its only possible reaction is lost. The next event check is the 10:00 monitoring run, half an hour after the open, and a sale then needs both model calls to give the same answers again. Until then only the stop-loss protects the position.
- How verified: Traced the schedule window against `_is_market_open`; confirmed nothing else stores a pending sell.
- Direction (not a fix): Either carry the decision to the open or remove the path. Whichever is chosen should follow from the decision on F-05-01.
- Verification: Confirmed: `now` is fixed at scan start inside the 08:00-09:00 window so `_is_market_open(now)` is always false there, the `else` branch only logs, and nothing else stores a sell; the position can still be sold later only if the 10:00 monitoring check happens to get the same two model answers again.

### F-05-03: Position size is set by the model's score plus up to five bonus points for data the model already saw
- Severity: High
- Confidence: Confirmed (arithmetic and code path); Likely (what Finnhub's insider rows contain)
- Where: `stock_agent/scheduler.py:949-962,984-993`; `stock_agent/tipranks_client.py:302-306,334-376`; `stock_agent/sentiment.py:115-144`; `stock_agent/analyst_trends.py:198-227`
- What happens: After the model returns its conviction, three rules add or subtract points: TipRanks (+2 for Smart Score 10, +1 for 9), news sentiment (plus or minus 1), and analyst/insider trends (up to plus or minus 2). The same data was pasted into the prompt, so it is counted twice. The adjusted number is what is compared with the minimum and what picks the size tier.
- Evidence:
  - `scheduler.py:950-962` applies the three adjustments in turn to `thesis.conviction`, then `:984` tests `thesis.conviction >= self.macro.get_effective_min_conviction()`.
  - Ran the three functions in that order with made-up best-case inputs:

    | Model said (BUY) | After TipRanks 10 | After sentiment | After analyst + insider | Trade? | Size |
    | --- | --- | --- | --- | --- | --- |
    | 2 | 4 | 5 | 7 | yes | 5% |
    | 3 | 5 | 6 | 8 | yes | 7% |
    | 5 | 7 | 8 | 10 | yes | 10% (largest tier) |
    | 6 | 8 | 9 | 10 | yes | 10% |

    TipRanks alone moves a model 5 to 7 (Smart Score 10) or a 6 to 7 (Smart Score 9).
  - TipRanks only ever adds. The lookup is built with `min_score=8` and rows below that are filtered out (`tipranks_client.py:302-306`), so the "-1" and "-2" branches for scores of 7 and below (`:371-374`) cannot be reached in the live path. Checked by feeding fake rows with scores 1 to 10 through `build_tipranks_lookup`: only 8, 9, 10 came back.
  - The sentiment point has no sample-size guard. A made-up Finnhub reply with one article in the week, 100% bullish, turns a 7 into an 8 (`sentiment.py:125-144`).
  - The insider rule counts any positive share change as "bought" (`analyst_trends.py:101-109`) and gives +1 when `buys > 0 and sells == 0` (`:222-223`). A made-up row with transaction code "A" (a stock award) and no sales turned a 7 into an 8 and was printed in the prompt as "BUY 12,000 sh @ $0.00". A single scheduled sale with no purchases turned a 7 into a 6. The transaction code is stored but never used to tell purchases from awards or option exercises.
  - The model's own number is overwritten in place before it is saved or posted (`scheduler.py:951-964`), and no log line records it, so there is no way to see afterwards how often the bonus decided a trade.
- Why it matters: Conviction sets size (5/7/8/10% of the account). A stock the model rated below the bar can be bought, and one it rated a marginal 7 can be bought at the largest size, on the strength of add-ons that double-count inputs and misread routine insider filings. Downward moves are smaller (at most -3) and TipRanks never contributes to them.
- How verified: Ran `adjust_conviction_with_tipranks`, `adjust_conviction_with_sentiment`, `adjust_conviction_with_analysts`, `_normalise_insiders`, `build_tipranks_lookup` (with a fake fetch) and `RiskManager.calculate_position_size` on made-up inputs. Not verified against a real Finnhub response.
- Direction (not a fix): Decide whether outside scores inform the model (in the prompt) or adjust its answer (after it), not both; cap the total adjustment; keep the raw number; make insider rules look at the transaction code.
- Verification: Confirmed: re-ran the three functions in scheduler order and got the same table (2 to 7, 5 to 10, at most +5), and the adjusted number is the one tested at `scheduler.py:984` and used for sizing; limits on the claim are that the reply must still say BUY, the result stays inside the 5-10% tiers and under the 15% cap, the full +5 needs all three sources to line up, and only the weekly and mid-week paths can get the TipRanks points (the event scan passes no TipRanks data, the daily and earnings paths apply no adjustments).

### F-05-04: Failed or oddly shaped model replies are read as "nothing to do", and nobody is told
- Severity: High
- Confidence: Confirmed
- Where: `stock_agent/analyst.py:41-51,159-161,188-198,250-252`; `stock_agent/scheduler.py:946-947,1016-1017,465-466,588-606,1062-1081`
- What happens: Any Perplexity failure (outage, expired key, no credit, rate limit, timeout) returns `None`. For a thesis, the symbol is skipped. For the event check, the bot records "no material event". The weekly run then finishes normally, marks itself done for the week, and sends its usual report. No alert says research failed.
- Evidence:
  - `analyst.py:46-51`: both `except` branches log and `return None`. There is no retry or back-off anywhere in `analyst.py` or `screener.py`.
  - `analyst.py:188-198`: empty content, an unparseable reply, or an exception all end in `return {"material_event": False}`.
  - `scheduler.py:946-947` (`if not thesis: continue`) and `:1016-1017` (`if not new_thesis: continue`), then `:465`: `self.portfolio.set_last_weekly_analysis(_now_utc())`. `_should_run_weekly` (`:255-259`) will not run again for five days.
  - The Sunday report always shows an empty signal list anyway, because signals are queued for Monday (`scheduler.py:438,451`), so a dead run and a quiet run look the same.
  - Retry behaviour is the wrong way round. A provider outage raises nothing, so the run is marked done and not retried. A parse crash in the ranking step (F-05-05) leaves `last_weekly_analysis` unset, so the whole run restarts on the next one-minute tick for as long as it is still Sunday 18:00-20:59.
  - The event check also goes quiet on small format changes. Replaying the gate at `scheduler.py:1062-1081` on made-up replies: `"thesis_impact": "Negative"` or `"very negative"` with severity 9 gives no re-analysis (the test is `impact == "negative"`); `"severity": "9"` or `null` raises a `TypeError` that the per-position handler swallows (`:625-626`, `:1094-1095`).
  - There is no start-up check that `PERPLEXITY_API_KEY` or `OPENAI_API_KEY` is set (`config.py:12-13` default to empty strings).
- Why it matters: The research half of the bot can be dead for weeks while the process stays up, positions are held, and reports look normal. During that time no thesis-based exit can happen and no new idea is found. "The call failed" and "the model looked and found nothing" are different facts and the bot cannot tell them apart.
- How verified: Read the failure paths; replayed the event gate with made-up replies. Whether this has happened on the droplet needs logs (see "Could not verify").
- Direction (not a fix): Count failures per run and alert when a run could not analyse most of its symbols; do not mark a run complete when research did not happen; make the event check return "unknown" on failure, and parse its fields tolerantly.
- Verification: Confirmed: every Perplexity failure path returns `None` or `{"material_event": False}`, the callers skip silently, the weekly run still reaches `set_last_weekly_analysis` at `scheduler.py:465`, and the only trace is `logger.error` lines in the service log (`__main__.py` installs no handler that forwards errors to Telegram or Discord).

### F-05-05: The ranking step trusts the small model's output completely; one malformed element aborts the whole cycle
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/screener.py:85-149`; `stock_agent/scheduler.py:416-417,528-529,636-637,1675-1676`
- What happens: The OpenAI reply decides which 20 stocks get researched. The code sorts by whatever is in `composite` and reads `r["symbol"]` with no checks. A failed call scores the batch 0.
- Evidence (all from running `Screener.screen_candidates` with a fake `_call_openai`, 54 made-up companies):
  - One element with `"composite": "7.5"` (a string) or `null`: `TypeError` in the sort at `screener.py:99`. One element that is a string: `AttributeError`. One top-20 element without a `symbol` key: `KeyError` at `scheduler.py:417`. Each of these propagates to the `except` at `scheduler.py:468` and ends the weekly run before deep analysis (`:427`) and before any held position is re-analysed (`:430`). On Wednesday the "done" flag is set first (`:486`), so there is no second attempt.
  - OpenAI down for the whole run: every company gets `composite: 0` (`screener.py:137-138`), the sort keeps the original order, and the "top 20" is simply the first 20 names in the universe list (the priority symbols, then the first sector). Deep analysis and buying proceed on that list.
  - A batch reply listing 1 of 5 companies: the other 4 vanish from the ranking without a log line. A reply with a made-up or lower-case symbol is ranked, then skipped later at `scheduler.py:918-920`, using up a top-20 slot.
  - The composite is not recomputed: sub-scores all 2 with `composite: 9.5` outranks sub-scores all 9 with `composite: 3.0`.
  - The prompt asks for scores on things the data does not contain (trends, sector P/E, P/FCF, consensus rating; see prompt above), and scores from different batches of five are compared as if on one scale.
- Why it matters: This step is the gate between about 50-150 candidates and the 20 that cost real research money and can be bought. Its failures are either silent (arbitrary selection) or total (no weekly research and no position review).
- How verified: Pure-function runs as described. Whether a crash has occurred in production needs the log lines "Weekly analysis failed" and "Failed to parse screening response".
- Direction (not a fix): Validate each returned row against the batch that was sent, compute the composite in code, and treat a failed batch as failed rather than as a score.

### F-05-06: Nothing the model says is checked, and out-of-range or partial replies are accepted
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/analyst.py:256-288,326-332`
- What happens: The reply is turned into a `Thesis` by picking fields out of the JSON with defaults. There is no comparison with data the bot holds and no range check beyond clamping conviction.
- Evidence: See the table under "How a reply becomes a `Thesis`". The points that matter:
  - `conviction = max(1, min(10, conviction))` (`analyst.py:269`) turns 11, or 85 on a 0-100 scale, into 10, the largest size tier, instead of rejecting a reply that did not follow the format.
  - `parsed.get("direction", "HOLD")` and `parsed.get("conviction", 5)` (`:264,268`) mean a reply of the wrong shape (for example the real answer nested under another key) is stored as a genuine "HOLD, conviction 5, No summary provided". A SELL inside such a wrapper is lost. This is the one place a parse failure is recorded as if it were a view.
  - A reply with only `direction` and `conviction` is a valid BUY. Empty reasoning and zero sources are not grounds for rejection.
  - `target_price` is never compared with the price the bot holds (`CompanyData.price`), nor required to be above it for a BUY. Zero, negative, `NaN` and infinity are stored (`_safe_float`, `:326-332`, has no such checks). The stored value is later read by the options scan (`scheduler.py:1440`).
  - The symbol in the reply is ignored (the caller's symbol is used, `:272`), which is safe, but nothing checks that the text is about the right company. The event check is the weak spot here because it is sent only the ticker, twice (F-05-13).
- Why it matters: The answer to the brief's question 4 is "no". A model that misreads the format, answers about the wrong company, or quotes a price target far from the real price is taken at its word, and the number it gives sizes the trade.
- How verified: Ran `_parse_thesis`, `_extract_json`, `_safe_float` on about 45 made-up replies.
- Direction (not a fix): Define the reply schema strictly, reject rather than clamp or default, and add a few sanity checks against FMP data (target versus price, non-empty reasoning and sources).

### F-05-07: No history of what the model said, so conviction can never be scored against results
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/portfolio.py:350-353`; `stock_agent/models.py:102-112,141`; `stock_agent/scheduler.py:1295-1303`
- What happens: Each new thesis overwrites the previous one for that symbol. Trades do not record the conviction they were opened with. The raw model reply is never saved or logged.
- Evidence:
  - `portfolio.py:352`: `self.state.active_theses[symbol] = thesis.model_dump() ...` (a dict keyed by symbol; the latest wins).
  - `Trade` has `symbol, action, shares, price, timestamp, reason, pnl, pnl_pct, hold_days` and no conviction field (`models.py:102-112`); `reason` keeps the first 100 characters of the summary (`scheduler.py:1301`).
  - `calculate_stats` (`portfolio.py:275-343`) reports totals only; nothing groups results by conviction.
  - `Position.thesis` is set once at entry and never updated (no assignment in `stock_agent/`), so even the position's own record does not follow the re-analyses.
  - Theses for stocks that were analysed but not bought are logged nowhere; only Discord receives them (`scheduler.py:967-982`).
- Why it matters: The sizing rule assumes a 10 is better than a 7. Nothing in the bot's own records can confirm or refute that, now or after a year of trading. The Discord channels are the only archive.
- How verified: Read the state models and every writer of `active_theses`.
- Direction (not a fix): Append every thesis (raw reply, raw and adjusted conviction, price at the time, model and prompt version) to a log that can later be joined to outcomes.

### F-05-08: The same question would not get the same answer tomorrow, and the bot asks until it hears yes
- Severity: Medium
- Confidence: Confirmed (code); Likely (size of the effect, which needs data)
- Where: `stock_agent/analyst.py:36-39`; `stock_agent/scheduler.py:634-650,887-898,984,1677`
- What happens: Perplexity is called with default sampling settings and a live web search, so replies vary run to run. The same names are analysed again on Sunday, Wednesday, every weekday morning, and on every event scan, and the first reply that clears the bar becomes a trade. The bar and the inputs differ by path.
- Evidence:
  - Payload is `{"model": model, "messages": messages}` only (`analyst.py:36-39`).
  - The daily scan takes `symbols[:30]` (`scheduler.py:634`), which is the same 30 names every day (priority symbols first, then the first sectors in fixed order), ranks them on fundamentals that change quarterly, and sends the top 5 to `sonar-pro` (`:637-650`). So the same handful of stocks gets a fresh draw each weekday, and any single reply of BUY with conviction 8 or more queues a purchase.
  - The same stock can be scored under three different regimes in one week: with TipRanks and Finnhub bonuses on Sunday (`:949-962`), with Finnhub bonuses but no TipRanks on an event scan (`:1677` omits the argument), and with no bonuses on the morning scan (`:647-649`).
  - The Finnhub enrichment sends four requests per symbol for all symbols at once with no throttle (`news_feed.py:73-74`, `sentiment.py:62-64`, `analyst_trends.py:144-146`), about 80 at a time for 20 symbols. On a rate-limited plan, which symbols get data, and so which get bonus points, would vary between runs. Failures in the sentiment and analyst fetches are logged at DEBUG, which the process never emits (`__main__.py:10-13` sets INFO; `Config.LOG_LEVEL` is not used).
  - There is no usage accounting: the `usage` block in provider responses is never read and there is no spending cap.
- Why it matters: A noisy score checked many times against a threshold will cross it by chance. Buying on the first pass and selling on the first SELL (F-05-01) turns model noise into trades. It also means most of the Perplexity spend goes on repeating questions already asked.
- How verified: Read the call sites and parameters. The actual run-to-run spread cannot be measured without calling the API or seeing past output (see "Could not verify").
- Direction (not a fix): Fix sampling settings where the provider allows, cache or reuse a recent thesis instead of redrawing, and require agreement across draws before acting.

### F-05-09: In a market sell-off the bot lowers the buy threshold to 6; the "wider scan" it announces never happens
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/scheduler.py:984`; `stock_agent/macro_monitor.py:335-354,369-385`; `stock_agent/data_feed.py:291,330`; `stock_agent/risk_manager.py:143-145`
- What happens: The macro regime changes the minimum conviction used by the weekly, mid-week and event scans: 6 in the two "dip" regimes, 8 in "cautious", otherwise 7. The regime's "scan depth" (40/70/80 stocks) is shown in Discord but is not used by the code that builds the universe.
- Evidence:
  - `macro_monitor.py:337,342`: `snap.min_conviction_override = 6`. `scheduler.py:984` is the only gate on minimum conviction; `can_open_position` does not check it. A conviction-6 signal is sized at the conviction-7 tier (`risk_manager.py:143-145`; ran it: 50 shares on $100,000 at $100, the same as conviction 7).
  - `CLAUDE.md` records "Minimum conviction is 7" as a decision already made, and `tests/test_risk_tripwires.py:82` pins `Config.MIN_CONVICTION == 7`, but nothing pins the effective value.
  - `get_effective_universe_size` is called only for the morning-brief text (`scheduler.py:1867`). `screen_universe` uses `self.config.UNIVERSE_SIZE` directly (`data_feed.py:291,330`). The Discord macro brief says "Widening scan to 80 stocks" (`macro_monitor.py:470`); the scan stays at 50.
  - The analysis depth override is real (`scheduler.py:417`): 30 or 25 names instead of 20.
  - Mid-run change (a lead in the brief): not possible. The snapshot only changes inside `check_conditions`, called from `scheduler.py:330,1600,1803`, all awaited one after another on a single task; there is no `create_task` in the scheduler.
  - The pre-market and earnings paths ignore the regime and use a fixed 8 (`scheduler.py:650,769`).
- Why it matters: The bot buys more names at a lower bar exactly when markets are falling, which contradicts the limit written in `CLAUDE.md`. Combined with F-05-03, a model score of 3 or 4 can reach 6 with bonuses. And the Discord text describes behaviour that does not exist.
- How verified: Ran `_compute_regime` with made-up values (drawdown 8% and VIX 30 gives DIP_OPPORTUNITY, override 6; drawdown 12% and VIX 35 gives AGGRESSIVE_DEPLOY, override 6, depth 30). Traced every caller of the three getters.
- Direction (not a fix): Josh decides whether 6 is ever allowed (see questions). Whatever the answer, enforce the minimum in one place that every buy path passes through.

### F-05-10: A threshold wobble or one failed price quote can launch a full research-and-trade cycle in market hours
- Severity: Medium
- Confidence: Confirmed (mechanism); how often it fires needs logs
- Where: `stock_agent/scheduler.py:1578-1696`; `stock_agent/macro_monitor.py:186-229,264-354`
- What happens: After each 30-minute monitoring run the bot recomputes the macro regime. If the regime label differs from 30 minutes ago, or the S&P moved 1.5% or the VIX 3 points since the last check, it immediately re-analyses every position (each can be sold on one reply), re-ranks the universe, runs up to 20-30 new theses, and executes buys on the spot. The only brake is a 30-minute cooldown, which equals the monitoring interval.
- Evidence:
  - `scheduler.py:1626-1629` (regime change is a trigger), `:1596` (cooldown 1,800 s), `:1663-1683` (re-analysis, then deep analysis, then `_execute_signals` while the market is open).
  - The regime is a sum of threshold tests, so a value hovering at a threshold flips it. Ran `_compute_regime`: S&P above the hard-coded upper level with VIX 14.9 gives CAUTIOUS; VIX 15.1 gives NORMAL.
  - A failed quote counts as zero. `check_conditions` leaves a field at 0.0 when its FMP call fails (`macro_monitor.py:186-229`). Ran the scoring with the VIX set to 0: CAUTIOUS becomes NORMAL, and DIP_OPPORTUNITY becomes NORMAL. With the S&P at 0: AGGRESSIVE_DEPLOY becomes NORMAL. Each flip is a "regime change" event and also moves the minimum conviction.
  - The cycle runs inline on the only task (`scheduler.py:199-205`), so stop-loss monitoring waits until 20 or more `sonar-pro` calls (120 s timeout each) and several hundred FMP calls finish.
  - This path calls `_deep_analyze(top_symbols, companies)` without TipRanks data (`:1677`), does not reconcile with Alpaca first, and overwrites the saved universe without the TipRanks names (`:1672`).
  - The docstring lists "Oil (crude) moved > 3%" as a trigger (`:1587`); there is no such check in the body.
- Why it matters: On a volatile day this can run again and again, each time giving every position another chance to be sold on one reply and placing buys under a threshold that a data hiccup may have changed. It also multiplies API spend at the moment the price loop most needs to be responsive.
- How verified: Read the function; ran `MacroMonitor._compute_regime` on made-up snapshots. Frequency in practice needs the "MARKET EVENT DETECTED" log lines and `macro_history.json`.
- Direction (not a fix): Treat a failed quote as "unknown", add hysteresis and a daily limit, and decide whether an event should trigger trading or only an alert.

### F-05-11: One run can queue a BUY and execute a SELL for the same stock
- Severity: Medium
- Confidence: Confirmed (code path); how often the two prompts disagree is unknown
- Where: `stock_agent/scheduler.py:427-438,917-999,1003-1028`
- What happens: Deep analysis does not skip stocks already held. A held stock in the top 20 gets a "potential investment" thesis and, minutes later, a separate "re-evaluate" thesis from a different prompt. If the first says BUY and the second says SELL, the bot sells the position and keeps the buy signal queued.
- Evidence: `_deep_analyze` has no held-symbol test between `:917` and `:997` (the pre-market path has one at `:641`). `_re_analyze_existing_positions` runs next (`:430`) and sells on SELL (`:1022-1026`), removing the position from the ledger (`:1345-1346`). The queued signal (`:437`) is executed at the next open, where `can_open_position` finds no existing position and allows a new full-size buy. In the event-triggered scan the order is sell first, then buy, within the same pass while the market is open (`:1667,1677-1680`). Both writes go to `active_theses[symbol]`, so the stored thesis is whichever ran last.
- Why it matters: A sell and a re-buy of the same stock at the same open costs the spread twice, resets the entry price and stop, and counts toward the bot's day-trade tally (`risk_manager.py:191-193` counts any symbol bought and sold on one calendar day), which can then block all new buys.
- How verified: Read both functions and the order they are called in.
- Direction (not a fix): One decision per symbol per run, made from one view.

### F-05-12: Text from outside sources is pasted into prompts unmarked, and research notes fail open
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/analyst.py:107-123,208-214`; `stock_agent/news_feed.py:83-100`; `stock_agent/analyst_trends.py:184-194`; `stock_agent/tipranks_client.py:411-422`; `stock_agent/research_notes.py:62-152`
- What happens: These strings go into the `sonar-pro` prompt exactly as received: up to 8 Finnhub headlines with their source names, insider names, TipRanks labels, the FMP company name, and the full body of every applicable research note. The re-analysis prompt replays the stored thesis text, which the model itself wrote from web pages. On top of that, Perplexity reads whatever web pages its search returns; the bot has no say over which.
- Evidence:
  - `news_feed.py:97`: `lines.append(f"- [{when} / {source}] {headline}")`, with no length limit and no cleaning. The blocks are joined into the prompt at `analyst.py:107-123` with no delimiters or "treat as data" instruction. Only research notes carry a caveat (`research_notes.py:142,147-151`).
  - Research notes, tested with made-up files: a note whose `expires` is not in `YYYY-MM-DD` form never expires (`_parse_date` returns `None`, `:84-90`); a file that begins with a byte-order mark or a blank line loses its header, so it applies to every symbol, never expires, and the header text itself is pasted into the prompt; a misspelt `symbol:` key makes the note apply to every stock; there is no size limit (a 200,000-character note was pasted whole, into every matching `sonar-pro` call); a note containing "IGNORE ALL PREVIOUS INSTRUCTIONS…" is inserted verbatim.
  - The module docstring describes loading third-party strategist notes (`research_notes.py:3-6`). Whatever is in that folder is sent to Perplexity.
  - Because `Position.thesis` is never updated (F-05-07), any text that entered an entry thesis is replayed in every later re-analysis of that position.
- Why it matters: Could outside text steer the model? Yes. The damage is bounded to one symbol's direction and conviction, so to one position of up to 10% bought, or one position sold. The realistic case is not an attacker but promotional press-release headlines and a stale or mis-formatted note quietly tilting every analysis.
- How verified: Read each formatter; ran `load_notes` and `format_research_notes_context` on ten made-up files. The notes actually on the droplet were not available.
- Direction (not a fix): Fence and cap outside text, validate note headers loudly, and decide what may be sent to a third party.

### F-05-13: The prompts ask for judgements the models are not given the facts for
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/analyst.py:66-92,119-148,167-178,206-239`; `stock_agent/screener.py:55-83,114-129`; `stock_agent/data_feed.py:150,201`; `stock_agent/scheduler.py:586,1060`
- What happens: Several instructions cannot be followed from the data supplied, some data is mislabelled, and missing data is dropped silently so the model cannot tell "not available" from "not relevant".
- Evidence:
  - Re-analysis asks about taking profits and cutting losses without the entry price, P&L, or holding period (`analyst.py:206-223`).
  - The ranking prompt asks for margin trends, P/E versus sector, P/FCF and consensus rating; none is supplied (`screener.py:114-119` against `:55-83`). `analyst_rating` is always `None` (`data_feed.py:150,219`).
  - The line labelled "Forward P/E" in both prompts is filled from FMP's PEG ratio field (`data_feed.py:201`: `forward_pe=_safe_float(latest_ratios.get("priceEarningsToGrowthRatio"))`; printed at `analyst.py:74` and `screener.py:67`). A PEG of 1.4 is shown as "Forward P/E: 1.4".
  - Empty fields are omitted without comment (`analyst.py:92`). With a made-up company that has only profile data, the model receives five lines (symbol, name, sector, market cap, price) and is still asked for a full score or thesis. Some lines are also dropped when the value is exactly zero (`analyst.py:71-77` test truthiness).
  - The event check passes the ticker as the company name (`scheduler.py:586`: `pos.symbol, pos.thesis.symbol if pos.thesis else pos.symbol`; `:1060`), so tickers that are ordinary words are searched on the web with no company name.
  - "Conviction: 1-10" has no rubric in any prompt, although 7, 8, 9 and 10 map to different position sizes.
  - Fields the prompts request and the parser discards: `key_metrics_to_watch` and `thesis_change` (`analyst.py:144,236` against `:271-285`).
- Why it matters: Where the data is missing the model fills the gap from memory or search, and the result looks just as confident. Scores built this way decide which stocks are researched and how large positions are.
- How verified: Compared each prompt with the formatter that feeds it; printed both data blocks for made-up companies.
- Direction (not a fix): Give each prompt the facts its questions need, label fields correctly, state when data is missing, and define the conviction scale.

### F-05-14: TipRanks names enter the universe without the $10B size floor or the symbol filter, and can outnumber the screened list
- Severity: Medium
- Confidence: Confirmed (code); Likely (effect, which depends on what TipRanks returns)
- Where: `stock_agent/scheduler.py:388-400,504-517`; `stock_agent/tipranks_client.py:291-309`; `stock_agent/data_feed.py:318-330`
- What happens: The FMP screen applies a $10B minimum and removes symbols that are not plain 1-5 letter tickers. TipRanks names are appended afterwards with only a Smart Score test.
- Evidence: `scheduler.py:390-396` builds `tr_new_symbols` from `tipranks_lookup` with the single condition `TIPRANKS_MIN_SMART_SCORE <= smart_score`, then `candidate_symbols.extend(tr_new_symbols)`. The TipRanks request is `minSmartScore`, `country=US`, `limit=100` (`tipranks_client.py:291-295`) with no size parameter. The FMP path's filters are at `data_feed.py:297,318-324`. With `TIPRANKS_UNIVERSE_LIMIT=100` the additions can be twice the size of the 50-name screened list.
- Why it matters: `MIN_MARKET_CAP` reads as a rule of the strategy ("$10B minimum", `config.py:168`). Smaller, less liquid companies can reach the ranking, the thesis step and a market order, and those same names then receive the TipRanks conviction bonus (F-05-03). It also triples the weekly FMP and OpenAI call counts. On the brief's question: TipRanks is not a sole buy trigger (see "Checked and fine"), but it can supply most of the candidates and tip the decision.
- How verified: Read the merge code and both filter sites. How many names TipRanks really adds needs the log line "TipRanks added %d new discovery symbols to universe".
- Direction (not a fix): Apply the same eligibility rules to every source of candidates.

### F-05-15: The Finnhub key is written to the service log whenever a news request fails
- Severity: Medium
- Confidence: Confirmed
- Where: `stock_agent/news_feed.py:33-46` (also `stock_agent/sentiment.py:31-39`, `stock_agent/analyst_trends.py:34-42`, which log at a level the process does not emit)
- What happens: The key travels in the URL (`"token": api_key`), and the error handler logs the exception text. For an HTTP error status, the `httpx` library puts the full URL, key included, in that text.
- Evidence: `news_feed.py:44-45`: `except Exception as exc: logger.warning("Finnhub company news fetch failed for %s: %s", symbol, exc)`. Built an `httpx.Response(429)` for this URL offline with a fake key and called `raise_for_status()`; the message was `Client error '429 Too Many Requests' for url 'https://finnhub.io/api/v1/company-news?symbol=TEST&from=…&to=…&token=FAKE_KEY_FOR_AUDIT'`. A 429 is the likely error here because of the unthrottled burst described in F-05-08.
- Why it matters: `CLAUDE.md` says no secret values in logs. The playbook lists credential exposure under Critical; this is rated Medium because the key only buys market data, the journal is readable only on the droplet, and `scripts/vps_snapshot.sh:237` redacts `token=` values before a snapshot is packed. The Perplexity and OpenAI keys are not affected (they are sent in headers, and those handlers log only the response body).
- How verified: Offline `httpx` object test as described; read the three handlers.
- Direction (not a fix): Log the status code and endpoint, not the exception text; the same pattern exists for the FMP key (see Handoffs).

### F-05-16: The README and several comments describe a different pipeline
- Severity: Low
- Confidence: Confirmed
- Where: `README.md:15-16,21-25,48,57`; `stock_agent/scheduler.py:479-482,633,1103-1108,1587`; `stock_agent/sentiment.py:7-8`; `stock_agent/analyst_trends.py:8`; `stock_agent/tipranks_client.py:340-346`
- What happens: Documentation and code disagree in these places.
- Evidence:
  - `README.md:25` "Perplexity + Anthropic — thesis research and analyst reasoning". No Anthropic call exists. OpenAI, which ranks the universe, is not mentioned under Inputs.
  - `README.md:57` describes `screener.py` as "Universe construction". The universe is built in `data_feed.screen_universe`; `screener.py` is the OpenAI scoring step.
  - `README.md:15` "portfolio rebalance": there is no rebalancing, only new buys, top-ups, and whole-position sells. `README.md:16` describes Wednesday as a "check-in"; it is a full repeat of Sunday.
  - `README.md:21` "conviction boost for SS 8–10": a score of 8 adds nothing.
  - `README.md:48` names a setting `POSITION_SIZE_PCT_BY_CONVICTION` that does not exist (it is `CONVICTION_SIZE_MAP`).
  - `scheduler.py:633` "top 10 only for daily scan": the code takes 30 and analyses 5.
  - `scheduler.py:1104` "every other monitoring cycle": it is both runs in even hours and neither in odd hours. The `if not self._last_monitor_time` branch at `:1105` can never be true because `:1035` sets it first.
  - `sentiment.py:7-8` "We fold the two scores"; only one is fetched. `analyst_trends.py:8` "only nudged by strong, unambiguous signals"; one insider row is enough.
  - `tipranks_client.py:340-346` documents negative adjustments that the live path cannot reach.
- Why it matters: Anyone planning the revamp from the README would plan for a different system.
- How verified: Compared each statement with the code path.
- Direction (not a fix): Rewrite the README from the audit once the plan is settled.

### F-05-17: The thesis's target price and time horizon never lead to an exit
- Severity: Note
- Confidence: Confirmed
- Where: `stock_agent/analyst.py:280-282`; `stock_agent/models.py:68-70`
- What happens: Every thesis carries a `target_price` and a `time_horizon` (the prompt suggests "4-8 weeks" and the model tends to echo it). No equity code path reads either to take profit at the target or to review a position whose horizon has passed. `target_price` is read only by Discord (`discord_alerts.py:219-220`) and the options scan (`scheduler.py:1440`).
- Evidence: Search for `target_price` and `time_horizon` across `stock_agent/`.
- Why it matters: Context for the revamp. Equity exits today are the stop-loss or a model SELL, nothing else.
- How verified: Grep and read of each use.
- Direction (not a fix): None needed; a design input for the plan.

## Checked and fine

- Only OpenAI and Perplexity are called for model work (`screener.py:12`, `analyst.py:12`); no other provider endpoint exists in `stock_agent/`.
- TipRanks is never a sole buy trigger in the strict sense: every `Signal` is created only after `thesis.direction == "BUY"` from `sonar-pro` (`scheduler.py:650,769,984`). TipRanks-only discovery names pass through the same ranking, thesis and risk check (`scheduler.py:388-427,1210`). Its influence on the threshold is F-05-03.
- `check_for_material_events` cannot sell on its own. A sale needs a second call, `re_analyze_position`, to return SELL (`scheduler.py:1081-1091`).
- The macro regime cannot change in the middle of a run (single task, no `create_task`; see F-05-09).
- Garbled, refused, truncated, array-shaped and empty replies, non-numeric conviction, and null text fields all produce no thesis and no trade (`analyst.py:259-262,286-288`; table above).
- Direction matching fails closed for buys: only exactly `BUY` after upper-casing buys; `AVOID` becomes `HOLD` (`analyst.py:264-266`).
- `_extract_json` copes with code fences and surrounding prose (`analyst.py:291-323`; tested).
- One symbol's failure does not stop the others in deep analysis or re-analysis (`scheduler.py:998-999,1027-1028`).
- A Finnhub enrichment failure does not block analysis (`scheduler.py:889-898`), and each adjustment function clamps to 1-10.
- Perplexity and OpenAI keys go in headers, and their error handlers log the status and the first 500 characters of the response body, not the request (`analyst.py:47`, `screener.py:49`).
- Research notes: a missing folder is fine, a note with a valid past `expires` date is skipped, Windows line endings parse correctly (`research_notes.py:100-130`; tested).
- The pre-market and earnings-beat paths skip stocks already held (`scheduler.py:641,743`).
- Nothing in the research files talks to Alpaca; orders are placed only through `_execute_signals` and `_execute_sell`.

## Could not verify

- **Whether thesis-driven sells actually execute at the broker.** `place_buy` leaves a GTC stop order for the full share count (`executor.py:117-127`), and `_execute_sell` does not cancel it before sending a market sell. If Alpaca holds those shares for the stop, the sell is rejected and F-05-01 is, in practice, a sell that never happens. A comment at `config.py:157-161` says every sell up to that date was a stop-out. Needed: an Alpaca order-history export (rejected orders included).
- **How often replies fail to parse or runs abort.** Needed: journal log counts for "Could not extract JSON", "Failed to parse thesis", "Failed to parse screening response", "Weekly analysis failed", "Perplexity API error", "OpenAI API error".
- **Whether the model names are still served** (`gpt-4.1-nano`, `sonar`, `sonar-pro`) and whether Perplexity still returns a `citations` field (if not, `sources` is always empty). Needed: recent logs or one saved response.
- **Run-to-run stability of conviction.** Needed: an export of the Discord thesis channel, or logs of "BUY signal: … conviction=" across several weeks, to compare the same symbol on consecutive runs. Measuring it directly would mean calling the API, which the audit does not do.
- **Cost in dollars.** Needed: the Perplexity and OpenAI billing pages for one month. The repository has call counts only.
- **Finnhub plan and whether the sentiment layer has ever returned data** (the endpoint may not be on the free plan), and whether the burst is rate-limited. Needed: the log line "Finnhub enrichment built: news=… sentiment=… analyst=…" and any "Finnhub company news fetch failed" lines.
- **What Finnhub's insider rows contain** (awards and option exercises alongside open-market trades). Needed: one saved response.
- **What TipRanks returns**: row count, company sizes, ordering. Needed: log lines "TipRanks fetch: … rows" and "TipRanks added … new discovery symbols".
- **Which research notes are live.** Needed: a listing of `data/stock_agent/research_notes/` with sizes and header lines (not the bodies).
- **Which prompt lines are actually populated.** The FMP field names in `data_feed.py:200-217` look like the older API's names while the base URL is the newer `/stable` one. If they do not match, most valuation lines are blank and the models see little more than name, sector, size and price. Also unknown: whether `growthRevenue` for quarterly periods is year-on-year as the prompt label says. Needed: one saved FMP response per endpoint, or a Discord thesis post showing the numbers the model quoted.
- **FMP screener ordering**, which decides the fixed 50 names and the fixed 30 for the daily scan. Needed: the `universe` list from `portfolio.json` (symbols only) on two different days.
- **How often event-triggered scans fire and which regime is current.** Needed: "MARKET EVENT DETECTED" log lines and `macro_history.json`.

## Questions for Josh

1. Should one model reply be enough to sell a whole position, or should a thesis-based sale need a second confirmation (another model call, a rule, or your approval)?
2. In a sell-off the bot lowers the buy threshold from 7 to 6. `CLAUDE.md` says the minimum is 7. Which do you want?
3. Should TipRanks, sentiment and insider data be allowed to raise the model's conviction after it has answered, given that it already saw them? If yes, what is the most they may add in total?
4. When a sell is decided before the open, should it execute at the open, or wait for a fresh check?
5. Is the Wednesday run meant to be a full repeat of Sunday (same cost, another chance to sell everything), or a lighter check?
6. Do you want the roughly 500 "anything material?" checks per week (for about 15 holdings), or would fewer checks plus an alert to you be enough?
7. Should a large market move make the bot trade immediately, or only alert you?
8. Should stocks found through TipRanks have to meet the $10B minimum like the rest?
9. Do you want every thesis saved (raw answer, score, price at the time) so the bot's conviction can be graded against results?
10. Are the research notes in the bot's folder allowed to be sent to Perplexity under their source's terms, and which notes are in there now?
11. Which Finnhub plan are you on, and what monthly budget do you want for Perplexity and OpenAI?

## Handoffs

- 03: `_execute_sell` reads `self._tipranks_lookup` (`scheduler.py:1349`), which is never assigned anywhere (AST check: one read, zero writes). It raises after the order is placed and the ledger updated, so every successful sell sends a "Sell failed" alert and no trade alert.
- 03: `_execute_sell` does not cancel the GTC stop from `place_buy`; the market sell may be rejected or leave an orphan stop. Sells issued on Sunday or Wednesday evening are booked locally at once, at a stale price, with cash credited (`scheduler.py:1320-1346`, `portfolio.py:84-89`).
- 03: Queued buy signals execute at the next open using Sunday's price for sizing and the stop (`scheduler.py:990,1201`), with no Alpaca reconcile on the pending, pre-market, earnings, or event-scan paths.
- 01: `_pending_signals` lives in memory; any restart between Sunday evening and Monday's open loses the week's signals while the run stays marked done. The mid-week, pre-market and earnings "done" flags are also in memory, so a restart inside the window repeats the whole cycle.
- 01: `_should_run_weekly` restarts the weekly run every minute after an abort or an early return, for up to three hours (`scheduler.py:248-260,385,413,468`).
- 02: `Thesis` has no `beta` field, so `getattr(signal.thesis, "beta", None)` is always `None` and the stop is always the 7% floor (`scheduler.py:1258-1259`). Conviction 6 is sized as conviction 7 (`risk_manager.py:143-145`).
- 04: The options scan gives any universe symbol without a thesis a default conviction of 6, which is inside the cash-secured-put band, and uses the unvalidated model `target_price` (`scheduler.py:1435-1441`, `options_engine.py:196,205`). Stored theses never expire.
- 06: `forward_pe` is filled from the PEG field (`data_feed.py:201`); `analyst_rating` is always `None`; three of nine FMP calls per symbol fetch data that no prompt or rule uses (`analyst-estimates`, `stock-peers`, `revenue-product-segmentation`); the field names may not match the `/stable` API.
- 06: The universe cut keeps only the first five of seven sectors (`data_feed.py:291,330`); the TipRanks client ignores the config credentials and makes a blocking call inside the event loop (`tipranks_client.py:25-31,54-59`).
- 06: `macro_monitor.check_conditions` records a failed quote as 0, which can flip the regime (demonstrated in F-05-10); the S&P and QQQ levels that drive the regime are hard-coded (`macro_monitor.py:48-56`).
- 07: `active_theses` grows without limit and keeps only the latest thesis per symbol; the macro history keeps 90 entries, about a week at 13 checks a day.
- 08: The Discord screener table reads `score`, `composite_score` and `name`, none of which the screener returns, so it shows 0.0 and a blank name on every row (`discord_alerts.py:453-454`). The weekly report always shows zero signals because they are queued (`scheduler.py:438,451`).
- 09: TipRanks credentials have hard-coded defaults (`config.py:194-195`, `tipranks_client.py:18-19`); `.env.example:2-3` contains what look like a real Telegram token and chat ID; the FMP key reaches the logs by the same `httpx` pattern as F-05-15 (`data_feed.py:49-50`, `macro_monitor.py:144-145,167-168`).
- 10: There are no tests for `analyst.py`, `screener.py`, the adjustment functions, or `research_notes.py`; prompts are inline strings with no version.
