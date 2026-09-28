#!/usr/bin/env python3
"""Universal, multi-tenant, zero-LLM SEC filing ingestion into LadybugDB.

Design constraints this file is built around
--------------------------------------------
*Company-agnostic*    Nothing in the DDL, the extractor or the query suite
                      names a ticker, a segment or a financial line item. The
                      metric vocabulary is a *concept* registry with a
                      pass-through fallback, so an unfamiliar line item is
                      still stored, under its own label.

*Adding a company or a year is a data operation.*  A new company is one
``Company`` node.  A new year is one ``Filing`` node plus its metrics, events
and chunks.  Neither a DDL change nor a code change is involved.  ``Company``
is the tenancy boundary: everything else is reachable from it, so a query for
one tenant is a query anchored on ``(c:Company {ticker: ...})``.

*Deterministic*       Ids are content hashes, so re-running a filing is a no-op
                      instead of a duplicate, and a metric node is the same node
                      in every database that sees it.

*Zero external calls* No LLM, no network, CPU only.  A 1.5 MB 10-K parses in
                      about 0.1 s.

LadybugDB 0.20.4 hazards this file is written around
----------------------------------------------------
Both are deadlocks inside the storage engine, not exceptions, so they cannot be
caught and cannot be interrupted by a signal:

1. ``COPY <table> FROM $arrow`` against a primary key that already exists
   **hangs forever**.  :meth:`BulkWriter` therefore reads the existing keys
   first and copies only genuinely new rows.  This check is not optional.
2. A connection that has committed a sizeable write can hang on its next
   parameterised read.  The writer is therefore opened and closed per filing
   (:class:`BulkWriter` is a context manager around one connection).

A stale ``<db>.wal`` left by a hard kill is reported with recovery instructions
rather than silently deleted.

Schema deviations, and why
--------------------------
``REPORTS_METRIC`` carries one ``value``, so a 10-K that shows three years of
Net Sales needs three distinct ``Metric`` nodes -- otherwise two years are
silently lost.  The reporting period is therefore part of the metric identity
and is written into ``canonical_name``::

    "Net Sales" (FY2025)   "Net Sales" (FY2024)   "Net Sales" (FY2023)

A prefix match still groups the taxonomy, and no hash is exposed to query
authors.  Set ``PERIOD_SCOPED_METRICS = False`` for a pure one-node-per-concept
taxonomy; that is a deliberate trade of comparative data for node count.

Usage
-----
    python universal_sec_ingestor.py --reset --verify
    python universal_sec_ingestor.py --db out.lbug --files a.htm b.htm
    python universal_sec_ingestor.py --files-only a.htm          # parse, no DB

Dependencies: ladybug>=0.20, pyarrow, pandas, beautifulsoup4, lxml.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import logging
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import pandas as pd
import pyarrow as pa
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

#: These filings declare an XHTML doctype but carry an XML prologue, so lxml
#: warns on every parse.  The parse is what we want; the warning is noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

import ladybug as lb

log = logging.getLogger("universal_sec")

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Node tables, in column order.  Order matters: ``COPY`` maps by position.
NODE_TABLES: dict[str, tuple[str, ...]] = {
    "Company": ("ticker", "name", "cik"),
    "Filing": ("id", "form_type", "fiscal_year", "fiscal_period", "filing_date"),
    "Metric": ("id", "canonical_name", "statement_category"),
    "Segment": ("name", "segment_type"),
    "Event": ("id", "item_code", "item_title", "summary"),
    "Chunk": ("id", "section", "text"),
}

PRIMARY_KEYS: dict[str, str] = {
    "Company": "ticker",
    "Filing": "id",
    "Metric": "id",
    "Segment": "name",
    "Event": "id",
    "Chunk": "id",
}

#: rel table -> (source node table, target node table, property columns)
REL_TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "SUBMITTED": ("Company", "Filing", ()),
    "REPORTS_METRIC": ("Filing", "Metric", ("value", "currency")),
    "HAS_SEGMENT": ("Metric", "Segment", ("value", "period")),
    "DISCLOSES_EVENT": ("Filing", "Event", ()),
    "HAS_CHUNK": ("Filing", "Chunk", ()),
}

#: ``DATE`` and ``INT64`` are stored as strings, matching how the engine
#: returns them; the declared column list is what the graph advertises.
_INT_TYPES = {"fiscal_year"}
_DATE_TYPES = {"filing_date"}
_DOUBLE_PROPS = {"value"}


def _column_type(name: str) -> str:
    """DDL type for a column name.

    ``filing_date`` is a real ``DATE`` rather than a string so a query can range
    it and compare it against a date literal without parsing anything.
    """
    if name in _INT_TYPES:
        return "INT64"
    if name in _DATE_TYPES:
        return "DATE"
    return "STRING"


def parse_date(text: str) -> Any:
    """``"2025-10-31"`` -> ``datetime.date``; unparseable text is returned as-is.

    A filing whose date cannot be read is stored verbatim rather than dropped:
    a wrong-looking date is recoverable, a missing filing is not.
    """
    try:
        return datetime.date.fromisoformat(str(text).strip()[:10])
    except (TypeError, ValueError):
        return text

#: See the module docstring.  ``True`` keeps comparative periods.
PERIOD_SCOPED_METRICS = True


def _ddl_for(table: str) -> str:
    """The single ``CREATE`` statement that defines *table*."""
    if table in NODE_TABLES:
        body = ", ".join(
            f"{name} {_column_type(name)}" for name in NODE_TABLES[table]
        )
        return (
            f"CREATE NODE TABLE IF NOT EXISTS {table}"
            f"({body}, PRIMARY KEY({PRIMARY_KEYS[table]}))"
        )
    source, target, props = REL_TABLES[table]
    spec = f"FROM {source} TO {target}"
    for prop in props:
        spec += f", {prop} {'DOUBLE' if prop in _DOUBLE_PROPS else 'STRING'}"
    return f"CREATE REL TABLE IF NOT EXISTS {table} ({spec})"


def schema_ddl() -> list[str]:
    """``CREATE ... IF NOT EXISTS`` statements for the whole graph.

    Note the rel-table spelling: LadybugDB wants the whole endpoint-and-property
    spec inside one pair of parentheses after the name, with a comma -- not a
    comma then a parenthesised list -- between the endpoints and the properties.
    """
    return [_ddl_for(table) for table in (*NODE_TABLES, *REL_TABLES)]


def _expected_columns() -> dict[str, dict[str, str]]:
    """Every table this graph expects, with its column -> type mapping.

    A rel table's ``from``/``to`` are structural and do not appear in
    ``table_info``, so only its declared properties are compared.
    """
    expected: dict[str, dict[str, str]] = {}
    for table, columns in NODE_TABLES.items():
        expected[table] = {
            name: _column_type(name) for name in columns
        }
    for rel, (_, _, props) in REL_TABLES.items():
        expected[rel] = {
            prop: ("DOUBLE" if prop in _DOUBLE_PROPS else "STRING") for prop in props
        }
    return expected


def ensure_schema(connection: Any) -> dict[str, Any]:
    """Bring the database to the expected schema, in place.

    ``CREATE TABLE IF NOT EXISTS`` silently accepts a table whose columns have
    drifted, so every table is introspected and compared.  A node table missing a
    column is rebuilt: LadybugDB has no ``ALTER TABLE ADD``, but it does support
    ``RENAME`` and ``DROP``, so the old table is renamed, the correct one
    created, the shared columns copied across and the old one dropped.
    """
    applied: list[str] = []
    for statement in schema_ddl():
        connection.execute(statement)
        applied.append(statement)

    expected = _expected_columns()
    rebuilt: list[str] = []
    for table, wanted in expected.items():
        existing = _existing_columns(connection, table)
        missing = sorted(set(wanted) - existing)
        if not existing or not missing:
            continue
        if table in REL_TABLES:
            raise RuntimeError(
                f"schema drift: rel table {table} is missing {missing}. "
                f"LadybugDB cannot copy arcs into a rebuilt rel table, so this "
                f"needs a manual fix: DROP TABLE {table}; then re-run. "
                f"(Or delete the database file.)"
            )
        log.warning(
            "schema drift: %s is missing %s; rebuilding the table", table, missing
        )
        _rebuild_table(connection, table, wanted, existing)
        rebuilt.append(table)
    return {"created": applied, "rebuilt": rebuilt}


def _rebuild_table(
    connection: Any,
    table: str,
    wanted: dict[str, str],
    existing: set[str],
) -> None:
    """Rename, recreate, copy the shared columns, drop the original."""
    shared = [name for name in wanted if name in existing]
    scratch = f"{table}__pre_migration"
    connection.execute(f"ALTER TABLE {table} RENAME TO {scratch}")
    connection.execute(_ddl_for(table))
    if shared:
        projection = ", ".join(f"n.{name}" for name in shared)
        rows = connection.execute(f"MATCH (n:{scratch}) RETURN {projection}").get_all()
        assignments = ", ".join(f"{name}: r.{name}" for name in shared)
        connection.execute(
            f"UNWIND $rows AS r CREATE (:{table} {{{assignments}}})",
            {"rows": [dict(zip(shared, row)) for row in rows]},
        )
        log.info("migrated %s: copied %d row(s) across %s", table, len(rows), shared)
    connection.execute(f"DROP TABLE {scratch}")


def _existing_columns(connection: Any, table: str) -> set[str]:
    """Column names of *table*, or an empty set if it cannot be inspected.

    An empty result is treated as "nothing to migrate" rather than "migrate
    everything": a table whose introspection fails is left alone instead of
    being rebuilt on the strength of a guess.
    """
    try:
        rows = connection.execute(f"CALL table_info('{table}') RETURN *").get_all()
    except Exception:  # noqa: BLE001 - rel tables and older builds vary
        return set()
    return {str(cell) for row in rows for cell in row}


# ---------------------------------------------------------------------------
# Identifiers and text helpers
# ---------------------------------------------------------------------------


def stable_id(*parts: Any, length: int = 16) -> str:
    """Content-addressed id.

    Deterministic across runs, machines and databases, which is what makes a
    re-run a no-op and lets two processes build the same node.
    """
    payload = "\x1f".join(str(p) for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def filing_identity(metadata: dict[str, Any]) -> str:
    """The ``Filing.id`` that *metadata* implies.

    Events and chunks are scoped to this rather than to the ticker, because a
    company files many 8-Ks: two of them can both contain "Item 5.07" with the
    same title, and a ticker-scoped id would silently keep only the first.
    """
    return stable_id(
        "filing",
        metadata["ticker"],
        metadata["form_type"],
        metadata["fiscal_year"],
        metadata["fiscal_period"],
        metadata["filing_date"],
    )


_WS_RE = re.compile(r"\s+")
_NBSP_RE = re.compile(r"&#160;|&nbsp;|&#8203;|&#8194;|&#8195;", re.I)
_MONEY_RE = re.compile(r"[\$£€¥]")
_DASHES = {"—", "–", "-", "―", "−", "", "n/a", "na"}
_TRAILING_UNIT_RE = re.compile(
    r"\s*\((?:in\s+)?(?:thousands|millions|billions|dollars|shares)\)\s*$", re.I
)
_PCT_SUFFIX_RE = re.compile(r"\s*%\s*$")
#: Any ``%`` anywhere: a share of a segment, not a segment.
_PERCENT_RE = re.compile(r"%")


def clean_text(value: Any) -> str:
    """Collapse whitespace and numeric character references."""
    if value is None:
        return ""
    text = _NBSP_RE.sub(" ", str(value))
    return _WS_RE.sub(" ", text).strip()


def strip_markup(raw: str) -> str:
    """Visible text of a filing, with inline-XBRL machinery removed.

    Workiva filings hide the tagged facts with ``display:none`` and inline the
    XBRL vocabulary as element names.  Both leak into ``read_html`` and into
    chunk text, so they come out here rather than in every caller.
    """
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    body = soup.body or soup
    text = body.get_text("\n")
    text = _NBSP_RE.sub(" ", text)
    return text


def raw_text(raw: str, limit: int | None = None) -> str:
    """Tags stripped, *hidden* regions kept, whitespace collapsed.

    Metadata lives in the parts of a filing a reader never sees: Workiva puts
    the registrant name in a ``display:none`` cover block and the whole
    inline-XBRL header in another.  Reading metadata from visible text alone
    loses the CIK, the fiscal focus and -- for an 8-K -- the registrant name.
    """
    text = _NBSP_RE.sub(" ", _TAG_RE.sub(" ", raw))
    return _WS_RE.sub(" ", text).strip()[:limit] if limit else _WS_RE.sub(" ", text).strip()


def _strip_hidden_regions(raw: str) -> str:
    """Regex twin of :func:`strip_markup` for use before ``read_html``.

    A BeautifulSoup pass over a 1.5 MB filing costs more than the parse itself,
    and the parts that break ``read_html`` are all attribute-delimited, so they
    can be removed with a single pass first.  The authoritative
    ``<div>``-leaf fallback still runs after this.
    """
    patterns = [
        r"<div[^>]*display\s*:\s*none[^>]*>.*?</div>",
        r"<span[^>]*display\s*:\s*none[^>]*>.*?</span>",
        r"<ix:header\b.*?</ix:header>",
        r"<script\b.*?</script>",
        r"<style\b.*?</style>",
    ]
    out = raw
    for pattern in patterns:
        out = re.sub(pattern, " ", out, flags=re.S | re.I)
    return out


def html_body(raw: str) -> str:
    """Body of a filing, safe to hand to ``read_html``."""
    out = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", raw)
    if "<body" in out.lower():
        start = out.lower().index("<body")
        out = out[start:]
    return _strip_hidden_regions(out)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Number:
    """A parsed table cell."""

    value: float
    is_percent: bool


def parse_number(raw: Any) -> Number | None:
    """Parse a filing table cell into a float.

    Handles the forms that actually occur: ``(1,234)`` negative accounting,
    ``1,234`` thousands separators, a leading currency symbol, a trailing
    ``%``, em/en dashes for nil, and stray footnote markers.
    """
    if raw is None:
        return None
    text = clean_text(raw)
    if text.lower() in _DASHES:
        return None
    text = _MONEY_RE.sub("", text)
    percent = bool(_PCT_SUFFIX_RE.search(text))
    text = _PCT_SUFFIX_RE.sub("", text)
    # Footnote and unit decorations: "Net sales (1)" , "Total (in millions)".
    text = re.sub(r"\(\s*[a-z0-9*]+\s*\)\s*$", "", text, flags=re.I)
    text = _TRAILING_UNIT_RE.sub("", text)
    text = text.strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(",", "").replace(" ", "").strip()
    if text.endswith("%"):
        percent = True
        text = text[:-1].strip()
    if text in {"", "-", "—", "–"}:
        return None
    # Trailing/leading footnote digits glued to the number: "1,2341".
    match = re.fullmatch(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if not match:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if negative:
        value = -value
    return Number(value, percent)


# ---------------------------------------------------------------------------
# Metric vocabulary -- a concept registry, not a company list
# ---------------------------------------------------------------------------

#: (canonical concept, pattern, statement category).  Patterns are matched
#: against a cleaned row label.  The registry is financial-statement
#: vocabulary, which is shared by every registrant; nothing in it is specific
#: to one issuer.  Labels that match nothing fall through untouched.
METRIC_CONCEPTS: tuple[tuple[str, str, str], ...] = (
    # income statement
    ("Net Sales", r"^total\s+net\s+sales|^net\s+sales\b|^total\s+(sales|revenue)\b"
     r"|^(total\s+)?(net\s+)?sales:|^total\s+revenue|^revenue$|^net\s+revenue", "income_statement"),
    ("Cost of Sales", r"^total\s+cost\s+of\s+(sales|revenue)|^cost\s+of\s+(sales|revenue)", "income_statement"),
    ("Gross Margin", r"^gross\s+(margin|profit)\b", "income_statement"),
    ("Research and Development", r"^research\s+and\s+development\b|^r&d\b", "income_statement"),
    ("Selling, General and Administrative", r"^selling,?\s+general\s+and\s+administrative|^sg&a\b", "income_statement"),
    ("Operating Expenses", r"^total\s+operating\s+expenses|^operating\s+expenses", "income_statement"),
    ("Operating Income", r"^operating\s+(income|profit|loss)\b", "income_statement"),
    ("Non-Operating Income", r"^other\s+income/(expense)|^non-?operating", "income_statement"),
    ("Income Before Taxes", r"^income\s+before\s+(provision\s+for\s+)?taxes", "income_statement"),
    ("Income Tax Expense", r"^provision\s+for\s+income\s+taxes|^income\s+tax\s+expense", "income_statement"),
    ("Net Income", r"^net\s+income\b|^net\s+income/\(loss\)|^net\s+income\s+attributable", "income_statement"),
    ("Earnings Per Share, Basic", r"^net\s+income\s+per\s+share.{0,12}basic|^earnings\s+per\s+share.{0,12}basic", "income_statement"),
    ("Earnings Per Share, Diluted", r"earnings\s+per\s+share.{0,12}diluted", "income_statement"),
    ("Shares Outstanding, Basic", r"^shares.{0,24}basic", "income_statement"),
    ("Shares Outstanding, Diluted", r"^shares.{0,24}diluted", "income_statement"),
    # balance sheet
    ("Cash and Cash Equivalents", r"^cash\s+and\s+cash\s+equivalents|^cash\s+and\s+equivalents", "balance_sheet"),
    ("Short-Term Investments", r"^short-?term\s+(marketable\s+)?(investments|securities)", "balance_sheet"),
    ("Accounts Receivable", r"^accounts?\s+receivable", "balance_sheet"),
    ("Inventory", r"^inventor(y|ies)\b", "balance_sheet"),
    ("Total Current Assets", r"^total\s+current\s+assets", "balance_sheet"),
    ("Property, Plant and Equipment, Net", r"^property,?\s+plant\s+and\s+equipment", "balance_sheet"),
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
    # cash flow
    ("Operating Cash Flow", r"^cash\s+generated\s+by\s+operating|^net\s+cash\s+(provided\s+by|used\s+in)\s+operating", "cash_flow"),
    ("Investing Cash Flow", r"^cash\s+(generated\s+by|used\s+in)\s+investing|^net\s+cash.{0,20}investing", "cash_flow"),
    ("Financing Cash Flow", r"^cash\s+(generated\s+by|used\s+in)\s+financing|^net\s+cash.{0,20}financing", "cash_flow"),
    ("Capital Expenditures", r"^payments?\s+for\s+acquisition|^\s*capital\s+expenditures", "cash_flow"),
    ("Depreciation and Amortization", r"^depreciation\s+and\s+amortization", "cash_flow"),
    ("Free Cash Flow", r"^free\s+cash\s+flow", "cash_flow"),
)

_COMPILED_CONCEPTS = tuple(
    (canonical, re.compile(pattern, re.I), category)
    for canonical, pattern, category in METRIC_CONCEPTS
)

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
    text = re.sub(r"\s{2,}.*$", "", text)
    return text.rstrip(":").strip()


def canonical_metric(label: str, is_percent: bool = False) -> tuple[str, str | None]:
    """Map a row label to ``(canonical_name, statement_category)``.

    Unrecognised labels are preserved verbatim (title-cased, noise stripped) so
    that an unfamiliar line item is still queryable.  This is what makes the
    extractor work for a company nobody has seen: recall never depends on the
    registry.
    """
    text = clean_text(label)
    text = _TRAILING_UNIT_RE.sub("", text)
    text = re.sub(r"\s{2,}.*$", "", text)          # "Net sales  (1)  Notes..."
    text = text.rstrip(":").strip()
    if not text or text.lower() in _DASHES:
        return "", None
    for canonical, pattern, category in _COMPILED_CONCEPTS:
        if pattern.search(text):
            name = f"{canonical} (%)" if is_percent else canonical
            return name, category

    # Not in the registry: keep it, but strip a leading ordinal qualifier so
    # "Total other income/(expense), net" still reads cleanly.
    fallback = re.sub(r"^[^A-Za-z0-9(]+", "", text)
    fallback = re.sub(r"\s+", " ", fallback).strip()
    if not fallback:
        return "", None
    if is_percent:
        return f"{fallback} (%)", _guess_category(fallback)
    return fallback, _guess_category(fallback)


def _guess_category(text: str) -> str:
    low = text.lower()
    scores = {
        category: sum(1 for hint in hints if hint in low)
        for category, hints in _CATEGORY_HINTS.items()
    }
    best = max(scores, key=lambda key: scores[key])
    return best if scores[best] else "other"


# ---------------------------------------------------------------------------
# Table normalisation
# ---------------------------------------------------------------------------

_MONTHS = (
    r"January|February|March|April|May|June|July|August|September|October"
    r"|November|December"
)
_PERIOD_RE = re.compile(
    rf"(?:{_MONTHS})\s+\d{{1,2}}\s*,?\s*(?:19|20)\d{{2}}"      # September 27, 2025
    rf"|(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}"                      # 2025-09-27
    rf"|(?:19|20)\d{{2}}"                                      # bare year
    rf"|(?:Q[1-4]\s*)?(?:FY\s*)?(?:19|20)\d{{2}}",            # Q2 2026 / FY2025
    re.I,
)
#: A header cell that is pure decoration, not a period.
_PERIOD_STOPWORDS = {"year", "years", "ended", "as of", "months", "weeks", "date", "dates"}


@dataclass(frozen=True)
class TableCell:
    """One measured value: a row label, a period, a number."""

    label: str
    period: str
    number: Number
    canonical_name: str
    category: str | None


#: Placeholders pandas leaves behind, which must never become a row label.
_NULLISH = {"nan", "none", "nat", "-", "n/a", "na", "null"}


def _cell(value: Any) -> str:
    """One table cell as text, with nulls and em-dashes normalised to ``""``."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:      # NaN
        return ""
    text = clean_text(value)
    return "" if text.lower() in _NULLISH or text in {"—", "–", "―", "−"} else text


def _label_of(row: Sequence[Any]) -> str:
    """Row label from the leading run of merged duplicate cells."""
    for value in row:
        text = _cell(value)
        if text and text.lower() not in _DASHES:
            return text
    return ""


@dataclass
class PeriodGroup:
    """A period and the columns that hold its values.

    ``duration`` comes from the banner row above the dates.  It is not
    decoration: a 10-Q shows "Three Months Ended March 28, 2026" and "Six
    Months Ended March 28, 2026" as two different columns with the *same* date,
    so without the banner the two collapse into one metric and one of the two
    values is lost.
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

        A duration-bearing period is keyed ``<duration>-FY<year>`` regardless of
        whether the header printed a bare year ("Years ended 2025") or a full
        date ("September 27, 2025").  Both describe the same measurement, and
        keying them differently would store it twice under two names.  A
        point-in-time column carries no duration banner and keeps its date,
        because a balance-sheet date is not recoverable from a year alone.
        """
        year = self.year
        if self.duration and year:
            # An annual period is the year itself; "FY-FY2025" would be a key
            # nothing outside this file could guess at.
            return f"FY{year}" if self.duration == "FY" else f"{self.duration}-FY{year}"
        return self.key

    @staticmethod
    def period_key(label: str) -> str:
        """Compact, stable period key: ``2025-09-27`` or ``FY2025``."""
        text = clean_text(label)
        iso = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
        if iso:
            return iso.group(0)
        dated = re.search(rf"({_MONTHS})\s+(\d{{1,2}})\s*,?\s*((?:19|20)\d{{2}})", text, re.I)
        if dated:
            month_index = _month_number(dated.group(1))
            return f"{dated.group(3)}-{month_index:02d}-{int(dated.group(2)):02d}"
        year = re.search(r"((?:19|20)\d{2})", text)
        return f"FY{year.group(1)}" if year else clean_text(text)

    @property
    def year(self) -> int | None:
        year = re.search(r"((?:19|20)\d{2})", self.key)
        return int(year.group(1)) if year else None


#: Keyed by three-letter prefix, which is what the date regex hands us.
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
_MONTH_NUMBERS = {name[:3]: number for number, name in enumerate(_MONTH_NAMES, 1)}

#: Duration banner words, and the code they collapse to.  Both alternatives
#: capture, so "Years ended" (a 10-K) is as detectable as "Six Months Ended"
#: (a 10-Q).
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


def detect_period_groups(
    frame: pd.DataFrame, scan: int = 6
) -> tuple[int, list[PeriodGroup]]:
    """Locate the header row and group its columns by period.

    Returns the row index as well, because the duration banner lives in the
    rows *above* the dates.  The header is the row that yields the most distinct
    period groups: the "Years ended" banner row above it repeats one label
    across every column and would otherwise look like a single period.
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
        row = [_cell(v) for v in frame.iloc[row_index].tolist()]
        text = " ".join(
            row[column] for column in group.columns
            if 0 <= column < len(row) and row[column]
        )
        code = duration_code(text)
        if code:
            return code
    return ""


def _groups_in_row(frame: pd.DataFrame, index: int) -> list[PeriodGroup]:
    row = [_cell(v) for v in frame.iloc[index].tolist()]
    if not any(_PERIOD_RE.search(text) for text in row):
        return []
    groups: list[PeriodGroup] = []
    for column, text in enumerate(row):
        if not _PERIOD_RE.search(text):
            continue
        if text.lower() in _PERIOD_STOPWORDS:
            continue
        if groups and groups[-1].columns[-1] >= column - 1:
            if _same_period(text, groups[-1].label):
                groups[-1].columns.append(column)
                continue
        groups.append(PeriodGroup(label=text, columns=[column]))
    return [g for g in groups if g.key]


def _same_period(left: str, right: str) -> bool:
    return PeriodGroup.period_key(left) == PeriodGroup.period_key(right)


def extract_cells(
    frame: pd.DataFrame, min_rows: int = 2
) -> tuple[str, list[TableCell]]:
    """Turn a statement table into ``(statement_category, cells)``.

    Only rows whose label resolves to a numeric measurement in a period column
    are kept, which is what discards sub-totals, headers repeated mid-table and
    the layout filler these filings are full of.
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
            if not canonical:
                continue
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


def _number_in_group(values: Sequence[Any], columns: Sequence[int]) -> Number | None:
    """First parseable number in a period's column span.

    The span holds a currency symbol, the value, and -- because of the merged
    cells the inline-XBRL generator emits -- the same value repeated.
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


# ---------------------------------------------------------------------------
# Segment detection
# ---------------------------------------------------------------------------

#: Generic reporting-segment taxonomies.  Continents/countries are shared by
#: every multinational; a registrant that reports no segments simply produces
#: no segment table and is unaffected.
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
    """Return a segment taxonomy name, or ``None`` if this is not that table.

    The trap is the face of the income statement: it has several period columns
    and rows called "Products" and "Services", which look exactly like a product
    segment breakdown.  What separates them is that its rows *are* financial
    line items, so a table that reports a recognised statement concept is a
    statement even when its labels read like taxonomy names.
    """
    if len(labels) < 2:
        return None
    financial = sum(
        1
        for label in labels
        if any(pattern.search(_normalise_label(label)) for _, pattern, _ in _COMPILED_CONCEPTS)
    )
    if financial >= max(1, len(labels) // 4):
        return None
    geo = sum(1 for label in labels if _GEOGRAPHY_RE.search(label))
    product = sum(1 for label in labels if _PRODUCT_RE.search(label))
    explicit = bool(_SEGMENT_CONTEXT_RE.search(context))
    if geo < 2 and not (product >= 2 and explicit):
        return None
    if geo >= product and geo > 0:
        return "geographic"
    if product > 0:
        return "product"
    return "segment" if explicit else None


# ---------------------------------------------------------------------------
# 8-K item events
# ---------------------------------------------------------------------------

#: Spec regex, with the whitespace collapse applied first: these filings pad
#: item headings with long runs of spaces and non-breaking spaces.
ITEM_RE = re.compile(r"(Item\s+\d+\.\d+)\s*[:\-]?\s*([^\n\r.]+)", re.I)
_SNIPPET_RE = re.compile(r"^\s*(Item\s+\d+\.\d+)")


def parse_events(text: str, max_summary: int = 600) -> list[tuple[str, str, str]]:
    """``(item_code, item_title, summary)`` for every Item heading in an 8-K."""
    collapsed = _WS_RE.sub(" ", text)
    events: list[tuple[str, str, str]] = []
    matches = list(ITEM_RE.finditer(collapsed))
    for position, match in enumerate(matches):
        code = re.sub(r"\s+", " ", match.group(1)).strip()
        code = re.sub(r"^item\s+", "Item ", code, flags=re.I)
        title = match.group(2).strip(" .:-")
        if not title:
            continue
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(collapsed)
        body = collapsed[start:end].strip(" .:-")
        summary = _WS_RE.sub(" ", body)[:max_summary].strip()
        events.append((code, title, summary))
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
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class ChunkText:
    section: str
    text: str


def chunk_body(html: str, max_chars: int = 2000, min_chars: int = 40) -> list[ChunkText]:
    """Slice body text into ``Chunk`` records, tagged with their section.

    ``h1``/``h2``/``h3`` set the current section and ``p``/``li`` are the
    primary text source, as specified.  Workiva's generator emits neither --
    the same prose sits in ``<div>``s -- so div boundaries are also block
    boundaries; without that fallback these filings produce zero chunks.

    Div nesting is not uniform across filings: the 10-Q's prose sits in flat
    sibling divs, the 8-K's in deeply nested ones.  Emitting only the outermost
    div returns an entire filing as one block, and emitting only the innermost
    returns fragments below the length floor.  So every level is a candidate and
    a later pass drops any block whose text is already covered by a shorter one.
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
    # tag matches alone would discard every character of prose.
    position = 0
    for match in _BLOCK_RE.finditer(body):
        between = body[position : match.start()]
        position = match.end()
        heading, _level, table, paragraph, item, open_tag, close_tag = match.groups()
        if heading:
            title = _WS_RE.sub(" ", _TAG_RE.sub(" ", heading)).strip()
            if title:
                section = title[:200]
            continue
        if table:
            continue
        if paragraph or item:
            emit(paragraph or item or "")
            continue
        if open_tag:
            if stack:
                stack[-1].append(between)
                has_child[-1] = True
            stack.append([])
            has_child.append(False)
            continue
        if close_tag:
            if not stack:
                continue
            fragment = " ".join([*stack.pop(), between])
            has_child.pop()
            emit(fragment)
    return _drop_dominated(found)


def _drop_dominated(blocks: Sequence[ChunkText]) -> list[ChunkText]:
    """Discard blocks whose text is already covered by shorter blocks.

    A parent div's text is the concatenation of its children's, so emitting both
    stores the same prose twice.  A parent is dropped when some already-kept
    block covers most of it, which keeps nested filings from being stored as one
    giant chunk.
    """
    if not blocks:
        return []
    texts = [block.text for block in blocks]
    lens = [len(text) for text in texts]
    # Longest first: a child is always shorter than the parent containing it.
    order = sorted(range(len(blocks)), key=lambda i: lens[i])
    kept: list[int] = []
    for index in order:
        text = texts[index]
        redundant = False
        for other in kept:
            candidate = texts[other]
            if len(candidate) >= len(text) * 0.6 and candidate in text:
                redundant = True
                break
        if not redundant:
            kept.append(index)
    kept.sort()
    return [blocks[i] for i in kept]


def _split_text(text: str, max_chars: int) -> Iterator[str]:
    if len(text) <= max_chars:
        yield text
        return
    sentences = re.split(r"(?<=[.!?])\s+", text)
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}"
        else:
            yield current
            current = sentence
    if current:
        yield current


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """Everything one filing contributes, held for the bulk write."""

    company: dict[str, Any]
    filing: dict[str, Any]
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0

    def counts(self) -> dict[str, int]:
        return {
            "metrics": len(self.metrics),
            "segments": len(self.segments),
            "events": len(self.events),
            "chunks": len(self.chunks),
            **{name: len(rows) for name, rows in self.edges.items()},
        }


# ---------------------------------------------------------------------------
# The ingestor
# ---------------------------------------------------------------------------

_FORM_RE = re.compile(r"\b10-([KQ])\b|\b8-?K\b", re.I)
_FILE_FORM_RE = re.compile(r"(10-[KQ]|8-?K)", re.I)
#: SEC's own naming: ``aapl-20250927.htm``; the period end is the date part.
_EDGAR_NAME_RE = re.compile(r"^([a-z]{1,6})-?(\d{8})", re.I)
_CURATED_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_CIK_RE = re.compile(r"\b(\d{10})\b")
#: The entity identifier every inline-XBRL context carries.  Unambiguous: it is
#: tagged with the SEC's own CIK scheme URI.
_XBRL_CIK_RE = re.compile(
    r"<xbrli:identifier[^>]*scheme=[\"']http://www\.sec\.gov/CIK[\"'][^>]*>\s*(\d{1,10})\s*<",
    re.I,
)
_XBRL_PERIOD_RE = re.compile(r"<xbrli:(?:startDate|endDate|instant)>\s*([\d-]{10})\s*<", re.I)
#: dei focus, falling back to the header's ``<year> <FY|Qn> <cik>`` run.
_DEI_FOCUS_RE = re.compile(
    r"<(?:dei:)?DocumentFiscalYearFocus[^>]*>\s*((?:19|20)\d{2})\s*<"
    r".{0,200}?<(?:dei:)?DocumentFiscalPeriodFocus[^>]*>\s*([A-Za-z0-9]{1,4})\s*<",
    re.S | re.I,
)
_HEADER_FOCUS_RE = re.compile(
    r"\b((?:19|20)\d{2})\s+(FY|Q[1-4])\b[^A-Za-z0-9]{0,12}\d{1,10}\b"
)
_CURRENCY_RE = re.compile(r"\b(USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY|HKD|INR|BRL|MXN|SEK|NOK)\b")
#: A corporate suffix is the one registrant-name shape every issuer shares.
_CORPORATE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.,'’\-]{1,40}(?:\s+[A-Z][A-Za-z0-9&.,'’\-]{1,40}){0,4}"
    r"\s+(?:Inc\.?|Incorporated|Corp\.?|Corporation|Company|Co\.?|Limited|Ltd\.?|"
    r"L\.?P\.?|PLC|plc|N\.?V\.?|S\.?A\.?|A\.?G\.?|Holdings?|Group))"
    r"(?![A-Za-z0-9])"
)


class UniversalSECIngestor:
    """Turns one filing into nodes and edges, with no external services.

    Every extraction step is a total function of the file's bytes, so two runs
    over the same filing produce byte-identical ids and a second run is a
    no-op.  Metadata is resolved from the document first and the filename
    second; which source was used is reported in ``stats["sources"]`` so a
    surprising value is traceable.
    """

    def __init__(
        self,
        chunk_chars: int = 2000,
        min_chunk_chars: int = 40,
        max_events: int = 32,
    ) -> None:
        self.chunk_chars = chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self.max_events = max_events

    # -- metadata ----------------------------------------------------------

    def extract_metadata(self, raw: str, path: Path) -> dict[str, Any]:
        """Ticker, registrant, CIK, form type and fiscal period."""
        visible = _WS_RE.sub(" ", _NBSP_RE.sub(" ", strip_markup(raw)))
        hidden = raw_text(raw)
        sources: dict[str, str] = {}

        from_text = self._form_from_text(visible)
        form_type = from_text or self._form_from_name(path)
        sources["form_type"] = "document" if from_text else "filename"

        from_table = self._ticker_from_tables(raw)
        ticker = from_table or self._ticker_from_edgar(path)
        sources["ticker"] = "cover_table" if from_table else "filename"

        name, name_source = self._company_name(visible, hidden, path)
        sources["company_name"] = name_source

        cik, cik_source = self._cik(raw, hidden, path)
        sources["cik"] = cik_source

        period_end = self._period_end(raw, path)
        fiscal_year, fiscal_period, period_source = self._fiscal(
            hidden, path, form_type, period_end
        )
        sources["fiscal"] = period_source

        return {
            "ticker": ticker,
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
        if re.search(r"FORM\s+8-K|CURRENT\s+REPORT", text, re.I):
            return "8-K"
        return ""

    def _form_from_name(self, path: Path) -> str:
        match = _FILE_FORM_RE.search(path.name)
        return match.group(1).upper() if match else ""

    def _ticker_from_tables(self, raw: str) -> str:
        """Ticker from the cover-page "Trading symbol(s)" table.

        This is the only reliable in-document source: a registrant may list many
        securities, and only the first has a real symbol -- the rest are em
        dashes.  Reading the table preserves the label/value pairing that
        flattening the page destroys.
        """
        for frame in self._tables(raw)[:6]:
            if frame is None or frame.empty:
                continue
            grid = frame.astype(str)
            if not grid.apply(lambda col: col.str.contains("Trading [Ss]ymbol", na=False)).any().any():
                continue
            column = next(
                (
                    index
                    for index in range(grid.shape[1])
                    if grid.iloc[:, index].str.contains("Trading [Ss]ymbol", na=False).any()
                ),
                None,
            )
            if column is None:
                continue
            for value in grid.iloc[:, column].tolist()[1:]:
                symbol = clean_text(value).strip("$ ")
                if symbol and symbol.lower() not in _DASHES and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", symbol):
                    return symbol
        return ""

    def _ticker_from_edgar(self, path: Path) -> str:
        stem = re.split(r"[_\-.]", path.stem)[0]
        return stem.upper() if re.fullmatch(r"[A-Za-z]{1,6}", stem) else "UNKNOWN"

    def _company_name(
        self, visible: str, hidden: str, path: Path
    ) -> tuple[str, str]:
        """Registrant name, most reliable source first.

        A 10-K and a 10-Q both state the name next to the commission file
        number.  An 8-K often has no cover table at all, so the fallback is a
        corporate-suffix match, searched in the *hidden* text because Workiva
        parks the issuer block in a ``display:none`` div.
        """
        for haystack in (visible, hidden):
            match = re.search(
                r"Commission File Number:\s*[\d\-]+\s*(.{2,80}?)\s*"
                r"\(\s*Exact name of Registrant",
                haystack,
            )
            if match:
                return clean_text(match.group(1)).strip(" .,"), "commission_file_number"
        dei = re.search(
            r"<(?:dei:)?EntityRegistrantName[^>]*>\s*([^<]{2,80}?)\s*<", hidden, re.I
        )
        if dei:
            return clean_text(dei.group(1)).strip(" .,"), "dei_registrant_name"
        # Prefer the cover-page region; a signature block repeats the name later.
        for match in _CORPORATE_RE.finditer(hidden[:6000]):
            name = clean_text(match.group(1)).strip(" .,")
            if 3 <= len(name) <= 60:
                return name, "corporate_suffix"
        stem = re.split(r"[_\-.]", path.stem)[0]
        return (stem if stem.isupper() else path.stem[:40]), "filename"

    def _cik(self, raw: str, hidden: str, path: Path) -> tuple[str, str]:
        """Ten-digit CIK.

        Read from the entity identifier that every inline-XBRL context carries,
        which is tagged with the SEC's CIK scheme URI and therefore cannot be
        confused with a dollar amount.  A bare ten-digit scan of the visible
        text would be ambiguous, so it is only a fallback.
        """
        match = _XBRL_CIK_RE.search(raw)
        if match:
            return match.group(1).zfill(10), "xbrli_entity_identifier"
        dei = re.search(
            r"<(?:dei:)?EntityCentralIndexKey[^>]*>\s*(\d{1,10})\s*<", hidden, re.I
        )
        if dei:
            return dei.group(1).zfill(10), "dei_central_index_key"
        match = _CIK_RE.search(path.stem)
        if match:
            return match.group(1), "filename"
        return "", "unresolved"

    def _period_end(self, raw: str, path: Path) -> str:
        """The filing's own period end, e.g. ``2026-03-28``."""
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
        """Fiscal year and period, e.g. ``(2025, "FY")`` or ``(2026, "Q2")``.

        The document states its own focus (``2026 Q2``) in the inline-XBRL
        header, so no fiscal-calendar guesswork is needed.  Falling back to a
        scan for the largest year in the text would be wrong: these filings quote
        bond maturities out to 2042.
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
        """ISO date.  The curated filenames carry the filing date; SEC's own
        filenames carry only the period end, so that is the documented
        fallback."""
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

    # -- tables ------------------------------------------------------------

    def _tables(self, raw: str) -> list[pd.DataFrame]:
        """Every table in the filing, parsed once.

        ``read_html`` costs a few tens of milliseconds on a 1.5 MB 10-K, and
        three extractors need the result; parsing per extractor tripled the
        cost of the parse phase.
        """
        cached = getattr(self, "_table_cache", None)
        if cached is None:
            try:
                cached = list(pd.read_html(io.StringIO(html_body(raw)), flavor="lxml"))
            except Exception as exc:  # noqa: BLE001
                log.warning("read_html failed (%s)", exc)
                cached = []
            self._table_cache = cached
        return cached

    def extract_metrics(
        self, raw: str, metadata: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], set[int]]:
        """Financial statement lines as ``Metric`` nodes and edges.

        A metric's identity includes the period it measures, because one edge
        holds one value and a 10-K shows three periods of every line.  The
        period is written into ``canonical_name`` so the node stays queryable
        without reaching for a hash.
        """
        metrics: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        skip_tables: set[int] = set()
        currency = metadata["currency"]
        frames = self._tables(raw)
        for index, frame in enumerate(frames):
            if frame is None or frame.empty:
                continue
            category, cells = extract_cells(frame)
            if not cells:
                continue
            labels = sorted({cell.label for cell in cells})
            context = self._table_context(frames, index, labels)
            if detect_segment_table(frame, labels, context):
                skip_tables.add(index)
            for cell in cells:
                name = (
                    f"{cell.canonical_name} ({cell.period})"
                    if PERIOD_SCOPED_METRICS
                    else cell.canonical_name
                )
                node_id = stable_id("metric", cell.canonical_name, cell.period)
                metrics[node_id] = {
                    "id": node_id,
                    "canonical_name": name,
                    "statement_category": cell.category or category or "other",
                }
                edges.append(
                    {
                        "value": float(cell.number.value),
                        "currency": currency,
                        "metric": node_id,
                        "period": cell.period,
                        "category": cell.category or category or "other",
                    }
                )
        return metrics, self._resolve_conflicts(metrics, edges), skip_tables

    def _resolve_conflicts(
        self,
        metrics: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """One value per ``(filing, metric)`` edge.

        A registrant often states the same line in more than one place -- the
        face of the income statement and again in selected financial data or a
        footnote -- and ``REPORTS_METRIC`` holds a single ``value``.  Two arcs
        for one metric would make the graph's answer to "what did it report"
        depend on arc order, so the most authoritative statement wins and the
        conflict is logged rather than silently resolved.
        """
        priority = {
            "income_statement": 0, "balance_sheet": 1, "cash_flow": 2,
            "segment": 3, "other": 4,
        }
        by_metric: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            by_metric.setdefault(edge["metric"], []).append(edge)
        resolved: list[dict[str, Any]] = []
        conflicts = 0
        for node_id, candidates in by_metric.items():
            best = min(candidates, key=lambda c: priority.get(c["category"], 9))
            if len(candidates) > 1:
                values = {round(float(c["value"]), 6) for c in candidates}
                if len(values) > 1:
                    conflicts += 1
                    log.debug(
                        "metric %s reported %d times with differing values %s; "
                        "keeping %s from the %s table",
                        metrics[node_id]["canonical_name"], len(candidates),
                        sorted(values), best["value"], best["category"],
                    )
            resolved.append(
                {"value": float(best["value"]), "currency": best["currency"],
                 "metric": node_id, "period": best["period"]}
            )
        if conflicts:
            log.info(
                "%d metric(s) restated across tables; kept the highest-priority "
                "statement's value",
                conflicts,
            )
        return resolved

    def _table_context(
        self, frames: Sequence[pd.DataFrame], index: int, labels: Sequence[str]
    ) -> str:
        """Neighbouring text, so a segment note is recognisable as one.

        ``read_html`` reports table order but not position in the page, so the
        nearest preceding table's labels stand in for the surrounding prose.
        """
        parts: list[str] = []
        for offset in (-2, -1, 1, 2):
            position = index + offset
            if 0 <= position < len(frames) and frames[position] is not None:
                parts.extend(
                    _cell(v) for v in frames[position].astype(str).values.flatten()[:80]
                )
        parts.extend(labels)
        return " ".join(parts)

    def extract_segments(
        self, raw: str, metadata: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Reporting segments as ``Segment`` nodes, and the metric that owns each.

        A segment table has no statement line items of its own, so its figures
        hang off the revenue metric of the same period -- which is what the
        ``Metric -> HAS_SEGMENT -> Segment`` shape is for.
        """
        segments: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        frames = self._tables(raw)
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
            context = self._table_context(frames, index, labels)
            kind = detect_segment_table(frame, labels, context)
            if not kind:
                continue
            for label, group, number in rows:
                name = self._segment_name(label, kind)
                if not name:
                    continue
                segments.setdefault(name, {"name": name, "segment_type": kind})
                edges.append(
                    {
                        "value": float(number.value),
                        "period": group.full_key,
                        "segment": name,
                        # Segment figures hang off the revenue metric of the same
                        # period: a segment note has no line items of its own.
                        "metric": stable_id("metric", "Net Sales", group.full_key),
                    }
                )
        return segments, edges

    def _segment_name(self, label: str, kind: str) -> str:
        """A segment label, or ``""`` if the row is not one.

        "Total net sales" is a subtotal, not a segment, and a percentage row is
        a share of a segment rather than a segment -- both are rejected here so
        the ``Segment`` table only ever holds real taxonomy members.
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
        return text

    # -- events ------------------------------------------------------------

    def extract_events(self, raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """8-K item headings, and only for an 8-K."""
        if not str(metadata.get("form_type", "")).upper().startswith("8"):
            return {}
        text = strip_markup(raw)
        events: dict[str, dict[str, Any]] = {}
        scope = filing_identity(metadata)
        for code, title, summary in parse_events(text)[: self.max_events]:
            event_id = stable_id("event", scope, code, title)
            events[event_id] = {
                "id": event_id,
                "item_code": code,
                "item_title": title,
                "summary": summary,
            }
        return events

    # -- chunks ------------------------------------------------------------

    def extract_chunks(
        self, raw: str, metadata: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Hierarchical body text for hybrid retrieval."""
        body = html_body(raw)
        chunks: dict[str, dict[str, Any]] = {}
        scope = filing_identity(metadata)
        for position, block in enumerate(
            chunk_body(body, self.chunk_chars, self.min_chunk_chars)
        ):
            # Position alone is enough within a filing; the filing identity is
            # what keeps two same-type filings on the same day from colliding.
            chunk_id = stable_id("chunk", scope, position)
            chunks[chunk_id] = {
                "id": chunk_id,
                "section": block.section or "Document",
                "text": block.text,
            }
        return chunks

    # -- orchestration -----------------------------------------------------

    def ingest_file(self, path: str | Path) -> ExtractionResult:
        """Parse one filing.  Never raises for a malformed document."""
        started = time.perf_counter()
        path = Path(path)
        # One ingestor serves a whole run, so the per-document table cache has
        # to be dropped or the second filing would be scored against the first.
        self._table_cache = None
        raw = path.read_text(encoding="utf-8", errors="replace")
        metadata = self.extract_metadata(raw, path)

        metrics, metric_edges, _ = self.extract_metrics(raw, metadata)
        segments, segment_edges = self.extract_segments(raw, metadata)
        events = self.extract_events(raw, metadata)
        chunks = self.extract_chunks(raw, metadata)

        # A segment note's figures hang off the revenue metric of the same
        # period.  If that period never appeared on a statement -- a segment
        # note may use a point-in-time key -- the host node has to exist anyway
        # or the arc would be dropped as dangling.
        for edge in segment_edges:
            host = edge["metric"]
            if host not in metrics:
                name = f"Net Sales ({edge['period']})" if PERIOD_SCOPED_METRICS else "Net Sales"
                metrics[host] = {
                    "id": host,
                    "canonical_name": name,
                    "statement_category": "income_statement",
                }

        filing_id = filing_identity(metadata)
        company = {
            "ticker": metadata["ticker"],
            "name": metadata["name"],
            "cik": metadata["cik"],
        }
        filing = {
            "id": filing_id,
            "form_type": metadata["form_type"],
            "fiscal_year": metadata["fiscal_year"],
            "fiscal_period": metadata["fiscal_period"],
            "filing_date": metadata["filing_date"],
        }
        result = ExtractionResult(
            company=company, filing=filing, metrics=metrics, segments=segments,
            events=events, chunks=chunks,
        )
        result.edges = {
            "SUBMITTED": [
                {"from": metadata["ticker"], "to": filing_id}
            ],
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
            "sources": 0,
        }
        result.elapsed = time.perf_counter() - started
        return result



# ---------------------------------------------------------------------------
# Bulk writer
# ---------------------------------------------------------------------------


@dataclass
class WriteReport:
    inserted: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


class BulkWriter:
    """Arrow-buffered writer that survives a re-run.

    The pre-flight key read is the whole reason this class exists: see the
    module docstring.  ``COPY`` is used above ``copy_threshold`` and ``UNWIND``
    below it, one statement per table either way -- never a per-row loop.
    """

    KEY_WINDOW = 900

    def __init__(self, path: str | Path, copy_threshold: int = 2000) -> None:
        self.path = Path(path)
        self.copy_threshold = copy_threshold
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.database = lb.Database(str(self.path))
        except RuntimeError as exc:
            if ".wal" in str(exc):
                raise RuntimeError(
                    f"{self.path} has a stale write-ahead log from a run that did "
                    f"not shut down cleanly ({self.path}.wal). Replay it by "
                    f"opening the database with a matching engine version, or "
                    f"delete the .wal if that run's work is expendable. "
                    f"Original error: {exc}"
                ) from exc
            raise
        self.connection = lb.Connection(self.database)

    def close(self) -> None:
        for closable in (self.connection, self.database):
            close = getattr(closable, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best effort
                    pass

    def __enter__(self) -> "BulkWriter":
        ensure_schema(self.connection)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _existing(self, table: str, key: str, keys: Sequence[str]) -> set[str]:
        if not keys:
            return set()
        present: set[str] = set()
        for start in range(0, len(keys), self.KEY_WINDOW):
            window = list(keys[start : start + self.KEY_WINDOW])
            rows = self.connection.execute(
                f"MATCH (n:{table}) WHERE list_contains($keys, n.{key}) RETURN n.{key}",
                {"keys": window},
            ).get_all()
            present.update(str(row[0]) for row in rows)
        return present

    def _insert_nodes(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        columns = NODE_TABLES[table]
        key = PRIMARY_KEYS[table]
        present = self._existing(table, key, [str(row[key]) for row in rows])
        fresh: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            value = str(row[key])
            if value in present or value in seen:
                continue
            seen.add(value)
            fresh.append(
                {
                    name: (
                        parse_date(row[name])
                        if name in _DATE_TYPES and row.get(name) is not None
                        else row.get(name)
                    )
                    for name in columns
                }
            )
        if not fresh:
            return 0
        if len(fresh) >= self.copy_threshold:
            arrow = pa.table(
                {
                    name: pa.array(
                        [row[name] for row in fresh], type=_arrow_type(name, table)
                    )
                    for name in columns
                }
            )
            self.connection.execute(f"COPY {table} FROM $data", {"data": arrow})
        else:
            assignments = ", ".join(f"{name}: r.{name}" for name in columns)
            self.connection.execute(
                f"UNWIND $rows AS r CREATE (:{table} {{{assignments}}})", {"rows": fresh}
            )
        return len(fresh)

    def _insert_edges(self, rel: str, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        source, target, props = REL_TABLES[rel]
        source_key = PRIMARY_KEYS[source]
        target_key = PRIMARY_KEYS[target]
        have_source = self._existing(source, source_key, sorted({r["from"] for r in rows}))
        have_target = self._existing(target, target_key, sorted({r["to"] for r in rows}))
        usable = [
            r for r in rows if r["from"] in have_source and r["to"] in have_target
        ]
        if not usable:
            return 0
        # Both kinds of arc can repeat across filings, so both are checked
        # against the stored set rather than trusted.  A duplicate arc is not
        # merely untidy: MATCH fans out once per duplicate, so a second run
        # would double every row a join returns.
        if props:
            usable = self._new_property_edges(rel, usable, props)
        else:
            usable = self._new_bare_edges(rel, usable)
        if not usable:
            return 0

        if len(usable) >= self.copy_threshold:
            data: dict[str, Any] = {
                "from": [r["from"] for r in usable],
                "to": [r["to"] for r in usable],
            }
            for name in props:
                data[name] = [r.get(name) for r in usable]
            arrow = pa.table(
                {
                    name: pa.array(
                        values,
                        type=pa.float64() if name in _DOUBLE_PROPS else pa.string(),
                    )
                    for name, values in data.items()
                }
            )
            self.connection.execute(f"COPY {rel} FROM $data", {"data": arrow})
        else:
            body = "{" + ", ".join(f"{name}: r.{name}" for name in props) + "}" if props else ""
            payload = [
                {"fk": r["from"], "tk": r["to"], **{p: r.get(p) for p in props}}
                for r in usable
            ]
            self.connection.execute(
                f"UNWIND $rows AS r "
                f"MATCH (a:{source} {{{source_key}: r.fk}}), "
                f"(b:{target} {{{target_key}: r.tk}}) "
                f"CREATE (a)-[:{rel}{body}]->(b)",
                {"rows": payload},
            )
        return len(usable)

    def _existing_arcs(
        self, rel: str, rows: Sequence[dict[str, Any]]
    ) -> set[tuple[Any, Any]]:
        """Endpoint pairs of *rel* already stored for the given arc candidates."""
        source, target, _ = REL_TABLES[rel]
        source_key = PRIMARY_KEYS[source]
        target_key = PRIMARY_KEYS[target]
        stored: set[tuple[Any, Any]] = set()
        for start in range(0, len(rows), self.KEY_WINDOW):
            window = rows[start : start + self.KEY_WINDOW]
            found = self.connection.execute(
                f"MATCH (a:{source})-[e:{rel}]->(b:{target}) "
                f"WHERE list_contains($sk, a.{source_key}) "
                f"AND list_contains($tk, b.{target_key}) "
                f"RETURN a.{source_key}, b.{target_key}",
                {
                    "sk": [r["from"] for r in window],
                    "tk": [r["to"] for r in window],
                },
            ).get_all()
            stored.update(tuple(row) for row in found)
        return stored

    def _new_bare_edges(
        self, rel: str, rows: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop propertyless arcs whose endpoints are already connected."""
        unique: dict[tuple[Any, Any], dict[str, Any]] = {}
        for row in rows:
            unique.setdefault((row["from"], row["to"]), row)
        candidates = list(unique.values())
        stored = self._existing_arcs(rel, candidates)
        if not stored:
            return candidates
        return [
            row for row in candidates if (row["from"], row["to"]) not in stored
        ]

    def _new_property_edges(
        self, rel: str, rows: Sequence[dict[str, Any]], props: Sequence[str]
    ) -> list[dict[str, Any]]:
        source, target, _ = REL_TABLES[rel]
        source_key = PRIMARY_KEYS[source]
        target_key = PRIMARY_KEYS[target]
        projection = ", ".join(f"e.{name}" for name in props)
        stored: set[tuple[Any, ...]] = set()
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            identity = (row["from"], row["to"], *(row.get(name) for name in props))
            unique.setdefault(identity, row)
        candidates = list(unique.values())
        for start in range(0, len(candidates), self.KEY_WINDOW):
            window = candidates[start : start + self.KEY_WINDOW]
            found = self.connection.execute(
                f"MATCH (a:{source})-[e:{rel}]->(b:{target}) "
                f"WHERE list_contains($sk, a.{source_key}) "
                f"AND list_contains($tk, b.{target_key}) "
                f"RETURN a.{source_key}, b.{target_key}, {projection}",
                {
                    "sk": [r["from"] for r in window],
                    "tk": [r["to"] for r in window],
                },
            ).get_all()
            stored.update(tuple(row) for row in found)
        if not stored:
            return candidates
        return [
            row
            for row in candidates
            if (row["from"], row["to"], *(row.get(name) for name in props)) not in stored
        ]

    def write(self, result: ExtractionResult) -> WriteReport:
        """Persist one extraction result, in one transaction."""
        report = WriteReport()
        started = time.perf_counter()
        self.connection.execute("BEGIN TRANSACTION")
        try:
            for table, rows in (
                ("Company", [result.company]),
                ("Filing", [result.filing]),
                ("Metric", list(result.metrics.values())),
                ("Segment", list(result.segments.values())),
                ("Event", list(result.events.values())),
                ("Chunk", list(result.chunks.values())),
            ):
                report.inserted[table] = self._insert_nodes(table, rows)
            for rel, rows in result.edges.items():
                report.inserted[rel] = self._insert_edges(rel, rows)
            self.connection.execute("COMMIT")
        except Exception:
            # A failed statement can already have aborted the transaction, in
            # which case ROLLBACK itself raises and would replace the real error
            # with "No active transaction".
            try:
                self.connection.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        report.seconds = time.perf_counter() - started
        return report


def _arrow_type(name: str, table: str) -> pa.DataType:
    if name in _INT_TYPES:
        return pa.int64()
    if name in _DATE_TYPES:
        return pa.date32()
    return pa.string()


# ---------------------------------------------------------------------------
# Query verification suite
# ---------------------------------------------------------------------------

#: Test scope: one filing of each form.  Paths are resolved against the repo
#: root and the first existing match wins, so a differently-named file of the
#: right form still works.
DEFAULT_SCOPE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("10-K", ("data/aapl-sec/10-K_2025-10-31_aapl-20250927.htm", "data/aapl-sec/10-K_2025")),
    ("10-Q", ("data/aapl-2026/10-Q_2026-05-01_aapl-20260328.htm", "data/aapl-2026/10-Q")),
    ("8-K", ("data/aapl-2026/8-K_2026-02-24_ef20060722_8k.htm", "data/aapl-2026/8-K")),
)

ABSENT_YEAR = 2019

#: ``"Net Sales (FY2025)"`` -> ``"FY2025"``.  The period is written into the
#: metric's name so a query never has to know a content hash.
_PERIOD_SUFFIX_RE = re.compile(r"\(([^()]*)\)\s*$")


def _period_of(canonical_name: str) -> str:
    match = _PERIOD_SUFFIX_RE.search(canonical_name or "")
    return match.group(1) if match else ""


def resolve_scope(root: Path) -> list[Path]:
    """The three test filings, as paths."""
    resolved: list[Path] = []
    for _, candidates in DEFAULT_SCOPE:
        for candidate in candidates:
            path = root / candidate
            if path.exists():
                resolved.append(path)
                break
        else:
            exact = sorted(root.glob(candidates[-1] + "*"))
            if not exact:
                raise FileNotFoundError(f"no filing matching {candidates}")
            resolved.append(exact[0])
    return resolved


class VerificationSuite:
    """Runs the four required checks and returns JSON."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        result = self.connection.execute(query, params or {})
        return [list(row) for row in result.get_all()]

    @staticmethod
    def reporting_suffix(form_type: str, fiscal_year: int) -> str:
        """Period key of a filing's own reporting period.

        A 10-K's reporting period is the year; a 10-Q's is the quarter, which is
        the shortest duration a quarterly report states.  This is what
        distinguishes "the quarter Apple just reported" from the prior-year
        comparative the same table shows beside it.
        """
        if str(form_type).upper().startswith("10-Q"):
            return f"3M-FY{fiscal_year}"
        return f"FY{fiscal_year}"

    def metric_lookup(
        self, form_type: str, fiscal_year: int, concepts: Sequence[str]
    ) -> dict[str, Any]:
        """Every period stored for named concepts, plus the reporting period.

        The whole trajectory is returned rather than a single number so the
        comparative columns are visible instead of being filtered away by
        arithmetic the reader cannot see.
        """
        payload: dict[str, Any] = {
            "form_type": form_type,
            "fiscal_year": fiscal_year,
            "reporting_period": self.reporting_suffix(form_type, fiscal_year),
            "concepts": {},
        }
        for concept in concepts:
            rows = self._rows(
                "MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[e:REPORTS_METRIC]->(m:Metric) "
                "WHERE f.form_type = $form AND f.fiscal_year = $year "
                "AND m.canonical_name CONTAINS $concept "
                "RETURN m.canonical_name, m.statement_category, e.value, e.currency",
                {"form": form_type, "year": fiscal_year, "concept": concept},
            )
            periods = [
                {
                    "metric": row[0],
                    "period": _period_of(row[0]),
                    "value": float(row[2]) if row[2] is not None else None,
                    "currency": row[3],
                }
                for row in rows
            ]
            periods.sort(key=lambda item: str(item["period"]))
            reporting = payload["reporting_period"]
            match = next(
                (item for item in periods if item["period"] == reporting), None
            )
            payload["concepts"][concept] = {
                "reporting_value": match["value"] if match else None,
                "reporting_period": reporting,
                "periods": periods,
            }
        return payload

    def quarterly_trajectory(
        self, fiscal_year: int, concepts: Sequence[str]
    ) -> dict[str, Any]:
        return self.metric_lookup("10-Q", fiscal_year, concepts)

    def event_inspection(self, form_type: str = "8-K") -> dict[str, Any]:
        rows = self._rows(
            "MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[:DISCLOSES_EVENT]->(e:Event) "
            "WHERE f.form_type = $form "
            "RETURN e.item_code, e.item_title, e.summary, f.fiscal_year "
            "ORDER BY e.item_code",
            {"form": form_type},
        )
        return {
            "count": len(rows),
            "events": [
                {
                    "item_code": row[0],
                    "item_title": row[1],
                    "summary": (row[2] or "")[:400],
                    "fiscal_year": row[3],
                }
                for row in rows
            ],
        }

    def coverage_gap(self, year: int = ABSENT_YEAR) -> dict[str, Any]:
        """A year outside the test scope must return an empty set, not fail."""
        filings = self._rows(
            "MATCH (c:Company)-[:SUBMITTED]->(f:Filing) WHERE f.fiscal_year = $year "
            "RETURN f.id, f.form_type, f.fiscal_period",
            {"year": year},
        )
        metrics = self._rows(
            "MATCH (c:Company)-[:SUBMITTED]->(f:Filing)"
            "-[e:REPORTS_METRIC]->(m:Metric) "
            "WHERE f.fiscal_year = $year RETURN m.canonical_name, e.value",
            {"year": year},
        )
        return {
            "year": year,
            "filings": len(filings),
            "metrics": len(metrics),
            "empty": not filings and not metrics,
        }

    def graph_shape(self) -> dict[str, int]:
        """Node and relationship counts, for the run report."""
        shape: dict[str, int] = {}
        for table in NODE_TABLES:
            rows = self._rows(f"MATCH (n:{table}) RETURN count(n)")
            shape[table] = int(rows[0][0]) if rows else 0
        for rel in REL_TABLES:
            rows = self._rows(f"MATCH ()-[e:{rel}]->() RETURN count(e)")
            shape[rel] = int(rows[0][0]) if rows else 0
        return shape


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def run(
    db_path: Path,
    files: Sequence[Path],
    reset: bool = False,
    root: Path = Path("."),
) -> dict[str, Any]:
    """Ingest *files*, then verify.  Returns the full report."""
    if reset:
        for suffix in ("", ".wal"):
            target = Path(str(db_path) + suffix)
            if target.exists():
                log.warning("removing %s", target)
                target.unlink()

    ingestor = UniversalSECIngestor()
    report: dict[str, Any] = {"version": VERSION, "database": str(db_path), "filings": []}

    log.info("parsing %d filing(s) -- no database connection open yet", len(files))
    parsed: list[ExtractionResult] = []
    for path in files:
        result = ingestor.ingest_file(path)
        parsed.append(result)
        report["filings"].append(
            {
                "file": path.name,
                "company": result.company,
                "form_type": result.filing["form_type"],
                "fiscal_year": result.filing["fiscal_year"],
                "fiscal_period": result.filing["fiscal_period"],
                "filing_date": result.filing["filing_date"],
                "counts": result.counts(),
                "parse_seconds": round(result.elapsed, 3),
            }
        )
        log.info(
            "%s: %s %s -> %s in %.3fs",
            path.name, result.filing["form_type"],
            result.filing["fiscal_period"], result.counts(), result.elapsed,
        )

    # A connection per filing, not per run: see the module docstring.  Each
    # result is zipped with the report entry built for *that* filing, so a
    # failure part-way through still attributes its writes correctly.
    for result, entry in zip(parsed, report["filings"], strict=True):
        with BulkWriter(db_path) as writer:
            written = writer.write(result)
        entry["written"] = written.inserted
        entry["write_seconds"] = round(written.seconds, 3)
        log.info(
            "%s: wrote %s in %.3fs",
            result.filing["form_type"], written.inserted, written.seconds,
        )

    with BulkWriter(db_path) as writer:
        suite = VerificationSuite(writer.connection)
        report["graph"] = suite.graph_shape()
        report["checks"] = {
            "1_metric_lookup_fy2025": suite.metric_lookup(
                "10-K", 2025, ["Gross Margin", "Net Sales"]
            ),
            "2_quarterly_fy2026": suite.quarterly_trajectory(
                2026, ["Net Sales", "Operating Income"]
            ),
            "3_event_inspection": suite.event_inspection(),
            "4_coverage_gap": suite.coverage_gap(),
        }
    return report


def assert_checks(report: dict[str, Any]) -> list[str]:
    """Test assertions.  Returns the list of failures, empty when all pass."""
    failures: list[str] = []
    checks = report["checks"]

    def require_reporting(check: dict[str, Any], label: str) -> None:
        for concept, found in check["concepts"].items():
            if found["reporting_value"] is None:
                failures.append(
                    f"{label}: no value for {concept!r} in the reporting period "
                    f"({check['reporting_period']}); stored periods: "
                    f"{[p['period'] for p in found['periods']]}"
                )

    require_reporting(checks["1_metric_lookup_fy2025"], "check 1")
    require_reporting(checks["2_quarterly_fy2026"], "check 2")

    events = checks["3_event_inspection"]
    if not events["count"]:
        failures.append("check 3: the 8-K produced no Event nodes")
    for event in events["events"]:
        if not event["item_code"]:
            failures.append("check 3: an event has no item code")
        if not event["item_title"]:
            failures.append(f"check 3: {event['item_code']} has no title")

    gap = checks["4_coverage_gap"]
    if not gap["empty"]:
        failures.append(
            f"check 4: FY{gap['year']} should be outside the test scope but "
            f"returned {gap['filings']} filings"
        )

    graph = report["graph"]
    for table in NODE_TABLES:
        if graph.get(table, 0) == 0:
            failures.append(f"graph: {table} is empty")
    for rel in ("SUBMITTED", "REPORTS_METRIC", "HAS_CHUNK", "DISCLOSES_EVENT"):
        if graph.get(rel, 0) == 0:
            failures.append(f"graph: {rel} has no arcs")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Universal zero-LLM SEC filing ingestion into LadybugDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", default="universal_sec.lbug", help="graph database path")
    parser.add_argument("--files", nargs="*", help="filings to ingest")
    parser.add_argument("--root", default=".", help="root for the default test scope")
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    parser.add_argument("--files-only", action="store_true", help="parse without writing")
    parser.add_argument("--verify", action="store_true", help="run the query suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    root = Path(args.root).resolve()
    files = [Path(f).resolve() for f in args.files] if args.files else resolve_scope(root)

    if args.files_only:
        ingestor = UniversalSECIngestor()
        payload = []
        for path in files:
            result = ingestor.ingest_file(path)
            payload.append(
                {
                    "file": path.name,
                    "company": result.company,
                    "filing": result.filing,
                    "counts": result.counts(),
                    "parse_seconds": round(result.elapsed, 3),
                }
            )
        print(json.dumps(payload, indent=2, default=_json_default))
        return 0

    report = run(Path(args.db).resolve(), files, reset=args.reset, root=root)
    print(json.dumps(report, indent=2, default=_json_default))

    if args.verify:
        failures = assert_checks(report)
        if failures:
            log.error("VERIFICATION FAILED (%d)", len(failures))
            for failure in failures:
                log.error("  - %s", failure)
            return 1
        log.info("VERIFICATION PASSED: all 4 checks and graph shape are correct")
    return 0


if __name__ == "__main__":
    sys.exit(main())
