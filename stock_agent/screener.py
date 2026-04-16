import json
import logging
from datetime import datetime, timezone

import httpx

from stock_agent.config import Config
from stock_agent.models import CompanyData

logger = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class Screener:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _call_openai(self, messages: list[dict]) -> dict | None:
        """Call OpenAI GPT-4.1 Nano for structured screening."""
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self.config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-4.1-nano",
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        }

        try:
            resp = await client.post(OPENAI_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data
        except httpx.HTTPStatusError as e:
            logger.error("OpenAI API error: %s — %s", e.response.status_code, e.response.text[:500])
            return None
        except Exception as e:
            logger.error("OpenAI call failed: %s", e)
            return None

    def _format_company_for_screening(self, company: CompanyData) -> str:
        lines = [
            f"Symbol: {company.symbol} | {company.name}",
            f"Sector: {company.sector} | Market Cap: ${company.market_cap:,.0f}",
            f"Price: ${company.price:.2f}",
        ]

        def _add(label: str, val, fmt: str = ".2f"):
            if val is not None:
                lines.append(f"{label}: {val:{fmt}}")

        _add("P/E", company.pe_ratio, ".1f")
        _add("Forward P/E", company.forward_pe, ".1f")
        _add("P/B", company.pb_ratio, ".1f")
        _add("EV/EBITDA", company.ev_ebitda, ".1f")
        _add("Rev Growth YoY", company.revenue_growth_yoy, ".1%")
        _add("Earnings Growth YoY", company.earnings_growth_yoy, ".1%")
        _add("Gross Margin", company.gross_margin, ".1%")
        _add("Operating Margin", company.operating_margin, ".1%")
        _add("Net Margin", company.net_margin, ".1%")
        _add("ROE", company.roe, ".1%")
        _add("ROIC", company.roic, ".1%")
        _add("FCF Yield", company.fcf_yield, ".1%")
        _add("Debt/Equity", company.debt_to_equity, ".2f")
        _add("Current Ratio", company.current_ratio, ".2f")
        _add("Analyst Target", company.analyst_target_price, ".2f")
        _add("DCF Value", company.dcf_value, ".2f")

        return "\n".join(lines)

    async def screen_candidates(self, companies: list[CompanyData]) -> list[dict]:
        """Screen and score a list of companies using GPT-4.1 Nano.

        Returns ranked list of dicts with scores and summaries.
        """
        all_results: list[dict] = []
        batch_size = 5

        for i in range(0, len(companies), batch_size):
            batch = companies[i : i + batch_size]
            batch_results = await self._screen_batch(batch)
            all_results.extend(batch_results)

        # Sort by composite score descending
        all_results.sort(key=lambda x: x.get("composite", 0), reverse=True)
        logger.info(
            "Screened %d companies, top score: %s (%.1f)",
            len(all_results),
            all_results[0].get("symbol", "?") if all_results else "N/A",
            all_results[0].get("composite", 0) if all_results else 0,
        )
        return all_results

    async def _screen_batch(self, companies: list[CompanyData]) -> list[dict]:
        """Score a batch of companies."""
        companies_text = ""
        for c in companies:
            companies_text += f"\n---\n{self._format_company_for_screening(c)}\n"

        prompt = f"""You are a quantitative stock screener. For EACH of the following companies, score them 1-10 on each factor:
- Revenue Growth (YoY quarterly)
- Margin Quality (gross margin trend, operating margin trend)
- Valuation (P/E vs sector, P/FCF, EV/EBITDA)
- Balance Sheet (debt/equity, current ratio, FCF yield)
- Analyst Sentiment (consensus rating, price target upside)

COMPANIES:
{companies_text}

Return JSON with a "results" array. Each element must have:
{{"symbol": "...", "scores": {{"revenue_growth": X, "margin_quality": X, "valuation": X, "balance_sheet": X, "analyst_sentiment": X}}, "composite": X, "summary": "one sentence"}}

The composite score should be the weighted average: revenue_growth (25%), margin_quality (20%), valuation (25%), balance_sheet (15%), analyst_sentiment (15%).

Return ONLY the JSON object with the "results" array."""

        messages = [
            {"role": "system", "content": "You are a quantitative stock screener. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ]

        response = await self._call_openai(messages)
        if not response:
            return [{"symbol": c.symbol, "composite": 0, "summary": "Screening failed"} for c in companies]

        try:
            content = response["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            results = parsed.get("results", [])
            if isinstance(results, list):
                return results
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error("Failed to parse screening response: %s", e)

        return [{"symbol": c.symbol, "composite": 0, "summary": "Parse failed"} for c in companies]
