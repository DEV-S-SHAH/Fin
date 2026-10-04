"""Fetch SEC EDGAR filings and feed them to the existing GraphRAG pipeline.

This module is deliberately confined to the *ingestion* side of the pipeline.
It selects filings from an EDGAR submissions index, downloads the primary
document, and adapts the parsed text to the :class:`graphrag.document.Document`
that :func:`graphrag.ingest.ingest_document` already accepts. Extraction,
entity resolution and graph construction are used exactly as they ship.

HTML from EDGAR has no page boundaries, so the text is cut into fixed-size
segments that stand in for pages. That keeps ``Chunk.location`` honest about
being a synthetic locator rather than inventing real page numbers.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from typing import Iterator, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

import document_loader as dl  # noqa: E402
from graphrag.document import Document, chunk_document  # noqa: E402

# SEC asks automated clients to identify themselves and to stay under roughly
# ten requests per second. We stay well below that.
DEFAULT_USER_AGENT = "FinResearch contact@example.com"
REQUEST_DELAY_SECONDS = 0.2
MAX_ATTEMPTS = 4
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Timeout configuration - separate connect and read timeouts
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
MAX_TOTAL_TIMEOUT = 60.0

# All supported SEC form types organized by category
CORE_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A")
IMPORTANT_FORMS = ("3", "4", "5")
USEFUL_FORMS = ("13F-HR", "SC 13D", "SC 13G", "S-3", "S-8", "424B", "424B1", "424B2", "424B3", "424B4", "424B5")
OPTIONAL_FORMS = ("ARS", "SD", "11-K")
ALL_SEC_FORMS = CORE_FORMS + IMPORTANT_FORMS + USEFUL_FORMS + OPTIONAL_FORMS

DEFAULT_FORMS = CORE_FORMS

# EDGAR HTML has no pages; this stands in for one so chunk locations read as
# ranges rather than a single meaningless "page 1".
SEGMENT_CHARS = 3_000


@dataclasses.dataclass(frozen=True)
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
        return Filing(
            form=self.form,
            filing_date=self.filing_date,
            accession=self.accession,
            primary_document=self.primary_document,
            cik=self.cik,
            report_date=self.report_date,
            document_title=kwargs.get("document_title", self.document_title),
            document_url=kwargs.get("document_url", self.document_url),
            source=kwargs.get("source", self.source),
            source_authority=kwargs.get("source_authority", self.source_authority),
            filing_status=kwargs.get("filing_status", self.filing_status),
            fiscal_period=kwargs.get("fiscal_period", self.fiscal_period),
            fiscal_year=kwargs.get("fiscal_year", self.fiscal_year),
            is_amended=kwargs.get("is_amended", self.is_amended),
            amendment_type=kwargs.get("amendment_type", self.amendment_type),
            exhibit_type=kwargs.get("exhibit_type", self.exhibit_type),
            content_hash=kwargs.get("content_hash", self.content_hash),
        )


@dataclasses.dataclass
class DownloadStats:
    cached: int = 0
    downloaded: int = 0
    failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def _request(
    url: str,
    user_agent: str,
    delay: float,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
) -> bytes:
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

        if delay:
            time.sleep(delay)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
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


def build_manifest(
    cik: str,
    forms: Sequence[str] = DEFAULT_FORMS,
    start: str = "2020-01-01",
    end: str = "2025-12-31",
    user_agent: str = DEFAULT_USER_AGENT,
    delay: float = REQUEST_DELAY_SECONDS,
) -> list[Filing]:
    """Return the filings matching *forms* whose filing date falls in range.

    Apple's older filings may live in a supplementary submissions file, so the
    additional index is merged in rather than assuming ``recent`` is complete.
    """
    cik_padded = cik.zfill(10)
    base = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    index = json.loads(_request(base, user_agent, delay))

    rows: list[dict[str, str]] = []
    recent = index["filings"]["recent"]
    for position in range(len(recent["form"])):
        rows.append(
            {key: recent[key][position] for key in recent if isinstance(recent[key], list)}
        )
    for extra in index["filings"].get("files", []):
        # The index lists supplementary shards as bare filenames.
        shard = extra["name"]
        if not shard.startswith("http"):
            shard = f"https://data.sec.gov/submissions/{shard}"
        payload = json.loads(_request(shard, user_agent, delay))
        for position in range(len(payload["form"])):
            rows.append(
                {
                    key: payload[key][position]
                    for key in payload
                    if isinstance(payload[key], list)
                }
            )

    # Normalize form types for matching (handle 424B variants, etc.)
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
        # Match 424B variants
        if form_raw.startswith("424B"):
            form_normalized = "424B"
        else:
            form_normalized = form_raw

        if form_normalized not in wanted_normalized:
            continue
        filing_date = row.get("filingDate", "")
        if not (start <= filing_date <= end):
            continue

        # Extract additional metadata from the row
        document_title = row.get("primaryDocument", "")
        report_date = row.get("reportDate", "")
        fiscal_year = row.get("fiscalYear", "")
        fiscal_period = row.get("fiscalPeriod", "")
        is_amended = form_raw.endswith("/A") or form_raw.endswith("-A")
        amendment_type = "A" if is_amended else ""

        selected.append(
            Filing(
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
            )
        )
    selected.sort(key=lambda f: (f.form, f.filing_date))
    return selected


def download_filings(
    filings: Sequence[Filing],
    dest: Path,
    user_agent: str = DEFAULT_USER_AGENT,
    delay: float = REQUEST_DELAY_SECONDS,
    refresh: bool = False,
) -> DownloadStats:
    """Download each filing's primary document into *dest*, skipping cached hits."""
    dest.mkdir(parents=True, exist_ok=True)
    stats = DownloadStats()
    for filing in filings:
        # Sanitize primary_document to handle subdirectories
        safe_primary = filing.primary_document.replace("/", "_")
        target = dest / f"{filing.form.replace('/', '-')}_{filing.filing_date}_{safe_primary}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 0 and not refresh:
            stats.cached += 1
            continue
        try:
            payload = _request(filing.archive_url, user_agent, delay)
        except Exception as exc:  # keep going; report at the end
            stats.failed.append((target.name, f"{type(exc).__name__}: {exc}"))
            continue
        target.write_bytes(payload)
        # Compute content hash for deduplication
        content_hash = filing.compute_content_hash(payload)
        stats.downloaded += 1
    return stats


def load_text(path: Path) -> str:
    """Parse an already-downloaded filing, tolerating EDGAR's markup quirks."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return dl.load_document(path).content
        except dl.DocumentLoadError:
            return dl.load_document(path, file_type="html").content


def load_filing_with_metadata(path: Path, filing: Filing) -> tuple[str, dict]:
    """Parse a filing and return content with extracted metadata."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            doc = dl.load_document(path)
            content = doc.content
            metadata = doc.metadata
        except dl.DocumentLoadError:
            doc = dl.load_document(path, file_type="html")
            content = doc.content
            metadata = doc.metadata

    # Add filing metadata to the document metadata
    metadata.update({
        "form_type": filing.form,
        "filing_date": filing.filing_date,
        "accession_number": filing.accession,
        "cik": filing.cik,
        "report_date": filing.report_date,
        "document_title": filing.document_title,
        "document_url": filing.document_url,
        "source": filing.source,
        "source_authority": filing.source_authority,
        "filing_status": filing.filing_status,
        "fiscal_period": filing.fiscal_period,
        "fiscal_year": filing.fiscal_year,
        "is_amended": filing.is_amended,
        "amendment_type": filing.amendment_type,
        "exhibit_type": filing.exhibit_type,
        "content_hash": filing.content_hash,
    })
    return content, metadata


def to_document(path: Path, content: str | None = None) -> Document:
    """Adapt filing *content* to the pipeline's :class:`Document` type.

    The pipeline is untouched: this only supplies the plain text it expects,
    segmented into stand-in pages because EDGAR HTML is unpaginated.
    """
    text = content if content is not None else load_text(path)
    segments = [text[i : i + SEGMENT_CHARS] for i in range(0, len(text), SEGMENT_CHARS)]
    if not segments:
        segments = [""]
    return Document(path=path, pages=segments)


def to_document_with_metadata(path: Path, filing: Filing, content: str | None = None) -> tuple[Document, dict]:
    """Create a Document with filing metadata for provenance tracking."""
    text, metadata = load_filing_with_metadata(path, filing)
    if content is not None:
        text = content
    segments = [text[i : i + SEGMENT_CHARS] for i in range(0, len(text), SEGMENT_CHARS)]
    if not segments:
        segments = [""]
    return Document(path=path, pages=segments), metadata


def iter_chunks(
    path: Path, chunk_tokens: int, overlap_tokens: int
) -> Iterator[tuple[Path, int, object]]:
    """Yield ``(path, index, Chunk)`` for a filing without re-parsing it."""
    document = to_document(path)
    for chunk in chunk_document(document, chunk_tokens, overlap_tokens):
        yield path, chunk.index, chunk


def describe_corpus(root: Path, chunk_tokens: int, overlap_tokens: int) -> dict[str, int]:
    """Parse and chunk every cached filing, tallying what a full run would cost."""
    totals = {"filings": 0, "chars": 0, "chunks": 0, "empty": 0, "failed": 0}
    for path in sorted(root.rglob("*.htm*")):
        try:
            document = to_document(path)
        except Exception:
            totals["failed"] += 1
            continue
        totals["filings"] += 1
        totals["chars"] += sum(len(p) for p in document.pages)
        count = len(chunk_document(document, chunk_tokens, overlap_tokens))
        totals["chunks"] += count
        if count == 0:
            totals["empty"] += 1
    return totals


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cik", default="0000320193", help="issuer CIK")
    parser.add_argument(
        "--forms",
        nargs="+",
        default=list(DEFAULT_FORMS),
        help=(
            "filing forms to select. Categories: "
            "core (10-K,10-Q,8-K,DEF 14A), "
            "important (3,4,5), "
            "useful (13F-HR,SC 13D,SC 13G,S-3,S-8,424B*), "
            "optional (ARS,SD,11-K), "
            "or 'all' for everything"
        ),
    )
    parser.add_argument("--start", default="2020-01-01", help="earliest filing date")
    parser.add_argument("--end", default="2026-12-31", help="latest filing date")
    parser.add_argument(
        "--dest", type=Path, default=Path("data/aapl-sec"), help="download directory"
    )
    parser.add_argument(
        "--user-agent", default=DEFAULT_USER_AGENT, help="SEC-compliant contact string"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help="seconds to wait between requests",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-download files already cached"
    )
    parser.add_argument(
        "--skip-download", action="store_true", help="only report on cached filings"
    )
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        default=None,
        help="override the pipeline chunk size when measuring",
    )
    parser.add_argument(
        "--overlap-tokens",
        type=int,
        default=None,
        help="override the pipeline overlap when measuring",
    )
    parser.add_argument(
        "--backfill-years",
        nargs="+",
        type=int,
        default=None,
        help="specific years to backfill (e.g., 2020 2021 2022); default: all from start to end",
    )
    parser.add_argument(
        "--report-coverage",
        action="store_true",
        help="report year coverage without downloading",
    )
    return parser.parse_args(argv)


def _expand_forms(forms: list[str]) -> list[str]:
    """Expand form category names to actual form types."""
    expanded = []
    for form in forms:
        form_lower = form.lower()
        if form_lower == "core":
            expanded.extend(CORE_FORMS)
        elif form_lower == "important":
            expanded.extend(IMPORTANT_FORMS)
        elif form_lower == "useful":
            expanded.extend(USEFUL_FORMS)
        elif form_lower == "optional":
            expanded.extend(OPTIONAL_FORMS)
        elif form_lower == "all":
            expanded.extend(ALL_SEC_FORMS)
        else:
            expanded.append(form)
    return expanded


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    # Expand form categories
    forms = _expand_forms(args.forms)

    chunk_tokens = args.chunk_tokens
    overlap_tokens = args.overlap_tokens
    if chunk_tokens is None or overlap_tokens is None:
        from graphrag.config import GraphRAGConfig

        config = GraphRAGConfig()
        chunk_tokens = chunk_tokens or config.chunk_tokens
        overlap_tokens = overlap_tokens or config.chunk_overlap_tokens

    # Handle backfill years
    if args.backfill_years:
        # Build manifest for each year separately
        all_filings = []
        for year in sorted(args.backfill_years):
            year_start = f"{year}-01-01"
            year_end = f"{year}-12-31"
            filings = build_manifest(
                args.cik, forms, year_start, year_end, args.user_agent, args.delay
            )
            all_filings.extend(filings)
        filings = all_filings
    else:
        filings = build_manifest(
            args.cik, forms, args.start, args.end, args.user_agent, args.delay
        )

    by_form: dict[str, int] = {}
    by_year: dict[int, int] = {}
    for filing in filings:
        by_form[filing.form] = by_form.get(filing.form, 0) + 1
        try:
            fy = int(filing.fiscal_year) if filing.fiscal_year else int(filing.filing_date[:4])
            by_year[fy] = by_year.get(fy, 0) + 1
        except (ValueError, TypeError):
            pass

    if args.report_coverage:
        print(f"Coverage report for {args.cik}:")
        print(f"  Total filings: {len(filings)}")
        print(f"  By form: {by_form}")
        print(f"  By fiscal year: {dict(sorted(by_year.items()))}")
        return 0

    print(f"manifest: {len(filings)} filings {by_form}")
    print(f"  by fiscal year: {dict(sorted(by_year.items()))}")

    if not args.skip_download:
        stats = download_filings(
            filings, args.dest, args.user_agent, args.delay, args.refresh
        )
        print(
            f"downloads: {stats.downloaded} fetched, {stats.cached} cached, "
            f"{len(stats.failed)} failed"
        )
        for name, reason in stats.failed:
            print(f"  FAILED {name}: {reason}", file=sys.stderr)

    totals = describe_corpus(args.dest, chunk_tokens, overlap_tokens)
    print(
        f"corpus: {totals['filings']} files, {totals['chars']:,} chars, "
        f"{totals['chunks']:,} chunks "
        f"({chunk_tokens} tokens / {overlap_tokens} overlap)"
    )
    if totals["failed"] or totals["empty"]:
        print(
            f"  warnings: {totals['failed']} unreadable, {totals['empty']} produced no chunks",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
