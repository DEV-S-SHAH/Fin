# Contributing to FinGraph

FinGraph is GraphRAG for Financial Insights — it turns SEC filings into a queryable property graph with provenance-tagged evidence. We welcome contributions. Please read this guide before submitting.

> **Note:** The authoritative engineering constitution is [`AGENTS.md`](AGENTS.md). Where this document and AGENTS.md overlap, AGENTS.md takes precedence.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Project Architecture](#project-architecture)
3. [How to Contribute](#how-to-contribute)
4. [Coding Standards](#coding-standards)
5. [Testing](#testing)
6. [Security](#security)
7. [Pull Request Process](#pull-request-process)
8. [Code of Conduct](#code-of-conduct)

---

## Getting Started

### Prerequisites

- **Python 3.13** (managed via `uv`)
- **Node.js 20+** (for React workspaces)
- **`SEC_USER_AGENT`** environment variable set for SEC EDGAR access

### Setup

```bash
# Clone the repo
git clone https://github.com/DEV-S-SHAH/Fin.git
cd Fin

# Python environment
uv sync

# UI dependencies (PRIMARY UI)
cd ui/fingraph && npm install && cd ../..

# Copy environment config (never commit real keys)
cp .env.example .env
# Edit .env with your API keys
```

### Running the UI server (port 9100)

```bash
FINGRAPH_DEV_LOGIN=1 python -m ui.fingraph --port 9100 --no-browser
# → http://127.0.0.1:9100
```

### Building the LadybugDB graph

```bash
python -m sandbox_engine --reset    # builds graph + runs 5 benchmarks (~11s)
```

---

## Project Architecture

FinGraph has **two distinct pipelines** sharing LadybugDB (an embedded property graph at `sandbox_engine/_run/sandbox.lbug`). Do not treat them as interchangeable.

| Pipeline | Owns |
|----------|------|
| `sandbox_engine` | SEC-specific, deterministic HTML parsing → Arrow/Parquet → LadybugDB; traversal; provenance |
| `graphrag` | Domain-agnostic PDF ingestion, chunking, LLM triple extraction |
| `ingestion` | Multi-company orchestration, company registry, SEC acquisition |
| `ui` | Presentation, routing, API, auth, input validation, SSRF, rendering |

### Non-negotiable invariants

- **Provenance**: assigned by retrieval code, never by the model. Tags: `STATED`, `DERIVED`, `INFERRED`, `EXTERNAL`, `GAP`.
- **Company isolation**: retrieval scopes to one issuer. No cross-company leakage.
- **Fiscal calendars**: never assume universal fiscal year-end — AAPL → Sep 30, MSFT → Jun 30, TSLA → Dec 31.
- **Idempotency**: writes must be idempotent (Cypher `MERGE`, `ConceptRegistry`, atomic checkpoints via `.tmp → os.replace`).
- **Tier-1 SLA**: SEC fetch is a 2.5s hard budget (`MAX_TOTAL_BUDGET_SECONDS = 2.5`). Do not increase timeouts without review.
- **LadybugDB**: single writer; `drain_staging.py` checks port 9000 and refuses if a server holds the DB; `CREATE IF NOT EXISTS` accepts drifted tables silently — `ensure_schema()` introspects every table.

---

## How to Contribute

### 1. Pick an issue
Look for issues labeled `good first issue` or `help wanted`. New ideas should be discussed in an issue first to confirm scope.

### 2. Create a branch
```bash
git checkout -b feature/your-feature-name
```

### 3. Make your change
- Follow the coding standards below.
- Make the smallest correct change; no unrelated refactoring.
- Preserve existing behavior unless the task requires change.
- Inspect callers before changing interfaces (`grep` usages).
- Inspect tests before changing behavior.

### 4. Run tests
```bash
# Backend (all offline — no network, no key, no DB build)
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s sandbox_engine -p "test_*.py"

# India Company Graph (offline — no network, no key)
PYTHONPATH=. python -m pytest tests/test_india_*.py -q

# Frontend
cd web && npm run build
```

### 5. Commit
The pre-commit hook is environmentally broken (Python 3.9 vs repo's 3.13). Use `--no-verify`:
```bash
git commit --no-verify -m "feat: your change description"
```

### 6. Open a pull request
- Describe what changed and why.
- Include before/after evidence (screenshots for UI, test output for logic).
- State whether it's a one-way or two-way door change.

---

## Coding Standards

### Language-specific rules

- **Python**: type hints required on public functions; docstrings for modules and public classes; f-strings for formatting.
- **JavaScript**: ES modules (`import`/`export`); `d3` for graph visualization; no `var`; null-guard all DOM lookups (`$()` may return null).
- **CSS**: use design-system tokens (`var(--primary)`, `var(--dur)`, etc.); no hardcoded colors where a token exists.

### File organization

- Inspect implementation before proposing changes.
- Preserve backwards compatibility where practical.
- Document breaking changes.
- No broad rewrites without explicit authorization.

### AGENTS.md overrides

`AGENTS.md` is the repo's engineering constitution. Read it before acting. It overrides this document where they overlap.

---

## Testing

| Change area | Required tests |
|-------------|---------------|
| Ingestion/retrieval | `test_generic_ingestion.py`, `test_ingestion.py`, `test_router.py` |
| Provenance | `test_provenance.py`, `test_provenance_ui.py` |
| Idempotency | `test_persistence_restart.py`, `test_drain_staging.py` |
| Routing | `test_router.py`, golden queries |
| Cold-start | `test_coldstart_extract.py`, `test_coldstart_stitch.py`, `test_coldstart_latency.py` |
| Graph traversal | `test_multi_hop_traversal.py`, `test_neighborhood_priority.py` |
| Company isolation | `TestCompanyIsolation`, anti-leak benchmarks |
| Security | `test_ssrf.py`, `test_http_concurrency.py` |
| Benchmark-sensitive | Run relevant golden queries; preserve anti-leak, negative controls, minimum-hop requirements |

### Before you submit
1. Inspect relevant tests and identify affected invariants.
2. Make the change.
3. Run the smallest relevant suite; broader tests for architectural changes.
4. Benchmark-sensitive changes: run golden queries; preserve anti-leak and negative controls.
5. Report test results honestly.

---

## Security

- **No hardcoded secrets** — use env vars (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `SEC_USER_AGENT`).
- Preserve SSRF protection (`FINGRAPH_SSRF_CONFIG` allow-list: `www.sec.gov`, `data.sec.gov`, Yahoo Finance).
- Validate external URLs before requests.
- Auth boundaries, HMAC sessions, no secrets in logs.
- Security changes require `test_ssrf.py` + `test_http_concurrency.py`.

---

## Pull Request Process

1. Open a draft PR early for feedback.
2. Ensure CI passes (all relevant tests green).
3. Request review from a maintainer.
4. Address review feedback.
5. Maintainer merges after approval.

---

## Code of Conduct

Be respectful, inclusive, and constructive. Harassment of any kind is not tolerated.

This project is licensed under the MIT License.