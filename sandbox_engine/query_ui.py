"""GraphRAG Question-Answering & Interactive Explorer for the blueprint LadybugDB.

Provides the same three-pane UI as graphrag/web.py and graphrag/static/index.html:
  left   — entity browser (Company, Filing, FinancialMetric, Segment, DisclosureEvent)
  centre — interactive force-directed graph with citation highlighting
  right  — natural-language question box powered by NVIDIA gpt-oss-20b GraphRAG

Run::

    source .venv/bin/activate
    python -m sandbox_engine.query_ui            # opens http://127.0.0.1:9000/
    python -m sandbox_engine.query_ui --port 9001
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import ladybug as lb


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
from openai import OpenAI

log = logging.getLogger("graphrag_ui")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

_HERE = Path(__file__).resolve().parent
# The blueprint database. This UI's Cypher is written against that schema --
# FinancialMetric.metric_id, Segment.segment_id, DISAGGREGATED_BY and so on.
# The _run/sandbox.lbug graph built by ``python -m sandbox_engine`` uses a
# different schema (Metric.id, Segment.name, HAS_SEGMENT, Event), and pointing
# this at it silently yields 17 visible nodes out of 1392, so it stays on _run2.
_DB_CANDIDATES = (
    _HERE / "_run2" / "blueprint.lbug",   # blueprint schema, what the Cypher expects
    _HERE / "_run" / "sandbox.lbug",       # engine schema, only if nothing else exists
)


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
    # properties
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
    # present in the blueprint schema only
    "scale": "NULL",
    "raw_label": "NULL",
    "event_date": "NULL",
    "document_text": "text",
    "chunk_type": "section",
}

_TRANSLATE_RE = re.compile(
    r"\b(" + "|".join(sorted(_BLUEPRINT_TO_ENGINE, key=len, reverse=True)) + r")\b"
)


def translate_for_engine(cypher: str) -> str:
    """Rewrite blueprint-schema Cypher into engine-schema Cypher."""
    return _TRANSLATE_RE.sub(lambda m: _BLUEPRINT_TO_ENGINE[m.group(1)], cypher)


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

# NVIDIA OpenAI client configuration
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "openai/gpt-oss-20b"


def _load_api_key() -> str:
    """The key for the question box, in precedence order.

    1. the ``NVIDIA_API_KEY`` environment variable;
    2. ``sandbox_engine/.env``, which the repo's ``.env`` rule already ignores.

    A parser rather than ``python-dotenv``, which is not a dependency here: the
    file is a flat ``KEY=value`` list and a regex is enough for it. The key used
    to be a literal in this source file, which put a live credential in a file
    people copy around; everything except the question box works without one.
    """
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if key:
        return key
    env_file = _HERE / ".env"
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == "NVIDIA_API_KEY":
            return value.strip().strip("'\"")
    return ""


NVIDIA_API_KEY = _load_api_key()

MAX_BODY = 128 * 1024

_CITATION_RE = re.compile(r"\[(E\d+)\]")

ANSWER_SYSTEM = """\
You answer questions using only a retrieved subgraph from a financial knowledge graph.

You are given entities and relationships, each carrying a bracketed tag like [E1] or [E12].

Rules:
- Use only the supplied context. If it does not contain the answer, say so plainly and state what is missing.
- Cite every factual claim using bracketed tags like [E1] or [E2] corresponding to the entities or relationships.
- Financial values in relationships include scale properties: scale=6 means in millions (e.g. 416161.0 scale=6 is $416,161 million USD). State the units clearly.
- Answer directly and factually in clean prose or concise bullet points. No preamble.
"""


# ── Database Layer ────────────────────────────────────────────────────────────

class KnowledgeGraph:
    def __init__(self, db_path: Path):
        self.db = lb.Database(str(db_path))
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

    def stats(self) -> dict[str, Any]:
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
        for r in self.execute("MATCH (e:DisclosureEvent) RETURN e.event_id, e.item_code, e.item_title, e.event_date, e.summary"):
            eid, code, title, edate, summary = r[0], r[1], r[2], r[3], r[4]
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

def retrieve_financial_context(kg: KnowledgeGraph, question: str) -> tuple[str, list[dict], list[dict], dict[str, str], list[str]]:
    """Retrieves relevant entities and relations matching the question and formats context with tags [E1], [E2]..."""
    q_low = question.lower()

    # Identify candidate seeds
    seed_nodes: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, name: str, etype: str, desc: str = "", hint: str = ""):
        if nid not in seen_ids:
            seen_ids.add(nid)
            seed_nodes.append({"id": nid, "name": name, "label_hint": hint, "type": etype, "description": desc})

    # Always include the core company
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
    for m in metrics:
        mid, cname, stype, aclass = m[0], m[1], m[2], m[3]
        name_clean = re.sub(r"\(.*?\)", "", cname).strip().lower()
        keywords = [name_clean]
        if "sales" in name_clean or "revenue" in name_clean:
            keywords.extend(["sales", "revenue", "top line"])
        if "profit" in name_clean:
            keywords.extend(["profit", "gross margin"])
        if "income" in name_clean:
            keywords.extend(["income", "operating income", "net income", "earnings"])
        if "research" in name_clean:
            keywords.extend(["r&d", "research", "development"])
        if "operating expense" in name_clean:
            keywords.extend(["opex", "operating expense", "expenses"])
        if "cost" in name_clean:
            keywords.extend(["cost", "cogs"])

        if any(kw in q_low for kw in keywords):
            add_node(mid, cname, "FinancialMetric", f"Statement: {stype}, Class: {aclass}")

    # Check segments
    segments = kg.execute("MATCH (s:Segment) RETURN s.segment_id, s.dimension_name, s.dimension_type")
    for s in segments:
        sid, name, dtype = s[0], s[1], s[2]
        if name.lower() in q_low or dtype.lower() in q_low or "segment" in q_low or "breakdown" in q_low or "geograph" in q_low or "product" in q_low:
            add_node(sid, name, "Segment", f"Dimension type: {dtype}")

    # Check disclosure events
    events = kg.execute("MATCH (e:DisclosureEvent) RETURN e.event_id, e.item_code, e.item_title, e.event_date, e.summary")
    for e in events:
        eid, code, title, edate, summary = e[0], e[1], e[2], e[3], e[4]
        if code in q_low or "8-k" in q_low or "event" in q_low or "item" in q_low or "press release" in q_low or "operation" in q_low:
            clean_title = re.sub(r"&[a-z0-9#]+;", " ", title).strip()
            add_node(eid, f"Item {code}: {clean_title}", "DisclosureEvent", f"Date: {edate}, Summary: {summary[:160]}")

    # If too few nodes matched (e.g. broad general question), populate with top metrics
    if len(seen_ids) <= 3:
        for m in metrics:
            mid, cname, stype, aclass = m[0], m[1], m[2], m[3]
            if any(term in mid.lower() for term in ["netsales", "grossprofit", "operatingincome", "netincome", "researchdevelopment"]):
                add_node(mid, cname, "FinancialMetric", f"Statement: {stype}, Class: {aclass}")
        for s in segments[:10]:
            add_node(s[0], s[1], "Segment", f"Dimension type: {s[2]}")

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


def _explain_api_error(exc: Exception) -> str:
    """Turn an upstream failure into something the reader can act on.

    A bare ``403 Authorization failed`` in the answer box looks like a bug in
    this app. It is not: the key authenticated, so the request was refused
    further up, and the two refusals mean different things.
    """
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return (
            f"NVIDIA refused the request ({status}). The key in "
            f"sandbox_engine/.env authenticates but has no inference "
            f"entitlement for {NVIDIA_MODEL}, so answering is unavailable. "
            f"Check the key's permissions at build.nvidia.com, or set a "
            f"working NVIDIA_API_KEY in the environment. The graph explorer "
            f"below does not need it."
        )
    if status == 410:
        return (
            f"{NVIDIA_MODEL} is retired upstream (410 Gone). Choose a current "
            f"model and update NVIDIA_MODEL in query_ui.py."
        )
    if status == 429:
        return "Rate limited by NVIDIA (429). Wait a moment and try again."
    return f"Error communicating with {NVIDIA_MODEL}: {exc}"


def ask_rag(kg: KnowledgeGraph, question: str) -> dict[str, Any]:
    if not NVIDIA_API_KEY:
        return {
            "error": (
                "NVIDIA_API_KEY is not set, so the question box is disabled. "
                "Export it and restart to enable GraphRAG answers; the graph "
                "explorer does not need it."
            ),
            "id": "n/a",
        }

    t0 = time.perf_counter()
    context_str, nodes, edges, tag_map, seed_ids = retrieve_financial_context(kg, question)

    log.info("Retrieved %d entities and %d relationships for: %s", len(nodes), len(edges), question)

    # Initialize OpenAI client targeting NVIDIA
    client = OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=NVIDIA_API_KEY,
    )

    try:
        completion = client.chat.completions.create(
            model=NVIDIA_MODEL,
            messages=[
                {"role": "system", "content": ANSWER_SYSTEM},
                {
                    "role": "user",
                    "content": f"CONTEXT (retrieved knowledge graph):\n{context_str}\n\nQUESTION: {question}\n\nAnswer using only the context above, citing tags like [E1].",
                },
            ],
            temperature=0.2,
            top_p=1,
            max_tokens=4096,
            stream=False,
        )
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
        log.error("NVIDIA API call failed: %s", exc)
        return {
            "question": question,
            "text": _explain_api_error(exc),
            "reasoning": "",
            "grounded": False,
            "used_tags": [],
            "tag_map": tag_map,
            "context_entities": len(nodes),
            "context_edges": len(edges),
            "flow": "",
            "graph": None,
        }

    # Extract cited tags
    cited_raw = _CITATION_RE.findall(content)
    used_tags = sorted(set(t for t in cited_raw if t in tag_map), key=lambda x: int(x[1:]))

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
</style>
</head>
<body>
<header>
  <h1>graphrag viewer</h1>
  <div class="stats" id="stats">loading…</div>
  <div class="spacer"></div>
  <button id="reportsBtn" style="font-size:12px;padding:5px 10px">reports</button>
  <label class="stats" for="hops">hops</label>
  <select id="hops" style="width:auto">
    <option value="1">1</option>
    <option value="2" selected>2</option>
  </select>
  <button id="fit">fit</button>
</header>

<main>
  <aside>
    <div class="pane">
      <label class="field" for="entitySearch">Entities</label>
      <input type="search" id="entitySearch" placeholder="filter by name or type…" autocomplete="off">
    </div>
    <div id="entityList"></div>
    <div class="pane">
      <div class="row">
        <button id="showAll" class="primary" style="flex:1">show whole graph</button>
        <button id="clearSeed">clear</button>
      </div>
    </div>
  </aside>

  <div id="graphWrap">
    <svg id="svg"><g id="viewport"></g></svg>
    <div class="toolbar">
      <button id="relayout">re-layout</button>
      <button id="zoomIn">+</button>
      <button id="zoomOut">−</button>
    </div>
    <div id="legend"></div>
    <div class="hint">drag node to move · click to focus · scroll to zoom · drag background to pan</div>
    <div id="tip"></div>
  </div>

  <section class="qa">
    <div class="pane">
      <label class="field" for="askBox">Ask the graph (NVIDIA gpt-oss-20b)</label>
      <textarea id="askBox" placeholder="e.g. What was Apple's total net sales in 2025 and how much came from the Americas segment?"></textarea>
      <div class="row" style="margin-top:8px">
        <button id="askBtn" class="primary" style="flex:1">ask</button>
      </div>
      <div class="examples" id="examples"></div>
    </div>
    <div id="answerWrap">
      <div class="empty">Answers are generated from the retrieved subgraph and cite entities as
        <code>[E1]</code>. Click a citation to highlight it in the graph.</div>
    </div>
  </section>
</main>

<!-- Reports Overlay -->
<div id="reportsOverlay">
  <div id="reportsPanel">
    <div id="reportsHeader">
      <h2>Canned Reports — graph query results</h2>
      <span class="chip" style="font-size:11px">Zero-LLM · Pure Cypher · All companies</span>
      <button onclick="document.getElementById('reportsOverlay').classList.remove('open')">✕ close</button>
    </div>
    <div id="reportsList"></div>
    <div id="reportsBody">
      <div class="empty">Select a report above to run it against the graph.</div>
    </div>
  </div>
</div>

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
};

const $ = (id) => document.getElementById(id);
const svg = $("svg"), viewport = $("viewport"), tip = $("tip");

function typeColor(type) {
  const t = (type || "unspecified").toLowerCase();
  const known = {
    company: "#7ee0b8", filing: "#6ea8fe", financialmetric: "#ffb454",
    segment: "#d2a8ff", disclosureevent: "#f78fb3", documentchunk: "#8fd3f4",
    executive: "#ffd479", supplier: "#a0e8a0",
  };
  if (known[t]) return known[t];
  let h = 0;
  for (const ch of t) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360} 55% 62%)`;
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

class Sim {
  constructor(nodes, links, width, height) {
    this.nodes = nodes; this.links = links;
    this.w = width; this.h = height;
    const n = nodes.length;
    this.repel = 2600 + n * 26;
    this.spring = 0.055;
    this.damp = 0.86;
    nodes.forEach((d, i) => {
      const a = i * 2.399963, r = 26 * Math.sqrt(i + 1);
      d.x = width / 2 + r * Math.cos(a);
      d.y = height / 2 + r * Math.sin(a);
      d.vx = 0; d.vy = 0;
    });
    this.alpha = 1;
  }
  tick() {
    const { nodes, links, w, h } = this;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { d2 = 1; dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); }
        const d = Math.sqrt(d2);
        const f = (this.repel / d2) * this.alpha;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
      }
    }
    for (const l of links) {
      const a = l.source, b = l.target;
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.max(1, Math.hypot(dx, dy));
      const f = (d - 150) * this.spring * this.alpha;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    for (const d of nodes) {
      d.vx += (w / 2 - d.x) * 0.0022 * this.alpha;
      d.vy += (h / 2 - d.y) * 0.0022 * this.alpha;
      d.vx *= this.damp; d.vy *= this.damp;
      d.x += d.vx; d.y += d.vy;
    }
    this.alpha *= 0.992;
    return this.alpha > 0.02;
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

const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
};

function draw() {
  viewport.textContent = "";
  viewport.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);

  const hasFocus = S.seeds.size > 0 || S.cited.size > 0;
  const gLinks = el("g", {}), gLabels = el("g", {}), gNodes = el("g", {});

  for (const l of S.links) {
    if (!l.source || !l.target) continue;
    const inSeed = S.seeds.has(l.source.id) || S.seeds.has(l.target.id);
    const isCited = S.cited.has(l.source.id) && S.cited.has(l.target.id);
    const cls = isCited ? "link cited" : (hasFocus && !inSeed ? "link dim" : "link");
    const line = el("line", {
      class: cls, x1: l.source.x, y1: l.source.y, x2: l.target.x, y2: l.target.y,
    });
    line.appendChild(el("title", {})).textContent =
      `${l.source.name} —${l.relation}→ ${l.target.name}` +
      (l.description ? `\n${l.description}` : "");
    gLinks.appendChild(line);

    const mx = (l.source.x + l.target.x) / 2, my = (l.source.y + l.target.y) / 2;
    if (S.view.k > 0.62 || isCited) {
      const t = el("text", { class: "lbl", x: mx, y: my - 4, "text-anchor": "middle" });
      t.textContent = l.relation.replace(/_/g, " ");
      gLabels.appendChild(t);
    }
  }

  for (const d of S.nodes) {
    const isCited = S.cited.has(d.id);
    const isSeed = S.seeds.has(d.id);
    const g = el("g", {
      class: "node" + (isSeed ? " seed" : "") +
             (isCited ? " cited" : "") +
             (hasFocus && !isSeed && !isCited ? " dim" : ""),
      transform: `translate(${d.x},${d.y})`,
    });
    g.appendChild(el("circle", { r: d.r || 9, fill: typeColor(d.type) }));
    if (S.view.k > 0.45) {
      const label = el("text", { y: (d.r || 9) + 12, "text-anchor": "middle" });
      label.textContent = d.name.length > 26 ? d.name.slice(0, 25) + "…" : d.name;
      g.appendChild(label);
    }
    gNodes.appendChild(g);
  }
  viewport.appendChild(gLinks);
  viewport.appendChild(gLabels);
  viewport.appendChild(gNodes);
}

function fit() {
  const r = svg.getBoundingClientRect();
  if (!S.nodes.length) { S.view = { x: 0, y: 0, k: 1 }; draw(); return; }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const d of S.nodes) {
    minX = Math.min(minX, d.x - 30); maxX = Math.max(maxX, d.x + 30);
    minY = Math.min(minY, d.y - 30); maxY = Math.max(maxY, d.y + 30);
  }
  const k = Math.min(2, Math.max(0.15,
    Math.min(r.width / (maxX - minX), r.height / (maxY - minY)) * 0.9));
  S.view = {
    k,
    x: r.width / 2 - ((minX + maxX) / 2) * k,
    y: r.height / 2 - ((minY + maxY) / 2) * k,
  };
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

let dragNode = null, panning = null;

function toLocal(evt) {
  const r = svg.getBoundingClientRect();
  return {
    x: (evt.clientX - r.left - S.view.x) / S.view.k,
    y: (evt.clientY - r.top - S.view.y) / S.view.k,
  };
}

function nodeAt(evt) {
  const p = toLocal(evt);
  let best = null, bd = Infinity;
  for (const n of S.nodes) {
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    if (d < (n.r || 9) + 4 && d < bd) { bd = d; best = n; }
  }
  return best;
}

svg.addEventListener("pointerdown", (e) => {
  svg.setPointerCapture(e.pointerId);
  const hit = nodeAt(e);
  if (hit) { dragNode = hit; hideTip(); return; }
  panning = { x: e.clientX, y: e.clientY, vx: S.view.x, vy: S.view.y };
  svg.classList.add("dragging");
});

svg.addEventListener("pointermove", (e) => {
  if (dragNode) {
    const p = toLocal(e);
    dragNode.x = p.x; dragNode.y = p.y;
    dragNode.vx = 0; dragNode.vy = 0;
    if (S.sim) S.sim.alpha = Math.max(S.sim.alpha, 0.35);
    startSim(false);
    return;
  }
  if (panning) {
    S.view.x = panning.vx + (e.clientX - panning.x);
    S.view.y = panning.vy + (e.clientY - panning.y);
    draw();
    return;
  }
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

const endDrag = () => { dragNode = null; panning = null; svg.classList.remove("dragging"); };
svg.addEventListener("pointerup", endDrag);
svg.addEventListener("pointercancel", endDrag);
svg.addEventListener("pointerleave", hideTip);
svg.addEventListener("click", (e) => {
  const best = nodeAt(e);
  if (best) focusEntity(best.id);
});
svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = svg.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const k = Math.max(0.1, Math.min(3, S.view.k * (e.deltaY < 0 ? 1.12 : 0.89)));
  S.view.x = mx - (mx - S.view.x) * (k / S.view.k);
  S.view.y = my - (my - S.view.y) * (k / S.view.k);
  S.view.k = k;
  draw();
}, { passive: false });

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("stats").textContent = `${s.nodes} entities · ${s.edges} relationships`;
    const legend = $("legend");
    legend.textContent = "";
    for (const [type, n] of (s.entity_types || [])) {
      const b = document.createElement("button");
      b.className = "chip";
      b.style.borderColor = typeColor(type);
      b.style.color = typeColor(type);
      b.textContent = `${type} ${n}`;
      b.onclick = () => {
        const term = type.toLowerCase();
        const input = $("entitySearch");
        input.value = input.value === term ? "" : term;
        input.dispatchEvent(new Event("input"));
      };
      legend.appendChild(b);
    }
  } catch (e) { $("stats").textContent = "error: " + e.message; }
}

async function loadEntities(q) {
  try {
    const d = await api(`/api/entities?limit=500${q ? "&q=" + encodeURIComponent(q) : ""}`);
    S.all = d.entities;
    renderEntities();
  } catch (e) {}
}

function renderEntities() {
  const box = $("entityList");
  const term = ($("entitySearch").value || "").toLowerCase();
  const rows = S.all.filter((e) =>
    !term || e.name.toLowerCase().includes(term) ||
    (e.entity_type || "").toLowerCase().includes(term));

  box.textContent = "";
  if (!rows.length) {
    box.innerHTML = '<div class="empty" style="padding:10px">no matching entities</div>';
    return;
  }
  for (const e of rows) {
    const d = document.createElement("div");
    d.className = "entity" + (S.selected === e.id ? " sel" : "");
    d.innerHTML = `<span class="nm"></span><span class="ty"></span>`;
    d.querySelector(".nm").textContent = e.name;
    d.querySelector(".ty").textContent = e.entity_type || "";
    d.onclick = () => focusEntity(e.id);
    box.appendChild(d);
  }
}

async function focusEntity(id) {
  S.selected = id;
  renderEntities();
  const hops = $("hops").value;
  try {
    const g = await api(`/api/graph?seed=${encodeURIComponent(id)}&hops=${hops}`);
    loadGraph(g, { fresh: true });
  } catch (e) {
    $("answerWrap").innerHTML = `<div class="note">${esc(e.message)}</div>`;
  }
}

async function showAll() {
  S.selected = null;
  renderEntities();
  try {
    const g = await api("/api/graph?limit=300");
    loadGraph(g, { fresh: true });
  } catch (e) {
    $("answerWrap").innerHTML = `<div class="note">${esc(e.message)}</div>`;
  }
}

async function askQuestion() {
  const q = $("askBox").value.trim();
  if (!q || S.busy) return;
  S.busy = true;
  $("askBtn").disabled = true;
  $("answerWrap").innerHTML = '<div class="spinner"><span>⚡</span> Retrieving graph context & generating answer with gpt-oss-20b…</div>';
  try {
    const res = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    renderAnswer(res);
    if (res.graph) {
      const citedIds = (res.used_tags || []).map((t) => res.tag_map[t]).filter(Boolean);
      loadGraph(res.graph, { cited: citedIds });
    }
  } catch (e) {
    $("answerWrap").innerHTML = `<div class="note">Query failed: ${esc(e.message)}</div>`;
  } finally {
    S.busy = false;
    $("askBtn").disabled = false;
  }
}

function renderAnswer(res) {
  const wrap = $("answerWrap");
  wrap.textContent = "";

  const meta = document.createElement("div");
  meta.className = "meta";

  // Citation chips
  for (const t of res.used_tags || []) {
    const id = (res.tag_map || {})[t];
    const ent = res.graph && (res.graph.nodes || []).find((n) => n.id === id);
    const c = document.createElement("span");
    c.className = "chip cite";
    c.textContent = `${t}${ent ? " " + ent.name : ""}`;
    c.title = "Click to highlight in graph";
    c.onclick = () => {
      S.cited = new Set([id]);
      draw();
    };
    meta.appendChild(c);
  }

  if (res.context_entities) {
    const c = document.createElement("span");
    c.className = "chip";
    c.textContent = `${res.context_entities} entities / ${res.context_edges} rels`;
    meta.appendChild(c);
  }

  const g = document.createElement("span");
  g.className = "chip " + (res.grounded ? "ok" : "bad");
  g.textContent = res.grounded ? "grounded" : "not grounded";
  meta.appendChild(g);

  if (res.elapsed_sec) {
    const s = document.createElement("span");
    s.className = "chip";
    s.textContent = `${res.elapsed_sec}s`;
    meta.appendChild(s);
  }

  wrap.appendChild(meta);

  // Main Answer text
  const body = document.createElement("div");
  body.id = "answer";
  body.textContent = res.text;
  wrap.appendChild(body);

  // Model Reasoning Collapsible
  if (res.reasoning) {
    const rBox = document.createElement("details");
    rBox.className = "reasoning-box";
    rBox.innerHTML = `<summary>🧠 Model Reasoning (gpt-oss-20b)</summary><div style="margin-top:6px">${esc(res.reasoning)}</div>`;
    wrap.appendChild(rBox);
  }

  // Retrieval Flow Trace
  if (res.flow) {
    const n = document.createElement("details");
    n.className = "note";
    n.innerHTML = `<summary style="cursor:pointer;font-weight:600">Trace: Graph Retrieval Flow</summary><div style="margin-top:6px;line-height:1.5">${esc(res.flow)}</div>`;
    wrap.appendChild(n);
  }
}

$("entitySearch").addEventListener("input", (e) => {
  if (e.target.value === "") { loadEntities(""); return; }
  clearTimeout(window.__t);
  window.__t = setTimeout(() => loadEntities(e.target.value), 180);
});
$("showAll").onclick = showAll;
$("clearSeed").onclick = () => { S.cited = new Set(); showAll(); };
$("hops").onchange = () => { if (S.selected) focusEntity(S.selected); else showAll(); };
$("fit").onclick = fit;
$("relayout").onclick = () => {
  if (S.sim) { S.sim.alpha = 1; startSim(true); }
  setTimeout(fit, 700);
};
$("zoomIn").onclick = () => { S.view.k = Math.min(3, S.view.k * 1.25); draw(); };
$("zoomOut").onclick = () => { S.view.k = Math.max(0.1, S.view.k / 1.25); draw(); };
$("askBtn").onclick = askQuestion;
$("askBox").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) askQuestion();
});
window.addEventListener("resize", () => draw());

const SAMPLE_QUESTIONS = [
  "What was Apple's total net sales in fiscal year 2025 and how much came from the Americas segment?",
  "What was Apple's Gross Profit, Operating Income, and R&D in FY2025?",
  "What quarterly revenue and period did Apple report in the 10-Q?",
  "What items and events were disclosed in Apple's Form 8-K?",
  "How does Apple's revenue break down across product and geographic segments?",
  // 5 new cross-company questions
  "Compare net income across Apple, NVIDIA, Microsoft, Tesla, Meta, and Amazon for their latest fiscal year.",
  "Which company had the highest operating margin among all companies in the graph?",
  "What was NVIDIA's revenue and gross profit for FY2026?",
  "How does Amazon's operating income compare to Meta's for fiscal year 2025?",
  "What are the total assets and stockholders equity for Tesla and Microsoft?",
];

function seedExamples() {
  const box = $("examples");
  box.textContent = "";
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
  // Mark active button
  for (const b of $('reportsList').querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.id === id);
  }
  const body = $('reportsBody');
  body.innerHTML = '<div class="spinner"><span>⚡</span> Running report…</div>';
  try {
    const d = await api('/api/reports/' + encodeURIComponent(id));
    if (d.error) { body.innerHTML = `<div class="note">${esc(d.error)}</div>`; return; }

    const fmtVal = (col, v) => {
      if (v === null || v === undefined) return '—';
      if ((col === 'value') && typeof v === 'number') return v.toLocaleString(undefined, {maximumFractionDigits: 2});
      if (col === 'scale' && typeof v === 'number') {
        return v === 6 ? 'millions' : v === 3 ? 'thousands' : v === 9 ? 'billions' : String(v);
      }
      return String(v);
    };

    let html = `<div id="reportTitle">${esc(d.title)}</div>`;
    html += `<div id="reportDesc">${esc(d.description)}</div>`;
    if (!d.rows || d.rows.length === 0) {
      html += '<div class="empty">No rows returned — graph may not contain matching data yet.</div>';
    } else {
      html += '<table id="reportTable"><thead><tr>';
      for (const col of (d.columns || [])) html += `<th>${esc(col)}</th>`;
      html += '</tr></thead><tbody>';
      for (const row of d.rows) {
        html += '<tr>';
        for (let i = 0; i < (d.columns || []).length; i++) {
          html += `<td>${esc(fmtVal(d.columns[i], row[i]))}</td>`;
        }
        html += '</tr>';
      }
      html += '</tbody></table>';
      html += `<div id="reportCount">${d.row_count} row${d.row_count !== 1 ? 's' : ''}</div>`;
    }
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = `<div class="note">Report failed: ${esc(e.message)}</div>`;
  }
}

$('reportsBtn').onclick = () => {
  document.getElementById('reportsOverlay').classList.add('open');
  if (_reportsMeta.length === 0) loadReportsList();
};
document.getElementById('reportsOverlay').addEventListener('click', (e) => {
  if (e.target === document.getElementById('reportsOverlay'))
    document.getElementById('reportsOverlay').classList.remove('open');
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
        parsed = urlparse(self.path)
        p, qs = parsed.path, parse_qs(parsed.query)
        if p in ("/", "/index.html"):
            return self._send(200, _HTML.encode(), "text/html; charset=utf-8")
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

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/ask":
            return self._api_ask()
        return self._err(404, f"no route: {parsed.path}")

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

def serve(host: str = "127.0.0.1", port: int = 9000, open_browser: bool = True) -> None:
    db_path = resolve_db_path()
    if db_path is None:
        log.error(
            "No graph database found. Looked for:\n  %s\n"
            "Build one from the committed filings first:\n"
            "  python -m sandbox_engine --reset\n"
            "then start this server again.",
            "\n  ".join(str(p) for p in _DB_CANDIDATES),
        )
        raise SystemExit(1)

    kg = KnowledgeGraph(db_path)
    handler = type("_BoundHandler", (_Handler,), {"kg": kg})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True

    url = f"http://{host}:{port}/"
    stats = kg.stats()
    print(f"\n{'═'*70}")
    print(f"  GraphRAG Viewer & Question Answering Engine")
    print(f"{'═'*70}")
    print(f"  Web UI       : {url}")
    print(f"  Database     : {db_path}")
    print(f"  Graph Stats  : {stats['nodes']} entities, {stats['edges']} relationships")
    print(f"  RAG Model    : {NVIDIA_MODEL} via NVIDIA NIM ({NVIDIA_BASE_URL})")
    print(f"  Press Ctrl-C to stop")
    print(f"{'═'*70}\n")

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
    p = argparse.ArgumentParser(prog="python -m sandbox_engine.query_ui",
                                description="GraphRAG Question Answering UI for the Blueprint LadybugDB")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    serve(args.host, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    _main()
