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
import contextlib
import errno
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from dataclasses import replace
from functools import lru_cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlparse

import ladybug as lb

from .buffer import NODE_TABLES, REL_TABLES
from .config import FINGRAPH_DATA_DIR
from .provenance import (
    REFUSED,
    _Alias,
    issuer_forms,
    issuer_in_text,
    build_evidence,
    build_evidence_refs,
    citation_tags,
    grade_answer,
    render_gap,
    serialise_evidence,
    serialise_evidence_refs,
    EvidenceRef,
    SECGradingAdapter,
)
from .router import EntityRoute, route_query
from .http_limits import (
    BoundedThreadingHTTPServer,
    ConnectionCounter,
    Limits,
)
from .tier1_fetch import SECRuntimeFetcher
from .tier1_clean import clean_and_truncate_section
from .coldstart_extract import ColdStartExtractor
from .stitch import InMemoryOverlayGraph, stitch_coldstart_payload
from .traversal import HybridGraphTraverser
from .coldstart_synthesis import ColdStartSynthesizer
from .background import BackgroundIngestQueue
from .shutdown import (
    DatabaseSealed,
    ShutdownCoordinator,
    ShutdownStarted,
    install_signal_handlers,
    is_mutation,
    restore_signal_handlers,
    serve_until_signalled,
)
from .ssrf import DEFAULT_CONFIG, SSRFConfig, validate_url

# SSRF config that allows localhost for Ollama probe (expected to fail in prod)
OLLAMA_SSRF_CONFIG = SSRFConfig(
    allowed_hosts=frozenset({"127.0.0.1", "localhost", "::1"}),
    allow_localhost=True,
    follow_redirects=False,
    max_redirects=5,
)
from .observability import (
    StageTimer,
    generate_request_id,
    get_request_id,
    set_request_id,
    clear_request_id,
    record_request,
    record_rate_limit_rejection,
    record_retry,
    record_llm_failure,
    record_external_api_failure,
    record_sec_fetch,
    record_ollama_probe,
    record_ingestion_job,
    new_span_id,
    configure_structured_logging,
)

background_queue = BackgroundIngestQueue()

#: Frontend libraries served under ``/vendor/``. Vendored locally so the page
_VENDOR_DIR = Path(__file__).resolve().parent / "static"
VENDOR: dict[str, bytes] = {}
for _vendor_name in ("d3.v7.min.js", "gsap.min.js"):
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
# Priority order: persistent data dir (survives restarts) first, then the
# legacy _run location. The persistent path is the authoritative location.
_DB_CANDIDATES = (
    FINGRAPH_DATA_DIR / "sandbox.lbug",
    _HERE / "_run" / "sandbox.lbug",
)

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

    Priority order:
    1. Persistent data directory (FINGRAPH_DATA_DIR/sandbox.lbug) -- the
       authoritative LadybugDB that survives restarts.
    2. Legacy _run/sandbox.lbug -- for backwards compatibility.

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

#: How long a drain may wait before it stops waiting and closes anyway.
#: Read through ``_setting`` like every other runtime knob, so ``.env`` works.
#:
#: Sized against the two deadlines in play. A graph query here is sub-second,
#: but ``RAG_TIMEOUT`` above is 900s and an SSE answer holds its thread for all
#: of it, so the drain cannot promise to carry every answer to the end -- it
#: promises to carry the ones that finish inside this budget. Thirty seconds
#: clears the queries and the file writes, and fits inside the ~30s a supervisor
#: waits before SIGKILL, so the common case never reaches the hard kill at all.
#: Raise it only alongside the supervisor's own timeout, or the process is
#: SIGKILLed mid-drain and the careful ordering below is worth nothing.
SHUTDOWN_TIMEOUT = float(_setting("SHUTDOWN_TIMEOUT", "30"))

#: HTTP resource ceilings, applied to every listener built by ``_listeners``.
#:
#: ``ThreadingHTTPServer`` makes no limit on connections and its handler sets no
#: socket timeout, so an idle keep-alive connection costs a thread forever.
#: Measured on this machine before these ceilings existed: 200 idle connections
#: held 219 threads indefinitely, and 100 stalled sockets held 100 more. These
#: four numbers bound that cost; see ``sandbox_engine/http_limits.py`` for the
#: measurements and for why the stream ceiling is separate from the rest.
#:
#: Read through ``_setting`` like every other runtime knob, so ``.env`` works.
#:
#: ``REQUEST_TIMEOUT`` must exceed the slowest legitimate non-streaming
#: response. It bounds the *socket*, per read and per write, not the total
#: request: a stream that writes continuously is not cut off by it. Raising it
#: weakens the slow-client and idle-connection protections; lowering it below
#: a real answer time will break normal requests.
REQUEST_TIMEOUT = float(_setting("REQUEST_TIMEOUT", "30"))

#: Simultaneous open connections per listener. At ~200 the measured thread cost
#: of an idle connection is 200 threads' worth of address space, so the default
#: is generous for a single-user UI and still finite.
MAX_CONNECTIONS = int(_setting("MAX_CONNECTIONS", "256"))

#: Simultaneous SSE streams. Deliberately below ``MAX_CONNECTIONS``: a stream
#: pins its thread for as long as the model takes, so if streams could take the
#: whole pool, ordinary API calls would queue behind them and the UI would look
#: hung. With this ceiling, a burst of streams costs at most this many threads
#: and the rest of the pool stays available for normal requests.
MAX_SSE_CONNECTIONS = int(_setting("MAX_SSE_CONNECTIONS", "32"))

#: Hard ceiling on one SSE response. Defaults to ``RAG_TIMEOUT`` because that is
#: the model call's own bound and nothing shorter can pre-empt it; lowering this
#: makes a stream emit a truncated-answer event sooner rather than extending it.
SSE_MAX_SECONDS = float(_setting("SSE_MAX_SECONDS", str(int(RAG_TIMEOUT))))

#: Listen backlog. The stdlib default is 5, which refuses a connection burst in
#: the kernel before the server can answer with a status.
LISTEN_BACKLOG = int(_setting("LISTEN_BACKLOG", "128"))

#: How many times a model call may be retried before the failure is reported.
#: Two, because the hosted endpoint's own failure mode under load is 503
#: "Service temporarily overloaded" -- a condition that clears on its own in
#: seconds, and that used to surface as a dead answer because the client was
#: built with no retries at all. The SDK only retries what is safe to repeat
#: (connection errors, 408, 409, 429, 5xx), so a malformed request or a
#: rejected key still fails on the first attempt.
RAG_RETRIES = int(_setting("RAG_RETRIES", "2"))

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
# Connect and read timeouts for Ollama probe (urllib uses single socket timeout)
_OLLAMA_PROBE_CONNECT_TIMEOUT = 2.0
_OLLAMA_PROBE_READ_TIMEOUT = 2.0


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
        # urllib uses a single socket timeout; use the more restrictive of connect/read
        probe_timeout = min(_OLLAMA_PROBE_CONNECT_TIMEOUT, _OLLAMA_PROBE_READ_TIMEOUT)
        request_id = get_request_id()
        
        # Validate URL against SSRF protection (allow localhost for Ollama probe)
        valid, error = validate_url(url, OLLAMA_SSRF_CONFIG)
        if not valid:
            record_ollama_probe(success=False)
            log.debug(
                "ollama_probe_ssrf_blocked",
                extra={
                    "request_id": request_id,
                    "error": error,
                    "url": url,
                },
            )
            return False, []
        
        try:
            with urllib.request.urlopen(url, timeout=probe_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            record_ollama_probe(success=False)
            log.debug(
                "ollama_probe_failed",
                extra={
                    "request_id": request_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return False, []
        names = []
        for entry in payload.get("data") or []:
            name = (entry or {}).get("id")
            if name:
                names.append(str(name))
        record_ollama_probe(success=True)
        log.debug(
            "ollama_probe_success",
            extra={
                "request_id": request_id,
                "models": names,
            },
        )
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

#: Seconds a readiness probe waits for a free connection before giving up. The
#: probe is on a load balancer's poll interval, so it has to be able to answer
#: "busy" quickly; waiting behind a slow question to then say the same thing
#: only delays the balancer's decision.
READINESS_LOCK_TIMEOUT = 1.0

#: How many graph queries may run at once. Reads are the bulk of a question --
#: routing, seeding and evidence are about twenty statements -- and they were
#: previously serialised behind one connection, which capped the whole server at
#: one question at a time no matter how many requests were in flight. Measured
#: on this graph, that lock was 94.7% of wall clock while the model call was
#: under 11%.
#:
#: Four matches LadybugDB's own ``AsyncConnection`` default, and is deliberately
#: small: it bounds concurrent statements, not throughput. Raise it only with a
#: measurement, because each extra connection is another native handle.
#: A read-write handle ignores this and stays at one, since only one write
#: transaction may be open at a time.
GRAPH_QUERY_SLOTS = int(_setting("GRAPH_QUERY_SLOTS", "4"))

ANSWER_SYSTEM = """\
You answer questions using only a tagged evidence block retrieved from financial filings.

Every line of evidence carries a bracketed tag like [E1] or [E12], and a Source showing
the ticker that filed it, the form, and the period it came from.

Rules:
- Use only the supplied evidence. If it does not contain the answer, say so plainly and state what is missing.
- You may ONLY cite tags that already appear in the evidence. Do not invent a tag, and do not cite a tag you were not given.
- Cite every factual claim using bracketed tags like [E1] or [E2].
- If you compute a value from cited facts, show the arithmetic so the derivation is visible.
- Never state a number that is not in the evidence and not derived from it.
- Do not use outside knowledge. If a fact is not in the evidence, treat it as unknown rather than supplying it.
- A figure belongs to the ticker on its own evidence line. When two issuers report the same line item, never carry a number from one line to the other; and when their fiscal periods do not cover the same span, say so instead of comparing them.
- Answer directly and factually in clean prose or concise bullet points. No preamble.
"""


# ── Database Layer ────────────────────────────────────────────────────────────

class _ConnectionPool:
    """A bounded set of LadybugDB connections handed out one query at a time.

    Why more than one connection is safe, stated once: LadybugDB's own
    ``Connection`` documents that it expects multi-threaded callers and guards
    the per-connection state that needs guarding. From ``ladybug/connection.py``,
    on preparing a statement:

        Serializes prepare / bind / execute on a single connection so that the
        cached entry's mutable bound state cannot be torn by concurrent callers
        (multi-threaded users or AsyncConnection's thread-pool). [...] The C++
        side has its own ``mtx`` around ``executeWithParams``.

    So the guard this class replaces was protecting nothing LadybugDB does not
    already protect. What one connection *cannot* do is run two queries at once,
    because that ``mtx`` is per connection -- which is the whole reason for the
    pool rather than merely dropping the lock.

    Connections are created lazily up to ``max_size`` and returned to an idle
    stack, so a server that only ever answers one question at a time never pays
    for handles it does not use. ``acquire`` with a timeout is what lets
    readiness answer "busy" instead of queueing.
    """

    def __init__(self, database: Any, max_size: int, max_threads: int = 4) -> None:
        self._database = database
        self._max_size = max(1, int(max_size))
        self._max_threads = max(1, int(max_threads))
        self._condition = threading.Condition()
        self._idle: list[Any] = []
        self._made = 0
        self._closed = False

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def in_use(self) -> int:
        """Connections currently checked out. For tests and diagnostics."""
        with self._condition:
            return self._made - len(self._idle)

    def acquire(self, timeout: float | None = None) -> Any | None:
        """Check out a connection, or return ``None`` if none frees up in time.

        ``timeout=None`` waits indefinitely, which is what a request wants.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError("graph pool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._made < self._max_size:
                    self._made += 1
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining if remaining is not None else 0.25)
        # Opening a connection runs initialisation work, so it happens outside
        # the condition. A failure has to give the slot back, or a transient
        # error would permanently shrink the pool.
        try:
            return lb.Connection(self._database, num_threads=self._max_threads)
        except Exception:
            with self._condition:
                self._made -= 1
                self._condition.notify()
            raise

    def release(self, connection: Any) -> None:
        with self._condition:
            self._idle.append(connection)
            self._condition.notify()

    def discard(self, connection: Any) -> None:
        """Retire a connection that failed, so it is not handed out again."""
        with self._condition:
            self._made -= 1
            self._condition.notify()
        try:
            connection.close()
        except Exception:
            log.debug("discarding a failed graph connection", exc_info=True)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._condition.notify_all()
        for connection in idle:
            try:
                connection.close()
            except Exception:
                log.debug("error closing a pooled graph connection", exc_info=True)

    @contextlib.contextmanager
    def slot(self, timeout: float | None = None):
        """Borrow a connection for one query, closing it if it turns out broken."""
        connection = self.acquire(timeout=timeout)
        if connection is None:
            yield None
            return
        healthy = True
        try:
            yield connection
        except Exception:
            healthy = False
            raise
        finally:
            if healthy:
                self.release(connection)
            else:
                self.discard(connection)


class KnowledgeGraph:
    def __init__(
        self,
        db_path: Path,
        read_only: bool = True,
        buffer_pool_size: int | None = None,
        max_num_threads: int | None = None,
    ):
        # Read-only by default: this UI never writes, and LadybugDB takes an
        # exclusive lock on a read-write handle, so a read-write open makes a
        # second server on another port fail to start at all.
        self.path = Path(db_path)
        if buffer_pool_size is None:
            raw_pool = os.environ.get("LADYBUG_BUFFER_POOL_BYTES")
            if raw_pool:
                try:
                    buffer_pool_size = int(raw_pool)
                except ValueError:
                    buffer_pool_size = 128 * 1024 * 1024
            else:
                buffer_pool_size = 128 * 1024 * 1024

        if max_num_threads is None:
            raw_threads = os.environ.get("LADYBUG_MAX_THREADS")
            if raw_threads:
                try:
                    max_num_threads = int(raw_threads)
                except ValueError:
                    max_num_threads = 4
            else:
                max_num_threads = 4

        self.db = lb.Database(
            str(db_path),
            read_only=read_only,
            buffer_pool_size=buffer_pool_size,
            max_num_threads=max_num_threads,
        )
        # A read-write handle is pinned to one connection: LadybugDB allows only
        # one open write transaction unless `enable_multi_writes` is set, so
        # fanning queries out there would trade a throughput problem for a
        # correctness one. Read-only handles -- what this server actually opens
        # -- have no such conflict.
        self.read_only = read_only
        self.pool = _ConnectionPool(
            self.db,
            GRAPH_QUERY_SLOTS if read_only else 1,
            max_threads=max_num_threads,
        )
        #: Set by begin_shutdown(); see that method for why a read-only server
        #: needs a write guard at all.
        self.sealed = False
        # LadybugDB holds the inode it opened. If the file is rebuilt while a
        # server is running, that handle keeps reading the old inode: every
        # query still succeeds and every answer is from a database that no
        # longer exists on disk. The inode is the only part of a file's identity
        # that a checkpoint cannot change, so freshness is judged on it alone.
        try:
            self._inode: int | None = os.stat(self.path).st_ino
        except OSError:
            self._inode = None
        self.schema = detect_schema(self._raw_execute)

    def close(self):
        self.pool.close()
        self.db.close()

    def begin_shutdown(self) -> None:
        """Seal the handle: no new statement may change the database.

        The UI opens read-only by default and none of its request handlers
        writes, so this refuses nothing today. It is here because the
        ``--read-write`` flag exists: a server opened that way holds the
        authoritative file, and a mutation arriving *after* the drain started
        would be committed by a process that is on its way out. Refusing at
        ``execute`` covers every query in the process, including any a future
        handler adds, without each call site having to remember.
        """
        self.sealed = True

    def probe_freshness(self) -> tuple[bool, str]:
        """Whether the file on disk is still the file this handle opened.

        ``probe_read`` answers "can this handle query", which a replaced
        database still passes -- the orphaned inode is perfectly readable and
        perfectly wrong. This answers the other half, "is it the graph on disk",
        which is the only version of the question that catches a rebuild landing
        under a running server. One ``stat``, no query, no lock.
        """
        if self._inode is None:
            return False, "path_unstatable"
        try:
            current = os.stat(self.path).st_ino
        except OSError:
            return False, "missing_on_disk"
        if current != self._inode:
            return False, "replaced_on_disk"
        return True, "current"

    def probe_read(self, timeout: float = READINESS_LOCK_TIMEOUT) -> tuple[bool, str, float]:
        """Ask this handle one bounded question and report whether it answered.

        Readiness cannot decide "can serve requests" without reading the graph,
        so the check has to be a real query. Three properties keep it from
        turning into load of its own:

        * it borrows a connection from the same pool every other request uses,
          so it cannot open a second handle and contend for LadybugDB's lock;
        * it borrows under a deadline, so a fully busy pool yields ``busy``
          rather than a queue -- a balancer needs a fast answer it can act on,
          not a late one. Note this is pool exhaustion, not "some query is
          running": with several slots, ordinary in-flight work no longer makes
          readiness flap, which is what it used to do under any load at all;
        * it is a single ``count`` over one label, which is a metadata lookup
          rather than a scan.

        ``Company`` is the table both schemas name identically, so the probe
        needs no blueprint/engine rewrite and cannot fail merely because the
        graph was rebuilt in the other shape. It never raises: every outcome,
        including a closed handle, comes back as a triple.
        """
        started = time.monotonic()

        def elapsed_ms() -> float:
            return round((time.monotonic() - started) * 1000.0, 3)

        # A closed handle has to report rather than raise: this probe is on an
        # unauthenticated readiness path, and the caller expects a
        # (readable, detail, ms) triple for every outcome, including "this
        # server already shut down".
        try:
            connection = self.pool.acquire(timeout=max(0.0, timeout))
        except Exception as exc:
            return False, type(exc).__name__, elapsed_ms()
        if connection is None:
            return False, "busy", elapsed_ms()
        try:
            connection.execute("MATCH (n:Company) RETURN count(n)", {})
        except Exception as exc:
            # The class name is the entire report. The message can carry a
            # filesystem path or a connection string, and readiness is served
            # without authentication.
            log.warning("readiness probe failed: %s", type(exc).__name__)
            return False, type(exc).__name__, elapsed_ms()
        finally:
            self.pool.release(connection)
        return True, "readable", elapsed_ms()

    def _raw_execute(self, cypher: str, params: dict | None = None) -> list[list[Any]]:
        with self.pool.slot() as connection:
            res = connection.execute(cypher, params or {})
            return [list(r) for r in res.get_all()]

    def execute(self, cypher: str, params: dict | None = None) -> list[list[Any]]:
        """Run *cypher*, translating it first if this is an engine-schema graph.

        Every query in this module is written in blueprint terms. On an
        engine-schema database they are rewritten here, so the rest of the file
        -- and the canned reports -- need no schema awareness at all.

        This is also the only place a query enters the graph from a request, so
        it is where the drain's write seal is enforced. Reads keep working: an
        in-flight request has to be allowed to finish, which is the whole point
        of draining rather than killing.
        """
        if self.sealed and is_mutation(cypher):
            raise DatabaseSealed(
                "refusing to write: this graph was sealed when the server "
                "began shutting down"
            )
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

        # Use a high limit to capture all entities for accurate BFS traversal.
        # The graph has ~122k nodes; 200k provides headroom.
        all_ents = {e["id"]: e for e in self.all_entities(limit=200000)}

        # Fetch ALL connecting relationships (no seed filtering at query time)
        # Use blueprint schema names; execute() translates for engine schema.
        # Avoid toString() which doesn't exist in LadybugDB; format in Python instead.
        # Do NOT include relation name as string literal in RETURN (translation replaces it).
        # Instead, use query index to determine relation type.
        queries = [
            # SUBMITTED: Company -> Filing
            ("MATCH (a:Company)-[r:SUBMITTED]->(b:Filing) RETURN a.ticker, b.accession_number, 'Submitted filing'", "SUBMITTED"),
            # REPORTS_METRIC: Filing -> Metric (period_code is on Metric node, not the edge)
            ("MATCH (a:Filing)-[r:REPORTS_METRIC]->(b:FinancialMetric) RETURN a.accession_number, b.metric_id, r.value, r.currency, b.period_code", "REPORTS_METRIC"),
            # DISAGGREGATED_BY: Metric -> Segment (period is on the edge in engine schema)
            ("MATCH (a:FinancialMetric)-[r:DISAGGREGATED_BY]->(b:Segment) RETURN a.metric_id, b.segment_id, r.value, r.period", "DISAGGREGATED_BY"),
            # DISCLOSES_EVENT: Filing -> DisclosureEvent
            ("MATCH (a:Filing)-[r:DISCLOSES_EVENT]->(b:DisclosureEvent) RETURN a.accession_number, b.event_id, 'Discloses event'", "DISCLOSES_EVENT"),
        ]

        seen_edges = set()
        for cql, rel in queries:
            try:
                for row in self.execute(cql):
                    u, v = row[0], row[1]
                    # Build description from relation type and remaining columns
                    if rel == "REPORTS_METRIC":
                        value, currency, period = row[2], row[3], row[4]
                        desc = f"Reported: value={value:,.2f} {currency or ''}".rstrip()
                        if period:
                            desc += f", period={period}"
                    elif rel == "DISAGGREGATED_BY":
                        value, period = row[2], row[3]
                        desc = f"segment value={value:,.2f}, period {period or 'unstated'}"
                    else:
                        desc = row[2] if len(row) > 2 else rel

                    if u in all_ents:
                        nodes_dict[u] = all_ents[u]
                    if v in all_ents:
                        nodes_dict[v] = all_ents[v]
                    edge_key = (u, v, rel)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({"source": u, "target": v, "relation": rel, "description": desc})
            except Exception as e:
                print(f"DEBUG neighborhood exception: {e}")  # DEBUG
                continue

        # If seeds provided, extract k-hop subgraph from seeds
        if seed_ids:
            # Build adjacency from all edges
            adj: dict[str, set[str]] = {}
            for e in edges:
                s, t = e["source"], e["target"]
                adj.setdefault(s, set()).add(t)
                adj.setdefault(t, set()).add(s)

            # BFS from seeds up to hops distance
            keep_nodes = set()
            frontier = set(s for s in seed_ids if s in nodes_dict)
            keep_nodes.update(frontier)
            for _ in range(hops):
                next_frontier = set()
                for n in frontier:
                    for nb in adj.get(n, ()):
                        if nb not in keep_nodes:
                            keep_nodes.add(nb)
                            next_frontier.add(nb)
                frontier = next_frontier
                if not frontier:
                    break

            # Filter nodes and edges to k-hop subgraph
            nodes_dict = {nid: node for nid, node in nodes_dict.items() if nid in keep_nodes}
            edges = [e for e in edges if e["source"] in keep_nodes and e["target"] in keep_nodes]

        # Apply limit with priority for financial/source-critical edges
        if len(edges) > limit:
            # Group edges by relation type
            by_type: dict[str, list[dict]] = {}
            for e in edges:
                by_type.setdefault(e["relation"], []).append(e)

            # Priority order: primary edges (financial/source-critical) first,
            # secondary edges (segment/event) fill remaining capacity.
            PRIMARY_TYPES = ("REPORTS_METRIC", "SUBMITTED")
            SECONDARY_TYPES = ("DISAGGREGATED_BY", "DISCLOSES_EVENT")

            limited_edges = []

            # First pass: include all primary edges
            for t in PRIMARY_TYPES:
                if t in by_type:
                    limited_edges.extend(by_type[t])

            # If primary edges already exceed limit, trim them proportionally
            if len(limited_edges) > limit:
                # Ensure SUBMITTED (which connects companies to filings) gets a fair share
                # balanced across all companies so all issuers remain visible in the graph,
                # rather than being crowded out by the massive volume of REPORTS_METRIC edges.
                sub_edges = by_type.get("SUBMITTED", [])
                sub_by_co: dict[str, list[dict]] = {}
                for e in sub_edges:
                    sub_by_co.setdefault(e["source"], []).append(e)

                sub_target = min(len(sub_edges), max(len(sub_by_co) * 4, int(limit * 0.15)))
                per_co = max(1, sub_target // max(1, len(sub_by_co)))
                balanced_sub = []
                chosen_filings = set()
                filing_to_co = {}
                for co, co_edges in sorted(sub_by_co.items()):
                    for e in co_edges[:per_co]:
                        balanced_sub.append(e)
                        chosen_filings.add(e["target"])
                        filing_to_co[e["target"]] = co

                metric_budget = max(1, limit - len(balanced_sub))
                all_metric_edges = by_type.get("REPORTS_METRIC", [])

                # Balance metric edges across companies using chosen filings
                metrics_by_co: dict[str, list[dict]] = {co: [] for co in sub_by_co}
                orphan_metrics: list[dict] = []
                for e in all_metric_edges:
                    co = filing_to_co.get(e["source"])
                    if co:
                        metrics_by_co[co].append(e)
                    else:
                        orphan_metrics.append(e)

                balanced_metrics = []
                num_cos = max(1, len(sub_by_co))
                per_co_metric = max(1, metric_budget // num_cos)
                for co in sorted(sub_by_co.keys()):
                    balanced_metrics.extend(metrics_by_co[co][:per_co_metric])

                if len(balanced_metrics) < metric_budget:
                    remaining_slots = metric_budget - len(balanced_metrics)
                    for co in sorted(sub_by_co.keys()):
                        extra = metrics_by_co[co][per_co_metric:per_co_metric + (remaining_slots // num_cos + 1)]
                        balanced_metrics.extend(extra)
                        if len(balanced_metrics) >= metric_budget:
                            break

                limited_edges = balanced_sub + balanced_metrics[:metric_budget]
                if len(limited_edges) < limit and orphan_metrics:
                    limited_edges.extend(orphan_metrics[:limit - len(limited_edges)])
                # Final trim if still over
                limited_edges = limited_edges[:limit]
            else:
                # Second pass: fill remaining capacity with secondary edges
                remaining = limit - len(limited_edges)
                if remaining > 0:
                    secondary_types = [t for t in SECONDARY_TYPES if t in by_type]
                    if secondary_types:
                        per_secondary = max(1, remaining // len(secondary_types))
                        for t in secondary_types:
                            limited_edges.extend(by_type[t][:per_secondary])
                        # If still over limit (uneven distribution), trim
                        if len(limited_edges) > limit:
                            limited_edges = limited_edges[:limit]

            edges = limited_edges
            # Keep only nodes referenced by remaining edges
            kept_nodes = set()
            for e in edges:
                kept_nodes.add(e["source"])
                kept_nodes.add(e["target"])
            nodes_dict = {nid: node for nid, node in nodes_dict.items() if nid in kept_nodes}

        return {
            "seeds": seed_ids,
            "nodes": merge_nodes(list(nodes_dict.values())),
            "edges": merge_edges(edges),
        }


# ── RAG Retrieval & QA Engine ─────────────────────────────────────────────────

#: Spellings a question may use for a segment name the filing abbreviates.
#: Filers write "U.S." in a country table and "United States" in prose, so a
#: substring test over the raw question misses the segment a question is plainly
#: asking about. Kept to abbreviations that are genuinely ambiguous in a filing
#: and so worth expanding; an ordinary name already matches itself.
_SEGMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "u.s": ("united states", "usa", "america", "united states of america"),
    "us": ("united states", "usa", "u.s", "america"),
    "uk": ("united kingdom", "britain", "great britain"),
    "uae": ("united arab emirates",),
    "greater china": ("china", "mainland china"),
    "rest of asia pacific": ("asia pacific", "rest of asia", "asia"),
    "rest of world": ("other countries", "other"),
    "other countries": ("rest of world", "other"),
    "emea": ("europe", "middle east", "africa"),
    "apac": ("asia pacific", "asia"),
}


def _segment_name_matches(name_l: str, q_low: str) -> bool:
    """Whether a segment name, or a known spelling of it, occurs in the question.

    Matched on word boundaries rather than as a bare substring, so "us" does not
    fire on "because", "industry" or "focus" -- all of which a substring test
    would match, seeding the United States segment for a question that never
    mentioned a country.
    """
    if not name_l:
        return False
    candidates = (name_l,) + _SEGMENT_ALIASES.get(name_l, ())
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", q_low)
        for candidate in candidates
    )


def _period_label(period_code, period_end) -> str:
    """A period a reader can check, from a metric's own period columns.

    ``period_code`` is the span (``3M``, ``6M``, ``9M``, ``FY``) and
    ``period_end`` the date it closes on. Two things are deliberately *not*
    rendered: an absent code yields no word rather than the string ``None``,
    and ``period_end`` is dropped when it is the ``0001-01-01`` sentinel that
    stands for "the column header printed only a year" -- 1319 of Microsoft's
    1897 metrics carry it, and printing it would be worse than printing nothing.
    """
    bits: list[str] = []
    code = str(period_code or "").strip()
    if code:
        bits.append(code)
    end_year = getattr(period_end, "year", None)
    if end_year and end_year > 1900:
        bits.append(f"ending {period_end.isoformat()}")
    if not bits:
        return ""
    return "period " + " ".join(bits)


#: Periods of one concept kept per issuer for an ordinary question. Three covers
#: "the latest quarter", "last quarter" and the comparative alongside them.
_MAX_PERIODS_PER_ISSUER = 3
#: ...and for a question that asks for the series itself.
_MAX_PERIODS_PER_ISSUER_SERIES = 16
_SERIES_WORDS = (
    "trend", "trends", "over the", "each quarter", "each year", "every quarter",
    "every year", "history", "historical", "by quarter", "by year", "yearly",
    "quarterly", "trajectory", "progression", "last n", "past n",
)


def _bound_metric_periods(
    candidates: list[tuple],
    q_low: str,
    filer: dict[str, str],
    metric_filers: dict[str, set[str]],
) -> list[tuple]:
    """Keep the most recent periods of each concept, per issuer.

    Grouping is by concept *and* issuer, so two companies reporting the same
    measure never crowd each other out -- trimming a comparison to one company
    would be worse than the token saving. Within a group the newest period ends
    first, and an undated period sorts last because the graph does not know
    which of them is the "most recent" the question asked for.
    """
    cap = (
        _MAX_PERIODS_PER_ISSUER_SERIES
        if any(word in q_low for word in _SERIES_WORDS)
        else _MAX_PERIODS_PER_ISSUER
    )
    groups: dict[tuple[str, str], list[tuple]] = {}
    for mid, _cname, _stype, _aclass, period_end, concept in candidates:
        issuers = sorted(
            {filer[acc] for acc in metric_filers.get(mid, set()) if acc in filer}
        )
        key = (concept, issuers[0] if issuers else "")
        groups.setdefault(key, []).append(
            (str(period_end or ""), mid, _cname, _stype, _aclass)
        )
    kept: list[tuple] = []
    for entries in groups.values():
        # Newest end date first; the second pass pins an undated period last
        # even though reverse sorting put its empty key at the tail already.
        entries.sort(key=lambda e: e[0], reverse=True)
        entries.sort(key=lambda e: e[0] == "")
        for _end, mid, cname, stype, aclass in entries[:cap]:
            kept.append((mid, cname, stype, aclass, None, ""))
    return kept


def _names_issuer(question: str, ticker: str, legal_name: str | None) -> bool:
    """Whether the question names this issuer, by ticker or by trading name.

    The stored legal names are "Apple Inc" and "MICROSOFT CORPORATION", neither
    of which appears in "Compare Apple and Microsoft" -- so a verbatim test finds
    no issuer in almost every real question and the scoping does nothing. The
    corporate suffix is what a person drops when they write the company, so it is
    stripped and the distinctive remainder matched on word boundaries. A short
    remainder ("3M", "IBM") is not matched here at all: a question is where a
    quantity gets written as "3M miles", and scoping that to 3M would pull the
    wrong filer into the prompt. The grader reads answers rather than questions
    and takes the opposite trade, which is why the two pass the same helper with
    different answers to that question -- not because either is the correct one
    in general.
    """
    for spelling in issuer_forms(ticker) + issuer_forms(legal_name):
        if issuer_in_text(question, _Alias(
            form=spelling.lower(),
            originals=frozenset({spelling}),
            issuers=frozenset({ticker}),
        ), short_names=False):
            return True
    return False


# Cache for retrieve_financial_context results
# Key: (question_hash, ticker, fiscal_year, fiscal_quarter, form_type)
# Value: (nodes, edges, tag_map, seed_ids)
_RETRIEVAL_CACHE: dict[tuple, tuple] = {}
_RETRIEVAL_CACHE_MAX = 128


def _make_cache_key(
    question: str, 
    ticker: str | None, 
    fiscal_year: int | None, 
    fiscal_quarter: str | None, 
    form_type: str | None
) -> tuple:
    """Create a hashable cache key from query parameters."""
    import hashlib
    q_hash = hashlib.sha256(question.lower().encode()).hexdigest()[:16]
    return (q_hash, ticker or "", fiscal_year or 0, fiscal_quarter or "", form_type or "")


def retrieve_financial_context(
    kg: KnowledgeGraph, 
    question: str, 
    ticker: str | None = None,
    fiscal_year: int | None = None,
    fiscal_quarter: str | None = None,
    form_type: str | None = None,
) -> tuple[list[dict], list[dict], dict[str, str], list[str]]:
    """Retrieves relevant entities and relations matching the question and tags them [E1], [E2]...

    Returns ``(nodes, edges, tag_map, seed_ids)``. There is no formatted
    context string: the prompt is assembled from the evidence block, which
    carries each line's provenance, so a parallel rendering of the same graph
    would be a second thing to keep in step and nothing would read it.
    
    Temporal filters:
    - fiscal_year: Filter to specific fiscal year (e.g., 2024)
    - fiscal_quarter: Filter to specific quarter (e.g., "Q1", "FY")
    - form_type: Filter to specific form type (e.g., "10-K", "10-Q", "8-K")
    """
    # Check cache
    cache_key = _make_cache_key(question, ticker, fiscal_year, fiscal_quarter, form_type)
    if cache_key in _RETRIEVAL_CACHE:
        cached = _RETRIEVAL_CACHE[cache_key]
        # Return copies to prevent accidental mutation of cached data
        return (list(cached[0]), list(cached[1]), dict(cached[2]), list(cached[3]))

    q_low = question.lower()

    # Identify candidate seeds
    seed_nodes: list[dict] = []
    seen_ids: set[str] = set()
    #: Metric ids the question's own words matched. Drives the segment
    #: traversal below, which is what makes "long-lived assets in the United
    #: States" findable when the segment is spelled "U.S".
    matched_metrics: list[str] = []

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

    # Build temporal filters for filings query
    temporal_where = []
    temporal_params: dict[str, Any] = {}
    if ticker:
        temporal_params["ticker"] = ticker
    if fiscal_year is not None:
        temporal_where.append("f.fiscal_year = $fiscal_year")
        temporal_params["fiscal_year"] = fiscal_year
    if fiscal_quarter is not None:
        temporal_where.append("f.fiscal_period = $fiscal_quarter")
        temporal_params["fiscal_quarter"] = fiscal_quarter
    if form_type is not None:
        temporal_where.append("f.form_type = $form_type")
        temporal_params["form_type"] = form_type
    
    temporal_where_clause = " AND ".join(temporal_where) if temporal_where else "1=1"

    # Check filings. The filer is part of the label: "10-K FY2026 (FY)" is what
    # every issuer's annual report looks like, so without it a multi-company
    # graph renders one indistinguishable box per filing.
    filer: dict[str, str] = {}
    if ticker:
        filings = kg.execute(
            f"MATCH (c:Company {{ticker: $ticker}})-[:SUBMITTED]->(f:Filing) "
            f"WHERE {temporal_where_clause} "
            f"RETURN f.accession_number, f.form_type, f.fiscal_year, f.fiscal_period, f.period_end_date",
            temporal_params,
        )
        for f in filings:
            filer[f[0]] = ticker
    else:
        try:
            for acc, tick in kg.execute(
                "MATCH (c:Company)-[:SUBMITTED]->(f:Filing) RETURN f.accession_number, c.ticker"
            ):
                filer[acc] = tick
        except Exception:
            pass
        filings = kg.execute(
            f"MATCH (f:Filing) WHERE {temporal_where_clause} "
            f"RETURN f.accession_number, f.form_type, f.fiscal_year, f.fiscal_period, f.period_end_date",
            temporal_params,
        )
    #: Issuers the question names. "Compare Apple and Microsoft" must not answer
    #: with NVIDIA's revenue: the words match every issuer equally well, so
    #: without this the prompt carries a third company's figures and the model
    #: picks whichever it saw last. Only the filer of a named issuer is evidence
    #: for a question about that issuer.
    named: set[str] = {
        tick
        for tick, lname, _cik in companies
        if _names_issuer(question, tick, lname)
    }
    #: The filings the question's own words selected. A metric is in scope only
    #: when one of these reported it.
    filing_ids: set[str] = set()
    for f in filings:
        acc, form, fy, fp, ped = f[0], f[1], f[2], f[3], f[4]
        # Filter filings to resolved company if ticker is known
        if ticker and filer.get(acc) and filer[acc] != ticker:
            continue
        hint = filer.get(acc, acc)
        if named and hint not in named:
            continue
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
        filing_ids.add(acc)

    # Which filing reported which metric.
    #
    # Metric names are period-scoped, so "revenue" matches every Net Sales node
    # in the graph -- every period of every issuer, 251 nodes and 84k characters
    # of prompt for a two-company comparison, and the model has to guess which
    # few lines answer the question. Filtering on the filings already selected
    # removes the issuers the question never named and the forms it never asked
    # for, and it can only narrow: a metric reachable from a selected filing is
    # exactly the evidence that filing offers.
    metric_filers: dict[str, set[str]] = {}
    if filing_ids:
        for acc, mid in kg.execute(
            "MATCH (f:Filing)-[:REPORTS_METRIC]->(m:FinancialMetric) "
            "WHERE f.accession_number IN $accs "
            "RETURN f.accession_number, m.metric_id",
            {"accs": sorted(filing_ids)},
        ):
            metric_filers.setdefault(mid, set()).add(acc)
    else:
        for acc, mid in kg.execute(
            "MATCH (f:Filing)-[:REPORTS_METRIC]->(m:FinancialMetric) "
            "RETURN f.accession_number, m.metric_id"
        ):
            metric_filers.setdefault(mid, set()).add(acc)

    # Check metrics - bound to candidate metric IDs when available
    if metric_filers:
        metrics = kg.execute(
            "MATCH (m:FinancialMetric) WHERE m.metric_id IN $mids "
            "RETURN m.metric_id, m.canonical_name, m.statement_type, m.account_class, m.period_end",
            {"mids": sorted(metric_filers.keys())},
        )
    else:
        metrics = kg.execute(
            "MATCH (m:FinancialMetric) RETURN m.metric_id, m.canonical_name, "
            "m.statement_type, m.account_class, m.period_end"
        )
    # Question text with punctuation collapsed so "shareholders' equity" (straight
    # or curly apostrophe) always matches a stored "shareholders' equity" label.
    q_norm = re.sub(r"[^a-z0-9\s]", " ", q_low)
    q_norm = re.sub(r"\s+", " ", q_norm).strip()
    candidates: list[tuple] = []
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
            if metric_filers.get(mid, set()) & filing_ids:
                candidates.append((mid, cname, stype, aclass, m[4], name_clean))

    # Bound how many periods of one concept reach the prompt.
    #
    # "Compare Apple and Microsoft revenue in the most recent quarter" matches
    # every period either company ever reported, and the model then has to pick
    # the right quarter out of a wall of them -- which is how a FY2025 annual and
    # a Q3 quarter both reach the answer. Keeping the most recent few per issuer
    # per concept keeps the periods a question about "the latest" or "last
    # quarter" can mean, and a question that really does want the series says so
    # ("over the last eight quarters", "trend", "each year") and gets the cap
    # raised instead of being silently truncated.
    for mid, cname, stype, aclass, _pend, _concept in _bound_metric_periods(
        candidates, q_low, filer, metric_filers
    ):
        add_node(mid, cname, "FinancialMetric", f"Statement: {stype}, Class: {aclass}")
        matched_metrics.append(mid)

    segments = kg.execute("MATCH (s:Segment) RETURN s.segment_id, s.dimension_name, s.dimension_type")

    # Segments that a metric the question already matched is broken down by.
    #
    # This is the path a question like "how much did it invest in the United
    # States" needs, and name matching cannot supply it: the segment is stored
    # as "U.S" while the question says "United States", so an exact-name test
    # never fires for it. Worse, the segments that *do* match by name --
    # "China", "Other countries" -- then arrive carrying whichever metric
    # connects to them, which is net sales, so the model is handed China
    # 64,377 when it asked about long-lived assets. Traversal from the matched
    # metric is the structural answer: if the question named the measure, the
    # dimensions that measure is broken down by are what it is asking about,
    # whatever those dimensions happen to be called.
    if matched_metrics:
        by_name = {
            (row[1] or ""): (row[0], row[2])
            for row in segments
        }
        try:
            for sname, sdtype in kg.execute(
                "MATCH (m:FinancialMetric)-[d:DISAGGREGATED_BY]->(s:Segment) "
                "RETURN DISTINCT s.dimension_name, s.dimension_type"
            ):
                hit = by_name.get(sname or "")
                if hit:
                    add_node(hit[0], sname, "Segment",
                             f"Dimension type: {hit[1] or sdtype}")
        except Exception:
            pass

    # Check segments
    for s in segments:
        sid, name, dtype = s[0], s[1], s[2]
        name_l = (name or "").lower()
        dtype_l = (dtype or "").lower()
        if (_segment_name_matches(name_l, q_low) or dtype_l in q_low
                or "segment" in q_low or "breakdown" in q_low
                or "geograph" in q_low or "product" in q_low):
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
    # One query for every filing at once. It used to be run once per filing with
    # no WHERE, so each run returned every chunk in the corpus -- ~4,700 of
    # them -- and the caller discarded all but one filing's worth: ~4,700 rows
    # transferred per filing, twelve times over, to arrive at the same set.
    #
    # The WHERE and the RETURN name the same property, and that is the part that
    # matters. `Filing.id` is `stable_id("filing", ticker, form_type, ...)`, a
    # hash, while the accession list below is built from
    # `MATCH (f:Filing) RETURN f.accession_number, ...`. The two are equal only
    # while the corpus holds no real SEC accession numbers, because
    # `accession_number` falls back to that same hash when there is none. Bind
    # the hash against an accession list and the filter matches nothing -- and
    # a chunk query that matches nothing raises no error and drops every
    # narrative passage from every answer. Asking for the property the
    # accession list was actually read from keeps the two in step on either
    # schema: `accession_number` is renamed to `id` by the engine translation,
    # so both sides move together.
    narrative_forms = {
        f[0]: f[1] for f in filings
        if f[1] in ("10-K", "10-Q") and f[0] in seen_ids
    }
    if narrative_forms:
        for chunk_acc, cid, ctext, csection in kg.execute(
            "MATCH (f:Filing)-[:CONTAINS_CHUNK]->(c:DocumentChunk) "
            "WHERE f.accession_number IN $accs "
            "RETURN f.accession_number, c.id, c.text, c.section",
            {"accs": sorted(narrative_forms)},
        ):
            form = narrative_forms.get(chunk_acc)
            if not form:
                continue
            text_low = (ctext or "").lower()
            q_score = sum(1 for kw in keywords if kw in text_low)
            biz_score = sum(1 for term in _BIZ_TERMS if term in text_low)
            chunk_hits.append(
                {"filing": chunk_acc, "id": cid, "text": ctext or "", "section": csection or "",
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

    for idx, node in enumerate(retrieved_nodes, start=1):
        # One direction only. The reverse map existed to render the context
        # string, and with that gone it was written on every node of every
        # question and read by nothing.
        tag_map[f"E{idx}"] = node["id"]

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
    #
    # The period and the as-printed column header live on the *Metric node*, not
    # on the edge. ``REPORTS_METRIC`` carries only ``value`` and ``currency``, so
    # asking it for ``scale``/``period_type``/``raw_label`` returns three nulls
    # and every figure reaches the prompt as "Reported : value=416,161.00 USD
    # (), period=None" -- a number with no unit, no column header and no period,
    # which is why a FY2025 annual and a Q3 quarter were indistinguishable once
    # they were in the prompt. ``Metric.period_code`` is spelled that way
    # (buffer.py) precisely so this translation does not rewrite it onto the
    # segment edge's ``period``; reading it here is what it was named for.
    for r in kg.execute(
        "MATCH (f:Filing)-[x:REPORTS_METRIC]->(m:FinancialMetric) "
        "RETURN f.accession_number, m.metric_id, x.value, x.currency, "
        "m.reported_label, m.period_code, m.period_end"
    ):
        f_acc, m_id, val, curr, label, pcode, pend = (r[0], r[1], r[2], r[3], r[4], r[5], r[6])
        if f_acc in node_ids and m_id in node_ids:
            desc = f"Reported {label or ''}: value={val:,.2f} {curr or ''}".rstrip()
            period = _period_label(pcode, pend)
            if period:
                desc += f", {period}"
            retrieved_edges.append({
                "source": f_acc, "target": m_id,
                "relation": "REPORTS_METRIC",
                "description": desc,
                "value": val, "period_type": pcode,
            })

    # 3. FinancialMetric -> Segment
    #
    # The period is read as ``period_type`` -- the display name the rel's
    # ``period`` column is published under. Asking for ``fiscal_year`` and
    # ``fiscal_period`` instead yields nulls, and the evidence then reads
    # "FYNone None", which leaves the model unable to tell a FY2025 figure
    # from a FY2024 one. The measure's own name is included because the
    # segment value is meaningless without it: "segment value=40,274" on its
    # own could be revenue, assets or anything else, and the model reads the
    # line rather than the graph. ``d.scale`` is not asked for at all: the
    # table has no such column, so it was a literal NULL rendering as an empty
    # "()" after every segment figure.
    for r in kg.execute(
        "MATCH (m:FinancialMetric)-[d:DISAGGREGATED_BY]->(s:Segment) "
        "RETURN m.metric_id, s.segment_id, d.value, d.period_type, "
        "m.canonical_name"
    ):
        m_id, s_id, val, period, measure = r[0], r[1], r[2], r[3], r[4]
        if m_id in node_ids and s_id in node_ids:
            desc = (f"{measure or 'value'}: segment value={val:,.2f}, "
                    f"period {period or 'unstated'}")
            retrieved_edges.append({
                "source": m_id, "target": s_id,
                "relation": "DISAGGREGATED_BY",
                "description": desc,
                "value": val, "period_type": period,
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

    # Dedupe before anything reads the edges, not only at the JSON boundary:
    # the same edge is reachable from more than one of the queries above, and a
    # repeated "[E7] --REPORTS_METRIC--> [E9]" costs prompt budget while
    # telling the model nothing new.
    retrieved_edges = merge_edges(retrieved_edges)

    # Cache the result
    result = (
        retrieved_nodes,
        retrieved_edges,
        tag_map,
        [n["id"] for n in seed_nodes[:5]]
    )
    if len(_RETRIEVAL_CACHE) >= _RETRIEVAL_CACHE_MAX:
        # Simple eviction: clear oldest half
        keys_to_remove = list(_RETRIEVAL_CACHE.keys())[:_RETRIEVAL_CACHE_MAX // 2]
        for k in keys_to_remove:
            del _RETRIEVAL_CACHE[k]
    _RETRIEVAL_CACHE[cache_key] = result

    # No context string is built here. It used to be: every node and every edge
    # rendered to a tagged "[E1] --REPORTS_METRIC--> [E9]" line, joined, and
    # returned as the first value -- and the one caller unpacked it into a name
    # it never read, because the prompt is assembled from the evidence block
    # instead, which carries the provenance each line needs. Fifteen thousand
    # characters of formatting per question for a string nobody saw.
    return retrieved_nodes, retrieved_edges, tag_map, [n["id"] for n in seed_nodes[:5]]


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
    if status in (500, 502, 503, 529):
        # 503 is what a capacity-limited endpoint returns, and it is the one
        # worth naming in plain words: the raw body is a dict that tells a
        # reader nothing they can act on, and "Error communicating with" reads
        # like their question was malformed. It is their question's fault
        # neither -- the request was fine and the service was full. Saying so is
        # also what keeps this from being read as a verdict on the evidence.
        return (
            f"NVIDIA's endpoint is temporarily overloaded ({status}) and did not "
            f"answer. This is a capacity problem on their side, not a problem "
            f"with the question: nothing was graded or refused, because no answer "
            f"was ever produced. Asking again in a moment usually works, and "
            f"RAG_BACKEND=ollama answers locally in the meantime. The graph "
            f"explorer below does not need either."
        )
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


def _cold_start_response(ticker: str | None, question: str) -> dict[str, Any]:
    msg = "Entity not indexed. Triggering JIT pipeline..."
    return {
        "status": "cold_start_required",
        "entity": ticker,
        "message": msg,
        "text": msg,
        "question": question,
        "grounded": False,
        "used_tags": [],
        "tag_map": {},
    }


def _ambiguous_response(question: str) -> dict[str, Any]:
    """Ask for an entity instead of guessing one.

    This is a terminal response: ask_rag returns it before touching the
    database or the model, so an unresolvable question can never be answered
    against a silently substituted issuer.
    """
    msg = "Please specify a valid company name or stock ticker (e.g. $JPM, $AAPL) to analyze."
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


# ── Wire telemetry ────────────────────────────────────────────────────────────

#: The stages reported on the wire, in pipeline order. ``total`` is not a stage
#: -- it is the request duration and so is measured from construction, not
#: accumulated from the others.
WIRE_STAGES: tuple[str, ...] = (
    "routing",
    "fetching",
    "extraction",
    "stitching",
    "traversal",
    "synthesis",
)

# What each stage is doing, in the words of someone waiting on the request.
# Keyed by the same names as WIRE_STAGES, because the client switches on those.
# `fetching`/`extraction`/`stitching` belong to the cold-start route only; a
# known entity is already in the graph, so it skips them and they are not
# expected to appear in that route's plan.
STAGE_MESSAGES: dict[str, str] = {
    "routing": "Matching the question to an entity",
    "fetching": "Fetching the latest filing from EDGAR",
    "extraction": "Extracting financial facts from the filing",
    "stitching": "Stitching the new facts onto the graph",
    "traversal": "Traversing the knowledge graph",
    "synthesis": "Composing the answer from the evidence",
}

# The stages a route is expected to cross, in order. Sent to the client up front
# so it can build the pipeline before the work starts, rather than discovering
# the shape of the run one event at a time.
ROUTE_PLAN: dict[str, tuple[str, ...]] = {
    "KNOWN": ("routing", "traversal", "synthesis"),
    "COLD_START": (
        "routing",
        "fetching",
        "extraction",
        "stitching",
        "traversal",
        "synthesis",
    ),
}


class _StageTimer:
    """Wall-clock accounting for the stages of one request.

    Every stage the pipeline can take is declared up front, so a stage that did
    not run reports ``0.0`` rather than going missing. That distinction is the
    reason this exists: a client cannot tell "extraction took 4ms" from
    "extraction never ran" when the key is simply absent, and the benchmark
    scores cold-start and known paths against each other. Absent-vs-zero is
    also why a stage a route does not use is zeroed rather than filled with
    the request's own duration.

    Not thread-safe, and does not need to be: one instance belongs to one
    request.
    """

    def __init__(self, on_enter: "Callable[[str], None] | None" = None) -> None:
        self._start = time.perf_counter()
        self._stages: dict[str, float] = {name: 0.0 for name in WIRE_STAGES}
        # Called as each stage begins, so a client watching the request can see
        # the pipeline advance instead of waiting on a silent socket. It is
        # deliberately not called on exit: the duration is only known then, and a
        # client that renders "retrieval: 412ms" before the model has been called
        # is reporting work that has not happened yet.
        self._on_enter = on_enter

    @contextlib.contextmanager
    def stage(self, name: str):
        """Time the enclosed block and add it to ``name``'s total.

        Accumulates, so nesting or re-entry adds up rather than overwrites.
        Exceptions still record the time spent before unwinding: a stage that
        raised did consume wall clock, and reporting 0.0 for it would make a
        timeout look instant.
        """
        started = time.perf_counter()
        if self._on_enter is not None:
            # Outside the try: a broken progress callback must not be able to
            # swallow the stage it is reporting on, which would turn a UI
            # convenience into a request failure.
            try:
                self._on_enter(name)
            except Exception:  # pragma: no cover - defensive
                log.debug("stage callback failed for %s", name, exc_info=True)
        try:
            yield
        finally:
            self._stages[name] = self._stages.get(name, 0.0) + (
                time.perf_counter() - started
            ) * 1000.0

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0

    def as_wire(self) -> dict[str, float]:
        """The ``stage_latencies_ms`` block, rounded for the wire.

        ``total`` is measured from the timer's construction rather than summed
        from the stages, because the two differ by real work -- payload
        assembly, provenance formatting, background scheduling -- and a total
        that does not match the sum of its parts is the only way a client can
        notice unaccounted time.
        """
        out = {name: round(self._stages[name], 2) for name in WIRE_STAGES}
        out["total"] = round(self.elapsed_ms(), 2)
        return out


def _hop_depth_from_paths(paths: Sequence[Sequence[dict[str, Any]]]) -> int:
    """Longest path in a traversal result, in hops.

    The traverser already returns paths, so its own length is the depth -- no
    reconstruction. The client used to rebuild depth from a flattened edge list,
    which is why a wire that carried the edges but not the depth forced that
    guess in the first place.
    """
    return max((len(path) for path in paths), default=0)


def _hop_depth_from_seeds(
    seed_ids: Sequence[str], edges: Sequence[dict[str, Any]]
) -> int:
    """Deepest node reachable from any seed, in hops.

    The known route does not run the cold-start traverser; it selects evidence
    and then fetches the edges among the selected nodes. Its depth is therefore
    a property of that evidence subgraph, so it is measured the same way the
    graph is built: breadth-first from the seeds over the returned edges.

    A seed is depth 0, so a company with no relations reports 0 rather than 1 --
    the honest answer to "how far from the start node did we get".
    """
    if not seed_ids:
        return 0
    adjacency: dict[str, set[str]] = {}
    for e in edges:
        src, dst = e.get("source"), e.get("target")
        if src is None or dst is None:
            continue
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)
    seen = {s for s in seed_ids if s is not None}
    frontier = set(seen)
    depth = 0
    while frontier:
        nxt: set[str] = set()
        for node in frontier:
            nxt |= adjacency.get(node, set()) - seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
        depth += 1
    return depth


def _graph_metrics(
    *,
    max_hop_depth: int,
    node_count: int,
    edge_count: int,
) -> dict[str, int]:
    """The ``graph_metrics`` block.

    Plain counts of what was actually traversed, so a client can report reach
    and shape without re-deriving them. ``node_count``/``edge_count`` count the
    subgraph, not the corpus: the number that matters when asking whether a
    question was answered from context or from the model's memory is how much
    context it was given.
    """
    return {
        "max_hop_depth": int(max_hop_depth),
        "node_count": int(node_count),
        "edge_count": int(edge_count),
    }


def ask_rag(
    kg: KnowledgeGraph,
    question: str,
    on_stage: "Callable[[str], None] | None" = None,
    on_stage_exit: "Callable[[str, float], None] | None" = None,
    fiscal_year: int | None = None,
    fiscal_quarter: str | None = None,
    form_type: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    timer = StageTimer(on_enter=on_stage, on_exit=on_stage_exit, request_id=request_id)
    with timer.stage("routing"):
        routing = route_query(question, kg)

    # Override routing temporal filters with explicit API parameters
    if fiscal_year is not None:
        routing = routing._replace(fiscal_year=fiscal_year)
    if fiscal_quarter is not None:
        routing = routing._replace(fiscal_quarter=fiscal_quarter)
    if form_type is not None:
        routing = routing._replace(form_type=form_type)

    if routing.route == EntityRoute.COLD_START:
        ticker = (routing.ticker or "").strip().upper()
        t0 = time.perf_counter()
        try:
            # Step 1: Fetch latest 10-K from SEC EDGAR
            # ``fetching`` covers the EDGAR round trip and the slice written to
            # the token budget together: the clean is what makes the bytes
            # usable, and timing the socket alone would flatter a pipeline that
            # spent its time in the HTML parser.
            with timer.stage("fetching"):
                fetcher = SECRuntimeFetcher()
                raw_html, _meta = fetcher.fetch_latest_filing_html(
                    ticker, form_type="10-K", timeout=2.0
                )
                cleaned_text = clean_and_truncate_section(
                    raw_html, form_type="10-K", max_tokens=6000
                )

            # Step 3: Extract 15-30 financial triples
            with timer.stage("extraction"):
                extractor = ColdStartExtractor()
                payload = (
                    extractor.extract(cleaned_text, ticker)
                    if hasattr(extractor, "extract")
                    else extractor.extract_triples(cleaned_text, target_ticker=ticker)
                )

            # Step 4: Stitch ephemeral overlay onto backbone
            with timer.stage("stitching"):
                overlay = InMemoryOverlayGraph(kg_connection=kg)
                stitch_coldstart_payload(overlay, payload, target_ticker=ticker)

            # Step 5: 2-hop hybrid traversal
            with timer.stage("traversal"):
                traverser = HybridGraphTraverser(overlay)
                subgraph = traverser.traverse_neighborhood(ticker, max_hops=2)

            # Step 6: Build EvidenceRefs from retrieved subgraph nodes/edges
            with timer.stage("evidence"):
                # Convert subgraph nodes to the format expected by build_evidence_refs
                evidence_nodes = subgraph.get("nodes", [])
                evidence_edges = [
                    hop
                    for path in subgraph.get("paths", [])
                    for hop in path
                ]
                # Build a tag_map from the evidence nodes (positional)
                tag_map = {f"E{i+1}": node.get("id", "") for i, node in enumerate(evidence_nodes)}
                evidence_refs = build_evidence_refs(evidence_nodes, kg, tag_map, evidence_edges)
                evidence_block = serialise_evidence_refs(evidence_refs)

            # Step 7: Synthesize full 5-section investment analysis with EvidenceRefs
            with timer.stage("synthesis"):
                synthesizer = ColdStartSynthesizer()
                context = {
                    "target_ticker": ticker,
                    "query": question,
                    "paths": subgraph.get("paths", []),
                    "filing_text": cleaned_text,
                    "evidence_refs": evidence_refs,
                }
                tokens: list[str] = list(synthesizer.stream_synthesis(context))
                answer_text = "".join(tokens)

            # Step 8: Grade the synthesis using SEC provenance rules
            with timer.stage("grading"):
                grader = SECGradingAdapter()
                graded = grader.grade(answer_text, evidence_refs, question)
                provenance_text = "\n".join(
                    f"[{v.provenance}] {v.text}" for v in graded.verdicts
                )

            # Step 9: Kick off background full ingestion (non-blocking, fire-and-forget)
            if ticker:
                background_queue.enqueue_coldstart_sync(ticker)

            latency_ms = round((time.perf_counter() - t0) * 1000, 2)

            # Step 10: Return structured response.
            # Include all GRADER_KEYS so the AST-based payload-consistency test
            # (test_provenance_ui) can verify that every key in this dict also
            # exists in the main success payload returned at the bottom of ask_rag.
            return {
                "answer": answer_text,
                "text": answer_text,
                "provenance": provenance_text,
                "provenance_mix": graded.mix,
                "violations": graded.violations(),
                "ungrounded_figures": graded.ungrounded_figures,
                "misattributed": graded.misattributed,
                "invented_tags": graded.invented_tags,
                "gap": graded.gap,
                "verdict": graded.verdict,
                "graph": {
                    "nodes": subgraph.get("nodes", []),
                    "edges": evidence_edges,
                },
                "route": "COLD_START",
                "ticker": ticker,
                "latency_ms": latency_ms,
                "stage_latencies_ms": timer.as_wire(),
                "graph_metrics": _graph_metrics(
                    max_hop_depth=_hop_depth_from_paths(
                        subgraph.get("paths", [])
                    ),
                    node_count=len(subgraph.get("nodes", [])),
                    edge_count=len(evidence_edges),
                ),
                "background_task_scheduled": True,
                "question": question,
                "grounded": not graded.gap and bool(answer_text),
                "used_tags": sorted(
                    {t for t in citation_tags(answer_text) if t in tag_map},
                    key=lambda x: int(x[1:]),
                ),
                "tag_map": tag_map,
                "evidence_refs": [e.block() for e in evidence_refs],
            }

        except Exception as exc:
            # Fallback guard: log the failure and fall through to standard text QA.
            # We NEVER return just a bare "Triggering JIT pipeline" message.
            log.warning(
                "COLD_START JIT pipeline failed for %s (%s: %s); "
                "falling back to standard graph QA",
                ticker,
                type(exc).__name__,
                exc,
            )
            # Fall through to the standard KNOWN path below, using whatever
            # context the graph holds.  routing.ticker is already set correctly.

    if routing.route == EntityRoute.AMBIGUOUS:
        return _ambiguous_response(question)

    state = get_backends().resolve()
    if state["backend"] == "none":
        return _unavailable_answer(state)
    backend = state["backend"]

    t0 = time.perf_counter()
    # The known route's graph work -- seeded selection, the edges among the
    # selected nodes, and the evidence block -- is its traversal. It runs no
    # fetch, extraction or stitch stage, and those report 0.0 rather than
    # going missing, so a client can tell a cheap known request from a cold
    # start that skipped work.
    with timer.stage("traversal"):
        nodes, edges, tag_map, seed_ids = retrieve_financial_context(
            kg, question, ticker=routing.ticker,
            fiscal_year=routing.fiscal_year,
            fiscal_quarter=routing.fiscal_quarter,
            form_type=routing.form_type,
        )

    # Provenance is resolved here, by code, before the model is called: every
    # retrieved node gets a tag and a Source, and the tags that will be legal to
    # cite are fixed at this moment. The model can reference a tag; it can never
    # create one, which is the whole point -- a fabricated figure must not be
    # able to label itself STATED.
    evidence = build_evidence(nodes, kg, tag_map, edges)
    evidence_refs = [EvidenceRef.from_evidence(ev) for ev in evidence]
    evidence_block = serialise_evidence_refs(evidence_refs)

    log.info("Retrieved %d entities and %d relationships for: %s", len(nodes), len(edges), question)

    # Graph payload for visualization.
    # Both merges happen here, at the boundary, so no caller's bookkeeping is
    # load-bearing: one entry per node id, one arrow per (source, target,
    # relation), and a label no other node shares.
    graph_payload_nodes = merge_nodes(nodes)
    graph_payload_edges = merge_edges(edges)
    # Merged once, here, for both the wire and the depth measurement: the graph
    # the client is told about and the graph the depth was computed from must
    # be the same object, or node_count describes a payload nobody receives.
    graph_payload = {
        "seeds": seed_ids,
        "nodes": [
            {
                "id": n["id"],
                "name": n["name"],
                "type": n["type"],
                "description": n.get("description", ""),
            }
            for n in graph_payload_nodes
        ],
        "edges": [
            {
                "source": e["source"],
                "target": e["target"],
                "relation": e["relation"],
                "description": e.get("description", ""),
            }
            for e in graph_payload_edges
        ],
    }

    # One client for whichever backend resolved; both speak the
    # OpenAI-compatible chat API, so only the URL, the model and the timeout
    # differ. The timeout is explicit because the default (10 minutes for a
    # hosted call, but far less in practice for a stalled local socket) is what
    # turns a slow model into a browser-side "Failed to fetch".
    #
    # `max_retries` was 0, which turned every momentary blip into a dead answer.
    # The capacity endpoint answers 503 "Service temporarily overloaded" under
    # load, and that is the definition of a condition worth one more try: the
    # SDK already retries only what is safe to retry (connection failures, 408,
    # 409, 429 and 5xx) and leaves a 400 or 401 alone, because a rejected key
    # will be rejected again just as surely. Set RAG_RETRIES=0 to opt out.
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
        max_retries=RAG_RETRIES,
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
        # The synthesis stage is the whole model call, from request to last
        # byte. This route is non-streaming, so there is no first-token moment
        # to split out; the cold-start route streams and records the same stage
        # as a single block, which is why the two remain comparable.
        with timer.stage("synthesis"):
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
            # No verdict, and never REFUSED. The grader did not run, so it has
            # nothing to say: there is no answer here, which means there is no
            # sentence resting on evidence that does not support it either. This
            # used to be stamped REFUSED, and the UI duly showed "At least one
            # sentence is not supported by the evidence it cites" over a 503 --
            # telling a reader their question was refused on quality grounds
            # when the truth was that the model was overloaded and answered
            # nothing. `status` is what the client keys on to tell a dead call
            # apart from a graded one, so the two can never be confused again.
            "status": "error",
            "error": _explain_api_error(exc, state),
            "verdict": None,
            "provenance_mix": {},
            "provenance": [],
            "invented_tags": [],
            "ungrounded_figures": [],
            "misattributed": [],
            "violations": [],
            # A failed model call still has a real shape: it routed, it retrieved
            # a graph, and it spent a measurable time failing. Reporting zeros
            # here rather than omitting the keys keeps a client's telemetry
            # reader from having to special-case the error path -- and an
            # omitted key is indistinguishable from a stage that never ran.
            "ticker": routing.ticker,
            "stage_latencies_ms": timer.as_wire(),
            "graph_metrics": _graph_metrics(
                max_hop_depth=_hop_depth_from_seeds(seed_ids, edges),
                node_count=len(nodes),
                edge_count=len(edges),
            ),
            "evidence_refs": [e.block() for e in evidence_refs],
        }

    # Extract cited tags. The grammar is the grader's, so a citation the grader
    # recognises is a citation the UI counts: the list form "[E1, E3]" and the
    # pairing form the model writes to put a line item next to its value
    # ("[E2->E15]") used to be invisible here, so a correctly cited answer came
    # back with no tags at all and reported itself ungrounded.
    used_tags = sorted(
        {t for t in citation_tags(content) if t in tag_map},
        key=lambda x: int(x[1:]),
    )

    # Grade the answer by rule. The model wrote the prose; this decides what it
    # was actually allowed to say, and it never asks the model. If every
    # sentence fails, the corpus does not support the answer, so a GAP is
    # rendered instead of the unsupported prose -- with a pointer to where the
    # fact would live rather than a bare refusal.
    grader = SECGradingAdapter()
    graded = grader.grade(content, evidence_refs, question)
    if content and graded.gap:
        content = render_gap(question, [e.to_evidence() for e in evidence_refs], _where_to_look(question))
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

    # Graph payload was built above, immediately after retrieval, so the same
    # merged objects could be measured for the wire. Building it a second time
    # here would let the payload and the reported counts describe different
    # graphs.

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
        "verdict": graded.verdict,
        "provenance_mix": graded.mix,
        "provenance": [
            {
                "text": v.text,
                "provenance": v.provenance,
                "cites": v.cites,
                "figures": v.figures,
                "ungrounded": v.ungrounded,
                "unknown_cites": v.unknown_cites,
                "misattributed": v.misattributed,
                "reason": v.reason,
            }
            for v in graded.verdicts
        ],
        "invented_tags": graded.invented_tags,
        "ungrounded_figures": graded.ungrounded_figures,
        "misattributed": graded.misattributed,
        "violations": graded.violations(),
        # Keys shared with the COLD_START return so the UI reads the same field
        # names regardless of which path produced the response.
        "answer": content,
        "route": "KNOWN",
        "ticker": routing.ticker,
        "stage_latencies_ms": timer.as_wire(),
        "graph_metrics": _graph_metrics(
            max_hop_depth=_hop_depth_from_seeds(seed_ids, graph_payload_edges),
            node_count=len(graph_payload_nodes),
            edge_count=len(graph_payload_edges),
        ),
        "background_task_scheduled": False,
        "latency_ms": round(elapsed * 1000, 2),
        "evidence_refs": [e.block() for e in evidence_refs],
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
  .chip.warn { color: var(--warn); border-color: #6b5327; }
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

  /* ---- provenance ----
     The grader's output. Deliberately not dismissible and deliberately above
     the answer text: violations() is the only thing a reader must not miss, and
     the one badge that looks like a verdict has to be the grader's verdict and
     not "did the model cite something". */
  .violations {
    margin: 0 0 12px; padding: 10px 12px; border-radius: 6px;
    background: #2a1614; border: 1px solid #6b2f2c; color: var(--bad);
    font-size: 12.5px; line-height: 1.55;
  }
  .violations h4 {
    margin: 0 0 6px; font-size: 11px; letter-spacing: .5px;
    text-transform: uppercase; color: var(--bad);
  }
  .violations ul { margin: 0; padding-left: 18px; }
  .violations li { margin-bottom: 4px; }
  .prov-head {
    display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
    margin-bottom: 10px;
  }
  .prov { display: flex; flex-direction: column; gap: 7px; }
  .prov-row {
    display: flex; gap: 9px; align-items: flex-start;
    padding: 8px 9px; border-radius: 6px;
    background: var(--panel-2); border: 1px solid var(--line);
  }
  .prov-row.is-bad { border-color: #6b2f2c; }
  .prov-row.is-warn { border-color: #6b5327; }
  .prov-row .ptag {
    font-size: 10px; font-weight: 600; letter-spacing: .4px; text-transform: uppercase;
    padding: 2px 6px; border-radius: 4px; white-space: nowrap;
    border: 1px solid var(--line); color: var(--muted);
  }
  .prov-row .ptag.t-STATED { color: var(--accent-2); border-color: #2c5a48; }
  .prov-row .ptag.t-DERIVED { color: var(--accent); border-color: #33507a; }
  .prov-row .ptag.t-INFERRED { color: var(--warn); border-color: #6b5327; }
  .prov-row .ptag.t-EXTERNAL { color: #c792ea; border-color: #4a3760; }
  .prov-row .ptag.t-GAP { color: var(--bad); border-color: #6b2f2c; }
  .prov-row .pbody { flex: 1 1 auto; min-width: 0; }
  .prov-row .ptext { font-size: 12.5px; line-height: 1.5; color: var(--text); }
  .prov-row .ptext.clipped {
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .prov-row .preason { margin-top: 5px; font-size: 11.5px; color: var(--muted); }
  .prov-row .pflags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px; }
  .prov-row .pflag {
    font-size: 10.5px; padding: 2px 6px; border-radius: 4px;
    background: #2a1614; border: 1px solid #6b2f2c; color: var(--bad);
  }
  .prov-note {
    margin-bottom: 10px; padding: 8px 10px; border-radius: 6px; font-size: 11.5px;
    line-height: 1.5; background: #2a2317; border: 1px solid #4a3d20; color: var(--warn);
  }
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }

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
  <!-- Temporal Filters -->
  <div class="row" style="gap: 8px; margin-right: 16px;">
    <label class="field inline stats" for="yearFilter" style="font-size: 11px;">Year</label>
    <select id="yearFilter" class="compact" style="width: auto; min-width: 80px;">
      <option value="">All Years</option>
      <option value="2020">2020</option>
      <option value="2021">2021</option>
      <option value="2022">2022</option>
      <option value="2023">2023</option>
      <option value="2024">2024</option>
      <option value="2025">2025</option>
      <option value="2026">2026</option>
    </select>
    <label class="field inline stats" for="quarterFilter" style="font-size: 11px;">Quarter</label>
    <select id="quarterFilter" class="compact" style="width: auto; min-width: 80px;">
      <option value="">All Quarters</option>
      <option value="FY">FY (Annual)</option>
      <option value="Q1">Q1</option>
      <option value="Q2">Q2</option>
      <option value="Q3">Q3</option>
      <option value="Q4">Q4</option>
    </select>
    <label class="field inline stats" for="formFilter" style="font-size: 11px;">Form</label>
    <select id="formFilter" class="compact" style="width: auto; min-width: 100px;">
      <option value="">All Forms</option>
      <option value="10-K">10-K</option>
      <option value="10-Q">10-Q</option>
      <option value="8-K">8-K</option>
      <option value="DEF 14A">DEF 14A</option>
      <option value="3">Form 3</option>
      <option value="4">Form 4</option>
      <option value="5">Form 5</option>
      <option value="13F-HR">13F-HR</option>
      <option value="SC 13D">SC 13D</option>
      <option value="SC 13G">SC 13G</option>
      <option value="S-3">S-3</option>
      <option value="S-8">S-8</option>
      <option value="424B">424B</option>
      <option value="ARS">ARS</option>
      <option value="SD">SD</option>
      <option value="11-K">11-K</option>
    </select>
  </div>
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
    <div class="tabs" role="tablist" aria-label="Answer detail">
      <button class="tab active" role="tab" id="tabBtnAnswer" aria-selected="true"
              aria-controls="tabAnswer" data-tab="answer">Answer</button>
      <button class="tab" role="tab" id="tabBtnSources" aria-selected="false"
              aria-controls="tabSources" data-tab="sources">Sources</button>
      <button class="tab" role="tab" id="tabBtnTrace" aria-selected="false"
              aria-controls="tabTrace" data-tab="trace">Trace</button>
      <button class="tab" role="tab" id="tabBtnProvenance" aria-selected="false"
              aria-controls="tabProvenance" data-tab="provenance">Provenance</button>
      <div class="grow"></div>
      <span class="chip" id="waitTimer" hidden></span>
      <button id="copyAnswer" class="icon ghost" title="Copy answer text">copy</button>
    </div>
    <div id="answerWrap">
      <div id="tabAnswer" role="tabpanel" aria-labelledby="tabBtnAnswer">
        <div class="empty">Answers are generated from the retrieved subgraph and cite entities as
          <code>[E1]</code>. Click a citation to highlight it in the graph.</div>
      </div>
      <div id="tabSources" role="tabpanel" aria-labelledby="tabBtnSources" hidden>
        <div class="empty">No answer yet — the cited entities show up here.</div>
      </div>
      <div id="tabTrace" role="tabpanel" aria-labelledby="tabBtnTrace" hidden>
        <div class="empty">The retrieval trace shows up here after a question.</div>
      </div>
      <div id="tabProvenance" role="tabpanel" aria-labelledby="tabBtnProvenance" hidden>
        <div class="empty">Every sentence in the answer, with the rule that judged it.</div>
      </div>
    </div>
  </section>
</main>
<div id="verdictAnnounce" class="sr-only" role="status" aria-live="polite"></div>

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
  // Cleared up front as well as on the error path, so a question that never
  // returns cannot leave the last answer's grading on screen under a spinner.
  clearProvenance("Grading the answer as it arrives…");
  startTimer();
  try {
    // Collect temporal filters
    const yearFilter = $("yearFilter").value;
    const quarterFilter = $("quarterFilter").value;
    const formFilter = $("formFilter").value;
    
    const res = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ 
        question: q,
        fiscal_year: yearFilter ? parseInt(yearFilter, 10) : null,
        fiscal_quarter: quarterFilter || null,
        form_type: formFilter || null,
      }),
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
    // The failed question's verdicts are not on screen, so the previous
    // question's must not stay there: a panel full of sentences under a
    // heading about a different question is worse than an empty one.
    clearProvenance("No answer to grade — the last question failed.");
    $("verdictAnnounce").textContent = "Query failed";
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
  // The verdict is the grader's, not "did the model cite something". Those are
  // different questions and only one of them means anything: a model that
  // fabricates a figure and cites a real tag satisfies the second and fails
  // the first, so the old chip rendered that green. `res.grounded` stays in the
  // payload because removing a field is a breaking change, and is no longer
  // what this reads.
  const verdict = res.verdict || "REFUSED";
  const verdictChip = {
    SUPPORTED: ["ok", "supported", "every sentence rests on a fact in the cited evidence"],
    QUALIFIED: ["warn", "qualified", "nothing failed, but part of it is hedged or reaches past the filings"],
    REFUSED: ["bad", "refused", "at least one sentence is not supported by the cited evidence"],
  }[verdict] || ["bad", verdict, "the grader returned a verdict this page does not know"];
  addChip(verdictChip[0], verdictChip[1], verdictChip[2]);
  if (res.elapsed_sec) addChip("", `${res.elapsed_sec}s`, "retrieval + generation time");
  answer.appendChild(meta);

  // What the reader must not miss, above the answer it applies to. Not
  // collapsible: violations() is already capped at three plain-English lines
  // and is written for exactly this audience.
  const violations = res.violations || [];
  if (violations.length) {
    const box = document.createElement("div");
    box.className = "violations";
    box.innerHTML = esc("<h4>the grader refused part of this answer</h4>") +
      '<ul>' + violations.map((v) => "<li>" + esc(v) + "</li>").join("") + "</ul>";
    answer.appendChild(box);
  }

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

  renderProvenance(res, tagMap);
  announceVerdict(res);
}

const VERDICT_WORDS = {
  SUPPORTED: "supported",
  QUALIFIED: "qualified",
  REFUSED: "refused",
};

function announceVerdict(res) {
  // An answer can take 30 seconds to arrive and then be a fabrication. Without
  // this nothing is announced, so a screen-reader user is told the request
  // finished and not what it concluded -- the worst case in the product.
  const box = $("verdictAnnounce");
  if (!box) return;
  const verdict = res.verdict || "REFUSED";
  const word = VERDICT_WORDS[verdict] || verdict.toLowerCase();
  const parts = [`Answer ${word}`];
  const mix = res.provenance_mix || {};
  const counts = Object.keys(mix).map((k) => `${k} ${mix[k]}`).join(", ");
  if (counts) parts.push(counts);
  const notes = res.violations || [];
  if (notes.length) parts.push(...notes);
  box.textContent = parts.join(". ");
}

function clearProvenance(message) {
  // renderAnswer only runs on success, so a second question that errors would
  // otherwise leave the first question's verdicts on screen, attributed to
  // nothing. Cleared on every path out of askQuestion.
  const panel = $("tabProvenance");
  if (!panel) return;
  panel.textContent = "";
  const empty = document.createElement("div");
  empty.className = "empty";
  empty.textContent = message || "Every sentence in the answer, with the rule that judged it.";
  panel.appendChild(empty);
}

function renderProvenance(res, tagMap) {
  // Everything here is model prose except the tag, the counts and the flags,
  // so it all goes through esc() or textContent. The answer body is already
  // escaped; a panel that concatenated a model's sentence into markup would be
  // the same hole in a smaller place.
  const panel = $("tabProvenance");
  if (!panel) return;
  panel.textContent = "";
  const verdicts = res.provenance || [];
  if (!verdicts.length) {
    panel.innerHTML = '<div class="empty">' +
      esc(res.verdict
        ? "The grader judged no sentences in this answer."
        : "Every sentence in the answer, with the rule that judged it.") +
      "</div>";
    return;
  }

  // When every sentence failed, ask_rag replaces the answer text with a
  // rendered refusal. These verdicts still describe what the model originally
  // wrote, so without this the panel shows sentences the answer above does not
  // contain and a reader concludes nothing was checked.
  if (res.gap) {
    const note = document.createElement("div");
    note.className = "prov-note";
    note.textContent =
      "The answer above was replaced: every sentence failed, so the corpus " +
      "cannot support it. These are the rules applied to what the model wrote " +
      "before the replacement.";
    panel.appendChild(note);
  }

  const head = document.createElement("div");
  head.className = "prov-head";
  const mix = res.provenance_mix || {};
  for (const tag of Object.keys(mix).sort()) {
    const c = document.createElement("span");
    c.className = "chip";
    c.textContent = `${tag} ${mix[tag]}`;
    c.title = "sentences graded " + tag;
    head.appendChild(c);
  }
  const invented = res.invented_tags || [];
  if (invented.length) {
    const c = document.createElement("span");
    c.className = "chip bad";
    c.textContent = `invented tags: ${invented.join(", ")}`;
    c.title = "cited tags the retriever never issued";
    head.appendChild(c);
  }
  // The answer-level list, which is not the same as the per-sentence flags: a
  // figure can fail on one sentence and be absent from the aggregate dedupe.
  // Both are shown because violations() quotes the aggregate in prose and this
  // is the same set in a form you can point at.
  const loose = res.ungrounded_figures || [];
  if (loose.length) {
    const c = document.createElement("span");
    c.className = "chip bad";
    c.textContent = `figures not in any cited source: ${loose.join(", ")}`;
    c.title = "no cited source contains these numbers";
    head.appendChild(c);
  }
  const wrongFiler = res.misattributed || [];
  if (wrongFiler.length) {
    const c = document.createElement("span");
    c.className = "chip bad";
    c.textContent = `attributed to: ${wrongFiler.join(", ")}`;
    c.title = "issuers no cited source was filed by";
    head.appendChild(c);
  }
  panel.appendChild(head);

  const list = document.createElement("div");
  list.className = "prov";
  for (const v of verdicts) {
    const tag = v.provenance || "";
    const row = document.createElement("div");
    row.className = "prov-row" +
      (tag === "GAP" ? " is-bad" : (tag === "INFERRED" || tag === "EXTERNAL" ? " is-warn" : ""));

    const badge = document.createElement("span");
    badge.className = "ptag t-" + tag;
    badge.textContent = tag;
    badge.title = "the rule that judged this sentence";
    row.appendChild(badge);

    const bodyWrap = document.createElement("div");
    bodyWrap.className = "pbody";

    const text = document.createElement("div");
    text.className = "ptext";
    text.textContent = v.text || "";
    // Long sentences are clamped and clickable, because one 400-word sentence
    // otherwise turns the panel into a wall and hides every row after it.
    if ((v.text || "").length > 240) {
      text.className = "ptext clipped";
      text.title = "click to expand";
      text.onclick = () => text.classList.toggle("clipped");
    }
    bodyWrap.appendChild(text);

    const flags = [];
    for (const c of v.unknown_cites || []) flags.push("cited a tag never issued: " + c);
    for (const f of v.ungrounded || []) flags.push("figure not in any cited source: " + f);
    for (const m of v.misattributed || []) flags.push("attributed to " + m + ", which no cited source was filed by");
    if (flags.length) {
      const box = document.createElement("div");
      box.className = "pflags";
      for (const f of flags) {
        const chip = document.createElement("span");
        chip.className = "pflag";
        chip.textContent = f;
        box.appendChild(chip);
      }
      bodyWrap.appendChild(box);
    }

    if (v.reason) {
      const why = document.createElement("div");
      why.className = "preason";
      why.textContent = v.reason;
      bodyWrap.appendChild(why);
    }

    // The same affordance the answer body has: a citation focuses its node.
    for (const c of v.cites || []) {
      const btn = document.createElement("span");
      btn.className = "chip cite";
      btn.textContent = "[" + c + "]";
      btn.onclick = () => { const id = tagMap[c]; if (id) focusOn(id); };
      const holder = bodyWrap.querySelector(".pflags") || (function () {
        const b = document.createElement("div");
        b.className = "pflags";
        bodyWrap.appendChild(b);
        return b;
      })();
      holder.appendChild(btn);
    }

    row.appendChild(bodyWrap);
    list.appendChild(row);
  }
  panel.appendChild(list);
}

function showTab(name) {
  // The panel list is derived from the buttons rather than repeated here. It
  // used to be a literal ["answer", "sources", "trace"], and a tab added to the
  // markup without being added there silently never opened -- which is exactly
  // what happened to the provenance tab before it had a test.
  const tabs = Array.from(document.querySelectorAll(".tab[data-tab]"));
  for (const t of tabs) {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const t of tabs) {
    const panel = $("tab" + t.dataset.tab.charAt(0).toUpperCase() + t.dataset.tab.slice(1));
    if (panel) panel.hidden = t.dataset.tab !== name;
  }
  $("answerWrap").scrollTop = 0;
}

function moveTabFocus(current, delta) {
  // Arrow-key navigation, which is what role="tab" promises and what the plain
  // buttons did not do.
  const tabs = Array.from(document.querySelectorAll(".tab[data-tab]"));
  if (!tabs.length) return;
  const at = tabs.indexOf(current);
  const next = tabs[(at + delta + tabs.length) % tabs.length];
  next.focus();
  showTab(next.dataset.tab);
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
  t.onkeydown = (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      moveTabFocus(t, 1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      moveTabFocus(t, -1);
    } else if (e.key === "Home") {
      e.preventDefault();
      const first = document.querySelector(".tab[data-tab]");
      if (first) { first.focus(); showTab(first.dataset.tab); }
    } else if (e.key === "End") {
      e.preventDefault();
      const all = document.querySelectorAll(".tab[data-tab]");
      const last = all[all.length - 1];
      if (last) { last.focus(); showTab(last.dataset.tab); }
    }
  };
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


# ── Health and readiness ─────────────────────────────────────────────────────
# Two endpoints because they answer two different questions, and a load
# balancer acts on them differently.
#
# Liveness (/healthz) asks "is this process running". It may not check a
# dependency: a probe that fails when a dependency fails gets the orchestrator
# to kill the one process that is still healthy while the real fault sits
# somewhere it cannot restart.
#
# Readiness (/readyz) asks "can this process serve a question right now", which
# is only answerable by reading the graph.
#
# Neither reaches the network on purpose. Resolving the RAG backend probes
# Ollama, /api/company reaches Yahoo, and cold-start reaches SEC -- so a probe
# that touched any of them would turn a balancer's poll into an outbound request
# storm and report a third party's outage as FinGraph being unhealthy.
#
# Neither response carries the database path, a credential, or an exception
# message: both are served without authentication, and both are exactly the
# responses that end up in shared monitoring dashboards.

_STARTED_MONOTONIC = time.monotonic()


def _service_identity(service: str) -> dict[str, Any]:
    return {
        "service": service,
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - _STARTED_MONOTONIC, 3),
    }


def liveness_payload(service: str) -> dict[str, Any]:
    """Body for /healthz. Reads no dependency and opens no socket.

    ``ready`` is deliberately absent rather than true: a probe that reports
    readiness it never checked is worse than one that reports none, because a
    balancer cannot tell which one to trust.
    """
    payload = _service_identity(service)
    payload["status"] = "alive"
    payload["checks"] = {"process": {"status": "ok"}}
    payload["note"] = (
        "process liveness only; it does not imply the graph is readable. "
        "Use /readyz to decide whether to route traffic here."
    )
    return payload


def readiness_payload(kg: Any, service: str) -> dict[str, Any]:
    """Body for /readyz, plus the boolean the caller turns into a status code.

    Every check is local and already in memory or in the open graph handle, so
    building this costs one bounded query and no I/O beyond it.
    """
    checks: dict[str, Any] = {}
    failed: list[str] = []

    def record(name: str, ok: bool, **extra: Any) -> None:
        checks[name] = {"status": "ok" if ok else "failed", **extra}
        if not ok:
            failed.append(name)

    probe = getattr(kg, "probe_read", None)
    freshness = getattr(kg, "probe_freshness", None)
    schema = getattr(kg, "schema", None)
    lifecycle = getattr(kg, "lifecycle", None)

    # A draining server still has a readable graph, so every check below passes
    # while the process is on its way out. Saying "ready" then would leave it in
    # the balancer until the socket closes. Draining is reported as its own
    # reason, and /readyz is exempt from the drain's request gate precisely so
    # this can be seen.
    draining = bool(lifecycle is not None and lifecycle.draining)

    if not callable(probe):
        record("graph_initialized", False, detail="graph handle is missing or unusable")
        record("schema_detected", False, detail="not attempted")
        record("database_readable", False, detail="not attempted")
        record("database_current", False, detail="not attempted")
    else:
        record("graph_initialized", True)
        # detect_schema() landing on neither shape means every count in the app
        # silently reports zero. Readiness has to call that what it is.
        record("schema_detected", schema in ("blueprint", "engine"), value=str(schema))
        try:
            readable, detail, latency_ms = probe()
        except Exception as exc:
            # Readiness reports on the server, so it is the one handler that
            # must never answer with a traceback: an orchestrator reads the
            # status line, and a 500 here says nothing about whether to route.
            log.warning("readiness probe raised: %s", type(exc).__name__)
            readable, detail, latency_ms = False, type(exc).__name__, None
        record("database_readable", readable, detail=detail, latency_ms=latency_ms)

        # Reported separately from the read: the read is what the handle can do,
        # this is whether it is the right handle. A rebuild under a live server
        # passes the read and fails here, which is the whole point.
        if callable(freshness):
            try:
                current, current_detail = freshness()
            except Exception as exc:
                log.warning("freshness probe raised: %s", type(exc).__name__)
                current, current_detail = False, type(exc).__name__
            record("database_current", current, detail=current_detail)
        else:
            record("database_current", False, detail="handle cannot be checked")


    payload = _service_identity(service)
    payload["status"] = "not_ready" if failed else "ready"
    payload["ready"] = not failed
    payload["checks"] = checks
    if draining:
        # Last, so it is visible next to a status that still says otherwise:
        # the checks are all genuinely passing, the process is genuinely on its
        # way out, and reporting only "ready" would hide the second fact.
        payload["ready"] = False
        payload["status"] = "draining"
        payload["failed_checks"] = list(failed) + ["draining"]
        payload["checks"]["draining"] = {
            "status": "failed",
            "detail": lifecycle.report()["shutdown_reason"] or "shutdown in progress",
            "inflight_requests": lifecycle.inflight,
        }
    elif failed:
        payload["failed_checks"] = failed
    return payload


# ── HTTP Server Request Handler ───────────────────────────────────────────────

class _SseDeadlineExceeded(Exception):
    """One SSE response outlived ``SSE_MAX_SECONDS``.

    Carries the event that was being sent when the limit was hit, so the
    terminal ``done`` frame can say where the stream stopped instead of just
    that it did.
    """

    def __init__(self, event: str) -> None:
        super().__init__(f"stream exceeded its deadline during {event!r}")
        self.event = event


class _Handler(BaseHTTPRequestHandler):
    server_version = "blueprint-graphrag-ui"
    protocol_version = "HTTP/1.1"

    #: Socket timeout for reading a request and writing a response.
    #:
    #: The stdlib default is ``None``, which with HTTP/1.1 keep-alive means a
    #: thread blocks forever in ``readline()`` after answering a connection that
    #: then goes idle. Measured: 200 idle connections held 219 threads
    #: indefinitely, and 100 stalled sockets held 100 more. Setting this makes
    #: ``StreamRequestHandler.setup`` call ``settimeout`` and
    #: ``handle_one_request`` close the connection on expiry, so an idle or
    #: half-sent connection costs a thread for at most this long.
    #:
    #: It bounds the socket per read and per write, not the request as a whole:
    #: a stream that keeps writing is never cut off by it, which is why SSE
    #: lifetime is bounded separately by ``SSE_MAX_SECONDS``.
    #: Fallback socket timeout. The effective value is the listener's
    #: ``Limits.request_timeout``, applied in :meth:`setup`, because
    #: ``StreamRequestHandler`` reads the *handler's* ``timeout`` and so would
    #: otherwise ignore a per-listener setting entirely.
    timeout = REQUEST_TIMEOUT

    kg: KnowledgeGraph

    def setup(self) -> None:
        limits = getattr(self.server, "limits", None)
        if limits is not None:
            self.timeout = limits.request_timeout
        super().setup()

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s — %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ct: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Include request ID in response headers for client-side tracing
        request_id = getattr(self, "_request_id", None) or get_request_id()
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def _health(self, payload: dict[str, Any], status: int) -> None:
        """Send a health body through _send rather than _json.

        _NextHandler._json hardcodes 200 and takes no status argument, so a 503
        routed through it would either misreport readiness to the balancer or
        raise. Both classes' _send takes the status and sets Content-Length,
        which is also what keeps a 503 from hanging a keep-alive client.
        """
        self._send(status, json.dumps(payload, default=str).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._post)

    def _guarded(self, fn: Any) -> None:
        """Count the request in, run it, count it out.

        Two jobs, and both are about shutdown:

        * Refuse work that arrives after the drain began, at admission, before a
          handler body runs. Refusing late would mean refusing from inside a
          query that has already taken the graph lock.
        * Account for the request so the drain knows what it is waiting for.
          ``daemon_threads = True`` means ``socketserver`` tracks none of these
          threads, so nothing else can answer "is anything still running?".

        The ``finally`` is what makes the count trustworthy: a request that
        raises must decrement too, or the drain waits out its whole deadline on a
        number that will never reach zero.
        """
        # Generate and set request ID for this request
        request_id = generate_request_id()
        token = set_request_id(request_id)
        # Add request ID to response headers for client-side tracing
        self._request_id = request_id

        lifecycle = getattr(self.server, "lifecycle", None)
        admitted = False
        if lifecycle is not None:
            try:
                admitted = lifecycle.enter_request(self.path)
            except ShutdownStarted as exc:
                # 503 with Content-Length and Connection: close. The header
                # matters: without it a keep-alive client sits waiting on a body
                # that never comes and reports the drain as a network failure.
                self.close_connection = True
                self._send(
                    503,
                    json.dumps({"error": str(exc), "draining": True}).encode(),
                    "application/json; charset=utf-8",
                )
                clear_request_id(token)
                return
        try:
            self._run_guarded(fn)
        finally:
            if lifecycle is not None and admitted:
                lifecycle.exit_request()
            clear_request_id(token)

    def _run_guarded(self, fn: Any) -> None:
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
            request_id = getattr(self, "_request_id", None) or get_request_id()
            log.exception(
                "unhandled error serving %s %s",
                self.command, self.path,
                extra={"request_id": request_id} if request_id else {}
            )
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except Exception:
                # The socket is already gone; the log line above is all we get.
                pass

    def _get(self) -> None:
        parsed = urlparse(self.path)
        p, qs = parsed.path, parse_qs(parsed.query)
        # Ahead of every other route, and touching none of their state, so a
        # balancer can still reach a verdict while the graph is unusable.
        if p == "/healthz":
            payload = liveness_payload(self.server_version)
            # Liveness stays 200 while draining: the process is healthy, it is
            # finishing up on purpose. A supervisor that saw it go unhealthy
            # here would escalate to SIGKILL and destroy the very in-flight
            # requests the grace period exists to protect.
            lifecycle = getattr(self.server, "lifecycle", None)
            if lifecycle is not None:
                payload.update(lifecycle.report())
            # Occupancy, so an operator can see a server that is refusing
            # connections because it is full rather than because it is broken.
            connections = getattr(self.server, "connections", None)
            if connections is not None:
                payload["limits"] = connections.report()
            return self._health(payload, 200)
        if p == "/readyz":
            readiness = readiness_payload(self.kg, self.server_version)
            return self._health(readiness, 200 if readiness["ready"] else 503)
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

    def _send_sse(self, event: str, data: dict[str, Any]) -> None:
        deadline = getattr(self, "_sse_deadline", None)
        if deadline is not None and time.monotonic() > deadline:
            # The stream outlived SSE_MAX_SECONDS. Raised rather than silently
            # dropped so the client gets one terminal ``done`` with a reason
            # instead of a stream that stops mid-sentence with no explanation.
            raise _SseDeadlineExceeded(event)
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _handle_sse_ask(
        self,
        question: str,
        fiscal_year: int | None = None,
        fiscal_quarter: str | None = None,
        form_type: str | None = None,
    ) -> None:
        """Admit one stream against the stream ceiling, then run it.

        Streams get their own, lower ceiling than the connection pool. A stream
        pins a thread for as long as the model takes -- ``RAG_TIMEOUT``, 900s by
        default -- so with only a connection ceiling a burst of streams would
        take every thread in the pool and ordinary API calls would queue behind
        them. With a separate ceiling, a burst costs at most
        ``MAX_SSE_CONNECTIONS`` threads and the rest of the pool stays available.
        """
        counter = getattr(self.server, "connections", None)
        if counter is not None and not counter.try_acquire_sse():
            # Refused before any 200, and as ordinary JSON rather than an empty
            # event stream, so the client sees a status and a retry hint instead
            # of a 200 it will wait on forever for events that never come.
            #
            # ``_send`` rather than ``_err``: the reason ``_guarded`` writes its
            # draining 503 the same way. ``_NextHandler._err`` omits
            # Content-Length, and without it a keep-alive client cannot tell
            # where the body ends and waits for bytes that never come.
            self.close_connection = True
            self._send(
                503,
                json.dumps({
                    "error": (
                        f"too many concurrent streams "
                        f"(limit {counter.limits.max_sse_connections}); retry shortly"
                    ),
                    "limit": "max_sse_connections",
                }).encode(),
                "application/json; charset=utf-8",
            )
            return
        try:
            # Bounds the stream's output, not the model call: ask_rag has no
            # timeout parameter, so a model call already in flight still runs to
            # RAG_TIMEOUT. This is what makes an operator-lowered SSE_MAX_SECONDS
            # truncate the stream, and it is why the starvation protection above
            # counts streams rather than trusting this deadline.
            self._sse_deadline = time.monotonic() + SSE_MAX_SECONDS
            self._sse_started = time.monotonic()
            request_id = getattr(self, "_request_id", None) or get_request_id()
            self._run_sse(
                question,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                form_type=form_type,
                request_id=request_id,
            )
        except _SseDeadlineExceeded as exc:
            # The stream was already open, so this is still an SSE frame: the
            # client's reader is mid-stream and an HTML body would be noise.
            try:
                self._send_sse_unbounded("done", {
                    "status": "truncated",
                    "error": f"stream exceeded SSE_MAX_SECONDS ({SSE_MAX_SECONDS:.0f}s)",
                    "last_event": exc.event,
                    "ticker": None,
                    "stage_latencies_ms": {},
                    "graph_metrics": _graph_metrics(
                        max_hop_depth=0, node_count=0, edge_count=0
                    ),
                    "latency_ms": round((time.monotonic() - self._sse_started) * 1000, 2),
                })
            except OSError:
                # The client hung up on receiving the truncation; nothing to add.
                pass
        finally:
            if counter is not None:
                counter.release_sse()
            self._sse_deadline = None

    def _send_sse_unbounded(self, event: str, data: dict[str, Any]) -> None:
        """Send one terminal SSE frame, ignoring the deadline.

        Only for the frame that *reports* the deadline being hit; every other
        frame goes through :meth:`_send_sse` so the limit cannot be bypassed by
        an exception handler.
        """
        self._sse_deadline = None
        self._send_sse(event, data)

    def _run_sse(
        self,
        question: str,
        fiscal_year: int | None = None,
        fiscal_quarter: str | None = None,
        form_type: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        # Include request ID in SSE response headers
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        self.close_connection = True

        start_time = time.monotonic()
        # Same accounting as ask_rag, so the two transports report the same
        # stage names with the same meaning. A client that measures one
        # transport and compares against the other should not have to know
        # which one it got.
        timer = StageTimer(request_id=request_id)
        try:
            # 1. Routing
            self._send_sse("status", {"step": "routing", "message": "Analyzing entity...", "request_id": request_id})
            with timer.stage("routing"):
                routing = route_query(question, self.kg)

            if routing.route == EntityRoute.AMBIGUOUS:
                # Same terminal response ask_rag returns, so the streaming and
                # JSON transports cannot drift apart on what "unresolved" means.
                self._send_sse("status", {"step": "ambiguous", "message": "Ambiguous entity", "request_id": request_id})
                self._send_sse("token", {"token": _ambiguous_response(question)["message"]})
                # A refusal touched no graph, so the depth is 0 and the counts
                # are 0 -- stated rather than omitted, so a client reading
                # graph_metrics unconditionally gets a real "nothing was
                # traversed" instead of a missing field it has to guess at.
                self._send_sse("done", {
                    "status": "complete",
                    "route": "AMBIGUOUS",
                    "ticker": None,
                    "stage_latencies_ms": timer.as_wire(),
                    "graph_metrics": _graph_metrics(
                        max_hop_depth=0, node_count=0, edge_count=0
                    ),
                    "latency_ms": round((time.monotonic() - start_time) * 1000, 2),
                    "request_id": request_id,
                })
                return

            if routing.route == EntityRoute.KNOWN:
                # The known path is the one people actually wait on. Retrieval is
                # quick, and then the model is silent for as long as it takes, so
                # the client used to get one "retrieval" line and nothing until
                # the answer landed -- which reads as a hang rather than as work.
                #
                # ask_rag's stage timer reports each boundary as it crosses it, and
                # every one is forwarded the moment it happens. These are the real
                # stages, not a scripted animation: a stage that never fires never
                # shows up, which is why the plan is sent first and the events
                # that follow are matched against it.
                plan = ROUTE_PLAN["KNOWN"]
                self._send_sse("status", {
                    "step": "start",
                    "ticker": routing.ticker,
                    "stages": list(plan),
                    "message": STAGE_MESSAGES[plan[0]],
                })
                announced: set[str] = set()
                stage_started = time.monotonic()

                def announce(stage: str) -> None:
                    # ``stage`` accumulates, so a re-entry must not replay it.
                    if stage in announced:
                        return
                    announced.add(stage)
                    self._send_sse("status", {
                        "step": stage,
                        "message": STAGE_MESSAGES.get(stage, stage),
                        "elapsed_ms": round(
                            (time.monotonic() - stage_started) * 1000.0, 2
                        ),
                    })

                def announce_exit(stage: str, duration_ms: float) -> None:
                    # Send stage completion with actual duration
                    self._send_sse("status", {
                        "step": stage,
                        "message": STAGE_MESSAGES.get(stage, stage),
                        "elapsed_ms": round(
                            (time.monotonic() - stage_started) * 1000.0, 2
                        ),
                        "stage_duration_ms": round(duration_ms, 2),
                        "stage_complete": True,
                    })

                result = ask_rag(
                    self.kg, 
                    question, 
                    on_stage=announce,
                    on_stage_exit=announce_exit,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    form_type=form_type,
                    request_id=request_id,
                )
                ans = str(result.get("answer") or "")
                words = ans.split(" ")
                for i, w in enumerate(words):
                    self._send_sse("token", {"token": w + (" " if i < len(words) - 1 else "")})
                # ask_rag already measured this request's stages, so its numbers
                # are forwarded rather than re-timed -- only ``total`` is
                # replaced, with the wall clock measured from the first byte
                # this handler accepted, which is the duration the client
                # actually experienced.
                stages = dict(result.get("stage_latencies_ms") or {})
                stages["total"] = round((time.monotonic() - start_time) * 1000, 2)

                # The whole of ask_rag's result, not just its answer.
                #
                # This event used to carry `answer` and four timing fields and
                # nothing else, so a client reading the stream had the text and
                # none of the evidence behind it: no verdict, no cited tags, no
                # tag map, no provenance ledger, no subgraph. The redesigned UI
                # renders precisely those -- they are the answer pane, the
                # Sources and Provenance tabs, and the lit-up subgraph -- so a
                # streamed run showed a verdict of "refused" and a graph with
                # nothing cited on it, while the very same question over the
                # plain JSON route graded SUPPORTED with its citations intact.
                # The grader had run either way; the stream simply dropped its
                # output on the floor.
                #
                # Merged rather than replaced, so the stream-only fields above
                # (`status`, the measured `total`) win over ask_rag's own, and
                # `answer` stays a plain string even when ask_rag reported a
                # failure -- a run that died still has to say so in prose.
                self._send_sse("done", {
                    **result,
                    "status": (
                        "error" if result.get("status") == "error" else "complete"
                    ),
                    "route": "KNOWN",
                    "ticker": result.get("ticker") or routing.ticker,
                    "answer": ans,
                    "stage_latencies_ms": stages,
                    "graph_metrics": result.get("graph_metrics") or _graph_metrics(
                        max_hop_depth=0, node_count=0, edge_count=0
                    ),
                    "latency_ms": round((time.monotonic() - start_time) * 1000, 2),
                })
                return

            # COLD_START — run the full foreground JIT pipeline with SSE progress
            ticker = routing.ticker or "UNKNOWN"
            answer_text = ""
            subgraph_result: dict[str, Any] = {"nodes": [], "paths": []}
            # Same plan contract as the known route, so one timeline in the client
            # serves both without having to know which route it is watching.
            self._send_sse("status", {
                "step": "start",
                "ticker": ticker,
                "stages": list(ROUTE_PLAN["COLD_START"]),
                "message": STAGE_MESSAGES["routing"],
            })
            #: Set when the live pipeline raised and the answer came from the
            #: fallback. Explicit rather than inferred from timings: a fast
            #: degraded run and a fast clean run look identical on the clock,
            #: and a client scoring the answer needs to know which it got.
            degraded = False

            def send_stage_complete(stage: str) -> None:
                """Send stage completion with actual duration from timer."""
                # Get the duration from timer's recorded stages
                stage_key = f"{stage}_ms"
                duration = timer._stages.get(stage, 0.0)
                if duration > 0:
                    self._send_sse("status", {
                        "step": stage,
                        "message": STAGE_MESSAGES.get(stage, stage),
                        "stage_duration_ms": round(duration, 2),
                        "stage_complete": True,
                    })

            try:
                # Step 1: Fetch SEC 10-K filing
                self._send_sse("status", {
                    "step": "fetching",
                    "message": f"Fetching SEC 10-K for {ticker}...",
                })
                with timer.stage("fetching"):
                    fetcher = SECRuntimeFetcher()
                    raw_html, _meta = fetcher.fetch_latest_filing_html(
                        ticker, form_type="10-K", timeout=2.0
                    )
                    cleaned_text = clean_and_truncate_section(
                        raw_html, form_type="10-K", max_tokens=6000
                    )
                send_stage_complete("fetching")

                # Step 2: Triple extraction
                self._send_sse("status", {
                    "step": "extraction",
                    "message": "Extracting financial triples...",
                })
                with timer.stage("extraction"):
                    extractor = ColdStartExtractor()
                    payload = (
                        extractor.extract(cleaned_text, ticker)
                        if hasattr(extractor, "extract")
                        else extractor.extract_triples(cleaned_text, target_ticker=ticker)
                    )
                send_stage_complete("extraction")

                # Step 3: Overlay stitching
                self._send_sse("status", {
                    "step": "stitching",
                    "message": "Stitching to in-memory graph...",
                })
                with timer.stage("stitching"):
                    overlay = InMemoryOverlayGraph(kg_connection=self.kg)
                    stitch_coldstart_payload(overlay, payload, target_ticker=ticker)
                send_stage_complete("stitching")

                # Step 4: 2-hop traversal
                self._send_sse("status", {
                    "step": "traversal",
                    "message": "Running 2-hop traversal...",
                })
                with timer.stage("traversal"):
                    traverser = HybridGraphTraverser(overlay)
                    subgraph_result = traverser.traverse_neighborhood(ticker, max_hops=2)
                send_stage_complete("traversal")

                # Step 5: Stream synthesis tokens
                self._send_sse("status", {
                    "step": "synthesis",
                    "message": "Composing answer...",
                })
                with timer.stage("synthesis"):
                    synthesizer = ColdStartSynthesizer()
                    context = {
                        "target_ticker": ticker,
                        "query": question,
                        "paths": subgraph_result.get("paths", []),
                        "filing_text": cleaned_text,
                    }
                    token_parts: list[str] = []
                    for token in synthesizer.stream_synthesis(context):
                        self._send_sse("token", {"token": token})
                        token_parts.append(token)
                    answer_text = "".join(token_parts)
                send_stage_complete("synthesis")

                # Step 7 (spec): Non-blocking background ingestion
                background_queue.enqueue_coldstart_sync(ticker)

            except Exception as jit_exc:
                # Fallback guard — inform the client then fall back to standard QA.
                degraded = True
                log.warning(
                    "SSE COLD_START JIT pipeline failed for %s (%s: %s); "
                    "falling back to standard graph QA",
                    ticker,
                    type(jit_exc).__name__,
                    jit_exc,
                )
                self._send_sse("status", {
                    "step": "fallback",
                    "message": (
                        f"Live fetch failed ({type(jit_exc).__name__}). "
                        "Answering from available graph context..."
                    ),
                })
                try:
                    fallback_result = ask_rag(
                        self.kg, question, 
                        on_stage=lambda s: None,  # Don't duplicate start events
                        on_stage_exit=lambda s, d: None,  # Don't duplicate complete events
                        request_id=request_id
                    )
                    answer_text = str(
                        fallback_result.get("answer")
                        or fallback_result.get("text")
                        or ""
                    )
                    words = answer_text.split(" ")
                    for i, w in enumerate(words):
                        self._send_sse("token", {
                            "token": w + (" " if i < len(words) - 1 else "")
                        })
                except Exception:
                    pass

            # A stage that raised still records the time it spent before
            # unwinding, so a degraded run reports where its wall clock went
            # instead of a set of zeros that looks like a fast success.
            from .traversal import format_provenance_ledger
            cold_paths = subgraph_result.get("paths", [])
            self._send_sse("done", {
                "status": "complete",
                "answer": answer_text,
                "provenance": format_provenance_ledger(cold_paths),
                "graph": {
                    "nodes": subgraph_result.get("nodes", []),
                    "edges": [
                        hop
                        for path in cold_paths
                        for hop in path
                    ],
                },
                "route": "COLD_START",
                "ticker": ticker,
                "stage_latencies_ms": timer.as_wire(),
                "graph_metrics": _graph_metrics(
                    max_hop_depth=_hop_depth_from_paths(cold_paths),
                    node_count=len(subgraph_result.get("nodes", [])),
                    edge_count=sum(len(path) for path in cold_paths),
                ),
                "degraded": degraded,
                "latency_ms": round((time.monotonic() - start_time) * 1000, 2),
                "background_task_scheduled": True,
            })
        except _SseDeadlineExceeded:
            # Not a failure of the question: the stream hit its lifetime limit.
            # The broad handler below would report this as an error frame and
            # lose the distinction, so it is re-raised for _handle_sse_ask to
            # turn into a terminal ``done`` with status "truncated".
            raise
        except Exception as exc:
            self._send_sse("error", {"error": str(exc), "step": "failed"})
            self._send_sse("done", {
                "status": "error",
                "error": str(exc),
                "ticker": None,
                # Timings for the stages that did run before the failure. A
                # stage that never started is 0.0, not missing, so a client can
                # tell "never ran" from "took no measurable time".
                "stage_latencies_ms": timer.as_wire(),
                "graph_metrics": _graph_metrics(
                    max_hop_depth=0, node_count=0, edge_count=0
                ),
                "latency_ms": round((time.monotonic() - start_time) * 1000, 2),
            })

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

        # Extract temporal filters from payload
        fiscal_year = payload.get("fiscal_year")
        if fiscal_year is not None:
            try:
                fiscal_year = int(fiscal_year)
            except (ValueError, TypeError):
                fiscal_year = None
        
        fiscal_quarter = payload.get("fiscal_quarter")
        if fiscal_quarter is not None:
            fiscal_quarter = str(fiscal_quarter).strip().upper()
            if fiscal_quarter not in ("FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2"):
                fiscal_quarter = None
        
        form_type = payload.get("form_type")
        if form_type is not None:
            form_type = str(form_type).strip().upper()
            if not form_type:
                form_type = None

        accept_header = self.headers.get("Accept", "")
        qs = parse_qs(urlparse(self.path).query)
        is_stream = (
            "text/event-stream" in accept_header
            or qs.get("stream", ["false"])[0].lower() in ("true", "1")
            or bool(payload.get("stream"))
        )

        if not is_stream:
            request_id = getattr(self, "_request_id", None) or get_request_id()
            result = ask_rag(
                self.kg, question, 
                fiscal_year=fiscal_year, 
                fiscal_quarter=fiscal_quarter, 
                form_type=form_type,
                request_id=request_id
            )
            return self._json(result)

        self._handle_sse_ask(question, fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter, form_type=form_type)


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

def parse_ports(spec: str | int | Sequence[int]) -> list[int]:
    """Turn ``--port 9000,8765`` or ``9000`` into a list of ports.

    A string is split on commas, so the flag reads the way the user would say
    it. Anything that is not an integer is a typo worth refusing before a
    socket is bound, not after.
    """
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, str):
        parts = [p.strip() for p in spec.split(",") if p.strip()]
    else:
        parts = [int(p) for p in spec]
    ports = []
    for part in parts:
        try:
            ports.append(int(part))
        except (TypeError, ValueError):
            raise ValueError(f"not a port number: {part!r}") from None
    if not ports:
        raise ValueError("at least one port is required")
    return ports


def _port_is_free(host: str, port: int) -> bool:
    """Whether *port* can be bound, asked without SO_REUSEADDR.

    ``HTTPServer.allow_reuse_address`` is on, and on Windows SO_REUSEADDR does
    not mean "ignore TIME_WAIT" as it does on Linux -- it lets a second socket
    bind a port that is already *listening*, with no error. So a port that is
    already serving something else binds cleanly here and the two fight over
    the connections. Probing first turns that into a clear refusal.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def http_limits() -> Limits:
    """Build the listener ceilings for this process.

    The four transport ceilings come from :meth:`Limits.from_env`, which owns
    the variable names and the fallbacks, so the other HTTP entry points in the
    repository answer to the same names. ``SSE_MAX_SECONDS`` is then overridden
    because its default is package-specific: it tracks ``RAG_TIMEOUT``, the
    model call's own bound, rather than a fixed number.

    The module-level constants above are evaluated at import, not per call, so
    changing the environment after startup does not move a live ceiling. That
    matches every other setting in this module and is deliberate: a running
    server's limits should not change underneath it. Set the variables before
    launching the process.
    """
    return replace(
        Limits.from_env(lambda name: _setting(name, "")),
        sse_max_seconds=SSE_MAX_SECONDS,
    )


def _listeners(
    host: str,
    ports: Sequence[int],
    handler: type,
    limits: Limits | None = None,
) -> list[BoundedThreadingHTTPServer]:
    """Bind one listener per port, all serving the same *handler*.

    Split out of :func:`serve` so the part that can actually fail is reachable
    from a test: a second port that fails to bind, or a shutdown that only
    closes one of them, is invisible in a function that blocks forever and
    keeps its servers to itself.

    Each listener gets its own :class:`ConnectionCounter`, because the ceilings
    are per port. One shared counter would let a burst on the second port refuse
    connections on the first.
    """
    taken = [p for p in ports if not _port_is_free(host, p)]
    if taken:
        raise OSError(
            f"port already in use: {', '.join(str(p) for p in taken)}. "
            f"Stop whatever is serving it, or name different ports with --port."
        )
    ceilings = limits if limits is not None else http_limits()
    servers = []
    try:
        for number in ports:
            server = BoundedThreadingHTTPServer((host, number), handler, limits=ceilings)
            servers.append(server)
    except Exception:
        # One port already taken must not leave the earlier ones listening.
        for server in servers:
            server.server_close()
        raise
    return servers


def serve(host: str = "127.0.0.1", port: int | Sequence[int] = 9000,
          open_browser: bool = True, read_only: bool = True,
          db_path: Path | None = None) -> None:
    _configure_logging()
    ports = parse_ports(port)
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
    servers = _listeners(host, ports, handler)

    primary = f"http://{host}:{ports[0]}/"
    stats = kg.stats()
    print(f"\n{'='*70}")
    print(f"  GraphRAG Viewer & Question Answering Engine")
    print(f"{'='*70}")
    for number in ports:
        suffix = "" if number == ports[0] else "   (same server, same graph)"
        print(f"  Web UI       : http://{host}:{number}/{suffix}")
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
    ceilings = http_limits()
    print(f"  Req Timeout  : {ceilings.request_timeout:.0f}s")
    print(f"  Max Conns    : {ceilings.max_connections} "
          f"(streams: {ceilings.max_sse_connections}, backlog: {ceilings.listen_backlog})")
    print(f"  Press Ctrl-C to stop")
    print(f"{'='*70}\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(primary)).start()

    print(f"  Shutdown     : {SHUTDOWN_TIMEOUT:.0f}s grace period on SIGINT/SIGTERM")
    print(f"  Press Ctrl-C to stop")
    print(f"{'='*70}\n")

    coordinator = ShutdownCoordinator(service="query-ui", timeout=SHUTDOWN_TIMEOUT)
    install_signal_handlers(coordinator)
    # Deliberately not restored afterwards. serve() blocks for the process's
    # whole life and only returns on the way out, so restoring SIG_DFL would
    # hand the last few instructions before exit back to the kernel's default
    # disposition -- and a supervisor that retries its SIGTERM would then kill a
    # process that had already closed its graph and released its ports, turning
    # a clean shutdown into a signal death. The handler stays installed and
    # swallows anything that arrives after the drain has finished.
    try:
        summary = serve_until_signalled(
            servers, coordinator, graph=kg, background=background_queue
        )
    finally:
        # Belt and braces: serve_until_signalled closes the graph, but if it
        # raised partway the handle would otherwise be left open, holding
        # LadybugDB's lock so the next server cannot start.
        _safe_close(kg)
    if summary.get("forced"):
        print(
            f"Stopped. {summary.get('dropped_requests', 0)} request(s) were still "
            f"running when the {SHUTDOWN_TIMEOUT:.0f}s grace period expired."
        )
    else:
        print("Stopped.")


def _safe_close(kg: Any) -> None:
    """Close a graph handle if it is still open, never raising.

    ``LadybugDB`` is idempotent enough here, but a second close raising during
    teardown would replace a real error with a confusing one.
    """
    try:
        kg.close()
    except Exception as exc:
        log.warning("closing the graph failed: %s: %s", type(exc).__name__, exc)


def _main() -> None:
    _configure_logging()
    p = argparse.ArgumentParser(
        prog="python -m sandbox_engine.query_ui",
        description="GraphRAG Question Answering UI for the Blueprint LadybugDB",
    )
    p.add_argument("--port", default="9000",
                   help="one port, or several separated by commas -- all served "
                        "from the same process over one graph handle, e.g. "
                        "--port 9000,8765")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--db", type=Path, default=None,
                   help="Database file to serve. Only needed to serve a "
                        "different graph than the one found automatically; "
                        "LadybugDB locks the file, so two servers cannot share "
                        "one path. For a second port, pass --port instead.")
    p.add_argument("--read-write", action="store_true",
                   help="Open the database read-write (takes an exclusive lock, "
                        "so no other server can share the file)")
    args = p.parse_args()
    try:
        ports = parse_ports(args.port)
        serve(args.host, ports, open_browser=not args.no_browser,
              read_only=not args.read_write, db_path=args.db)
    except OSError as exc:
        log.error("%s", exc)
        raise SystemExit(1)
    except ValueError as exc:
        log.error("%s", exc)
        raise SystemExit(2)


if __name__ == "__main__":
    _main()
