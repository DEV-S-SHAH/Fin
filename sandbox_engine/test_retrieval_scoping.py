"""Regression tests for prompt-scoping in :mod:`sandbox_engine.query_ui`.

Retrieval decisions here are pure functions, tested without a database. The
layout they pin: "Compare Apple and Microsoft revenue in the most recent
quarter" used to carry 251 metric nodes and 84k characters of prompt, because
every period of every issuer matched "revenue" -- and a model that has to pick
an answer out of that wall reliably picks the wrong quarter. The fix is scoping.

Run with::

    .venv/bin/python -m unittest sandbox_engine.test_retrieval_scoping
"""

from __future__ import annotations

import unittest

from sandbox_engine.query_ui import _bound_metric_periods, _names_issuer


class NamesIssuerTests(unittest.TestCase):
    def test_named_by_ticker(self) -> None:
        # q_low is lowercased at the call site, so the helper receives it so.
        self.assertTrue(_names_issuer("how did msft do?", "MSFT", "MICROSOFT CORPORATION"))

    def test_named_by_trading_name_after_suffix_strip(self) -> None:
        # "Apple Inc" never appears verbatim in "Compare Apple and Microsoft",
        # but "apple" does after the corporate suffix falls away.
        self.assertTrue(_names_issuer("compare apple and microsoft", "AAPL", "Apple Inc"))

    def test_corporate_suffix_is_not_required(self) -> None:
        self.assertTrue(_names_issuer("nvidia versus apple", "NVDA", "NVIDIA CORPORATION"))

    def test_unrelated_question_names_no_issuer(self) -> None:
        self.assertFalse(_names_issuer("what is the inflation outlook", "AAPL", "Apple Inc"))
        self.assertFalse(_names_issuer("how stable is the market", "MSFT", "MICROSOFT CORPORATION"))

    def test_short_names_do_not_match_ordinary_words(self) -> None:
        # A two-letter remainder is too likely to be a real word.
        self.assertFalse(_names_issuer("traveled 3M miles", "MMM", "3M Company"))


class BoundMetricPeriodsTests(unittest.TestCase):
    def setUp(self) -> None:
        # accession -> ticker for every filing that reports something below.
        self.filer: dict[str, str] = {}

    def _filer(self, acc: str, tick: str) -> None:
        self.filer[acc] = tick

    def _candidate(self, mid: str, name: str, pend: str) -> tuple:
        return (mid, name, "income_statement", "lin", pend, "net sales")

    def test_issuers_do_not_crowd_each_other_out(self) -> None:
        # Apple and Microsoft must both survive a comparison query: the cap
        # applies per issuer, not to the concept as a whole.
        self._filer("a1", "AAPL")
        self._filer("m1", "MSFT")
        metric_filers = {"qa": {"a1"}, "qm": {"m1"}}
        candidates = [
            self._candidate("qa", "Net Sales (3M-2026-06-27)", "2026-06-27"),
            self._candidate("qm", "Net Sales (3M-2026-03-31)", "2026-03-31"),
        ]
        kept = _bound_metric_periods(candidates, "most recent quarter", self.filer, metric_filers)
        self.assertEqual({k[0] for k in kept}, {"qa", "qm"})

    def test_only_newest_periods_of_an_issuer_are_kept(self) -> None:
        self._filer("f1", "AAPL")
        self._filer("f2", "AAPL")
        self._filer("f3", "AAPL")
        self._filer("f4", "AAPL")
        metric_filers = {f"q{i}": {f"f{i}"} for i in (1, 2, 3, 4)}
        candidates = [
            self._candidate("q1", "Net Sales (3M-2026-06-27)", "2026-06-27"),
            self._candidate("q2", "Net Sales (3M-2026-03-28)", "2026-03-28"),
            self._candidate("q3", "Net Sales (3M-2025-12-27)", "2025-12-27"),
            self._candidate("q4", "Net Sales (3M-2025-09-27)", "2025-09-27"),
        ]
        kept = _bound_metric_periods(candidates, "latest quarter", self.filer, metric_filers)
        self.assertEqual([k[0] for k in kept], ["q1", "q2", "q3"])

    def test_a_series_question_gets_a_larger_cap(self) -> None:
        for i in range(20):
            self._filer(f"f{i}", "AAPL")
        metric_filers = {f"q{i}": {f"f{i}"} for i in range(20)}
        candidates = [
            self._candidate(f"q{i}", f"Net Sales (3M-202{i}-06-2{i})", f"202{i}-06-2{i}")
            for i in range(20)
        ]
        kept_narrow = _bound_metric_periods(candidates, "latest quarter", self.filer, metric_filers)
        kept_series = _bound_metric_periods(candidates, "quarterly revenue trend", self.filer, metric_filers)
        self.assertEqual(len(kept_narrow), 3)
        self.assertGreater(len(kept_series), 3)

    def test_undated_periods_sort_last(self) -> None:
        # With five periods for one issuer and a cap of three, the undated and
        # the oldest dated periods are the ones cut -- the newest first, the
        # open-ended one last of all.
        for i in range(1, 6):
            self._filer(f"f{i}", "AAPL")
        metric_filers = {f"q{i}": {f"f{i}"} for i in range(1, 5)} | {"qn": {"f5"}}
        candidates = [
            self._candidate("qn", "Net Sales (FY2024)", None),
            self._candidate("q1", "Net Sales (3M-2026-06-27)", "2026-06-27"),
            self._candidate("q2", "Net Sales (3M-2026-03-28)", "2026-03-28"),
            self._candidate("q3", "Net Sales (3M-2025-12-27)", "2025-12-27"),
            self._candidate("q4", "Net Sales (3M-2025-09-27)", "2025-09-27"),
        ]
        kept = _bound_metric_periods(candidates, "latest quarter", self.filer, metric_filers)
        self.assertEqual([k[0] for k in kept], ["q1", "q2", "q3"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()