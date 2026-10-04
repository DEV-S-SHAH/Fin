# Phase 0 Baseline — FinGraph

Frozen behaviour reference for the production-hardening roadmap
(`FIN_PRODUCTION_AUDIT.md` §21, Phase 0). No functional code was changed to
produce this document. Measurements only.

Captured: 2026-10-03, macOS (darwin) arm64, Python 3.13.15 in `.venv`.

> **Baseline verdict: the repository is NOT clean, so `v1.0-baseline` was NOT
> created.** See §10. A tag on the current HEAD would describe
> `d7c9c6e4`, which is *not* the code or data that is actually running.

---

## 1. Git baseline

| Field | Value |
|---|---|
| Branch | `main` |
| HEAD | `d7c9c6e4c5deb0572612e121002cceeb3660f71c` |
| Upstream | `origin/main` (`https://github.com/DEV-S-SHAH/Fin.git`) |
| Ahead / behind | `0 / 0` — in sync |
| Tags | none (0) |
| Stashes | none |
| **Working tree** | **DIRTY — 133 porcelain entries** |

Uncommitted work (must not be discarded):

| Status | Count | Examples |
|---|---|---|
| Modified (M) | 14 | `sandbox_engine/query_ui.py`, `sandbox_engine/router.py`, `sandbox_engine/buffer.py`, `sandbox_engine/ingestion.py`, `sandbox_engine/parser.py`, `graphrag/llm.py`, `document_loader.py`, `tools/sec_fetch.py`, `tools/sec_ingest.py`, `README.md`, `.gitignore`, `ui/fingraph/studio/graph.js`, `ui/fingraph/studio/styles.css`, `benchmarks/results/.checkpoint_50.json` |
| Deleted (D) | 72 | all of `web/**` (23,310 lines), all of `sandbox_engine/ui_next/**`, `graphrag/web.py`, `graphrag/static/index.html`, all 4 `samples/*.pdf`, 2 `RESEARCH_REPORT*.md` |
| Untracked (dirs) | 47 | `ingestion/`, `ui/fingraph/**`, `.github/workflows/ci.yml`, `FIN_PRODUCTION_AUDIT.md`, `docs/LOAD_BALANCING_SCALING_RESEARCH.md`, 3 new test files |
| Untracked (files, `-uall`) | 90 | |

The deletions are **partly a directory move in progress**
(`sandbox_engine/ui_next/` → `ui/fingraph/`) but not a clean one: content differs
(`auth/styles.css` md5 `5763fe9…` at HEAD vs `dc2cc19…` in `ui/fingraph/`) and
`web/` is gone outright with nothing at `web/` on disk.

---

## 2. Test baseline

Suite discovered: 30 files — 24 in `tests/`, 6 in `sandbox_engine/`. There is no
`pytest.ini` / `pyproject.toml` / `setup.cfg` / `tox.ini`, so collection order
and rootdir are implicit. CI (`.github/workflows/ci.yml`, untracked) runs
`pytest tests/ -q` only — it excludes `sandbox_engine/test_*.py`.

Command used:

```
.venv/bin/python -m pytest tests/ sandbox_engine/ -q \
  --ignore=tests/test_ui_next_auth.py --ignore=tests/test_ui_next_server.py \
  -W always -p no:randomly
```

| Metric | Value |
|---|---|
| Collected | 982 |
| Passed | **946** |
| Failed | **11** |
| Errors | **22** (setup/teardown) |
| Skipped | **5** |
| Subtests passed | 187 |
| Warnings | **153** |
| Wall time | **35.99 s** (`real 36.29 s`) |

### 2.1 Pre-existing failures — all 33 accounted for

None is a latent data-corruption bug. Every one is caused by the uncommitted
deletions, or by test drift against uncommitted production code.

**A. Deleted module / fixture (24)** — working tree is mid-refactor:

| Cause | Tests | Error |
|---|---|---|
| `sandbox_engine/ui_next/` deleted | 2 collection errors (`test_ui_next_auth.py`, `test_ui_next_server.py`) | `ModuleNotFoundError: No module named 'sandbox_engine.ui_next'` |
| `graphrag/web.py` deleted | 22 errors (`test_graphrag.py::WebUITests::*`) | `ImportError` |
| `samples/*.pdf` deleted (4 files) | 4 failures + 2 subtest failures | `PDFExtractionError: PDF not found: samples/…` |

**B. Test drift vs. uncommitted production code (9):**

- `test_query_ui_transport.py::AskTransportTests` (2) — the stub is
  `ok(kg, question)`, but uncommitted `sandbox_engine/query_ui.py:4798` now
  calls `ask_rag(self.kg, question, fiscal_year=…, fiscal_quarter=…,
  form_type=…)`. `TypeError` → 500. The test signature is stale, not the server.
- `test_graphrag.py::HeuristicTests::test_reports_itself_as_non_model` (1) — the
  uncommitted `graphrag/llm.py` diff flips `HeuristicClient.is_model` from
  `False` to `True`. The test asserts it is `False`.
- 4 remaining `EndToEndTests` failures (`test_failed_chunk_does_not_abort_ingestion`,
  `test_hallucinated_citation_is_flagged`,
  `test_question_outside_graph_is_reported_not_hallucinated`,
  `test_reingesting_is_idempotent`) and
  `test_extracts_text_from_fixture`,
  `test_padded_run_is_rejected_by_grounding` — all trace to
  `PDF not found: samples/marine_biology.pdf`.

### 2.2 Warnings

153 total, dominated by two benign classes: `XMLParsedAsHTMLWarning`
(`sandbox_engine/parser.py:194`, 24+ from `test_temporal_integrity.py`) and
`DeprecationWarning: datetime.utcnow()`
(`ingestion/orchestrator.py:153,872`).

### 2.3 Database safety of the test run

Tests use `tempfile.mkdtemp()`; the only reference to the authoritative DB is a
read-then-skip at `tests/test_router.py:158`. Verified:

| File | MD5 before | MD5 after |
|---|---|---|
| `sandbox_engine/_run/sandbox.lbug` | `4204aafc129b58cbb97d295c5dcfb20e` | identical |
| `data/aapl-2026.lbug` | `86480d038556205f51f8ec3f1982b858` | identical |

No `.lbug.wal` / `.lbug.tmp` was created.

---

## 3. Application baseline

| Role | Command | Port | Status at capture |
|---|---|---|---|
| FinGraph UI (primary) | `python -m ui.fingraph --port 9100 --no-browser` | 9100 `$PORT_QUERY_UI_V2` | running, PID 85669, 30 min, RSS 86 MB |
| GraphRAG Workspace (React/Vite) | `cd web && npm run dev` | 5173 | running, PID 33085/33126, 6 h — **serving 404** (§3.1) |
| Legacy GraphRAG Explorer | `python -m ui.legacy_graphrag` | 9000 `$PORT_QUERY_UI` | not running |
| GraphRAG web UI | `python -m ui.graphrag_web` | 8765 `$PORT_GRAPHRAG_UI` | not running |

Runtime: `.venv/bin/python` = **Python 3.13.15** (system `python3` is 3.9.6 and
is not the project interpreter). Env: `.env` sets only `NVIDIA_API_KEY` and
`RAG_BACKEND=auto`.

Port precedence: `--port` > env var > default. LadybugDB takes an exclusive lock
on a read-write handle, so a read-write open makes a second server fail to start;
the UI therefore defaults to `read_only=True`
(`sandbox_engine/query_ui.py:789`, `ui/fingraph/server.py:1208`).

### 3.1 The running 9100 instance is serving a stale database

`GET /api/stats` on 9100 returns `nodes: 0, edges: 0, companies: []`. The
authoritative DB holds 122,158 nodes. Cause, confirmed with `lsof`:

| | PID 85669 (port 9100) | fresh instance (port 9200) |
|---|---|---|
| inode of open `sandbox.lbug` | **14431241, 16,384 bytes** | 14433772, 57,118,720 bytes |
| `/api/stats` | `0 / 0`, `schema: "blueprint"` | `48,938 / 57,593`, `schema: "engine"` |

`sandbox.lbug` has mtime `2026-10-03 18:01:32`; PID 85669 started
`17:53:38`. The file was **replaced (new inode) ~8 minutes after that server
opened it**, so the server holds an orphaned 16 KB inode and silently reports an
empty graph with `HTTP 200`.

This is a real correctness hazard: a rebuild under a live reader produces no
error, no log line, and no non-200 — only silently wrong answers. It is the
strongest Phase 1 candidate and needs no new dependency.

---

## 4. API baseline

Measured on a fresh instance of the current working tree (port 9200, current
code + current DB), 5 sequential runs each.

### 4.1 Endpoint surface

| Endpoint | Code | p50 latency | Notes |
|---|---|---|---|
| `GET /` | 200 | 0.6 ms | landing |
| `GET /app` | 200 | 1.0 ms | studio |
| `GET /auth` | 200 | 0.8 ms | |
| `GET /animation` | 200 | 0.8 ms | |
| `GET /company`, `/pricing` | 302 | 0.5 ms | redirect |
| `GET /api/stats` | 200 | 7.5 ms | |
| `GET /api/companies` | 200 | 2.0 ms | returns 4 companies with filings/periods |
| `GET /api/entities` | 200 | 19 ms | |
| `GET /api/graph` | 200 | 54 ms | 62,887 bytes |
| `GET /api/rag` | 200 | 0.6 ms | backend = `nvidia` |
| `GET /api/markets` | 200 | 608 ms | Yahoo Finance, 60 s TTL cache |
| `GET /api/company/AAPL` | 200 | **17,486 ms** | see §4.3 |
| `GET /api/auth/session` | 200 | 0.7 ms | unauthenticated |
| `GET /api/route?q=AAPL` | 200 | 0.7 ms | `{"route":"KNOWN","ticker":"AAPL"}` |
| `GET /api/reports` | 401 | **hangs to client timeout** | see §4.2 |
| `GET /api/ingestion` | 401 | **hangs to client timeout** | see §4.2 |
| `GET /healthz`, `/readyz`, `/health` | 404 | — | **do not exist** |
| `POST /api/ask` | 200 | 6.5–28.1 s | §6 |
| `/vendor/` | 404 | — | |

There is **no HTTP ingestion endpoint**. `/api/ingestion` appears only as an
auth gate (`ui/fingraph/server.py:1117,1157`); no handler implements it.
Ingestion is CLI-only: `python -m sandbox_engine --reset`,
`ingestion/cli.py`, `tools/sec_ingest.py`, `tools/drain_staging.py`.

### 4.2 Pre-existing bug: protected endpoints hang the client

`GET /api/reports` and `GET /api/ingestion` return `401` **with no
`Content-Length` header**, so on HTTP/1.1 keep-alive the client cannot delimit
the body and blocks until its own timeout (observed: exactly 90.0 s with
`curl -m 90`, 60.0 s with `-m 60`, 6.0 s with `-m 6`).

Verified header diff — 401:

```
HTTP/1.1 401 Unauthorized
Content-Type: application/json; charset=utf-8
Access-Control-Allow-Origin: http://localhost:5173
(… no Content-Length …)
```

versus a working 200:

```
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 753
```

Source: `ui/fingraph/server.py:1133-1146` (`_require_auth`) omits the header
that `_json()` sets. Affects every auth-gated route. Stdlib-only fix.

### 4.3 Pre-existing bug: `/api/company/<T>` always reports `in_graph: false`

`ui/fingraph/server.py:737` does `from ..query_ui import …`, which resolves to
`ui.query_ui` — a module that does not exist. The `ModuleNotFoundError` is
swallowed by a bare `except Exception:` at line 754, so `graph_info` is always
`{"in_graph": false}` and graph filings are never attached, for every ticker.

`GET /api/company/AAPL` returns `in_graph: false` while `GET /api/companies`
lists AAPL with 10 filings — a direct contradiction. Correct import is
`sandbox_engine.query_ui`, as used elsewhere in the same file.

The same endpoint also degrades silently on upstream failure: every Yahoo field
(`quote`, `fundamentals`, `technicals`, `chart_1mo/3mo/1y`, `news`) is null/empty
after ~17.5 s of timeouts, yet the response is `HTTP 200` with no error signal.
(Secondary: this path opens a brand-new `KnowledgeGraph` per request instead of
reusing the shared handle.)

---

## 5. Database baseline

Authoritative store: **LadybugDB 0.20.4**, embedded, single file.

| Path | Size | Role |
|---|---|---|
| `sandbox_engine/_run/sandbox.lbug` | **57,118,720 B (54.5 MiB)** | **authoritative** — resolved by `_DB_CANDIDATES` (`query_ui.py:169`), the only candidate |
| `data/aapl-2026.lbug` | 3,588,096 B (3.4 MiB) | legacy PDF GraphRAG graph |
| `sandbox_engine/_run/staging/*.parquet` | 12 MB, 50 files | Parquet staging |

WAL: **no `.lbug.wal` and no `.lbug.tmp` present** — clean checkpoint, nothing
unflushed at capture. Neither DB file is tracked by git (`data/` and `*.lbug` are
gitignored), so **the graph has no version-control recovery path at all.**

Schema as detected by the app: **37 node tables, 50 rel tables; 87 tables in the
catalog.** The DB is in **engine** schema (`Metric`, `Chunk`, `Event`, …), not
blueprint (`FinancialMetric`, `DocumentChunk`, …);
`detect_schema()` (`query_ui.py:315`) probes and rewrites.

### 5.1 Graph size (read-only handle, `read_only=True`)

| | Count |
|---|---|
| **Total nodes (all populated node tables)** | **122,158** |
| **Total edges (all populated rel tables)** | **189,723** |

Top node tables: `RawFact` 70,591 · `Chunk` 27,748 · `Metric` 20,519 ·
`Footnote` 984 · `Section` 533 · `Event`/`RiskFactor` 396 · `CorporateEvent` 394 ·
`Filing` 236 · `FiscalQuarter` 64 · `FiscalPeriod` 62 · `Company` 4.

Top edges: `REPORTED_IN` 73,808 · `DISCLOSED_IN` 46,980 · `HAS_CHUNK` 27,748 ·
`REPORTS_METRIC` 28,143 · `NORMALIZES_TO` 8,650 · `BROKEN_DOWN_BY` 633 ·
`HAS_SEGMENT` 1,070.

Companies (4): `AAPL` (CIK 0000320193), `MSFT` (0000789019),
`NVDA` (0001045810), `TSLA` (0001318605).
Filings (236): 173 × 8-K, 47 × 10-Q, 16 × 10-K.

`GET /api/stats` reports a **subset** — `48,938` nodes / `57,593` edges — because
`stats()` (`query_ui.py:830`) only counts 6 hardcoded node and 5 rel tables.

> The audit's §13.1 baseline ("30 filings, ~39 MB") is **stale**. The real graph
> is 236 filings and 54.5 MiB. Phase 1+ capacity reasoning must use the numbers
> above, not the audit's.

### 5.2 Read integrity

Read-only open and traversal both succeed; see §6.2. MD5 unchanged before and
after the whole test run and both API baselines.

---

## 6. Performance baseline

### 6.1 Query path (`POST /api/ask`, NVIDIA backend, model `nvidia/nemotron-3-ultra-550b-a55b`)

Server-reported `stage_latencies_ms`:

| Query | routing | traversal | **synthesis (LLM)** | total |
|---|---|---|---|---|
| AAPL FY2026 net sales | 0.28 ms | 91.6 ms | 27,686 ms | **28,058 ms** |
| MSFT FY2026 revenue #1 | — | 140.8 ms | 6,272 ms | **6,491 ms** |
| MSFT FY2026 revenue #2 | — | 138.9 ms | 7,112 ms | **7,354 ms** |
| MSFT FY2026 revenue #3 | — | 121.7 ms | 12,966 ms | **13,191 ms** |

Answer quality: grounded, cited (`[E13] [E14] [E15]`), and it correctly refuses
to fabricate an unavailable FY total (`ungrounded: []`, `violations: []`).
Returned subgraph: 65 nodes / 50 edges, `max_hop_depth: 2`.

Routing is not a factor — `0.28 ms`, and a cold-start probe routes in `0.95 ms`.
**Traversal is stable at 91–141 ms; synthesis is 6.3–27.7 s and dominates
end-to-end latency by 2 orders of magnitude.** Cold-start candidates resolve
correctly: `AMZN`, `PLTR`, `GOOGL` → `{"route":"COLD_START","ticker":…}`.

Note: `time_starttransfer` ≈ `time_total` (28.059 s vs 28.059 s) — the SSE/stage
announcements are not flushed to the client before synthesis finishes, so the
UI shows nothing for the entire LLM wait.

### 6.2 Graph traversal (read-only handle, 3 runs each)

| Query | min | max |
|---|---|---|
| 1-hop Company→Filing | 0.57 ms | 2.66 ms |
| 2-hop Company→Filing→Metric | 0.91 ms | 1.23 ms |
| 3-hop + `count` aggregate | 0.99 ms | 1.81 ms |
| `has_company('AAPL')` | 0.14 ms | 0.79 ms |
| scan 10k `RawFact` | 4.92 ms | 6.25 ms |
| scan 10k `Chunk` | 7.48 ms | 24.20 ms |
| `count(Metric)` (20,519) | 0.28 ms | 0.56 ms |
| `stats()` 6-table counts | 0.26 ms | 0.40 ms |

**At 122k nodes / 190k edges LadybugDB reads are not a bottleneck.** The audit's
P0 "single writer" concern is real for concurrent *writes*, but no read latency
pressure is measurable at this size. Constraint 13 applies: do not assume
scalability limits — these are the only measured read numbers.

### 6.3 Not measured, and why

- **Ingestion throughput.** Ingestion writes to the authoritative
  `sandbox.lbug`. Per the freeze, it was **not run**. Measuring it requires a
  scratch DB harness (Phase 1 prerequisite), not the live graph. The audit's
  "30–60 s/ticker" figure remains unverified.
- **Cold-start end-to-end** (SEC fetch 2.5 s + extraction 3.5 s + synthesis).
  Routing to `COLD_START` was verified; the ingest leg was not executed for the
  same reason.
- **Load / concurrency.** Deferred to the audit's Phase 9. No load tool
  (`hey`, `ab`) is present, and none may be installed.

---

## 7. State baseline

**Persistent (authoritative)** — `sandbox_engine/_run/sandbox.lbug` (54.5 MiB,
LadybugDB). Not in git. Protected by LadybugDB's single-writer lock; no backup,
no export, no RPO/RTO.

**Persistent (secondary)** — `data/aapl-2026.lbug` (3.4 MiB);
`sandbox_engine/_run/staging/` (50 Parquet files, 12 MB) — the COPY source of
truth for reloads.

**Derived (rebuildable)** — `sandbox_engine/_run/report.json` (724 KB),
`concepts.json` (6.3 MB), `data/checkpoints/` (100 KB),
`data/ingestion-cache/` (412 KB), `benchmarks/results/`.

**Raw source cache (not authoritative, large)** — `data/` totals **402 MB**:
`microsoft-sec` 203 MB, `tsla-sec` 149 MB, `tsla-cache` 46 MB,
`data/staging` 32 KB, `data/sec-filings` empty.

**In-memory caches (per process, not shared)** — `ui/fingraph/server.py`:
`_markets_cache` (TTL 60 s), `_company_quote_cache` (TTL 60 s),
`_company_detail_cache` (TTL 300 s), `_apple_jwks_cache`.
`query_ui.py` holds one `threading.Lock` per `KnowledgeGraph` serialising all
queries on that handle.

**Background-job state** — `sandbox_engine/background.py`:
`ThreadPoolExecutor(max_workers=2)` with an in-process `self.tasks: list[Future]`.
**Queue depth and results are memory-only**: a restart loses every queued and
in-flight cold-start ingestion, and nothing can report depth or health. This is
why `/readyz` cannot be trivially added without also exposing queue state.

**Session state** — HMAC-SHA256 signed cookie `fin_session`,
`SESSION_TTL = 7 × 24 × 3600`, `hmac.compare_digest` verification.
> **Risk: `AUTH_SECRET` (`ui/fingraph/server.py:787`) falls back to
> `secrets.token_hex(32)` when `FINGRAPH_AUTH_SECRET` is unset — and it is unset**
> (absent from `.env` and the environment; `.env.example` documents only
> `NVIDIA_API_KEY`). Every restart therefore **invalidates all sessions** and
> silently rotates the signing key. Any horizontal-scaling plan (audit Phase 2)
> is broken without this pinned, because replicas would not share a secret.

---

## 8. Dependency baseline

Runtime `.venv`: **Python 3.13.15**, **56 packages**, all pins matching
`requirements.txt` — **zero drift, nothing upgraded or installed**.

| Package | Required | Installed |
|---|---|---|
| ladybug | `==0.20.4` | 0.20.4 |
| pandas | `==3.0.6` | 3.0.6 |
| lxml | `==6.1.3` | 6.1.3 |
| beautifulsoup4 | `==4.15.0` | 4.15.0 |
| pyarrow | `==25.0.1` | 25.0.1 |
| pypdf | `==6.19.0` | 6.19.0 |
| openai | `==3.19.2` | 3.19.2 |
| typer | `==0.27.2` | 0.27.2 |
| pydantic | `>=2.0.0` | 2.13.5 |
| networkx | `>=3.0` | 3.7 |
| requests | `>=2.31.0` | 2.34.2 |
| numpy | (transitive) | 2.5.3 |

Frozen and **not to be replaced**: LadybugDB, stdlib `ThreadingHTTPServer`,
`ThreadPoolExecutor`, openai SDK, PyArrow/Parquet, networkx, pydantic, typer,
`requests`. LLM provider is NVIDIA-hosted (`nvidia/nemotron-3-ultra-550b-a55b`).

---

## 9. Baseline of pre-existing failures

Ranked by production impact. All are reproducible from a clean checkout of the
working tree without any change.

| # | Severity | Finding | Fix class |
|---|---|---|---|
| 1 | **P0** | DB replaced under a live reader → server silently serves an empty graph with `HTTP 200` (§3.1) | stdlib; health/restart-detection |
| 2 | **P1** | All auth-gated endpoints hang the client to timeout (missing `Content-Length` on 401) (§4.2) | stdlib; one header |
| 3 | **P1** | `/api/company/<T>` always `in_graph: false` — wrong `..query_ui` import swallowed by bare `except` (§4.3) | one-line import fix |
| 4 | **P1** | `AUTH_SECRET` random per process → sessions die on every restart; blocks replicas (§7) | env var, no code |
| 5 | **P1** | No `/healthz` or `/readyz` (both 404) — nothing for a load balancer or orchestrator to probe | stdlib routes |
| 6 | P2 | Background queue state is memory-only; unreportable, lost on restart (§7) | stdlib |
| 7 | P2 | `/api/company/<T>` returns `200` with all-null Yahoo data after 17.5 s — silent upstream failure (§4.3) | stdlib |
| 8 | P2 | SSE stage events not flushed; UI blank for the full 27 s synthesis (§6.1) | stdlib flush |
| 9 | P2 | 33 test failures, 2 collection errors — all from the mid-refactor tree (§2.1) | land or revert the refactor first |
| 10 | P2 | `web/` deleted while Vite serves 404s; CI's frontend job cannot pass (§3) | restore or drop |
| 11 | P3 | Audit §13.1 baseline stale (30 filings/39 MB vs 236/54.5 MiB) (§5.1) | doc correction |

None of these requires a new dependency.

---

## 10. Freeze status and `v1.0-baseline`

**The tag was NOT created.** The precondition in the task — *the repository is
clean* — is not met: 133 uncommitted entries covering a partial directory move,
a deleted 23k-line frontend, and 6 modified runtime modules. Tagging `HEAD`
(`d7c9c6e4`) would produce a "baseline" that matches neither the running code
nor the data, and would be actively misleading as a rollback target.

Nothing was committed, stashed, reset, checked out, or deleted. Working-tree
state at the end of Phase 0 is byte-identical to the start: 133 porcelain
entries (14 M / 72 D / 47 ??), 90 untracked files, both DB checksums unchanged,
no WAL created.

**To create a truthful baseline, one of these must happen first** (owner
decision, not taken here):

1. Land the in-progress refactor on a branch, then tag — but only after
   `samples/`, `graphrag/web.py`, and `sandbox_engine/ui_next` are either
   restored or their tests removed/retargeted, so the suite collects.
2. Discard the refactor (`git restore` + clean untracked) and tag `HEAD` — this
   is the only option that yields a *green* suite, and it **destroys the user's
   uncommitted work**. Explicitly not done.
3. Tag the working tree as `v1.0-baseline-dirty` to name the real state.

The database needs a **separate** baseline artifact: it is untracked and has no
backup. A checksummed copy of `sandbox.lbug` is the only thing that would make a
Phase 1 rollback of graph state possible. Creating it was outside this phase's
read-only scope.

---

## 11. Is it safe to proceed to Phase 1?

**Yes for code work on a branch. Not yet for a tagged freeze, and not for any
ingestion run against the live graph.**

Prerequisites before Phase 1 writes anything:

1. **Resolve the mid-refactor working tree.** 33 test failures and 2 collection
   errors mean the suite cannot currently gate a change. Establish a green
   baseline first, or Phase 1 has no regression signal.
2. **Create the `v1.0-baseline` tag** (or the `-dirty` variant) per §10, so
   rollback is real.
3. **Back up `sandbox.lbug`** with a recorded checksum. It is the only copy of
   the authoritative graph and is not in git.
4. **Restart the stale 9100 instance.** It is currently answering users with an
   empty graph. This is an operational action, not a code change.
5. **Pin `FINGRAPH_AUTH_SECRET`** before any multi-process work.

Nothing found in this phase requires replacing any technology. Findings 1–8 are
all addressable with the frozen stack (stdlib + existing modules), so the
constraint set holds for Phase 1.
