"""Orchestrates scraping -> graph building for one company, one session.

Called lazily on the user's first question. Scrapes NSE + BSE (announcements
and financials) and MCA (company master via CIN), normalizes into ScrapedRecord
objects, and builds the in-memory SessionGraph deterministically. Provenance on
every record is set by source: NSE/BSE/MCA = STATED.

The NSE and BSE APIs are keyed on trading *symbol*, not MCA CIN. The orchestrator
resolves cin -> symbol via the resolver before calling the exchange scrapers, and
stores the canonical cin on every record so provenance/isolation stay correct.

When live scraping returns nothing (anti-bot, no network), the orchestrator falls
back to bundled fixtures so the full graph -> evidence -> Q&A flow can be
demonstrated offline. Fixtures are clearly marked as sample data.
"""

from __future__ import annotations

from typing import Any

from .config import IndiaConfig
from .fixtures import load as load_fixture
from .graph import SessionGraph
from .provenance import build_evidence, grade_answer
from .resolver import TickerCINResolver
from .retrieval import retrieve
from .scrapers.bse import BSEScraper
from .scrapers.google_finance import GoogleFinanceScraper
from .scrapers.mca import MCAScraper
from .scrapers.nse import NSEScraper


def _record_ok(result) -> bool:
    return getattr(result, "ok", False) and bool(getattr(result, "records", []))


class IndiaOrchestrator:
    """Builds a SessionGraph for one company by scraping India sources."""

    def __init__(
        self,
        config: IndiaConfig | None = None,
        resolver: TickerCINResolver | None = None,
        use_fixtures: bool = False,
    ) -> None:
        self.config = config or IndiaConfig()
        self.resolver = resolver or TickerCINResolver(config)
        self.nse = NSEScraper(config)
        self.bse = BSEScraper(config)
        self.mca = MCAScraper(config)
        self.google = GoogleFinanceScraper(config)
        self.use_fixtures = use_fixtures

    # -- public API ---------------------------------------------------------

    def build_for_cin(self, cin: str, company_name: str = "") -> SessionGraph:
        """Scrape all sources for *cin* and build the session graph.

        When *use_fixtures* is True, live scraping is skipped entirely and
        only bundled sample fixtures are loaded — useful for offline demos
        and for keeping the graph deterministic.
        """
        graph = SessionGraph(cin=cin, company_name=company_name)
        company_node = graph.add_company(cin=cin, name=company_name, source="MCA")

        if self.use_fixtures:
            # Fixtures only — skip all live scraping (NSE, BSE, MCA).
            self._load_fixtures(graph, cin, company_name)
            self._tag_graph(graph)
            return graph

        # --- MCA: company master (STATED) ---
        mca_result = self.mca.fetch_company(cin)
        if _record_ok(mca_result):
            rec = mca_result.records[0]
            data = rec.data
            if data.get("company_name") and not company_name:
                company_name = data["company_name"]
                graph.company_name = company_name
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

        # Resolve cin -> symbol for the exchange scrapers (they key on symbol).
        symbol = self.resolver.reverse_resolve(cin)

        # --- NSE: announcements + financials (STATED) ---
        nse_ann = self.nse.fetch_announcements(symbol or cin)
        if _record_ok(nse_ann):
            for rec in nse_ann.records:
                title = rec.data.get("title", "")
                if not title:
                    continue  # skip announcements with no subject
                rec.data["cin"] = cin
                ann_node = graph.add_announcement(
                    title=title,
                    date=rec.data.get("date", ""),
                    category=rec.data.get("category", ""),
                    source_url=rec.data.get("attachment", ""),
                    source="NSE",
                )
                graph.has_announcement(company_node, ann_node)

        nse_fin = self.nse.fetch_financials(symbol or cin)
        if _record_ok(nse_fin):
            for rec in nse_fin.records:
                rec.data["cin"] = cin
                data = rec.data
                # Use the record's actual source (NSE or Yahoo Finance fallback)
                # so provenance stays honest — Yahoo Finance data is EXTERNAL,
                # not NSE STATED.
                src = rec.source or "NSE"

                # --- Last price metric ---
                if data.get("last_price") is not None:
                    metric_node = graph.add_metric(
                        metric_name=f"{src} {data.get('company_name', 'quote')} last price",
                        value=data.get("last_price"),
                        scale="INR",
                        unit="INR",
                        period="latest",
                        cin=cin,
                        ticker=symbol or "",
                        source=src,
                    )
                    graph.reports_metric(
                        company_node,
                        metric_node,
                        value=data.get("last_price"),
                        description=(
                            f"{src} last price {data.get('last_price')} for "
                            f"{data.get('company_name', symbol or cin)}"
                        ),
                    )

                # --- Revenue metric (quarterly, since Indian cos report quarterly) ---
                revenue = data.get("revenue")
                if revenue is not None:
                    period = data.get("revenue_period") or "latest"
                    metric_node = graph.add_metric(
                        metric_name=f"{src} {data.get('company_name', 'company')} revenue",
                        value=revenue,
                        scale="INR",
                        unit="INR",
                        period=period,
                        cin=cin,
                        ticker=symbol or "",
                        source=src,
                    )
                    graph.reports_metric(
                        company_node,
                        metric_node,
                        value=revenue,
                        description=(
                            f"{src} revenue {revenue} for {data.get('company_name', symbol or cin)} "
                            f"({period})"
                        ),
                    )

        # --- BSE: announcements + financials (STATED) ---
        bse_ann = self.bse.fetch_announcements(symbol or cin)
        if _record_ok(bse_ann):
            for rec in bse_ann.records:
                title = rec.data.get("title", "")
                if not title:
                    continue  # skip announcements with no heading
                rec.data["cin"] = cin
                ann_node = graph.add_announcement(
                    title=title,
                    date=rec.data.get("date", ""),
                    category=rec.data.get("category", ""),
                    source_url=rec.data.get("attachment", ""),
                    source="BSE",
                )
                graph.has_announcement(company_node, ann_node)

        bse_fin = self.bse.fetch_financials(symbol or cin)
        if _record_ok(bse_fin):
            for rec in bse_fin.records:
                rec.data["cin"] = cin
                data = rec.data
                value = data.get("revenue") or data.get("profit_after_tax")
                if value is None:
                    continue  # skip valueless financials
                metric_node = graph.add_metric(
                    metric_name=f"BSE {data.get('company_name', 'financials')}",
                    value=value,
                    scale="INR",
                    unit="INR",
                    period=data.get("period", ""),
                    fiscal_year=data.get("fiscal_year", ""),
                    cin=cin,
                    ticker=symbol or "",
                )
                graph.reports_metric(
                    company_node,
                    metric_node,
                    value=value,
                    description=(
                        f"BSE {data.get('period', '')} revenue "
                        f"{data.get('revenue')} PAT {data.get('profit_after_tax')}"
                    ),
                )

        # --- Google Finance: price + revenue (EXTERNAL provenance) ---
        # Fallback when NSE is blocked and BSE has no financials.
        gf_result = self.google.fetch_financials(symbol or cin)
        if _record_ok(gf_result):
            for rec in gf_result.records:
                rec.data["cin"] = cin
                data = rec.data
                src = rec.source or "Google Finance"

                # Last price metric
                if data.get("last_price") is not None:
                    metric_node = graph.add_metric(
                        metric_name=f"{src} {data.get('company_name', 'quote')} last price",
                        value=data.get("last_price"),
                        scale="INR",
                        unit="INR",
                        period="latest",
                        cin=cin,
                        ticker=symbol or "",
                        source=src,
                    )
                    graph.reports_metric(
                        company_node,
                        metric_node,
                        value=data.get("last_price"),
                        description=(
                            f"{src} last price {data.get('last_price')} for "
                            f"{data.get('company_name', symbol or cin)}"
                        ),
                    )

                # Revenue metric
                revenue = data.get("revenue")
                if revenue is not None:
                    period = data.get("revenue_period") or "latest"
                    metric_node = graph.add_metric(
                        metric_name=f"{src} {data.get('company_name', 'company')} revenue",
                        value=revenue,
                        scale="INR",
                        unit="INR",
                        period=period,
                        cin=cin,
                        ticker=symbol or "",
                        source=src,
                    )
                    graph.reports_metric(
                        company_node,
                        metric_node,
                        value=revenue,
                        description=(
                            f"{src} revenue {revenue} for {data.get('company_name', symbol or cin)} "
                            f"({period})"
                        ),
                    )

        # --- Offline fallback: bundled fixtures (sample data only) ---
        # Load fixtures when live scraping returned no announcements or metrics
        # (graph holds only the Company node).
        ann_count = len(graph.find_announcements())
        metric_count = len(graph.find_metrics())
        if ann_count == 0 and metric_count == 0:
            self._load_fixtures(graph, cin, company_name)

        self._tag_graph(graph)
        return graph

    def _tag_graph(self, graph: SessionGraph) -> None:
        """Tag all nodes for evidence building."""
        for idx, node in enumerate(graph.as_nodes(), start=1):
            graph.tag_node(f"E{idx}", node["id"])

    # -- fixtures -----------------------------------------------------------

    def _load_fixtures(self, graph: SessionGraph, cin: str, company_name: str) -> None:
        """Load bundled sample data when live scraping returned nothing.

        Fixtures are clearly sample data — they let the graph, evidence and
        Q&A flow be demonstrated offline without hitting NSE/BSE/MCA.
        """
        payload = load_fixture(cin)
        if payload is None:
            return

        # Company master
        master = payload.get("mca_company", {})
        if master:
            graph.graph.nodes[graph.find_company_node() or graph.add_company(cin, company_name)].update({
                "sector": master.get("sector", ""),
                "industry": master.get("industry", ""),
                "date_of_incorporation": master.get("date_of_incorporation", ""),
                "registered_office_address": master.get("registered_office_address", ""),
                "source": "MCA (fixture)",
            })

        # Announcements
        for ann in payload.get("announcements", []):
            ann_node = graph.add_announcement(
                title=ann.get("title", ""),
                date=ann.get("date", ""),
                category=ann.get("category", ""),
                source_url=ann.get("attachment", ""),
                source=ann.get("source", "NSE"),
            )
            comp = graph.find_company_node()
            if comp:
                graph.has_announcement(comp, ann_node)

        # Financial metrics
        for fin in payload.get("financials", []):
            metric_node = graph.add_metric(
                metric_name=fin.get("metric_name", ""),
                value=fin.get("value"),
                scale=fin.get("scale", "INR"),
                unit=fin.get("unit", "INR"),
                period=fin.get("period", ""),
                fiscal_year=fin.get("fiscal_year", ""),
                cin=cin,
                ticker=fin.get("ticker", ""),
            )
            comp = graph.find_company_node()
            if comp:
                graph.reports_metric(
                    comp, metric_node,
                    value=fin.get("value"),
                    description=fin.get("description", ""),
                )

    # -- Q&A ---------------------------------------------------------------

    def answer(self, graph: SessionGraph, question: str) -> dict[str, Any]:
        """Produce a deterministic, provenance-grounded answer for *question*.

        Retrieves relevant evidence from the graph, renders a short answer,
        and grades it. Returns a dict suitable for the API response.
        """
        evidence = retrieve(graph, question)
        nodes = graph.as_nodes()
        edges = graph.as_edges()
        tag_map = graph.tag_map()
        # Rebuild full evidence for grading context (all nodes, not just retrieved).
        full_evidence = build_evidence(nodes, edges, tag_map)

        answer_parts: list[str] = []
        citations = []

        # Determine whether the graph has any citable facts beyond the
        # Company node (announcements or financial metrics). If not, there
        # is genuinely no data to answer with — report GAP clearly instead
        # of just echoing the company name.
        ann_count = len(graph.find_announcements())
        metric_count = len(graph.find_metrics())
        has_facts = ann_count > 0 or metric_count > 0

        if evidence and has_facts:
            answer_parts.append(f"Here's what I found for {cin_label(graph)}:")
            for ev in evidence:
                answer_parts.append(f"• [{ev.tag}] {ev.text}  ⟵ {ev.source.cite()}")
                citations.append({
                    "tag": ev.tag,
                    "text": ev.text,
                    "source": ev.source.cite(),
                })
        else:
            answer_parts.append(
                f"No citable financial data found yet for {cin_label(graph)} ({graph.cin}). "
                "This is reported as GAP — no figures were fabricated."
            )
            citations.append({"tag": "GAP", "text": "No citable evidence found.", "source": ""})

        answer = "\n".join(answer_parts)
        graded = grade_answer(answer, full_evidence, question=question)

        return {
            "answer": answer,
            "verdict": graded.verdict,
            "citations": citations,
            "evidence": [
                {
                    "tag": ev.tag,
                    "text": ev.text,
                    "provenance": ev.provenance,
                    "source": ev.source.cite(),
                    "kind": ev.kind,
                }
                for ev in full_evidence
            ],
            "violations": graded.violations(),
        }


def cin_label(graph: SessionGraph) -> str:
    """A display label for the graph's company."""
    return graph.company_name or graph.cin


def build_graph_for_session(session_id: str, cin: str, company_name: str = "",
                            config: IndiaConfig | None = None,
                            resolver: TickerCINResolver | None = None,
                            use_fixtures: bool = False) -> SessionGraph:
    """Convenience: build a SessionGraph for a session's company."""
    orch = IndiaOrchestrator(config=config, resolver=resolver, use_fixtures=use_fixtures)
    return orch.build_for_cin(cin, company_name)
