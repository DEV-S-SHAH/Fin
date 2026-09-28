"""Regression tests for issuer identity in :mod:`sandbox_engine.parser`.

Company identity is the one part of the graph that must never be guessed. A
duplicate node for the wrong reason is far more damaging than a duplicate node
for the right one: merging NVIDIA, Microsoft and Amazon into "Apple Inc"
because a filing's name came back as a filename looks like a clean-up and is in
fact a data-loss event. These tests pin the two defects that could produce it.

Run with::

    .venv/bin/python -m unittest sandbox_engine.test_parser_identity
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sandbox_engine.parser import FilingParser, _dei_value


# A minimal Workiva-style cover block. The registrant fact is in the
# inline-XBRL *attribute* form, not a <dei:...> wrapper, and its name is split
# across the element and the mixed content that follows -- which is exactly how
# NVIDIA's generator emits it.
_NVIDIA = """
<html><body><div style="display:none">
<span>Commission file number: 0-23985</span>
<ix:nonNumeric contextRef="c-1" name="dei:EntityRegistrantName" id="f-7">NVIDIA CORP</ix:nonNumeric>ORATION</span></div>
<div><span>(Exact name of registrant as specified in its charter)</span></div>
</body></html>
"""

# No block boundary and no "Exact name of" marker after the fact: the name runs
# straight into a table, which is what Microsoft's generator produces.
_MICROSOFT = """
<html><body><div>
<ix:nonNumeric name="dei:EntityRegistrantName">MICROSOFT CORPORATION</ix:nonNumeric>
</div><table><tr><td style="visibility:collapse">Unrelated</td></tr></table>
</body></html>
"""


class DeiValueTest(unittest.TestCase):
    def test_attribute_form_fact(self) -> None:
        self.assertEqual(_dei_value(_NVIDIA), "NVIDIA CORPORATION")

    def test_word_split_across_elements_is_rejoined(self) -> None:
        # Substituting a space for the wrapper would yield "NVIDIA CORP ORATION".
        self.assertNotIn("CORP ORATION", _dei_value(_NVIDIA))

    def test_stops_at_block_boundary(self) -> None:
        # Without the stop, the window runs on into the table and picks up
        # "Unrelated".
        self.assertEqual(_dei_value(_MICROSOFT), "MICROSOFT CORPORATION")

    def test_absent_fact_is_empty_not_an_exception(self) -> None:
        self.assertEqual(_dei_value("<html><body>nothing here</body></html>"), "")


class CompanyNameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = FilingParser()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _path(self, name: str) -> Path:
        return self.tmp / name

    def test_dead_dei_branch_is_reachable(self) -> None:
        # The old regex searched a text rendering in which every tag had
        # already been stripped, so the element name it looked for could not
        # exist. Assert the branch actually fires rather than just that it
        # returns something truthy.
        path = self._path("10-K_2026-02-25_nvda-20260125.htm")
        name, source = self.parser._company_name(_NVIDIA, _NVIDIA, _NVIDIA, path)
        self.assertEqual(name, "NVIDIA CORPORATION")
        self.assertEqual(source, "dei_registrant_name")

    def test_never_returns_the_filename(self) -> None:
        # A filing with no usable cover page used to yield
        # "10-K_2026-02-25_nvda-20260125" as the issuer's legal name.
        path = self._path("10-K_2026-02-25_nvda-20260125.htm")
        empty = "<html><body>no cover page at all</body></html>"
        name, source = self.parser._company_name(empty, empty, "", path)
        self.assertNotIn("10-K", name)
        self.assertNotIn("nvda-20260125", name)
        self.assertEqual(source, "ticker")

    def test_commission_file_number_still_wins(self) -> None:
        path = self._path("10-K_2025-10-31_aapl-20250927.htm")
        visible = (
            "Commission File Number: 001-36743  Apple Inc. "
            "(Exact name of Registrant as specified in its charter)"
        )
        name, source = self.parser._company_name(visible, visible, visible, path)
        self.assertEqual(name, "Apple Inc")
        self.assertEqual(source, "commission_file_number")


class TableCacheTest(unittest.TestCase):
    """A stale table cache is a wrong *ticker*, and ticker is the Company PK."""

    def test_cache_is_keyed_on_the_document(self) -> None:
        parser = FilingParser()
        first = "<html><body><table><tr><td>one</td></tr></table></body></html>"
        second = "<html><body><table><tr><td>two</td></tr></table></body></html>"
        parser.tables(first)
        after_first = parser.tables(first)
        self.assertEqual(len(parser.tables(second)), 1)
        # The first document must still parse to its own single table, and
        # re-reading it must not hand back the second document's.
        self.assertEqual(len(after_first), 1)
        self.assertEqual(parser.tables(first)[0].iloc[0, 0], "one")

    def test_extract_metadata_does_not_leak_between_documents(self) -> None:
        # extract_metadata is public and does not clear the cache; only
        # ingest_file used to. This is the call shape that produced one
        # issuer's ticker on another's filing.
        parser = FilingParser()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        a, b = tmp / "a.htm", tmp / "b.htm"
        a.write_text("<html><body>alpha</body></html>")
        b.write_text("<html><body>beta</body></html>")
        first = parser.extract_metadata(a.read_text(), a)
        second = parser.extract_metadata(b.read_text(), b)
        self.assertNotEqual(first["ticker"], second["ticker"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
