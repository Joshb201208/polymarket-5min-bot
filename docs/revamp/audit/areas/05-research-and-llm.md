# 05. Research and LLM pipeline

Output: `docs/revamp/audit/findings/05-research-and-llm.md`

## Scope

`stock_agent/screener.py` (OpenAI), `stock_agent/analyst.py` (Perplexity
Sonar and Sonar Pro), `research_notes.py`, `sentiment.py`,
`analyst_trends.py`, `news_feed.py` as inputs to prompts, and the scheduler
functions that drive them (`_run_weekly_analysis`, `_run_midweek_analysis`,
`_run_daily_prescan`, `_run_earnings_reactive`, `_deep_analyze`,
`_re_analyze_existing_positions`, `_check_for_market_event`).

## Why it matters

Every trade starts as text returned by a language model. The conviction score
in that text sets the position size. This area decides whether the bot's
"research" is something to trust with money, and how much it costs to run.

## Questions to answer

1. Map the funnel with real numbers: how many symbols enter, how many survive
   each stage, which model sees what, and what a full weekly run costs in API
   calls. Which models and parameters are actually called? Note where the
   README or comments say something different.
2. Quote each prompt (system and user). What is the model told to do, what
   data is it given, and what is it left to look up or guess?
3. How is the model's reply turned into a `Thesis`? What happens with
   malformed JSON, missing fields, a conviction of 11 or "high", a price
   target of zero, or a refusal? Is a parse failure ever treated as a signal?
4. Is anything the model says checked against data the bot already has (for
   example that the price it quotes is near the real price, or that the
   symbol it answers about is the one asked)?
5. How stable is the output? Would the same inputs give the same conviction
   tomorrow? Is there any record of past theses to compare against, or any
   measurement of whether high-conviction calls did better?
6. How does a HOLD or SELL thesis on an existing position turn into an order?
   Can one bad model reply sell a position?
7. What text from outside sources (news headlines, research notes, analyst
   text) is pasted into prompts, and could that text steer the model?
8. How do research notes, TipRanks scores, sentiment, and analyst trends
   change the outcome? Is TipRanks ever a sole buy trigger? (The decision on
   record says it must not be.)
9. What are the timeouts, retries, and rate-limit behaviour? What does a
   Perplexity or OpenAI outage do to a weekly run?

## Leads to check (unverified)

- The README mentions a model provider the code does not call. Establish what
  is really used.
- `_extract_json` and `_parse_thesis`: test them by hand with awkward inputs.
- Check whether `check_for_material_events` can trigger sells on its own.
- Check whether the macro regime changes minimum conviction and universe size
  mid-run.

## Out of scope

Raw data fetching (06). Sizing from conviction (02).
