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
from .config import SCOPE_PATTERNS, SCOPE_LIMIT
from .ddl import ensure_schema
from .loader import BulkLoader, LoadReport
from .parser import FilingParser, ExtractionResult

__all__ = [
    "ParseReport",
    "BufferReport",
    "LoadReport",
    "resolve_scope",
    "parse_all",
    "buffer_all",
    "load_all",
    "run_pipeline",
    "PipelineReport",
]

log = logging.getLogger("sandbox_engine.ingestion")


# ---------------------------------------------------------------------------
# Stage 1: Scope resolution
# ---------------------------------------------------------------------------


def resolve_scope() -> list[Path]:
    """Return an ordered list of filing file paths to ingest.

    Uses ``SCOPE_PATTERNS`` and ``SCOPE_LIMIT`` from config. For each
    pattern, resolves the exact file if it exists, otherwise falls back
    to the most recent matching file in the same directory.
    """
    resolved: list[Path] = []
    for form_type, filename, data_dir in SCOPE_PATTERNS[:SCOPE_LIMIT]:
        p = data_dir / filename
        if p.exists():
            resolved.append(p)
        else:
            # glob fallback within the same data_dir
            candidates = sorted(data_dir.glob(f"{form_type}_*"))
            if candidates:
                resolved.append(candidates[-1])
                log.warning("exact file not found; using %s", candidates[-1].name)
            else:
                raise FileNotFoundError(
                    f"No {form_type} filing found in {data_dir}. Expected: {filename}"
                )
    if not resolved:
        raise FileNotFoundError("No filings resolved from scope patterns")
    return resolved


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
    combined = ExtractionResult()
    filing_reports: list[dict[str, Any]] = []

    for path in paths:
        ft0 = time.perf_counter()
        result = parser.ingest_file(path)
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
                "elapsed_sec": elapsed,
            }
        )
        combined.merge(result)

    total_elapsed = time.perf_counter() - t0
    counts = combined.counts()

    return ParseReport(
        filings=filing_reports,
        total_metrics=counts.get("metrics", 0),
        total_segments=counts.get("segments", 0),
        total_events=counts.get("events", 0),
        total_chunks=counts.get("chunks", 0),
        total_executives=counts.get("executives", 0),
        elapsed_sec=total_elapsed,
        result=combined,
    )
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
    return stage.spill()


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

    with lb.connect(str(db_path), buffer_pool_bytes=buffer_pool_bytes) as conn:
        ensure_schema(conn)

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
    paths = resolve_scope()
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
        report.buffer = buffer_all(report.parse.result, staging_dir, batch_rows=batch_rows)
        log.info(
            "Buffer complete: %d tables, %d batches, %.2f MB (%.2fs)",
            len(report.buffer.files),
            sum(report.buffer.batches.values()),
            report.buffer.bytes_written / 1e6,
            report.buffer.seconds,
        )

    if parse_only:
        report.total_elapsed_sec = time.perf_counter() - t0
        return report

    if load_only:
        # Reconstruct buffer from existing Parquet
        log.info("Stage 3 (load-only): Reading existing Parquet from %s...", staging_dir)
        buffer = StageBuffer(staging_dir, batch_rows=batch_rows)
        report.buffer = buffer.spill()

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
