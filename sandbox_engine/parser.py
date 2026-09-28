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

import hashlib
import io
import logging
import re
import time
import warnings
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
)
_PERIOD_RE = re.compile(
    rf"(?:{_MONTHS})\s+\d{{1,2}}\s*,?\s*(?:19|20)\d{{2}}"       # September 27, 2025
    rf"|(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}"                        # 2025-09-27
    rf"|(?:19|20)\d{{2}}"                                        # bare year
    rf"|(?:Q[1-4]\s*)?(?:FY\s*)?(?:19|20)\d{{2}}",              # Q2 2026 / FY2025
    re.I,
)
#: A header cell that is pure decoration, not a period. Without this, the word
#: "Years" in a banner row is read as a period label.
_PERIOD_STOPWORDS = frozenset(
    {"year", "years", "ended", "as of", "months", "weeks", "date", "dates"}
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


@dataclass
class PeriodGroup:
    """A period and the columns that hold its values.

    ``duration`` comes from the banner row *above* the dates, and it is not
    decoration. See hazard 1 in the module docstring: without it a 10-Q's 3M and
    6M columns collapse into one metric.
    """

    label: str
    columns: list[int]
    duration: str = ""

    @property
    def key(self) -> str:
        return self.period_key(self.label)

    @property
    def full_key(self) -> str:
        """Period identity used for metric nodes.

        A duration-bearing period is keyed ``<duration>-FY<year>`` whether the
        header printed a bare year ("Years ended 2025") or a full date
        ("September 27, 2025"); both describe the same measurement, and keying
        them differently would store it twice under two names. An annual period
        is keyed plain ``FY<year>`` -- "FY-FY2025" would be a key nothing
        outside this file could guess at.

        A point-in-time column carries no duration banner and keeps its date,
        because a balance-sheet date is not recoverable from a year alone.
        """
        year = self.year
        if self.duration and year:
            return f"FY{year}" if self.duration == "FY" else f"{self.duration}-FY{year}"
        return self.key

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
        year = re.search(r"((?:19|20)\d{2})", self.key)
        return int(year.group(1)) if year else None


def detect_period_groups(
    frame: pd.DataFrame, scan: int = 6
) -> tuple[int, list[PeriodGroup]]:
    """Locate the header row and group its columns by period.

    Returns the row index too, because the duration banner lives in the rows
    *above* the dates.

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
        group.duration = _duration_above(frame, group, best_row)
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


def _groups_in_row(frame: pd.DataFrame, index: int) -> list[PeriodGroup]:
    row = [_cell(value) for value in frame.iloc[index].tolist()]
    if not any(_PERIOD_RE.search(text) for text in row):
        return []
    groups: list[PeriodGroup] = []
    for column, text in enumerate(row):
        if not _PERIOD_RE.search(text) or text.lower() in _PERIOD_STOPWORDS:
            continue
        # Merge into the previous group when the date repeats in an adjacent
        # column. The inline-XBRL generator emits the value twice across two
        # columns, and those are one measurement, not two.
        if groups and groups[-1].columns[-1] >= column - 1:
            if PeriodGroup.period_key(text) == PeriodGroup.period_key(groups[-1].label):
                groups[-1].columns.append(column)
                continue
        groups.append(PeriodGroup(label=text, columns=[column]))
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
    """One measured value: a row label, a period, and a number."""

    label: str
    period: str
    number: Number
    canonical_name: str
    category: str | None


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
    frame: pd.DataFrame, min_rows: int = 2
) -> tuple[str, list[TableCell]]:
    """Turn a statement table into ``(statement_category, cells)``.

    Only rows whose label resolves to a numeric measurement in a period column
    are kept. That is what discards sub-totals, headers repeated mid-table, and
    the layout filler these filings are full of.

    Fewer than two period groups returns nothing: a single-column table has no
    period to key a metric by, and storing it would produce an identity that
    collides with every other single-column table in the filing.
    """
    _, groups = detect_period_groups(frame)
    if len(groups) < 2:
        return "", []
    label_end = max(1, groups[0].columns[0])
    cells: list[TableCell] = []
    labels: list[str] = []

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
            canonical, category = canonical_metric(label, number.is_percent)
            if canonical:
                cells.append(
                    TableCell(
                        label=label,
                        period=group.full_key,
                        number=number,
                        canonical_name=canonical,
                        category=category,
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

    def counts(self) -> dict[str, int]:
        """Row count per table, for the run report and benchmark 1."""
        return {
            "metrics": len(self.metrics),
            "segments": len(self.segments),
            "events": len(self.events),
            "chunks": len(self.chunks),
            **{name: len(rows) for name, rows in self.edges.items()},
        }

    def merge(self, other: "ExtractionResult") -> None:
        """Merge another ExtractionResult into this one."""
        self.metrics.update(other.metrics)
        self.segments.update(other.segments)
        self.events.update(other.events)
        self.chunks.update(other.chunks)
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
_CIK_RE = re.compile(r"\b(\d{10})\b")
_CURRENCY_RE = re.compile(
    r"\b(USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY|HKD|INR|BRL|MXN|SEK|NOK)\b"
)
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
_HEADER_FOCUS_RE = re.compile(r"\b((?:19|20)\d{2})\s+(FY|Q[1-4])\b[^A-Za-z0-9]{0,12}\d{1,10}\b")
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

    def _metric_id(self, name: str, period: str) -> tuple[str, str]:
        """``(node_id, canonical_name)`` for a metric concept in *period*.

        Returns the *entity's* name, not the incoming surface form. An entity
        keeps the name it was created with and is never renamed, so two filings
        that spell the same concept differently must both write the canonical
        spelling -- otherwise the loader's first-write-wins would make the
        stored name depend on which file was read first.
        """
        resolution = self.registry.register(
            "metric", name, scope=period if PERIOD_SCOPED_METRICS else ""
        )
        entity = self.registry.partition("metric", period if PERIOD_SCOPED_METRICS else "").get(
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
            hidden, path, form_type, period_end
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

    def _period_end(self, raw: str, path: Path) -> str:
        """The filing's own period end, e.g. ``2025-09-27``."""
        dei = re.search(
            r"<(?:dei:)?DocumentPeriodEndDate[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*<", raw, re.I
        )
        if dei:
            return dei.group(1)
        # The document period's context is the one carrying only an end date.
        for match in re.finditer(
            r"<xbrli:context\b[^>]*>(?:(?!</xbrli:context>).)*?"
            r"<xbrli:instant>\s*(\d{4}-\d{2}-\d{2})\s*<",
            raw,
            re.S | re.I,
        ):
            return match.group(1)
        match = _EDGAR_NAME_RE.search(path.stem)
        if match:
            stamp = match.group(2)
            return f"20{stamp[0:2]}-{stamp[2:4]}-{stamp[4:6]}"
        return ""

    def _fiscal(
        self, hidden: str, path: Path, form_type: str, period_end: str
    ) -> tuple[int | None, str, str]:
        """Fiscal year and period, e.g. ``(2025, "FY")`` or ``(2026, "Q3")``.

        The document states its own focus in the inline-XBRL header, so no
        fiscal-calendar guesswork is needed. Falling back to "the largest year
        in the text" would be wrong: these filings quote bond maturities out to
        2042.
        """
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
        for frame in self.tables(raw):
            if frame is None or frame.empty:
                continue
            category, cells = extract_cells(frame)
            if not cells:
                continue
            for cell in cells:
                node_id, name = self._metric_id(cell.canonical_name, cell.period)
                metrics[node_id] = {
                    "id": node_id,
                    "canonical_name": name,
                    "statement_category": cell.category or category or "other",
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
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Reporting segments as ``Segment`` nodes, and the metric that owns each.

        A segment table has no statement line items of its own, so its figures
        hang off the revenue metric of the same period. That is the entire reason
        the ``Metric -> HAS_SEGMENT -> Segment`` shape exists.

        A segment's name goes through the shared registry rather than a
        per-filing ``setdefault``. Both the 10-K and the 10-Q carry a segment
        note, and ``setdefault`` is scoped to one filing, so each note minted its
        own copy of ``Americas``, ``iPhone`` and the rest -- 18 rows for 13
        segments. The registry is shared for the whole run, so the second filing's
        ``iPhone`` resolves to the node the first one created.
        """
        segments: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        frames = self.tables(raw)
        for index, frame in enumerate(frames):
            if frame is None or frame.empty:
                continue
            _, groups = detect_period_groups(frame)
            if len(groups) < 2:
                continue
            label_end = max(1, groups[0].columns[0])
            rows: list[tuple[str, PeriodGroup, Number]] = []
            for _, row in frame.iterrows():
                values = row.tolist()
                label = _label_of(values[:label_end])
                if not label or _PERIOD_RE.search(label):
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
                host_id, _ = self._metric_id("Net Sales", group.full_key)
                edges.append(
                    {
                        "value": float(number.value),
                        "period": group.full_key,
                        "segment": canonical,
                        "metric": host_id,
                    }
                )
        return segments, edges

    def _segment_name(self, label: str) -> str:
        """A segment label, or ``""`` if the row is not one.

        "Total net sales" is a subtotal, not a segment, and a percentage row is
        a share of a segment rather than a segment. Both are rejected so the
        ``Segment`` table only ever holds real taxonomy members.

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
        if _PERIOD_RE.search(text) or _PERCENT_RE.search(text):
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
        segments, segment_edges = self.extract_segments(raw, metadata)
        events = self.extract_events(raw, metadata)
        chunks = self.extract_chunks(raw, metadata)

        # A segment note's figures hang off the revenue metric of the same
        # period. If that period never appeared on a statement -- a segment note
        # may use a point-in-time key -- the host node has to exist anyway, or
        # the arc would be dropped as dangling by the loader. The host id comes
        # from the registry for the same reason the segment's does: a synthesised
        # "Net Sales" has to be the *same node* as a "Net Sales" the statement
        # reported, or the arc points at an orphan.
        for edge in segment_edges:
            host = edge["metric"]
            if host not in metrics:
                metrics[host] = {
                    "id": host,
                    "canonical_name": (
                        f"Net Sales ({edge['period']})"
                        if PERIOD_SCOPED_METRICS else "Net Sales"
                    ),
                    "statement_category": "income_statement",
                }

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

# 35 canonical metric seeds matching ingest_sandbox._METRIC_SEEDS
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
    event_date = metadata.get("period_end_date") or metadata.get("filing_date", "")

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
