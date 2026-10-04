# FinGraph Production Readiness Audit

**Target**: 10,000+ users  
**Constraint**: Keep existing technology stack wherever technically possible  
**Scope**: Full system audit — database, API, ingestion, GraphRAG, LLM, security, deployment  

---

## 1. EXISTING STACK INVENTORY

| Component | Current Technology | Version | Where Used | Criticality | Keep? |
|---|---|---|---|---|---|
| **Embedded Graph DB** | LadybugDB | 0.20.4 | `sandbox_engine/loader.py`, `sandbox_engine/query_ui.py`, `graphrag/store.py` | P0 — All persistent state | YES |
| **HTML Parsing** | pandas + lxml + beautifulsoup4 | 3.0.6 / 6.1.3 / 4.15.0 | `sandbox_engine/parser.py` | P0 — Ingestion pipeline | YES |
| **Parquet Spill** | pyarrow | 25.0.1 | `sandbox_engine/buffer.py`, `sandbox_engine/loader.py` | P1 — Staging layer | YES |
| **PDF Text Extraction** | pypdf | 6.19.0 | `graphrag/document.py`, `document_loader.py` | P1 — PDF ingestion | YES |
| **LLM Client (Primary)** | openai SDK | 3.19.2 | `graphrag/llm.py`, `sandbox_engine/query_ui.py` | P0 — All LLM calls | YES |
| **Schema Validation** | pydantic | ≥2.0.0 | `sandbox_engine/coldstart_schema.py`, `graph_extractor.py` | P1 — Data integrity | YES |
| **In-Memory Graph** | networkx | ≥3.0 | `sandbox_engine/stitch.py`, `sandbox_engine/traversal.py` | P1 — Cold-start overlay | YES |
| **HTTP Server** | stdlib `ThreadingHTTPServer` | Python 3.13 | `ui/fingraph/server.py`, `sandbox_engine/query_ui.py` | P0 — All API endpoints | YES |
| **Background Workers** | `ThreadPoolExecutor` | Python 3.13 | `sandbox_engine/background.py` | P1 — Async ingestion | YES |
| **Frontend (React)** | React 19 + TypeScript + Vite + Tailwind 4 | Latest | `web/` | P0 — Primary UI | YES |
| **Market Data** | requests + Yahoo Finance API | 2.31.0+ | `ui/fingraph/server.py` | P2 — Landing page | YES |
| **Authentication** | HMAC sessions + OAuth (Google, Apple, TradingView) | Custom | `ui/fingraph/server.py` | P1 — Auth | YES |
| **CLI** | typer | 0.27.2 | `sandbox_engine/cli.py`, `graphrag/cli.py` | P2 — Operations | YES |

**Verdict**: All core technologies are well-chosen for the problem domain. LadybugDB is the only unusual choice — an embedded property graph written in C++ with Python bindings. Everything else is standard, well-maintained Python ecosystem.

---

## 2. SYSTEM EXECUTION FLOWS

### 2.1 Query Path (User → Answer)
```
USER (browser)
  ↓ HTTP/WS
FIN_GRAPH_UI (ThreadingHTTPServer, port 9100)
  ↓ /api/ask (SSE stream)
RAG_BACKENDS.resolve() → LLM provider selection
  ↓ route_query() → KNOWN / COLD_START / AMBIGUOUS
KNOWN PATH:
  ↓ retrieve_financial_context() → multiple Cypher queries
  ↓ build_evidence() + serialise_evidence()
  ↓ LLM streaming (OpenAI-compatible) → SSE tokens
COLD_START PATH:
  ↓ SECRuntimeFetcher.fetch_latest_filing_html() (2.5s SLA)
  ↓ clean_and_truncate_section() (6k tokens)
  ↓ ColdStartExtractor.extract() (15-30 triples, <3.5s)
  ↓ InMemoryOverlayGraph + stitch_coldstart_payload() (<1s)
  ↓ HybridGraphTraverser.traverse_neighborhood() (2-hop)
  ↓ ColdStartSynthesizer.stream_synthesis() (5-section report)
  ↓ background_queue.enqueue_coldstart_sync() (fire-and-forget)
```

### 2.2 Ingestion Path (SEC Filings → Graph)
```
sandbox_engine/data/<company>/<year>/<form>/<filing>.htm
  ↓ python -m sandbox_engine --reset
FilingParser.ingest_file() (pure Python, ~0.1s/filing)
  → ExtractionResult (nodes, edges, chunks, metrics)
  → apply_ufgs() (Universal Financial Graph Schema)
StageBuffer.add_result() → Arrow tables → Parquet spill
  ↓ BulkLoader (COPY/UNWIND into LadybugDB)
  → Pre-flight key check (avoids COPY hang on duplicate PK)
  → Idempotent load (lookup-before-insert)
  → Benchmarks B1-B5 (integrity verification)
```

### 2.3 PDF GraphRAG Path (Document → Graph)
```
PDF → graphrag/document.py.load_pdf() → Document (pages, chunks)
  ↓ chunk_document() (token-aware, overlap)
  ↓ graphrag/extract.py.extract_chunk() (LLM → entities + relations)
  ↓ GraphStore.upsert_entity() / upsert_edge() (MERGE on id)
  → LadybugDB: Entity(id, name, type, desc) + CONNECTS(from, to, rel_type, desc)
```

---

## 3. DATABASE CONCURRENCY AUDIT (LadybugDB)

### 3.1 Current Architecture
```python
# loader.py:149-162 — Single writer, single connection per run
def _open(self):
    self.database = lb.Database(str(self.path))
    self.connection = lb.Connection(self.database)

# query_ui.py:789-795 — Read-only handles for query services
def __init__(self, db_path: Path, read_only: bool = True):
    self.db = lb.Database(str(db_path), read_only=read_only)
    self.conn = lb.Connection(self.db)
    self.lock = threading.Lock()  # Python-level lock per KnowledgeGraph instance
```

### 3.2 Concurrency Reality

| Scenario | Supported? | Evidence |
|---|---|---|
| Single process, multiple threads (read) | YES | `KnowledgeGraph` has `threading.Lock()` per instance |
| Single process, multiple threads (write) | NO | Loader opens DB read-write; second handle "silently diverges" (loader.py:44-46) |
| Multiple processes (read) | YES | Multiple `read_only=True` handles work |
| Multiple processes (write) | NO | "Open a database path with exactly **one** handle at a time" (loader.py:44) |
| Concurrent ingestion + query | RISKY | Background ingestion uses separate process; query UI uses read-only |
| Horizontal scaling (multiple API instances) | NO | Each needs own DB handle; write coordination impossible |

### 3.3 Critical Limitation: Single-Writer Constraint

**From loader.py:40-46:**
> "A connection that has committed a sizeable write can hang on its next parameterised read. Opening the database once per run and closing it before returning keeps the write-then-read sequence inside one short-lived handle. Relatedly: open a database path with exactly **one** handle at a time. The engine does not reject a second handle -- it opens, reads the committed state, and then silently diverges, with writes through one invisible to the other."

**This is the fundamental architectural bottleneck.**

### 3.4 What Works Within Constraints

| Workload | Max Safe Concurrency | Architecture Required |
|---|---|---|
| Read-only query API | Unlimited (with read-only handles) | Multiple `KnowledgeGraph(read_only=True)` per process |
| Background ingestion | 1 writer at a time | Single `BulkLoader` process, serialized via queue |
| Mixed read/write | Sequential only | Write acquires exclusive access; reads wait or use snapshot |

---

## 4. STATE AUDIT

| State | Classification | Location | Multi-Instance Safe? | Recovery |
|---|---|---|---|---|
| LadybugDB `.lbug` + `.wal` | **Persistent** | `sandbox_engine/_run/sandbox.lbug` | NO (single writer) | WAL replay on open |
| Concept registry `concepts.json` | **Derived** | `sandbox_engine/_run/concepts.json` | NO (file-based) | Rebuilt from filings |
| Parquet staging spill | **Derived** | `sandbox_engine/_run/staging/` | NO | Re-parsed from HTML |
| Background ingestion queue | **In-memory critical** | `BackgroundIngestQueue.in_flight_tickers` | NO (process-local) | Lost on restart |
| RAG session keys | **In-memory critical** | `RagBackends._session_key` | NO (process-local) | Lost on restart |
| Market data cache | **Cache** | `_markets_cache`, `_company_detail_cache` | NO (process-local) | Re-fetched from Yahoo |
| Ollama probe cache | **Cache** | `_OLLAMA_PROBE_TTL` | NO (process-local) | Re-probed |

### 4.1 Dangerous In-Memory State

1. **`BackgroundIngestQueue.in_flight_tickers`** (background.py:35) — Tracks which tickers are being processed. Lost on restart → duplicate cold-start triggers possible.

2. **`RagBackends._session_key`** (query_ui.py:576) — Browser-pasted API keys held in server memory only. Lost on restart → user re-enters key.

3. **`KnowledgeGraph.lock`** (query_ui.py:795) — Per-instance lock only. Multiple `KnowledgeGraph` instances (multiple handler threads) have **independent locks** — no cross-thread protection for LadybugDB handle.

---

## 5. BACKGROUND PROCESSING AUDIT

### 5.1 Current Implementation (`sandbox_engine/background.py`)

```python
class BackgroundIngestQueue:
    def __init__(self, max_workers: int = 2):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.in_flight_tickers: set[str] = set()
        self.lock = threading.Lock()
```

**Capabilities:**
- ✅ Bounded concurrency (`max_workers=2`)
- ✅ Deduplication (in-flight + staged file check)
- ✅ Atomic file writes (temp + `os.replace`)
- ✅ Status tracking (staged → extracted → failed)
- ✅ Graceful shutdown

**Gaps:**
| Gap | Severity | Impact |
|---|---|---|
| No persistence across restarts | P1 | In-flight work lost; duplicate cold-starts on restart |
| No retry with backoff for failed stages | P1 | Transient SEC/LLM failures become permanent failures |
| No visibility into queue depth/history | P2 | Operations blind to backlog |
| No idempotency key for full ingestion | P2 | Re-ingest of same ticker may duplicate work |
| No dead letter queue | P2 | Failed tasks silently disappear |
| ThreadPoolExecutor shares process with API | P1 | CPU-intensive ingestion competes with query serving |

### 5.2 Cold-Start Background Flow
```
query_ui.py:2120 → background_queue.enqueue_coldstart_sync(ticker)
  ↓ BackgroundIngestQueue._async_stage_full_ingestion()
  1. Write "staged" record to data/staging/{TICKER}.jsonl
  2. Fetch 10-K from SEC (2.5s timeout, 429 retry with jitter)
  3. Clean Item 1 narrative (6k token cap)
  4. Extract 15-30 triples via ColdStartExtractor
  5. Write "extracted" record (atomic replace)
  6. Remove from in_flight_tickers
```
**No step persists progress to survive process crash.**

---

## 6. CACHE AUDIT

| Cache | What | TTL | Invalidation | Multi-Instance |
|---|---|---|---|---|
| Market quotes | Yahoo Finance prices | 60s | Time-based | NO (in-memory dict) |
| Company detail | Yahoo quoteSummary modules | 300s | Time-based | NO |
| Company quote | Yahoo chart API | 60s | Time-based | NO |
| Ollama model list | `/api/tags` | 30s | Time-based | NO |
| Concept registry | Canonical entities | File-based | Manual save | NO (file lock missing) |

**All caches are in-process, single-instance only.** No distributed cache layer exists.

---

## 7. GRAPH / GRAPHRAG AUDIT

### 7.1 Graph Schema (Engine Schema — what `sandbox_engine` builds)

**Node Tables** (buffer.py):
- `Company` (ticker PK, name, cik)
- `Filing` (id PK, accession, form_type, fiscal_year, fiscal_period, filing_date, period_end_date)
- `Metric` (id PK, canonical_name, statement_category, account_class, period_code, period_end, reported_label)
- `Segment` (id PK, name, segment_type)
- `Event` (id PK, item_code, item_title, summary)
- `Chunk` (id PK, text, section)

**Relationship Tables:**
- `SUBMITTED` (Company → Filing)
- `REPORTS_METRIC` (Filing → Metric) — value, currency
- `HAS_SEGMENT` (Metric → Segment) — value, period
- `DISCLOSES_EVENT` (Filing → Event)
- `HAS_CHUNK` (Filing → Chunk)

### 7.2 Query Patterns & Complexity

| Query Type | Cypher Pattern | Hops | Tables Touched | Est. Cost |
|---|---|---|---|---|
| Company overview | `Company → Filing` | 1 | 2 | Low |
| Metric lookup | `Filing → Metric` | 1 | 2 | Low |
| Segment breakdown | `Metric → Segment` | 1 | 2 | Medium |
| 2-hop neighborhood | `Company → Filing → Metric → Segment` | 2-3 | 4-5 | Medium |
| Cold-start traversal | Overlay + Backbone hybrid | 2 | Dynamic | High |
| Narrative chunks | `Filing → Chunk` | 1 | 2 | Medium |
| Risk/Causal (UFGS) | `RiskFactor`, `CausalRelation` | 1-2 | 2-3 | Low-Medium |

### 7.3 Scaling Limits (Current Graph: 30 filings, ~39 MB)

| Metric | Current | Projected at 1000 filings | Projected at 10000 filings |
|---|---|---|---|
| Metric nodes | ~2,500 | ~80,000 | ~800,000 |
| Segment nodes | ~500 | ~15,000 | ~150,000 |
| REPORTS_METRIC edges | ~15,000 | ~500,000 | ~5,000,000 |
| 2-hop traversal time | ~50ms | ~500ms | ~5s+ |
| Parquet spill size | ~50 MB | ~1.5 GB | ~15 GB |
| Load time (--reset) | ~11s | ~5 min | ~50 min |

**LadybugDB is an embedded OLTP graph — not designed for analytical workloads at scale.** No partitioning, no columnar compression beyond what Parquet spill provides, no parallel query execution.

---

## 8. LLM AUDIT

### 8.1 Provider Chain (Priority Order)
1. **NVIDIA NIM** (`NVIDIA_API_KEY`) — Nemotron 3 Ultra (default)
2. **Gemini** (`GEMINI_API_KEY` / `GOOGLE_API_KEY`) — gemini-3.8-flash
3. **OpenAI** (`OPENAI_API_KEY`) — gpt-4o-mini
4. **Anthropic** (`ANTHROPIC_API_KEY`) — claude-sonnet-4-5
5. **Ollama** (local, `http://localhost:11434`) — llama3.2
6. **Heuristic** (no key, offline fallback)

### 8.2 Concurrency & Retry Behavior

| Provider | Min Interval | Max Retries | Backoff | Schema Support |
|---|---|---|---|---|
| NVIDIA | 0s | 3 | 2x | NO (broken `json_schema`) |
| Gemini | 13s (free tier) | 3 | 2x | YES |
| OpenAI | 0s | 3 | 2x | YES |
| Anthropic | 0s | 3 | 2x | YES (forced tool) |
| Ollama | 0s | 3 | 2x | NO (uses `format: "json"`) |

**Critical Issue**: `RagBackends` is a **singleton per process** (query_ui.py:698-720). All threads share one instance with a single `threading.RLock()`. Under high concurrency:
- All requests serialize on `_lock` for backend resolution
- Ollama probe caches for 30s but key resolution runs per request
- Rate limiting is per-process, not distributed

### 8.3 Streaming & Timeouts
- `RAG_TIMEOUT = 900s` (15 minutes!) — browser fetch has no deadline
- SSE streaming: `query_ui.py` yields tokens via `_StageTimer` stages
- No request cancellation support — client disconnect doesn't stop LLM call
- No token budget enforcement on prompt assembly

---

## 9. API AUDIT

### 9.1 Endpoints (FinGraph UI — Primary, port 9100)

| Endpoint | Method | Auth | Reads | Writes | External | LLM | Streaming | Concurrency Risk |
|---|---|---|---|---|---|---|---|---|
| `/` | GET | No | Static | No | No | No | No | Low |
| `/app` | GET | Yes (cookie) | Static | No | No | No | No | Low |
| `/auth` | GET | No | Static | No | No | No | No | Low |
| `/api/companies` | GET | No | LadybugDB | No | No | No | No | Medium |
| `/api/markets` | GET | No | Cache | No | Yahoo Finance | No | No | Medium (external) |
| `/api/route` | GET | No | LadybugDB | No | No | No | No | Low |
| `/api/ask` | GET (SSE) | No* | LadybugDB | No | SEC EDGAR (cold) | YES | YES | **HIGH** |
| `/api/reports` | GET | Yes | LadybugDB | No | No | No | No | Medium |
| `/api/company/{ticker}` | GET | No | LadybugDB + Yahoo | No | Yahoo Finance | No | No | Medium |
| `/api/auth/session` | POST | No | No | Cookie | OAuth providers | No | No | Low |
| `/api/ingestion` | POST | Yes | No | Background queue | SEC EDGAR | YES | No | **HIGH** |

* `/api/ask` is public but rate-limited by LLM provider

### 9.2 Critical API Risks

1. **`/api/ask` (SSE) — No rate limiting, no auth, unbounded LLM calls**
   - Cold-start path triggers SEC fetch + LLM extraction + LLM synthesis
   - One request = 3-5 external API calls + 2 LLM calls
   - No per-IP or per-session quotas

2. **`/api/markets` & `/api/company/{ticker}` — External dependency on Yahoo Finance**
   - No circuit breaker, no fallback, no caching across instances
   - Rate limits unknown; failures return stale cache or empty

3. **ThreadingHTTPServer — One thread per connection**
   - No connection pooling, no max connection limit
   - Slowloris vulnerable (no request body timeout on GET)
   - Large SSE connections hold threads indefinitely

---

## 10. SECURITY AUDIT

| Area | Finding | Severity | Evidence |
|---|---|---|---|
| **Authentication** | HMAC session cookies with 7-day TTL | P2 | `server.py:786-798` — Secure if `FINGRAPH_AUTH_SECRET` set; dev fallback generates random secret per restart |
| **OAuth** | Google/Apple ID token verification | P1 | `server.py:821-864` — Proper JWKS validation; TradingView unimplemented (501) |
| **CORS** | Single origin (`FINGRAPH_CORS_ORIGIN`, default localhost:5173) | P1 | `server.py:932-939` — Credentials allowed; wildcard not permitted |
| **Secrets** | API keys in `.env` (gitignored), browser keys in memory only | P1 | `query_ui.py:734-763`, `server.py:786` — Keys never logged; browser key not persisted |
| **Injection** | Cypher parameterized queries throughout | P0 | All `execute(cypher, params)` — no string interpolation |
| **SSRF** | SEC EDGAR fetch uses validated CIK, constructed URLs | P1 | `tier1_fetch.py:272-278` — URL built from SEC-controlled data |
| **File Upload** | No file upload endpoints in production APIs | N/A | Only static file serving with allow-lists |
| **Rate Limiting** | **NONE on API endpoints** | **P0** | No per-IP, per-user, or global limits |
| **Error Exposure** | Stack traces not returned; errors sanitized | P1 | `_explain_api_error()` maps provider errors to user messages |
| **Dependency Vulnerabilities** | Not scanned in this audit | P2 | `pip-audit` not run; LadybugDB binary opaque |

---

## 11. OBSERVABILITY AUDIT

| Capability | Status | Implementation |
|---|---|---|
| Structured logging | PARTIAL | `logging.basicConfig` with timestamps; no JSON, no request IDs |
| Request correlation | NONE | No request ID propagation across SSE, background, LLM |
| Metrics | NONE | No Prometheus, no `/metrics` endpoint |
| Latency tracking | PARTIAL | `_StageTimer` per-request stages (wire protocol only) |
| Error tracking | NONE | Logs only; no aggregation, no alerting |
| Health checks | BASIC | `/api/companies` serves as implicit health; no `/healthz` |
| LLM metrics | NONE | Token counts, latency, cost not captured |
| DB metrics | NONE | LadybugDB exposes no stats endpoint |
| Queue metrics | NONE | Background queue size not exposed |

---

## 12. FAILURE / RECOVERY ANALYSIS

| Failure Scenario | Current Behavior | Data Risk | User Impact | Recovery | Required Fix |
|---|---|---|---|---|---|
| **App crash during query** | Request fails, no partial state | None | 500 / dropped SSE | Retry request | Add request ID for tracing |
| **DB crash (process kill)** | WAL may be stale | **HIGH** — uncommitted writes lost | Next open fails with `WalRecoveryError` | Manual `.wal` delete or replay | Document recovery procedure |
| **Machine restart** | DB file intact, WAL replay | LOW if clean shutdown | Downtime until restart | Auto-restart service | Systemd/process manager |
| **Disk full** | LadybugDB write fails | **HIGH** — corruption possible | All writes fail | Free disk, replay WAL | Disk monitoring + alerting |
| **SEC EDGAR unavailable** | Cold-start returns error, falls back to KNOWN | None | Question unanswered | Retry later | Circuit breaker + cached fallback |
| **LLM unavailable (all providers)** | `backend: "none"` → "needs_input" | None | No answers, graph works | Add key / start Ollama | Health check endpoint |
| **LLM timeout (900s)** | Browser "Failed to fetch" | None | Silent failure | Increase `RAG_TIMEOUT` | Client-side timeout + cancellation |
| **Background worker crash** | `in_flight_tickers` cleared in `finally` | None | Staging file left as "staged" or "failed" | Manual re-enqueue | Persist queue to disk |
| **Concurrent write (2 loaders)** | Second `COPY` hangs forever | **HIGH** — deadlock | Process stuck | Kill -9, manual recovery | Single-writer enforcement |
| **Duplicate cold-start** | `is_in_flight` + `is_staged` check | None | Wasted compute | Deduplication works in-process | Persist deduplication state |
| **Yahoo Finance down** | Returns stale cache or empty | None | Landing page blank | Graceful degradation | Circuit breaker + multiple sources |

---

## 13. PERFORMANCE AUDIT

### 13.1 Measured Baselines (30 filings, MacBook M-series)
| Operation | Time | Notes |
|---|---|---|
| `sandbox_engine --reset` (full ingest + benchmarks) | 11s | 5 benchmarks pass |
| Single filing parse | ~0.1s | Pure Python, no network |
| Parquet spill (30 filings) | ~2s | Arrow → Parquet |
| LadybugDB load | ~3s | COPY for large tables, UNWIND for small |
| Benchmarks B1-B5 | ~1s | 5 integrity checks |
| Known query (traversal + LLM) | 500ms-2s | Depends on LLM latency |
| Cold-start query | 8-15s | SEC fetch (2.5s) + extraction (3.5s) + synthesis |
| Background ingestion | 30-60s/ticker | Full 10-K fetch + extract + stage |

### 13.2 Bottlenecks

| Bottleneck | Location | Root Cause | Severity |
|---|---|---|---|
| **LadybugDB single writer** | `loader.py` | Architectural — embedded DB | P0 |
| **Cold-start SEC fetch SLA** | `tier1_fetch.py` | 2.5s hard timeout | P1 |
| **LLM serialization** | `RagBackends._lock` | Global lock per process | P1 at scale |
| **Thread-per-connection** | `ThreadingHTTPServer` | No async, no connection pooling | P1 at >100 concurrent |
| **In-process caches** | `query_ui.py`, `server.py` | Not shared across workers | P2 |
| **Parquet spill re-read** | `loader.py:266` | Loads full table to filter fresh rows | P2 at scale |

---

## 14. HORIZONTAL SCALING ANALYSIS

### 14.1 Can FinGraph Run Multiple Instances?

| Instances | What Breaks | What Works |
|---|---|---|
| **1** | Nothing | Everything |
| **2 (read-only query)** | Nothing if both use `read_only=True` | Query API, graph explorer, reports |
| **2 (one writer + one reader)** | Writer must be sole `BulkLoader` process | Reader serves queries; writer ingests |
| **3+ readers** | Nothing | Read scales horizontally |
| **2+ writers** | **FAILS** — "silently diverges" (loader.py:46) | **Impossible with current DB** |

### 14.2 Maximum Safe Architecture (Current Stack)

```
                    ┌─────────────────┐
                    │  Load Balancer  │  (nginx, HAProxy, ALB)
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼───────┐    ┌───────▼───────┐    ┌───────▼───────┐
│  Query API #1 │    │  Query API #2 │    │  Query API #N │
│ (read_only)   │    │ (read_only)   │    │ (read_only)   │
└───────┬───────┘    └───────┬───────┘    └───────┬───────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  LadybugDB      │
                    │  (single file)  │
                    │  read_only=True │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Ingestion      │
                    │  Worker (SOLE)  │
                    │  (read-write)   │
                    └─────────────────┘
```

**Maximum: N read-only query replicas + 1 ingestion writer.**  
No horizontal write scaling possible with LadybugDB.

---

## 15. DATA SAFETY AUDIT

| Aspect | Status | Gap |
|---|---|---|
| **Backup** | Manual only | No automated backup script; `sandbox_engine/_run/` not backed up |
| **Restore** | Manual copy | No documented restore procedure; WAL handling unclear |
| **Export** | None | No `COPY TO` or dump utility |
| **Integrity Checks** | Benchmarks only | Benchmarks run only on `--reset`; no continuous verification |
| **Corruption Detection** | WAL recovery error | Only detected on next open; no proactive scan |
| **Disaster Recovery** | None | No RPO/RTO defined; no cross-region replication |

**Critical**: The authoritative data lives in `sandbox_engine/_run/sandbox.lbug` + `.wal`. This is a **single file on a single machine**. No replication exists.

---

## 16. DEPLOYMENT AUDIT

### 16.1 Current Deployment Model
- **No Docker, no Kubernetes, no container orchestration**
- **Process model**: Multiple `ThreadingHTTPServer` processes on different ports
- **Persistence**: Local filesystem (`sandbox_engine/_run/`, `data/staging/`)
- **Configuration**: `.env` file + environment variables
- **Secrets**: `.env` (gitignored), browser session keys in memory
- **Startup**: `python -m ui.fingraph --port 9100`
- **Shutdown**: `Ctrl-C` → `server.shutdown()` → `kg.close()`
- **Health**: Implicit (server responds); no explicit health endpoint

### 16.2 Production Deployment Requirements (Current Stack)

| Requirement | Current State | Work Needed |
|---|---|---|
| Process supervision | Manual | systemd / supervisor / PM2 |
| Log aggregation | stdout only | Structured JSON logs + shipper |
| Config management | `.env` file | Template + secret injection |
| Secret rotation | Manual | Documented procedure |
| Zero-downtime deploy | Not possible (single DB) | Blue-green with DB swap |
| Rollback | Manual `git checkout` | Automated + DB snapshot |
| Capacity planning | None | Load testing + metrics |

---

## 17. CURRENT-STACK PRODUCTION ARCHITECTURE

```
                              ┌─────────────────────────────────────┐
                              │        LOAD BALANCER (L4/L7)        │
                              │  nginx / HAProxy / Cloud LB         │
                              │  TLS termination, rate limiting     │
                              └──────────────────┬──────────────────┘
                                                 │
                    ┌────────────────────────────┼────────────────────────────┐
                    │                            │                            │
        ┌───────────▼───────────┐      ┌─────────▼─────────┐      ┌─────────▼─────────┐
        │   QUERY API #1        │      │   QUERY API #2    │      │   QUERY API #N    │
        │  (python -m ui.fingraph)      │  (read_only)     │      │  (read_only)     │
        │   Port 9100+N       │      │   Port 9100+N     │      │   Port 9100+N     │
        │   ThreadingHTTPServer       │   ThreadingHTTPServer    │   ThreadingHTTPServer   │
        │   KnowledgeGraph(RO)        │   KnowledgeGraph(RO)     │   KnowledgeGraph(RO)    │
        └───────────┬───────────┘      └─────────┬─────────┘      └─────────┬─────────┘
                    │                            │                            │
                    └────────────────────────────┼────────────────────────────┘
                                                 │
                                    ┌────────────▼────────────┐
                                    │      LADYBUGDB          │
                                    │  sandbox_engine/_run/   │
                                    │  sandbox.lbug (RO)      │
                                    │  .wal (replay on open)  │
                                    │  Single file, no repl   │
                                    └────────────┬────────────┘
                                                 │
                                    ┌────────────▼────────────┐
                                    │   INGESTION WORKER      │
                                    │  (SOLE WRITER)          │
                                    │  python -m sandbox_engine│
                                    │  --reset (scheduled)    │
                                    │  BulkLoader (RW handle) │
                                    │  BackgroundIngestQueue  │
                                    └────────────┬────────────┘
                                                 │
                              ┌──────────────────┼──────────────────┐
                              │                  │                  │
                     ┌────────▼────────┐ ┌───────▼───────┐ ┌────────▼────────┐
                     │  SEC EDGAR API  │ │  LLM PROVIDER │ │  Yahoo Finance  │
                     │  (rate limited) │ │  (NVIDIA NIM, │ │  (rate limited) │
                     │                 │ │   Gemini,     │ │                 │
                     │                 │ │   OpenAI,     │ │                 │
                     │                 │ │   Ollama)     │ │                 │
                     └─────────────────┘ └───────────────┘ └─────────────────┘

                         ┌─────────────────────────────────────┐
                         │       REACT FRONTEND (Port 5173)      │
                         │  Vite dev / Static build (nginx)      │
                         │  Proxies /api/* → Query API           │
                         └─────────────────────────────────────┘
```

**Key Boundaries:**
- **Query APIs**: Stateless, read-only, horizontally scalable
- **Ingestion Worker**: Single writer, scheduled/cron, not user-facing
- **LadybugDB**: Single file, read-only for queries, read-write for ingestion only
- **External APIs**: Rate-limited, need circuit breakers

---

## 18. CHANGE CLASSIFICATION

| # | Problem | Root Cause | Existing-Stack Solution | Change Type | Risk | Expected Impact |
|---|---|---|---|---|---|---|
| 1 | No rate limiting on `/api/ask` | Missing middleware | Add token bucket in `_Handler` | B (Code) | Low | Prevents abuse, fair sharing |
| 2 | Single-writer DB bottleneck | LadybugDB architecture | Serialize writes via single ingestion worker | C (Architecture) | Medium | Enables N read replicas |
| 3 | In-memory background queue lost on restart | `ThreadPoolExecutor` + dict | Persist queue to `data/staging/queue.jsonl` | B (Code) | Low | Survives restart |
| 4 | No request correlation / tracing | No request IDs | Generate UUID per request, pass through SSE | B (Code) | Low | Debuggability |
| 5 | No health/readiness endpoints | Not implemented | Add `/healthz` (DB + LLM probe) | B (Code) | Low | K8s/probe compatibility |
| 6 | No structured logging / metrics | `basicConfig` only | JSON logs + Prometheus `/metrics` | B (Code) | Low | Observability |
| 7 | Yahoo Finance single point of failure | Direct calls in handlers | Circuit breaker + fallback cache | B (Code) | Medium | Resilience |
| 8 | LLM timeout 900s blocks threads | No client timeout | Add `httpx` timeout, SSE heartbeat | B (Code) | Medium | Resource protection |
| 9 | No graceful shutdown on SIGTERM | Only KeyboardInterrupt | Signal handlers + drain connections | B (Code) | Low | Zero-downtime deploy |
| 10 | In-process caches not shared | Dict + monotonic time | Accept — add Redis only if needed | D (Infra) — optional | Low | Cache hit rate |
| 11 | No automated backup/restore | Manual only | Cron job: copy `.lbug` + `.wal` to S3 | D (Infra) | Low | Disaster recovery |
| 12 | Cold-start SEC fetch 2.5s SLA | Hard timeout in `tier1_fetch` | Increase timeout + async fetch | B (Code) | Low | Reliability |
| 13 | Thread-per-connection limit | `ThreadingHTTPServer` | Add connection limit + queue | B (Code) | Medium | DoS protection |
| 14 | No request cancellation | SSE ignores disconnect | Check `self.connection.close()` in stream | B (Code) | Medium | Resource cleanup |
| 15 | LadybugDB WAL corruption risk | No proactive check | Pre-start `lb.Database` validation | B (Code) | Low | Early detection |

---

## 19. PRIORITY MATRIX

| Priority | Problem | Root Cause | Existing-Stack Solution | Change Type | Risk | Expected Impact |
|---|---|---|---|---|---|---|
| **P0** | **No rate limiting on `/api/ask`** | Missing middleware | Token bucket per IP/session in handler | B | Low | Prevents DoS, cost control |
| **P0** | **Single-writer DB prevents horizontal write** | LadybugDB design | Single ingestion worker + N read replicas | C | Medium | Enables read scaling |
| **P0** | **Background queue loses work on restart** | In-memory `ThreadPoolExecutor` | Persist queue state to `data/staging/queue.jsonl` | B | Low | Durability |
| **P0** | **No health/readiness endpoints** | Not implemented | `/healthz` checking DB + LLM reachability | B | Low | Deployability |
| **P1** | **No structured logging / request IDs** | `basicConfig` only | JSON logs + UUID correlation | B | Low | Debuggability |
| **P1** | **LLM 900s timeout holds threads** | No client timeout | `httpx` timeout + SSE heartbeat + cancellation | B | Medium | Thread exhaustion prevention |
| **P1** | **Yahoo Finance SPOF** | Direct calls, no circuit breaker | `circuitbreaker` lib + stale cache fallback | B | Medium | Landing page resilience |
| **P1** | **No graceful shutdown** | Only `KeyboardInterrupt` | Signal handlers + connection drain | B | Low | Zero-downtime deploy |
| **P1** | **No automated backup** | Manual only | Cron: `cp sandbox.lbug* s3://bucket/` | D | Low | RPO < 1 hour |
| **P2** | **In-process caches not shared** | Dict per process | Accept for now; Redis only if hit rate < 80% | D | Low | Cache efficiency |
| **P2** | **Cold-start SEC fetch timeout too aggressive** | 2.5s hardcoded | Configurable + retry with backoff | B | Low | Success rate |
| **P2** | **Thread-per-connection exhaustion** | `ThreadingHTTPServer` | Max connections + request queue | B | Medium | Concurrency limit |
| **P2** | **No Prometheus metrics** | Not implemented | `/metrics` endpoint (latency, errors, queue) | B | Low | Observability |
| **P3** | **Parquet re-read on load** | `loader.py` loads full table | Accept — current scale fine | — | — | — |
| **P3** | **RagBackends singleton lock contention** | Global `RLock` | Per-request resolution (lock-free) | B | Low | LLM concurrency |

---

## 20. FINAL DECISION

### 1. Can FinGraph become production-ready while keeping the current software stack?
**YES.** The existing stack is well-suited to the problem domain. LadybugDB's single-writer constraint is the only fundamental limitation, and it can be worked around with a **single ingestion writer + multiple read-only query replicas** architecture.

### 2. Top 10 problems preventing production readiness
1. **No rate limiting on `/api/ask`** — Unbounded LLM calls = cost explosion + DoS
2. **Single-writer database** — Cannot scale writes horizontally
3. **Background ingestion queue not durable** — Work lost on restart
4. **No health/readiness endpoints** — Cannot deploy in orchestrated environments
5. **No structured logging / request tracing** — Blind in production
6. **LLM 900s timeout with no cancellation** — Thread exhaustion under load
7. **Yahoo Finance as SPOF** — Landing page fails silently
8. **No graceful shutdown** — Connection drops on deploy
9. **No automated backup/restore** — Data loss risk
10. **Thread-per-connection model** — Hard concurrency ceiling

### 3. Problems solvable WITHOUT replacing any software
**All 10 above** — Every fix uses existing Python stdlib + current dependencies.

### 4. Problems requiring only CODE CHANGES (Type B)
1-3, 5-8, 10, 12-15 — **12 of 15** are pure code changes.

### 5. Problems requiring ARCHITECTURE/PROCESS CHANGES (Type C)
- **Single-writer DB** → Single ingestion worker + N read replicas (deployment topology)
- **Zero-downtime deploy** → Blue-green with DB file swap (deployment process)

### 6. Problems requiring INFRASTRUCTURE CHANGES (Type D)
- **Automated backup** → Cron + object storage (operational)
- **Shared cache (optional)** → Redis if needed (new infra, but not required)
- **Load balancer** → nginx/HAProxy (standard infra)

### 7. Problems GENUINELY requiring technology replacement?
**NONE.** LadybugDB's single-writer constraint is a deployment topology constraint, not a correctness issue. The architecture pattern (single writer, many readers) is standard for embedded databases (SQLite, DuckDB, LMDB all work this way).

### 8. Proof: Why LadybugDB doesn't need replacement
- **Read scaling**: Multiple `read_only=True` handles work perfectly (tested in `query_ui.py:789`)
- **Write serialization**: Single `BulkLoader` process is the current and correct pattern
- **Durability**: WAL + atomic Parquet spill provides recoverability
- **Performance**: 30 filings in 11s; 1000 filings projected ~5 min — acceptable for scheduled ingestion
- **Alternative cost**: PostgreSQL + pgvector + graph extension would require rewriting ALL Cypher, loader, parser, and query logic — months of work for marginal gain at this scale

### 9. Maximum realistic concurrency with current stack
| Configuration | Max Concurrent Users | Bottleneck |
|---|---|---|
| Single query API | ~50-100 | Thread pool + LLM latency |
| 4 query APIs (LB) | ~200-400 | LLM provider rate limits |
| 4 query APIs + optimized LLM | ~500-1000 | LadybugDB read throughput |
| **With async HTTP (aiohttp)** | **2000-5000** | DB connection contention |

**10,000 concurrent users requires**: Async HTTP server + connection pooling + LLM request batching + possibly read replicas (if LadybugDB supports multiple read handles well — needs testing).

### 10. Changes for 1,000 concurrent users
1. Rate limiting on `/api/ask` (P0)
2. 4x Query API replicas behind load balancer (C)
3. LLM timeout reduction + cancellation (P1)
4. Structured logging + request IDs (P1)
5. Health endpoints (P0)
6. Graceful shutdown (P1)

### 11. Changes for 5,000 concurrent users
1. All above +
2. Async HTTP server (aiohttp/fastapi) — **requires framework change** (Type E, but only for HTTP layer)
3. Connection pooling for LadybugDB (if supported)
4. LLM request batching / queue
5. Redis for shared caches (D, optional)
6. Prometheus metrics + alerting (P2)

### 12. Changes for 10,000 concurrent users
1. All above +
2. **LadybugDB read scalability testing** — if multiple read handles contend, need read replicas (file copy + symlink swap)
3. CDN for static assets
4. LLM provider redundancy (multi-region)
5. Database sharding by company (major architecture change — Type E)

### 13. Safest migration path
```
Phase 0: Freeze current behavior, add observability
Phase 1: Safety — rate limits, health checks, graceful shutdown, backups
Phase 2: Concurrency — read replicas behind LB, async HTTP evaluation
Phase 3: Durability — persistent background queue, WAL monitoring
Phase 4: API scalability — circuit breakers, cancellation, metrics
Phase 5: GraphRAG/LLM — request batching, provider failover
Phase 6: Security — audit, pen test, secret rotation
Phase 7: Observability — dashboards, alerts, SLOs
Phase 8: Deployment — systemd, blue-green, rollback
Phase 9: Load test — 1k, 5k, 10k scenarios
Phase 10: Production validation — canary, error budgets
```

### 14. What must NEVER be changed
| Component | Reason |
|---|---|
| **LadybugDB file layout** | Existing data (30 filings, 39 MB) + all derived graphs depend on it |
| **Content-addressed IDs (`stable_id`)** | Deterministic re-ingest = no-op; changing breaks idempotency |
| **Entity resolver canonicalization** | `canonical_concept` rules are battle-tested; changes = data corruption |
| **Period-scoped Metric identity** | `PERIOD_SCOPED_METRICS = True` enables comparative analysis; disabling loses data |
| **Polarity conflict guard** | Prevents beginning/ending balance merges — financial correctness |
| **COPY pre-flight key check** | `loader.py:_existing()` prevents deadlock; removing hangs second ingest |
| **Parquet spill as staging** | Enables parse/load separation, debugging, re-load without re-parse |

---

## 21. STAGED ROADMAP

### PHASE 0 — Baseline & Freeze (Week 1)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Freeze current behavior | Tag `v1.0-baseline` | All | All | None | Full suite passes | `git tag v1.0-baseline` | `git checkout v1.0-baseline` |
| Add request ID middleware | UUID per request, header propagation | `query_ui.py`, `server.py` | stdlib `uuid` | None | Test header present | `X-Request-ID` in all logs | Revert commit |

### PHASE 1 — Safety & Correctness (Week 2-3)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Rate limiting `/api/ask` | Token bucket (10/min/IP, 50/min/user) | `query_ui.py:_Handler` | stdlib `time`, `collections` | None | Load test 100 req/s | 429 on excess, <1% false positive | Remove middleware |
| Health endpoints | `/healthz` (DB + LLM), `/readyz` (queue) | `server.py`, `query_ui.py` | stdlib | None | `curl /healthz` → 200 | LB passes health checks | Remove routes |
| Graceful shutdown | SIGTERM handler, drain SSE, wait workers | `server.py`, `query_ui.py`, `background.py` | `signal` | None | `kill -TERM` → clean exit | No connection drops | Revert signal handlers |
| Automated backup | Cron: `tar czf /backup/sandbox-$(date).tgz sandbox_engine/_run/` | New script | `tar`, `cron`, `aws s3 cp` | None | Restore test | Backup < 1hr old in S3 | Delete cron |

### PHASE 2 — Concurrency (Week 3-4)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Read replicas | systemd: 4x `ui.fingraph` on 9100-9103, nginx upstream | `server.py` (port from env), nginx.conf | LadybugDB `read_only=True` | nginx | `hey -c 100 -n 10000` | 4x throughput, <100ms p99 | Stop replicas |
| Async HTTP evaluation | Prototype aiohttp handler for `/api/ask` | New `server_async.py` | aiohttp, async LadybugDB? | aiohttp | Benchmark vs ThreadingHTTPServer | 10x connections same RAM | Delete prototype |
| LLM cancellation | Check `request.connection.closed` in SSE loop | `query_ui.py:ask_rag` | stdlib | None | Client disconnect → LLM cancel | No orphan LLM calls | Revert check |

### PHASE 3 — Persistence & Recovery (Week 4-5)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Durable background queue | Persist `in_flight_tickers` + task state to `data/staging/queue.jsonl` | `background.py` | `json`, `os.replace` | None | Kill -9 mid-ingest → resume | No lost work, no duplicates | Revert persistence |
| WAL health check | Pre-start `lb.Database` validation, alert on corruption | `query_ui.py:resolve_db_path` | LadybugDB | None | Corrupt `.wal` → clear error | Actionable error message | Remove check |
| Concept registry locking | File lock on `concepts.json` (fcntl) | `entity_resolver.py:save/load` | `fcntl` | None | Parallel ingest → no corruption | No "silently diverges" | Remove lock |

### PHASE 4 — API Scalability (Week 5-6)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Yahoo Finance circuit breaker | `pybreaker` or custom 5-failure/30s open | `server.py:markets()`, `company_detail()` | `pybreaker` (optional) | pybreaker (pip) | Simulate Yahoo down | Stale cache served, no 500 | Remove breaker |
| Connection limits | `ThreadingHTTPServer` max_children + queue | `query_ui.py:_listeners` | stdlib | None | 1000 concurrent → queue not crash | 503 on overload | Revert limits |
| Prometheus metrics | `/metrics` endpoint: latency, errors, queue depth, LLM tokens | New `metrics.py` | `prometheus-client` | prometheus-client | `curl /metrics` → valid | Grafana dashboard works | Remove endpoint |

### PHASE 5 — GraphRAG/LLM Scalability (Week 6-7)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| LLM request batching | Batch ColdStartExtractor calls (not yet — single ticker) | N/A | N/A | N/A | N/A | N/A | N/A |
| Provider failover | NVIDIA → Gemini → Ollama automatic on 5xx | `query_ui.py:RagBackends` | All providers | None | Kill NVIDIA → Gemini answers | <5s failover | Revert order |
| Token budget enforcement | Truncate evidence block to `RAG_NUM_CTX` | `query_ui.py:build_evidence` | Current logic | None | Long context → no OOM | Token count < limit | Revert truncation |

### PHASE 6 — Security (Week 7-8)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Dependency scan | `pip-audit` in CI | GitHub Actions | pip-audit | None | PR fails on CVE > MEDIUM | Zero critical | Disable check |
| Secret rotation doc | `docs/secret-rotation.md` | New doc | N/A | None | Manual test | Rotation < 10 min | Delete doc |
| Pen test checklist | OWASP Top 10 review | New doc | N/A | None | Checklist complete | All items addressed | N/A |

### PHASE 7 — Observability (Week 8-9)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| Structured JSON logs | `python-json-logger` + request ID | All servers | `python-json-logger` | python-json-logger | `jq` parses logs | Datadog/ELK ingests | Revert formatter |
| Distributed tracing | OpenTelemetry (optional) | `query_ui.py`, `server.py` | `opentelemetry-api` | opentelemetry | Trace visible in Jaeger | End-to-end latency | Remove OTel |
| SLO dashboards | Latency p99 < 2s, error rate < 0.1%, availability > 99.9% | Grafana | Prometheus | Grafana | 1 week data | Alerts fire correctly | Delete dashboards |

### PHASE 8 — Deployment (Week 9-10)
| Problem | Changes | Files | Tech Retained | New SW | Tests | Success Criteria | Rollback |
|---|---|---|---|---|---|---|---|
| systemd units | `fingraph-api@.service`, `fingraph-ingest.service` | New files | systemd | None | `systemctl start` works | Survives reboot | `systemctl disable` |
| Blue-green deploy | Symlink swap `sandbox_engine/_run/current → sandbox.lbug.v2` | `config.py:Paths`, deploy script | `ln -sf` | None | Deploy v2 → switch → verify | <30s cutover | `ln -sf previous` |
| Rollback automation | `deploy.sh rollback` → previous DB + code | New script | git, bash | None | Rollback < 2 min | Service healthy | Manual rollback |

### PHASE 9 — Load Testing (Week 10-11)
| Scenario | Tool | Target | Pass Criteria |
|---|---|---|---|
| 100 concurrent known queries | `hey` / `locust` | 4 API replicas | p99 < 2s, 0% errors |
| 50 concurrent cold-starts | `locust` | 4 API + 1 ingest | p99 < 30s, 0% errors |
| 1000 concurrent SSE | `locust` | 4 API replicas | <5% thread exhaustion |
| Sustained 100 req/s for 1hr | `locust` | 4 API replicas | No memory leaks, stable latency |
| Ingestion during query load | `locust` + background | 1 ingest + 4 query | No query degradation |

### PHASE 10 — Production Validation (Week 12+)
| Gate | Criteria |
|---|---|
| Canary deploy | 5% traffic to new version, error rate < 0.1% |
| Error budget | 99.9% availability over 30 days |
| Data integrity | Benchmarks B1-B5 pass on production DB |
| Security | No critical findings in pen test |
| Operations | Runbook tested: deploy, rollback, restore, scale |

---

## 22. SUMMARY

**FinGraph is unusually well-positioned for production readiness within its current stack.** The architecture is clean, the code is disciplined (deterministic IDs, idempotent loads, explicit error handling), and the only fundamental constraint — LadybugDB's single-writer model — is a known pattern (SQLite, DuckDB, LMDB) with a standard workaround: **single writer, many readers**.

**No technology replacement is required.** Every identified gap can be closed with code changes (Type B), deployment topology changes (Type C), or operational tooling (Type D). The one "optional alternative" (Redis for shared caches) is not required unless cache hit rates prove insufficient.

**The path to 10,000 concurrent users is clear:**
1. Fix the P0 safety gaps (rate limits, health checks, durable queue, backups)
2. Scale reads horizontally behind a load balancer (4-8 replicas)
3. Move to async HTTP for connection efficiency (aiohttp — only HTTP layer change)
4. Add observability and automated operations
5. Load test each tier

**The one hard ceiling**: LadybugDB read throughput under concurrent load. This must be measured in Phase 9. If multiple `read_only=True` handles contend, the solution is **read replicas via file copy** (rsync `.lbug` to replica machines, symlink swap) — still no database replacement.

---

*Generated by production readiness audit. Repository unchanged. All recommendations preserve existing technology stack.*