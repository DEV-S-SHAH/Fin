"""End-to-end ingestion: PDF -> chunks -> extraction -> graph."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import GraphRAGConfig
from .document import Document, chunk_document, load_pdf
from .extract import extract_chunk
from .llm import LLMClient
from .store import GraphStore

log = logging.getLogger("graphrag.ingest")


@dataclass
class IngestReport:
    """What an ingestion run actually did."""

    document: str
    pages: int = 0
    chunks: int = 0
    entities_seen: int = 0
    relationships_seen: int = 0
    nodes_upserted: int = 0
    edges_upserted: int = 0
    nodes_added: int = 0
    edges_added: int = 0
    rejected: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        lines = [
            f"document      : {self.document}",
            f"pages         : {self.pages}",
            f"chunks        : {self.chunks}",
            f"entities seen : {self.entities_seen}",
            f"relations seen: {self.relationships_seen}",
            f"nodes upserted: {self.nodes_upserted}",
            f"edges upserted: {self.edges_upserted}",
            f"nodes new    : {self.nodes_added}",
            f"edges new    : {self.edges_added}",
        ]
        if self.failures:
            lines.append(f"chunk failures: {len(self.failures)}")
            for failure in self.failures[:5]:
                lines.append(f"  - {failure}")
        if self.rejected:
            lines.append(f"records rejected: {self.rejected} (failed validation)")
        if self.warnings:
            lines.append("warnings:")
            for warning in self.warnings[:5]:
                lines.append(f"  - {warning}")
        return "\n".join(lines)


def ingest_document(
    store: GraphStore,
    client: LLMClient,
    document: Document,
    config: GraphRAGConfig | None = None,
    progress: bool = False,
) -> IngestReport:
    """Extract a parsed document into the graph.

    Writes are idempotent, so this function is safe to call repeatedly on the
    same document: entities merge by slug and relationships merge on
    (source, target, relation).
    """
    config = config or store.config
    report = IngestReport(document=str(document.path), pages=document.page_count)

    chunks = chunk_document(document, config.chunk_tokens, config.chunk_overlap_tokens)
    report.chunks = len(chunks)
    if not chunks:
        report.failures.append("no extractable text found in document")
        return report

    nodes_before, edges_before = store.counts()
    # Fan-out is counted per run rather than by querying the graph, so a warning
    # is not skewed by edges a previous run already stored.
    fan_out: Counter[tuple[str, str]] = Counter()

    for position, chunk in enumerate(chunks, start=1):
        if progress:
            log.info("chunk %d/%d (%s)", position, len(chunks), chunk.location)

        extraction = extract_chunk(client, chunk.text, config, chunk.location)
        if extraction.error:
            report.failures.append(f"chunk {chunk.index}: {extraction.error}")
            continue

        report.entities_seen += len(extraction.entities)
        report.relationships_seen += len(extraction.relationships)
        report.rejected += extraction.rejected

        # Nodes before edges, so every edge endpoint is guaranteed to exist.
        page_note = f"Source: {document.path.name}, {chunk.location}."
        for entity in extraction.entities:
            description = entity.description
            if description and page_note not in description:
                description = f"{description} ({page_note})"
            written = store.upsert_entity(entity.name, entity.entity_type, description)
            if written:
                report.nodes_upserted += 1

        for edge in extraction.relationships:
            fan_out[(edge.source, edge.relation)] += 1
            written = store.upsert_edge(
                edge.source, edge.target, edge.relation, edge.description
            )
            if written:
                report.edges_upserted += 1

    nodes_after, edges_after = store.counts()
    report.nodes_added = nodes_after - nodes_before
    report.edges_added = edges_after - edges_before
    report.warnings.extend(_quality_warnings(fan_out, report))
    return report


def _quality_warnings(
    fan_out: Counter[tuple[str, str]], report: IngestReport
) -> list[str]:
    """Flag extraction patterns that suggest the model filled arrays with junk.

    A model asked for relationships will sometimes emit one relation type
    repeatedly from a single source rather than omitting what the passage does
    not support. That is reported rather than silently dropped, because wide
    fan-out can also be legitimate; the operator decides.

    *fan_out* counts ``(source, relation)`` pairs emitted during this run only,
    so re-ingesting into a populated graph cannot inflate or mask the signal.
    """
    warnings: list[str] = []
    if report.edges_added == 0 or not fan_out:
        return warnings

    total = sum(fan_out.values())
    for (source, rel_type), count in fan_out.most_common(5):
        if count >= 5 and count / total > 0.4:
            warnings.append(
                f"'{source}' has {count} '{rel_type}' relationships, over 40% "
                "of everything added by this run; the model may be padding the "
                "relationship list"
            )
    return warnings


def ingest_pdf(
    pdf_path: str | Path,
    store: GraphStore,
    client: LLMClient,
    config: GraphRAGConfig | None = None,
    progress: bool = False,
) -> IngestReport:
    """Convenience wrapper: load *pdf_path* then ingest it."""
    config = config or store.config
    document = load_pdf(pdf_path)
    if document.token_count == 0:
        report = IngestReport(document=str(pdf_path), pages=document.page_count)
        report.failures.append(
            "no text layer found; the PDF is probably scanned images and needs OCR"
        )
        return report
    store.ensure_schema()
    return ingest_document(store, client, document, config, progress=progress)
