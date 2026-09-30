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
| Redesigned graph explorer | `python -m sandbox_engine.ui_next` | 9100, `$PORT_QUERY_UI_V2` | `sandbox_engine/_run/sandbox.lbug` | Same as above, new front end |
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

## The redesigned explorer

`sandbox_engine/ui_next/` is a second front end for the same knowledge graph. It
replaces the layout, not the answers: `server.py` subclasses the handler in
`query_ui.py`, so graph traversal, the RAG pipeline, grading, canned reports and
the SSE transport are the same code answering the same questions. The old page
is untouched and the two can run at the same time.

```bash
python -m sandbox_engine.query_ui    # original,  http://127.0.0.1:9000
python -m sandbox_engine.ui_next     # redesigned, http://127.0.0.1:9100
```

Both take `--port`, `--host`, `--no-browser` and `--db`, and the new one defaults
to 9100 via `$PORT_QUERY_UI_V2` so the two defaults cannot collide.

### What it adds

| Endpoint | Why |
|---|---|
| `GET /api/companies` | Filings, forms and periods per issuer. The old page hard-codes eight Apple questions, which is wrong the moment the graph holds a second issuer; this builds the sample questions from what is actually stored. |
| `GET /api/route?q=` | Which retrieval route a question would take — `KNOWN`, `COLD_START` or `AMBIGUOUS`. One graph query, no model call. |

That route probe is why the two transports differ. `COLD_START` fetches a filing
from EDGAR and synthesises it through a generator, so the server emits tokens as
it goes and the page streams them. `KNOWN` is one blocking call that returns the
whole answer at once, so the page asks for it in a single request: the old SSE
handler "streams" that route by splitting the finished string into words, which
looks like progress and costs a second full request to recover the grading the
stream never carried.

### Layout

```
sandbox_engine/ui_next/
  server.py            the legacy handler, plus static routes and the two endpoints above
  static/index.html    the shell
  static/styles.css    design system, light and dark
  static/app.js        wiring: panels, keyboard, command palette
  static/graph.js      D3 force graph
  static/answer.js     answer, verdict, sources, trace, provenance ledger
  static/reports.js    canned reports drawer
  static/api.js        fetch wrappers, JSON and SSE
  static/store.js      shared state
  static/util.js       DOM, formatting, markdown, toasts
```

No build step and no npm: the page is native ES modules, and D3 is the copy
already vendored at `sandbox_engine/static/d3.v7.min.js`.

### Moving the panels

The entity list and the answer column are separated by a draggable divider, and
the answer column is the one people resize: a graded answer with a verdict, a
source list and a provenance ledger does not read well in 440px. Three ways to
move it, all clamped so the graph keeps a usable share of the window:

- drag the divider, which has a visible grip and a 15px hit area
- focus it and use `←` / `→`, or `⇧` with them for larger steps; `Home` resets
- `⌘K` and search for *widen*, *narrow* or *reset* the answer panel

The width is remembered per browser.

### What it does not do

- It never writes. The graph is opened read-only unless you pass `--read-write`.
- It does not reimplement retrieval. Two front ends sharing one backend is the
  point; a second copy of the RAG pipeline would drift from the first.
- `?q=...` re-runs a question on load rather than restoring a stored answer. An
  answer is only as good as the graph it was graded against, and that changes
  when the next filing lands, so the link says what the evidence supports today.

## Ports

| Variable | Default | Service | Flag |
|---|---|---|---|
| `PORT_QUERY_UI` | `9000` | `python -m sandbox_engine.query_ui` | `--port` |
| `PORT_QUERY_UI_V2` | `9100` | `python -m sandbox_engine.ui_next` | `--port` |
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
PORT_QUERY_UI_V2=9200 python -m sandbox_engine.ui_next
PORT_GRAPHRAG_UI=9300 python -m graphrag.cli serve
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

### Query Routing & Tier 1 Ingestion (Cold-Start JIT Graph RAG)

Natural-language questions in `sandbox_engine.query_ui` are evaluated by `sandbox_engine/router.py` before retrieval or calling an LLM:

- **`KNOWN`**: The requested entity is indexed in the knowledge graph. Retrieval executes filtered specifically to that entity rather than scanning all companies.
- **`COLD_START`**: The entity was extracted (via cashtag `$TICKER`, uppercase token, or alias dictionary) but is not yet indexed. Returns an explicit staging response (`{"status": "cold_start_required", "entity": "...", ...}`) to trigger the JIT pipeline.
- **`AMBIGUOUS`**: No clear entity was identified. Prompts for ticker clarification without calling the LLM and without silent fallback to AAPL/MSFT.

When cold start triggers:
- **`sandbox_engine/tier1_fetch.py`**: Fetches the latest filing directly from SEC EDGAR under a strict 2.5s SLA budget (2.0s socket timeout) with zero silent drops, retrying transient HTTP 429s once using `Retry-After` with jitter.
- **`sandbox_engine/tier1_clean.py`**: Extracts high-signal narrative sections (Item 1 Business for 10-K, Item 2 MD&A for 10-Q) using universal section extractors, strips HTML markup/tables/scripts, and caps output at 6,000 tokens (preserving sentence boundaries).
- **`sandbox_engine/coldstart_schema.py`**: Enforces strict typed Pydantic taxonomies for financial entities (`Company`, `Executive`, `Supplier`, `Competitor`, `RiskFactor`) and relations (`SOURCES_FROM`, `SERVES_AS`, `COMPETES_WITH`, `EXPOSED_TO`, `LED_DIVISION`), disallowing self-loops and limiting quotes to <= 30 words.
- **`sandbox_engine/coldstart_extract.py`**: Extracts 15–30 typed triples under a strict 3.5s SLA timeout budget, ranking excess triples by confidence.
- **`sandbox_engine/stitch.py`**: Maintains an ephemeral `networkx.DiGraph` overlay, normalizes entities via `ConceptRegistry` and `stable_id`, stitches into the read-only LadybugDB backbone, and deduplicates arcs in memory in < 1.0s.
- **`sandbox_engine/traversal.py`**: Executes hybrid 2-hop graph traversals navigating both ephemeral overlay and persistent LadybugDB nodes, preventing cycles and outputting deterministic provenance ledgers.
- **`sandbox_engine/coldstart_synthesis.py`**: Formulates structured 5-section financial investment reports and streams incremental tokens.
- **`sandbox_engine/background.py`**: Non-blocking `BackgroundIngestQueue` using a bounded ThreadPoolExecutor and thread-safe deduplication to stage full historical ingestion atomically without locking LadybugDB.
- **`sandbox_engine/community.py`**: NetworkX Louvain modularity clustering generating community partitions, hub node centrality rankings, and analytical briefs.
- **`sandbox_engine/query_ui.py` (SSE Streaming & Retrieval Push-down)**: Emits real-time SSE progress events (`routing`, `fetching`, `stitching`, `token`, `done`), schedules background ingestion, and eliminates O(filings × chunks) scans by pushing down Cypher WHERE filters.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"          # 698 tests
python -m unittest discover -s sandbox_engine -p "test_*.py" # 112 tests
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
| `tests/test_coldstart_stitch.py` | 9 | Schema validation, extractor budget/SLA, in-memory backbone stitching |
| `tests/test_tier1_fetch.py` | 7 | SEC runtime fetching SLA, rate limit backoff, section cleaning & token cap |
| `tests/test_multi_hop_traversal.py` | 4 | Hybrid 2-hop traversal, cycle prevention, provenance ledger formatting |
| `tests/test_coldstart_latency.py` | 2 | Traversal budget and interactive pipeline streaming SLA |
| `tests/test_background_community.py` | 5 | Background queue deduplication, atomic staging writes, Louvain community detection |
| `tests/test_query_ui_transport.py` | 11 | `query_ui` request transport & SSE streaming events |
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
| `sandbox_engine/ui_next/` | Redesigned front end over the same graph; imports the backend, adds two read-only endpoints |
| `sandbox_engine/router.py` | Discriminated query router (KNOWN, COLD_START, AMBIGUOUS) |
| `sandbox_engine/tier1_fetch.py` | Runtime SEC EDGAR filing fetcher (< 2.5s SLA budget) |
| `sandbox_engine/tier1_clean.py` | High-signal section slicing (Item 1/2) and token capping |
| `sandbox_engine/coldstart_schema.py` | Pydantic schema validation for entities and relations |
| `sandbox_engine/coldstart_extract.py` | Fast LLM triple extraction (15–30 triples, < 3.5s SLA) |
| `sandbox_engine/stitch.py` | In-memory overlay graph & backbone stitching (< 1.0s) |
| `sandbox_engine/traversal.py` | Hybrid 2-hop graph traverser & provenance ledger |
| `sandbox_engine/coldstart_synthesis.py` | 5-section investment analysis & token streaming |
| `sandbox_engine/background.py` | Non-blocking background ingestion queue manager |
| `sandbox_engine/community.py` | Graph Louvain community clustering & brief generator |
| `sandbox_engine/cli.py` | Typer entry point |
| `sandbox_engine/EVAL_SET.md` | 30-question eval set and the live defect register |
| `sandbox_engine/eval_set.py` | EVAL_SET.md as runnable questions; `provenance_match_rate` |
| `graphrag/` | Older GraphRAG package, kept for the legacy `financial_graphrag.py` path |

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

### What the answer panel is allowed to say

Every sentence the model writes is graded by rule against the cited evidence,
and the answer is then reduced to one of three states:

| Verdict | Means |
|---|---|
| **supported** | every sentence rests on a fact in the evidence it cited |
| **qualified** | nothing failed, but part of it is hedged or reaches past the filings |
| **refused** | at least one sentence is not supported by what it cited |

Refusal dominates: an answer of nine `STATED` sentences and one `GAP` sentence
is refused, not qualified, because a reader takes the nine and misses the one.

The **Provenance** tab shows every sentence with the rule that judged it, the
figures it asserts, and the citations that support it — clicking a citation
highlights the node in the graph, as in the answer itself. A red banner above
the answer lists anything the grader refused, and screen readers are told the
verdict when an answer arrives.

The chip is the grader's verdict, not whether the model cited something. Those
are different questions, and only one of them means anything: a model that
invents a figure and cites a real entity satisfies the second and fails the
first.

## Dependencies

Pinned in `requirements.txt`: `ladybug`, `pandas`, `lxml`, `beautifulsoup4`,
`pyarrow`, `pypdf`, `openai`, `typer`, `pydantic`, `networkx`. All have wheels for 3.13 on macOS, Linux
and Windows, so `pip install -r requirements.txt` needs no compiler.

`pypdf` is pinned rather than optional because `graphrag/document.py` imports
`PdfReader` at module scope — a missing pypdf breaks `import graphrag`
entirely, taking both services down. `document_loader.py` prefers `pdfplumber`
for table recovery but degrades to pypdf-only without it, so that one is left
unpinned.
