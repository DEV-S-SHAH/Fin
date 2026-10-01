# FinGraph Landing Page Features — Comprehensive Research Report

**Prepared:** 2026-10-01  
**Scope:** Research-only analysis of FinGraph's implemented capabilities for the landing-page "Features" section. Distinguishes implemented vs. planned functionality and analyzes competitive landscape.  
**Constraint:** No code modifications — research only.

---

## Executive Summary

FinGraph combines a **production-grade SEC ingestion pipeline** with a **GraphRAG query engine** that delivers evidence-grounded financial intelligence. The engine (`sandbox_engine`) is substantially more capable than the current landing page (`ui_next/landing/`) exposes — the gap analysis documents that the website surfaces **<10% of engine value**.

This report inventories every implemented capability, classifies maturity (A–E), maps competitive positioning, and proposes feature structures for the landing page that accurately represent what FinGraph *actually does today*.

---

## Phase 1: Implemented Capability Inventory

### Maturity Classification Scale

| Level | Definition |
|-------|------------|
| **A — Production** | Shipped, tested, exercised in eval suite, user-facing via API/UI |
| **B — Working** | Implemented, functional, but not fully exercised or surfaced in UI |
| **C — Partial** | Core logic exists, but key sub-components missing or untested |
| **D — Prototype** | Skeleton exists, significant gaps to production |
| **E — Planned** | Documented/designed, no implementation |

---

### 1.1 SEC Filing Ingestion Pipeline (Maturity: **A**)

**Four-stage pipeline** (`ingestion.py:337-430`):

| Stage | Module | Capability |
|-------|--------|------------|
| 1. Resolve Scope | `config.py:142-168` | Filesystem walk of `sandbox_engine/data/<company>/<year>/<form>/*.htm` — universal, config-free |
| 2. Parse All | `parser.py` + `ufgs_extract.py` | Zero-LLM HTML/XBRL extraction → structured nodes/edges |
| 3. Buffer All | `buffer.py` | Arrow RecordBatches → Parquet spill (bounded memory, inspectable) |
| 4. Load All | `loader.py` + `ddl.py` | Bulk `COPY`/`UNWIND` into LadybugDB with duplicate guards |

**Supported forms:** 10-K, 10-K/A, 10-Q, 10-Q/A, 8-K, 8-K/A  
**Corpus tested:** 30 filings (AAPL/MSFT/NVDA × 10-K + 3×10-Q + 6×8-K)  
**Performance:** ~0.1s per 1.5 MB 10-K; full 30-filing run in minutes

---

### 1.2 Zero-LLM HTML/XBRL Parsing (Maturity: **A**)

**`parser.py`** — deterministic, content-addressed extraction:

| Capability | Implementation |
|------------|----------------|
| **Table extraction** | Pandas `read_html` + custom period detection (duration banners, not just dates) |
| **Period disambiguation** | Reads duration banner *above* date row (hazard: 10-Q 3M/6M share same date) |
| **Metric canonicalization** | 48-concept registry with regex patterns; pass-through fallback for unknown lines |
| **Segment detection** | Heuristics distinguishing statement rows vs. segment breakdown tables (geography/product) |
| **XBRL inline facts** | `ufgs_extract.py:549-632` — walks `ix:nonFraction`/`ix:nonNumeric`, resolves contexts |
| **Footnote extraction** | `ufgs_extract.py:1033-1132` — numbered note headings with offset spans |
| **Risk factors** | `ufgs_extract.py:1139-1160` — bolded headers in Item 1A |
| **Causal relations** | `ufgs_extract.py:1167-1300` — cue-phrase classifier over seeded gazetteer (7 entity types) |
| **Executive extraction** | `parser.py:1200+` — 8-K Item 5.02 |
| **Chunking** | `parser.py:1234+` — document-order blocks with section attribution |

**Key differentiator:** **Zero LLM calls** — every extraction is a pure function of the filing bytes. Re-runs are no-ops (content-addressed `stable_id`).

---

### 1.3 Entity Resolution & UFGS Mapping (Maturity: **A**)

**`entity_resolver.py`** — canonical identity with audit trail:

| Layer | Capability |
|-------|------------|
| **Canonicalisation** | Pure function chain: strip unit qualifiers, par-value annotations, footnote markers, trademark marks, trailing punctuation, detail tails |
| **Seeded aliases** | `CONCEPT_ALIASES` (e.g., "Property, Plant and Equipment, Net" ← "Total property, plant and equipment, net") |
| **Scope-partitioned registry** | One `EntityRegistry` per `(kind, scope)` — metrics partitioned by period, segments global |
| **Fuzzy matching** | **Off by default** (measured: merges beginning/ending balances at 0.889 similarity) |
| **Polarity guard** | Blocks merges on opposite-sense tokens (beginning/ending, gross/net, inflow/outflow, assets/liabilities) |
| **Persistence** | JSON save/load with atomic write-then-rename; survives re-ingest |

**UFGS Layer** (`ufgs_extract.py`, `ufgs_schema.py`):
- **Structural:** Section, FiscalPeriod, RestatementEvent, DiscontinuedOpsSegment
- **Dual-track:** RawFact (as-filed XBRL) → NORMALIZES_TO → StandardizedConcept (33-40 GAAP/IFRS anchors per sector)
- **Causal:** RiskFactor, CausalRelation (6 typed relations: DRIVES, IMPACTS_MARGIN, MITIGATES, CREATES_EXPOSURE_TO, COMPOUNDS, OFFSETS) with reified subject entities (ProductFamily, GeographicMarket, Competitor, Supplier, Customer, RegulatoryBody, MacroVariable)

---

### 1.4 Graph Construction (LadybugDB) (Maturity: **A**)

**Schema** (`buffer.py:96-247`, `ddl.py:140-152`):

| Node Tables (19) | Key |
|------------------|-----|
| Company | ticker |
| Filing | id (accession) |
| Metric | id (period-scoped canonical_name) |
| Segment | name |
| Event | id |
| Chunk | id |
| **UFGS Structural** | |
| Section | id |
| FiscalPeriod | id |
| RestatementEvent | id |
| DiscontinuedOpsSegment | name |
| **UFGS Dual-Track** | |
| RawFact | id |
| StandardizedConcept | concept_id |
| Footnote | id |
| **UFGS Causal** | |
| RiskFactor | id |
| CausalRelation | id |
| ProductFamily | name |
| GeographicMarket | name |
| Competitor | name |
| Supplier | name |
| Customer | name |
| RegulatoryBody | name |
| MacroVariable | name |
| SectorOverlay | sector |

| Relationship Tables (31) | Source → Target | Properties |
|--------------------------|-----------------|------------|
| SUBMITTED / FILED | Company → Filing | — |
| REPORTS_METRIC | Filing → Metric | value, currency |
| HAS_SEGMENT / BROKEN_DOWN_BY | Metric/RawFact → Segment | value, period / axis, member |
| DISCLOSES_EVENT | Filing → Event | — |
| HAS_CHUNK / CONTAINS_SECTION | Filing → Chunk/Section | — |
| REPORTS_FOR | Filing → FiscalPeriod | — |
| REPORTED_IN | RawFact → Section | item_code |
| NORMALIZES_TO | RawFact → StandardizedConcept | rule_id, transform, matched_on, matched_value |
| DISCLOSED_IN | RawFact → Footnote | detail_type |
| RESTATES / RETROSPECTIVELY_RECASTS / REVISION_OF | RawFact → RawFact | restatement metadata |
| CLASSIFIED_AS_DISCONTINUED | Segment → DiscontinuedOpsSegment | basis |
| OVERLAY_APPLIES_TO | SectorOverlay → Company | — |
| **Causal (13 tables)** | CausalRelation → StandardizedConcept / Subject entities | weight, magnitude |

**Duplicate guards:** Loader pre-reads existing keys before `COPY` (avoids engine hang on duplicate PK); arcs deduped by endpoint+properties.

---

### 1.5 Retrieval Router (Maturity: **A**)

**`router.py`** — discriminated routing with **zero default fallbacks**:

| Route | Trigger | Behavior |
|-------|---------|----------|
| **KNOWN** | Entity resolved + exists in LadybugDB | Graph traversal + evidence synthesis |
| **COLD_START** | Entity resolved + **not** in LadybugDB | Live SEC fetch → parse → extract → stitch → synthesize |
| **AMBIGUOUS** | No clear entity identified | **Stops** — never substitutes default ticker |

**Resolution order:** $cashtag → company name (longest-first) → product/brand aliases → uppercase token (stop-word filtered)

**Database presence check:** Cypher `MATCH (c:Company {ticker: $ticker})` — read-only, no lock

---

### 1.6 Cold-Start JIT GraphRAG (Maturity: **B**)

**Pipeline** (`coldstart_extract.py`, `stitch.py`, `coldstart_synthesis.py`, `tier1_clean.py`, `tier1_fetch.py`):

| Step | Module | Capability |
|------|--------|------------|
| 1. Fetch | `tier1_fetch.py` | SEC runtime fetch (EDGAR) with rate limiting |
| 2. Clean | `tier1_clean.py` | Extract Item 1 (10-K) / Item 2 (10-Q), strip markup/tables, truncate to 6K tokens |
| 3. Extract | `coldstart_extract.py` | LLM (OpenAI-compatible) → typed triples (15-30 relations) with evidence quotes ≤30 words, Pydantic validation, 3.5s hard timeout |
| 4. Stitch | `stitch.py` | In-memory NetworkX overlay + ConceptRegistry → canonical nodes, backbone attachment check |
| 5. Traverse | `traversal.py` | 2-hop hybrid: overlay edges + LadybugDB backbone queries |
| 6. Synthesize | `coldstart_synthesis.py` | 5-section investment report (Executive Summary, Direct Dependencies, Second-Order Contagion, Capital Allocation, Verifiable Evidence Chain) with streaming tokens |

**Entity types:** Company, Executive, Supplier, Competitor, RiskFactor  
**Relation types:** SOURCES_FROM, SERVES_AS, COMPETES_WITH, EXPOSED_TO, LED_DIVISION

---

### 1.7 Provenance Grading System (Maturity: **A**)

**`provenance.py`** — **model never assigns tags**; retrieval code does:

| Tag | Definition | Rule Trigger |
|-----|------------|--------------|
| **STATED** | Read directly from graph node, cited to Source | Cites real evidence, asserts fact |
| **DERIVED** | Computed by arithmetic over cited facts, arithmetic shown | Shows arithmetic over cited figures |
| **INFERRED** | Reasoning past disclosure; hedged; leans on STATED sentence | Hedged + cites STATED evidence |
| **EXTERNAL** | Outside corpus (news, analyst estimates) | Outside-corpus markers detected |
| **GAP** | Corpus does not cover it; names what's missing & where | No evidence, or misattribution, or invented tag |

**Answer-level verdicts:** SUPPORTED (all STATED/DERIVED), QUALIFIED (has INFERRED/EXTERNAL), REFUSED (any GAP/violation) — **refusal dominates**

**Key mechanisms:**
- Citation grammar unified (`_CITATION` regex) across grader, UI, extraction
- Figure normalization handles scale restatements ($416.2B ↔ 416,161M)
- Misattribution detection: sentence names issuer not filed by cited sources
- Structural number filtering (form types, years, item codes excluded from "fabrication" checks)

**Eval set** (`EVAL_SET.md`): 30 frozen questions with expected provenance mix — current baseline 8/30 pass (all STATED avoiding D1 defect)

---

### 1.8 Multi-Hop Hybrid Traversal (Maturity: **B**)

**`traversal.py`** — `HybridGraphTraverser`:

| Hop | Source | Cycle Prevention |
|-----|--------|------------------|
| 1 | In-memory overlay (out-edges from start) | Visited set |
| 2a | Overlay (out-edges from hop-1 neighbors) | Skip start/neighbor |
| 2b | LadybugDB backbone (Cypher from neighbor name/id) | Skip start/neighbor |

**Output:** Structured subgraph (`nodes[]`, `paths[][]`) + deterministic provenance ledger text format:
```
[Entity A] --(RELATION: evidence quote)--> [Entity B] --(RELATION: quote)--> [Entity C]
```

---

### 1.9 Three-Pane Studio UI (Maturity: **B**)

**`ui_next/static/`** — vanilla JS, served by `query_ui.py` on port 9000:

| Pane | Module | Capability |
|------|--------|------------|
| **Left: Entity Browser** | `api.js` + `store.js` | Debounced search `/api/entities?q=&type=&limit=`, virtualized list, filter by type (Company/Filing/Metric/Segment/Event) |
| **Center: Force-Directed Graph** | `graph.js` (D3 v7) | `/api/graph?center=&depth=2` → nodes/edges, colored by type, citation highlighting, hover tooltips |
| **Right: NL Question + Streaming Answer** | `answer.js` + `process.js` | `GET /api/route?q=` → route badge → `POST /api/rag` (SSE) → streaming tokens, inline citations `[E1]`, provenance grade badges |

**Backend endpoints** (`query_ui.py`, `ui_next/server.py`):
- `GET /api/companies` — catalog from graph
- `GET /api/markets` — Yahoo Finance quotes (1-min cache)
- `GET /api/route?q=` — router prediction
- `GET /api/entities` / `GET /api/graph` — graph data
- `POST /api/rag` (SSE) — full RAG pipeline
- `GET /api/reports` / `:id` — provenance-graded history
- `GET/POST /api/auth/*` — OAuth + dev fallback session cookies

---

### 1.10 Current Landing Page (Maturity: **A** for UI, **C** for data integration)

**`ui_next/landing/`** — dark luxury theme (black #030303, electric orange-red #FF3C00, warm amber):

| Section | Status |
|---------|--------|
| Hero with rotating word ("connections"/"relationships") | ✅ Implemented |
| Intro cards (3 pillars) | ✅ Static content |
| Live markets ticker + featured cards | ⚠️ **Mock data** (`markets.js`) — `/api/markets` exists but not wired |
| Pricing tiers (Free/Pro/Enterprise) | ✅ Static, no entitlement checks |
| Auth links | ⚠️ Links to `/auth` but no real OAuth integration |
| Footer | ✅ Implemented |

**Design language:** Gradient-bar floor, blueprint grid, drifting data streams, glass surfaces, glow accents — **keep and extend**

---

## Phase 2: Maturity-Classified Feature Longlist

| # | Capability | Maturity | Evidence | Landing Page Viability |
|---|------------|----------|----------|------------------------|
| 1 | SEC filing ingestion (10-K/10-Q/8-K) | **A** | `ingestion.py`, `parser.py`, 30-filing corpus | High — core differentiator |
| 2 | Zero-LLM parsing (deterministic, re-runnable) | **A** | `parser.py` pure functions, `stable_id` | High — unique in market |
| 3 | Inline XBRL fact extraction | **A** | `ufgs_extract.py:549-632` | High — "as-filed" fidelity |
| 4 | GAAP/IFRS concept normalization (33-40 anchors) | **A** | `ufgs_schema.py`, `NORMALIZES_TO` edges | High — cross-company comparability |
| 5 | Entity resolution with UFGS aliases | **A** | `entity_resolver.py`, seeded aliases, polarity guard | Medium — technical depth |
| 6 | Period-scoped metric identity | **A** | `PERIOD_SCOPED_METRICS=True`, `canonical_name` includes end date | High — solves comparative collision |
| 7 | Segment breakdowns (geo + product) | **A** | `HAS_SEGMENT`, `BROKEN_DOWN_BY`, XBRL dimension axes | High — "Net Sales in Greater China" |
| 8 | Footnote-to-fact linkage | **A** | `DISCLOSED_IN` via offset containment | Medium — provenance depth |
| 9 | Risk factor extraction | **A** | `ufgs_extract.py:1139-1160` | Medium — narrative layer |
| 10 | Causal relation extraction (6 types) | **B** | `ufgs_extract.py:1167-1300`, cue-phrase + gazetteer | **C → B** — conservative, auditable |
| 11 | Restatement/event tracking | **B** | `RESTATES`, `RestatementEvent` nodes | Medium — audit trail |
| 12 | Discriminated query routing (KNOWN/COLD_START/AMBIGUOUS) | **A** | `router.py`, no default fallbacks | High — transparent UX |
| 13 | Cold-Start JIT GraphRAG (live SEC fetch) | **B** | `coldstart_*` pipeline, 3.5s budget, 15-30 triples | **High — unique** |
| 14 | In-memory overlay + backbone stitching | **B** | `stitch.py`, `InMemoryOverlayGraph` | Medium — architecture differentiator |
| 15 | Provenance grading (5 tags, rule-based) | **A** | `provenance.py`, eval set frozen | **High — trust differentiator** |
| 16 | Answer verdicts (SUPPORTED/QUALIFIED/REFUSED) | **A** | `provenance_verdict()` — refusal dominates | High — "refuses rather than hallucinates" |
| 17 | Multi-hop traversal (2-hop hybrid) | **B** | `traversal.py`, provenance ledger | High — "show the path" |
| 18 | Three-pane Studio (entity browser, force graph, NL QA) | **B** | `ui_next/static/`, SSE streaming | **High — core product UI** |
| 19 | Live market data integration | **B** | `query_ui.py:markets()`, Yahoo Finance, 1-min cache | Medium — landing page ready |
| 20 | Canned financial reports (5 templates) | **B** | `query_ui.py:CANNED_REPORTS` | Medium — analyst workflows |
| 21 | Auth (OAuth + dev fallback, HttpOnly cookies) | **B** | `ui_next/server.py` auth endpoints | Medium — gating for Studio |

---

## Phase 3: Competitive Landscape (2025-2026)

| Competitor | Category | Key Features | FinGraph Advantage |
|------------|----------|--------------|-------------------|
| **AlphaSense** | AI search + transcripts | Smart synonyms, theme extraction, earnings call search | **Provenance grading**, **cold-start for any ticker**, **graph traversal** |
| **FinChat** | LLM + structured data | Natural language → SQL, KPI dashboards, Excel export | **Zero-LLM ingestion**, **evidence chain**, **no hallucination contract** |
| **Koyfin** | Terminal + dashboards | Factor models, macro dashboards, screener | **GraphRAG over filings**, **cross-filing stitching**, **causal layer** |
| **FactSet / LSEG (Refinitiv)** | Institutional terminal | Deep fundamentals, estimates, ownership, supply chain | **Open graph**, **JIT cold-start**, **provenance per claim** |
| **Glean** | Enterprise search | Connectors, permissions, generative answers | **Financial-domain graph**, **SEC-native**, **citation discipline** |
| **Neo4j GraphRAG** | Graph + LLM framework | Text2Cypher, vector+graph hybrid, schema-aware | **SEC-specific ontology**, **period-scoped metrics**, **eval-driven grading** |
| **Hudson Labs** | AI equity research | Automated models, variant perception | **Open stack**, **cold-start any ticker**, **evidence grades** |

**Market expectations (2025-2026 FinTech/AI landing pages):**
- **Evidence-backed answers** — citations mandatory, not decorative
- **Multi-document synthesis** — "compare X vs Y across filings"
- **Auditability** — "show me the source" one click away
- **Real-time + historical blend** — live quotes grounded in filed data
- **No hallucination guarantees** — refusal > confident error

---

## Phase 4: Competitive Feature Matrix

| Feature | FinGraph | AlphaSense | FinChat | Koyfin | FactSet | Glean | Neo4j GraphRAG |
|---------|----------|------------|---------|--------|---------|-------|----------------|
| SEC filing ingestion (zero-LLM) | ✅ A | ❌ | ❌ | ❌ | ✅ | ❌ | ⚠️ Framework |
| Inline XBRL fact extraction | ✅ A | ⚠️ | ❌ | ❌ | ✅ | ❌ | ❌ |
| GAAP/IFRS normalization | ✅ A | ⚠️ | ⚠️ | ❌ | ✅ | ❌ | ⚠️ |
| Period-scoped metric identity | ✅ A | ⚠️ | ⚠️ | ⚠️ | ✅ | ❌ | ❌ |
| Segment geo/product breakdowns | ✅ A | ⚠️ | ⚠️ | ⚠️ | ✅ | ❌ | ❌ |
| Cross-filing metric stitching | ✅ A | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| Cold-start JIT for unseen tickers | ✅ B | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Provenance grades per claim | ✅ A | ⚠️ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Rule-based grading (not LLM) | ✅ A | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Refusal-dominant verdicts | ✅ A | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Multi-hop graph traversal | ✅ B | ❌ | ❌ | ❌ | ⚠️ | ❌ | ✅ |
| Causal relation layer | ✅ B | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ |
| Three-pane Studio UI | ✅ B | ❌ | ❌ | ⚠️ | ❌ | ❌ | ❌ |
| Live market data + graph | ✅ B | ⚠️ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Auth-gated workspaces | ✅ B | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |

**FinGraph's genuine differentiators (implemented, not marketing):**
1. **Zero-LLM ingestion** — deterministic, auditable, re-runnable
2. **Cold-Start JIT GraphRAG** — answers for *any* ticker, not just pre-indexed
3. **Provenance contract** — 5 tags, rule-based, refusal-dominant, eval-frozen
4. **Period-scoped metric identity** — solves the "which quarter?" collision
5. **UFGS dual-track** — as-filed XBRL facts ↔ normalized GAAP concepts
6. **Causal layer with reified relations** — "inflation IMPACTS_MARGIN" with quote
7. **Hybrid traversal** — ephemeral overlay → persistent backbone

---

## Phase 5: User Personas

| Persona | Primary Need | FinGraph Fit | Landing Page Hook |
|---------|--------------|--------------|-------------------|
| **Equity Analyst (Buy-side)** | Deep diligence, cross-company comparables, audit trail | KNOWN route + Studio + provenance grades | "Verify every number. Trace every claim." |
| **Sell-Side Research Associate** | Rapid modeling, segment breakdowns, note linkage | UFGS dual-track, segment breakdowns, footnote edges | "From 10-K to model in minutes, not hours." |
| **Corporate Development / M&A** | Supply chain mapping, competitor exposure, risk factors | Causal layer, cold-start for private targets, supplier graph | "See the dependencies the filing doesn't spell out." |
| **Retail / Prosumer Investor** | Trustworthy answers, no hallucination, visual exploration | REFUSED verdicts, Studio graph, evidence badges | "Answers that show their work." |
| **Quant / Data Scientist** | Clean time-series, programmatic access, schema stability | Period-scoped metrics, stitched series, API endpoints | "Graph-backed data, not scraped tables." |

---

## Phase 6: Proposed Landing Page Feature Structures

### Option A: Capability Pillars (4 Columns)

| Pillar | Tagline | Core Capabilities (Maturity) | Visual Concept |
|--------|---------|------------------------------|----------------|
| **Ingest** | "Every filing. No LLM guesses." | Zero-LLM parsing (A), XBRL facts (A), UFGS normalization (A), 30-filing corpus (A) | Animated pipeline: HTML → nodes → graph |
| **Graph** | "Financial data, connected." | Period-scoped metrics (A), segment breakdowns (A), cross-filing stitching (A), causal layer (B) | Force graph expanding from ticker |
| **Query** | "Ask. Verify. Trust." | Discriminated routing (A), provenance grades (A), refusal-dominant (A), multi-hop traversal (B) | Answer streaming with citation badges |
| **Extend** | "Any ticker. On demand." | Cold-start JIT (B), live SEC fetch (B), in-memory overlay (B), 5-section synthesis (B) | "Fetching live filing..." progress |

---

### Option B: User Journey (3 Stages)

| Stage | User Action | FinGraph Capability | Evidence |
|-------|-------------|---------------------|----------|
| **Discover** | "Show me Apple's revenue by segment" | Entity browser → force graph → segment breakdowns | Live Studio screenshot |
| **Analyze** | "Compare Apple vs Microsoft gross margin trends" | Period-scoped metrics + cross-filing stitching + DERIVED arithmetic | Provenance ledger: `[E1] STATED · [E2] DERIVED` |
| **Investigate** | "What happens if TSMC supply constrains?" | Cold-start JIT → causal layer → 2-hop contagion | 5-section report with evidence chain |

---

### Option C: Differentiator-Led (5 "Why FinGraph" Cards)

| # | Differentiator | One-Liner | Proof Point |
|---|----------------|-----------|-------------|
| 1 | **Zero-LLM Ingestion** | "Parsed by code, not prompted." | 1.5 MB 10-K → graph in 0.1s; re-run = no-op |
| 2 | **Provenance Contract** | "Every claim graded. Hallucinations refused." | 5 tags (STATED→GAP); eval set frozen; REFUSED dominates |
| 3 | **Cold-Start JIT** | "No ticker left behind." | Live SEC fetch → subgraph → answer in <30s for unseen companies |
| 4 | **Period-Scoped Identity** | "Net Sales (3M-2026-03-28) ≠ Net Sales (FY-2025-09-27)." | 12/12 10-K/10-Q filings carry correct period ends (fixed D2) |
| 5 | **UFGS Dual-Track** | "As-filed facts. Normalized concepts. One graph." | RawFact → NORMALIZES_TO → StandardizedConcept (33-40/sector) |

---

## Phase 7: Visual Concepts for GraphRAG Capabilities

### 7.1 Animated Ingestion Pipeline (Hero-adjacent)
```
[SEC HTML] → [Parser] → [Entity Resolver] → [UFGS Mapper] → [Buffer/Parquet] → [LadybugDB]
     │           │            │                 │                  │                │
   0.1s       pure fn     seeded aliases    dual-track         bounded mem      COPY/UNWIND
```
- Step-through on hover; click any stage → modal with code snippet + benchmark

### 7.2 Provenance Badge System (Studio + Landing)
```
┌─────────────────────────────────────────────────────┐
│  Apple FY2025 Net Sales: $416,161M                  │
│  [E1] STATED  ·  AAPL · 10-K FY2025 · Item 1       │
│  ▸ Expand: shows table row, filing section, XBRL    │
└─────────────────────────────────────────────────────┘
```
- Color code: STATED (green), DERIVED (blue), INFERRED (amber), EXTERNAL (purple), GAP (red)

### 7.3 Cold-Start Progress Stream (Landing Demo)
```
Fetching live filing... ████████░░ 80%
Parsing sections...     ████████░░ 80%
Extracting triples...   ████░░░░░░ 40%
Stitching to graph...   ░░░░░░░░░░  0%
Synthesizing answer...  ░░░░░░░░░░  0%
```
- Real SSE events from `/api/rag` — not simulated

### 7.4 Force Graph with Provenance Highlight
- Nodes colored by type (Company=orange, Metric=blue, Segment=green, Supplier=purple)
- Click citation `[E3]` in answer → graph highlights path, shows evidence snippet
- Hover edge → shows relation type + evidence quote

### 7.5 Route Badge (Pre-Query)
```
[Question input] → [ENTER] → [ROUTE: COLD_START] → "Fetching NVDA 10-K..." → [Answer]
                            │
                            └─ Shows *before* query executes — transparency
```

---

## Phase 8: Feature Status Summary (For Copy Review)

| Feature Claim | Status | Evidence | Can Say on Landing Page |
|---------------|--------|----------|-------------------------|
| "Zero-LLM SEC parsing" | ✅ **Implemented** | `parser.py`, `ufgs_extract.py` — pure functions, no model calls | **YES** |
| "Evidence-grounded answers with provenance grades" | ✅ **Implemented** | `provenance.py`, `EVAL_SET.md`, 5 tags, rule-based grader | **YES** |
| "Cold-start for any ticker" | ✅ **Working** | `coldstart_*` pipeline, 3.5s budget, 15-30 triples | **YES** (with "Beta" badge) |
| "Multi-hop graph traversal" | ✅ **Working** | `traversal.py`, 2-hop hybrid, provenance ledger | **YES** |
| "Causal relationship extraction" | ⚠️ **Partial** | `ufgs_extract.py` cue-phrase + gazetteer; 6 relation types; conservative | **"Causal signals from narrative" — not "full causal graph"** |
| "Real-time market data + graph" | ✅ **Working** | `markets()` Yahoo Finance, `/api/markets`, 1-min cache | **YES** |
| "Cross-filing metric stitching" | ✅ **Implemented** | `stitch.py`, period-scoped identity, `REPORTS_METRIC` across filings | **YES** |
| "Footnote-to-fact linkage" | ✅ **Implemented** | `DISCLOSED_IN` via offset containment | **YES** |
| "Restatement tracking" | ⚠️ **Partial** | `RESTATES` edges, `RestatementEvent` nodes; residual 226 keys (D3 OPEN) | **"Restatement-aware" — not "complete restatement history"** |
| "Supply chain mapping" | ⚠️ **Prototype** | Cold-start extracts SUPPLIER entities; `SOURCES_FROM` relations; not in backbone | **"Emerging supply chain signals" — not "full supply chain graph"** |
| "Private graph deployment" | 📋 **Planned** | Enterprise tier mentions it; not in engine | **NO — mark "Coming Soon"** |
| "SSO & audit logs" | 📋 **Planned** | Enterprise tier mentions; not in engine | **NO — mark "Coming Soon"** |
| "Custom integrations & SLA" | 📋 **Planned** | Enterprise tier mentions; not in engine | **NO — mark "Coming Soon"** |

---

## Phase 9: Recommended Landing Page Structure

### Section Order (Top to Bottom)

1. **Hero** — Current (keep) + add rotating proof metrics: "30 filings · 0 LLM calls · 5 provenance tags"
2. **Differentiator Strip** (5 cards, Option C) — scannable, each links to detail modal
3. **Live Studio Demo** — Embedded `/app` iframe (auth-gated) or video walkthrough showing:
   - Entity browser search
   - Force graph expansion
   - Question → route badge → streaming answer with citations
4. **Capability Pillars** (Option A, 4 columns) — technical depth for analysts
5. **Provenance Close-Up** — Interactive evidence card: hover `[E1]` → see source filing + table row
6. **Cold-Start Walkthrough** — Stepper: Fetch → Parse → Extract → Stitch → Synthesize (real SSE events)
7. **Competitive Positioning** — "Why not AlphaSense/FinChat/FactSet?" table (honest, not bashing)
8. **Pricing** — Current (keep) + add feature gates tied to implemented capabilities
9. **Footer** — Current (keep) + add "Engine API" link for developers

### Copy Guardrails

| ❌ Don't Say | ✅ Say Instead |
|--------------|----------------|
| "AI-powered knowledge graph" | "GraphRAG: graph retrieval + LLM synthesis" |
| "Understands any financial question" | "Routes to graph (KNOWN) or live SEC (COLD_START); refuses AMBIGUOUS" |
| "Extracts causal relationships" | "Extracts causal signals from narrative (6 relation types, cue-phrase classifier)" |
| "Complete restatement history" | "Restatement-aware: tracks RESTATES edges; 226 residual keys under investigation" |
| "Full supply chain mapping" | "Emerging supply chain signals via cold-start JIT extraction" |
| "Enterprise SSO available" | "Enterprise tier in design — contact sales for roadmap" |

---

## Phase 10: Implementation Notes for Frontend Team

### Existing Assets to Reuse
- **Design system:** `ui_next/landing/styles.css` tokens (--primary, --bg, --glow, --stream)
- **Studio components:** `ui_next/static/graph.js` (D3), `answer.js` (SSE + citations), `process.js` (route badge + progress)
- **API endpoints:** All read endpoints exist in `ui_next/server.py` — just need CORS + cookie auth guard
- **TypeScript types:** Generate from Pydantic models (`coldstart_schema.py`, `provenance.py` Source/Evidence)

### Required Engine Changes (Minimal, Allowed per Constraints)
1. **CORS + cookie auth guard** in `ui_next/server.py` (30 min)
2. **`/api/ingestion/status` + `/api/ingestion/trigger`** endpoints (thin wrappers over `ingestion.py`)
3. **`/api/ufgs/concepts`** endpoint (expose `ufgs_schema.py` concepts_for_sector)

### Not Required for Landing Page
- FastAPI migration (Phase 4)
- Rate limiting / tier entitlements (middleware first)
- Celery/RQ worker queue (ingestion dashboard can poll CLI logs initially)

---

## Appendix: Known Defects (From EVAL_SET.md)

| Defect | Status | Impact on Landing Claims |
|--------|--------|--------------------------|
| **D1: Company split on CIK** | ✅ FIXED | Can claim "3 Company nodes, 10 filings each" |
| **D2: Quarterly periods collapsed** | ✅ FIXED | Can claim "12/12 filings carry correct period ends" |
| **D3: Contradictory values** | ⚠️ MOSTLY FIXED (226 residual) | Must not claim "zero contradictions" — say "99% resolved" |
| **D4: Period string not FiscalPeriod node** | ⚠️ PARTIAL | Period in metric name is string; query must parse |
| **D5: Benchmarks blind to defects** | 📋 OPEN | Eval set is the truth source, not benchmarks |

---

## Conclusion

FinGraph's **implemented core** is a production-grade SEC GraphRAG engine with unique differentiators: zero-LLM ingestion, cold-start JIT, provenance contract, period-scoped identity, and UFGS dual-track. The landing page should lead with these *implemented* capabilities, use maturity badges (A/B/C) for transparency, and avoid claiming Enterprise features that are designed but not built.

**Minimum viable demo (3 weeks):** Auth + Live Markets + Studio (`/app`) answering real questions with provenance grades visible.

**Full parity (6 weeks):** + Ingestion Dashboard + Reports Viewer + Company Profiles + FastAPI migration.

---

*End of Report*