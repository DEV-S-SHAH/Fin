"""Tests for sandbox_engine.anthropic_kg — knowledge graph builder."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from sandbox_engine.anthropic_kg import (
    DEFAULT_QUERY,
    BuildReport,
    _entity_id,
    _fetch_filing,
    _parse_and_extract,
    _slugify,
    build_anthropic_knowledge_graph,
)
from sandbox_engine.sec_search import FilingHit


class SlugifyTests(unittest.TestCase):
    """Test _slugify helper."""

    def test_slugify_lowercases_and_dashes(self):
        self.assertEqual(_slugify("Company Name"), "company-name")

    def test_slugify_strips_non_alphanumeric(self):
        self.assertEqual(_slugify("Foo Bar Baz!"), "foo-bar-baz")

    def test_slugify_empty_returns_entity(self):
        self.assertEqual(_slugify(""), "entity")


class EntityIdTests(unittest.TestCase):
    """Test _entity_id deterministic ID generation."""

    def test_entity_id_is_deterministic(self):
        id1 = _entity_id("Company", "Apple Inc.", "http://example.com")
        id2 = _entity_id("Company", "Apple Inc.", "http://example.com")
        self.assertEqual(id1, id2)

    def test_entity_id_differs_by_source_url(self):
        id1 = _entity_id("Company", "Apple Inc.", "http://a.com")
        id2 = _entity_id("Company", "Apple Inc.", "http://b.com")
        self.assertNotEqual(id1, id2)

    def test_entity_id_differs_by_type(self):
        id1 = _entity_id("Company", "Apple Inc.", "")
        id2 = _entity_id("Filing", "Apple Inc.", "")
        self.assertNotEqual(id1, id2)


class FetchFilingTests(unittest.TestCase):
    """Test _fetch_filing with mocked HTTP."""

    def test_fetch_returns_html_and_metadata(self):
        hit = FilingHit(
            cik="0000320193",
            company_name="Apple Inc.",
            form="10-K",
            file_date="2024-10-31",
            accession="000032019324000123",
            primary_document="aapl-20240928.htm",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html><body>Apple 10-K</body></html>"
        mock_resp.headers.get.return_value = None
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_resp
        mock_cm.__exit__.return_value = False

        import time

        with patch("sandbox_engine.anthropic_kg.safe_urlopen", return_value=mock_cm):
            html, metadata = _fetch_filing(hit, timeout=5.0, start_time=time.monotonic() - 0.5)

        self.assertIsNotNone(html)
        self.assertIn("Apple 10-K", html)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["company_name"], "Apple Inc.")
        self.assertEqual(metadata["form"], "10-K")

    def test_fetch_returns_none_on_error(self):
        hit = FilingHit(
            cik="0000320193",
            company_name="Apple Inc.",
            form="10-K",
            file_date="2024-10-31",
            accession="000032019324000123",
            primary_document="aapl-20240928.htm",
            url="https://evil.example.com/bad.htm",
        )

        import time

        html, metadata = _fetch_filing(hit, timeout=5.0, start_time=time.monotonic())

        self.assertIsNone(html)
        self.assertIsNone(metadata)


class ParseAndExtractTests(unittest.TestCase):
    """Test _parse_and_extract with a mock parser."""

    def test_extract_creates_company_filing_and_anthropic_nodes(self):
        # Mock parser result
        mock_result = MagicMock()
        mock_result.metrics = []
        mock_result.segments = []
        mock_result.events = []
        mock_result.chunks = []

        mock_parser = MagicMock()
        mock_parser.ingest_file.return_value = mock_result

        html = "<html><body>test</body></html>"
        metadata = {
            "cik": "0000320193",
            "company_name": "Apple Inc.",
            "form": "10-K",
            "file_date": "2024-10-31",
            "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
        }

        entities, edges = _parse_and_extract(html, metadata, mock_parser)

        # Should have Company (filer), Filing, Company (Anthropic)
        company_entities = [e for e in entities if e.entity_type == "Company"]
        filing_entities = [e for e in entities if e.entity_type == "Filing"]
        self.assertEqual(len(company_entities), 2)  # Apple + Anthropic
        self.assertEqual(len(filing_entities), 1)

        # Should have SUBMITTED and MENTIONS edges
        submitted = [e for e in edges if e.rel_type == "SUBMITTED"]
        mentions = [e for e in edges if e.rel_type == "MENTIONS"]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(len(mentions), 1)

    def test_extract_metric_entities(self):
        mock_metric = MagicMock()
        mock_metric.canonical_name = "Revenue"
        mock_metric.reported_label = None

        mock_result = MagicMock()
        mock_result.metrics = [mock_metric]
        mock_result.segments = []
        mock_result.events = []
        mock_result.chunks = []

        mock_parser = MagicMock()
        mock_parser.ingest_file.return_value = mock_result

        metadata = {
            "cik": "0000320193",
            "company_name": "Apple Inc.",
            "form": "10-K",
            "file_date": "2024-10-31",
            "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
        }

        entities, edges = _parse_and_extract("<html></html>", metadata, mock_parser)

        metric_entities = [e for e in entities if e.entity_type == "Metric"]
        self.assertEqual(len(metric_entities), 1)
        self.assertEqual(metric_entities[0].name, "Revenue")

        report_edges = [e for e in edges if e.rel_type == "REPORTS_METRIC"]
        self.assertEqual(len(report_edges), 1)

    def test_extract_segment_entities(self):
        mock_segment = MagicMock()
        mock_segment.name = "iPhone"

        mock_result = MagicMock()
        mock_result.metrics = []
        mock_result.segments = [mock_segment]
        mock_result.events = []
        mock_result.chunks = []

        mock_parser = MagicMock()
        mock_parser.ingest_file.return_value = mock_result

        metadata = {
            "cik": "0000320193",
            "company_name": "Apple Inc.",
            "form": "10-K",
            "file_date": "2024-10-31",
            "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
        }

        entities, edges = _parse_and_extract("<html></html>", metadata, mock_parser)

        seg_entities = [e for e in entities if e.entity_type == "Segment"]
        self.assertEqual(len(seg_entities), 1)
        self.assertEqual(seg_entities[0].name, "iPhone")

        seg_edges = [e for e in edges if e.rel_type == "HAS_SEGMENT"]
        self.assertEqual(len(seg_edges), 1)


class BuildReportTests(unittest.TestCase):
    """Test BuildReport dataclass."""

    def test_build_report_defaults(self):
        report = BuildReport(
            success=True,
            filings_found=3,
            filings_processed=2,
            entities_created=10,
            edges_created=15,
            seconds=2.5,
        )
        self.assertTrue(report.success)
        self.assertEqual(report.error, None)
        self.assertEqual(report.attempt, 0)


class BuildAnthropicKnowledgeGraphTests(unittest.TestCase):
    """Test build_anthropic_knowledge_graph with fully mocked pipeline."""

    def _make_filing_hit(self, company="Apple Inc.", form="10-K", accession="000032019324000123"):
        return FilingHit(
            cik="0000320193",
            company_name=company,
            form=form,
            file_date="2024-10-31",
            accession=accession,
            primary_document="aapl-20240928.htm",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
        )

    @patch("sandbox_engine.anthropic_kg.KGStore")
    @patch("sandbox_engine.anthropic_kg._fetch_filing")
    @patch("sandbox_engine.anthropic_kg.search_edgar_full_text")
    def test_build_success_single_attempt(
        self, mock_search, mock_fetch, mock_kgstore_class
    ):
        mock_search.return_value = [self._make_filing_hit()]

        mock_fetch.return_value = (
            "<html><body>Apple 10-K content</body></html>",
            {
                "cik": "0000320193",
                "company_name": "Apple Inc.",
                "form": "10-K",
                "file_date": "2024-10-31",
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
            },
        )

        mock_store = MagicMock()
        mock_kgstore_class.return_value.__enter__.return_value = mock_store
        mock_kgstore_class.return_value.__exit__.return_value = False

        report = build_anthropic_knowledge_graph(timeout=5.0, max_retries=1)

        self.assertTrue(report.success)
        self.assertEqual(report.filings_found, 1)
        self.assertEqual(report.filings_processed, 1)
        self.assertGreater(report.entities_created, 0)
        self.assertGreater(report.edges_created, 0)
        self.assertIsNone(report.error)
        self.assertEqual(report.attempt, 1)
        self.assertLess(report.seconds, 5.0)

        mock_search.assert_called_once()
        mock_fetch.assert_called_once()
        mock_store.ensure_schema.assert_called_once()
        self.assertGreater(mock_store.upsert_entity.call_count, 0)
        self.assertGreater(mock_store.upsert_edge.call_count, 0)

    @patch("sandbox_engine.anthropic_kg.KGStore")
    @patch("sandbox_engine.anthropic_kg._fetch_filing")
    @patch("sandbox_engine.anthropic_kg.search_edgar_full_text")
    def test_build_no_hits_returns_success(
        self, mock_search, mock_fetch, mock_kgstore_class
    ):
        mock_search.return_value = []

        report = build_anthropic_knowledge_graph(timeout=5.0, max_retries=1)

        self.assertTrue(report.success)
        self.assertEqual(report.filings_found, 0)
        self.assertEqual(report.filings_processed, 0)
        self.assertEqual(report.entities_created, 0)
        self.assertEqual(report.edges_created, 0)
        mock_fetch.assert_not_called()

    @patch("sandbox_engine.anthropic_kg.search_edgar_full_text")
    def test_build_retries_on_search_error(self, mock_search):
        mock_search.side_effect = Exception("network error")

        report = build_anthropic_knowledge_graph(timeout=5.0, max_retries=3)

        self.assertFalse(report.success)
        self.assertIsNotNone(report.error)
        self.assertEqual(report.attempt, 3)
        self.assertEqual(mock_search.call_count, 3)

    @patch("sandbox_engine.anthropic_kg.search_edgar_full_text")
    def test_build_default_query(self, mock_search):
        mock_search.return_value = []

        build_anthropic_knowledge_graph(timeout=5.0, max_retries=1)

        mock_search.assert_called_once()
        args, _kwargs = mock_search.call_args
        self.assertEqual(args[0], DEFAULT_QUERY)


if __name__ == "__main__":
    unittest.main()
