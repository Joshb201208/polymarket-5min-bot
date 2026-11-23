"""Deterministic pre-LLM materiality scoring for news stories and SEC filings (Decision A5).

Scores items on an integer `0..10` scale (`MATERIALITY_THRESHOLD = 5`) before any LLM call
so listicle/recap noise never triggers paid model evaluations while earnings, guidance,
M&A, regulatory actions, 8-K filings, and major AI/semiconductor catalysts pass through.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

MATERIALITY_THRESHOLD: int = 5

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_CATALYST_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "trading_halt_or_distress",
        re.compile(
            r"\b(trading\s+halt|halted|circuit\s+breaker|delisting|bankruptcy|"
            r"chapter\s+11|going\s+concern|default\s+notice)\b",
            re.I,
        ),
        6,
    ),
    (
        "earnings_or_guidance",
        re.compile(
            r"\b(earnings|eps|revenue|guidance|outlook|raises?\s+guidance|"
            r"cuts?\s+guidance|lowers?\s+guidance|preliminary\s+results|"
            r"beats?\s+estimates|misses?\s+estimates|quarterly\s+results)\b",
            re.I,
        ),
        5,
    ),
    (
        "mna_or_strategic",
        re.compile(
            r"\b(acquires?|acquisition|merger|buyout|takeover|spin-?off|"
            r"divestiture|strategic\s+review|tender\s+offer|joint\s+venture)\b",
            re.I,
        ),
        5,
    ),
    (
        "regulatory_or_legal",
        re.compile(
            r"\b(fda|approval|complete\s+response\s+letter|crl|clinical\s+trial|"
            r"phase\s+[23]|doj|ftc|sec\s+probe|subpoena|antitrust|"
            r"export\s+controls?|tariffs?|sanctions?|injunction|recall)\b",
            re.I,
        ),
        5,
    ),
    (
        "ai_semis_or_contract",
        re.compile(
            r"\b(blackwell|rubin|gpu|ai\s+chips?|ai\s+accelerators?|hyperscaler|"
            r"datacenter\s+capex|data\s+center\s+capex|hbm\d*|foundry|wafer|"
            r"supply\s+agreement|multi-?year\s+deal|contract\s+win|awarded|"
            r"backlog|sovereign\s+ai)\b",
            re.I,
        ),
        4,
    ),
    (
        "capital_or_exec",
        re.compile(
            r"\b(ceo|cfo|resigns?|steps?\s+down|appointed\s+ceo|buyback|"
            r"share\s+repurchase|dividend\s+(?:hike|increase|cut|suspension|initiation)|"
            r"secondary\s+offering|convertible\s+notes|restructuring|layoffs?)\b",
            re.I,
        ),
        4,
    ),
    (
        "analyst_rating_change",
        re.compile(
            r"\b(upgrades?|downgrades?|initiates?\s+coverage|price\s+target\s+(?:raised|cut|lowered|hiked))\b",
            re.I,
        ),
        4,
    ),
)

_NOISE_RE = re.compile(
    r"\b(why\s+[a-z0-9.\-]+\s+(?:stock|shares)\s+(?:moved|jumped|dropped|fell|popped|slumped)|"
    r"top\s+\d+\s+stocks|stocks\s+to\s+watch\s+today|if\s+you\s+invested\s+\$|"
    r"motley\s+fool|zacks\s+rank\s+additions|etf\s+inflows|here(?:'s|\s+is)\s+why)\b",
    re.I,
)

MATERIAL_8K_ITEMS: frozenset[str] = frozenset(
    {
        "1.01",
        "1.02",
        "1.03",
        "2.01",
        "2.02",
        "2.04",
        "2.05",
        "2.06",
        "3.01",
        "4.01",
        "4.02",
        "5.02",
    }
)


def sanitize_news_text(raw: str | None, *, max_chars: int = 600) -> str:
    """Strip HTML tags, decode entities, collapse whitespace, and cap length."""
    if not raw:
        return ""
    unescaped = html.unescape(str(raw))
    no_tags = _TAG_RE.sub(" ", unescaped)
    clean = _WS_RE.sub(" ", no_tags).strip()
    if len(clean) > max_chars:
        return clean[: max_chars - 1].rstrip() + "…"
    return clean


@dataclass(frozen=True)
class MaterialityVerdict:
    score: int
    is_material: bool
    reasons: tuple[str, ...]


def score_materiality(
    headline: str,
    summary: str = "",
    *,
    symbols: Sequence[str] = (),
    held_or_target: set[str] | None = None,
    focus_set: set[str] | None = None,
    sec_form: str | None = None,
    sec_items: Sequence[str] = (),
    threshold: int = MATERIALITY_THRESHOLD,
) -> MaterialityVerdict:
    """Compute cheap pre-LLM materiality score (`0..10` int) and catalyst tags."""
    text = f"{headline} {summary}".strip()
    if not text and not sec_form:
        return MaterialityVerdict(0, False, ())

    score = 1
    reasons: list[str] = []
    catalyst_hits = 0

    if sec_form:
        form_upper = sec_form.strip().upper()
        if form_upper.startswith("8-K"):
            if (set(sec_items) & MATERIAL_8K_ITEMS) or not sec_items:
                score += 6
                catalyst_hits += 1
                reasons.append(f"sec_{form_upper.lower()}")
            else:
                score += 3
                reasons.append("sec_8k_other")
        elif form_upper in {"10-K", "10-Q", "6-K"}:
            score += 5
            catalyst_hits += 1
            reasons.append(f"sec_{form_upper.lower()}")

    for label, pattern, weight in _CATALYST_PATTERNS:
        if pattern.search(text):
            score += weight
            catalyst_hits += 1
            reasons.append(label)

    sym_set = {s.strip().upper() for s in symbols if s and s.strip()}
    if focus_set and (sym_set & focus_set):
        score += 2
        reasons.append("owner_focus")
    elif held_or_target and (sym_set & held_or_target) and len(sym_set) <= 3:
        score += 1
        reasons.append("single_issuer_focus")

    if len(sym_set) > 6 and "sec_" not in ",".join(reasons):
        score -= 2
        reasons.append("wide_roundup_penalty")

    if _NOISE_RE.search(text) and catalyst_hits == 0:
        score = min(score, 2)
        reasons.append("recap_noise")

    bounded = max(0, min(10, score))
    unique_reasons = tuple(dict.fromkeys(reasons))
    return MaterialityVerdict(
        score=bounded,
        is_material=bounded >= threshold,
        reasons=unique_reasons,
    )
