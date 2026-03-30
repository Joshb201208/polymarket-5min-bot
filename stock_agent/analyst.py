import json
import logging
from datetime import datetime, timezone

import httpx

from stock_agent.config import Config
from stock_agent.models import CompanyData, Thesis

logger = logging.getLogger(__name__)

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"


class Analyst:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _call_perplexity(self, model: str, messages: list[dict]) -> dict | None:
        """Call Perplexity Sonar API and return the parsed response."""
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self.config.PERPLEXITY_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
        }

        try:
            resp = await client.post(PERPLEXITY_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data
        except httpx.HTTPStatusError as e:
            logger.error("Perplexity API error (%s): %s — %s", model, e.response.status_code, e.response.text[:500])
            return None
        except Exception as e:
            logger.error("Perplexity call failed (%s): %s", model, e)
            return None

    def _extract_content(self, response: dict | None) -> str:
        if not response:
            return ""
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return ""

    def _extract_citations(self, response: dict | None) -> list[str]:
        if not response:
            return []
        return response.get("citations", [])

    def _format_financial_data(self, data: CompanyData) -> str:
        lines = [
            f"Symbol: {data.symbol}",
            f"Company: {data.name}",
            f"Sector: {data.sector} | Industry: {data.industry}",
            f"Market Cap: ${data.market_cap:,.0f}" if data.market_cap else "",
            f"Price: ${data.price:.2f}" if data.price else "",
            f"P/E Ratio: {data.pe_ratio:.1f}" if data.pe_ratio else "",
            f"Forward P/E: {data.forward_pe:.1f}" if data.forward_pe else "",
            f"P/B Ratio: {data.pb_ratio:.1f}" if data.pb_ratio else "",
            f"P/S Ratio: {data.ps_ratio:.1f}" if data.ps_ratio else "",
            f"EV/EBITDA: {data.ev_ebitda:.1f}" if data.ev_ebitda else "",
            f"Revenue Growth YoY: {data.revenue_growth_yoy:.1%}" if data.revenue_growth_yoy is not None else "",
            f"Earnings Growth YoY: {data.earnings_growth_yoy:.1%}" if data.earnings_growth_yoy is not None else "",
            f"Gross Margin: {data.gross_margin:.1%}" if data.gross_margin is not None else "",
            f"Operating Margin: {data.operating_margin:.1%}" if data.operating_margin is not None else "",
            f"Net Margin: {data.net_margin:.1%}" if data.net_margin is not None else "",
            f"ROE: {data.roe:.1%}" if data.roe is not None else "",
            f"ROIC: {data.roic:.1%}" if data.roic is not None else "",
            f"FCF Yield: {data.fcf_yield:.1%}" if data.fcf_yield is not None else "",
            f"Debt/Equity: {data.debt_to_equity:.2f}" if data.debt_to_equity is not None else "",
            f"Current Ratio: {data.current_ratio:.2f}" if data.current_ratio is not None else "",
            f"Analyst Target: ${data.analyst_target_price:.2f}" if data.analyst_target_price else "",
            f"DCF Value: ${data.dcf_value:.2f}" if data.dcf_value else "",
            f"Next Earnings: {data.next_earnings_date}" if data.next_earnings_date else "",
        ]
        return "\n".join(line for line in lines if line)

    async def analyze_stock(
        self, symbol: str, company_data: CompanyData, tipranks_context: str = ""
    ) -> Thesis | None:
        """Deep analysis using Perplexity Sonar Pro."""
        financial_str = self._format_financial_data(company_data)

        supplementary = ""
        if tipranks_context:
            supplementary = f"\n{tipranks_context}\n"

        prompt = f"""You are a senior equity analyst at a top hedge fund. Analyze {symbol} ({company_data.name}) for a potential investment.

FINANCIAL DATA:
{financial_str}
{supplementary}

TASK: Form a comprehensive investment thesis. Research the following:
1. Recent earnings performance vs estimates — any surprises?
2. Management's forward guidance and commentary from the latest earnings call
3. Competitive positioning — who are the threats? Is the moat strengthening or weakening?
4. Key catalysts in the next 1-3 months (product launches, FDA approvals, earnings, macro events)
5. Sector/macro tailwinds or headwinds affecting this stock
6. Analyst consensus and any notable recent upgrades/downgrades

OUTPUT FORMAT (JSON):
{{
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
}}

Be specific and cite numbers. If you wouldn't put your own money on this, say AVOID.
Respond ONLY with the JSON object, no other text."""

        messages = [
            {"role": "system", "content": "You are a senior equity research analyst. Provide structured JSON analysis."},
            {"role": "user", "content": prompt},
        ]

        response = await self._call_perplexity("sonar-pro", messages)
        content = self._extract_content(response)
        citations = self._extract_citations(response)

        if not content:
            logger.warning("Empty response from Perplexity for %s", symbol)
            return None

        return self._parse_thesis(symbol, content, citations)

    async def check_for_material_events(self, symbol: str, company_name: str) -> dict:
        """Quick check for material events using Sonar (cheap model)."""
        prompt = f"""Has anything material happened with {symbol} ({company_name}) in the last 24 hours that would affect an investment thesis? Check for:
- Earnings announcements or guidance changes
- Major analyst upgrades/downgrades
- Product launches, FDA decisions, or regulatory changes
- Management changes or insider trading
- Competitive threats or sector shifts
- Macro events affecting the stock

If nothing material, respond: {{"material_event": false}}
If something material, respond: {{"material_event": true, "event": "description", "thesis_impact": "positive/negative/neutral", "severity": 1-10}}

Respond ONLY with the JSON object."""

        messages = [
            {"role": "system", "content": "You are a financial news monitor. Respond only with JSON."},
            {"role": "user", "content": prompt},
        ]

        response = await self._call_perplexity("sonar", messages)
        content = self._extract_content(response)

        if not content:
            return {"material_event": False}

        try:
            parsed = _extract_json(content)
            if parsed:
                return parsed
        except Exception:
            pass

        return {"material_event": False}

    async def re_analyze_position(
        self, symbol: str, company_data: CompanyData, current_thesis: Thesis
    ) -> Thesis | None:
        """Weekly re-analysis of an existing position."""
        financial_str = self._format_financial_data(company_data)

        prompt = f"""You are a senior equity analyst. Re-evaluate your thesis on {symbol} ({company_data.name}).

CURRENT THESIS (written on {current_thesis.generated_at.strftime('%Y-%m-%d')}):
Direction: {current_thesis.direction} | Conviction: {current_thesis.conviction}/10
Summary: {current_thesis.summary}
Bull case: {current_thesis.bull_case}
Bear case: {current_thesis.bear_case}
Catalysts: {', '.join(current_thesis.catalysts)}
Target price: {current_thesis.target_price}

UPDATED FINANCIAL DATA:
{financial_str}

TASK: Has anything changed that would strengthen, weaken, or invalidate this thesis?
- Has the thesis played out? Should we take profits?
- Have risks materialized? Should we cut losses?
- Are catalysts still intact or have they passed?
- Has the competitive landscape shifted?

OUTPUT FORMAT (JSON):
{{
  "direction": "BUY" or "HOLD" or "SELL",
  "conviction": 1-10,
  "summary": "2-3 sentence updated thesis",
  "bull_case": "updated strongest bull argument",
  "bear_case": "updated strongest bear argument",
  "catalysts": ["catalyst 1", "catalyst 2"],
  "risks": ["risk 1", "risk 2"],
  "target_price": null or number,
  "time_horizon": "4-8 weeks",
  "thesis_change": "strengthened/unchanged/weakened/broken"
}}

Respond ONLY with the JSON object."""

        messages = [
            {"role": "system", "content": "You are a senior equity research analyst. Provide structured JSON analysis."},
            {"role": "user", "content": prompt},
        ]

        response = await self._call_perplexity("sonar-pro", messages)
        content = self._extract_content(response)
        citations = self._extract_citations(response)

        if not content:
            logger.warning("Empty re-analysis response for %s", symbol)
            return None

        return self._parse_thesis(symbol, content, citations)

    def _parse_thesis(self, symbol: str, content: str, citations: list[str]) -> Thesis | None:
        """Parse LLM JSON output into a Thesis model."""
        try:
            parsed = _extract_json(content)
            if not parsed:
                logger.warning("Could not extract JSON from Perplexity response for %s", symbol)
                return None

            direction = parsed.get("direction", "HOLD").upper()
            if direction == "AVOID":
                direction = "HOLD"

            conviction = int(parsed.get("conviction", 5))
            conviction = max(1, min(10, conviction))

            return Thesis(
                symbol=symbol,
                direction=direction,
                conviction=conviction,
                summary=parsed.get("summary", "No summary provided"),
                bull_case=parsed.get("bull_case", ""),
                bear_case=parsed.get("bear_case", ""),
                catalysts=parsed.get("catalysts", []),
                risks=parsed.get("risks", []),
                target_price=_safe_float(parsed.get("target_price")),
                stop_loss_price=None,
                time_horizon=parsed.get("time_horizon", "4-8 weeks"),
                sources=citations or [],
                generated_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.error("Failed to parse thesis for %s: %s", symbol, e)
            return None


def _extract_json(text: str) -> dict | None:
    """Try to extract a JSON object from text that may contain markdown fences."""
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try stripping markdown code fences
    if "```" in text:
        # Find content between first ``` and last ```
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # Try finding { ... } in the text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
