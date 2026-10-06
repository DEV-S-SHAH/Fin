#!/usr/bin/env python3
"""MSFT Inventory & Acquisition Script.

Inventories existing MSFT filings, identifies missing fiscal periods,
discovers SEC EDGAR and Microsoft IR sources, acquires missing documents,
and parses them into MSFT staging using deterministic parser.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Add project root to path for imports
sys.path.insert(0, "/Users/dev/Downloads/Fin")

from ingestion.registry import CompanyRegistry, get_registry, FiscalCalendar
from ingestion.sec_acquisition import SECAcquisition, FilingManifest, Filing, DEFAULT_FORMS, ALL_SEC_FORMS
from sandbox_engine.parser import FilingParser
from sandbox_engine.config import FINGRAPH_DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("msft_inventory")

# MSFT Fiscal Calendar (June 30 year end)
MSFT_FISCAL_CALENDAR = FiscalCalendar(
    year_end_month=6,
    year_end_day=30,
    fiscal_year_is_calendar_year_of_end=True,
)

# Staging paths
STAGING_RAW = Path("/Users/dev/Downloads/Fin/data/staging/MSFT/raw")
STAGING_PARSED = Path("/Users/dev/Downloads/Fin/data/staging/MSFT/parsed")

# Existing data directory
EXISTING_DATA_DIR = Path("/Users/dev/Downloads/Fin/data/microsoft-sec")

STAGING_RAW.mkdir(parents=True, exist_ok=True)
STAGING_PARSED.mkdir(parents=True, exist_ok=True)


@dataclass
class FilingInventoryItem:
    """One filing found in inventory."""
    form: str
    filing_date: str
    accession: str
    primary_document: str
    fiscal_year: str
    fiscal_period: str
    source_path: str
    file_size: int
    content_hash: str
    document_identity: str


@dataclass
class MissingPeriod:
    """A missing fiscal period."""
    fiscal_year: int
    fiscal_quarter: str | None  # Q1, Q2, Q3, Q4, FY
    form_type: str
    expected_filing_window: str


@dataclass
class AcquisitionResult:
    """Result of acquiring a filing."""
    filing: Filing
    success: bool
    local_path: str | None = None
    error: str | None = None
    content_hash: str | None = None


@dataclass
class ParseResult:
    """Result of parsing a filing."""
    filing_identity: str
    success: bool
    metrics_count: int = 0
    segments_count: int = 0
    chunks_count: int = 0
    events_count: int = 0
    executives_count: int = 0
    error: str | None = None
    parsed_path: str | None = None


@dataclass
class SummaryReport:
    """Complete summary report."""
    inventory_date: str
    existing_files: list[dict] = field(default_factory=list)
    missing_periods: list[dict] = field(default_factory=list)
    sources_discovered: dict = field(default_factory=dict)
    files_acquired: list[dict] = field(default_factory=list)
    files_parsed: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    staging_paths: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory_existing_filings() -> list[FilingInventoryItem]:
    """Inventory all existing MSFT filings in microsoft-sec directory."""
    log.info("Inventorying existing MSFT filings in %s", EXISTING_DATA_DIR)
    
    if not EXISTING_DATA_DIR.exists():
        log.warning("Existing data directory not found: %s", EXISTING_DATA_DIR)
        return []
    
    items = []
    for path in EXISTING_DATA_DIR.glob("*.htm"):
        # Parse filename to extract metadata
        # Format: FORM_DATE_accession_document.htm or FORM_DATE_document.htm
        name = path.name
        stat = path.stat()
        
        # Extract form type
        form_match = re.match(r"^([A-Z0-9\s]+)_", name)
        form = form_match.group(1) if form_match else "UNKNOWN"
        
        # Extract filing date
        date_match = re.search(r"_(\d{4}-\d{2}-\d{2})_", name)
        filing_date = date_match.group(1) if date_match else ""
        
        # Extract accession and document
        accession = ""
        primary_document = name
        if "msft-" in name:
            # Format: FORM_DATE_msft-XXXX.htm
            parts = name.split("_")
            if len(parts) >= 3:
                primary_document = parts[2]
        elif "d" in name and name.count("_") >= 2:
            # Format: FORM_DATE_dXXXXXXX.htm (accession format)
            parts = name.split("_")
            if len(parts) >= 3:
                primary_document = parts[2]
                # Try to extract accession from filename
                acc_match = re.search(r"d([a-f0-9]{10,})", primary_document)
                if acc_match:
                    accession = acc_match.group(1)
        
        # Compute fiscal year/period from filing date and form
        fiscal_year = ""
        fiscal_period = ""
        if filing_date:
            try:
                fd = date.fromisoformat(filing_date)
                fiscal_year = str(MSFT_FISCAL_CALENDAR.fiscal_year_for_date(fd))
                fiscal_period = MSFT_FISCAL_CALENDAR.fiscal_period_label(fd)
            except Exception:
                pass
        
        content_hash = compute_file_hash(path)
        doc_identity = f"0000789019|{accession or 'unknown'}|{form}"
        
        items.append(FilingInventoryItem(
            form=form,
            filing_date=filing_date,
            accession=accession,
            primary_document=primary_document,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            source_path=str(path),
            file_size=stat.st_size,
            content_hash=content_hash,
            document_identity=doc_identity,
        ))
    
    log.info("Found %d existing filings", len(items))
    return items


def build_sec_manifest() -> FilingManifest:
    """Build a complete SEC EDGAR manifest for MSFT."""
    log.info("Building SEC EDGAR manifest for MSFT...")
    
    acquisition = SECAcquisition()
    # Get all forms from 2019 onwards to capture full history
    manifest = acquisition.build_manifest(
        ticker="MSFT",
        forms=list(ALL_SEC_FORMS),
        start="2019-01-01",
        end="2026-12-31",
    )
    
    log.info("SEC manifest: %d filings discovered", len(manifest.filings))
    for filing in manifest.filings:
        log.debug("  %s %s %s (FY%s %s)", filing.form, filing.filing_date, filing.accession, filing.fiscal_year, filing.fiscal_period)
    
    return manifest


def identify_missing_periods(existing: list[FilingInventoryItem], manifest: FilingManifest) -> list[MissingPeriod]:
    """Identify missing fiscal periods by comparing existing vs SEC manifest."""
    log.info("Identifying missing periods...")
    
    # Build set of existing document identities
    existing_identities = set()
    for item in existing:
        existing_identities.add(item.document_identity)
    
    # Group manifest filings by fiscal year and form
    missing = []
    
    # Expected fiscal years: 2020-2026 (MSFT FY ends June 30)
    # For each FY, expect: 10-K (annual), 10-Q (Q1, Q2, Q3), DEF 14A (proxy)
    # Q4 is covered by 10-K
    expected_fiscal_years = range(2020, 2027)
    core_forms = ["10-K", "10-Q", "DEF 14A", "8-K"]
    
    for fy in expected_fiscal_years:
        # 10-K for this fiscal year
        fy_10k = [f for f in manifest.filings if f.form == "10-K" and f.fiscal_year and int(f.fiscal_year) == fy]
        if not fy_10k:
            missing.append(MissingPeriod(
                fiscal_year=fy,
                fiscal_quarter="FY",
                form_type="10-K",
                expected_filing_window=f"July-Sep {fy}",
            ))
        else:
            for f in fy_10k:
                if f.document_identity() not in existing_identities:
                    missing.append(MissingPeriod(
                        fiscal_year=fy,
                        fiscal_quarter="FY",
                        form_type="10-K",
                        expected_filing_window=f"July-Sep {fy}",
                    ))
        
        # 10-Q for Q1, Q2, Q3 (Q4 is 10-K)
        for q in ["Q1", "Q2", "Q3"]:
            fy_10q = [f for f in manifest.filings if f.form == "10-Q" and f.fiscal_year and int(f.fiscal_year) == fy and f.fiscal_period == q]
            if not fy_10q:
                missing.append(MissingPeriod(
                    fiscal_year=fy,
                    fiscal_quarter=q,
                    form_type="10-Q",
                    expected_filing_window=f"{q} FY{fy}",
                ))
            else:
                for f in fy_10q:
                    if f.document_identity() not in existing_identities:
                        missing.append(MissingPeriod(
                            fiscal_year=fy,
                            fiscal_quarter=q,
                            form_type="10-Q",
                            expected_filing_window=f"{q} FY{fy}",
                        ))
        
        # DEF 14A (proxy statement) - typically filed before annual meeting
        fy_def = [f for f in manifest.filings if f.form == "DEF 14A" and f.fiscal_year and int(f.fiscal_year) == fy]
        if not fy_def:
            missing.append(MissingPeriod(
                fiscal_year=fy,
                fiscal_quarter="FY",
                form_type="DEF 14A",
                expected_filing_window=f"Sep-Nov {fy}",
            ))
        else:
            for f in fy_def:
                if f.document_identity() not in existing_identities:
                    missing.append(MissingPeriod(
                        fiscal_year=fy,
                        fiscal_quarter="FY",
                        form_type="DEF 14A",
                        expected_filing_window=f"Sep-Nov {fy}",
                    ))
    
    log.info("Identified %d missing periods", len(missing))
    return missing


def discover_sources() -> dict:
    """Discover official SEC EDGAR and Microsoft IR endpoints."""
    log.info("Discovering official sources...")
    
    sources = {
        "sec_edgar": {
            "submissions_index": "https://data.sec.gov/submissions/CIK0000789019.json",
            "company_tickers": "https://www.sec.gov/files/company_tickers.json",
            "archive_base": "https://www.sec.gov/Archives/edgar/data/789019/",
            "search_api": "https://efts.sec.gov/LATEST/search-index",
        },
        "microsoft_ir": {
            "homepage": "https://www.microsoft.com/en-us/investor/",
            "sec_filings": "https://www.microsoft.com/en-us/investor/sec-filings",
            "annual_reports": "https://www.microsoft.com/en-us/investor/reports/annual-reports",
            "quarterly_earnings": "https://www.microsoft.com/en-us/investor/earnings/fy-2024",
            "financial_reports": "https://www.microsoft.com/en-us/investor/reports",
        }
    }
    
    log.info("Discovered SEC EDGAR endpoints: %d", len(sources["sec_edgar"]))
    log.info("Discovered Microsoft IR endpoints: %d", len(sources["microsoft_ir"]))
    
    return sources


def acquire_missing_filings(manifest: FilingManifest, existing_identities: set[str]) -> list[AcquisitionResult]:
    """Download missing filings from SEC EDGAR to staging raw directory."""
    log.info("Acquiring missing filings...")
    
    acquisition = SECAcquisition()
    results = []
    
    # Filter manifest to only filings not already in existing_identities
    missing_filings = [f for f in manifest.filings if f.document_identity() not in existing_identities]
    
    log.info("Need to acquire %d missing filings", len(missing_filings))
    
    for filing in missing_filings:
        log.info("Acquiring: %s %s %s", filing.form, filing.filing_date, filing.accession)
        
        # Create target directory
        fy = filing.fiscal_year or filing.filing_date[:4]
        form_dir = filing.form.replace("/", "-")
        target_dir = STAGING_RAW / fy / form_dir.lower()
        target_dir.mkdir(parents=True, exist_ok=True)
        
        target_path = target_dir / filing.local_filename()
        
        if target_path.exists() and target_path.stat().st_size > 0:
            log.info("  Already cached: %s", target_path.name)
            content_hash = compute_file_hash(target_path)
            results.append(AcquisitionResult(
                filing=filing,
                success=True,
                local_path=str(target_path),
                content_hash=content_hash,
            ))
            continue
        
        try:
            payload = acquisition._request(filing.archive_url)
            target_path.write_bytes(payload)
            content_hash = filing.compute_content_hash(payload)
            
            results.append(AcquisitionResult(
                filing=filing,
                success=True,
                local_path=str(target_path),
                content_hash=content_hash,
            ))
            log.info("  Downloaded: %s (%d bytes)", target_path.name, len(payload))
            
        except Exception as e:
            log.error("  Failed: %s", e)
            results.append(AcquisitionResult(
                filing=filing,
                success=False,
                error=str(e),
            ))
        
        # Respect SEC rate limits
        time.sleep(0.2)
    
    return results


def parse_acquired_filings(acquisition_results: list[AcquisitionResult]) -> list[ParseResult]:
    """Parse acquired filings using deterministic parser into staging parsed directory."""
    log.info("Parsing acquired filings...")
    
    parser = FilingParser()
    results = []
    
    for acq in acquisition_results:
        if not acq.success or not acq.local_path:
            continue
        
        path = Path(acq.local_path)
        filing = acq.filing
        
        # Build metadata for parser
        metadata = {
            "ticker": "MSFT",
            "cik": "0000789019",
            "form_type": filing.form,
            "fiscal_year": filing.fiscal_year or filing.filing_date[:4],
            "fiscal_period": filing.fiscal_period or "FY",
            "filing_date": filing.filing_date,
            "accession_number": filing.accession,
            "content_hash": acq.content_hash or "",
        }
        
        log.info("Parsing: %s", path.name)
        
        try:
            result = parser.ingest_file(path, metadata=metadata)
            
            counts = result.counts()
            
            # Save parsed result to staging
            safe_id = filing.document_identity().replace("|", "_").replace("/", "_")
            parsed_path = STAGING_PARSED / f"MSFT_{safe_id}.json"
            
            parsed_data = {
                "filing_id": filing.document_identity(),
                "company_ticker": "MSFT",
                "metadata": metadata,
                "metrics": [
                    {
                        "id": m.id,
                        "canonical_name": m.canonical_name,
                        "category": m.category,
                        "period": m.period,
                        "value": m.value,
                        "unit": m.unit,
                    }
                    for m in result.metrics
                ],
                "segments": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "type": s.type,
                        "metrics": s.metrics,
                    }
                    for s in result.segments
                ],
                "chunks": [
                    {
                        "id": c.id,
                        "text": c.text,
                        "page_start": c.page_start,
                        "page_end": c.page_end,
                    }
                    for c in result.chunks
                ],
                "events": [
                    {
                        "id": e.id,
                        "item_code": e.item_code,
                        "title": e.title,
                        "summary": e.summary,
                    }
                    for e in result.events
                ],
                "executives": [
                    {
                        "id": ex.id,
                        "name": ex.name,
                        "title": ex.title,
                    }
                    for ex in result.executives
                ],
                "counts": counts,
                "parsed_at": datetime.utcnow().isoformat() + "Z",
            }
            
            parsed_path.write_text(json.dumps(parsed_data, indent=2))
            
            results.append(ParseResult(
                filing_identity=filing.document_identity(),
                success=True,
                metrics_count=counts.get("metrics", 0),
                segments_count=counts.get("segments", 0),
                chunks_count=counts.get("chunks", 0),
                events_count=counts.get("events", 0),
                executives_count=counts.get("executives", 0),
                parsed_path=str(parsed_path),
            ))
            
            log.info("  Parsed: %d metrics, %d segments, %d chunks, %d events, %d executives",
                     counts.get("metrics", 0), counts.get("segments", 0),
                     counts.get("chunks", 0), counts.get("events", 0),
                     counts.get("executives", 0))
            
        except Exception as e:
            log.error("  Parse failed: %s", e)
            results.append(ParseResult(
                filing_identity=filing.document_identity(),
                success=False,
                error=str(e),
            ))
    
    return results


def main() -> SummaryReport:
    """Run the complete MSFT inventory and acquisition workflow."""
    log.info("=" * 60)
    log.info("MSFT INVENTORY & ACQUISITION STARTED")
    log.info("=" * 60)
    
    report = SummaryReport(
        inventory_date=datetime.utcnow().isoformat() + "Z",
    )
    
    # Step 1: Inventory existing filings
    existing = inventory_existing_filings()
    report.existing_files = [asdict(item) for item in existing]
    
    # Step 2: Build SEC manifest
    manifest = build_sec_manifest()
    
    # Step 3: Identify missing periods
    missing = identify_missing_periods(existing, manifest)
    report.missing_periods = [asdict(m) for m in missing]
    
    # Step 4: Discover sources
    sources = discover_sources()
    report.sources_discovered = sources
    
    # Step 5: Acquire missing filings
    existing_identities = {item.document_identity for item in existing}
    acquisition_results = acquire_missing_filings(manifest, existing_identities)
    report.files_acquired = [asdict(r) for r in acquisition_results]
    
    # Step 6: Parse acquired filings
    parse_results = parse_acquired_filings(acquisition_results)
    report.files_parsed = [asdict(r) for r in parse_results]
    
    # Collect failures
    for r in acquisition_results:
        if not r.success:
            report.failures.append({
                "stage": "acquisition",
                "filing": r.filing.document_identity() if r.filing else "unknown",
                "error": r.error,
            })
    for r in parse_results:
        if not r.success:
            report.failures.append({
                "stage": "parsing",
                "filing": r.filing_identity,
                "error": r.error,
            })
    
    # Staging paths
    report.staging_paths = {
        "raw": str(STAGING_RAW),
        "parsed": str(STAGING_PARSED),
    }
    
    # Save report
    report_path = STAGING_PARSED.parent / "msft_inventory_report.json"
    report_path.write_text(report.to_json())
    log.info("Report saved to: %s", report_path)
    
    log.info("=" * 60)
    log.info("SUMMARY:")
    log.info("  Existing files: %d", len(existing))
    log.info("  Missing periods: %d", len(missing))
    log.info("  Files acquired: %d", sum(1 for r in acquisition_results if r.success))
    log.info("  Files parsed: %d", sum(1 for r in parse_results if r.success))
    log.info("  Failures: %d", len(report.failures))
    log.info("=" * 60)
    
    return report


if __name__ == "__main__":
    report = main()
    print(report.to_json())