# LLM Backend Concurrency Analysis & Fix

## Objective
Analyze and safely reduce unnecessary serialization in FinGraph's LLM backend/request path while preserving the existing stack, correctness, provider fallback, and rate-limit behavior. Add concurrency tests, benchmark at 1/10/25/50/100 requests, and report exact contention and remaining bottlenecks.

---

## Important Details
- **No new dependencies, frameworks, runtimes, databases, providers, or orchestration layers.**
- **LadybugDB remains authoritative**; no destructive DB operations or replacement/migration.
- Preserve existing UI/API behavior, GraphRAG semantics, rate limits, retries, provider fallback, and file/schema/ID invariants.
- `RagBackends._lock` protects mutable state (`_session_key`, `_forced`, `_stored_key_rejected`, `_probe`) and is held for microseconds — **must not be removed blindly**.
- `ollama_models()` releases the `RagBackends` lock before network probing.
- No explicit lock-dependent rate limiter was found in `RagBackends`; `RAG_RETRIES=2` is retry behavior, not rate limiting.
- Actual LLM execution creates a fresh `OpenAI` client per request and calls `client.chat.completions.create()` outside `RagBackends._lock`.
- **Real serializer was `KnowledgeGraph.lock`** (single `lb.Connection` guarded by a mutex).
- LadybugDB documents multi-threaded callers; `AsyncConnection` uses a native connection pool (`max_concurrent_queries=4`).
- **Safety incident**: authoritative `sandbox_engine/_run/sandbox.lbug` MD5 changed from `4204aafc129b58cbb97d295c5dcfb20e` to `da4563064322bb78866650983cc0a037` after earlier read-write opens/checkpointing. Semantic fingerprint still matches backup: `122,158` nodes / `189,723` relationships; no schema/WAL damage.
- Do not restart or alter former port `9100` service without user approval.

---

## Work State

### Completed
- HTTP hardening for all four documented servers (`query_ui`, `fingraph`, `legacy_graphrag`, `graphrag_web`).
- `http_limits.py` + `BoundedThreadingHTTPServer`; limits documented in `.env.example`.
- Fixed malformed `_REFUSAL` `Content-Length`.
- `tests/test_http_concurrency.py`: 51 passed.
- LLM-path measurements with stubbed OpenAI (`LLM_DELAY=0.05`):
  - 600 `resolve()` lock acquisitions: p50 `0.1 µs`, p99 `1.1 µs`, max `1.9 µs` — **not the bottleneck**.
  - With single locked connection (pool=1): flat ~1.2 rps at n=1..100.
- LadybugDB stress: 384 queries, 0 errors, ~4,850–5,366 q/s (1 connection); 11,567 q/s (pool=8).
- Live bounded-server verification: `graphrag_web` serves `/` and `/api/stats`; 8 idle keep-alive sockets drop to 0 after `REQUEST_TIMEOUT=3`.
- **Implemented bounded connection pool** in `sandbox_engine/query_ui.py`:
  - `GRAPH_QUERY_SLOTS` default `4` (measured optimum).
  - Read-only handles use the pool; read-write handles forced to 1 slot.
  - Lazy connection creation, safe release/discard, idempotent close.
  - `probe_read()` reports `busy` only when pool exhausted.
- Updated `tests/test_health_endpoints.py` (busy = pool exhaustion, not lock).
- Fixed `tests/test_graceful_shutdown.py` to exhaust pool (was using removed `kg.lock`).
- Fixed `tests/test_qa_eval.py` to use public `kg.execute()` API instead of private `kg.conn`.
- **Added dedicated concurrency test suite** (`tests/test_graph_concurrency.py`): 19 tests covering pool ceiling, overlap proof, failure lifecycle, readiness semantics, and `RagBackends` lock audit.

### Active
- None — all implementation and test updates complete.

### Blocked
- Pre-existing test failures/errors unchanged (11 failed, 22 errors, all in `test_graphrag.py` and `test_query_ui_transport.py`; missing `ui_next`, deleted PDF fixtures, etc.).
- Port `9100` PID `85669` no longer running; restore requires user approval.
- `ui_next` absent; documented `PORT_QUERY_UI_V2=9100` command cannot be restored as written.
- `graphrag_web` has pre-existing no-argument `GraphStore()` startup bug.

---

## Next Move
1. **Done** — full implementation, tests, benchmarks, and verification complete.
2. If the user wants a different `GRAPH_QUERY_SLOTS` default or additional LLM-provider tests, those are follow-ups.

---

## Before/After Benchmark (Stubs LLM at 50 ms)

| concurrency | before (pool=1) wall | after (pool=4) wall | rps before | rps after | speedup |
|-------------|---------------------|---------------------|------------|-----------|---------|
| 1           | 0.819 s             | 0.751 s             | 1.2        | 1.3       | 1.1×    |
| 10          | 7.825 s             | 3.506 s             | 1.3        | 2.9       | **2.2×** |
| 25          | 21.287 s            | 10.194 s            | 1.2        | 2.5       | **2.1×** |
| 50          | 46.325 s            | 23.719 s            | 1.1        | 2.1       | **1.9×** |
| 100         | 94.239 s            | 55.375 s            | 1.1        | 1.8       | **1.6×** |

- Throughput improved **~2× at moderate concurrency**; single-request latency unchanged.
- **4 slots is the measured optimum** (sweep: 1→1.3, 2→2.0, **4→2.4**, 8→2.2, 16→2.0 rps at n=25).
- 0 errors in all runs.

### Detailed Attribution (n=25, pool=4)
| component              | aggregate time | % of wall | notes |
|------------------------|----------------|-----------|-------|
| pool WAIT (queued)     | 140.6 s        | 1,382%    | demand exceeds 4 slots; each request issues ~20 sequential graph queries |
| conn.execute (real)    | 21.2 s         | 208%      | avg concurrency **2.08 of 4 slots** — pool NOT saturated |
| LLM stub (50 ms)       | 2.5 s          | 25%       | with real model (~9 s) this dominates completely |

**Remaining bottlenecks:**
1. **Per-request sequential query chain** (~20 Cypher round-trips) dominates latency; parallelism limited by dependency order.
2. **LLM provider latency** (~9 s/call) swamps everything — this change mainly prevents other requests' graph work from queuing behind one request's LLM call (the old code already did this for LLM, but not for graph reads).
3. **LadybugDB/CPython contention** caps effective pool utilization at ~2 concurrent queries even with 4 slots.

---

## Test Results
- **Full suite** (minus `ui_next`): **1,130 passed, 11 failed, 22 errors, 5 skipped** — identical to pre-change baseline (11 failed, 22 errors).
- **+19 new concurrency tests** in `tests/test_graph_concurrency.py` — all passing.
- Targeted suites:
  - `tests/test_health_endpoints.py`: 39 passed, 9 subtests.
  - `tests/test_graceful_shutdown.py`: 35 passed, 23 subtests.
  - `tests/test_qa_eval.py`: 24 passed, 6 subtests.
  - `tests/test_http_concurrency.py`: 51 passed.

---

## Correctness Reasoning
| change | risk | mitigation |
|--------|------|------------|
| `RagBackends._lock` retained | medium — mutable state | audit proves lock held ≤2 µs; tests `test_concurrent_setters_and_readers_stay_consistent` and `test_a_key_typed_in_the_browser_is_never_lost_under_contention` pin behavior |
| single connection → pool | high — schema/translation/transactions | `KnowledgeGraph.execute()` still routes through `_raw_execute` → pool slot → `translate_for_engine` → `connection.execute`; all paths preserved |
| `probe_read` busy semantics | medium — readiness flapping | changed from "any lock contention" to "pool exhausted"; test `test_readiness_reports_busy_only_when_the_pool_is_exhausted` enforces |
| `kg.close()` order | medium — in-flight query + DB close | `pool.close()` stops new acquires, then `db.close()`; tests `test_using_a_closed_pool_raises...` and shutdown test verify |
| read-write forced to 1 slot | low — mutation safety | `KnowledgeGraph(read_only=False)` → `pool.max_size=1`; test `ReadWriteHandlesStaySerialised` verifies |

**Rollback**: Revert `sandbox_engine/query_ui.py` and `.env.example` `GRAPH_QUERY_SLOTS`; tests revert automatically. No DB migration.

---

## Files Changed
- `sandbox_engine/query_ui.py` — pool, `KnowledgeGraph`, `GRAPH_QUERY_SLOTS`, `probe_read`, `_raw_execute`.
- `sandbox_engine/http_limits.py` — already hardened (prior work).
- `tests/test_health_endpoints.py` — busy semantics updated.
- `tests/test_graceful_shutdown.py` — pool exhaustion test.
- `tests/test_qa_eval.py` — public API instead of `kg.conn`.
- `tests/test_graph_concurrency.py` — **new**: 19 concurrency tests.
- `.env.example` — documents `GRAPH_QUERY_SLOTS=4`.

---

## Acceptance
- ✅ Throughput ~2× at n=10..50 with 0 errors.
- ✅ Single-request latency unchanged.
- ✅ `RagBackends` lock retained; audit proves microsecond hold.
- ✅ Pool never exceeds `GRAPH_QUERY_SLOTS`; failed connections discarded.
- ✅ Read-only pool=4, read-write pool=1.
- ✅ Readiness busy only when pool exhausted.
- ✅ Full suite baseline unchanged (11 failed, 22 errors — all pre-existing).
- ✅ DB semantic fingerprint identical (122,158 / 189,723).
- ✅ No new dependencies, no schema changes, no destructive operations.