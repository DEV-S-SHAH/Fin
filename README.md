# Fin

Turns SEC filings into a property graph you can query, with a browser UI over it
and LLM question answering on top. No LLM runs on the way in — the parse stage is
pure Python, so a build is deterministic and needs no API key. The model is only
ever used to phrase an answer over retrieved graph context.

## Architecture

Two independent HTTP services over two pipelines, in one repository.

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
     python -m sandbox_engine.query_ui       python -m graphrag.cli serve
       graph explorer, reports, stats          domain-agnostic PDF viewer
       /api/ask → LLM answer, per request      /api/ask → LLM answer, per request
       $PORT_QUERY_UI · 127.0.0.1:9000         $PORT_GRAPHRAG_UI · 127.0.0.1:8765
```

| Service | Command | Port | Serves | Without a key |
|---|---|---|---|---|
| Graph explorer + RAG | `python -m sandbox_engine.query_ui` | 9000, `$PORT_QUERY_UI` | `sandbox_engine/_run/sandbox.lbug` | Explorer, reports, stats all work. `/api/ask` asks for a key or offers the local model. |
| PDF graph viewer | `python -m graphrag.cli serve` | 8765, `$PORT_GRAPHRAG_UI` | `data/*.lbug` | Serves fine; answers fall back to a lexical provider |

The two defaults live in `sandbox_engine/query_ui.py` and `graphrag/config.py`,
so they cannot be made to collide by editing a literal. Both services bind to
loopback and neither authenticates — `--host 0.0.0.0` is an explicit choice, not
an accident.

## Quick start

Requires **Python 3.13**. Nothing else — no database server, no Docker, no
system packages.

### macOS / Linux

```bash
git clone https://github.com/DEV-S-SHAH/Fin.git
cd Fin

python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python setup.py                     # prompts for your NVIDIA key, verifies it
python -m sandbox_engine --reset    # build the graph, run 5 benchmarks
python -m sandbox_engine.query_ui   # http://127.0.0.1:9000
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
python -m sandbox_engine.query_ui
```

If PowerShell refuses to activate the venv, its execution policy is blocking
it. Either run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or
skip activation entirely — every command below works with the interpreter
called out in full:

```powershell
.\.venv\Scripts\python.exe -m sandbox_engine --reset
```

`--reset` prints a load report and five benchmarks, then exits. **All five
should pass**, in about 11 seconds:

```
  [PASS] B1 graph shape and referential integrity
  [PASS] B2 point lookup: Net Sales 3M-2026-07-26 on 10-Q FY2027
  [PASS] B3 comparative-period separation: Net Sales 3M-2026-07-26 on 10-Q FY2027
  [PASS] B4 segment fan-out from 'Net Sales'
  [PASS] B5 negative control: FY2019 absent
  5/5 passed
```

If `py -3.13` is not recognised, install Python 3.13 from python.org and tick
"Add python.exe to PATH".

## The second service

`graphrag/` is the older, domain-agnostic pipeline: arbitrary PDFs in, an
LLM-discovered graph out, no SEC vocabulary anywhere. It has its own database
and its own UI, so it needs its own ingest before it will serve:

```bash
python -m graphrag.cli ingest samples/marine_biology.pdf   # or any PDF, or a directory
python -m graphrag.cli serve                              # http://127.0.0.1:8765
python -m graphrag.cli ask "what are the main species?" --json
```

`samples/` holds four PDFs for exactly this. `ingest` defaults to
`data/aapl-2026.lbug`; `serve` and `ask` take the same `--db`. The database is
build output, so a fresh clone has to run `ingest` once — `serve` against a
missing file fails inside LadybugDB with `Cannot create an empty database under
READ ONLY mode` rather than naming the command that would fix it. That is the
one rough edge here.

```
graphrag UI   : http://127.0.0.1:8765/
database      : data/aapl-2026.lbug
graph         : 1042 entities, 1519 relationships
press Ctrl-C to stop
```

`ingest` accepts `--provider heuristic` to skip the LLM entirely. It is lexical
rather than model-quality, but it needs no key and no network, which makes it
the fastest way to get a graph on screen.

## Ports

| Variable | Default | Service | Flag |
|---|---|---|---|
| `PORT_QUERY_UI` | `9000` | `python -m sandbox_engine.query_ui` | `--port` |
| `PORT_GRAPHRAG_UI` | `8765` | `python -m graphrag.cli serve` | `--port` |

Precedence is `--port`, then the variable, then the default. A value that is not
an integer in 1–65535 is rejected at start-up with exit code 2. A typo is not
worth a traceback, and it is not worth being ignored either: `PORT_QUERY_UI=90OO`
that looked applied while the server quietly listened somewhere else is the same
failure as no configuration at all.

A port already in use exits 1 and names the two things worth trying:

```
error: port 9000 on 127.0.0.1 is already in use.
  Another copy of this server is probably still running: lsof -nP -iTCP:9000 -sTCP:LISTEN
  Or pick another port: --port <n>, or PORT_QUERY_UI=<n>
  (The other service here, the graphrag UI, defaults to 8765; set PORT_GRAPHRAG_UI to move it.)
```

```bash
PORT_QUERY_UI=9100 python -m sandbox_engine.query_ui
PORT_GRAPHRAG_UI=9200 python -m graphrag.cli serve
```

Both read `.env` as well as the process environment, so the variable belongs in
`.env.example` like everything else.

## Environment variables

Read from the process environment first, `.env` second, so a one-off prefix
wins over the file:

```bash
NVIDIA_API_KEY=nvapi-... python -m sandbox_engine.query_ui
```

`setup.py` writes `.env` for you and is gitignored, so the key is never in the
repository — every clone asks for its own. Copy the template by hand if you
prefer:

```bash
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `PORT_QUERY_UI` | `9000` | `sandbox_engine.query_ui` | Port for the graph explorer |
| `PORT_GRAPHRAG_UI` | `8765` | `graphrag.cli serve` | Port for the PDF graph viewer |
| `NVIDIA_API_KEY` | — | both | Hosted model key, from [build.nvidia.com](https://build.nvidia.com) |
| `RAG_BACKEND` | `auto` | `query_ui` | `auto`, `nvidia` or `ollama`. `ollama` forces the local model even with a key present |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | `query_ui` | Override only if your endpoint differs |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` | `query_ui` | |
| `RAG_OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | `query_ui` | Local model server |
| `RAG_OLLAMA_MODEL` | `llama3.2` | `query_ui` | |
| `RAG_NUM_CTX` | `16384` | `query_ui` | Context window for the answer |
| `RAG_TIMEOUT` | `900` | `query_ui` | Seconds. The SDK default is long enough to turn a slow model into a browser-side "Failed to fetch" |
| `OPENAI_API_KEY` | — | `graphrag` | Also accepted by `query_ui`, after `NVIDIA_API_KEY` |
| `GRAPHRAG_PROVIDER` | `auto` | `graphrag` | `gemini`, `nvidia`, `openai`, `anthropic`, `ollama`, `heuristic` |
| `GRAPHRAG_MODEL` | per provider | `graphrag` | Override the provider's model |
| `GRAPHRAG_ENV_FILE` | `.env` | `graphrag` | Read the file from somewhere else |
| `GRAPHRAG_TEMPERATURE` | `0.0` | `graphrag` | |

### How the key is read

Ingestion and the benchmarks need no credential. The key is only for the
question box. You do not have to configure a backend: each service resolves one
per request and says what it picked, so starting `ollama serve` or entering a
key takes effect without a restart.

`sandbox_engine/query_ui.py` accepts `NVIDIA_API_KEY` or `OPENAI_API_KEY`, from
the environment, from `sandbox_engine/.env`, or from the repo-root `.env`.

| Found | Uses |
| --- | --- |
| A key in the environment or `.env` | hosted NVIDIA model |
| No key, Ollama answering on `127.0.0.1:11434` | local `llama3.2` |
| Neither | the question box asks for a key, or offers the local model |

A key the provider refuses with 401 or 403 is set aside rather than offered
again, and the UI falls back to whatever else is available. You can also paste a
key into the browser instead of writing a file; it is held in the server's
memory for the life of the process, never written to disk, logged, or sent back
to the page.

`graphrag/` resolves once per request from a different set. It reads
`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `NVIDIA_API_KEY`, in
that order of preference. NVIDIA is last on purpose: it is the only key a
default checkout has, and being the sole key should not outrank one you supplied
on purpose. With no key and no local Ollama it answers from a lexical provider —
a restatement of the retrieved graph, not reasoning. `ingest` prints a note when
that happens; `serve` does not, so check `python -m graphrag.cli stats` before
trusting an answer.

### Query Routing (Cold-Start JIT Graph RAG)

Natural-language questions in `sandbox_engine.query_ui` are evaluated by `sandbox_engine/router.py` before retrieval or calling an LLM:

- **`KNOWN`**: The requested entity is indexed in the knowledge graph. Retrieval executes filtered specifically to that entity rather than scanning all companies.
- **`COLD_START`**: The entity was extracted (via cashtag `$TICKER`, uppercase token, or alias dictionary) but is not yet indexed. Returns an explicit staging response (`{"status": "cold_start_required", "entity": "...", ...}`) to trigger the JIT pipeline.
- **`AMBIGUOUS`**: No clear entity was identified. Prompts for ticker clarification without calling the LLM and without silent fallback to AAPL/MSFT.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"          # 568 tests
python -m unittest discover -s sandbox_engine -p "test_*.py" # 93 tests
```

Both suites are offline: no network, no key, no database build.

| File | Tests | Covers |
|---|---|---|
| `tests/test_graphrag.py` | 172 | Providers, `graphrag` package, port and bind behaviour |
| `tests/test_graph_store.py` | 111 | `GraphStore` writes, traversals, buffer pool |
| `tests/test_entity_resolver.py` | 92 | Canonical entity registry |
| `tests/test_graph_extractor.py` | 69 | Entity and relation extraction |
| `tests/test_provenance.py` | 43 | Citation provenance |
| `tests/test_document_loader.py` | 44 | PDF chunking |
| `tests/test_router.py` | 15 | Query routing (KNOWN, COLD_START, AMBIGUOUS), entity filtering |
| `tests/test_query_ui_transport.py` | 7 | `query_ui` request transport |
| `tests/test_setup.py` | 11 | `setup.py` key handling |
| `tests/test_ingestion.py` | 4 | Corpus presence — run this first on a new machine |
| `sandbox_engine/test_entity_resolver.py` | 46 | Sense-opposite label protection |
| `sandbox_engine/test_rag_backends.py` | 26 | Backend resolution, lazy registry |
| `sandbox_engine/test_graph_payload.py` | 12 | Graph payload for the browser |
| `sandbox_engine/test_parser_identity.py` | 9 | Registrant identity from the filing |

`tests/test_ingestion.py` is the one to run first on a new machine: it fails
loudly if `sandbox_engine/data/` did not come down with the clone, which is the
one fresh-clone failure that is otherwise hard to diagnose.

## What's in the repository

| | |
|---|---|
| **Committed** | All source, tests, `requirements.txt`, the UI assets, `samples/`, and the **30 filings** under `sandbox_engine/data/` (39 MB) |
| **Not committed** | Generated `.lbug` databases, the Parquet spill, `.env` |

The graph databases are build output, so a fresh clone has to run `--reset` once.
`query_ui` says so explicitly if you skip it.

The corpus is a directory tree, and the tree *is* the scope — the resolver never
names a company:

```
sandbox_engine/data/<company>/<year>/<10k|10q|8k>/<filing>.htm
```

To add a company, create its folder and drop filings in. Nothing else to edit.

## Layout

| Path | Role |
|---|---|
| `sandbox_engine/parser.py` | HTML → nodes/edges; registrant name, DEI facts, fiscal calendars |
| `sandbox_engine/entity_resolver.py` | Canonical entity registry; merges concepts across filings |
| `sandbox_engine/ufgs_extract.py` | Universal Financial Graph Schema tables |
| `sandbox_engine/buffer.py` | Node/relationship tables, Arrow batching, Parquet spill |
| `sandbox_engine/loader.py` | Idempotent load into LadybugDB (lookup-before-insert) |
| `sandbox_engine/benchmarks.py` | The five graph integrity benchmarks (B1–B5) |
| `sandbox_engine/query_ui.py` | HTTP server, `/api/ask`, graph payload for the UI |
| `sandbox_engine/cli.py` | Typer entry point |
| `sandbox_engine/EVAL_SET.md` | 30-question eval set and the live defect register |
| `graphrag/` | Domain-agnostic PDF → graph package behind the 8765 service |
| `financial_graphrag.py` | Single-file in-process financial GraphRAG, standalone |
| `graphrag_synthesis.py` | Retrieval-to-answer half of the above |
| `document_loader.py`, `graph_extractor.py`, `graph_store.py`, `entity_resolver.py` | Root-level modules the `tests/` suite exercises directly |

## Correctness

Three defects found by `EVAL_SET.md` are fixed and covered by the benchmark
suite; the register in that file tracks what is still open.

- **Company identity is the CIK, not the ticker.** SEC filenames for 8-Ks carry
  a hash, so a ticker read from the filename alone split Microsoft into `MSFT`
  and `UNKNOWN`. The ticker is read from `dei:TradingSymbol` on the cover.
- **Periods are read from the filing, not inferred.** A 10-Q's own period end
  comes from its cover; the fiscal year comes from the issuer's
  `dei:CurrentFiscalYearEndDate` combined with that date, because a column
  header prints a *calendar* year and most quarters are not in the fiscal year
  their calendar year suggests.
- **A metric's period is its end date** (`3M-2026-03-28`), not a fiscal year. A
  fiscal year holds four quarters, so `3M-FY2026` named three different
  quarters at once and the three filings that reported them attached three
  values to one node. B2 now fails if a single period carries two values.

Entity identity is always the id — ticker, accession number, or a
content-addressed `stable_id` — never the display name. Two issuers can share a
legal name, so merging by name would silently destroy data; the UI breaks label
collisions in the *label* and leaves the nodes distinct. The registry also
treats "gross"/"net" and "beginning"/"ending" as opposite-sense labels that must
never be fuzzy-merged.

The `query_ui` server reports which schema it detected at start-up, because the
Cypher is written against the blueprint vocabulary and `detect_schema` /
`translate_for_engine` translate on the way out when the database is the engine
schema instead:

```
  Web UI       : http://127.0.0.1:9000/
  Database     : .../sandbox_engine/_run/sandbox.lbug
  Schema       : engine
  Graph Stats  : 8720 entities, 9710 relationships
```

## Dependencies

Pinned in `requirements.txt`: `ladybug`, `pandas`, `lxml`, `beautifulsoup4`,
`pyarrow`, `pypdf`, `openai`, `typer`. All have wheels for 3.13 on macOS, Linux
and Windows, so `pip install -r requirements.txt` needs no compiler.

`pypdf` is pinned rather than optional because `graphrag/document.py` imports
`PdfReader` at module scope — a missing pypdf breaks `import graphrag`
entirely, taking both services down. `document_loader.py` prefers `pdfplumber`
for table recovery but degrades to pypdf-only without it, so that one is left
unpinned.
