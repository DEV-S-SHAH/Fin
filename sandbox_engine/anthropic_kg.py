"""Anthropic SEC knowledge graph builder.

Searches SEC EDGAR full-text for filings mentioning ``anthropic``, parses
them, extracts entities and relationships, and loads them into a LadybugDB
knowledge graph. Targets < 5 seconds end-to-end with automatic retry on
failure.

Pipeline per attempt:

    1. EDGAR full-text search  (efts.sec.gov)
    2. Fetch matching filing HTML  (www.sec.gov)
    3. Parse with zero-LLM FilingParser  (sandbox_engine.parser)
    4. Extract entities + edges from parsed data
    5. Load into LadybugDB (idempotent MERGE)

On any failure, waits briefly and retries. Each attempt targets < 5 seconds.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .kg_store import Edge, Entity, KGStore
from .parser import FilingParser
from .sec_search import (
    FilingHit,
    SecSearchTimeoutError,
    search_edgar_full_text,
)
from .ssrf import DEFAULT_CONFIG, safe_urlopen, validate_url

log = logging.getLogger("graphrag.anthropic_kg")

DEFAULT_QUERY = "anthropic"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RETRIES = 5
MAX_FILINGS = 3
CHUNKS_PER_FILING = 5


@dataclass
class BuildReport:
    """Result of a knowledge graph build attempt."""

    success: bool
    filings_found: int
    filings_processed: int
    entities_created: int
    edges_created: int
    seconds: float
    error: Optional[str] = None
    attempt: int = 0


def _slugify(text: str) -> str:
    """Deterministic slug for entity IDs."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "entity"


def _entity_id(entity_type: str, name: str, source_url: str = "") -> str:
    """Content-addressed entity ID, deterministic across runs."""
    key = f"{entity_type}|{name}|{source_url}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{_slugify(entity_type)}-{digest}"


def _fetch_filing(
    hit: FilingHit, timeout: float, start_time: float
) -> tuple[Optional[str], Optional[dict]]:
    """Fetch a single filing HTML document.

    Returns ``(html, metadata)`` or ``(None, None)`` on failure.
    """
    import gzip

    elapsed = time.monotonic() - start_time
    remaining = timeout - elapsed
    if remaining <= 0:
        raise SecSearchTimeoutError("Timeout before filing fetch")

    socket_timeout = min(2.0, max(0.01, remaining))

    valid, error = validate_url(hit.url, DEFAULT_CONFIG)
    if not valid:
        log.warning("ssrf_blocked_filing", extra={"url": hit.url, "error": error})
        return None, None

    headers = {
        "User-Agent": "FinancialGraphRAG contact@domain.com",
        "Accept": "text/html,*/*",
        "Accept-Encoding": "gzip",
    }

    try:
        with safe_urlopen(
            hit.url, timeout=socket_timeout, config=DEFAULT_CONFIG, headers=headers
        ) as response:
            payload = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            html = payload.decode("utf-8", errors="replace")
            metadata = {
                "cik": hit.cik,
                "company_name": hit.company_name,
                "form": hit.form,
                "file_date": hit.file_date,
                "accession": hit.accession,
                "primary_document": hit.primary_document,
                "url": hit.url,
            }
            return html, metadata
    except Exception as exc:  # noqa: BLE001 - intentional retry on any failure
        log.warning("filing_fetch_failed", extra={"url": hit.url, "error": str(exc)})
        return None, None


def _parse_and_extract(
    html: str, metadata: dict, parser: FilingParser
) -> tuple[list[Entity], list[Edge]]:
    """Parse filing HTML and extract entities + edges from parsed data."""
    # Write HTML to a temp file so FilingParser.ingest_file can read it.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".htm", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(html)
        tmp_path = tmp.name

    try:
        result = parser.ingest_file(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    entities: list[Entity] = []
    edges: list[Edge] = []
    source_url = metadata.get("url", "")
    company_name = metadata.get("company_name", "Unknown")
    company_cik = metadata.get("cik", "")
    form = metadata.get("form", "")
    file_date = metadata.get("file_date", "unknown date")

    # Company node for the filer.
    company_id = _entity_id("Company", company_name, source_url)
    entities.append(
        Entity(
            id=company_id,
            name=company_name,
            entity_type="Company",
            description=f"SEC filer with CIK {company_cik}",
            source_url=source_url,
        )
    )

    # Filing node.
    filing_id = _entity_id("Filing", f"{company_name} {form} {file_date}", source_url)
    entities.append(
        Entity(
            id=filing_id,
            name=f"{company_name} {form}",
            entity_type="Filing",
            description=f"{form} filed on {file_date}",
            source_url=source_url,
        )
    )
    edges.append(
        Edge(
            from_id=company_id,
            to_id=filing_id,
            rel_type="SUBMITTED",
            description=f"{company_name} submitted {form}",
            source_url=source_url,
        )
    )

    # Search-target company node (Anthropic) linked from every filing.
    anthropic_id = _entity_id("Company", "Anthropic, Inc.", "")
    entities.append(
        Entity(
            id=anthropic_id,
            name="Anthropic, Inc.",
            entity_type="Company",
            description="Search target: SEC filings mentioning Anthropic",
            source_url=source_url,
        )
    )
    edges.append(
        Edge(
            from_id=filing_id,
            to_id=anthropic_id,
            rel_type="MENTIONS",
            description=f"{form} mentions Anthropic",
            source_url=source_url,
        )
    )

    # Metric entities + edges.
    metrics = getattr(result, "metrics", [])
    if isinstance(metrics, dict):
        metrics = list(metrics.values())
    for metric in metrics[:20]:
        metric_name = (
            getattr(metric, "canonical_name", None)
            or getattr(metric, "reported_label", None)
            or "unknown_metric"
        )
        metric_id = _entity_id("Metric", metric_name, source_url)
        entities.append(
            Entity(
                id=metric_id,
                name=metric_name,
                entity_type="Metric",
                description=f"{metric_name} reported in {form}",
                source_url=source_url,
            )
        )
        edges.append(
            Edge(
                from_id=filing_id,
                to_id=metric_id,
                rel_type="REPORTS_METRIC",
                description=f"{form} reports {metric_name}",
                source_url=source_url,
            )
        )

    # Segment entities + edges.
    segments = getattr(result, "segments", [])
    if isinstance(segments, dict):
        segments = list(segments.values())
    for segment in segments[:10]:
        seg_name = getattr(segment, "name", None) or "unknown_segment"
        seg_id = _entity_id("Segment", seg_name, source_url)
        entities.append(
            Entity(
                id=seg_id,
                name=seg_name,
                entity_type="Segment",
                description=f"Reporting segment: {seg_name}",
                source_url=source_url,
            )
        )
        edges.append(
            Edge(
                from_id=filing_id,
                to_id=seg_id,
                rel_type="HAS_SEGMENT",
                description=f"{form} has segment {seg_name}",
                source_url=source_url,
            )
        )

    # Event entities + edges.
    events = getattr(result, "events", [])
    if isinstance(events, dict):
        events = list(events.values())
    for event in events[:10]:
        event_name = (
            getattr(event, "item_title", None)
            or getattr(event, "item_code", None)
            or "unknown_event"
        )
        event_id = _entity_id("Event", event_name, source_url)
        entities.append(
            Entity(
                id=event_id,
                name=event_name,
                entity_type="Event",
                description=getattr(event, "summary", "") or f"8-K item: {event_name}",
                source_url=source_url,
            )
        )
        edges.append(
            Edge(
                from_id=filing_id,
                to_id=event_id,
                rel_type="DISCLOSES_EVENT",
                description=f"{form} discloses {event_name}",
                source_url=source_url,
            )
        )

    # Chunk entities + edges (limited for speed).
    chunks = getattr(result, "chunks", [])
    if isinstance(chunks, dict):
        chunks = list(chunks.values())
    for i, chunk in enumerate(chunks[:CHUNKS_PER_FILING]):
        chunk_text = str(chunk)[:200]
        chunk_id = _entity_id("Chunk", f"chunk-{i}", source_url)
        entities.append(
            Entity(
                id=chunk_id,
                name=f"Chunk {i}",
                entity_type="Chunk",
                description=chunk_text,
                source_url=source_url,
            )
        )
        edges.append(
            Edge(
                from_id=filing_id,
                to_id=chunk_id,
                rel_type="HAS_CHUNK",
                description=f"{form} contains chunk {i}",
                source_url=source_url,
            )
        )

    return entities, edges


def build_anthropic_knowledge_graph(
    query: str = DEFAULT_QUERY,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
    db_path: str | Path = "data/anthropic_kg.lbug",
) -> BuildReport:
    """Build an Anthropic SEC knowledge graph, retrying until success.

    Each attempt targets < ``timeout`` seconds. On any failure, waits briefly
    and retries up to ``max_retries`` times.

    Returns a :class:`BuildReport` with counts and timing.
    """
    start_time = time.monotonic()
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            # Phase 1: EDGAR full-text search.
            remaining = timeout - (time.monotonic() - start_time)
            if remaining <= 0.5:
                raise SecSearchTimeoutError("Insufficient time budget for search")

            hits = search_edgar_full_text(
                query, timeout=min(remaining, 2.0)
            )

            if not hits:
                return BuildReport(
                    success=True,
                    filings_found=0,
                    filings_processed=0,
                    entities_created=0,
                    edges_created=0,
                    seconds=time.monotonic() - start_time,
                    attempt=attempt,
                )

            # Phases 2-5: fetch, parse, extract, load.
            parser = FilingParser()
            all_entities: list[Entity] = []
            all_edges: list[Edge] = []
            processed = 0

            for hit in hits[:MAX_FILINGS]:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0.5:
                    break

                html, metadata = _fetch_filing(hit, remaining, start_time)
                if html is None:
                    continue

                entities, edges = _parse_and_extract(html, metadata, parser)
                all_entities.extend(entities)
                all_edges.extend(edges)
                processed += 1

            # Load into LadybugDB (idempotent MERGE).
            with KGStore(db_path) as store:
                store.ensure_schema()
                for entity in all_entities:
                    store.upsert_entity(entity)
                for edge in all_edges:
                    store.upsert_edge(edge)

            seconds = time.monotonic() - start_time
            log.info(
                "kg_build_complete",
                extra={
                    "attempt": attempt,
                    "entities": len(all_entities),
                    "edges": len(all_edges),
                    "seconds": round(seconds, 2),
                },
            )

            return BuildReport(
                success=True,
                filings_found=len(hits),
                filings_processed=processed,
                entities_created=len(all_entities),
                edges_created=len(all_edges),
                seconds=seconds,
                attempt=attempt,
            )

        except Exception as exc:  # noqa: BLE001 - intentional retry on any failure
            last_error = str(exc)
            log.warning("kg_build_attempt_failed", extra={"attempt": attempt, "error": last_error})
            if attempt < max_retries:
                time.sleep(min(0.2 * attempt, 0.5))

    seconds = time.monotonic() - start_time
    return BuildReport(
        success=False,
        filings_found=0,
        filings_processed=0,
        entities_created=0,
        edges_created=0,
        seconds=seconds,
        error=last_error,
        attempt=max_retries,
    )
