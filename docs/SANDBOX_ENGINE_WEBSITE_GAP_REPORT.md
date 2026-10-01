# Sandbox Engine → FinGraph Website Gap Analysis Report

**Prepared:** 2026-09-30  
**Scope:** Analyze sandbox_engine architecture vs. current FinGraph React website; produce actionable gap report.  
**Constraint:** No source code, routes, database, APIs, styling, or configuration modifications — analysis only.

---

## A. Executive Summary

The `sandbox_engine` is a **production-grade SEC ingestion + GraphRAG query engine** with capabilities that far exceed the current FinGraph website's feature set. The website today is a marketing/landing shell (React + Vite + Tailwind) with:
- Landing page, auth, pricing, live markets ticker
- Demo GraphRAG page (`/demo`) with static mock data
- No real ingestion pipeline, no live graph queries, no provenance, no cold-start JIT, no three-pane studio

The `sandbox_engine` delivers:
- **Zero-LLM HTML parsing** of SEC filings (10-K, 10-Q, 8-K, etc.) via `parser.py` + `tier1_clean.py`
- **Entity resolution & UFGS mapping** (`entity_resolver.py`, `ufgs_*`) to GAAP/IFRS concepts
- **LadybugDB-backed property graph** (`ddl.py`, `loader.py`) with Companies, Filings, Metrics, Segments, Footnotes
- **Three retrieval routes** (`router.py`): KNOWN (graph), COLD_START (live fetch+synthesize), AMBIGUOUS
- **Cold-Start JIT GraphRAG** (`coldstart_*`) — builds subgraph on-the-fly for unseen tickers/questions
- **Provenance grading** (`provenance.py`) — A/B/C/D evidence grades per claim
- **Three-pane Studio UI** (`query_ui.py`, `ui_next/static/`) — Entity Browser, Force-Directed Graph, NL Question + Streaming Answer

**Gap Verdict:** The website exposes <10% of sandbox_engine's value. Closing the gap requires wiring the engine's **read APIs** into the React frontend, adding **auth-gated Studio**, **live ingestion status**, **provenance UI**, and **market-data integration** — all without touching engine internals.

---

## B. Sandbox Engine Capability Map

| Module | Capability | Exposed API (ui_next/server.py) | Website Equivalent |
|--------|------------|----------------------------------|---------------------|
| `parser.py` + `tier1_*` | SEC HTML → structured JSON (sections, tables, XBRL facts) | — (batch CLI only) | ❌ None |
| `entity_resolver.py` | Fuzzy ticker/name → canonical CIK + UFGS concept IDs | — | ❌ None |
| `ufgs_schema.py` / `ufgs_extract.py` | GAAP/IFRS concept taxonomy + extraction | — | ❌ None |
| `ddl.py` / `loader.py` / `buffer.py` | LadybugDB schema + bulk load (Companies, Filings, Metrics, Segments, Footnotes, Relationships) | — | ❌ None |
| `ingestion.py` | End-to-end filing ingestion orchestration | — | ❌ None |
| `router.py` | `route_query(question, kg)` → `{route: KNOWN|COLD_START|AMBIGUOUS, ticker}` | `GET /api/route?q=...` | ❌ None |
| `coldstart_extract.py` + `coldstart_synthesis.py` | Live SEC fetch → subgraph build → answer synthesis | Via `/api/rag` (legacy handler) | ❌ None |
| `stitch.py` | Cross-filing entity stitching (same company across periods) | — | ❌ None |
| `provenance.py` | Evidence grading (A=primary source, B=derived, C=inferred, D=hallucination risk) | Embedded in `/api/rag` SSE stream | ❌ None |
| `traversal.py` | Graph traversal helpers (neighbors, paths, subgraph extraction) | Via `/api/graph`, `/api/entities` | ❌ None |
| `community.py` | Louvain community detection on metric co-occurrence | — | ❌ None |
| `query_ui.py` (legacy) | Three-pane Studio: Entity Browser / Force Graph / NL QA + SSE | `/api/stats`, `/api/entities`, `/api/graph`, `/api/rag`, `/api/reports` | ❌ Demo page only (mock) |
| `ui_next/server.py` | **New endpoints**: `/api/companies`, `/api/markets`, `/api/route`, `/api/auth/*` | ✅ All implemented | ⚠️ Partial (markets only on landing) |
| `cli.py` | Commands: `--reset`, `--ingest`, `--stats`, `--serve`, `--bench` | — | ❌ None |

**Key Insight:** The engine already serves **read-only HTTP APIs** via `ui_next/server.py` (subclassing legacy handler). The React website needs only to **consume these endpoints** — no engine modification required.

---

## C. Current Website Capability Map

| Page / Feature | Tech | Data Source | Gaps vs. Engine |
|----------------|------|-------------|-----------------|
| Landing (`/`) | React + Tailwind | Static content + `markets.js` (mock) | No real `/api/markets` integration; no company overview from graph |
| Auth (`/auth`) | React context + mock OAuth | localStorage mock | No real `/api/auth/*` integration; no session cookie handling |
| Pricing (`/#pricing`) | Static sections | Hardcoded tiers | No entitlement checks against engine tiers |
| Live Markets (`/live-markets`) | React + `gradient-bars-background` | Mock ticker data | Should consume `/api/markets` + `/api/companies` |
| GraphRAG Demo (`/demo`) | React + static mock | Hardcoded JSON | **Complete mock** — no `/api/rag`, `/api/graph`, `/api/entities`, `/api/route` |
| Navigation | React Router | — | No `/app` (Studio) route; no auth-gated routes |
| Styling | Tailwind + custom CSS (`index.css`) | Black/near-black, #FF3C00 accent, glow, data-wave | Matches engine's `ui_next` design language — **keep** |

**Critical Missing Pages:** `/app` (GraphRAG Studio), `/ingestion` (pipeline status), `/reports` (provenance-graded answers), `/admin` (ingestion triggers — auth-gated).

---

## D. Missing Functionality (Engine → Website)

### D.1 Data & Ingestion Layer
| Missing | Engine Source | Website Need |
|---------|---------------|--------------|
| Filing ingestion status & history | `ingestion.py`, `buffer.py` | `/ingestion` page: queue, progress, errors, last-success per ticker |
| Company/filing catalog from graph | `companies(kg)` in `server.py` | Landing "Companies in Graph" section; Studio entity browser source |
| UFGS concept taxonomy browse | `ufgs_schema.py` | Studio sidebar: searchable concept picker (Revenue, Assets, etc.) |
| Cross-filing stitching results | `stitch.py` | Studio: "Show me Revenue across all years" → stitched time series |

### D.2 Query & Retrieval Layer
| Missing | Engine Source | Website Need |
|---------|---------------|--------------|
| Route prediction (KNOWN/COLD_START/AMBIGUOUS) | `router.py` → `/api/route` | Studio: show route badge before query executes |
| KNOWN-route graph traversal | `traversal.py` → `/api/graph`, `/api/entities` | Studio: force-directed subgraph, entity cards |
| COLD_START-route live fetch + synthesis | `coldstart_*` → `/api/rag` (SSE) | Studio: streaming answer with "Fetching live filing…" progress |
| Provenance grades per claim | `provenance.py` → embedded in SSE | Studio: inline evidence badges (A/B/C/D), expandable source viewer |
| Community detection (metric clusters) | `community.py` | Studio: "Related Metrics" cluster pills |

### D.3 Auth & Entitlements
| Missing | Engine Source | Website Need |
|---------|---------------|--------------|
| OAuth session cookies (Google/Apple/TradingView) | `server.py` `_api_auth_session` | Auth context + protected routes (`/app`, `/ingestion`, `/reports`) |
| Dev-mode fallback session | `server.py` `provider="dev"` | Local dev without OAuth keys |
| Tier entitlements (Free/Pro/Enterprise) | Not in engine — **design gap** | Pricing page → feature gates (Cold-Start daily limit, Studio access) |

### D.4 Real-Time & Market Data
| Missing | Engine Source | Website Need |
|---------|---------------|--------------|
| Live quotes (Yahoo Finance, 1-min cache) | `markets()` → `/api/markets` | Landing ticker strip + Live Markets page (replace mock) |
| Graph-backed company list for markets | `companies(kg)` → `/api/companies` | Landing: "Companies in Graph" chips; Studio: entity browser source |

---

## E. Recommended Features to Add (Website)

### E.1 **GraphRAG Studio (`/app`)** — **P0**
Three-pane layout mirroring `ui_next/static/index.html`:
1. **Left — Entity Browser**: Virtualized list from `/api/entities?q=` + `/api/companies`; filter by type (Company, Metric, Filing, Segment); click → centers graph + loads detail.
2. **Center — Force-Directed Graph**: D3/Canvas from `/api/graph?center=<eid>&depth=2`; nodes colored by type; edges labeled by relationship; hover → tooltip with provenance grade.
3. **Right — Natural Language QA**: Question input → `GET /api/route?q=` shows route badge → `POST /api/rag` (SSE) streams answer with inline citations → each citation shows provenance grade badge (A/B/C/D) + expandable source snippet (filing section, table, footnote).

**Reuse:** `ui_next/static/` already has `graph.js` (D3), `answer.js` (SSE + citations), `store.js` (state), `api.js` (fetch wrappers). **Port to React components** — do not rewrite logic.

### E.2 **Ingestion Dashboard (`/ingestion`)** — **P1**
- Table: Ticker | Form | Fiscal Year/Period | Status (Queued/Running/Done/Error) | Started | Completed | Error Detail
- Poll `/api/ingestion/status` (new endpoint needed — see §F) or tail engine logs via SSE
- "Trigger Ingestion" button (auth-gated, Pro+) → POST `/api/ingestion/trigger` {ticker, forms[]}

### E.3 **Provenance Report Viewer (`/reports/:id`)** — **P1**
- Read-only view of a completed QA session (persisted via `/api/reports` in legacy handler)
- Render: Question, Route, Answer, **Evidence Table** (Claim | Grade | Source Filing | Section | Snippet | Confidence)
- Export PDF/Markdown

### E.4 **Company Profile Pages (`/company/:ticker`)** — **P2**
- Hero: live quote from `/api/markets` + key metrics from graph (latest Revenue, Assets, Cash Flow)
- Filings timeline (from `/api/companies` periods)
- Metric explorer: searchable UFGS concepts → time-series chart (stitched via `stitch.py`)
- "Ask about this company" → deep link to Studio with pre-filled ticker context

### E.5 **Landing Page Enhancements** — **P2**
- "Companies in Graph" chip cloud from `/api/companies` (click → `/company/:ticker`)
- Live ticker strip from `/api/markets` (replace `markets.js` mock)
- Route predictor demo: text input → calls `/api/route` → shows badge + explanation

---

## F. Required API / Integration Changes

| Endpoint | Current | Required Change | Owner |
|----------|---------|-----------------|-------|
| `GET /api/companies` | ✅ Exists in `ui_next/server.py` | Consume in React (Landing, Studio, Company pages) | Frontend |
| `GET /api/markets` | ✅ Exists | Replace mock `markets.js` with real fetch + 60s SWR cache | Frontend |
| `GET /api/route?q=` | ✅ Exists | Studio: call before query; show route badge | Frontend |
| `GET /api/entities?q=&type=&limit=` | ✅ Legacy handler | Studio Entity Browser: debounced search, pagination | Frontend |
| `GET /api/graph?center=&depth=` | ✅ Legacy handler | Studio Force Graph: fetch subgraph on entity click | Frontend |
| `POST /api/rag` (SSE) | ✅ Legacy handler | Studio QA: stream answer, parse citations, render grades | Frontend |
| `GET /api/reports` / `GET /api/reports/:id` | ✅ Legacy handler | Reports page: list + detail view | Frontend |
| `GET /api/auth/config` | ✅ Exists | Auth context: show/hide OAuth buttons | Frontend |
| `POST /api/auth/session` | ✅ Exists | Auth context: exchange provider token → cookie | Frontend |
| `GET /api/auth/session` | ✅ Exists | Auth context: validate session on load | Frontend |
| `POST /api/auth/logout` | ✅ Exists | Auth context: clear cookie | Frontend |
| **NEW** `GET /api/ingestion/status` | ❌ | Ingestion dashboard: poll for queue state | **Engine** (add to `ui_next/server.py`) |
| **NEW** `POST /api/ingestion/trigger` | ❌ | Ingestion dashboard: enqueue filing fetch | **Engine** (add to `ui_next/server.py`) |
| **NEW** `GET /api/ufgs/concepts` | ❌ | Studio sidebar: concept taxonomy search | **Engine** (expose `ufgs_schema.py`) |

**Integration Pattern:** React `fetch`/`EventSource` → `ui_next` server (port 9100). **CORS:** Add `Access-Control-Allow-Origin: https://fingraph.app` (or `*` for dev) in `server.py` `_send()`. **Auth:** All `/api/*` except `/api/auth/*`, `/api/companies`, `/api/markets`, `/api/route` require valid session cookie (check in `_NextHandler` — add guard).

---

## G. Data Flow & Architecture Diagram

```mermaid
flowchart LR
    subgraph Browser[FinGraph React App (Port 3000/443)]
        Landing[Landing Page]
        Auth[Auth Context]
        Studio[GraphRAG Studio /app]
        Ingestion[Ingestion Dashboard /ingestion]
        Reports[Reports /reports/:id]
        Company[Company Profile /company/:ticker]
    end

    subgraph API[sandbox_engine.ui_next Server (Port 9100)]
        Static[Static Assets /landing, /static, /auth]
        Companies[GET /api/companies]
        Markets[GET /api/markets]
        Route[GET /api/route?q=]
        Entities[GET /api/entities]
        Graph[GET /api/graph]
        RAG[POST /api/rag SSE]
        ReportsAPI[GET /api/reports*]
        AuthAPI[/api/auth/*]
        IngestionAPI[/api/ingestion/* NEW]
        UFGS[/api/ufgs/concepts NEW]
    end

    subgraph Engine[Core sandbox_engine (Python)]
        KG[(LadybugDB\nKnowledgeGraph)]
        Router[router.py\nroute_query()]
        ColdStart[coldstart_*\nJIT GraphRAG]
        Provenance[provenance.py\nGrader]
        IngestionCore[ingestion.py\nPipeline]
        UFGSCore[ufgs_schema.py\nTaxonomy]
    end

    Landing --> Markets
    Landing --> Companies
    Auth --> AuthAPI
    Studio --> Route
    Studio --> Entities
    Studio --> Graph
    Studio --> RAG
    Studio --> UFGS
    Ingestion --> IngestionAPI
    Reports --> ReportsAPI
    Company --> Companies
    Company --> Markets

    Companies --> KG
    Markets --> YF[(Yahoo Finance)]
    Route --> Router
    Entities --> KG
    Graph --> KG
    RAG --> Router
    RAG --> ColdStart
    RAG --> Provenance
    ReportsAPI --> KG
    IngestionAPI --> IngestionCore
    IngestionCore --> KG
    UFGS --> UFGSCore
    ColdStart --> KG
    Provenance --> KG
```

**Data Flow Notes:**
1. **Read path (Studio QA):** Question → `/api/route` (sync, <50ms) → route badge shown → `POST /api/rag` (SSE) → engine routes to `KNOWN` (graph traversal) or `COLD_START` (live fetch → `coldstart_extract` → `coldstart_synthesis`) → streaming tokens + citations → `provenance.py` grades each citation → client renders.
2. **Write path (Ingestion):** User triggers → `/api/ingestion/trigger` → `ingestion.py` fetches SEC → `parser.py` → `entity_resolver.py` → `ufgs_extract.py` → `buffer.py` → `loader.py` → LadybugDB. Status polled via `/api/ingestion/status`.
3. **Auth:** OAuth redirect → `/auth/callback` (static page) → POST `/api/auth/session` {provider, token} → HttpOnly cookie set → all subsequent `/api/*` include cookie.

---

## H. Performance & Scalability Considerations

| Concern | Engine Behavior | Website Mitigation |
|---------|-----------------|---------------------|
| `/api/rag` SSE long-lived connections | One Python thread per connection (ThreadingHTTPServer) | **P0:** Deploy `ui_next` behind **gunicorn + gevent** or **uvicorn + FastAPI wrapper** for async SSE; or keep Python server for dev, proxy via **NGINX** with `proxy_buffering off` |
| Graph query latency (KNOWN route) | Cypher on LadybugDB — typically 10–200ms | React: SWR caching for `/api/entities`, `/api/graph`; optimistic UI |
| Cold-Start latency (live SEC fetch + LLM) | 5–30s depending on filing size + model | Studio: show progress steps (Fetch → Parse → Extract → Synthesize) via SSE events; timeout 60s |
| Market data freshness | 1-min cache in `markets()` | React: SWR `refreshInterval: 60000`; stale-while-revalidate |
| Ingestion throughput | Single-threaded CLI; `buffer.py` batches writes | **P1:** Add Celery/RQ worker queue for `/api/ingestion/trigger`; status via Redis |
| Concurrent users | `ThreadingHTTPServer` ~100 concurrent SSE | **P0:** Move to ASGI (FastAPI) + **Redis pub/sub** for SSE fan-out |

**Recommendation:** For production, **wrap `ui_next` in FastAPI** (preserving all handlers) to gain async, WebSocket/SSE scaling, OpenAPI schema, and dependency injection. The subclass pattern makes this a clean migration.

---

## I. Security & Compliance

| Area | Current Engine | Website Requirement |
|------|----------------|---------------------|
| **Authentication** | HMAC-signed session cookie (7-day TTL), HttpOnly, SameSite=Lax, Secure flag via env | React: store nothing in localStorage; rely on cookie; `fetch` with `credentials: 'include'` |
| **Authorization** | None (read-only graph) | **Add:** Role claim in session (Free/Pro/Enterprise) → middleware gates `/app`, `/ingestion`, `/reports` |
| **Rate Limiting** | None | **Add:** NGINX `limit_req_zone` on `/api/rag` (e.g., 10/min Free, 60/min Pro) |
| **CORS** | Not configured | **Add:** `Access-Control-Allow-Origin: <production-origin>` + `Allow-Credentials: true` |
| **Data Privacy** | Graph holds public SEC filings only | No PII risk; but session cookies must be `Secure` in prod |
| **Secret Management** | `FINGRAPH_AUTH_SECRET`, OAuth client IDs in env | **Never** commit; use Vercel/Netlify/Cloudflare secrets; rotate quarterly |
| **Audit Logging** | None | **Add:** Structured logs for `/api/rag` (question hash, route, latency, grade distribution) |

---

## J. Priority Matrix (P0 / P1 / P2)

| Priority | Item | Effort | Impact | Dependency |
|----------|------|--------|--------|------------|
| **P0** | Wire `/api/markets` + `/api/companies` into Landing & Live Markets | Low | High (immediate live data) | None |
| **P0** | Implement Auth context + OAuth flow (Google/Apple/TradingView + dev fallback) | Medium | High (gates all protected features) | `ui_next` auth endpoints exist |
| **P0** | Build **GraphRAG Studio (`/app`)** as React port of `ui_next/static/` | High | **Critical** (core product) | Reuse `graph.js`, `answer.js`, `api.js`, `store.js` |
| **P0** | Add CORS + cookie auth guard to `ui_next/server.py` | Low | High (security) | Engine change (allowed) |
| **P1** | Ingestion Dashboard (`/ingestion`) + `/api/ingestion/*` endpoints | Medium | High (ops visibility) | New engine endpoints |
| **P1** | Provenance Report Viewer (`/reports/:id`) | Medium | High (trust differentiator) | Legacy `/api/reports` exists |
| **P1** | Company Profile Pages (`/company/:ticker`) | Medium | Medium (SEO + engagement) | `/api/companies`, `/api/markets`, graph metrics |
| **P2** | UFGS Concept Taxonomy Sidebar in Studio | Low | Medium (power-user) | New `/api/ufgs/concepts` endpoint |
| **P2** | Landing Route Predictor Demo | Low | Medium (marketing) | `/api/route` exists |
| **P2** | Tier Entitlements (Free/Pro/Enterprise) + feature flags | Medium | Medium (monetization) | Design decision — not in engine |
| **P2** | Migrate `ui_next` to FastAPI for async SSE scaling | High | High (prod readiness) | Refactor, not feature |

---

## K. Implementation Order (Phased)

### Phase 0 — Foundation (Week 1)
1. **CORS + Auth Guard** in `ui_next/server.py` (30 min)
2. **React Auth Context** — consume `/api/auth/config`, `/api/auth/session`, `/api/auth/logout`; OAuth redirect flow; dev fallback
3. **SWR Hooks** for `/api/companies`, `/api/markets`, `/api/route` (reusable)
4. **Replace Landing mock markets** with real `/api/markets` + 60s SWR refresh
5. **Add "Companies in Graph" chip cloud** from `/api/companies` on Landing

### Phase 1 — GraphRAG Studio (Weeks 2–3)
6. **Route `/app`** in React Router (auth-gated)
7. **Port `ui_next/static/` to React components:**
   - `store.js` → React Context + `useReducer`
   - `api.js` → typed `fetch`/`EventSource` wrappers
   - `graph.js` → `ForceGraph` component (D3/Canvas, memoized)
   - `answer.js` → `StreamingAnswer` component (SSE parser, citation renderer, provenance badges)
   - `process.js` → `QueryProcess` (route badge, progress steps for COLD_START)
   - `reports.js` → `ReportHistory` sidebar
   - `components.js` → shared UI (EntityCard, MetricPill, GradeBadge)
8. **Entity Browser** — virtualized list (`react-window`) fed by `/api/entities`
9. **Integration test** against running `ui_next` server

### Phase 2 — Ingestion & Reports (Week 4)
10. **Add `/api/ingestion/status` + `/api/ingestion/trigger`** to `ui_next/server.py` (thin wrappers over `ingestion.py`)
11. **Build `/ingestion` page** — table + trigger modal + SSE status
12. **Build `/reports/:id` page** — consume `/api/reports/:id`, render evidence table with grade badges

### Phase 3 — Company Profiles & Polish (Week 5)
13. **Company Profile page** — hero quote, filings timeline, metric explorer (UFGS search → stitched time series via new `/api/metrics/:concept?ticker=` endpoint)
14. **UFGS Concept Sidebar** in Studio — add `/api/ufgs/concepts` endpoint (expose `ufgs_schema.py` concepts)
15. **Landing Route Predictor** — inline demo calling `/api/route`

### Phase 4 — Production Hardening (Week 6+)
16. **FastAPI migration** of `ui_next` (preserve handlers, gain async)
17. **Rate limiting, audit logging, tier entitlements**
18. **Load test** SSE concurrency; add Redis pub/sub if needed

---

## L. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **SSE scaling bottleneck** (ThreadingHTTPServer) | High | High (Studio unusable under load) | **P0:** FastAPI migration (Phase 4) or NGINX + gunicorn/gevent interim |
| **Auth cookie not sent cross-origin** (dev: localhost:3000 → localhost:9100) | Medium | High (auth broken) | Dev: run React on `:9100` via Vite proxy (`/api` → `http://localhost:9100`); Prod: same origin or `Access-Control-Allow-Credentials` |
| **Cold-Start latency > 30s causes SSE timeout** | Medium | Medium | Stream progress events (`stage: fetch|parse|extract|synthesize`); client shows spinner per stage |
| **Engine schema changes break React types** | Low | Medium | Generate TypeScript types from engine's Pydantic models (if any) or OpenAPI from FastAPI migration |
| **Yahoo Finance API rate limit / breakage** | Medium | Low (markets only) | Cache 1-min; fallback to cached data; monitor; consider paid provider later |
| **SEC HTML structure changes break parser** | Low | High (ingestion fails) | `parser.py` has fallback selectors; add integration test against live SEC; alert on ingestion errors |
| **Provenance grading ambiguity (B vs C)** | Medium | Medium (trust) | Document grading rubric in UI; allow user feedback "Was this helpful?" → log for model improvement |
| **No tier entitlements in engine** | High | Medium (monetization) | Implement in React middleware first; engine stays read-only; add `/api/entitlements` later |

---

## M. Final Roadmap Summary

| Phase | Weeks | Deliverable | Key Metrics |
|-------|-------|-------------|-------------|
| **0** | 1 | Auth + Live Markets + Company Chips | Auth works; landing shows real data |
| **1** | 2–3 | **GraphRAG Studio (`/app`)** — three-pane, streaming, provenance | Studio answers real questions from graph; grades visible |
| **2** | 4 | Ingestion Dashboard + Reports Viewer | Ops can trigger/Monitor ingestion; users can audit answers |
| **3** | 5 | Company Profiles + UFGS Sidebar + Route Predictor | SEO pages; power-user features; marketing demo |
| **4** | 6+ | FastAPI Migration + Rate Limiting + Tiers | Production-ready, scalable, monetizable |

**Total Estimated Effort:** 6 weeks for full parity + production hardening.  
**Minimum Viable Demo (Phase 0 + 1):** 3 weeks — Studio answering live questions with provenance.

---

## N. Highest-Priority Changes (Concise List)

1. **Add CORS + cookie auth guard to `ui_next/server.py`** (engine change — allowed per constraints)
2. **Build React Auth Context** consuming `/api/auth/*` endpoints
3. **Wire `/api/markets` + `/api/companies`** into Landing page (replace mocks)
4. **Port `ui_next/static/` → React Studio (`/app`)** — three panes, SSE streaming, provenance badges
5. **Add `/api/ingestion/status` + `/api/ingestion/trigger`** endpoints to `ui_next/server.py`
6. **Build Ingestion Dashboard (`/ingestion`)** + Reports Viewer (`/reports/:id`)
7. **Migrate `ui_next` to FastAPI** for async SSE scaling (production blocker)

---

**End of Report**