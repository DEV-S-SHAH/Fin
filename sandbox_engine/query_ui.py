"""GraphRAG Question-Answering & Interactive Explorer for the blueprint LadybugDB.

Provides the same three-pane UI as graphrag/web.py and graphrag/static/index.html:
  left   — entity browser (Company, Filing, FinancialMetric, Segment, DisclosureEvent)
  centre — interactive force-directed graph with citation highlighting
  right  — natural-language question box powered by NVIDIA nemotron GraphRAG

Run::

    source .venv/bin/activate
    python -m sandbox_engine.query_ui            # opens http://127.0.0.1:9000/
    python -m sandbox_engine.query_ui --port 9001

The port defaults to ``$PORT_QUERY_UI``, then to 9000. It is a separate service
from the graphrag UI on ``$PORT_GRAPHRAG_UI`` (8765); the two defaults live in
this module and ``graphrag/config.py`` so they cannot be made to collide by
editing a literal.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import ladybug as lb

from .buffer import NODE_TABLES, REL_TABLES
from .provenance import (
    build_evidence,
    grade_answer,
    render_gap,
    serialise_evidence,
)
from .router import EntityRoute, route_query

#: Frontend libraries served under ``/vendor/``. Vendored locally so the page
#: works in a browser with no internet access; d3 drives the force layout below.
_VENDOR_DIR = Path(__file__).resolve().parent / "static"
VENDOR: dict[str, bytes] = {}
for _vendor_name in ("d3.v7.min.js",):
    _vendor_path = _VENDOR_DIR / _vendor_name
    if _vendor_path.is_file():
        VENDOR[_vendor_name] = _vendor_path.read_bytes()


def merge_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated *ids*, then make every remaining label unique.

    Identity in this graph is the id -- ticker, accession number, or a
    content-addressed ``stable_id`` -- never the name. Two rules follow, and
    they pull in opposite directions, so both are applied here rather than
    trusted to each call site:

    1. Same id means same node. Re-emit it once; a repeat is the same node
       arriving by two paths, and appending it twice makes the renderer draw
       two overlapping boxes for one entity.
    2. Same *name* does not mean same node. A name collision must be broken in
       the label, not resolved by merging. Merging on name is how six distinct
       issuers that happen to share a legal name get collapsed into one node
       with one node's edges.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        nid = node.get("id")
        if nid is None:
            continue
        existing = by_id.get(nid)
        if existing is None:
            by_id[nid] = dict(node)
            continue
        # Same node, second sighting: keep the richer description rather than
        # letting whichever query ran last overwrite it with a bare one.
        for key, value in node.items():
            if len(str(value or "")) > len(str(existing.get(key) or "")):
                existing[key] = value

    merged = list(by_id.values())
    collisions = Counter(n.get("name", "") for n in merged)
    for node in merged:
        if collisions[node.get("name", "")] > 1:
            hint = node.pop("label_hint", "") or str(node.get("id", ""))
            node["name"] = f"{node['name']} · {hint}"
        else:
            node.pop("label_hint", None)
    return merged


def merge_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated ``(source, target, relation)`` triples.

    The retrieval path fans out over several Cypher queries, so the same edge
    can be reached more than once -- a filing's edge to its net-sales metric
    arrives once per query that touches that pair. Duplicates are not merely
    redundant rows: the renderer draws one arrow per entry, so the graph comes
    back looking like it has parallel edges the database does not have.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for edge in edges:
        key = (edge.get("source"), edge.get("target"), edge.get("relation"))
        if key in seen:
            continue
        seen.add(key)
        out.append(edge)
    return out


log = logging.getLogger("graphrag_ui")


def _configure_logging() -> None:
    """Install the UI's log format.

    Called from the entry points, never at import: ``basicConfig`` mutates the
    process-wide root logger, and a library that reconfigures logging merely
    because something imported it changes the output of every other module in
    the process. A server needs it; ``import`` does not.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


_HERE = Path(__file__).resolve().parent
# The graph this UI serves, built by ``python -m sandbox_engine --reset``.
#
# The Cypher below is written against the blueprint schema (FinancialMetric.
# metric_id, Segment.segment_id, DISAGGREGATED_BY and so on). That schema is
# translated on the way out by ``detect_schema``/``translate_for_engine`` when
# the database turns out to be the engine schema, so the two differ in
# vocabulary but not in meaning.
#
# A single explicit path, not a list of hopeful candidates. An earlier version
# listed a ``_run2/blueprint.lbug`` first; nothing in this repository builds
# that file, so the entry only ever missed and the search fell through to the
# line below -- which meant the server started on a different schema than the
# one it was written against without saying so.
_DB_CANDIDATES = (_HERE / "_run" / "sandbox.lbug",)

#: Environment variable that overrides the UI port, and the port used when it
#: is unset. The 8765 graphrag UI is a separate service; keeping the two
#: defaults here makes the collision impossible to introduce by editing source.
UI_PORT_ENV = "PORT_QUERY_UI"
DEFAULT_UI_PORT = 9000


def default_ui_port() -> int:
    """The port to bind, from ``PORT_QUERY_UI`` or the built-in default.

    An explicit ``--port`` always wins. Read through ``_setting`` rather than
    ``os.environ`` so a ``PORT_QUERY_UI`` in ``.env`` is honoured: the file is
    what ``.env.example`` tells a reader to copy, and a variable set there that
    is silently ignored is the same failure as no configuration at all.

    A value that is not a usable port is an error rather than a silent fall
    back. ``PORT_QUERY_UI=90OO`` would otherwise look applied while the server
    quietly listened somewhere else, and an out-of-range integer such as
    ``70000`` fails much later, inside ``bind``, as a bare ``OverflowError``.
    """
    raw = _setting(UI_PORT_ENV)
    if not raw:
        return DEFAULT_UI_PORT
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError(
            f"{UI_PORT_ENV}={raw!r} is not a usable port; expected an integer "
            f"between 1 and 65535"
        ) from None
    return port


# ── Schema translation ────────────────────────────────────────────────────────
#
# This UI's Cypher was written against the blueprint schema. The pipeline
# (`python -m sandbox_engine`) builds a graph with a different, smaller schema.
# A fresh clone can only produce the latter, because the databases are build
# artefacts -- so a UI that spoke only the blueprint dialect served a graph
# where every entity query raised "Cannot find property legal_name" and the page
# rendered blank.
#
# Rather than maintain two copies of every query, queries are written once in
# blueprint terms and translated per database. Properties with no counterpart
# map to the literal NULL, which preserves the arity of a RETURN list and so
# keeps positional unpacking correct -- dropping the column instead would
# silently shift every later value.
_BLUEPRINT_TO_ENGINE = {
    # node tables
    "FinancialMetric": "Metric",
    "DisclosureEvent": "Event",
    "DocumentChunk": "Chunk",
    # relationship tables
    "DISAGGREGATED_BY": "HAS_SEGMENT",
    "CONTAINS_CHUNK": "HAS_CHUNK",
}

#: Blueprint property -> engine property. A property missing from this map
#: keeps its name. The ones the engine dropped entirely are not listed here:
#: :func:`translate_for_engine` detects those from the real column lists, which
#: is what lets ``period_type`` survive as ``period`` on HAS_SEGMENT while
#: becoming a null on REPORTS_METRIC, a table that has no such column.
_PROP_RENAME = {
    "legal_name": "name",
    "accession_number": "id",
    "period_end_date": "filing_date",
    "metric_id": "id",
    "statement_type": "statement_category",
    "segment_id": "name",
    "dimension_name": "name",
    "dimension_type": "segment_type",
    "event_id": "id",
    "account_class": "statement_category",
    "period_type": "period",
    "document_text": "text",
    "chunk_type": "section",
}


_REL_BIND_RE = re.compile(r"\[\s*(?:(\w+)\s*)?:\s*(\w+)")
_NODE_BIND_RE = re.compile(r"\(\s*(?:(\w+)\s*)?:\s*(\w+)(?:\s*\{[^}]*\})?\s*\)")
_ACCESSOR_RE = re.compile(r"\b(\w+)\.(\w+)\b")
_TABLE_RENAME_RE = re.compile(
    r"\b(" + "|".join(sorted(_BLUEPRINT_TO_ENGINE, key=len, reverse=True)) + r")\b"
)


def _bindings(cypher: str) -> dict[str, tuple[str, bool]]:
    """Map each pattern variable to its ``(blueprint_table, is_relationship)``."""
    found: dict[str, tuple[str, bool]] = {}
    for m in _REL_BIND_RE.finditer(cypher):
        found[m.group(1) or ""] = (m.group(2), True)
    for m in _NODE_BIND_RE.finditer(cypher):
        found[m.group(1) or ""] = (m.group(2), False)
    return found


def _engine_columns(table: str, is_relationship: bool) -> set[str]:
    """Columns of *table* after renaming, or an empty set when unrecognised."""
    name = _BLUEPRINT_TO_ENGINE.get(table, table)
    if is_relationship:
        spec = REL_TABLES.get(name)
        return set(spec[2]) if spec else set()
    return set(NODE_TABLES.get(name, ()))


def translate_for_engine(cypher: str) -> str:
    """Rewrite blueprint-schema Cypher into engine-schema Cypher.

    A blanket search-and-replace over property names gets two things wrong, and
    both surface as parser errors rather than as nulls -- which is why serving a
    rebuilt graph raised "Cannot find property" on entity queries and
    "Invalid input <x.NULL>" on the retrieval queries:

    * It rewrites the *output* name too, so ``r.scale AS scale`` became
      ``r.NULL AS NULL``, and ``AS NULL`` is not a legal projection item.
    * It leaves the accessor prefix behind, so mapping ``scale`` to the literal
      ``NULL`` turned ``x.scale`` into ``x.NULL`` -- a property read on a
      constant, which does not parse at all.

    So properties are only rewritten in ``alias.property`` position, where the
    pattern variables are known, and one the target table has no column for
    becomes the bare ``NULL`` literal. That preserves the arity of a RETURN
    list, so the positional unpacking at each call site still lines up.
    """
    bindings = _bindings(cypher)

    def rewrite(m: re.Match[str]) -> str:
        alias, prop = m.group(1), m.group(2)
        binding = bindings.get(alias)
        if binding is None:
            return m.group(0)
        table, is_relationship = binding
        renamed = _PROP_RENAME.get(prop, prop)
        if renamed in _engine_columns(table, is_relationship):
            return f"{alias}.{renamed}"
        return "NULL"

    cypher = _ACCESSOR_RE.sub(rewrite, cypher)
    return _TABLE_RENAME_RE.sub(lambda m: _BLUEPRINT_TO_ENGINE[m.group(1)], cypher)


def detect_schema(kg_execute) -> str:
    """``"blueprint"`` or ``"engine"``, decided by which table actually exists.

    Probing the schema rather than trusting the filename means a rebuild that
    produces either shape is served correctly without configuration.
    """
    for query, schema in (
        ("MATCH (m:FinancialMetric) RETURN count(m)", "blueprint"),
        ("MATCH (m:Metric) RETURN count(m)", "engine"),
    ):
        try:
            kg_execute(query)
            return schema
        except Exception:
            continue
    return "blueprint"


def resolve_db_path() -> Path | None:
    """First candidate database that exists on disk.

    The databases are build artefacts and are not in version control, so on a
    fresh clone none of them exist yet and a hard-coded path would abort the
    server with nothing to act on. Resolving here means a clone that *has* run
    the pipeline starts, and one that has not gets told exactly which command
    to run.
    """
    for candidate in _DB_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 5 Canned Cypher reports available at /api/reports
# ---------------------------------------------------------------------------

CANNED_REPORTS: list[dict] = [
    {
        "id":          "report_net_sales_by_company",
        "title":       "Net Sales by Company (all filings)",
        "description": "Highest reported Net Sales value per company across all filings in the graph.",
        "cypher": """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
WHERE m.canonical_name CONTAINS 'Net Sales'
   OR m.canonical_name CONTAINS 'Revenue'
RETURN c.ticker        AS ticker,
       c.legal_name    AS company,
       f.form_type     AS form,
       f.fiscal_year   AS fiscal_year,
       f.fiscal_period AS period,
       m.canonical_name AS metric,
       r.value          AS value,
       r.scale          AS scale,
       r.currency       AS currency
ORDER BY c.ticker, r.value DESC
""",
        "columns": ["ticker", "company", "form", "fiscal_year", "period",
                    "metric", "value", "scale", "currency"],
    },
    {
        "id":          "report_profitability",
        "title":       "Profitability Metrics (Gross / Operating / Net Income)",
        "description": "Gross Profit, Operating Income, and Net Income for every filing in the graph.",
        "cypher": """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
WHERE m.account_class IN ['profit']
RETURN c.ticker        AS ticker,
       f.form_type     AS form,
       f.fiscal_year   AS fiscal_year,
       f.fiscal_period AS period,
       m.canonical_name AS metric,
       r.value          AS value,
       r.scale          AS scale,
       r.currency       AS currency
ORDER BY c.ticker, f.fiscal_year DESC, m.canonical_name
""",
        "columns": ["ticker", "form", "fiscal_year", "period",
                    "metric", "value", "scale", "currency"],
    },
    {
        "id":          "report_segment_breakdown",
        "title":       "Revenue Segment Breakdown",
        "description": "Net Sales disaggregated by product and geographic segments for all companies.",
        "cypher": """
MATCH (m:FinancialMetric)-[d:DISAGGREGATED_BY]->(s:Segment)
WHERE m.canonical_name CONTAINS 'Net Sales'
RETURN m.canonical_name  AS metric,
       s.dimension_name  AS segment,
       s.dimension_type  AS type,
       d.value           AS value,
       d.scale           AS scale,
       d.fiscal_year     AS fiscal_year,
       d.fiscal_period   AS period
ORDER BY d.fiscal_year DESC, d.value DESC
""",
        "columns": ["metric", "segment", "type", "value", "scale",
                    "fiscal_year", "period"],
    },
    {
        "id":          "report_balance_sheet",
        "title":       "Balance Sheet Snapshot (Assets, Liabilities, Equity)",
        "description": "Total Assets, Total Liabilities, and Stockholders Equity across all companies.",
        "cypher": """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
WHERE m.account_class IN ['asset', 'liability', 'equity']
  AND (m.canonical_name CONTAINS 'Total Assets'
    OR m.canonical_name CONTAINS 'Total Liabilities'
    OR m.canonical_name CONTAINS 'Stockholders')
RETURN c.ticker        AS ticker,
       f.fiscal_year   AS fiscal_year,
       f.fiscal_period AS period,
       m.canonical_name AS metric,
       r.value          AS value,
       r.scale          AS scale,
       r.currency       AS currency
ORDER BY c.ticker, f.fiscal_year DESC, m.canonical_name
""",
        "columns": ["ticker", "fiscal_year", "period", "metric",
                    "value", "scale", "currency"],
    },
    {
        "id":          "report_cash_flow",
        "title":       "Cash Flow Summary (Operating / CapEx / Free Cash Flow)",
        "description": "Operating Cash Flow, Capital Expenditures, and Free Cash Flow across all filings.",
        "cypher": """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
WHERE m.statement_type = 'cash_flow'
RETURN c.ticker        AS ticker,
       f.fiscal_year   AS fiscal_year,
       f.fiscal_period AS period,
       m.canonical_name AS metric,
       r.value          AS value,
       r.scale          AS scale,
       r.currency       AS currency
ORDER BY c.ticker, f.fiscal_year DESC, m.canonical_name
""",
        "columns": ["ticker", "fiscal_year", "period", "metric",
                    "value", "scale", "currency"],
    },
]


def run_report(kg: KnowledgeGraph, report_id: str) -> dict:
    """Execute a canned report and return rows + metadata."""
    rpt = next((r for r in CANNED_REPORTS if r["id"] == report_id), None)
    if rpt is None:
        return {"error": f"Unknown report: {report_id}"}
    try:
        rows = kg.execute(rpt["cypher"])
        return {
            "id":          rpt["id"],
            "title":       rpt["title"],
            "description": rpt["description"],
            "columns":     rpt["columns"],
            "rows":        rows,
            "row_count":   len(rows),
        }
    except Exception as exc:
        return {"error": str(exc), "id": report_id}

# ---------------------------------------------------------------------------
# RAG configuration
# ---------------------------------------------------------------------------

def _env_files() -> list[Path]:
    """Env files to read, nearest first: this package, then the repo root."""
    return [_HERE / ".env", _HERE.parent / ".env"]


def _env_map() -> dict[str, str]:
    """Parsed ``KEY=value`` pairs from every env file, nearest file winning."""
    found: dict[str, str] = {}
    for env_file in reversed(_env_files()):
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            found[name.strip()] = value.strip().strip("'\"")
    return found


def _setting(name: str, default: str = "") -> str:
    """One RAG setting: the environment first, then the env files.

    Reading the files is what makes the documented setup work. The variables
    below are process environment today, so a ``.env`` that sets ``RAG_BACKEND``
    would be ignored and the server would quietly come up on Ollama instead --
    the configuration looks applied and is not.
    """
    value = os.environ.get(name, "").strip()
    return value or _env_map().get(name, "").strip() or default


# NVIDIA OpenAI client configuration
NVIDIA_BASE_URL = _setting("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_MODEL = _setting("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")

# Ollama serves an OpenAI-compatible API on localhost, so the same client and
# the same request body work against it -- only the base URL, the model name and
# the timeout change. ``ollama`` is the conventional placeholder key; the local
# server ignores it but the client refuses to construct without one.
OLLAMA_BASE_URL = _setting(
    "RAG_OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"
).rstrip("/")
OLLAMA_MODEL = _setting("RAG_OLLAMA_MODEL", "llama3.2")

#: An explicit pin still wins, so a reader with a working key can force the
#: local model (no network, no cost) by setting ``RAG_BACKEND=ollama``. Anything
#: else -- including the absent case -- resolves on credentials and reachability.
RAG_BACKEND = _setting("RAG_BACKEND", "auto").lower()

#: Generous, because a local 3B model on CPU is slower than a hosted one, and
#: the first request also pays the model load. The UI's fetch has no deadline
#: of its own, so this is the only thing standing between a slow answer and a
#: dropped connection -- "Failed to fetch" in the browser, with the request
#: still running.
RAG_TIMEOUT = float(_setting("RAG_TIMEOUT", "900"))

#: Ollama defaults to a 4k context, which the retrieved subgraph plus the
#: instructions can overrun; a truncated prompt loses the tail of the evidence
#: and the model answers from half a graph.
RAG_NUM_CTX = int(_setting("RAG_NUM_CTX", "16384"))

#: Probing a server that is not running is a refused connection, not a hang, so
#: the deadline only bounds a wedged one. Thirty seconds is short enough that
#: starting ``ollama serve`` after the UI is already up is noticed without a
#: restart, and long enough that the probe is not repeated on every question.
_OLLAMA_PROBE_TTL = 30.0
_OLLAMA_PROBE_TIMEOUT = 2.0


class RagBackends:
    """Resolves which model phrases answers, re-evaluated on every request.

    The backend used to be one module constant read at import, which made the
    wrong choice permanent: a reader with a working key in ``.env`` still got
    the local backend unless they also knew to set ``RAG_BACKEND=nvidia``, and
    the only remedy was editing a file and restarting. Resolution is a function
    of observable state -- which credentials exist, whether the local server
    answers -- so it belongs here rather than in a name someone has to remember.

    A key entered in the browser lives here and nowhere else. It is not written
    to ``.env``, not logged, and not included in any response body; it dies with
    the process.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session_key = ""
        self._forced = RAG_BACKEND if RAG_BACKEND in ("nvidia", "ollama") else ""
        self._stored_key_rejected = ""
        self._probe: tuple[float, bool, list[str]] = (0.0, False, [])

    def set_session_key(self, key: str) -> None:
        with self._lock:
            self._session_key = key.strip()
            self._stored_key_rejected = ""

    def clear_session_key(self) -> None:
        with self._lock:
            self._session_key = ""

    def set_backend(self, name: str) -> None:
        """Pin a backend, or pass ``"auto"`` to hand the choice back."""
        with self._lock:
            self._forced = name.strip().lower() if name.strip().lower() in ("nvidia", "ollama") else ""

    def reject_key(self) -> str:
        """Record that the active key was refused upstream, and return its source.

        A 401 or 403 means the credential exists and was not accepted. Left
        alone, auto-resolution would pick the same key again for the next
        question, so the reader would be offered the identical failure once per
        attempt. The stored key is marked rather than the session key cleared so
        that a key typed into the browser survives a rotation of the stored one.
        """
        with self._lock:
            if self._session_key:
                self._session_key = ""
                return "the key entered in this browser"
            self._stored_key_rejected = "1"
            return "the key in .env or the environment"

    def ollama_models(self, refresh: bool = False) -> tuple[bool, list[str]]:
        """Whether the local server answers, and which models it has pulled."""
        with self._lock:
            checked, reachable, models = self._probe
            if not refresh and (time.monotonic() - checked) < _OLLAMA_PROBE_TTL:
                return reachable, models
        reachable, models = self._probe_ollama()
        with self._lock:
            self._probe = (time.monotonic(), reachable, models)
        return reachable, models

    def _probe_ollama(self) -> tuple[bool, list[str]]:
        url = f"{OLLAMA_BASE_URL}/models"
        try:
            with urllib.request.urlopen(url, timeout=_OLLAMA_PROBE_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, []
        names = []
        for entry in payload.get("data") or []:
            name = (entry or {}).get("id")
            if name:
                names.append(str(name))
        return True, sorted(names)

    def resolve(self) -> dict[str, Any]:
        """The backend to use now, and why. Never raises."""
        stored = _load_api_key()
        with self._lock:
            session_key = self._session_key
            forced = self._forced
            stored_rejected = bool(self._stored_key_rejected)

        reachable, models = self.ollama_models()
        base_key = {
            "key_present": False,
            "key_source": "",
            "stored_key_rejected": stored_rejected,
            "ollama_reachable": reachable,
            "ollama_models": models,
            "forced": forced,
        }

        if forced == "ollama":
            return {**base_key, "backend": "ollama", "base_url": OLLAMA_BASE_URL,
                    "model": OLLAMA_MODEL, "reason": "RAG_BACKEND=ollama pins the local model."}
        if forced == "nvidia":
            if session_key or (stored and not stored_rejected):
                return self._nvidia(session_key or stored, base_key)
            return {**base_key, "backend": "none", "base_url": NVIDIA_BASE_URL,
                    "model": NVIDIA_MODEL,
                    "reason": "RAG_BACKEND=nvidia is set but no API key was found."}

        if session_key:
            return self._nvidia(session_key, base_key)
        if stored and not stored_rejected:
            return self._nvidia(stored, base_key)
        if reachable:
            reason = ("No NVIDIA key found, and the local model server is running, "
                      f"so answers use {OLLAMA_MODEL}.")
            if stored_rejected:
                reason = ("The stored NVIDIA key was refused upstream, so answers "
                          f"use the local model {OLLAMA_MODEL} instead.")
            return {**base_key, "backend": "ollama", "base_url": OLLAMA_BASE_URL,
                    "model": OLLAMA_MODEL, "reason": reason}
        return {**base_key, "backend": "none", "base_url": "", "model": "",
                "reason": ("No NVIDIA key was found and no local model server is "
                           "running, so there is nothing to phrase answers with.")}

    def _nvidia(self, key: str, base: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            source = "browser" if self._session_key else "file"
        reason = ("Using the hosted NVIDIA model." if source == "file"
                  else "Using the hosted NVIDIA model with the key from this browser.")
        return {**base, "backend": "nvidia", "base_url": NVIDIA_BASE_URL,
                "model": NVIDIA_MODEL, "key_present": True, "key_source": source,
                "reason": reason}

    def credentials(self, backend: str) -> tuple[str, str]:
        """``(api_key, base_url)`` for *backend*. Local runs on a placeholder."""
        if backend != "nvidia":
            return "ollama", OLLAMA_BASE_URL
        with self._lock:
            session_key = self._session_key
        return session_key or _load_api_key(), NVIDIA_BASE_URL


def get_backends() -> RagBackends:
    """The backend registry, built on first use and cached thereafter.

    ``RagBackends`` owns mutable process state -- a lock, the key typed into the
    browser, a forced-backend pin -- so there is exactly one of them and it has
    to be shared. What it must not do is exist before anything asks for it:
    constructing it reads credential files and probes for a local Ollama, and
    doing that merely because a module was imported makes ``import
    sandbox_engine.query_ui`` cost something and reach the filesystem.

    Every reference goes through here rather than naming the global. A module
    ``__getattr__`` fires for attribute access (``query_ui.BACKENDS``) but *not*
    for a bare global lookup inside a function, so a plain ``get_backends().resolve()``
    raises ``NameError`` in a fresh process -- it only appeared to work in the
    test suite because a test had already touched the module attribute first.
    Reading ``globals()`` here keeps one code path for both spellings and stays
    correct whether the name is absent, lazily built, or patched.
    """
    backends = globals().get("BACKENDS")
    if backends is None:
        backends = RagBackends()
        globals()["BACKENDS"] = backends
    return backends


def __getattr__(name: str) -> Any:
    """Expose the backend registry as a module attribute (PEP 562).

    Keeps ``query_ui.BACKENDS`` readable -- and patchable, which the test suite
    relies on -- without constructing it during import.
    """
    if name == "BACKENDS":
        return get_backends()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_api_key() -> str:
    """The key for the question box, in precedence order.

    1. the ``NVIDIA_API_KEY`` (or ``OPENAI_API_KEY``) environment variable;
    2. ``sandbox_engine/.env``;
    3. the repository root ``.env``.

    Both names and both files are accepted because the root ``.env`` is the one
    a reader is told to copy from ``.env.example``, and it carries
    ``OPENAI_API_KEY`` for the legacy ``graphrag`` package while this module
    historically looked only for ``NVIDIA_API_KEY`` in its own directory. A
    setup step that silently does nothing is worse than one spelled out.

    A parser rather than ``python-dotenv``, which is not an dependency here: the
    file is a flat ``KEY=value`` list and a regex is enough for it. The key used
    to be a literal in this source file, which put a live credential in a file
    people copy around; everything except the question box works without one.

    Called on demand, never at import. A module-level ``NVIDIA_API_KEY =
    _load_api_key()`` read a live credential into module state the moment
    anything imported this file, and nothing ever read it back --
    ``RagBackends`` already calls this per resolution, so a key typed into the
    browser or added to the environment took effect immediately while the
    cached copy went stale and only lingered.
    """
    for name in ("NVIDIA_API_KEY", "OPENAI_API_KEY"):
        found = _setting(name)
        if found:
            return found
    return ""


MAX_BODY = 128 * 1024

_CITATION_RE = re.compile(r"\[(E\d+)\]")

ANSWER_SYSTEM = """\
You answer questions using only a tagged evidence block retrieved from financial filings.

Every line of evidence carries a bracketed tag like [E1] or [E12], and a Source showing
the form and period it came from.

Rules:
- Use only the supplied evidence. If it does not contain the answer, say so plainly and state what is missing.
- You may ONLY cite tags that already appear in the evidence. Do not invent a tag, and do not cite a tag you were not given.
- Cite every factual claim using bracketed tags like [E1] or [E2].
- If you compute a value from cited facts, show the arithmetic so the derivation is visible.
- Never state a number that is not in the evidence and not derived from it.
- Do not use outside knowledge. If a fact is not in the evidence, treat it as unknown rather than supplying it.
- Financial values include scale properties: scale=6 means in millions (e.g. 416161.0 scale=6 is $416,161 million USD). State the units clearly.
- Answer directly and factually in clean prose or concise bullet points. No preamble.
"""


# ── Database Layer ────────────────────────────────────────────────────────────

class KnowledgeGraph:
    def __init__(self, db_path: Path, read_only: bool = True):
        # Read-only by default: this UI never writes, and LadybugDB takes an
        # exclusive lock on a read-write handle, so a read-write open makes a
        # second server on another port fail to start at all.
        self.db = lb.Database(str(db_path), read_only=read_only)
        self.conn = lb.Connection(self.db)
        self.lock = threading.Lock()
        self.schema = detect_schema(self._raw_execute)

    def close(self):
        self.conn.close()
        self.db.close()

    def _raw_execute(self, cypher: str, params: dict | None = None) -> list[list[Any]]:
        with self.lock:
            res = self.conn.execute(cypher, params or {})
            return [list(r) for r in res.get_all()]

    def execute(self, cypher: str, params: dict | None = None) -> list[list[Any]]:
        """Run *cypher*, translating it first if this is an engine-schema graph.

        Every query in this module is written in blueprint terms. On an
        engine-schema database they are rewritten here, so the rest of the file
        -- and the canned reports -- need no schema awareness at all.
        """
        if self.schema == "engine":
            cypher = translate_for_engine(cypher)
        return self._raw_execute(cypher, params)

    def has_company(self, ticker: str) -> bool:
        """Check if a company with the given ticker exists in the graph."""
        if not ticker:
            return False
        rows = self.execute(
            "MATCH (c:Company {ticker: $ticker}) RETURN c.ticker LIMIT 1",
            {"ticker": ticker.strip().upper()},
        )
        return len(rows) > 0

    def stats(self) -> dict[str, Any]:
        state = get_backends().resolve()
        node_tables = ["Company", "Filing", "FinancialMetric", "Segment", "DisclosureEvent", "DocumentChunk"]
        rel_tables = ["SUBMITTED", "REPORTS_METRIC", "DISAGGREGATED_BY", "DISCLOSES_EVENT", "CONTAINS_CHUNK"]
        counts = {}
        for t in node_tables:
            try:
                counts[t] = self.execute(f"MATCH (n:{t}) RETURN count(n)")[0][0]
            except Exception:
                counts[t] = 0
        rel_counts = {}
        for r in rel_tables:
            try:
                rel_counts[r] = self.execute(f"MATCH ()-[x:{r}]->() RETURN count(x)")[0][0]
            except Exception:
                rel_counts[r] = 0
        total_nodes = sum(counts.values())
        total_rels = sum(rel_counts.values())
        entity_types = [(t, counts[t]) for t in node_tables if counts[t] > 0]
        return {
            "nodes": total_nodes,
            "edges": total_rels,
            "table_counts": counts,
            "rel_counts": rel_counts,
            "entity_types": entity_types,
            "schema": self.schema,
            "rag_model": state["model"],
            "rag_backend": state["backend"],
            "rag_reason": state["reason"],
            "rag_needs_input": state["backend"] == "none",
            "rag_key_source": state["key_source"],
            "rag_stored_key_rejected": state["stored_key_rejected"],
            "rag_ollama_reachable": state["ollama_reachable"],
            "rag_ollama_models": state["ollama_models"],
            "rag_forced": state["forced"],
        }

    def all_entities(self, query: str = "", limit: int = 500) -> list[dict[str, Any]]:
        q = query.lower().strip()
        entities = []

        # 1. Company
        for r in self.execute("MATCH (c:Company) RETURN c.ticker, c.legal_name"):
            tid, name = r[0], r[1]
            if not q or q in tid.lower() or q in name.lower() or q in "company":
                # The ticker is the id and the only thing that distinguishes two
                # issuers sharing a legal name, so it doubles as the label hint.
                entities.append({"id": tid, "name": name, "label_hint": tid, "entity_type": "Company", "description": f"Ticker: {tid}"})

        # 2. Filing
        # A filing's label says nothing about who filed it, so a graph spanning
        # several issuers renders one identical box per issuer's 10-K. Fetch the
        # filer to break the tie.
        filer: dict[str, str] = {}
        try:
            for acc, tick in self.execute(
                "MATCH (c:Company)-[:SUBMITTED]->(f:Filing) RETURN f.accession_number, c.ticker"
            ):
                filer[acc] = tick
        except Exception:
            pass
        for r in self.execute("MATCH (f:Filing) RETURN f.accession_number, f.form_type, f.fiscal_year, f.fiscal_period, f.period_end_date"):
            acc, form, fy, fp, ped = r[0], r[1], r[2], r[3], r[4]
            name = f"{form} FY{fy} ({fp})"
            desc = f"Form {form}, Fiscal Year {fy}, Period {fp}, Period End: {ped}"
            if not q or q in name.lower() or q in form.lower() or q in str(fy) or q in "filing":
                entities.append({"id": acc, "name": name, "label_hint": filer.get(acc, acc), "entity_type": "Filing", "description": desc})

        # 3. Segments
        for r in self.execute("MATCH (s:Segment) RETURN s.segment_id, s.dimension_name, s.dimension_type"):
            sid, name, dtype = r[0], r[1], r[2]
            if not q or q in name.lower() or q in dtype.lower() or q in "segment":
                entities.append({"id": sid, "name": name, "entity_type": "Segment", "description": f"Dimension: {dtype}"})

        # 4. DisclosureEvent
        for r in self.execute("MATCH (e:DisclosureEvent) RETURN e.event_id, e.item_code, e.item_title, e.summary"):
            eid, code, title, summary = r[0], r[1], r[2], r[3]
            name = f"Item {code}: {title.strip()[:40]}"
            if not q or q in name.lower() or q in code.lower() or q in "event":
                entities.append({"id": eid, "name": name, "entity_type": "DisclosureEvent", "description": summary[:120]})

        # 5. FinancialMetric
        for r in self.execute("MATCH (m:FinancialMetric) RETURN m.metric_id, m.canonical_name, m.statement_type"):
            mid, name, stype = r[0], r[1], r[2]
            if not q or q in name.lower() or q in mid.lower() or q in stype.lower() or q in "metric":
                entities.append({"id": mid, "name": name, "entity_type": "FinancialMetric", "description": f"Statement: {stype}"})

        return entities[:limit]

    def neighborhood(self, seed_ids: list[str], hops: int = 2, limit: int = 150) -> dict[str, Any]:
        nodes_dict: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []

        all_ents = {e["id"]: e for e in self.all_entities(limit=1000)}

        for sid in seed_ids:
            if sid in all_ents:
                nodes_dict[sid] = all_ents[sid]

        # Fetch connecting relationships
        queries = [
            # SUBMITTED: Company -> Filing
            ("MATCH (a:Company)-[r:SUBMITTED]->(b:Filing) RETURN a.ticker, b.accession_number, 'SUBMITTED', 'Submitted filing'", "ticker", "accession_number"),
            # REPORTS_METRIC: Filing -> Metric
            ("MATCH (a:Filing)-[r:REPORTS_METRIC]->(b:FinancialMetric) RETURN a.accession_number, b.metric_id, 'REPORTS_METRIC', 'value=' + toString(r.value) + ' (' + toString(r.period_type) + ')'", "accession_number", "metric_id"),
            # DISAGGREGATED_BY: Metric -> Segment
            ("MATCH (a:FinancialMetric)-[r:DISAGGREGATED_BY]->(b:Segment) RETURN a.metric_id, b.segment_id, 'DISAGGREGATED_BY', 'segment value=' + toString(r.value)", "metric_id", "segment_id"),
            # DISCLOSES_EVENT: Filing -> DisclosureEvent
            ("MATCH (a:Filing)-[r:DISCLOSES_EVENT]->(b:DisclosureEvent) RETURN a.accession_number, b.event_id, 'DISCLOSES_EVENT', 'Discloses event'", "accession_number", "event_id"),
        ]

        seen_edges = set()
        for cql, _, _ in queries:
            try:
                for row in self.execute(cql):
                    u, v, rel, desc = row[0], row[1], row[2], row[3]
                    # if seed_ids given, restrict to incident
                    if seed_ids and (u not in seed_ids and v not in seed_ids):
                        continue
                    if u in all_ents:
                        nodes_dict[u] = all_ents[u]
                    if v in all_ents:
                        nodes_dict[v] = all_ents[v]
                    edge_key = (u, v, rel)
                    if edge_key not in seen_edges and len(edges) < limit:
                        seen_edges.add(edge_key)
                        edges.append({"source": u, "target": v, "relation": rel, "description": desc})
            except Exception:
                continue

        return {
            "seeds": seed_ids,
            "nodes": merge_nodes(list(nodes_dict.values())),
            "edges": merge_edges(edges),
        }


# ── RAG Retrieval & QA Engine ─────────────────────────────────────────────────

def retrieve_financial_context(kg: KnowledgeGraph, question: str, ticker: str | None = None) -> tuple[str, list[dict], list[dict], dict[str, str], list[str]]:
    """Retrieves relevant entities and relations matching the question and formats context with tags [E1], [E2]..."""
    q_low = question.lower()

    # Identify candidate seeds
    seed_nodes: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, name: str, etype: str, desc: str = "", hint: str = ""):
        if nid not in seen_ids:
            seen_ids.add(nid)
            seed_nodes.append({"id": nid, "name": name, "label_hint": hint, "type": etype, "description": desc})

    # Include the core company - specifically filtered to resolved entity if given
    if ticker:
        companies = kg.execute("MATCH (c:Company {ticker: $ticker}) RETURN c.ticker, c.legal_name, c.cik", {"ticker": ticker})
    else:
        companies = kg.execute("MATCH (c:Company) RETURN c.ticker, c.legal_name, c.cik")
    for c in companies:
        add_node(c[0], c[1], "Company", f"Ticker: {c[0]}, CIK: {c[2]}", hint=c[0])

    # Check filings. The filer is part of the label: "10-K FY2026 (FY)" is what
    # every issuer's annual report looks like, so without it a multi-company
    # graph renders one indistinguishable box per filing.
    filer: dict[str, str] = {}
    try:
        for acc, tick in kg.execute(
            "MATCH (c:Company)-[:SUBMITTED]->(f:Filing) RETURN f.accession_number, c.ticker"
        ):
            filer[acc] = tick
    except Exception:
        pass
    filings = kg.execute("MATCH (f:Filing) RETURN f.accession_number, f.form_type, f.fiscal_year, f.fiscal_period, f.period_end_date")
    for f in filings:
        acc, form, fy, fp, ped = f[0], f[1], f[2], f[3], f[4]
        # Filter filings to resolved company if ticker is known
        if ticker and filer.get(acc) and filer[acc] != ticker:
            continue
        hint = filer.get(acc, acc)
        # match 10-k, 10-q, 8-k, annual, quarterly, 2025
        if ("10-k" in q_low or "annual" in q_low or "year" in q_low) and form == "10-K":
            add_node(acc, f"{form} FY{fy} ({fp})", "Filing", f"Form {form}, Fiscal Year {fy}, Period Ended {ped}", hint)
        elif ("10-q" in q_low or "quarter" in q_low or "q3" in q_low) and form == "10-Q":
            add_node(acc, f"{form} FY{fy} ({fp})", "Filing", f"Form {form}, Fiscal Year {fy}, Period Ended {ped}", hint)
        elif ("8-k" in q_low or "event" in q_low or "press release" in q_low or "disclosure" in q_low) and form == "8-K":
            add_node(acc, f"{form} ({ped})", "Filing", f"Form {form} Current Report, Filed {ped}", hint)
        else:
            # If general query, include all filings
            add_node(acc, f"{form} FY{fy} ({fp})", "Filing", f"Form {form}, Fiscal Year {fy}, Period Ended {ped}", hint)

    # Check metrics
    metrics = kg.execute("MATCH (m:FinancialMetric) RETURN m.metric_id, m.canonical_name, m.statement_type, m.account_class")
    # Question text with punctuation collapsed so "shareholders' equity" (straight
    # or curly apostrophe) always matches a stored "shareholders' equity" label.
    q_norm = re.sub(r"[^a-z0-9\s]", " ", q_low)
    q_norm = re.sub(r"\s+", " ", q_norm).strip()
    for m in metrics:
        mid, cname, stype, aclass = m[0], m[1], m[2], m[3]
        name_clean = re.sub(r"\(.*?\)", "", cname).strip().lower()
        keywords = [re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", name_clean)).strip()]
        if "sales" in name_clean or "revenue" in name_clean:
            keywords.extend(["sales", "revenue", "top line", "margin", "profit"])
        if "profit" in name_clean:
            keywords.extend(["profit", "gross margin", "margin"])
        if "income" in name_clean:
            keywords.extend(["income", "operating income", "net income",
                             "earnings", "operating margin", "margin"])
        if "research" in name_clean:
            keywords.extend(["r&d", "research", "development"])
        if "operating expense" in name_clean:
            keywords.extend(["opex", "operating expense", "expenses"])
        if "cost" in name_clean:
            keywords.extend(["cost", "cogs"])

        if any(kw and kw in q_norm for kw in keywords):
            add_node(mid, cname, "FinancialMetric", f"Statement: {stype}, Class: {aclass}")

    # Check segments
    segments = kg.execute("MATCH (s:Segment) RETURN s.segment_id, s.dimension_name, s.dimension_type")
    for s in segments:
        sid, name, dtype = s[0], s[1], s[2]
        name_l = (name or "").lower()
        dtype_l = (dtype or "").lower()
        if name_l in q_low or dtype_l in q_low or "segment" in q_low or "breakdown" in q_low or "geograph" in q_low or "product" in q_low:
            add_node(sid, name, "Segment", f"Dimension type: {dtype}")

    # Check disclosure events
    events = kg.execute("MATCH (e:DisclosureEvent) RETURN e.event_id, e.item_code, e.item_title, e.summary")
    for e in events:
        eid, code, title, summary = e[0], e[1], e[2], e[3]
        if (code or "") in q_low or "8-k" in q_low or "event" in q_low or "item" in q_low or "press release" in q_low or "operation" in q_low:
            clean_title = re.sub(r"&[a-z0-9#]+;", " ", (title or "")).strip()
            add_node(eid, f"Item {code}: {clean_title}", "DisclosureEvent", f"Summary: {(summary or '')[:160]}")

    # If too few nodes matched (e.g. broad general question), populate with top metrics
    if len(seen_ids) <= 3:
        for m in metrics:
            mid, cname, stype, aclass = m[0], m[1], m[2], m[3]
            if any(term in mid.lower() for term in ["netsales", "grossprofit", "operatingincome", "netincome", "researchdevelopment"]):
                add_node(mid, cname, "FinancialMetric", f"Statement: {stype}, Class: {aclass}")
        for s in segments[:10]:
            add_node(s[0], s[1], "Segment", f"Dimension type: {s[2]}")

    # Narrative prose. The Item 1 business description, MD&A and risk-factor
    # text live in DocumentChunk nodes, which none of the seed rules above
    # reach (they only match filings, metrics, segments and events) -- so a
    # general question like "what does apple do?" used to get an
    # entity-and-edge-only context with nothing to answer from. Pull a bounded
    # set of chunks from each seeded narrative filing, preferring the ones
    # whose text mentions the question's own terms, and let the answer be
    # grounded in the issuer's prose rather than its table rows.
    _STOP = {"what", "does", "do", "is", "are", "was", "were", "has", "have",
             "the", "a", "an", "and", "or", "of", "to", "in", "for", "on",
             "it", "this", "that", "with", "how", "much", "many", "did"}
    # Terms that mark a chunk as *describing the business itself*, not one
    # metric or one line item. A general question ("what does apple do?")
    # rarely shares words with the answer: the opening Item 1 sentence is "The
    # Company designs, manufactures and markets smartphones..." -- no "apple",
    # no "do" -- so a literal keyword match alone would keep picking the
    # "Apple News"/"Apple TV" service paragraphs and miss the hardware line.
    _BIZ_TERMS = ("design", "manufactur", "products", "services", "market",
                  "sells", "sale of", "operat", "develop", "hardware",
                  "software", "device", "wearab", "smartphone", "computer",
                  "tablet", "subsidiar", "segment")
    _BIZ_WEIGHT = 2
    keywords = set(re.findall(r"[a-z0-9]+", q_low)) - _STOP
    chunk_hits: list[dict[str, Any]] = []
    for acc, form in ((f[0], f[1]) for f in filings):
        if form not in ("10-K", "10-Q") or acc not in seen_ids:
            continue
        for chunk_acc, cid, ctext, csection in kg.execute(
            "MATCH (f:Filing)-[:CONTAINS_CHUNK]->(c:DocumentChunk) "
            "RETURN f.id, c.id, c.text, c.section"
        ):
            if chunk_acc != acc:
                continue
            text_low = (ctext or "").lower()
            q_score = sum(1 for kw in keywords if kw in text_low)
            biz_score = sum(1 for term in _BIZ_TERMS if term in text_low)
            chunk_hits.append(
                {"filing": acc, "id": cid, "text": ctext or "", "section": csection or "",
                 "score": q_score + _BIZ_WEIGHT * biz_score, "form": form}
            )
    # Best score first, then 10-K before 10-Q, then a stable tie-break. Never
    # more than ``_MAX_CHUNK_CONTEXT`` chunks total, so the prompt stays
    # bounded.
    _MAX_CHUNK_CONTEXT = 8
    _MAX_RISK_FACTORS = 6
    _MAX_CAUSAL = 10
    selected_chunks = sorted(
        chunk_hits,
        key=lambda c: (-c["score"], c["form"] != "10-K", c["id"]),
    )[:_MAX_CHUNK_CONTEXT]
    for c in selected_chunks:
        text = re.sub(r"\s+", " ", c["text"]).strip()
        add_node(
            c["id"],
            f"{c['section']} chunk: {text[:48]}{'…' if len(text) > 48 else ''}",
            "DocumentChunk",
            text,
            hint=c["form"],
        )

    # UFGS structural layer: Section nodes (item index -> title). The parser
    # extracts every regulated item of a 10-K/10-Q (Item 1 Business, Item 7
    # MD&A, Item 1C Cybersecurity, ...) but those nodes were never sent to the
    # model, so "what is the title of 10-K Item 7" was unanswerable even though
    # the title sits in the graph. Pull the section index of every seeded
    # narrative filing when the question is about the document structure.
    section_nodes: list[dict[str, Any]] = []
    if any(t in q_low for t in ("item", "section", "10-k", "10-q",
                                "10k", "10q", "mda", "management's",
                                "management analysis", "risk")):
        for sacc, sid, scode, stitle in kg.execute(
            "MATCH (f:Filing)-[:CONTAINS_SECTION]->(s:Section) "
            "RETURN f.id, s.id, s.item_code, s.section_title"
        ):
            if sacc not in seen_ids:
                continue
            section_nodes.append(
                {"filing": sacc, "id": sid, "code": scode, "title": stitle}
            )
            add_node(
                sid,
                f"Item {scode}: {stitle}",
                "Section",
                f"Item {scode} · {stitle}",
                hint=sacc,
            )

    # UFGS causal layer: RiskFactor nodes (Item 1A) and CausalRelation
    # statements. Both live in the graph but no retrieval rule reached them, so
    # every question about the risk narrative or the typed causal edges came
    # back "not in context". Risk factors surface when the question is about
    # risks; causal statements when it is about drivers, exposures, suppliers,
    # customers, currency or margins.
    if any(t in q_low for t in ("risk", "threat", "exposure", "factor", "1a",
                                "uncertain", "macro", "econom", "inflation",
                                "currency", "exchange", "supply")):
        rf_descs: list[str] = []
        for rid, rcode, header, rtext in kg.execute(
            "MATCH (n:RiskFactor) RETURN n.id, n.item_code, n.rf_header, n.rf_text"
        ):
            low = (header or "").lower() + " " + (rtext or "").lower()
            score = sum(1 for kw in keywords if kw in low)
            rf_descs.append((score, rid, rcode, header, rtext))
        for score, rid, rcode, header, rtext in sorted(
            rf_descs, key=lambda t: (-t[0], t[1])
        )[:_MAX_RISK_FACTORS]:
            text = re.sub(r"\s+", " ", rtext or "").strip()
            add_node(
                rid,
                f"Risk factor ({rcode}): {(header or '')[:50]}",
                "RiskFactor",
                f"{header}\n{text[:600]}",
                hint="10-K",
            )
    if any(t in q_low for t in ("causal", "drives", "driven", "margin",
                                "expos", "impact", "driver", "supplier",
                                "customer", "competitor", "macro", "inflation",
                                "interest rate", "currency", "fx", "commod",
                                "offset", "risk")):
        causal_descs: list[dict[str, Any]] = []
        for cid, rtype, subj, obj, quote in kg.execute(
            "MATCH (n:CausalRelation) RETURN n.id, n.relation_type, "
            "n.subject_name, n.object_name, n.source_quote"
        ):
            line = f"{subj} {rtype} {obj}".lower()
            score = sum(1 for kw in keywords if kw in line)
            causal_descs.append(
                {"id": cid, "type": rtype, "subject": subj, "object": obj,
                 "quote": quote, "score": score}
            )
        for c in sorted(causal_descs, key=lambda c: (-c["score"], c["id"]))[:_MAX_CAUSAL]:
            quote = re.sub(r"\s+", " ", c["quote"] or "").strip()
            desc = f"{c['subject']} {c['type']} {c['object']}"
            if quote:
                desc += f" — \"{quote[:240]}\""
            add_node(
                c["id"],
                f"{c['subject']} → {c['object']}",
                "CausalRelation",
                desc,
                hint=c["type"],
            )

    # Now fetch connecting edges
    retrieved_edges: list[dict] = []
    retrieved_nodes = list(seed_nodes)

    # Map tag IDs E1, E2, ...
    tag_map: dict[str, str] = {}     # tag -> node_id
    id_to_tag: dict[str, str] = {}   # node_id -> tag

    for idx, node in enumerate(retrieved_nodes, start=1):
        tag = f"E{idx}"
        tag_map[tag] = node["id"]
        id_to_tag[node["id"]] = tag

    # Query specific relationships between retrieved nodes
    node_ids = set(seen_ids)

    # 1. Company -> Filing
    for r in kg.execute("MATCH (c:Company)-[:SUBMITTED]->(f:Filing) RETURN c.ticker, f.accession_number"):
        if r[0] in node_ids and r[1] in node_ids:
            retrieved_edges.append({
                "source": r[0], "target": r[1],
                "relation": "SUBMITTED",
                "description": "Company filed report with the SEC",
            })

    # 2. Filing -> FinancialMetric
    for r in kg.execute(
        "MATCH (f:Filing)-[x:REPORTS_METRIC]->(m:FinancialMetric) "
        "RETURN f.accession_number, m.metric_id, x.value, x.scale, x.currency, x.period_type, x.raw_label"
    ):
        f_acc, m_id, val, scale, curr, ptype, label = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        if f_acc in node_ids and m_id in node_ids:
            scale_desc = "in millions" if scale == 6 else ("in thousands" if scale == 3 else "")
            desc = f"Reported {label or ''}: value={val:,.2f} {curr} ({scale_desc}), period={ptype}"
            retrieved_edges.append({
                "source": f_acc, "target": m_id,
                "relation": "REPORTS_METRIC",
                "description": desc,
                "value": val, "scale": scale, "period_type": ptype,
            })

    # 3. FinancialMetric -> Segment
    for r in kg.execute(
        "MATCH (m:FinancialMetric)-[d:DISAGGREGATED_BY]->(s:Segment) "
        "RETURN m.metric_id, s.segment_id, d.value, d.scale, d.fiscal_year, d.fiscal_period"
    ):
        m_id, s_id, val, scale, fy, fp = r[0], r[1], r[2], r[3], r[4], r[5]
        if m_id in node_ids and s_id in node_ids:
            scale_desc = "in millions" if scale == 6 else ("in thousands" if scale == 3 else "")
            desc = f"Disaggregated segment value={val:,.2f} ({scale_desc}), FY{fy} {fp}"
            retrieved_edges.append({
                "source": m_id, "target": s_id,
                "relation": "DISAGGREGATED_BY",
                "description": desc,
                "value": val, "scale": scale,
            })

    # 4. Filing -> DisclosureEvent
    for r in kg.execute("MATCH (f:Filing)-[:DISCLOSES_EVENT]->(e:DisclosureEvent) RETURN f.accession_number, e.event_id"):
        if r[0] in node_ids and r[1] in node_ids:
            retrieved_edges.append({
                "source": r[0], "target": r[1],
                "relation": "DISCLOSES_EVENT",
                "description": "Filing discloses event",
            })

    # 5. Filing -> DocumentChunk (narrative prose selected above)
    for c in selected_chunks:
        if c["filing"] in node_ids and c["id"] in node_ids:
            retrieved_edges.append({
                "source": c["filing"], "target": c["id"],
                "relation": "CONTAINS_CHUNK",
                "description": f"{c['form']} {c['section']} prose",
            })

    # 6. Filing -> Section (UFGS structural index)
    if section_nodes:
        for s in section_nodes:
            if s["filing"] in node_ids and s["id"] in node_ids:
                retrieved_edges.append({
                    "source": s["filing"], "target": s["id"],
                    "relation": "CONTAINS_SECTION",
                    "description": f"Item {s['code']} · {s['title']}",
                })

    # Dedupe before the context string is built, not only at the JSON boundary:
    # the same edge is reachable from more than one of the queries above, and a
    # repeated "[E7] --REPORTS_METRIC--> [E9]" line costs prompt budget while
    # telling the model nothing new.
    retrieved_edges = merge_edges(retrieved_edges)

    # Build context string for prompt
    ent_lines = ["ENTITIES:"]
    for node in retrieved_nodes:
        t = id_to_tag[node["id"]]
        desc_part = f": {node['description']}" if node.get("description") else ""
        ent_lines.append(f'[{t}] "{node["name"]}" ({node["type"]}){desc_part}')

    rel_lines = ["\nRELATIONSHIPS:"]
    for e in retrieved_edges:
        s_tag = id_to_tag.get(e["source"])
        t_tag = id_to_tag.get(e["target"])
        if s_tag and t_tag:
            desc_part = f": {e['description']}" if e.get("description") else ""
            rel_lines.append(f"[{s_tag}] --{e['relation']}--> [{t_tag}]{desc_part}")

    context_str = "\n".join(ent_lines) + "\n" + "\n".join(rel_lines)
    return context_str, retrieved_nodes, retrieved_edges, tag_map, [n["id"] for n in seed_nodes[:5]]


def _explain_api_error(exc: Exception, state: dict[str, Any]) -> str:
    """Turn an upstream failure into something the reader can act on.

    A bare ``403 Authorization failed`` in the answer box looks like a bug in
    this app. It is not: the key authenticated, so the request was refused
    further up, and the two refusals mean different things. The local backend
    has its own common failure -- the server simply is not running -- which is
    worth naming, because "Failed to fetch" gives the reader nothing to act on.
    """
    status = getattr(exc, "status_code", None)
    if state["backend"] == "ollama":
        name = type(exc).__name__
        if name in ("APIConnectionError", "ConnectError", "ConnectionError"):
            return (
                f"Could not reach the local model server at {OLLAMA_BASE_URL}. "
                f"Start it with `ollama serve` and confirm "
                f"`ollama list` shows {OLLAMA_MODEL}. The graph explorer below "
                f"does not need it."
            )
        if name in ("APITimeoutError", "Timeout"):
            return (
                f"{OLLAMA_MODEL} did not answer within {RAG_TIMEOUT:.0f}s. A local "
                f"model on CPU can be slow; raise RAG_TIMEOUT if the question "
                f"is a large one."
            )
        return f"Error talking to the local model {OLLAMA_MODEL}: {exc}"
    if status in (401, 403):
        where = get_backends().reject_key()
        recovered = get_backends().resolve()
        if recovered["backend"] == "ollama":
            tail = (f" Answers are no longer routed to a refused key; the next "
                    f"question will use the local model {OLLAMA_MODEL} instead.")
        else:
            tail = (" No local model server is running, so enter a working key "
                    "above before asking again.")
        return (
            f"NVIDIA refused the request ({status}). The key from {where} "
            f"authenticates but has no inference entitlement for {NVIDIA_MODEL}, "
            f"so answering is unavailable. Check the key's permissions at "
            f"build.nvidia.com, or enter a different one above.{tail} The graph "
            f"explorer below does not need it."
        )
    if status in (410, 504):
        return (
            f"{NVIDIA_MODEL} is unavailable upstream ({status}). NVIDIA's "
            f"deployment is retired or timing out; pick another model, or run "
            f"the local backend with RAG_BACKEND=ollama."
        )
    if status == 429:
        return "Rate limited by NVIDIA (429). Wait a moment and try again."
    return f"Error communicating with {NVIDIA_MODEL}: {exc}"


def _unavailable_answer(state: dict[str, Any]) -> dict[str, Any]:
    """The payload for a question that has no model to phrase it.

    The reader can act on this, so it names the two ways out rather than
    reporting a missing setting: paste a key, or start the local server. The
    graph explorer keeps working either way, which the message says outright.
    """
    return {
        "error": state["reason"] + (
            " Paste an NVIDIA API key above to use the hosted model, or run "
            f"`ollama serve` and pull {OLLAMA_MODEL} for a local one. The graph "
            "explorer below does not need either."
        ),
        "needs_input": True,
        "rag": state,
        "id": "n/a",
    }


def _where_to_look(question: str) -> list[str]:
    """Pointers for a GAP: where the missing fact would live if it were filed.

    These describe the *shape* of SEC filings, not any one company, so they hold
    across the corpus. They exist so a GAP names the document a reader should
    open next, rather than being a bare refusal they have to interpret.
    """
    q = (question or "").lower()
    out: list[str] = []
    if "product" in q and any(
        w in q for w in ("region", "geograph", "country", "europe", "china", "japan")
    ):
        out.append(
            "The segment note reports products and geographies as two separate "
            "tables; there is no product-by-region cross-tab in the corpus, so the "
            "join this question needs was never filed."
        )
    if any(
        w in q
        for w in ("will ", "expect", "forecast", "guidance", "next year", "fy2027", "outlook")
    ):
        out.append(
            "Forward-looking statements are not filed historical facts. They would "
            "appear in an Item 2.02 results exhibit or an Item 7.01 Reg FD exhibit, "
            "not in the financial statements."
        )
    if any(w in q for w in ("market share", "headcount", "employees", "competitor")):
        out.append(
            "This is not an SEC-filed fact for this corpus. It would come from a "
            "non-filing source (an earnings-call transcript, a press report or an "
            "analyst dataset), which is outside these forms."
        )
    return out


def ask_rag(kg: KnowledgeGraph, question: str) -> dict[str, Any]:
    routing = route_query(question, kg)
    if routing.route == EntityRoute.COLD_START:
        msg = "Entity not indexed. Triggering JIT pipeline..."
        return {
            "status": "cold_start_required",
            "entity": routing.ticker,
            "message": msg,
            "text": msg,
            "question": question,
            "grounded": False,
            "used_tags": [],
            "tag_map": {},
        }
    if routing.route == EntityRoute.AMBIGUOUS:
        msg = "Please specify a company ticker or name (e.g. $AAPL, $MSFT) to answer your question."
        return {
            "status": "ambiguous",
            "message": msg,
            "text": msg,
            "prompt": msg,
            "question": question,
            "grounded": False,
            "used_tags": [],
            "tag_map": {},
        }

    state = get_backends().resolve()
    if state["backend"] == "none":
        return _unavailable_answer(state)
    backend = state["backend"]

    t0 = time.perf_counter()
    context_str, nodes, edges, tag_map, seed_ids = retrieve_financial_context(kg, question, ticker=routing.ticker)

    # Provenance is resolved here, by code, before the model is called: every
    # retrieved node gets a tag and a Source, and the tags that will be legal to
    # cite are fixed at this moment. The model can reference a tag; it can never
    # create one, which is the whole point -- a fabricated figure must not be
    # able to label itself STATED.
    evidence = build_evidence(nodes, kg, tag_map, edges)
    evidence_block = serialise_evidence(evidence)

    log.info("Retrieved %d entities and %d relationships for: %s", len(nodes), len(edges), question)

    # One client for whichever backend resolved; both speak the
    # OpenAI-compatible chat API, so only the URL, the model and the timeout
    # differ. The timeout is explicit because the default (10 minutes for a
    # hosted call, but far less in practice for a stalled local socket) is what
    # turns a slow model into a browser-side "Failed to fetch".
    #
    # Imported here, not at module scope: the graph explorer, the canned
    # reports and the stats panel never construct a client, and a top-level
    # import made the whole SDK -- pydantic, httpx, numpy -- a precondition for
    # serving a page that needs none of it.
    from openai import OpenAI

    api_key, base_url = get_backends().credentials(backend)
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=RAG_TIMEOUT,
        max_retries=0,
    )

    request: dict[str, Any] = {
        "model": state["model"],
        "messages": [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": f"CONTEXT (retrieved knowledge graph):\n{evidence_block}\n\nQUESTION: {question}\n\nAnswer using only the evidence above, citing tags like [E1] that appear in it.",
            },
        ],
        "temperature": 0.2,
        "top_p": 1,
        "max_tokens": 16384,
        "stream": False,
    }
    if backend == "ollama":
        # Ollama-specific knobs ride along in extra_body; the hosted API would
        # reject them as unknown parameters.
        request["extra_body"] = {
            "options": {"num_ctx": RAG_NUM_CTX, "temperature": 0.2},
        }
    elif backend == "nvidia":
        # Nemotron's thinking mode, exactly as the NVIDIA quickstart passes it.
        # The reasoning trace comes back on the message as ``reasoning_content``
        # and is surfaced by the worksheet view alongside the answer.
        request["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": True},
        }

    try:
        completion = client.chat.completions.create(**request)
        msg = completion.choices[0].message
        content = (msg.content or "").strip()
        reasoning = (
            getattr(msg, "reasoning_content", None)
            or getattr(msg, "reasoning", None)
            or ""
        )
        if not content and reasoning:
            content = reasoning
    except Exception as exc:
        log.error("RAG call to %s (%s) failed: %s", state["model"], backend, exc)
        return {
            "question": question,
            "text": _explain_api_error(exc, state),
            "reasoning": "",
            "grounded": False,
            "used_tags": [],
            "tag_map": tag_map,
            "context_entities": len(nodes),
            "context_edges": len(edges),
            "flow": "",
            "graph": None,
            "gap": False,
            "provenance_mix": {},
            "provenance": [],
            "invented_tags": [],
            "ungrounded_figures": [],
            "violations": [],
        }

    # Extract cited tags. The model sometimes writes arrow-style citations
    # ("[E2→E15]") to pair a line item with its value; split those back into
    # plain tags so every cited entity is counted and grounded is true.
    content_cites = re.sub(
        r"(\[E\d+)\s*(?:-{1,2}(?:>|→)|=>|→)\s*(E\d+\])", r"\1] [\2", content
    )
    cited_raw = _CITATION_RE.findall(content_cites)
    used_tags = sorted(set(t for t in cited_raw if t in tag_map), key=lambda x: int(x[1:]))

    # Grade the answer by rule. The model wrote the prose; this decides what it
    # was actually allowed to say, and it never asks the model. If every
    # sentence fails, the corpus does not support the answer, so a GAP is
    # rendered instead of the unsupported prose -- with a pointer to where the
    # fact would live rather than a bare refusal.
    graded = grade_answer(content, evidence, question)
    if content and graded.gap:
        content = render_gap(question, evidence, _where_to_look(question))
        used_tags = []

    # Flow trace
    flow_lines = [
        "FLOW (retrieval path & grounded evidence):",
        f"  Question: {question}",
        f"  ├─ Linked {len(seed_ids)} initial seed(s) from the graph",
    ]
    inv_map = {nid: tag for tag, nid in tag_map.items()}
    for sid in seed_ids[:4]:
        name = next((n["name"] for n in nodes if n["id"] == sid), sid)
        flow_lines.append(f"  │   [{inv_map.get(sid, '?')}] {name}")
    flow_lines.append(f"  ├─ Retrieved {len(nodes)} entities & {len(edges)} relationships")
    flow_lines.append(f"  └─ Generated answer citing: {', '.join(f'[{t}]' for t in used_tags) if used_tags else 'none'}")
    flow = "\n".join(flow_lines)

    # Graph payload for visualization
    # Both merges happen here, at the boundary, so no caller's bookkeeping is
    # load-bearing: one entry per node id, one arrow per (source, target,
    # relation), and a label no other node shares.
    graph_nodes = merge_nodes(nodes)
    graph_edges = merge_edges(edges)
    graph_payload = {
        "seeds": seed_ids,
        "nodes": [
            {
                "id": n["id"],
                "name": n["name"],
                "type": n["type"],
                "description": n.get("description", ""),
            }
            for n in graph_nodes
        ],
        "edges": [
            {
                "source": e["source"],
                "target": e["target"],
                "relation": e["relation"],
                "description": e.get("description", ""),
            }
            for e in graph_edges
        ],
    }

    elapsed = time.perf_counter() - t0
    log.info("RAG QA completed in %.2fs (cited: %s)", elapsed, used_tags)

    return {
        "question": question,
        "text": content,
        "reasoning": reasoning,
        "grounded": bool(used_tags),
        "used_tags": used_tags,
        "tag_map": tag_map,
        "context_entities": len(nodes),
        "context_edges": len(edges),
        "flow": flow,
        "graph": graph_payload,
        "elapsed_sec": round(elapsed, 2),
        "rag_backend": backend,
        "rag_model": state["model"],
        "gap": graded.gap,
        "provenance_mix": graded.mix,
        "provenance": [
            {
                "text": v.text,
                "provenance": v.provenance,
                "cites": v.cites,
                "figures": v.figures,
                "ungrounded": v.ungrounded,
                "reason": v.reason,
            }
            for v in graded.verdicts
        ],
        "invented_tags": graded.invented_tags,
        "ungrounded_figures": graded.ungrounded_figures,
        "violations": graded.violations(),
    }


# ── Frontend HTML ─────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>graphrag viewer</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --panel-2: #1e222b;
    --line: #2a2f3a;
    --text: #e6e8ee;
    --muted: #9aa3b2;
    --accent: #6ea8fe;
    --accent-2: #7ee0b8;
    --warn: #ffb454;
    --bad: #ff7b72;
    --radius: 8px;
  }
  * { box-sizing: border-box; margin:0; padding:0; }
  html, body { height: 100%; margin: 0; overflow:hidden; }
  body {
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: 16px;
    padding: 10px 16px; background: var(--panel);
    border-bottom: 1px solid var(--line); flex: 0 0 auto;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  header .stats { color: var(--muted); font-size: 12px; }
  header .spacer { flex: 1; }
  header select {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 4px 8px; font: inherit; outline: none;
  }

  main { flex: 1 1 auto; display: flex; min-height: 0; overflow: hidden; }

  /* ---- left: entity browser ---- */
  aside {
    width: 290px; flex: 0 0 290px; background: var(--panel);
    border-right: 1px solid var(--line);
    display: flex; flex-direction: column; min-height: 0;
  }
  aside .pane { padding: 10px 12px; border-bottom: 1px solid var(--line); }
  label.field { display: block; font-size: 11px; text-transform: uppercase;
    letter-spacing: .6px; color: var(--muted); margin-bottom: 6px; }
  input[type=search], input[type=text], textarea, select {
    width: 100%; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px;
    font: inherit; outline: none;
  }
  input:focus, textarea:focus, select:focus { border-color: var(--accent); }
  .row { display: flex; gap: 8px; align-items: center; }
  .row > * { min-width: 0; }
  button {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 7px 12px; font: inherit; cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: #fff; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.primary { background: #24405f; border-color: #35597f; }

  #entityList { flex: 1 1 auto; overflow-y: auto; padding: 6px; }
  .entity {
    padding: 7px 9px; border-radius: 6px; cursor: pointer;
    display: flex; align-items: baseline; gap: 8px;
  }
  .entity:hover { background: var(--panel-2); }
  .entity.sel { background: #24405f; }
  .entity .nm { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .entity .ty { font-size: 10px; color: var(--muted); text-transform: uppercase;
    letter-spacing: .4px; margin-left: auto; white-space: nowrap; }

  /* ---- centre: graph ---- */
  #graphWrap { flex: 1 1 auto; position: relative; min-width: 0; overflow:hidden; }
  svg { width: 100%; height: 100%; display: block; cursor: grab; }
  svg.dragging { cursor: grabbing; }
  .toolbar {
    position: absolute; top: 10px; left: 10px; display: flex; gap: 6px;
    background: rgba(23,26,33,.92); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 6px; z-index: 5;
  }
  .toolbar button { padding: 5px 9px; font-size: 12px; }
  .hint {
    position: absolute; bottom: 10px; left: 10px; color: var(--muted);
    font-size: 11px; background: rgba(23,26,33,.9); padding: 5px 8px;
    border-radius: 6px; border: 1px solid var(--line); z-index: 5;
  }
  #legend {
    position: absolute; top: 10px; right: 10px; z-index: 5; max-width: 260px;
    display: flex; flex-wrap: wrap; gap: 5px; justify-content: flex-end;
  }
  #legend .chip { background: rgba(23,26,33,.92); cursor: pointer; }
  #tip {
    position: absolute; pointer-events: none; z-index: 10; max-width: 320px;
    background: #0b0d11; border: 1px solid var(--line); border-radius: 6px;
    padding: 7px 9px; font-size: 12px; display: none;
    box-shadow: 0 6px 20px rgba(0,0,0,.5);
  }
  #tip .t { font-weight: 600; margin-bottom: 3px; }
  #tip .ty { color: var(--accent-2); font-size: 10px; text-transform: uppercase;
    letter-spacing: .4px; }
  #tip .d { color: var(--muted); margin-top: 4px; }
  #tip .r { color: var(--warn); margin-top: 4px; }

  /* ---- right: query + answer ---- */
  section.qa {
    width: 440px; flex: 0 0 440px; background: var(--panel);
    border-left: 1px solid var(--line); display: flex; flex-direction: column;
    min-height: 0; overflow:hidden;
  }
  section.qa .pane { padding: 12px; border-bottom: 1px solid var(--line); }
  #askBox { min-height: 70px; resize: vertical; font-size: 13px; line-height:1.45; }
  #answerWrap { flex: 1 1 auto; overflow-y: auto; padding: 14px; }
  .meta { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .chip {
    font-size: 11px; padding: 3px 8px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--line); color: var(--muted);
  }
  .chip.ok { color: var(--accent-2); border-color: #2c5a48; }
  .chip.bad { color: var(--bad); border-color: #6b2f2c; }
  .chip.cite { cursor: pointer; color: var(--accent); border-color: #33507a; }
  .chip.cite:hover { background: #24405f; }
  #answer { white-space: pre-wrap; line-height: 1.6; font-size: 13px; color: #f1f5f9; }
  .reasoning-box {
    margin-top: 12px; padding: 10px 12px; border-radius: 6px; font-size: 12px;
    background: #17212e; border: 1px solid #233854; color: #93c5fd;
    white-space: pre-wrap; max-height: 200px; overflow-y: auto;
  }
  .reasoning-box summary { cursor: pointer; font-weight: 600; color: #6ea8fe; margin-bottom: 6px; }
  .note {
    margin-top: 12px; padding: 8px 10px; border-radius: 6px; font-size: 11.5px;
    background: #2a2317; border: 1px solid #4a3d20; color: var(--warn);
    white-space: pre-wrap; font-family: monospace;
  }
  .examples { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .examples button { font-size: 11px; padding: 4px 8px; border-radius: 999px; text-align: left; }
  .spinner { color: var(--accent); font-size: 13px; display:flex; align-items:center; gap:8px; }
  .empty { color: var(--muted); font-size: 13px; }

  /* ---- reports overlay ---- */
  #reportsOverlay {
    display:none; position:fixed; inset:0; background:rgba(0,0,0,.72);
    z-index:100; align-items:center; justify-content:center;
  }
  #reportsOverlay.open { display:flex; }
  #reportsPanel {
    background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
    width:min(96vw,1100px); max-height:88vh; display:flex; flex-direction:column;
    overflow:hidden;
  }
  #reportsHeader {
    display:flex; align-items:center; gap:12px; padding:12px 16px;
    border-bottom:1px solid var(--line); flex:0 0 auto;
  }
  #reportsHeader h2 { font-size:14px; margin:0; flex:1; }
  #reportsList {
    display:flex; gap:8px; flex-wrap:wrap; padding:12px 16px;
    border-bottom:1px solid var(--line); flex:0 0 auto;
  }
  #reportsList button { font-size:12px; }
  #reportsList button.active { background:#24405f; border-color:#35597f; color:#fff; }
  #reportsBody { flex:1 1 auto; overflow:auto; padding:14px 16px; }
  #reportTitle { font-size:13px; font-weight:600; margin-bottom:4px; }
  #reportDesc  { font-size:12px; color:var(--muted); margin-bottom:10px; }
  #reportTable { width:100%; border-collapse:collapse; font-size:12px; }
  #reportTable th {
    background:var(--panel-2); color:var(--muted); text-align:left;
    padding:5px 8px; border-bottom:1px solid var(--line);
    position:sticky; top:0; z-index:2;
  }
  #reportTable td { padding:5px 8px; border-bottom:1px solid #1e2330; }
  #reportTable tr:hover td { background:var(--panel-2); }
  #reportCount { font-size:11px; color:var(--muted); margin-top:8px; }
  .link { stroke: #39404f; stroke-width: 1.4px; }
  .link.cited { stroke: var(--accent); stroke-width: 2.5px; }
  .link.dim { stroke: #23272f; }
  .lbl { font-size: 10px; fill: #7d879a; pointer-events: none;
    paint-order: stroke; stroke: #0f1115; stroke-width: 3px; stroke-linejoin: round; }
  .node circle { stroke: #0f1115; stroke-width: 2px; cursor: pointer; }
  .node.seed circle { stroke: var(--accent); stroke-width: 3.5px; }
  .node.cited circle { stroke: var(--accent-2); stroke-width: 4px; filter: drop-shadow(0 0 6px rgba(126,224,184,.6)); }
  .node.dim { opacity: .22; }
  .node text { font-size: 11px; fill: var(--text); pointer-events: none;
    paint-order: stroke; stroke: #0f1115; stroke-width: 3.5px; stroke-linejoin: round; }

  /* ── layout: three resizable columns, each one its own scroll context ── */
  html, body { height: 100%; }
  body { height: 100dvh; }
  main {
    flex: 1 1 auto; min-height: 0; overflow: hidden;
    display: grid;
    grid-template-columns: var(--w-left, 300px) 5px minmax(0, 1fr) 5px var(--w-right, 460px);
  }
  .splitter { cursor: col-resize; background: transparent; }
  .splitter:hover, .splitter.dragging { background: var(--accent); }
  /* width comes from the grid columns, not the pane itself */
  aside, section.qa { width: auto; min-height: 0; min-width: 0; }
  # every scroll container gets an explicit zero minimum, otherwise a flex
  # item refuses to shrink below its content and the scrollbar never appears,
  # which is what made the answer pane look frozen
  #entityList, #answerWrap, #reportsBody, .reasoning-box { min-height: 0; overscroll-behavior: contain; }

  /* Scrollbars you can actually see. The default dark-theme scrollbar was
     invisible against --panel, which is why panes looked frozen. */
  * { scrollbar-width: thin; scrollbar-color: #3d4657 #12151b; }
  ::-webkit-scrollbar { width: 12px; height: 12px; }
  ::-webkit-scrollbar-track { background: #12151b; }
  ::-webkit-scrollbar-thumb { background: #3d4657; border-radius: 8px; border: 3px solid #12151b; }
  ::-webkit-scrollbar-thumb:hover { background: #56637a; }

  /* ── shared bits ── */
  .grow { flex: 1 1 auto; }
  .row { display: flex; gap: 8px; align-items: center; }
  .row > * { min-width: 0; }
  button.icon { padding: 5px 9px; font-size: 12px; line-height: 1.2; }
  button.ghost { background: transparent; }
  button.on { background: #24405f; border-color: #35597f; color: #fff; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .field.inline { display: inline; margin: 0; }
  .count { color: var(--accent); font-weight: 600; letter-spacing: 0; text-transform: none; }
  select.compact, input.compact { padding: 5px 7px; }

  /* ── left column ── */
  .side-head { flex: 0 0 auto; }
  .side-foot { flex: 0 0 auto; border-bottom: none; border-top: 1px solid var(--line); }
  #entityList { padding: 6px; }
  #entityList .entity { cursor: pointer; }
  #entityList .entity .nm { flex: 1 1 auto; }
  #entityList .entity.kb { box-shadow: inset 0 0 0 1px var(--accent); }

  /* ── graph ── */
  #graphWrap { min-width: 0; min-height: 0; }
  .toolbar { flex-wrap: wrap; max-width: calc(100% - 20px); }
  .hint { max-width: 60%; line-height: 1.35; }
  #legend { max-height: 40%; overflow-y: auto; }
  #legend.hidden, .toolbar.hidden, .hint.hidden { display: none; }

  /* ── right column ── */
  .qa-head { flex: 0 0 auto; }
  #askBox { min-height: 62px; max-height: 220px; resize: vertical; font-size: 13px; line-height: 1.45; }
  .examples { max-height: 92px; overflow-y: auto; padding-right: 4px; margin-top: 8px; }
  .examples button { font-size: 11px; padding: 4px 8px; border-radius: 999px; text-align: left; }
  #keyPanel { margin-top: 10px; padding: 10px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel-2); }
  #keyPanel[hidden] { display: none; }
  #keyPanel .note { margin: 0; }
  #keyInput { flex: 1; min-width: 0; font-family: var(--mono); font-size: 12px;
    padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line);
    background: var(--bg); color: var(--fg); }
  #keyPrivacy { font-size: 10px; line-height: 1.3; }
  .tabs { display: flex; align-items: center; gap: 4px; padding: 6px 10px;
    border-bottom: 1px solid var(--line); background: var(--panel); flex: 0 0 auto; }
  .tab { background: transparent; border: 1px solid transparent; font-size: 12px; padding: 4px 9px; }
  .tab.active { background: var(--panel-2); border-color: var(--line); color: #fff; }
  #answerWrap { flex: 1 1 auto; padding: 14px; }
  #answerWrap p { margin: 0 0 9px; }
  #answerWrap ul, #answerWrap ol { margin: 0 0 10px 20px; }
  #answerWrap li { margin-bottom: 4px; }
  #answer h4 { font-size: 12px; text-transform: uppercase; letter-spacing: .5px;
    color: var(--muted); margin: 14px 0 6px; }
  #answer b { color: #fff; }
  #answer code { background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 4px; padding: 0 4px; font-size: 12px; }
  .inline-cite { background: #1d3557; color: var(--accent); border: 1px solid #33507a;
    border-radius: 4px; font-size: 11px; padding: 0 4px; margin: 0 1px; cursor: pointer; }
  .inline-cite:hover { background: #24405f; color: #fff; }
  .srcs { display: flex; flex-direction: column; gap: 6px; }
  .src { display: flex; align-items: baseline; gap: 8px; padding: 7px 9px;
    background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px;
    cursor: pointer; }
  .src:hover { border-color: var(--accent); }
  .src .tag { font-size: 11px; color: var(--accent); min-width: 34px; }
  .src .nm { flex: 1 1 auto; font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .src .ty { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }
  .src .val { font-size: 12px; color: var(--accent-2); font-variant-numeric: tabular-nums; }
  #waitTimer { font-variant-numeric: tabular-nums; }

  /* ── reports overlay ── */
  #reportsOverlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.72); z-index: 100; }
  #reportsOverlay.open { display: flex; align-items: center; justify-content: center; }
  #reportsPanel {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    width: min(96vw, 1180px); height: min(90vh, 900px);
    display: grid; grid-template-rows: auto auto minmax(0, 1fr);
  }
  #reportsList { max-height: 22vh; overflow-y: auto; }
  #reportsBody { min-height: 0; }
  #reportsHeader input { width: 200px; }

  /* ── toasts ── */
  #toasts { position: fixed; right: 14px; bottom: 14px; z-index: 200;
    display: flex; flex-direction: column; gap: 8px; max-width: 380px; }
  .toast { background: var(--panel-2); border: 1px solid var(--line); border-left: 3px solid var(--accent);
    border-radius: 6px; padding: 9px 12px; font-size: 12.5px; box-shadow: 0 8px 24px rgba(0,0,0,.55); }
  .toast.bad { border-left-color: var(--bad); }
  .toast.ok { border-left-color: var(--accent-2); }

  /* ── narrow windows: stack the columns and let the page scroll ── */
  @media (max-width: 1000px) {
    main { display: flex; flex-direction: column; overflow-y: auto; padding-bottom: 10px; }
    .splitter { display: none; }
    #graphWrap { flex: 0 0 auto; height: 52vh; min-height: 300px; border-bottom: 1px solid var(--line); }
    aside, section.qa { width: auto; flex: 0 0 auto; border: none;
      border-top: 1px solid var(--line); }
    aside { max-height: 46vh; }
    section.qa { max-height: 90vh; }
    #entityList { max-height: 26vh; }
    header { flex-wrap: wrap; row-gap: 6px; }
  }
</style>
</head>
<body>
<header>
  <h1>graphrag viewer</h1>
  <div class="stats" id="stats">loading…</div>
  <span class="chip" id="modelChip" title="Answering model and backend">model</span>
  <div class="spacer"></div>
  <label class="field inline stats" for="hops">hops</label>
  <select id="hops" class="compact" style="width:auto">
    <option value="1">1</option>
    <option value="2" selected>2</option>
  </select>
  <button id="legendBtn" class="icon">legend</button>
  <button id="reportsBtn" class="icon">reports</button>
  <button id="fit" class="icon">fit</button>
</header>

<main>
  <aside>
    <div class="pane side-head">
      <label class="field" for="entitySearch">Entities <span class="count" id="entityCount"></span></label>
      <input type="search" id="entitySearch" placeholder="filter by name or type…" autocomplete="off">
      <div class="row" style="margin-top:6px">
        <select id="typeFilter" class="compact"></select>
        <button id="clearSearch" class="icon ghost" title="Clear filter">✕</button>
      </div>
    </div>
    <div id="entityList" tabindex="0" aria-label="Entity list"></div>
    <div class="pane side-foot">
      <div class="row">
        <button id="showAll" class="primary" style="flex:1">show whole graph</button>
        <button id="clearSeed" class="icon">clear</button>
      </div>
      <div class="row" style="margin-top:6px">
        <label class="field inline stats" for="graphLimit" style="font-size:11px">nodes</label>
        <select id="graphLimit" class="compact">
          <option value="60">60</option>
          <option value="150" selected>150</option>
          <option value="300">300</option>
          <option value="500">500</option>
        </select>
        <div class="grow"></div>
        <span class="chip" id="graphCount">0 nodes</span>
      </div>
    </div>
  </aside>

  <div class="splitter" id="splitL" role="separator" aria-orientation="vertical" title="Drag to resize"></div>

  <div id="graphWrap">
    <svg id="svg"><g id="viewport"></g></svg>
    <div class="toolbar" id="toolbar">
      <button id="relayout" class="icon">re-layout</button>
      <button id="zoomIn" class="icon" title="Zoom in">+</button>
      <button id="zoomOut" class="icon" title="Zoom out">−</button>
      <button id="fitGraph" class="icon">fit</button>
      <button id="labelsBtn" class="icon on" title="Toggle node labels">labels</button>
    </div>
    <div id="legend"></div>
    <div class="hint" id="graphHint">drag node to move · click to focus · scroll to zoom · drag background to pan</div>
    <div id="tip"></div>
  </div>

  <div class="splitter" id="splitR" role="separator" aria-orientation="vertical" title="Drag to resize"></div>

  <section class="qa">
    <div class="pane qa-head">
      <label class="field" for="askBox">Ask the graph <span class="count" id="modelName">…</span></label>
      <textarea id="askBox" placeholder="e.g. What was Apple's total net sales in 2025 and how much came from the Americas segment?"></textarea>
      <div class="row" style="margin-top:8px">
        <button id="askBtn" class="primary" style="flex:1">ask</button>
        <button id="examplesBtn" class="icon" title="Show or hide sample questions">examples</button>
        <span class="chip" title="Keyboard shortcut">ctrl+enter</span>
      </div>
      <div class="examples" id="examples"></div>
      <div id="keyPanel" hidden>
        <div class="note" id="keyNote"></div>
        <div class="row" style="margin-top:8px">
          <input id="keyInput" type="password" autocomplete="off" spellcheck="false"
                 placeholder="nvapi-…" aria-label="NVIDIA API key" style="flex:1">
          <button id="keySave" class="primary">use key</button>
        </div>
        <div class="row" style="margin-top:6px">
          <button id="keyForget" class="icon ghost" hidden>forget key</button>
          <button id="useLocal" class="icon ghost" hidden>use local model</button>
          <span class="count" id="keyPrivacy"></span>
        </div>
      </div>
    </div>
    <div class="tabs">
      <button class="tab active" data-tab="answer">Answer</button>
      <button class="tab" data-tab="sources">Sources</button>
      <button class="tab" data-tab="trace">Trace</button>
      <div class="grow"></div>
      <span class="chip" id="waitTimer" hidden></span>
      <button id="copyAnswer" class="icon ghost" title="Copy answer text">copy</button>
    </div>
    <div id="answerWrap">
      <div id="tabAnswer">
        <div class="empty">Answers are generated from the retrieved subgraph and cite entities as
          <code>[E1]</code>. Click a citation to highlight it in the graph.</div>
      </div>
      <div id="tabSources" hidden><div class="empty">No answer yet — the cited entities show up here.</div></div>
      <div id="tabTrace" hidden><div class="empty">The retrieval trace shows up here after a question.</div></div>
    </div>
  </section>
</main>

<!-- Reports Overlay -->
<div id="reportsOverlay">
  <div id="reportsPanel">
    <div id="reportsHeader">
      <h2>Canned Reports — graph query results</h2>
      <span class="chip" style="font-size:11px">Zero-LLM · Pure Cypher</span>
      <input id="reportFilter" class="compact" placeholder="filter rows…" autocomplete="off">
      <button id="csvBtn" class="icon" title="Download the current result as CSV">csv</button>
      <button id="closeReports" class="icon">✕ close</button>
    </div>
    <div id="reportsList"></div>
    <div id="reportsBody">
      <div class="empty">Select a report above to run it against the graph.</div>
    </div>
  </div>
</div>
<div id="toasts" aria-live="polite"></div>

<script src="/vendor/d3.v7.min.js"></script>
<script>
"use strict";

const S = {
  nodes: [], links: [],
  byId: new Map(),
  seeds: new Set(), cited: new Set(),
  selected: null,
  all: [],
  sim: null,
  view: { x: 0, y: 0, k: 1 },
  busy: false,
  ragModel: "",
  ragBackend: "",
  showLabels: true,
  lastAnswer: "",
  kbIndex: -1,
  timer: null,
};

const $ = (id) => document.getElementById(id);
const svg = $("svg"), viewport = $("viewport"), tip = $("tip");

function typeColor(type) {
  const t = (type || "unspecified").toLowerCase();
  const known = {
    company: "#7ee0b8", filing: "#6ea8fe", financialmetric: "#ffb454",
    segment: "#d2a8ff", disclosureevent: "#f78fb3", documentchunk: "#8fd3f4",
    executive: "#ffd479", supplier: "#a0e8a0",
    section: "#f2a7c9", riskfactor: "#e86a6a", causalrelation: "#b9a7f2",
    productfamily: "#ffd479", geographicmarket: "#9ad1d1",
    competitor: "#f2a7a7", customer: "#a8d9a0", regulatorybody: "#c9b3e8",
    macrovariable: "#f0c27a", standardizedconcept: "#7ed6a8",
    rawfact: "#e8b3d4", footnote: "#cfe0a8", fiscalperiod: "#96c8e8",
  };
  if (known[t]) return known[t];
  let h = 0;
  for (const ch of t) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360} 55% 62%)`;
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    // A rejected fetch is the browser refusing to tell us anything: it means
    // the request never completed, so there is no status, no body, and no
    // usable error message. Surfacing e.message verbatim just prints
    // "Failed to fetch", which is true and useless -- it does not say whether
    // the server is gone or the server failed, and those need different fixes.
    // One cheap probe on a cheap endpoint tells them apart.
    throw new Error(await describeTransportFailure(path));
  }
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

async function describeTransportFailure(path) {
  const where = location.origin;
  let alive = false;
  try {
    const ctl = new AbortController();
    setTimeout(() => ctl.abort(), 3000);
    const probe = await fetch("/api/stats", { signal: ctl.signal, cache: "no-store" });
    alive = probe.ok;
  } catch (_) { alive = false; }
  if (!alive) {
    return (
      `could not reach the server at ${where} -- ${path} never got a response. ` +
      `It is not running, or it stopped partway through. If you started it in ` +
      `a terminal, check that terminal for a traceback and restart it with ` +
      `python -m sandbox_engine.query_ui.`
    );
  }
  return (
    `${where} is running, but it dropped the connection during ${path} ` +
    `without sending a response, which means the request hit an error inside ` +
    `the server. The details were printed to the terminal running it.`
  );
}

class Sim {
  constructor(nodes, links, width, height) {
    this.nodes = nodes; this.links = links;
    this.w = width; this.h = height;
    // d3-force drives the layout. The wrapper keeps the same contract the rest
    // of this file relies on: a mutable ``alpha`` and a ``tick()`` that answers
    // "is there more to do", so the rAF loop below is unchanged.
    this.force = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id)
        .distance(110).strength(0.10))
      .force("charge", d3.forceManyBody().strength((d) => -120 - 5 * (d.r || 9)))
      .force("x", d3.forceX(width / 2).strength(0.04))
      .force("y", d3.forceY(height / 2).strength(0.04))
      .force("center", d3.forceCenter(width / 2, height / 2))
      // Keep labels readable: nodes never overlap, something the old hand-rolled
      // sum-of-pairs repulsion could not guarantee.
      .force("collide", d3.forceCollide((d) => (d.r || 9) + 7).iterations(2))
      .stop();
  }
  get alpha() { return this.force.alpha(); }
  set alpha(v) { this.force.alpha(v).stop(); }
  tick() {
    if (this.force.alpha() < this.force.alphaMin()) return false;
    this.force.tick();
    return this.force.alpha() > this.force.alphaMin();
  }
  resize(w, h) {
    this.w = w; this.h = h;
    this.force
      .force("x", d3.forceX(w / 2).strength(0.04))
      .force("y", d3.forceY(h / 2).strength(0.04))
      .force("center", d3.forceCenter(w / 2, h / 2));
  }
}

let raf = null;
function startSim(reheat) {
  if (raf) cancelAnimationFrame(raf);
  if (reheat && S.sim) S.sim.alpha = 1;
  const loop = () => {
    const more = S.sim ? S.sim.tick() : false;
    draw();
    raf = more ? requestAnimationFrame(loop) : null;
  };
  raf = requestAnimationFrame(loop);
}

// ── d3 idiom from the getting-started guide: bind selections once, then
// re-run data joins on each tick instead of clearing and rebuilding the DOM.
const linkSel = d3.select(viewport).append("g");
const labelSel = d3.select(viewport).append("g");
const nodeSel = d3.select(viewport).append("g");

function applyView() {
  viewport.setAttribute("transform",
    `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
}

let nodeDrag = false;
let suppressClick = false;
let dragStart = null, dragTravel = 0;

const zoom = d3.zoom()
  .scaleExtent([0.1, 3])
  .filter((event) => !nodeDrag && !event.button)
  .on("start", () => svg.classList.add("dragging"))
  .on("zoom", (event) => {
    S.view = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
    applyView();
    draw();
  })
  .on("end", () => svg.classList.remove("dragging"));
d3.select(svg).call(zoom);

function setView(x, y, k) {
  S.view = { x, y, k };
  d3.select(svg).call(zoom.transform, d3.zoomIdentity.translate(x, y).scale(k));
}

const dragBehavior = d3.drag()
  .on("start", (event, d) => {
    nodeDrag = true;
    suppressClick = false;
    dragStart = { x: event.x, y: event.y };
    dragTravel = 0;
    d.fx = d.x; d.fy = d.y;
    if (!event.active && S.sim) S.sim.alpha = Math.max(S.sim.alpha, 0.7);
    svg.classList.add("dragging");
    hideTip();
  })
  .on("drag", (event, d) => {
    // d3.pointer reports graph-space coordinates here (nodes sit under the
    // zoomed viewport), so pin the node straight onto the force's seat.
    d.fx = event.x; d.fy = event.y;
    dragTravel = Math.max(dragTravel,
      Math.hypot(event.x - dragStart.x, event.y - dragStart.y));
    if (S.sim) S.sim.alpha = Math.max(S.sim.alpha, 0.35);
    startSim(false);
  })
  .on("end", (event, d) => {
    d.fx = null; d.fy = null;
    nodeDrag = false;
    svg.classList.remove("dragging");
    suppressClick = dragTravel > 4;
    if (suppressClick) setTimeout(() => { suppressClick = false; }, 0);
  });

function draw() {
  const hasFocus = S.seeds.size > 0 || S.cited.size > 0;

  linkSel.selectAll("line")
    .data(S.links, (l) => `${l.source.id}|${l.target.id}|${encodeURIComponent(l.relation)}`)
    .join("line")
    .attr("class", (l) => {
      const isCited = S.cited.has(l.source.id) && S.cited.has(l.target.id);
      const inSeed = S.seeds.has(l.source.id) || S.seeds.has(l.target.id);
      return isCited ? "link cited" : (hasFocus && !inSeed ? "link dim" : "link");
    })
    .attr("x1", (l) => l.source.x).attr("y1", (l) => l.source.y)
    .attr("x2", (l) => l.target.x).attr("y2", (l) => l.target.y)
    .each(function (l) {
      if (!this.firstElementChild) {
        d3.select(this).append("title")
          .text(`${l.source.name} —${l.relation.replace(/_/g, " ")}→ ${l.target.name}` +
            (l.description ? `\n${l.description}` : ""));
      }
    });

  labelSel.selectAll("text.lbl")
    .data(S.links.filter((l) => l.source && l.target &&
      (S.view.k > 0.62 || (S.cited.has(l.source.id) && S.cited.has(l.target.id)))),
      (l) => `${l.source.id}|${l.target.id}|${encodeURIComponent(l.relation)}`)
    .join("text")
    .attr("class", "lbl")
    .attr("text-anchor", "middle")
    .attr("x", (l) => (l.source.x + l.target.x) / 2)
    .attr("y", (l) => (l.source.y + l.target.y) / 2 - 4)
    .text((l) => l.relation.replace(/_/g, " "));

  const nodeGroups = nodeSel.selectAll("g.node")
    .data(S.nodes, (d) => d.id)
    .join(
      (enter) => {
        const g = enter.append("g").attr("class", "node");
        g.append("circle");
        g.append("title");
        g.append("text").attr("y", (d) => (d.r || 9) + 12).attr("text-anchor", "middle");
        g.call(dragBehavior);
        return g;
      },
      (update) => update,
      (exit) => exit.remove())
    .attr("class", (d) => {
      const isCited = S.cited.has(d.id), isSeed = S.seeds.has(d.id);
      return "node" + (isSeed ? " seed" : "") + (isCited ? " cited" : "") +
             (hasFocus && !isSeed && !isCited ? " dim" : "");
    })
    .attr("transform", (d) => `translate(${d.x},${d.y})`);

  nodeGroups.select("circle")
    .attr("r", (d) => d.r || 9)
    .attr("fill", (d) => typeColor(d.type));
  nodeGroups.select("text")
    .text((d) => d.name.length > 26 ? d.name.slice(0, 25) + "…" : d.name)
    .style("display", (d) =>
      S.showLabels && (S.view.k > 0.45 || S.seeds.has(d.id) || S.cited.has(d.id))
        ? null : "none");
  nodeGroups.select("title")
    .text((d) => `${d.name} (${d.type}${d.description ? " — " + d.description : ""})`);
}

function fit() {
  const r = svg.getBoundingClientRect();
  if (!S.nodes.length) {
    setView(r.width / 2, r.height / 2, 1);
    draw();
    return;
  }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const d of S.nodes) {
    minX = Math.min(minX, d.x - 30); maxX = Math.max(maxX, d.x + 30);
    minY = Math.min(minY, d.y - 30); maxY = Math.max(maxY, d.y + 30);
  }
  const k = Math.min(2, Math.max(0.15,
    Math.min(r.width / (maxX - minX), r.height / (maxY - minY)) * 0.9));
  setView(
    r.width / 2 - ((minX + maxX) / 2) * k,
    r.height / 2 - ((minY + maxY) / 2) * k,
    k);
  draw();
}

function zoomBy(factor) {
  const r = svg.getBoundingClientRect();
  const cx = r.width / 2, cy = r.height / 2;
  const k = Math.max(0.1, Math.min(3, S.view.k * factor));
  setView(cx - (cx - S.view.x) * (k / S.view.k), cy - (cy - S.view.y) * (k / S.view.k), k);
  draw();
}

function loadGraph(payload, opts) {
  opts = opts || {};
  const prev = S.byId;
  S.nodes = (payload.nodes || []).map((n) => {
    const old = prev.get(n.id);
    return { ...n, x: old ? old.x : 0, y: old ? old.y : 0,
             vx: 0, vy: 0, r: 7 + Math.min(7, (n.name || "").length / 20) };
  });
  S.byId = new Map(S.nodes.map((n) => [n.id, n]));
  S.links = (payload.edges || [])
    .filter((e) => S.byId.has(e.source) && S.byId.has(e.target))
    .map((e) => ({ ...e, source: S.byId.get(e.source), target: S.byId.get(e.target) }));
  S.seeds = new Set((payload.seeds || []).filter((id) => S.byId.has(id)));
  S.cited = new Set(opts.cited || []);
  S.sim = new Sim(S.nodes, S.links, svg.clientWidth || 900, svg.clientHeight || 600);
  S.sim.alpha = opts.fresh ? 1 : 0.6;
  $("graphCount").textContent = `${S.nodes.length} nodes · ${S.links.length} links`;
  startSim(true);
  if (opts.fresh) setTimeout(fit, 700);
}

function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const wrap = $("graphWrap").getBoundingClientRect();
  let x = evt.clientX - wrap.left + 14, y = evt.clientY - wrap.top + 14;
  if (x + tip.offsetWidth > wrap.width) x = evt.clientX - wrap.left - tip.offsetWidth - 14;
  if (y + tip.offsetHeight > wrap.height) y = wrap.height - tip.offsetHeight - 8;
  tip.style.left = Math.max(4, x) + "px";
  tip.style.top = Math.max(4, y) + "px";
}
const hideTip = () => { tip.style.display = "none"; };

function nodeAt(evt) {
  const [px, py] = d3.pointer(evt, nodeSel.node());
  let best = null, bd = Infinity;
  for (const n of S.nodes) {
    const d = Math.hypot(n.x - px, n.y - py);
    if (d < (n.r || 9) + 4 && d < bd) { bd = d; best = n; }
  }
  return best;
}

svg.addEventListener("pointermove", (e) => {
  const best = nodeAt(e);
  if (best) {
    const rel = S.links.filter((l) => l.source === best || l.target === best)
      .slice(0, 6)
      .map((l) => {
        const other = l.source === best ? l.target : l.source;
        return `<div class="r">—${esc(l.relation)}→ ${esc(other.name)}</div>`;
      }).join("");
    showTip(e,
      `<div class="t">${esc(best.name)}</div>` +
      `<div class="ty">${esc(best.type)}</div>` +
      (best.description ? `<div class="d">${esc(best.description)}</div>` : "") + rel);
  } else hideTip();
});

svg.addEventListener("pointerleave", hideTip);

svg.addEventListener("click", (e) => {
  if (suppressClick) return;
  const best = nodeAt(e);
  if (best) focusEntity(best.id);
});

const BACKEND_LABEL = { nvidia: "NVIDIA NIM", ollama: "local Ollama" };

// The panel is the only place a key can be typed, and it appears only when
// there is a decision to make: nothing to phrase answers with, or a key that
// was refused. A reader with a working key never sees it.
function applyRagState(s) {
  S.ragModel = s.rag_model || "";
  S.ragBackend = s.rag_backend || "";
  const where = BACKEND_LABEL[S.ragBackend];
  const chip = where ? `${S.ragModel} · ${where}` : "no model available";
  $("modelChip").textContent = chip;
  $("modelChip").title = s.rag_reason || "";
  $("modelChip").className = "chip" + (where ? "" : " bad");
  $("modelName").textContent = where ? `(${S.ragModel}, ${where})` : "(disabled)";

  const blocked = s.rag_backend === "none" || s.rag_stored_key_rejected;
  $("askBtn").disabled = blocked;
  $("askBox").placeholder = blocked
    ? "Answering is off until a model is available — see the note below."
    : "e.g. What was Apple's total net sales in 2025 and how much came from the Americas segment?";
  $("keyPanel").hidden = !blocked;
  if (!blocked) return;

  const bits = [s.rag_reason];
  if (s.rag_stored_key_rejected) {
    bits.push("Paste a different key below, or start the local model.");
  } else {
    bits.push(`Get a key at ${"https://build.nvidia.com"} and paste it below.`);
  }
  if (!s.rag_ollama_reachable) {
    bits.push(`Or run \`ollama serve\` then \`ollama pull ${"llama3.2"}\` and press “use local model”.`);
  } else {
    const pulled = (s.rag_ollama_models || []).join(", ");
    bits.push(pulled
      ? `The local server is up with ${pulled}.`
      : "The local server is up but has no models pulled yet.");
  }
  $("keyNote").textContent = bits.join(" ");
  $("useLocal").hidden = !s.rag_ollama_reachable;
  $("keyForget").hidden = s.rag_key_source !== "browser";
  $("keyPrivacy").textContent =
    "Held in memory for this server process only — never written to disk, gone on restart.";
  $("keyInput").value = "";
}

async function postRag(body) {
  try {
    applyRagState(await api("/api/rag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  } catch (e) {
    $("keyNote").textContent = e.message;
  }
}

$("keySave").onclick = () => {
  const key = $("keyInput").value.trim();
  if (!key) { $("keyNote").textContent = "Paste a key first."; return; }
  postRag({ key, backend: "nvidia" });
};

$("keyForget").onclick = () => postRag({ key: "", backend: "auto" });
$("useLocal").onclick = () => postRag({ backend: "ollama" });
$("keyInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("keySave").click(); });

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("stats").textContent = `${s.nodes} entities · ${s.edges} relationships`;
    applyRagState(s);
    if (s.schema) {
      const b = document.createElement("span");
      b.className = "chip";
      b.textContent = s.schema;
      $("stats").after(b);
    }

    const legend = $("legend");
    legend.textContent = "";
    const sel = $("typeFilter");
    sel.textContent = "";
    sel.appendChild(new Option("all types", ""));
    for (const [type, n] of (s.entity_types || [])) {
      const b = document.createElement("button");
      b.className = "chip";
      b.style.borderColor = typeColor(type);
      b.style.color = typeColor(type);
      b.textContent = `${type} ${n}`;
      b.title = `Filter the entity list by ${type}`;
      b.onclick = () => {
        const term = type.toLowerCase();
        const input = $("entitySearch");
        input.value = input.value === term ? "" : term;
        input.dispatchEvent(new Event("input"));
        sel.value = "";
      };
      legend.appendChild(b);
      sel.appendChild(new Option(`${type} (${n})`, type.toLowerCase()));
    }
  } catch (e) { $("stats").textContent = "error: " + e.message; }
}

function activeTerm() {
  return ($("entitySearch").value || $("typeFilter").value || "").toLowerCase();
}

async function loadEntities(q) {
  try {
    const d = await api(`/api/entities?limit=500${q ? "&q=" + encodeURIComponent(q) : ""}`);
    S.all = d.entities || [];
    renderEntities();
  } catch (e) { toast(`entity list failed: ${e.message}`, "bad"); }
}

function renderEntities() {
  const box = $("entityList");
  const term = activeTerm();
  const rows = S.all.filter((e) =>
    !term || (e.name || "").toLowerCase().includes(term) ||
    (e.entity_type || "").toLowerCase().includes(term) ||
    (e.description || "").toLowerCase().includes(term));

  S.kbIndex = -1;
  $("entityCount").textContent = rows.length === S.all.length
    ? `${rows.length}` : `${rows.length}/${S.all.length}`;
  box.textContent = "";
  if (!rows.length) {
    box.innerHTML = '<div class="empty" style="padding:10px">no matching entities</div>';
    return;
  }
  for (const e of rows) {
    const d = document.createElement("div");
    d.className = "entity" + (S.selected === e.id ? " sel" : "");
    d.title = e.description || e.name;
    d.innerHTML = `<span class="nm"></span><span class="ty"></span>`;
    d.querySelector(".nm").textContent = e.name;
    d.querySelector(".ty").textContent = e.entity_type || "";
    d.onclick = () => focusEntity(e.id);
    box.appendChild(d);
  }
}

function focusOn(id) {
  const n = S.byId.get(id);
  if (!n) { toast("that entity is not in the current view", "bad"); return; }
  S.cited = new Set([id]);
  const r = svg.getBoundingClientRect();
  setView(r.width / 2 - n.x * S.view.k, r.height / 2 - n.y * S.view.k, S.view.k);
  draw();
}

async function focusEntity(id) {
  S.selected = id;
  renderEntities();
  const hops = $("hops").value;
  try {
    const g = await api(`/api/graph?seed=${encodeURIComponent(id)}&hops=${hops}`);
    loadGraph(g, { fresh: true });
  } catch (e) {
    toast(`graph query failed: ${e.message}`, "bad");
  }
}

async function showAll() {
  S.selected = null;
  renderEntities();
  try {
    const g = await api("/api/graph?limit=" + ($("graphLimit").value || 300));
    loadGraph(g, { fresh: true });
  } catch (e) {
    toast(`graph query failed: ${e.message}`, "bad");
  }
}

function startTimer() {
  const chip = $("waitTimer");
  const t0 = Date.now();
  chip.hidden = false;
  clearInterval(S.timer);
  S.timer = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    chip.textContent = s > 20
      ? `local model thinking… ${s}s (this can take minutes on CPU)`
      : `retrieving… ${s}s`;
  }, 1000);
}

function stopTimer() {
  clearInterval(S.timer);
  $("waitTimer").hidden = true;
}

async function askQuestion() {
  const q = $("askBox").value.trim();
  if (!q) { toast("type a question first", "bad"); return; }
  if (S.busy) { toast("a question is already running", "bad"); return; }
  S.busy = true;
  $("askBtn").disabled = true;
  $("askBtn").textContent = "asking…";
  showTab("answer");
  $("tabAnswer").innerHTML =
    `<div class="spinner"><span>⚡</span> Retrieving graph context, then ` +
    `generating an answer with ${esc(S.ragModel || "the model")}…</div>`;
  startTimer();
  try {
    const res = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    renderAnswer(res);
    if (res.rag) applyRagState(res.rag);
    if (res.rag_backend) {
      S.ragBackend = res.rag_backend;
      const where = BACKEND_LABEL[res.rag_backend];
      $("modelChip").className = "chip" + (where ? "" : " bad");
    }
    if (res.needs_input) {
      $("keyInput").focus();
      toast("no model is available to phrase the answer", "bad");
    }
    if (res.graph) {
      const citedIds = (res.used_tags || []).map((t) => res.tag_map[t]).filter(Boolean);
      loadGraph(res.graph, { cited: citedIds });
    }
  } catch (e) {
    $("tabAnswer").innerHTML = `<div class="note">Query failed: ${esc(e.message)}</div>`;
    toast(`query failed: ${e.message}`, "bad");
  } finally {
    stopTimer();
    S.busy = false;
    // Not unconditionally false: a server with nothing to phrase answers must
    // not look ready, or the button invites a question that cannot be answered.
    $("askBtn").disabled = S.ragBackend === "none";
    $("askBtn").textContent = "ask";
  }
}

// Minimal markdown for model output: **bold**, `code`, and - / 1. lists.
// Input is escaped first, so nothing here can inject markup.
function mdLite(text) {
  const safe = esc(text || "");
  const inline = (s) => s
    .replace(/\[(E\d+)\]/g, '<button class="inline-cite" data-tag="$1">$1</button>')
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let list = null;
  for (const raw of safe.split(/\r?\n/)) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      const kind = /^\d/.test(bullet[0].trim()) ? "ol" : "ul";
      if (!list || list.kind !== kind) {
        if (list) out.push(`</${list.kind}>`);
        out.push(`<${kind}>`);
        list = { kind };
      }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    if (list) { out.push(`</${list.kind}>`); list = null; }
    if (!line.trim()) { out.push(""); continue; }
    if (/^#{1,6}\s+/.test(line)) {
      out.push(`<h4>${inline(line.replace(/^#{1,6}\s+/, ""))}</h4>`);
      continue;
    }
    out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list.kind}>`);
  return out.join("\n");
}

function renderAnswer(res) {
  S.lastAnswer = res.text || "";
  const tagMap = res.tag_map || {};
  const nodesById = new Map(((res.graph || {}).nodes || []).map((n) => [n.id, n]));

  // ── Answer tab ──
  const answer = $("tabAnswer");
  answer.textContent = "";

  const meta = document.createElement("div");
  meta.className = "meta";
  const addChip = (cls, text, title, onClick) => {
    const c = document.createElement("span");
    c.className = "chip " + cls;
    c.textContent = text;
    if (title) c.title = title;
    if (onClick) c.onclick = onClick;
    meta.appendChild(c);
    return c;
  };
  addChip("ok", "asked: " + (res.question || "").slice(0, 60), "the question that produced this answer");
  if (res.context_entities) {
    addChip("", `${res.context_entities} entities / ${res.context_edges} rels`,
      "size of the retrieved subgraph handed to the model");
  }
  addChip(res.grounded ? "ok" : "bad", res.grounded ? "grounded" : "not grounded",
    "whether the model reported finding its answer in the retrieved subgraph");
  if (res.elapsed_sec) addChip("", `${res.elapsed_sec}s`, "retrieval + generation time");
  answer.appendChild(meta);

  const body = document.createElement("div");
  body.id = "answer";
  body.innerHTML = mdLite(res.text);
  body.addEventListener("click", (e) => {
    const btn = e.target.closest(".inline-cite");
    if (!btn) return;
    const id = tagMap[btn.dataset.tag];
    if (id) focusOn(id);
  });
  answer.appendChild(body);

  if (res.reasoning) {
    const box = document.createElement("details");
    box.className = "reasoning-box";
    box.innerHTML = `<summary>Model reasoning (${esc(S.ragModel || "model")})</summary>` +
      `<div style="margin-top:6px">${esc(res.reasoning)}</div>`;
    answer.appendChild(box);
  }

  // ── Sources tab ──
  const sources = $("tabSources");
  sources.textContent = "";
  const tags = res.used_tags || [];
  if (!tags.length) {
    sources.innerHTML = '<div class="empty">The model cited no entities in this answer.</div>';
  } else {
    const list = document.createElement("div");
    list.className = "srcs";
    for (const t of tags) {
      const id = tagMap[t];
      const n = nodesById.get(id);
      const row = document.createElement("div");
      row.className = "src";
      row.title = n ? n.description || n.name : "";
      row.innerHTML = `<span class="tag"></span><span class="nm"></span>` +
        `<span class="val"></span><span class="ty"></span>`;
      row.querySelector(".tag").textContent = t;
      row.querySelector(".nm").textContent = n ? n.name : "(not in this view)";
      const rel = n && n.description || "";
      row.querySelector(".val").textContent = /value=/.test(rel)
        ? (rel.match(/value=[^ ]+/) || [""])[0].slice(6) : "";
      row.querySelector(".ty").textContent = n ? (n.type || "") : "";
      row.onclick = () => { if (id) focusOn(id); };
      list.appendChild(row);
    }
    sources.appendChild(list);
  }

  // ── Trace tab ──
  const trace = $("tabTrace");
  trace.textContent = "";
  trace.innerHTML = res.flow
    ? `<div class="note" style="white-space:pre-wrap;font-family:inherit">${esc(res.flow)}</div>`
    : '<div class="empty">No retrieval trace for this answer.</div>';
}

function showTab(name) {
  for (const t of document.querySelectorAll(".tab")) {
    t.classList.toggle("active", t.dataset.tab === name);
  }
  for (const id of ["answer", "sources", "trace"]) {
    $("tab" + id[0].toUpperCase() + id.slice(1)).hidden = id !== name;
  }
  $("answerWrap").scrollTop = 0;
}

function toast(msg, kind) {
  const box = $("toasts");
  const t = document.createElement("div");
  t.className = "toast " + (kind || "");
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" ? 8000 : 4000);
}

// ── controls ────────────────────────────────────────────────────────────

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

$("entitySearch").addEventListener("input", (e) => {
  const term = e.target.value;
  if (term === "") { loadEntities(""); return; }
  debounce(loadEntities, 180)(term);
});
$("typeFilter").addEventListener("change", () => {
  $("entitySearch").value = "";
  loadEntities("");
});
$("clearSearch").onclick = () => {
  $("entitySearch").value = "";
  $("typeFilter").value = "";
  loadEntities("");
};

// ↑/↓ walk the list, Enter focuses, "/" jumps to the search box.
$("entityList").addEventListener("keydown", (e) => {
  const rows = [...$("entityList").querySelectorAll(".entity")];
  if (!rows.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    S.kbIndex = Math.max(0, Math.min(rows.length - 1, S.kbIndex + (e.key === "ArrowDown" ? 1 : -1)));
    rows.forEach((r, i) => r.classList.toggle("kb", i === S.kbIndex));
    rows[S.kbIndex].scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && S.kbIndex >= 0) {
    e.preventDefault();
    rows[S.kbIndex].click();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeReports();
  if (e.key === "/" && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
    e.preventDefault(); $("entitySearch").focus(); $("entitySearch").select();
  }
});

$("showAll").onclick = showAll;
$("clearSeed").onclick = () => { S.cited = new Set(); draw(); };
$("graphLimit").onchange = showAll;
$("hops").onchange = () => { if (S.selected) focusEntity(S.selected); else showAll(); };
$("fit").onclick = fit;
$("fitGraph").onclick = fit;
$("relayout").onclick = () => {
  if (S.sim) { S.sim.alpha = 1; startSim(true); }
  setTimeout(fit, 700);
};
$("zoomIn").onclick = () => zoomBy(1.25);
$("zoomOut").onclick = () => zoomBy(1 / 1.25);
$("labelsBtn").onclick = () => {
  S.showLabels = !S.showLabels;
  $("labelsBtn").classList.toggle("on", S.showLabels);
  draw();
};
$("legendBtn").onclick = () => {
  const hidden = $("legend").classList.toggle("hidden");
  $("legendBtn").classList.toggle("on", !hidden);
};
$("askBtn").onclick = askQuestion;
$("examplesBtn").onclick = () => {
  const box = $("examples");
  box.hidden = !box.hidden;
  $("examplesBtn").classList.toggle("on", !box.hidden);
};
$("askBox").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); askQuestion(); }
});
$("copyAnswer").onclick = async () => {
  if (!S.lastAnswer) { toast("nothing to copy yet", "bad"); return; }
  try {
    await navigator.clipboard.writeText(S.lastAnswer);
    toast("answer copied to the clipboard", "ok");
  } catch (e) { toast("clipboard blocked by the browser", "bad"); }
};
for (const t of document.querySelectorAll(".tab")) {
  t.onclick = () => showTab(t.dataset.tab);
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  if (S.sim) {
    S.sim.resize(svg.clientWidth || S.sim.w, svg.clientHeight || S.sim.h);
  }
  draw();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(fit, 200);
});

// ── draggable column splitters, widths remembered across reloads ─────────
(function setupSplitters() {
  const root = document.documentElement;
  const LIMITS = { "--w-left": [220, 520], "--w-right": [320, 760] };
  const stored = { "--w-left": "leftW", "--w-right": "rightW" };
  for (const [prop, key] of Object.entries(stored)) {
    const v = Number(localStorage.getItem(key));
    if (v) root.style.setProperty(prop, v + "px");
  }
  // The pane each splitter controls, so a drag starts from the width actually
  // on screen rather than from the CSS default.
  const PANES = { "--w-left": () => $("splitL").previousElementSibling,
                  "--w-right": () => $("splitR").nextElementSibling };
  const attach = (el, prop) => {
    if (!el) return;
    let startX = 0, startW = 0;
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      startX = e.clientX;
      const pane = PANES[prop]();
      startW = pane ? pane.getBoundingClientRect().width
                    : (parseFloat(getComputedStyle(root).getPropertyValue(prop)) || 320);
    });
    el.addEventListener("pointermove", (e) => {
      if (!el.hasPointerCapture(e.pointerId)) return;
      const [lo, hi] = LIMITS[prop];
      // the right column grows as the pointer moves left, and vice versa
      const delta = prop === "--w-left" ? e.clientX - startX : startX - e.clientX;
      const w = Math.max(lo, Math.min(hi, startW + delta));
      root.style.setProperty(prop, w + "px");
      localStorage.setItem(stored[prop], String(Math.round(w)));
      draw();
    });
    const end = (e) => {
      el.classList.remove("dragging");
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      fit();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  };
  attach($("splitL"), "--w-left");
  attach($("splitR"), "--w-right");
})();

const SAMPLE_QUESTIONS = [
  "What was Apple's total net sales in fiscal year 2025 and how much came from the Americas segment?",
  "What was Apple's Gross Profit, Operating Income, and R&D in FY2025?",
  "What quarterly revenue and period did Apple report in the 10-Q?",
  "What items and events were disclosed in Apple's Form 8-K?",
  "How does Apple's revenue break down across product and geographic segments?",
  "What are Apple's total assets and total liabilities in FY2025?",
  "Which Apple fiscal years show the highest reported net sales?",
  "What did Apple report for research and development expense in the 10-Q?",
];

function seedExamples() {
  const box = $("examples");
  box.textContent = "";
  box.hidden = true;
  $("examplesBtn").classList.remove("on");
  for (const q of SAMPLE_QUESTIONS) {
    const b = document.createElement("button");
    b.textContent = q;
    b.onclick = () => { $("askBox").value = q; askQuestion(); };
    box.appendChild(b);
  }
}

// ── Reports panel ─────────────────────────────────────────────────────────

let _reportsMeta = [];
let _activeReport = null;

async function loadReportsList() {
  try {
    const d = await api('/api/reports');
    _reportsMeta = d.reports || [];
    const box = $('reportsList');
    box.textContent = '';
    for (const r of _reportsMeta) {
      const b = document.createElement('button');
      b.textContent = r.title;
      b.dataset.id = r.id;
      b.onclick = () => runReport(r.id);
      box.appendChild(b);
    }
  } catch (e) {
    $('reportsList').textContent = 'Error loading reports: ' + e.message;
  }
}

async function runReport(id) {
  _activeReport = id;
  for (const b of $('reportsList').querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.id === id);
  }
  const body = $('reportsBody');
  body.innerHTML = '<div class="spinner"><span>⚡</span> Running report…</div>';
  $('reportFilter').value = '';
  try {
    const d = await api('/api/reports/' + encodeURIComponent(id));
    if (d.error) { body.innerHTML = `<div class="note">${esc(d.error)}</div>`; return; }
    _lastReport = d;
    renderReportRows();
  } catch (e) {
    body.innerHTML = `<div class="note">Report failed: ${esc(e.message)}</div>`;
    toast(`report failed: ${e.message}`, "bad");
  }
}

let _lastReport = null;

const fmtCell = (col, v) => {
  if (v === null || v === undefined) return '—';
  if (col === 'value' && typeof v === 'number') {
    return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  if (col === 'scale' && typeof v === 'number') {
    return v === 6 ? 'millions' : v === 3 ? 'thousands' : v === 9 ? 'billions' : String(v);
  }
  return String(v);
};

function renderReportRows() {
  const body = $('reportsBody');
  const d = _lastReport;
  if (!d) return;
  const cols = d.columns || [];
  const term = ($('reportFilter').value || '').toLowerCase();
  const rows = (d.rows || []).filter((row) =>
    !term || row.some((c) => String(c == null ? '' : c).toLowerCase().includes(term)));

  let html = `<div id="reportTitle">${esc(d.title)}</div>`;
  html += `<div id="reportDesc">${esc(d.description)}</div>`;
  if (!d.rows || d.rows.length === 0) {
    html += '<div class="empty">No rows returned — the graph has no matching data yet.</div>';
  } else if (!rows.length) {
    html += `<div class="empty">No row matches "${esc(term)}".</div>`;
  } else {
    html += '<table id="reportTable"><thead><tr>';
    for (const col of cols) html += `<th>${esc(col)}</th>`;
    html += '</tr></thead><tbody>';
    for (const row of rows) {
      html += '<tr>';
      for (let i = 0; i < cols.length; i++) {
        html += `<td>${esc(fmtCell(cols[i], row[i]))}</td>`;
      }
      html += '</tr>';
    }
    html += '</tbody></table>';
    const hidden = (d.rows.length - rows.length);
    html += `<div id="reportCount">${rows.length} of ${d.row_count} row` +
      `${d.row_count !== 1 ? 's' : ''}${hidden > 0 ? ` (${hidden} filtered out)` : ''}</div>`;
  }
  body.innerHTML = html;
}

$('reportFilter').addEventListener('input', renderReportRows);

$('csvBtn').onclick = () => {
  const d = _lastReport;
  if (!d || !d.rows) { toast('run a report first', "bad"); return; }
  const cell = (v) => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
  const text = [d.columns.map(cell).join(',')]
    .concat(d.rows.map((r) => r.map(cell).join(',')))
    .join('\r\n');
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `${d.title || 'report'}.csv`.replace(/[^\w.-]+/g, '_');
  a.click();
  URL.revokeObjectURL(url);
  toast('report downloaded as CSV', "ok");
};

function closeReports() {
  document.getElementById('reportsOverlay').classList.remove('open');
}

$('reportsBtn').onclick = () => {
  document.getElementById('reportsOverlay').classList.add('open');
  if (_reportsMeta.length === 0) loadReportsList();
};
$('closeReports').onclick = closeReports;
document.getElementById('reportsOverlay').addEventListener('click', (e) => {
  if (e.target === document.getElementById('reportsOverlay')) closeReports();
});

(async function init() {
  await loadStats();
  await loadEntities('');
  await showAll();
  seedExamples();
})();
</script>
</body>
</html>
"""


# ── HTTP Server Request Handler ───────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "blueprint-graphrag-ui"
    protocol_version = "HTTP/1.1"

    kg: KnowledgeGraph

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s — %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ct: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._post)

    def _guarded(self, fn: Any) -> None:
        """Run a handler body, turning an unexpected raise into a 500.

        socketserver handles an exception escaping do_POST by printing a
        traceback to stderr and closing the socket *without writing a
        response*. The browser reports that as ``Failed to fetch`` -- with no
        status, no body, and no clue which of the two very different causes it
        was: the server is gone, or the server just failed. The traceback is
        the only real diagnosis, and it is on the terminal that is running the
        server, which is not where the reader is looking.

        So the answer goes in the response, where the failure is being
        reported, and the same message goes to the log.
        """
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up. Nothing to report and nothing to fix.
            raise
        except Exception as exc:
            log.exception("unhandled error serving %s %s", self.command, self.path)
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except Exception:
                # The socket is already gone; the log line above is all we get.
                pass

    def _get(self) -> None:
        parsed = urlparse(self.path)
        p, qs = parsed.path, parse_qs(parsed.query)
        if p in ("/", "/index.html"):
            return self._send(200, _HTML.encode(), "text/html; charset=utf-8")
        if p.startswith("/vendor/") and p[len("/vendor/"):] in VENDOR:
            name = p[len("/vendor/"):]
            return self._send(200, VENDOR[name], "application/javascript; charset=utf-8")
        if p == "/api/stats":
            return self._json(self.kg.stats())
        if p == "/api/entities":
            q = (qs.get("q") or [""])[0].strip()
            limit = _int_param(qs, "limit", 200, maximum=1000)
            return self._json({"entities": self.kg.all_entities(query=q, limit=limit)})
        if p == "/api/graph":
            raw_seed = (qs.get("seed") or [""])[0].strip()
            seeds = [s.strip() for s in raw_seed.split(",") if s.strip()] if raw_seed else []
            hops = _int_param(qs, "hops", 2, minimum=1, maximum=3)
            limit = _int_param(qs, "limit", 250, maximum=500)
            return self._json(self.kg.neighborhood(seeds, hops=hops, limit=limit))
        if p == "/api/rag":
            return self._json(get_backends().resolve())
        if p == "/api/reports":
            # List all canned reports
            return self._json({
                "reports": [
                    {"id": r["id"], "title": r["title"], "description": r["description"]}
                    for r in CANNED_REPORTS
                ]
            })
        if p.startswith("/api/reports/"):
            report_id = p[len("/api/reports/"):]
            return self._json(run_report(self.kg, report_id))
        return self._err(404, f"no route: {p}")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/ask":
            return self._api_ask()
        if parsed.path == "/api/rag":
            return self._api_rag()
        return self._err(404, f"no route: {parsed.path}")

    def _api_rag(self) -> None:
        """Accept a key or a backend choice, then report what is now in effect.

        The key is held in process memory and never echoed back: a response
        that repeated it would put the credential in the browser's devtools
        history and in any proxy log between here and the tab.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "invalid Content-Length")
        if length <= 0:
            return self._err(400, "empty body")
        if length > MAX_BODY:
            return self._err(413, "body too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            return self._err(400, f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return self._err(400, "expected a JSON object")

        if "backend" in payload:
            requested = str(payload.get("backend") or "auto").strip().lower()
            if requested not in ("auto", "nvidia", "ollama"):
                return self._err(400, f"unknown backend: {requested}")
            get_backends().set_backend(requested)

        if "key" in payload:
            key = str(payload.get("key") or "").strip()
            if key and not key.lower().startswith("nvapi-"):
                return self._err(400, "that does not look like an NVIDIA API key "
                                       "(expected it to start with nvapi-)")
            get_backends().set_session_key(key)

        return self._json(get_backends().resolve())

    def _api_ask(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "invalid Content-Length")
        if length <= 0:
            return self._err(400, "empty body")
        if length > MAX_BODY:
            return self._err(413, "body too large")

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            return self._err(400, f"invalid JSON: {exc}")

        question = str(payload.get("question") or "").strip()
        if not question:
            return self._err(400, "question is required")

        result = ask_rag(self.kg, question)
        self._json(result)


def _int_param(qs: dict, name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = (qs.get(name) or [""])[0].strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        return default
    if minimum is not None: v = max(minimum, v)
    if maximum is not None: v = min(maximum, v)
    return v


# ── Server Runner ─────────────────────────────────────────────────────────────

def bind_server(
    handler: type[BaseHTTPRequestHandler], host: str, port: int
) -> ThreadingHTTPServer:
    """Bind *host*:*port*, turning a busy port into an explanation.

    ``ThreadingHTTPServer`` binds and listens inside its constructor, so a port
    already in use surfaces as ``OSError(EADDRINUSE)`` from this call. The bare
    traceback names the exception and nothing a reader can act on, so the two
    causes -- another copy of this server, and the graphrag UI on its own port
    -- are named here instead.
    """
    try:
        return ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        raise OSError(
            errno.EADDRINUSE,
            f"port {port} on {host} is already in use.\n"
            f"  Another copy of this server is probably still running: "
            f"lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
            f"  Or pick another port: --port <n>, or {UI_PORT_ENV}=<n>\n"
            f"  (The other service here, the graphrag UI, defaults to 8765; "
            f"set PORT_GRAPHRAG_UI to move it.)",
        ) from None


def serve(host: str = "127.0.0.1", port: int | None = None, open_browser: bool = True,
          read_only: bool = True, db_path: Path | None = None) -> None:
    _configure_logging()
    port = default_ui_port() if port is None else port

    if db_path is not None and not db_path.is_file():
        log.error("No graph database at %s", db_path)
        raise SystemExit(1)

    db_path = db_path or resolve_db_path()
    if db_path is None:
        log.error(
            "No graph database found. Looked for:\n  %s\n"
            "Build one from the committed filings first:\n"
            "  python -m sandbox_engine --reset\n"
            "then start this server again. To serve a graph from somewhere "
            "else, pass --db <path>.",
            "\n  ".join(str(p) for p in _DB_CANDIDATES),
        )
        raise SystemExit(1)

    # Build the backend registry here, where the cost is visible at start-up,
    # rather than on the first question where it looks like a hang. This reads
    # the credential files and probes for a local Ollama.
    get_backends()

    kg = KnowledgeGraph(db_path, read_only=read_only)
    handler = type("_BoundHandler", (_Handler,), {"kg": kg})
    server = bind_server(handler, host, port)
    server.daemon_threads = True

    url = f"http://{host}:{server.server_address[1]}/"
    stats = kg.stats()
    print(f"\n{'='*70}")
    print(f"  GraphRAG Viewer & Question Answering Engine")
    print(f"{'='*70}")
    print(f"  Web UI       : {url}")
    print(f"  Database     : {db_path}")
    print(f"  Schema       : {stats['schema']}")
    print(f"  Graph Stats  : {stats['nodes']} entities, {stats['edges']} relationships")
    where = {"nvidia": "NVIDIA NIM", "ollama": "local Ollama"}.get(stats["rag_backend"], "unavailable")
    print(f"  RAG Model    : {stats['rag_model'] or 'none'} via {where}")
    print(f"  Why          : {stats['rag_reason']}")
    if stats["rag_backend"] == "none":
        print(f"  Answers      : DISABLED. Paste a key in the browser, or run "
              f"`ollama serve` and `ollama pull {OLLAMA_MODEL}`, then re-ask.")
    print(f"  RAG Timeout  : {RAG_TIMEOUT:.0f}s")
    print(f"  Press Ctrl-C to stop")
    print(f"{'='*70}\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.shutdown()
        server.server_close()
        kg.close()


def _main() -> None:
    _configure_logging()
    p = argparse.ArgumentParser(prog="python -m sandbox_engine.query_ui",
                                description="GraphRAG Question Answering UI for the Blueprint LadybugDB")
    p.add_argument("--port", type=int, default=None,
                   help="port to listen on (default: $%s, else %d)"
                        % (UI_PORT_ENV, DEFAULT_UI_PORT))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--db", type=Path, default=None,
                   help="Database file to serve (default: %s). Use a separate "
                        "copy of the graph to run a second UI on another port; "
                        "LadybugDB locks the file, so two servers cannot share "
                        "one path." % _DB_CANDIDATES[0])
    p.add_argument("--read-write", action="store_true",
                   help="Open the database read-write (takes an exclusive lock, "
                        "so no other server can share the file)")
    args = p.parse_args()
    try:
        serve(args.host, args.port, open_browser=not args.no_browser,
              read_only=not args.read_write, db_path=args.db)
    except OSError as exc:
        # A busy port is an operator decision, not a crash: report it and exit
        # non-zero rather than printing a traceback from inside http.server.
        log.error("%s", exc)
        raise SystemExit(1)
    except ValueError as exc:
        # Same reasoning for a mistyped PORT_QUERY_UI: a one-character slip is
        # not worth a traceback, and a different exit code keeps it
        # distinguishable from the port-already-in-use case above.
        log.error("%s", exc)
        raise SystemExit(2)


if __name__ == "__main__":
    _main()
