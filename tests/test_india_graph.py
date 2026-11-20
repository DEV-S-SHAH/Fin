"""Tests for the in-memory SessionGraph."""

from india_graph.graph import SessionGraph


def test_add_company_and_metric():
    g = SessionGraph(cin="L17110MH1973PLC019786", company_name="Reliance Industries")
    comp = g.add_company("L17110MH1973PLC019786", "Reliance Industries", sector="Energy")
    filing = g.add_filing("Annual Return", "2025-08-30", fiscal_year="2025")
    metric = g.add_metric("Revenue", value=1000000, scale="INR", period="FY2025")

    g.submit(comp, filing)
    g.reports_metric(filing, metric, value=1000000, description="Revenue FY2025")

    assert g.find_company_node() == comp
    metrics = g.find_metrics("Revenue")
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "Revenue"
    filings = g.find_filings()
    assert len(filings) == 1


def test_add_announcement():
    g = SessionGraph(cin="C1", company_name="Test Co")
    comp = g.add_company("C1", "Test Co")
    ann = g.add_announcement("Board meeting", date="2025-11-01", category="Board", source="NSE")
    g.has_announcement(comp, ann)
    anns = g.find_announcements()
    assert len(anns) == 1
    assert anns[0]["title"] == "Board meeting"


def test_nodes_and_edges():
    g = SessionGraph(cin="C1", company_name="Test Co")
    comp = g.add_company("C1", "Test Co")
    filing = g.add_filing("10-K", "2025-03-31")
    g.submit(comp, filing)

    nodes = g.as_nodes()
    edges = g.as_edges()
    assert len(nodes) == 2
    assert len(edges) == 1
    assert edges[0]["rel"] == "SUBMITTED"
    assert edges[0]["source"] == comp
    assert edges[0]["target"] == filing


def test_tag_map():
    g = SessionGraph(cin="C1", company_name="Test Co")
    n1 = g.add_company("C1", "Test Co")
    g.tag_node("E1", n1)
    assert g.tag_map() == {"E1": n1}
