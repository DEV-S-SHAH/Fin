# India Company Graph — Consolidated Design

**Status:** All design decisions settled (Rounds 1–5 complete).  
**Relationship to FinGraph:** Fresh India-first product that reuses FinGraph's *patterns* (provenance tags, company isolation, graph traversal, fiscal calendars) but with its own India data model and ephemeral DB. No existing India/NSE/BSE/MCA/SEBI integration exists in FinGraph — this is a new build.

---

## 1. Product Strategy (Round 1)

| Decision | Value |
|---|---|
| New product vs extend FinGraph | Fresh India-first product reusing FinGraph patterns |
| Data persistence | Ephemeral per-session graph; JIT build; destroyed on session end / TTL |
| Source priority | NSE + BSE → MCA (CIN) → SEBI / gov PDFs |
| Canonical company ID | MCA CIN; NSE/BSE symbol + ISIN as aliases |
| Primary UX | Chat / natural-language Q&A over the graph |
| Scope | India only, single region (no US+India multi-region yet) |

---

## 2. Graph & Storage (Round 2)

| Decision | Value |
|---|---|
| Graph store | In-memory graph (NetworkX) per session |
| NSE/BSE scraping | Scrape NSE/BSE directly as primary; Yahoo Finance / screener.in as fallback |
| Ticker → CIN resolver | Built once at setup from MCA master + exchange directories; cached; refreshed weekly; MCA live as fallback |
| LLM role | LLM synthesizes answers from graph-gathered evidence with provenance tags; graph construction is deterministic (scraper → structured records → graph) |
| Graph lifetime | Destroyed after session TTL (30 min inactivity) or explicit logout — not after every question |
| Fiscal calendar | Default FYE = March 31; per-company override; INT64 fiscal_year + DATE filing_date |

---

## 3. Q&A Model & Isolation (Round 3)

| Decision | Value |
|---|---|
| Provenance per source | NSE/BSE announcements & financials = STATED; MCA master = STATED; Yahoo Finance/screener = EXTERNAL; calculated ratios = DERIVED; unhedged forward claims = GAP |
| Company isolation | One company (one CIN) per session graph; cross-company questions → GAP / refused |
| JIT build trigger | Lazily on the user's first question |
| Company resolution flow | User picks a company first → system resolves to CIN → Q&A over that company's graph |
| Chat UI | Streamlit |
| Initial scraping targets | NSE corporate announcements + financial results; BSE announcements; MCA master via CIN. Skip annual reports & SEBI PDFs (Phase 2). |

---

## 4. Scraper & Session Implementation (Round 4)

| Decision | Value |
|---|---|
| Scraper architecture | One scraper module per exchange (NSE, BSE, MCA) with a shared interface (fetch_company, fetch_announcements, fetch_financials, normalize) |
| NSE anti-bot | Rotate User-Agents + jittered delays + respect Retry-After + single jittered retry on 429 |
| Ticker→CIN build | Build once at setup, cache as local lookup, refresh weekly; MCA live fallback for unresolved |
| Session management | In-memory dict keyed by session ID with last-accessed timestamp; background sweeper destroys after TTL |
| Synthesis + grading | Reuse FinGraph's build_evidence() + grade_answer() pattern; GAP → REFUSED; misattribution → GAP |
| Tech stack | Python throughout (scrapers + NetworkX graph + Streamlit UI) |

---

## 5. Deployment, Security, Tests, Launch (Round 5)

| Decision | Value |
|---|---|
| Deployment | localhost for now (python -m streamlit run); move to Railway/Render later |
| Persistence boundary | Per-session graph = ephemeral (nothing persists between sessions); ticker→CIN resolver cache = small persistent file (metadata only, refreshed weekly) |
| Security | SSRF allow-list (nseindia.com, bseindia.com, mca.gov.in, sebi.gov.in only); env-based secrets; per-IP rate limiting |
| Testing | Mocked scraper unit tests + golden-queries test suite on every deploy; provenance/grading tests added later |
| Launch scope | NIFTY 50 on day one |
| Graph destruction | After 30 min inactivity (background sweeper) or explicit logout |

---

## 6. Data Flow

```
User picks company (ticker/name)
    │
    ▼
Resolve ticker → CIN (cached resolver; MCA fallback)
    │
    ▼ (first question triggers JIT build)
Scrape NSE/BSE announcements + financials + MCA master (deterministic, per-source provenance)
    │
    ▼
Normalize to common records → in-memory NetworkX graph (one company, one session)
    │
    ▼
LLM synthesizes answer from graph evidence, tagged STATED / DERIVED / INFERRED / EXTERNAL / GAP
    │
    ▼
grade_answer() checks figures against cited evidence only; GAP → REFUSED; misattribution → GAP
    │
    ▼
Streamlit UI returns answer with citations
    │
    ▼ (30 min inactivity or logout)
Session graph destroyed; nothing persists
```

---

## 7. Files to Create Next (Implementation Order)

1. `india_graph/scrapers/nse.py` — NSE announcements + financials scraper
2. `india_graph/scrapers/bse.py` — BSE announcements scraper
3. `india_graph/scrapers/mca.py` — MCA master data via CIN
4. `india_graph/resolver.py` — ticker → CIN resolver (cached)
5. `india_graph/graph.py` — in-memory NetworkX session graph builder
6. `india_graph/session.py` — session dict + TTL sweeper
7. `india_graph/provenance.py` — build_evidence + grade_answer (adapted from FinGraph)
8. `india_graph/ui.py` — Streamlit chat UI
9. `india_graph/config.py` — SSRF allow-list, FYE defaults, TTL, NIFTY 50 list
10. `tests/` — mocked scraper tests + golden queries

---

**Next step:** Pick a deployment platform (Railway vs Render) and begin implementing `india_graph/scrapers/nse.py`. Want me to start on any specific module?