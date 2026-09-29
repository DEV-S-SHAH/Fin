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

import pandas as pd
from pathlib import Path

from sandbox_engine.parser import (
    FilingParser,
    TableCell,
    _dei_value,
    detect_period_groups,
    extract_cells,
    stable_id,
)
from sandbox_engine.entity_resolver import ConceptRegistry


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


class PeriodBannerTests(unittest.TestCase):
    """Splitting a date across cells must not collapse a period to its year.

    Microsoft prints a header as ``Three Months Ended September 30, | 2025``,
    the day in the caption column and the year in the value column. Read cell by
    cell, the year alone keys the period ``FY2025`` -- the calendar year, not the
    September fiscal year -- and the 3M and 9M columns of one table collide under
    it. The caption has to be rejoined to the year it announces.
    """

    def test_split_cell_header_rejoins_day_and_year(self) -> None:
        frame = pd.DataFrame(
            {
                0: ["Three Months Ended September 30,", None, "Revenue:", "Total"],
                1: ["2025", "2025", "", ""],
                2: ["2024", "2024", "", ""],
            }
        )
        _, groups = detect_period_groups(frame, year_end=(6, 30))
        keys = {group.key: group for group in groups}
        self.assertEqual(set(keys), {"2025-09-30", "2024-09-30"})
        self.assertEqual(keys["2025-09-30"].duration, "3M")
        self.assertEqual(keys["2025-09-30"].full_key, "3M-2025-09-30")

    def test_a_full_date_cell_is_left_alone(self) -> None:
        frame = pd.DataFrame(
            {0: ["Total", "Total"], 1: ["Sep 27, 2025", ""], 2: ["Sep 28, 2024", ""]}
        )
        _, groups = detect_period_groups(frame, year_end=(9, 28))
        keys = {group.key for group in groups}
        self.assertEqual(keys, {"2025-09-27", "2024-09-28"})

    def test_filing_period_end_resolves_a_bare_year(self) -> None:
        # A "Years Ended 2026" column with no date anywhere in the row takes the
        # filing's own period end when the printed year agrees with it.
        frame = pd.DataFrame(
            {0: ["Years Ended", None, "Total", "Total"], 1: ["2026", "2026", "", ""]}
        )
        _, groups = detect_period_groups(
            frame, year_end=(6, 30), filing_period_end="2026-06-30"
        )
        self.assertEqual([g.key for g in groups], ["2026-06-30"])

    def test_a_comparative_year_keeps_its_bare_year(self) -> None:
        # 2025 is not the filing's year, so there is no date to recover and the
        # period must stay a bare year instead of being invented.
        frame = pd.DataFrame(
            {0: ["Years Ended", None, "Total", "Total"], 1: ["2025", "2025", "", ""]}
        )
        _, groups = detect_period_groups(
            frame, year_end=(6, 30), filing_period_end="2026-06-30"
        )
        self.assertEqual([g.key for g in groups], ["FY2025"])


class SegmentNameTests(unittest.TestCase):
    """A period banner is not a segment.

    "Year Ended June 30", "As of June 30" and "Three Months Ended September 30"
    are the leftmost cells of balance-sheet and income-statement headers. They
    carry a month and day but no year, so :data:`_PERIOD_RE` misses them and they
    land in the ``Segment`` table as if a country were a reporting unit -- on a
    July close the same three headers appear in every issuer's filing and merge
    into one cross-company segment node.
    """

    def test_period_banners_are_rejected(self) -> None:
        parser = FilingParser()
        for label in (
            "Year Ended June 30",
            "Three Months Ended September 30",
            "As of June 30",
        ):
            with self.subTest(label=label):
                self.assertEqual(parser._segment_name(label), "")

    def test_real_segments_still_pass(self) -> None:
        parser = FilingParser()
        for label in ("United States", "Other countries", "iPhone"):
            with self.subTest(label=label):
                self.assertNotEqual(parser._segment_name(label), "")


class PercentIdentityTests(unittest.TestCase):
    """A margin percent and the same dollar figure are different facts.

    NVDA prints ``73.4 %`` as separate cells -- the ``%`` is a unit column
    *inside* the period band, not a suffix on every margin, and a ``% of
    Revenue`` table decorates only its ``Revenue`` and ``Net income`` rows.
    Both layouts must still mark the bare margin cells percent, or the 75.0
    margin (percent) and the 75.0 in dollars would land on one node.
    """

    def _extract(self, label: str, period_band: list[str]) -> list[TableCell]:
        # band is (current [value, value, unit], prior [value, value, unit]).
        frame = pd.DataFrame(
            {
                0: ["Three Months Ended", None, label],
                1: ["Sep 30, 2025", "", period_band[0]],
                2: ["Sep 30, 2025", "", period_band[1]],
                3: ["Sep 30, 2025", "", period_band[2]],
                4: ["Sep 30, 2024", "", period_band[3]],
                5: ["Sep 30, 2024", "", period_band[4]],
                6: ["Sep 30, 2024", "", period_band[5]],
            }
        )
        _, cells = extract_cells(frame, year_end=(9, 28))
        return cells

    def test_an_in_band_unit_marker_marks_the_row_percent(self) -> None:
        cells = self._extract("Gross margin", ["73.4", "73.4", "%", "72.4", "72.4", "%"])
        by_period = {cell.period: cell for cell in cells}
        self.assertEqual(by_period["2025-09-30"].canonical_name, "Gross Margin (%)")
        self.assertEqual(by_period["2024-09-30"].canonical_name, "Gross Margin (%)")

    def test_a_currency_symbol_does_not_mark_the_row_percent(self) -> None:
        cells = self._extract("Gross margin", ["$", "73.4", "", "$", "72.4", ""])
        by_period = {cell.period: cell for cell in cells}
        self.assertEqual(by_period["2025-09-30"].canonical_name, "Gross Margin")
        self.assertEqual(by_period["2025-09-30"].number.is_percent, False)

    def test_column_level_marker_covers_undecorated_rows(self) -> None:
        # Only the Revenue row prints "%"; the margin row below is bare but is
        # part of the same percent column and must be marked by column, not row.
        frame = pd.DataFrame(
            {
                0: ["Three Months Ended", None, "Revenue", "Gross margin"],
                1: ["Sep 30, 2025", "", "100.0", "75.0"],
                2: ["Sep 30, 2025", "", "100.0", "75.0"],
                3: ["Sep 30, 2025", "", "%", ""],
                4: ["Sep 30, 2024", "", "100.0", "72.4"],
                5: ["Sep 30, 2024", "", "100.0", "72.4"],
                6: ["Sep 30, 2024", "", "%", ""],
            }
        )
        _, cells = extract_cells(frame, year_end=(9, 28))
        margin = [cell for cell in cells if "margin" in cell.label.lower()]
        self.assertEqual(
            {cell.period for cell in margin},
            {"2025-09-30", "2024-09-30"},
        )
        for cell in margin:
            self.assertEqual(cell.canonical_name, "Gross Margin (%)")

    def test_registry_keeps_percent_and_currency_apart(self) -> None:
        reg = ConceptRegistry(stable_id)
        scope = "NVDA:3M-2025-07-27"
        percent = reg.register("metric", "Gross Margin (%)", scope=scope)
        currency = reg.register("metric", "Gross Margin", scope=scope)
        self.assertNotEqual(percent.canonical_id, currency.canonical_id)
        entity = reg.partition("metric", scope).get(percent.canonical_id)
        self.assertEqual(entity.name, "Gross Margin (%)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
