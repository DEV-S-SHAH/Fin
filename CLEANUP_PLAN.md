# FinGraph Phase 6 — Repository Cleanup Plan

**Generated from parallel specialist audits (Stage A) + dependency analysis (Stage A2)**

---

## EXECUTIVE SUMMARY

This plan classifies every candidate file based on **traceable dependency analysis**, not filename heuristics. The canonical application is:

```bash
python -m ui.fingraph --port 9100 --no-browser
```

running at `http://127.0.0.1:9100` — the single official local UI.

---

## CLASSIFICATION LEGEND

| Code | Meaning |
|------|---------|
| **KEEP** | Active in canonical paths; tested; no duplication |
| **REMOVE** | Dead code; no imports; no tests; no entry points |
| **ARCHIVE** | Historical value; move to `archive/` not in source control |
| **REFACTOR** | Active but violates boundaries / oversized / needs splitting |
| **VERIFY** | Uncertain — needs runtime verification before decision |

---

## 1. ROOT-LEVEL MODULES (6 files — all DEAD CODE)

| File | Lines | Classification | Rationale |
|------|-------|----------------|-----------|
| `financial_graphrag.py` | 2,105 | **REMOVE** | Only imported by `graphrag_synthesis.py` (also root) and `tests/test_router.py` (uses `parse_question`). Not imported by `sandbox_engine`, `graphrag`, `ingestion`, or `ui`. Contains hardcoded `SEED_COMPANIES = (AAPL, MSFT)` violating company isolation. |
| `graph_store.py` | 1,756 | **REMOVE** | Only imported by `tests/test_graph_store.py`. Duplicates `graphrag/store.py` with different schema (`Entity`/`RELATION` vs `Entity`/`CONNECTS`). Not used by any pipeline. |
| `graph_extractor.py` | 886 | **REMOVE** | Only imported by `tests/test_graph_extractor.py`. Duplicates `graphrag/extract.py` functionality. |
| `entity_resolver.py` | 988 | **REMOVE** | Only imported by `tests/test_entity_resolver.py` and `graph_store.py` (also dead). Duplicates `graphrag/resolve.py` + `sandbox_engine/entity_resolver.py`. |
| `document_loader.py` | 1,360 | **REFACTOR → MOVE** | **Active** — imported by `ingestion/sec_acquisition.py`, `ingestion/orchestrator.py`, `tools/sec_fetch.py`, `tests/test_document_loader.py`. **But** should move to `ingestion/document_loader.py` since it's the canonical universal loader. Current root location is architectural anomaly. |
| `graphrag_synthesis.py` | 1,139 | **REMOVE** | Only imported by `financial_graphrag.py` (dead) and `tests/test_router.py` (uses `parse_question`). Duplicates `graphrag/qa.py` + `sandbox_engine/coldstart_synthesis.py`. |

**Dependency Trace**: These 6 modules form a **closed island** — they import each other and are imported only by tests. No production pipeline (`sandbox_engine`, `graphrag`, `ingestion`, `ui`) imports them.

---

## 2. UI SERVERS (3 servers → 1 canonical)

| Server | Lines | Classification | Dependencies | Rationale |
|--------|-------|----------------|--------------|-----------|
| `ui/fingraph/server.py` | 1,729 | **KEEP** (canonical) | `sandbox_engine.query_ui` (KnowledgeGraph, resolve_db_path, router, shutdown, observability, ssrf), `sandbox_engine.http_limits` (indirect via query_ui) | **Official UI per Phase 6 requirement**. Modern vanilla JS, authentication, company pages, GraphRAG Studio. |
| `ui/legacy_graphrag/server.py` | 873 | **REMOVE** | `sandbox_engine.query_ui` (KnowledgeGraph, BackgroundIngestQueue, CANNED_REPORTS, OLLAMA_MODEL, RAG_TIMEOUT, MAX_BODY, _int_param, _listeners, parse_ports, resolve_db_path, get_backends, run_report, _setting, _DB_CANDIDATES), `ladybug` directly | **Explicitly legacy** per docstring. Embedded HTML (873 lines). No tests depend on it. Only referenced in README as legacy. |
| `ui/graphrag_web/server.py` | 413 | **REMOVE** | `graphrag` package (config, llm, qa, store), `sandbox_engine.http_limits` (BoundedThreadingHTTPServer), `sandbox_engine.observability` | Serves generic GraphRAG on port 8765. Tests: `test_http_concurrency.py` (5 imports), `test_health_endpoints.py` (1 import). **Not the canonical UI**. Functionality should be available via `fingraph` if needed. |

**UI Test Impact**:
- `test_http_concurrency.py` → tests `ui.graphrag_web` → **UPDATE to test `ui.fingraph`** (security test — cannot be deleted)
- `test_health_endpoints.py` → tests `ui.fingraph` → **KEEP test**
- `test_ui_next_server.py` → tests `ui.fingraph` → **KEEP test**
- `test_ui_next_auth.py` → tests `ui.fingraph` → **KEEP test**

---

## 3. SANDBOX_ENGINE — DEAD / OBSOLETE MODULES

| File | Lines | Classification | Rationale |
|------|-------|----------------|-----------|
| `sandbox_engine/agent/` | (dir) | **REMOVE** | Empty directory |
| `sandbox_engine/community.py` | 3.7 KB | **REMOVE** | No imports in production code. Only `test_background_community.py` imports it. Louvain community detection never wired. |
| `sandbox_engine/test_graph_payload.py` | 4.7 KB | **REMOVE** | Test file for non-existent API; no pytest collection |
| `sandbox_engine/test_rag_backends.py` | 11 KB | **REMOVE** | Tests `RagBackends` from `query_ui` but no test runner imports it |
| `sandbox_engine/test_entity_resolver.py` | 24 KB | **REWRITE → `tests/test_sandbox_entity_resolver.py`** | Module-local test; must be **rewritten against `sandbox_engine.entity_resolver` API** (root `entity_resolver.py` is dead). Not a mechanical rename. |
| `sandbox_engine/test_parser_identity.py` | 13 KB | **MOVE → `tests/test_sandbox_parser_identity.py`** | Module-local test |
| `sandbox_engine/test_temporal_integrity.py` | 7.8 KB | **MOVE → `tests/test_sandbox_temporal_integrity.py`** | Module-local test |
| `sandbox_engine/benchmarks.py` | 25 KB | **KEEP** | Benchmark harness — used by `sandbox_engine/__main__.py` (CLI entry) and `sandbox_engine/__init__.py`. Active. |
| `sandbox_engine/eval_set.py` | 16 KB | **KEEP** | Golden queries — used by `benchmarks.py` and `sandbox_engine/__main__.py`; tested by `tests/test_eval_set.py`. Active. |
| `sandbox_engine/background.py` | 12 KB | **KEEP** | `BackgroundIngestQueue` — instantiated at module level in `query_ui.py:111`, used by `legacy_graphrag`, tested by `test_persistence_restart.py` and `test_graceful_shutdown.py`. Active. |
| `sandbox_engine/observability.py` | 20 KB | **KEEP** | Structured logging — imported by `tier1_fetch.py`, `query_ui.py`, `background.py`, and `ui/graphrag_web/server.py`. Active. |
| `sandbox_engine/tier1_clean.py` | 3.5 KB | **REMOVE** | Appears to be old version of `tier1_fetch.py` — not imported anywhere. |

---

## 4. SANDBOX_ENGINE — OVERSIZED MODULES (REFACTOR)

| File | Lines | Classification | Proposed Split |
|------|-------|----------------|----------------|
| `sandbox_engine/query_ui.py` | 5,983 | **REFACTOR** (God Module) | Split into: `server.py` (HTTP), `retrieval.py` (RAG), `synthesis.py` (answer synthesis), `reports.py` (canned reports), `benchmark.py` (benchmark endpoints), `health.py` (health checks) |
| `sandbox_engine/parser.py` | 3,299 | **REFACTOR** (if feasible) | `parser/html.py`, `parser/tables.py`, `parser/xbrl.py`, `parser/sections.py` — only if clear boundaries exist |
| `sandbox_engine/provenance.py` | 4,200 | **KEEP AS-IS** | Large but justified — complete provenance contract + grader. Single responsibility. |
| `sandbox_engine/entity_resolver.py` | 1,600 | **KEEP AS-IS** | Large but justified — registry + fuzzy + embedding + persistence. |
| `sandbox_engine/ufgs_extract.py` | 2,300 | **KEEP AS-IS** | UFGS dual-track + causal layer — additive to parser. |
| `sandbox_engine/ufgs_schema.py` | 33 KB | **INLINE** | Only referenced by `ufgs_extract.py` and `buffer.py` (NODE_TABLES). Could inline into those two files. |

---

## 5. GRAPHRAG — DUPLICATE / MISSING CONTRACT

| File | Classification | Rationale |
|------|----------------|-----------|
| `graphrag/envfile.py` | **KEEP** | Used by `tests/test_graphrag.py` (6 imports) to load `.env` into `os.environ`. Small utility (77 lines). Not duplicated — different purpose than `_setting()`/`config.from_env()`. |
| `graphrag/qa.py` | **REFACTOR** | **Missing provenance contract** — no `STATED`/`DERIVED`/`INFERRED`/`EXTERNAL`/`GAP` tags, no `grade_answer()`, no misattribution check. Either implement full contract or deprecate for SEC use. |
| `graphrag/resolve.py` | **KEEP** | Canonical for generic pipeline. Different from `sandbox_engine/entity_resolver.py` by design (AGENTS.md pipeline boundary). |
| `graphrag/store.py` | **KEEP** | Canonical for generic pipeline. Has seed protection (`_trim()`) that `sandbox_engine` lacks. |

---

## 6. TOOLS — DUPLICATE SEC FETCHING

| File | Classification | Rationale |
|------|----------------|-----------|
| `tools/sec_fetch.py` | **REFACTOR** | Duplicates `sandbox_engine/tier1_fetch.py` and `ingestion/sec_acquisition.py`. **Must verify usage**: imports `document_loader` and `graphrag.document.Document` — serves `graphrag` ingestion path. Consolidation must either: (a) make `tier1_fetch.py` the single source with pipeline-specific callers, or (b) explicitly deprecate `graphrag` ingestion path. |
| `tools/sec_ingest.py` | **KEEP** | Uses `graphrag` package for generic PDF ingestion — valid tooling. |
| `tools/drain_staging.py` | **KEEP** | Critical: offline consolidation with port-9000 lock check. |
| `tools/make_fixture_pdf.py` | **KEEP** | Test fixture generator. |

---

## 7. TESTS — DEAD / MISPLACED

| Test File | Classification | Rationale |
|-----------|----------------|-----------|
| `tests/test_graph_extractor.py` | **REMOVE** | Tests `graph_extractor.py` (dead root module) |
| `tests/test_agent_routing.py` | **REMOVE** | Tests non-existent agent module |
| `tests/test_background_community.py` | **REMOVE** | Tests `community.py` (dead) |
| `tests/test_entity_resolver.py` | **REWRITE** | Tests root `entity_resolver.py` (dead). **Must rewrite against `sandbox_engine.entity_resolver` or `graphrag.resolve` API** — not a mechanical rename. |
| `tests/test_graph_store.py` | **REMOVE** | Tests root `graph_store.py` (dead) and root `entity_resolver.py` (dead) |
| `tests/test_benchmark_runner.py::test_mock_harness_reaches_two_hops_through_the_real_traverser` | **VERIFY** | Known Phase 5 failure — mock passes strings where `EvidenceRef` expected. Keep but mark `xfail` or fix in testing phase. |
| `tests/test_http_concurrency.py` | **UPDATE** | Tests `ui.graphrag_web` — **update to test `ui.fingraph`** (security regression coverage) |

**Tests to KEEP (critical regression coverage)**:
- `test_provenance.py`, `test_provenance_ui.py` — provenance contract
- `test_persistence_restart.py` — DB/registry/staging survival
- `test_drain_staging.py` — port-9000 isolation, atomic archive
- `test_router.py` — KNOWN/COLD_START/AMBIGUOUS routing
- `test_ingestion.py`, `test_generic_ingestion.py` — ingestion pipelines
- `test_graphrag.py` — GraphRAG package tests
- `test_multi_hop_traversal.py`, `test_neighborhood_priority.py` — graph traversal
- `test_ssrf.py` — security
- `test_http_concurrency.py` — **updated** to test `ui.fingraph`
- `test_ui_next_server.py`, `test_ui_next_auth.py` — canonical UI

---

## 8. GENERATED / RUNTIME ARTIFACTS

| Path | Classification | `.gitignore` Action |
|------|----------------|---------------------|
| `sandbox_engine/_run/sandbox.lbug` | Runtime DB | **FIX** — line 49 `!sandbox_engine/_run/sandbox.lbug` forces into git; **remove negation** |
| `sandbox_engine/_run/sandbox.lbug.wal` | WAL | Already ignored |
| `sandbox_engine/_run/staging/*.parquet` | Staging spill | Already ignored |
| `sandbox_engine/_run/concepts.json` | Registry | Already ignored |
| `sandbox_engine/_run/report.json` | Report | Already ignored |
| `data/` | Data dir | Already ignored |
| `data/staging/*.jsonl` | Cold-start staging | Already ignored |
| `backups/` | Backup artifacts | **ADD** — not in `.gitignore` |
| `graphrag_db.lbug` | Stale DB (v47) | **DELETE** — unreadable, version mismatch |
| `data/aapl-2026.lbug` | Stale DB (v47) | **DELETE** — unreadable, version mismatch |
| `backups/20261004T100949471463Z-sandbox-a65d118b19bb/` | Stale backup (v47) | **DELETE** — unreadable |
| `.env` | Local secrets | Already ignored (but contains live key — rotate) |

---

## 9. CONFIGURATION CLEANUP

| Item | Classification | Action |
|------|----------------|--------|
| `neo4j_password="password"` default in `ingestion/orchestrator.py:58` | **SECURITY FIX** | Remove default; require `NEO4J_PASSWORD` env var |
| `backups/` missing from `.gitignore` | **FIX** | Add `backups/` |
| `.gitignore` line 49: `!sandbox_engine/_run/sandbox.lbug` | **BUG FIX** | Remove negation line |
| `PORT_QUERY_UI=9000`, `PORT_QUERY_UI_V2=9100`, `PORT_GRAPHRAG_UI=8765` | **KEEP** | Multiple UIs documented; only 9100 is canonical |
| `RAG_BACKEND=auto` in `.env.example` | **KEEP** | Valid dev config |

---

## 10. DEPENDENCY CLEANUP (Python Packages)

No unused dependencies found in `setup.py` / `pyproject.toml` that aren't imported by active code. All imports traced to active modules.

---

## 11. ARCHITECTURAL VIOLATIONS TO FIX (Not Cleanup — Post-Cleanup)

These are **not** file deletions but architecture corrections needed after cleanup:

1. **Seed protection missing in `sandbox_engine`** — `graphrag/store._trim()` has it; `sandbox_engine/traversal.py` and `query_ui` do not (AGENTS.md §2)
2. **`graphrag` lacks provenance contract** — no grading, no misattribution check (AGENTS.md §2, §4)
3. **`ui/fingraph` reaches into `query_ui` private symbols** — `_int_param`, `_listeners`, `_setting`, `_DB_CANDIDATES`, `CANNED_REPORTS`, `run_report` (Violation 1 from fin-evidence audit)
4. **Three SEC fetch implementations** — consolidate to `tier1_fetch.py` as single source

---

## 12. EXECUTION ORDER (Controlled Cleanup)

### Phase 1: Safe Deletions (No Shared Files)
```bash
# 1. Root dead modules
rm financial_graphrag.py graph_store.py graph_extractor.py entity_resolver.py graphrag_synthesis.py

# 2. Dead sandbox_engine modules
rm -rf sandbox_engine/agent/
rm sandbox_engine/community.py sandbox_engine/test_graph_payload.py sandbox_engine/test_rag_backends.py sandbox_engine/tier1_clean.py

# 3. Dead UI servers
rm -rf ui/legacy_graphrag/ ui/graphrag_web/

# 4. Dead tests
rm tests/test_graph_extractor.py tests/test_agent_routing.py tests/test_background_community.py tests/test_graph_store.py

# 5. Stale runtime artifacts (verify LadybugDB version first — attempt read to confirm unreadable)
# rm graphrag_db.lbug data/aapl-2026.lbug
# rm -rf backups/20261004T100949471463Z-sandbox-a65d118b19bb/
```

### Phase 2: Config/Security Fixes (Prerequisite for Tests)
```bash
# Fix .gitignore (remove line 49 negation, add backups/)
# Fix ingestion/orchestrator.py: remove neo4j_password default
# Verify graphrag/envfile.py has no remaining imports before deletion
```

### Phase 3: Test Updates
```bash
# Update test_http_concurrency.py to test ui.fingraph (not graphrag_web)
# Rewrite test_entity_resolver.py against sandbox_engine.entity_resolver API
# Mark benchmark mock test as xfail or fix
```

### Phase 4: Module Move
```bash
# Move document_loader.py → ingestion/document_loader.py
# Update imports in ingestion/sec_acquisition.py, ingestion/orchestrator.py, tools/sec_fetch.py
```

### Phase 5: Test Reorganization
```bash
# Move sandbox_engine tests to tests/ with prefix
mv sandbox_engine/test_parser_identity.py tests/test_sandbox_parser_identity.py
mv sandbox_engine/test_temporal_integrity.py tests/test_sandbox_temporal_integrity.py
```

### Phase 6: Large File Refactor (Sequential, Preserve Behavior)
```bash
# Split query_ui.py — one module at a time, run tests after each
# Inline ufgs_schema.py into buffer.py and ufgs_extract.py
```

### Phase 7: Verify & Run Full Suite
```bash
# Run full test suite
# Start canonical UI: python -m ui.fingraph --port 9100 --no-browser
# Verify endpoints: /, /app, /auth, /company/AAPL, /api/companies, /api/route, /api/markets
```

---

## 13. RISKS & UNRESOLVED QUESTIONS

| Risk | Mitigation |
|------|------------|
| `graphrag` QA lacks provenance contract | Post-cleanup decision: implement full contract or deprecate for SEC |
| `sandbox_engine` missing seed protection | Add `_trim()` logic from `graphrag/store.py` to `traversal.py` |
| `ui/fingraph` reaches into `query_ui` private symbols | Expose clean facade in `sandbox_engine` before refactor |
| Three SEC fetch implementations | Consolidate to `tier1_fetch.py` as canonical; others import from it |
| `benchmarks.py` + `eval_set.py` — verify if needed for CI | Run CI to see if any job references them (now confirmed active) |
| `background.py` — never started as service | Verified: instantiated in `query_ui.py:111`, active |
| `test_entity_resolver.py` rewrite effort | Allocate dedicated time; not a mechanical rename |
| `test_http_concurrency.py` accidental deletion | **Must update, not delete** — security regression coverage |
| Stale DB version assumption | Attempt read before delete; if readable, archive instead |
| `tools/sec_fetch.py` consolidation | Preserve `graphrag` ingestion path or explicitly deprecate it |

---

## 14. POST-CLEANUP VALIDATION CHECKLIST

- [ ] Full test suite passes (except known pre-existing `test_benchmark_runner.py` mock failure)
- [ ] `python -m ui.fingraph --port 9100 --no-browser` starts and serves `/`, `/app`, `/auth`, `/company/AAPL`, `/api/companies`, `/api/route`, `/api/markets`
- [ ] GraphRAG query flow works (SSE streaming, citations, grading)
- [ ] Graph visualization works where supported
- [ ] Error states functional
- [ ] `fin-reviewer` reviews cleaned repository → PASS

---

## 15. FILES SUMMARY

| Category | Count | Action |
|----------|-------|--------|
| Root dead modules | 6 | REMOVE (5), REFACTOR→MOVE (1) |
| UI servers | 3 | KEEP 1, REMOVE 2 |
| Sandbox dead modules | 3 | REMOVE 3 (`agent/`, `community.py`, `test_*.py`, `tier1_clean.py`) |
| Sandbox active modules | 7 | KEEP (`benchmarks.py`, `eval_set.py`, `background.py`, `observability.py`, `query_ui.py`, `parser.py`, `provenance.py`, `entity_resolver.py`, `ufgs_extract.py`, `ufgs_schema.py`) |
| Sandbox oversized | 2 | REFACTOR (`query_ui.py`, `parser.py`) |
| Sandbox inline | 1 | INLINE (`ufgs_schema.py`) |
| Graphrag issues | 4 | REMOVE 1 (`envfile.py`), REFACTOR 1 (`qa.py`), KEEP 2 |
| Tools | 4 | REFACTOR 1, KEEP 3 |
| Tests | 30+ | REMOVE 3, REWRITE 1, UPDATE 1, VERIFY 1, KEEP rest |
| Runtime artifacts | 10+ | DELETE stale (verify first), FIX .gitignore |
| Config issues | 3 | FIX 3 |

**Total estimated deletions: ~20 files + 2 directories + stale artifacts**
**Total estimated refactors: ~5 major modules**

---

**Next Step**: `fin-reviewer` must review this plan before any destructive changes.