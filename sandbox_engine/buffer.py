"""Stage 2: Arrow RecordBatch buffering with a Parquet spill to disk.

Takes the dictionaries from :mod:`sandbox_engine.parser` and turns them into
Arrow tables, accumulating them in ``RecordBatch`` chunks and spilling Parquet
files under the staging directory. The loader in :mod:`sandbox_engine.loader`
reads those files back.

Why this stage exists at all
----------------------------

``COPY <table> FROM $arrow`` wants a ``pyarrow.Table``, so a pipeline could go
straight from parser dictionaries to one giant table. Buffering is worth the
stage because it changes three things that matter:

* **The load is decoupled from the parse.** A run can parse, exit, and load the
  Parquet spill on another machine. ``--parse-only`` writes the spill and
  stops; ``--load-only`` builds the graph from a spill written earlier. The
  pipeline is then debuggable at the seam, which is where a schema mismatch
  between parser and DDL actually shows up.
* **Peak memory is bounded by ``batch_rows``, not by the filing.** Rows
  accumulate into a fixed-size ``RecordBatch``; the batch is flushed and its
  memory released before the next is built. A 75-filing run spills instead of
  growing without limit.
* **The spill is inspectable.** Parquet carries the Arrow schema, so a
  mismatch between what the parser produced and what the DDL expects is a
  readable error from ``pyarrow``, not a silent truncation inside the engine.

The Parquet round trip is a real boundary, not a formality: the loader never
sees the parser's Python objects. That is what makes the type mapping in
:func:`arrow_schema` load-bearing rather than decorative.

Type mapping, and the one place it is lossy
-------------------------------------------

Arrow types are chosen to match the DDL exactly, so ``COPY`` does not have to
coerce. ``filing_date`` is a real ``date32`` because a range comparison against
a date literal should not require string parsing in the query.

``fiscal_year`` is the one lossy column. It is declared ``INT64`` in the DDL and
is an ``int64`` here, but a filing whose fiscal year cannot be resolved yields
``None`` from the parser. ``None`` cannot go in a non-nullable ``int64``, so
those rows are written as a **sentinel** and
:func:`arrow_schema`'s companion check in the loader reports them. Dropping the
row instead would lose a filing that otherwise parsed fine; inventing a year
would misfile it. The sentinel is surfaced, not hidden.

    from sandbox_engine.buffer import StageBuffer

    stage = StageBuffer(staging_dir)
    stage.add_result(result)
    for table, path in stage.spill():
        print(table, path)
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from .parser import ExtractionResult

__all__ = [
    "ARROW_SCHEMAS",
    "BufferReport",
    "StageBuffer",
    "DATE_FLOOR",
    "FISCAL_YEAR_SENTINEL",
    "REL_TABLES",
    "NODE_TABLES",
    "PRIMARY_KEYS",
    "RESULT_NODE_SOURCES",
    "arrow_schema",
    "identity_of",
    "to_arrow",
    "write_parquet",
    "read_parquet",
]

log = logging.getLogger("sandbox_engine.buffer")

# ---------------------------------------------------------------------------
# Table contract
#
# Single source of truth, shared with ddl.py. Column order is significant:
# ``COPY`` maps Arrow columns to table columns by position, so a column inserted
# in the wrong place here silently writes values into the wrong properties.
# ---------------------------------------------------------------------------

#: Node tables, in column order.
#:
#: The first six are the original question-answering graph. The rest implement
#: the Universal Financial Graph Schema (UFGS-2026-09); see
#: :mod:`sandbox_engine.ufgs_schema` for the reference tables. The two layers
#: coexist deliberately -- ``Metric`` is the tabular line item the QA UI
#: queries, ``RawFact`` is the as-filed XBRL fact the universal schema
#: normalizes, and a filing produces both from one parse.
NODE_TABLES: dict[str, tuple[str, ...]] = {
    "Company": ("ticker", "name", "cik", "sic_code", "sector",
                "fiscal_year_end_month", "fiscal_year_end_day_rule"),
    # ``id`` stays the primary key so the existing SUBMITTED/REPORTS_METRIC
    # edges keep resolving; the spec's ``accession_number`` is carried alongside
    # as provenance rather than promoted to the key.
    "Filing": ("id", "form_type", "fiscal_year", "fiscal_period",
               "filing_date", "accession_number", "period_end_date",
               "reporting_lag_in_days", "audit_status", "document_type",
               "document_title", "document_url", "source", "source_authority",
               "filing_status", "is_amended", "amendment_type", "exhibit_type",
               "content_hash", "retrieved_at"),
    # ``period_code``/``period_start``/``period_end``/``period_days`` describe the
    # period the value was measured over; ``period_cumulative`` is 1 for a
    # year-to-date figure and 0 for a discrete quarter or an instant;
    # ``reported_label`` is the column header as printed. ``period_code`` rather
    # than ``period_type`` so the blueprint->engine translation in query_ui does
    # not rewrite it onto the segment edge's ``period`` property.
    "Metric": ("id", "canonical_name", "statement_category",
               "period_code", "period_start", "period_end", "period_days",
               "period_cumulative", "reported_label", "form_type"),
    "Segment": ("name", "segment_type"),
    "Event": ("id", "item_code", "item_title", "summary"),
    "Chunk": ("id", "section", "text"),
    # -- UFGS structural layer ------------------------------------------
    "Section": ("id", "form_type", "item_code", "section_title",
                "char_start", "char_end", "char_count"),
    "FiscalPeriod": ("id", "fiscal_year", "fiscal_quarter", "period_end_date",
                     "calendar_year_overlap", "reporting_lag_in_days",
                     "period_start"),
    "RestatementEvent": ("id", "restatement_type", "materiality",
                         "restatement_reason", "effective_date",
                         "amended_form_type"),
    "DiscontinuedOpsSegment": ("name", "ticker", "disposal_date",
                               "disposal_method"),
    # -- UFGS dual-track layer ------------------------------------------
    "RawFact": ("id", "xbrl_tag", "as_filed_label", "reported_value", "unit",
                "period_type", "period_start", "period_end", "audit_status",
                "presentation_basis", "scale", "decimals", "context_ref",
                "continuing_ops"),
    "StandardizedConcept": ("concept_id", "name", "definition",
                            "statement_type", "sector_applicability"),
    "Footnote": ("id", "note_number", "note_title", "note_text", "note_type"),
    # -- UFGS causal layer ----------------------------------------------
    "RiskFactor": ("id", "item_code", "rf_header", "rf_text",
                   "extracted_entities", "severity", "year_disclosed",
                   "year_removed"),
    "CausalRelation": ("id", "relation_type", "subject_type", "subject_name",
                       "object_type", "object_name", "weight", "magnitude",
                       "source_quote", "context"),
    "ProductFamily": ("name", "issuer", "launch_date", "lifecycle_stage"),
    "GeographicMarket": ("name", "iso_code"),
    "Competitor": ("name", "ticker", "relation_strength"),
    "Supplier": ("name", "relationship_type", "criticality"),
    "Customer": ("name", "concentration_pct"),
    "RegulatoryBody": ("name", "jurisdiction", "scope"),
    "MacroVariable": ("name", "variable_type"),
    "SectorOverlay": ("sector", "overlay_name", "overlay_concept_list"),
    # -- Temporal Hierarchy Layer ---------------------------------------
    "FiscalYear": ("id", "company_ticker", "fiscal_year", "year_start_date", "year_end_date"),
    "FiscalQuarter": ("id", "fiscal_year_id", "quarter_number", "quarter_label", "quarter_start_date", "quarter_end_date"),
    # -- SEC Filing Intelligence Layer ----------------------------------
    "Insider": ("id", "name", "title", "cik", "is_director", "is_officer",
                "is_ten_percent_owner"),
    "InsiderTransaction": ("id", "transaction_date", "transaction_code",
                           "security_title", "shares", "price_per_share",
                           "acquired_disposed", "ownership_form",
                           "direct_indirect", "nature_of_ownership"),
    "InstitutionalHolder": ("id", "name", "cik", "filer_type"),
    "InstitutionalHolding": ("id", "cusip", "security_name", "shares",
                             "value", "put_call", "discretion", "voting_authority"),
    "Shareholder": ("id", "name", "cik", "holder_type"),
    "Shareholding": ("id", "shares", "percent_outstanding", "filing_date"),
    "Security": ("id", "cusip", "isin", "ticker", "security_type",
                 "security_title", "issuer"),
    "CorporateEvent": ("id", "event_type", "event_date", "description",
                       "item_code", "materiality"),
    "CapitalRaise": ("id", "offering_type", "amount", "price",
                     "shares", "underwriters", "use_of_proceeds"),
    "EquityCompensationPlan": ("id", "plan_name", "shares_authorized",
                               "shares_outstanding", "exercise_price"),
    "Exhibit": ("id", "exhibit_number", "exhibit_title", "description"),
    "SupportingDocument": ("id", "doc_type", "title", "description",
                           "source_url", "retrieved_at"),
}

PRIMARY_KEYS: dict[str, str] = {
    "Company": "ticker",
    "Filing": "id",
    "Metric": "id",
    "Segment": "name",
    "Event": "id",
    "Chunk": "id",
    "Section": "id",
    "FiscalPeriod": "id",
    "RestatementEvent": "id",
    "DiscontinuedOpsSegment": "name",
    "RawFact": "id",
    "StandardizedConcept": "concept_id",
    "Footnote": "id",
    "RiskFactor": "id",
    "CausalRelation": "id",
    "ProductFamily": "name",
    "GeographicMarket": "name",
    "Competitor": "name",
    "Supplier": "name",
    "Customer": "name",
    "RegulatoryBody": "name",
    "MacroVariable": "name",
    "SectorOverlay": "sector",
    # -- Temporal Hierarchy Layer ---------------------------------------
    "FiscalYear": "id",
    "FiscalQuarter": "id",
    # -- SEC Filing Intelligence Layer ----------------------------------
    "Insider": "id",
    "InsiderTransaction": "id",
    "InstitutionalHolder": "id",
    "InstitutionalHolding": "id",
    "Shareholder": "id",
    "Shareholding": "id",
    "Security": "id",
    "CorporateEvent": "id",
    "CapitalRaise": "id",
    "EquityCompensationPlan": "id",
    "Exhibit": "id",
    "SupportingDocument": "id",
}

#: Relationship table -> (source node, target node, property columns).
REL_TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # -- original graph -------------------------------------------------
    "SUBMITTED": ("Company", "Filing", ()),
    "REPORTS_METRIC": ("Filing", "Metric", ("value", "currency")),
    "HAS_SEGMENT": ("Metric", "Segment", ("value", "period")),
    "DISCLOSES_EVENT": ("Filing", "Event", ()),
    "HAS_CHUNK": ("Filing", "Chunk", ()),
    # -- UFGS structural ------------------------------------------------
    "FILED": ("Company", "Filing", ()),
    "CONTAINS_SECTION": ("Filing", "Section", ()),
    "REPORTS_FOR": ("Filing", "FiscalPeriod", ()),
    # -- UFGS dual track ------------------------------------------------
    "REPORTED_IN": ("RawFact", "Section", ("item_code",)),
    "NORMALIZES_TO": ("RawFact", "StandardizedConcept",
                      ("rule_id", "transform", "matched_on", "matched_value")),
    "DISCLOSED_IN": ("RawFact", "Footnote", ("detail_type",)),
    # -- UFGS edge cases ------------------------------------------------
    "RESTATES": ("RawFact", "RawFact",
                 ("restatement_type", "materiality", "effective_date")),
    "RETROSPECTIVELY_RECASTS": ("RawFact", "RawFact", ("reason",)),
    "REVISION_OF": ("RawFact", "RawFact", ("materiality", "magnitude_pct")),
    "CLASSIFIED_AS_DISCONTINUED": ("Segment", "DiscontinuedOpsSegment",
                                   ("basis",)),
    "OVERLAY_APPLIES_TO": ("SectorOverlay", "Company", ()),
    # A dimensional fact, split by the taxonomy axis that qualified it. This is
    # the edge that answers "what were net sales in Greater China" or "how much
    # did the iPhone make" with the filer's own tagged figures rather than with
    # a figure scraped out of a table cell. It is not in the spec's relation
    # list, which has no relation for a dimensional breakdown, but a
    # ``RawFact`` with the axis dropped is a fact that can only be aggregated --
    # and 415 of the 1,639 facts in a single 10-K are dimensional.
    "BROKEN_DOWN_BY": ("RawFact", "Segment", ("axis", "member")),
    # -- UFGS causal ----------------------------------------------------
    # The spec's Table 8.2 gives the six typed relations a union endpoint
    # ("Entity -> Outcome"), which no typed rel table can express. The relation
    # is therefore reified: ``CausalRelation`` is the statement node carrying
    # the subject, the verb, and the quote, the six tables below attach the
    # financial outcome, and the seven below attach the subject so traversal
    # still runs Supplier -> CausalRelation -> StandardizedConcept. The
    # relation *names* stay first-class in the graph, which is what makes
    # "which issuers state that inflation IMPACTS_MARGIN" answerable.
    "DRIVES": ("CausalRelation", "StandardizedConcept", ("weight", "magnitude")),
    "IMPACTS_MARGIN": ("CausalRelation", "StandardizedConcept",
                       ("weight", "magnitude")),
    "MITIGATES": ("CausalRelation", "StandardizedConcept",
                  ("weight", "magnitude")),
    "CREATES_EXPOSURE_TO": ("CausalRelation", "StandardizedConcept",
                            ("weight", "magnitude")),
    "COMPOUNDS": ("CausalRelation", "StandardizedConcept",
                  ("weight", "magnitude")),
    "OFFSETS": ("CausalRelation", "StandardizedConcept",
                ("weight", "magnitude")),
    "SUBJECT_PRODUCT_FAMILY": ("ProductFamily", "CausalRelation", ()),
    "SUBJECT_GEOGRAPHIC_MARKET": ("GeographicMarket", "CausalRelation", ()),
    "SUBJECT_COMPETITOR": ("Competitor", "CausalRelation", ()),
    "SUBJECT_SUPPLIER": ("Supplier", "CausalRelation", ()),
    "SUBJECT_CUSTOMER": ("Customer", "CausalRelation", ()),
    "SUBJECT_REGULATORY_BODY": ("RegulatoryBody", "CausalRelation", ()),
    "SUBJECT_MACRO_VARIABLE": ("MacroVariable", "CausalRelation", ()),
    # -- SEC Filing Intelligence Layer ----------------------------------
    "INSIDER_OF": ("Insider", "Company", ()),
    "FILED_INSIDER_FORM": ("Insider", "Filing", ("form_type",)),
    "TRANSACTED": ("InsiderTransaction", "Security", ()),
    "REPORTED_HOLDING": ("InstitutionalHolder", "InstitutionalHolding", ()),
    "HOLDS_SECURITY": ("InstitutionalHolding", "Security", ()),
    "OWNS": ("Shareholder", "Shareholding", ()),
    "SHAREHOLDING_IN": ("Shareholding", "Company", ()),
    "ISSUED": ("Company", "Security", ()),
    "HAS_EVENT": ("Company", "CorporateEvent", ()),
    "DISCLOSED_IN_FILING": ("CorporateEvent", "Filing", ()),
    "RAISED_CAPITAL": ("Company", "CapitalRaise", ()),
    "CAPITAL_RAISE_IN_FILING": ("CapitalRaise", "Filing", ()),
    "HAS_EQUITY_PLAN": ("Company", "EquityCompensationPlan", ()),
    "EQUITY_PLAN_IN_FILING": ("EquityCompensationPlan", "Filing", ()),
    "HAS_EXHIBIT": ("Filing", "Exhibit", ()),
    "HAS_SUPPORTING_DOC": ("Company", "SupportingDocument", ()),
    # -- Amended Filing Relationships -------------------
    "AMENDS": ("Filing", "Filing", ("amendment_type",)),
    # -- Temporal Hierarchy Layer ---------------------------------------
    "HAS_FISCAL_YEAR": ("Company", "FiscalYear", ()),
    "HAS_FISCAL_QUARTER": ("FiscalYear", "FiscalQuarter", ()),
    "FILED_IN_QUARTER": ("Filing", "FiscalQuarter", ()),
}

#: Columns stored as a non-string type.
_INT_COLUMNS = frozenset({
    "fiscal_year", "fiscal_year_end_month", "year_disclosed",
    "year_removed", "calendar_year_overlap", "reporting_lag_in_days",
    "char_start", "char_end", "char_count", "scale", "decimals",
    "period_days", "period_cumulative",
    "shares", "voting_authority", "shares_authorized", "shares_outstanding",
    "quarter_number",
})
_DATE_COLUMNS = frozenset({
    "filing_date", "period_start", "period_end", "period_end_date",
    "effective_date", "disposal_date", "launch_date",
    "transaction_date", "event_date", "retrieved_at",
    "year_start_date", "year_end_date", "quarter_start_date", "quarter_end_date",
})
#: Node columns stored as DOUBLE. Separate from ``_DOUBLE_PROPS`` because that
#: set types rel-table properties; a fact's reported value is a node column and
#: has to go through the same ``column_type``/``_arrow_type`` pair.
_DOUBLE_COLUMNS = frozenset({
    "reported_value", "weight", "magnitude", "concentration_pct",
    "price_per_share", "value", "amount", "price", "exercise_price",
    "percent_outstanding",
})
#: Rel-table properties stored as DOUBLE.
_DOUBLE_PROPS = frozenset({"value", "weight", "magnitude", "concentration_pct"})

#: ``(table, ExtractionResult attribute)`` for the UFGS node collections.
#: Listed here rather than in :meth:`StageBuffer.add_result` so the table
#: contract stays in one place: a table added to :data:`NODE_TABLES` without a
#: source attribute here would be created by the DDL and then never populated,
#: which is the shape of a bug that only shows up as an empty table in a query.
RESULT_NODE_SOURCES: tuple[tuple[str, str], ...] = (
    ("Section", "sections"),
    ("RawFact", "raw_facts"),
    ("StandardizedConcept", "concepts"),
    ("Footnote", "footnotes"),
    ("RiskFactor", "risk_factors"),
    ("CausalRelation", "causal_relations"),
    ("FiscalPeriod", "fiscal_periods"),
    ("RestatementEvent", "restatements"),
    ("DiscontinuedOpsSegment", "discontinued_segments"),
    ("SectorOverlay", "sector_overlays"),
    # -- SEC Filing Intelligence Layer ----------------------------------
    ("Insider", "insiders"),
    ("InsiderTransaction", "insider_transactions"),
    ("InstitutionalHolder", "institutional_holders"),
    ("InstitutionalHolding", "institutional_holdings"),
    ("Shareholder", "shareholders"),
    ("Shareholding", "shareholdings"),
    ("Security", "securities"),
    ("CorporateEvent", "corporate_events"),
    ("CapitalRaise", "capital_raises"),
    ("EquityCompensationPlan", "equity_compensation_plans"),
    ("Exhibit", "exhibits"),
    ("SupportingDocument", "supporting_documents"),
    # -- Temporal Hierarchy Layer ---------------------------------------
    ("FiscalYear", "fiscal_years"),
    ("FiscalQuarter", "fiscal_quarters"),
)

#: Written when a filing's fiscal year could not be resolved. Not a real year:
#: no year in this corpus is below it, so a query can filter it out with
#: ``fiscal_year >= 0`` and the loader reports the count.
FISCAL_YEAR_SENTINEL = -1

#: Written when a filing's date could not be parsed. LadybugDB's ``DATE`` floor;
#: distinguishable from a real filing date because no filing is dated 0001-01-01.
DATE_FLOOR = datetime.date(1, 1, 1)

ARROW_SCHEMAS: dict[str, pa.Schema] = {}


def _arrow_type(name: str) -> pa.DataType:
    """Arrow type for a node column, matching the DDL exactly."""
    if name in _INT_COLUMNS:
        return pa.int64()
    if name in _DATE_COLUMNS:
        return pa.date32()
    if name in _DOUBLE_COLUMNS:
        return pa.float64()
    return pa.string()


# Built after ``_arrow_type`` is defined: the dict comprehension below calls it
# at import time, so declaring the schemas first would raise NameError.
ARROW_SCHEMAS.update(
    {
        table: pa.schema([(name, _arrow_type(name)) for name in columns])
        for table, columns in NODE_TABLES.items()
    }
)
ARROW_SCHEMAS.update(
    {
        rel: pa.schema(
            [("from", pa.string()), ("to", pa.string())]
            + [
                (name, pa.float64() if name in _DOUBLE_PROPS else pa.string())
                for name in props
            ]
        )
        for rel, (_, _, props) in REL_TABLES.items()
    }
)

def arrow_schema(table: str) -> pa.Schema:
    """The Arrow schema for *table* (node or relationship).

    Raises:
        KeyError: *table* is not in the schema. A typo here would otherwise
            produce a Parquet file that the loader silently skips.
    """
    try:
        return ARROW_SCHEMAS[table]
    except KeyError:
        raise KeyError(
            f"unknown table {table!r}; known tables: "
            f"{', '.join(sorted(ARROW_SCHEMAS))}"
        ) from None


# ---------------------------------------------------------------------------
# Dictionary -> Arrow
# ---------------------------------------------------------------------------


def _coerce(value: Any, dtype: pa.DataType) -> Any:
    """One value coerced to *dtype*.

    Every column here is non-nullable by construction, so nothing resolves to
    ``None`` except a ``DOUBLE`` property, where a genuinely absent measurement
    is legitimately null. An empty string stays an empty string rather than
    becoming null: ``Company.cik`` is documented as storing ``""`` for an
    unresolved CIK, and turning that into ``NULL`` would make
    ``cik = ''`` and ``cik IS NULL`` disagree about the same fact.

    ``date32`` and ``int64`` have no empty value, so the two sentinel cases
    above are the only way a bad value reaches disk -- and both are reported by
    the run report rather than being silent.
    """
    if pa.types.is_floating(dtype):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            log.warning("non-numeric value %r in a DOUBLE column; storing null", value)
            return None
    if pa.types.is_date(dtype):
        if isinstance(value, datetime.date):
            return value
        if value is None or value == "":
            return DATE_FLOOR
        try:
            return datetime.date.fromisoformat(str(value)[:10])
        except ValueError:
            # A filing whose date will not parse is stored as the engine's date
            # floor and reported, not dropped: a wrong-looking date is
            # recoverable, a missing filing is not.
            log.warning("unparseable date %r; storing date floor", value)
            return DATE_FLOOR
    if pa.types.is_integer(dtype):
        if value is None or value == "":
            return FISCAL_YEAR_SENTINEL
        try:
            return int(value)
        except (TypeError, ValueError):
            log.warning("unparseable integer %r; storing %d", value, FISCAL_YEAR_SENTINEL)
            return FISCAL_YEAR_SENTINEL
    return "" if value is None else str(value)


def to_arrow(table: str, rows: Sequence[dict[str, Any]]) -> pa.Table:
    """Build a typed ``pa.Table`` for *table* from *rows*.

    Columns are read from the schema, never from the row dicts, so a row missing
    a column yields ``None`` rather than shifting every later column left. That
    is the failure mode that makes a dict-driven Arrow build dangerous: one
    absent key would write ``canonical_name``'s value into ``statement_category``
    and the error would not surface until a query returned nonsense.
    """
    schema = arrow_schema(table)
    columns = {
        field.name: pa.array(
            [_coerce(row.get(field.name), field.type) for row in rows],
            type=field.type,
        )
        for field in schema
    }
    return pa.table(columns, schema=schema)


def write_parquet(table: pa.Table, path: Path, compression: str = "snappy") -> Path:
    """Write *table* to *path*, returning the path.

    ``zstd`` is not the default despite compressing better: this spill is
    written and immediately re-read in the same run, and snappy's write is
    roughly 3x faster for a 4 MB payload. Compression ratio only matters if the
    spill is archived, which this is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression=compression)
    return path


def read_parquet(path: Path) -> pa.Table:
    """Read a spilled Parquet file back as an Arrow table."""
    return pq.read_table(path)


def identity_of(table: str, row: dict[str, Any]) -> tuple[str, ...]:
    """The identity of one staged row.

    A node row is identified by its primary key. A rel row is identified by its
    endpoints *and* its properties, because two arcs between the same pair
    carrying different values are two facts: a later filing restating a number
    must not be deduplicated away.

    Public because the identity rule is the contract benchmark 1 checks the
    loaded graph against, so the benchmark recomputes it from the spilled
    Parquet rather than trusting the loader's own arithmetic.
    """
    if table in NODE_TABLES:
        return (str(row.get(PRIMARY_KEYS[table], "")),)
    _, _, props = REL_TABLES[table]
    return (
        str(row.get("from", "")),
        str(row.get("to", "")),
        *(("" if row.get(name) is None else str(row.get(name))) for name in props),
    )


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------


@dataclass
class BufferReport:
    """What the buffer stage did, for the run report and benchmark 1."""

    rows_per_table: dict[str, int] = field(default_factory=dict)
    distinct_per_table: dict[str, int] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    batches: dict[str, int] = field(default_factory=dict)
    sentinel_fiscal_years: int = 0
    bytes_written: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_per_table": dict(sorted(self.rows_per_table.items())),
            "distinct_per_table": dict(sorted(self.distinct_per_table.items())),
            "files": dict(sorted(self.files.items())),
            "record_batches": dict(sorted(self.batches.items())),
            "sentinel_fiscal_years": self.sentinel_fiscal_years,
            "bytes_written": self.bytes_written,
            "seconds": round(self.seconds, 3),
        }


class StageBuffer:
    """Accumulates parser output into Arrow and spills it to Parquet.

    Rows are held in a pending list per table and flushed into a Parquet file
    whenever that table reaches ``batch_rows``. :meth:`spill` flushes whatever
    is left, so the total row count on disk always equals the total row count
    added, with nothing stranded in memory at the end of a run.

    Alongside the rows, a set of *identity keys* is accumulated per table: the
    primary key for a node table, the endpoint pair plus properties for a rel
    table. The count of distinct keys is what benchmark 1 asserts the loaded
    graph against. It is tracked here, independently of the loader, so the check
    is a real comparison rather than the loader confirming its own arithmetic --
    without it, "stored <= staged" is all that can be asserted, and a loader
    that silently dropped 700 of 796 metrics would pass.
    """

    def __init__(self, staging: str | Path, batch_rows: int = 50_000) -> None:
        self.staging = Path(staging)
        self.batch_rows = max(1, batch_rows)
        self.report = BufferReport()
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._keys: dict[str, set[tuple[str, ...]]] = {}
        self._batches: dict[str, int] = {}
        self._part: dict[str, int] = {}
        self._started: float | None = None

    # -- ingest ------------------------------------------------------------

    def add(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        """Queue *rows* for *table*. Returns the number accepted."""
        arrow_schema(table)          # fail fast on an unknown table name
        materialised = list(rows)
        if not materialised:
            return 0
        keys = self._keys.setdefault(table, set())
        keys.update(identity_of(table, row) for row in materialised)
        self._pending.setdefault(table, []).extend(materialised)
        self._started = self._started or time.perf_counter()
        while len(self._pending[table]) >= self.batch_rows:
            self._flush(table, self.batch_rows)
        return len(materialised)

    def add_result(self, result: ExtractionResult) -> dict[str, int]:
        """Queue every table and edge of one parsed filing.

        Nodes are queued before the edges that reference them. Order is not
        required by the loader, which resolves endpoints itself, but it keeps
        the spill readable in the order a reader expects.
        """
        counts: dict[str, int] = {}
        counts["Company"] = self.add("Company", [result.company])
        counts["Filing"] = self.add("Filing", [result.filing])
        for name, nodes in (
            ("Metric", result.metrics),
            ("Segment", result.segments),
            ("Event", result.events),
            ("Chunk", result.chunks),
        ):
            counts[name] = self.add(name, list(nodes.values()))
        for name, nodes in RESULT_NODE_SOURCES:
            counts[name] = self.add(name, list(getattr(result, nodes).values()))
        for table, nodes in result.entities.items():
            counts[table] = self.add(table, list(nodes.values()))
        for rel, rows in result.edges.items():
            counts[rel] = self.add(rel, rows)
        return counts

    # -- spill -------------------------------------------------------------

    def _flush(self, table: str, count: int) -> None:
        """Write *count* pending rows of *table* to the next Parquet part."""
        pending = self._pending[table]
        taken, self._pending[table] = pending[:count], pending[count:]
        arrow = to_arrow(table, taken)
        part = self._part.get(table, 0)
        self._part[table] = part + 1
        path = self.staging / f"{table}.part{part:04d}.parquet"
        write_parquet(arrow, path)
        self._batches[table] = self._batches.get(table, 0) + 1
        self.report.rows_per_table[table] = self.report.rows_per_table.get(table, 0) + len(taken)
        self.report.bytes_written += path.stat().st_size

    def spill(self) -> dict[str, Path]:
        """Flush every remaining row and return ``{table: first part path}``.

        Returns the *first* part per table. The loader reads all parts; this is
        the canonical path, used for the size line in the run report.
        """
        for table in sorted(self._pending):
            if self._pending[table]:
                self._flush(table, len(self._pending[table]))

        first: dict[str, Path] = {}
        for table in sorted(self._part):
            head = self.staging / f"{table}.part0000.parquet"
            first[table] = head
            self.report.files[table] = str(head)
        self.report.batches = dict(self._batches)
        self.report.distinct_per_table = {
            table: len(keys) for table, keys in self._keys.items()
        }
        self.report.sentinel_fiscal_years = self._count_sentinels()
        if self._started is not None:
            self.report.seconds = time.perf_counter() - self._started
        return first

    def _count_sentinels(self) -> int:
        """Filings whose fiscal year could not be resolved.

        Counted from the staged Parquet rather than from the buffer's own state,
        so the number reported is the number that reached disk.
        """
        path = self.staging / "Filing.part0000.parquet"
        if not path.exists():
            return 0
        column = read_parquet(path).column("fiscal_year").to_pylist()
        return sum(1 for value in column if value == FISCAL_YEAR_SENTINEL)

    # -- reading back ------------------------------------------------------

    def parts(self, table: str) -> list[Path]:
        """Every Parquet part for *table*, in order."""
        return sorted(self.staging.glob(f"{table}.part*.parquet"))

    def load_table(self, table: str) -> pa.Table:
        """Concatenate every part of *table* into one Arrow table.

        An empty ``pa.Table`` is returned for a table that was never staged, so
        a filing with no events yields an empty ``Event`` table rather than an
        error. The schema is still attached, which is what lets the loader skip
        the table without a special case.
        """
        parts = self.parts(table)
        if not parts:
            return pa.Table.from_pylist([], schema=arrow_schema(table))
        return pa.concat_tables([read_parquet(part) for part in parts])

    def iter_tables(self) -> Iterator[tuple[str, pa.Table]]:
        """Yield ``(table, arrow)`` for every staged table, nodes then rels."""
        for table in NODE_TABLES:
            yield table, self.load_table(table)
        for rel in REL_TABLES:
            yield rel, self.load_table(rel)

    def clear(self) -> None:
        """Drop the staging directory. Used by ``--reset``."""
        import shutil

        if self.staging.is_dir():
            shutil.rmtree(self.staging)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._pending.clear()
        self._keys.clear()
        self._batches.clear()
        self._part.clear()
        self.report = BufferReport()
