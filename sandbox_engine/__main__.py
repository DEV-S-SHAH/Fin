"""Command-line entrypoint: ``python -m sandbox_engine``.

    python -m sandbox_engine --reset                 # full run, all 5 benchmarks
    python -m sandbox_engine --parse-only            # stop after the Parquet spill
    python -m sandbox_engine --load-only             # build the graph from a spill
    python -m sandbox_engine --verify-only           # re-run benchmarks, no ingest
    python -m sandbox_engine --files a.htm b.htm     # override the document-tree scope

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
from .backup import (
    DEFAULT_BACKUP_ROOT,
    DEFAULT_RETENTION,
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from .config import COPY_THRESHOLD, VERSION, default_paths, resolve_scope
from .entity_resolver import ConceptRegistry
from .ingestion import apply_ufgs
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
                        help="ingest these files instead of the document-tree scope")
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
    parser.add_argument("--period", default=None,
                        help="exact period key the benchmarks target, e.g. "
                             "3M-2026-03-28 (default: newest reported quarter)")
    parser.add_argument("--copy-threshold", type=int, default=COPY_THRESHOLD,
                        help="rows at or above which COPY is used instead of UNWIND")
    parser.add_argument("--batch-rows", type=int, default=50_000,
                        help="rows buffered before a Parquet part is flushed")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"sandbox_engine {VERSION}")

    group = parser.add_argument_group("backup")
    group.add_argument(
        "--backup",
        choices=("create", "list", "verify", "restore"),
        default=None,
        help="run a backup action and exit, without ingesting anything: "
             "create a verified backup of the database, list backups, "
             "verify one, or restore one",
    )
    group.add_argument(
        "--backup-dir",
        default=DEFAULT_BACKUP_ROOT,
        help=f"directory holding timestamped backups (default: {DEFAULT_BACKUP_ROOT})",
    )
    group.add_argument(
        "--backup-id", default=None, help="backup directory name for --backup verify/restore"
    )
    group.add_argument(
        "--retention",
        type=int,
        default=DEFAULT_RETENTION,
        help=f"how many complete backups to keep (default: {DEFAULT_RETENTION})",
    )
    group.add_argument(
        "--confirm",
        action="store_true",
        help="required acknowledgement for --backup restore; the existing database "
             "is moved aside, never deleted",
    )
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
        t_step = time.perf_counter()
        result = parser.ingest_file(path)
        base_parse_sec = time.perf_counter() - t_step
        t_step = time.perf_counter()
        # The Universal Financial Graph Schema layer is added here rather than
        # inside ``ingest_file`` so the two graphs stay separable. This CLI has
        # its own loop rather than calling ``ingestion.parse_all``, so the call
        # has to be repeated; the alternative is having the CLI delegate to
        # ``parse_all`` and lose the per-filing registry handoff below.
        apply_ufgs(result, path)
        ufgs_sec = time.perf_counter() - t_step
        buffer.add_result(result)
        filings.append(
            {
                "file": path.name,
                "company": path.parents[2].name,
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
                "base_parse_sec": round(base_parse_sec, 3),
                "ufgs_sec": round(ufgs_sec, 3),
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
    timing = report.get("timing")
    if timing:
        print("\nStage timing (seconds)")
        print(f"  scope      : {timing['scope']:.3f}")
        print(f"  parse+ufgs : {timing['parse']:.3f}")
        print(f"  pipe+load  : {timing['load']:.3f}")
        print(f"  benchmarks : {timing['benchmarks']:.3f}")
        print(f"  total      : {timing['total']:.3f}")

    parse = report.get("parse")
    if parse:
        print(f"\nParsed {len(parse['filings'])} filing(s) in {parse['seconds']}s")
        print(f"  {'FILE':<30} {'CO':<8} {'FORM':<5} {'FY':<4} "
              f"{'PARSE':>7} {'UFGS':>6} {'TOTAL':>7}")
        for filing in parse["filings"]:
            counts = filing["counts"]
            print(
                f"  {filing['file']:<30} {filing['company']:<8} "
                f"{filing['form_type']:<5} FY{filing['fiscal_year']} "
                f"{filing['base_parse_sec']:>6.2f}s {filing['ufgs_sec']:>5.2f}s "
                f"{filing['base_parse_sec'] + filing['ufgs_sec']:>6.2f}s"
            )
            print(
                f"  {'':<30} {'':<8} {'':<5} {'':<4} "
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


def _run_backup_action(args: argparse.Namespace, db_path: Path) -> int:
    """Dispatch ``--backup``. Returns a process exit code.

    Backup never shares a process with ingestion: LadybugDB hands the writer
    lock to whoever opens read-write, so a backup taken from inside a running
    load would race the very writer it is trying to avoid. This runs before
    any stage touches the database.
    """
    root = Path(args.backup_dir)

    if args.backup == "create":
        result = create_backup(db_path, root, retention=args.retention)
        print(f"backup {result.backup_id}")
        print(f"  path   {result.path}")
        print(f"  rows   {result.rows:,} nodes")
        print(f"  sha256 {result.sha256}")
        return 0

    if args.backup == "list":
        entries = list_backups(root)
        if not entries:
            print(f"no backups in {root}")
            return 0
        print(f"{'backup id':44} {'state':10} {'rows':>9}  created")
        for item in entries:
            rows = f"{item['rows']:,}" if item["rows"] is not None else "-"
            state = "complete" if item["complete"] else "INCOMPLETE"
            print(f"{item['backup_id']:44} {state:10} {rows:>9}  {item['created_utc']}")
        return 0

    if not args.backup_id:
        print("error: --backup-id is required for verify and restore", file=sys.stderr)
        return 2

    if args.backup == "verify":
        checked = verify_backup(root, args.backup_id)
        source = checked["manifest"]["source"]
        print(f"backup {args.backup_id} is intact")
        print(f"  source {source['path']}")
        print(f"  sha256 {source['sha256']}")
        print(f"  nodes  {source['fingerprint']['node_rows']:,}")
        print(f"  rels   {source['fingerprint']['rel_rows']:,}")
        return 0

    result = restore_backup(
        db_path, root, args.backup_id, confirm=args.confirm, on_progress=print
    )
    print(f"restored {args.backup_id} -> {result.restored}")
    if result.displaced is not None:
        print(f"  previous database preserved at {result.displaced}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    paths = _resolve_paths(args)
    paths.ensure()

    if args.backup:
        try:
            return _run_backup_action(args, paths.db)
        except BackupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.reset:
        paths.reset()

    report: dict[str, Any] = {"version": VERSION, "db": str(paths.db)}
    timing: dict[str, float] = {"scope": 0.0, "parse": 0.0, "load": 0.0,
                                "benchmarks": 0.0, "total": 0.0}
    report["timing"] = timing
    t_start = time.perf_counter()

    if not args.load_only and not args.verify_only:
        t_step = time.perf_counter()
        files = _select_files(args, paths)
        timing["scope"] = time.perf_counter() - t_step
        log.info("scope: %s", ", ".join(path.name for path in files))
        report["parse"] = _parse_stage(
            files, paths.staging, args.batch_rows, paths.registry
        )
        timing["parse"] = report["parse"]["seconds"]
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
        t_step = time.perf_counter()
        try:
            with BulkLoader(paths.db, copy_threshold=args.copy_threshold) as loader:
                report["load"] = loader.load(
                    StageBuffer(paths.staging, batch_rows=args.batch_rows)
                ).as_dict()
                report["counts"] = loader.counts()
        except WalRecoveryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        timing["load"] = report["load"].get("seconds", time.perf_counter() - t_step)

    if not args.parse_only:
        t_step = time.perf_counter()
        with BulkLoader(paths.db) as loader:
            results = run_all(
                loader.connection,
                distinct=report.get("distinct", {}),
                form_type=args.form_type,
                fiscal_year=args.fiscal_year,
                concept=args.concept,
                period=args.period,
            )
        timing["benchmarks"] = time.perf_counter() - t_step
        report["benchmarks"] = [result.as_dict() for result in results]

    timing["total"] = time.perf_counter() - t_start

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
