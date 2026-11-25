"""Deterministic ranking of observed financial factors; no LLM dependency."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    sector: str
    tradable: bool
    metrics: dict[str, float] = field(default_factory=dict)
    focus: bool = False


_FACTORS = {
    "revenue_growth_yoy": (30, 1),
    "gross_margin": (20, 1),
    "earnings_yield": (20, 1),
    "debt_to_equity": (15, -1),
    "relative_strength": (15, 1),
}


class Screener:
    def __init__(self, config):
        self.config = config

    async def close(self) -> None:
        return None

    @staticmethod
    def rank(companies: list[Candidate], *, include_relative_strength: bool = True) -> list[dict]:
        eligible = {row.symbol: row for row in companies if row.tradable}
        factors = {name: spec for name, spec in _FACTORS.items()
                   if include_relative_strength or name != "relative_strength"}
        total_weight = sum(weight for weight, _ in factors.values())
        observations = {
            name: {ticker: row.metrics[name] * position for ticker, row in eligible.items()
                   if name in row.metrics and math.isfinite(row.metrics[name])}
            for name, (_, position) in factors.items()
        }
        ranked = []
        for symbol, row in eligible.items():
            scores = {}
            for name, values in observations.items():
                if symbol not in values:
                    continue
                same_sector = [value for other, value in values.items() if eligible[other].sector == row.sector]
                peers = same_sector if len(same_sector) >= 3 else list(values.values())
                target = values[symbol]
                scores[name] = ((sum(value < target for value in peers) +
                                sum(value == target for value in peers) / 2) / len(peers)) * 10
            # Unknown factors center at neutral rather than increasing score,
            # then factor coverage is prioritized at a tied composite. The
            # screener only directs research; it is never an entry gate.
            composite = sum(weight * scores.get(name, 5.0) for name, (weight, _) in factors.items()) / total_weight
            ranked.append({"symbol": symbol, "name": row.name, "scores": scores, "composite": composite,
                           "score": composite, "coverage": len(scores), "focus": row.focus})
        return sorted(ranked, key=lambda row: (-row["focus"], -row["composite"], -row["coverage"], row["symbol"]))

    async def screen_candidates(self, companies: list) -> list[dict]:
        """Compatibility for callers still preparing the legacy CompanyData rows."""
        rows = []
        for company in companies:
            metrics = {}
            for field in _FACTORS:
                value = getattr(company, field, None)
                if value is not None:
                    metrics[field] = value
            if company.pe_ratio is not None and company.pe_ratio > 0:
                metrics["earnings_yield"] = 1 / company.pe_ratio
            rows.append(Candidate(
                company.symbol, company.name, company.sector,
                company.price > 0 and (not company.market_cap or company.market_cap >= self.config.MIN_MARKET_CAP),
                metrics,
            ))
        return self.rank(rows)
