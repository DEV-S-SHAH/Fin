"""Unit tests for Slice 2: Runtime SEC EDGAR Fetching & High-Signal Document Slicing."""

import email.message
import io
import json
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from sandbox_engine.tier1_clean import (
    SectionNotFoundError,
    clean_and_truncate_section,
)
from sandbox_engine.tier1_fetch import (
    EDGARRateLimitError,
    FetchError,
    FetchTimeoutError,
    FilingNotFoundError,
    RateLimitError,
    SECRuntimeFetcher,
)


class TestTier1Fetch(unittest.TestCase):
    """Test suite for SECRuntimeFetcher and clean_and_truncate_section."""

    def setUp(self):
        self.fetcher = SECRuntimeFetcher(user_agent="TestApp test@example.com")
        self.sample_submissions = {
            "filings": {
                "recent": {
                    "form": ["10-K", "10-Q", "8-K"],
                    "accessionNumber": [
                        "0000320193-24-000106",
                        "0000320193-24-000050",
                        "0000320193-24-000010",
                    ],
                    "primaryDocument": [
                        "aapl-20240928.htm",
                        "aapl-20240629.htm",
                        "aapl-20240115.htm",
                    ],
                    "filingDate": ["2024-11-01", "2024-07-25", "2024-01-16"],
                    "reportDate": ["2024-09-28", "2024-06-29", "2024-01-15"],
                }
            }
        }
        self.sample_10k_html = (
            "<!DOCTYPE html><html><body>"
            "<div id='toc'>Table of Contents</div>"
            "<div>Item 1. Business</div>"
            "<p>The Company designs, manufactures and markets smartphones, personal computers, "
            "tablets, wearables and accessories, and sells a variety of related services.</p>"
            "<table border='1'><tr><td>Garbage table cell</td><td>Data</td></tr></table>"
            "<script>var tracker = 123;</script>"
            "<ix:nonNumeric name='us-gaap:EntityInformation'>Apple Inc.</ix:nonNumeric>"
            "<p>Our products include iPhone, Mac, iPad, and Wearables.</p>"
            "<div>Item 1A. Risk Factors</div>"
            "<p>Global economic conditions could materially adversely affect our business.</p>"
            "<div>Item 2. Properties</div>"
            "<p>We own various data centers.</p>"
            "</body></html>"
        )

    def test_exceptions_hierarchy(self):
        """Ensure exceptions conform to Zero Silent Drops typed taxonomy."""
        self.assertTrue(issubclass(FetchTimeoutError, FetchError))
        self.assertTrue(issubclass(EDGARRateLimitError, FetchError))
        self.assertTrue(issubclass(FilingNotFoundError, FetchError))
        self.assertTrue(issubclass(RateLimitError, FetchError))

    @patch("urllib.request.urlopen")
    def test_fetch_successful_10k(self, mock_urlopen):
        """Verify happy path returns raw HTML and expected metadata."""
        # Response 1: Submissions JSON
        sub_resp = MagicMock()
        sub_resp.read.return_value = json.dumps(self.sample_submissions).encode("utf-8")
        sub_resp.headers = {"Content-Encoding": "identity"}
        sub_resp.__enter__.return_value = sub_resp

        # Response 2: 10-K HTML Document
        doc_resp = MagicMock()
        doc_resp.read.return_value = self.sample_10k_html.encode("utf-8")
        doc_resp.headers = {"Content-Encoding": "identity"}
        doc_resp.__enter__.return_value = doc_resp

        mock_urlopen.side_effect = [sub_resp, doc_resp]

        raw_html, metadata = self.fetcher.fetch_latest_filing_html("AAPL", form_type="10-K", timeout=2.0)

        self.assertIn("Item 1. Business", raw_html)
        self.assertEqual(metadata["ticker"], "AAPL")
        self.assertEqual(metadata["form"], "10-K")
        self.assertEqual(metadata["accession_number"], "0000320193-24-000106")
        self.assertEqual(metadata["primary_document"], "aapl-20240928.htm")
        self.assertEqual(metadata["filing_date"], "2024-11-01")
        self.assertIn("https://www.sec.gov/Archives/edgar/data/", metadata["url"])
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_rate_limit_retry_after(self, mock_urlopen):
        """Assert HTTP 429 parses Retry-After header and raises EDGARRateLimitError if budget expires."""
        hdrs = email.message.EmailMessage()
        hdrs["Retry-After"] = "5"
        err_429 = urllib.error.HTTPError(
            url="https://data.sec.gov/submissions/CIK0000320193.json",
            code=429,
            msg="Too Many Requests",
            hdrs=hdrs,
            fp=io.BytesIO(b"Rate limited"),
        )
        mock_urlopen.side_effect = err_429

        start = time.monotonic()
        with self.assertRaises((EDGARRateLimitError, RateLimitError)):
            self.fetcher.fetch_latest_filing_html("AAPL", form_type="10-K", timeout=2.0)
        elapsed = time.monotonic() - start

        # Should fail immediately without waiting 5 seconds because 5s > 2.0s budget
        self.assertLess(elapsed, 0.5)

    @patch("urllib.request.urlopen")
    def test_timeout_enforcement(self, mock_urlopen):
        """Mock a hanging socket and assert FetchTimeoutError is raised in <= 2.1 seconds."""
        def hanging_open(*args, **kwargs):
            time.sleep(0.3)
            raise TimeoutError("Socket read timed out")

        mock_urlopen.side_effect = hanging_open

        start = time.monotonic()
        with self.assertRaises(FetchTimeoutError):
            self.fetcher.fetch_latest_filing_html("AAPL", form_type="10-K", timeout=0.25)
        elapsed = time.monotonic() - start

        self.assertLessEqual(elapsed, 2.1)

    @patch("urllib.request.urlopen")
    def test_filing_not_found_on_absent_form(self, mock_urlopen):
        """Verify FilingNotFoundError is raised when target form type is absent in submissions."""
        sub_resp = MagicMock()
        sub_resp.read.return_value = json.dumps(self.sample_submissions).encode("utf-8")
        sub_resp.headers = {"Content-Encoding": "identity"}
        sub_resp.__enter__.return_value = sub_resp
        mock_urlopen.return_value = sub_resp

        with self.assertRaises(FilingNotFoundError):
            self.fetcher.fetch_latest_filing_html("AAPL", form_type="20-F", timeout=2.0)

    def test_section_clean_and_token_cap(self):
        """Pass sample 10-K HTML with Item 1 and Item 1A; verify Item 1 is cleanly extracted and capped."""
        # Create a document where Item 1 has narrative text repeated to exceed 30,000 characters
        sentence = (
            "The Company designs and markets consumer electronics and software services globally. "
            "Our primary revenue drivers include hardware devices and subscription ecosystems. "
        )
        long_body = sentence * 300  # ~50,000 chars

        html = (
            "<html><body>"
            "<div>Item 1. Business</div>"
            "<script>var tracker = 'skip me';</script>"
            "<style>.hide { display: none; }</style>"
            "<table><tr><td>Skip this table data</td></tr></table>"
            f"<p>{long_body}</p>"
            "<ix:nonNumeric name='dei:Entity'>Some Tag Text</ix:nonNumeric>"
            "<div>Item 1A. Risk Factors</div>"
            "<p>Risk factors must NOT be in the Item 1 extract.</p>"
            "</body></html>"
        )

        cleaned = clean_and_truncate_section(html, form_type="10-K", max_tokens=6000)

        # 1. Output must not be empty
        self.assertTrue(len(cleaned) > 0)
        # 2. Hard character stop at max_tokens * 4 = 24,000 chars
        self.assertLessEqual(len(cleaned), 24000)
        # 3. Item 1A should NOT be included
        self.assertNotIn("Risk factors must NOT", cleaned)
        # 4. Markup/tables/scripts must be stripped
        self.assertNotIn("<table", cleaned)
        self.assertNotIn("Skip this table data", cleaned)
        self.assertNotIn("<script", cleaned)
        self.assertNotIn("tracker = 'skip me'", cleaned)
        self.assertNotIn("<ix:nonNumeric", cleaned)
        # 5. Preserves complete sentences at the truncation boundary
        self.assertTrue(cleaned.endswith(".") or cleaned.endswith("!") or cleaned.endswith("?"))

    def test_section_not_found_raises_exception(self):
        """Verify SectionNotFoundError is raised if Item 1 / Item 2 cannot be found."""
        html_without_items = "<html><body><h1>Annual Report</h1><p>General intro without standard items.</p></body></html>"
        with self.assertRaises(SectionNotFoundError):
            clean_and_truncate_section(html_without_items, form_type="10-K")


if __name__ == "__main__":
    unittest.main()
