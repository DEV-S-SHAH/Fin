#!/usr/bin/env python3
"""Parse MSFT staging files using deterministic parser."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/Users/dev/Downloads/Fin")

from sandbox_engine.parser import FilingParser
from sandbox_engine import parser as parser_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("msft_parse")

STAGING_RAW = Path("/Users/dev/Downloads/Fin/data/staging/MSFT/raw")
STAGING_PARSED = Path("/Users/dev/Downloads/Fin/data/staging/MSFT/parsed")

MSFT_FISCAL_CALENDAR = {
    "year_end_month": 6,
    "year_end_day": 30,
}

# Core financial forms to parse
CORE_FORMS = {"10-K", "10-Q", "8-K", "DEF 14A", "11-K"}

# Monkey-patch the buggy extract_suppliers function
_original_extract_suppliers = parser_module.extract_suppliers

def _fixed_extract_suppliers(raw: str, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fixed version of extract_suppliers that handles float values in pandas."""
    import io
    import pandas as pd
    from sandbox_engine.parser import clean_text, strip_markup, html_body, _DASHES
    
    suppliers: dict[str, dict[str, Any]] = {}
    ticker = metadata.get("ticker", "")
    filing_date = metadata.get("filing_date", "")
    
    # Extract from explicit supplier lists in tables
    tables = []
    try:
        tables = list(pd.read_html(io.StringIO(html_body(raw)), flavor="lxml"))
    except Exception:
        pass

    for frame in tables:
        if frame is None or frame.empty:
            continue
        grid = frame.astype(str)
        # Look for supplier-related columns
        for col in grid.columns:
            # FIX: Convert all values to strings before joining
            col_text = " ".join(str(x) for x in grid[col].tolist()).lower()
            if any(kw in col_text for kw in ["supplier", "vendor", "foundry", "assembly", "manufacturing partner"]):
                for val in grid[col].tolist():
                    val = clean_text(val)
                    if val and len(val) > 3 and val.lower() not in _DASHES:
                        key = val
                        if key not in suppliers:
                            suppliers[key] = {
                                "name": key,
                                "relationship_type": "Supplier",
                                "criticality": "Medium",
                                "ticker": "",
                                "cik": "",
                                "headquarters": "",
                                "description": f"Listed in supplier table in {filing_date} filing",
                            }
    return suppliers

# Apply monkey patch
parser_module.extract_suppliers = _fixed_extract_suppliers


def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_filing_metadata(path: Path) -> dict:
    """Extract metadata from file path."""
    name = path.name
    
    # Extract form
    form_match = None
    for form in ["10-K", "10-Q", "8-K", "DEF 14A", "11-K", "3", "4", "5"]:
        if name.startswith(f"{form}_"):
            form_match = form
            break
    form = form_match or "UNKNOWN"
    
    # Extract filing date
    import re
    date_match = re.search(r"_(\d{4}-\d{2}-\d{2})_", name)
    filing_date = date_match.group(1) if date_match else ""
    
    # Extract fiscal year from path
    fiscal_year = path.parts[-3] if len(path.parts) >= 3 else ""
    
    # Compute fiscal period
    fiscal_period = ""
    if filing_date:
        from datetime import date
        try:
            fd = date.fromisoformat(filing_date)
            if fd.month > 6 or (fd.month == 6 and fd.day >= 30):
                fy = fd.year + 1
            else:
                fy = fd.year
            fiscal_year = str(fy)
            # Determine quarter
            months_since_start = (fd.month - 7) % 12 + 1
            if fd.month == 6 and fd.day >= 30:
                months_since_start = 12
            quarter = (months_since_start - 1) // 3 + 1
            fiscal_period = f"FY{fy}-Q{quarter}"
        except Exception:
            pass
    
    # Extract accession
    accession = ""
    if "msft-" in name:
        parts = name.split("_")
        if len(parts) >= 3:
            primary_document = parts[2]
    else:
        primary_document = name
        acc_match = re.search(r"d([a-f0-9]{10,})", primary_document)
        if acc_match:
            accession = acc_match.group(1)
    
    return {
        "form": form,
        "filing_date": filing_date,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "accession": accession,
        "primary_document": primary_document,
    }


def parse_filing(path: Path, parser: FilingParser) -> dict:
    """Parse a single filing."""
    metadata = extract_filing_metadata(path)
    
    # Skip non-core forms
    if metadata["form"] not in CORE_FORMS:
        return {"skipped": True, "reason": f"Non-core form: {metadata['form']}"}
    
    content_hash = compute_file_hash(path)
    
    try:
        result = parser.ingest_file(path)
        counts = result.counts()
        
        # Build safe ID for output
        safe_id = f"0000789019|{metadata['accession'] or 'unknown'}|{metadata['form']}".replace("|", "_").replace("/", "_")
        parsed_path = STAGING_PARSED / f"MSFT_{safe_id}.json"
        
        # Extract data from ExtractionResult (which uses dicts keyed by ID)
        metrics_list = []
        for metric_id, metric_data in result.metrics.items():
            if isinstance(metric_data, dict):
                metrics_list.append({
                    "id": metric_id,
                    "canonical_name": metric_data.get("canonical_name", ""),
                    "category": metric_data.get("statement_category", ""),
                    "period": metric_data.get("period", ""),
                    "value": metric_data.get("value", 0),
                    "unit": metric_data.get("currency", "USD"),
                })
        
        segments_list = []
        for seg_id, seg_data in result.segments.items():
            if isinstance(seg_data, dict):
                segments_list.append({
                    "id": seg_id,
                    "name": seg_data.get("name", ""),
                    "type": seg_data.get("type", ""),
                    "metrics": seg_data.get("metrics", []),
                })
        
        chunks_list = []
        for chunk_id, chunk_data in result.chunks.items():
            if isinstance(chunk_data, dict):
                chunks_list.append({
                    "id": chunk_id,
                    "text": chunk_data.get("text", ""),
                    "page_start": chunk_data.get("page_start", 0),
                    "page_end": chunk_data.get("page_end", 0),
                })
        
        events_list = []
        for event_id, event_data in result.events.items():
            if isinstance(event_data, dict):
                events_list.append({
                    "id": event_id,
                    "item_code": event_data.get("item_code", ""),
                    "title": event_data.get("title", ""),
                    "summary": event_data.get("summary", ""),
                })
        
        executives_list = []
        for exec_id, exec_data in result.executives.items():
            if isinstance(exec_data, dict):
                executives_list.append({
                    "id": exec_id,
                    "name": exec_data.get("name", ""),
                    "title": exec_data.get("title", ""),
                })
        
        parsed_data = {
            "filing_id": f"0000789019|{metadata['accession'] or 'unknown'}|{metadata['form']}",
            "company_ticker": "MSFT",
            "metadata": result.metadata,
            "metrics": metrics_list,
            "segments": segments_list,
            "chunks": chunks_list,
            "events": events_list,
            "executives": executives_list,
            "counts": counts,
            "parsed_at": datetime.utcnow().isoformat() + "Z",
        }
        
        parsed_path.write_text(json.dumps(parsed_data, indent=2))
        
        return {
            "success": True,
            "filing": path.name,
            "form": metadata["form"],
            "fiscal_year": metadata["fiscal_year"],
            "fiscal_period": metadata["fiscal_period"],
            "metrics": counts.get("metrics", 0),
            "segments": counts.get("segments", 0),
            "chunks": counts.get("chunks", 0),
            "events": counts.get("events", 0),
            "executives": counts.get("executives", 0),
            "parsed_path": str(parsed_path),
        }
        
    except Exception as e:
        log.error("Parse failed for %s: %s", path.name, e)
        return {
            "success": False,
            "filing": path.name,
            "form": metadata["form"],
            "error": str(e),
        }


def main():
    """Parse all core form filings in staging raw."""
    log.info("Starting MSFT staging parse...")
    
    parser = FilingParser()
    results = []
    
    # Find all .htm files
    htm_files = list(STAGING_RAW.rglob("*.htm"))
    log.info("Found %d .htm files in staging raw", len(htm_files))
    
    for path in sorted(htm_files):
        log.info("Parsing: %s", path.name)
        result = parse_filing(path, parser)
        results.append(result)
        
        if result.get("success"):
            log.info("  Success: %d metrics, %d segments, %d chunks, %d events, %d executives",
                     result["metrics"], result["segments"], result["chunks"],
                     result["events"], result["executives"])
        elif result.get("skipped"):
            log.info("  Skipped: %s", result["reason"])
        else:
            log.error("  Failed: %s", result.get("error"))
    
    # Summary
    successful = [r for r in results if r.get("success")]
    skipped = [r for r in results if r.get("skipped")]
    failed = [r for r in results if not r.get("success") and not r.get("skipped")]
    
    log.info("=" * 60)
    log.info("PARSE SUMMARY:")
    log.info("  Total files: %d", len(results))
    log.info("  Successful: %d", len(successful))
    log.info("  Skipped: %d", len(skipped))
    log.info("  Failed: %d", len(failed))
    if successful:
        log.info("  Total metrics: %d", sum(r["metrics"] for r in successful))
        log.info("  Total segments: %d", sum(r["segments"] for r in successful))
        log.info("  Total chunks: %d", sum(r["chunks"] for r in successful))
        log.info("  Total events: %d", sum(r["events"] for r in successful))
        log.info("  Total executives: %d", sum(r["executives"] for r in successful))
    log.info("=" * 60)
    
    # Save summary report
    report = {
        "parse_date": datetime.utcnow().isoformat() + "Z",
        "total_files": len(results),
        "successful": len(successful),
        "skipped": len(skipped),
        "failed": len(failed),
        "results": results,
    }
    
    report_path = STAGING_PARSED / "parse_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    log.info("Parse report saved to: %s", report_path)


if __name__ == "__main__":
    main()