# FinGraph (Dev)

**GraphRAG for SEC filings.** Turns 10-K, 10-Q, and 8-K filings into a queryable property graph with LLM question answering. Pure-Python ingestion (deterministic, no API key), model only used at query time to phrase answers over retrieved graph context.

## Demo

https://github.com/user-attachments/assets/562b2311-8066-4879-899b-d8bc6858a6ed

Financial intelligence, connected.

## Presentations

PPTX files download directly. PDFs open in GitHub's built-in viewer for slide-by-slide navigation.

| File | PPTX | PDF (View Online) |
|------|------|-------------------|
| Review 1 | [Download](https://raw.githubusercontent.com/DEV-S-SHAH/Fin/main/ppt/Graph%20RAG%20based%20Financial%20Insights%20Review%201.pptx) | [View](https://github.com/DEV-S-SHAH/Fin/blob/main/ppt/Graph%20RAG%20based%20Financial%20Insights%20Review%201.pdf) |
| Review 2 | [Download](https://raw.githubusercontent.com/DEV-S-SHAH/Fin/main/ppt/Graph%20RAG%20based%20Financial%20Insights%20Review%202.pptx) | [View](https://github.com/DEV-S-SHAH/Fin/blob/main/ppt/Graph%20RAG%20based%20Financial%20Insights%20Review%202.pdf) |
| Review 3 | [Download](https://raw.githubusercontent.com/DEV-S-SHAH/Fin/main/ppt/Graph%20RAG%20based%20Financial%20Insights%20Review%203.pptx) | [View](https://github.com/DEV-S-SHAH/Fin/blob/main/ppt/Graph%20RAG%20based%20Financial%20Insights%20Review%203.pdf) |

[View all files in /ppt folder](https://github.com/DEV-S-SHAH/Fin/tree/main/ppt)

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

## Tests

```bash
# Backend (all offline — no network, no key, no DB build)
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s sandbox_engine -p "test_*.py"

# Frontend
cd web && npm run build
```

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

## License

This project is licensed under the MIT License.
