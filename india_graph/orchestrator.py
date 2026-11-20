"""Orchestrates scraping -> graph building for one company, one session.

Called lazily on the user's first question. Scrapes NSE + BSE (announcements
and financials) and MCA (company master via CIN), normalizes into ScrapedRecord
objects, and builds the in-memory SessionGraph deterministically. Provenance on
every record is set by source: NSE/BSE/MCA = STATED.
"""

from __future__ import annotations

from .config import IndiaConfig
from .graph import SessionGraph
from .resolver import TickerCINResolver
from .scrapers.bse import BSEScraper
from .scrapers.mca import MCAScraper
from .scrapers.nse import NSEScraper


class IndiaOrchestrator:
    """Builds a SessionGraph for one company by scraping India sources."""

    def __init__(self, config: IndiaConfig | None = None, resolver: TickerCINResolver | None = None) -> None:
        self.config = config or IndiaConfig()
        self.resolver = resolver or TickerCINResolver(config)
        self.nse = NSEScraper(config)
        self.bse = BSEScraper(config)
        self.mca = MCAScraper(config)

    def build_for_cin(self, cin: str, company_name: str = "") -> SessionGraph:
        """Scrape all sources for *cin* and build the session graph."""
        graph = SessionGraph(cin=cin, company_name=company_name)
        company_node = graph.add_company(cin=cin, name=company_name, source="MCA")

        # --- MCA: company master (STATED) ---
        mca_result = self.mca.fetch_company(cin)
        if mca_result.ok and mca_result.records:
            rec = mca_result.records[0]
            data = rec.data
            if data.get("company_name") and not company_name:
                company_name = data["company_name"]
                graph.company_name = company_name
            # Update company node with master data
            graph.graph.nodes[company_node].update({
                "sector": data.get("sector", ""),
                "industry": data.get("industry", ""),
                "roc": data.get("roc", ""),
                "date_of_incorporation": data.get("date_of_incorporation", ""),
                "paid_up_capital": data.get("paid_up_capital", ""),
                "authorized_capital": data.get("authorized_capital", ""),
                "registered_office_address": data.get("registered_office_address", ""),
                "email": data.get("email", ""),
            })

        # --- NSE: announcements + financials (STATED) ---
        nse_ann = self.nse.fetch_announcements(cin)
        if nse_ann.ok:
            for rec in nse_ann.records:
                ann_node = graph.add_announcement(
                    title=rec.data.get("title", ""),
                    date=rec.data.get("date", ""),
                    category=rec.data.get("category", ""),
                    source_url=rec.data.get("attachment", ""),
                    source="NSE",
                )
                graph.has_announcement(company_node, ann_node)

        nse_fin = self.nse.fetch_financials(cin)
        if nse_fin.ok:
            for rec in nse_fin.records:
                data = rec.data
                metric_node = graph.add_metric(
                    metric_name=f"NSE {data.get('company_name', 'quote')} last price",
                    value=data.get("last_price"),
                    scale="INR",
                    unit="INR",
                    period="latest",
                    cin=cin,
                )
                graph.reports_metric(
                    company_node, metric_node,
                    value=data.get("last_price"),
                    description=f"NSE last price {data.get('last_price')} for {data.get('company_name', symbol_for_cin(cin))}",
                )

        # --- BSE: announcements + financials (STATED) ---
        bse_ann = self.bse.fetch_announcements(cin)
        if bse_ann.ok:
            for rec in bse_ann.records:
                ann_node = graph.add_announcement(
                    title=rec.data.get("title", ""),
                    date=rec.data.get("date", ""),
                    category=rec.data.get("category", ""),
                    source_url=rec.data.get("attachment", ""),
                    source="BSE",
                )
                graph.has_announcement(company_node, ann_node)

        bse_fin = self.bse.fetch_financials(cin)
        if bse_fin.ok:
            for rec in bse_fin.records:
                data = rec.data
                metric_node = graph.add_metric(
                    metric_name=f"BSE {data.get('company_name', 'financials')}",
                    value=data.get("revenue") or data.get("profit_after_tax"),
                    scale="INR",
                    unit="INR",
                    period=data.get("period", ""),
                    fiscal_year=data.get("fiscal_year", ""),
                )
                graph.reports_metric(
                    company_node, metric_node,
                    value=data.get("revenue") or data.get("profit_after_tax"),
                    description=f"BSE {data.get('period', '')} revenue {data.get('revenue')} PAT {data.get('profit_after_tax')}",
                )

        # Tag all nodes for evidence building
        for idx, node in enumerate(graph.as_nodes(), start=1):
            graph.tag_node(f"E{idx}", node["id"])

        return graph


def symbol_for_cin(cin: str) -> str:
    """Best-effort symbol display for a CIN (the resolver holds the real map)."""
    return cin  # placeholder; resolver would map cin->symbol


def build_graph_for_session(session_id: str, cin: str, company_name: str = "",
                            config: IndiaConfig | None = None,
                            resolver: TickerCINResolver | None = None) -> SessionGraph:
    """Convenience: build a SessionGraph for a session's company."""
    orch = IndiaOrchestrator(config=config, resolver=resolver)
    return orch.build_for_cin(cin, company_name)
