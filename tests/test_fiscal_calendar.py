"""Tests for FiscalCalendar date-range conversion methods.

Covers conversion of fiscal years and quarters to calendar date ranges
for each company's configured fiscal year-end:

- AAPL: fiscal year ends September 30
- MSFT: fiscal year ends June 30
- TSLA: fiscal year ends December 31
"""

from __future__ import annotations

import unittest
from datetime import date

from ingestion.registry import (
    DEFAULT_COMPANIES,
    FiscalCalendar,
    get_registry,
)


class TestFiscalCalendarYearBoundaries(unittest.TestCase):
    """Fiscal year start/end dates per company calendar."""

    def test_msft_year_start_and_end(self):
        """MSFT FY ends June 30: FY2025 = Jul 1 2024 - Jun 30 2025."""
        cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        self.assertEqual(cal.year_start_date(2025), date(2024, 7, 1))
        self.assertEqual(cal.year_end_date(2025), date(2025, 6, 30))
        self.assertEqual(cal.date_range_for_fiscal_year(2025),
                         (date(2024, 7, 1), date(2025, 6, 30)))

    def test_aapl_year_start_and_end(self):
        """AAPL FY ends September 30: FY2025 = Oct 1 2024 - Sep 30 2025."""
        cal = FiscalCalendar(year_end_month=9, year_end_day=30)
        self.assertEqual(cal.year_start_date(2025), date(2024, 10, 1))
        self.assertEqual(cal.year_end_date(2025), date(2025, 9, 30))
        self.assertEqual(cal.date_range_for_fiscal_year(2025),
                         (date(2024, 10, 1), date(2025, 9, 30)))

    def test_tsla_year_start_and_end(self):
        """TSLA FY ends December 31: FY2025 = Jan 1 2025 - Dec 31 2025."""
        cal = FiscalCalendar(year_end_month=12, year_end_day=31)
        self.assertEqual(cal.year_start_date(2025), date(2025, 1, 1))
        self.assertEqual(cal.year_end_date(2025), date(2025, 12, 31))
        self.assertEqual(cal.date_range_for_fiscal_year(2025),
                         (date(2025, 1, 1), date(2025, 12, 31)))

    def test_year_boundary_consecutive_years(self):
        """FY2025 ends on year_end_day; FY2026 starts the next day."""
        cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        self.assertEqual(cal.year_end_date(2025), date(2025, 6, 30))
        self.assertEqual(cal.year_start_date(2026), date(2025, 7, 1))

    def test_default_registry_companies(self):
        """DEFAULT_COMPANIES have the AGENTS.md-mandated year-ends."""
        self.assertEqual(
            DEFAULT_COMPANIES["AAPL"].fiscal_calendar.year_end_month, 9)
        self.assertEqual(
            DEFAULT_COMPANIES["AAPL"].fiscal_calendar.year_end_day, 30)
        self.assertEqual(
            DEFAULT_COMPANIES["MSFT"].fiscal_calendar.year_end_month, 6)
        self.assertEqual(
            DEFAULT_COMPANIES["MSFT"].fiscal_calendar.year_end_day, 30)
        self.assertEqual(
            DEFAULT_COMPANIES["TSLA"].fiscal_calendar.year_end_month, 12)
        self.assertEqual(
            DEFAULT_COMPANIES["TSLA"].fiscal_calendar.year_end_day, 31)

    def test_default_registry_date_ranges(self):
        """DEFAULT_COMPANIES fiscal year date ranges match AGENTS.md."""
        registry = get_registry()

        aapl = registry.get("AAPL").fiscal_calendar
        self.assertEqual(aapl.date_range_for_fiscal_year(2025),
                         (date(2024, 10, 1), date(2025, 9, 30)))

        msft = registry.get("MSFT").fiscal_calendar
        self.assertEqual(msft.date_range_for_fiscal_year(2025),
                         (date(2024, 7, 1), date(2025, 6, 30)))

        tsla = registry.get("TSLA").fiscal_calendar
        self.assertEqual(tsla.date_range_for_fiscal_year(2025),
                         (date(2025, 1, 1), date(2025, 12, 31)))


class TestFiscalCalendarQuarterBoundaries(unittest.TestCase):
    """Fiscal quarter start/end dates per company calendar."""

    def test_msft_quarters(self):
        """MSFT FY ends June 30: Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun."""
        cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        self.assertEqual(cal.quarter_start_date(2026, 1), date(2025, 7, 1))
        self.assertEqual(cal.quarter_end_date(2026, 1), date(2025, 9, 30))
        self.assertEqual(cal.quarter_start_date(2026, 2), date(2025, 10, 1))
        self.assertEqual(cal.quarter_end_date(2026, 2), date(2025, 12, 31))
        self.assertEqual(cal.quarter_start_date(2026, 3), date(2026, 1, 1))
        self.assertEqual(cal.quarter_end_date(2026, 3), date(2026, 3, 31))
        self.assertEqual(cal.quarter_start_date(2026, 4), date(2026, 4, 1))
        self.assertEqual(cal.quarter_end_date(2026, 4), date(2026, 6, 30))

    def test_aapl_quarters(self):
        """AAPL FY ends September 30: Q1=Oct-Dec, Q2=Jan-Mar, Q3=Apr-Jun, Q4=Jul-Sep."""
        cal = FiscalCalendar(year_end_month=9, year_end_day=30)
        self.assertEqual(cal.quarter_start_date(2026, 1), date(2025, 10, 1))
        self.assertEqual(cal.quarter_end_date(2026, 1), date(2025, 12, 31))
        self.assertEqual(cal.quarter_start_date(2026, 2), date(2026, 1, 1))
        self.assertEqual(cal.quarter_end_date(2026, 2), date(2026, 3, 31))
        self.assertEqual(cal.quarter_start_date(2026, 3), date(2026, 4, 1))
        self.assertEqual(cal.quarter_end_date(2026, 3), date(2026, 6, 30))
        self.assertEqual(cal.quarter_start_date(2026, 4), date(2026, 7, 1))
        self.assertEqual(cal.quarter_end_date(2026, 4), date(2026, 9, 30))

    def test_tsla_quarters(self):
        """TSLA FY ends December 31: fiscal quarters = calendar quarters."""
        cal = FiscalCalendar(year_end_month=12, year_end_day=31)
        self.assertEqual(cal.quarter_start_date(2025, 1), date(2025, 1, 1))
        self.assertEqual(cal.quarter_end_date(2025, 1), date(2025, 3, 31))
        self.assertEqual(cal.quarter_start_date(2025, 2), date(2025, 4, 1))
        self.assertEqual(cal.quarter_end_date(2025, 2), date(2025, 6, 30))
        self.assertEqual(cal.quarter_start_date(2025, 3), date(2025, 7, 1))
        self.assertEqual(cal.quarter_end_date(2025, 3), date(2025, 9, 30))
        self.assertEqual(cal.quarter_start_date(2025, 4), date(2025, 10, 1))
        self.assertEqual(cal.quarter_end_date(2025, 4), date(2025, 12, 31))

    def test_quarter_date_range(self):
        """date_range_for_fiscal_quarter returns (start, end)."""
        cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        self.assertEqual(cal.date_range_for_fiscal_quarter(2026, 1),
                         (date(2025, 7, 1), date(2025, 9, 30)))

    def test_invalid_quarter_raises(self):
        """Quarter outside 1-4 raises ValueError."""
        cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        with self.assertRaises(ValueError):
            cal.quarter_start_date(2025, 0)
        with self.assertRaises(ValueError):
            cal.quarter_start_date(2025, 5)
        with self.assertRaises(ValueError):
            cal.quarter_end_date(2025, 0)
        with self.assertRaises(ValueError):
            cal.quarter_end_date(2025, 5)


class TestFiscalCalendarCrossCompanyComparison(unittest.TestCase):
    """Same fiscal year number = different calendar periods per company."""

    def test_fy2025_different_calendar_periods(self):
        """AAPL FY2025 ends Sep 2025; MSFT FY2025 ends Jun 2025; TSLA FY2025 ends Dec 2025."""
        registry = get_registry()

        aapl_start, aapl_end = registry.get("AAPL").fiscal_calendar.date_range_for_fiscal_year(2025)
        msft_start, msft_end = registry.get("MSFT").fiscal_calendar.date_range_for_fiscal_year(2025)
        _tsla_start, tsla_end = registry.get("TSLA").fiscal_calendar.date_range_for_fiscal_year(2025)

        # All three are "FY2025" but cover different calendar windows
        self.assertNotEqual(aapl_start, msft_start)
        self.assertNotEqual(aapl_end, msft_end)
        self.assertNotEqual(aapl_end, tsla_end)

        # AAPL FY2025 ends Sep 30, 2025
        self.assertEqual(aapl_end, date(2025, 9, 30))
        # MSFT FY2025 ends Jun 30, 2025
        self.assertEqual(msft_end, date(2025, 6, 30))
        # TSLA FY2025 ends Dec 31, 2025
        self.assertEqual(tsla_end, date(2025, 12, 31))


class TestRetrieveFinancialContextFiscalCalendar(unittest.TestCase):
    """retrieve_financial_context applies per-ticker FiscalCalendar date ranges.

    Uses a mock KG connection to capture the Cypher queries and parameters,
    verifying that fiscal_year/quarter are resolved to per-issuer calendar
    date ranges via the FiscalCalendar -- never assuming fiscal year =
    calendar year.
    """

    def setUp(self):
        from unittest.mock import MagicMock
        self.kg = MagicMock()
        self._filing_calls: list[tuple[str, dict]] = []

    def _run_with_filing_mock(self, ticker, fiscal_year, fiscal_quarter=None,
                              form_type=None):
        """Run retrieve_financial_context with a filing-query capture mock."""
        from unittest.mock import MagicMock

        from sandbox_engine.query_ui import retrieve_financial_context

        kg = MagicMock()
        self._filing_calls = []

        def side_effect(*args):
            cypher = args[0]
            params = args[1] if len(args) > 1 else {}

            has_company = "c:Company" in cypher
            has_filing = "f:Filing" in cypher
            has_submitted = "SUBMITTED" in cypher

            # Filing query with company: MATCH (c:Company)-[:SUBMITTED]->(f:Filing)
            if has_submitted and has_filing:
                self._filing_calls.append((cypher, dict(params)))
                # Return a properly structured filing row:
                # accession_number, form_type, fiscal_year, fiscal_period, period_end_date
                return [["0000320193-25-000123", "10-K", 2025, "FY", "2025-09-27"]]

            # Standalone company query (no SUBMITTED)
            if has_company and not has_submitted:
                p_ticker = params.get("ticker", "")
                if p_ticker:
                    return [[p_ticker, p_ticker + " Inc.", "0000000000"]]
                return [["AAPL", "Apple Inc.", "00000320193"],
                        ["MSFT", "Microsoft Corp.", "0000789019"]]

            # Standalone filing query (no SUBMITTED)
            if has_filing and not has_submitted:
                self._filing_calls.append((cypher, dict(params)))
                return []

            # All other queries: empty
            return []

        kg.execute.side_effect = side_effect

        retrieve_financial_context(
            kg, "revenue", ticker=ticker, fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter, form_type=form_type)
        return kg

    @property
    def _submitted_filing_params(self) -> dict:
        """Params of the SUBMITTED filing query (the one with temporal filters)."""
        for _cypher, params in self._filing_calls:
            if "SUBMITTED" in _cypher:
                return params
        return {}

    @property
    def _submitted_filing_cypher(self) -> str:
        """Cypher of the SUBMITTED filing query."""
        for _cypher, params in self._filing_calls:
            if "SUBMITTED" in _cypher:
                return _cypher
        return ""

    def test_aapl_fy2025_gets_aapl_date_range(self):
        """AAPL fiscal_year=2025 resolves to Oct 1 2024 - Sep 30 2025."""
        self._run_with_filing_mock("AAPL", 2025)

        cypher = self._submitted_filing_cypher
        params = self._submitted_filing_params

        # AAPL FY2025 = Oct 1 2024 - Sep 30 2025
        self.assertIn("period_end_date >= $fy_start", cypher)
        self.assertIn("period_end_date <= $fy_end", cypher)
        self.assertEqual(params["fy_start"], "2024-10-01")
        self.assertEqual(params["fy_end"], "2025-09-30")
        # fiscal_year filter is also kept
        self.assertEqual(params["fiscal_year"], 2025)

    def test_msft_fy2025_gets_msft_date_range(self):
        """MSFT fiscal_year=2025 resolves to Jul 1 2024 - Jun 30 2025."""
        self._run_with_filing_mock("MSFT", 2025)

        cypher = self._submitted_filing_cypher
        params = self._submitted_filing_params

        # MSFT FY2025 = Jul 1 2024 - Jun 30 2025
        self.assertIn("period_end_date >= $fy_start", cypher)
        self.assertIn("period_end_date <= $fy_end", cypher)
        self.assertEqual(params["fy_start"], "2024-07-01")
        self.assertEqual(params["fy_end"], "2025-06-30")

    def test_same_fiscal_year_different_ranges_per_ticker(self):
        """Fiscal year 2026 = different calendar windows for AAPL vs MSFT."""
        self._run_with_filing_mock("AAPL", 2026)
        aapl_params = self._submitted_filing_params
        self.assertEqual(aapl_params["fy_start"], "2025-10-01")
        self.assertEqual(aapl_params["fy_end"], "2026-09-30")

        self._run_with_filing_mock("MSFT", 2026)
        msft_params = self._submitted_filing_params
        self.assertEqual(msft_params["fy_start"], "2025-07-01")
        self.assertEqual(msft_params["fy_end"], "2026-06-30")

        # Same fiscal_year number, different calendar ranges
        self.assertNotEqual(aapl_params["fy_start"], msft_params["fy_start"])
        self.assertNotEqual(aapl_params["fy_end"], msft_params["fy_end"])

    def test_fiscal_year_and_quarter_narrow_range(self):
        """fiscal_year + fiscal_quarter narrows to the quarter's date range."""
        self._run_with_filing_mock("MSFT", 2026, fiscal_quarter="Q1")

        params = self._submitted_filing_params
        # MSFT FY2026 Q1 = Jul 1 2025 - Sep 30 2025
        self.assertEqual(params["fy_start"], "2025-07-01")
        self.assertEqual(params["fy_end"], "2026-06-30")
        self.assertEqual(params["fq_start"], "2025-07-01")
        self.assertEqual(params["fq_end"], "2025-09-30")
        self.assertIn("$fq_start", self._submitted_filing_cypher)
        self.assertIn("$fq_end", self._submitted_filing_cypher)

    def test_no_ticker_no_fiscal_calendar(self):
        """Without a ticker, FiscalCalendar is not consulted (no per-issuer calendar)."""
        self._run_with_filing_mock(None, 2025)

        # Check all filing calls for any FiscalCalendar date range
        for _cypher, params in self._filing_calls:
            self.assertNotIn("fy_start", params)
            self.assertNotIn("fy_end", params)


if __name__ == "__main__":
    unittest.main()


