"""CLI entry point for the generic multi-company ingestion pipeline."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .orchestrator import (
    IngestionConfig,
    IngestionOrchestrator,
    run_ingestion,
)
from .registry import CompanyRegistry, get_registry, DEFAULT_REGISTRY_PATH

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.pipeline",
        description="FinGraph Generic Multi-Company Ingestion Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ingest Apple 2020-2026
  python -m ingestion.pipeline --ticker AAPL --start-year 2020 --end-year 2026

  # Ingest Tesla and Microsoft
  python -m ingestion.pipeline --tickers TSLA,MSFT --start-year 2020 --end-year 2026

  # Ingest all registered companies
  python -m ingestion.pipeline --all --start-year 2020 --end-year 2026

  # Ingest specific forms only
  python -m ingestion.pipeline --ticker AAPL --forms 10-K,10-Q

  # Dry run (no downloads, no processing)
  python -m ingestion.pipeline --ticker AAPL --dry-run

  # Force refresh (re-download and re-process)
  python -m ingestion.pipeline --ticker AAPL --refresh
        """,
    )
    
    # Company selection
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--ticker",
        help="Single company ticker (e.g., AAPL)",
    )
    group.add_argument(
        "--tickers",
        help="Comma-separated list of tickers (e.g., AAPL,TSLA,MSFT)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Ingest all active companies in registry",
    )
    
    # Date range
    parser.add_argument(
        "--start-year",
        type=int,
        default=2020,
        help="Start year (default: 2020)",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=2026,
        help="End year (default: 2026)",
    )
    parser.add_argument(
        "--start-date",
        help="Start date YYYY-MM-DD (overrides --start-year)",
    )
    parser.add_argument(
        "--end-date",
        help="End date YYYY-MM-DD (overrides --end-year)",
    )
    
    # Form types
    parser.add_argument(
        "--forms",
        help="Comma-separated SEC form types (default: core forms 10-K,10-Q,8-K,DEF 14A)",
    )
    parser.add_argument(
        "--all-forms",
        action="store_true",
        help="Ingest all supported SEC form types",
    )
    
    # Data directories
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/sec-filings"),
        help="Root directory for downloaded filings",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data/ingestion-cache"),
        help="Directory for parsed content and checkpoints",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=Path("data/checkpoints"),
        help="Directory for pipeline checkpoints",
    )
    
    # Database
    parser.add_argument(
        "--pgvector-db",
        default="postgresql://localhost:5432/fingraph",
        help="PostgreSQL/pgvector connection string",
    )
    parser.add_argument(
        "--neo4j-uri",
        default="bolt://localhost:7687",
        help="Neo4j connection URI",
    )
    parser.add_argument(
        "--ladybug-db",
        type=Path,
        default=Path("data/graphrag.lbug"),
        help="LadybugDB path for GraphRAG",
    )
    
    # Processing options
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        default=800,
        help="Tokens per chunk (default: 800)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=100,
        help="Token overlap between chunks (default: 100)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Parallel workers for processing (default: 4)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size for database operations (default: 100)",
    )
    
    # Behavior flags
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip SEC download, use existing local files",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-download and re-process (ignore cache)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Process all filings even if already processed",
    )
    parser.add_argument(
        "--keep-temp-files",
        action="store_true",
        help="Keep temporary PDF files after ingestion",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only, no downloads or processing",
    )
    
    # Registry
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="Path to company registry JSON",
    )
    
    # Output
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("data/reports"),
        help="Directory for pipeline reports",
    )
    
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    
    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ingestion.pipeline")
    
    # Build config
    start_date = args.start_date or f"{args.start_year}-01-01"
    end_date = args.end_date or f"{args.end_year}-12-31"
    
    if args.all_forms:
        from .sec_acquisition import ALL_SEC_FORMS
        forms = list(ALL_SEC_FORMS)
    elif args.forms:
        forms = [f.strip() for f in args.forms.split(",")]
    else:
        from .sec_acquisition import DEFAULT_FORMS
        forms = list(DEFAULT_FORMS)
    
    config = IngestionConfig(
        data_root=args.data_root,
        cache_dir=args.cache_dir,
        checkpoint_dir=args.checkpoint_dir,
        pgvector_db=args.pgvector_db,
        neo4j_uri=args.neo4j_uri,
        ladybug_db=args.ladybug_db,
        chunk_tokens=args.chunk_tokens,
        chunk_overlap_tokens=args.chunk_overlap,
        max_workers=args.max_workers,
        batch_size=args.batch_size,
        forms=tuple(forms),
        start_date=start_date,
        end_date=end_date,
        skip_download=args.skip_download,
        refresh_cache=args.refresh,
        skip_existing=not args.no_skip_existing,
        delete_temp_files=not args.keep_temp_files,
        dry_run=args.dry_run,
    )
    
    # Load registry
    registry = get_registry(args.registry if args.registry.exists() else None)
    
    # Determine companies to ingest
    if args.all:
        tickers = "all"
    elif args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    else:
        tickers = [args.ticker.upper()]
    
    # Validate tickers exist in registry
    if tickers != "all":
        for ticker in tickers:
            if ticker not in registry:
                log.error(f"Unknown company: {ticker}. Register it first in {args.registry}")
                return 2
    
    log.info(f"Starting ingestion for: {tickers}")
    log.info(f"Date range: {start_date} to {end_date}")
    log.info(f"Forms: {forms}")
    log.info(f"Data root: {config.data_root}")
    
    if args.dry_run:
        log.info("DRY RUN - no actual processing will occur")
    
    try:
        reports = run_ingestion(
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            forms=forms,
            config=config,
            registry=registry,
        )
    except Exception as e:
        log.exception("Ingestion failed")
        return 1
    
    # Print summary
    print("\n" + "=" * 70)
    print("INGESTION SUMMARY")
    print("=" * 70)
    
    for ticker, report in reports.items():
        print(f"\n{ticker}:")
        print(f"  Duration: {report.total_duration_seconds:.1f}s")
        print(f"  Filings discovered: {report.filings_discovered}")
        print(f"  Filings downloaded: {report.filings_downloaded}")
        print(f"  Filings processed:  {report.filings_processed}")
        print(f"  Filings skipped:    {report.filings_skipped}")
        print(f"  Filings failed:     {report.filings_failed}")
        print(f"  Chunks created:     {report.chunks_created}")
        print(f"  Embeddings ready:   {report.embeddings_generated}")
        print(f"  Evidence stored:    {report.evidence_stored}")
        
        if report.stages:
            for stage in report.stages:
                if stage.errors:
                    print(f"  Errors in {stage.stage}: {len(stage.errors)}")
                    for err in stage.errors[:3]:
                        print(f"    - {err}")
    
    print("\n" + "=" * 70)
    
    # Return non-zero if any failures
    any_failed = any(r.filings_failed > 0 for r in reports.values())
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())