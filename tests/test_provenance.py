"""The provenance contract, tested against the two regressions that motivated it.

The contract is only worth anything if it *rejects* things. A tagging scheme
that assigns STATED to whatever the model produced would pass every "does a
good answer come out" test and be worth nothing, so most of what follows is
about the refusal path.

The two named cases are the acceptance criteria:

* ``What is Apple company?`` must come back GAP on the business description --
  the corpus has Apple's 10-K, so the temptation is to paraphrase Item 1, and
  the failure is doing that without a Source.
* the geographic x product sales table with the corrupt 9M row must be
  rejected or flagged. The failure mode there is subtler than invention: both
  the product split and the geographic split are genuinely in the corpus, so a
  model that crosses them produces a table where every *cell* is unsourced
  while every *row* looks cited.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

# The module has a `__main__` block, so it is meant to be runnable on its own as
# well as under pytest, and that needs the repository root importable -- pytest
# puts it there for us and a direct `python tests/test_provenance.py` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_engine.buffer import NODE_TABLES
from sandbox_engine.provenance import (
    DERIVED,
    EXTERNAL,
    GAP,
    INFERRED,
    STATED,
    Evidence,
    Source,
    SourceResolver,
    build_evidence,
    citation_tags,
    extract_figures,
    grade_answer,
    normalise_number,
    render_gap,
    serialise_evidence,
    _split_sentences,
)


def src(**kw) -> Source:
    base = dict(
        node_id="n1",
        label="Net Sales",
        form_type="10-K",
        filing_date="2025-10-31",
        accession="0000320193-25-000079",
        item_code="8",
        section="Management's Discussion and Analysis",
        period_end="2025-09-27",
        fiscal_year="2025",
        fiscal_period="FY",
    )
    base.update(kw)
    return Source(**base)


def ev(tag: str, text: str, **kw) -> Evidence:
    return Evidence(tag=tag, text=text, source=kw.pop("source", None) or src(), provenance=kw.pop("provenance", STATED))


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

class NumberHandlingTests(unittest.TestCase):
    def test_scale_and_separators_normalise_together(self):
        """The evidence and the answer spell the same figure differently, and
        string comparison would call that a fabrication."""
        for spelling in ("416,161", "$416,161", "416161", "416,161M", "USD 416,161"):
            self.assertEqual(normalise_number(spelling), 416161.0, spelling)

    def test_dot_is_a_decimal_not_a_thousands_separator(self):
        """Deliberately strict. Reading "416.161" as 416161 would let a
        genuinely different figure pass as grounded, and a contract that cries
        wolf in the lenient direction is worse than one that refuses. SEC data
        is US-convention anyway, so the strict reading is also the right one.
        """
        self.assertEqual(normalise_number("416.161"), 416.161)

    def test_structural_tokens_are_not_figures(self):
        """A correctly-cited sentence that says 'the 10-K' must not be flagged
        for the 10, nor 'FY2026' for the 2026."""
        self.assertEqual(extract_figures("Reported in the 10-K for FY2025, Item 1"), [])
        self.assertEqual(extract_figures("filed 2025-10-31"), [])
        self.assertEqual(extract_figures("CIK 0000320193"), [])

    def test_real_figures_survive(self):
        self.assertEqual(extract_figures("net sales were 416,161"), ["416,161"])
        self.assertEqual(extract_figures("margin of 31.97%"), ["31.97%"])

    def test_a_citation_tag_is_not_a_reported_figure(self):
        """Regression: ``[E158]`` was read as the figure ``158``.

        No evidence item can ever ground the number a tag happens to end in, so
        a correct sentence citing a three-digit tag was rejected as fabrication
        and a correct answer was replaced with a GAP.
        """
        self.assertEqual(
            extract_figures("net sales were 416,161 million [E158]"), ["416,161"]
        )
        self.assertEqual(
            extract_figures("net sales were 416,161 million [E158] per the 10-K [E4]"),
            ["416,161"],
        )

    def test_the_arrow_citation_form_is_also_masked(self):
        # The model writes this when it pairs a line item with its value.
        self.assertEqual(extract_figures("line item [E12] reported 120,451 [E2→E15]"), ["120,451"])
        self.assertEqual(extract_figures("total [E101] of 98,000 [E102→E103]"), ["98,000"])

    def test_masking_tags_does_not_hide_a_real_figure(self):
        self.assertEqual(
            extract_figures("filed 2025-10-31 with revenue of 416,161 [E158]"), ["416,161"]
        )

    def test_a_list_of_tags_is_masked_as_one_citation(self):
        """Regression: only the single-tag form was masked.

        ``[E158, E200]`` left the digits in, so the two tag numbers were
        reported as figures no evidence could ground and a correct sentence
        was rejected as fabrication -- the same failure 01c47cc fixed for
        ``[E158]``, one comma away.
        """
        self.assertEqual(
            extract_figures("net sales 416,161 [E158, E200]"), ["416,161"]
        )
        self.assertEqual(
            extract_figures("net sales 416,161 [E1, E3]"), ["416,161"]
        )

    def test_every_citation_form_yields_its_tags(self):
        """One grammar, so the mask, the grader and the UI cannot disagree.

        The tag numbers in a three-digit citation are the giveaway: whichever
        caller failed to recognise the form reported them as figures or as
        absent citations.
        """
        for text, expected in (
            ("net sales 416,161 [E1]", ["E1"]),
            ("net sales 416,161 [E1, E3]", ["E1", "E3"]),
            ("net sales 416,161 [E1,E3]", ["E1", "E3"]),
            ("net sales 120,451 [E12->E15]", ["E12", "E15"]),
            ("net sales 120,451 [E12 -> E15]", ["E12", "E15"]),
            ("net sales 120,451 [E12 => E15]", ["E12", "E15"]),
            ("net sales 416,161 [E1] and 281,724 [E158]", ["E1", "E158"]),
        ):
            self.assertEqual(citation_tags(text), expected, text)
            # A tag number surviving tokenisation is the failure this guards.
            figures = extract_figures(text)
            self.assertNotIn("158", figures, text)
            self.assertNotIn("15", figures, text)
            self.assertNotIn("12", figures, text)

    def test_a_repeated_tag_is_counted_once(self):
        self.assertEqual(citation_tags("x [E1] y [E1]"), ["E1"])


class SentenceSplitTests(unittest.TestCase):
    def test_decimals_do_not_split_a_sentence(self):
        got = _split_sentences("Revenue was 416,161.50 million. That is the total.")
        self.assertEqual(len(got), 2, got)

    def test_abbreviations_do_not_split(self):
        self.assertEqual(len(_split_sentences("Apple Inc. reported growth.")), 1)


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

class RuleTests(unittest.TestCase):
    def test_cited_figure_is_stated(self):
        e = [ev("E1", "Net Sales 416,161 USD millions")]
        g = grade_answer("Net sales were 416,161 million [E1].", e)
        self.assertEqual(g.verdicts[0].provenance, STATED)

    def test_unsourced_number_is_gap(self):
        e = [ev("E1", "Net Sales 416,161 USD millions")]
        g = grade_answer("Net sales were 512,000 million.", e)
        self.assertEqual(g.verdicts[0].provenance, GAP)
        self.assertIn("512,000", g.ungrounded_figures)

    def test_invented_tag_is_gap(self):
        e = [ev("E1", "Net Sales 416,161 USD millions")]
        g = grade_answer("Net sales were 416,161 million [E7].", e)
        self.assertEqual(g.verdicts[0].provenance, GAP)
        self.assertIn("E7", g.invented_tags)

    def test_arithmetic_over_cited_facts_is_derived(self):
        e = [ev("E1", "Net Sales 416,161"), ev("E2", "Net Sales 391,035")]
        g = grade_answer(
            "Growth was 416,161 - 391,035 = 25,126 [E1] [E2].", e
        )
        self.assertEqual(g.verdicts[0].provenance, DERIVED)

    def test_hedged_sentence_infers_only_after_a_stated_one(self):
        e = [ev("E1", "Net Sales 416,161")]
        alone = grade_answer("This may indicate durable demand.", e)
        self.assertEqual(alone.verdicts[0].provenance, GAP)

        after = grade_answer(
            "Net sales were 416,161 [E1]. This may indicate durable demand.", e
        )
        self.assertEqual(after.verdicts[0].provenance, STATED)
        self.assertEqual(after.verdicts[1].provenance, INFERRED)

    def test_uncited_assertion_is_gap(self):
        e = [ev("E1", "Net Sales 416,161")]
        g = grade_answer("Apple reported record profitability.", e)
        self.assertEqual(g.verdicts[0].provenance, GAP)

    def test_outside_corpus_is_external(self):
        e = [ev("E1", "Net Sales 416,161")]
        g = grade_answer("Analysts said this week that demand is strong.", e)
        self.assertEqual(g.verdicts[0].provenance, EXTERNAL)

    def test_uncited_arithmetic_is_gap(self):
        """Growth computed over nothing is not a derivation."""
        e = [ev("E1", "Net Sales 416,161")]
        g = grade_answer("Growth was 25,126 = 6.43% of the prior year.", e)
        self.assertEqual(g.verdicts[0].provenance, GAP)

    def test_violations_are_reported_for_the_reader(self):
        e = [ev("E1", "Net Sales 416,161")]
        g = grade_answer("Revenue was 999,999 [E9].", e)
        self.assertTrue(g.violations())


# ---------------------------------------------------------------------------
# The two named acceptance cases
# ---------------------------------------------------------------------------

class AppleCompanyQuestionTests(unittest.TestCase):
    """'What is Apple company?' must not be answered from thin air.

    The corpus contains Apple's 10-K, so a model asked this will happily
    paraphrase Item 1. The contract's job is to notice that the *business
    description* it produced rests on no evidence line, and to say GAP.
    """

    QUESTION = "What is Apple company?"

    def _evidence(self) -> list[Evidence]:
        return [
            ev("E1", "Apple Inc", source=src(form_type="", section="", item_code="")),
            ev("E2", "10-K FY2025 (FY) filed 2025-10-31"),
            ev("E3", "Ticker: AAPL, CIK: 0000320193", source=src(section="Cover Page", item_code="")),
        ]

    def test_business_description_with_no_evidence_is_gap(self):
        e = self._evidence()
        fabricated = (
            "Apple designs, manufactures and markets smartphones, personal "
            "computers, tablets, wearables and a growing services business."
        )
        g = grade_answer(fabricated, e)
        self.assertTrue(
            all(v.provenance == GAP for v in g.verdicts),
            [v.provenance for v in g.verdicts],
        )
        self.assertTrue(g.gap)

    def test_a_business_description_backed_by_a_cited_chunk_is_stated(self):
        """The same sentence is STATED when a DocumentChunk actually carries it.

        The contract grades the sentence against the evidence it was given, not
        against a list of forbidden topics -- otherwise a corpus that *did*
        answer the question would still be refused.
        """
        e = self._evidence() + [
            ev(
                "E4",
                "Business chunk: The Company designs, manufactures and markets "
                "smartphones, personal computers and wearable devices.",
                source=src(item_code="1", section="Business"),
            )
        ]
        g = grade_answer(
            "Apple designs and markets smartphones and computers [E4].", e
        )
        self.assertEqual(g.verdicts[0].provenance, STATED)
        self.assertFalse(g.gap)

    def test_gap_answer_names_where_the_description_would_be(self):
        text = render_gap(
            self.QUESTION,
            self._evidence(),
            ["Apple 10-K FY2025, Item 1 Business — the issuer's own description of itself"],
        )
        self.assertIn("GAP", text)
        self.assertIn("Item 1 Business", text)
        # A refusal that does not say what is missing is not an answer.
        self.assertIn("does not cover", text)


class GeographicProductTableTests(unittest.TestCase):
    """The corrupt 9M row: every cell unsourced, every row looking cited.

    Apple discloses a product split and a geographic split, separately. A
    model asked for the cross-tab will produce a grid whose row labels and
    column labels are all genuinely in the corpus, which is exactly what makes
    it dangerous -- the table looks cited end to end. No cell is, because no
    single Source states a product-and-region figure.
    """

    def _evidence(self) -> list[Evidence]:
        return [
            ev("E1", "Segment: iPhone", source=src(item_code="1", section="Business")),
            ev("E2", "Segment: Mac"),
            ev("E3", "Segment: iPad"),
            ev("E4", "Segment: Wearables, Home and Accessories"),
            ev("E5", "Segment: Americas", source=src(section="Segment Information")),
            ev("E6", "Segment: Europe"),
            ev("E7", "Segment: Greater China"),
            ev("E8", "Segment: Japan"),
            ev("E9", "Segment: Rest of Asia Pacific"),
            # The 9M row that was corrupt: one period key, contradictory values.
            ev("E10", "Net Sales 364,357 USD millions"),
        ]

    CROSS_TAB = (
        "iPhone in the Americas was 120,451, in Europe 98,332, in Greater China "
        "64,890, in Japan 21,004 and in Rest of Asia Pacific 17,551 [E1] [E5]."
    )

    def test_cross_tab_cells_are_not_grounded(self):
        g = grade_answer(self.CROSS_TAB, self._evidence())
        v = g.verdicts[0]
        self.assertEqual(v.provenance, GAP)
        # The specific cell values are what must be caught, not the row labels.
        for cell in ("120,451", "98,332", "64,890", "21,004", "17,551"):
            self.assertIn(cell, g.ungrounded_figures, cell)

    def test_cited_row_labels_do_not_launder_the_cells(self):
        """Citing [E1] and [E5] -- a real product and a real region -- must not
        make a figure sourced. The tags are real, the cell is not.
        """
        g = grade_answer(self.CROSS_TAB, self._evidence())
        self.assertEqual(g.verdicts[0].provenance, GAP)
        self.assertTrue(g.violations())

    def test_the_two_real_splits_still_answer(self):
        """The contract must not break the questions it can answer, or it will
        be switched off by the first false refusal."""
        e = self._evidence()
        product = grade_answer("Apple discloses iPhone, Mac, iPad and Wearables [E1] [E2] [E3] [E4].", e)
        self.assertEqual(product.verdicts[0].provenance, STATED)

        geo = grade_answer("Apple's geographic segments are Americas, Europe, Greater China, Japan and Rest of Asia Pacific [E5] [E6] [E7] [E8] [E9].", e)
        self.assertEqual(geo.verdicts[0].provenance, STATED)

    def test_gap_for_the_cross_tab_points_at_the_underlying_note(self):
        text = render_gap(
            "Net sales by product and region?",
            self._evidence(),
            ["Apple 10-K FY2025, Segment Information note — discloses each split "
             "separately; the product x region cross-tab is not tabulated"],
        )
        self.assertIn("Segment Information", text)
        self.assertIn("not tabulated", text)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

class IssuerAttributionTests(unittest.TestCase):
    """A number read off one issuer's line and written on another's.

    The corpus holds several issuers, and every one of them files a Net Sales
    line, so a model handed a wall of evidence can carry a figure from one to
    the other. That is the most damaging thing it can do and the figure check
    cannot see it: the number is in the cited text, it just is not *that
    issuer's* number. So a sentence that names an issuer the cited sources were
    not filed by is refused on its own account.
    """

    APPLE = Source(node_id="m-apple", label="Net Sales (FY2025)", ticker="AAPL",
                  cik="0000320193", form_type="10-K", fiscal_year="2025")
    MICROSOFT = Source(node_id="m-msft", label="Net Sales (FY2025)", ticker="MSFT",
                       cik="0000789019", form_type="10-K", fiscal_year="2025")

    def _evidence(self) -> list[Evidence]:
        # The Company lines are what link "Apple" to "AAPL": a Company node is
        # keyed on its own ticker and labelled with its legal name, and they
        # arrive in the block for every question because retrieval always
        # includes them.
        return [
            Evidence("E1", "Apple Inc — Ticker: AAPL, CIK: 0000320193",
                     Source(node_id="AAPL", label="Apple Inc", ticker="Apple Inc"),
                     kind="Company"),
            Evidence("E2", "Net Sales (FY2025) — Reported: value=416,161.00 USD",
                     self.APPLE, kind="FinancialMetric"),
            Evidence("E3", "Microsoft Corporation — Ticker: MSFT, CIK: 0000789019",
                     Source(node_id="MSFT", label="Microsoft Corporation",
                            ticker="Microsoft Corporation"),
                     kind="Company"),
            Evidence("E4", "Net Sales (FY2025) — Reported: value=281,724.00 USD",
                     self.MICROSOFT, kind="FinancialMetric"),
        ]

    def test_apple_figure_attributed_to_microsoft_is_gap(self):
        """The regression, verbatim: STATED, ungrounded=[], headline metric
        blind to it."""
        g = grade_answer(
            "Microsoft's FY2025 net sales were 416,161 million [E2].", self._evidence()
        )
        v = g.verdicts[0]
        self.assertEqual(v.provenance, GAP, v.reason)
        self.assertEqual(v.ungrounded, [])
        self.assertEqual(v.misattributed, ["MSFT"])
        self.assertEqual(g.misattributed, ["MSFT"])
        # And the reader is told, in words.
        self.assertTrue(any("MSFT" in note for note in g.violations()), g.violations())

    def test_the_right_issuer_still_answers_by_every_spelling(self):
        """A contract that refuses correct answers gets switched off, so the
        two spellings of one issuer have to resolve to the same issuer."""
        e = self._evidence()
        for text in (
            "Apple's FY2025 net sales were 416,161 million [E2].",
            "Apple Inc reported 416,161 million [E2].",
            "AAPL's FY2025 net sales were 416,161 million [E2].",
        ):
            g = grade_answer(text, e)
            self.assertEqual(g.verdicts[0].provenance, STATED, (text, g.verdicts[0].reason))
            self.assertEqual(g.verdicts[0].misattributed, [], text)

    def test_a_comparison_citing_both_issuers_answers(self):
        g = grade_answer(
            "Apple's net sales were 416,161 and Microsoft's were 281,724 [E2] [E4].",
            self._evidence(),
        )
        self.assertEqual(g.verdicts[0].provenance, STATED, g.verdicts[0].reason)
        self.assertEqual(g.verdicts[0].misattributed, [])

    def test_shown_arithmetic_does_not_launder_a_misattribution(self):
        g = grade_answer(
            "Microsoft's net sales were 416,161 - 0 = 416,161 [E2].", self._evidence()
        )
        self.assertEqual(g.verdicts[0].provenance, GAP, g.verdicts[0].reason)

    def test_a_figure_cited_to_a_line_that_does_not_carry_it_is_ungrounded(self):
        """Grounding is per cited tag, not per evidence block.

        The whole block used to be unioned in, which turned any tag into a
        laundering device: cite one line and every figure anywhere in the
        evidence became groundable, so a sentence could carry a number off a
        filing it never cited.
        """
        e = self._evidence() + [
            Evidence("E5", "Net Sales (FY2024) — Reported: value=391,035.00 USD",
                     self.APPLE, kind="FinancialMetric"),
        ]
        g = grade_answer("Apple's FY2024 net sales were 391,035 million [E2].", e)
        self.assertEqual(g.verdicts[0].provenance, GAP, g.verdicts[0].reason)
        self.assertIn("391,035", g.ungrounded_figures)

    def test_evidence_with_no_filer_names_no_issuer_to_check_against(self):
        """An untraced line says nothing about who filed it.

        Without this, every chunk-backed claim -- a business description
        grounded in Item 1, say -- would be refused for naming the company the
        chunk is about, which is the first false refusal and enough to get the
        contract switched off.
        """
        chunk = Evidence(
            "E1",
            "Business chunk: The Company designs and markets smartphones.",
            src(section="Business"),
            kind="DocumentChunk",
        )
        g = grade_answer("Apple designs and markets smartphones [E1].", [chunk])
        self.assertEqual(g.verdicts[0].provenance, STATED, g.verdicts[0].reason)
        self.assertEqual(g.verdicts[0].misattributed, [])


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class SerialisationTests(unittest.TestCase):
    def test_every_block_carries_its_tag_and_source(self):
        e = [ev("E1", "Net Sales 416,161"), ev("E2", "Segment: iPhone")]
        text = serialise_evidence(e)
        self.assertIn("[E1]", text)
        self.assertIn("[E2]", text)
        # A fact with no source line is not citable, so the block must show it.
        self.assertIn("10-K", text)

    def test_model_is_told_it_may_only_cite_given_tags(self):
        self.assertIn("ONLY cite tags", serialise_evidence([ev("E1", "x")]))


class ResolverTests(unittest.TestCase):
    """The Source has to be read off the graph, not invented."""

    class FakeGraph:
        """Answers only the queries the resolver actually issues.

        Matching on the real query text rather than a flag is deliberate: if
        the resolver's traversal changes, the fixture stops matching and the
        test fails loudly instead of silently returning nothing.
        """

        def __init__(self):
            self.queries: list[tuple[str, dict]] = []

        def execute(self, q, params=None):
            self.queries.append((q, dict(params or {})))
            if q.startswith("MATCH (f:Filing) RETURN f.id"):
                return [("acc1", "10-K", "2025-10-31", "0000320193-25-000079",
                         "2025-09-27", 2025, "FY")]
            if q.startswith("MATCH (f:Filing)-[r]->(n)"):
                # Answers only for the ids the query asked for, so a resolver
                # that dropped the filter would be caught by this fixture
                # returning rows it was never asked about.
                wanted = set((params or {}).get("ids", []))
                return [(n, f, c) for n, f, c in (("m1", "acc1", ""), ("elsewhere", "acc2", "1A")) if n in wanted]
            if "REPORTED_IN" in q:
                return [("m1", "8", "Management's Discussion and Analysis")]
            if "DISAGGREGATED_BY" in q:
                return []
            return []

    def test_metric_resolves_to_filing_and_item(self):
        nodes = [{"id": "m1", "name": "Net Sales", "type": "FinancialMetric"}]
        s = SourceResolver(self.FakeGraph()).sources_for(nodes)["m1"]
        self.assertEqual(s.form_type, "10-K")
        self.assertEqual(s.filing_date, "2025-10-31")
        self.assertEqual(s.item_code, "8")
        self.assertIn("filed 2025-10-31", s.cite())
        self.assertIn("Item 8", s.cite())

    def test_untraceable_node_gets_no_source(self):
        """A fact that cannot be traced must not be citable."""
        nodes = [{"id": "zzz", "name": "Mystery", "type": "FinancialMetric"}]
        out = SourceResolver(self.FakeGraph()).sources_for(nodes)
        self.assertNotIn("zzz", out)

    def test_build_evidence_marks_untraced_rather_than_dropping_it(self):
        """Hiding it would leave the model to guess; showing it untraced makes
        the gap explicit."""
        nodes = [{"id": "zzz", "name": "Mystery", "type": "FinancialMetric"}]
        e = build_evidence(nodes, self.FakeGraph())
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0].source.section, "untraced")

    def test_company_is_citeable(self):
        nodes = [{"id": "c1", "name": "Apple Inc", "type": "Company"}]
        out = SourceResolver(self.FakeGraph()).sources_for(nodes)
        self.assertIn("c1", out)

    def test_the_filing_traversal_filters_in_the_engine(self):
        """The ids go in the query, not only in the loop that reads it.

        Batching kept this from being one query per node, but unfiltered it was
        still one query that looked at every filing-to-node row in the graph --
        ~9,700 of them -- to pick out the few hundred a broad question
        retrieved. That is the dominant cost of resolving a Source and it was
        paid on every question.
        """
        graph = self.FakeGraph()
        nodes = [
            {"id": "m1", "name": "Net Sales", "type": "FinancialMetric"},
            {"id": "m2", "name": "Cost of Sales", "type": "FinancialMetric"},
        ]
        SourceResolver(graph).sources_for(nodes)
        traversals = [
            (q, p) for q, p in graph.queries
            if q.startswith("MATCH (f:Filing)-[r]->(n)")
        ]
        self.assertEqual(len(traversals), 1, graph.queries)
        query, params = traversals[0]
        self.assertIn("WHERE n.id IN $ids", query)
        self.assertEqual(params.get("ids"), ["m1", "m2"])

    def test_a_node_the_query_was_not_asked_about_is_not_traced(self):
        """The fixture answers only the ids it was given, so this fails if the
        filter is dropped: "elsewhere" would come back traced."""
        graph = self.FakeGraph()
        out = SourceResolver(graph).sources_for(
            [{"id": "m1", "name": "Net Sales", "type": "FinancialMetric"}]
        )
        self.assertIn("m1", out)
        self.assertNotIn("elsewhere", out)


class SegmentResolutionTests(unittest.TestCase):
    """Segments are reachable by two different paths, and only by name.

    A Segment node carries ``name`` and ``segment_type`` and nothing else --
    there is no ``id`` to join on -- so the resolver keys on the name the
    retriever uses. The corpus splits across two traversals: some segments hang
    off a metric (``HAS_SEGMENT``), others off a raw fact reached through the
    section it was reported in (``BROKEN_DOWN_BY``). Missing either one leaves
    real segments untraceable, which is what happened before.
    """

    #: Every filing-to-node row the fixture graph holds. The traversal returns
    #: only the ones whose node is in the id list it is given, so a resolver
    #: that stopped filtering in the engine would be visible here as a fixture
    #: that answers the whole graph.
    ALL_EDGES = [("m1", "f1", "8"), ("unrelated", "f2", "1A")]

    class FakeGraph:
        def execute(self, q, params=None):
            if q.startswith("MATCH (f:Filing) RETURN f.id"):
                return [
                    ("recent", "10-K", "2026-08-26", "acc-recent", "2026-06-30", 2026, "FY"),
                    ("old", "10-K", "2025-08-26", "acc-old", "2025-06-30", 2025, "FY"),
                ]
            if q.startswith("MATCH (f:Filing)-[r]->(n)"):
                wanted = set((params or {}).get("ids", []))
                return [(n, f, c) for n, f, c in SegmentResolutionTests.ALL_EDGES if n in wanted]
            if "HAS_SEGMENT" in q:
                # Reported by two filings; the newer must win.
                return [("Americas", "old", "2025-08-26"), ("Americas", "recent", "2026-08-26")]
            if "BROKEN_DOWN_BY" in q:
                return [("Products", "recent", "2026-08-26")]
            return []

    def test_segment_via_metric_is_traced_to_its_filing(self):
        nodes = [{"id": "Americas", "name": "Americas", "type": "Segment"}]
        out = SourceResolver(self.FakeGraph()).sources_for(nodes)
        self.assertIn("Americas", out)
        self.assertEqual(out["Americas"].form_type, "10-K")

    def test_segment_via_raw_fact_is_also_traced(self):
        nodes = [{"id": "Products", "name": "Products", "type": "Segment"}]
        out = SourceResolver(self.FakeGraph()).sources_for(nodes)
        self.assertIn("Products", out)

    def test_a_segment_reported_by_many_filings_cites_the_most_recent(self):
        nodes = [{"id": "Americas", "name": "Americas", "type": "Segment"}]
        out = SourceResolver(self.FakeGraph()).sources_for(nodes)
        self.assertEqual(out["Americas"].accession, "acc-recent")

    def test_no_traversal_asks_a_segment_for_a_property_it_does_not_have(self):
        """The dead fallback, pinned against the real schema.

        A third traversal used to sit in ``sources_for``, keyed on ``s.id``
        through ``DISAGGREGATED_BY``. ``Segment`` carries ``name`` and
        ``segment_type`` and nothing else, so that query raised on every
        database -- and the ``except: pass`` around it made a fallback that
        had never once run look like a working one. Asserted against
        :data:`NODE_TABLES` rather than as a string, so it holds whatever the
        schema grows.
        """
        seen: list[str] = []

        class RecordingGraph(SegmentResolutionTests.FakeGraph):
            def execute(self, q):
                seen.append(q)
                return super().execute(q)

        SourceResolver(RecordingGraph()).sources_for(
            [{"id": "Americas", "name": "Americas", "type": "Segment"}]
        )
        self.assertTrue(seen, "the resolver issued no query at all")
        segment_columns = set(NODE_TABLES["Segment"])
        for query in seen:
            for alias in re.findall(r"\(\s*(\w+)\s*:\s*Segment\s*\)", query):
                for prop in re.findall(rf"\b{alias}\.(\w+)\b", query):
                    self.assertIn(
                        prop, segment_columns,
                        f"Segment has no {prop!r}: {query}",
                    )


class EvidenceTagAndGroundingTests(unittest.TestCase):
    """Two wiring bugs that made correct answers look fabricated.

    The retriever's ``tag_map`` is ``tag -> node_id``, not the reverse; reading
    it the other way labelled every fact with its node id, so a model citing
    ``[E9]`` was told it had invented a tag. And a figure lives on the edge that
    reports it, not on the node, so evidence built from nodes alone carried no
    number and every cited value graded as ungrounded.
    """

    class NoGraph:
        def execute(self, q):
            return []

    def test_tags_are_positional_not_node_ids(self):
        nodes = [
            {"id": "AAPL", "name": "Apple Inc", "type": "Company"},
            {"id": "m1", "name": "Net Sales", "type": "FinancialMetric"},
        ]
        evidence = build_evidence(nodes, self.NoGraph(), {"E1": "AAPL", "E2": "m1"})
        self.assertEqual([e.tag for e in evidence], ["E1", "E2"])

    def test_edge_facts_are_folded_into_the_cited_node(self):
        nodes = [{"id": "m1", "name": "Net Sales (FY2025)", "type": "FinancialMetric"}]
        edges = [
            {
                "source": "f1",
                "target": "m1",
                "relation": "REPORTS_METRIC",
                "description": "Reported net_sales: value=416,161.00 USD (in millions)",
            }
        ]
        evidence = build_evidence(nodes, self.NoGraph(), {"E1": "m1"}, edges)
        self.assertIn("416,161", evidence[0].text)

    def test_a_figure_supplied_by_an_edge_grades_stated(self):
        nodes = [{"id": "m1", "name": "Net Sales (FY2025)", "type": "FinancialMetric"}]
        edges = [
            {
                "source": "f1",
                "target": "m1",
                "relation": "REPORTS_METRIC",
                "description": "Reported net_sales: value=416,161.00 USD (in millions)",
            }
        ]
        evidence = build_evidence(nodes, self.NoGraph(), {"E1": "m1"}, edges)
        graded = grade_answer("Net sales were 416,161 million USD [E1].", evidence, "net sales?")
        self.assertFalse(graded.gap)
        self.assertEqual(graded.mix, {STATED: 1})


class UnitRestatementTests(unittest.TestCase):
    """A figure restated in another unit is the cited figure, not a new one.

    Regression: the grader compared numerically with no unit or rounding
    awareness, so a model that answered correctly, cited the right item and then
    wrote "($416,161 million), i.e. approximately $416.2 billion" was judged to
    have fabricated 416.2 and the whole answer was replaced with a GAP. The
    corpus could not be blamed -- the model was right and the check was wrong.
    """

    def _evidence(self) -> list[Evidence]:
        return [
            ev(
                "E158",
                "Net sales FY2025; scale=6; value=416161",
                source=src(form_type="10-K", section="", item_code=""),
            )
        ]

    def test_a_rounded_billion_restatement_is_stated(self):
        answer = (
            "Apple's net sales in FY2025 were **$416,161 million** "
            "(i.e., approximately $416.2 billion) [E158]."
        )
        g = grade_answer(answer, self._evidence())
        self.assertEqual(g.verdicts[0].provenance, STATED, g.verdicts[0].reason)
        self.assertFalse(g.gap)

    def test_a_trillion_restatement_is_stated(self):
        g = grade_answer("Net sales were 416,161 million, about 0.4 trillion [E158].",
                         self._evidence())
        self.assertFalse(g.gap, [v.reason for v in g.verdicts])

    def test_the_bare_rounded_form_is_stated(self):
        g = grade_answer("Net sales were $416.2 billion [E158].", self._evidence())
        self.assertEqual(g.verdicts[0].provenance, STATED, g.verdicts[0].reason)

    def test_a_different_number_in_another_unit_is_still_refused(self):
        # The rounding allowance must not become a general escape hatch: this
        # is not 416,161 million in any unit and any precision.
        g = grade_answer("Net sales were $999.9 billion [E158].", self._evidence())
        self.assertTrue(g.gap)
        self.assertEqual(g.verdicts[0].provenance, GAP)

    def test_precision_the_source_cannot_support_is_still_refused(self):
        g = grade_answer("Net sales were 416.16123 billion [E158].", self._evidence())
        self.assertTrue(g.gap, [v.reason for v in g.verdicts])

    def test_an_uncited_figure_is_still_refused(self):
        g = grade_answer("Net sales were 416,161 million.", self._evidence())
        self.assertTrue(g.gap)


# Last in the file, or running it as a script silently skips every TestCase
# declared after it: `unittest.main()` runs at import time on the classes
# defined so far, and the rest of the module is never reached.
if __name__ == "__main__":
    unittest.main()
