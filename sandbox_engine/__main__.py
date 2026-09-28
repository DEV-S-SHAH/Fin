"""Command-line entrypoint: ``python -m sandbox_engine``.

    python -m sandbox_engine --reset                 # full run, all 5 benchmarks
    python -m sandbox_engine --parse-only            # stop after the Parquet spill
    python -m sandbox_engine --load-only             # build the graph from a spill
    python -m sandbox_engine --verify-only           # re-run benchmarks, no ingest
    python -m sandbox_engine --files a.htm b.htm     # override the 3-file scope

The stage split is real, not cosmetic: ``--parse-only`` and ``--load-only`` run
in separate processes against the same staging directory, which is how you
isolate a bug to the parse side or the load side instead of bisecting a
five-second run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .benchmarks import run_all
from .buffer import NODE_TABLES, REL_TABLES, StageBuffer, identity_of
from .config import COPY_THRESHOLD, VERSION, default_paths, resolve_scope
from .entity_resolver import ConceptRegistry
from .loader import BulkLoader, WalRecoveryError
from .parser import FilingParser, stable_id

log = logging.getLogger("sandbox_engine")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sandbox_engine",
        description="Sandbox SEC ingestion and query engine: HTML -> Arrow -> "
                    "Parquet -> LadybugDB -> Cypher benchmarks.",
    )
    parser.add_argument("--root", default=None,
                        help="repository root holding data/ (default: inferred)")
    parser.add_argument("--db", default=None, help="database path override")
    parser.add_argument("--staging", default=None, help="Parquet spill directory override")
    parser.add_argument("--report", default=None, help="run report JSON path override")
    parser.add_argument("--registry", default=None,
                        help="canonical-entity registry path override; the dedup "
                             "state that makes a re-ingest a no-op")
    parser.add_argument("--files", nargs="*", default=None,
                        help="ingest these files instead of the 3-file scope")
    parser.add_argument("--reset", action="store_true",
                        help="delete the database and the staging spill first")
    parser.add_argument("--parse-only", action="store_true",
                        help="parse and spill to Parquet, do not touch the database")
    parser.add_argument("--load-only", action="store_true",
                        help="load the existing Parquet spill, do not parse")
    parser.add_argument("--verify-only", action="store_true",
                        help="re-run the benchmarks against the existing database")
    parser.add_argument("--concept", default="Net Sales",
                        help="metric concept the benchmarks look up")
    parser.add_argument("--form-type", default="10-Q",
                        help="form type the benchmarks target")
    parser.add_argument("--fiscal-year", type=int, default=None,
                        help="fiscal year the benchmarks target (default: newest)")
    parser.add_argument("--copy-threshold", type=int, default=COPY_THRESHOLD,
                        help="rows at or above which COPY is used instead of UNWIND")
    parser.add_argument("--batch-rows", type=int, default=50_000,
                        help="rows buffered before a Parquet part is flushed")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"sandbox_engine {VERSION}")
    return parser


def _resolve_paths(args: argparse.Namespace):
    paths = default_paths(args.root)
    if args.db:
        paths = type(paths)(**{**paths.__dict__, "db": Path(args.db)})
    if args.staging:
        paths = type(paths)(**{**paths.__dict__, "staging": Path(args.staging)})
    if args.report:
        paths = type(paths)(**{**paths.__dict__, "report": Path(args.report)})
    if args.registry:
        paths = type(paths)(**{**paths.__dict__, "registry": Path(args.registry)})
    return paths


def _select_files(args: argparse.Namespace, paths) -> list[Path]:
    if args.files:
        return [Path(name) for name in args.files]
    return resolve_scope(paths.root)


def _parse_stage(
    files: Sequence[Path],
    staging: Path,
    batch_rows: int,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Stage 1 + 2: parse each filing, buffer to Arrow, spill to Parquet.

    The entity registry is loaded from *registry_path* when it exists, so a
    re-ingest resolves onto the entities the previous run created rather than
    rediscovering them -- and a second run over the same filings is then a
    no-op instead of a second copy of the graph. It is written back afterwards.
    """
    buffer = StageBuffer(staging, batch_rows=batch_rows)
    registry = (
        ConceptRegistry.load(registry_path, stable_id)
        if registry_path and registry_path.is_file()
        else ConceptRegistry(stable_id)
    )
    parser = FilingParser(registry=registry)
    started = time.perf_counter()
    filings: list[dict[str, Any]] = []
    for path in files:
        result = parser.ingest_file(path)
        buffer.add_result(result)
        filings.append(
            {
                "file": path.name,
                "form_type": result.filing["form_type"],
                "fiscal_year": result.filing["fiscal_year"],
                "fiscal_period": result.filing["fiscal_period"],
                "filing_date": result.filing["filing_date"],
                "ticker": result.company["ticker"],
                "name": result.company["name"],
                "cik": result.company["cik"],
                "counts": result.counts(),
                "metadata_sources": result.stats["sources"],
                "bytes": result.stats["bytes"],
                "elapsed": round(result.elapsed, 3),
            }
        )
        log.info(
            "parsed %s: %s FY%s -> %s",
            path.name, result.filing["form_type"],
            result.filing["fiscal_year"], result.counts(),
        )
    buffer.spill()
    if registry_path:
        registry.save(registry_path)
    return {
        "filings": filings,
        "buffer": buffer.report.as_dict(),
        "resolution": parser.resolution_report(),
        "seconds": round(time.perf_counter() - started, 3),
    }


def _print_summary(report: dict[str, Any]) -> None:
    parse = report.get("parse")
    if parse:
        print(f"\nParsed {len(parse['filings'])} filing(s) in {parse['seconds']}s")
        for filing in parse["filings"]:
            counts = filing["counts"]
            print(
                f"  {filing['form_type']:5} FY{filing['fiscal_year']} "
                f"{filing['ticker']:5} "
                f"metrics={counts['metrics']:5} segments={counts['segments']:3} "
                f"events={counts['events']:3} chunks={counts['chunks']:5}"
            )
        rows = parse["buffer"]["rows_per_table"]
        print(f"  spilled {sum(rows.values())} row(s) to Parquet in "
              f"{len(parse['buffer']['files'])} table file(s), "
              f"{parse['buffer']['bytes_written']} bytes")

    resolution = report.get("parse", {}).get("resolution")
    if resolution:
        stats = resolution["stats"]
        print(f"  entities: {resolution['entities']} canonical node(s) across "
              f"{len(resolution['partitions'])} partition(s); "
              f"{stats['created']} created, {stats['merged']} merged onto an "
              f"existing node")

    load = report.get("load")
    if load:
        print(f"\nLoaded in {load['seconds']}s")
        for table, count in load["inserted"].items():
            method = load["methods"].get(table, "-")
            skipped = load["skipped_duplicates"].get(table, 0)
            print(
                f"  {table:16} inserted={count:6}  skipped={skipped:5}  via={method}"
            )
        if load["dropped_dangling_arcs"]:
            print(f"  WARNING: dropped {load['dropped_dangling_arcs']} dangling arc(s)")
        if load["schema_rebuilt"]:
            print(f"  rebuilt drifted table(s): {', '.join(load['schema_rebuilt'])}")

    benchmarks = report.get("benchmarks")
    if benchmarks:
        print("\nBenchmarks")
        for result in benchmarks:
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"  [{mark}] {result['key']} {result['name']} ({result['seconds']}s)")
            for failure in result["failures"]:
                print(f"         - {failure}")
        passed = sum(1 for result in benchmarks if result["passed"])
        print(f"  {passed}/{len(benchmarks)} passed")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    paths = _resolve_paths(args)
    paths.ensure()
    if args.reset:
        paths.reset()

    report: dict[str, Any] = {"version": VERSION, "db": str(paths.db)}

    if not args.load_only and not args.verify_only:
        files = _select_files(args, paths)
        log.info("scope: %s", ", ".join(path.name for path in files))
        report["parse"] = _parse_stage(
            files, paths.staging, args.batch_rows, paths.registry
        )
        report["distinct"] = report["parse"]["buffer"]["distinct_per_table"]
    else:
        # --load-only / --verify-only: recover the distinct-identity baseline
        # from the existing spill, so benchmark 1 still has something to assert
        # against instead of failing for want of a comparison.
        buffer = StageBuffer(paths.staging, batch_rows=args.batch_rows)
        distinct: dict[str, int] = {}
        for table in (*NODE_TABLES, *REL_TABLES):
            arrow = buffer.load_table(table)
            if arrow.num_rows == 0:
                continue
            distinct[table] = len({
                identity_of(table, row) for row in arrow.to_pylist()
            })
        report["distinct"] = distinct

    if not args.parse_only:
        try:
            with BulkLoader(paths.db, copy_threshold=args.copy_threshold) as loader:
                report["load"] = loader.load(
                    StageBuffer(paths.staging, batch_rows=args.batch_rows)
                ).as_dict()
                report["counts"] = loader.counts()
        except WalRecoveryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not args.parse_only:
        with BulkLoader(paths.db) as loader:
            results = run_all(
                loader.connection,
                distinct=report.get("distinct", {}),
                form_type=args.form_type,
                fiscal_year=args.fiscal_year,
                concept=args.concept,
            )
        report["benchmarks"] = [result.as_dict() for result in results]

    paths.report.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _print_summary(report)
    print(f"\nreport: {paths.report}")

    if not args.parse_only:
        failed = [r for r in report["benchmarks"] if not r["passed"]]
        if failed:
            print(f"\n{len(failed)} benchmark(s) FAILED", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
