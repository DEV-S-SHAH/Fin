"""Scope resolution over the committed document tree.

This is the test that fails on a fresh clone when the dataset was not committed,
which is the one failure a new machine cannot diagnose on its own: the pipeline
raises ``FileNotFoundError`` naming a path that plainly exists on the author's
laptop. It also pins the invariant the pipeline relies on -- the folder tree is
the scope, so every company contributes the same set of form folders.

Run with::

    .venv/bin/python -m unittest tests.test_ingestion
"""

from __future__ import annotations

import unittest
from pathlib import Path

from sandbox_engine.config import DATA_DIR, FORM_FOLDERS, resolve_scope

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_TREE = REPO_ROOT / DATA_DIR


class ScopeResolutionTest(unittest.TestCase):
    def test_document_tree_is_committed(self) -> None:
        """The corpus must be in version control, not just on one machine."""
        self.assertTrue(
            DOC_TREE.is_dir(),
            f"{DATA_DIR} is missing from the repository, so a fresh clone has "
            f"nothing to ingest; commit the document tree",
        )
        self.assertTrue(
            any(DOC_TREE.rglob("*.htm")),
            f"no .htm filings under {DATA_DIR}; commit the document tree",
        )

    def test_scope_covers_every_company_and_form(self) -> None:
        """Every company directory must contribute all three form folders.

        A company added with only a 10-K would otherwise ingest silently and the
        graph would carry a company whose quarterly questions have no answer.
        """
        companies = sorted(
            path for path in DOC_TREE.iterdir() if path.is_dir() and not path.name.startswith(".")
        )
        self.assertTrue(companies, f"no company directories under {DATA_DIR}")
        for company in companies:
            years = [
                year for year in company.iterdir()
                if year.is_dir() and year.name.isdigit()
            ]
            self.assertTrue(years, f"{company.name} has no <year> directory")
            for year in years:
                for form in FORM_FOLDERS:
                    folder = year / form
                    self.assertTrue(
                        folder.is_dir(),
                        f"{company.name}/{year.name} is missing the {form}/ "
                        f"folder; the resolver would skip those filings silently",
                    )

    def test_every_scoped_filing_exists_on_disk(self) -> None:
        """A scope entry that is not a real file is a broken clone, not a run."""
        filings = resolve_scope(REPO_ROOT)
        self.assertTrue(filings)
        for path in filings:
            self.assertTrue(
                path.is_file(), f"scope names a missing filing: {path}"
            )

    def test_form_folder_order_is_preserved(self) -> None:
        """10-K before 10-Q before 8-K, so a first-write-wins load is stable."""
        filings = resolve_scope(REPO_ROOT)
        order = {form: index for index, form in enumerate(FORM_FOLDERS)}
        ranks = [
            order[path.parent.name]
            for path in filings
            if path.parent.name in order
        ]
        self.assertEqual(ranks, sorted(ranks))


if __name__ == "__main__":
    unittest.main()
