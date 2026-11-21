"""Tests for deterministic evidence retrieval over the session graph."""

from india_graph.graph import SessionGraph
from india_graph.retrieval import retrieve


def _build_sample_graph():
    g = SessionGraph(cin="L17110MH1973PLC019786", company_name="Reliance Industries Ltd")
    comp = g.add_company("L17110MH1973PLC019786", "Reliance Industries Ltd")
    price = g.add_metric("NSE last price", value=2950.50, scale="INR", period="latest",
                         cin="L17110MH1973PLC019786", ticker="RELIANCE")
    revenue = g.add_metric("BSE revenue", value=3750000000000, scale="INR", period="Q2 FY2027",
                           cin="L17110MH1973PLC019786", ticker="RELIANCE")
    g.reports_metric(comp, price, value=2950.50, description="NSE last price 2950.50")
    g.reports_metric(comp, revenue, value=3750000000000, description="BSE revenue 3750000000000")
    return g


def test_retrieve_price_keyword():
    g = _build_sample_graph()
    evidence = retrieve(g, "What is RELIANCE price?", top_k=6)
    tags = [e.tag for e in evidence]
    # Price metric should be among the retrieved evidence.
    price_tags = [e.tag for e in evidence if "price" in e.text.lower()]
    assert len(price_tags) >= 1, f"price metric not retrieved: {tags}"


def test_retrieve_revenue_keyword():
    g = _build_sample_graph()
    evidence = retrieve(g, "What is RELIANCE revenue?", top_k=6)
    revenue_tags = [e.tag for e in evidence if "revenue" in e.text.lower()]
    assert len(revenue_tags) >= 1, f"revenue metric not retrieved: {[e.tag for e in evidence]}"


def test_retrieve_returns_evidence_when_no_match():
    g = _build_sample_graph()
    evidence = retrieve(g, "zzz unmatched query qqq", top_k=3)
    assert len(evidence) > 0  # falls back to graph order


def test_retrieve_respects_top_k():
    g = _build_sample_graph()
    evidence = retrieve(g, "RELIANCE", top_k=2)
    assert len(evidence) <= 2
