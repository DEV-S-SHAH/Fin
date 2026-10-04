"""Tests for the generic multi-company ingestion pipeline."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from ingestion.registry import (
    Company,
    CompanyRegistry,
    FiscalCalendar,
    DEFAULT_COMPANIES,
    get_registry,
    reset_registry,
)
from ingestion.sec_acquisition import (
    SECAcquisition,
    Filing,
    FilingManifest,
    DEFAULT_FORMS,
    ALL_SEC_FORMS,
)
from ingestion.orchestrator import (
    IngestionConfig,
    IngestionOrchestrator,
    PipelineReport,
    StageReport,
    run_ingestion,
)
from ingestion.retrieval import (
    CompanyScope,
    CompanyIsolatedRetriever,
    CrossCompanyQuery,
    get_retriever,
)


class TestCompanyRegistry(TestCase):
    """Tests for the company registry."""
    
    def setUp(self) -> None:
        reset_registry()
    
    def test_default_companies_registered(self) -> None:
        registry = get_registry()
        self.assertIn("AAPL", registry)
        self.assertIn("TSLA", registry)
        self.assertIn("MSFT", registry)
        self.assertEqual(len(registry), 3)
    
    def test_aapl_configuration(self) -> None:
        registry = get_registry()
        aapl = registry.get("AAPL")
        self.assertIsNotNone(aapl)
        self.assertEqual(aapl.ticker, "AAPL")
        self.assertEqual(aapl.name, "Apple Inc.")
        self.assertEqual(aapl.cik, "0000320193")
        self.assertEqual(aapl.fiscal_calendar.year_end_month, 9)
        self.assertEqual(aapl.fiscal_calendar.year_end_day, 30)
    
    def test_tsla_configuration(self) -> None:
        registry = get_registry()
        tsla = registry.get("TSLA")
        self.assertIsNotNone(tsla)
        self.assertEqual(tsla.ticker, "TSLA")
        self.assertEqual(tsla.name, "Tesla, Inc.")
        self.assertEqual(tsla.cik, "0001318605")
        self.assertEqual(tsla.fiscal_calendar.year_end_month, 12)
        self.assertEqual(tsla.fiscal_calendar.year_end_day, 31)
    
    def test_msft_configuration(self) -> None:
        registry = get_registry()
        msft = registry.get("MSFT")
        self.assertIsNotNone(msft)
        self.assertEqual(msft.ticker, "MSFT")
        self.assertEqual(msft.name, "Microsoft Corporation")
        self.assertEqual(msft.cik, "0000789019")
        self.assertEqual(msft.fiscal_calendar.year_end_month, 6)
        self.assertEqual(msft.fiscal_calendar.year_end_day, 30)
    
    def test_fiscal_year_calculation(self) -> None:
        from datetime import date
        
        # MSFT: fiscal year ends June 30
        msft_cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        
        # July 1, 2025 -> FY2026
        self.assertEqual(msft_cal.fiscal_year_for_date(date(2025, 7, 1)), 2026)
        # June 30, 2025 -> FY2025
        self.assertEqual(msft_cal.fiscal_year_for_date(date(2025, 6, 30)), 2025)
        # January 1, 2025 -> FY2025
        self.assertEqual(msft_cal.fiscal_year_for_date(date(2025, 1, 1)), 2025)
        
        # AAPL: fiscal year ends September 30
        aapl_cal = FiscalCalendar(year_end_month=9, year_end_day=30)
        
        # October 1, 2025 -> FY2026
        self.assertEqual(aapl_cal.fiscal_year_for_date(date(2025, 10, 1)), 2026)
        # September 30, 2025 -> FY2025
        self.assertEqual(aapl_cal.fiscal_year_for_date(date(2025, 9, 30)), 2025)
    
    def test_fiscal_quarter_calculation(self) -> None:
        from datetime import date
        
        msft_cal = FiscalCalendar(year_end_month=6, year_end_day=30)
        
        # July-Sep -> Q1
        self.assertEqual(msft_cal.fiscal_quarter_for_date(date(2025, 7, 15)), 1)
        self.assertEqual(msft_cal.fiscal_quarter_for_date(date(2025, 9, 15)), 1)
        # Oct-Dec -> Q2
        self.assertEqual(msft_cal.fiscal_quarter_for_date(date(2025, 10, 15)), 2)
        self.assertEqual(msft_cal.fiscal_quarter_for_date(date(2025, 12, 15)), 2)
        # Jan-Mar -> Q3
        self.assertEqual(msft_cal.fiscal_quarter_for_date(date(2026, 1, 15)), 3)
        # Apr-Jun -> Q4
        self.assertEqual(msft_cal.fiscal_quarter_for_date(date(2026, 4, 15)), 4)
    
    def test_registry_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "companies.json"
            registry = CompanyRegistry(DEFAULT_COMPANIES)
            registry.to_json(path)
            
            # Load it back
            loaded = CompanyRegistry.from_json(path)
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded.get("AAPL").cik, "0000320193")
    
    def test_case_insensitive_lookup(self) -> None:
        registry = get_registry()
        self.assertIsNotNone(registry.get("aapl"))
        self.assertIsNotNone(registry.get("Aapl"))
        self.assertIsNotNone(registry.get("AAPL"))


class TestSECAcquisition(TestCase):
    """Tests for SEC acquisition (mocked)."""
    
    def setUp(self) -> None:
        reset_registry()
        self.registry = get_registry()
        self.acquisition = SECAcquisition(registry=self.registry)
    
    @mock.patch("ingestion.sec_acquisition.urllib.request.urlopen")
    def test_build_manifest_aapl(self, mock_urlopen) -> None:
        # Mock SEC submissions response
        mock_response = mock.Mock()
        response_data = {
            "filings": {
                "recent": {
                    "form": ["10-K", "10-Q", "8-K"],
                    "filingDate": ["2024-10-31", "2024-08-01", "2024-07-30"],
                    "accessionNumber": [
                        "0000320193-24-000123",
                        "0000320193-24-000124",
                        "0000320193-24-000125",
                    ],
                    "primaryDocument": [
                        "aapl-20240927.htm",
                        "aapl-20240629.htm",
                        "aapl-8k-20240730.htm",
                    ],
                    "reportDate": ["2024-09-28", "2024-06-29", "2024-07-30"],
                    "fiscalYear": ["2024", "2024", "2024"],
                    "fiscalPeriod": ["FY", "Q3", ""],
                },
                "files": [],
            }
        }
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.headers = {}
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)
        mock_urlopen.return_value = mock_response
        
        manifest = self.acquisition.build_manifest(
            "AAPL",
            forms=["10-K", "10-Q"],
            start="2024-01-01",
            end="2024-12-31",
        )
        
        self.assertEqual(manifest.company.ticker, "AAPL")
        self.assertEqual(len(manifest.filings), 2)  # Only 10-K and 10-Q
        forms = [f.form for f in manifest.filings]
        self.assertIn("10-K", forms)
        self.assertIn("10-Q", forms)
        self.assertNotIn("8-K", forms)
    
    def test_filing_identity(self) -> None:
        filing = Filing(
            form="10-K",
            filing_date="2024-10-31",
            accession="0000320193-24-000123",
            primary_document="aapl-20240927.htm",
            cik="0000320193",
            ticker="AAPL",
        )
        
        identity = filing.document_identity()
        self.assertEqual(identity, "0000320193|0000320193-24-000123|10-K")
        
        # Same filing should have same identity
        filing2 = Filing(
            form="10-K",
            filing_date="2024-10-31",
            accession="0000320193-24-000123",
            primary_document="aapl-20240927.htm",
            cik="0000320193",
            ticker="AAPL",
        )
        self.assertEqual(filing.document_identity(), filing2.document_identity())
        
        # Amendment should have different identity
        filing_amend = Filing(
            form="10-K/A",
            filing_date="2024-11-15",
            accession="0000320193-24-000123",
            primary_document="aapl-20240927-amend.htm",
            cik="0000320193",
            ticker="AAPL",
            is_amended=True,
            amendment_type="A",
        )
        self.assertNotEqual(filing.document_identity(), filing_amend.document_identity())
    
    def test_filing_local_filename(self) -> None:
        filing = Filing(
            form="10-K",
            filing_date="2024-10-31",
            accession="0000320193-24-000123",
            primary_document="aapl-20240927.htm",
            cik="0000320193",
            ticker="AAPL",
        )
        
        filename = filing.local_filename()
        self.assertIn("10-K", filename)
        self.assertIn("2024-10-31", filename)
        self.assertIn("aapl-20240927.htm", filename)


class TestIngestionOrchestrator(TestCase):
    """Tests for the ingestion orchestrator."""
    
    def setUp(self) -> None:
        reset_registry()
        self.registry = get_registry()
        
        # Use temp directories
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
    
    def tearDown(self) -> None:
        self.temp_dir.cleanup()
    
    def test_config_creation(self) -> None:
        config = IngestionConfig(
            data_root=self.temp_path / "data",
            cache_dir=self.temp_path / "cache",
            checkpoint_dir=self.temp_path / "checkpoints",
        )
        
        self.assertTrue(config.data_root.exists())
        self.assertTrue(config.cache_dir.exists())
        self.assertTrue(config.checkpoint_dir.exists())
    
    def test_orchestrator_creation(self) -> None:
        config = IngestionConfig(
            data_root=self.temp_path / "data",
            cache_dir=self.temp_path / "cache",
            checkpoint_dir=self.temp_path / "checkpoints",
            dry_run=True,
        )
        
        orchestrator = IngestionOrchestrator(config=config, registry=self.registry)
        self.assertIsNotNone(orchestrator)
    
    def test_ingest_company_dry_run(self) -> None:
        config = IngestionConfig(
            data_root=self.temp_path / "data",
            cache_dir=self.temp_path / "cache",
            checkpoint_dir=self.temp_path / "checkpoints",
            dry_run=True,
        )
        
        orchestrator = IngestionOrchestrator(config=config, registry=self.registry)
        
        # Run actual dry-run (which skips all stages)
        report = orchestrator.ingest_company("AAPL")
        
        self.assertEqual(report.company_ticker, "AAPL")
        # Dry run should complete without errors
        self.assertIsNotNone(report.completed_at)
    
    def test_run_ingestion_function(self) -> None:
        config = IngestionConfig(
            data_root=self.temp_path / "data",
            cache_dir=self.temp_path / "cache",
            checkpoint_dir=self.temp_path / "checkpoints",
            dry_run=True,
        )
        
        with mock.patch("ingestion.orchestrator.IngestionOrchestrator.ingest_company") as mock_ingest:
            mock_ingest.return_value = PipelineReport(
                company_ticker="AAPL",
                started_at="2024-01-01T00:00:00Z",
                filings_processed=5,
            )
            
            reports = run_ingestion(
                tickers=["AAPL"],
                config=config,
                registry=self.registry,
            )
            
            self.assertIn("AAPL", reports)
            self.assertEqual(reports["AAPL"].company_ticker, "AAPL")


class TestCompanyIsolation(TestCase):
    """Tests for company-isolated retrieval."""
    
    def setUp(self) -> None:
        reset_registry()
        self.registry = get_registry()
    
    def test_single_company_scope(self) -> None:
        scope = CompanyScope(ticker="AAPL")
        self.assertTrue(scope.is_single_company)
        self.assertFalse(scope.is_cross_company)
        self.assertFalse(scope.is_global)
        self.assertEqual(scope.get_tickers(self.registry), ["AAPL"])
        self.assertEqual(scope.cache_key_suffix(), "aapl")
    
    def test_cross_company_scope(self) -> None:
        scope = CompanyScope(tickers=("AAPL", "MSFT"))
        self.assertFalse(scope.is_single_company)
        self.assertTrue(scope.is_cross_company)
        self.assertFalse(scope.is_global)
        self.assertEqual(scope.get_tickers(self.registry), ["AAPL", "MSFT"])
        self.assertEqual(scope.cache_key_suffix(), "aapl_msft")
    
    def test_global_scope(self) -> None:
        scope = CompanyScope(all_companies=True)
        self.assertFalse(scope.is_single_company)
        self.assertFalse(scope.is_cross_company)
        self.assertTrue(scope.is_global)
        self.assertEqual(scope.get_tickers(self.registry), ["AAPL", "TSLA", "MSFT"])
        self.assertEqual(scope.cache_key_suffix(), "global")
    
    def test_scope_validation(self) -> None:
        # Cannot specify both ticker and tickers
        with self.assertRaises(ValueError):
            CompanyScope(ticker="AAPL", tickers=("MSFT",))
        
        # Cannot combine all_companies with others
        with self.assertRaises(ValueError):
            CompanyScope(all_companies=True, ticker="AAPL")
    
    def test_cross_company_query(self) -> None:
        query = CrossCompanyQuery(
            question="Compare revenue between Apple and Microsoft",
            tickers=["AAPL", "MSFT"],
            comparison_type="compare",
        )
        
        scope = query.to_scope()
        self.assertEqual(scope.get_tickers(self.registry), ["AAPL", "MSFT"])
        
        errors = query.validate(self.registry)
        self.assertEqual(errors, [])
    
    def test_cross_company_query_validation(self) -> None:
        query = CrossCompanyQuery(
            question="Compare revenue",
            tickers=["AAPL", "INVALID"],
        )
        
        errors = query.validate(self.registry)
        self.assertIn("Unknown company: INVALID", errors)
    
    def test_retriever_cache_key(self) -> None:
        retriever = CompanyIsolatedRetriever(registry=self.registry)
        
        scope1 = CompanyScope(ticker="AAPL")
        scope2 = CompanyScope(ticker="MSFT")
        
        key1 = retriever.cache_key("query:revenue", scope1)
        key2 = retriever.cache_key("query:revenue", scope2)
        
        # Same base query, different companies -> different cache keys
        self.assertNotEqual(key1, key2)
        
        # Same scope -> same cache key
        key1_again = retriever.cache_key("query:revenue", scope1)
        self.assertEqual(key1, key1_again)


class TestPipelineReport(TestCase):
    """Tests for pipeline reporting."""
    
    def test_stage_report(self) -> None:
        stage = StageReport(
            stage="test",
            started_at="2024-01-01T00:00:00Z",
        )
        
        stage.items_processed = 10
        stage.items_failed = 1
        stage.add_error("Test error")
        stage.add_warning("Test warning")
        stage.complete(extra="metadata")
        
        self.assertIsNotNone(stage.completed_at)
        self.assertGreater(stage.duration_seconds, 0)
        self.assertEqual(stage.errors, ["Test error"])
        self.assertEqual(stage.warnings, ["Test warning"])
        self.assertEqual(stage.metadata["extra"], "metadata")
    
    def test_pipeline_report(self) -> None:
        report = PipelineReport(
            company_ticker="AAPL",
            started_at="2024-01-01T00:00:00Z",
        )
        
        report.filings_discovered = 10
        report.filings_processed = 8
        report.filings_skipped = 2
        
        stage = StageReport(
            stage="discovery",
            started_at="2024-01-01T00:00:00Z",
        )
        stage.complete()
        report.add_stage(stage)
        
        report.complete()
        
        self.assertIsNotNone(report.completed_at)
        self.assertGreater(report.total_duration_seconds, 0)
        
        # Test serialization
        data = report.to_dict()
        self.assertEqual(data["company_ticker"], "AAPL")
        self.assertEqual(data["summary"]["filings_discovered"], 10)
        self.assertEqual(len(data["stages"]), 1)


class TestDocumentTaxonomy(TestCase):
    """Tests that all SEC form types are supported."""
    
    def test_core_forms_in_default(self) -> None:
        for form in ["10-K", "10-Q", "8-K", "DEF 14A"]:
            self.assertIn(form, DEFAULT_FORMS)
    
    def test_important_forms_in_all(self) -> None:
        for form in ["3", "4", "5"]:
            self.assertIn(form, ALL_SEC_FORMS)
    
    def test_useful_forms_in_all(self) -> None:
        for form in ["13F-HR", "SC 13D", "SC 13G", "S-3", "S-8"]:
            self.assertIn(form, ALL_SEC_FORMS)
    
    def test_424b_variants_in_all(self) -> None:
        for form in ["424B", "424B1", "424B2", "424B3", "424B4", "424B5"]:
            self.assertIn(form, ALL_SEC_FORMS)
    
    def test_optional_forms_in_all(self) -> None:
        for form in ["ARS", "SD", "11-K"]:
            self.assertIn(form, ALL_SEC_FORMS)


class TestIdempotency(TestCase):
    """Tests for idempotency and checkpoint/resume."""
    
    def setUp(self) -> None:
        reset_registry()
        self.registry = get_registry()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
    
    def tearDown(self) -> None:
        self.temp_dir.cleanup()
    
    def test_checkpoint_save_load(self) -> None:
        config = IngestionConfig(
            data_root=self.temp_path / "data",
            cache_dir=self.temp_path / "cache",
            checkpoint_dir=self.temp_path / "checkpoints",
        )
        
        orchestrator = IngestionOrchestrator(config=config, registry=self.registry)
        
        # Save checkpoint
        test_data = {"filing1", "filing2", "filing3"}
        orchestrator._save_checkpoint("AAPL", "parsing", test_data)
        
        # Load checkpoint
        loaded = orchestrator._load_checkpoint("AAPL", "parsing")
        self.assertEqual(loaded, test_data)
    
    def test_manifest_save_load(self) -> None:
        config = IngestionConfig(
            data_root=self.temp_path / "data",
            cache_dir=self.temp_path / "cache",
            checkpoint_dir=self.temp_path / "checkpoints",
        )
        
        orchestrator = IngestionOrchestrator(config=config, registry=self.registry)
        
        # Create a mock manifest
        manifest = {
            "company": "AAPL",
            "date_range": ("2024-01-01", "2024-12-31"),
            "forms": ["10-K", "10-Q"],
            "filings": [
                {
                    "form": "10-K",
                    "filing_date": "2024-10-31",
                    "accession": "0000320193-24-000123",
                    "primary_document": "aapl-20240927.htm",
                    "cik": "0000320193",
                    "report_date": "2024-09-28",
                    "document_title": "",
                    "document_url": "",
                    "fiscal_period": "FY",
                    "fiscal_year": "2024",
                    "is_amended": False,
                    "amendment_type": "",
                    "exhibit_type": "",
                    "ticker": "AAPL",
                }
            ],
        }
        
        # Save
        manifest_path = orchestrator._manifest_path("AAPL")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest))
        
        # Load
        loaded = orchestrator._load_manifest("AAPL")
        self.assertEqual(loaded["company"], "AAPL")
        self.assertEqual(len(loaded["filings"]), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()