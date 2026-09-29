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
    """
    return stable_id(
        "filing",
        metadata["ticker"],
        metadata["form_type"],
        metadata["fiscal_year"],
        metadata["fiscal_period"],
        metadata["filing_date"],
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
        }

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


def extract_suppliers(raw: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Placeholder for supplier extraction (DEPENDS_ON relationships).

    Currently returns empty list. Future implementation would parse
    supply chain disclosures from 10-K/10-Q/8-K.
    """
    # TODO: Implement supplier extraction from relevant sections
    return []
