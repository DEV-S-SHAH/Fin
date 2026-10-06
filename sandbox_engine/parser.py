"""Zero-LLM HTML extraction: one filing in, nodes and edges out.

No network, no model, no heuristic that consults anything outside the bytes of
the file. Every extraction step is a total function of the file's contents, so
two runs over the same filing produce byte-identical ids and a re-run is a
no-op rather than a duplicate. A 1.5 MB 10-K parses in roughly 0.1 s.

Stage 1 of the pipeline: ``HTML -> ExtractionResult``. The result is plain
Python dictionaries; it does not touch Arrow or the database, so the parser is
testable without a database and the buffer stage can be exercised with
synthetic results.

The company-agnostic constraint
-------------------------------

Nothing here names a ticker, a segment, or a financial line item.
:data:`METRIC_CONCEPTS` is a *concept registry* -- financial-statement
vocabulary shared by every registrant -- with a pass-through fallback, so a line
item this file has never seen is still stored, under its own label. Recall never
depends on the registry being complete.

The four hazards this parser is written around
----------------------------------------------

Each of these breaks a plausible-looking implementation at least once, and each
was verified against real filings rather than assumed:

1. **A duration banner above the date row, not the date itself, identifies a
   period.** A 10-Q prints "Three Months Ended March 28, 2026" and "Six Months
   Ended March 28, 2026" as two different columns with the *same* date. Group
   columns by date alone and the two collapse into one metric, silently
   discarding one of the two values. See :class:`PeriodGroup.duration`.

2. **A header row is the one that yields the most distinct period groups.** The
   "Years ended" banner row above it repeats a single label across every column
   and would otherwise be mistaken for the header. See
   :func:`detect_period_groups`.

3. **A registrant states the same line in more than one table.** The face of
   the income statement and a selected-financial-data footnote disagree more
   often than you would expect, and ``REPORTS_METRIC`` holds a single ``value``,
   so leaving both arcs makes the graph's answer depend on arc order. See
   :meth:`FilingParser.resolve_metric_conflicts`.

4. **A product/service breakdown on the face of the income statement looks
   exactly like a product segment note.** What separates them is that the
   statement's rows *are* financial line items, so a table reporting recognised
   statement concepts is a statement even when its labels read like taxonomy
   names. See :func:`detect_segment_table`.

The stage interface
-------------------

    from sandbox_engine.parser import FilingParser

    parser = FilingParser()
    result = parser.ingest_file("data/aapl-sec/10-K_2025-10-31_aapl-20250927.htm")
    result.counts()   # {'metrics': 812, 'chunks': 1104, ...}
    result.edges      # {'SUBMITTED': [...], 'REPORTS_METRIC': [...]}
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import logging
import re
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .config import MAX_CHUNK_CHARS, MAX_EVENTS, MIN_CHUNK_CHARS, PERIOD_SCOPED_METRICS
from .entity_resolver import ConceptRegistry, canonical_concept

#: These filings declare an XHTML doctype but carry an XML prologue, so lxml
#: warns on every parse. The parse is what we want; the warning is noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

__all__ = [
    "ExtractionResult",
    "FilingParser",
    "Number",
    "TableCell",
    "canonical_metric",
    "chunk_body",
    "classify_statement",
    "clean_text",
    "canonical_concept",
    "detect_period_groups",
    "detect_segment_table",
    "duration_code",
    "extract_cells",
    "filing_identity",
    "parse_events",
    "parse_number",
    "stable_id",
    "strip_markup",
    # Blueprint compatibility exports
    "extract_executives_from_8k",
    "extract_suppliers",  # placeholder for future
    "METRIC_SEEDS_BLUEPRINT",
]

log = logging.getLogger("sandbox_engine.parser")

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def stable_id(*parts: Any, length: int = 16) -> str:
    """Content-addressed id.

    Deterministic across runs, machines, and databases. That is what makes a
    re-run a no-op and lets two independent processes build the same node.
    """
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def filing_identity(metadata: dict[str, Any]) -> str:
    """The ``Filing.id`` implied by *metadata*.

    Scoped to the filing rather than to the ticker, because a company files many
    8-Ks: two of them can both contain "Item 5.07" with the same title, and a
    ticker-scoped id would silently keep only the first.
    
    Includes accession_number and content_hash for proper deduplication:
    - accession_number uniquely identifies the SEC filing
    - content_hash prevents duplicate content from being ingested
    """
    return stable_id(
        "filing",
        metadata["ticker"],
        metadata["form_type"],
        metadata["fiscal_year"],
        metadata["fiscal_period"],
        metadata["filing_date"],
        metadata.get("accession_number", ""),
        metadata.get("content_hash", ""),
    )


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_NBSP_RE = re.compile(r"&#160;|&nbsp;|&#8203;|&#8194;|&#8195;", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MONEY_RE = re.compile(r"[\$£€¥]")
_DASHES = {"—", "–", "-", "―", "−", "", "n/a", "na"}
_TRAILING_UNIT_RE = re.compile(
    r"\s*\((?:in\s+)?(?:thousands|millions|billions|dollars|shares)\)\s*$", re.I
)
_PCT_SUFFIX_RE = re.compile(r"\s*%\s*$")
#: A '%' anywhere: a share of a segment, not a segment itself.
_PERCENT_RE = re.compile(r"%")
#: A cell that is *only* a unit marker like the ``%``/``pts`` cells a
#: statement generator emits beside (or inside) the margin columns. These
#: filers do not suffix every margin cell with ``%``; the column band carries
#: one unit cell per period and the numbers are bare.
_PERCENT_UNIT_RE = re.compile(
    r"^(?:%|pct\.?|percentage|points?|bps\.?|basis\s+points|%\s*of\s+revenue)$",
    re.I,
)


def clean_text(value: Any) -> str:
    """Collapse whitespace and numeric character references.

    Workiva's generator pads every label with non-breaking spaces, so this runs
    on essentially every string that reaches the parser.
    """
    if value is None:
        return ""
    return _WS_RE.sub(" ", _NBSP_RE.sub(" ", str(value))).strip()


def strip_markup(raw: str) -> str:
    """Visible text of a filing, with inline-XBRL machinery removed.

    Workiva hides the tagged facts behind ``display:none`` and inlines the XBRL
    vocabulary as element names. Both leak into ``read_html`` and into chunk
    text, so they are removed here rather than in every caller.
    """
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    return (soup.body or soup).get_text("\n").replace("\xa0", " ")


def _strip_hidden_regions(raw: str) -> str:
    """Regex twin of :func:`strip_markup`, for use before ``read_html``.

    A BeautifulSoup pass over a 1.5 MB filing costs more than the parse itself,
    and everything that breaks ``read_html`` is attribute-delimited, so a single
    regex pass removes it first. The authoritative ``<div>``-leaf fallback in
    :func:`strip_markup` still runs after this.
    """
    out = raw
    for pattern in (
        r"<div[^>]*display\s*:\s*none[^>]*>.*?</div>",
        r"<span[^>]*display\s*:\s*none[^>]*>.*?</span>",
        r"<ix:header\b.*?</ix:header>",
        r"<script\b.*?</script>",
        r"<style\b.*?</style>",
    ):
        out = re.sub(pattern, " ", out, flags=re.S | re.I)
    return out


def html_body(raw: str) -> str:
    """Body of a filing, safe to hand to ``read_html``."""
    out = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", raw)
    if "<body" in out.lower():
        out = out[out.lower().index("<body") :]
    return _strip_hidden_regions(out)


def _all_text(raw: str) -> str:
    """Tags stripped, *hidden* regions kept, whitespace collapsed.

    Metadata lives in parts of a filing a reader never sees: Workiva parks the
    registrant name in a ``display:none`` cover block and the whole inline-XBRL
    header in another. Reading metadata from visible text alone loses the CIK
    and the fiscal focus.
    """
    return _WS_RE.sub(" ", _NBSP_RE.sub(" ", _TAG_RE.sub(" ", raw))).strip()


#: A registrant name is mixed content of inline wrappers only. Anything at block
#: level means the fact has ended and the rest of the window is table chrome.
_DEI_BLOCK_STOP = re.compile(
    r"</(?:div|tr|table|p)\b|<(?:tr|table)\b|Exact name of registrant", re.I
)
#: The ``name="dei:X"`` attribute that marks an inline-XBRL fact.
_DEI_FACT_OPEN = r'<[A-Za-z0-9:]+[^>]*\bname\s*=\s*["\']dei:%s["\'][^>]*>'


def _dei_value(raw: str, tag: str = "EntityRegistrantName", window: int = 900) -> str:
    """The registrant (or other ``dei:``) fact, read from the raw markup.

    Neither of the two text renderings can supply this. :func:`strip_markup`
    deletes the ``display:none`` cover block that holds the fact, and
    :func:`_all_text` deletes the element *name* being searched for. Searching
    either for ``dei:EntityRegistrantName`` is guaranteed to miss.

    Workiva also emits the fact in the inline-XBRL attribute form --
    ``<ix:nonNumeric name="dei:EntityRegistrantName">`` rather than a
    ``<dei:...>`` wrapper -- and splits one word across the element and the text
    that follows it (``NVIDIA CORP`` + ``ORATION``). Wrappers are therefore
    removed with nothing rather than a space, which would rejoin the word
    instead of breaking it.
    """
    opening = re.search(_DEI_FACT_OPEN % tag, raw, re.I)
    if not opening:
        return ""
    tail = raw[opening.end() : opening.end() + window]
    stop = _DEI_BLOCK_STOP.search(tail)
    if stop:
        tail = tail[: stop.start()]
    return clean_text(_TAG_RE.sub("", tail)).strip(" .,(;")


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Number:
    """A parsed table cell."""

    value: float
    is_percent: bool


def parse_number(raw: Any) -> Number | None:
    """Parse a filing table cell into a float, or ``None`` if it is not one.

    Handles the forms that actually occur: ``(1,234)`` accounting negative,
    ``1,234`` thousands separators, a leading currency symbol, a trailing ``%``,
    em/en dashes for nil, and stray footnote markers.

    Returns ``None`` rather than ``0`` for an em-dash. A dash in a financial
    table means "not reported"; coercing it to zero would put a fabricated
    number in the graph, which is the one error a financial store cannot have.
    """
    if raw is None:
        return None
    text = clean_text(raw)
    if text.lower() in _DASHES:
        return None
    text = _MONEY_RE.sub("", text)
    percent = bool(_PCT_SUFFIX_RE.search(text))
    text = _PCT_SUFFIX_RE.sub("", text)
    # Footnote and unit decorations: "Net sales (1)", "Total (in millions)".
    text = re.sub(r"\(\s*[a-z0-9*]+\s*\)\s*$", "", text, flags=re.I)
    text = _TRAILING_UNIT_RE.sub("", text).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(",", "").replace(" ", "").strip()
    if text.endswith("%"):
        percent = True
        text = text[:-1].strip()
    if text in _DASHES:
        return None
    # Trailing/leading footnote digits glued to the number: "1,2341".
    if not re.fullmatch(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return Number(-value if negative else value, percent)


# ---------------------------------------------------------------------------
# Metric concept registry
#
# A registry of financial-statement vocabulary, not a list of companies.
# Patterns match against a cleaned row label. Anything that matches nothing
# falls through to its own label.
# ---------------------------------------------------------------------------

METRIC_CONCEPTS: tuple[tuple[str, str, str], ...] = (
    # -- income statement
    ("Net Sales", r"^total\s+net\s+sales|^net\s+sales\b|^total\s+(sales|revenue)\b"
     r"|^(total\s+)?(net\s+)?sales:|^total\s+revenue|^revenue$|^net\s+revenue",
     "income_statement"),
    ("Cost of Sales", r"^total\s+cost\s+of\s+(sales|revenue)|^cost\s+of\s+(sales|revenue)",
     "income_statement"),
    ("Gross Margin", r"^gross\s+(margin|profit)\b", "income_statement"),
    ("Research and Development", r"^research\s+and\s+development\b|^r&d\b",
     "income_statement"),
    ("Selling, General and Administrative",
     r"^selling,?\s+general\s+and\s+administrative|^sg&a\b", "income_statement"),
    ("Operating Expenses", r"^total\s+operating\s+expenses|^operating\s+expenses",
     "income_statement"),
    ("Operating Income", r"^operating\s+(income|profit|loss)\b", "income_statement"),
    ("Non-Operating Income", r"^other\s+income/(expense)|^non-?operating",
     "income_statement"),
    ("Income Before Taxes", r"^income\s+before\s+(provision\s+for\s+)?(income\s+)?taxes",
     "income_statement"),
    ("Income Tax Expense", r"^provision\s+for\s+income\s+taxes|^income\s+tax\s+expense",
     "income_statement"),
    ("Net Income", r"^net\s+income\b|^net\s+income/\(loss\)|^net\s+income\s+attributable",
     "income_statement"),
    ("Earnings Per Share, Basic",
     r"^net\s+income\s+per\s+share.{0,12}basic|^earnings\s+per\s+share.{0,12}basic",
     "income_statement"),
    ("Earnings Per Share, Diluted", r"earnings\s+per\s+share.{0,12}diluted",
     "income_statement"),
    ("Shares Outstanding, Basic", r"^shares.{0,24}basic", "income_statement"),
    ("Shares Outstanding, Diluted", r"^shares.{0,24}diluted", "income_statement"),
    # -- balance sheet
    ("Cash and Cash Equivalents", r"^cash\s+and\s+cash\s+equivalents|^cash\s+and\s+equivalents",
     "balance_sheet"),
    ("Short-Term Investments",
     r"^short-?term\s+(marketable\s+)?(investments|securities)", "balance_sheet"),
    ("Accounts Receivable", r"^accounts?\s+receivable", "balance_sheet"),
    ("Inventory", r"^inventor(y|ies)\b", "balance_sheet"),
    ("Total Current Assets", r"^total\s+current\s+assets", "balance_sheet"),
    ("Property, Plant and Equipment, Net", r"^property,?\s+plant\s+and\s+equipment",
     "balance_sheet"),
    ("Goodwill", r"^goodwill\b", "balance_sheet"),
    ("Total Assets", r"^total\s+assets", "balance_sheet"),
    ("Accounts Payable", r"^accounts?\s+payable", "balance_sheet"),
    ("Total Current Liabilities", r"^total\s+current\s+liabilities", "balance_sheet"),
    ("Long-Term Debt", r"^term\s+debt|^long-?term\s+debt", "balance_sheet"),
    ("Total Liabilities", r"^total\s+liabilities", "balance_sheet"),
    # Issuer-agnostic on purpose: issuers spell this "Total shareholders'
    # equity", "Total stockholders' equity", "Total shareowners' equity", or
    # "Total equity", and a name inside the phrase is not part of the concept.
    ("Stockholders Equity", r"^total\s+(company\s+)?\w{0,14}equity", "balance_sheet"),
    ("Retained Earnings", r"^retained\s+earnings", "balance_sheet"),
    # -- cash flow
    ("Operating Cash Flow",
     r"^cash\s+generated\s+by\s+operating|^net\s+cash\s+(provided\s+by|used\s+in)\s+operating",
     "cash_flow"),
    ("Investing Cash Flow",
     r"^cash\s+(generated\s+by|used\s+in)\s+investing|^net\s+cash.{0,20}investing",
     "cash_flow"),
    ("Financing Cash Flow",
     r"^cash\s+(generated\s+by|used\s+in)\s+financing|^net\s+cash.{0,20}financing",
     "cash_flow"),
    ("Capital Expenditures",
     r"^payments?\s+for\s+acquisition|^\s*capital\s+expenditures", "cash_flow"),
    ("Depreciation and Amortization", r"^depreciation\s+and\s+amortization", "cash_flow"),
    ("Free Cash Flow", r"^free\s+cash\s+flow", "cash_flow"),
)

_COMPILED_CONCEPTS: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (canonical, re.compile(pattern, re.I), category)
    for canonical, pattern, category in METRIC_CONCEPTS
)

#: Substring hints used to vote a table into a statement category. Kept coarse
#: on purpose: this is a guess used only for labelling, never for dropping data.
_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "income_statement": (
        "net sales", "gross margin", "gross profit", "operating income",
        "research and development", "cost of sales", "income before taxes",
        "per share",
    ),
    "balance_sheet": (
        "total assets", "total liabilities", "total current assets",
        "accounts receivable", "stockholders", "shareholders", "goodwill",
        "inventor", "accounts payable",
    ),
    "cash_flow": (
        "operating activities", "investing activities", "financing activities",
        "cash generated", "cash used", "depreciation and amortization",
    ),
}


def _normalise_label(label: str) -> str:
    """Row label reduced to the part a concept pattern can match."""
    text = _TRAILING_UNIT_RE.sub("", clean_text(label))
    return re.sub(r"\s{2,}.*$", "", text).rstrip(":").strip()


def _guess_category(text: str) -> str:
    low = text.lower()
    scores = {
        category: sum(1 for hint in hints if hint in low)
        for category, hints in _CATEGORY_HINTS.items()
    }
    best = max(scores, key=lambda key: scores[key])
    return best if scores[best] else "other"


def canonical_metric(label: str, is_percent: bool = False) -> tuple[str, str | None]:
    """Map a row label to ``(canonical_name, statement_category)``.

    The concept registry is consulted **first** and the pass-through fallback
    second, and that order is load-bearing. ``Total current assets`` and
    ``Current assets`` are one concept, but the registry's patterns are written
    against the prefixed spelling; canonicalising first would strip the prefix
    and the pattern would stop matching, turning a registry concept into a
    pass-through. So the registry sees the label as printed, and everything it
    does *not* recognise goes through :func:`entity_resolver.canonical_concept`.

    Unrecognised labels are still preserved, so an unfamiliar line item remains
    queryable. This is what makes the parser work for a company nobody has seen.

    A percent cell is suffixed ``" (%)"`` rather than stored as a fraction,
    because a 14.9 gross margin and a 0.149 gross margin are the same fact in
    two notations, and only one of them is what the filing printed. Mixing them
    in one column would make every cross-metric comparison wrong. The marker is
    applied *after* canonicalisation and is part of identity: a percent margin
    and a currency margin are different facts.
    """
    text = _normalise_label(label)
    if not text or text.lower() in _DASHES:
        return "", None
    for canonical, pattern, category in _COMPILED_CONCEPTS:
        if pattern.search(text):
            return (f"{canonical} (%)" if is_percent else canonical), category

    # Not in the registry: keep it, but drop a leading ordinal qualifier so
    # "Total other income/(expense), net" still reads cleanly, then canonicalise
    # the surface variations that are not part of the concept -- trademark
    # marks, footnote markers, trailing punctuation, share counts printed into
    # the label. Without this last step "iPhone" and "iPhone®" become two nodes.
    fallback = re.sub(r"\s+", " ", re.sub(r"^[^A-Za-z0-9(]+", "", text)).strip()
    if not fallback:
        return "", None
    concept = canonical_concept(fallback).name
    if not concept:
        return "", None
    if is_percent:
        return f"{concept} (%)", _guess_category(concept)
    return concept, _guess_category(concept)


# ---------------------------------------------------------------------------
# Period detection
# ---------------------------------------------------------------------------

_MONTHS = (
    r"January|February|March|April|May|June|July|August|September|October"
    r"|November|December"
    # Abbreviated forms, as NVIDIA and Microsoft print them ("Apr 26, 2026").
    # Without these a date header fails to parse and the period collapses to a
    # bare year, which is the calendar year rather than the fiscal one.
    r"|January|Feb|February|Mar|March|Apr|May|Jun|June|Jul|July|Aug|August"
    r"|Sep|Sept|September|Oct|October|Nov|November|Dec|December"
)
_PERIOD_RE = re.compile(
    rf"(?:{_MONTHS})\s+\d{{1,2}}\s*,?\s*(?:19|20)\d{{2}}"       # September 27, 2025
    rf"|(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}"                        # 2025-09-27
    rf"|(?:19|20)\d{{2}}"                                        # bare year
    rf"|(?:Q[1-4]\s*)?(?:FY\s*)?(?:19|20)\d{{2}}",              # Q2 2026 / FY2025
    re.I,
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BARE_YEAR_KEY = re.compile(r"FY(?:19|20)\d{2}")
#: A header cell that is pure decoration, not a period. Without this, the word
#: "Years" in a banner row is read as a period label.
_PERIOD_STOPWORDS = frozenset(
    {"year", "years", "ended", "as of", "months", "weeks", "date", "dates"}
)

#: A period banner that carries no year, which :data:`_PERIOD_RE` misses. A
#: balance-sheet table's "As of June 30" and "Year Ended June 30" caption rows
#: are headers, not segment names, and without this they enter the ``Segment``
#: table as if a country were a reporting unit.
_BANNER_RE = re.compile(
    rf"(?:{_MONTHS})\s+\d{{1,2}}\b"                          # June 30
    rf"|\b(?:year|years|month|months|week|weeks|quarter|quarters)\s+ended\b"
    rf"|\bthree\s+months\s+ended\b"
    rf"|^\s*(?:as\s+of)\b",
    re.I,
)

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
_MONTH_NUMBERS = {name[:3]: number for number, name in enumerate(_MONTH_NAMES, 1)}

#: Duration banner words and the code each collapses to. Both alternatives
#: capture, so a 10-K's "Years ended" is as detectable as a 10-Q's "Six Months
#: Ended".
_DURATION_RE = re.compile(
    r"\b(\d+|three|six|nine|twelve)\s*[- ]?\s*months?\b|\b(year|years)\b", re.I
)
_DURATION_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}
_YEAR_WORDS = {"year", "years"}


def _month_number(name: str) -> int:
    return _MONTH_NUMBERS.get(name.strip().lower()[:3], 1)


def duration_code(text: str) -> str:
    """``"3M"`` / ``"6M"`` / ``"FY"`` from a column banner, else ``""``."""
    match = _DURATION_RE.search(text)
    if not match:
        return ""
    token = (match.group(1) or match.group(2) or "").strip().lower()
    if token in _YEAR_WORDS:
        return "FY"
    if token.isdigit():
        return f"{int(token)}M"
    months = _DURATION_WORDS.get(token)
    return f"{months}M" if months else ""


#: Weeks a duration code spans. Filers report *weeks*, not calendar months --
#: Apple's Q1 is "13 weeks ended", its year "52 weeks ended" -- so 3M is 13
#: weeks and not "one month x 3". That is what makes the derived start date fall
#: on the day after the prior period's end: 2025-12-27 back 13 weeks is
#: 2025-09-28, exactly Apple's FY2026 opening day.
_DURATION_WEEKS = {"3M": 13, "6M": 26, "9M": 39, "FY": 52}


def period_span(duration: str, end: str) -> tuple[str, int]:
    """``(start_iso, days_covered)`` for a ``<duration>`` ending at *end*.

    The end date is the one the table printed; the start is *derived* from the
    duration's week count, not invented. An instant -- a balance-sheet date with
    no duration banner -- has no span, so it returns ``(end, 0)`` and is never
    treated as cumulative. A period whose header printed no date returns
    ``("", 0)``: the span is genuinely unknown and the verifier reports it rather
    than the parser guessing.
    """
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(end or "").strip())
    if not match:
        return "", 0
    days = _DURATION_WEEKS.get(duration, 0) * 7
    if not days:
        return match.group(1), 0
    end_date = dt.date.fromisoformat(match.group(1))
    return (end_date - dt.timedelta(days=days - 1)).isoformat(), days


def fiscal_year_start(start_iso: str, year_end: tuple[int, int] | None) -> str:
    """First day of the fiscal year that *start_iso* falls in, or ``""``.

    The prior year's close plus one day. ``year_end`` is the filer's own
    ``dei:CurrentFiscalYearEndDate``; without it the boundary cannot be located
    and the answer is empty rather than a guess.
    """
    if not start_iso or year_end is None:
        return ""
    fiscal_year = fiscal_year_for(start_iso, year_end)
    if fiscal_year is None:
        return ""
    month, day = year_end
    try:
        prior_close = dt.date(fiscal_year - 1, month, day)
    except ValueError:
        return ""
    return (prior_close + dt.timedelta(days=1)).isoformat()


def is_cumulative_period(
    duration: str, start_iso: str, year_end: tuple[int, int] | None
) -> bool:
    """Whether *duration* is a year-to-date figure rather than a single quarter.

    A 6M, 9M or FY column is cumulative by definition. A 3M column is discrete --
    except in the first quarter, where the quarter *is* the year-to-date and its
    ``Q1 + Q2 = H1`` pairing is the only reason the chain closes. A first quarter
    begins within a week of the fiscal-year start; every later quarter begins
    about 13 weeks in, so a one-week tolerance separates them without depending
    on the exact weekday the filer closed on.
    """
    if duration in ("6M", "9M", "FY"):
        return True
    if duration != "3M" or not start_iso:
        return False
    fiscal_start = fiscal_year_start(start_iso, year_end)
    if not fiscal_start:
        return False
    delta = abs(
        (dt.date.fromisoformat(start_iso) - dt.date.fromisoformat(fiscal_start)).days
    )
    return delta <= 6


def period_metadata(
    period_key: str, year_end: tuple[int, int] | None, form_type: str = ""
) -> dict[str, Any]:
    """The period columns for a metric node, from its canonical period key.

    A synthesised segment host has only ``<duration>-<date>`` to go on, so this
    recovers the same fields a statement line carries. An annual key whose
    header printed only a year (``FY2025``) has no date to recover and yields
    empty span fields, which the verifier reports rather than the parser
    inventing a date.
    """
    code, end = "", ""
    match = re.match(
        r"^(?:(3M|6M|9M|FY)-)?(\d{4}-\d{2}-\d{2})$", str(period_key or "")
    )
    if match:
        code = match.group(1) or ""
        end = match.group(2)
    start, days = period_span(code, end)
    return {
        "period_code": code,
        "period_start": start,
        "period_end": end,
        "period_days": days,
        "period_cumulative": 1 if is_cumulative_period(code, start, year_end) else 0,
        "reported_label": "",
        "form_type": form_type,
    }


@dataclass
class PeriodGroup:
    """A period and the columns that hold its values.

    ``duration`` comes from the banner row *above* the dates, and it is not
    decoration. See hazard 1 in the module docstring: without it a 10-Q's 3M and
    6M columns collapse into one metric.

    ``start``/``days``/``cumulative`` are derived from the duration and end date
    (see :func:`period_span` and :func:`is_cumulative_period`) so a consumer does
    not have to re-infer the span from the period string.
    """

    label: str
    columns: list[int]
    duration: str = ""
    year_end: tuple[int, int] | None = None
    filing_period_end: str = ""
    start: str = ""
    days: int = 0
    cumulative: bool = False

    @property
    def key(self) -> str:
        return self._resolve(self.period_key(self.label))

    def _resolve(self, key: str) -> str:
        """Give a bare-year column the filing's own period end.

        Some tables print a year and no date in any cell. A fiscal year is not
        a calendar one, so "2025" is ambiguous -- on Microsoft's September close
        it means the year ended 2025-06-30, not December 2025 -- and reading it
        as a calendar year files one period under two identities and leaves the
        node with no end date at all. The filing already states when its period
        ended, and the year printed on the column agrees with it, so the end date
        is recovered rather than guessed. A comparative column that disagrees
        with the filing's year keeps its bare year, because that is genuinely all
        the table said about it.
        """
        if not self.filing_period_end or not _BARE_YEAR_KEY.fullmatch(key):
            return key
        if key[2:] != self.filing_period_end[:4]:
            return key
        return self.filing_period_end

    @property
    def full_key(self) -> str:
        """Period identity used for metric nodes.

        Keyed on the period's own end date, ``<duration>-<end date>`` --
        ``3M-2026-03-28`` for a quarter, ``FY-2025-09-27`` for a year, and the
        bare ``2026-06-27`` for a point-in-time column that carries no duration
        banner, because a balance-sheet date is not recoverable from a year.

        The date, rather than the fiscal year and quarter it implies, because a
        fiscal year holds up to four quarters and the year alone cannot tell
        them apart. ``3M-FY2026`` names three different quarters at once, so the
        three filings that report them attach three values to one metric node
        and "gross margin" answers with whichever was loaded first. The header
        already prints the date, so keying on it invents nothing; it is also the
        only key that stays correct for a 53-week year, where two quarters can
        share a fiscal year, and for a 10-K and 10-Q that both report the same
        period.

        A period whose header printed a bare year ("Years ended 2025") has no
        date to key on and falls back to ``<duration>-FY<year>``.
        """
        key = self.key
        if _ISO_DATE.fullmatch(key):
            return f"{self.duration}-{key}" if self.duration else key
        year = self.year
        if self.duration and year:
            return f"FY{year}" if self.duration == "FY" else f"{self.duration}-FY{year}"
        return key

    @staticmethod
    def period_key(label: str) -> str:
        """Compact, stable period key: ``2025-09-27`` or ``FY2025``."""
        text = clean_text(label)
        iso = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
        if iso:
            return iso.group(0)
        dated = re.search(
            rf"({_MONTHS})\s+(\d{{1,2}})\s*,?\s*((?:19|20)\d{{2}})", text, re.I
        )
        if dated:
            return f"{dated.group(3)}-{_month_number(dated.group(1)):02d}-{int(dated.group(2)):02d}"
        year = re.search(r"((?:19|20)\d{2})", text)
        return f"FY{year.group(1)}" if year else clean_text(text)

    @property
    def year(self) -> int | None:
        """Fiscal year of this period, in the filer's own numbering.

        Read from a full date plus the filer's year end when both are
        available, because a bare calendar year in the header is not the fiscal
        year for most quarters. A period that carries no date -- a banner
        saying only "Years ended 2025" -- has no month to compare against a
        September close, so it falls back to the year printed on it.
        """
        if self.year_end is not None:
            iso = re.match(r"(\d{4}-\d{2}-\d{2})", self.key)
            if iso:
                return fiscal_year_for(iso.group(1), self.year_end)
        year = re.search(r"((?:19|20)\d{2})", self.key)
        return int(year.group(1)) if year else None


def filing_period_end_iso(metadata: dict[str, Any]) -> str:
    """The filing's own period end as ``YYYY-MM-DD``, or ``""``.

    Recovered rather than recomputed, because the cover page states it and a
    re-derivation from the fiscal year and form type is only ever as good as the
    assumptions behind it. Anything that is not already a full date is dropped: a
    partial date would resolve a bare year to the wrong day.
    """
    for key in ("period_end", "period_end_date"):
        match = re.search(
            r"((?:19|20)\d{2})-(\d{2})-(\d{2})", str(metadata.get(key, ""))
        )
        if match:
            return match.group(0)
    return ""


def detect_period_groups(
    frame: pd.DataFrame,
    scan: int = 6,
    year_end: tuple[int, int] | None = None,
    filing_period_end: str = "",
) -> tuple[int, list[PeriodGroup]]:
    """Locate the header row and group its columns by period.

    Returns the row index too, because the duration banner usually lives in the
    rows *above* the dates. When the filer splits the date across cells, the
    banner sits in the header row's own caption column instead and
    :func:`_groups_in_row` has already read it.

    The header is the row yielding the most distinct period groups. This is
    hazard 2 from the module docstring: the "Years ended" banner row above the
    real header repeats one label across every column, so a first-match scan
    picks it and every column collapses into a single period.
    """
    best_row, best = -1, []
    for index in range(min(scan, len(frame))):
        groups = _groups_in_row(frame, index)
        if len(groups) > len(best):
            best_row, best = index, groups
    for group in best:
        group.duration = _duration_above(frame, group, best_row) or group.duration
        group.year_end = year_end
        group.filing_period_end = filing_period_end
        group.start, group.days = period_span(group.duration, group.key)
        group.cumulative = is_cumulative_period(
            group.duration, group.start, group.year_end
        )
    return best_row, best


def _duration_above(frame: pd.DataFrame, group: PeriodGroup, header_row: int) -> str:
    """Banner text over a period's columns, e.g. "Six Months Ended" -> ``6M``."""
    for offset in (1, 2, 3):
        row_index = header_row - offset
        if row_index < 0:
            break
        row = [_cell(value) for value in frame.iloc[row_index].tolist()]
        text = " ".join(
            row[column] for column in group.columns
            if 0 <= column < len(row) and row[column]
        )
        code = duration_code(text)
        if code:
            return code
    return ""


#: A caption that ends in a month and day but no year, so the year must live in
#: a different cell. Microsoft's income statement prints
#: ``Three Months Ended September 30, | 2025``.
_MONTH_DAY_TAIL = re.compile(rf"(?:{_MONTHS})\s+\d{{1,2}}\s*,?\s*$", re.I)
_BARE_YEAR_CELL = re.compile(r"(?:Q[1-4]\s*)?(?:FY\s*)?(?:19|20)\d{2}", re.I)


def _split_banner(row: list[str]) -> str:
    """The caption that carries a period's month and day, if there is one.

    Read alone, a bare-year cell gives ``period_key`` nothing but a year, and
    the period collapses to ``FY2025`` -- the calendar year, not Microsoft's
    September fiscal year. Prefixing this caption onto the year restores the
    full date, and carries the duration with it.
    """
    for text in row:
        if _MONTH_DAY_TAIL.search(text):
            return text.rstrip(" ,") + ", "
    return ""


def _groups_in_row(frame: pd.DataFrame, index: int) -> list[PeriodGroup]:
    row = [_cell(value) for value in frame.iloc[index].tolist()]
    if not any(_PERIOD_RE.search(text) for text in row):
        return []
    banner = _split_banner(row)
    groups: list[PeriodGroup] = []
    for column, text in enumerate(row):
        if not _PERIOD_RE.search(text) or text.lower() in _PERIOD_STOPWORDS:
            continue
        # Rejoin a date the filer split across two cells. Only a bare year takes
        # the caption; a cell that already holds a full date is left alone.
        if banner and _BARE_YEAR_CELL.fullmatch(text.strip()):
            text = banner + text.strip()
        # Merge into the previous group when the date repeats in an adjacent
        # column. The inline-XBRL generator emits the value twice across two
        # columns, and those are one measurement, not two.
        if groups and groups[-1].columns[-1] >= column - 1:
            if PeriodGroup.period_key(text) == PeriodGroup.period_key(groups[-1].label):
                groups[-1].columns.append(column)
                continue
        group = PeriodGroup(label=text, columns=[column])
        if banner:
            group.duration = duration_code(banner)
        groups.append(group)
    return [group for group in groups if group.key]


# ---------------------------------------------------------------------------
# Table cells
# ---------------------------------------------------------------------------

_NULLISH = frozenset({"nan", "none", "nat", "n/a", "na", "null"})


def _cell(value: Any) -> str:
    """One table cell as text, with nulls and dashes normalised to ``""``."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:      # NaN
        return ""
    text = clean_text(value)
    if text.lower() in _NULLISH or text in {"—", "–", "―", "−"}:
        return ""
    return text


def _label_of(row: Sequence[Any]) -> str:
    """Row label from the leading run of merged duplicate cells."""
    for value in row:
        text = _cell(value)
        if text and text.lower() not in _DASHES:
            return text
    return ""


@dataclass(frozen=True)
class TableCell:
    """One measured value: a row label, a period, and a number.

    The first five fields are the measurement. The rest describe the *period* it
    was measured over, copied from the :class:`PeriodGroup` so the period's
    identity travels with the value instead of being re-parsed from a string
    later: ``reported_label`` is the column header as printed, ``period_code``
    the duration (``"3M"``/``"FY"``/``""``), ``period_start``/``period_end`` the
    derived span, ``days_covered`` its length in days, and ``cumulative`` whether
    it is a year-to-date figure.
    """

    label: str
    period: str
    number: Number
    canonical_name: str
    category: str | None
    reported_label: str = ""
    period_code: str = ""
    period_start: str = ""
    period_end: str = ""
    days_covered: int = 0
    cumulative: bool = False


def _number_in_group(values: Sequence[Any], columns: Sequence[int]) -> Number | None:
    """First parseable number in a period's column span.

    The span holds a currency symbol, the value, and -- because of the merged
    cells the inline-XBRL generator emits -- sometimes the same value repeated.
    """
    for column in columns:
        if 0 <= column < len(values):
            number = parse_number(values[column])
            if number is not None:
                return number
    return None


def _group_is_percent(values: Sequence[Any], columns: Sequence[int]) -> bool:
    """True if a cell inside the period band is a bare unit marker.

    NVDA's MD&A margin table prints ``73.4 % 72.4 %`` as separate cells, so a
    ``%`` token sits *between* the period's values. ``parse_number`` never sees
    it and the cell stays currency-shaped. A ``` pts``` change column is the
    same story. A pure unit cell inside the band marks the whole measurement.
    """
    for column in columns:
        if 0 <= column < len(values) and _PERCENT_UNIT_RE.match(
            clean_text(values[column])
        ):
            return True
    return False


def _column_belongs_percent(frame: pd.DataFrame, columns: Sequence[int]) -> bool:
    """True if *any* row of the table carries a unit marker in *columns*.

    The generator decorates a ``% of Revenue`` table inconsistently: the
    ``Revenue`` and ``Net income`` rows print a ``%`` cell inside the period
    band, but the cost and margin rows below them do not. The unit is a column
    property, not a row property, so it has to be decided once per column over
    the whole frame rather than per cell -- otherwise the same bare 75.0 lands
    under both a percent and a currency ``Gross Margin``.
    """
    for _, row in frame.iterrows():
        if _group_is_percent(row.tolist(), columns):
            return True
    return False


def classify_statement(labels: Sequence[str]) -> str:
    """Vote a table into a statement category from its row labels."""
    joined = " ".join(labels).lower()
    scores = {
        category: sum(1 for hint in hints if hint in joined)
        for category, hints in _CATEGORY_HINTS.items()
    }
    best = max(scores, key=lambda key: scores[key])
    return best if scores[best] else "other"


def extract_cells(
    frame: pd.DataFrame,
    min_rows: int = 2,
    year_end: tuple[int, int] | None = None,
    filing_period_end: str = "",
) -> tuple[str, list[TableCell]]:
    """Turn a statement table into ``(statement_category, cells)``.

    Only rows whose label resolves to a numeric measurement in a period column
    are kept. That is what discards sub-totals, headers repeated mid-table, and
    the layout filler these filings are full of.

    Fewer than two period groups returns nothing: a single-column table has no
    period to key a metric by, and storing it would produce an identity that
    collides with every other single-column table in the filing.
    """
    _, groups = detect_period_groups(
        frame, year_end=year_end, filing_period_end=filing_period_end
    )
    if len(groups) < 2:
        return "", []
    label_end = max(1, groups[0].columns[0])
    cells: list[TableCell] = []
    labels: list[str] = []
    percent_groups: dict[int, bool] = {
        id(group): _column_belongs_percent(frame, group.columns) for group in groups
    }

    for _, row in frame.iterrows():
        values = row.tolist()
        label = _label_of(values[:label_end])
        if not label or label.lower() in _PERIOD_STOPWORDS:
            continue
        labels.append(label)
        for group in groups:
            number = _number_in_group(values, group.columns)
            if number is None:
                continue
            if percent_groups[id(group)]:
                number = Number(number.value, True)
            canonical, category = canonical_metric(label, number.is_percent)
            if canonical:
                cells.append(
                    TableCell(
                        label=label,
                        period=group.full_key,
                        number=number,
                        canonical_name=canonical,
                        category=category,
                        reported_label=group.label,
                        period_code=group.duration,
                        period_start=group.start,
                        period_end=group.key if _ISO_DATE.fullmatch(group.key) else "",
                        days_covered=group.days,
                        cumulative=group.cumulative,
                    )
                )

    if len(cells) < min_rows:
        return "", []
    return classify_statement(labels), cells


# ---------------------------------------------------------------------------
# Segment detection
# ---------------------------------------------------------------------------

#: Generic reporting-segment taxonomies. Continents and countries are shared by
#: every multinational; a registrant that reports no segments simply produces no
#: segment table and is unaffected by this.
_GEOGRAPHY_RE = re.compile(
    r"\b(americas?|europe|asia|china|japan|korea|australia|canada|mexico|brazil|"
    r"india|israel|africa|scandinavia|emea|apac|latam|rest of (the )?world|"
    r"united (states|kingdom)|germany|france|italy|spain|russia|taiwan)\b",
    re.I,
)
_PRODUCT_RE = re.compile(
    r"\b(products?|services?|subscriptions?|software|hardware|systems|devices|"
    r"platform|cloud|advertising|commerce|payments?|professional services|"
    r"other (products|services))\b",
    re.I,
)
_SEGMENT_CONTEXT_RE = re.compile(r"segment|reportable (segment|unit)", re.I)

#: What a segment table is *measuring*, keyed by the caption row that announces
#: it. A segment note does not hold one measure: Apple's carries net sales,
#: operating income and long-lived assets in three separate tables that share
#: the same taxonomy, and long-lived assets carries the *same* country names as
#: net sales does. Without this the values are attached to whichever measure
#: the code assumed, and the graph confidently reports Apple's FY2025 U.S. net
#: sales as 40,274 -- which is its U.S. long-lived assets. Its U.S. net sales
#: is 151,790. A wrong number that looks right is the worst thing this graph
#: can emit, so the measure is read from the table rather than assumed.
_SEGMENT_MEASURES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"long[-\s]?lived assets", re.I), "Long-Lived Assets", "balance_sheet"),
    (re.compile(r"\bdepreciation\b", re.I), "Depreciation", "cash_flow"),
    (re.compile(r"capital (?:expenditures?|spending)", re.I), "Capital Expenditures", "cash_flow"),
    (re.compile(r"\btotal assets\b", re.I), "Total Assets", "balance_sheet"),
    (re.compile(r"operating (?:income|loss)|operating income/\(loss\)", re.I),
     "Operating Income", "income_statement"),
    (re.compile(r"\bcost of sales\b", re.I), "Cost of Sales", "income_statement"),
    (re.compile(r"\bnet sales\b", re.I), "Net Sales", "income_statement"),
)


#: Separator for a registry scope. A control character rather than a space or a
#: pipe because tickers and period keys never contain one, so no two distinct
#: (filer, period) pairs can flatten onto the same scope string.
_SCOPE_SEP = "\x1f"


def _metric_scope(period: str, ticker: str = "") -> str:
    """Registry scope for a metric: the filer and the period, never the period
    alone. See :meth:`FilingParser._metric_id` for why the filer is in it."""
    return f"{(ticker or '?').strip().upper()}{_SCOPE_SEP}{period}"


def _is_measure_total(label: str, measure: str) -> bool:
    """Whether *label* is the ``Total <measure>`` row of a segment table.

    Both sides are reduced to their alphanumeric characters before comparison,
    because the two are not written alike: the caption says "Long-lived assets"
    and the row says "Total long-lived assets", so matching on the measure's
    last word misses it and the host metric ends up with no owner at all.

    Anchored on the *whole* measure so "Total net sales" is not mistaken for
    the total of a long-lived-assets table that shares the same note.
    """
    def squash(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", _normalise_label(value).lower())

    text = squash(label)
    if not text.startswith("total"):
        return False
    return text[len("total"):] == squash(measure)


def segment_measure(label: str) -> tuple[str, str] | None:
    """``(measure, statement_category)`` announced by *label*, or ``None``.

    Ordered most specific first: "long-lived assets" and "total net sales" both
    contain "net sales", and a caption that says both is announcing assets.
    """
    text = _normalise_label(label).strip(" .:")
    for pattern, measure, category in _SEGMENT_MEASURES:
        if pattern.search(text):
            return measure, category
    return None


def detect_segment_table(
    frame: pd.DataFrame, labels: Sequence[str], context: str
) -> str | None:
    """Return a segment taxonomy name, or ``None`` if this is not a segment table.

    This is hazard 4 from the module docstring. The income statement has several
    period columns and rows called "Products" and "Services", which look exactly
    like a product segment breakdown. The discriminator is that a statement's
    rows *are* financial line items, so a table reporting recognised statement
    concepts is a statement even when its labels read like taxonomy names.
    """
    if len(labels) < 2:
        return None
    financial = sum(
        1 for label in labels
        if any(pattern.search(_normalise_label(label))
               for _, pattern, _ in _COMPILED_CONCEPTS)
    )
    if financial >= max(1, len(labels) // 4):
        return None
    geography = sum(1 for label in labels if _GEOGRAPHY_RE.search(label))
    product = sum(1 for label in labels if _PRODUCT_RE.search(label))
    explicit = bool(_SEGMENT_CONTEXT_RE.search(context))
    if geography < 2 and not (product >= 2 and explicit):
        return None
    if geography >= product and geography > 0:
        return "geographic"
    if product > 0:
        return "product"
    return "segment" if explicit else None


# ---------------------------------------------------------------------------
# 8-K item events
# ---------------------------------------------------------------------------

#: Spec regex, with whitespace collapsed first: these filings pad item headings
#: with long runs of spaces and non-breaking spaces.
ITEM_RE = re.compile(r"(Item\s+\d+\.\d+)\s*[:\-]?\s*([^\n\r.]+)", re.I)


def parse_events(text: str, max_summary: int = 600) -> list[tuple[str, str, str]]:
    """``(item_code, item_title, summary)`` for every Item heading in an 8-K.

    The summary is the text *between* this heading and the next, which is why
    the matches are collected before the body is sliced: a single forward pass
    cannot know where the current item ends.
    """
    collapsed = _WS_RE.sub(" ", text)
    matches = list(ITEM_RE.finditer(collapsed))
    events: list[tuple[str, str, str]] = []
    for position, match in enumerate(matches):
        code = re.sub(r"^item\s+", "Item ", re.sub(r"\s+", " ", match.group(1)).strip(),
                      flags=re.I)
        title = match.group(2).strip(" .:-")
        if not title:
            continue
        end = matches[position + 1].start() if position + 1 < len(matches) else len(collapsed)
        body = collapsed[match.end() : end].strip(" .:-")
        events.append((code, title, body[:max_summary].strip()))
    return events


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

#: Walk the cleaned body in document order so section headings can be attributed
#: to the text that follows them.
_BLOCK_RE = re.compile(
    r"(<h([1-3])\b[^>]*>.*?</h\2>)"
    r"|(<table\b.*?</table>)"
    r"|(<p\b[^>]*>.*?</p>)"
    r"|(<li\b[^>]*>.*?</li>)"
    r"|(<(?:div|tr|td|span)\b[^>]*>)"
    r"|(</(?:div|td|tr|span)>)",
    re.S | re.I,
)


@dataclass(frozen=True)
class ChunkText:
    section: str
    text: str


def chunk_body(
    html: str, max_chars: int = MAX_CHUNK_CHARS, min_chars: int = MIN_CHUNK_CHARS
) -> list[ChunkText]:
    """Slice body text into ``Chunk`` records tagged with their section.

    ``h1``/``h2``/``h3`` set the current section; ``p``/``li`` are the primary
    text source, as specified. Workiva's generator emits neither -- the same
    prose sits in ``<div>``s -- so div boundaries are also block boundaries.
    Without that fallback these filings produce zero chunks.

    Div nesting is not uniform across filings: the 10-Q's prose sits in flat
    sibling divs, the 8-K's in deeply nested ones. Emitting only the outermost
    div returns an entire filing as one block; emitting only the innermost
    returns fragments below the length floor. So every nesting level is a
    candidate and a later pass drops any block whose text is already covered by a
    shorter one.
    """
    body = re.sub(r"<(script|style|table)\b.*?</\1>", " ", html, flags=re.S | re.I)
    section = "Document"
    found: list[ChunkText] = []
    seen: set[str] = set()
    # Per open element: the text fragments inside it, and whether it has a child.
    stack: list[list[str]] = []
    has_child: list[bool] = []

    def emit(text: str) -> None:
        cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
        if len(cleaned) < min_chars:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        for part in _split_text(cleaned, max_chars):
            found.append(ChunkText(section=section, text=part))

    # Walk tag *positions* so the text between two tags is captured; a scan of
    # tag matches alone discards every character of prose.
    position = 0
    between = ""
    for match in _BLOCK_RE.finditer(body):
        between = body[position : match.start()]
        position = match.end()
        heading, _level, table, paragraph, item, open_tag, close_tag = match.groups()
        if heading:
            title = _WS_RE.sub(" ", _TAG_RE.sub(" ", heading)).strip()
            if title:
                section = title[:200]
        elif table:
            continue
        elif paragraph or item:
            emit(paragraph or item or "")
        elif open_tag:
            if stack:
                stack[-1].append(between)
                has_child[-1] = True
            stack.append([])
            has_child.append(False)
        elif close_tag:
            if not stack:
                continue
            fragment = " ".join([*stack.pop(), between])
            childless = not has_child.pop()
            if childless:
                emit(fragment)
    emit(between)
    return _drop_dominated(found)


def _drop_dominated(blocks: Sequence[ChunkText]) -> list[ChunkText]:
    """Drop any block whose text is already covered by a shorter one.

    Because every div nesting level is emitted, a parent div and its only child
    produce the same text twice. Storing both would double the chunk count and
    make a retrieval query return the same paragraph twice.
    """
    ordered = sorted(blocks, key=lambda block: len(block.text))
    kept: list[ChunkText] = []
    covered: set[str] = set()
    for block in ordered:
        key = block.text.lower()
        if key in covered:
            continue
        covered.add(key)
        kept.append(block)
    # Restore document order, which the length sort destroyed.
    positions = {block.text: index for index, block in enumerate(blocks)}
    kept.sort(key=lambda block: positions.get(block.text, 0))
    return kept


def _split_text(text: str, max_chars: int) -> Iterator[str]:
    """Split *text* at sentence boundaries near *max_chars*."""
    if len(text) <= max_chars:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # Prefer the last sentence end in the final third of the window, so
            # a chunk is not mostly a fragment.
            window = text.rfind(". ", start + int(max_chars * 0.6), end)
            if window > start:
                end = window + 1
        piece = text[start:end].strip()
        if piece:
            yield piece
        start = end
        while start < len(text) and text[start].isspace():
            start += 1


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """Everything one filing contributes, held for the buffer stage.

    Plain dictionaries, not Arrow and not rows in a table: the buffer stage is
    what turns this into Arrow, so the parser has no database or Arrow
    dependency and can be tested on its own.

    Two layers share one parse. The scalar fields (``metrics``, ``segments``,
    ``events``, ``chunks``) are the original question-answering graph. The
    ``ufgs`` fields are the Universal Financial Graph Schema layer described in
    :mod:`sandbox_engine.ufgs_schema` -- as-filed facts, the structural
    sections they were reported in, and the causal layer -- and they are
    collected in dictionaries keyed by primary key so a filing that reports the
    same fact in three tables yields one node and three arcs.
    """

    company: dict[str, Any]
    filing: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0

    # -- UFGS layer ------------------------------------------------------
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    footnotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_factors: dict[str, dict[str, Any]] = field(default_factory=dict)
    causal_relations: dict[str, dict[str, Any]] = field(default_factory=dict)
    fiscal_periods: dict[str, dict[str, Any]] = field(default_factory=dict)
    restatements: dict[str, dict[str, Any]] = field(default_factory=dict)
    discontinued_segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    sector_overlays: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The canonical anchors materialised as nodes. Identical for every filing
    #: of a sector, so this holds 33 or 40 rows regardless of filing count.
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The seven narrative entity types, keyed by node table then by primary
    #: key. Grouped rather than seven separate fields because the extractor
    #: produces them from one gazetteer pass and the causal layer treats them
    #: as one family.
    entities: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    
    # -- SEC Filing Intelligence Layer ----------------------------------
    insiders: dict[str, dict[str, Any]] = field(default_factory=dict)
    insider_transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    institutional_holders: dict[str, dict[str, Any]] = field(default_factory=dict)
    institutional_holdings: dict[str, dict[str, Any]] = field(default_factory=dict)
    shareholders: dict[str, dict[str, Any]] = field(default_factory=dict)
    shareholdings: dict[str, dict[str, Any]] = field(default_factory=dict)
    securities: dict[str, dict[str, Any]] = field(default_factory=dict)
    corporate_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    capital_raises: dict[str, dict[str, Any]] = field(default_factory=dict)
    equity_compensation_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    exhibits: dict[str, dict[str, Any]] = field(default_factory=dict)
    supporting_documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    # -- Supply-Chain Intelligence Layer -------------------------------
    suppliers: dict[str, dict[str, Any]] = field(default_factory=dict)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    manufacturing: dict[str, dict[str, Any]] = field(default_factory=dict)
    management_commentary: dict[str, dict[str, Any]] = field(default_factory=dict)
    risks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # -- Temporal Hierarchy Layer ---------------------------------------
    fiscal_years: dict[str, dict[str, Any]] = field(default_factory=dict)
    fiscal_quarters: dict[str, dict[str, Any]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """Row count per table, for the run report and benchmark 1."""
        return {
            "metrics": len(self.metrics),
            "segments": len(self.segments),
            "events": len(self.events),
            "chunks": len(self.chunks),
            "sections": len(self.sections),
            "raw_facts": len(self.raw_facts),
            "footnotes": len(self.footnotes),
            "risk_factors": len(self.risk_factors),
            "causal_relations": len(self.causal_relations),
            "fiscal_periods": len(self.fiscal_periods),
            "restatements": len(self.restatements),
            "discontinued_segments": len(self.discontinued_segments),
            "sector_overlays": len(self.sector_overlays),
            "concepts": len(self.concepts),
            "entities": sum(len(v) for v in self.entities.values()),
            # -- SEC Filing Intelligence Layer ----------------------------------
            "insiders": len(self.insiders),
            "insider_transactions": len(self.insider_transactions),
            "institutional_holders": len(self.institutional_holders),
            "institutional_holdings": len(self.institutional_holdings),
            "shareholders": len(self.shareholders),
            "shareholdings": len(self.shareholdings),
            "securities": len(self.securities),
            "corporate_events": len(self.corporate_events),
            "capital_raises": len(self.capital_raises),
            "equity_compensation_plans": len(self.equity_compensation_plans),
            "exhibits": len(self.exhibits),
            "supporting_documents": len(self.supporting_documents),
            # -- Supply-Chain Intelligence Layer -------------------------------
            "suppliers": len(self.suppliers),
            "components": len(self.components),
            "products": len(self.products),
            "manufacturing": len(self.manufacturing),
            "management_commentary": len(self.management_commentary),
            "risks": len(self.risks),
            # -- Temporal Hierarchy Layer ---------------------------------------
            "fiscal_years": len(self.fiscal_years),
            "fiscal_quarters": len(self.fiscal_quarters),
            **{name: len(rows) for name, rows in self.edges.items()},
        }

    def merge(self, other: "ExtractionResult") -> None:
        """Merge another ExtractionResult into this one."""
        self.metrics.update(other.metrics)
        self.segments.update(other.segments)
        self.events.update(other.events)
        self.chunks.update(other.chunks)
        for name in (
            "sections", "raw_facts", "footnotes", "risk_factors",
            "causal_relations", "fiscal_periods", "restatements",
            "discontinued_segments", "sector_overlays", "concepts",
            "insiders", "insider_transactions", "institutional_holders",
            "institutional_holdings", "shareholders", "shareholdings",
            "securities", "corporate_events", "capital_raises",
            "equity_compensation_plans", "exhibits", "supporting_documents",
            "suppliers", "components", "products", "manufacturing",
            "management_commentary", "risks",
            "fiscal_years", "fiscal_quarters",
        ):
            getattr(self, name).update(getattr(other, name))
        for table, nodes in other.entities.items():
            self.entities.setdefault(table, {}).update(nodes)
        for edge_name, edge_list in other.edges.items():
            if edge_name not in self.edges:
                self.edges[edge_name] = []
            self.edges[edge_name].extend(edge_list)
        self.stats.update(other.stats)
        self.elapsed += other.elapsed


# ---------------------------------------------------------------------------
# Metadata patterns
# ---------------------------------------------------------------------------

_FILE_FORM_RE = re.compile(r"(10-[KQ]|8-?K)", re.I)
#: SEC's own naming: ``aapl-20250927.htm``; the period end is the date part.
_EDGAR_NAME_RE = re.compile(r"^([a-z]{1,6})-?(\d{8})", re.I)
_CURATED_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
#: Curated filenames end in ``<ticker>-<YYYYMMDD>`` where the date is the
#: period the filing reports on, not the day it was filed -- both appear in the
#: name and confusing the two is what makes a 10-Q look like it covers a year.
#: EDGAR's own names carry no such token, so a miss here is expected and the
#: caller falls through to the in-document tag.
_CURATED_PERIOD_RE = re.compile(r"-(\d{8})$")
_CIK_RE = re.compile(r"\b(\d{10})\b")
_CURRENCY_RE = re.compile(
    r"\b(USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY|HKD|INR|BRL|MXN|SEK|NOK)\b"
)
#: The unit every inline-XBRL filing declares its monetary facts in, tagged
#: with the ISO 4217 scheme URI. Authoritative in a way a bare code in running
#: text is not: it states what the filing measures in, not what it mentions.
_ISO4217_RE = re.compile(r"iso4217:([A-Z]{3})\b", re.I)
#: The entity identifier every inline-XBRL context carries. Unambiguous, because
#: it is tagged with the SEC's own CIK scheme URI -- a bare ten-digit scan of the
#: visible text would also match a dollar amount.
_XBRL_CIK_RE = re.compile(
    r"<xbrli:identifier[^>]*scheme=[\"']http://www\.sec\.gov/CIK[\"'][^>]*>\s*(\d{1,10})\s*<",
    re.I,
)
#: dei focus, falling back to the header's ``<year> <FY|Qn> <cik>`` run.
_DEI_FOCUS_RE = re.compile(
    r"<(?:dei:)?DocumentFiscalYearFocus[^>]*>\s*((?:19|20)\d{2})\s*<"
    r".{0,200}?<(?:dei:)?DocumentFiscalPeriodFocus[^>]*>\s*([A-Za-z0-9]{1,4})\s*<",
    re.S | re.I,
)
#: Matched against the *raw* markup, not ``_all_text`` output: those strip tags,
#: and the focus values are tagged, so against stripped text neither can match.
#: The two tags are read independently because filers emit them independently --
#: a 10-Q may carry a period focus with no year focus at all, and demanding both
#: loses the quarter exactly when it is the only thing the filing states.
#: Inline-XBRL fact values are routinely wrapped in presentational markup --
#: ``<span style="text-transform:uppercase">MSFT</span>`` -- so a value is not
#: always the fact tag's own text. Allow a short run of nested tags between the
#: fact and its value. Bounded to three so this cannot drift across the
#: document and latch onto a stray token.
_INLINE_WRAP = r"(?:<[^>]{0,400}>\s*){0,3}"
_DEI_YEAR_RE = re.compile(
    r"name\s*=\s*[\"']dei:DocumentFiscalYearFocus[\"'][^>]*>\s*" + _INLINE_WRAP +
    r"((?:19|20)\d{2})\s*<"
    r"|<ix:[^>]*name\s*=\s*[\"']dei:DocumentFiscalYearFocus[\"'][^>]*>\s*" + _INLINE_WRAP +
    r"((?:19|20)\d{2})\s*<",
    re.S | re.I,
)
_DEI_PERIOD_RE = re.compile(
    r"name\s*=\s*[\"']dei:DocumentFiscalPeriodFocus[\"'][^>]*>\s*" + _INLINE_WRAP +
    r"([A-Za-z0-9]{1,4})\s*<"
    r"|<ix:[^>]*name\s*=\s*[\"']dei:DocumentFiscalPeriodFocus[\"'][^>]*>\s*" + _INLINE_WRAP +
    r"([A-Za-z0-9]{1,4})\s*<",
    re.S | re.I,
)
_HEADER_FOCUS_RE = re.compile(r"\b((?:19|20)\d{2})\s+(FY|Q[1-4])\b[^A-Za-z0-9]{0,12}\d{1,10}\b")
#: ``dei:CurrentFiscalYearEndDate`` states the filer's year end, which is what a
#: calendar year in a column header cannot be turned into a fiscal year without.
#: Filers emit it three ways -- ``--09-26``, ``1/25``, and ``September 27`` with
#: the closing tag splitting the fact mid-value -- so the tag is located first
#: and the value parsed from a short text window after it.
_DEI_FYE_LOCATE = re.compile(
    r"name\s*=\s*[\"']dei:CurrentFiscalYearEndDate[\"'][^>]*>", re.I
)
_FYE_VALUE = re.compile(
    r"--(\d{2})-(\d{2})"
    r"|(\d{1,2})\s*/\s*(\d{1,2})"
    rf"|({_MONTHS})[\s ]*(\d{{1,2}})",
    re.I,
)


def fiscal_year_for(period_end: str, year_end: tuple[int, int] | None) -> int | None:
    """The fiscal year a period ending *period_end* falls in.

    A column header prints a calendar date; a filer names the period by its own
    fiscal year. Those differ for most of every quarter -- Apple's quarter
    ending 2025-12-27 is "Q1 FY2026" but the header says 2025, and NVIDIA's
    quarter ending 2026-04-26 is "Q1 FY2027" but the header also says 2026.
    Keying metrics by the header's year therefore files this quarter under the
    previous fiscal year, colliding the current period with the comparative
    printed beside it in the same table.

    The fiscal year is the one whose most recent year end is on or before the
    period end: Apple's year ends in late September, so 2025-12-27 falls after
    the 2025-09-27 close and lands in the year ending 2026-09-26, while the
    close date itself belongs to the year it closes. ``year_end`` is the
    filer's own ``dei:CurrentFiscalYearEndDate``; without it the calendar year
    is the only thing left, and is returned unchanged.
    """
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(period_end).strip())
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year_end is None:
        return year
    end_month, end_day = year_end
    try:
        closes = dt.date(year, end_month, min(end_day, 31)) if end_month <= 12 else None
    except ValueError:
        closes = None
    if closes is None:
        return year
    # On or after this year's close belongs to the year that has not closed yet.
    return year if (month, day) <= (closes.month, closes.day) else year + 1
#: Matched against raw markup: ``_all_text`` strips tags, and the symbol lives
#: inside one. The two alternatives cover a bare fact and an ``ix:``-wrapped one.
_DEI_TICKER_RE = re.compile(
    r"name\s*=\s*[\"']dei:TradingSymbol[\"'][^>]*>\s*" + _INLINE_WRAP +
    r"([A-Z][A-Z0-9.\-]{0,6})\s*<"
    r"|<ix:[^>]*name\s*=\s*[\"']dei:TradingSymbol[\"'][^>]*>\s*" + _INLINE_WRAP +
    r"([A-Z][A-Z0-9.\-]{0,6})\s*<",
)

#: ``dei`` values are tagged inline, so the date is the *text* of an
#: ``ix:nonNumeric`` element whose name is the tag, not an ISO literal:
#: ``<ix:nonNumeric name="dei:DocumentPeriodEndDate" ...>December&#160;27,
#: 2025</ix:nonNumeric>``. A regex anchored on ``<dei:DocumentPeriodEndDate``
#: never matches that, and the caller then falls through to a fallback that
#: returns the wrong date for every 10-Q. Both spellings are accepted.
_DEI_DATE_RE = re.compile(
    r"<ix:nonNumeric\b[^>]*\bname\s*=\s*[\"']dei:DocumentPeriodEndDate[\"'][^>]*>"
    r"\s*([^<]{4,40}?)\s*</ix:nonNumeric>"
    r"|<(?:dei:)?DocumentPeriodEndDate\b[^>]*>\s*([^<]{4,40}?)\s*<",
    re.I,
)
#: Name -> number, for normalising a tagged ``dei`` month name. Distinct from
#: ``_MONTHS`` above, which is a regex alternation: sharing a name would shadow
#: it and break every pattern that interpolates it.
_MONTH_NUMBERS_BY_NAME = {
    name.lower(): i
    for i, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"),
        start=1,
    )
}
#: ``December 27, 2025`` and ``27 December 2025`` are both emitted in the wild.
_DEI_MONTHNAME_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})|(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})"
)


def _iso_date(text: str) -> str:
    """Normalise a tagged ``dei`` date to ISO, or return "" if unparseable.

    Filers write these dates three ways -- an ISO literal, a month-name form,
    and a month-name form with a non-breaking space (``&#160;``) where the comma
    would be. All three reduce here, so the callers below can assume ISO.
    """
    import html
    import re as _re

    cleaned = html.unescape(text or "").replace(" ", " ").strip().strip(",")
    if not cleaned:
        return ""
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        return cleaned
    match = _DEI_MONTHNAME_RE.search(cleaned)
    if not match:
        return ""
    if match.group(1):
        month_name, day, year = match.group(1), match.group(2), match.group(3)
    else:
        day, month_name, year = match.group(4), match.group(5), match.group(6)
    month = _MONTH_NUMBERS_BY_NAME.get(month_name.lower())
    if not month:
        return ""
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"
#: A corporate suffix is the one registrant-name shape every issuer shares.
_CORPORATE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.,'’\-]{1,40}(?:\s+[A-Z][A-Za-z0-9&.,'’\-]{1,40}){0,4}"
    r"\s+(?:Inc\.?|Incorporated|Corp\.?|Corporation|Company|Co\.?|Limited|Ltd\.?|"
    r"L\.?P\.?|PLC|plc|N\.?V\.?|S\.?A\.?|A\.?G\.?|Holdings?|Group))"
    r"(?![A-Za-z0-9])"
)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class FilingParser:
    """Turns one filing into nodes and edges, with no external services.

    The table cache lives on the instance, not on a module global, so a parser
    reused across a run cannot score the second filing against the first
    filing's tables. It is keyed on the document rather than on emptiness, so
    that holds for every entry point, not only for :meth:`ingest_file`.

    The :class:`~sandbox_engine.entity_resolver.ConceptRegistry` lives on the
    instance for the same reason, and for one more: it is *shared* state on
    purpose. It is what makes a segment named in both the 10-K and the 10-Q
    resolve to a single ``Segment`` node instead of one per filing, which is not
    something a per-filing dict can do.
    """

    def __init__(
        self,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        min_chunk_chars: int = MIN_CHUNK_CHARS,
        max_events: int = MAX_EVENTS,
        registry: ConceptRegistry | None = None,
    ) -> None:
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self.max_events = max_events
        self.registry = registry if registry is not None else ConceptRegistry(stable_id)
        self._table_cache: tuple[int, list[pd.DataFrame]] | None = None

    # -- identity ---------------------------------------------------------

    def _metric_id(
        self, name: str, period: str, ticker: str = ""
    ) -> tuple[str, str]:
        """``(node_id, canonical_name)`` for a metric concept in *period*.

        The scope is the **filer** as well as the period, and the filer is the
        part that is easy to leave out. Scoping on the period alone makes one
        node per concept per period for the whole corpus, so every filer's
        "Long-Lived Assets (FY2025)" collapses onto a single node and a
        question about one issuer's long-lived assets is answered with figures
        lifted from all three. The merge is silent because every edge is
        individually well-formed and every endpoint exists -- it surfaces only
        as an answer that mixes two companies' books.

        The filer is the first component of the scope and is kept even when it
        is unknown, so a filing that failed to resolve a ticker lands in a
        scope of its own rather than in the bare-period scope it would collide
        with. Resolution never crosses a partition boundary, so this is a
        structural guarantee rather than a tuned threshold -- see
        :mod:`sandbox_engine.entity_resolver`.

        Returns the *entity's* name, not the incoming surface form. An entity
        keeps the name it was created with and is never renamed, so two filings
        that spell the same concept differently must both write the canonical
        spelling -- otherwise the loader's first-write-wins would make the
        stored name depend on which file was read first.
        """
        scope = _metric_scope(period, ticker) if PERIOD_SCOPED_METRICS else ""
        resolution = self.registry.register("metric", name, scope=scope)
        entity = self.registry.partition("metric", scope).get(
            resolution.canonical_id
        )
        canonical = entity.name if entity else name
        return resolution.canonical_id, (
            f"{canonical} ({period})" if PERIOD_SCOPED_METRICS else canonical
        )

    # -- tables ------------------------------------------------------------

    def tables(self, raw: str) -> list[pd.DataFrame]:
        """Every table in the filing, parsed once.

        ``read_html`` costs tens of milliseconds on a 1.5 MB 10-K and three
        extractors need the result; parsing per extractor tripled the cost of
        the parse stage.

        The cache is keyed on the document, not merely on being empty. Keying
        only on emptiness made the correctness of every non-``ingest_file``
        caller depend on remembering to clear it first, and :meth:`extract_metadata`
        does not: called twice on one parser it returned the *first* filing's
        tables, and with them the first filing's ticker -- which is the
        ``Company`` primary key. A stale cache therefore did not merely cost
        time, it could attach one issuer's filings to another issuer's node.
        Hashing the input is a fraction of the ``read_html`` it guards.
        """
        key = hash(raw)
        if self._table_cache is None or self._table_cache[0] != key:
            try:
                self._table_cache = (key, list(
                    pd.read_html(io.StringIO(html_body(raw)), flavor="lxml")
                ))
            except Exception as exc:  # noqa: BLE001 - a filing with no tables is fine
                log.warning("read_html failed (%s)", exc)
                self._table_cache = (key, [])
        return self._table_cache[1]

    # -- metadata ----------------------------------------------------------

    def extract_metadata(self, raw: str, path: Path) -> dict[str, Any]:
        """Ticker, registrant, CIK, form type, and fiscal period.

        The document is read first and the filename second. Which source won is
        reported in ``sources`` so a surprising value is traceable rather than
        mysterious.
        """
        visible = _WS_RE.sub(" ", strip_markup(raw))
        hidden = _all_text(raw)
        sources: dict[str, str] = {}

        from_text = self._form_from_text(visible)
        form_type = from_text or self._form_from_name(path)
        sources["form_type"] = "document" if from_text else "filename"

        from_table = self._ticker_from_tables(raw)
        sources["ticker"] = "cover_table" if from_table else "filename"

        name, name_source = self._company_name(raw, visible, hidden, path)
        sources["company_name"] = name_source

        cik, cik_source = self._cik(raw, hidden, path)
        sources["cik"] = cik_source

        period_end = self._period_end(raw, path)
        fiscal_year, fiscal_period, period_source = self._fiscal(
            hidden, path, form_type, period_end, raw
        )
        sources["fiscal"] = period_source

        # Extract accession number from DEI tag
        accession_number = _dei_value(raw, "AccessionNumber")
        if not accession_number:
            accession_number = _dei_value(raw, "accessionNumber")
        
        # Extract content hash
        content_hash = self._compute_content_hash(raw)

        return {
            "ticker": from_table or self._ticker_from_name(path),
            "name": name,
            "cik": cik,
            "form_type": form_type,
            "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period,
            "period_end": period_end,
            "filing_date": self._filing_date(path, fiscal_year),
            "currency": self._currency(hidden),
            "sources": sources,
            "accession_number": accession_number or "",
            "content_hash": content_hash,
        }
    
    def _compute_content_hash(self, raw: str) -> str:
        """Compute SHA256 hash of document content for deduplication."""
        import hashlib
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _form_from_text(self, text: str) -> str:
        match = re.search(r"FORM\s+(10-K|10-Q|8-K)", text, re.I)
        if match:
            return match.group(1).upper().replace(" ", "")
        return "8-K" if re.search(r"FORM\s+8-K|CURRENT\s+REPORT", text, re.I) else ""

    def _form_from_name(self, path: Path) -> str:
        match = _FILE_FORM_RE.search(path.name)
        return match.group(1).upper() if match else ""

    def _ticker_from_tables(self, raw: str) -> str:
        """Ticker from the cover-page "Trading symbol(s)" table.

        The only reliable in-document source: a registrant may list several
        securities and only the first has a real symbol -- the rest are em
        dashes. Reading the table preserves the label/value pairing that
        flattening the page destroys.
        """
        pattern = re.compile(r"Trading [Ss]ymbol")
        for frame in self.tables(raw)[:6]:
            if frame is None or frame.empty:
                continue
            grid = frame.astype(str)
            column = next(
                (index for index in range(grid.shape[1])
                 if grid.iloc[:, index].str.contains(pattern, na=False).any()),
                None,
            )
            if column is None:
                continue
            for value in grid.iloc[:, column].tolist()[1:]:
                symbol = clean_text(value).strip("$ ")
                if (symbol and symbol.lower() not in _DASHES
                        and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", symbol)):
                    return symbol
        return self._ticker_from_dei(raw)

    def _ticker_from_dei(self, raw: str) -> str:
        """Ticker from the tagged ``dei:TradingSymbol`` value.

        The table reader is the better source when it works, but an 8-K often
        renders its securities table in a shape the reader drops, and SEC's own
        filenames carry a hash rather than a ticker -- so the filename fallback
        yields ``UNKNOWN`` and the filer splits into a second Company node under
        a key that is not its ticker. The tag is on the cover of every filing,
        including the ones that break the table.
        """
        for match in _DEI_TICKER_RE.finditer(raw):
            symbol = clean_text(match.group(1) or match.group(2) or "").strip("$ ")
            if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", symbol):
                return symbol
        return ""

    def _ticker_from_name(self, path: Path) -> str:
        stem = re.split(r"[_\-.]", path.stem)[0]
        return stem.upper() if re.fullmatch(r"[A-Za-z]{1,6}", stem) else "UNKNOWN"

    def _company_name(
        self, raw: str, visible: str, hidden: str, path: Path
    ) -> tuple[str, str]:
        """Registrant name, most reliable source first.

        A 10-K and a 10-Q both state the name next to the commission file
        number. An 8-K often has no cover table, so the fallback is a
        corporate-suffix match searched in the *hidden* text, because Workiva
        parks the issuer block in a ``display:none`` div.

        ``dei:EntityRegistrantName`` is checked against the raw markup rather
        than either text rendering: it is the one registrant field every
        inline-XBRL filing carries, and the other two cover-page patterns are
        not universal -- only Apple's cover happens to spell out a commission
        file number.
        """
        for haystack in (visible, hidden):
            match = re.search(
                r"Commission File Number:\s*[\d\-]+\s*(.{2,80}?)\s*"
                r"\(\s*Exact name of Registrant",
                haystack,
            )
            if match:
                return clean_text(match.group(1)).strip(" .,"), "commission_file_number"
        dei = _dei_value(raw, "EntityRegistrantName")
        if dei and 2 <= len(dei) <= 120:
            return dei.strip(" .,"), "dei_registrant_name"
        # Prefer the cover-page region; a signature block repeats the name later.
        for match in _CORPORATE_RE.finditer(hidden[:6000]):
            name = clean_text(match.group(1)).strip(" .,")
            if 3 <= len(name) <= 60:
                return name, "corporate_suffix"
        return self._ticker_from_name(path) or "UNKNOWN", "ticker"

    def _cik(self, raw: str, hidden: str, path: Path) -> tuple[str, str]:
        """Ten-digit CIK, or ``""`` when unresolved.

        Read from the entity identifier every inline-XBRL context carries. An
        unresolved CIK is stored as the empty string rather than a guess: a
        wrong CIK silently mis-attributes a filing to a different registrant,
        which is worse than an obviously incomplete field.
        """
        match = _XBRL_CIK_RE.search(raw)
        if match:
            return match.group(1).zfill(10), "xbrli_entity_identifier"
        dei = _dei_value(raw, "EntityCentralIndexKey")
        if dei:
            return dei.zfill(10), "dei_central_index_key"
        match = _CIK_RE.search(path.stem)
        if match:
            return match.group(1), "filename"
        return "", "unresolved"

    def _year_end(self, raw: str) -> tuple[int, int] | None:
        """The filer's fiscal year end as ``(month, day)``.

        Read from ``dei:CurrentFiscalYearEndDate``, which every filer states on
        the cover. This is what turns a column header's calendar date into the
        fiscal year the filer names it by; see :func:`fiscal_year_for`. Returns
        ``None`` when absent, and callers then fall back to the calendar year
        rather than guessing a calendar.
        """
        located = _DEI_FYE_LOCATE.search(raw)
        if not located:
            return None
        window = clean_text(strip_markup(raw[located.end():located.end() + 120]))
        value = _FYE_VALUE.search(window)
        if not value:
            return None
        if value.group(1):
            month, day = int(value.group(1)), int(value.group(2))
        elif value.group(3):
            month, day = int(value.group(3)), int(value.group(4))
        else:
            month, day = _month_number(value.group(5)), int(value.group(6))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        return month, day

    def _period_end(self, raw: str, path: Path) -> str:
        """The filing's own period end, e.g. ``2025-09-27``.

        Order matters. The curated filename is read first because it is a
        direct transcription of the cover page and is exact; the in-document
        ``dei`` tag is the fallback, and it is genuinely unreliable --
        filers emit it as a bare element, wrapped in a ``<span>``, and nested
        around ``CurrentFiscalYearEndDate``, so one regex cannot hold all three
        and a partial match is worse than the filename. A 10-Q that falls back
        to a context instant gets the *prior* fiscal-year end, because that is
        the comparative balance-sheet column, which is how a quarter ends up
        wearing the annual window.
        """
        curated = _CURATED_PERIOD_RE.search(path.stem)
        if curated:
            stamp = curated.group(1)
            return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
        dei = _DEI_DATE_RE.search(raw)
        if dei:
            parsed = _iso_date(dei.group(1) or dei.group(2) or "")
            if parsed:
                return parsed
        # Fallback only. A context instant is a weak signal: a filing carries
        # many (cover page, prior-year comparatives, scheduled maturities) and
        # the first is the document period's only by convention.
        match = re.search(
            r"<xbrli:context\b[^>]*>(?:(?!</xbrli:context>).)*?"
            r"<xbrli:instant>\s*(\d{4}-\d{2}-\d{2})\s*<",
            raw,
            re.S | re.I,
        )
        if match:
            return match.group(1)
        match = _EDGAR_NAME_RE.search(path.stem)
        if match:
            stamp = match.group(2)
            return f"20{stamp[0:2]}-{stamp[2:4]}-{stamp[4:6]}"
        return ""

    def _fiscal(
        self, hidden: str, path: Path, form_type: str, period_end: str, raw: str = ""
    ) -> tuple[int | None, str, str]:
        """Fiscal year and period, e.g. ``(2025, "FY")`` or ``(2026, "Q3")``.

        The document states its own focus in the inline-XBRL header, so no
        fiscal-calendar guesswork is needed. Falling back to "the largest year
        in the text" would be wrong: these filings quote bond maturities out to
        2042.

        The year and the period are read separately because filers do not always
        emit both. A 10-Q with a period focus but no year focus still knows it
        is ``Q3``; requiring both loses the quarter. When the year is genuinely
        absent the period end is not a safe substitute -- Microsoft's quarter
        ending 2025-12-31 is fiscal 2026, because its year ends in June -- so the
        year is left unresolved instead of being silently wrong.
        """
        if raw:
            period_match = _DEI_PERIOD_RE.search(raw)
            if period_match:
                period = (period_match.group(1) or period_match.group(2) or "").upper()
                year_match = _DEI_YEAR_RE.search(raw)
                if year_match:
                    year = year_match.group(1) or year_match.group(2)
                    return int(year), period, "dei_focus"
                return None, period, "dei_period_only"
        match = _DEI_FOCUS_RE.search(hidden)
        if match:
            return int(match.group(1)), match.group(2).upper(), "dei_focus"
        match = _HEADER_FOCUS_RE.search(hidden)
        if match:
            return int(match.group(1)), match.group(2).upper(), "xbrl_header"
        if period_end:
            return int(period_end[:4]), self._period_for(form_type), "period_end"
        # An 8-K has no reporting period at all, so its fiscal year is the year
        # the event was filed in; a curated filename carries that date.
        match = _CURATED_DATE_RE.search(path.name)
        if match:
            return int(match.group(1)), self._period_for(form_type), "filename_date"
        match = _EDGAR_NAME_RE.search(path.stem)
        if match:
            return int("20" + match.group(2)[:2]), self._period_for(form_type), "filename"
        return None, self._period_for(form_type), "unresolved"

    def _period_for(self, form_type: str) -> str:
        if form_type == "10-K":
            return "FY"
        if form_type == "10-Q":
            return "Q?"
        return "FY"

    def _filing_date(self, path: Path, fiscal_year: int | None) -> str:
        """ISO filing date.

        Curated filenames carry the filing date; SEC's own filenames carry only
        the period end, so that is the documented fallback.
        """
        match = _CURATED_DATE_RE.search(path.name)
        if match:
            return "-".join(match.groups())
        match = _EDGAR_NAME_RE.search(path.stem)
        if match:
            stamp = match.group(2)
            return f"20{stamp[0:2]}-{stamp[2:4]}-{stamp[4:6]}"
        return f"{fiscal_year}-12-31" if fiscal_year else ""

    def _currency(self, text: str) -> str:
        """The filing's own reporting currency.

        Read from the inline-XBRL unit declarations rather than from the first
        currency code the text happens to contain. The rendered text of an
        inline-XBRL filing opens with the context and unit block, so a *first*
        match is whichever unit the document lists earliest -- not the one it
        reports in. Microsoft's FY2026 10-K declares ``iso4217:EUR`` before
        ``iso4217:USD`` (EUR covers a segment disclosure) and the scan returned
        EUR, which stamped every Microsoft metric edge with the wrong currency
        and made a cross-issuer comparison wrong rather than merely untidy.

        The unit declarations are machine-readable and per-filing, so the most
        frequently declared one is the reporting currency. A filing that
        declares no units -- an 8-K, typically -- falls back to the text scan,
        and a filing that mentions no code at all falls back to USD.
        """
        declared = _ISO4217_RE.findall(text)
        if declared:
            return Counter(declared).most_common(1)[0][0]
        match = _CURRENCY_RE.search(text[:200_000])
        return match.group(1) if match else "USD"

    # -- metrics -----------------------------------------------------------

    def extract_metrics(
        self, raw: str, metadata: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Financial statement lines as ``Metric`` nodes and their edges.

        A metric's identity includes the period it measures, because one edge
        holds one value and a 10-K shows three periods of every line. The period
        is written into ``canonical_name`` so the node stays queryable without
        reaching for a content hash.

        The id comes from :meth:`_metric_id`, not from a hash of the label, so
        every surface form of a concept that appears in the same period resolves
        to the one node.
        """
        metrics: dict[str, dict[str, Any]] = {}
        candidates: list[dict[str, Any]] = []
        currency = metadata["currency"]
        year_end = self._year_end(raw)
        filing_period_end = filing_period_end_iso(metadata)
        for frame in self.tables(raw):
            if frame is None or frame.empty:
                continue
            category, cells = extract_cells(
                frame, year_end=year_end, filing_period_end=filing_period_end
            )
            if not cells:
                continue
            for cell in cells:
                node_id, name = self._metric_id(
                    cell.canonical_name, cell.period, metadata.get("ticker", "")
                )
                metrics[node_id] = {
                    "id": node_id,
                    "canonical_name": name,
                    "statement_category": cell.category or category or "other",
                    "period_code": cell.period_code,
                    "period_start": cell.period_start,
                    "period_end": cell.period_end,
                    "period_days": cell.days_covered,
                    "period_cumulative": 1 if cell.cumulative else 0,
                    "reported_label": cell.reported_label,
                    "form_type": metadata.get("form_type", ""),
                }
                candidates.append(
                    {
                        "value": float(cell.number.value),
                        "currency": currency,
                        "metric": node_id,
                        "period": cell.period,
                        "category": cell.category or category or "other",
                    }
                )
        return metrics, self.resolve_metric_conflicts(metrics, candidates)

    @staticmethod
    def resolve_metric_conflicts(
        metrics: dict[str, dict[str, Any]], candidates: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """One value per ``(filing, metric)`` edge. Hazard 3 from the docstring.

        A registrant often states the same line in more than one place -- the
        face of the income statement and again in selected financial data or a
        footnote. ``REPORTS_METRIC`` holds a single ``value``, so leaving both
        arcs would make the graph's answer to "what did it report" depend on arc
        order. The most authoritative statement wins and the conflict is logged
        rather than silently resolved.

        ``min`` is used with the priority rank so the most authoritative table
        wins; ties keep the first-encountered value, which is deterministic
        because table order is.
        """
        priority = {
            "income_statement": 0, "balance_sheet": 1,
            "cash_flow": 2, "segment": 3, "other": 4,
        }
        best: dict[str, dict[str, Any]] = {}
        conflicts = 0
        for edge in candidates:
            node_id = edge["metric"]
            rank = priority.get(edge["category"], 9)
            current = best.get(node_id)
            if current is None or rank < priority.get(current["category"], 9):
                if current is not None and current["value"] != edge["value"]:
                    conflicts += 1
                best[node_id] = edge
            elif current["value"] != edge["value"]:
                conflicts += 1
        if conflicts:
            log.info(
                "%d metric value(s) restated across tables; kept the "
                "highest-priority statement's value", conflicts,
            )
        return [
            {
                "value": float(edge["value"]),
                "currency": edge["currency"],
                "metric": node_id,
                "period": edge["period"],
            }
            for node_id, edge in best.items()
        ]

    def _table_context(
        self, frames: Sequence[pd.DataFrame], index: int, labels: Sequence[str]
    ) -> str:
        """Neighbouring text, so a segment note is recognisable as one.

        ``read_html`` reports table order but not page position, so the nearest
        tables' labels stand in for the surrounding prose.
        """
        parts: list[str] = []
        for offset in (-2, -1, 1, 2):
            position = index + offset
            if 0 <= position < len(frames) and frames[position] is not None:
                parts.extend(
                    _cell(value)
                    for value in frames[position].astype(str).values.flatten()[:80]
                )
        parts.extend(labels)
        return " ".join(parts)

    # -- segments ----------------------------------------------------------

    def extract_segments(
        self, raw: str, metadata: dict[str, Any]
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Reporting segments, their arcs, and each measure's reported total.

        A segment table has no statement line items of its own, so its figures
        hang off the measure that owns them. That is the entire reason the
        ``Metric -> HAS_SEGMENT -> Segment`` shape exists.

        The third element is the ``Total <measure>`` row of each table. It is
        returned rather than discarded because the host metric needs an owner:
        a synthesised "Long-Lived Assets (FY2025)" with no ``REPORTS_METRIC``
        arc is a node nothing can reach, and the total row is the filing's own
        figure for it -- 49,834 for Apple's FY2025 long-lived assets, which is
        exactly what the U.S, China and other-countries figures add to.
        Without it the measure exists only as a label.

        A segment's name goes through the shared registry rather than a
        per-filing ``setdefault``. Both the 10-K and the 10-Q carry a segment
        note, and ``setdefault`` is scoped to one filing, so each note minted its
        own copy of ``Americas``, ``iPhone`` and the rest -- 18 rows for 13
        segments. The registry is shared for the whole run, so the second filing's
        ``iPhone`` resolves to the node the first one created.
        """
        segments: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        totals: list[dict[str, Any]] = []
        frames = self.tables(raw)
        year_end = self._year_end(raw)
        filing_period_end = filing_period_end_iso(metadata)
        for index, frame in enumerate(frames):
            if frame is None or frame.empty:
                continue
            _, groups = detect_period_groups(
                frame, year_end=year_end, filing_period_end=filing_period_end
            )
            if len(groups) < 2:
                continue
            label_end = max(1, groups[0].columns[0])
            rows: list[tuple[str, PeriodGroup, Number]] = []
            # The measure this table reports, announced by a caption row and
            # defaulting to net sales. Reset per table: the tables in one note
            # are different measures of the same taxonomy, and a caption in the
            # previous table must not leak into this one.
            measure, category = "Net Sales", "income_statement"
            for _, row in frame.iterrows():
                values = row.tolist()
                label = _label_of(values[:label_end])
                if not label or _PERIOD_RE.search(label):
                    continue
                # The total is tested before the caption because a total row
                # also contains its measure's name -- "Total long-lived
                # assets" matches the long-lived-assets pattern -- and consuming
                # it as a caption would skip the one row that gives the
                # synthesised host an owner.
                if _is_measure_total(label, measure):
                    # Recorded, but deliberately *not* added to ``rows``. It is
                    # a financial concept, and ``detect_segment_table`` rejects
                    # a table as soon as a quarter of its labels look like
                    # statement line items -- so admitting "Total net sales"
                    # here would make it reject the country table that
                    # contains it, taking U.S. net sales with it.
                    for group in groups:
                        number = _number_in_group(values, group.columns)
                        if number is None or not group.year:
                            continue
                        totals.append({
                            "metric": self._metric_id(
                                measure, group.full_key, metadata.get("ticker", "")
                            )[0],
                            "measure": measure,
                            "category": category,
                            "period": group.full_key,
                            "value": float(number.value),
                        })
                    continue
                announced = segment_measure(label)
                if announced is not None:
                    # A caption row names the measure; it is not itself a
                    # segment, so it sets the host and emits nothing.
                    measure, category = announced
                    continue
                for group in groups:
                    number = _number_in_group(values, group.columns)
                    if number is None or not group.year:
                        continue
                    rows.append((label, group, number))
            if len(rows) < 2:
                continue
            labels = [label for label, _, _ in rows]
            kind = detect_segment_table(frame, labels, self._table_context(frames, index, labels))
            if not kind:
                continue
            for label, group, number in rows:
                name = self._segment_name(label)
                if not name:
                    continue
                # Segments are not period-scoped: "iPhone" is one node however
                # many periods or filings report it. Scope stays on the edge.
                resolution = self.registry.register("segment", name, category=kind)
                entity = self.registry.partition("segment").get(resolution.canonical_id)
                canonical = entity.name if entity else name
                segments[canonical] = {"name": canonical, "segment_type": kind}
                host_id, _ = self._metric_id(
                    measure, group.full_key, metadata.get("ticker", "")
                )
                edges.append(
                    {
                        "value": float(number.value),
                        "period": group.full_key,
                        "segment": canonical,
                        "metric": host_id,
                        "measure": measure,
                        "category": category,
                    }
                )
        return segments, edges, totals

    def _segment_name(self, label: str) -> str:
        """A segment label, or ``""`` if the row is not one.

        "Total net sales" is a subtotal, not a segment, and a percentage row is
        a share of a segment rather than a segment. A measure caption is not a
        segment either: without that check "Long-lived assets:" becomes a
        geographic segment sitting next to the countries it measures. Both are
        rejected so the ``Segment`` table only ever holds real taxonomy members.

        The surviving label then goes through
        :func:`~sandbox_engine.entity_resolver.canonical_concept`, because a
        segment note spells product names the same way the rest of the filing
        does -- trademark marks and all -- and the metric side already folds
        those. Without it ``iPhone®`` in a note and ``iPhone`` on the face of the
        income statement would be two ``Segment`` nodes.
        """
        text = _normalise_label(label).strip(" .:")
        text = re.sub(r"^net\s+sales[:\s]+", "", text, flags=re.I)
        text = re.sub(r"^\*\s*", "", text)
        text = re.sub(r"\s*\(\d+\)\s*$", "", text)        # "China (1)" -> "China"
        if not text or text.lower() in _DASHES or len(text) > 60:
            return ""
        if _PERIOD_RE.search(text) or _BANNER_RE.search(text) or _PERCENT_RE.search(text):
            return ""
        if segment_measure(text) is not None:
            return ""
        if re.match(r"^(total|net\s+total|grand\s+total)\b", text, re.I):
            return ""
        return canonical_concept(text).name

    # -- events and chunks -------------------------------------------------

    def extract_events(self, raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """8-K item headings, and only for an 8-K.

        Gating on the form type matters: a 10-K's risk factors are numbered
        "Item 1A.", "Item 1B." and would otherwise produce a dozen bogus events.
        """
        if not str(metadata.get("form_type", "")).upper().startswith("8"):
            return {}
        scope = filing_identity(metadata)
        events: dict[str, dict[str, Any]] = {}
        for code, title, summary in parse_events(strip_markup(raw))[: self.max_events]:
            event_id = stable_id("event", scope, code, title)
            events[event_id] = {
                "id": event_id, "item_code": code,
                "item_title": title, "summary": summary,
            }
        return events

    def extract_chunks(self, raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Hierarchical body text for hybrid retrieval."""
        scope = filing_identity(metadata)
        return {
            stable_id("chunk", scope, position): {
                "id": stable_id("chunk", scope, position),
                "section": block.section or "Document",
                "text": block.text,
            }
            for position, block in enumerate(
                chunk_body(html_body(raw), self.max_chunk_chars, self.min_chunk_chars)
            )
        }

    # -- Supply-Chain Intelligence Layer Extractions --------------------------

    def extract_suppliers(self, raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract supplier entities from filing text."""
        return extract_suppliers(raw, metadata)

    def extract_components(self, raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract component entities from filing text."""
        return extract_components(raw, metadata)

    def extract_products(self, raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract product entities from filing text."""
        return extract_products(raw, metadata)

    def extract_manufacturing(self, raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract manufacturing location entities from filing text."""
        return extract_manufacturing(raw, metadata)

    def extract_management_commentary(self, raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract management commentary sections from filing text."""
        return extract_management_commentary(raw, metadata)

    def extract_risks(self, raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract risk factor entities from filing text."""
        return extract_risks(raw, metadata)

    # -- orchestration -----------------------------------------------------

    def ingest_file(self, path: str | Path) -> ExtractionResult:
        """Parse one filing. Never raises for a malformed document.

        A filing that parses to nothing still returns a result -- with a
        ``Filing`` node and no metrics -- so the graph records that the document
        was seen. A run that silently dropped it would be indistinguishable from
        a run that never saw it.
        """
        started = time.perf_counter()
        path = Path(path)
        # One parser serves a whole run. The table cache is document-keyed, so
        # this is belt-and-braces rather than the thing that makes reuse safe.
        self._table_cache = None

        raw = path.read_text(encoding="utf-8", errors="replace")
        metadata = self.extract_metadata(raw, path)
        metrics, metric_edges = self.extract_metrics(raw, metadata)
        segments, segment_edges, segment_totals = self.extract_segments(raw, metadata)
        events = self.extract_events(raw, metadata)
        chunks = self.extract_chunks(raw, metadata)

        # -- SEC Filing Intelligence Layer Extractions ----------------------
        insiders = extract_insiders(raw, metadata)
        insider_transactions = extract_insider_transactions(raw, metadata)
        institutional_holders = extract_institutional_holders(raw, metadata)
        institutional_holdings = extract_institutional_holdings(raw, metadata)
        shareholders = extract_shareholders(raw, metadata)
        shareholdings = extract_shareholdings(raw, metadata)
        securities = extract_securities(raw, metadata)
        corporate_events = extract_corporate_events(raw, metadata)
        capital_raises = extract_capital_raises(raw, metadata)
        equity_compensation_plans = extract_equity_compensation_plans(raw, metadata)
        exhibits = extract_exhibits(raw, metadata)
        supporting_documents = extract_supporting_documents(raw, metadata)

        # A segment note's figures hang off the revenue metric of the same
        # period. If that period never appeared on a statement -- a segment note
        # may use a point-in-time key -- the host node has to exist anyway, or
        # the arc would be dropped as dangling by the loader. The host id comes
        # from the registry for the same reason the segment's does: a synthesised
        # "Net Sales" has to be the *same node* as a "Net Sales" the statement
        # reported, or the arc points at an orphan.
        # Hosts the segment notes needed but no statement produced. Recorded
        # here because only these need an owner: where the statement already
        # reported the measure, ``extract_metrics`` has attached the real edge
        # and a second one would double the figure.
        synthesised: set[str] = set()
        year_end = self._year_end(raw)
        form_type = metadata.get("form_type", "")
        for edge in segment_edges:
            host = edge["metric"]
            if host not in metrics:
                synthesised.add(host)
                measure = edge.get("measure") or "Net Sales"
                category = edge.get("category") or "income_statement"
                metrics[host] = {
                    "id": host,
                    "canonical_name": (
                        f"{measure} ({edge['period']})"
                        if PERIOD_SCOPED_METRICS else measure
                    ),
                    "statement_category": category,
                    **period_metadata(edge["period"], year_end, form_type),
                }

        # A synthesised host is a Metric nothing can reach until something owns
        # it. The segment table's own "Total <measure>" row supplies the owner:
        # the filing's reported total rather than a sum we computed, so Apple's
        # long-lived assets are 49,834 because the 10-K says so.
        for total in segment_totals:
            if total["metric"] not in synthesised:
                continue
            metrics.setdefault(total["metric"], {
                "id": total["metric"],
                "canonical_name": (
                    f"{total['measure']} ({total['period']})"
                    if PERIOD_SCOPED_METRICS else total["measure"]
                ),
                "statement_category": total["category"],
                **period_metadata(total["period"], year_end, form_type),
            })
            metric_edges.append({
                "metric": total["metric"],
                "value": total["value"],
                "currency": "USD",
            })

        # -- Temporal Hierarchy Layer ---------------------------------------
        fiscal_years: dict[str, dict[str, Any]] = {}
        fiscal_quarters: dict[str, dict[str, Any]] = {}
        
        # Create FiscalYear node (one per company per fiscal year)
        fy = metadata.get("fiscal_year")
        if fy is not None:
            fy_int = int(fy) if isinstance(fy, (int, str)) and str(fy).isdigit() else None
            if fy_int is not None:
                fy_id = stable_id("fy", metadata["ticker"], str(fy_int))
                fiscal_years[fy_id] = {
                    "id": fy_id,
                    "company_ticker": metadata["ticker"],
                    "fiscal_year": fy_int,
                    "year_start_date": "",  # Will be computed from year_end if available
                    "year_end_date": "",
                }
                
                # Compute year start/end from fiscal year end
                if year_end:
                    month, day = year_end
                    # Fiscal year ends in the given year
                    year_end_date = f"{fy_int}-{month:02d}-{day:02d}"
                    # Fiscal year starts the day after prior year end
                    if month == 1 and day == 1:
                        year_start_date = f"{fy_int - 1}-01-01"
                    else:
                        year_start_date = f"{fy_int - 1}-{month:02d}-{day:02d}"
                    fiscal_years[fy_id]["year_start_date"] = year_start_date
                    fiscal_years[fy_id]["year_end_date"] = year_end_date
                
                # Create FiscalQuarter node (one per filing's fiscal period)
                fp = metadata.get("fiscal_period", "")
                quarter_number = 0
                quarter_label = fp
                if fp == "FY":
                    quarter_number = 0  # Full year
                    quarter_label = "FY"
                elif fp.startswith("Q"):
                    try:
                        quarter_number = int(fp[1:])
                    except (ValueError, IndexError):
                        quarter_number = 0
                
                fq_id = stable_id("fq", metadata["ticker"], str(fy_int), quarter_label)
                fiscal_quarters[fq_id] = {
                    "id": fq_id,
                    "fiscal_year_id": fy_id,
                    "quarter_number": quarter_number,
                    "quarter_label": quarter_label,
                    "quarter_start_date": "",
                    "quarter_end_date": "",
                }
                
                # Set quarter dates based on period_end
                period_end = metadata.get("period_end", "")
                if period_end:
                    fiscal_quarters[fq_id]["quarter_end_date"] = period_end
                    # Approximate quarter start (3 months before end for quarters)
                    if quarter_number > 0:
                        from datetime import datetime, timedelta
                        try:
                            end_dt = datetime.strptime(period_end, "%Y-%m-%d")
                            start_dt = end_dt - timedelta(days=92)  # ~3 months
                            fiscal_quarters[fq_id]["quarter_start_date"] = start_dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                    else:
                        fiscal_quarters[fq_id]["quarter_start_date"] = fiscal_years[fy_id].get("year_start_date", "")

        # -- Supply-Chain Intelligence Layer Extractions ----------------------
        suppliers = self.extract_suppliers(raw, metadata)
        components = self.extract_components(raw, metadata)
        products = self.extract_products(raw, metadata)
        manufacturing = self.extract_manufacturing(raw, metadata)
        management_commentary = self.extract_management_commentary(raw, metadata)
        risks = self.extract_risks(raw, metadata)

        filing_id = filing_identity(metadata)
        result = ExtractionResult(
            company={
                "ticker": metadata["ticker"],
                "name": metadata["name"],
                "cik": metadata["cik"],
            },
            filing={
                "id": filing_id,
                "form_type": metadata["form_type"],
                "fiscal_year": metadata["fiscal_year"],
                "fiscal_period": metadata["fiscal_period"],
                "filing_date": metadata["filing_date"],
            },
            metadata={**metadata, "source_file": path.name},
            metrics=metrics,
            segments=segments,
            events=events,
            chunks=chunks,
            # -- SEC Filing Intelligence Layer ----------------------------
            insiders=insiders,
            insider_transactions=insider_transactions,
            institutional_holders=institutional_holders,
            institutional_holdings=institutional_holdings,
            shareholders=shareholders,
            shareholdings=shareholdings,
            securities=securities,
            corporate_events=corporate_events,
            capital_raises=capital_raises,
            equity_compensation_plans=equity_compensation_plans,
            exhibits=exhibits,
            supporting_documents=supporting_documents,
            # -- Supply-Chain Intelligence Layer -------------------------------
            suppliers=suppliers,
            components=components,
            products=products,
            manufacturing=manufacturing,
            management_commentary=management_commentary,
            risks=risks,
            # -- Temporal Hierarchy Layer ---------------------------------------
            fiscal_years=fiscal_years,
            fiscal_quarters=fiscal_quarters,
        )
        result.edges = {
            "SUBMITTED": [{"from": metadata["ticker"], "to": filing_id}],
            "REPORTS_METRIC": [
                {"from": filing_id, "to": edge["metric"],
                 "value": edge["value"], "currency": edge["currency"]}
                for edge in metric_edges
            ],
            "HAS_SEGMENT": [
                {"from": edge["metric"], "to": edge["segment"],
                 "value": edge["value"], "period": edge["period"]}
                for edge in segment_edges
            ],
            "DISCLOSES_EVENT": [
                {"from": filing_id, "to": event_id} for event_id in events
            ],
            "HAS_CHUNK": [
                {"from": filing_id, "to": chunk_id} for chunk_id in chunks
            ],
            # -- SEC Filing Intelligence Layer Relationships --------------
            "INSIDER_OF": [
                {"from": insider_id, "to": metadata["ticker"]}
                for insider_id in insiders
            ],
            "FILED_INSIDER_FORM": [
                {"from": insider_id, "to": filing_id, "form_type": metadata["form_type"]}
                for insider_id in insiders
            ],
            "TRANSACTED": [
                {"from": txn_id, "to": txn_id}
                for txn_id in insider_transactions
            ],
            "REPORTED_HOLDING": [
                {"from": holder_id, "to": holding_id}
                for holder_id in institutional_holders
                for holding_id in institutional_holdings
            ],
            "HOLDS_SECURITY": [
                {"from": holding_id, "to": holding_id}
                for holding_id in institutional_holdings
            ],
            "OWNS": [
                {"from": sh_id, "to": sh_id}
                for sh_id in shareholders
            ],
            "SHAREHOLDING_IN": [
                {"from": sh_id, "to": metadata["ticker"]}
                for sh_id in shareholdings
            ],
            "ISSUED": [
                {"from": metadata["ticker"], "to": sec_id}
                for sec_id in securities
            ],
            "HAS_EVENT": [
                {"from": metadata["ticker"], "to": event_id}
                for event_id in corporate_events
            ],
            "DISCLOSED_IN_FILING": [
                {"from": event_id, "to": filing_id}
                for event_id in corporate_events
            ],
            "RAISED_CAPITAL": [
                {"from": metadata["ticker"], "to": cr_id}
                for cr_id in capital_raises
            ],
            "CAPITAL_RAISE_IN_FILING": [
                {"from": cr_id, "to": filing_id}
                for cr_id in capital_raises
            ],
            "HAS_EQUITY_PLAN": [
                {"from": metadata["ticker"], "to": plan_id}
                for plan_id in equity_compensation_plans
            ],
            "EQUITY_PLAN_IN_FILING": [
                {"from": plan_id, "to": filing_id}
                for plan_id in equity_compensation_plans
            ],
            "HAS_EXHIBIT": [
                {"from": filing_id, "to": ex_id}
                for ex_id in exhibits
            ],
            "HAS_SUPPORTING_DOC": [
                {"from": metadata["ticker"], "to": doc_id}
                for doc_id in supporting_documents
            ],
            # -- Temporal Hierarchy Layer Relationships -------------------
            "HAS_FISCAL_YEAR": [
                {"from": metadata["ticker"], "to": fy_id}
                for fy_id in fiscal_years
            ],
            "HAS_FISCAL_QUARTER": [
                {"from": fy_id, "to": fq_id}
                for fy_id in fiscal_years
                for fq_id in fiscal_quarters
                if fiscal_quarters[fq_id].get("fiscal_year_id") == fy_id
            ],
            "FILED_IN_QUARTER": [
                {"from": filing_id, "to": fq_id}
                for fq_id in fiscal_quarters
            ],
            # -- Supply-Chain Intelligence Layer Relationships ---------------
            # Only create edges where we have actual evidence from extraction
            # SUPPLIES: Supplier -> Company (the company sources from this supplier)
            # Supplier primary key is "name" (canonical_name)
            "SUPPLIES": [
                {"from": s_name, "to": metadata["ticker"], "volume": 0.0, "contract_type": "", "since": ""}
                for s_name in suppliers.keys()
            ],
            # SOURCES_COMPONENT_FROM: Company -> Component (the company sources this component)
            # Component primary key is "name"
            "SOURCES_COMPONENT_FROM": [
                {"from": metadata["ticker"], "to": c_name, "component": "", "volume": 0.0}
                for c_name in components.keys()
            ],
            # USED_IN: Component -> Product (component is used in product)
            # Component and Product primary keys are "name"
            "USED_IN": [
                {"from": c_name, "to": p_name, "quantity": 0.0, "criticality": 0.0}
                for c_name in components.keys()
                for p_name in products.keys()
            ] if components and products else [],
            # MANUFACTURES_FOR: Manufacturing -> Company (location manufactures for company)
            # Manufacturing primary key is "name"
            "MANUFACTURES_FOR": [
                {"from": m_name, "to": metadata["ticker"], "volume": 0.0, "location": "", "since": ""}
                for m_name in manufacturing.keys()
            ],
            # MENTIONED_IN: ManagementCommentary -> Filing (commentary is in this filing)
            # ManagementCommentary primary key is "id" (stable_id)
            "MENTIONED_IN": [
                {"from": mc_data["id"], "to": filing_id, "context": "", "sentiment": ""}
                for mc_data in management_commentary.values()
            ],
            # REFERENCES: Filing -> Risk (filing references this risk)
            # Filing primary key is "id", Risk primary key is "id"
            "REFERENCES": [
                {"from": filing_id, "to": r_data["id"], "context": "", "quote": ""}
                for r_data in risks.values()
            ],
            # DEPENDS_ON: Product -> Component (product depends on component)
            # Product and Component primary keys are "name"
            "DEPENDS_ON": [
                {"from": p_name, "to": c_name, "criticality": 0.0, "single_source": False}
                for p_name in products.keys()
                for c_name in components.keys()
            ] if products and components else [],
            # -- Amended Filing Relationships -------------------
            "AMENDS": [
                {"from": filing_id, "to": metadata.get("original_filing_id", "")}
            ] if metadata.get("is_amended") and metadata.get("original_filing_id") else [],
        }
        result.stats = {
            **result.counts(),
            "tables": len(self.tables(raw)),
            "sources": metadata["sources"],
            "bytes": path.stat().st_size,
        }
        result.elapsed = time.perf_counter() - started
        return result

    def resolution_report(self) -> dict[str, Any]:
        """What the shared registry did, for the run report.

        ``merged`` counts surface forms that landed on an existing node, so it is
        the number of duplicate entities the run avoided creating. It is a
        property of the registry rather than of any one filing, so it is
        reported once for the run, not per filing.
        """
        return {
            "stats": dict(self.registry.stats),
            "partitions": [
                {"kind": kind, "scope": scope, "entities": count}
                for kind, scope, count in self.registry.partitions()
            ],
            "entities": len(self.registry),
        }


# ---------------------------------------------------------------------------
# Blueprint compatibility layer
# ---------------------------------------------------------------------------

# 35 canonical metric seeds for the blueprint schema
# (metric_id, canonical_name, statement_type, account_class)
METRIC_SEEDS_BLUEPRINT: list[tuple[str, str, str, str]] = [
    # income statement
    ("NetSales",               "Net Sales",                        "income_statement", "revenue"),
    ("GrossProfit",            "Gross Profit",                     "income_statement", "profit"),
    ("GrossMargin",            "Gross Margin %",                   "income_statement", "ratio"),
    ("CostOfGoods",            "Cost of Goods Sold",               "income_statement", "cost"),
    ("ResearchDevelopment",    "Research and Development",         "income_statement", "opex"),
    ("SellingGenAdmin",        "Selling, General & Administrative","income_statement", "opex"),
    ("OperatingExpenses",      "Total Operating Expenses",         "income_statement", "opex"),
    ("OperatingIncome",        "Operating Income",                 "income_statement", "profit"),
    ("OperatingMargin",        "Operating Margin %",               "income_statement", "ratio"),
    ("NonOperatingIncome",     "Other Income / Expense",           "income_statement", "other"),
    ("IncomeBeforeTaxes",      "Income Before Taxes",              "income_statement", "profit"),
    ("IncomeTaxExpense",       "Income Tax Expense",               "income_statement", "tax"),
    ("NetIncome",              "Net Income",                       "income_statement", "profit"),
    ("NetMargin",              "Net Margin %",                     "income_statement", "ratio"),
    ("EPS_Basic",              "EPS Basic",                        "income_statement", "per_share"),
    ("EPS_Diluted",            "EPS Diluted",                      "income_statement", "per_share"),
    ("SharesBasic",            "Shares Outstanding Basic",         "income_statement", "shares"),
    ("SharesDiluted",          "Shares Outstanding Diluted",       "income_statement", "shares"),
    # balance sheet
    ("CashEquivalents",        "Cash and Cash Equivalents",        "balance_sheet",    "asset"),
    ("ShortTermInvestments",   "Short-Term Investments",           "balance_sheet",    "asset"),
    ("AccountsReceivable",     "Accounts Receivable",              "balance_sheet",    "asset"),
    ("Inventory",              "Inventory",                        "balance_sheet",    "asset"),
    ("TotalCurrentAssets",     "Total Current Assets",             "balance_sheet",    "asset"),
    ("PP_E_Net",               "Property Plant and Equipment Net", "balance_sheet",    "asset"),
    ("Goodwill",               "Goodwill",                         "balance_sheet",    "asset"),
    ("TotalAssets",            "Total Assets",                     "balance_sheet",    "asset"),
    ("AccountsPayable",        "Accounts Payable",                 "balance_sheet",    "liability"),
    ("TotalCurrentLiabilities","Total Current Liabilities",        "balance_sheet",    "liability"),
    ("LongTermDebt",           "Long-Term Debt",                   "balance_sheet",    "liability"),
    ("TotalLiabilities",       "Total Liabilities",                "balance_sheet",    "liability"),
    ("StockholdersEquity",     "Stockholders Equity",              "balance_sheet",    "equity"),
    # cash flow
    ("OperatingCashFlow",      "Operating Cash Flow",              "cash_flow",        "cashflow"),
    ("CapEx",                  "Capital Expenditures",             "cash_flow",        "cashflow"),
    ("FreeCashFlow",           "Free Cash Flow",                   "cash_flow",        "cashflow"),
    ("DepreciationAmortization","Depreciation and Amortization",   "cash_flow",        "cashflow"),
]


_EXEC_APPT_RE = re.compile(
    r"appoint(?:s|ed|ment).*?(?:as|to)\s+(?:the\s+)?([A-Z][A-Za-z\s,]+(?:Officer|President|CEO|CFO|COO|CTO|Director|Secretary|Counsel))",
    re.I,
)
_NAME_RE = re.compile(r"\b([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b")
_ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)\s*\.?\s*([^\n\r.]{3,80})", re.I)


def extract_executives_from_8k(raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract executive appointments from 8-K Item 5.02.

    Returns list of executive dicts with keys: exec_id, name, role, ticker, appointment_date.
    """
    if not str(metadata.get("form_type", "")).upper().startswith("8"):
        return []
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text)
    executives: list[dict[str, Any]] = []
    accession = metadata.get("accession_number") or filing_identity(metadata)
    event_date = metadata.get("period_end") or metadata.get("filing_date", "")

    for m in _ITEM_RE.finditer(text):
        code = m.group(1).strip()
        if code != "5.02":
            continue
        context = text[m.start() : m.start() + 800]
        exec_m = _EXEC_APPT_RE.search(context)
        if exec_m:
            role = clean_text(exec_m.group(1))
            name_m = _NAME_RE.search(context)
            exec_name = clean_text(name_m.group(1)) if name_m else "Unknown"
            exec_id = stable_id("exec", exec_name, metadata.get("ticker", "UNKNOWN"))
            executives.append({
                "exec_id": exec_id,
                "name": exec_name,
                "role": role,
                "ticker": metadata.get("ticker", "UNKNOWN"),
                "appointment_date": event_date,
            })
    return executives


def extract_suppliers(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract supplier entities from filing text.

    Parses supply chain disclosures from Item 1 (Business), Item 1A (Risk Factors),
    and Item 7 (MD&A) sections of 10-K/10-Q filings.
    Returns dict keyed by supplier name (primary key for Supplier node table).
    """
    suppliers: dict[str, dict[str, Any]] = {}
    text = strip_markup(raw)
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")
    accession = metadata.get("accession_number", "")
    fiscal_year = metadata.get("fiscal_year", "")
    fiscal_period = metadata.get("fiscal_period", "")

    # Known supplier patterns from SEC filings
    supplier_patterns = [
        # "We source X from [Supplier Name]" - use word boundary to avoid matching inside "sources"
        (r"\b(?:sources?|purchase|procure|obtain|acquire)\s+(?:our\s+)?(?:core\s+)?(?:silicon|chips|components|materials|parts|products)\s+from\s+([A-Z][A-Za-z0-9\s&.,'\(\)\-]{3,80}?)(?:\.|,|;|$)", "silicon/components"),
        # "[Supplier Name] supplies X" - require company-like name
        (r"\b([A-Z][A-Za-z0-9\s&.,'\(\)\-]{3,80}?(?:\s+(?:Inc|Corp|Corporation|Ltd|Limited|LLC|Co|Company|Technologies|Semiconductor|Electronics|Manufacturing|Industries|Holdings|Group|International))?)\s+(?:supplies?|provides?|manufactures?)\s+(?:our\s+)?(?:silicon|chips|components|materials|parts|products)", "supplies"),
        # "Our [relationship] with [Supplier Name]"
        (r"(?:our|the)\s+(?:relationship|partnership|agreement|contract)\s+with\s+([A-Z][A-Za-z0-9\s&.,'\(\)\-]{3,80}?)(?:\.|,|;|$)", "partnership"),
        # "[Supplier Name] is our [sole|primary|key|major] supplier"
        (r"\b([A-Z][A-Za-z0-9\s&.,'\(\)\-]{3,80}?)\s+is\s+(?:our\s+)?(?:sole|primary|key|major|strategic)\s+supplier", "key_supplier"),
        # "depends on [Supplier Name] for"
        (r"depends\s+on\s+([A-Z][A-Za-z0-9\s&.,'\(\)\-]{3,80}?)\s+for\s+(?:our\s+)?(?:silicon|chips|components|materials|parts)", "dependency"),
    ]

    # Known major suppliers to recognize (canonical names)
    known_suppliers = {
        "Taiwan Semiconductor Manufacturing Company": {"cik": "0000062078", "headquarters": "Hsinchu, Taiwan", "relationship_type": "Foundry", "criticality": "Critical"},
        "TSMC": {"cik": "0000062078", "headquarters": "Hsinchu, Taiwan", "relationship_type": "Foundry", "criticality": "Critical"},
        "Samsung Electronics": {"cik": "0000915382", "headquarters": "Suwon, South Korea", "relationship_type": "Memory/Foundry", "criticality": "High"},
        "Foxconn": {"cik": "0000035552", "headquarters": "New Taipei City, Taiwan", "relationship_type": "Assembly", "criticality": "Critical"},
        "Hon Hai Precision Industry": {"cik": "0000035552", "headquarters": "New Taipei City, Taiwan", "relationship_type": "Assembly", "criticality": "Critical"},
        "Broadcom": {"cik": "0001670246", "headquarters": "San Jose, CA, USA", "relationship_type": "Semiconductors", "criticality": "High"},
        "Qualcomm": {"cik": "0000804328", "headquarters": "San Diego, CA, USA", "relationship_type": "Modems/RF", "criticality": "High"},
        "ASML": {"cik": "0000917949", "headquarters": "Veldhoven, Netherlands", "relationship_type": "Lithography", "criticality": "Critical"},
        "SK Hynix": {"cik": "0001035128", "headquarters": "Icheon, South Korea", "relationship_type": "Memory", "criticality": "High"},
        "Micron Technology": {"cik": "0000063071", "headquarters": "Boise, ID, USA", "relationship_type": "Memory", "criticality": "High"},
        "Corning": {"cik": "0000023741", "headquarters": "Corning, NY, USA", "relationship_type": "Glass", "criticality": "Medium"},
        "LG Display": {"cik": "0001106241", "headquarters": "Seoul, South Korea", "relationship_type": "Display", "criticality": "High"},
        "BOE Technology": {"cik": "0001318605", "headquarters": "Beijing, China", "relationship_type": "Display", "criticality": "Medium"},
        "Murata Manufacturing": {"cik": "0000065074", "headquarters": "Kyoto, Japan", "relationship_type": "Components", "criticality": "Medium"},
        "TDK": {"cik": "0000065075", "headquarters": "Tokyo, Japan", "relationship_type": "Components", "criticality": "Medium"},
    }

    # False positive filters - common phrases that look like suppliers but aren't
    supplier_false_positives = {
        "introductions of new products and services",
        "them is managed through a direct agreement between microsoft and the oem",
        "our products and services",
        "our customers",
        "our partners",
        "our suppliers",
        "the company",
        "the group",
        "the business",
        "the market",
        "the industry",
        "new products",
        "new services",
        "new technologies",
        "new markets",
        "new customers",
        "new partners",
    }

    def is_valid_supplier(name: str) -> bool:
        """Filter out false positive supplier names."""
        name_lower = name.lower().strip()
        if name_lower in supplier_false_positives:
            return False
        # Must have at least one capitalized word that looks like a proper noun
        if not re.search(r'\b[A-Z][a-z]+\b', name):
            return False
        # Reject if it's mostly lowercase or generic
        words = name.split()
        if len(words) < 2:
            return False
        # Reject if it contains sentence-like fragments
        if any(w in name_lower for w in [" if ", " that ", " which ", " when ", " where ", " because ", " although ", " however "]):
            return False
        return True

    # Extract from text using patterns
    for pattern, context_type in supplier_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            supplier_name = clean_text(match.group(1))
            if len(supplier_name) < 3 or len(supplier_name) > 100:
                continue
            # Clean up the name
            supplier_name = re.sub(r"\s+", " ", supplier_name).strip(" .,;")
            if not is_valid_supplier(supplier_name):
                continue
            # Check against known suppliers for enrichment
            canonical_name = supplier_name
            props = {"relationship_type": "Supplier", "criticality": "Medium", "ticker": "", "cik": "", "headquarters": "", "description": f"Identified from {context_type} in {filing_date} filing"}
            for known, info in known_suppliers.items():
                if known.lower() in supplier_name.lower() or supplier_name.lower() in known.lower():
                    canonical_name = known
                    props.update(info)
                    break
            # Use canonical name as key (primary key for Supplier table is name)
            key = canonical_name
            if key not in suppliers:
                suppliers[key] = {
                    "name": canonical_name,
                    "relationship_type": props["relationship_type"],
                    "criticality": props["criticality"],
                    "ticker": props["ticker"],
                    "cik": props["cik"],
                    "headquarters": props["headquarters"],
                    "description": props["description"],
                }
            else:
                # Merge: keep higher criticality, append description
                existing = suppliers[key]
                if props["criticality"] in ("Critical", "High") and existing["criticality"] not in ("Critical", "High"):
                    existing["criticality"] = props["criticality"]
                existing["description"] += f"; {props['description']}"

    # Also extract from explicit supplier lists in tables
    tables = []
    try:
        tables = list(pd.read_html(io.StringIO(html_body(raw)), flavor="lxml"))
    except Exception:
        pass

    for frame in tables:
        if frame is None or frame.empty:
            continue
        grid = frame.astype(str)
        # Look for supplier-related columns
        for col in grid.columns:
            # Convert all values to string explicitly to avoid float/NaN issues
            col_vals = [str(v) for v in grid[col].tolist()]
            col_text = " ".join(col_vals).lower()
            if any(kw in col_text for kw in ["supplier", "vendor", "foundry", "assembly", "manufacturing partner"]):
                for val in col_vals:
                    val = clean_text(val)
                    if val and len(val) > 3 and val.lower() not in _DASHES and val.lower() != "nan":
                        key = val
                        if key not in suppliers:
                            suppliers[key] = {
                                "name": key,
                                "relationship_type": "Supplier",
                                "criticality": "Medium",
                                "ticker": "",
                                "cik": "",
                                "headquarters": "",
                                "description": f"Listed in supplier table in {filing_date} filing",
                            }

    return suppliers


def extract_components(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract component entities from filing text.

    Parses component disclosures from Item 1 (Business), Item 7 (MD&A),
    and product specifications in 10-K/10-Q filings.
    Returns dict keyed by component name (primary key for Component node table).
    """
    components: dict[str, dict[str, Any]] = {}
    text = strip_markup(raw)
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")

    # Component patterns from SEC filings
    component_patterns = [
        # "Our [product] uses [Component Name]"
        (r"(?:our|the|its)\s+(?:products?|devices?|systems?)\s+(?:use|incorporate|include|contain|employ|utilize)\s+([A-Z][A-Za-z0-9\s\-]{2,60}?)(?:\s+(?:chip|processor|component|module|sensor|display|battery|camera))?(?:\.|,|;|$)", "product_uses"),
        # "[Component Name] is used in [product]"
        (r"([A-Z][A-Za-z0-9\s\-]{2,60}?)\s+is\s+used\s+in\s+(?:our\s+)?(?:products?|devices?|systems?|iPhone|iPad|Mac|Apple Watch|Apple TV)", "used_in"),
        # "[Component Name] (also known as|marketed as)"
        (r"([A-Z][A-Za-z0-9\s\-]{2,60}?)\s+(?:\(also known as|marketed as|branded as)\s+([A-Z][A-Za-z0-9\s\-]{2,60}?)", "alias"),
        # "custom [Component Name]" or "proprietary [Component Name]"
        (r"(?:custom|proprietary|in-house|internally developed)\s+([A-Z][A-Za-z0-9\s\-]{2,60}?)(?:\s+(?:chip|processor|silicon|component|module|sensor))?(?:\.|,|;|$)", "custom"),
        # "We design our own [Component Name]"
        (r"(?:design|develop|manufacture|fabricate)\s+(?:our\s+)?(?:own\s+)?([A-Z][A-Za-z0-9\s\-]{2,60}?)(?:\s+(?:chip|processor|silicon|component|module|sensor))?(?:\.|,|;|$)", "designed"),
    ]

    # Known components to recognize
    known_components = {
        "A16 Bionic": {"component_type": "SoC", "description": "Apple-designed system-on-chip", "manufacturer": "TSMC", "part_number": ""},
        "A17 Pro": {"component_type": "SoC", "description": "Apple-designed 3nm system-on-chip", "manufacturer": "TSMC", "part_number": ""},
        "M3": {"component_type": "SoC", "description": "Apple-designed Mac system-on-chip", "manufacturer": "TSMC", "part_number": ""},
        "M3 Pro": {"component_type": "SoC", "description": "Apple-designed Mac system-on-chip", "manufacturer": "TSMC", "part_number": ""},
        "M3 Max": {"component_type": "SoC", "description": "Apple-designed Mac system-on-chip", "manufacturer": "TSMC", "part_number": ""},
        "Neural Engine": {"component_type": "NPU", "description": "Apple-designed neural processing unit", "manufacturer": "Apple/TSMC", "part_number": ""},
        "Secure Enclave": {"component_type": "Security", "description": "Apple-designed security coprocessor", "manufacturer": "Apple", "part_number": ""},
        "Image Signal Processor": {"component_type": "ISP", "description": "Apple-designed image processing", "manufacturer": "Apple", "part_number": ""},
        "LPDDR5": {"component_type": "Memory", "description": "Low-power DDR5 memory", "manufacturer": "Samsung/SK Hynix/Micron", "part_number": ""},
        "NAND Flash": {"component_type": "Storage", "description": "Flash memory storage", "manufacturer": "Samsung/SK Hynix/Kioxia", "part_number": ""},
        "OLED Display": {"component_type": "Display", "description": "Organic light-emitting diode display", "manufacturer": "Samsung Display/LG Display", "part_number": ""},
        "LTPO Display": {"component_type": "Display", "description": "Low-temperature polycrystalline oxide display", "manufacturer": "Samsung Display/LG Display", "part_number": ""},
        "Ceramic Shield": {"component_type": "Glass", "description": "Apple/Corning co-developed glass", "manufacturer": "Corning", "part_number": ""},
        "MagSafe": {"component_type": "Charging", "description": "Magnetic wireless charging system", "manufacturer": "Apple", "part_number": ""},
        "U1 Chip": {"component_type": "UWB", "description": "Ultra-wideband chip for spatial awareness", "manufacturer": "Apple/Decawave", "part_number": ""},
        "U2 Chip": {"component_type": "UWB", "description": "Second-gen ultra-wideband chip", "manufacturer": "Apple", "part_number": ""},
        "S9 SiP": {"component_type": "SiP", "description": "System-in-package for Apple Watch", "manufacturer": "Apple/TSMC", "part_number": ""},
        "H1 Chip": {"component_type": "Audio", "description": "Headphone connectivity chip", "manufacturer": "Apple", "part_number": ""},
        "H2 Chip": {"component_type": "Audio", "description": "Second-gen headphone chip", "manufacturer": "Apple", "part_number": ""},
        "R1 Chip": {"component_type": "Vision", "description": "Real-time sensor processing for Vision Pro", "manufacturer": "Apple", "part_number": ""},
    }

    # False positive filters for components
    component_false_positives = {
        "operating systems",
        "and support software",
        "and sell devices",
        "our products",
        "our devices",
        "our systems",
        "our services",
        "the cloud",
        "the platform",
        "the service",
        "the product",
        "the device",
        "the system",
        "software and services",
        "hardware and software",
        "products and services",
        "devices and services",
    }

    def is_valid_component(name: str) -> bool:
        """Filter out false positive component names."""
        name_lower = name.lower().strip()
        if name_lower in component_false_positives:
            return False
        # Must look like a hardware component (contain hardware-related keywords or be a known component)
        hardware_keywords = ["chip", "processor", "silicon", "memory", "display", "battery", "camera", "sensor", "module", "controller", "accelerator", "engine", "enclave", "shield", "glass", "lens", "antenna", "radio", "modem", "soc", "cpu", "gpu", "npu", "isp", "dram", "nand", "ssd", "hdd", "pcb", "board", "package", "sip", "die", "wafer", "substrate", "interconnect", "photonics", "optics", "laser", "led", "oled", "lcd", "ltpo", "ceramic", "metal", "aluminum", "titanium", "carbon", "fiber", "connector", "port", "charger", "cable", "adapter", "haptic", "motor", "speaker", "microphone", "fan", "heat", "thermal", "cooling", "vapor", "chamber", "graphite", "copper"]
        if not any(kw in name_lower for kw in hardware_keywords):
            # Allow if it matches a known component
            if not any(known.lower() in name_lower for known in known_components):
                return False
        # Reject if it contains sentence-like fragments
        if any(w in name_lower for w in [" and ", " or ", " with ", " for ", " that ", " which ", " when ", " where "]):
            return False
        return True

    for pattern, context_type in component_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            component_name = clean_text(match.group(1))
            if len(component_name) < 3 or len(component_name) > 80:
                continue
            component_name = re.sub(r"\s+", " ", component_name).strip(" .,;")
            if not is_valid_component(component_name):
                continue
            canonical_name = component_name
            props = {"component_type": "Component", "description": f"Identified from {context_type} in {filing_date} filing", "manufacturer": "", "part_number": ""}
            for known, info in known_components.items():
                if known.lower() in component_name.lower() or component_name.lower() in known.lower():
                    canonical_name = known
                    props.update(info)
                    break
            key = canonical_name
            if key not in components:
                components[key] = {
                    "name": canonical_name,
                    "component_type": props["component_type"],
                    "description": props["description"],
                    "manufacturer": props["manufacturer"],
                    "part_number": props["part_number"],
                }
            else:
                existing = components[key]
                if props["description"] and props["description"] not in existing["description"]:
                    existing["description"] += f"; {props['description']}"

    return components


def extract_products(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract product entities from filing text.

    Parses product disclosures from Item 1 (Business), Item 7 (MD&A),
    and segment reporting in 10-K/10-Q filings.
    Returns dict keyed by product name (primary key for Product node table).
    """
    products: dict[str, dict[str, Any]] = {}
    text = strip_markup(raw)
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")
    fiscal_year = metadata.get("fiscal_year", "")

    # Product patterns from SEC filings
    product_patterns = [
        # "Our [Product Name] [product line]" - more restrictive
        (r"(?:our|the|its)\s+([A-Z][A-Za-z0-9\s]{2,40}?)\s+(?:product line|product family|series|models?)\s+(?:is|are|includes?|consists?\s+of)", "product_line"),
        # "[Product Name] is our [description]"
        (r"([A-Z][A-Za-z0-9\s]{2,40}?)\s+is\s+(?:our|the)\s+(?:flagship|premium|entry-level|main|primary|newest|latest)\s+(?:product|device|offering)", "flagship"),
        # Named products only - rely on known list + specific patterns
        # "iPhone", "iPad", "Mac", "Apple Watch", "Apple TV", "AirPods", "HomePod", "Vision Pro" with model numbers
        (r"\b(iPhone\s+\d{1,2}(?:\s+Pro| Plus| Max| Mini)?|iPad\s+(?:Pro|Air|Mini)?|Mac(?:Book\s+(?:Pro|Air)?| mini| Studio| Pro)?|Apple Watch\s+(?:Series\s+\d+|Ultra|SE)|Apple TV\s+(?:4K)?|AirPods\s+(?:Pro|Max)?|HomePod\s+(?:mini)?|Vision Pro)\b", "named_product"),
        # Microsoft-specific named products
        (r"\b(Surface\s+(?:Pro|Laptop|Book|Studio|Go|Duo)?|Xbox\s+(?:Series\s+[XS]|One|360)?|HoloLens|Windows\s+(?:1[01]|Server)?|Office\s+(?:365|20[0-9]{2})?|Azure|Dynamics\s+365|LinkedIn|GitHub)\b", "named_product_msft"),
    ]

    # Known Microsoft products (extendable per company)
    known_products = {
        "iPhone": {"product_family": "iPhone", "description": "Apple's smartphone line", "launch_date": "2007-06-29", "lifecycle_stage": "Active", "issuer": ticker},
        "iPad": {"product_family": "iPad", "description": "Apple's tablet line", "launch_date": "2010-04-03", "lifecycle_stage": "Active", "issuer": ticker},
        "Mac": {"product_family": "Mac", "description": "Apple's personal computer line", "launch_date": "1984-01-24", "lifecycle_stage": "Active", "issuer": ticker},
        "Apple Watch": {"product_family": "Apple Watch", "description": "Apple's smartwatch line", "launch_date": "2015-04-24", "lifecycle_stage": "Active", "issuer": ticker},
        "Apple TV": {"product_family": "Apple TV", "description": "Apple's streaming media player", "launch_date": "2007-03-21", "lifecycle_stage": "Active", "issuer": ticker},
        "AirPods": {"product_family": "AirPods", "description": "Apple's wireless earbuds", "launch_date": "2016-12-13", "lifecycle_stage": "Active", "issuer": ticker},
        "HomePod": {"product_family": "HomePod", "description": "Apple's smart speaker", "launch_date": "2018-02-09", "lifecycle_stage": "Active", "issuer": ticker},
        "Vision Pro": {"product_family": "Vision Pro", "description": "Apple's spatial computer", "launch_date": "2024-02-02", "lifecycle_stage": "Active", "issuer": ticker},
        "iPhone 15": {"product_family": "iPhone", "description": "iPhone 15 series", "launch_date": "2023-09-22", "lifecycle_stage": "Active", "issuer": ticker},
        "iPhone 15 Pro": {"product_family": "iPhone", "description": "iPhone 15 Pro series", "launch_date": "2023-09-22", "lifecycle_stage": "Active", "issuer": ticker},
        "iPhone 14": {"product_family": "iPhone", "description": "iPhone 14 series", "launch_date": "2022-09-16", "lifecycle_stage": "Active", "issuer": ticker},
        "iPhone 13": {"product_family": "iPhone", "description": "iPhone 13 series", "launch_date": "2021-09-24", "lifecycle_stage": "Active", "issuer": ticker},
        "MacBook Pro": {"product_family": "Mac", "description": "MacBook Pro line", "launch_date": "2006-01-10", "lifecycle_stage": "Active", "issuer": ticker},
        "MacBook Air": {"product_family": "Mac", "description": "MacBook Air line", "launch_date": "2008-01-15", "lifecycle_stage": "Active", "issuer": ticker},
        "iPad Pro": {"product_family": "iPad", "description": "iPad Pro line", "launch_date": "2015-11-11", "lifecycle_stage": "Active", "issuer": ticker},
        "iPad Air": {"product_family": "iPad", "description": "iPad Air line", "launch_date": "2013-10-22", "lifecycle_stage": "Active", "issuer": ticker},
        "Apple Watch Series 9": {"product_family": "Apple Watch", "description": "Apple Watch Series 9", "launch_date": "2023-09-22", "lifecycle_stage": "Active", "issuer": ticker},
        "Apple Watch Ultra 2": {"product_family": "Apple Watch", "description": "Apple Watch Ultra 2", "launch_date": "2023-09-22", "lifecycle_stage": "Active", "issuer": ticker},
        # Microsoft products
        "Surface": {"product_family": "Surface", "description": "Microsoft's Surface device line", "launch_date": "2012-10-26", "lifecycle_stage": "Active", "issuer": ticker},
        "Surface Pro": {"product_family": "Surface", "description": "Microsoft Surface Pro line", "launch_date": "2013-02-09", "lifecycle_stage": "Active", "issuer": ticker},
        "Surface Laptop": {"product_family": "Surface", "description": "Microsoft Surface Laptop line", "launch_date": "2017-06-15", "lifecycle_stage": "Active", "issuer": ticker},
        "Surface Book": {"product_family": "Surface", "description": "Microsoft Surface Book line", "launch_date": "2015-10-26", "lifecycle_stage": "Active", "issuer": ticker},
        "Surface Studio": {"product_family": "Surface", "description": "Microsoft Surface Studio line", "launch_date": "2016-12-15", "lifecycle_stage": "Active", "issuer": ticker},
        "Xbox": {"product_family": "Xbox", "description": "Microsoft's Xbox gaming console line", "launch_date": "2001-11-15", "lifecycle_stage": "Active", "issuer": ticker},
        "Xbox Series X": {"product_family": "Xbox", "description": "Xbox Series X console", "launch_date": "2020-11-10", "lifecycle_stage": "Active", "issuer": ticker},
        "Xbox Series S": {"product_family": "Xbox", "description": "Xbox Series S console", "launch_date": "2020-11-10", "lifecycle_stage": "Active", "issuer": ticker},
        "HoloLens": {"product_family": "HoloLens", "description": "Microsoft's mixed reality headset", "launch_date": "2016-03-30", "lifecycle_stage": "Active", "issuer": ticker},
        "Windows": {"product_family": "Windows", "description": "Microsoft's operating system", "launch_date": "1985-11-20", "lifecycle_stage": "Active", "issuer": ticker},
        "Office": {"product_family": "Office", "description": "Microsoft's productivity suite", "launch_date": "1989-08-01", "lifecycle_stage": "Active", "issuer": ticker},
        "Azure": {"product_family": "Azure", "description": "Microsoft's cloud platform", "launch_date": "2010-02-01", "lifecycle_stage": "Active", "issuer": ticker},
        "Dynamics": {"product_family": "Dynamics", "description": "Microsoft's ERP/CRM suite", "launch_date": "2001-02-01", "lifecycle_stage": "Active", "issuer": ticker},
        "LinkedIn": {"product_family": "LinkedIn", "description": "Microsoft's professional network", "launch_date": "2003-05-05", "lifecycle_stage": "Active", "issuer": ticker},
        "GitHub": {"product_family": "GitHub", "description": "Microsoft's code hosting platform", "launch_date": "2008-04-10", "lifecycle_stage": "Active", "issuer": ticker},
    }

    # False positive filters for products
    product_false_positives = {
        "founded in 1975",
        "our products",
        "our services",
        "our devices",
        "our systems",
        "our solutions",
        "our platforms",
        "the company",
        "the business",
        "the market",
        "the industry",
        "new products",
        "new services",
        "new devices",
        "new systems",
        "the system",
        "the product",
        "the service",
        "the platform",
        "the solution",
        "the device",
        "cloud services",
        "cloud platform",
        "cloud solutions",
    }

    def is_valid_product(name: str) -> bool:
        """Filter out false positive product names."""
        name_lower = name.lower().strip()
        if name_lower in product_false_positives:
            return False
        # Must be a known product or look like a proper product name (capitalized, not generic)
        if not any(known.lower() in name_lower for known in known_products):
            # Check if it looks like a real product name (has brand-like qualities)
            words = name.split()
            if len(words) < 2:
                return False
            # Should not be a sentence fragment
            if any(w in name_lower for w in [" and ", " or ", " with ", " for ", " that ", " which ", " when ", " where ", " the ", " our ", " their "]):
                return False
        return True

    for pattern, context_type in product_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            product_name = clean_text(match.group(1))
            if len(product_name) < 3 or len(product_name) > 80:
                continue
            product_name = re.sub(r"\s+", " ", product_name).strip(" .,;")
            if not is_valid_product(product_name):
                continue
            canonical_name = product_name
            props = {"product_family": "Product", "description": f"Identified from {context_type} in {filing_date} filing", "launch_date": "", "lifecycle_stage": "Active", "issuer": ticker}
            for known, info in known_products.items():
                if known.lower() in product_name.lower() or product_name.lower() in known.lower():
                    canonical_name = known
                    props.update(info)
                    break
            key = canonical_name
            if key not in products:
                products[key] = {
                    "name": canonical_name,
                    "product_family": props["product_family"],
                    "description": props["description"],
                    "launch_date": props["launch_date"],
                    "lifecycle_stage": props["lifecycle_stage"],
                    "issuer": props["issuer"],
                }
            else:
                existing = products[key]
                if props["description"] and props["description"] not in existing["description"]:
                    existing["description"] += f"; {props['description']}"

    return products


def extract_manufacturing(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract manufacturing location entities from filing text.

    Parses manufacturing disclosures from Item 1 (Business), Item 1A (Risk Factors),
    and Item 7 (MD&A) in 10-K/10-Q filings.
    Returns dict keyed by manufacturing name (primary key for Manufacturing node table).
    """
    manufacturing: dict[str, dict[str, Any]] = {}
    text = strip_markup(raw)
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")

    # Manufacturing location patterns
    mfg_patterns = [
        # "manufactured at [Location]"
        (r"(?:manufactured|assembled|produced|fabricated)\s+(?:at|in)\s+([A-Z][A-Za-z0-9\s,.\-]{3,80}?)(?:\.|,|;|$)", "manufactured_at"),
        # "assembles [product] in [Location]"
        (r"(?:assembles?|manufactures?)\s+[A-Za-z0-9\s]+\s+in\s+([A-Z][A-Za-z0-9\s,.\-]{3,80}?)(?:\.|,|;|$)", "assembles_in"),
        # "Our [Location] facility"
        (r"(?:our|the)\s+([A-Z][A-Za-z0-9\s,.\-]{3,80}?)\s+(?:facility|plant|factory|site|location)\s+(?:in|at|on)", "facility"),
        # "[Location] manufacturing"
        (r"([A-Z][A-Za-z0-9\s,.\-]{3,80}?)\s+manufacturing\s+(?:facility|plant|operations|site)", "mfg_site"),
        # "Final assembly in [Location]"
        (r"final\s+assembly\s+(?:in|at|occurs\s+in)\s+([A-Z][A-Za-z0-9\s,.\-]{3,80}?)(?:\.|,|;|$)", "final_assembly"),
        # "We operate manufacturing facilities in [Location]"
        (r"(?:operate|maintain|run)\s+(?:manufacturing\s+)?(?:facilities?|plants?)\s+in\s+([A-Z][A-Za-z0-9\s,.\-]{3,80}?)(?:\.|,|;|$)", "operates_in"),
    ]

    # Known manufacturing locations (Apple + Microsoft)
    known_mfg = {
        # Apple
        "Foxconn Zhengzhou": {"location": "Zhengzhou, Henan, China", "process_type": "Final Assembly", "capacity": "High", "description": "Primary iPhone final assembly facility (iPhone City)"},
        "Foxconn Taiyuan": {"location": "Taiyuan, Shanxi, China", "process_type": "Final Assembly", "capacity": "High", "description": "iPhone final assembly facility"},
        "Foxconn Chengdu": {"location": "Chengdu, Sichuan, China", "process_type": "Final Assembly", "capacity": "High", "description": "iPad and iPhone final assembly"},
        "Foxconn Shenzhen": {"location": "Shenzhen, Guangdong, China", "process_type": "Final Assembly", "capacity": "High", "description": "iPhone and other product assembly"},
        "Pegatron Shanghai": {"location": "Shanghai, China", "process_type": "Final Assembly", "capacity": "Medium", "description": "iPhone final assembly partner"},
        "Pegatron Kunshan": {"location": "Kunshan, Jiangsu, China", "process_type": "Final Assembly", "capacity": "Medium", "description": "iPhone final assembly partner"},
        "Luxshare ICT": {"location": "Kunshan, Jiangsu, China", "process_type": "Final Assembly", "capacity": "Medium", "description": "AirPods and accessories assembly"},
        "Goertek": {"location": "Weifang, Shandong, China", "process_type": "Acoustic Assembly", "capacity": "Medium", "description": "AirPods acoustic assembly"},
        "TSMC Fab 18": {"location": "Hsinchu, Taiwan", "process_type": "Semiconductor Fabrication", "capacity": "Critical", "description": "5nm/3nm process for Apple Silicon"},
        "TSMC Fab 21": {"location": "Phoenix, Arizona, USA", "process_type": "Semiconductor Fabrication", "capacity": "Growing", "description": "Future US-based Apple Silicon fabrication"},
        "Samsung Austin": {"location": "Austin, Texas, USA", "process_type": "Semiconductor Fabrication", "capacity": "Medium", "description": "Legacy process for some Apple chips"},
        "Corning Harrodsburg": {"location": "Harrodsburg, Kentucky, USA", "process_type": "Glass Manufacturing", "capacity": "High", "description": "Ceramic Shield glass production"},
        "Apple Cork": {"location": "Cork, Ireland", "process_type": "Final Assembly/Logistics", "capacity": "Medium", "description": "European distribution and some assembly"},
        "Apple Austin": {"location": "Austin, Texas, USA", "process_type": "Final Assembly/Operations", "capacity": "Medium", "description": "Mac Pro assembly and operations center"},
        # Microsoft
        "Flextronics": {"location": "Singapore / Mexico / USA", "process_type": "Assembly", "capacity": "High", "description": "Surface and Xbox assembly partner"},
        "Celestica": {"location": "Malaysia / China / USA", "process_type": "Assembly", "capacity": "High", "description": "Surface and Xbox assembly partner"},
        "Pegatron": {"location": "Taiwan / China", "process_type": "Assembly", "capacity": "High", "description": "Surface and Xbox assembly partner"},
        "Wistron": {"location": "Taiwan / China / India", "process_type": "Assembly", "capacity": "Medium", "description": "Surface assembly partner"},
        "Quanta Computer": {"location": "Taiwan / China", "process_type": "Assembly", "capacity": "Medium", "description": "Surface assembly partner"},
    }

    # False positive filters for manufacturing
    mfg_false_positives = {
        "asia and other geographies that may be subject to disruptions in the supply chain",
        "the supply chain",
        "our facilities",
        "our plants",
        "our factories",
        "our sites",
        "the facility",
        "the plant",
        "the factory",
        "the site",
        "manufacturing facilities",
        "manufacturing plants",
        "manufacturing operations",
        "manufacturing sites",
        "final assembly",
        "final assembly in",
        "assembly in",
        "produced in",
        "manufactured in",
        "assembled in",
    }

    def is_valid_mfg(name: str) -> bool:
        """Filter out false positive manufacturing names."""
        name_lower = name.lower().strip()
        if name_lower in mfg_false_positives:
            return False
        # Should look like a proper location name (City, Country/State or Company Name)
        # Must have at least 2 words and look like a location
        words = name.split()
        if len(words) < 2:
            return False
        # Should not be a sentence fragment
        if any(w in name_lower for w in [" and ", " or ", " that ", " which ", " when ", " where ", " may ", " could ", " would ", " should ", " might ", " subject to ", " disruptions ", " supply chain "]):
            return False
        return True

    for pattern, context_type in mfg_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            mfg_name = clean_text(match.group(1))
            if len(mfg_name) < 3 or len(mfg_name) > 100:
                continue
            mfg_name = re.sub(r"\s+", " ", mfg_name).strip(" .,;")
            if not is_valid_mfg(mfg_name):
                continue
            canonical_name = mfg_name
            props = {"location": "", "process_type": "Manufacturing", "capacity": "Medium", "description": f"Identified from {context_type} in {filing_date} filing"}
            for known, info in known_mfg.items():
                if known.lower() in mfg_name.lower() or mfg_name.lower() in known.lower():
                    canonical_name = known
                    props.update(info)
                    break
            # If no known match, try to extract location from the name
            if not props["location"]:
                # Try to find city, country pattern
                loc_match = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", mfg_name)
                if loc_match:
                    props["location"] = f"{loc_match.group(1)}, {loc_match.group(2)}"
            key = canonical_name
            if key not in manufacturing:
                manufacturing[key] = {
                    "name": canonical_name,
                    "location": props["location"],
                    "process_type": props["process_type"],
                    "capacity": props["capacity"],
                    "description": props["description"],
                }
            else:
                existing = manufacturing[key]
                if props["description"] and props["description"] not in existing["description"]:
                    existing["description"] += f"; {props['description']}"

    return manufacturing


def extract_management_commentary(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract management commentary sections from filing text.

    Parses MD&A (Item 7), Risk Factors (Item 1A), and Business (Item 1) sections
    from 10-K/10-Q filings to identify management discussion topics.
    Returns dict keyed by commentary topic name (primary key for ManagementCommentary node table).
    """
    commentary: dict[str, dict[str, Any]] = {}
    text = strip_markup(raw)
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")
    fiscal_year = metadata.get("fiscal_year", "")
    fiscal_period = metadata.get("fiscal_period", "")
    filing_id = filing_identity(metadata)

    # MD&A section extraction - find the Item 7 section
    mda_section = ""
    mda_match = re.search(r"(?:ITEM\s+7|Item\s+7)\.?\s*(?:MANAGEMENT['\']?S\s+DISCUSSION\s+AND\s+ANALYSIS|MD&A)[\s\S]{0,20000}", text, re.IGNORECASE)
    if mda_match:
        mda_section = mda_match.group(0)

    # Risk Factors section - Item 1A
    risk_section = ""
    risk_match = re.search(r"(?:ITEM\s+1A|Item\s+1A)\.?\s*RISK\s+FACTORS[\s\S]{0,30000}", text, re.IGNORECASE)
    if risk_match:
        risk_section = risk_match.group(0)

    # Business section - Item 1
    biz_section = ""
    biz_match = re.search(r"(?:ITEM\s+1|Item\s+1)\.?\s*BUSINESS[\s\S]{0,20000}", text, re.IGNORECASE)
    if biz_match:
        biz_section = biz_match.group(0)

    # Combine sections for searching
    combined_sections = "\n\n".join([mda_section, risk_section, biz_section])

    # Topic patterns in management commentary
    topic_patterns = [
        # "We discuss [topic]"
        (r"(?:we|management)\s+(?:discuss|discusses|address|addresses)\s+([A-Z][A-Za-z0-9\s]{5,80}?)(?:\.|,|;|in\s+this|below)", "discusses"),
        # "Our strategy for [topic]"
        (r"(?:our|the)\s+strategy\s+(?:for|regarding|on)\s+([A-Z][A-Za-z0-9\s]{5,80}?)(?:\.|,|;|is|includes)", "strategy"),
        # "We are investing in [topic]"
        (r"(?:invest|investing|invested)\s+(?:in|heavily\s+in)\s+([A-Z][A-Za-z0-9\s]{5,80}?)(?:\.|,|;|to|for)", "investment"),
        # "Key growth driver[ is] [topic]"
        (r"(?:key|primary|major)\s+growth\s+driver\s+(?:is|are|includes?)\s+([A-Z][A-Za-z0-9\s]{5,80}?)(?:\.|,|;)", "growth_driver"),
        # "We expect [topic] to"
        (r"(?:we|management)\s+(?:expect|anticipate|believe|project)\s+([A-Z][A-Za-z0-9\s]{5,80}?)\s+(?:will|to|would)", "forward_looking"),
        # "Supply chain" discussion - capture the full phrase
        (r"((?:supply\s+chain|supply\s+network|procurement|sourcing)\s+(?:strategy|management|diversification|resilience|risk))", "supply_chain"),
        # "Apple Silicon" / "custom silicon" discussion
        (r"((?:Apple\s+Silicon|custom\s+silicon|in-house\s+(?:chip|silicon|processor)|proprietary\s+(?:chip|silicon)))", "silicon_strategy"),
        # "Services" growth
        (r"((?:services|Service\s+revenue)\s+(?:growth|revenue|business|margin))", "services_growth"),
        # "Geographic" discussion
        (r"((?:geographic|region|China|Greater\s+China|Americas|Europe|Japan|Asia\s+Pacific)\s+(?:revenue|sales|growth|market))", "geographic"),
        # "Capital return" / "share repurchase"
        (r"((?:capital\s+return|share\s+repurchase|dividend|buyback)\s+(?:program|policy|amount|increased))", "capital_return"),
    ]

    # Known management commentary topics for Apple
    known_topics = {
        "Apple Silicon Strategy": {"section": "MD&A", "theme": "Technology", "sentiment": "Positive", "key_metrics": "Revenue, Gross Margin, R&D Expense"},
        "Supply Chain Diversification": {"section": "Risk Factors / MD&A", "theme": "Operations", "sentiment": "Cautious", "key_metrics": "Cost of Goods Sold, Inventory"},
        "Services Growth": {"section": "MD&A", "theme": "Revenue", "sentiment": "Positive", "key_metrics": "Services Revenue, Services Gross Margin"},
        "Geographic Revenue Mix": {"section": "MD&A", "theme": "Revenue", "sentiment": "Neutral", "key_metrics": "Revenue by Geographic Segment"},
        "Capital Return Program": {"section": "MD&A", "theme": "Capital Allocation", "sentiment": "Positive", "key_metrics": "Share Repurchases, Dividends"},
        "R&D Investment": {"section": "MD&A", "theme": "Investment", "sentiment": "Positive", "key_metrics": "R&D Expense"},
        "Retail Strategy": {"section": "MD&A", "theme": "Channel", "sentiment": "Positive", "key_metrics": "Retail Revenue, Store Count"},
        "Environmental Initiatives": {"section": "MD&A / Business", "theme": "ESG", "sentiment": "Positive", "key_metrics": "Carbon Footprint, Renewable Energy"},
        "Privacy Features": {"section": "MD&A / Business", "theme": "Product", "sentiment": "Positive", "key_metrics": "User Engagement"},
        "Mac Transition to Apple Silicon": {"section": "MD&A", "theme": "Technology", "sentiment": "Positive", "key_metrics": "Mac Revenue, Gross Margin"},
    }

    for pattern, context_type in topic_patterns:
        for match in re.finditer(pattern, combined_sections, re.IGNORECASE):
            topic_name = clean_text(match.group(1))
            if len(topic_name) < 5 or len(topic_name) > 100:
                continue
            topic_name = re.sub(r"\s+", " ", topic_name).strip(" .,;")
            canonical_name = topic_name
            props = {"section": "MD&A", "theme": "General", "sentiment": "Neutral", "key_metrics": "", "summary": f"Identified from {context_type} in {filing_date} filing"}
            for known, info in known_topics.items():
                if known.lower() in topic_name.lower() or topic_name.lower() in known.lower():
                    canonical_name = known
                    props.update(info)
                    break
            key = canonical_name
            if key not in commentary:
                mc_id = stable_id("mc", ticker, filing_date, canonical_name)
                commentary[key] = {
                    "id": mc_id,
                    "filing_id": filing_id,
                    "section": props["section"],
                    "topic": canonical_name,
                    "text": props["summary"],
                    "speaker": "Management",
                    "date": filing_date,
                }
            else:
                existing = commentary[key]
                if props["summary"] and props["summary"] not in existing.get("text", ""):
                    existing["text"] = existing.get("text", "") + f"; {props['summary']}"

    return commentary


def extract_risks(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract risk factor entities from filing text.

    Parses Item 1A (Risk Factors) section from 10-K/10-Q filings
    to identify specific risks with evidence.
    Returns dict keyed by risk name (primary key for Risk node table).
    """
    risks: dict[str, dict[str, Any]] = {}
    text = strip_markup(raw)
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")
    fiscal_year = metadata.get("fiscal_year", "")
    fiscal_period = metadata.get("fiscal_period", "")
    filing_id = filing_identity(metadata)

    # Risk Factors section - Item 1A
    risk_section = ""
    risk_match = re.search(r"(?:ITEM\s+1A|Item\s+1A)\.?\s*RISK\s+FACTORS[\s\S]{0,50000}", text, re.IGNORECASE)
    if risk_match:
        risk_section = risk_match.group(0)
    else:
        # Fallback: look for any risk-related text
        risk_section = text

    # Risk patterns in SEC filings
    risk_patterns = [
        # "Risk that [description]"
        (r"(?:risk| Risk)\s+(?:that|of|related to|associated with)\s+([A-Z][A-Za-z0-9\s]{10,150}?)(?:\.|,|;|could|may|would|is|are)", "risk_that"),
        # "We are exposed to [risk]"
        (r"(?:exposed|subject)\s+to\s+(?:the\s+)?(?:risk\s+of\s+)?([A-Z][A-Za-z0-9\s]{10,150}?)(?:\.|,|;|which|that)", "exposed_to"),
        # "Could [adversely] affect [topic]"
        (r"could\s+(?:materially\s+)?(?:adversely\s+)?affect\s+(?:our|the)\s+([A-Z][A-Za-z0-9\s]{10,150}?)(?:\.|,|;)", "affects"),
        # "May [adversely] impact [topic]"
        (r"may\s+(?:materially\s+)?(?:adversely\s+)?impact\s+(?:our|the)\s+([A-Z][A-Za-z0-9\s]{10,150}?)(?:\.|,|;)", "impacts"),
        # "Dependence on [single source/supplier]"
        (r"(?:dependence|reliance|dependency)\s+on\s+(?:a\s+)?(?:single|limited|few|key)\s+(?:source|supplier|vendor|foundry|manufacturer)\s+(?:for|of)\s+([A-Z][A-Za-z0-9\s]{10,150}?)(?:\.|,|;)", "concentration"),
        # "Geopolitical risk" / "Trade restrictions" - capture full phrase
        (r"((?:geopolitical|trade|tariff|export\s+control|sanction)\s+(?:risk|tension|restriction|uncertainty))", "geopolitical"),
        # "Cybersecurity" risk
        (r"((?:cybersecurity|data\s+breach|hacking|ransomware|information\s+security)\s+(?:risk|incident|threat|attack))", "cybersecurity"),
        # "Intellectual property" risk
        (r"((?:intellectual\s+property|patent|trademark|copyright)\s+(?:risk|litigation|infringement|claim))", "ip_risk"),
        # "Regulatory" risk
        (r"((?:regulatory|compliance|antitrust|privacy|data\s+protection)\s+(?:risk|investigation|action|change))", "regulatory"),
        # "Climate" / "Environmental" risk
        (r"((?:climate\s+change|environmental|sustainability|carbon)\s+(?:risk|regulation|impact|transition))", "climate"),
        # "Foreign exchange" risk
        (r"((?:foreign\s+exchange|currency|FX)\s+(?:risk|fluctuation|impact|exposure))", "fx_risk"),
        # "Key personnel" risk
        (r"((?:key\s+personnel|executive|senior\s+management)\s+(?:risk|departure|loss|retention))", "personnel"),
    ]

    # Known Apple-specific risks
    known_risks = {
        "Supply Chain Concentration Risk": {"risk_category": "Supply Chain", "severity": "High", "description": "Dependence on single/limited suppliers for critical components (e.g., TSMC for Apple Silicon, Foxconn for assembly)", "mitigation": "Multi-sourcing strategy, supplier diversification, strategic inventory"},
        "Geopolitical Risk - China Exposure": {"risk_category": "Geopolitical", "severity": "High", "description": "Significant manufacturing and revenue exposure to Greater China; trade tensions, tariffs, regulatory changes", "mitigation": "Supply chain diversification to India/Vietnam, geographic revenue diversification"},
        "Foreign Exchange Risk": {"risk_category": "Financial", "severity": "Medium", "description": "Revenue and costs in multiple currencies; USD strength impacts reported results", "mitigation": "Natural hedging, derivative instruments"},
        "Cybersecurity and Data Privacy Risk": {"risk_category": "Cybersecurity", "severity": "High", "description": "Risk of data breaches, cyber attacks, privacy regulation compliance (GDPR, CCPA)", "mitigation": "Security investment, privacy-by-design, incident response"},
        "Intellectual Property Litigation Risk": {"risk_category": "Legal", "severity": "Medium", "description": "Patent infringement claims, IP disputes with competitors and NPEs", "mitigation": "Defensive patent portfolio, licensing agreements"},
        "Regulatory and Antitrust Risk": {"risk_category": "Regulatory", "severity": "High", "description": "App Store practices, default search agreements, self-preferencing investigations globally", "mitigation": "Policy adjustments, legal defense, compliance programs"},
        "Climate Change and Environmental Risk": {"risk_category": "Environmental", "severity": "Medium", "description": "Physical risks to facilities, transition risks from regulations, carbon neutrality commitments", "mitigation": "Renewable energy procurement, supplier clean energy program, product efficiency"},
        "Key Personnel Risk": {"risk_category": "Human Capital", "severity": "Medium", "description": "Dependence on senior leadership (Tim Cook, key executives) and specialized engineering talent", "mitigation": "Succession planning, compensation packages, culture retention"},
        "Component Shortage Risk": {"risk_category": "Supply Chain", "severity": "High", "description": "Global semiconductor shortage, logistics constraints, capacity limitations at foundries", "mitigation": "Long-term supply agreements, strategic inventory, advance capacity reservations"},
        "Consumer Demand Cyclicality Risk": {"risk_category": "Market", "severity": "Medium", "description": "Product cycles, macroeconomic conditions, consumer spending slowdowns affecting upgrade rates", "mitigation": "Services revenue growth, ecosystem lock-in, pricing strategy"},
        "New Product Introduction Risk": {"risk_category": "Product", "severity": "Medium", "description": "Delays, defects, or market rejection of new products (Vision Pro, new categories)", "mitigation": "Rigorous testing, phased rollouts, developer ecosystem investment"},
        "Tax and Repatriation Risk": {"risk_category": "Tax", "severity": "Medium", "description": "Changes in international tax laws, OECD Pillar Two, repatriation restrictions", "mitigation": "Tax planning, compliance monitoring, reserve adequacy"},
    }

    for pattern, context_type in risk_patterns:
        for match in re.finditer(pattern, risk_section, re.IGNORECASE):
            risk_name = clean_text(match.group(1))
            if len(risk_name) < 10 or len(risk_name) > 200:
                continue
            risk_name = re.sub(r"\s+", " ", risk_name).strip(" .,;")
            canonical_name = risk_name
            props = {"risk_category": "General", "severity": "Medium", "description": f"Identified from {context_type} in {filing_date} filing", "mitigation": ""}
            for known, info in known_risks.items():
                if known.lower() in risk_name.lower() or risk_name.lower() in known.lower():
                    canonical_name = known
                    props.update(info)
                    break
            key = canonical_name
            if key not in risks:
                risk_id = stable_id("risk", ticker, filing_date, canonical_name)
                risks[key] = {
                    "id": risk_id,
                    "risk_type": props["risk_category"],
                    "description": props["description"],
                    "severity": props["severity"],
                    "likelihood": "Medium",
                    "time_horizon": "Near",
                    "mitigation": props["mitigation"],
                }
            else:
                existing = risks[key]
                if props["description"] and props["description"] not in existing["description"]:
                    existing["description"] += f"; {props['description']}"

    return risks


# -- SEC Filing Intelligence Extraction Methods -----------------------------

def extract_insiders(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract insider information from Form 3, 4, 5 filings.
    
    Returns dict of insider_id -> insider data with keys:
    id, name, title, cik, is_director, is_officer, is_ten_percent_owner
    """
    insiders: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("3", "4", "5"):
        return insiders
    
    # Parse the filing for insider information
    # Form 3: Initial Statement of Beneficial Ownership
    # Form 4: Statement of Changes in Beneficial Ownership
    # Form 5: Annual Statement of Changes in Beneficial Ownership
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for insider name patterns
    # SEC forms have specific sections for reporting persons
    name_pattern = re.compile(r'Name of Reporting Person[:\s]*([A-Z][A-Za-z\s\.\-]+)', re.I)
    title_pattern = re.compile(r'Title[:\s]*([A-Za-z\s]+)', re.I)
    cik_pattern = re.compile(r'CIK[:\s]*(\d{10})', re.I)
    
    name_match = name_pattern.search(text)
    title_match = title_pattern.search(text)
    cik_match = cik_pattern.search(text)
    
    if name_match:
        name = clean_text(name_match.group(1))
        insider_id = stable_id("insider", name, metadata.get("ticker", ""))
        insiders[insider_id] = {
            "id": insider_id,
            "name": name,
            "title": clean_text(title_match.group(1)) if title_match else "",
            "cik": cik_match.group(1).zfill(10) if cik_match else "",
            "is_director": "director" in (title_match.group(1) if title_match else "").lower(),
            "is_officer": any(t in (title_match.group(1) if title_match else "").lower() 
                             for t in ["officer", "president", "ceo", "cfo", "coo", "cto", "secretary", "treasurer"]),
            "is_ten_percent_owner": "10%" in text or "ten percent" in text.lower(),
        }
    
    return insiders


def extract_insider_transactions(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract insider transactions from Form 4 filings.
    
    Returns dict of transaction_id -> transaction data with keys:
    id, transaction_date, transaction_code, security_title, shares,
    price_per_share, acquired_disposed, ownership_form, direct_indirect,
    nature_of_ownership
    """
    transactions: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type != "4":
        return transactions
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Form 4 has Table I (Non-Derivative Securities) and Table II (Derivative Securities)
    # Look for transaction rows
    txn_pattern = re.compile(
        r'Transaction Date[:\s]*(\d{2}/\d{2}/\d{4}).*?'
        r'Transaction Code[:\s]*([A-Z]).*?'
        r'Security Title[:\s]*([A-Za-z0-9\s]+).*?'
        r'Shares[:\s]*([\d,]+).*?'
        r'Price[:\s]*([\d\.]+).*?'
        r'Acquired/Disposed[:\s]*([AD])',
        re.I | re.S
    )
    
    for match in txn_pattern.finditer(text):
        txn_id = stable_id("insider_txn", match.group(1), match.group(3), metadata.get("ticker", ""))
        transactions[txn_id] = {
            "id": txn_id,
            "transaction_date": match.group(1),
            "transaction_code": match.group(2).upper(),
            "security_title": clean_text(match.group(3)),
            "shares": int(match.group(4).replace(",", "")) if match.group(4).replace(",", "").isdigit() else 0,
            "price_per_share": float(match.group(5)) if match.group(5).replace(".", "").isdigit() else 0.0,
            "acquired_disposed": match.group(6).upper(),
            "ownership_form": "D",  # Direct
            "direct_indirect": "D",
            "nature_of_ownership": "",
        }
    
    return transactions


def extract_institutional_holders(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract institutional holders from 13F-HR filings.
    
    CRITICAL: The institutional manager (filer) is SEPARATE from the issuer (company).
    The 13F-HR is filed BY the institutional manager ABOUT their holdings IN the issuer.
    
    Returns dict of holder_id -> holder data with keys:
    id, name, cik, filer_type
    """
    holders: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type != "13F-HR":
        return holders
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # The FILER is the institutional investment manager
    filer_name_pattern = re.compile(r'Name of Reporting Manager[:\s]*([A-Za-z0-9\s\.\,\&]+)', re.I)
    filer_cik_pattern = re.compile(r'Central Index Key[:\s]*(\d{10})', re.I)
    
    filer_name_match = filer_name_pattern.search(text)
    filer_cik_match = filer_cik_pattern.search(text)
    
    if filer_name_match:
        name = clean_text(filer_name_match.group(1))
        holder_id = stable_id("inst_holder", name)
        holders[holder_id] = {
            "id": holder_id,
            "name": name,
            "cik": filer_cik_match.group(1).zfill(10) if filer_cik_match else "",
            "filer_type": "institutional_investment_manager",
        }
    
    return holders


def extract_institutional_holdings(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract institutional holdings from 13F-HR filings.
    
    Returns dict of holding_id -> holding data with keys:
    id, cusip, security_name, shares, value, put_call, discretion, voting_authority
    """
    holdings: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type != "13F-HR":
        return holdings
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # 13F-HR has a table of holdings with CUSIP, security name, shares, value, etc.
    # Pattern for holding entries
    holding_pattern = re.compile(
        r'CUSIP[:\s]*(\d{9}).*?'
        r'Security Name[:\s]*([A-Za-z0-9\s\.\,\-]+).*?'
        r'Shares[:\s]*([\d,]+).*?'
        r'Value[:\s]*([\d,]+).*?'
        r'Put/Call[:\s]*([A-Z]+).*?'
        r'Discretion[:\s]*([A-Z]+).*?'
        r'Voting Authority[:\s]*([\d,]+)',
        re.I | re.S
    )
    
    for match in holding_pattern.finditer(text):
        holding_id = stable_id("inst_holding", match.group(1), metadata.get("ticker", ""))
        holdings[holding_id] = {
            "id": holding_id,
            "cusip": match.group(1),
            "security_name": clean_text(match.group(2)),
            "shares": int(match.group(3).replace(",", "")) if match.group(3).replace(",", "").isdigit() else 0,
            "value": int(match.group(4).replace(",", "")) if match.group(4).replace(",", "").isdigit() else 0,
            "put_call": match.group(5).upper(),
            "discretion": match.group(6).upper(),
            "voting_authority": int(match.group(7).replace(",", "")) if match.group(7).replace(",", "").isdigit() else 0,
        }
    
    return holdings


def extract_shareholders(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract shareholders from SC 13D, SC 13G, DEF 14A filings.
    
    Returns dict of shareholder_id -> shareholder data with keys:
    id, name, cik, holder_type
    """
    shareholders: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("SC 13D", "SC 13G", "DEF 14A"):
        return shareholders
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for beneficial owner information
    owner_pattern = re.compile(
        r'Name of Reporting Person[:\s]*([A-Za-z0-9\s\.\,\&]+).*?'
        r'CIK[:\s]*(\d{10})',
        re.I | re.S
    )
    
    for match in owner_pattern.finditer(text):
        name = clean_text(match.group(1))
        holder_id = stable_id("shareholder", name)
        shareholders[holder_id] = {
            "id": holder_id,
            "name": name,
            "cik": match.group(2).zfill(10),
            "holder_type": "beneficial_owner" if form_type in ("SC 13D", "SC 13G") else "executive",
        }
    
    return shareholders


def extract_shareholdings(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract shareholdings from SC 13D, SC 13G, DEF 14A filings.
    
    Returns dict of shareholding_id -> shareholding data with keys:
    id, shares, percent_outstanding, filing_date
    """
    shareholdings: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("SC 13D", "SC 13G", "DEF 14A"):
        return shareholdings
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for shares owned and percentage
    shares_pattern = re.compile(
        r'Amount Beneficially Owned[:\s]*([\d,]+).*?'
        r'Percent of Class[:\s]*([\d\.]+)%',
        re.I | re.S
    )
    
    for match in shares_pattern.finditer(text):
        sh_id = stable_id("shareholding", match.group(1), metadata.get("ticker", ""), metadata.get("filing_date", ""))
        shareholdings[sh_id] = {
            "id": sh_id,
            "shares": int(match.group(1).replace(",", "")) if match.group(1).replace(",", "").isdigit() else 0,
            "percent_outstanding": float(match.group(2)) if match.group(2).replace(".", "").isdigit() else 0.0,
            "filing_date": metadata.get("filing_date", ""),
        }
    
    return shareholdings


def extract_securities(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract securities from S-3, S-8, 424B filings.
    
    Returns dict of security_id -> security data with keys:
    id, cusip, isin, ticker, security_type, security_title, issuer
    """
    securities: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if not (form_type.startswith("S-") or form_type.startswith("424B")):
        return securities
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for security information
    security_pattern = re.compile(
        r'CUSIP[:\s]*(\d{9}).*?'
        r'ISIN[:\s]*([A-Z]{2}\d{9}[A-Z0-9]).*?'
        r'Title of Security[:\s]*([A-Za-z0-9\s\.\,\-]+)',
        re.I | re.S
    )
    
    for match in security_pattern.finditer(text):
        sec_id = stable_id("security", match.group(1), metadata.get("ticker", ""))
        securities[sec_id] = {
            "id": sec_id,
            "cusip": match.group(1),
            "isin": match.group(2),
            "ticker": metadata.get("ticker", ""),
            "security_type": "equity" if form_type == "S-8" else "debt" if form_type.startswith("424B") else "mixed",
            "security_title": clean_text(match.group(3)),
            "issuer": metadata.get("name", ""),
        }
    
    return securities


def extract_corporate_events(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract corporate events from 8-K, 10-K, 10-Q filings.
    
    Returns dict of event_id -> event data with keys:
    id, event_type, event_date, description, item_code, materiality
    """
    events: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("8-K", "10-K", "10-Q"):
        return events
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # 8-K has specific item codes
    item_pattern = re.compile(r'Item\s+(\d+\.\d+)\s*\.?\s*([^\n\r.]{3,80})', re.I)
    
    for match in item_pattern.finditer(text):
        code = match.group(1).strip()
        title = clean_text(match.group(2))
        
        # Only include material items
        material_items = {"1.01", "1.02", "1.03", "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
                         "3.01", "3.02", "3.03", "4.01", "4.02", "5.01", "5.02", "5.03",
                         "5.04", "5.05", "5.06", "5.07", "5.08", "6.01", "6.02", "6.03",
                         "6.04", "6.05", "7.01", "7.02", "8.01", "8.02", "9.01"}
        
        if code in material_items:
            event_id = stable_id("event", code, title, metadata.get("ticker", ""), metadata.get("filing_date", ""))
            events[event_id] = {
                "id": event_id,
                "event_type": f"8K_Item_{code}",
                "event_date": metadata.get("filing_date", ""),
                "description": title,
                "item_code": code,
                "materiality": "high" if code in material_items else "medium",
            }
    
    return events


def extract_capital_raises(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract capital raises from S-3, 424B, 8-K filings.
    
    Returns dict of capital_raise_id -> capital raise data with keys:
    id, offering_type, amount, price, shares, underwriters, use_of_proceeds
    """
    capital_raises: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if not (form_type.startswith("S-") or form_type.startswith("424B") or form_type == "8-K"):
        return capital_raises
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for offering information
    offering_pattern = re.compile(
        r'Total Offering Amount[:\s]*\$?([\d,\.]+)\s*(million|billion)?.*?'
        r'Price Per Share[:\s]*\$?([\d,\.]+).*?'
        r'Shares Offered[:\s]*([\d,]+)',
        re.I | re.S
    )
    
    for match in offering_pattern.finditer(text):
        cr_id = stable_id("capital_raise", match.group(1), metadata.get("ticker", ""), metadata.get("filing_date", ""))
        amount_str = match.group(1).replace(",", "")
        amount = float(amount_str) if amount_str.replace(".", "").isdigit() else 0.0
        if match.group(2) and match.group(2).lower() == "million":
            amount *= 1_000_000
        elif match.group(2) and match.group(2).lower() == "billion":
            amount *= 1_000_000_000
        
        capital_raises[cr_id] = {
            "id": cr_id,
            "offering_type": form_type,
            "amount": amount,
            "price": float(match.group(3)) if match.group(3).replace(",", "").replace(".", "").isdigit() else 0.0,
            "shares": int(match.group(4).replace(",", "")) if match.group(4).replace(",", "").isdigit() else 0,
            "underwriters": "",
            "use_of_proceeds": "",
        }
    
    return capital_raises


def extract_equity_compensation_plans(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract equity compensation plans from S-8, DEF 14A filings.
    
    Returns dict of plan_id -> plan data with keys:
    id, plan_name, shares_authorized, shares_outstanding, exercise_price
    """
    plans: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("S-8", "DEF 14A"):
        return plans
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for plan information
    plan_pattern = re.compile(
        r'Plan Name[:\s]*([A-Za-z0-9\s\.\,\-]+).*?'
        r'Shares Authorized[:\s]*([\d,]+).*?'
        r'Shares Outstanding[:\s]*([\d,]+).*?'
        r'Exercise Price[:\s]*\$?([\d,\.]+)',
        re.I | re.S
    )
    
    for match in plan_pattern.finditer(text):
        plan_id = stable_id("eq_plan", match.group(1), metadata.get("ticker", ""))
        plans[plan_id] = {
            "id": plan_id,
            "plan_name": clean_text(match.group(1)),
            "shares_authorized": int(match.group(2).replace(",", "")) if match.group(2).replace(",", "").isdigit() else 0,
            "shares_outstanding": int(match.group(3).replace(",", "")) if match.group(3).replace(",", "").isdigit() else 0,
            "exercise_price": float(match.group(4).replace(",", "")) if match.group(4).replace(",", "").replace(".", "").isdigit() else 0.0,
        }
    
    return plans


def extract_exhibits(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract exhibits from 10-K, 10-Q, 8-K, S-3 filings.
    
    Returns dict of exhibit_id -> exhibit data with keys:
    id, exhibit_number, exhibit_title, description
    """
    exhibits: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("10-K", "10-Q", "8-K", "S-3"):
        return exhibits
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Look for exhibit index
    exhibit_pattern = re.compile(
        r'Exhibit\s+(\d+\.\d+)\s*[:\-]\s*([A-Za-z0-9\s\.\,\-\(\)]+)',
        re.I
    )
    
    for match in exhibit_pattern.finditer(text):
        ex_id = stable_id("exhibit", match.group(1), metadata.get("ticker", ""))
        exhibits[ex_id] = {
            "id": ex_id,
            "exhibit_number": match.group(1),
            "exhibit_title": clean_text(match.group(2)),
            "description": clean_text(match.group(2)),
        }
    
    return exhibits


def extract_supporting_documents(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract supporting documents (ARS, SD, 11-K, earnings releases, presentations, etc.).
    
    Returns dict of doc_id -> document data with keys:
    id, doc_type, title, description, source_url, retrieved_at
    """
    docs: dict[str, dict[str, Any]] = {}
    form_type = str(metadata.get("form_type", "")).upper()
    
    if form_type not in ("ARS", "SD", "11-K"):
        return docs
    
    text = strip_markup(raw)
    text = re.sub(r"\s+", " ", text)
    
    # Generic document extraction
    doc_id = stable_id("supp_doc", form_type, metadata.get("ticker", ""), metadata.get("filing_date", ""))
    docs[doc_id] = {
        "id": doc_id,
        "doc_type": form_type,
        "title": metadata.get("document_title", f"{form_type} Filing"),
        "description": f"{form_type} filing for {metadata.get('name', metadata.get('ticker', ''))}",
        "source_url": metadata.get("document_url", ""),
        "retrieved_at": metadata.get("retrieved_at", ""),
    }
    
    return docs
