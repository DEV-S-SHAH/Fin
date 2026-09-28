"""Ingest a downloaded SEC corpus into the GraphRAG graph.

Extraction is a pure function of ``(chunk text, config)``, so the expensive part
-- the model call -- is run on a thread pool while the cheap, order-sensitive
part -- resolving names and writing to the graph -- is replayed serially in the
original chunk order. The resulting graph is therefore identical to what the
sequential loop in :mod:`graphrag.ingest` would have produced, but it finishes
in a fraction of the time.

Long runs are resumable: each chunk's extraction is cached on disk, so an
interrupted run continues from the last completed chunk instead of paying for
the same model call twice.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from graphrag.config import GraphRAGConfig  # noqa: E402
from graphrag.document import chunk_document  # noqa: E402
from graphrag.extract import (  # noqa: E402
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelationship,
    extract_chunk,
)
from graphrag.ingest import IngestReport, _quality_warnings  # noqa: E402
from graphrag.llm import resolve_client  # noqa: E402
from graphrag.store import GraphStore  # noqa: E402
from tools.sec_fetch import to_document  # noqa: E402

log = logging.getLogger("sec_ingest")
CACHE_VERSION = 1


def _encode(value: Any) -> Any:
    """Make an extraction result JSON-safe."""
    if is_dataclass(value) and not isinstance(value, type):
        return {"__dataclass__": type(value).__name__, "fields": asdict(value)}
    raise TypeError(f"cannot encode {type(value)!r}")


def _decode(payload: dict[str, Any], classes: dict[str, type]) -> Any:
    if "__dataclass__" in payload:
        name = payload["__dataclass__"]
        fields = payload["fields"]
        if name == "ChunkExtraction":
            fields = dict(fields)
            fields["entities"] = [
                classes["ExtractedEntity"](**e) for e in fields["entities"]
            ]
            fields["relationships"] = [
                classes["ExtractedRelationship"](**r) for r in fields["relationships"]
            ]
            return classes["ChunkExtraction"](**fields)
        return classes[name](**fields)
    return payload


def _cache_path(cache_dir: Path, source: Path, chunk_index: int) -> Path:
    name = source.name.replace("/", "-")
    return cache_dir / f"{name}.{chunk_index:05d}.json"


def _load_cached(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    return payload.get("result")


def _store_cached(path: Path, result: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"version": CACHE_VERSION, "result": _encode(result)})
    )
    tmp.replace(path)


def _plan(corpus: Path, config: GraphRAGConfig) -> list[tuple[Path, int, Any]]:
    """Chunk every filing up front so work can be scheduled and cached."""
    plan: list[tuple[Path, int, Any]] = []
    for path in sorted(corpus.rglob("*.htm*")):
        document = to_document(path)
        for chunk in chunk_document(document, config.chunk_tokens, config.chunk_overlap_tokens):
            plan.append((path, chunk.index, chunk))
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=Path("data/aapl-sec"))
    parser.add_argument("--db", type=Path, default=Path("data/aapl.lbug"))
    parser.add_argument("--cache", type=Path, default=Path("data/aapl-cache"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="openai/gpt-oss-20b")
    parser.add_argument("--reset", action="store_true", help="clear the graph first")
    parser.add_argument("--refresh", action="store_true", help="ignore cached extractions")
    parser.add_argument("--limit", type=int, default=None, help="stop after N chunks")
    parser.add_argument("--dry-run", action="store_true", help="plan only, no model calls")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = GraphRAGConfig()
    plan = _plan(args.corpus, config)
    if args.limit:
        plan = plan[: args.limit]
    print(f"planned {len(plan)} chunks across the corpus (workers={args.workers})")
    if args.dry_run:
        return 0

    args.cache.mkdir(parents=True, exist_ok=True)
    client = resolve_client(args.provider, args.model)
    if not client.is_model:
        print("refusing to run: no model credentials resolved", file=sys.stderr)
        return 2
    print(f"provider: {client.name} / {args.model}")

    store = GraphStore(args.db, config)
    store.ensure_schema()
    if args.reset:
        store.reset()
        print("graph reset")
    nodes_before, edges_before = store.counts()

    # Extract in parallel, cache every result, then replay writes in order.
    classes = {
        "ExtractedEntity": ExtractedEntity,
        "ExtractedRelationship": ExtractedRelationship,
        "ChunkExtraction": ChunkExtraction,
    }

    def extract_one(item: tuple[Path, int, Any]) -> tuple[Path, int, Any, Any]:
        path, index, chunk = item
        cache_file = _cache_path(args.cache, path, index)
        if not args.refresh:
            cached = _load_cached(cache_file)
            if cached is not None:
                return path, index, chunk, _decode(cached, classes)
        result = extract_chunk(client, chunk.text, config, chunk.location)
        _store_cached(cache_file, result)
        return path, index, chunk, result

    started = time.time()
    results: list[tuple[Path, int, Any, Any]] = []
    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for path, index, chunk, result in pool.map(extract_one, plan):
            results.append((path, index, chunk, result))
            done += 1
            if result.error:
                errors += 1
            if done % 10 == 0 or done == len(plan):
                rate = done / max(1e-6, time.time() - started)
                remaining = (len(plan) - done) / max(1e-6, rate)
                print(
                    f"  {done}/{len(plan)} extracted | {errors} errors | "
                    f"{rate:.2f} chunks/s | ~{remaining / 60:.0f} min left",
                    flush=True,
                )

    # Serial replay: nodes before edges, original order, identical to ingest.py.
    entities_seen = relationships_seen = rejected = 0
    failures: list[str] = []
    nodes_upserted = edges_upserted = 0
    from collections import Counter

    fan_out: Counter[tuple[str, str]] = Counter()
    orphan_sources = 0
    for path, index, chunk, result in results:
        if result.error:
            failures.append(f"{path.name} chunk {index}: {result.error}")
            continue
        entities_seen += len(result.entities)
        relationships_seen += len(result.relationships)
        rejected += result.rejected
        if result.entities and not result.relationships:
            orphan_sources += 1
        page_note = f"Source: {path.name}, {chunk.location}."
        for entity in result.entities:
            description = entity.description
            if description and page_note not in description:
                description = f"{description} ({page_note})"
            if store.upsert_entity(entity.name, entity.entity_type, description):
                nodes_upserted += 1
        for edge in result.relationships:
            fan_out[(edge.source, edge.relation)] += 1
            if store.upsert_edge(
                edge.source, edge.target, edge.relation, edge.description
            ):
                edges_upserted += 1

    nodes_after, edges_after = store.counts()
    report = IngestReport(document="aapl-sec", pages=0, chunks=len(plan))
    report.entities_seen = entities_seen
    report.relationships_seen = relationships_seen
    report.rejected = rejected
    report.nodes_added = nodes_after - nodes_before
    report.edges_added = edges_after - edges_before
    report.nodes_upserted = nodes_upserted
    report.edges_upserted = edges_upserted
    report.failures = failures
    report.warnings = list(_quality_warnings(fan_out, report))
    if orphan_sources:
        report.warnings.append(
            f"{orphan_sources} chunks produced entities but no relationships; "
            "financial-statement tables tend to extract as isolated nodes"
        )
    print("\n" + "=" * 72)
    print(f"chunks            {len(plan)} ({errors} with errors)")
    print(f"entities seen     {entities_seen}")
    print(f"relationships     {relationships_seen}")
    print(f"rejected          {rejected}")
    print(f"nodes added       {nodes_after - nodes_before} ({nodes_upserted} upserts)")
    print(f"edges added       {edges_after - edges_before} ({edges_upserted} upserts)")
    print(f"entity-only chunks {orphan_sources} (no relationships extracted)")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    if failures:
        print(f"\nfailures ({len(failures)}):")
        for line in failures[:20]:
            print(f"  {line}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
