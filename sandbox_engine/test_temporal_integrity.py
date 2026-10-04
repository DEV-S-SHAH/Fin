"""Tests for temporal integrity in FinGraph."""

from __future__ import annotations

import unittest
from pathlib import Path

from sandbox_engine.parser import FilingParser, stable_id


class TestTemporalHierarchy(unittest.TestCase):
    """Test that temporal hierarchy nodes and relationships are created correctly."""

    def setUp(self):
        self.parser = FilingParser()
        self.path_10k = Path("sandbox_engine/data/apple/2026/10k/10-K_2025-10-31_aapl-20250927.htm")
        self.path_10q = Path("sandbox_engine/data/apple/2026/10q/10-Q_2026-01-30_aapl-20251227.htm")

    def test_10k_creates_fiscal_year(self):
        """10-K should create a FiscalYear node."""
        result = self.parser.ingest_file(self.path_10k)
        self.assertEqual(len(result.fiscal_years), 1)
        fy = list(result.fiscal_years.values())[0]
        self.assertEqual(fy["company_ticker"], "AAPL")
        self.assertEqual(fy["fiscal_year"], 2025)
        self.assertTrue(fy["year_start_date"])
        self.assertTrue(fy["year_end_date"])

    def test_10k_creates_fiscal_quarter(self):
        """10-K should create a FiscalQuarter node with FY label."""
        result = self.parser.ingest_file(self.path_10k)
        self.assertEqual(len(result.fiscal_quarters), 1)
        fq = list(result.fiscal_quarters.values())[0]
        self.assertEqual(fq["quarter_label"], "FY")
        self.assertEqual(fq["quarter_number"], 0)

    def test_10q_creates_fiscal_year(self):
        """10-Q should create a FiscalYear node."""
        result = self.parser.ingest_file(self.path_10q)
        self.assertEqual(len(result.fiscal_years), 1)
        fy = list(result.fiscal_years.values())[0]
        self.assertEqual(fy["company_ticker"], "AAPL")
        self.assertEqual(fy["fiscal_year"], 2026)

    def test_10q_creates_fiscal_quarter(self):
        """10-Q should create a FiscalQuarter node with Q1 label."""
        result = self.parser.ingest_file(self.path_10q)
        self.assertEqual(len(result.fiscal_quarters), 1)
        fq = list(result.fiscal_quarters.values())[0]
        self.assertEqual(fq["quarter_label"], "Q1")
        self.assertEqual(fq["quarter_number"], 1)

    def test_temporal_edges_created(self):
        """Temporal hierarchy edges should be created."""
        result = self.parser.ingest_file(self.path_10k)
        self.assertIn("HAS_FISCAL_YEAR", result.edges)
        self.assertIn("HAS_FISCAL_QUARTER", result.edges)
        self.assertIn("FILED_IN_QUARTER", result.edges)
        self.assertEqual(len(result.edges["HAS_FISCAL_YEAR"]), 1)
        self.assertEqual(len(result.edges["HAS_FISCAL_QUARTER"]), 1)
        self.assertEqual(len(result.edges["FILED_IN_QUARTER"]), 1)


class TestDuplicatePrevention(unittest.TestCase):
    """Test that duplicate filings are prevented."""

    def test_filing_identity_includes_accession_and_content_hash(self):
        """filing_identity should include accession_number and content_hash."""
        from sandbox_engine.parser import filing_identity
        
        metadata1 = {
            "ticker": "AAPL",
            "form_type": "10-K",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "filing_date": "2025-10-31",
            "accession_number": "0000320193-25-000123",
            "content_hash": "abc123",
        }
        metadata2 = {
            "ticker": "AAPL",
            "form_type": "10-K",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "filing_date": "2025-10-31",
            "accession_number": "0000320193-25-000123",
            "content_hash": "abc123",
        }
        metadata3 = {
            "ticker": "AAPL",
            "form_type": "10-K",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "filing_date": "2025-10-31",
            "accession_number": "0000320193-25-000456",  # Different accession
            "content_hash": "abc123",
        }
        
        id1 = filing_identity(metadata1)
        id2 = filing_identity(metadata2)
        id3 = filing_identity(metadata3)
        
        # Same accession and content_hash should produce same ID
        self.assertEqual(id1, id2)
        # Different accession should produce different ID
        self.assertNotEqual(id1, id3)


class TestTemporalQueryFilters(unittest.TestCase):
    """Test temporal filter extraction from queries."""

    def test_extract_year(self):
        from sandbox_engine.router import extract_temporal_filters
        
        filters = extract_temporal_filters("What was Apple revenue in 2024?")
        self.assertEqual(filters.get("fiscal_year"), 2024)
    
    def test_extract_quarter(self):
        from sandbox_engine.router import extract_temporal_filters
        
        filters = extract_temporal_filters("Apple Q3 2023 earnings")
        self.assertEqual(filters.get("fiscal_year"), 2023)
        self.assertEqual(filters.get("fiscal_quarter"), "Q3")
    
    def test_extract_form_type(self):
        from sandbox_engine.router import extract_temporal_filters
        
        filters = extract_temporal_filters("10-K FY2022 revenue")
        self.assertEqual(filters.get("form_type"), "10-K")
        
        filters = extract_temporal_filters("8-K events in 2024")
        self.assertEqual(filters.get("form_type"), "8-K")

    def test_routing_includes_temporal_filters(self):
        from sandbox_engine.router import route_query
        import ladybug as lb
        from sandbox_engine.ddl import ensure_schema
        from sandbox_engine.buffer import StageBuffer
        from sandbox_engine.loader import BulkLoader
        from sandbox_engine.parser import FilingParser
        import tempfile
        from pathlib import Path
        
        parser = FilingParser()
        path = Path("sandbox_engine/data/apple/2026/10k/10-K_2025-10-31_aapl-20250927.htm")
        result = parser.ingest_file(path)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir) / 'staging'
            db_path = Path(tmpdir) / 'test.lbug'
            
            with BulkLoader(str(db_path)) as loader:
                buffer = StageBuffer(staging)
                buffer.add_result(result)
                buffer.spill()
                loader.load(buffer)
            
            db = lb.Database(str(db_path), read_only=True)
            conn = lb.Connection(db)
            
            routing = route_query("What was Apple revenue in 2025?", conn)
            self.assertEqual(routing.fiscal_year, 2025)
            
            routing = route_query("Apple Q1 2026 revenue", conn)
            self.assertEqual(routing.fiscal_year, 2026)
            self.assertEqual(routing.fiscal_quarter, "Q1")
            
            db.close()


class TestFilingMetadata(unittest.TestCase):
    """Test that filing metadata is properly extracted."""

    def test_10k_metadata(self):
        result = FilingParser().ingest_file("sandbox_engine/data/apple/2026/10k/10-K_2025-10-31_aapl-20250927.htm")
        self.assertEqual(result.metadata.get("form_type"), "10-K")
        self.assertEqual(result.metadata.get("fiscal_year"), 2025)
        self.assertEqual(result.metadata.get("fiscal_period"), "FY")
        # content_hash is computed from document content
        self.assertTrue(result.metadata.get("content_hash"))
        # accession_number may not be in local HTML files (comes from EDGAR index)

    def test_10q_metadata(self):
        result = FilingParser().ingest_file("sandbox_engine/data/apple/2026/10q/10-Q_2026-01-30_aapl-20251227.htm")
        self.assertEqual(result.metadata.get("form_type"), "10-Q")
        self.assertEqual(result.metadata.get("fiscal_year"), 2026)
        self.assertEqual(result.metadata.get("fiscal_period"), "Q1")
        self.assertTrue(result.metadata.get("content_hash"))


if __name__ == "__main__":
    unittest.main()
