"""Typer-based CLI for the four-stage ingestion pipeline.

This module provides the command-line interface for running the ingestion
pipeline. It delegates all logic to :mod:`sandbox_engine.ingestion` and
handles argument parsing, logging setup, and pretty output formatting.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from .config import default_paths
from .ingestion import PipelineReport, run_pipeline

__all__ = ["app", "main"]

app = typer.Typer(
    name="ingest-sandbox",
    help="SEC filing ingestion pipeline for LadybugDB",
    add_completion=False,
    no_args_is_help=True,
)


def _setup_logging(verbose: bool) -> None:
    """Configure logging for the CLI."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    if not verbose:
        logging.getLogger("ladybug").setLevel(logging.WARNING)
        logging.getLogger("pyarrow").setLevel(logging.WARNING)

def _print_summary(report: PipelineReport) -> None:
    """Print a human-readable summary of the pipeline run."""
    W = 80
    print("\n" + "=" * W)
    print(f"  ingest-sandbox complete in {report.total_elapsed_sec:.2f}s")
    print(f"  extraction : ZERO-LLM / ZERO-API -- local HTML parser only")
    if report.staging_dir:
        print(f"  staging    -> {report.staging_dir}")
    if report.db_path:
        print(f"  database   -> {report.db_path}")
    print("=" * W)

    if report.parse:
        print("\n" + "-" * W)
        print(f"  STAGE TIMING (seconds)")
        print(f"  {'scope':<16} {'parse+ufgs':<12} {'buffer':<9} {'load':<8} total")
        parse_sec = report.parse.elapsed_sec
        buffer_sec = report.buffer.seconds if report.buffer else 0.0
        load_sec = report.load.seconds if report.load else 0.0
        print(
            f"  {report.scope_sec:<16.3f} {parse_sec:<12.3f} "
            f"{buffer_sec:<9.3f} {load_sec:<8.3f} "
            f"{report.total_elapsed_sec:.3f}"
        )
        print("-" * W)

    if report.parse and report.parse.filings:
        print("\n" + "-" * W)
        print(
            f"  {'FILE':<32} {'CO':<8} {'TICKER':<6} {'FORM':<5} {'FY':<5} "
            f"{'METS':>4} {'SEGS':>4} {'EVTS':>4} "
            f"{'PARSE':>7} {'UFGS':>7} {'TOTAL':>7}"
        )
        print("-" * W)
        for f in report.parse.filings:
            fn = f["file"]
            fn_short = fn if len(fn) <= 32 else "..." + fn[-31:]
            print(
                f"  {fn_short:<32} {str(f['company']):<8} {str(f['ticker']):<6} "
                f"{str(f['form_type']):<5} {str(f['fiscal_year']):<5} "
                f"{f['metrics']:>4} {f['segments']:>4} {f['events']:>4} "
                f"{f['base_parse_sec']:>6.2f}s {f['ufgs_sec']:>6.2f}s "
                f"{f['elapsed_sec']:>6.2f}s"
            )
        print("-" * W)

    if report.buffer:
        print("\n" + "-" * W)
        print(f"  PARQUET SPILL SUMMARY")
        print("-" * W)
        print(f"  Tables written : {len(report.buffer.files)}")
        print(f"  Total batches  : {sum(report.buffer.batches.values())}")
        print(f"  Bytes written  : {report.buffer.bytes_written / 1e6:.2f} MB")
        print(f"  Elapsed        : {report.buffer.seconds:.2f}s")
        if report.buffer.sentinel_fiscal_years:
            print(f"  Sentinel FYs   : {sorted(report.buffer.sentinel_fiscal_years)}")
        print("-" * W)

    if report.load:
        inserted = report.load.inserted
        nodes = {k: v for k, v in inserted.items() if not k.isupper()}
        rels = {k: v for k, v in inserted.items() if k.isupper()}
        print("\n" + "-" * W)
        print(f"  GRAPH CONTENTS")
        print("-" * W)
        print(f"  {'NODE TABLES':<32}  {'REL TABLES':<32}")
        print(f"  {'-'*32}  {'-'*32}")
        node_items = sorted(nodes.items())
        rel_items = sorted(rels.items())
        for i in range(max(len(node_items), len(rel_items))):
            nl = f"{node_items[i][0]:<26} {node_items[i][1]:>5}" if i < len(node_items) else ""
            rl = f"{rel_items[i][0]:<26} {rel_items[i][1]:>5}" if i < len(rel_items) else ""
            print(f"  {nl:<32}  {rl:<32}")
        print("-" * W)
        total_nodes = sum(nodes.values())
        total_rels = sum(rels.values())
        print(f"  Total nodes: {total_nodes:,}   Total relationships: {total_rels:,}   ")
        print(f"  Bulk-load: {report.load.seconds:.2f}s")
        print("=" * W + "\n")


@app.command()
def run(
    db_path: Annotated[
        Path,
        typer.Option(
            "--db",
            help="Path to LadybugDB database file",
            exists=False,
        ),
    ] = default_paths().db,
    staging_dir: Annotated[
        Path,
        typer.Option(
            "--staging",
            help="Directory for Parquet spill",
            exists=False,
        ),
    ] = default_paths().staging,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Wipe database and staging directory before run",
        ),
    ] = False,
    parse_only: Annotated[
        bool,
        typer.Option(
            "--parse-only",
            help="Stop after Parquet spill (stages 1-3 only)",
        ),
    ] = False,
    load_only: Annotated[
        bool,
        typer.Option(
            "--load-only",
            help="Load existing Parquet, skip parse/buffer stages",
        ),
    ] = False,
    batch_rows: Annotated[
        int,
        typer.Option(
            "--batch-rows",
            help="Max rows per RecordBatch in buffer stage",
            min=1000,
            max=1000000,
        ),
    ] = 50000,
    buffer_pool_bytes: Annotated[
        int,
        typer.Option(
            "--buffer-pool",
            help="Database buffer pool size in bytes",
            min=1024 * 1024,
        ),
    ] = 256 * 1024 * 1024,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help="Enable debug logging",
        ),
    ] = False,
) -> None:
    """Run the ingestion pipeline."""
    _setup_logging(verbose)

    if parse_only and load_only:
        typer.echo("Error: --parse-only and --load-only are mutually exclusive", err=True)
        raise typer.Exit(1)

    try:
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        report = run_pipeline(
            db_path=db_path,
            staging_dir=staging_dir,
            reset=reset,
            parse_only=parse_only,
            load_only=load_only,
            batch_rows=batch_rows,
            buffer_pool_bytes=buffer_pool_bytes,
            root=repo_root,
        )
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        logging.getLogger("sandbox_engine.cli").exception("Pipeline failed")
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    _print_summary(report)


@app.command()
def scope() -> None:
    """Show the resolved filing scope without running the pipeline."""
    from .ingestion import resolve_scope
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent

    paths = resolve_scope(root=repo_root)
    print("Resolved filing scope:")
    for i, p in enumerate(paths, 1):
        print(f"  {i}. {p}")


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
