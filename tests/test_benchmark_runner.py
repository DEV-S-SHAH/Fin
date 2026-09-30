"""Unit tests for the 50-query benchmark harness itself.

A benchmark that cannot detect a broken score is worse than no benchmark: it
reports green while the system regresses. These tests pin the scoring rules to
known-good answers on a three-case synthetic fixture -- one clean pass, one
Apple-context leak, one insufficient hop depth -- and assert the exact
percentages, not merely that nothing raised.

The dataset's own 50 cases are validated separately in
``TestGoldenDatasetIntegrity``; the fixture here is hand-built so the expected
numbers are obvious by inspection.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from benchmarks.eval_50_queries import (
    BenchmarkCheckpoint,
    LiveHarness,
    MockHarness,
    QueryOutcome,
    RunReport,
    TARGETS,
    _InterruptFlag,
    _install_signal_handlers,
    _restore_signal_handlers,
    evaluate_case,
    find_terms,
    is_transport_error,
    main,
    max_path_depth,
    render_report,
    run_benchmark,
    score_text,
    ungrounded_entities,
)
from benchmarks.queries_50 import APPLE_LEAK_TERMS, BenchmarkCase, cases_for_category

#: Two-edge chain: JPM -> supplier -> foundry, matching what the real
#: traverser emits for a cold-start company with one second-order dependency.
_PATH_2HOP = [
    [
        {"source_id": "jpm", "target_id": "sup", "relation": "SOURCES_FROM"},
        {"source_id": "sup", "target_id": "fdy", "relation": "SOURCES_FROM"},
    ]
]

_PATH_1HOP = [
    [{"source_id": "jpm", "target_id": "sup", "relation": "SOURCES_FROM"}]
]

_LEDGER = (
    "[JPM] --(SOURCES_FROM: depends on a limited number of suppliers)--> "
    "[ADVANCED PACKAGING SUPPLIER]\n"
    "[ADVANCED PACKAGING SUPPLIER] --(SOURCES_FROM: single foundry partner)--> "
    "[LEADING EDGE FOUNDRY PARTNER]\n"
)

# A clean answer: every entity it names is in the ledger above.
_CLEAN_ANSWER = (
    "1. Executive Summary & Thesis\n"
    "JPM sources from ADVANCED PACKAGING SUPPLIER, which in turn sources from "
    "LEADING EDGE FOUNDRY PARTNER.\n"
    "2. Direct Dependencies (1-hop)\n"
    "Supply concentration is the filed risk.\n"
)

# The failure this suite exists to catch: a JPMorgan answer built from Apple's
# context, which is what silently substituting a default issuer looks like.
_LEAKED_ANSWER = (
    "1. Executive Summary & Thesis\n"
    "JPM competes with Apple, whose iPhone and Mac revenue dominate the segment. "
    "Supply concentration remains the filed risk.\n"
)


def _cases() -> list[BenchmarkCase]:
    """Three synthetic cases: clean, leaked, and too-shallow."""
    return [
        BenchmarkCase(
            id="T1",
            category="cold_start",
            query="What does JPM source from?",
            expected_route="COLD_START",
            expected_ticker="JPM",
            forbidden_terms=[],
            required_concepts=["Supply concentration"],
            min_hops=2,
        ),
        BenchmarkCase(
            id="T2",
            category="cold_start",
            query="What does JPM compete with?",
            expected_route="COLD_START",
            expected_ticker="JPM",
            forbidden_terms=[],
            required_concepts=[],
            min_hops=1,
        ),
        BenchmarkCase(
            id="T3",
            category="contagion",
            query="How does a supply shock reach JPM?",
            expected_route="COLD_START",
            expected_ticker="JPM",
            forbidden_terms=[],
            required_concepts=[],
            min_hops=2,
        ),
    ]


def _raw(
    *,
    route: str = "COLD_START",
    ticker: str | None = "JPM",
    answer: str = _CLEAN_ANSWER,
    provenance: str = _LEDGER,
    paths: list | None = None,
    evidence: str = "",
    latency_ms: dict | None = None,
) -> dict:
    return {
        "route": route,
        "ticker": ticker,
        "answer": answer,
        "provenance": provenance,
        "evidence_corpus": evidence or provenance,
        "paths": _PATH_2HOP if paths is None else paths,
        "latency_ms": latency_ms if latency_ms is not None
        else {"routing_ms": 1.0, "total_ms": 10.0},
    }


# ── Scoring rules ────────────────────────────────────────────────────────────


class TestScoringRules(unittest.TestCase):
    def test_clean_answer_passes_every_metric(self):
        case = _cases()[0]
        outcome = evaluate_case(case, _raw())

        self.assertTrue(outcome.routing_ok, "route and ticker both correct")
        self.assertTrue(outcome.isolation_ok, outcome.leaked_terms)
        self.assertTrue(outcome.hops_ok)
        self.assertTrue(outcome.groundedness_ok, outcome.ungrounded)
        self.assertTrue(outcome.concept_ok, outcome.missing_concepts)
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.path_depth, 2)
        self.assertEqual(outcome.failures(), [])

    def test_apple_context_leak_is_caught_on_a_non_apple_case(self):
        """The anti-leak invariant: a JPM answer carrying Apple's context fails."""
        case = _cases()[1]
        outcome = evaluate_case(case, _raw(answer=_LEAKED_ANSWER))

        self.assertFalse(outcome.isolation_ok, "leak must be detected")
        self.assertFalse(outcome.passed)
        self.assertIn("iPhone", outcome.leaked_terms)
        self.assertIn("Mac", outcome.leaked_terms)
        self.assertIn("Apple", outcome.leaked_terms)
        self.assertTrue(
            any("context leak" in f for f in outcome.failures()),
            outcome.failures(),
        )

    def test_apple_accession_in_a_non_apple_answer_is_a_leak(self):
        """An Apple filing identifier is Apple context, however it is phrased.

        Citing ``0000320193-25-000079`` in a JPM answer leaks the same filing
        that saying "iPhone" would, so it must fail isolation on its own -- with
        no product name present to trip the term check.
        """
        case = _cases()[1]
        answer = (
            "Supply concentration sits with the filed supplier; the 10-K "
            "accession 0000320193-25-000079 describes it."
        )
        outcome = evaluate_case(case, _raw(answer=answer))

        self.assertEqual(outcome.leaked_terms, [], "no product name to match")
        self.assertEqual(outcome.apple_accessions, ["0000320193-25-000079"])
        self.assertFalse(outcome.isolation_ok)
        self.assertFalse(outcome.passed)
        self.assertTrue(
            any("Apple accession leak" in f for f in outcome.failures()),
            outcome.failures(),
        )

    def test_apple_accession_is_allowed_in_an_apple_answer(self):
        """The check is about isolation, not a blanket ban on the CIK."""
        apple = BenchmarkCase(
            id="A9", category="seeded_multi_hop", query="What is AAPL's risk?",
            expected_route="KNOWN", expected_ticker="AAPL",
            forbidden_terms=[], required_concepts=[], min_hops=1,
        )
        outcome = evaluate_case(
            apple, _raw(answer="Filed under 0000320193-25-000079 with no peer context.")
        )
        self.assertEqual(outcome.apple_accessions, [])
        self.assertTrue(outcome.isolation_ok)

    def test_a_peer_accession_is_not_flagged_as_a_leak(self):
        """Comparing filings across issuers is the job, not a bug.

        Only Apple's own CIK prefix is treated as leaked context. Flagging
        every accession would fail the contagion and transition cases, which
        legitimately reason across companies.
        """
        case = _cases()[1]
        answer = "Peer filings 0000789019-25-000010 and 0000320193-24-000106 were compared."
        outcome = evaluate_case(case, _raw(answer=answer))
        self.assertEqual(outcome.apple_accessions, ["0000320193-24-000106"])

        peer_only = evaluate_case(case, _raw(answer="Peer filing 0000789019-25-000010 was compared."))
        self.assertEqual(peer_only.apple_accessions, [])
        self.assertTrue(peer_only.isolation_ok, peer_only.leaked_terms)

    def test_accession_detection_does_not_fire_inside_a_longer_number(self):
        """Digit guards, so a figure that merely contains the CIK is not a leak."""
        case = _cases()[1]
        outcome = evaluate_case(
            case, _raw(answer="Segment revenue was 100003201935 across the period.")
        )
        self.assertEqual(outcome.apple_accessions, [])
        self.assertTrue(outcome.isolation_ok)

    def test_apple_terms_are_not_forbidden_for_an_apple_case(self):
        """AAPL-targeted cases must be able to discuss iPhone and Mac."""
        case = BenchmarkCase(
            id="T4",
            category="seeded_multi_hop",
            query="Apple iPhone supply chain?",
            expected_route="KNOWN",
            expected_ticker="AAPL",
            forbidden_terms=[],
            required_concepts=[],
            min_hops=1,
        )
        answer = "iPhone and Mac revenue drive the segment; Apple is the issuer."
        scored = score_text(case, answer, _LEDGER)

        self.assertTrue(scored["isolation_ok"], scored["leaked_terms"])
        self.assertEqual(scored["leaked_terms"], [])

    def test_leak_matching_respects_word_boundaries(self):
        """A substring match would flag "macro" for "Mac" and "Applesauce"."""
        self.assertEqual(find_terms("macro headwind and MacBook", ["Mac"]), [])
        self.assertEqual(find_terms("Applesauce revenue", ["Apple"]), [])
        self.assertEqual(find_terms("Apple's supply chain", ["Apple"]), ["Apple"])
        self.assertEqual(find_terms("Mac revenue fell", ["Mac"]), ["Mac"])

    def test_insufficient_hop_depth_fails(self):
        case = _cases()[2]
        outcome = evaluate_case(case, _raw(paths=_PATH_1HOP))

        self.assertEqual(outcome.path_depth, 1)
        self.assertFalse(outcome.hops_ok)
        self.assertFalse(outcome.passed)
        self.assertTrue(any("hop depth" in f for f in outcome.failures()))

    def test_routing_mismatch_names_both_route_and_ticker(self):
        case = _cases()[0]
        outcome = evaluate_case(case, _raw(route="KNOWN", ticker="AAPL"))

        self.assertFalse(outcome.routing_ok)
        reasons = " ".join(outcome.failures())
        self.assertIn("COLD_START expected, got KNOWN", reasons)
        self.assertIn("'JPM' expected, got 'AAPL'", reasons)

    def test_error_in_raw_is_recorded_not_raised(self):
        """One broken case must not abort the other 49."""
        case = _cases()[0]
        outcome = evaluate_case(case, {"error": "HTTP 500", "route": "ERROR"})

        self.assertEqual(outcome.error, "HTTP 500")
        self.assertFalse(outcome.passed)
        self.assertTrue(any("error" in f for f in outcome.failures()))

    def test_missing_keys_score_as_failure_not_exception(self):
        case = _cases()[0]
        outcome = evaluate_case(case, {})

        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.actual_route, "UNKNOWN")


# ── Groundedness ─────────────────────────────────────────────────────────────


class TestGroundedness(unittest.TestCase):
    def test_entity_in_ledger_is_grounded(self):
        self.assertEqual(
            ungrounded_entities("JPM uses ADVANCED PACKAGING SUPPLIER.", _LEDGER), []
        )

    def test_entity_absent_from_evidence_is_flagged(self):
        found = ungrounded_entities("Revenue is driven by Zorblax Systems.", _LEDGER)
        self.assertIn("Zorblax Systems", found)

    def test_report_headings_are_not_entities(self):
        """Headings would otherwise be scored as hallucinated entities."""
        answer = (
            "1. Executive Summary & Thesis\n"
            "2. Direct Dependencies (1-hop)\n"
            "3. Second-Order Contagion (Supply Chain / Competitors / Key Talent)\n"
            "4. Capital Allocation & Margin Outlook\n"
            "5. Verifiable Evidence Chain\n"
        )
        self.assertEqual(ungrounded_entities(answer, _LEDGER), [])

    def test_newline_does_not_splice_a_phantom_entity(self):
        """A heading tail plus a sentence head is not one entity."""
        answer = "Verifiable Evidence Chain\nJPM sources from a supplier."
        found = ungrounded_entities(answer, _LEDGER)
        self.assertNotIn("Chain JPM", found)

    def test_tolerance_allows_two_but_not_three(self):
        from benchmarks.eval_50_queries import GROUNDEDNESS_MAX_UNGROUNDED

        self.assertEqual(GROUNDEDNESS_MAX_UNGROUNDED, 2)
        case = _cases()[0]
        two = "Risk sits with Zorblax Systems and Quibbler Holdings."
        three = two + " Also Yaxley Ventures."

        ok = score_text(case, two, _LEDGER)
        bad = score_text(case, three, _LEDGER)
        self.assertEqual(len(ok["ungrounded"]), 2, ok["ungrounded"])
        self.assertTrue(ok["groundedness_ok"], ok["ungrounded"])
        self.assertEqual(len(bad["ungrounded"]), 3, bad["ungrounded"])
        self.assertFalse(bad["groundedness_ok"], bad["ungrounded"])

    def test_refusal_is_not_penalised_for_saying_nothing(self):
        """A negative control that refuses must not fail on missing concepts."""
        case = BenchmarkCase(
            id="T5",
            category="negative_control",
            query="How do I bake a cake?",
            expected_route="AMBIGUOUS",
            expected_ticker=None,
            forbidden_terms=[],
            required_concepts=["revenue"],
            min_hops=2,
            expect_refusal=True,
        )
        outcome = evaluate_case(
            case,
            _raw(
                route="AMBIGUOUS",
                ticker=None,
                answer="Please specify a valid company name or stock ticker.",
                provenance="",
                paths=[],
            ),
        )

        self.assertTrue(outcome.refusal)
        self.assertTrue(outcome.concept_ok, outcome.missing_concepts)
        self.assertTrue(outcome.groundedness_ok, outcome.ungrounded)
        self.assertTrue(outcome.hops_ok, "a refusal legitimately traverses nothing")
        self.assertTrue(outcome.passed)


# ── Aggregation arithmetic ───────────────────────────────────────────────────


class TestAggregation(unittest.TestCase):
    def test_three_case_fixture_scores_exactly(self):
        """Pin the arithmetic: only the clean case passes, and for one reason each.

        T1 clean, T2 Apple leak, T3 too shallow -- so exactly one case passes.
        Isolation and hop depth each fail on a different case, which is what
        makes them independent metrics rather than restatements of "passed".
        """
        cases = _cases()
        raws = {
            "T1": _raw(),                       # clean
            "T2": _raw(answer=_LEAKED_ANSWER),  # leaks Apple
            "T3": _raw(paths=_PATH_1HOP),       # too shallow
        }
        report = run_benchmark(cases, _raw_harness(raws))
        scores = report.scores()

        self.assertEqual(len(report.outcomes), 3)
        self.assertEqual(scores["routing_accuracy"], 100.0)
        self.assertEqual(scores["isolation_score"], 200.0 / 3)
        self.assertEqual(scores["multi_hop_reachability"], 200.0 / 3)
        self.assertEqual(scores["cold_start_success"], 100.0 / 3)
        self.assertEqual(report.overall_pass_rate(), 100.0 / 3)

        passed = [o.case_id for o in report.outcomes if o.passed]
        self.assertEqual(passed, ["T1"])

    def test_percent_of_empty_subset_is_zero_not_crash(self):
        report = RunReport(mode="mock-llm")
        self.assertEqual(report.scores()["routing_accuracy"], 0.0)
        self.assertEqual(report.percent([], lambda o: True), 0.0)

    def test_mock_mode_reports_content_metric_as_not_applicable(self):
        """A metric the harness cannot measure must not read as a failure."""
        report = run_benchmark(_cases(), _raw_harness({}, measures_content=False))
        self.assertFalse(report.measures_content)
        self.assertNotIn("groundedness", report.applicable_targets())
        self.assertIn("routing_accuracy", report.applicable_targets())
        self.assertIn("n/a", render_report(report))

    def test_mock_mode_ignores_content_metrics_in_pass_rate(self):
        """Otherwise a fixed mock answer would fail every case, always."""
        cases = _cases()
        # Missing every required concept: a content failure, and only that.
        raw = _raw(answer="Nothing relevant here.")
        report = run_benchmark(
            cases, _raw_harness({c.id: raw for c in cases}, measures_content=False)
        )

        self.assertFalse(report.measures_content)
        for outcome in report.outcomes:
            self.assertTrue(outcome.concept_ok, "content parked green in mock mode")
            self.assertTrue(
                outcome.missing_concepts == ["Supply concentration"]
                or outcome.missing_concepts == [],
                outcome.missing_concepts,
            )
        # T1 misses "Supply concentration" yet still passes, because in mock
        # mode the concept check is not a measurement.
        self.assertEqual(
            [o.case_id for o in report.outcomes if o.passed], ["T1", "T2", "T3"]
        )

    def test_content_metrics_still_gate_in_content_scoring_mode(self):
        """The n/a path must not quietly disable the check everywhere."""
        cases = _cases()
        raw = _raw(answer="Nothing relevant here.")
        report = run_benchmark(cases, _raw_harness({c.id: raw for c in cases}))

        self.assertTrue(report.measures_content)
        t1 = next(o for o in report.outcomes if o.case_id == "T1")
        self.assertFalse(t1.concept_ok)
        self.assertFalse(t1.passed)


# ── Path depth and SSE reconstruction ────────────────────────────────────────


class TestPathDepth(unittest.TestCase):
    def test_depth_is_longest_path_not_path_count(self):
        """A 1-hop fan-out of 12 is depth 1, not 12."""
        fanout = [[{"source_id": "a", "target_id": f"n{i}"}] for i in range(12)]
        self.assertEqual(max_path_depth(fanout), 1)
        self.assertEqual(max_path_depth(_PATH_2HOP), 2)

    def test_empty_traversal_is_depth_zero(self):
        self.assertEqual(max_path_depth([]), 0)
        self.assertEqual(max_path_depth([[]]), 0)


# ── Report rendering ─────────────────────────────────────────────────────────


class TestReportRendering(unittest.TestCase):
    def test_report_renders_all_required_sections(self):
        cases = _cases()
        raws = {
            "T1": _raw(),
            "T2": _raw(answer=_LEAKED_ANSWER),
            "T3": _raw(paths=_PATH_1HOP),
        }
        report = run_benchmark(cases, _raw_harness(raws))
        md = render_report(report)

        for heading in (
            "## 1. Executive Scorecard",
            "## 2. Category Breakdown",
            "## 3. Latency Distribution",
            "## 4. Per-Query Results",
            "## 5. Failure Log",
        ):
            self.assertIn(heading, md)

    def test_failure_log_names_the_failing_ids_and_reasons(self):
        cases = _cases()
        raws = {
            "T1": _raw(),
            "T2": _raw(answer=_LEAKED_ANSWER),
            "T3": _raw(paths=_PATH_1HOP),
        }
        md = render_report(run_benchmark(cases, _raw_harness(raws)))
        failure_section = md.split("## 5. Failure Log", 1)[1]

        self.assertIn("T2", failure_section)
        self.assertIn("T3", failure_section)
        self.assertNotIn("| T1 |", failure_section, "passing case is not a failure")
        self.assertIn("context leak", failure_section)
        self.assertIn("hop depth", failure_section)

    def test_pure_negative_control_category_renders_n_a_not_zero(self):
        """An empty denominator must not print as a total failure."""
        case = BenchmarkCase(
            id="N1",
            category="negative_control",
            query="How do I bake a cake?",
            expected_route="AMBIGUOUS",
            expected_ticker=None,
            forbidden_terms=[],
            required_concepts=[],
            min_hops=1,
            expect_refusal=True,
        )
        report = run_benchmark([case], _raw_harness({}))
        md = render_report(report)

        self.assertIn("n/a", md)
        self.assertIn("100%", md, "the case itself passed")

    def test_unmeasured_hop_depth_reports_n_a_not_zero(self):
        """A server that sends no ``graph_metrics`` reads as uninstrumented."""
        cases = _cases()
        raws = {c.id: _raw(paths=[]) for c in cases}
        report = run_benchmark(cases, _raw_harness(raws))
        self.assertFalse(report.measures_depth())
        self.assertNotIn(
            "multi_hop_reachability", report.applicable_targets(),
            "no depth was measured, so reachability is not reportable",
        )
        md = render_report(report)
        self.assertIn("not reported by the server", md)
        self.assertIn("50", md)  # the case count in the surfaced note

    def test_latency_table_reports_observed_percentiles(self):
        report = run_benchmark(_cases(), _raw_harness({c.id: _raw() for c in _cases()}))
        md = render_report(report)

        self.assertIn("P50 (ms)", md)
        self.assertIn("P99 (ms)", md)
        self.assertIn("total_ms", md)

    def test_category_table_breaks_out_latency(self):
        """Per-category latency, so a slow category is visible as its own row.

        A single global percentile averages a cold-start fetch together with a
        multi-hop traversal, which hides which one is slow.
        """
        cases = _cases()
        raws = {}
        for c in cases:
            # Give each category a distinct total so the columns are checkable.
            base = {"T1": 10.0, "T2": 20.0, "T3": 30.0}[c.id]
            raws[c.id] = _raw(latency_ms={"total_ms": base, "routing_ms": 1.0})
        report = run_benchmark(cases, _raw_harness(raws))
        md = render_report(report)

        self.assertIn("P50 total (ms)", md)
        self.assertIn("P95 total (ms)", md)

        by_cat = report.category_latency()
        self.assertEqual(by_cat, {c.category: {"T1": 10.0, "T2": 20.0, "T3": 30.0}[c.id]
                                  for c in cases})
        # Each category here is a single case, so P50 and P95 agree exactly.
        for category, value in by_cat.items():
            self.assertIn(f"| {value:.1f} | {value:.1f} |", md, category)

    def test_category_latency_is_n_a_when_no_case_recorded_a_total(self):
        """A harness that times nothing must not print a fake 0.0 ms."""
        cases = _cases()
        report = run_benchmark(
            cases, _raw_harness({c.id: _raw(latency_ms={}) for c in cases})
        )
        self.assertEqual(report.category_latency(), {})
        md = render_report(report)
        self.assertIn("n/a | n/a |", md)


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestCli(unittest.TestCase):
    def test_category_filter_runs_and_writes_report(self, ):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "report.md"
            code = main(
                ["--mock-llm", "--category", "negative_control", "--output", str(out)]
            )

            self.assertEqual(code, 0)
            self.assertTrue(out.exists(), "parent dirs are created")
            md = out.read_text(encoding="utf-8")
            self.assertIn("Cases:** 5", md)

    def test_mode_is_required(self):
        with self.assertRaises(SystemExit):
            main(["--output", "/tmp/should_not_exist.md"])

    def test_mock_flag_and_live_flag_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            main(["--mock-llm", "--live"])

    def test_unknown_category_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            main(["--mock-llm", "--category", "not_a_category"])


# ── Dataset integrity ────────────────────────────────────────────────────────


class TestGoldenDatasetIntegrity(unittest.TestCase):
    def test_fifty_unique_cases_across_five_categories(self):
        from benchmarks.queries_50 import CATEGORIES, CASES

        self.assertEqual(len(CASES), 50)
        self.assertEqual(len({c.id for c in CASES}), 50)
        counts = {c: sum(1 for x in CASES if x.category == c) for c in CATEGORIES}
        self.assertEqual(
            counts,
            {
                "seeded_multi_hop": 10,
                "cold_start": 15,
                "executive_transition": 10,
                "contagion": 10,
                "negative_control": 5,
            },
        )

    def test_benchmark_queries_projection_matches_cases(self):
        from benchmarks.queries_50 import BENCHMARK_QUERIES, CASES

        self.assertEqual(len(BENCHMARK_QUERIES), len(CASES))
        self.assertEqual(
            [d["id"] for d in BENCHMARK_QUERIES], [c.id for c in CASES]
        )
        self.assertEqual(BENCHMARK_QUERIES[0]["query"], CASES[0].query)

    def test_categories_are_consistent_with_expectations(self):
        """A refusal case must expect AMBIGUOUS, and vice versa."""
        for case in cases_for_category("negative_control"):
            self.assertEqual(case.expected_route, "AMBIGUOUS", case.id)
            self.assertIsNone(case.expected_ticker, case.id)
            self.assertTrue(case.expect_refusal, case.id)

        for case in cases_for_category("cold_start"):
            self.assertEqual(case.expected_route, "COLD_START", case.id)
            self.assertIsNotNone(case.expected_ticker, case.id)
            self.assertFalse(case.expect_refusal, case.id)

    def test_non_apple_cases_forbid_apple_terms_or_explain_why_not(self):
        """The leak invariant is applied centrally, so no case may be AAPL-only."""
        for case in cases_for_category("cold_start"):
            self.assertNotEqual(case.expected_ticker, "AAPL", case.id)
            self.assertTrue(
                any("Apple" in t or "Cook" in t for t in case.forbidden_terms)
                or case.note,
                f"{case.id} has no Apple term and no note explaining why",
            )

    def test_cases_for_category_filters_and_all_returns_everything(self):
        self.assertEqual(len(cases_for_category("")), 50)
        self.assertEqual(len(cases_for_category("contagion")), 10)
        self.assertEqual(cases_for_category("nope"), [])

    def test_apple_leak_terms_cover_segment_dimensions(self):
        for term in ("iPhone", "iPad", "Tim Cook", "MacBook"):
            self.assertIn(term, APPLE_LEAK_TERMS)


# ── Mock harness against the real router ─────────────────────────────────────


class TestMockHarnessAgainstRealRouter(unittest.TestCase):
    def test_mock_harness_routes_a_cold_start_name_to_jpm(self):
        """The regression this whole suite was built for."""
        case = BenchmarkCase(
            id="M1",
            category="cold_start",
            query="What does JP MORGAN DO and what are its primary regulatory capital risks?",
            expected_route="COLD_START",
            expected_ticker="JPM",
            forbidden_terms=[],
            required_concepts=[],
            min_hops=1,
        )
        outcome = evaluate_case(case, MockHarness()(case))

        self.assertEqual(outcome.actual_route, "COLD_START")
        self.assertEqual(outcome.actual_ticker, "JPM")
        self.assertTrue(outcome.routing_ok)
        self.assertTrue(outcome.isolation_ok, outcome.leaked_terms)
        self.assertNotIn("AAPL", outcome.leaked_terms)

    def test_mock_harness_refuses_apple_fallback_for_entityless_query(self):
        """A metric-only question must not come back carrying AAPL or MSFT."""
        case = BenchmarkCase(
            id="M2",
            category="negative_control",
            query="net sales and operating income trends",
            expected_route="AMBIGUOUS",
            expected_ticker=None,
            forbidden_terms=[],
            required_concepts=[],
            min_hops=1,
            expect_refusal=True,
        )
        outcome = evaluate_case(case, MockHarness()(case))

        self.assertEqual(outcome.actual_route, "AMBIGUOUS")
        self.assertIsNone(outcome.actual_ticker)
        self.assertTrue(outcome.routing_ok)
        self.assertTrue(outcome.isolation_ok)

    def test_mock_harness_reaches_two_hops_through_the_real_traverser(self):
        """Hop depth is measured on the real stitcher and traverser."""
        case = BenchmarkCase(
            id="M3",
            category="contagion",
            query="How does a supply shock propagate through $ASML?",
            expected_route="COLD_START",
            expected_ticker="ASML",
            forbidden_terms=[],
            required_concepts=[],
            min_hops=2,
        )
        outcome = evaluate_case(case, MockHarness()(case), content_scored=False)

        self.assertEqual(outcome.path_depth, 2)
        self.assertTrue(outcome.hops_ok)

    def test_live_harness_preflight_reports_unreachable_server_clearly(self):
        """A dead server must say so, not produce 50 identical connection errors."""
        harness = LiveHarness(base_url="http://127.0.0.1:1", timeout=1.0)
        with self.assertRaises(SystemExit) as ctx:
            harness.preflight()
        self.assertIn("cannot reach", str(ctx.exception))


# ── Helpers ──────────────────────────────────────────────────────────────────


def _raw_harness(responses: dict[str, dict], measures_content: bool = True):
    """A harness that replays canned results keyed by case id.

    Lets the aggregation and rendering tests pin exact numbers without
    standing up a pipeline, so a failure points at the scoring layer rather
    than at the graph. ``measures_content=False`` reproduces the mock
    harness's declaration that it cannot score answer text.
    """
    calls: list[str] = []

    def _run(case: BenchmarkCase) -> dict:
        calls.append(case.id)
        return responses.get(case.id, _raw())

    _run.mode = "fixture"  # type: ignore[attr-defined]
    _run.measures_content = measures_content  # type: ignore[attr-defined]
    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ── Server wire telemetry ─────────────────────────────────────────────────────


class TestServerWireTelemetry(unittest.TestCase):
    """The client must read the server's numbers, not reconstruct them.

    The stage timings and hop depth used to be derived client-side: the ticker
    was regexed out of a human-readable status string, and depth was re-chained
    from a flattened edge list. Both were ways for the benchmark to disagree
    with the server about what the server did. These pin the replacement.
    """

    def _sse(self, status_steps=(), done=None, tokens=("hello",)):
        events = [("status", {"step": s, "message": m}) for s, m in status_steps]
        events += [("token", {"token": t}) for t in tokens]
        events.append(("done", done or {}))
        return events

    def test_stage_latencies_are_read_from_the_wire(self):
        done = {
            "status": "complete",
            "route": "COLD_START",
            "ticker": "JPM",
            "answer": "JPM sources from a supplier.",
            "provenance": "[JPM] --(SOURCES_FROM)--> [SUPPLIER]",
            "stage_latencies_ms": {
                "routing": 1.5, "fetching": 210.0, "extraction": 88.25,
                "stitching": 3.0, "traversal": 12.5, "synthesis": 900.0,
                "total": 1215.75,
            },
            "graph_metrics": {
                "max_hop_depth": 2, "node_count": 17, "edge_count": 23,
            },
        }
        case = _cases()[0]
        out = self._serve_once_with_case(case, self._sse(done=done))

        lat = out["latency_ms"]
        self.assertEqual(lat["routing_ms"], 1.5)
        self.assertEqual(lat["fetching_ms"], 210.0)
        self.assertEqual(lat["extraction_ms"], 88.25)
        self.assertEqual(lat["stitching_ms"], 3.0)
        self.assertEqual(lat["traversal_ms"], 12.5)
        self.assertEqual(lat["synthesis_ms"], 900.0)
        self.assertEqual(lat["total_ms"], 1215.75, "server total is passed through")
        self.assertIn("ttft_ms", lat, "client-observed first token is kept")
        self.assertIn("client_total_ms", lat, "client's own total is kept beside")

    def test_graph_metrics_are_read_from_the_wire(self):
        done = {
            "status": "complete", "route": "COLD_START", "ticker": "JPM",
            "answer": "x", "provenance": "",
            "graph": {"nodes": [], "edges": []},
            "graph_metrics": {
                "max_hop_depth": 3, "node_count": 42, "edge_count": 55,
            },
        }
        out = self._serve_once_with_case(_cases()[0], self._sse(done=done))
        self.assertEqual(out["graph_metrics"]["max_hop_depth"], 3)
        self.assertEqual(out["graph_metrics"]["node_count"], 42)
        self.assertEqual(out["graph_metrics"]["edge_count"], 55)

    def test_server_hop_depth_is_used_verbatim(self):
        """The server measured the traversal; this client only sees flat edges."""
        done = {
            "status": "complete", "route": "COLD_START", "ticker": "JPM",
            "answer": "x", "provenance": "",
            # Two flat edges that say nothing about which path they belong to.
            "graph": {"nodes": [], "edges": [
                {"source_id": "a", "target_id": "b"},
                {"source_id": "b", "target_id": "c"},
            ]},
            "graph_metrics": {"max_hop_depth": 2, "node_count": 3, "edge_count": 2},
        }
        outcome = evaluate_case(_cases()[0], self._serve_once_with_case(_cases()[0], self._sse(done=done)))
        self.assertEqual(outcome.path_depth, 2)
        self.assertTrue(outcome.depth_measured)

    def test_a_flat_edge_list_is_not_re_chained_into_a_depth(self):
        """No depth is invented when the server reports none.

        Two edges that chain are two hops only if they were the same path, and
        the flat list does not say. A client that assumed they chained would
        report depth 2 for a one-hop answer and quietly credit itself with
        multi-hop reachability it never demonstrated.
        """
        done = {
            "status": "complete", "route": "COLD_START", "ticker": "JPM",
            "answer": "x", "provenance": "",
            "graph": {"nodes": [], "edges": [
                {"source_id": "a", "target_id": "b"},
                {"source_id": "b", "target_id": "c"},
            ]},
        }
        raw = self._serve_once_with_case(_cases()[0], self._sse(done=done))
        self.assertEqual(raw["paths"], [], "no client-side path reconstruction")

        outcome = evaluate_case(_cases()[0], raw)
        self.assertEqual(outcome.path_depth, 0)
        self.assertFalse(
            outcome.depth_measured,
            "a missing measurement must be distinguishable from a zero-hop result",
        )

    def test_server_reported_paths_are_still_honoured(self):
        """Whole server-labelled paths are not a heuristic; they are the data."""
        done = {
            "status": "complete", "route": "COLD_START", "ticker": "JPM",
            "answer": "x", "provenance": "",
            "paths": [
                [{"source_id": "a", "target_id": "b"}, {"source_id": "b", "target_id": "c"}],
            ],
        }
        outcome = evaluate_case(_cases()[0], self._serve_once_with_case(_cases()[0], self._sse(done=done)))
        self.assertEqual(outcome.path_depth, 2)
        self.assertTrue(outcome.depth_measured)

    def test_ticker_comes_from_the_done_payload_not_a_status_string(self):
        """Status wording is for humans; the ticker is a wire field now.

        The old parser read ``"... for {TICKER}"`` out of a status message, so
        rewording that sentence silently broke live routing. Here the status
        text deliberately contains no ticker at all -- only the ``done``
        payload does -- and the case must still route.
        """
        done = {
            "status": "complete", "route": "COLD_START", "ticker": "JPM",
            "answer": "x", "provenance": "",
            "graph_metrics": {"max_hop_depth": 2, "node_count": 1, "edge_count": 1},
        }
        events = self._sse(
            status_steps=[("fetching", "Fetching SEC 10-K...")],  # no ticker here
            done=done,
        )
        out = self._serve_once_with_case(_cases()[0], events)
        self.assertEqual(out["ticker"], "JPM")

    def test_degraded_flag_is_read_from_the_wire(self):
        done = {
            "status": "complete", "route": "COLD_START", "ticker": "JPM",
            "answer": "x", "provenance": "", "degraded": True,
            "graph_metrics": {"max_hop_depth": 0, "node_count": 0, "edge_count": 0},
        }
        out = self._serve_once_with_case(_cases()[0], self._sse(done=done))
        self.assertTrue(out["degraded"])

    def _serve_once_with_case(self, case, events):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev, data in events:
                    self.wfile.write(
                        f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
                    )
                    self.wfile.flush()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            harness = LiveHarness(
                base_url=f"http://127.0.0.1:{srv.server_address[1]}"
            )
            return harness(case)
        finally:
            srv.shutdown()


class TestServerTelemetryEmission(unittest.TestCase):
    """The server side of the contract: the payload actually carries the fields.

    Asserted against the real ``_StageTimer`` and the real depth helpers rather
    than on the source text, so a refactor that keeps the field names but breaks
    the measurement fails here.
    """

    def test_stage_timer_reports_every_wire_stage_even_when_unused(self):
        from sandbox_engine.query_ui import WIRE_STAGES, _StageTimer

        wire = _StageTimer().as_wire()
        self.assertEqual(set(wire), {*WIRE_STAGES, "total"})
        for stage in WIRE_STAGES:
            self.assertEqual(wire[stage], 0.0, f"{stage} should start at zero")

    def test_stage_timer_records_time_for_the_stage_that_ran(self):
        from sandbox_engine.query_ui import _StageTimer

        timer = _StageTimer()
        with timer.stage("fetching"):
            time.sleep(0.02)
        wire = timer.as_wire()
        self.assertGreater(wire["fetching"], 10.0)
        self.assertEqual(wire["extraction"], 0.0, "untouched stages stay zero")
        self.assertGreaterEqual(wire["total"], wire["fetching"])

    def test_stage_timer_records_a_stage_that_raised(self):
        """A timeout is not instant; reporting 0.0 would hide it."""
        from sandbox_engine.query_ui import _StageTimer

        timer = _StageTimer()
        with self.assertRaises(ValueError):
            with timer.stage("synthesis"):
                time.sleep(0.02)
                raise ValueError("model timeout")
        self.assertGreater(timer.as_wire()["synthesis"], 10.0)

    def test_hop_depth_from_seeds_measures_the_evidence_subgraph(self):
        from sandbox_engine.query_ui import _hop_depth_from_seeds

        edges = [
            {"source": "AAPL", "target": "F1"},
            {"source": "F1", "target": "M1"},
            {"source": "M1", "target": "S1"},
        ]
        self.assertEqual(_hop_depth_from_seeds(["AAPL"], edges), 3)
        self.assertEqual(_hop_depth_from_seeds(["AAPL"], []), 0)
        self.assertEqual(_hop_depth_from_seeds([], edges), 0, "no seed, no depth")
        # Unrelated component is not reachable from the seed.
        self.assertEqual(
            _hop_depth_from_seeds(["AAPL"], [{"source": "X", "target": "Y"}]), 0
        )

    def test_hop_depth_survives_a_cycle(self):
        from sandbox_engine.query_ui import _hop_depth_from_seeds

        cyclic = [{"source": "A", "target": "B"}, {"source": "B", "target": "A"}]
        self.assertEqual(_hop_depth_from_seeds(["A"], cyclic), 1)

    def test_hop_depth_from_paths_uses_the_traversers_own_paths(self):
        from sandbox_engine.query_ui import _hop_depth_from_paths

        self.assertEqual(
            _hop_depth_from_paths([[{"a": 1}, {"b": 2}, {"c": 3}], [{"a": 1}, {"b": 2}]]),
            3,
        )
        self.assertEqual(_hop_depth_from_paths([]), 0)


# ── Checkpointing, resume, circuit breaker ────────────────────────────────────


class TestCheckpointPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / ".checkpoint_50.json"
        self.addCleanup(self._tmp.cleanup)

    def test_written_after_every_completed_query(self):
        """Not at the end. The point is surviving a kill mid-run."""
        checkpoint = BenchmarkCheckpoint(self.path)
        seen_writes: list[int] = []
        original_save = checkpoint.save

        def counting_save():
            seen_writes.append(len(checkpoint.records))
            original_save()

        checkpoint.save = counting_save  # type: ignore[method-assign]

        report = run_benchmark(
            _cases(),
            _raw_harness({c.id: _raw() for c in _cases()}),
            checkpoint=checkpoint,
        )
        self.assertEqual(len(report.outcomes), 3)
        self.assertEqual(seen_writes, [1, 2, 3], "one write per completed query")
        self.assertTrue(self.path.exists())

    def test_the_file_on_disk_is_complete_json_after_each_write(self):
        """Atomicity is the whole reason for tmp + os.replace.

        A checkpoint truncated by a kill mid-write looks resumable and is not,
        so the file must be replaced rather than rewritten in place.
        """
        checkpoint = BenchmarkCheckpoint(self.path)
        report = run_benchmark(
            _cases(),
            _raw_harness({c.id: _raw() for c in _cases()}),
            checkpoint=checkpoint,
        )
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["version"], BenchmarkCheckpoint.VERSION)
        self.assertEqual(payload["completed"], 3)
        self.assertEqual(sorted(payload["records"]), ["T1", "T2", "T3"])
        self.assertEqual(len(report.outcomes), 3)
        # No temp file left behind.
        leftovers = list(self.path.parent.glob(".*tmp*"))
        self.assertEqual(leftovers, [], f"temp file left behind: {leftovers}")

    def test_load_restores_outcomes_in_case_order(self):
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(_cases(), _raw_harness({}), checkpoint=checkpoint)

        fresh = BenchmarkCheckpoint(self.path)
        self.assertEqual(len(fresh.load()), 3)
        restored = fresh.restore(_cases())
        self.assertEqual([o.case_id for o in restored], ["T1", "T2", "T3"])
        self.assertTrue(all(o.passed for o in restored), "scoring survived the round trip")

    def test_a_corrupt_checkpoint_is_discarded_not_fatal(self):
        """Losing the file costs time; refusing to start costs the run."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json at all")
        checkpoint = BenchmarkCheckpoint(self.path)
        self.assertEqual(checkpoint.load(), {})
        report = run_benchmark(
            _cases(), _raw_harness({}), checkpoint=checkpoint
        )
        self.assertEqual(len(report.outcomes), 3, "still ran every case")

    def test_a_checkpoint_from_another_version_is_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "version": 999, "records": {"T1": {"actual_route": "COLD_START"}},
        }))
        self.assertEqual(BenchmarkCheckpoint(self.path).load(), {})

    def test_reset_deletes_the_file_and_the_records(self):
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(_cases(), _raw_harness({}), checkpoint=checkpoint)
        self.assertTrue(self.path.exists())
        checkpoint.reset()
        self.assertFalse(self.path.exists())
        self.assertEqual(checkpoint.records, {})

    def test_expectations_are_not_persisted(self):
        """The checkpoint must not outlive the expectations it was measured against.

        If the query set is edited between runs, a stored ``expected_route``
        would let a stale pass be reported as current.
        """
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(_cases(), _raw_harness({}), checkpoint=checkpoint)
        stored = json.loads(self.path.read_text())["records"]["T1"]
        for key in ("expected_route", "expected_ticker", "query", "category", "case_id"):
            self.assertNotIn(key, stored, f"{key} must come from the case, not the file")
        self.assertIn("actual_route", stored)

    def test_restored_outcomes_take_expectations_from_the_live_case(self):
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(_cases(), _raw_harness({}), checkpoint=checkpoint)
        edited = [
            BenchmarkCase(
                id=c.id, category=c.category, query=c.query,
                expected_route="KNOWN",  # deliberately different
                expected_ticker="MSFT",
                forbidden_terms=c.forbidden_terms,
                required_concepts=c.required_concepts,
                min_hops=c.min_hops,
            )
            for c in _cases()
        ]
        restored = BenchmarkCheckpoint(self.path).restore(edited)
        self.assertTrue(all(o.expected_route == "KNOWN" for o in restored))
        self.assertFalse(any(o.routing_ok for o in restored), "re-scored, not replayed")


class TestResume(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".checkpoint_50.json"
        self.addCleanup(self._tmp.cleanup)

    def test_resume_skips_already_completed_cases(self):
        harness = _raw_harness({})
        first = run_benchmark(_cases(), harness, checkpoint=BenchmarkCheckpoint(self.path))
        self.assertEqual(harness.calls, ["T1", "T2", "T3"])
        self.assertEqual(len(first.outcomes), 3)

        second_harness = _raw_harness({})
        second = run_benchmark(
            _cases(), second_harness, checkpoint=BenchmarkCheckpoint(self.path)
        )
        self.assertEqual(second_harness.calls, [], "nothing was re-run")
        self.assertEqual(len(second.outcomes), 3, "report still covers every case")
        self.assertEqual(second.resumed, 3)

    def test_resume_runs_only_the_missing_cases(self):
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(
            _cases()[:2],
            _raw_harness({"T1": _raw(), "T2": _raw(answer=_LEAKED_ANSWER)}),
            checkpoint=checkpoint,
        )
        self.assertEqual(sorted(checkpoint.completed_ids()), ["T1", "T2"])

        partial = _raw_harness({})
        report = run_benchmark(
            _cases(), partial, checkpoint=BenchmarkCheckpoint(self.path)
        )
        self.assertEqual(partial.calls, ["T3"], "only the missing case ran")
        self.assertEqual(len(report.outcomes), 3)
        self.assertEqual([o.case_id for o in report.outcomes], ["T1", "T2", "T3"])

    def test_resume_merges_restored_and_fresh_results(self):
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(
            _cases(),
            _raw_harness({"T1": _raw(), "T2": _raw(answer=_LEAKED_ANSWER), "T3": _raw()}),
            checkpoint=checkpoint,
        )
        report = run_benchmark(_cases(), _raw_harness({}), checkpoint=checkpoint)
        by_id = {o.case_id: o for o in report.outcomes}
        self.assertFalse(by_id["T2"].isolation_ok, "the earlier leak is still reported")
        self.assertTrue(by_id["T1"].passed)
        self.assertEqual(report.overall_pass_rate(), 200.0 / 3)

    def test_resume_runs_a_new_case_added_since_the_checkpoint(self):
        """The checkpoint tracks ids, so a new case runs and an old one is dropped."""
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(_cases()[:2], _raw_harness({}), checkpoint=checkpoint)

        extended = _cases() + [
            BenchmarkCase(
                id="T9", category="cold_start", query="New question?",
                expected_route="COLD_START", expected_ticker="JPM",
                forbidden_terms=[], required_concepts=[], min_hops=1,
            )
        ]
        fresh = _raw_harness({})
        report = run_benchmark(extended, fresh, checkpoint=checkpoint)
        self.assertEqual(fresh.calls, ["T3", "T9"], "only the two new cases ran")
        self.assertEqual(len(report.outcomes), 4)

    def test_resume_reports_true_position_in_the_suite(self):
        checkpoint = BenchmarkCheckpoint(self.path)
        run_benchmark(_cases()[:2], _raw_harness({}), checkpoint=checkpoint)
        report = run_benchmark(_cases(), _raw_harness({}), checkpoint=checkpoint)
        self.assertEqual([o.case_id for o in report.outcomes], ["T1", "T2", "T3"])
        self.assertEqual(report.cases_total, 3)
        self.assertEqual(report.resumed, 2)


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".checkpoint_50.json"
        self.addCleanup(self._tmp.cleanup)

    def _dead(self, case):
        return {
            "error": "URLError: <urlopen error [Errno 61] Connection refused>",
            "route": "ERROR",
        }

    def test_consecutive_transport_failures_stop_the_run(self):
        report = run_benchmark(
            _cases(), self._dead, checkpoint=BenchmarkCheckpoint(self.path),
            max_consecutive_failures=3,
        )
        self.assertTrue(report.breaker_tripped)
        self.assertEqual(len(report.outcomes), 3, "stopped at the third failure")
        self.assertFalse(report.interrupted, "a breaker stop is not an interrupt")

    def test_a_success_resets_the_failure_run(self):
        """An intermittent network must not accumulate into a false trip."""
        def alternating(case):
            if int(case.id[1:]) % 2 == 1:
                return {"error": "TimeoutError: timed out", "route": "ERROR"}
            return _raw()

        report = run_benchmark(
            _cases(), alternating, checkpoint=BenchmarkCheckpoint(self.path),
            max_consecutive_failures=2,
        )
        self.assertFalse(report.breaker_tripped)
        self.assertEqual(len(report.outcomes), 3, "every case ran")

    def test_a_metric_failure_is_not_a_transport_failure(self):
        """Bad scores are the result. Stopping on them would discard the findings."""
        def bad_scores(case):
            return _raw(route="COLD_START", ticker="ZZZZ", answer="x", paths=[])

        report = run_benchmark(
            _cases(), bad_scores, checkpoint=BenchmarkCheckpoint(self.path),
            max_consecutive_failures=1,
        )
        self.assertFalse(report.breaker_tripped)
        self.assertEqual(len(report.outcomes), 3)
        self.assertFalse(any(o.passed for o in report.outcomes), "they did fail")

    def test_zero_disables_the_breaker(self):
        report = run_benchmark(
            _cases(), self._dead, checkpoint=BenchmarkCheckpoint(self.path),
            max_consecutive_failures=0,
        )
        self.assertFalse(report.breaker_tripped)
        self.assertEqual(len(report.outcomes), 3)

    def test_transport_error_classification(self):
        for text in (
            "URLError: <urlopen error [Errno 61] Connection refused>",
            "TimeoutError: timed out",
            "HTTPError",
            "RemoteDisconnected",
            "ConnectionResetError: Connection reset by peer",
            # The formatted status string, not the class name. A 503 is the
            # outage this breaker exists for and it is exactly the shape
            # LiveHarness produces, so the classifier is pinned on it.
            "HTTP 503: service unavailable",
            "HTTP 500: internal error",
            "HTTP 429: Too Many Requests",
            "HTTP 408: Request Timeout",
        ):
            self.assertTrue(is_transport_error(text), text)
        for text in (
            "",
            "route KNOWN expected, got COLD_START",
            "hop depth 0 below required depth",
            # A malformed request is this client's bug, not a broken network:
            # stopping the run would hide it behind a breaker message.
            "HTTP 400: question is required",
            "HTTP 413: body too large",
        ):
            self.assertFalse(is_transport_error(text), text)

    def test_stopped_run_says_so_on_the_report_face(self):
        report = run_benchmark(
            _cases(), self._dead, checkpoint=BenchmarkCheckpoint(self.path),
            max_consecutive_failures=2,
        )
        md = render_report(report)
        self.assertIn("CIRCUIT BREAKER", md)
        self.assertIn("2 of 3", md)


class TestInterruptHandling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".checkpoint_50.json"
        self.addCleanup(self._tmp.cleanup)

    def test_interrupt_flag_stops_the_run_at_a_query_boundary(self):
        """The in-flight query is allowed to finish, then the run stops."""
        flag = _InterruptFlag()
        seen: list[str] = []
        harness = _raw_harness({})

        def wrapped(case):
            seen.append(case.id)
            if len(seen) == 2:
                flag.request(signal.SIGINT)  # as a handler would
            return _raw()

        wrapped.mode = "fixture"
        wrapped.measures_content = True
        report = run_benchmark(
            _cases(), wrapped, checkpoint=BenchmarkCheckpoint(self.path),
            should_stop=flag,
        )
        self.assertTrue(report.interrupted)
        self.assertEqual(seen, ["T1", "T2"], "the third query never started")
        self.assertEqual(len(report.outcomes), 2)
        self.assertEqual(len(json.loads(self.path.read_text())["records"]), 2)

    def test_completed_work_is_saved_before_an_interrupt(self):
        """A stop before the first query leaves nothing behind, and says so."""
        flag = _InterruptFlag()
        flag.request(signal.SIGTERM)
        report = run_benchmark(
            _cases(), _raw_harness({}), checkpoint=BenchmarkCheckpoint(self.path),
            should_stop=flag,
        )
        self.assertTrue(report.interrupted, "the request to stop is honoured")
        self.assertEqual(len(report.outcomes), 0)
        # No file, because nothing was ever recorded: an empty checkpoint would
        # be a file claiming a run happened. The CLI writes one on the way out.
        reloaded = BenchmarkCheckpoint(self.path)
        self.assertEqual(reloaded.load(), {})

    def test_interrupted_report_is_marked_partial(self):
        flag = _InterruptFlag()

        def stop_after_first(case):
            flag.request(signal.SIGINT)
            return _raw()

        stop_after_first.mode = "fixture"
        stop_after_first.measures_content = True
        report = run_benchmark(
            _cases()[:2], stop_after_first, checkpoint=BenchmarkCheckpoint(self.path),
            should_stop=flag,
        )
        md = render_report(report)
        self.assertIn("[INTERRUPTED]", md)
        self.assertIn("1 of 2", md)
        self.assertIn("not", md.lower())

    def test_signal_handlers_are_installed_and_restored(self):
        before_int = signal.getsignal(signal.SIGINT)
        before_term = signal.getsignal(signal.SIGTERM)
        flag = _InterruptFlag()
        previous = _install_signal_handlers(flag)
        try:
            self.assertIn(signal.SIGINT, previous)
            self.assertIn(signal.SIGTERM, previous)
            self.assertIsNot(signal.getsignal(signal.SIGINT), before_int)
            # A delivered signal sets the flag instead of raising.
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            self.assertTrue(flag(), "the flag was set")
            self.assertEqual(flag.signum, signal.SIGINT)
        finally:
            _restore_signal_handlers(previous)
        self.assertIs(signal.getsignal(signal.SIGINT), before_int)
        self.assertIs(signal.getsignal(signal.SIGTERM), before_term)

    def test_a_second_signal_escalates(self):
        flag = _InterruptFlag()
        previous = _install_signal_handlers(flag)
        try:
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            self.assertFalse(flag.hard)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            self.assertTrue(flag.hard, "a second press means 'stop now'")
        finally:
            _restore_signal_handlers(previous)

    def test_keyboardinterrupt_marks_the_run_interrupted(self):
        def boom(case):
            raise KeyboardInterrupt

        report = run_benchmark(
            _cases(), boom, checkpoint=BenchmarkCheckpoint(self.path)
        )
        self.assertTrue(report.interrupted)

    def test_a_real_sigint_to_a_real_process_saves_and_exits_130(self):
        """End-to-end: signal in, checkpoint and partial report out.

        A stub harness is slowed so the signal lands mid-run rather than after
        it, which is the only way to prove the saving path.
        """
        driver = Path(self._tmp.name) / "slow.py"
        driver.write_text(
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import benchmarks.eval_50_queries as m\n"
            "_orig = m.MockHarness.__call__\n"
            "def slow(self, case):\n"
            "    time.sleep(0.2)\n"
            "    return _orig(self, case)\n"
            "m.MockHarness.__call__ = slow\n"
            "raise SystemExit(m.main(sys.argv[1:]))\n" % str(Path.cwd())
        )
        out_md = Path(self._tmp.name) / "partial.md"
        proc = subprocess.Popen(
            [sys.executable, str(driver), "--mock-llm", "--reset",
             "--checkpoint", str(self.path), "--output", str(out_md)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(Path.cwd()), start_new_session=True,
        )
        try:
            time.sleep(2.0)
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            _out, err = proc.communicate(timeout=90)
        finally:
            if proc.poll() is None:  # pragma: no cover - only on a hung child
                proc.kill()
                proc.communicate()

        self.assertEqual(proc.returncode, 130, err)
        self.assertIn("INTERRUPTED", err)
        self.assertIn("--resume", err, "told how to continue")
        saved = json.loads(self.path.read_text())["completed"]
        self.assertGreater(saved, 0)
        self.assertLess(saved, 50)
        self.assertIn("[INTERRUPTED]", out_md.read_text())


class TestCheckpointCliFlags(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".checkpoint_50.json"
        self.out = Path(self._tmp.name) / "report.md"
        self.addCleanup(self._tmp.cleanup)

    def _run(self, *extra: str) -> int:
        return main([
            "--mock-llm", "--checkpoint", str(self.path),
            "--output", str(self.out), *extra,
        ])

    def test_reset_starts_from_nothing(self):
        self._run("--category", "negative_control")
        self.assertTrue(self.path.exists())
        self._run("--category", "negative_control", "--reset")
        self.assertEqual(json.loads(self.path.read_text())["completed"], 5)

    def test_resume_flag_runs_the_whole_suite_when_nothing_is_stored(self):
        code = self._run("--category", "negative_control", "--resume")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.path.read_text())["completed"], 5)

    def test_a_plain_run_re_runs_everything_and_only_resume_skips(self):
        # A plain repeat is a fresh run: --resume is the only thing that makes
        # an existing checkpoint a reason to skip work. If the default resumed,
        # --resume would be a no-op flag.
        self._run("--category", "negative_control")
        self._run("--category", "negative_control")
        self.assertNotIn(
            "Continued from a checkpoint",
            self.out.read_text(),
            "a plain run is a re-run, not a resume",
        )

        self._run("--category", "negative_control", "--resume")
        self.assertIn(
            "Continued from a checkpoint: 5 case(s)",
            self.out.read_text(),
            "--resume restores and skips the already-completed cases",
        )

    def test_reset_and_resume_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._run("--reset", "--resume")

    def test_a_breaker_stop_exits_non_zero(self):
        """A partial run must not be mistakable for a clean pass.

        Against a server that is *up* (so preflight passes) but failing every
        request -- the shape of a real outage. An unreachable server is a
        different case: preflight refuses it outright, before any run starts.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                body = b"service unavailable"
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = self.out
            code = main([
                "--live", "--base-url", f"http://127.0.0.1:{srv.server_address[1]}",
                "--category", "negative_control", "--max-consecutive-failures", "2",
                "--checkpoint", str(self.path), "--output", str(out),
            ])
        finally:
            srv.shutdown()
        self.assertNotEqual(code, 0, "partial run exits non-zero")
        self.assertIn("CIRCUIT BREAKER", out.read_text())

    def test_an_unreachable_server_fails_fast_before_running(self):
        """Preflight exists so 50 identical connection errors are not produced."""
        out = self.out
        with self.assertRaises(SystemExit):
            main([
                "--live", "--base-url", "http://127.0.0.1:1",
                "--category", "negative_control",
                "--checkpoint", str(self.path), "--output", str(out),
            ])


if __name__ == "__main__":
    unittest.main()
