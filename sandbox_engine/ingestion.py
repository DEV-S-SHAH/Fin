"""Canonical four-stage ingestion pipeline for SEC filings.

Stages
------
1. **resolve_scope()** -> ``list[Path]``
   Returns an ordered list of filing file paths to process. Pure function,
   no I/O side effects other than filesystem reads for existence.

2. **parse_all(paths)** -> ``ParseReport``
   Runs the zero-LLM parser over every path, returning a typed report
   with per-filing counts and a concatenated ``ExtractionResult``.

3. **buffer_all(result, staging_dir)** -> ``BufferReport``
   Converts the extraction result into Arrow RecordBatches, spills
   Parquet files to ``staging_dir``, returns a report with row counts,
   bytes written, and sentinel counts.

4. **load_all(buffer, db_path)** -> ``LoadReport``
   Bulk-loads the staged Parquet into LadybugDB via COPY/UNWIND,
   returns a report with inserted row counts per table and timing.

Each stage is independently testable and can be invoked in isolation
(via ``--parse-only``, ``--load-only``, or unit tests).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from .buffer import StageBuffer, BufferReport
from .config import SCOPE, SCOPE_LIMIT, resolve_scope as config_resolve_scope
from .ddl import ensure_schema
from .loader import BulkLoader, LoadReport
from .parser import FilingParser, ExtractionResult, filing_identity
from .ufgs_extract import (
    audit_status_for,
    extract_ufgs,
    fiscal_year_end_rule,
)
from .ufgs_schema import sector_for_sic, sector_for_ticker  # noqa: F401

__all__ = [
    "ParseReport",
    "BufferReport",
    "LoadReport",
    "resolve_scope",
    "parse_all",
    "apply_ufgs",
    "buffer_all",
    "load_all",
    "run_pipeline",
    "PipelineReport",
]

log = logging.getLogger("sandbox_engine.ingestion")


# ---------------------------------------------------------------------------
# Stage 1: Scope resolution
# ---------------------------------------------------------------------------


def resolve_scope(root: Path | None = None) -> list[Path]:
    """Return an ordered list of filing file paths to ingest.

    Uses the config's resolve_scope which uses SCOPE and SCOPE_LIMIT.
    """
    if root is None:
        root = Path(__file__).resolve().parent.parent
    return config_resolve_scope(root)


# ---------------------------------------------------------------------------
# Stage 2: Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseReport:
    """Report emitted by :func:`parse_all`."""

    filings: list[dict[str, Any]] = field(default_factory=list)
    total_metrics: int = 0
    total_segments: int = 0
    total_events: int = 0
    total_chunks: int = 0
    total_executives: int = 0
    total_raw_facts: int = 0
    total_sections: int = 0
    total_footnotes: int = 0
    total_risk_factors: int = 0
    total_causal_relations: int = 0
    total_entities: int = 0
    total_normalizations: int = 0
    elapsed_sec: float = 0.0
    result: ExtractionResult | None = None


def parse_all(paths: list[Path], parser: FilingParser | None = None) -> ParseReport:
    """Parse all filings in ``paths`` and return a combined report.

    Args:
        paths: List of filing file paths to parse.
        parser: Optional ``FilingParser`` instance. If not provided,
            a new one is created with default settings.

    Returns:
        ``ParseReport`` with per-filing breakdown and concatenated
        ``ExtractionResult`` containing all nodes and edges.
    """
    t0 = time.perf_counter()
    parser = parser or FilingParser()
    combined = None
    filing_reports: list[dict[str, Any]] = []

    for path in paths:
        ft0 = time.perf_counter()
        result = parser.ingest_file(path)
        apply_ufgs(result, path)
        elapsed = time.perf_counter() - ft0

        filing_reports.append(
            {
                "file": path.name,
                "ticker": result.metadata.get("ticker"),
                "form_type": result.metadata.get("form_type"),
                "fiscal_year": result.metadata.get("fiscal_year"),
                "metrics": result.counts().get("metrics", 0),
                "segments": result.counts().get("segments", 0),
                "events": result.counts().get("events", 0),
                "chunks": result.counts().get("chunks", 0),
                "executives": result.counts().get("executives", 0),
                "raw_facts": result.counts().get("raw_facts", 0),
                "sections": result.counts().get("sections", 0),
                "risk_factors": result.counts().get("risk_factors", 0),
                "elapsed_sec": elapsed,
            }
        )
        if combined is None:
            combined = result
        else:
            combined.merge(result)

    total_elapsed = time.perf_counter() - t0
    counts = combined.counts() if combined else {}

    return ParseReport(
        filings=filing_reports,
        total_metrics=counts.get("metrics", 0),
        total_segments=counts.get("segments", 0),
        total_events=counts.get("events", 0),
        total_chunks=counts.get("chunks", 0),
        total_executives=counts.get("executives", 0),
        total_raw_facts=counts.get("raw_facts", 0),
        total_sections=counts.get("sections", 0),
        total_footnotes=counts.get("footnotes", 0),
        total_risk_factors=counts.get("risk_factors", 0),
        total_causal_relations=counts.get("causal_relations", 0),
        total_entities=counts.get("entities", 0),
        total_normalizations=counts.get("NORMALIZES_TO", 0),
        elapsed_sec=total_elapsed,
        result=combined,
    )


def apply_ufgs(result: ExtractionResult, path: Path) -> ExtractionResult:
    """Add the Universal Financial Graph Schema layer to one parsed filing.

    Called after :meth:`FilingParser.ingest_file` rather than inside it, for
    two reasons. The UFGS extractor needs the raw markup and the parser holds it
    only for the duration of a call, and the two modules would otherwise import
    each other in a cycle. More importantly the layering is real: the original
    graph answers "what did this filing report in its tables" and the UFGS
    layer answers "what was tagged, where, and what does it normalise to", and
    the two disagree often enough -- an inline-XBRL fact in a dimensional
    context has no table row, a table row may have no fact tag -- that merging
    them inside one function would hide which layer produced what.

    Never raises. A filing whose UFGS extraction fails still contributes its
    original-graph nodes, because a partial graph with a reported gap is
    recoverable and a failed run is not.
    """
    filing_id = filing_identity(result.metadata)
    try:
        bundle = extract_ufgs(
            path.read_text(encoding="utf-8", errors="replace"),
            path,
            result.metadata,
            filing_id,
        )
    except Exception as exc:  # noqa: BLE001 - a UFGS gap must not lose the parse
        log.warning("UFGS extraction failed for %s: %s", path.name, exc)
        result.stats["ufgs_error"] = str(exc)[:200]
        return result

    ticker = result.metadata.get("ticker") or ""
    fye_month, fye_rule = fiscal_year_end_rule(ticker)
    period_end = ""
    for period in bundle.fiscal_periods.values():
        period_end = period.get("period_end_date") or ""
        break
    reporting_lag = -1
    for period in bundle.fiscal_periods.values():
        reporting_lag = period.get("reporting_lag_in_days", -1)
        break

    # The UFGS fields on Company/Filing are additive, so they are written onto
    # the nodes the parser already built rather than replacing them. ``ticker``
    # and ``id`` stay the primary keys, which is what keeps the original
    # graph's arcs resolving.
    result.company.update({
        "sic_code": bundle.stats.get("sic_code", ""),
        "sector": bundle.stats.get("sector") or "",
        "fiscal_year_end_month": fye_month,
        "fiscal_year_end_day_rule": fye_rule,
    })
    result.filing.update({
        "accession_number": bundle.stats.get("accession_number", ""),
        "period_end_date": period_end,
        "reporting_lag_in_days": reporting_lag,
        "audit_status": audit_status_for(result.metadata.get("form_type")),
    })

    result.sections = bundle.sections
    result.raw_facts = bundle.raw_facts
    result.footnotes = bundle.footnotes
    result.risk_factors = bundle.risk_factors
    result.causal_relations = bundle.causal_relations
    result.fiscal_periods = bundle.fiscal_periods
    result.restatements = bundle.restatements
    result.discontinued_segments = bundle.discontinued_segments
    result.sector_overlays = bundle.sector_overlays
    result.entities = bundle.entities
    # Members found only in XBRL context refs extend the parser's segment set
    # rather than replacing it: the table parser reads the visible note and
    # the context reader reads the tags, and they overlap but neither is a
    # superset. ``update`` keeps one node per name, and the parser's
    # ``segment_type`` is not overwritten, so a member typed "geographic" in the
    # note keeps that classification.
    result.segments = {**bundle.segments, **result.segments}
    # StandardizedConcept nodes are the same 33 or 40 rows for every filing, so
    # they are added once per run rather than re-added per filing; ``update``
    # would also grow the dict to 20 entries for 20 filings.
    result.concepts = bundle.concepts
    for rel, rows in bundle.edges.items():
        result.edges.setdefault(rel, []).extend(rows)
    result.stats.update(bundle.stats)
    return result
# ---------------------------------------------------------------------------
# Stage 3: Buffering (Parquet spill)
# ---------------------------------------------------------------------------

def buffer_all(
    result: ExtractionResult,
    staging_dir: Path,
    batch_rows: int = 50000,
) -> BufferReport:
    """Buffer extraction result to Parquet files in ``staging_dir``.

    Args:
        result: The ``ExtractionResult`` from ``parse_all``.
        staging_dir: Directory to write Parquet parts to.
        batch_rows: Maximum rows per RecordBatch before flushing to disk.

    Returns:
        ``BufferReport`` with per-table row counts, bytes written,
        batch counts, and sentinel fiscal year count.
    """
    stage = StageBuffer(staging_dir, batch_rows=batch_rows)
    stage.add_result(result)
    stage.spill()
    return stage


# ---------------------------------------------------------------------------
# Stage 4: Loading (LadybugDB bulk load)
# ---------------------------------------------------------------------------

def load_all(
    buffer: StageBuffer,
    db_path: Path,
    buffer_pool_bytes: int = 256 * 1024 * 1024,
) -> LoadReport:
    """Load staged Parquet files into LadybugDB.

    Args:
        buffer: The ``StageBuffer`` containing staged Parquet files.
        db_path: Path to the LadybugDB database file.
        buffer_pool_bytes: Buffer pool size for the database connection.

    Returns:
        ``LoadReport`` with inserted row counts per table and timing.
    """
    # Ensure schema exists before loading
    import ladybug as lb

    database = lb.Database(str(db_path), buffer_pool_size=buffer_pool_bytes)
    conn = lb.Connection(database)
    ensure_schema(conn)
    conn.close()
    database.close()

    loader = BulkLoader(db_path, buffer_pool_bytes=buffer_pool_bytes)
    return loader.load(buffer)


# ---------------------------------------------------------------------------
# Full pipeline orchestration
# ---------------------------------------------------------------------------

@dataclass
class PipelineReport:
    """Complete pipeline run report."""

    parse: ParseReport | None = None
    buffer: BufferReport | None = None
    load: LoadReport | None = None
    total_elapsed_sec: float = 0.0
    db_path: Path | None = None
    staging_dir: Path | None = None

def run_pipeline(
    db_path: Path,
    staging_dir: Path,
    reset: bool = False,
    parse_only: bool = False,
    load_only: bool = False,
    batch_rows: int = 50000,
    buffer_pool_bytes: int = 256 * 1024 * 1024,
    root: Path | None = None,
) -> PipelineReport:
    """Run the complete four-stage ingestion pipeline.

    Args:
        db_path: Path to LadybugDB database file.
        staging_dir: Directory for Parquet spill.
        reset: If True, wipe database and staging directory before run.
        parse_only: If True, stop after Parquet spill (stage 3).
        load_only: If True, skip parse/buffer and load existing Parquet.
        batch_rows: Max rows per RecordBatch in buffer stage.
        buffer_pool_bytes: Buffer pool size for database connection.
        root: Repository root path for resolving scope. Defaults to sandbox_engine parent.

    Returns:
        ``PipelineReport`` with all stage reports and timing.
    """
    import ladybug as lb

    t0 = time.perf_counter()
    report = PipelineReport(db_path=db_path, staging_dir=staging_dir)

    if reset:
        log.info("RESET: wiping database and staging directory")
        if db_path.exists():
            db_path.unlink()
        if staging_dir.exists():
            import shutil
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: Resolve scope
    log.info("Stage 1: Resolving scope...")
    paths = resolve_scope(root)
    log.info("Resolved %d filing(s)", len(paths))

    if not load_only:
        # Stage 2: Parse all filings
        log.info("Stage 2: Parsing %d filing(s)...", len(paths))
        report.parse = parse_all(paths)
        log.info(
            "Parse complete: %d metrics, %d segments, %d events, %d chunks, %d executives (%.2fs)",
            report.parse.total_metrics,
            report.parse.total_segments,
            report.parse.total_events,
            report.parse.total_chunks,
            report.parse.total_executives,
            report.parse.elapsed_sec,
        )

        # Stage 3: Buffer to Parquet
        log.info("Stage 3: Buffering to Parquet at %s...", staging_dir)
        buffer = buffer_all(report.parse.result, staging_dir, batch_rows=batch_rows)
        report.buffer = buffer.report
        log.info(
            "Buffer complete: %d tables, %d batches, %.2f MB (%.2fs)",
            len(buffer.report.files),
            sum(buffer.report.batches.values()),
            buffer.report.bytes_written / 1e6,
            buffer.report.seconds,
        )

    if parse_only:
        report.total_elapsed_sec = time.perf_counter() - t0
        return report

    if load_only:
        # Reconstruct buffer from existing Parquet
        log.info("Stage 3 (load-only): Reading existing Parquet from %s...", staging_dir)
        buffer = StageBuffer(staging_dir, batch_rows=batch_rows)
        report.buffer = buffer.report

    # Stage 4: Load into LadybugDB
    log.info("Stage 4: Loading into LadybugDB at %s...", db_path)
    report.load = load_all(buffer, db_path, buffer_pool_bytes=buffer_pool_bytes)
    log.info(
        "Load complete: %d node tables, %d rel tables loaded (%.2fs)",
        len([k for k in report.load.inserted if not k.isupper()]),
        len([k for k in report.load.inserted if k.isupper()]),
        report.load.seconds,
    )

    report.total_elapsed_sec = time.perf_counter() - t0
    return report
