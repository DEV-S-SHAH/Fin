# FIn

A sandbox engine that turns SEC filings into a property graph, plus a browser
UI over it with GraphRAG question answering. Zero LLM calls on the way in — the
parse stage is pure Python; the model is only used to phrase answers over
retrieved graph context.

```
HTML filings ──▶ parser ──▶ Arrow ──▶ Parquet spill ──▶ LadybugDB ──▶ Cypher / UI
                    │                                                │
              canonical entity                              GraphRAG answers
                registry
```

## Quick start

```bash
git clone https://github.com/DEV-S-SHAH/FIn.git
cd Fin
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Build the graph from the three committed filings (~10s)
python -m sandbox_engine --reset

# Serve the UI with question answering on http://127.0.0.1:9000
python -m sandbox_engine.query_ui
```

`--reset` prints a load report and five benchmarks. All five should pass.

The databases are build artefacts and are not in version control, so the
`--reset` step is required on a fresh clone — `query_ui` will tell you so
explicitly if you skip it.

## The API key is committed

`.env` and `sandbox_engine/.env` are **tracked on purpose** in this private
repository, so a fresh clone can answer questions with no extra setup. The key
is live.

Before making this repository public, or before adding any collaborator,
**rotate it at [build.nvidia.com](https://build.nvidia.com)** — git history
retains the value even after the files are deleted. To untrack them:

```bash
git rm --cached .env sandbox_engine/.env
printf '.env\n.env.*\n' >> .gitignore
```

The server reads the key from the environment first and the file second, so
exporting `NVIDIA_API_KEY` overrides whatever is committed.

## What is and isn't in the repo

| | |
|---|---|
| Committed | All source, tests, `requirements.txt`, and the **3 filings** the sandbox scope ingests (2.3 MB) |
| Not committed | The other ~56 MB of the SEC corpus, generated `.lbug` databases, Parquet spill |

If you widen `SCOPE` in `sandbox_engine/config.py`, drop the matching filings
into `data/` — the pipeline resolves paths relative to the repo root.

## Tests

```bash
python -m unittest sandbox_engine.test_entity_resolver \
                   sandbox_engine.test_parser_identity \
                   sandbox_engine.test_graph_payload
python -m unittest discover -s tests -p "test_*.py"
```

## Layout

| Path | Role |
|---|---|
| `sandbox_engine/parser.py` | HTML → nodes/edges, plus the registrant-name and DEI fact extraction |
| `sandbox_engine/entity_resolver.py` | Canonical entity registry; merges concepts across filings |
| `sandbox_engine/buffer.py` | Node/relationship tables, Arrow batching, Parquet spill |
| `sandbox_engine/loader.py` | Idempotent load into LadybugDB (lookup-before-insert) |
| `sandbox_engine/benchmarks.py` | The five graph integrity benchmarks |
| `sandbox_engine/query_ui.py` | HTTP server, `/api/ask`, graph payload for the UI |
| `sandbox_engine/cli.py` | Typer entry point (currently has a pre-existing `SyntaxError`) |

## Notes on identity

Entity identity is always the id — ticker, accession number, or a
content-addressed `stable_id` — never the display name. Two issuers can share a
legal name, so merging nodes by name would silently destroy data; the UI breaks
label collisions in the *label* instead and leaves the nodes distinct. The
registry therefore also treats "gross"/"net" and "beginning"/"ending" as
opposite-sense labels that must never be fuzzy-merged.
