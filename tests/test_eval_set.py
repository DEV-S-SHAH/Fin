"""Tests for :mod:`sandbox_engine.eval_set` and the guards it makes runnable.

D5, in the eval set's own defect register: *"Benchmark suite is blind to all
of this. 5/5 passed with D1-D3 present. Green benchmarks are not a correctness
signal; the D-a5 and D-a6 guards are the real tests."* These are those guards,
plus the parsing and scoring the metric needs in order to mean anything.

The last class runs against the real graph, and skips when it is not built --
the same bargain ``test_document_loader`` strikes for its PDF. A guard that
silently passes because it had nothing to run against is the failure mode D5
is complaining about, so it skips loudly instead.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_engine import query_ui
from sandbox_engine.eval_set import (
    EVAL_SET_PATH,
    EvalAnswer,
    load_eval_set,
    parse_eval_set,
    provenance_match_rate,
    run,
    score,
)
from sandbox_engine.provenance import DERIVED, EXTERNAL, GAP, INFERRED, STATED


class ParseTests(unittest.TestCase):
    """The document is the source of truth, so the parser is part of the spec.

    A second hand-typed list of the same 30 questions would drift from the
    document the reader consults, and a document nothing reads cannot be
    corrected. So these assert the document's own shape: the counts in its
    section headings, and the two rows whose expected tags are not a plain set.
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.questions = load_eval_set()
        except FileNotFoundError:
            raise unittest.SkipTest("EVAL_SET.md not found in package")

    def test_the_whole_set_parses(self):
        self.assertEqual(len(self.questions), 30)

    def test_section_headings_agree_with_the_rows_under_them(self):
        # The headings promise 8/7/8/5/2. If a row is added or removed and the
        # heading is not, the document is lying to its reader and nothing here
        # would notice.
        expected = {"STATED": 8, "GAP": 7, "DERIVED": 8, "INFERRED": 5, "EXTERNAL": 2}
        counts: dict[str, int] = {}
        for question in self.questions:
            counts[question.section] = counts.get(question.section, 0) + 1
        self.assertEqual(counts, expected)
        self.assertEqual(sum(counts.values()), 30)

    def test_ids_are_unique_and_questions_are_not_repeated(self):
        ids = [q.id for q in self.questions]
        self.assertEqual(len(ids), len(set(ids)))
        asked = [q.question for q in self.questions]
        self.assertEqual(len(asked), len(set(asked)))

    def test_every_row_names_only_real_tags(self):
        for question in self.questions:
            for alternative in question.acceptable:
                self.assertTrue(alternative, question.id)
                for tag in alternative:
                    self.assertIn(tag, (STATED, DERIVED, INFERRED, EXTERNAL, GAP), question.id)

    def test_a_row_offering_a_choice_accepts_either_alternative(self):
        # E1 reads "STATED or GAP". Comparing against only the first would score
        # a correct GAP answer as a failure, so the row carries both sets.
        row = next(q for q in self.questions if q.id == "E1")
        self.assertEqual(len(row.acceptable), 2)
        self.assertTrue(row.matches([STATED]))
        self.assertTrue(row.matches([GAP]))
        self.assertFalse(row.matches([STATED, GAP]))
        self.assertFalse(row.matches([DERIVED]))

    def test_a_row_naming_two_tags_needs_both(self):
        # D-a5 is "DERIVED + GAP": a refusal that shows no arithmetic is not
        # the same answer as one that does, and neither alone is the contract.
        row = next(q for q in self.questions if q.id == "D-a5")
        self.assertEqual(row.tags, frozenset({DERIVED, GAP}))
        self.assertTrue(row.matches([DERIVED, GAP]))
        self.assertFalse(row.matches([DERIVED]))
        self.assertFalse(row.matches([GAP]))
        self.assertFalse(row.matches([STATED]))

    def test_an_unknown_tag_is_refused_rather_than_ignored(self):
        # Silently dropping a tag nobody recognises would turn a typo in the
        # document into a row that is unpassable rather than wrong.
        with self.assertRaises(ValueError):
            parse_eval_set(
                "| # | Question | Expected tags | Accept when | status |\n"
                "|---|---|---|---|---|\n"
                "| X1 | Something? | CONFABULATED | no | PASS |\n"
            )

    def test_a_document_with_no_rows_is_an_error_not_an_empty_set(self):
        # An empty eval set scores 1.0 or 0.0 depending on the division and
        # reports success either way, which is the opposite of useful.
        with self.assertRaises(ValueError):
            parse_eval_set("# Nothing here\n\nProse only.\n")

    def test_the_eval_set_is_shipped_with_the_package(self):
        self.assertTrue(EVAL_SET_PATH.exists(), EVAL_SET_PATH)


class MatchRateTests(unittest.TestCase):
    """``provenance_match_rate``, the metric the document calls the headline.

    The document's scoring section is a definition nobody could check, because
    the function it names did not exist. It exists now, and these pin the two
    behaviours that decide whether the number can be believed: an answer that is
    right but tagged wrong must not match, and a question nobody answered must
    not be quietly excluded.
    """

    def setUp(self):
        try:
            self.questions = load_eval_set()
        except FileNotFoundError:
            self.skipTest("EVAL_SET.md not found in package")

    def _answers(self, by_id):
        return {
            qid: EvalAnswer(question_id=qid, tags=frozenset(tags))
            for qid, tags in by_id.items()
        }
    def test_a_matching_tag_mix_scores(self):
        answers = self._answers({"S1": [STATED], "G1": [GAP], "D-a1": [DERIVED]})
        matched, total = provenance_match_rate(self.questions, answers)
        self.assertEqual((matched, total), (3, 30))

    def test_a_right_number_tagged_wrongly_is_not_a_match(self):
        # The document's own words: "An answer that is *right* but tagged
        # STATED when GAP was required is a failure."
        answers = self._answers({"G1": [STATED]})
        matched, _ = provenance_match_rate(self.questions, answers)
        self.assertEqual(matched, 0)

    def test_an_extra_tag_the_contract_did_not_ask_for_is_not_a_match(self):
        answers = self._answers({"S1": [STATED, INFERRED]})
        matched, _ = provenance_match_rate(self.questions, answers)
        self.assertEqual(matched, 0)

    def test_an_unanswered_question_is_counted_and_cannot_match(self):
        # Otherwise a run that asked nothing scores 1.0 and the headline means
        # nothing at all.
        matched, total = provenance_match_rate(self.questions, {})
        self.assertEqual((matched, total), (0, 30))

    def test_a_question_the_runner_failed_on_is_not_a_pass(self):
        answers = {"S1": EvalAnswer(question_id="S1", error="backend unreachable")}
        result = score(self.questions, answers)
        self.assertEqual(result.matched, 0)
        self.assertIn(("S1", "backend unreachable"), result.errors)
        self.assertEqual(result.total, 30)


class VerdictPassthroughTests(unittest.TestCase):
    """The grader's verdict reaches the eval run, and stays its own axis.

    The tag mix and the verdict answer different questions: "did the model
    produce the tags the document expected" and "was this answer allowed to say
    that". An answer can be all-STATED and still be refused, and one can carry
    the expected tags and still be a fabrication. Folding either into the other
    would hide that, so both are reported and neither moves the other.
    """

    def setUp(self):
        try:
            self.questions = load_eval_set()
        except FileNotFoundError:
            self.skipTest("EVAL_SET.md not found in package")

    def _response(self, tags, verdict=None, **extra):
        body = {
            "answer": "text",
            "provenance": [
                {"text": "a sentence", "provenance": t, "cites": ["E1"]}
                for t in tags
            ],
        }
        if verdict is not None:
            body["verdict"] = verdict
        body.update(extra)
        return body

    def test_a_refused_answer_is_counted_and_the_rate_is_untouched(self):
        # E1 accepts STATED, so this row matches the expected mix. The answer is
        # still refused, and the rate must not move.
        before = run(lambda _q: self._response([STATED], "SUPPORTED"), self.questions)
        after = run(lambda _q: self._response([STATED], "REFUSED"), self.questions)
        self.assertEqual(before.refused, 0)
        self.assertEqual(after.refused, 30)
        self.assertEqual(
            after.matched, before.matched,
            "a refused answer still matched the expected tag mix, which is the point",
        )
        self.assertEqual(after.provenance_match_rate, before.provenance_match_rate)

    def test_a_missing_verdict_is_unknown_and_never_a_pass(self):
        result = run(lambda _q: self._response([STATED]), self.questions)
        self.assertEqual(result.refused, 0)
        self.assertEqual(
            result.unreported_verdict, 30,
            "an absent verdict must be reported as unknown, not counted as supported",
        )
        self.assertIn("verdict not reported", result.summary())

    def test_the_summary_names_the_refusal_count(self):
        result = run(lambda _q: self._response([STATED], "REFUSED"), self.questions)
        self.assertIn("refused by the grader  30", result.summary())


class SecondaryMetricTests(unittest.TestCase):
    """The targets of zero.

    An answer can carry the right tags and still do the things the contract
    exists to prevent. Those are counted, not averaged, because there is no
    acceptable number of them.
    """

    def setUp(self):
        try:
            self.questions = load_eval_set()
        except FileNotFoundError:
            self.skipTest("EVAL_SET.md not found in package")
        self.answers = {
            "S1": EvalAnswer(
                question_id="S1",
                tags=frozenset({STATED}),
                ungrounded_figures=("416,161",),
                misattributed=("MSFT",),
            )
        }

    def test_a_matching_tag_mix_does_not_hide_a_grounded_figure(self):
        result = score(self.questions, self.answers)
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.ungrounded_figures, [("S1", ("416,161",))])

    def test_a_misattribution_is_reported_next_to_the_match_rate(self):
        result = score(self.questions, self.answers)
        self.assertEqual(result.misattributed, [("S1", ("MSFT",))])

    def test_the_summary_names_the_rate_and_the_zero_targets(self):
        text = score(self.questions, self.answers).summary()
        self.assertIn("provenance_match_rate", text)
        self.assertIn("1/30", text)
        self.assertIn("ungrounded figures     1", text)
        self.assertIn("misattributed issuers  1", text)


class RunLoopTests(unittest.TestCase):
    """The loop, driven by a stub. This is the part that needed a model before."""

    def setUp(self):
        try:
            self.questions = load_eval_set()
        except FileNotFoundError:
            self.skipTest("EVAL_SET.md not found in package")

    def _response(self, tags, cites=("E1",), **extra):
        return {
            "answer": "text",
            "provenance": [
                {"text": "a sentence", "provenance": t, "cites": list(cites)} for t in tags
            ],
            **extra,
        }

    def test_each_question_is_asked_once_and_scored(self):
        asked: list[str] = []

        def ask(question):
            asked.append(question)
            row = next(r for r in self.questions if r.question == question)
            return self._response(sorted(row.tags))

        result = run(ask, self.questions)
        self.assertEqual(len(asked), 30)
        self.assertEqual(result.matched, 30)
        self.assertEqual(result.provenance_match_rate, 1.0)

    def test_a_factual_sentence_with_no_citation_is_counted(self):
        def ask(_):
            return self._response([STATED], cites=())

        result = run(ask, self.questions)
        # The stub always says STATED, so it matches the nine rows that ask for
        # STATED and nothing else. What matters is that every reply's uncited
        # sentence is still on the list: a right tag mix does not excuse a
        # factual claim with nothing behind it.
        self.assertEqual(result.matched, 9)
        self.assertEqual(len(result.uncited_sentences), 30)

    def test_a_cited_sentence_is_not_counted_as_uncited(self):
        def ask(_):
            return self._response([STATED], cites=("E1",))

        self.assertEqual(run(ask, self.questions).uncited_sentences, [])

    def test_a_raising_backend_is_scored_not_swallowed(self):
        def ask(_):
            raise ConnectionError("backend unreachable")

        result = run(ask, self.questions)
        self.assertEqual(result.matched, 0)
        self.assertEqual(result.total, 30)
        self.assertEqual(len(result.errors), 30)
        self.assertIn("backend unreachable", result.errors[0][1])


class RealGraphGuardTests(unittest.TestCase):
    """The D-a5 guard, run against the graph.

    D5 named D-a5 as a real test. Its question -- "How many times bigger is
    Apple's revenue than Microsoft's?" -- is the one the corpus cannot answer,
    and it is unanswerable for a structural reason: the two issuers' periods
    end on different dates, so no period key is shared and there is nothing to
    divide. Under D2, when every Apple quarter carried the FY2025 annual window,
    a shared key existed and the ratio became spuriously computable. So the
    guard is the property itself, not a number: the evidence for a cross-issuer
    question must never put two issuers on one period.

    Asserted structurally rather than against the document's figures, which
    belong to a corpus snapshot and go stale when the graph is rebuilt.
    """

    @classmethod
    def setUpClass(cls):
        path = query_ui.resolve_db_path()
        if not Path(path).exists():
            raise unittest.SkipTest(f"graph not built at {path}")
        try:
            cls.questions = load_eval_set()
        except FileNotFoundError:
            raise unittest.SkipTest("EVAL_SET.md not found in package")
        cls.kg = query_ui.KnowledgeGraph(path)
        cls.evidence = cls._evidence_for(
            "How many times bigger is Apple's revenue than Microsoft's?"
        )

    @classmethod
    def tearDownClass(cls):
        kg = getattr(cls, "kg", None)
        if kg is not None:
            kg.db.close()

    @classmethod
    def _evidence_for(cls, question):
        from sandbox_engine.provenance import build_evidence

        nodes, edges, tag_map, _seeds = query_ui.retrieve_financial_context(
            cls.kg, question
        )
        return build_evidence(nodes, cls.kg, tag_map, edges)

    def test_the_evidence_block_spans_more_than_one_issuer(self):
        # Without this the rest of the class would pass on a corpus holding one
        # company, which is exactly the gap in the hand-built fixtures this
        # guard exists to close.
        filers = {e.source.ticker for e in self.evidence if e.source.ticker}
        self.assertGreaterEqual(len(filers), 2, filers)
        self.assertIn("AAPL", filers)
        self.assertIn("MSFT", filers)
    def test_no_period_key_is_shared_between_the_two_issuers(self):
        import re

        period = re.compile(r"\(([^)]*\d{4}[^)]*)\)")
        by_filer: dict[str, set[str]] = {}
        for line in self.evidence:
            found = period.search(line.text)
            if found and line.source.ticker:
                by_filer.setdefault(line.source.ticker, set()).add(found.group(1))
        self.assertIn("AAPL", by_filer, "no periodised Apple line in the evidence")
        self.assertIn("MSFT", by_filer, "no periodised Microsoft line in the evidence")
        shared = by_filer["AAPL"] & by_filer["MSFT"]
        self.assertEqual(
            shared, set(), f"the two issuers share a period, so a ratio is computable: {shared}"
        )

    def test_the_unanswerable_ratio_is_refused_by_the_grader(self):
        # The refusal has to survive the grader, not just be the right idea.
        # A refusal is two sentences: the filed figures, cited and STATED, then
        # the conclusion that they do not divide, hedged and citing nothing --
        # which is the only shape the contract accepts. A bare hedged sentence
        # is a GAP, and so is one that quietly states a ratio.
        from sandbox_engine.provenance import grade_answer

        apple, microsoft = self._periodised("Net Sales", ("AAPL", "MSFT"))
        answer = (
            f"Apple reported net sales of {self._value(apple)} and Microsoft reported "
            f"{self._value(microsoft)} for the periods their filings cover "
            f"[{apple.tag}] [{microsoft.tag}]. "
            "A direct ratio of the two may therefore be misleading, because the two "
            "fiscal years end on different dates."
        )
        graded = grade_answer(answer, self.evidence)
        self.assertFalse(graded.gap, [v.reason for v in graded.verdicts])
        self.assertIn(STATED, graded.mix)
        self.assertIn(INFERRED, graded.mix)

    def test_apple_figure_attributed_to_microsoft_is_refused_on_real_evidence(self):
        # The reported failure, on the real block rather than a hand-built one.
        from sandbox_engine.provenance import grade_answer

        apple, _ = self._periodised("Net Sales", ("AAPL", "MSFT"))
        graded = grade_answer(
            f"Microsoft's net sales were {self._value(apple)} [{apple.tag}].", self.evidence
        )
        verdict = graded.verdicts[0]
        self.assertEqual(verdict.provenance, GAP, verdict.reason)
        self.assertEqual(verdict.misattributed, ["MSFT"])

    # -- helpers ----------------------------------------------------------

    def _periodised(self, metric, filers):
        """The periodised evidence line for *metric*, one per filer in *filers*."""
        import re

        found = {}
        for line in self.evidence:
            key = next((f for f in filers if line.source.ticker == f), None)
            if key and key not in found and metric in line.text and "value=" in line.text:
                found[key] = line
        missing = [f for f in filers if f not in found]
        if missing:
            self.skipTest(f"no periodised {metric} line for {missing}")
        return tuple(found[f] for f in filers)

    @staticmethod
    def _value(line):
        import re

        return re.search(r"value=([\d,\.]+)", line.text).group(1)


if __name__ == "__main__":
    unittest.main()
