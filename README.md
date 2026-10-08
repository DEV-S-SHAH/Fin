# FinGraph

**GraphRAG for SEC filings.** Turns 10-K, 10-Q, and 8-K filings into a queryable property graph with LLM question answering. Pure-Python ingestion (deterministic, no API key), model only used at query time to phrase answers over retrieved graph context.

---

## Quick Start

**Requires:** Python 3.13, Node.js 20+ (for React workspace). No Docker, no database server.

```bash
git clone https://github.com/DEV-S-SHAH/Fin.git
cd Fin

# Backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python setup.py                     # prompts for NVIDIA key, verifies it
python -m sandbox_engine --reset    # builds graph + runs 5 benchmarks (~11s)

# Terminal 1: FinGraph UI (landing + studio + auth)
python -m ui.fingraph --port 9100 --no-browser
# → http://127.0.0.1:9100

# Terminal 2 (optional): React GraphRAG Workspace
cd web && npm install && npm run dev
# → http://localhost:5173 (proxies API to port 9100)
```

**All five benchmarks should pass:**
```
[PASS] B1 graph shape and referential integrity
[PASS] B2 point lookup: Net Sales 3M-2026-07-26 on 10-Q FY2027
[PASS] B3 comparative-period separation
[PASS] B4 segment fan-out from 'Net Sales'
[PASS] B5 negative control: FY2019 absent
```

---

## What You Get

| Route | Description |
|-------|-------------|
| `/` | Public landing page — hero, live market ticker, features, pricing |
| `/app` | **GraphRAG Studio** (protected) — entity browser, force-directed D3 graph, Ask FinGraph with streaming answers & provenance |
| `/auth` | Sign-in — Google, Apple, TradingView OAuth + dev fallback |
| `/company/{ticker}` | Company overview — filings, metrics, chart, news, technicals |
| `/api/ask` | SSE streaming QA — routes KNOWN/COLD_START/AMBIGUOUS, returns answer + citations + graph |

**Cold-Start JIT Graph RAG:** Questions about companies not in the graph trigger live SEC fetch → extraction → in-memory overlay stitching → 2-hop traversal → streaming answer. All under SLA budgets (fetch < 2.5s, extract < 3.5s, stitch < 1s).

---

## Architecture

```
sandbox_engine/data/<company>/<year>/<form>/*.htm  →  30 committed filings (39 MB)
         │
         ▼  python -m sandbox_engine --reset
         │  pure Python, ~11s, no network
         ▼
   Arrow → Parquet spill → LadybugDB
         │
         ▼  sandbox_engine/_run/sandbox.lbug
         │
         ▼  python -m ui.fingraph --port 9100
    FinGraph UI: landing + studio + auth
```

**Key modules:**
| Path | Role |
|------|------|
| `sandbox_engine/parser.py` | HTML → nodes/edges; registrant, DEI facts, fiscal calendars |
| `sandbox_engine/entity_resolver.py` | Canonical registry; merges concepts across filings |
| `sandbox_engine/buffer.py` + `loader.py` | Arrow batching, Parquet spill, idempotent LadybugDB load |
| `sandbox_engine/router.py` | Query router: KNOWN / COLD_START / AMBIGUOUS |
| `sandbox_engine/tier1_fetch.py` | SEC EDGAR fetcher (< 2.5s SLA, retry 429 with Retry-After) |
| `sandbox_engine/coldstart_extract.py` | LLM triple extraction (15–30, < 3.5s) |
| `sandbox_engine/stitch.py` | In-memory `networkx` overlay + backbone stitching (< 1s) |
| `sandbox_engine/traversal.py` | Hybrid 2-hop traverser + provenance ledger |
| `sandbox_engine/query_ui.py` | Backend logic: SSE events, Cypher WHERE push-down |
| `ui/fingraph/server.py` | Unified HTTP server (landing, studio, auth, APIs) |

---

## Environment

```bash
cp .env.example .env   # or let setup.py create it
```

**Default API key:** The `.env.example` includes a default `NVIDIA_API_KEY` so anyone deploying can run queries immediately without additional setup. Override by running `python setup.py` or editing `.env`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT_QUERY_UI_V2` | `9100` | FinGraph UI port |
| `NVIDIA_API_KEY` | — | Hosted model key from [build.nvidia.com](https://build.nvidia.com) |
| `RAG_BACKEND` | `auto` | `auto`, `nvidia`, `ollama` |
| `RAG_OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Local model server |
| `RAG_OLLAMA_MODEL` | `llama3.2` | |
| `FINGRAPH_DATA_DIR` | `./data` | Persistent data directory (DB, staging, registry, checkpoints) |
| `FINGRAPH_AUTH_SECRET` | random | HMAC secret for session cookies (set to persist sessions across restarts) |
| `FINGRAPH_DEV_LOGIN` | `1` | Enable dev sign-in without OAuth |

**Key resolution:** Key in `.env` → NVIDIA hosted model. No key + Ollama running → local `llama3.2`. Neither → UI asks for key or offers local model. Key can also be pasted in-browser (memory only).

---

## Database Backup & Restore

The authoritative LadybugDB lives at `sandbox_engine/_run/sandbox.lbug` (or `$FINGRAPH_DATA_DIR/sandbox.lbug`). It is **never deleted by `--reset`** — only staging, reports, and registry are cleared.

```bash
# Create a verified backup
python -m sandbox_engine --backup create
# → backups/20261004T100949Z-sandbox-<sha>/sandbox.lbug + manifest.json

# List backups
python -m sandbox_engine --backup list

# Verify a backup
python -m sandbox_engine --backup verify --backup-id <id>

# Restore (preserves existing DB, requires --confirm)
python -m sandbox_engine --backup restore --backup-id <id> --confirm
```

---

## Tests

```bash
# Backend (all offline — no network, no key, no DB build)
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s sandbox_engine -p "test_*.py"

# Frontend
cd web && npm run build
```

---

## Repository Layout

```
Fin/
├── sandbox_engine/       # Ingestion pipeline + query backend
│   ├── _run/sandbox.lbug     # Authoritative LadybugDB (build output)
│   ├── data/                 # SEC filings (committed)
│   └── tests/                # 112 unit tests
├── ui/
│   ├── fingraph/             # PRIMARY UI (port 9100) — landing, studio, auth
│   ├── legacy_graphrag/      # Legacy three-pane explorer (port 9000)
│   └── graphrag_web/         # PDF GraphRAG viewer (port 8765)
├── graphrag/                 # Domain-agnostic PDF GraphRAG package
├── ingestion/                # Generic multi-company ingestion pipeline
├── web/                      # React + TypeScript GraphRAG Workspace
├── backups/                  # Verified DB backups (generated)
├── requirements.txt
├── setup.py
└── .env.example
```

---

## License

MIT# Sun Oct  4 16:55:37 IST 2026
