# AGENTS.md — FinGraph Engineering Constitution

**Read before acting. All agents must comply.**

---

## 1. PURPOSE & ARCHITECTURE

**FinGraph** — GraphRAG for Financial Insights. Retrieves, reasons over, and cites evidence from SEC filings. **Financial evidence integrity is a first-class requirement.**

**Two distinct pipelines sharing LadybugDB (embedded property graph):**

- `sandbox_engine` — SEC-specific, deterministic, zero-LLM HTML parsing → Arrow/Parquet → LadybugDB
- `graphrag` — Domain-agnostic, LLM-driven triple extraction from PDFs

**Agents must not treat these as interchangeable.** Different purposes, extraction methods, invariants.

---

## 2. NON-NEGOTIABLE INVARIANTS

### Provenance
**Assigned by retrieval code, never by the model.**
- Five tags: `STATED` (direct from node), `DERIVED` (arithmetic shown), `INFERRED` (hedged + leans on STATED), `EXTERNAL` (outside corpus), `GAP` (corpus lacks answer — names what/where)
- Model may ONLY cite tags emitted by retrieval
- Grader reduces sentences to `SUPPORTED` / `QUALIFIED` / `REFUSED` — any `GAP` → `REFUSED`

### Deterministic SEC Ingestion
`parser.py` = zero-LLM, deterministic. LLMs allowed downstream (cold-start, graphrag) but **must not replace SEC parsing**.

### Idempotency
Writes must be idempotent at every layer:
- Cypher `MERGE` on `(id)` and `(from_id, to_id, rel_type)`
- Deduplication via `ConceptRegistry` and `drain_staging.py`
- Atomic checkpoints (`.tmp` → `os.replace`)
- Exponential backoff with jitter, respecting `Retry-After`

### Company Isolation
**Retrieval must scope to one issuer.** Single LadybugDB holds multiple companies.
- Graph traversal anchors on seed tickers
- Cold-start overlays stitch via target ticker only
- `CompanyScope` enforces `is_single_company / is_cross_company / is_global`
- Misattribution check: sentence names issuer X but cites evidence from issuer Y → `GAP`
- Benchmarks forbid Apple terms in non-AAPL answers

### Fiscal Calendars
**Never assume universal fiscal year-end.** Use configured `FiscalCalendar`:
- AAPL → Sep 30, MSFT → Jun 30, TSLA → Dec 31

### Tier-1 SLA
Runtime SEC fetch (`tier1_fetch.py`): **2.5s hard SLA** (`MAX_TOTAL_BUDGET_SECONDS = 2.5`, `MAX_SOCKET_TIMEOUT_SECONDS = 2.0`). Single jittered retry on 429 within budget. Do not increase timeouts without review.

### LadybugDB
Embedded, file-based (`.lbug`). Respect:
- Single writer — `drain_staging.py` checks port 9000, refuses if server holds DB
- Exclusive write lock; concurrent readers OK
- No `ALTER TABLE ADD COLUMN` — node drift = rename/recreate/copy/drop; rel drift = `SchemaDriftError`
- `CREATE IF NOT EXISTS` accepts drifted tables silently — `ensure_schema()` introspects every table
- WAL on hard kill = hard error with recovery instructions

### Graph + Retrieval
**Do not replace graph traversal with generic vector-only retrieval.** Architecture relies on:
- Graph neighborhood traversal (`traversal.py`, `store.py` `neighborhood()`)
- Company-aware traversal (seed protection in `_trim()`)
- Name-based lookup with specificity ranking (`lookup_by_name()`)
- Provenance-aware evidence blocks (`build_evidence()`)

---

## 3. PIPELINE BOUNDARIES

| Pipeline | Owns |
|----------|------|
| `sandbox_engine` | SEC ingestion, routing (KNOWN/COLD_START/AMBIGUOUS), Tier-1 fetch, cold-start extraction, overlay graph, traversal, provenance, synthesis, query UI |
| `graphrag` | Generic PDF ingestion, chunking, LLM extraction, graph storage (idempotent MERGE), entity resolution, general QA |
| `ingestion` | Multi-company orchestration, company registry, SEC acquisition, company-isolated retrieval |
| `ui` | Presentation, routing, API, auth, input validation, SSRF, rendering — **must not bypass domain boundaries** |

---

## 4. EVIDENCE & FINANCIAL DATA

**Financial claims must remain traceable to authoritative evidence.** If unavailable: represent the gap, don't invent certainty.

- Source hierarchy: SEC EDGAR (submissions + filing HTML) = PRIMARY; Company Registry = METADATA; Filing metadata = DERIVED
- Identity: Company = (ticker, CIK); Filing = CIK \| accession \| form_type
- Evidence lineage: every citable fact = `Evidence(tag, text, source, provenance, kind)` — provenance set by retrieval
- Citation: model may ONLY cite tags in evidence block; invented tags → `GAP` → `REFUSED`
- `grade_answer()` checks figures against **only cited evidence** (not whole block)
- Misattribution: sentence credits issuer X but cites evidence filed by Y → `GAP`
- Conflicts: longer description wins; populated fields not overwritten by blanks
- External: "news", "analyst", "consensus", "market share" → `EXTERNAL` → `QUALIFIED`
- Inferred: hedged + leans on prior STATED → `INFERRED`; unhedged forward claim → `GAP`

---

## 5. LLM BOUNDARIES

### ALLOWED
- Cold-start extraction (`coldstart_extract.py`)
- Generic GraphRAG extraction (`extract_chunk()`)
- Answer synthesis (`coldstart_synthesis.py`, `qa.py`)
- Supported providers: `OpenAICompatibleClient`, `GeminiClient`, `AnthropicClient`, `NvidiaNimClient`, `HeuristicClient` (testing only)

### FORBIDDEN
- Fabricate evidence
- Decide authoritative provenance
- Replace deterministic SEC parsing
- Bypass company isolation
- Override DB constraints
- Invent missing financial facts

---

## 6. DATABASE RULES

- Inspect schema before changes (`buffer.py` `NODE_TABLES`, `REL_TABLES`, `PRIMARY_KEYS`)
- No casual schema changes — understand downstream: parser → loader → traversal → provenance
- Node drift: `rebuild_node_table()`; rel drift: manual `DROP TABLE`
- Preserve: `INT64 fiscal_year`, `DATE filing_date` for correct queries

---

## 7. TESTING & QUALITY

**Before change:** inspect relevant tests, identify affected invariants.
**After change:** run smallest relevant suite; broader tests for architectural changes; benchmarks for retrieval/synthesis.

| Change Area | Required Tests |
|-------------|----------------|
| Ingestion/retrieval | `test_generic_ingestion.py`, `test_ingestion.py`, `test_router.py` |
| Provenance | `test_provenance.py`, `test_provenance_ui.py` |
| Idempotency | `test_persistence_restart.py`, `test_drain_staging.py` |
| Routing | `test_router.py`, golden queries |
| Cold-start | `test_coldstart_extract.py`, `test_coldstart_stitch.py`, `test_coldstart_latency.py` |
| Graph traversal | `test_multi_hop_traversal.py`, `test_neighborhood_priority.py` |
| Company isolation | `TestCompanyIsolation`, anti-leak benchmarks |
| Security | `test_ssrf.py`, `test_http_concurrency.py` |

**Benchmark-sensitive changes:** run relevant golden queries; preserve anti-leak, negative controls, minimum-hop requirements.

---

## 8. SECURITY & PERFORMANCE

### Security
- No hardcoded secrets — env-based (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `SEC_USER_AGENT`)
- Preserve SSRF protection (`FINGRAPH_SSRF_CONFIG` allow-list: `www.sec.gov`, `data.sec.gov`, Yahoo Finance)
- Validate external URLs before requests
- Auth boundaries, HMAC sessions, no secrets in logs
- **Security changes require `test_ssrf.py` + `test_http_concurrency.py`**

### Performance
- Tier-1 fetch: 2.5s SLA
- Respect caching (checkpoints, disk cache, `_TTLCache`)
- Bound graph traversal (`max_hops`), context (`max_context_nodes/edges`)
- Seed-protection trimming preserved
- **Never optimize by weakening correctness or evidence integrity**

---

## 9. CHANGE DISCIPLINE

- Modify only necessary files — no unrelated refactoring
- Preserve existing behavior unless task requires change
- Inspect callers before changing interfaces (`grep` usages)
- Inspect tests before changing behavior
- Preserve backwards compatibility where practical
- Document breaking changes
- **No broad rewrites without explicit authorization**

---

## 10. AGENT OPERATING RULES

### Must
1. Read AGENTS.md before acting
2. Inspect implementation before proposing changes
3. Identify affected pipeline and invariants
4. Inspect tests
5. Make smallest correct change
6. Run relevant tests
7. Report what changed (files, functions, behavior)
8. Report test results honestly

### Must Not
- Invent architecture/requirements
- Bypass invariants (provenance, isolation, idempotency, fiscal, SLA)
- Fabricate evidence/citations
- Weaken or delete tests to pass
- Modify unrelated modules
- Silently rewrite architecture

---

## 11. STOP / ESCALATE

**Stop and escalate when:**
- Requirement conflicts with an architectural invariant
- Evidence integrity would be weakened
- Company isolation cannot be guaranteed
- Security control must be bypassed
- Unexpected DB schema change required (rel table drift, new node type)
- Breaking API change required without authorization
- Test failures cannot be explained
- Repository state is inconsistent/unexpected

**Do not guess through high-risk architectural decisions.**

---

## 12. PROJECT-SPECIFIC VS GLOBAL

**FinGraph-specific:** provenance contract, dual pipelines, company isolation, fiscal calendars, 2.5s SLA, LadybugDB constraints, SEC deterministic ingestion, anti-leak benchmarks.

**Generic (reusable):** read AGENTS.md first, inspect before change, smallest correct change, run tests, report honestly, stop on conflicts.

---

## 13. UNVERIFIED AREAS

Do not assume undocumented operational behavior. Verify deployment, backup/recovery, tenancy beyond Company node, or future vector-search before changing those areas.

---

## 14. COMPLETION CHECKLIST

- [ ] No rule contradicts implementation
- [ ] `sandbox_engine` / `graphrag` separation preserved
- [ ] Provenance explicit (retrieval-assigned)
- [ ] Company isolation explicit
- [ ] LadybugDB constraints explicit
- [ ] Testing requirements explicit
- [ ] Security requirements explicit
- [ ] No unsupported assumptions

---

**End of AGENTS.md**