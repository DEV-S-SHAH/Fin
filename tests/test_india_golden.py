"""Golden-query end-to-end tests for the India Company Graph.

No real network — scrapers are stubbed to return fixture records, verifying
the full flow: resolve CIN -> build graph -> gather evidence -> grade answer.
"""

from india_graph.config import IndiaConfig
from india_graph.graph import SessionGraph
from india_graph.orchestrator import IndiaOrchestrator
from india_graph.provenance import REFUSED, SUPPORTED, build_evidence, grade_answer
from india_graph.resolver import TickerCINResolver


def test_golden_reliance_price():
    """User asks: what is RELIANCE's price? Graph built from NSE, answer graded."""
    config = IndiaConfig()
    resolver = TickerCINResolver(config)
    resolver.register("RELIANCE", "L17110MH1973PLC019786")

    # Build graph manually (orchestrator calls scrapers in production)
    graph = SessionGraph(cin="L17110MH1973PLC019786", company_name="Reliance Industries Ltd")
    comp = graph.add_company("L17110MH1973PLC019786", "Reliance Industries Ltd")
    metric = graph.add_metric("NSE last price", value=2950.50, scale="INR", period="latest",
                              cin="L17110MH1973PLC019786", ticker="RELIANCE")
    graph.reports_metric(comp, metric, value=2950.50, description="NSE last price 2950.50")

    # Gather evidence
    nodes = graph.as_nodes()
    edges = graph.as_edges()
    tag_map = graph.tag_map()
    evidence = build_evidence(nodes, edges, tag_map)

    # Price lives on the Metric node (E2), Company is E1
    # Grade a correct answer citing the metric node
    graded = grade_answer("Reliance's price is 2950.50 [E2]", evidence, question="What is RELIANCE's price?")
    assert graded.verdict == SUPPORTED

    # Grade a wrong figure
    graded2 = grade_answer("Reliance's price is 999999 [E2]", evidence, question="What is RELIANCE's price?")
    assert graded2.verdict == REFUSED


def test_golden_no_data_gap():
    """No evidence for a company -> GAP -> REFUSED."""
    graph = SessionGraph(cin="L99999", company_name="Unknown Co")
    evidence = build_evidence(graph.as_nodes(), graph.as_edges(), graph.tag_map())
    graded = grade_answer("Revenue was 100 [E1]", evidence, question="What was revenue?")
    assert graded.verdict == REFUSED


def test_golden_fixture_graph_build():
    """Offline fixture build yields a graph with Company + announcements + metrics."""
    config = IndiaConfig()
    resolver = TickerCINResolver(config)
    resolver.register("RELIANCE", "L17110MH1973PLC019786")
    orch = IndiaOrchestrator(config=config, resolver=resolver, use_fixtures=True)
    graph = orch.build_for_cin("L17110MH1973PLC019786", "Reliance Industries Ltd")

    nodes = graph.as_nodes()
    edges = graph.as_edges()
    types = {n["type"] for n in nodes}
    assert "Company" in types
    assert "Announcement" in types
    assert "FinancialMetric" in types
    assert len(edges) >= 1
    # Company node carries the CIN
    comp = next(n for n in nodes if n["type"] == "Company")
    assert comp["cin"] == "L17110MH1973PLC019786"


def test_golden_fixture_qa_supported():
    """Fixture-based Q&A returns a grounded, SUPPORTED answer."""
    config = IndiaConfig()
    resolver = TickerCINResolver(config)
    resolver.register("RELIANCE", "L17110MH1973PLC019786")
    orch = IndiaOrchestrator(config=config, resolver=resolver, use_fixtures=True)
    graph = orch.build_for_cin("L17110MH1973PLC019786", "Reliance Industries Ltd")

    result = orch.answer(graph, "What is RELIANCE price?")
    assert result["verdict"] == SUPPORTED
    assert "2950.50" in result["answer"]
    assert result["violations"] == []

