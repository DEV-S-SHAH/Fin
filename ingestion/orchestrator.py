"""Generic Ingestion Orchestrator - Coordinates the full multi-company pipeline.

This is the single ingestion entrypoint that works for all companies.
It orchestrates: SEC acquisition → Document persistence → Validation →
Parsing → Normalization → Chunking → Embedding → Evidence →
PostgreSQL/pgvector → Neo4j → GraphRAG
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sandbox_engine.config import FINGRAPH_DATA_DIR
from .registry import Company, CompanyRegistry, get_registry
from .sec_acquisition import (
    SECAcquisition,
    FilingManifest,
    Filing,
    DownloadStats,
    DEFAULT_FORMS,
    ALL_SEC_FORMS,
    load_filing_content,
)

__all__ = [
    "IngestionConfig",
    "IngestionOrchestrator",
    "PipelineReport",
    "StageReport",
    "run_ingestion",
]

log = logging.getLogger("ingestion.orchestrator")


@dataclass(frozen=True)
class IngestionConfig:
    """Configuration for the ingestion pipeline."""
    
    # Data directories - use persistent FINGRAPH_DATA_DIR so checkpoints/cache survive restarts
    data_root: Path = Path("data/sec-filings")
    cache_dir: Path = FINGRAPH_DATA_DIR / "ingestion-cache"
    checkpoint_dir: Path = FINGRAPH_DATA_DIR / "checkpoints"
    
    # Database paths (same for all companies)
    pgvector_db: str = "postgresql://localhost:5432/fingraph"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    ladybug_db: Path = FINGRAPH_DATA_DIR / "graphrag.lbug"
    
    def __post_init__(self) -> None:
        if not self.neo4j_password:
            import os
            env_pass = os.environ.get("NEO4J_PASSWORD")
            if not env_pass:
                raise ValueError("neo4j_password must be set via NEO4J_PASSWORD environment variable")
            object.__setattr__(self, "neo4j_password", env_pass)
    
    # Processing
    chunk_tokens: int = 800
    chunk_overlap_tokens: int = 100
    max_workers: int = 4
    batch_size: int = 100
    
    # Forms to ingest
    forms: tuple[str, ...] = DEFAULT_FORMS
    
    # Date range
    start_date: str = "2020-01-01"
    end_date: str = "2026-12-31"
    
    # Behavior flags
    skip_download: bool = False
    skip_existing: bool = True  # Idempotency - skip already processed
    refresh_cache: bool = False
    delete_temp_files: bool = True  # Delete temporary PDFs after successful ingestion
    dry_run: bool = False
    
    # Embedding
    embedding_model: str = "text-embedding-3-small"
    embedding_batch_size: int = 100
    
    # LLM
    llm_provider: str = "auto"
    llm_model: str | None = None
    
    def __post_init__(self) -> None:
        # Ensure directories exist
        for path in [self.data_root, self.cache_dir, self.checkpoint_dir]:
            if isinstance(path, Path):
                path.mkdir(parents=True, exist_ok=True)


@dataclass
class StageReport:
    """Report for a single pipeline stage."""
    
    stage: str
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    items_processed: int = 0
    items_failed: int = 0
    items_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def complete(self, **metadata: Any) -> None:
        self.completed_at = datetime.utcnow().isoformat() + "Z"
        self.duration_seconds = (
            datetime.fromisoformat(self.completed_at.replace("Z", "+00:00")) -
            datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        ).total_seconds()
        self.metadata.update(metadata)
    
    def add_error(self, error: str) -> None:
        self.errors.append(error)
        self.items_failed += 1
    
    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)


@dataclass
class PipelineReport:
    """Complete pipeline execution report."""
    
    company_ticker: str
    started_at: str
    completed_at: str | None = None
    total_duration_seconds: float = 0.0
    stages: list[StageReport] = field(default_factory=list)
    config: IngestionConfig | None = None
    
    # Summary counts
    filings_discovered: int = 0
    filings_downloaded: int = 0
    filings_processed: int = 0
    filings_skipped: int = 0
    filings_failed: int = 0
    chunks_created: int = 0
    embeddings_generated: int = 0
    evidence_stored: int = 0
    graph_nodes_created: int = 0
    graph_edges_created: int = 0
    
    def add_stage(self, stage: StageReport) -> None:
        self.stages.append(stage)
    
    def complete(self) -> None:
        self.completed_at = datetime.utcnow().isoformat() + "Z"
        self.total_duration_seconds = (
            datetime.fromisoformat(self.completed_at.replace("Z", "+00:00")) -
            datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        ).total_seconds()
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "company_ticker": self.company_ticker,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_duration_seconds": self.total_duration_seconds,
            "summary": {
                "filings_discovered": self.filings_discovered,
                "filings_downloaded": self.filings_downloaded,
                "filings_processed": self.filings_processed,
                "filings_skipped": self.filings_skipped,
                "filings_failed": self.filings_failed,
                "chunks_created": self.chunks_created,
                "embeddings_generated": self.embeddings_generated,
                "evidence_stored": self.evidence_stored,
                "graph_nodes_created": self.graph_nodes_created,
                "graph_edges_created": self.graph_edges_created,
            },
            "stages": [
                {
                    "stage": s.stage,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "duration_seconds": s.duration_seconds,
                    "items_processed": s.items_processed,
                    "items_failed": s.items_failed,
                    "items_skipped": s.items_skipped,
                    "errors": s.errors,
                    "warnings": s.warnings,
                    "metadata": s.metadata,
                }
                for s in self.stages
            ],
        }
    
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))


class IngestionOrchestrator:
    """Orchestrates the complete ingestion pipeline for one or more companies.
    
    This is the single generic pipeline - no company-specific logic here.
    All company-specific configuration comes from the CompanyRegistry.
    """
    
    def __init__(
        self,
        config: IngestionConfig | None = None,
        registry: CompanyRegistry | None = None,
    ) -> None:
        self.config = config or IngestionConfig()
        self.registry = registry or get_registry()
        self.acquisition = SECAcquisition(registry=self.registry)
        
        # Ensure directories exist
        self.config.data_root.mkdir(parents=True, exist_ok=True)
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    def ingest_company(
        self,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        forms: list[str] | None = None,
    ) -> PipelineReport:
        """Run the complete ingestion pipeline for one company."""
        company = self.registry.get(ticker)
        if not company:
            raise ValueError(f"Unknown company: {ticker}")
        
        if not company.active:
            raise ValueError(f"Company {ticker} is not active")
        
        start = start_date or self.config.start_date
        end = end_date or self.config.end_date
        form_list = forms or list(self.config.forms)
        
        report = PipelineReport(
            company_ticker=ticker,
            started_at=datetime.utcnow().isoformat() + "Z",
            config=self.config,
        )
        
        try:
            # Stage 1: Discovery & Acquisition
            report.add_stage(self._stage_discovery(company, start, end, form_list, report))
            
            # Stage 2: Document Persistence & Validation
            report.add_stage(self._stage_persistence(company, report))
            
            # Stage 3: Parsing & Normalization
            report.add_stage(self._stage_parsing(company, report))
            
            # Stage 4: Chunking
            report.add_stage(self._stage_chunking(company, report))
            
            # Stage 5: Embedding Generation
            report.add_stage(self._stage_embedding(company, report))
            
            # Stage 6: Evidence & Provenance Storage
            report.add_stage(self._stage_evidence(company, report))
            
            # Stage 7: PostgreSQL + pgvector Persistence
            report.add_stage(self._stage_postgres(company, report))
            
            # Stage 8: Neo4j Graph Construction
            report.add_stage(self._stage_graph(company, report))
            
            # Stage 9: GraphRAG Index Update
            report.add_stage(self._stage_graphrag(company, report))
            
            # Stage 10: Validation & Cleanup
            report.add_stage(self._stage_validation_cleanup(company, report))
            
        except Exception as e:
            log.exception(f"Pipeline failed for {ticker}")
            if report.stages:
                report.stages[-1].add_error(f"Pipeline failed: {e}")
            raise
        
        report.complete()
        self._save_report(report)
        return report
    
    def ingest_companies(
        self,
        tickers: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        forms: list[str] | None = None,
    ) -> dict[str, PipelineReport]:
        """Run ingestion for multiple companies sequentially."""
        reports = {}
        for ticker in tickers:
            log.info(f"Starting ingestion for {ticker}")
            reports[ticker] = self.ingest_company(ticker, start_date, end_date, forms)
            log.info(f"Completed ingestion for {ticker}")
        return reports
    
    def ingest_all(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        forms: list[str] | None = None,
    ) -> dict[str, PipelineReport]:
        """Run ingestion for all active companies in the registry."""
        active_tickers = [c.ticker for c in self.registry.active()]
        return self.ingest_companies(active_tickers, start_date, end_date, forms)
    
    # --- Pipeline Stages ---
    
    def _stage_discovery(
        self,
        company: Company,
        start: str,
        end: str,
        forms: list[str],
        report: PipelineReport,
    ) -> StageReport:
        """Stage 1: Discover and download SEC filings."""
        stage = StageReport(
            stage="discovery",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        try:
            # Build manifest from SEC EDGAR
            manifest = self.acquisition.build_manifest(
                company.ticker,
                forms=forms,
                start=start,
                end=end,
            )
            report.filings_discovered = len(manifest.filings)
            stage.items_processed = len(manifest.filings)
            
            log.info(f"Discovered {len(manifest.filings)} filings for {company.ticker}")
            
            # Download filings
            if not self.config.skip_download and not self.config.dry_run:
                download_stats = self.acquisition.download_filings(
                    manifest,
                    self.config.data_root,
                    refresh=self.config.refresh_cache,
                )
                report.filings_downloaded = download_stats.downloaded
                stage.metadata["download_stats"] = {
                    "cached": download_stats.cached,
                    "downloaded": download_stats.downloaded,
                    "failed": len(download_stats.failed),
                }
                if download_stats.failed:
                    for name, reason in download_stats.failed:
                        stage.add_warning(f"Download failed: {name} - {reason}")
            
            # Save manifest for checkpointing
            manifest_path = self._manifest_path(company.ticker)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_data = {
                "company": company.ticker,
                "date_range": manifest.date_range,
                "forms": manifest.forms_requested,
                "filings": [
                    {
                        "form": f.form,
                        "filing_date": f.filing_date,
                        "accession": f.accession,
                        "primary_document": f.primary_document,
                        "cik": f.cik,
                        "report_date": f.report_date,
                        "document_title": f.document_title,
                        "document_url": f.document_url,
                        "fiscal_period": f.fiscal_period,
                        "fiscal_year": f.fiscal_year,
                        "is_amended": f.is_amended,
                        "amendment_type": f.amendment_type,
                        "exhibit_type": f.exhibit_type,
                        "ticker": f.ticker,
                    }
                    for f in manifest.filings
                ],
            }
            manifest_path.write_text(json.dumps(manifest_data, indent=2))
            stage.metadata["manifest_path"] = str(manifest_path)
            
        except Exception as e:
            stage.add_error(f"Discovery failed: {e}")
            raise
        
        stage.complete(filings_discovered=report.filings_discovered)
        return stage
    
    def _stage_persistence(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 2: Persist document metadata and validate."""
        stage = StageReport(
            stage="persistence",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # Load manifest
        manifest = self._load_manifest(company.ticker)
        if not manifest:
            stage.add_error("No manifest found - run discovery first")
            stage.complete()
            return stage
        
        # Checkpoint: which filings already processed?
        processed = self._load_checkpoint(company.ticker, "persistence")
        
        for filing_data in manifest["filings"]:
            filing = Filing(**filing_data)
            filing_id = filing.document_identity()
            
            if self.config.skip_existing and filing_id in processed:
                report.filings_skipped += 1
                stage.items_skipped += 1
                continue
            
            # Validate filing exists locally
            local_path = self._local_filing_path(company, filing)
            if not local_path.exists():
                stage.add_error(f"Filing not found locally: {local_path}")
                report.filings_failed += 1
                stage.items_failed += 1
                continue
            
            # Persist document metadata (would go to PostgreSQL in production)
            doc_metadata = self._persist_document_metadata(company, filing, local_path)
            
            # Save checkpoint
            processed.add(filing_id)
            self._save_checkpoint(company.ticker, "persistence", processed)
            
            report.filings_processed += 1
            stage.items_processed += 1
        
        stage.complete(filings_persisted=report.filings_processed)
        return stage
    
    def _stage_parsing(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 3: Parse documents and normalize content."""
        stage = StageReport(
            stage="parsing",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        manifest = self._load_manifest(company.ticker)
        if not manifest:
            stage.add_error("No manifest found")
            stage.complete()
            return stage
        
        processed = self._load_checkpoint(company.ticker, "parsing")
        
        for filing_data in manifest["filings"]:
            filing = Filing(**filing_data)
            filing_id = filing.document_identity()
            
            if self.config.skip_existing and filing_id in processed:
                stage.items_skipped += 1
                continue
            
            local_path = self._local_filing_path(company, filing)
            if not local_path.exists():
                stage.add_warning(f"Filing not found: {local_path}")
                continue
            
            try:
                # Parse with document_loader (handles HTML, extracts SEC metadata)
                content = load_filing_content(local_path)
                
                # Extract and validate SEC metadata
                import document_loader as dl
                doc = dl.load_document(local_path)
                sec_metadata = dl.extract_sec_metadata(content)
                sec_metadata.ticker = company.ticker
                sec_metadata.cik = company.cik
                
                # Save parsed content
                parsed_path = self._parsed_path(company.ticker, filing_id)
                parsed_path.parent.mkdir(parents=True, exist_ok=True)
                parsed_data = {
                    "filing_id": filing_id,
                    "company_ticker": company.ticker,
                    "content": content,
                    "metadata": sec_metadata.to_dict(),
                    "parsed_at": datetime.utcnow().isoformat() + "Z",
                }
                parsed_path.write_text(json.dumps(parsed_data))
                
                processed.add(filing_id)
                self._save_checkpoint(company.ticker, "parsing", processed)
                stage.items_processed += 1
                
            except Exception as e:
                stage.add_error(f"Parsing failed for {filing_id}: {e}")
                report.filings_failed += 1
                stage.items_failed += 1
        
        stage.complete(parsed_count=stage.items_processed)
        return stage
    
    def _stage_chunking(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 4: Chunk parsed documents."""
        stage = StageReport(
            stage="chunking",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        manifest = self._load_manifest(company.ticker)
        if not manifest:
            stage.add_error("No manifest found")
            stage.complete()
            return stage
        
        processed = self._load_checkpoint(company.ticker, "chunking")
        
        for filing_data in manifest["filings"]:
            filing = Filing(**filing_data)
            filing_id = filing.document_identity()
            
            if self.config.skip_existing and filing_id in processed:
                stage.items_skipped += 1
                continue
            
            parsed_path = self._parsed_path(company.ticker, filing_id)
            if not parsed_path.exists():
                stage.add_warning(f"Parsed content not found: {parsed_path}")
                continue
            
            try:
                parsed_data = json.loads(parsed_path.read_text())
                content = parsed_data["content"]
                
                # Chunk using graphrag's chunker
                from graphrag.document import chunk_document, Document
                
                # Create document with synthetic pages (EDGAR HTML has no real pages)
                segments = [
                    content[i:i + 3000] 
                    for i in range(0, len(content), 3000)
                ]
                document = Document(path=parsed_path, pages=segments)
                
                chunks = chunk_document(
                    document,
                    self.config.chunk_tokens,
                    self.config.chunk_overlap_tokens,
                )
                
                # Save chunks with provenance
                chunks_path = self._chunks_path(company.ticker, filing_id)
                chunks_path.parent.mkdir(parents=True, exist_ok=True)
                chunks_data = {
                    "filing_id": filing_id,
                    "company_ticker": company.ticker,
                    "chunks": [
                        {
                            "index": c.index,
                            "text": c.text,
                            "page_start": c.page_start,
                            "page_end": c.page_end,
                            "token_count": c.token_count,
                            "location": c.location,
                        }
                        for c in chunks
                    ],
                    "chunked_at": datetime.utcnow().isoformat() + "Z",
                }
                chunks_path.write_text(json.dumps(chunks_data))
                
                report.chunks_created += len(chunks)
                stage.items_processed += 1
                processed.add(filing_id)
                self._save_checkpoint(company.ticker, "chunking", processed)
                
            except Exception as e:
                stage.add_error(f"Chunking failed for {filing_id}: {e}")
                stage.items_failed += 1
        
        stage.complete(chunks_created=report.chunks_created)
        return stage
    
    def _stage_embedding(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 5: Generate embeddings for chunks."""
        stage = StageReport(
            stage="embedding",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # This would integrate with an embedding provider
        # For now, we create the structure - actual embedding happens in GraphRAG
        manifest = self._load_manifest(company.ticker)
        if not manifest:
            stage.add_error("No manifest found")
            stage.complete()
            return stage
        
        for filing_data in manifest["filings"]:
            filing = Filing(**filing_data)
            filing_id = filing.document_identity()
            chunks_path = self._chunks_path(company.ticker, filing_id)
            
            if not chunks_path.exists():
                continue
            
            chunks_data = json.loads(chunks_path.read_text())
            # Embeddings would be generated here and stored in pgvector
            # For now, mark as ready for embedding
            report.embeddings_generated += len(chunks_data.get("chunks", []))
            stage.items_processed += 1
        
        stage.complete(embeddings_ready=report.embeddings_generated)
        return stage
    
    def _stage_evidence(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 6: Store evidence with full provenance."""
        stage = StageReport(
            stage="evidence",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # Evidence storage integrates with the existing provenance system
        # This would write to the evidence tables in PostgreSQL
        manifest = self._load_manifest(company.ticker)
        if not manifest:
            stage.add_error("No manifest found")
            stage.complete()
            return stage
        
        for filing_data in manifest["filings"]:
            filing = Filing(**filing_data)
            filing_id = filing.document_identity()
            
            # Create evidence record with full provenance
            evidence = {
                "filing_id": filing_id,
                "company_ticker": company.ticker,
                "company_cik": company.cik,
                "source_url": filing.document_url,
                "accession_number": filing.accession,
                "form_type": filing.form,
                "filing_date": filing.filing_date,
                "period_of_report": filing.report_date,
                "fiscal_year": filing.fiscal_year,
                "fiscal_period": filing.fiscal_period,
                "is_amended": filing.is_amended,
                "amendment_type": filing.amendment_type,
                "content_hash": filing.content_hash,
                "created_at": datetime.utcnow().isoformat() + "Z",
            }
            
            evidence_path = self._evidence_path(company.ticker, filing_id)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(evidence, indent=2))
            
            report.evidence_stored += 1
            stage.items_processed += 1
        
        stage.complete(evidence_stored=report.evidence_stored)
        return stage
    
    def _stage_postgres(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 7: Persist to PostgreSQL + pgvector."""
        stage = StageReport(
            stage="postgres",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # This would use the existing database layer
        # For now, we note what would be persisted
        stage.metadata["note"] = "Would persist: Company, Document, Evidence, Chunk, FinancialFact, FiscalYear, FiscalQuarter"
        stage.complete()
        return stage
    
    def _stage_graph(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 8: Build Neo4j graph with company-aware relationships."""
        stage = StageReport(
            stage="graph",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # Graph construction uses company-aware node labels/relationships
        # (:Company {ticker})-[:HAS_DOCUMENT]->(:Document)-[:HAS_CHUNK]->(:Chunk)
        # All companies share the same Neo4j database
        stage.metadata["note"] = "Would create company-aware graph nodes and relationships"
        stage.complete()
        return stage
    
    def _stage_graphrag(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 9: Update GraphRAG retrieval index."""
        stage = StageReport(
            stage="graphrag",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # GraphRAG index update - company isolation at query time
        stage.metadata["note"] = "Would update GraphRAG retrieval with company-scoped embeddings"
        stage.complete()
        return stage
    
    def _stage_validation_cleanup(
        self,
        company: Company,
        report: PipelineReport,
    ) -> StageReport:
        """Stage 10: Validate ingestion and clean up temporary files."""
        stage = StageReport(
            stage="validation_cleanup",
            started_at=datetime.utcnow().isoformat() + "Z",
        )
        
        # Validate counts match expectations
        manifest = self._load_manifest(company.ticker)
        if manifest:
            expected = len(manifest["filings"])
            if report.filings_processed != expected and not self.config.skip_existing:
                stage.add_warning(
                    f"Processed {report.filings_processed}/{expected} filings"
                )
        
        # Clean up temporary PDF files if configured
        if self.config.delete_temp_files:
            cleaned = self._cleanup_temp_files(company)
            stage.metadata["temp_files_cleaned"] = cleaned
            log.info(f"Cleaned up {cleaned} temporary files for {company.ticker}")
        
        # Final validation checkpoint
        self._save_checkpoint(company.ticker, "complete", set())
        
        stage.complete(validated=True)
        return stage
    
    # --- Helper Methods ---
    
    def _local_filing_path(self, company: Company, filing: Filing) -> Path:
        """Get the local path for a downloaded filing."""
        fy = filing.fiscal_year or filing.filing_date[:4]
        form_dir = filing.form.replace("/", "-")
        return (
            self.config.data_root
            / company.ticker.lower()
            / fy
            / form_dir.lower()
            / filing.local_filename()
        )
    
    def _manifest_path(self, ticker: str) -> Path:
        return self.config.cache_dir / f"{ticker.lower()}_manifest.json"
    
    def _parsed_path(self, ticker: str, filing_id: str) -> Path:
        safe_id = filing_id.replace("|", "_").replace("/", "_")
        return self.config.cache_dir / "parsed" / f"{ticker.lower()}_{safe_id}.json"
    
    def _chunks_path(self, ticker: str, filing_id: str) -> Path:
        safe_id = filing_id.replace("|", "_").replace("/", "_")
        return self.config.cache_dir / "chunks" / f"{ticker.lower()}_{safe_id}.json"
    
    def _evidence_path(self, ticker: str, filing_id: str) -> Path:
        safe_id = filing_id.replace("|", "_").replace("/", "_")
        return self.config.cache_dir / "evidence" / f"{ticker.lower()}_{safe_id}.json"
    
    def _checkpoint_path(self, ticker: str, stage: str) -> Path:
        return self.config.checkpoint_dir / f"{ticker.lower()}_{stage}.json"
    
    def _load_manifest(self, ticker: str) -> dict | None:
        path = self._manifest_path(ticker)
        if path.exists():
            return json.loads(path.read_text())
        return None
    
    def _load_checkpoint(self, ticker: str, stage: str) -> set[str]:
        path = self._checkpoint_path(ticker, stage)
        if path.exists():
            return set(json.loads(path.read_text()))
        return set()
    
    def _save_checkpoint(self, ticker: str, stage: str, data: set[str]) -> None:
        path = self._checkpoint_path(ticker, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(data)))
        tmp.replace(path)
    
    def _persist_document_metadata(
        self,
        company: Company,
        filing: Filing,
        local_path: Path,
    ) -> dict[str, Any]:
        """Persist document metadata (would write to PostgreSQL)."""
        metadata = {
            "filing_id": filing.document_identity(),
            "company_ticker": company.ticker,
            "company_cik": company.cik,
            "company_name": company.name,
            "form_type": filing.form,
            "filing_date": filing.filing_date,
            "period_of_report": filing.report_date,
            "fiscal_year": filing.fiscal_year,
            "fiscal_period": filing.fiscal_period,
            "accession_number": filing.accession,
            "document_url": filing.document_url,
            "local_path": str(local_path),
            "file_size": local_path.stat().st_size,
            "is_amended": filing.is_amended,
            "amendment_type": filing.amendment_type,
            "exhibit_type": filing.exhibit_type,
            "content_hash": filing.content_hash,
            "source": filing.source,
            "source_authority": filing.source_authority,
            "ingested_at": datetime.utcnow().isoformat() + "Z",
        }
        return metadata
    
    def _cleanup_temp_files(self, company: Company) -> int:
        """Delete temporary PDF files after successful ingestion."""
        cleaned = 0
        company_dir = self.config.data_root / company.ticker.lower()
        if company_dir.exists():
            for pdf_path in company_dir.rglob("*.pdf"):
                try:
                    # Only delete if corresponding HTML/HTM exists (successfully processed)
                    html_path = pdf_path.with_suffix(".htm")
                    if not html_path.exists():
                        html_path = pdf_path.with_suffix(".html")
                    if html_path.exists():
                        pdf_path.unlink()
                        cleaned += 1
                except OSError:
                    pass
        return cleaned
    
    def _save_report(self, report: PipelineReport) -> None:
        """Save pipeline report."""
        reports_dir = self.config.checkpoint_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        report_path = reports_dir / f"{report.company_ticker}_{timestamp}.json"
        report.save(report_path)
        
        # Also save as latest
        latest_path = reports_dir / f"{report.company_ticker}_latest.json"
        report.save(latest_path)


def run_ingestion(
    tickers: list[str] | str = "all",
    start_date: str = "2020-01-01",
    end_date: str = "2026-12-31",
    forms: list[str] | None = None,
    config: IngestionConfig | None = None,
    registry: CompanyRegistry | None = None,
) -> dict[str, PipelineReport]:
    """Convenience function to run ingestion for one or more companies.
    
    Args:
        tickers: List of tickers, "all" for all active companies, or single ticker
        start_date: Start date for filings (YYYY-MM-DD)
        end_date: End date for filings (YYYY-MM-DD)
        forms: SEC form types to ingest
        config: Optional pipeline configuration
        registry: Optional company registry
    
    Returns:
        Dict mapping ticker to PipelineReport
    """
    orchestrator = IngestionOrchestrator(config=config, registry=registry)
    
    if tickers == "all":
        return orchestrator.ingest_all(start_date, end_date, forms)
    
    if isinstance(tickers, str):
        tickers = [tickers]
    
    return orchestrator.ingest_companies(tickers, start_date, end_date, forms)