# FinGraph

Turns SEC filings into a property graph you can query, with a premium GraphRAG workspace and LLM question answering on top. No LLM runs on the way in — the parse stage is pure Python, so a build is deterministic and needs no API key. The model is only ever used to phrase an answer over retrieved graph context.

## Architecture

Two independent HTTP services over two pipelines, plus a React + TypeScript GraphRAG workspace, in one repository.

```
              30 SEC filings (committed, 39 MB)
        sandbox_engine/data/<company>/<year>/<form>/
                                    │
            python -m sandbox_engine --reset
              pure Python, ~11 s, no network
                                    │
              Arrow → Parquet spill → LadybugDB
                                    │
        sandbox_engine/_run/sandbox.lbug          data/<name>.lbug
                                    │                            │
                                    │                   python -m graphrag.cli ingest <pdf>
                                    │                            │
                                    ▼                            ▼
      python -m ui.fingraph              python -m graphrag.cli serve
        FinGraph UI (port 9100)          domain-agnostic PDF viewer
        landing + studio + auth          /api/ask → LLM answer, per request
        /api/ask → LLM answer            $PORT_GRAPHRAG_UI · 127.0.0.1:8765

      cd web && npm run dev
        Vite + React 19 + Tailwind 4
        Premium GraphRAG Studio at http://localhost:5173
        Proxies API calls to ui.fingraph on port 9100
```

| Service | Command | Port | Serves | Without a key |
|---|---|---|---|---|
| FinGraph UI (primary) | `python -m ui.fingraph` | 9100, `$PORT_QUERY_UI_V2` | landing, studio, auth | Explorer works; `/api/ask` asks for key or offers local model |
| Legacy GraphRAG Explorer | `python -m ui.legacy_graphrag` | 9000, `$PORT_QUERY_UI` | sandbox_engine/_run/sandbox.lbug | Explorer, reports, stats work |
| PDF Graph Viewer | `python -m ui.graphrag_web` | 8765, `$PORT_GRAPHRAG_UI` | data/*.lbug | Serves fine; answers fall back to lexical provider |
| GraphRAG Workspace (React) | `cd web && npm run dev` | 5173 (proxies to 9100) | sandbox_engine/_run/sandbox.lbug | Same as FinGraph UI |

All services bind to loopback (`127.0.0.1`). `--host 0.0.0.0` is an explicit choice.

## Quick Start

Requires **Python 3.13** and **Node.js 20+**. No database server, no Docker, no system packages.

### macOS / Linux

```bash
git clone https://github.com/DEV-S-SHAH/Fin.git
cd Fin

# Backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python setup.py                     # prompts for your NVIDIA key, verifies it
python -m sandbox_engine --reset    # build the graph, run 5 benchmarks

# Terminal 1: FinGraph UI (landing + studio + auth)
python -m ui.fingraph --port 9100 --no-browser

# Terminal 2: Frontend (GraphRAG Workspace - React)
cd web && npm install && npm run dev
# Opens http://localhost:5173
```

### Windows (PowerShell)

```powershell
git clone https://github.com/DEV-S-SHAH/Fin.git
cd Fin

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python setup.py
python -m sandbox_engine --reset

# Terminal 1: FinGraph UI
python -m ui.fingraph --port 9100 --no-browser

# Terminal 2: Frontend
cd web
npm install
npm run dev
```

`--reset` prints a load report and five benchmarks, then exits. **All five should pass**, in about 11 seconds:

```
  [PASS] B1 graph shape and referential integrity
  [PASS] B2 point lookup: Net Sales 3M-2026-07-26 on 10-Q FY2027
  [PASS] B3 comparative-period separation: Net Sales 3M-2026-07-26 on 10-Q FY2027
  [PASS] B4 segment fan-out from 'Net Sales'
  [PASS] B5 negative control: FY2019 absent
  5/5 passed
```

## The Three UIs (all under `ui/`)

### 1. FinGraph UI (`ui/fingraph/`) — PRIMARY (port 9100)

Unified server serving three pages:
- **Landing page** (`/`) — Public marketing site with hero, markets ticker, features, pricing
- **GraphRAG Studio** (`/app`) — Protected analyst dashboard with entity browser, force graph, Ask FinGraph
- **Auth** (`/auth`) — Sign-in page with OAuth providers + dev fallback

```bash
python -m ui.fingraph --port 9100 --no-browser  # http://127.0.0.1:9100
```

**Routes:** `/` (landing), `/app` (studio), `/auth` (sign-in), `/company/{ticker}` (company overview), `/api/*` (REST + SSE)

**Adds endpoints:** `GET /api/companies`, `GET /api/route?q=`, `GET /api/markets`

**Structure:**
```
ui/fingraph/
├── server.py          # Unified HTTP server
├── __main__.py        # Entry point
├── studio/            # GraphRAG Studio (was sandbox_engine/ui_next/static/)
│   ├── index.html
│   ├── styles.css
│   ├── app.js, api.js, graph.js, answer.js, process.js, reports.js, store.js, util.js
├── landing/           # Public landing page (was sandbox_engine/ui_next/landing/)
│   ├── index.html
│   ├── styles.css
│   ├── app.js, components.js, nav.js, markets.js, hero-graph.js, gradient-bars.js, text-loop.js
├── auth/              # Authentication (was sandbox_engine/ui_next/auth/)
│   ├── index.html, callback.html, styles.css, app.js
└── vendor/            # Single source of vendor scripts
    ├── d3.v7.min.js
    └── gsap.min.js
```

### 2. Legacy GraphRAG Explorer (`ui/legacy_graphrag/`) — Port 9000

Original three-pane viewer (entity browser, force graph, question/answer).

```bash
python -m ui.legacy_graphrag --port 9000 --no-browser  # http://127.0.0.1:9000
```

### 3. GraphRAG Web UI (`ui/graphrag_web/`) — Port 8765

Domain-agnostic PDF graph viewer from the `graphrag` package.

```bash
python -m ui.graphrag_web --port 8765 --no-browser  # http://127.0.0.1:8765
```

### 4. GraphRAG Workspace (`web/`) — Independent React App

Premium React + TypeScript workspace (separate deployment, proxies to `ui.fingraph`).

```bash
cd web && npm run dev  # http://localhost:5173
```

## Ports

| Variable | Default | Service | Flag |
|---|---|---|---|
| `PORT_QUERY_UI` | `9000` | `python -m ui.legacy_graphrag` | `--port` |
| `PORT_QUERY_UI_V2` | `9100` | `python -m ui.fingraph` | `--port` |
| `PORT_GRAPHRAG_UI` | `8765` | `python -m ui.graphrag_web` | `--port` |
| (Vite) | `5173` | `cd web && npm run dev` | `--port` |

Precedence: `--port` > env var > default. Invalid values exit 2. Port in use exits 1 with actionable message.

## Environment Variables

Read from process env first, `.env` second. `setup.py` writes `.env` (gitignored).

```bash
cp .env.example .env          # or let setup.py create it
```

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `PORT_QUERY_UI` | `9000` | `ui.legacy_graphrag` | Legacy explorer port |
| `PORT_QUERY_UI_V2` | `9100` | `ui.fingraph` | FinGraph UI port |
| `PORT_GRAPHRAG_UI` | `8765` | `ui.graphrag_web` | PDF graph viewer port |
| `NVIDIA_API_KEY` | — | all | Hosted model key from [build.nvidia.com](https://build.nvidia.com) |
| `RAG_BACKEND` | `auto` | all | `auto`, `nvidia`, `ollama` |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | all | Override endpoint |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` | all | |
| `RAG_OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | all | Local model server |
| `RAG_OLLAMA_MODEL` | `llama3.2` | all | |
| `RAG_NUM_CTX` | `16384` | all | Context window |
| `RAG_TIMEOUT` | `900` | all | Seconds |
| `OPENAI_API_KEY` | — | all | Also accepted |
| `GRAPHRAG_PROVIDER` | `auto` | `ui.graphrag_web` | `gemini`, `nvidia`, `openai`, `anthropic`, `ollama`, `heuristic` |

### Key Resolution

| Found | Uses |
|---|---|
| Key in env or `.env` | Hosted NVIDIA model |
| No key, Ollama on `127.0.0.1:11434` | Local `llama3.2` |
| Neither | Question box asks for key or offers local model |

A rejected key (401/403) is set aside; UI falls back. You can also paste a key in-browser — held in server memory only.

## Query Routing & Tier 1 Ingestion (Cold-Start JIT Graph RAG)

Natural-language questions evaluated by `sandbox_engine/router.py` before retrieval:

- **`KNOWN`**: Entity indexed in KG. Filtered retrieval.
- **`COLD_START`**: Entity extracted (cashtag `$TICKER`, uppercase token, alias) but not indexed. Returns staging response to trigger JIT pipeline.
- **`AMBIGUOUS`**: No clear entity. Prompts for ticker clarification.

**Cold-Start Pipeline:**
1. **`tier1_fetch.py`** — Fetches latest filing from SEC EDGAR (< 2.5s SLA, 2.0s timeout, retry 429 with `Retry-After` + jitter)
2. **`tier1_clean.py`** — Extracts Item 1 (10-K) / Item 2 (10-Q), strips HTML/tables/scripts, caps 6,000 tokens
3. **`coldstart_schema.py`** — Pydantic taxonomies: `Company`, `Executive`, `Supplier`, `Competitor`, `RiskFactor`; relations: `SOURCES_FROM`, `SERVES_AS`, `COMPETES_WITH`, `EXPOSED_TO`, `LED_DIVISION`
4. **`coldstart_extract.py`** — 15–30 typed triples, < 3.5s SLA, confidence-ranked
5. **`stitch.py`** — Ephemeral `networkx.DiGraph` overlay, `ConceptRegistry` normalization, backbone stitching < 1.0s
6. **`traversal.py`** — Hybrid 2-hop traversal (overlay + LadybugDB), cycle prevention, provenance ledgers
7. **`coldstart_synthesis.py`** — 5-section investment report, token streaming
8. **`background.py`** — Non-blocking `BackgroundIngestQueue`, bounded ThreadPoolExecutor, atomic staging
9. **`community.py`** — Louvain modularity clustering, hub centrality, analytical briefs
10. **`query_ui.py`** — SSE events (`routing`, `fetching`, `stitching`, `token`, `done`), Cypher WHERE push-down

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"          # 698 tests
python -m unittest discover -s sandbox_engine -p "test_*.py" # 112 tests
```

Both suites offline: no network, no key, no database build.

| File | Tests | Covers |
|---|---|---|
| `tests/test_graphrag.py` | 172 | Providers, `graphrag` package, port/bind |
| `tests/test_graph_store.py` | 111 | `GraphStore` writes, traversals, buffer pool |
| `tests/test_entity_resolver.py` | 92 | Canonical entity registry |
| `tests/test_graph_extractor.py` | 69 | Entity/relation extraction |
| `tests/test_provenance.py` | 43 | Citation provenance |
| `tests/test_document_loader.py` | 44 | PDF chunking |
| `tests/test_router.py` | 15 | Query routing (KNOWN/COLD_START/AMBIGUOUS) |
| `tests/test_coldstart_stitch.py` | 9 | Schema, extractor budget, in-memory stitching |
| `tests/test_tier1_fetch.py` | 7 | SEC fetching SLA, rate limit, section cleaning |
| `tests/test_multi_hop_traversal.py` | 4 | 2-hop traversal, cycle prevention, provenance |
| `tests/test_coldstart_latency.py` | 2 | Traversal budget, streaming SLA |
| `tests/test_background_community.py` | 5 | Background queue, Louvain community |
| `tests/test_query_ui_transport.py` | 11 | SSE streaming events |
| `tests/test_setup.py` | 11 | `setup.py` key handling |
| `tests/test_ingestion.py` | 4 | Corpus presence — run first on new machine |

Run `tests/test_ingestion.py` first on a new machine: fails loudly if `sandbox_engine/data/` missing.

## Repository Contents

| | |
|---|---|
| **Committed** | All source, tests, `requirements.txt`, UI assets, `samples/`, **30 filings** under `sandbox_engine/data/` (39 MB), vendor JS (`d3.v7.min.js`, `gsap.min.js`) |
| **Not committed** | Generated `.lbug` databases, Parquet spill, `.env`, `node_modules/`, `dist/`, `data/`, `sandbox_engine/_run/`, `server.log`, `cookies.txt` |

Graph databases are build output — fresh clone runs `--reset` once.

Corpus is a directory tree; the tree *is* the scope:

```
sandbox_engine/data/<company>/<year>/<10k|10q|8k>/<filing>.htm
```

Add a company by creating its folder and dropping filings in. Nothing else to edit.

## Key Source Files

| Path | Role |
|---|---|
| `sandbox_engine/parser.py` | HTML → nodes/edges; registrant name, DEI facts, fiscal calendars |
| `sandbox_engine/entity_resolver.py` | Canonical entity registry; merges concepts across filings |
| `sandbox_engine/ufgs_extract.py` | Universal Financial Graph Schema tables |
| `sandbox_engine/buffer.py` | Node/relationship tables, Arrow batching, Parquet spill |
| `sandbox_engine/loader.py` | Idempotent load into LadybugDB (lookup-before-insert) |
| `sandbox_engine/benchmarks.py` | Five graph integrity benchmarks (B1–B5) |
| `sandbox_engine/query_ui.py` | Backend logic for HTTP server, `/api/ask`, graph payload |
| `ui/fingraph/` | FinGraph UI (landing, studio, auth) |
| `ui/legacy_graphrag/` | Legacy GraphRAG explorer |
| `ui/graphrag_web/` | PDF graph viewer |
| `sandbox_engine/router.py` | Discriminated query router (KNOWN, COLD_START, AMBIGUOUS) |
| `sandbox_engine/tier1_fetch.py` | Runtime SEC EDGAR fetcher (< 2.5s SLA) |
| `sandbox_engine/tier1_clean.py` | High-signal section slicing, token capping |
| `sandbox_engine/coldstart_schema.py` | Pydantic schema for entities/relations |
| `sandbox_engine/coldstart_extract.py` | Fast LLM triple extraction (15–30, < 3.5s) |
| `sandbox_engine/stitch.py` | In-memory overlay & backbone stitching (< 1.0s) |
| `sandbox_engine/traversal.py` | Hybrid 2-hop traverser & provenance ledger |
| `sandbox_engine/coldstart_synthesis.py` | 5-section investment analysis & streaming |
| `sandbox_engine/background.py` | Background ingestion queue manager |
| `sandbox_engine/community.py` | Louvain community clustering & briefs |
| `sandbox_engine/cli.py` | Typer entry point |
| `sandbox_engine/EVAL_SET.md` | 30-question eval set + live defect register |
| `sandbox_engine/eval_set.py` | EVAL_SET as runnable questions; `provenance_match_rate` |
| `web/src/` | React GraphRAG Workspace |
| `graphrag/` | PDF GraphRAG package |

## Correctness

Three defects from `EVAL_SET.md` fixed and covered by benchmarks; register tracks open items.

- **Company identity = CIK, not ticker.** SEC 8-K filenames carry hash; ticker read from `dei:TradingSymbol`.
- **Periods from filing, not inferred.** 10-Q period end from cover; fiscal year from issuer's `dei:CurrentFiscalYearEndDate` + that date.
- **Metric period = end date** (`3M-2026-03-28`), not fiscal year. Fiscal year holds 4 quarters; `3M-FY2026` named 3 different quarters.

Entity identity = id (ticker, accession, `stable_id`), never display name. Two issuers can share legal name; merging by name destroys data. Registry treats "gross"/"net" and "beginning"/"ending" as opposite-sense labels never fuzzy-merged.

### Answer Grading

Every model sentence graded against cited evidence → answer reduced to one of three states:

| Verdict | Means |
|---|---|
| **supported** | Every sentence rests on a fact in the evidence it cited |
| **qualified** | Nothing failed, but part hedged or reaches past filings |
| **refused** | At least one sentence not supported by what it cited |

Refusal dominates: 9 `STATED` + 1 `GAP` = refused. Provenance tab shows every sentence with rule, figures, citations (click → highlights graph node). Red banner lists refused items. Chip = grader's verdict, not whether model cited something.

## Dependencies

Pinned in `requirements.txt`: `ladybug`, `pandas`, `lxml`, `beautifulsoup4`, `pyarrow`, `pypdf`, `openai`, `typer`, `pydantic`, `networkx`. All have wheels for 3.13 on macOS, Linux, Windows — `pip install` needs no compiler.

`pypdf` pinned (not optional) because `graphrag/document.py` imports `PdfReader` at module scope — missing pypdf breaks `import graphrag` entirely. `document_loader.py` prefers `pdfplumber` for tables but degrades to pypdf-only.

Frontend deps in `web/package.json`: `react`, `react-dom`, `react-router-dom`, `swr`, `d3`, `motion`, `lucide-react`, `clsx`, `tailwind-merge`, `typescript`, `vite`, `@types/*`.

## Development

```bash
# Backend tests
python -m unittest discover -s tests -p "test_*.py"

# Frontend typecheck + build
cd web && npm run build

# Frontend dev (with hot reload)
cd web && npm run dev

# Run FinGraph UI
python -m ui.fingraph --port 9100 --no-browser  # Terminal 1

# Run GraphRAG Workspace (proxies to FinGraph UI)
cd web && npm run dev                                     # Terminal 2

# Run Legacy GraphRAG Explorer
python -m ui.legacy_graphrag --port 9000 --no-browser

# Run PDF Graph Viewer
python -m ui.graphrag_web --port 8765 --no-browser
```

## License

MIT