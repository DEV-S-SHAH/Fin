"""Tests for provenance tags and grading."""

from india_graph.provenance import (
    DERIVED,
    EXTERNAL,
    GAP,
    INFERRED,
    REFUSED,
    STATED,
    SUPPORTED,
    Evidence,
    Source,
    build_evidence,
    grade_answer,
    provenance_verdict,
)


def test_tags_constants():
    assert STATED == "STATED"
    assert DERIVED == "DERIVED"
    assert INFERRED == "INFERRED"
    assert EXTERNAL == "EXTERNAL"
    assert GAP == "GAP"


def test_build_evidence_from_nodes():
    nodes = [
        {"id": "n1", "type": "Company", "name": "Reliance", "cin": "L17110MH1973PLC019786", "ticker": "RELIANCE"},
        {"id": "n2", "type": "Filing", "label": "Annual Return", "form_type": "Annual Return",
         "filing_date": "2025-08-30", "fiscal_year": "2025", "source": "MCA"},
    ]
    edges = []
    evidence = build_evidence(nodes, edges)
    assert len(evidence) == 2
    assert evidence[0].tag == "E1"
    assert evidence[0].provenance == STATED
    assert evidence[0].source.cin == "L17110MH1973PLC019786"
    assert evidence[1].source.form_type == "Annual Return"


def test_grade_answer_supported():
    evidence = [
        Evidence(tag="E1", text="Revenue was 1000000 INR", source=Source(
            node_id="n1", label="Filing", ticker="RELIANCE", cin="C1", form_type="Annual Return",
            filing_date="2025-03-31", fiscal_year="2025", source="NSE"
        )),
    ]
    graded = grade_answer("Revenue was 1000000 [E1]", evidence, question="What was Reliance's revenue?")
    assert graded.verdict == SUPPORTED


def test_grade_answer_gap_no_evidence():
    graded = grade_answer("Revenue was 999999 [E1]", [], question="What was revenue?")
    assert graded.verdict == REFUSED
    assert len(graded.invented_tags) >= 1


def test_grade_answer_ungrounded_figure():
    evidence = [
        Evidence(tag="E1", text="Revenue was 1000000 INR", source=Source(
            node_id="n1", label="Filing", ticker="RELIANCE", cin="C1", source="NSE"
        )),
    ]
    graded = grade_answer("Revenue was 999999 [E1]", evidence, question="What was revenue?")
    assert graded.verdict == REFUSED
    assert "999999" in graded.ungrounded_figures


def test_grade_answer_gap_tag():
    evidence = [
        Evidence(tag="E1", text="Revenue was 1000000 INR", source=Source(
            node_id="n1", label="Filing", ticker="RELIANCE", cin="C1", source="NSE"
        )),
    ]
    # E99 was never issued
    graded = grade_answer("Revenue was 1000000 [E99]", evidence, question="What was revenue?")
    assert graded.verdict == REFUSED


def test_provenance_verdict_refused_on_gap():
    from india_graph.provenance import GradedAnswer, Verdict
    graded = GradedAnswer(
        verdicts=[
            Verdict(text="Revenue was 100 [E1]", provenance=STATED, cites=["E1"]),
            Verdict(text="Profit was unknown [E2]", provenance=GAP, cites=["E2"]),
        ],
        text="Revenue was 100 [E1]. Profit was unknown [E2].",
    )
    assert provenance_verdict(graded) == REFUSED
