"""In-memory session graph for one company, built just-in-time.

Built lazily on the user's first question, scoped to exactly one company (one
CIN), and destroyed when the session ends or times out. No LadybugDB, no
persistence between sessions — pure NetworkX in memory.

Nodes:
  Company    (cin, name, ticker, isin, sector, industry, exchange, fye_month, fye_day)
  Filing     (filing_id, form_type, filing_date, period_end, fiscal_year, fiscal_period, source_url, source)
  FinancialMetric (metric_id, metric_name, value, scale, unit, period, fiscal_year, fiscal_period)
  Announcement (announcement_id, title, date, category, source_url, source)

Edges:
  Company -[:SUBMITTED]-> Filing
  Filing -[:REPORTS_METRIC]-> FinancialMetric
  Company -[:HAS_ANNOUNCEMENT]-> Announcement
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import networkx as nx


class SessionGraph:
    """One in-memory graph per user session, scoped to one company (one CIN)."""

    def __init__(self, cin: str, company_name: str = "") -> None:
        self.cin = cin
        self.company_name = company_name
        self.graph = nx.DiGraph()
        self.built_at = datetime.now(timezone.utc).isoformat() + "Z"
        self._node_counter = 0
        #: Nodes that carry citable facts (tag -> node_id) for evidence building.
        self._tag_to_node: dict[str, str] = {}
        #: Edge descriptions for figures that live on relationships.
        self._edge_descriptions: dict[str, list[str]] = {}

    # -- node helpers -------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._node_counter += 1
        return f"{prefix}_{self._node_counter}"

    def add_company(self, cin: str, name: str, **metadata: Any) -> str:
        node_id = self._next_id("Company")
        self.graph.add_node(
            node_id,
            type="Company",
            id=node_id,
            cin=cin,
            name=name,
            label=name,
            **metadata,
        )
        return node_id

    def add_filing(
        self,
        form_type: str,
        filing_date: str,
        period_end: str = "",
        fiscal_year: str = "",
        fiscal_period: str = "",
        source_url: str = "",
        source: str = "",
    ) -> str:
        node_id = self._next_id("Filing")
        self.graph.add_node(
            node_id,
            type="Filing",
            id=node_id,
            form_type=form_type,
            filing_date=filing_date,
            period_end=period_end,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            source_url=source_url,
            source=source,
            label=f"{form_type} {filing_date}".strip(),
        )
        return node_id

    def add_metric(
        self,
        metric_name: str,
        value: Any = None,
        scale: str = "",
        unit: str = "",
        period: str = "",
        fiscal_year: str = "",
        fiscal_period: str = "",
        cin: str = "",
        ticker: str = "",
    ) -> str:
        node_id = self._next_id("Metric")
        self.graph.add_node(
            node_id,
            type="FinancialMetric",
            id=node_id,
            metric_name=metric_name,
            value=value,
            scale=scale,
            unit=unit,
            period=period,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            cin=cin,
            ticker=ticker,
            label=metric_name,
        )
        return node_id

    def add_announcement(
        self,
        title: str,
        date: str = "",
        category: str = "",
        source_url: str = "",
        source: str = "",
    ) -> str:
        node_id = self._next_id("Announcement")
        self.graph.add_node(
            node_id,
            type="Announcement",
            id=node_id,
            title=title,
            date=date,
            category=category,
            source_url=source_url,
            source=source,
            label=title,
        )
        return node_id

    # -- edge helpers -------------------------------------------------------

    def submit(self, company_node: str, filing_node: str) -> None:
        self.graph.add_edge(company_node, filing_node, rel="SUBMITTED")

    def reports_metric(
        self,
        filing_node: str,
        metric_node: str,
        value: Any = None,
        scale: str = "",
        period: str = "",
        description: str = "",
    ) -> None:
        self.graph.add_edge(
            filing_node,
            metric_node,
            rel="REPORTS_METRIC",
            value=value,
            scale=scale,
            period=period,
            description=description,
        )
        # Figures live on the edge — store description for evidence building.
        if description:
            self._edge_descriptions.setdefault(metric_node, []).append(description)

    def has_announcement(self, company_node: str, announcement_node: str) -> None:
        self.graph.add_edge(company_node, announcement_node, rel="HAS_ANNOUNCEMENT")

    # -- traversal ----------------------------------------------------------

    def neighbors(self, node_id: str) -> list[str]:
        """Direct neighbors (outgoing edges), used for 1-hop retrieval."""
        return list(self.graph.successors(node_id))

    def find_company_node(self) -> str | None:
        """Return the single Company node id in this session graph."""
        for nid, data in self.graph.nodes(data=True):
            if data.get("type") == "Company":
                return nid
        return None

    def find_metrics(self, metric_name_contains: str = "") -> list[dict[str, Any]]:
        """Return metric nodes, optionally filtered by name substring."""
        out: list[dict[str, Any]] = []
        for nid, data in self.graph.nodes(data=True):
            if data.get("type") != "FinancialMetric":
                continue
            if metric_name_contains and metric_name_contains.lower() not in str(data.get("metric_name", "")).lower():
                continue
            out.append({**data, "id": nid})
        return out

    def find_filings(self) -> list[dict[str, Any]]:
        """Return all filing nodes."""
        return [
            {**data, "id": nid}
            for nid, data in self.graph.nodes(data=True)
            if data.get("type") == "Filing"
        ]

    def find_announcements(self) -> list[dict[str, Any]]:
        """Return all announcement nodes."""
        return [
            {**data, "id": nid}
            for nid, data in self.graph.nodes(data=True)
            if data.get("type") == "Announcement"
        ]

    def as_nodes(self) -> list[dict[str, Any]]:
        """All nodes as dicts, for evidence building."""
        return [
            {**data, "id": nid} for nid, data in self.graph.nodes(data=True)
        ]

    def as_edges(self) -> list[dict[str, Any]]:
        """All edges as dicts, for evidence building."""
        return [
            {
                "source": u,
                "target": v,
                "rel": data.get("rel", ""),
                "description": data.get("description", ""),
                "value": data.get("value"),
                "scale": data.get("scale", ""),
                "period": data.get("period", ""),
            }
            for u, v, data in self.graph.edges(data=True)
        ]

    def tag_node(self, tag: str, node_id: str) -> None:
        """Record that *tag* refers to *node_id* (retriever's tag map)."""
        self._tag_to_node[tag] = node_id

    def tag_map(self) -> dict[str, str]:
        return dict(self._tag_to_node)
