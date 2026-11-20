"""Tests for sandbox_engine.sec_search — EDGAR full-text search client."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from sandbox_engine.sec_search import (
    DEFAULT_CONFIG,
    FilingHit,
    SecSearchError,
    SecSearchTimeoutError,
    search_edgar_full_text,
)
from sandbox_engine.ssrf import validate_url


def _make_hit_source(
    cik, company_name, form, file_date, accession, primary_document
):
    return {
        "cik": cik,
        "companyName": company_name,
        "form": form,
        "fileDate": file_date,
        "adsh": accession,
        "primaryDocument": primary_document,
    }


def _make_response(payload_dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload_dict).encode("utf-8")
    mock_resp.headers.get.return_value = None
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_resp
    mock_cm.__exit__.return_value = False
    return mock_cm


class ValidateEftsUrlTests(unittest.TestCase):
    """Test SSRF validation for efts.sec.gov URLs."""

    def test_efts_sec_gov_is_allowed(self):
        valid, error = validate_url(
            "https://efts.sec.gov/LLD/searchtext?q=anthropic", DEFAULT_CONFIG
        )
        self.assertTrue(valid, error)
        self.assertIsNone(error)

    def test_efts_sec_gov_in_allowed_hosts(self):
        self.assertIn("efts.sec.gov", DEFAULT_CONFIG.allowed_hosts)


class FilingHitTests(unittest.TestCase):
    """Test FilingHit dataclass."""

    def test_filing_hit_construction(self):
        hit = FilingHit(
            cik="0000320193",
            company_name="Apple Inc.",
            form="10-K",
            file_date="2024-10-31",
            accession="000032019324000123",
            primary_document="aapl-20240928.htm",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
        )
        self.assertEqual(hit.cik, "0000320193")
        self.assertEqual(hit.company_name, "Apple Inc.")
        self.assertEqual(hit.form, "10-K")


class SearchEdgarFullTextTests(unittest.TestCase):
    """Test search_edgar_full_text with mocked HTTP."""

    @patch("sandbox_engine.sec_search.safe_urlopen")
    def test_search_returns_filing_hits(self, mock_urlopen):
        payload = {
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [
                    {
                        "_source": _make_hit_source(
                            "0000320193",
                            "Apple Inc.",
                            "10-K",
                            "2024-10-31",
                            "0000320193-24-000123",
                            "aapl-20240928.htm",
                        )
                    }
                ],
            }
        }
        mock_urlopen.return_value = _make_response(payload)

        hits = search_edgar_full_text("anthropic", timeout=5.0)

        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.cik, "0000320193")
        self.assertEqual(hit.company_name, "Apple Inc.")
        self.assertEqual(hit.form, "10-K")
        self.assertEqual(hit.accession, "000032019324000123")
        self.assertEqual(hit.primary_document, "aapl-20240928.htm")
        self.assertIn("Archives/edgar/data", hit.url)

        # Verify the request URL was validated against SSRF
        called_url = mock_urlopen.call_args[0][0]
        self.assertIn("efts.sec.gov", called_url)
        self.assertIn("q=anthropic", called_url)

    @patch("sandbox_engine.sec_search.safe_urlopen")
    def test_search_returns_empty_on_no_hits(self, mock_urlopen):
        payload = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
        mock_urlopen.return_value = _make_response(payload)

        hits = search_edgar_full_text("nonexistentcompanyxyz", timeout=5.0)

        self.assertEqual(hits, [])

    @patch("sandbox_engine.sec_search.safe_urlopen")
    def test_search_skips_incomplete_hits(self, mock_urlopen):
        payload = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {
                        "_source": _make_hit_source(
                            "0000320193",
                            "Apple Inc.",
                            "10-K",
                            "2024-10-31",
                            "0000320193-24-000123",
                            "aapl-20240928.htm",
                        )
                    },
                    {
                        # Missing primaryDocument — should be skipped
                        "_source": _make_hit_source(
                            "0000789019",
                            "Microsoft Corporation",
                            "10-K",
                            "2024-06-30",
                            "0000789019-24-000456",
                            "",
                        )
                    },
                ],
            }
        }
        mock_urlopen.return_value = _make_response(payload)

        hits = search_edgar_full_text("anthropic", timeout=5.0)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].company_name, "Apple Inc.")

    @patch("sandbox_engine.sec_search.safe_urlopen")
    def test_search_raises_on_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network unreachable")

        with self.assertRaises(SecSearchError):
            search_edgar_full_text("anthropic", timeout=5.0)

    @patch("sandbox_engine.sec_search.time.monotonic")
    def test_search_raises_timeout_when_budget_exceeded(self, mock_monotonic):
        # start_time = 0.0, then elapsed check returns 10.0 (budget exhausted)
        mock_monotonic.side_effect = [0.0, 10.0]

        with self.assertRaises(SecSearchTimeoutError):
            search_edgar_full_text("anthropic", timeout=2.0)

    def test_validate_efts_url_blocks_non_allowed(self):
        from sandbox_engine.ssrf import DEFAULT_CONFIG as DC

        valid, error = validate_url(
            "https://evil.example.com/search", DC
        )
        self.assertFalse(valid)
        self.assertIn("host_not_allowed", error)


if __name__ == "__main__":
    unittest.main()
