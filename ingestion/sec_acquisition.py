"""SEC EDGAR Acquisition - Generic filing discovery and download.

Uses the CompanyRegistry for CIK resolution instead of hardcoded values.
Supports all SEC form types in the document taxonomy.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .registry import Company, CompanyRegistry, get_registry

__all__ = [
    "Filing",
    "FilingManifest",
    "SECAcquisition",
    "DownloadStats",
    "DEFAULT_USER_AGENT",
    "REQUEST_DELAY_SECONDS",
]

# SEC asks automated clients to identify themselves
DEFAULT_USER_AGENT = "FinGraph/1.0 (contact@example.com)"
REQUEST_DELAY_SECONDS = 0.2
MAX_ATTEMPTS = 4
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Timeout configuration - separate connect and read timeouts
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
MAX_TOTAL_TIMEOUT = 60.0

# Document taxonomy - all supported SEC form types
CORE_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A")
IMPORTANT_FORMS = ("3", "4", "5")
USEFUL_FORMS = (
    "13F-HR", "SC 13D", "SC 13G", "S-3", "S-8",
    "424B", "424B1", "424B2", "424B3", "424B4", "424B5"
)
OPTIONAL_FORMS = ("ARS", "SD", "11-K")
ALL_SEC_FORMS = CORE_FORMS + IMPORTANT_FORMS + USEFUL_FORMS + OPTIONAL_FORMS
DEFAULT_FORMS = CORE_FORMS


@dataclass(frozen=True)
class Filing:
    """One filing selected from an EDGAR submissions index."""
    
    form: str
    filing_date: str
    accession: str
    primary_document: str
    cik: str
    report_date: str = ""
    document_title: str = ""
    document_url: str = ""
    source: str = "SEC"
    source_authority: str = "EDGAR"
    filing_status: str = ""
    fiscal_period: str = ""
    fiscal_year: str = ""
    is_amended: bool = False
    amendment_type: str = ""
    exhibit_type: str = ""
    content_hash: str = ""
    ticker: str = ""
    
    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")
    
    @property
    def archive_url(self) -> str:
        return (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{self.cik}/{self.accession_nodash}/{self.primary_document}"
        )
    
    def compute_content_hash(self, content: bytes) -> str:
        """Compute SHA256 hash of document content for deduplication."""
        return hashlib.sha256(content).hexdigest()
    
    def with_metadata(self, **kwargs) -> "Filing":
        """Return a new Filing with updated metadata fields."""
        return dataclasses.replace(self, **kwargs)
    
    def document_identity(self) -> str:
        """Unique document identity for idempotency.
        
        Uses CIK + accession_number + form_type as the primary key.
        This matches the SEC's unique filing identifier.
        """
        return f"{self.cik}|{self.accession}|{self.form}"
    
    def local_filename(self) -> str:
        """Generate a safe local filename."""
        safe_primary = self.primary_document.replace("/", "_")
        form_safe = self.form.replace("/", "-")
        return f"{form_safe}_{self.filing_date}_{safe_primary}"


@dataclass
class DownloadStats:
    cached: int = 0
    downloaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FilingManifest:
    """Complete manifest of filings for a company within a date range."""
    
    company: Company
    filings: list[Filing]
    date_range: tuple[str, str]
    forms_requested: list[str]
    
    def by_form(self) -> dict[str, list[Filing]]:
        """Group filings by form type."""
        result: dict[str, list[Filing]] = {}
        for filing in self.filings:
            result.setdefault(filing.form, []).append(filing)
        return result
    
    def by_fiscal_year(self) -> dict[int, list[Filing]]:
        """Group filings by fiscal year."""
        result: dict[int, list[Filing]] = {}
        for filing in self.filings:
            try:
                fy = int(filing.fiscal_year) if filing.fiscal_year else int(filing.filing_date[:4])
                result.setdefault(fy, []).append(filing)
            except (ValueError, TypeError):
                pass
        return result
    
    def filter_by_date_range(self, start: str, end: str) -> "FilingManifest":
        """Return a new manifest filtered by filing date."""
        filtered = [f for f in self.filings if start <= f.filing_date <= end]
        return FilingManifest(
            company=self.company,
            filings=filtered,
            date_range=(start, end),
            forms_requested=self.forms_requested,
        )
    
    def filter_by_forms(self, forms: list[str]) -> "FilingManifest":
        """Return a new manifest filtered by form types."""
        forms_upper = {f.upper() for f in forms}
        filtered = [f for f in self.filings if f.form.upper() in forms_upper]
        return FilingManifest(
            company=self.company,
            filings=filtered,
            date_range=self.date_range,
            forms_requested=forms,
        )


class SECAcquisition:
    """Generic SEC EDGAR acquisition for any registered company.
    
    Uses CompanyRegistry for CIK resolution - no hardcoded values.
    """
    
    def __init__(
        self,
        registry: CompanyRegistry | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        delay: float = REQUEST_DELAY_SECONDS,
    ) -> None:
        self.registry = registry or get_registry()
        self.user_agent = user_agent
        self.delay = delay
    
    def build_manifest(
        self,
        ticker: str,
        forms: Sequence[str] = DEFAULT_FORMS,
        start: str = "2020-01-01",
        end: str = "2026-12-31",
    ) -> FilingManifest:
        """Build a filing manifest for a company from SEC EDGAR.
        
        Args:
            ticker: Company ticker symbol (e.g., "AAPL", "TSLA", "MSFT")
            forms: SEC form types to include
            start: Start date (YYYY-MM-DD)
            end: End date (YYYY-MM-DD)
        
        Returns:
            FilingManifest with all matching filings
        """
        company = self.registry.get(ticker)
        if not company:
            raise ValueError(f"Unknown company: {ticker}. Register it first.")
        
        cik = company.cik
        cik_padded = cik.zfill(10)
        base = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        index = json.loads(self._request(base))
        
        rows: list[dict[str, str]] = []
        recent = index["filings"]["recent"]
        for position in range(len(recent["form"])):
            rows.append(
                {key: recent[key][position] for key in recent if isinstance(recent[key], list)}
            )
        for extra in index["filings"].get("files", []):
            shard = extra["name"]
            if not shard.startswith("http"):
                shard = f"https://data.sec.gov/submissions/{shard}"
            payload = json.loads(self._request(shard))
            for position in range(len(payload["form"])):
                rows.append(
                    {
                        key: payload[key][position]
                        for key in payload
                        if isinstance(payload[key], list)
                    }
                )
        
        # Normalize form types for matching
        wanted_normalized = set()
        for form in forms:
            form_upper = form.upper()
            if form_upper.startswith("424B"):
                wanted_normalized.add("424B")
            else:
                wanted_normalized.add(form_upper)
        
        selected: list[Filing] = []
        for row in rows:
            form_raw = row.get("form", "").upper()
            if form_raw.startswith("424B"):
                form_normalized = "424B"
            else:
                form_normalized = form_raw
            
            if form_normalized not in wanted_normalized:
                continue
            
            filing_date = row.get("filingDate", "")
            if not (start <= filing_date <= end):
                continue
            
            document_title = row.get("primaryDocument", "")
            report_date = row.get("reportDate", "")
            fiscal_year = row.get("fiscalYear", "")
            fiscal_period = row.get("fiscalPeriod", "")
            is_amended = form_raw.endswith("/A") or form_raw.endswith("-A")
            amendment_type = "A" if is_amended else ""
            
            filing = Filing(
                form=form_raw,
                filing_date=filing_date,
                accession=row["accessionNumber"],
                primary_document=row["primaryDocument"],
                cik=str(int(cik_padded)),
                report_date=report_date,
                document_title=document_title,
                document_url=f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/{row['accessionNumber'].replace('-', '')}/{row['primaryDocument']}",
                source="SEC",
                source_authority="EDGAR",
                filing_status="filed",
                fiscal_period=fiscal_period,
                fiscal_year=fiscal_year,
                is_amended=is_amended,
                amendment_type=amendment_type,
                exhibit_type=row.get("exhibitType", "") if "exhibitType" in row else "",
                ticker=company.ticker,
            )
            selected.append(filing)
        
        selected.sort(key=lambda f: (f.form, f.filing_date))
        
        return FilingManifest(
            company=company,
            filings=selected,
            date_range=(start, end),
            forms_requested=list(forms),
        )
    
    def download_filings(
        self,
        manifest: FilingManifest,
        dest: Path,
        refresh: bool = False,
    ) -> DownloadStats:
        """Download all filings in a manifest to local storage.
        
        Args:
            manifest: FilingManifest to download
            dest: Destination directory (will create company/year/form subdirs)
            refresh: If True, re-download files already cached
        
        Returns:
            DownloadStats with counts
        """
        dest.mkdir(parents=True, exist_ok=True)
        stats = DownloadStats()
        
        for filing in manifest.filings:
            # Create company/year/form subdirectory structure
            fy = filing.fiscal_year or filing.filing_date[:4]
            form_dir = filing.form.replace("/", "-")
            target_dir = dest / manifest.company.ticker.lower() / fy / form_dir.lower()
            target_dir.mkdir(parents=True, exist_ok=True)
            
            target = target_dir / filing.local_filename()
            
            if target.exists() and target.stat().st_size > 0 and not refresh:
                stats.cached += 1
                continue
            
            try:
                payload = self._request(filing.archive_url)
            except Exception as exc:
                stats.failed.append((target.name, f"{type(exc).__name__}: {exc}"))
                continue
            
            target.write_bytes(payload)
            # Compute and store content hash for deduplication
            content_hash = filing.compute_content_hash(payload)
            stats.downloaded += 1
            
            # Update the filing with content hash (create new immutable)
            # Note: In practice, you'd want to persist this metadata
            # For now, we compute it on the fly when needed
        
        return stats
    
    def _request(self, url: str, connect_timeout: float | None = None, read_timeout: float | None = None) -> bytes:
        """GET *url*, honouring SEC etiquette and retrying transient failures.

        Uses separate connect and read timeouts. Retries only transient failures
        (429, 500, 502, 503, 504) with exponential backoff + jitter.
        Parses Retry-After header for 429 responses.
        """
        connect_timeout = min(max(0.1, connect_timeout or DEFAULT_CONNECT_TIMEOUT), MAX_TOTAL_TIMEOUT)
        read_timeout = min(max(0.1, read_timeout or DEFAULT_READ_TIMEOUT), MAX_TOTAL_TIMEOUT)
        start_time = time.monotonic()
        total_budget = MAX_TOTAL_TIMEOUT

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Check total budget
            elapsed = time.monotonic() - start_time
            if elapsed >= total_budget:
                raise RuntimeError(f"Total time budget ({total_budget}s) exceeded for {url}")

            if self.delay:
                time.sleep(self.delay)

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip",
                    "Accept": "text/html,application/json,*/*",
                },
            )

            # Use socket timeout for both connect and read (urllib limitation)
            # The effective timeout is the minimum of remaining budget and read_timeout
            remaining = total_budget - elapsed
            socket_timeout = min(read_timeout, max(0.01, remaining))

            try:
                with urllib.request.urlopen(request, timeout=socket_timeout) as response:
                    payload = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        payload = gzip.decompress(payload)
                    return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    # Parse Retry-After header
                    retry_after = 1.0
                    if exc.headers and hasattr(exc.headers, "get"):
                        retry_after_hdr = exc.headers.get("Retry-After")
                        if retry_after_hdr:
                            try:
                                retry_after = float(retry_after_hdr)
                            except (ValueError, TypeError):
                                pass
                    # Add jitter
                    import random
                    jitter = random.uniform(0.1, 0.5)
                    total_delay = retry_after + jitter

                    # Check if we have budget for retry
                    now_elapsed = time.monotonic() - start_time
                    if attempt >= MAX_ATTEMPTS or (now_elapsed + total_delay) >= total_budget:
                        raise
                    time.sleep(total_delay)
                    continue
                elif exc.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    # Exponential backoff with jitter for other retryable statuses
                    import random
                    delay = (2 ** attempt) + random.uniform(0.1, 0.5)
                    now_elapsed = time.monotonic() - start_time
                    if (now_elapsed + delay) >= total_budget:
                        raise
                    time.sleep(delay)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    # Exponential backoff with jitter for network errors
                    import random
                    delay = (2 ** attempt) + random.uniform(0.1, 0.5)
                    now_elapsed = time.monotonic() - start_time
                    if (now_elapsed + delay) >= total_budget:
                        raise
                    time.sleep(delay)
                    continue
                raise
        raise RuntimeError(f"exhausted retries for {url}") from last_error


def load_filing_content(path: Path) -> str:
    """Load and parse a downloaded filing HTML file."""
    import document_loader as dl
    
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return dl.load_document(path).content
        except dl.DocumentLoadError:
            return dl.load_document(path, file_type="html").content