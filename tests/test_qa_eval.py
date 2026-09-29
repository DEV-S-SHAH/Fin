"""Tests for :mod:`sandbox_engine.qa_eval`, the QA question table.

The harness asks ~60 questions against a live server and grades the answers. The
table is the part that can be wrong in a way nothing notices, because a wrong
row does not fail loudly -- it scores a correct answer as a failure and reports
a pass rate that means nothing.

The specific trap: a **negative control must be a fact the corpus does not
hold**. NVIDIA sat in the negative block for as long as it was split into an
``UNKNOWN`` Company node, which was true then and stopped being true when D1 was
fixed. After that it was an answerable question, so the row scored a correct
answer as "model answered an out-of-context fact" and rewarded a refusal on a
question the graph covers. A green run said nothing because the run could not
distinguish those two outcomes.

The other property: the table was 100% Apple, so it could not see a figure read
off one filer's line and attributed to another. That answer is fully grounded --
the number is in a cited source -- and no same-issuer question can detect it.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_engine import query_ui
from sandbox_engine.qa_eval import QUESTIONS, grade, is_unanswerable, norm

#: Words that name a filer, checked against the questions rather than the
#: category. A positive may name any filer; a negative may name none of them.
ISSUER_WORDS = ("Apple", "Microsoft", "NVIDIA")


class TableTests(unittest.TestCase):
    """Shape. Cheap, and it catches an edit that leaves the table half-updated."""

    def setUp(self):
        self.rows = [(cat, q, expect, neg) for cat, q, expect, neg in QUESTIONS]

    def test_every_row_is_a_four_tuple_with_a_category(self):
        for row in QUESTIONS:
            self.assertEqual(len(row), 4, row)
        categories = {cat for cat, _q, _e, _n in self.rows}
        self.assertTrue(categories <= {"fact", "multihop", "structural", "narrative", "causal", "negative"}, categories)

    def test_questions_are_unique(self):
        asked = [q for _c, q, _e, _n in self.rows]
        duplicates = {q for q in asked if asked.count(q) > 1}
        self.assertEqual(duplicates, set())

    def test_a_positive_names_what_it_expects(self):
        # ``grade`` compares the expected token against the whole answer, so a
        # positive with no token is a question that passes on any grounded
        # answer at all -- it tests the citation, never the fact.
        for cat, question, expect, negative in self.rows:
            if negative:
                continue
            self.assertIsNotNone(expect, f"{cat}: {question}")
            self.assertNotEqual(expect.strip(), "", question)

    def test_a_negative_expects_no_token(self):
        for cat, question, expect, negative in self.rows:
            if negative:
                self.assertIsNone(expect, f"{cat}: {question}")

    def test_the_table_covers_more_than_one_issuer(self):
        # The gap that let a cross-issuer misattribution pass: with one filer in
        # the table there is nothing to attribute a figure to.
        named = set()
        for _cat, question, _e, _n in self.rows:
            named.update(w for w in ISSUER_WORDS if w.lower() in question.lower())
        self.assertEqual(
            named, set(ISSUER_WORDS), f"questions only name {sorted(named)}"
        )

    def test_questions_name_an_issuer_or_a_topic(self):
        # Cheap sanity on the wording, kept here so a new row is obviously one
        # or the other rather than accidentally neither.
        for cat, question, _e, negative in self.rows:
            self.assertTrue(len(question) > 20, (cat, question))
            self.assertTrue(
                question.endswith("?") or not negative, (cat, question)
            )


class NegativeControlTests(unittest.TestCase):
    """A negative control must be a fact the corpus does not hold.

    Not a fact about a filer the corpus lacks: several valid controls name a
    filer that *is* in the corpus, scoped to a period ("Apple's net sales for
    fiscal year 2018") or to a fact the filings do not carry ("how many
    employees"). The row this guards is the one where those two got confused --
    a control that sat in the negative block because NVIDIA used to be
    unresolvable, and stayed there correct-by-coincidence after D1 made it
    resolvable.

    So the check is the fact, not the filer: no evidence line from a filer the
    question names may carry a value for a period the question names, for a
    metric the question names. It runs the real retrieval, so it skips when the
    graph is not built rather than passing on an empty list -- a control that
    verified nothing because there was nothing to verify is the failure this
    class exists for.
    """

    #: Words that say nothing about *which* fact was asked. The first group is
    #: the question frame ("for fiscal year 2025", "in the three months ended
    #: ..."), which is shared by every question of that shape; the second is
    #: generic measure words, which people add freely to a metric's name
    #: ("R&D expense" for a line called "Research and Development"). Keeping them
    #: in would make the subject empty enough to match anything, or strict
    #: enough to match nothing.
    STOPWORDS = frozenset(
        [
            "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
            "for", "from", "had", "has", "have", "how", "in", "is", "it", "its",
            "many", "much", "of", "on", "or", "per", "that", "the", "their",
            "there", "these", "this", "to", "was", "were", "what", "when", "where",
            "which", "who", "whose", "with", "would",
        ]
    )
    NOISE = frozenset(
        [
            "fiscal", "year", "years", "quarter", "quarterly", "months", "month",
            "ended", "ending", "twelve", "six", "three", "nine", "annual",
            "figure", "figures", "value", "values", "total", "amount",
            "expense", "expenses", "cost", "costs", "reported",
        ]
    )

    @classmethod
    def setUpClass(cls):
        from sandbox_engine import query_ui

        path = query_ui.resolve_db_path()
        if not Path(path).exists():
            raise unittest.SkipTest(f"graph not built at {path}")
        cls.kg = query_ui.KnowledgeGraph(path)
        result = cls.kg.conn.execute(
            "MATCH (c:Company) RETURN c.ticker AS ticker, c.name AS name"
        )
        names = result.get_column_names()
        cls.issuers = []
        while result.has_next():
            row = dict(zip(names, result.get_next()))
            cls.issuers.append((str(row["ticker"]), str(row["name"] or "")))
        result.close()

    @classmethod
    def tearDownClass(cls):
        kg = getattr(cls, "kg", None)
        if kg is not None:
            kg.db.close()

    def _evidence_for(self, question):
        from sandbox_engine.provenance import build_evidence

        nodes, edges, tag_map, _s = query_ui.retrieve_financial_context(
            self.kg, question
        )
        return build_evidence(nodes, self.kg, tag_map, edges)

    @classmethod
    def _asked_words(cls, question):
        """The words that name the fact asked for, and nothing else.

        A line is treated as holding the asked fact only when its metric name
        carries *every* one of these. Requiring all of them rather than any is
        what keeps the check sound in the near-miss case: "net sales" and
        "proceeds from sales of marketable securities" share "sales", and a
        control scoped to the first would be flagged by a line about the second.
        """
        words = {
            w
            for w in re.findall(r"[A-Za-z]+", question.lower())
            if len(w) >= 3 and w not in cls.STOPWORDS and w not in cls.NOISE
        }
        return words - {w.lower() for w in ISSUER_WORDS}

    def test_the_graph_was_read_and_is_not_empty(self):
        self.assertTrue(self.issuers, "the graph held no Company nodes")

    def test_no_negative_control_asks_for_a_fact_the_corpus_holds(self):
        from sandbox_engine.query_ui import _names_issuer

        checked = 0
        for _cat, question, _e, negative in QUESTIONS:
            if not negative:
                continue
            low = question.lower()
            named = [t for t, legal in self.issuers if _names_issuer(low, t, legal)]
            if not named:
                # A filer the corpus never had is unanswerable by construction.
                continue
            checked += 1
            years = set(re.findall(r"\b\d{4}\b", question))
            asked = self._asked_words(question)
            for line in self._evidence_for(question):
                if line.source.ticker not in named or "value=" not in line.text:
                    continue
                period = re.search(r"\(([^)]*)\)", line.text)
                if not years or not period:
                    continue
                if not (years & set(re.findall(r"\d{4}", period.group(1)))):
                    continue
                metric = set(re.findall(r"[A-Za-z]+", line.text.split(" —")[0].lower()))
                if asked and asked <= metric:
                    self.fail(
                        f"{question!r} is a negative control, but the corpus holds "
                        f"exactly that: [{line.tag}] {line.text[:90]}. The system will "
                        f"answer it, and the row will score a correct answer as a failure."
                    )
        self.assertTrue(checked, "no negative control named a filer in the corpus")

    def test_every_filer_is_reachable_from_the_question_table(self):
        from sandbox_engine.query_ui import _names_issuer

        asked = " ".join(q for _c, q, _e, _n in QUESTIONS).lower()
        for ticker, legal in self.issuers:
            self.assertTrue(
                _names_issuer(asked, ticker, legal),
                f"{ticker} is in the corpus but no question names it, so nothing "
                f"here exercises retrieving from its filings",
            )


class NegativeControlGradingTests(unittest.TestCase):
    """A negative control has to be able to fail.

    The control exists to catch a system that answers a question the corpus does
    not cover. If the harness cannot tell a refusal from a server that never
    answered, the control cannot fail, and the run reports a clean sweep having
    measured nothing -- which is the failure D5 was written about, reproduced
    in the harness meant to replace it.
    """

    def test_a_dead_backend_is_an_error_not_a_refusal(self):
        # An HTTP 500, a timeout, a refused connection: `ask` reports it in the
        # body and the empty answer it leaves behind has no citations, which
        # without the error flag is the exact shape of a correct refusal.
        for error in ("HTTP 500: Internal Server Error", "timed out", ""):
            with self.subTest(error=error):
                verdict, note = grade(None, True, "", False, error)
                self.assertEqual(verdict, "ERROR", note)

    def test_an_empty_answer_with_no_error_is_still_an_error(self):
        # Belt and braces for the same hole: whatever produced no text, an empty
        # answer is not evidence of a refusal.
        verdict, _ = grade(None, True, "", False, "empty answer")
        self.assertEqual(verdict, "ERROR")

    def test_a_real_refusal_still_passes(self):
        for text in (
            "That is not in the corpus.",
            "I cannot determine that from the filings.",
            "The corpus does not include it.",
        ):
            with self.subTest(text=text):
                self.assertEqual(grade(None, True, text, True)[0], "PASS", text)
        # And an ungrounded answer needs no phrasing at all.
        self.assertEqual(grade(None, True, "Tesla was $80B.", False)[0], "PASS")

    def test_a_hedge_does_not_launder_a_fabricated_figure(self):
        # The refusal phrase is real, but it is in a trailing clause and the
        # answer leads with a number. A phrase search alone waves this through,
        # which is the whole reason the figure is checked: a refusal carries no
        # number.
        verdict, note = grade(
            None, True,
            "Tesla's 2025 revenue was $97,690M, though I cannot determine that.",
            True,
        )
        self.assertEqual(verdict, "FAIL", note)
        self.assertIn("figure", note)

    def test_a_figure_free_refusal_naming_a_company_still_passes(self):
        # A refusal that mentions the company and a fiscal year must not be
        # failed for it, or the check would be unusable: "no" is the answer.
        self.assertEqual(
            grade(None, True, "Tesla's 2025 revenue is not in the corpus.", True)[0],
            "PASS",
        )

    def test_the_figure_test_ignores_structural_numbers(self):
        # "fiscal 2025" and "the 10-K" are not assertions, and failing a refusal
        # for containing one would make every negative control unpassable.
        from sandbox_engine.qa_eval import asserts_a_figure

        self.assertFalse(asserts_a_figure("Not disclosed in the FY2025 10-K."))
        self.assertTrue(asserts_a_figure("It was $97,690M."))


class GradeTests(unittest.TestCase):
    """The two verdicts, and the asymmetry that made the bad control dangerous."""

    def test_a_negative_control_fails_when_the_model_answers_it(self):
        verdict, note = grade(None, True, "NVIDIA's R&D expense was 7,054 million.", True)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("out-of-context", note)

    def test_a_negative_control_passes_on_an_explicit_refusal(self):
        verdict, _ = grade(None, True, "That figure is not in the corpus.", True)
        self.assertEqual(verdict, "PASS")

    def test_a_negative_control_passes_on_an_ungrounded_answer(self):
        verdict, _ = grade(None, True, "NVIDIA spent 7,054 on R&D.", False)
        self.assertEqual(verdict, "PASS")

    def test_a_positive_fails_when_nothing_is_cited(self):
        verdict, note = grade("331839", False, "Microsoft's net sales were 331,839.", False)
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(note, "no citations")

    def test_a_positive_fails_when_the_number_is_absent(self):
        verdict, note = grade("331839", False, "Microsoft reported net sales.", True)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("331839", note)

    def test_a_positive_passes_on_a_cited_answer_with_the_number(self):
        verdict, _ = grade("331839", False, "Microsoft's net sales were 331,839 million.", True)
        self.assertEqual(verdict, "PASS")

    def test_the_token_match_ignores_punctuation_and_case(self):
        verdict, _ = grade("$331,839", False, "microsoft net sales were 331839 usd", True)
        self.assertEqual(verdict, "PASS")

    def test_an_unanswerable_phrase_is_recognised(self):
        self.assertTrue(is_unanswerable("That is not in the corpus."))
        self.assertTrue(is_unanswerable("That cannot be answered from the filings."))
        self.assertTrue(is_unanswerable("The corpus does not include it."))
        self.assertFalse(is_unanswerable("Microsoft's net sales were 331,839."))

    def test_norm_strips_everything_but_alphanumerics(self):
        self.assertEqual(norm("$331,839 USD"), "331839usd")


if __name__ == "__main__":
    unittest.main()
