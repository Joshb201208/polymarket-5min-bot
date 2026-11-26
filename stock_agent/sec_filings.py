"""SEC EDGAR Archive primary-text fetcher, HTML/iXBRL cleaner, 10 req/s limiter, and disk cache.

Fetches primary issuer evidence from `https://www.sec.gov/Archives/edgar/data/...`:
- Recent Form 8-K earnings/FD releases (`Item 2.02` / `Item 7.01`, prioritizing `Exhibit 99.1`
  from `{accession}-index.htm` and falling back to the primary 8-K document), split into up to
  three bounded excerpts (`:1` headline/highlights, `:2` results/EPS, `:3` outlook/guidance).
- The latest Form 10-Q (`Item 2` MD&A) or Form 10-K (`Item 7` MD&A / `Item 1` Business)
  prose-verified summary section (strictly rejecting Table of Contents blocks).

Fail-closed invariants:
- Never invents filing dates, URLs, or text; returns `None` when archive retrieval fails or
  produces no verified prose text so callers fall back to the `-index.htm` metadata stub.
- Resolves document links strictly within `/Archives/edgar/data/{cik_int}/{accession_nodash}/`
  on `www.sec.gov` and strips non-HTML binary assets.
- Persists only verified non-empty extractions to disk (`DATA_DIR / "sec_filings_cache"`), so
  transient HTTP errors never poison future runs.
"""

from __future__ import annotations

import asyncio
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from stock_agent.config import Config
from stock_agent.http_safety import DataHttpClient

logger = logging.getLogger(__name__)

SEC_ARCHIVE_HOST = "https://www.sec.gov"
DEFAULT_MAX_SNIPPET_CHARS = 650
MIN_SUBSTANTIVE_CHARS = 45
MAX_RAW_HTML_CHARS = 6_000_000
DEFAULT_MAX_ARCHIVE_BYTES = 6 * 1024 * 1024
DEFAULT_SEC_REQUEST_TIMEOUT_S = 6.0
DEFAULT_SEC_LIVE_DEADLINE_S = 8.0
DEFAULT_SEC_SCHEDULED_DEADLINE_S = 20.0
_CACHE_SCHEMA_VERSION = 2
_CIK_INT_RE = re.compile(r"^[1-9]\d{0,9}$")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")

_ARCHIVE_ACCEPT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
}

_BINARY_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".xls",
    ".xlsx",
    ".xsd",
    ".xml",
    ".css",
    ".js",
)
_TEXT_DOC_EXTS = (".htm", ".html", ".txt")

_SGML_TEXT_RE = re.compile(r"(?is)<text>\s*(.*?)(?:</text>|$)")
_SGML_HEADER_LINE_RE = re.compile(
    r"(?im)^<(?:document|type|sequence|filename|description)>[^\n]*$"
)
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_IX_NOISE_RE = re.compile(
    r"(?is)<(?:ix:header|ix:hidden|ix:resources|ix:references)\b[^>]*>.*?</(?:ix:header|ix:hidden|ix:resources|ix:references)>"
)
_SCRIPT_STYLE_RE = re.compile(
    r"(?is)<(?:script|style|head|noscript|svg)\b[^>]*>.*?</(?:script|style|head|noscript|svg)>"
)
_CELL_TAG_RE = re.compile(r"(?i)</?(?:td|th)\b[^>]*>")
_BLOCK_TAG_RE = re.compile(
    r"(?i)</?(?:p|div|tr|br|li|ul|ol|h[1-6]|section|article|table|blockquote|hr)\b[^>]*>"
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_HSPACE_RE = re.compile(r"[ \t\r\f\v\u00a0]+")
_MULTI_NL_RE = re.compile(r"\n{2,}")

_EX99_TYPE_RE = re.compile(r"(?i)^ex[-.\s]*99(?:\b|[.\-_]\d+)")
_EX99_NAME_RE = re.compile(r"(?i)(?:ex[-_.]?99|exhibit[-_.]?99|99[-_.]1|pressrelease|earnings)")

_BULLET_ONLY_RE = re.compile(r"^[•◦▪‣–—\-*]+$")
_LEADING_BULLET_RE = re.compile(r"^([•◦▪‣])\s*")

_PAGE_NOISE_RE = re.compile(
    r"(?i)^(?:"
    r"\d{1,3}|"
    r"table\s+of\s+contents|"
    r".*\|\s*20\d\d\s+form\s+10-[qk]\s*\|\s*\d+|"
    r"\d{1,3}\s*\|\s*20\d\d\s+10-[qk]|"
    r"©\s*20\d\d\b.*all\s+rights\s+reserved\.?"
    r")$"
)
_BANNER_NOISE_RE = re.compile(
    r"(?i)^(?:"
    r"ex-99(?:[.\-_]\d+)?(?:\s+.*)?|"
    r"exhibit\s+99(?:[.\-_]\d+)?(?:\s+.*)?|"
    r"for\s+immediate\s+release|"
    r"(?:press|news|earnings|media)\s+release|"
    r"united\s+states\s+securities\s+and\s+exchange\s+commission|"
    r"washington,?\s+d\.?c\.?\s*\d*|"
    r"form\s+(?:8-k|10-q|10-k)(?:/a)?"
    r")$"
)
_CONTACT_HEADER_RE = re.compile(
    r"(?i)^(?:contacts?|media\s+contacts?|investor\s+(?:relations\s+)?contacts?|press\s+contacts?|"
    r"investor\s+relations(?:\s+media\s+relations)?|media\s+relations|public\s+relations|"
    r"corporate\s+communications)\s*:?$"
)
_EMAIL_OR_PHONE_RE = re.compile(
    r"(?i)(?:\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})"
)
_HEADLINE_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:reports?|announces?|announced|completes?|completed|delivers?|delivered|posts?|posted|"
    r"declares?|declared|achieves?|launches?|introduces?|quarter|fiscal|financial\s+results|acquisition)\b|"
    r"\b(?:nasdaq|nyse)\s*:)"
)
_DATELINE_OR_RESULTS_LEAD_RE = re.compile(
    r"(?:^[A-Z][A-Za-z .,'\-]{2,35}\s*[—–\-]+\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z.]*\s+\d{1,2}|"
    r"^[A-Z][A-Z .]{2,30},\s*(?:[A-Za-z .]+\s*[—–\-]+|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2})|"
    r"(?i:\btoday\s+(?:reported|announced)\s+(?:financial\s+)?results\b))"
)
_EXEC_QUOTE_LINE_RE = re.compile(
    r'^[“"].*(?:said|stated|commented|added)\b.*[A-Z][a-z]+.*$'
)
_8K_STOP_SECTION_RE = re.compile(
    r"(?i)^(?:"
    r"safe\s+harbor(?:\s+statement)?|"
    r"forward[- ]looking\s+statements?|"
    r"(?:prepared\s+materials\s+and\s+)?conference\s+call(?:\s+and\s+webcast)?(?:\s+information)?|"
    r"webcast\s+and\s+conference\s+call(?:\s+information)?|"
    r"non[- ]gaap\s+(?:financial\s+)?measures|"
    r"about\s+[a-z0-9][^\n]{0,60}|"
    r"advisors|"
    r"(?:press|media|investor(?:\s+relations)?)\s+contacts?\s*:?|"
    r"for\s+further\s+information,?\s+contact\s*:?|"
    r"note\s+to\s+editors\s*:?|"
    r"ends|#{3,}"
    r")$"
)
_SAFE_HARBOR_PARA_RE = re.compile(
    r"(?i)(?:private\s+securities\s+litigation\s+reform\s+act|"
    r"this\s+(?:press|news|earnings)\s+release\s+contains\s+forward[- ]looking\s+statements|"
    r"certain\s+statements\s+in\s+this\s+(?:press|news)\s+release\s+including|"
    r"will\s+(?:conduct|host|provide\s+live\s+streaming\s+of)\s+(?:a|its)\s+.*conference\s+call|"
    r"to\s+supplement\s+.*consolidated\s+financial\s+statements\s+presented\s+in\s+accordance\s+with\s+gaap|"
    r"reconciliation\s+of\s+.*non[- ]gaap\s+financial\s+measures|"
    r"periodically\s+provides\s+information\s+for\s+investors\s+on\s+its\s+corporate\s+website|"
    r"revolutionized\s+personal\s+technology\s+with\s+the\s+introduction\s+of)"
)
_FIN_STATEMENT_START_RE = re.compile(
    r"(?i)^(?:condensed\s+)?consolidated\s+(?:statements?\s+of\s+(?:operations|income|cash\s+flows)|balance\s+sheets)\b"
)
_OUTLOOK_HEADING_RE = re.compile(
    r"(?i)^(?:(?:financial|business|fiscal|corporate|q[1-4]|first|second|third|fourth|full[- ]year|20\d\d)\s+)*"
    r"(?:outlook|guidance)(?:\s+for\s+[^\n]{1,40})?$"
)
_OUTLOOK_END_HEADING_RE = re.compile(
    r"(?i)^(?:(?:company|product|business|operational|recent|segment|quarterly)\s+)?highlights$|"
    r"^cfo\s+commentary$|^quarterly\s+financial\s+results$|^annual\s+financial\s+results$"
)
_OUTLOOK_PROSE_RE = re.compile(
    r"(?i)(?:\b(?:outlook|guidance)\s+for\s+(?:the\s+)?(?:first|second|third|fourth|next|full|fiscal|q[1-4]|20\d\d)\b|"
    r"\b(?:expects?|anticipates?|projects?|forecasts?)\s+(?:q[1-4]|first|second|third|fourth|fiscal|full[- ]year|20\d\d|"
    r"revenue|net\s+sales|diluted\s+earnings|eps|operating\s+margin|gross\s+margin)\b)"
)

_ITEM_ANY_LINE_RE = re.compile(r"(?i)^(?:part\s+[ivx]+\b|item\s+\d+[a-z]?\b)")
_MDA_10Q_LINE_RE = re.compile(
    r"(?i)^(?:part\s+i[.\s\-—:]*)?item\s*2[.\s\-—:]+management['’`]?s\s+discussion\b"
)
_MDA_10K_LINE_RE = re.compile(
    r"(?i)^(?:part\s+ii[.\s\-—:]*)?item\s*7[.\s\-—:]+management['’`]?s\s+discussion\b"
)
_BIZ_10K_LINE_RE = re.compile(r"(?i)^(?:part\s+i[.\s\-—:]*)?item\s*1[.\s\-—:]+business\b")
_NEXT_SECTION_10Q_RE = re.compile(r"(?i)^(?:part\s+ii\b|item\s*[34]\b)")
_NEXT_SECTION_10K_RE = re.compile(r"(?i)^(?:item\s*(?:7a|8|1a|1b|2)\b)")
_MDA_PREAMBLE_SKIP_RE = re.compile(
    r"(?i)(?:"
    r"^forward[- ]looking\s+statements$|"
    r"contains\s+forward[- ]looking\s+statements|"
    r"all\s+references\s+to\s+[“\"'].*mean\b|"
    r"statements\s+that\s+[“\"']we\s+believe|"
    r"should\s+be\s+read\s+in\s+conjunction\s+with|"
    r"actual\s+results\s+may\s+differ\s+materially|"
    r"^for\s+an\s+overview\s+of\s+our\s+business,\s+see\b|"
    r"^overview$|^our\s+company\s+and\s+our\s+businesses$"
    r")"
)
_RESULTS_SUBHEAD_RE = re.compile(
    r"(?i)^(?:"
    r"financial\s+results|"
    r"(?:first|second|third|fourth)\s+quarter\s+of\s+fiscal\s+year\s+20\d\d\s+summary|"
    r"recent\s+developments.*|"
    r"results\s+of\s+operations|"
    r"segment\s+operating\s+performance|"
    r"consolidated\s+results"
    r")$"
)


@dataclass(frozen=True)
class ExtractedSecDocument:
    url: str
    snippet: str
    doc_type: str
    snippets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.snippets and self.snippet:
            object.__setattr__(self, "snippets", (self.snippet,))


class SecRateLimiter:
    """Async spacer enforcing SEC EDGAR fair-access <= 10 requests/second."""

    def __init__(
        self,
        *,
        max_per_second: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval = 1.0 / max(max_per_second, 0.1)
        self._clock = clock
        self._sleep = sleep
        self._next_allowed: float = 0.0
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def acquire(self) -> None:
        async with self._get_lock():
            now = self._clock()
            wait_s = self._next_allowed - now
            if wait_s > 0:
                await self._sleep(wait_s)
                now = self._clock()
            self._next_allowed = now + self.min_interval


class _IndexTableParser(HTMLParser):
    """Extracts `(doc_type, description, href)` rows from an EDGAR `-index.htm` table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str, str]] = []
        self._in_tr = False
        self._in_cell = False
        self._cells: list[str] = []
        self._cell_buf: list[str] = []
        self._row_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "tr":
            self._in_tr = True
            self._cells = []
            self._row_hrefs = []
        elif self._in_tr and t in {"td", "th"}:
            self._in_cell = True
            self._cell_buf = []
        elif self._in_tr and t == "a":
            for k, v in attrs:
                if k.lower() == "href" and v:
                    self._row_hrefs.append(v.strip())

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._in_cell and t in {"td", "th"}:
            self._in_cell = False
            self._cells.append(" ".join("".join(self._cell_buf).split()))
        elif self._in_tr and t == "tr":
            self._in_tr = False
            if self._row_hrefs and len(self._cells) >= 4:
                description = self._cells[1]
                doc_type = self._cells[3]
                self.rows.append((doc_type, description, self._row_hrefs[0]))
            elif self._row_hrefs and self._cells:
                self.rows.append(("", " ".join(self._cells), self._row_hrefs[0]))


def _safe_archive_doc_url(raw_href: str, *, cik_int: str, acc_nodash: str) -> str | None:
    """Resolve an EDGAR link strictly under `/Archives/edgar/data/{cik_int}/{acc_nodash}/`."""
    href = (raw_href or "").strip()
    if not href or "#" in href:
        return None
    parsed = urlsplit(href)
    if parsed.fragment:
        return None
    if parsed.scheme and parsed.scheme.lower() != "https":
        return None
    if parsed.hostname and parsed.hostname.lower() != "www.sec.gov":
        return None

    path = unquote(parsed.path or "")
    if path.lower().startswith("/ix") and parsed.query:
        doc_vals = parse_qs(parsed.query).get("doc") or []
        if not doc_vals:
            return None
        path = unquote(unquote(doc_vals[0].strip()))
    elif parsed.query:
        return None

    if (
        "?" in path
        or "#" in path
        or "\x00" in path
        or "\r" in path
        or "\n" in path
        or unquote(path) != path
    ):
        return None

    expected_prefix = f"/Archives/edgar/data/{cik_int}/{acc_nodash}/"
    if not path.startswith("/"):
        if "/" in path or "\\" in path or ".." in path:
            return None
        full_path = f"{expected_prefix}{path}"
    else:
        full_path = path

    if ".." in full_path or "\\" in full_path or not full_path.startswith(expected_prefix):
        return None
    rel_name = full_path[len(expected_prefix) :]
    if not rel_name or "/" in rel_name:
        return None
    low_name = rel_name.lower()
    if low_name.endswith(("-index.htm", "-index.html", "-index-headers.html")):
        return None
    if low_name.endswith(_BINARY_EXTS):
        return None
    if not low_name.endswith(_TEXT_DOC_EXTS):
        return None
    return f"{SEC_ARCHIVE_HOST}{full_path}"


def strip_sec_html_to_lines(raw_html: str) -> list[str]:
    """Strip SGML `<DOCUMENT>` headers, iXBRL hidden blocks, scripts/styles, and HTML tags."""
    if not raw_html:
        return []
    clipped = raw_html[:MAX_RAW_HTML_CHARS]
    sgml_match = _SGML_TEXT_RE.search(clipped)
    if sgml_match:
        clipped = sgml_match.group(1)
    clipped = _SGML_HEADER_LINE_RE.sub(" ", clipped)

    cleaned = _COMMENT_RE.sub(" ", clipped)
    cleaned = _IX_NOISE_RE.sub(" ", cleaned)
    cleaned = _SCRIPT_STYLE_RE.sub(" ", cleaned)
    cleaned = _CELL_TAG_RE.sub(" ", cleaned)
    cleaned = _BLOCK_TAG_RE.sub("\n", cleaned)
    cleaned = _ANY_TAG_RE.sub("", cleaned)
    cleaned = html.unescape(cleaned)

    raw_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        norm = _HSPACE_RE.sub(" ", raw_line).strip()
        if norm:
            raw_lines.append(norm)

    merged: list[str] = []
    i = 0
    while i < len(raw_lines):
        ln = raw_lines[i]
        if _BULLET_ONLY_RE.match(ln) and i + 1 < len(raw_lines):
            merged.append(f"• {raw_lines[i + 1].lstrip('•◦▪‣ ')}")
            i += 2
            continue
        merged.append(_LEADING_BULLET_RE.sub("• ", ln))
        i += 1
    return merged


def _cap_text(text: str, max_chars: int) -> str:
    compact = _HSPACE_RE.sub(" ", _MULTI_NL_RE.sub(" ", text).replace("\n", " ")).strip()
    if len(compact) <= max_chars:
        return compact
    cut = compact[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:")
    if len(cut) < max_chars // 2:
        cut = compact[: max_chars - 1]
    return f"{cut}…"


def _clean_8k_lines(lines: list[str]) -> list[str]:
    """Strip leading exhibit/contact metadata and trailing legal/IR boilerplate."""
    idx = 0
    in_top_contacts = False
    while idx < min(len(lines), 22):
        ln = lines[idx]
        if _BANNER_NOISE_RE.match(ln) or _PAGE_NOISE_RE.match(ln):
            idx += 1
            continue
        if _CONTACT_HEADER_RE.match(ln) or _EMAIL_OR_PHONE_RE.search(ln):
            in_top_contacts = True
            idx += 1
            continue
        if in_top_contacts:
            if (
                len(ln) <= 45
                and not _HEADLINE_SIGNAL_RE.search(ln)
                and not any(ch.isdigit() for ch in ln)
            ):
                idx += 1
                continue
            in_top_contacts = False
        break

    trimmed = lines[idx:]
    out: list[str] = []
    for ln in trimmed:
        if _PAGE_NOISE_RE.match(ln) or _BANNER_NOISE_RE.match(ln):
            continue
        if _FIN_STATEMENT_START_RE.match(ln) and sum(len(x) for x in out) >= 750:
            break
        if _8K_STOP_SECTION_RE.match(ln):
            break
        if _SAFE_HARBOR_PARA_RE.search(ln):
            continue
        if _EMAIL_OR_PHONE_RE.search(ln) and len(ln) < 120:
            continue
        out.append(ln)
    return out


def extract_8k_snippets(
    raw_html: str,
    *,
    max_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
) -> tuple[str, ...]:
    """Extract up to 3 clean excerpts (`:1` highlights, `:2` results/EPS, `:3` outlook/guidance)."""
    lines = _clean_8k_lines(strip_sec_html_to_lines(raw_html))
    if not lines:
        return ()

    for i, ln in enumerate(lines[:20]):
        if re.match(r"(?i)^item\s+(?:2\.02|7\.01|8\.01)\b", ln):
            lines = lines[i:]
            break

    outlook_span: tuple[int, int] | None = None
    outlook_text = ""
    for i, ln in enumerate(lines):
        if len(ln) <= 70 and _OUTLOOK_HEADING_RE.match(ln):
            end = min(len(lines), i + 18)
            for j in range(i + 1, end):
                if _OUTLOOK_END_HEADING_RE.match(lines[j]) or _8K_STOP_SECTION_RE.match(lines[j]):
                    end = j
                    break
            outlook_lines = [
                x
                for x in lines[i:end]
                if not re.search(
                    r"(?i)^(?:guidance\s+for\s+non-gaap|further\s+information\s+regarding\b)",
                    x,
                )
            ]
            cand = _cap_text(" ".join(outlook_lines), max_chars)
            if len(cand) >= MIN_SUBSTANTIVE_CHARS:
                outlook_span = (i, end)
                outlook_text = cand
                break

    if outlook_span is None:
        for i, ln in enumerate(lines):
            if i >= 3 and _OUTLOOK_PROSE_RE.search(ln) and not _SAFE_HARBOR_PARA_RE.search(ln):
                end = min(len(lines), i + 3)
                cand = _cap_text(" ".join(lines[i:end]), max_chars)
                if len(cand) >= MIN_SUBSTANTIVE_CHARS:
                    outlook_span = (i, end)
                    outlook_text = cand
                    break

    body_lines = [
        ln
        for i, ln in enumerate(lines)
        if outlook_span is None or not (outlook_span[0] <= i < outlook_span[1])
    ]
    if not body_lines:
        return (outlook_text,) if outlook_text else ()

    split_idx: int | None = None
    for i in range(1, min(len(body_lines), 16)):
        if _DATELINE_OR_RESULTS_LEAD_RE.search(body_lines[i]):
            pre_chars = sum(len(x) + 1 for x in body_lines[:i])
            if pre_chars >= 200:
                split_idx = i
                break

    chunks: list[str] = []
    if split_idx is not None:
        c1 = _cap_text(" ".join(body_lines[:split_idx]), max_chars)
        if len(c1) >= MIN_SUBSTANTIVE_CHARS:
            chunks.append(c1)
        rest = body_lines[split_idx : split_idx + 24]
        if (
            len(rest) >= 2
            and "$" not in rest[0]
            and "%" not in rest[0]
            and re.search(r"(?i)\b(?:highlights|results|summary)\b", rest[1])
        ):
            rest = rest[1:]
        factual_rest = [
            ln for ln in rest if not (_EXEC_QUOTE_LINE_RE.match(ln) and "$" not in ln and "%" not in ln)
        ]
        c2_source = factual_rest if sum(len(x) for x in factual_rest) >= 180 else rest
        c2 = _cap_text(" ".join(c2_source), max_chars)
        if len(c2) >= MIN_SUBSTANTIVE_CHARS and c2 != c1:
            chunks.append(c2)
    else:
        c1_lines: list[str] = []
        c1_len = 0
        next_i = 0
        for i, ln in enumerate(body_lines):
            if c1_lines and c1_len + len(ln) + 1 > max_chars:
                next_i = i
                break
            c1_lines.append(ln)
            c1_len += len(ln) + 1
            next_i = i + 1
        c1 = _cap_text(" ".join(c1_lines), max_chars)
        if len(c1) >= MIN_SUBSTANTIVE_CHARS:
            chunks.append(c1)
        if next_i < len(body_lines):
            rest = body_lines[next_i : next_i + 24]
            factual_rest = [
                ln for ln in rest if not (_EXEC_QUOTE_LINE_RE.match(ln) and "$" not in ln and "%" not in ln)
            ]
            c2_source = factual_rest if sum(len(x) for x in factual_rest) >= 180 else rest
            c2 = _cap_text(" ".join(c2_source), max_chars)
            if len(c2) >= MIN_SUBSTANTIVE_CHARS and c2 != c1:
                chunks.append(c2)

    if outlook_text and outlook_text not in chunks:
        chunks.append(outlook_text)

    return tuple(chunks[:3])


def extract_8k_snippet(raw_html: str, *, max_chars: int = DEFAULT_MAX_SNIPPET_CHARS) -> str | None:
    """Extract the primary headline/highlights snippet from an 8-K or Exhibit 99.1 document."""
    snippets = extract_8k_snippets(raw_html, max_chars=max_chars)
    return snippets[0] if snippets else None


def _is_toc_window(after_lines: list[str]) -> bool:
    window = after_lines[:14]
    if sum(1 for ln in window if _ITEM_ANY_LINE_RE.match(ln)) >= 2:
        return True
    for ln in window[:4]:
        if _PAGE_NOISE_RE.match(ln):
            continue
        if _ITEM_ANY_LINE_RE.match(ln):
            return True
        break
    return False


def _is_prose_line(ln: str) -> bool:
    if len(ln) < 55 or _ITEM_ANY_LINE_RE.match(ln) or _PAGE_NOISE_RE.match(ln):
        return False
    return len(ln.split()) >= 8 and any(ch.islower() for ch in ln)


def extract_periodic_summary_snippet(
    raw_html: str,
    *,
    form: str,
    max_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
) -> str | None:
    """Extract prose-verified MD&A (`10-Q` Item 2 / `10-K` Item 7 or Item 1); never emit TOC."""
    lines = strip_sec_html_to_lines(raw_html)
    if not lines:
        return None
    base_form = form.upper().split("/")[0]
    if base_form == "10-Q":
        heading_pats = [(_MDA_10Q_LINE_RE, _NEXT_SECTION_10Q_RE)]
    else:
        heading_pats = [
            (_MDA_10K_LINE_RE, _NEXT_SECTION_10K_RE),
            (_BIZ_10K_LINE_RE, _NEXT_SECTION_10K_RE),
            (_MDA_10Q_LINE_RE, _NEXT_SECTION_10Q_RE),
        ]

    for head_re, stop_re in heading_pats:
        candidate_indices: list[int] = []
        for idx, ln in enumerate(lines):
            if head_re.match(ln):
                candidate_indices.append(idx + 1)
            elif idx + 1 < len(lines) and re.match(
                r"(?i)^(?:part\s+[ivx]+[.\s\-—:]*)?item\s*(?:1|2|7)[.\s\-—:]*$",
                ln,
            ):
                if head_re.match(f"{ln} {lines[idx + 1]}"):
                    candidate_indices.append(idx + 2)

        # Evaluate from last heading backward so real body sections precede front-matter TOCs.
        for start_idx in reversed(candidate_indices):
            after = lines[start_idx : start_idx + 160]
            if not after or _is_toc_window(after):
                continue

            sec_lines: list[str] = []
            for ln in after:
                if stop_re.match(ln) and not _is_prose_line(ln):
                    break
                if _PAGE_NOISE_RE.match(ln) or _BANNER_NOISE_RE.match(ln):
                    continue
                if _MDA_PREAMBLE_SKIP_RE.search(ln):
                    continue
                sec_lines.append(ln)

            prose_lines = [ln for ln in sec_lines[:35] if _is_prose_line(ln)]
            prose_chars = sum(len(ln) for ln in prose_lines)
            prose_sentences = sum(len(re.findall(r"[.!?](?:\s|$)", ln)) for ln in prose_lines)
            if len(prose_lines) < 3 and not (prose_sentences >= 2 and prose_chars >= 200):
                continue

            best_start = 0
            for k, ln in enumerate(sec_lines[:45]):
                if _RESULTS_SUBHEAD_RE.match(ln):
                    best_start = k
                    break

            chosen = sec_lines[best_start : best_start + 25]
            if sum(1 for ln in chosen if _ITEM_ANY_LINE_RE.match(ln)) >= 2:
                continue
            snippet = _cap_text(" ".join(chosen), max_chars)
            if len(snippet) >= MIN_SUBSTANTIVE_CHARS:
                return snippet

    return None


def select_8k_candidate_urls(
    index_html: str,
    *,
    cik_int: str,
    acc_nodash: str,
    primary_document: str = "",
) -> list[tuple[str, str]]:
    """Return prioritized `(doc_type, safe_url)` candidates for an 8-K (`EX-99.1` first)."""
    parser = _IndexTableParser()
    try:
        parser.feed(index_html[:400_000])
    except Exception:
        pass

    ex99: list[tuple[str, str]] = []
    main_8k: list[tuple[str, str]] = []
    other_htm: list[tuple[str, str]] = []

    for doc_type, desc, href in parser.rows:
        safe_url = _safe_archive_doc_url(href, cik_int=cik_int, acc_nodash=acc_nodash)
        if not safe_url:
            continue
        dtype = doc_type.strip().upper()
        fname = safe_url.rsplit("/", 1)[-1]
        if _EX99_TYPE_RE.match(dtype) or _EX99_NAME_RE.search(fname) or "99.1" in desc:
            ex99.append((dtype or "EX-99.1", safe_url))
        elif dtype in {"8-K", "8-K/A"}:
            main_8k.append((dtype, safe_url))
        else:
            other_htm.append((dtype or "DOC", safe_url))

    ex99.sort(
        key=lambda pair: (
            0 if "99.1" in pair[0] or "99.1" in pair[1] or "99-1" in pair[1] else 1
        )
    )

    primary_url = _safe_archive_doc_url(primary_document, cik_int=cik_int, acc_nodash=acc_nodash)
    if primary_url:
        main_8k.append(("8-K", primary_url))

    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for dtype, url in [*ex99, *main_8k, *other_htm[:1]]:
        if url not in seen:
            seen.add(url)
            ordered.append((dtype, url))
    return ordered


@dataclass
class SecArchiveCallState:
    """Per-call cold-cache deadline and circuit-breaker state across symbols."""

    deadline_mono: float | None = None
    max_consecutive_failures: int = 2
    consecutive_failures: int = 0
    breaker_tripped: bool = False
    deadline_expired: bool = False
    last_status_code: int | None = None
    last_error_summary: str = ""

    def record_transient_failure(
        self, summary: str, *, status_code: int | None = None
    ) -> bool:
        self.consecutive_failures += 1
        self.last_status_code = status_code
        self.last_error_summary = summary
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.breaker_tripped = True
        return self.breaker_tripped


class SecArchiveFetcher:
    """Fetches, cleans, rate-limits, and disk-caches SEC EDGAR primary filing snippets."""

    def __init__(
        self,
        http: DataHttpClient,
        *,
        config: Any = Config,
        rate_limiter: SecRateLimiter | None = None,
        max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
    ) -> None:
        self.http = http
        self.config = config
        self.rate_limiter = rate_limiter or SecRateLimiter(max_per_second=10.0)
        self.max_snippet_chars = max_snippet_chars
        self.request_timeout_s = max(
            0.1, float(getattr(config, "SEC_REQUEST_TIMEOUT_S", DEFAULT_SEC_REQUEST_TIMEOUT_S))
        )
        self.max_download_bytes = max(
            1024, int(getattr(config, "SEC_MAX_ARCHIVE_BYTES", DEFAULT_MAX_ARCHIVE_BYTES))
        )
        self._mem_cache: dict[str, ExtractedSecDocument] = {}
        self._neg_cache: dict[str, float] = {}

    def _cache_dir(self) -> Path | None:
        raw_dir = getattr(self.config, "DATA_DIR", None)
        if not raw_dir:
            return None
        return Path(raw_dir) / "sec_filings_cache"

    def _cache_key(self, cik_int: str, acc_nodash: str) -> str:
        safe_cik = re.sub(r"[^0-9]", "", cik_int)
        safe_acc = re.sub(r"[^0-9A-Za-z]", "", acc_nodash)
        return f"{safe_cik}_{safe_acc}"

    def _read_disk_cache(self, key: str) -> ExtractedSecDocument | None:
        if key in self._mem_cache:
            return self._mem_cache[key]
        cdir = self._cache_dir()
        if cdir is None:
            return None
        path = cdir / f"{key}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("cache_version") != _CACHE_SCHEMA_VERSION:
                return None
            if payload.get("oversize") is True:
                self._neg_cache[key] = float("inf")
                return None
            url = str(payload.get("url") or "").strip()
            doc_type = str(payload.get("doc_type") or "").strip()
            raw_snippets = payload.get("snippets")
            if isinstance(raw_snippets, list) and raw_snippets:
                snippets = tuple(
                    _cap_text(str(s).strip(), self.max_snippet_chars)
                    for s in raw_snippets[:3]
                    if len(str(s).strip()) >= MIN_SUBSTANTIVE_CHARS
                )
            else:
                single = str(payload.get("snippet") or "").strip()
                snippets = (
                    (_cap_text(single, self.max_snippet_chars),)
                    if len(single) >= MIN_SUBSTANTIVE_CHARS
                    else ()
                )
            if (
                url.startswith(f"{SEC_ARCHIVE_HOST}/Archives/edgar/data/")
                and not url.lower().endswith(("-index.htm", "-index.html"))
                and snippets
            ):
                doc = ExtractedSecDocument(
                    url=url,
                    snippet=snippets[0],
                    doc_type=doc_type or "SEC",
                    snippets=snippets,
                )
                self._mem_cache[key] = doc
                return doc
        except Exception as exc:
            logger.debug("Ignoring unreadable SEC disk cache entry %s: %s", path.name, exc)
        return None

    def _write_disk_cache(self, key: str, doc: ExtractedSecDocument) -> None:
        self._mem_cache[key] = doc
        cdir = self._cache_dir()
        if cdir is None:
            return
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            target = cdir / f"{key}.json"
            tmp = cdir / f".{key}.{os.getpid()}.tmp"
            tmp.write_text(
                json.dumps(
                    {
                        "cache_version": _CACHE_SCHEMA_VERSION,
                        "url": doc.url,
                        "snippet": doc.snippet,
                        "snippets": list(doc.snippets),
                        "doc_type": doc.doc_type,
                    },
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, target)
        except Exception as exc:
            logger.debug("Could not persist SEC disk cache %s: %s", key, exc)

    def _write_oversize_marker(self, key: str, url: str) -> None:
        self._neg_cache[key] = float("inf")
        cdir = self._cache_dir()
        if cdir is None:
            return
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            target = cdir / f"{key}.json"
            tmp = cdir / f".{key}.{os.getpid()}.tmp"
            tmp.write_text(
                json.dumps(
                    {
                        "cache_version": _CACHE_SCHEMA_VERSION,
                        "oversize": True,
                        "url": url,
                    },
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, target)
        except Exception as exc:
            logger.debug("Could not persist SEC oversize marker %s: %s", key, exc)

    async def _sec_get_text(
        self,
        url: str,
        *,
        deadline_mono: float | None = None,
        call_state: SecArchiveCallState | None = None,
    ) -> tuple[str | None, bool, bool]:
        """Return `(decoded_text, abort_call, oversize)` enforcing byte, time & breaker limits."""
        eff_deadline = (
            call_state.deadline_mono
            if (call_state is not None and call_state.deadline_mono is not None)
            else deadline_mono
        )
        if call_state is not None and call_state.breaker_tripped:
            return None, True, False
        rem = None if eff_deadline is None else eff_deadline - time.monotonic()
        if rem is not None and rem <= 0.02:
            if call_state is not None:
                call_state.deadline_expired = True
            return None, True, False
        req_timeout = self.request_timeout_s if rem is None else min(self.request_timeout_s, rem)
        byte_cap = self.max_download_bytes

        async def _stream_body() -> tuple[str | None, bool, int | None]:
            await self.rate_limiter.acquire()
            raw_fn = getattr(self.http, "_raw", None)
            hdr_fn = getattr(self.http, "default_headers_for_url", None)
            if callable(raw_fn) and callable(hdr_fn):
                merged_headers = dict(hdr_fn(url))
                merged_headers.update(_ARCHIVE_ACCEPT_HEADERS)
                async with raw_fn().stream(
                    "GET", url, headers=merged_headers, timeout=req_timeout
                ) as resp:
                    if resp.status_code >= 400:
                        return None, False, resp.status_code
                    cl_hdr = resp.headers.get("content-length")
                    if cl_hdr:
                        try:
                            if int(cl_hdr) > byte_cap:
                                return None, True, 200
                        except ValueError:
                            pass
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if len(buf) + len(chunk) > byte_cap:
                            return None, True, 200
                        buf.extend(chunk)
                    encoding = resp.encoding or "utf-8"
                    return buf.decode(encoding, errors="replace"), False, 200
            resp = await self.http.get(url, headers=_ARCHIVE_ACCEPT_HEADERS, timeout=req_timeout)
            if resp.status_code >= 400:
                return None, False, resp.status_code
            content = resp.content or b""
            if len(content) > byte_cap:
                return None, True, 200
            return resp.text or "", False, 200

        try:
            text, oversize, status_code = await asyncio.wait_for(_stream_body(), timeout=req_timeout)
        except TimeoutError:
            logger.debug("SEC archive GET timed out for %s (budget=%.2fs)", url, req_timeout)
            exhausted = eff_deadline is not None and (eff_deadline - time.monotonic()) <= 0.05
            if call_state is not None:
                if exhausted:
                    call_state.deadline_expired = True
                tripped = call_state.record_transient_failure("SEC archive request timed out")
                return None, bool(exhausted or tripped), False
            return None, bool(exhausted), False
        except Exception as exc:
            logger.debug("SEC archive GET failed for %s: %s", url, exc)
            if call_state is not None:
                tripped = call_state.record_transient_failure(
                    f"SEC archive transport error: {type(exc).__name__}"
                )
                return None, tripped, False
            return None, False, False

        if status_code is not None and (status_code >= 500 or status_code == 429):
            if call_state is not None:
                tripped = call_state.record_transient_failure(
                    f"SEC archive HTTP {status_code}", status_code=status_code
                )
                return None, tripped, False
            return None, False, False
        if status_code is not None and status_code >= 400:
            return None, False, False

        if call_state is not None:
            call_state.consecutive_failures = 0
        if oversize:
            logger.info("Skipping oversize SEC archive document (>%d bytes): %s", byte_cap, url)
            return None, False, True
        if not text or len(text.strip()) < MIN_SUBSTANTIVE_CHARS:
            return None, False, False
        return text, False, False

    async def fetch_filing_document(
        self,
        *,
        cik_int: str,
        acc_nodash: str,
        accession: str,
        form: str,
        items: list[str] | tuple[str, ...] = (),
        primary_document: str = "",
        deadline_mono: float | None = None,
        call_state: SecArchiveCallState | None = None,
    ) -> ExtractedSecDocument | None:
        """Fetch and clean primary text for an 8-K (Item 2.02/7.01 + Ex 99.1) or 10-Q/10-K."""
        if (
            not isinstance(cik_int, str)
            or not _CIK_INT_RE.fullmatch(cik_int)
            or not isinstance(accession, str)
            or not _ACCESSION_RE.fullmatch(accession)
            or not isinstance(acc_nodash, str)
            or acc_nodash != accession.replace("-", "")
        ):
            logger.warning(
                "Rejected malformed SEC identifier cik_int=%r accession=%r", cik_int, accession
            )
            return None

        norm_form = form.upper().strip()
        if norm_form in {"8-K", "8-K/A"}:
            if items and not ({"2.02", "7.01"} & {str(it).strip() for it in items}):
                return None
        elif norm_form not in {"10-Q", "10-Q/A", "10-K", "10-K/A"}:
            return None

        key = self._cache_key(cik_int, acc_nodash)
        neg_until = self._neg_cache.get(key, 0.0)
        if neg_until > time.monotonic():
            return None

        cached = self._read_disk_cache(key)
        if cached is not None:
            return cached
        if self._neg_cache.get(key, 0.0) > time.monotonic():
            return None

        eff_deadline = (
            call_state.deadline_mono
            if (call_state is not None and call_state.deadline_mono is not None)
            else deadline_mono
        )
        if call_state is not None and call_state.breaker_tripped:
            return None
        if eff_deadline is not None and time.monotonic() >= eff_deadline:
            if call_state is not None:
                call_state.deadline_expired = True
            return None

        index_url = f"{SEC_ARCHIVE_HOST}/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession}-index.htm"
        abort_call = False
        saw_oversize = False
        oversize_url = index_url
        prior_failures = call_state.consecutive_failures if call_state is not None else 0

        if norm_form in {"8-K", "8-K/A"}:
            index_html, abort_call, is_over = await self._sec_get_text(
                index_url, deadline_mono=eff_deadline, call_state=call_state
            )
            if abort_call:
                return None
            if is_over:
                self._write_oversize_marker(key, index_url)
                return None
            candidates = select_8k_candidate_urls(
                index_html or "",
                cik_int=cik_int,
                acc_nodash=acc_nodash,
                primary_document=primary_document,
            )
            for dtype, doc_url in candidates[:2]:
                raw_doc, abort_call, is_over = await self._sec_get_text(
                    doc_url, deadline_mono=eff_deadline, call_state=call_state
                )
                if abort_call:
                    return None
                if is_over:
                    saw_oversize = True
                    oversize_url = doc_url
                    continue
                if not raw_doc:
                    continue
                snippets = extract_8k_snippets(raw_doc, max_chars=self.max_snippet_chars)
                if snippets:
                    result = ExtractedSecDocument(
                        url=doc_url,
                        snippet=snippets[0],
                        doc_type=dtype,
                        snippets=snippets,
                    )
                    self._write_disk_cache(key, result)
                    return result
        else:
            doc_url = _safe_archive_doc_url(
                primary_document, cik_int=cik_int, acc_nodash=acc_nodash
            )
            if not doc_url:
                index_html, abort_call, is_over = await self._sec_get_text(
                    index_url, deadline_mono=eff_deadline, call_state=call_state
                )
                if abort_call:
                    return None
                if is_over:
                    self._write_oversize_marker(key, index_url)
                    return None
                if index_html:
                    parser = _IndexTableParser()
                    try:
                        parser.feed(index_html[:400_000])
                    except Exception:
                        pass
                    for dtype, _desc, href in parser.rows:
                        if dtype.strip().upper() in {"10-Q", "10-Q/A", "10-K", "10-K/A"}:
                            doc_url = _safe_archive_doc_url(
                                href, cik_int=cik_int, acc_nodash=acc_nodash
                            )
                            if doc_url:
                                break
            if doc_url:
                raw_doc, abort_call, is_over = await self._sec_get_text(
                    doc_url, deadline_mono=eff_deadline, call_state=call_state
                )
                if abort_call:
                    return None
                if is_over:
                    self._write_oversize_marker(key, doc_url)
                    return None
                if raw_doc:
                    snippet = extract_periodic_summary_snippet(
                        raw_doc,
                        form=norm_form,
                        max_chars=self.max_snippet_chars,
                    )
                    if snippet:
                        result = ExtractedSecDocument(
                            url=doc_url,
                            snippet=snippet,
                            doc_type=norm_form,
                            snippets=(snippet,),
                        )
                        self._write_disk_cache(key, result)
                        return result

        transient_fail = (
            call_state is not None and call_state.consecutive_failures > prior_failures
        )
        if saw_oversize:
            self._write_oversize_marker(key, oversize_url)
        elif not abort_call and not transient_fail:
            self._neg_cache[key] = time.monotonic() + 300.0
        return None
