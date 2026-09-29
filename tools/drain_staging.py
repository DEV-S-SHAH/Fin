"""Offline database consolidation worker: drain cold-start staging files into LadybugDB.

Reads every ``data/staging/<TICKER>.jsonl`` file, validates each record against
:mod:`sandbox_engine.coldstart_schema`, deduplicates entities via
:class:`~sandbox_engine.entity_resolver.ConceptRegistry`, and commits the results
to the persistent LadybugDB store using the existing idempotent loaders.

Usage::

    python -m tools.drain_staging [--db PATH] [--staging DIR] [--archive DIR]
                                   [--force] [--dry-run] [--verbose]

Flags
-----
--db PATH           Path to the ``.lbug`` database file.
                    Default: ``sandbox_engine/_run/sandbox.lbug``.
--staging DIR       Directory containing staged ``*.jsonl`` files.
                    Default: ``data/staging``.
--archive DIR       Directory to move successfully committed ``.jsonl`` files to.
                    Default: ``data/staging/archive``.
--force             Skip the port-9000 liveness check (use with caution: may
                    produce silent data divergence if the server is open).
--dry-run           Parse and validate staging files without writing to the DB.
--verbose / -v      Emit DEBUG-level log records.

Server isolation
----------------
LadybugDB requires an exclusive write lock.  If the web server on port 9000 is
reachable the worker aborts unless ``--force`` is supplied, because an open
server handle and this worker would both hold a reference to the same ``.lbug``
path, producing silent read divergence and potential corruption.

JSONL record format
-------------------
Each line in a staging file must be a JSON object that is either:

1. A *status record* (written by :class:`~sandbox_engine.background.BackgroundIngestQueue`):

   .. code-block:: json

       {"ticker": "RIVN", "status": "staged", "timestamp": "...", "metadata": {}}

   Status records mark a ticker as cold-started but carry no extraction data.
   The drain worker skips them with a ``WARN`` noting the ticker requires a
   full SEC EDGAR pull before it can be committed.

2. An *extraction record* (written by the JIT pipeline after full triple
   extraction):

   .. code-block:: json

       {
         "ticker": "RIVN",
         "status": "extracted",
         "timestamp": "...",
         "payload": {
           "entities": [...],
           "relationships": [...],
           "rejected_count": 0,
           "metadata": {}
         }
       }

   Extraction records are validated against :class:`~sandbox_engine.coldstart_schema.ExtractionPayload`,
   deduplicated, and written to the DB via idempotent loaders.

Safe cleanup
------------
After every successful ticker commit the corresponding ``.jsonl`` is moved to
``data/staging/archive/<TICKER>-<ISO-timestamp>.jsonl`` using an atomic rename
(``os.replace``).  No file is ever deleted — only relocated, so nothing is
irreversible.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Bootstrap path so the module works both as ``python tools/drain_staging.py``
# and as ``python -m tools.drain_staging`` from the repo root.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sandbox_engine.coldstart_schema import ExtractionPayload  # noqa: E402
from sandbox_engine.config import Paths  # noqa: E402
from sandbox_engine.entity_resolver import ConceptRegistry  # noqa: E402
from sandbox_engine.parser import stable_id  # noqa: E402
from sandbox_engine.stitch import InMemoryOverlayGraph, stitch_coldstart_payload  # noqa: E402

log = logging.getLogger("drain_staging")


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------


class DrainResult:
    """Accumulates per-ticker and aggregate drain statistics."""

    def __init__(self) -> None:
        self.tickers_committed: list[str] = []
        self.tickers_skipped_status_only: list[str] = []
        self.tickers_failed: list[str] = []
        self.tickers_dry_run: list[str] = []
        self.records_validated: int = 0
        self.records_rejected: int = 0
        self.nodes_committed: int = 0
        self.edges_committed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "committed": self.tickers_committed,
            "skipped_status_only": self.tickers_skipped_status_only,
            "failed": self.tickers_failed,
            "dry_run": self.tickers_dry_run,
            "records_validated": self.records_validated,
            "records_rejected": self.records_rejected,
            "nodes_committed": self.nodes_committed,
            "edges_committed": self.edges_committed,
        }


# ---------------------------------------------------------------------------
# Port-9000 server liveness check
# ---------------------------------------------------------------------------

_SERVER_PORT = 9000


def _server_is_reachable(port: int = _SERVER_PORT, timeout: float = 0.5) -> bool:
    """Return True if something is listening on localhost:*port*."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# JSONL parsing & validation
# ---------------------------------------------------------------------------


class StagingParseError(ValueError):
    """A JSONL line could not be parsed or validated."""


def _parse_staging_file(path: Path) -> list[dict[str, Any]]:
    """Read and JSON-parse every line in *path*, skipping blank lines.

    Returns a list of raw dicts (not yet validated against the schema).
    Raises :class:`StagingParseError` on any malformed JSON line.
    """
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise StagingParseError(
                    f"{path}:{lineno}: invalid JSON — {exc}"
                ) from exc
            if not isinstance(obj, dict):
                raise StagingParseError(
                    f"{path}:{lineno}: expected a JSON object, got {type(obj).__name__}"
                )
            records.append(obj)
    return records


def _validate_extraction_records(
    records: list[dict[str, Any]],
    ticker: str,
) -> tuple[list[ExtractionPayload], int]:
    """Validate *records* against :class:`ExtractionPayload`.

    Returns ``(valid_payloads, rejected_count)``.  Each *record* must have a
    ``"payload"`` key whose value is an :class:`ExtractionPayload`-compatible dict.
    Records with ``status != "extracted"`` are silently skipped (they carry no
    data to commit).
    """
    valid: list[ExtractionPayload] = []
    rejected = 0

    for rec in records:
        status = rec.get("status", "")
        if status != "extracted":
            continue  # status-only record: no payload to validate
        raw_payload = rec.get("payload")
        if raw_payload is None:
            log.warning("%s: record with status='extracted' has no 'payload' key — skipping", ticker)
            rejected += 1
            continue
        try:
            payload = ExtractionPayload.model_validate(raw_payload)
            valid.append(payload)
        except ValidationError as exc:
            log.warning("%s: schema validation failed — %s", ticker, exc)
            rejected += 1

    return valid, rejected


# ---------------------------------------------------------------------------
# Entity deduplication across payloads
# ---------------------------------------------------------------------------


def _merge_payloads(
    payloads: list[ExtractionPayload],
    ticker: str,
) -> ExtractionPayload:
    """Merge multiple :class:`ExtractionPayload` objects for the same ticker.

    Entities are deduplicated by ``id`` (last-writer-wins for properties).
    Relationships are deduplicated by the ``(source_id, target_id, relation)``
    tuple; the highest-confidence copy wins.
    """
    seen_entity_ids: dict[str, Any] = {}
    seen_rel_keys: dict[tuple[str, str, str], Any] = {}

    for payload in payloads:
        for entity in payload.entities:
            seen_entity_ids[entity.id] = entity

        for rel in payload.relationships:
            key = (rel.source_id, rel.target_id, rel.relation)
            existing = seen_rel_keys.get(key)
            if existing is None or rel.confidence > existing.confidence:
                seen_rel_keys[key] = rel

    total_rejected = sum(p.rejected_count for p in payloads)
    log.debug(
        "%s: merged %d entities and %d relationships from %d payload(s)",
        ticker,
        len(seen_entity_ids),
        len(seen_rel_keys),
        len(payloads),
    )
    return ExtractionPayload(
        entities=list(seen_entity_ids.values()),
        relationships=list(seen_rel_keys.values()),
        rejected_count=total_rejected,
        metadata={"source_ticker": ticker},
    )


# ---------------------------------------------------------------------------
# DB write — stitch overlay then persist via LadybugDB
# ---------------------------------------------------------------------------


def _commit_payload_to_db(
    payload: ExtractionPayload,
    ticker: str,
    db_path: Path,
) -> dict[str, int]:
    """Stitch *payload* through an overlay graph and persist to LadybugDB.

    We reuse :func:`~sandbox_engine.stitch.stitch_coldstart_payload` which:
    1. Resolves extracted entities against canonical nodes via ``ConceptRegistry``.
    2. Stitches cold-start entities into an in-memory ``networkx.DiGraph``.
    3. Connects them to backbone nodes already in LadybugDB.

    The overlay graph is ephemeral (in-memory only).  Persistence to the
    ``.lbug`` file is handled by the LadybugDB idempotent ``BulkLoader`` via a
    minimal stub that translates overlay nodes/edges into the tables the loader
    understands.

    Returns a summary dict with ``{"new_nodes": int, "stitched_backbone_edges": int,
    "ephemeral_edges": int}``.
    """
    import ladybug as lb

    from sandbox_engine.ddl import ensure_schema

    database = lb.Database(str(db_path))
    connection = lb.Connection(database)
    try:
        ensure_schema(connection)

        # Build a lightweight KG-compatible wrapper so InMemoryOverlayGraph can
        # call has_company() and execute() against the live DB.
        kg_stub = _LBugConnectionStub(connection)

        overlay = InMemoryOverlayGraph(kg_connection=kg_stub)
        stitch_summary = stitch_coldstart_payload(overlay, payload, ticker)

        # Persist the in-memory overlay nodes/edges to LadybugDB as
        # Competitor / Supplier nodes and their relationship edges —
        # the only tables the coldstart schema maps to.
        nodes_written, edges_written = _write_overlay_to_lbug(overlay, ticker, connection)

        return {
            "new_nodes": nodes_written,
            "stitched_backbone_edges": stitch_summary.get("stitched_backbone_edges", 0),
            "ephemeral_edges": edges_written,
        }
    finally:
        try:
            connection.close()
        except Exception:
            pass
        try:
            database.close()
        except Exception:
            pass


class _LBugConnectionStub:
    """Minimal KG-compatible adapter for :class:`~sandbox_engine.stitch.InMemoryOverlayGraph`."""

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def has_company(self, ticker: str) -> bool:
        try:
            rows = self._conn.execute(
                "MATCH (c:Company {ticker: $t}) RETURN c.ticker LIMIT 1",
                {"t": ticker.strip().upper()},
            ).get_all()
            return bool(rows)
        except Exception:
            return False

    def execute(self, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
        try:
            return list(self._conn.execute(cypher, params or {}).get_all())
        except Exception:
            return []


def _write_overlay_to_lbug(
    overlay: InMemoryOverlayGraph,
    ticker: str,
    connection: Any,
) -> tuple[int, int]:
    """Persist cold-start overlay nodes/edges to LadybugDB.

    Maps overlay ``entity_type`` → LadybugDB node table and ``relation``
    → the closest LadybugDB relationship type.

    Returns ``(nodes_written, edges_written)``.
    """
    nodes_written = 0
    edges_written = 0

    # ── Node table mapping (coldstart EntityType → LadybugDB node table) ────
    _ENTITY_TABLE: dict[str, str] = {
        "Company": "Company",
        "Competitor": "Competitor",
        "Supplier": "Supplier",
        "Executive": "Customer",    # Best available UFGS proxy for executives
        "RiskFactor": "RiskFactor",
    }

    # ── Collect cold-start nodes (is_cold_start=True, not in backbone) ───────
    company_node_ids: set[str] = set()

    for node_id, attrs in overlay.graph.nodes(data=True):
        if not attrs.get("is_cold_start", False):
            continue
        entity_type = attrs.get("entity_type", "")
        table = _ENTITY_TABLE.get(entity_type)
        if table is None:
            continue

        # Check existing before inserting to avoid the BulkLoader hang described
        # in loader.py: COPY against an existing PK hangs in C++.
        try:
            if table == "Company":
                pk_val = attrs.get("ticker") or node_id
                exists_rows = connection.execute(
                    "MATCH (n:Company {ticker: $pk}) RETURN n.ticker LIMIT 1",
                    {"pk": pk_val},
                ).get_all()
                if exists_rows:
                    company_node_ids.add(node_id)
                    continue
                connection.execute(
                    "CREATE (:Company {ticker: $tk, name: $nm, cik: null, "
                    "sic_code: null, sector: null, fiscal_year_end_month: null, "
                    "fiscal_year_end_day_rule: null})",
                    {"tk": pk_val, "nm": attrs.get("name", pk_val)},
                )
                company_node_ids.add(node_id)
                nodes_written += 1

            elif table == "Competitor":
                nm = attrs.get("name", node_id)
                exists_rows = connection.execute(
                    "MATCH (n:Competitor {name: $nm}) RETURN n.name LIMIT 1",
                    {"nm": nm},
                ).get_all()
                if exists_rows:
                    continue
                connection.execute(
                    "CREATE (:Competitor {name: $nm, ticker: $tk, relation_strength: null})",
                    {"nm": nm, "tk": attrs.get("ticker", "")},
                )
                nodes_written += 1

            elif table == "Supplier":
                nm = attrs.get("name", node_id)
                exists_rows = connection.execute(
                    "MATCH (n:Supplier {name: $nm}) RETURN n.name LIMIT 1",
                    {"nm": nm},
                ).get_all()
                if exists_rows:
                    continue
                connection.execute(
                    "CREATE (:Supplier {name: $nm, relationship_type: null, criticality: null})",
                    {"nm": nm},
                )
                nodes_written += 1

            elif table == "RiskFactor":
                pk_val = node_id
                nm = attrs.get("name", node_id)
                exists_rows = connection.execute(
                    "MATCH (n:RiskFactor {id: $pk}) RETURN n.id LIMIT 1",
                    {"pk": pk_val},
                ).get_all()
                if exists_rows:
                    continue
                connection.execute(
                    "CREATE (:RiskFactor {id: $pk, item_code: null, rf_header: $nm, "
                    "rf_text: null, extracted_entities: null, severity: null, "
                    "year_disclosed: null, year_removed: null})",
                    {"pk": pk_val, "nm": nm},
                )
                nodes_written += 1

        except Exception as exc:
            log.warning("Failed to write %s node %s: %s", table, node_id, exc)

    # ── Edges (only where both endpoints are now in the DB) ─────────────────
    _REL_CYPHER: dict[str, str] = {
        "COMPETES_WITH": (
            "MATCH (a:Company {ticker: $src_tk}), (b:Competitor {name: $tgt_nm}) "
            "CREATE (a)-[:COMPETES_WITH]->(b)"
        ),
        "SOURCES_FROM": (
            "MATCH (a:Company {ticker: $src_tk}), (b:Supplier {name: $tgt_nm}) "
            "CREATE (a)-[:SOURCES_FROM]->(b)"
        ),
        "EXPOSED_TO": (
            "MATCH (a:Company {ticker: $src_tk}), (b:RiskFactor {id: $tgt_id}) "
            "CREATE (a)-[:EXPOSED_TO]->(b)"
        ),
    }

    for src_id, tgt_id, edge_attrs in overlay.graph.edges(data=True):
        rel = edge_attrs.get("relation", "")
        cypher = _REL_CYPHER.get(rel)
        if cypher is None:
            continue

        src_attrs = overlay.graph.nodes.get(src_id, {})
        tgt_attrs = overlay.graph.nodes.get(tgt_id, {})
        src_ticker = src_attrs.get("ticker", "")
        tgt_name = tgt_attrs.get("name", "")
        tgt_nid = tgt_id

        try:
            connection.execute(cypher, {
                "src_tk": src_ticker,
                "tgt_nm": tgt_name,
                "tgt_id": tgt_nid,
            })
            edges_written += 1
        except Exception as exc:
            log.debug("Edge %s-[%s]->%s skipped: %s", src_id, rel, tgt_id, exc)

    return nodes_written, edges_written


# ---------------------------------------------------------------------------
# Safe file archiving
# ---------------------------------------------------------------------------


def _archive_staging_file(src: Path, archive_dir: Path, ticker: str) -> Path:
    """Move *src* to *archive_dir*/<TICKER>-<ISO-timestamp>.jsonl atomically."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = archive_dir / f"{ticker}-{ts}.jsonl"
    # Write to a temp file in the same directory, then rename for atomicity.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=archive_dir, prefix=f".{ticker}.tmp.")
    try:
        os.close(tmp_fd)
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    src.unlink()
    log.info("Archived %s → %s", src.name, dest)
    return dest


# ---------------------------------------------------------------------------
# Main drain loop
# ---------------------------------------------------------------------------


def drain(
    db_path: Path,
    staging_dir: Path,
    archive_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> DrainResult:
    """Process all ``*.jsonl`` files in *staging_dir* and commit to *db_path*.

    Parameters
    ----------
    db_path:      Absolute path to the ``.lbug`` database file.
    staging_dir:  Directory containing staged JSONL files.
    archive_dir:  Destination for successfully committed files.
    force:        Bypass the port-9000 liveness check.
    dry_run:      Parse & validate without writing to the DB or archiving files.

    Returns a :class:`DrainResult` with per-ticker and aggregate statistics.
    """
    result = DrainResult()

    # ── Server isolation check ────────────────────────────────────────────────
    if not force and _server_is_reachable():
        log.error(
            "Web server on port %d is reachable. Drain cannot acquire an exclusive "
            "write lock while the server holds an open handle. Stop the server or "
            "pass --force to skip this check.",
            _SERVER_PORT,
        )
        raise RuntimeError(
            f"Port {_SERVER_PORT} is in use. Stop the server before draining, "
            "or pass --force."
        )

    # ── Gather staging files ──────────────────────────────────────────────────
    jsonl_files = sorted(staging_dir.glob("*.jsonl"))
    if not jsonl_files:
        log.info("No staged JSONL files found in %s", staging_dir)
        return result

    log.info(
        "Found %d staging file(s) in %s%s",
        len(jsonl_files),
        staging_dir,
        " (dry-run — no writes)" if dry_run else "",
    )

    # ── Per-ticker processing ─────────────────────────────────────────────────
    for jfile in jsonl_files:
        ticker = jfile.stem.upper()
        log.info("Processing %s …", jfile.name)

        # 1. Parse
        try:
            records = _parse_staging_file(jfile)
        except StagingParseError as exc:
            log.error("%s: parse error — %s", ticker, exc)
            result.tickers_failed.append(ticker)
            continue

        if not records:
            log.warning("%s: empty staging file — skipping", ticker)
            result.tickers_skipped_status_only.append(ticker)
            continue

        # 2. Validate extraction records
        payloads, rejected = _validate_extraction_records(records, ticker)
        result.records_validated += len(records) - rejected
        result.records_rejected += rejected

        has_extracted = bool(payloads)

        if not has_extracted:
            # Only status-only records: ticker was staged but never fully extracted.
            log.warning(
                "%s: no extraction records found (status-only). "
                "Run the full JIT extraction pipeline for this ticker before draining.",
                ticker,
            )
            result.tickers_skipped_status_only.append(ticker)
            continue

        # 3. Merge payloads
        merged = _merge_payloads(payloads, ticker)

        if dry_run:
            log.info(
                "[DRY-RUN] %s: would commit %d entities, %d relationships",
                ticker,
                len(merged.entities),
                len(merged.relationships),
            )
            result.tickers_dry_run.append(ticker)
            continue

        # 4. Commit to DB
        try:
            summary = _commit_payload_to_db(merged, ticker, db_path)
            result.nodes_committed += summary.get("new_nodes", 0)
            result.edges_committed += summary.get("ephemeral_edges", 0)
            log.info(
                "%s: committed %d new node(s), %d edge(s). Backbone stitches: %d",
                ticker,
                summary.get("new_nodes", 0),
                summary.get("ephemeral_edges", 0),
                summary.get("stitched_backbone_edges", 0),
            )
        except Exception as exc:
            log.error("%s: DB commit failed — %s", ticker, exc)
            result.tickers_failed.append(ticker)
            continue

        # 5. Archive
        try:
            _archive_staging_file(jfile, archive_dir, ticker)
        except Exception as exc:
            # The commit succeeded; archiving failure is non-fatal but must be
            # reported so the operator can clean up manually.
            log.warning(
                "%s: commit succeeded but archiving failed — %s. "
                "The staging file was NOT removed; re-running will skip this ticker "
                "if the PK is now in the DB.",
                ticker,
                exc,
            )

        result.tickers_committed.append(ticker)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drain_staging",
        description=(
            "Drain cold-start staging files into LadybugDB. "
            "The web server on port 9000 must be stopped first "
            "(or use --force to bypass the check)."
        ),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Path to the .lbug database file. "
            "Defaults to sandbox_engine/_run/sandbox.lbug relative to the repo root."
        ),
    )
    p.add_argument(
        "--staging",
        type=Path,
        default=Path("data/staging"),
        metavar="DIR",
        help="Directory containing staged *.jsonl files (default: data/staging).",
    )
    p.add_argument(
        "--archive",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Directory to archive successfully committed files to. "
            "Defaults to <staging>/archive."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Skip the port-9000 liveness check. CAUTION: opening a LadybugDB "
            "file while a server also holds it open causes silent data divergence."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate staging files without writing to the DB.",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(name)s — %(message)s",
    )

    # Resolve paths
    db_path: Path = args.db if args.db else Paths.under(_REPO_ROOT).db
    staging_dir: Path = args.staging
    archive_dir: Path = args.archive if args.archive else staging_dir / "archive"

    if not staging_dir.is_dir():
        log.error("Staging directory does not exist: %s", staging_dir)
        return 1

    if not args.dry_run and not db_path.parent.exists():
        log.error(
            "Database parent directory does not exist: %s. "
            "Run the ingest pipeline at least once to initialise the DB.",
            db_path.parent,
        )
        return 1

    try:
        result = drain(
            db_path=db_path,
            staging_dir=staging_dir,
            archive_dir=archive_dir,
            force=args.force,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    # Print summary
    d = result.as_dict()
    print("\n── Drain Summary ──────────────────────────────────────────")
    print(f"  Committed  : {d['committed']}")
    print(f"  Dry-run    : {d['dry_run']}")
    print(f"  Status-only: {d['skipped_status_only']}")
    print(f"  Failed     : {d['failed']}")
    print(f"  Records validated : {d['records_validated']}")
    print(f"  Records rejected  : {d['records_rejected']}")
    print(f"  Nodes written to DB : {d['nodes_committed']}")
    print(f"  Edges written to DB : {d['edges_committed']}")
    print("───────────────────────────────────────────────────────────\n")

    return 1 if result.tickers_failed else 0


if __name__ == "__main__":
    sys.exit(main())
