# FIn

Turns SEC filings into a property graph you can query, with a browser UI over
it and LLM question answering on top. No LLM is used on the way in — the parse
stage is pure Python, so a build is deterministic and needs no API key. The
model is only ever used to phrase an answer over retrieved graph context.

```
HTML filings ──▶ parser ──▶ Arrow ──▶ Parquet spill ──▶ LadybugDB ──▶ Cypher / UI
                    │                                                │
              canonical entity                              GraphRAG answers
                registry
```

The corpus is 30 real filings — 1×10-K, 3×10-Q and 6×8-K for each of Apple,
Microsoft and NVIDIA. A full build takes about 11 seconds.

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

python setup.py                        # prompts for your NVIDIA key, verifies it
python -m sandbox_engine --reset       # build the graph, run 5 benchmarks
python -m sandbox_engine.query_ui      # http://127.0.0.1:9000
```

`setup.py` writes the key to `.env`, which is gitignored. The key is never in
the repository, so every clone asks for its own — a leaked repo is not a leaked
credential. Skip it entirely and the UI will use a local model instead, or ask
for a key in the browser. The graph explorer works with no key at all.


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

`--reset` prints a load report and five benchmarks. **All five should pass.** If
`py -3.13` is not recognised, install Python 3.13 from python.org and tick
"Add python.exe to PATH".

## Enabling LLM answers

Ingestion and the benchmarks need no credential. The key is only for the
question box in the UI. You do not have to configure a backend: the UI works
out what it can use, and says so in the header.

| Found | Uses |
| --- | --- |
| A key in the environment or `.env` | hosted NVIDIA model |
| No key, Ollama answering on `127.0.0.1:11434` | local `llama3.2` |
| Neither | the question box asks for a key, or offers the local model |

Resolution happens per request, not once at startup, so starting `ollama serve`
or entering a key takes effect without a restart. A key the provider refuses
with 401 or 403 is set aside rather than offered again, and the UI falls back to
whatever else is available.

To use a hosted model, get a key at [build.nvidia.com](https://build.nvidia.com)
and put it in either `.env`:

```bash
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

```dotenv
NVIDIA_API_KEY=nvapi-your-key-here
```

`.env` is gitignored and the key is never committed — `.env.example` is the
tracked template.

You can also paste a key into the browser instead of writing a file. That key is
held in the server's memory for the life of the process and is never written to
disk, logged, or sent back to the page.

Every RAG setting is read from the process environment first and `.env` second,
so this works and the environment wins:

```bash
NVIDIA_API_KEY=nvapi-... python -m sandbox_engine.query_ui
```

To force the local model even with a key present — no network, no cost — set
`RAG_BACKEND=ollama` after installing [Ollama](https://ollama.com) and pulling a
model (`ollama pull llama3.2`).

### How the key is read

`sandbox_engine/query_ui.py` accepts `NVIDIA_API_KEY` or `OPENAI_API_KEY`, from
the environment, from `sandbox_engine/.env`, or from the repo-root `.env`. It
reports which backend it resolved to and why at startup, and the same reason is
in the model chip's tooltip.

## What's in the repository

| | |
|---|---|
| **Committed** | All source, tests, `requirements.txt`, the UI assets, and the **30 filings** under `sandbox_engine/data/` (39 MB) |
| **Not committed** | Generated `.lbug` databases, the Parquet spill, `.env` |

The graph databases are build output, so a fresh clone has to run `--reset` once.
It takes ~11 s and needs no network. `query_ui` says so explicitly if you skip
it.

The corpus is a directory tree, and the tree *is* the scope — the resolver never
names a company:

```
sandbox_engine/data/<company>/<year>/<10k|10q|8k>/<filing>.htm
```

To add a company, create its folder and drop filings in. Nothing else to edit.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"          # 484 tests
python -m unittest discover -s sandbox_engine -p "test_*.py" # 67 tests
```

`tests/test_ingestion.py` is the one to run first on a new machine: it fails
loudly if `sandbox_engine/data/` did not come down with the clone, which is the
one fresh-clone failure that is otherwise hard to diagnose.

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

## Dependencies

Pinned in `requirements.txt`: `ladybug`, `pandas`, `lxml`, `beautifulsoup4`,
`pyarrow`, `openai`, `typer`. All have wheels for 3.13 on macOS, Linux and
Windows, so `pip install -r requirements.txt` needs no compiler.
