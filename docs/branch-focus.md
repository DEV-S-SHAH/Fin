# Branch: `feature/graph`

## Purpose
Graph-layer work only: traversal, multi-hop reasoning, cold-start overlay, entity resolution, and neighborhood retrieval.

## What this branch owns
- `sandbox_engine/traversal.py` — graph neighborhood traversal
- `sandbox_engine/store.py` — `neighborhood()`, `build_evidence()`
- `sandbox_engine/coldstart_extract.py`, `coldstart_synthesis.py`, `stitch.py`
- `graphrag/` — generic GraphRAG extraction, chunking, entity resolution, graph storage
- Company-aware traversal and seed protection in `_trim()`

## What this branch must NOT do
- Replace graph traversal with generic vector-only retrieval
- Bypass company isolation (`CompanyScope`, seed ticker anchoring)
- Change ingestion parser/loader/router or SEC acquisition logic
- Alter LadybugDB schema without `ensure_schema()` introspection and review

## Related tests to keep green
- `test_multi_hop_traversal.py`, `test_neighborhood_priority.py`, `test_coldstart_extract.py`, `test_coldstart_stitch.py`, `test_coldstart_latency.py`, `test_provenance.py`
