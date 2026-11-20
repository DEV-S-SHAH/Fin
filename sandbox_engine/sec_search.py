"""EDGAR full-text search client for keyword-based filing discovery.

Uses the EDGAR full-text search API (efts.sec.gov) to find filings mentioning
a keyword. Complements the ticker-based ``tier1_fetch.py`` which only resolves
known CIKs.

All requests respect the SSRF allow-list (see ``sandbox_engine.ssrf``) and a
hard per-call time budget so the caller can stay within an end-to-end SLA.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from .ssrf import DEFAULT_CONFIG, safe_urlopen, validate_url

log = logging.getLogger("graphrag.sec_search")

EDGAR_FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LLD/searchtext"


@dataclass(frozen=True)
class FilingHit:
    """One filing returned by EDGAR full-text search."""

    cik: str
    company_name: str
    form: str
    file_date: str
    accession: str
    primary_document: str
    url: str


class SecSearchError(Exception):
    """Base exception for SEC full-text search errors."""


class SecSearchTimeoutError(SecSearchError, TimeoutError):
    """Search exceeded the time budget."""


def _validate_efts_url(url: str) -> None:
    valid, error = validate_url(url, DEFAULT_CONFIG)
    if not valid:
        raise SecSearchError(f"SSRF validation failed for {url}: {error}")


def search_edgar_full_text(
    query: str,
    *,
    forms: tuple[str, ...] = ("10-K", "10-Q", "8-K"),
    start_date: str = "2020-01-01",
    end_date: Optional[str] = None,
    timeout: float = 2.0,
) -> list[FilingHit]:
    """Search EDGAR full-text for filings mentioning ``query``.

    Returns up to 10 most recent matching filings. Raises
    :class:`SecSearchTimeoutError` if the call would exceed ``timeout``.
    """
    start_time = time.monotonic()

    params: dict[str, str] = {
        "q": query,
        "forms": ",".join(forms),
        "dateRange": "custom",
        "startdt": start_date,
    }
    if end_date:
        params["enddt"] = end_date

    url = EDGAR_FULL_TEXT_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    _validate_efts_url(url)

    elapsed = time.monotonic() - start_time
    remaining = timeout - elapsed
    if remaining <= 0:
        raise SecSearchTimeoutError(
            f"Search timed out before fetching ({elapsed:.2f}s >= {timeout:.2f}s)"
        )

    socket_timeout = min(2.0, max(0.01, remaining))

    headers = {
        "User-Agent": "FinancialGraphRAG contact@domain.com",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }

    try:
        with safe_urlopen(
            url, timeout=socket_timeout, config=DEFAULT_CONFIG, headers=headers
        ) as response:
            payload = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            data = json.loads(payload.decode("utf-8"))
    except SecSearchError:
        raise
    except Exception as exc:
        raise SecSearchError(f"EDGAR full-text search failed: {exc}") from exc

    hits: list[FilingHit] = []
    for hit in data.get("hits", {}).get("hits", [])[:10]:
        source = hit.get("_source", {})
        cik = str(source.get("cik", "")).zfill(10)
        accession = str(source.get("adsh", "")).replace("-", "")
        primary_doc = str(source.get("primaryDocument", ""))
        company_name = str(source.get("companyName", ""))
        form = str(source.get("form", ""))
        file_date = str(source.get("fileDate", ""))

        if not all([cik, accession, primary_doc]):
            continue

        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary_doc}"
        )

        hits.append(
            FilingHit(
                cik=cik,
                company_name=company_name,
                form=form,
                file_date=file_date,
                accession=accession,
                primary_document=primary_doc,
                url=filing_url,
            )
        )

    log.info(
        "sec_search_complete",
        extra={"query": query, "hits": len(hits), "elapsed_ms": round((time.monotonic() - start_time) * 1000, 2)},
    )
    return hits
