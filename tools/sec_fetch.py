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
DEFAULT_FORMS = ("10-K", "10-Q", "8-K")

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

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")

    @property
    def archive_url(self) -> str:
        return (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{self.cik}/{self.accession_nodash}/{self.primary_document}"
        )


@dataclasses.dataclass
class DownloadStats:
    cached: int = 0
    downloaded: int = 0
    failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def _request(url: str, user_agent: str, delay: float) -> bytes:
    """GET *url*, honouring SEC etiquette and retrying transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
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
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                time.sleep(2**attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(2**attempt)
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

    wanted = {form.upper() for form in forms}
    selected: list[Filing] = []
    for row in rows:
        if row.get("form", "").upper() not in wanted:
            continue
        filing_date = row.get("filingDate", "")
        if not (start <= filing_date <= end):
            continue
        selected.append(
            Filing(
                form=row["form"].upper(),
                filing_date=filing_date,
                accession=row["accessionNumber"],
                primary_document=row["primaryDocument"],
                cik=str(int(cik_padded)),
                report_date=row.get("reportDate", ""),
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
        target = dest / f"{filing.form.replace('/', '-')}_{filing.filing_date}_{filing.primary_document}"
        if target.exists() and target.stat().st_size > 0 and not refresh:
            stats.cached += 1
            continue
        try:
            payload = _request(filing.archive_url, user_agent, delay)
        except Exception as exc:  # keep going; report at the end
            stats.failed.append((target.name, f"{type(exc).__name__}: {exc}"))
            continue
        target.write_bytes(payload)
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
        "--forms", nargs="+", default=list(DEFAULT_FORMS), help="filing forms to select"
    )
    parser.add_argument("--start", default="2020-01-01", help="earliest filing date")
    parser.add_argument("--end", default="2025-12-31", help="latest filing date")
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    chunk_tokens = args.chunk_tokens
    overlap_tokens = args.overlap_tokens
    if chunk_tokens is None or overlap_tokens is None:
        from graphrag.config import GraphRAGConfig

        config = GraphRAGConfig()
        chunk_tokens = chunk_tokens or config.chunk_tokens
        overlap_tokens = overlap_tokens or config.chunk_overlap_tokens

    if not args.skip_download:
        filings = build_manifest(
            args.cik, args.forms, args.start, args.end, args.user_agent, args.delay
        )
        by_form: dict[str, int] = {}
        for filing in filings:
            by_form[filing.form] = by_form.get(filing.form, 0) + 1
        print(f"manifest: {len(filings)} filings {by_form}")
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
