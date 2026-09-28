#!/usr/bin/env python3
"""Autonomous offline ingestion engine for HTML / PDF-to-HTML financial filings.

Builds a knowledge graph in an embedded LadybugDB (or Kùzu) database with **zero
external LLM calls**. Everything is deterministic: the same document always
produces the same graph.

    pip install ladybug watchdog pyarrow pandas beautifulsoup4 lxml spacy
    python -m spacy download en_core_web_sm

Pipeline per document
---------------------
1. ``StructuralParser``  -- headings become ``Section`` nodes, paragraphs become
   ``Chunk`` nodes, and chunks are chained with ``NEXT_CHUNK`` so the original
   reading order survives into the graph.
2. ``TableParser``       -- every ``<table>`` is read with ``pandas.read_html``;
   a stub cell paired with its column header becomes a ``DataRecord``.
3. ``NLPExtractor``      -- local spaCy on CPU supplies named entities
   (ORG/PERSON/GPE/PRODUCT/MONEY/DATE) and subject-verb-object triples taken
   from dependency parses.
4. ``GraphWriter``       -- the whole document is buffered in memory, then
   written with batched statements (Arrow ``COPY`` or ``UNWIND``). No per-row
   query loop.

Why buffering matters
---------------------
Per-row ``MERGE`` is dominated by round-trip latency. Buffering a whole
document and issuing one statement per table turns thousands of round trips
into a handful, which is where the throughput claims come from.

A hazard specific to this engine
--------------------------------
``COPY ... FROM`` **hangs indefinitely** when a row violates the primary key;
it does not raise. LadybugDB 0.20.4 deadlocks instead of reporting the
conflict. Every bulk write here therefore reads the existing primary keys
first, subtracts them, and only copies rows that are genuinely new. That check
is not an optimisation -- it is what keeps a re-run from hanging forever.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import logging
import re
import shutil
import signal
import sys
import threading
import time
import uuid
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import ladybug as lb
except ImportError:  # pragma: no cover - Kùzu is API-compatible
    try:
        import kuzu as lb  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "No embedded graph engine found. Install one with:\n"
            "    pip install ladybug      # or: pip install kuzu"
        ) from exc

import pandas as pd
import pyarrow as pa
from bs4 import BeautifulSoup, Tag
from bs4 import XMLParsedAsHTMLWarning
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# Inline-XBRL filings are XML documents with embedded HTML. Parsing them with
# an HTML parser is intentional and works, but BeautifulSoup warns about it on
# every single file, which drowns the actual log.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger("ingest")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})

#: The only NER labels worth keeping. ``en_core_web_sm`` does not emit PRODUCT,
#: so that label only appears if a model which does is installed.
ENTITY_LABELS = frozenset({"ORG", "PERSON", "GPE", "PRODUCT", "MONEY", "DATE"})

#: Never become graph nodes. Money and dates are captured as ``DataRecord``
#: values instead, which is where a query would look for them.
REJECTED_LABELS = frozenset({"MONEY", "DATE", "CARDINAL", "ORDINAL", "QUANTITY"})

#: Stripped when deriving a canonical name, so "Apple Inc.", "APPLE INC" and
#: "Apple, Inc." collapse onto a single node.
CORPORATE_SUFFIXES = frozenset(
    {
        "incorporated", "corporation", "company", "limited", "holdings",
        "inc", "corp", "llc", "llp", "ltd", "plc", "co", "sa", "ag", "nv", "ab",
    }
)

#: Dropped before a mention is considered: pure punctuation, single characters,
#: the interrogatives that otherwise survive as spaCy "entities", and the fixed
#: vocabulary of a filing cover page. That last group matters: a 10-Q cover
#: reads "Registrant: Apple Inc." and NER is happy to label *Registrant* an
#: ORG, which would otherwise become the most-mentioned organisation in the
#: document and take ownership of every figure in it.
ENTITY_STOPWORDS = frozenset(
    """
    the a an and or of in on at to for from by with as is are was were be been
    being has have had will would could should may might must this that these
    those it its they them their there here what which who whom whose how when
    where why not no nor so such than then also other others same both each any
    all some more most many much very own
    registrant registrants document documents form part item items exhibit
    address addresses telephone phone zip jurisdiction incorporation entity
    title titles securities security class trading symbol name names
    commission file number fiscal year years ended month months quarter
    quarter(s) transition period emerging growth smaller reporting company
    accelerated filer large accelerated yes no n/a
    """.split()
)

MIN_ENTITY_CHARS = 2
MAX_ENTITY_TOKENS = 8

#: An all-caps token this long is far more likely to be a shouted common word
#: ("APPLE") than an acronym ("SEC"), so it gets title-cased.
ACRONYM_MAX_LEN = 4


@dataclass
class EngineConfig:
    """Everything tunable, in one place."""

    database: Path = Path("knowledge_graph.lbug")
    incoming: Path = Path("incoming_docs")
    processed: Path = Path("processed_docs")
    failed: Path = Path("failed_docs")

    #: Batch a table into ``COPY`` only above this many rows. Below it a single
    #: ``UNWIND`` is cheaper than building an Arrow table.
    copy_threshold: int = 2_000
    buffer_pool_mb: int = 256
    max_num_threads: int = 0
    compression: bool = True

    #: spaCy processes at most this many characters per document. Set well above
    #: a real filing (10-Ks run to several MB) because truncating mid-table
    #: silently corrupts the extracted figures, which is worse than a slow run.
    #: Set ``--max-chars`` lower to trade completeness for latency.
    max_chars: int = 3_000_000
    nlp_batch: int = 64
    disable_nlp: bool = False
    read_only: bool = False

    def ensure_dirs(self) -> None:
        for path in (self.incoming, self.processed, self.failed):
            path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

NODE_TABLES: dict[str, tuple[str, ...]] = {
    "Document": ("id", "title", "filename", "timestamp"),
    "Section": ("id", "title", "level"),
    "Chunk": ("id", "text", "chunk_index"),
    "Entity": ("name", "type"),
    "DataRecord": ("id", "label", "value", "period"),
}

#: ``source table -> [(rel name, target table, property columns)]``
REL_TABLES: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "Document": (("HAS_SECTION", "Section", ()),),
    "Section": (("CONTAINS_CHUNK", "Chunk", ()),),
    "Chunk": (
        ("NEXT_CHUNK", "Chunk", ()),
        ("MENTIONS", "Entity", ()),
    ),
    "Entity": (
        ("RELATION", "Entity", ("verb", "context")),
        ("RECORDED", "DataRecord", ()),
    ),
}

PRIMARY_KEYS: dict[str, str] = {
    "Document": "id",
    "Section": "id",
    "Chunk": "id",
    "Entity": "name",
    "DataRecord": "id",
}


def schema_ddl() -> tuple[str, ...]:
    """DDL for every node and relationship table, in dependency order."""
    statements: list[str] = []
    for table, columns in NODE_TABLES.items():
        typed = ", ".join(f"{n} {_column_type(n)}" for n in columns)
        statements.append(
            f"CREATE NODE TABLE IF NOT EXISTS {table} "
            f"({typed}, PRIMARY KEY ({PRIMARY_KEYS[table]}))"
        )
    # A relationship table is identified by its name alone, so two names
    # sharing one (FROM, TO) pair would make the second declaration a silent
    # no-op behind "IF NOT EXISTS". Fail loudly at import time instead.
    seen: dict[str, tuple[str, str]] = {}
    for source, targets in REL_TABLES.items():
        for name, target, props in targets:
            if name in seen:
                raise ValueError(
                    f"duplicate relationship table name {name!r}: "
                    f"{seen[name]} and {(source, target)}"
                )
            seen[name] = (source, target)
            extra = "".join(f", {p} STRING" for p in props)
            statements.append(
                f"CREATE REL TABLE IF NOT EXISTS {name} "
                f"(FROM {source} TO {target}{extra})"
            )
    return tuple(statements)


def _column_type(name: str) -> str:
    if name == "timestamp":
        return "TIMESTAMP"
    if name in {"level", "chunk_index"}:
        return "INT64"
    return "STRING"


def _arrow_type(name: str) -> pa.DataType:
    if name == "timestamp":
        return pa.timestamp("us")
    if name in {"level", "chunk_index"}:
        return pa.int64()
    return pa.string()


def rel_properties(rel: str) -> tuple[str, ...]:
    for targets in REL_TABLES.values():
        for name, _target, props in targets:
            if name == rel:
                return props
    return ()


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s&/-]", re.UNICODE)
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[A-Za-z0-9][\w&/-]*")
_POSSESSIVE = re.compile(r"['\u2019]s\b")


def clean_text(value: str) -> str:
    """Collapse whitespace and strip decorative punctuation."""
    return _WS.sub(" ", (value or "").replace("\xa0", " ")).strip()


def _title_case_all_caps(text: str) -> str:
    """Fold ``APPLE INC`` to ``Apple Inc`` but leave ``SEC`` alone."""
    if len(text) > ACRONYM_MAX_LEN and text == text.upper():
        return " ".join(w.capitalize() for w in text.split())
    return text


def canonical_entity(raw: str) -> str | None:
    """Canonical node name for a mention, or None when it is noise.

    This single function is the whole alias story, and it doubles as the
    primary key because ``Entity`` is keyed on ``name``. It therefore has to
    return something both human-readable and stable: a name that survives being
    written to a filing ("Apple", "SEC", "Tim Cook") rather than a hash.

    "Apple Inc.", "APPLE INC" and "Apple, Inc." all reduce to "Apple". Two names
    that normalise differently -- "Apple Computer" and "Apple" -- stay separate,
    since the schema has no place to record an alias.
    """
    text = clean_text(raw).strip("\"'`“”‘’()[]{}<>|*_#")
    # A possessive is part of the sentence, not the name: NER happily reports
    # "Apple's" and stripping the apostrophe alone would leave "Apple s".
    text = _POSSESSIVE.sub("", text)
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WS.sub(" ", text).strip(" -/&,").split() if t]
    # A leading article is part of the mention, not the name: "The Apple
    # Company" refers to the same entity as "Apple Inc."
    if len(tokens) > 1 and tokens[0].lower() in {"the", "a", "an"}:
        tokens.pop(0)
    while tokens and tokens[-1].lower().strip(".,") in CORPORATE_SUFFIXES:
        tokens.pop()
    # Inline XBRL and screen-reader markup frequently repeat a token
    # ("Americas Americas"); collapse the run back to one.
    deduped: list[str] = []
    for token in tokens:
        if not deduped or deduped[-1].lower() != token.lower():
            deduped.append(token)
    tokens = deduped
    if not tokens:
        return None

    text = _title_case_all_caps(" ".join(tokens))
    lowered = [t.lower() for t in _TOKEN.findall(text)]
    if not lowered:
        return None
    if len(text) < MIN_ENTITY_CHARS or len(lowered) > MAX_ENTITY_TOKENS:
        return None
    if all(t in ENTITY_STOPWORDS for t in lowered):
        return None
    # A mention made only of digits or punctuation is an amount, not a name.
    if all(t.isdigit() for t in lowered):
        return None
    return text


def stable_id(prefix: str, *parts: str) -> str:
    """Content-addressed primary key.

    Deterministic on purpose: re-processing a file produces the same ids, so the
    duplicate check in :meth:`GraphWriter._insert` recognises them instead of
    colliding.
    """
    digest = hashlib.sha1("␟".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


# --------------------------------------------------------------------------
# Extracted records
# --------------------------------------------------------------------------


@dataclass
class SectionRecord:
    id: str
    title: str
    level: int


@dataclass
class ChunkRecord:
    id: str
    text: str
    chunk_index: int
    section_id: str | None = None
    next_id: str | None = None


@dataclass
class EntityRecord:
    name: str
    type: str


@dataclass
class DataRecord:
    id: str
    label: str
    value: str
    period: str


@dataclass
class Edge:
    """One relationship row, ready for bulk insert."""

    rel: str
    source_table: str
    source_key: str
    target_table: str
    target_key: str
    props: dict[str, str] = field(default_factory=dict)

    def identity(self) -> tuple[str, str, str, str]:
        return (self.source_table, self.source_key, self.rel, self.target_key)

    def props_signature(self) -> str:
        return "\x1f".join(f"{k}={v}" for k, v in sorted(self.props.items()))


@dataclass
class DocumentGraph:
    """Everything extracted from one document, buffered for bulk insert."""

    document_id: str
    title: str
    filename: str
    timestamp: datetime
    sections: list[SectionRecord] = field(default_factory=list)
    chunks: list[ChunkRecord] = field(default_factory=list)
    entities: dict[str, EntityRecord] = field(default_factory=dict)
    data_records: list[DataRecord] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)

    def add_entity(self, name: str, label: str) -> EntityRecord | None:
        """Register a mention, returning the node it collapsed onto."""
        if label in REJECTED_LABELS:
            return None
        canonical = canonical_entity(name)
        if canonical is None:
            return None
        existing = self.entities.get(canonical)
        if existing is not None:
            self.stats["entity_merged"] += 1
            return existing
        record = EntityRecord(name=canonical, type=label)
        self.entities[canonical] = record
        self.stats["entity_new"] += 1
        return record

    def entity_for(self, text: str) -> EntityRecord | None:
        canonical = canonical_entity(text)
        return self.entities.get(canonical) if canonical else None

    def primary_entity(self) -> EntityRecord | None:
        """The most-mentioned organisation: the filing's subject.

        Used as the fallback owner of a table, so that a financial statement
        whose header is nothing but years still attaches to the filer.
        """
        tally: Counter = Counter()
        for edge in self.edges:
            if edge.rel == "MENTIONS":
                tally[edge.target_key] += 1
        if not tally:
            return None
        best = max(tally, key=lambda name: tally[name])
        record = self.entities.get(best)
        if record is not None and record.type == "ORG":
            return record
        orgs = [r for r in self.entities.values() if r.type == "ORG"]
        return max(orgs, key=lambda r: tally.get(r.name, 0), default=None)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

#: Tags that can hold text of their own. A div-based filing has no ``<p>`` at
#: all, so chunking has to descend to the leaves.
TEXT_TAGS = HEADING_TAGS + ("p", "div", "li", "blockquote", "pre")

#: A candidate is a leaf only when it contains none of these.
CONTAINER_TAGS = ("div", "p", "li", "table", "ul", "ol", "blockquote", "section", *HEADING_TAGS)

#: Inline-XBRL metadata: machine-readable facts with no narrative value. Left in
#: place they flood the graph with strings like "0000320193 us-gaap:CommonStockMember".
XBRTL_PREFIXES = ("xbrli", "xbrldi", "ix:", "ixheader", "link:")

_STRIP_TAGS = ("script", "style", "noscript", "head")

#: Hard cap on a stored chunk. Bounds the work spaCy has to do for a single
#: unit and stops one pathological block from becoming one enormous "sentence".
MAX_CHUNK_CHARS = 2_500

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")


def _is_hidden(tag: Tag) -> bool:
    """True for anything the filing itself hides from a reader."""
    for node in (tag, *tag.parents):
        if not isinstance(node, Tag):
            continue
        style = (node.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if str(node.get("aria-hidden") or "").lower() == "true":
            return True
    return False


def _strip_xbrl(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if any(name.startswith(prefix) for prefix in XBRTL_PREFIXES):
            tag.decompose()


def _split_long(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Break an over-long block into sentence-aligned pieces."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(text):
        while len(sentence) > limit:
            # A single "sentence" longer than the cap still has to be cut.
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(current) + len(sentence) + 1 <= limit:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


class StructuralParser:
    """Turns a filing's block structure into sections and ordered chunks.

    Real filings are the design constraint here. A Workiva-generated 10-Q
    contains no ``<h1>``–``<h3>`` and no ``<p>``: its narrative lives in leaf
    ``<div>``s, and its financial tables in ``<td>``s. So chunks come from leaf
    text blocks, table interiors are left to :class:`TableParser`, and a
    document with no headings gets a synthetic root section so that every chunk
    is still reachable through ``CONTAINS_CHUNK``.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

    def parse(self, html: str, graph: DocumentGraph) -> None:
        soup = BeautifulSoup(_html_body(html), "lxml")
        for tag in soup.find_all(_STRIP_TAGS):
            tag.decompose()
        _strip_xbrl(soup)

        blocks = self._blocks(soup)
        has_heading = any(level for _text, level in blocks)

        stack: list[SectionRecord] = []
        if not has_heading:
            # No semantic outline to build on, so anchor the body under one
            # section named after the document.
            root = SectionRecord(
                id=stable_id("sc", graph.document_id, graph.title, "root"),
                title=graph.title,
                level=0,
            )
            graph.sections.append(root)
            graph.edges.append(
                Edge("HAS_SECTION", "Document", graph.document_id, "Section", root.id)
            )
            stack.append(root)

        pending: list[str] = []
        chunk_index = 0

        def close_chunk() -> None:
            nonlocal chunk_index
            if not pending:
                return
            text = clean_text(" ".join(pending))
            pending.clear()
            if not text:
                return
            section = stack[-1] if stack else None
            for piece in _split_long(text):
                chunk = ChunkRecord(
                    id=stable_id("ch", graph.document_id, str(chunk_index)),
                    text=piece,
                    chunk_index=chunk_index,
                    section_id=section.id if section else None,
                )
                graph.chunks.append(chunk)
                if section is not None:
                    graph.edges.append(
                        Edge("CONTAINS_CHUNK", "Section", section.id, "Chunk", chunk.id)
                    )
                chunk_index += 1

        for text, level in blocks:
            if level:
                close_chunk()
                # Pop to the parent level *before* deciding whether this heading
                # is new, so a repeated heading reuses its section rather than
                # creating a second one with the same id. The stack decides
                # which section a block belongs to; the nesting itself is
                # recorded by ``level``, since the schema has one Document
                # -> Section edge only.
                while stack and stack[-1].level >= level:
                    stack.pop()
                sid = stable_id("sc", graph.document_id, text, str(level))
                section = next((s for s in graph.sections if s.id == sid), None)
                if section is None:
                    section = SectionRecord(id=sid, title=text, level=level)
                    graph.sections.append(section)
                    graph.edges.append(
                        Edge("HAS_SECTION", "Document", graph.document_id, "Section", sid)
                    )
                stack.append(section)
                graph.stats["sections"] += 1
            else:
                pending.append(text)

        close_chunk()
        if graph.chunks:
            graph.stats["chunks"] = len(graph.chunks)

        # Chain the chunks so the document's reading order is queryable.
        for previous, following in zip(graph.chunks, graph.chunks[1:]):
            previous.next_id = following.id
            graph.edges.append(
                Edge("NEXT_CHUNK", "Chunk", previous.id, "Chunk", following.id)
            )

    @staticmethod
    def _blocks(soup: BeautifulSoup) -> list[tuple[str, int]]:
        """``(text, heading level)`` in document order; level 0 means body text."""
        found: list[tuple[str, int]] = []
        for tag in soup.find_all(TEXT_TAGS):
            if not isinstance(tag, Tag) or tag.decomposed:
                continue
            if tag.find(CONTAINER_TAGS) is not None:
                continue  # not a leaf; its children speak for it
            if tag.find_parent("table") is not None:
                continue  # table interiors become DataRecords
            if _is_hidden(tag):
                continue
            text = clean_text(tag.get_text(" ", strip=True))
            if len(text) < 2:
                continue
            name = (tag.name or "").lower()
            found.append((text, int(name[1]) if name in HEADING_TAGS else 0))
        return found


def _html_body(html: str) -> str:
    """Drop anything before the root element.

    Inline-XBRL filings carry an XML prolog ahead of ``<html>``; leaving it in
    place confuses strict parsers for no benefit.
    """
    match = re.search(r"<\s*(!doctype\s+html|html|body)\b", html, re.IGNORECASE)
    return html[match.start() :] if match else html


def _is_numberish(text: str) -> bool:
    stripped = (
        text.replace(",", "")
        .replace("$", "")
        .replace("%", "")
        .replace("(", "")
        .replace(")", "")
        .replace("—", "")
        .strip()
    )
    if not stripped:
        return True
    try:
        float(stripped)
    except ValueError:
        return False
    return True


class TableParser:
    """Turns every ``<table>`` into ``DataRecord`` nodes.

    No financial vocabulary is hard-coded. A row's first non-empty cell is the
    label and each remaining cell is paired with its column header, which yields
    ``(label, period, value)`` for any tabular layout, and the record is attached
    to an entity named in the same table when one exists.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

    def parse(self, html: str, graph: DocumentGraph) -> None:
        # Inline-XBRL filings start with an XML prolog, and pandas reads a long
        # bare string as a filesystem path rather than as markup -- it tries to
        # open the markup itself and raises FileNotFoundError. Handing it a
        # file-like object after dropping the prolog avoids both problems.
        body = _html_body(html)
        try:
            frames = pd.read_html(io.StringIO(body), flavor="lxml")
        except Exception as exc:  # noqa: BLE001 - any failure means "no tables"
            log.debug("no tabular data in %s: %s: %s",
                      graph.filename, type(exc).__name__, exc)
            return
        for frame_no, frame in enumerate(frames):
            if frame is not None and not frame.empty:
                self._absorb(frame, graph, frame_no)
        if graph.data_records:
            log.info("%s: %d tables -> %d records",
                     graph.filename, len(frames), len(graph.data_records))

    def _absorb(self, frame: pd.DataFrame, graph: DocumentGraph, frame_no: int) -> None:
        frame = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")
        if frame.empty:
            return

        header, body = _split_header(frame)
        periods = [clean_text(str(c)) for c in header]
        owner = self._owner(header, graph)

        for label, values in _iter_rows(body, periods):
            if not label or canonical_entity(label) is None:
                continue
            for period, value in values:
                record = DataRecord(
                    id=stable_id(
                        "dr", graph.document_id, str(frame_no), label, period, value
                    ),
                    label=label,
                    value=value,
                    period=period,
                )
                graph.data_records.append(record)
                graph.stats["data_records"] += 1
                if owner is not None:
                    graph.edges.append(
                        Edge("RECORDED", "Entity", owner.name, "DataRecord", record.id)
                    )
                    graph.stats["recorded"] += 1

    @staticmethod
    def _owner(header: Sequence[str], graph: DocumentGraph) -> EntityRecord | None:
        """Choose the entity a table's figures belong to.

        Candidates come from the table's own header or caption. Type decides the
        winner: a company beats a person beats a place, because a cover page
        naming both "California" and "Apple Inc." is a statement about Apple.
        When the header offers nothing usable -- a financial statement headed
        only by years -- the filing's own subject is used instead, which is what
        makes ``Net sales | 2026 | 91,952`` reachable from the company node.
        """
        by_type: dict[str, EntityRecord] = {}
        for candidate in header:
            if not candidate or _is_numberish(candidate):
                continue
            record = graph.entity_for(candidate)
            if record is None:
                continue
            by_type.setdefault(record.type, record)
        for label in ("ORG", "PERSON", "GPE", "PRODUCT"):
            if label in by_type:
                return by_type[label]
        return graph.primary_entity()


def _split_header(frame: pd.DataFrame) -> tuple[list[str], pd.DataFrame]:
    """Use the first row as the period header when it looks like one.

    ``read_html`` promotes a real ``<thead>`` into ``frame.columns``. When the
    markup has no header row the first body row *is* the header, and keeping it
    would invent a data record labelled "Net sales".
    """
    first = [clean_text(str(v)) for v in frame.iloc[0].tolist()]
    body = frame.iloc[1:]
    non_numeric = sum(1 for c in first if c and not _is_numberish(c))
    if len(first) > 1 and non_numeric >= 2 and not _is_numberish(first[0]):
        return first, body
    return [clean_text(str(c)) for c in frame.columns], frame


def _iter_rows(
    body: pd.DataFrame, periods: Sequence[str]
) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """Yield ``(label, [(period, value), ...])`` for each populated row."""
    for _, row in body.iterrows():
        cells = [clean_text("" if pd.isna(v) else str(v)) for v in row.tolist()]
        if not cells or not cells[0]:
            continue
        values: list[tuple[str, str]] = []
        for offset, value in enumerate(cells[1:], start=1):
            if not value:
                continue
            values.append((periods[offset] if offset < len(periods) else "", value))
        if values:
            yield cells[0], values


# --------------------------------------------------------------------------
# NLP
# --------------------------------------------------------------------------


class NLPExtractor:
    """Local spaCy: named entities plus subject-verb-object triples.

    Runs entirely on CPU with no network access. Sentences go through
    ``nlp.pipe`` in batches because per-sentence calls dominate the cost. The
    lemmatizer stays enabled because the relation verb is read from ``lemma_``.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._nlp: Any = None

    def load(self) -> bool:
        """Load the model. Returns False when it is unavailable."""
        if self.config.disable_nlp:
            log.info("NLP disabled by configuration")
            return False
        try:
            import spacy
        except ImportError:
            log.warning("spaCy not installed; skipping NLP extraction")
            return False
        try:
            self._nlp = spacy.load("en_core_web_sm")
        except OSError:
            log.warning(
                "spaCy model en_core_web_sm not found; skipping NLP extraction. "
                "Install it with: python -m spacy download en_core_web_sm"
            )
            return False
        self._nlp.max_length = max(self._nlp.max_length, self.config.max_chars)
        log.info("spaCy ready: %s", spacy.info().get("version", "?"))
        return True

    def parse(self, graph: DocumentGraph) -> None:
        if self._nlp is None or not graph.chunks:
            return
        chunks = graph.chunks
        texts = [c.text for c in chunks]
        for offset in range(0, len(texts), self.config.nlp_batch):
            window_text = texts[offset : offset + self.config.nlp_batch]
            window_chunks = chunks[offset : offset + self.config.nlp_batch]
            for chunk, doc in zip(window_chunks, self._nlp.pipe(window_text, batch_size=8)):
                self._absorb(doc, graph, chunk)

    def _absorb(self, doc: Any, graph: DocumentGraph, chunk: ChunkRecord) -> None:
        mentioned: dict[str, str] = {}  # lowercased surface text -> entity name

        for ent in doc.ents:
            if ent.label_ not in ENTITY_LABELS:
                continue
            record = graph.add_entity(ent.text, ent.label_)
            if record is None:
                continue
            mentioned[ent.text.lower()] = record.name
            graph.edges.append(
                Edge("MENTIONS", "Chunk", chunk.id, "Entity", record.name)
            )
            graph.stats["mentions"] += 1

        for subject, verb, obj in _triples(doc, mentioned):
            graph.edges.append(
                Edge(
                    "RELATION",
                    "Entity",
                    subject,
                    "Entity",
                    obj,
                    {"verb": verb, "context": _shared_sentence(doc, subject, obj)},
                )
            )
            graph.stats["relations"] += 1


def _triples(doc: Any, mentioned: dict[str, str]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(subject, verb, object)`` for anchored S-V-O parses.

    Both arguments must already be known entities. A verb with nothing known on
    one side is skipped: it produces relationship noise rather than knowledge.
    """
    for token in doc:
        if token.pos_ not in {"VERB", "AUX"} or token.dep_ not in {"ROOT", "xcomp", "conj"}:
            continue
        subject = _arg(token, {"nsubj", "csubj"})
        obj = _arg(token, {"dobj", "pobj", "attr", "oprd"})
        if subject is None or obj is None:
            continue
        subject_name = _match_known(subject, mentioned)
        object_name = _match_known(obj, mentioned)
        if not subject_name or not object_name or subject_name == object_name:
            continue
        yield subject_name, (token.lemma_ or token.lower_).lower(), object_name


def _arg(token: Any, wanted: set[str]) -> Any | None:
    return next((c for c in token.children if c.dep_ in wanted), None)


def _match_known(token: Any, mentioned: dict[str, str]) -> str | None:
    """Resolve a syntactic argument to an entity already in the graph.

    The token's own subtree is checked before its ancestors, because what the
    parser attached is often a determiner or preposition wrapping the real name
    (``the company's`` -> ``Apple Inc.``).
    """
    for candidate in token.subtree:
        hit = mentioned.get(candidate.text.lower())
        if hit:
            return hit
    for ancestor in token.ancestors:
        for candidate in ancestor.subtree:
            hit = mentioned.get(candidate.text.lower())
            if hit:
                return hit
    return mentioned.get(token.text.lower())


def _shared_sentence(doc: Any, subject: str, obj: str, width: int = 160) -> str:
    """The sentence the two entities share, kept for later audit."""
    for sent in doc.sents:
        text = sent.text
        if subject.lower() in text.lower() and obj.lower() in text.lower():
            return clean_text(text)[:width]
    return ""


# --------------------------------------------------------------------------
# Bulk writer
# --------------------------------------------------------------------------


class GraphWriter:
    """Buffers a whole document and writes it with batched statements.

    Two write paths, both a single statement per table:

    * ``COPY <table> FROM $data`` with a pyArrow table, above ``copy_threshold``.
    * ``UNWIND $rows AS r CREATE (...)`` below it, where building an Arrow
      table costs more than it saves.

    Neither is a per-row loop. Both require the pre-flight key check described
    in the module docstring, because this engine deadlocks on a duplicate key
    rather than raising.
    """

    #: Cap on keys per existence query, to bound the parameter list.
    KEY_WINDOW = 900

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.path = Path(config.database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.database = lb.Database(
                str(self.path),
                buffer_pool_size=config.buffer_pool_mb * 1024 * 1024,
                max_num_threads=config.max_num_threads,
                compression=config.compression,
                read_only=config.read_only,
            )
        except RuntimeError as exc:
            # A database killed mid-write leaves a write-ahead log behind, and
            # the next open then fails with a message about a "temporary file"
            # that means nothing to whoever is restarting the daemon.
            if ".wal" in str(exc):
                raise RuntimeError(
                    f"{self.path} has a stale write-ahead log from a previous "
                    f"run that did not shut down cleanly ({self.path}.wal). "
                    f"Recover it by opening the database once with a matching "
                    f"engine version, or delete the .wal file if the last run's "
                    f"work is expendable. Original error: {exc}"
                ) from exc
            raise
        self.connection = lb.Connection(self.database)
        self._closed = False
        for statement in schema_ddl():
            self.connection.execute(statement)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closable in (self.connection, self.database):
            close = getattr(closable, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - best effort
                    pass

    def __enter__(self) -> "GraphWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- existence checks --------------------------------------------------

    def _existing_keys(self, table: str, keys: Sequence[str]) -> set[str]:
        """Primary keys of *table* already present.

        This is the guard that stops a re-run from deadlocking ``COPY``.
        """
        if not keys:
            return set()
        column = PRIMARY_KEYS[table]
        present: set[str] = set()
        for start in range(0, len(keys), self.KEY_WINDOW):
            window = list(keys[start : start + self.KEY_WINDOW])
            rows = self.connection.execute(
                f"MATCH (n:{table}) WHERE list_contains($keys, n.{column}) "
                f"RETURN n.{column}",
                {"keys": window},
            ).get_all()
            present.update(row[0] for row in rows)
        return present

    def _existing_property_edges(
        self, edges: Sequence[Edge], props: Sequence[str]
    ) -> set[tuple[str, ...]]:
        """Arcs of a property-bearing table that already exist.

        Property-free arcs cannot repeat across documents, because both
        endpoints are document-scoped ids. ``RELATION`` arcs connect shared
        entity nodes, so the same fact can be stated by two filings and has to
        be de-duplicated against what is already stored.

        Returned keys are ``(source, target, *prop values)`` in the order of
        *props*, which is the shape :meth:`_insert_edges` filters on.
        """
        if not edges or not props:
            return set()
        rel = edges[0].rel
        source_table = edges[0].source_table
        target_table = edges[0].target_table
        source_column = PRIMARY_KEYS[source_table]
        target_column = PRIMARY_KEYS[target_table]
        projection = ", ".join(f"e.{p}" for p in props)
        found: set[tuple[str, ...]] = set()
        for window in _windows(edges, self.KEY_WINDOW):
            rows = self.connection.execute(
                f"MATCH (a:{source_table})-[e:{rel}]->(b:{target_table}) "
                f"WHERE list_contains($sk, a.{source_column}) "
                f"AND list_contains($tk, b.{target_column}) "
                f"RETURN a.{source_column}, b.{target_column}, {projection}",
                {
                    "sk": [e.source_key for e in window],
                    "tk": [e.target_key for e in window],
                },
            ).get_all()
            for row in rows:
                found.add((row[0], row[1], *row[2 : 2 + len(props)]))
        return found

    # -- writing -----------------------------------------------------------

    def write(self, graph: DocumentGraph) -> Counter:
        """Persist one buffered document. Returns the row counts written."""
        written: Counter = Counter()
        self.connection.execute("BEGIN TRANSACTION")
        try:
            written["documents"] += self._insert(
                "Document",
                [{
                    "id": graph.document_id,
                    "title": graph.title,
                    "filename": graph.filename,
                    "timestamp": graph.timestamp,
                }],
            )
            if not written["documents"]:
                # Already ingested. Every id in this document is content
                # addressed, so the nodes are present and re-inserting the arcs
                # would duplicate them.
                self.connection.execute("ROLLBACK")
                log.info("%s: already ingested, nothing to do", graph.filename)
                return written

            written["sections"] += self._insert(
                "Section",
                [{"id": s.id, "title": s.title, "level": s.level} for s in graph.sections],
            )
            written["chunks"] += self._insert(
                "Chunk",
                [{"id": c.id, "text": c.text, "chunk_index": c.chunk_index}
                 for c in graph.chunks],
            )
            written["entities"] += self._insert(
                "Entity",
                [{"name": e.name, "type": e.type} for e in graph.entities.values()],
            )
            written["data_records"] += self._insert(
                "DataRecord",
                [{"id": d.id, "label": d.label, "value": d.value, "period": d.period}
                 for d in graph.data_records],
            )
            for rel, source_table, target_table, edges in _edge_groups(graph):
                written[f"rel:{rel}"] += self._insert_edges(
                    rel, source_table, target_table, edges
                )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return written

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Insert node rows, skipping any whose primary key already exists."""
        if not rows:
            return 0
        columns = NODE_TABLES[table]
        key_column = PRIMARY_KEYS[table]
        present = self._existing_keys(table, [str(r[key_column]) for r in rows])

        fresh: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            key = str(row[key_column])
            if key in present or key in seen:
                continue
            seen.add(key)
            fresh.append({name: row[name] for name in columns})
        if not fresh:
            log.debug("%s: all %d rows already present", table, len(rows))
            return 0

        if len(fresh) >= self.config.copy_threshold:
            arrow = pa.table(
                {n: pa.array([r[n] for r in fresh], type=_arrow_type(n)) for n in columns}
            )
            self.connection.execute(f"COPY {table} FROM $data", {"data": arrow})
        else:
            assignments = ", ".join(f"{n}: r.{n}" for n in columns)
            self.connection.execute(
                f"UNWIND $rows AS r CREATE (:{table} {{{assignments}}})",
                {"rows": fresh},
            )
        return len(fresh)

    def _insert_edges(
        self, rel: str, source_table: str, target_table: str, edges: list[Edge]
    ) -> int:
        """Insert arcs whose endpoints are all present in the database."""
        if not edges:
            return 0
        present_source = self._existing_keys(source_table, sorted({e.source_key for e in edges}))
        present_target = self._existing_keys(target_table, sorted({e.target_key for e in edges}))
        usable = [
            e for e in edges
            if e.source_key in present_source and e.target_key in present_target
        ]
        if len(usable) < len(edges):
            log.warning(
                "%s: dropped %d arcs with a missing endpoint", rel, len(edges) - len(usable)
            )
        if not usable:
            log.debug("%s: no arcs with both endpoints present", rel)
            return 0

        props = rel_properties(rel)
        unique: dict[tuple[str, str, str, str, str], Edge] = {}
        for edge in usable:
            unique.setdefault((*edge.identity(), edge.props_signature()), edge)
        rows = list(unique.values())

        if props:
            seen = self._existing_property_edges(rows, props)
            if seen:
                before = len(rows)
                rows = [
                    e for e in rows
                    if (e.source_key, e.target_key,
                        *(e.props.get(p, "") for p in props)) not in seen
                ]
                log.debug("%s: skipped %d arcs already present", rel, before - len(rows))
        if not rows:
            return 0

        if len(rows) >= self.config.copy_threshold:
            data = {
                "from": [e.source_key for e in rows],
                "to": [e.target_key for e in rows],
                **{p: [e.props.get(p, "") for e in rows] for p in props},
            }
            arrow = pa.table({k: pa.array(v, type=pa.string()) for k, v in data.items()})
            self.connection.execute(f"COPY {rel} FROM $data", {"data": arrow})
        else:
            source_column = PRIMARY_KEYS[source_table]
            target_column = PRIMARY_KEYS[target_table]
            body = "{" + ", ".join(f"{p}: r.{p}" for p in props) + "}" if props else ""
            payload = [
                {"fk": e.source_key, "tk": e.target_key, **e.props} for e in rows
            ]
            self.connection.execute(
                f"UNWIND $rows AS r "
                f"MATCH (a:{source_table} {{{source_column}: r.fk}}), "
                f"(b:{target_table} {{{target_column}: r.tk}}) "
                f"CREATE (a)-[:{rel}{body}]->(b)",
                {"rows": payload},
            )
        return len(rows)


def _windows(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _edge_groups(
    graph: DocumentGraph,
) -> Iterator[tuple[str, str, str, list[Edge]]]:
    grouped: dict[tuple[str, str, str], list[Edge]] = {}
    for edge in graph.edges:
        grouped.setdefault((edge.rel, edge.source_table, edge.target_table), []).append(edge)
    for (rel, source_table, target_table), edges in grouped.items():
        yield rel, source_table, target_table, edges


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


def _title_of(html: str, path: Path) -> str:
    for pattern in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>"):
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            title = clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))
            if title:
                return title[:300]
    return path.stem[:300]


class IngestionEngine:
    """Ties the parsers to the writer and owns file routing.

    Each document gets its own short-lived :class:`GraphWriter`. That is not
    tidiness, it is a workaround: on LadybugDB 0.20.4 a connection that has
    committed a sizeable write will deadlock on the next parameterised read,
    and the stall happens inside native code where a Python signal cannot
    interrupt it. Reopening is cheap for an embedded database and gives every
    document a clean connection.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        config.ensure_dirs()
        # Validate the schema once up front so a bad path fails at startup
        # rather than on the first file that arrives.
        with GraphWriter(config):
            pass
        self.structural = StructuralParser(config)
        self.tables = TableParser(config)
        self.nlp = NLPExtractor(config)
        self.nlp.load()
        self._lock = threading.Lock()
        self.processed_count = 0
        self.failed_count = 0

    def close(self) -> None:
        """Present for symmetry; writers are closed as they are used."""

    # -- per-document ------------------------------------------------------

    def process(self, path: Path) -> Counter:
        """Extract one document and bulk-load it. Raises on unusable input."""
        html = path.read_text(encoding="utf-8", errors="replace")
        if not html.strip():
            raise ValueError("file is empty")
        if len(html) > self.config.max_chars:
            log.warning(
                "%s is %.1f MB; truncating to %d characters",
                path.name, len(html) / 1e6, self.config.max_chars,
            )
            html = html[: self.config.max_chars]

        started = time.perf_counter()
        graph = DocumentGraph(
            document_id=stable_id("doc", path.name, str(path.stat().st_size)),
            title=_title_of(html, path),
            filename=path.name,
            timestamp=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(
                tzinfo=None
            ),
        )
        self.structural.parse(html, graph)
        # NLP runs before the tables on purpose. A table is attributed to the
        # company named in its header, and that company is only known as an
        # entity once NER has run -- parsing tables first left every RECORDED
        # edge unattached.
        self.nlp.parse(graph)
        self.tables.parse(html, graph)
        extracted = time.perf_counter() - started

        # The writer owns a single connection, so documents are committed one at a
        # time. Extraction is the expensive part and is not serialised.
        with self._lock, GraphWriter(self.config) as writer:
            written = writer.write(graph)
        elapsed = time.perf_counter() - started

        graph.stats["seconds_extract"] = round(extracted, 3)
        graph.stats["seconds_total"] = round(elapsed, 3)
        log.info(
            "%s: %d sections, %d chunks, %d entities, %d records, %d edges "
            "in %.2fs (extract %.2fs) -> %s",
            path.name, len(graph.sections), len(graph.chunks), len(graph.entities),
            len(graph.data_records), len(graph.edges), elapsed, extracted,
            dict(written) or "nothing new",
        )
        return graph.stats

    def ingest_path(self, path: Path) -> bool:
        """Process *path*, route the file, and report success. Never raises.

        Used by the watcher, where a file has to end up in exactly one of
        ``processed``/``failed``. ``--batch`` deliberately does not use this: it
        processes in place so a backfill directory is left intact.
        """
        try:
            self.process(path)
        except Exception as exc:  # noqa: BLE001 - a daemon must not die on one file
            self.failed_count += 1
            log.error("failed %s: %s: %s", path.name, type(exc).__name__, exc)
            self._move(path, self.config.failed, path.name)
            return False
        self.processed_count += 1
        self._move(path, self.config.processed, path.name)
        return True

    @staticmethod
    def _move(source: Path, folder: Path, name: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        if target.exists():
            target = folder / f"{uuid.uuid4().hex[:8]}_{name}"
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:  # pragma: no cover - cross-device moves
            log.warning("could not move %s: %s", source, exc)

    # -- folders -----------------------------------------------------------

    def batch(self, folder: Path) -> Counter:
        """Ingest every supported file under *folder* and return totals.

        A file that cannot be processed is reported and the run continues: one
        malformed filing should not abandon a backfill. Files are left where
        they are -- a backfill directory belongs to the caller -- so the summary
        names every failure rather than quietly skipping it.
        """
        skip = {self.config.processed.resolve(), self.config.failed.resolve()}
        candidates = sorted(
            p for p in folder.rglob("*")
            if p.is_file()
            and p.suffix.lower() in HTML_SUFFIXES
            and p.parent.resolve() not in skip
        )
        log.info("batch: %d candidate files under %s", len(candidates), folder)
        totals: Counter = Counter()
        failures: list[str] = []
        for path in candidates:
            try:
                self.process(path)
            except Exception as exc:  # noqa: BLE001 - keep the backfill going
                self.failed_count += 1
                failures.append(f"{path.name} ({type(exc).__name__})")
                log.error("failed %s: %s: %s", path.name, type(exc).__name__, exc)
            else:
                totals["files"] += 1
        if failures:
            log.warning("%d file(s) failed and were left in place: %s",
                        len(failures), ", ".join(failures))
        return totals

    def stats(self) -> dict[str, int]:
        with GraphWriter(self.config) as writer:
            return {
                table: writer.connection.execute(
                    f"MATCH (n:{table}) RETURN count(n)"
                ).get_next()[0]
                for table in NODE_TABLES
            }


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------


class _Handler(FileSystemEventHandler):
    """Debounces write storms, then feeds settled files to the engine."""

    def __init__(self, engine: IngestionEngine, settle_seconds: float = 1.0) -> None:
        self.engine = engine
        self.settle = settle_seconds
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def _consider(self, raw: str) -> None:
        path = Path(raw)
        if path.suffix.lower() not in HTML_SUFFIXES or not path.is_file():
            return
        with self._lock:
            self._pending[str(path)] = time.monotonic()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def seed(self) -> int:
        """Queue whatever is already waiting in the watched folder.

        ``on_created`` only fires for changes made after the observer starts, so
        a daemon launched with a backlog in ``incoming_docs`` would otherwise sit
        there looking healthy while ignoring every file in it.
        """
        found = 0
        for path in sorted(self.engine.config.incoming.iterdir()):
            if path.is_file() and path.suffix.lower() in HTML_SUFFIXES:
                self._consider(str(path))
                found += 1
        if found:
            log.info("seeded %d file(s) already in %s",
                     found, self.engine.config.incoming)
        return found

    def drain(self) -> None:
        """Ingest every pending file that has stopped changing."""
        now = time.monotonic()
        with self._lock:
            due = [p for p, seen in self._pending.items() if now - seen >= self.settle]
            for path in due:
                del self._pending[path]
        for path in due:
            source = Path(path)
            if source.exists():
                self.engine.ingest_path(source)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.drain()
            stop.wait(0.5)


def watch(engine: IngestionEngine, stop: threading.Event) -> None:
    """Block, watching ``incoming_docs`` until *stop* is set."""
    handler = _Handler(engine)
    handler.seed()
    observer = Observer()
    observer.schedule(handler, str(engine.config.incoming), recursive=False)
    observer.start()
    worker = threading.Thread(target=handler.run, args=(stop,), daemon=True)
    worker.start()
    log.info("watching %s (Ctrl-C to stop)", engine.config.incoming)

    def _shutdown(*_: object) -> None:
        log.info("shutting down")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except ValueError:  # pragma: no cover - not on the main thread
            pass

    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        handler.drain()
        observer.stop()
        observer.join(timeout=5)
        log.info("processed %d, failed %d", engine.processed_count, engine.failed_count)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    default = EngineConfig()
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=default.database, help="graph database path")
    parser.add_argument("--incoming", type=Path, default=default.incoming)
    parser.add_argument("--processed", type=Path, default=default.processed)
    parser.add_argument("--failed", type=Path, default=default.failed)
    parser.add_argument("--batch", type=Path, metavar="FOLDER",
                        help="ingest a folder once and exit")
    parser.add_argument("--watch", action="store_true",
                        help="watch --incoming and never exit")
    parser.add_argument("--no-nlp", action="store_true", help="skip spaCy extraction")
    parser.add_argument("--copy-threshold", type=int, default=default.copy_threshold,
                        help="row count above which Arrow COPY is used")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.batch and not args.watch:
        parser.print_help()
        return 2

    config = EngineConfig(
        database=args.db,
        incoming=args.incoming,
        processed=args.processed,
        failed=args.failed,
        copy_threshold=args.copy_threshold,
        disable_nlp=args.no_nlp,
    )
    engine = IngestionEngine(config)
    try:
        if args.batch:
            started = time.perf_counter()
            totals = engine.batch(args.batch)
            log.info("batch complete: %d files in %.1fs", totals["files"],
                     time.perf_counter() - started)
            log.info("graph: %s", engine.stats())
            return 0
        stop = threading.Event()
        watch(engine, stop)
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
