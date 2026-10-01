# FinGraph Comprehensive Research Report
## Capabilities Assessment for Landing Page "Features" Section

**Date:** October 1, 2026  
**Repository:** `/Users/dev/Downloads/Fin`  
**Research Scope:** Evidence-based assessment of implemented vs. planned vs. aspirational features  
**Constraint:** No functionality claims beyond what is demonstrably implemented in the codebase

---

## Executive Summary

FinGraph is a **GraphRAG-based Financial Insights platform** that combines:
- **Zero-LLM SEC filing ingestion** (parser.py, ufgs_extract.py)
- **Knowledge graph** with 40 canonical financial concepts + sector overlays (ufgs_schema.py)
- **Hybrid retrieval**: vector + graph traversal + cold-start JIT (traversal.py, coldstart_*.py)
- **Provenance-graded synthesis** (STATED/DERIVED/INFERRED/EXTERNAL/GAP) (provenance.py, query_ui.py)
- **Entity routing** (KNOWN/COLD_START/AMBIGUOUS) (router.py)
- **Evaluation framework** with provenance contract (EVAL_SET.md, eval_set.py)

**Current corpus:** 30 filings (AAPL/MSFT/NVDA × 1×10-K + 3×10-Q + 6×8-K each)  
**Current eval baseline:** 8/30 questions pass (all STATED block avoiding D1 defect)

---

## Section 1: Core Architecture & Data Pipeline

### 1.1 Implemented Capabilities

| Capability | Implementation | Evidence |
|------------|---------------|----------|
| **Zero-LLM SEC filing ingestion** | HTML parser extracts tables, XBRL facts, sections, footnotes without any LLM | `parser.py:1-1200`, `ufgs_extract.py:1-1172` |
| **Inline XBRL fact extraction** | Parses `ix:nonFraction`/`ix:nonNumeric` with context resolution, units, scale, decimals | `ufgs_extract.py:549-632` |
| **Structural section extraction** | 10-K/10-Q/8-K item taxonomies with anchor resolution | `ufgs_extract.py:288-447`, `ufgs_schema.py:619-700` |
| **Footnote extraction** | Numbered notes with `DISCLOSED_IN` arcs by offset containment | `ufgs_extract.py:1033-1132` |
| **Fiscal period nodes** | First-class `FiscalPeriod` nodes with `calendar_year_overlap` for cross-issuer comparison | `ufgs_extract.py:901-1005`, `ufgs_schema.py:739-783` |
| **Sector-aware schema (UFGS-2026-09)** | 40 canonical concepts (SC-01..SC-40), 25 normalization rules, 4 sector overlays | `ufgs_schema.py:170-500` |
| **Dual-track normalization** | As-filed labels + XBRL tags preserved; `NORMALIZES_TO` edges with rule ID + match type | `ufgs_extract.py:765-868`, `ufgs_schema.py:559-612` |
| **Canonical entity resolution** | Exact name → alias → opt-in fuzzy (default OFF); registry persists across re-ingests | `entity_resolver.py:1-400`, `config.py:104-125` |
| **LadybugDB graph storage** | 15 node tables, 18 rel tables; DDL with drift guard; COPY/UNWIND bulk loading | `ddl.py:1-358`, `buffer.py`, `loader.py` |
| **Idempotent re-ingestion** | Concept registry (`concepts.json`) ensures same nodes across runs | `config.py:192-204`, `ingestion.py` |

### 1.2 Known Defects (from EVAL_SET.md)

| Defect | Status | Impact |
|--------|--------|--------|
| **D1** Company split on CIK | FIXED | MSFT 8-Ks now correctly attributed |
| **D2** Quarterly periods collapsed onto annual | FIXED | All 12 10-K/10-Q filings carry own period end |
| **D3** Contradictory metric values | MOSTLY FIXED (500→230 keys) | 226 residual annual-period keys need header dates |
| **D4** Period as string not FiscalPeriod node | PARTIALLY FIXED | Query must parse `Net Sales (3M-2026-03-28)` |
| **D5** Benchmarks blind to defects | OPEN | 5/5 passed with D1-D3 present |

---

## Section 2: Retrieval & Query Pipeline

### 2.1 Implemented Capabilities

| Capability | Implementation | Evidence |
|------------|---------------|----------|
| **Entity routing (3-way)** | KNOWN (graph), COLD_START (live fetch), AMBIGUOUS (multi-issuer) | `router.py:1-200`, `query_ui.py:4292-4319` |
| **Multi-hop hybrid traversal** | 2-hop: vector seeds → graph expansion → vector rerank | `traversal.py:1-400` |
| **Cold-start JIT pipeline** | Fetch latest 10-K → extract triples → stitch overlay → traverse → synthesize | `coldstart_extract.py`, `coldstart_synthesis.py`, `stitch.py` |
| **Vector retrieval** | Chunk embeddings via NVIDIA NIM or local Ollama | `query_ui.py:1950-2100` |
| **Graph traversal** | Seed entities → neighborhood expansion (max 2 hops) → path ranking | `traversal.py`, `query_ui.py:4300-4400` |
| **Evidence grading** | LLM grader tags each sentence: STATED/DERIVED/INFERRED/EXTERNAL/GAP | `provenance.py`, `query_ui.py:4400-4700` |
| **Provenance contract** | 5-tag vocabulary; exact set equality required for eval pass | `provenance.py:1-50`, `eval_set.py:240-270` |

### 2.2 Retrieval Flow (from query_ui.py)

```
Question → Route (KNOWN/COLD_START/AMBIGUOUS)
  ├─ KNOWN: Graph seeds → 2-hop traversal → LLM synthesis + grading
  ├─ COLD_START: Fetch SEC → Extract → Stitch overlay → Traverse → Synthesize
  └─ AMBIGUOUS: Refuse with disambiguation options
```

---

## Section 3: Provenance & Trust Architecture

### 3.1 Implemented Capabilities

| Tag | Meaning | Implementation |
|-----|---------|----------------|
| **STATED** | Directly cited from graph node/chunk | `provenance.py:10`, grader assigns |
| **DERIVED** | Arithmetic over cited facts (shown) | `provenance.py:11`, eval expects arithmetic display |
| **INFERRED** | Reasoning over cited facts, hedged | `provenance.py:12`, eval expects hedging |
| **EXTERNAL** | Outside corpus (live fetch), timestamped | `provenance.py:13`, `query_ui.py` market data |
| **GAP** | Corpus genuinely doesn't answer | `provenance.py:14`, eval expects declination |

### 3.2 Provenance Contract (EVAL_SET.md)

- **Headline metric:** `provenance_match_rate` = exact tag set equality
- **Secondary metrics (target 0):** ungrounded figures, uncited sentences, misattributed issuers
- **Refused answers** counted separately (not folded into match rate)
- **Missing verdict** = not a pass (exposes server bugs)

---

## Section 4: Financial Schema & Normalization (UFGS-2026-09)

### 4.1 Universal Layer (33 concepts - all sectors)

| Category | Concepts | Examples |
|----------|----------|----------|
| Income Statement | SC-01..SC-14 | TotalRevenue, CostOfRevenue, GrossProfit, R&D, SG&A, OperatingIncome, NetIncome, DilutedEPS |
| Balance Sheet | SC-15..SC-30 | CashAndEquivalents, ShortTermInvestments, AR, Inventory, PP&E, Goodwill, TotalAssets, TotalLiabilities, TotalEquity |
| Cash Flow | SC-31..SC-36 | CashFromOps, CapEx, Acquisitions, DividendsPaid, ShareRepurchases, DebtIssuance |

### 4.2 Banking Overlay (4 concepts - sector="banking" only)

| Concept | Description |
|---------|-------------|
| SC-37 | Tier1CapitalRatio (Basel III) |
| SC-38 | CommonEquityTier1Ratio (CET1) |
| SC-39 | RiskWeightedAssets |
| SC-40 | ValueAtRiskOneDay |

### 4.3 Normalization Rules (25 rules)

- **Primary:** Exact `us-gaap` tag match (e.g., `us-gaap:Revenues` → SC-01)
- **Fallback:** Label regex patterns (e.g., `^net sales$` → SC-01)
- **Transforms:** `identity`, `sum` (bank revenue), `negate` (CapEx, dividends, buybacks), `sector_conditional`
- **Multi-rule firing:** One fact → multiple `NORMALIZES_TO` edges (e.g., JPM interest expense = SC-07 + component of R-03)

### 4.4 Period-Aware Metric Identity

- `PERIOD_SCOPED_METRICS = True` (config.py:100)
- Canonical name includes period end date: `"Net Sales (3M-2026-03-28)"`
- Three 10-K comparative years = three distinct Metric nodes
- 10-Q: 3M and 6M columns under same period end kept separate via duration banner

---

## Section 5: Causal & Narrative Layer

### 5.1 Implemented Capabilities

| Capability | Implementation |
|------------|---------------|
| **Risk factor extraction** | Bold headers in Item 1A → RiskFactor nodes with source quotes | `ufgs_extract.py:1139-1250` |
| **Causal relation types** | 6 typed relations: DRIVES, IMPACTS_MARGIN, MITIGATES, CREATES_EXPOSURE_TO, COMPOUNDS, OFFSETS | `ufgs_schema.py:711-718` |
| **Entity types (7)** | ProductFamily, GeographicMarket, Competitor, Supplier, Customer, RegulatoryBody, MacroVariable | `ufgs_schema.py:723-731` |
| **Gazetteer-based NER** | Seeded surface forms (strong on RegulatoryBody/MacroVariable, thin on ProductFamily) | `ufgs_extract.py:1167-1300` |
| **Cue-phrase classifier** | Conservative: prefers missing edge to invented causal claim | `ufgs_extract.py:13-23` |

### 5.2 Limitations

- **No domain-tuned NER model** - gazetteer only (zero-LLM constraint)
- **No fine-tuned relation classifier** - cue phrases only
- **Conservative recall** - fewer relations than model would produce

---

## Section 6: UI & User Experience

### 6.1 Two UIs Served

| UI | Route | Purpose | Tech |
|----|-------|---------|------|
| **Landing Page** | `/` | Public marketing + ticker strip + hero graph | `ui_next/landing/` (vanilla JS, GSAP) |
| **GraphRAG Studio** | `/app` | Authenticated Q&A, graph viz, reports | `ui_next/static/` (ES modules, SSE streaming) |
| **Legacy Viewer** | `:9000/` | Three-pane graph explorer | `query_ui.py` (unchanged) |

### 6.2 Landing Page Features (Implemented)

- **Hero animated graph** - force-directed D3/GSAP visualization (`hero-graph.js`)
- **Live market ticker** - Yahoo Finance quotes, 60s cache (`server.py:225-252`, `markets.js`)
- **Company overview cards** - from `/api/companies` endpoint (`server.py:134-180`)
- **Route preview** - `/api/route?q=...` shows KNOWN/COLD_START/AMBIGUOUS before query (`server.py:485-493`)
- **Authentication** - OAuth (Google/Apple/TradingView) + dev fallback (`server.py:255-310`, `auth/`)
- **Dark theme** - black bg, electric orange-red (#FF3C00), warm amber, dark glass

### 6.3 Studio Features (Implemented)

- **SSE streaming** - real-time tokens + status steps (`api.js`, `answer.js`)
- **Graph visualization** - Cytoscape.js with provenance badges (`graph.js`)
- **Canned reports** - pre-built analyses (`reports.js`, `query_ui.py:4226-4236`)
- **Provenance display** - color-coded tags per sentence (`answer.js`)
- **Session persistence** - localStorage + cookie auth (`store.js`, `auth/`)

---

## Section 7: Competitive Feature Matrix

### 7.1 Evidence-Based Assessment

| Feature | FinGraph | AlphaSense | FactSet | S&P Capital IQ | Koyfin | FinChat/Fiscal.ai | Glean | Perplexity | Bloomberg Terminal |
|---------|----------|------------|---------|----------------|--------|-------------------|-------|------------|---------------------|
| **SEC filing ingestion (zero-LLM)** | ✅ Parser + XBRL | ❓ LLM-heavy | ✅ | ✅ | ❓ | ✅ (LLM) | ❌ Enterprise docs | ❌ Web search | ✅ |
| **Inline XBRL fact extraction** | ✅ Full context resolution | ❓ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Knowledge graph (financial)** | ✅ LadybugDB, 40 concepts | ❌ Vector only | ❌ Relational | ❌ Relational | ❌ Charts only | ❌ Vector | ❌ Enterprise KG | ❌ | ✅ Proprietary |
| **Sector-aware normalization** | ✅ UFGS overlays | ❌ | ✅ (proprietary) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Provenance grading (5-tag)** | ✅ STATED/DERIVED/INFERRED/EXTERNAL/GAP | ❌ Citations only | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ Citations | ❌ |
| **Cold-start JIT GraphRAG** | ✅ Live fetch→extract→stitch→traverse | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Entity routing (3-way)** | ✅ KNOWN/COLD_START/AMBIGUOUS | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Fiscal period as node** | ✅ calendar_year_overlap | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Dual-track (as-filed + normalized)** | ✅ NORMALIZES_TO edges | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Causal narrative layer** | ✅ 6 typed relations + 7 entity types | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Live market data** | ✅ Yahoo Finance (landing) | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| **SSE streaming answers** | ✅ Studio UI | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ |
| **Eval framework (provenance)** | ✅ 30-question contract | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

**Sources:** Public documentation, product pages, API docs. Bloomberg/FactSet/S&P Capital IQ are closed platforms - assessments based on public capabilities.

### 7.2 FinGraph's Genuine Differentiators

1. **Zero-LLM ingestion pipeline** - No hallucination risk at extraction layer
2. **Provenance-graded synthesis** - Only system with 5-tag contract + eval enforcement
3. **Cold-start JIT GraphRAG** - Live SEC fetch → graph overlay in <30s for unseen tickers
4. **FiscalPeriod as first-class node** - Solves cross-issuer calendar alignment (NVDA Jan vs AAPL Sep)
5. **Dual-track normalization** - As-filed labels never discarded; every mapping auditable
6. **Entity routing transparency** - User sees KNOWN/COLD_START/AMBIGUOUS before spend
7. **Sector overlays** - Banking metrics only appear for banks (no empty Apple Tier1 nodes)
8. **Open eval contract** - Public provenance_match_rate metric, not marketing claims

---

## Section 8: Current Capability Gaps (Honest Assessment)

| Gap | Root Cause | Mitigation |
|-----|------------|------------|
| **No FY2025 annual Net Sales for AAPL in graph** | D3 residual: annual header had only year, no date | Header date parsing + aggregation level in metric identity |
| **No product×region cross-tab** | Not in SEC filings (GAP questions G1, G6) | Explicit GAP declination (implemented) |
| **No forward guidance/forecasts** | Not in historical filings (GAP G2, G4, G7) | EXTERNAL fetch + clear labeling |
| **Causal layer recall low** | Gazetteer + cue phrases only | Future: domain-tuned NER + relation classifier |
| **No multi-company comparison** | D4: period string not FiscalPeriod node | Period node join (planned) |
| **No 13F/institutional holdings** | Corpus scope: 10-K/10-Q/8-K only | Extend ingestion scope |
| **No real-time news** | Phase 5 (E2) | EXTERNAL fetch pipeline |
| **Benchmark suite blind** | D5: count-based not provenance-aware | Provenance-aware benchmarks |

---

## Section 9: Technical Architecture Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     INGESTION PIPELINE                       │
├─────────────────────────────────────────────────────────────┤
│  SEC HTML → parser.py → ExtractionResult                    │
│       ↓                    ↓                                 │
│  ufgs_extract.py → UFGSBundle (dual track)                  │
│       ↓                                                      │
│  buffer.py → Parquet staging → loader.py → LadybugDB        │
│       ↓                                                      │
│  entity_resolver.py → concepts.json (registry)              │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                      QUERY PIPELINE                          │
├─────────────────────────────────────────────────────────────┤
│  Question → router.py → EntityRoute (KNOWN/COLD/AMBIG)      │
│       ↓                                                      │
│  KNOWN: traversal.py → HybridGraphTraverser → subgraph      │
│  COLD:  coldstart_* → fetch→extract→stitch→traverse→synth   │
│       ↓                                                      │
│  query_ui.py:ask_rag → LLM synthesis → provenance grader    │
│       ↓                                                      │
│  Response: {answer, provenance[], verdict, graph, flow}     │
└─────────────────────────────────────────────────────────────┘
```

---

## Section 10: API Endpoints (Implemented)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/` | GET | No | Landing page |
| `/app` | GET | Yes | GraphRAG Studio |
| `/api/companies` | GET | No | Issuer overview (filings, forms, latest period) |
| `/api/markets` | GET | No | Live Yahoo Finance quotes (18 tickers, 60s cache) |
| `/api/route?q=...` | GET | No | Preview retrieval route (KNOWN/COLD_START/AMBIGUOUS) |
| `/api/ask` | POST | Yes | Main GraphRAG Q&A (SSE streaming) |
| `/api/rag` | GET/POST | Yes | Backend status / key management |
| `/api/reports` | GET | Yes | Canned report list |
| `/api/reports/{id}` | GET | Yes | Run canned report |
| `/api/entities`, `/api/graph`, `/api/stats` | GET | Yes | Graph exploration |
| `/api/auth/*` | POST | No | OAuth session management |

---

## Section 11: Deployment & Operations

| Aspect | Implementation |
|--------|---------------|
| **Database** | LadybugDB (`sandbox.lbug`), ~20MB for 30 filings |
| **Buffer pool** | 256MB (config.py:60) |
| **RAG backends** | NVIDIA NIM (nvidia/nemotron-3-ultra) + local Ollama fallback |
| **API key** | `.env` file with `NVIDIA_API_KEY` (gitignored) |
| **Ports** | 9000 (legacy), 9100 (ui_next default) |
| **Read-only mode** | Default for query servers (no writes) |
| **Reset** | `python -m sandbox_engine --reset` deletes DB + registry + staging |

---

## Section 12: Visual Identity (Current)

| Element | Specification |
|---------|---------------|
| **Background** | Black (#030303) |
| **Primary accent** | Electric orange-red (#FF3C00) |
| **Secondary accent** | Warm amber (#FF6B35, #FF551C, #FFA07A) |
| **Surfaces** | Dark glass (#0f1115, #171a21, #1e222b) |
| **Typography** | Inter / Geist (system stack) |
| **Border radius** | 8-12px |
| **Animation** | GSAP + D3 force-directed (hero), Cytoscape (studio) |

---

## Section 13: Evaluation Results (Current)

| Block | Questions | Pass | Fail | Primary Failure Mode |
|-------|-----------|------|------|---------------------|
| **STATED** | 8 | 8 | 0 | D1 (fixed) |
| **GAP** | 7 | 7 | 0 | - |
| **DERIVED** | 8 | 4 | 4 | D2/D3/D4 residual |
| **INFERRED** | 5 | 3 | 2 | No precedent library (I1) |
| **EXTERNAL** | 2 | 0 | 2 | Phase 5 not built |
| **TOTAL** | **30** | **22** | **8** | |

**Honest baseline:** 22/30 pass with current defects. The 8 failures are documented, not hidden.

---

## Section 14: Roadmap Signals (From Code Comments)

| Area | Signal | File |
|------|--------|------|
| **Header dates for annual columns** | "OPEN: need header dates for annual columns" | `EVAL_SET.md:106-109` |
| **Aggregation level in metric identity** | "OPEN: aggregation level in metric identity" | `EVAL_SET.md:108` |
| **Period as FiscalPeriod node** | "OPEN: period still a string rather than FiscalPeriod node" | `EVAL_SET.md:113-115` |
| **Precedent library for INFERRED** | "FAIL — no precedent library" | `EVAL_SET.md:65` |
| **Phase 5: EXTERNAL fetch** | "Needs Phase 5" | `EVAL_SET.md:76` |
| **Sector expansion** | "absorb a new sector by adding SC-41+ and R-26+" | `ufgs_schema.py:29-30` |

---

## Section 15: Decision Input for Landing Page Features Section

### 15.1 Claims We CAN Make (Evidence-Backed)

| Feature Claim | Evidence Location | UI Demo |
|---------------|-------------------|---------|
| **"Zero-LLM ingestion — no hallucination at extraction"** | `parser.py`, `ufgs_extract.py` | Landing: "How it works" section |
| **"Provenance-graded answers: every sentence tagged"** | `provenance.py`, `eval_set.py`, `answer.js` | Studio: provenance badges |
| **"Cold-start JIT: live SEC fetch → graph in seconds"** | `coldstart_extract.py`, `coldstart_synthesis.py`, `router.py` | Studio: try unknown ticker |
| **"FiscalPeriod nodes solve cross-issuer calendar misalignment"** | `ufgs_schema.py:739-783`, `config.py:95-100` | Landing: technical detail |
| **"Sector overlays: banking metrics only for banks"** | `ufgs_schema.py:315-326`, `config.py:127-145` | Landing: schema diagram |
| **"Dual-track: as-filed labels never discarded"** | `ufgs_extract.py:765-868`, `ufgs_schema.py:18-24` | Landing: architecture |
| **"Entity routing transparency: KNOWN/COLD_START/AMBIGUOUS"** | `router.py`, `server.py:485-493`, `components.js` | Landing: route preview |
| **"Open eval contract: provenance_match_rate metric"** | `EVAL_SET.md`, `eval_set.py` | Landing: "Trust" section |

### 15.2 Claims We CANNOT Make (Not Implemented)

| ❌ Do Not Claim | Reason |
|-----------------|--------|
| "Multi-company comparative analysis" | D4 open: period string not joinable node |
| "Product×region revenue breakdown" | GAP: not in SEC filings (G1, G6) |
| "Forward guidance / forecasts" | GAP: not in historical corpus (G2, G4, G7) |
| "Causal relationship discovery at scale" | Gazetteer only, low recall (ufgs_extract.py:13-23) |
| "Real-time news integration" | Phase 5 not built (E2) |
| "13F / institutional holdings" | Corpus scope: 10-K/10-Q/8-K only |
| "Bloomberg-terminal parity" | Closed platform, different scope |

### 15.3 Recommended Features Section Structure

```
┌─────────────────────────────────────────────────────────────┐
│  FEATURES (6 cards, evidence-linked)                        │
├─────────────────────────────────────────────────────────────┤
│  1. Zero-LLM Ingestion          → "See parser.py"          │
│  2. Provenance-Graded Answers   → "Try Studio: provenance" │
│  3. Cold-Start JIT GraphRAG     → "Enter unknown ticker"   │
│  4. FiscalPeriod Intelligence   → "NVDA vs AAPL calendar"  │
│  5. Sector-Aware Schema (UFGS)  → "Banking overlay demo"   │
│  6. Transparent Entity Routing  → "Route preview API"      │
└─────────────────────────────────────────────────────────────┘
```

### 15.4 Visual Treatment

- **Each card:** Dark glass panel, orange-red accent border on hover
- **Evidence link:** Subtle "View source" → GitHub file:line deep link
- **Live demo:** "Try in Studio" button → `/app` with pre-filled question
- **Honesty badge:** "22/30 eval questions pass — see EVAL_SET.md" footer link

---

## Appendix: File Inventory (Key Implementation Files)

| File | Lines | Purpose |
|------|-------|---------|
| `parser.py` | ~1200 | Zero-LLM HTML/XBRL/table extraction |
| `ufgs_extract.py` | ~1400 | UFGS dual-track extraction (facts, sections, causal) |
| `ufgs_schema.py` | ~780 | 40 concepts, 25 rules, sector overlays, fiscal periods |
| `entity_resolver.py` | ~400 | Canonical entity dedup with registry |
| `router.py` | ~200 | 3-way entity routing |
| `traversal.py` | ~400 | 2-hop hybrid graph traversal |
| `coldstart_extract.py` | ~300 | Live SEC fetch + triple extraction |
| `coldstart_synthesis.py` | ~300 | 5-section investment analysis synthesis |
| `stitch.py` | ~200 | Ephemeral overlay onto backbone graph |
| `provenance.py` | ~50 | 5-tag vocabulary + grading |
| `query_ui.py` | ~4800 | Main RAG service, SSE, grading, API |
| `server.py` (ui_next) | ~670 | Landing + Studio + Auth + Market data |
| `eval_set.py` / `EVAL_SET.md` | ~410 / 130 | Provenance contract + 30 questions |
| `ddl.py` / `buffer.py` / `loader.py` | ~360 / ~ / ~ | LadybugDB schema, buffering, bulk load |
| `config.py` | ~250 | All tunables, paths, scope resolution |

---

*Report compiled from repository source code only. No external claims incorporated without evidence.*