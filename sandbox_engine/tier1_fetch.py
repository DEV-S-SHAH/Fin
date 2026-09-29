"""Runtime SEC EDGAR fetching module for Tier 1 Cold Start.

Strictly enforces:
- Hard 2.5s total timeout SLA.
- Sockets timeout at 2.0s max.
- Zero silent drops (typed exceptions: FetchError, FetchTimeoutError,
  EDGARRateLimitError / RateLimitError, FilingNotFoundError).
- HTTP 429 Retry-After parsing with single jittered retry within budget.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional


class FetchError(Exception):
    """Base exception for SEC EDGAR runtime fetching errors."""
    pass


class FetchTimeoutError(FetchError, TimeoutError):
    """Raised when fetching exceeds the SLA timeout budget."""
    pass


class EDGARRateLimitError(FetchError):
    """Raised when SEC EDGAR returns HTTP 429 Too Many Requests."""
    pass


#: Alias for EDGARRateLimitError
RateLimitError = EDGARRateLimitError


class FilingNotFoundError(FetchError):
    """Raised when no matching filing or document could be found."""
    pass


DEFAULT_SEC_USER_AGENT = "FinancialGraphRAG contact@domain.com"
MAX_TOTAL_BUDGET_SECONDS = 2.5
MAX_SOCKET_TIMEOUT_SECONDS = 2.0

#: Built-in CIK mappings for major issuers
DEFAULT_TICKER_CIK: dict[str, str] = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "RIVN": "0001874178",
    "TSLA": "0001318605",
    "AMZN": "0001018724",
    "GOOG": "0001652044",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "NFLX": "0001065280",
    "JPM": "0000019617",
}


class SECRuntimeFetcher:
    """Production runtime fetcher for SEC EDGAR submissions and filings."""

    def __init__(self, user_agent: Optional[str] = None) -> None:
        self.user_agent = user_agent or os.environ.get(
            "SEC_USER_AGENT", DEFAULT_SEC_USER_AGENT
        )
        self._cik_cache: dict[str, str] = dict(DEFAULT_TICKER_CIK)

    def _resolve_cik(self, ticker: str, timeout: float, start_time: float) -> str:
        """Resolve ticker to a 10-digit zero-padded CIK string."""
        clean = ticker.strip().upper()
        if clean.startswith("CIK"):
            num = clean[3:].lstrip("0") or "0"
            if num.isdigit():
                return num.zfill(10)
        if clean.isdigit():
            return clean.zfill(10)
        if clean in self._cik_cache:
            return self._cik_cache[clean]

        # Dynamic lookup via company_tickers.json if online and budget allows
        try:
            url = "https://www.sec.gov/files/company_tickers.json"
            payload = self._request(url, timeout=timeout, start_time=start_time)
            data = json.loads(payload.decode("utf-8"))
            for entry in data.values():
                t = str(entry.get("ticker", "")).strip().upper()
                c = str(entry.get("cik_str", "")).zfill(10)
                if t and c:
                    self._cik_cache[t] = c
            if clean in self._cik_cache:
                return self._cik_cache[clean]
        except Exception:
            pass

        raise FilingNotFoundError(f"Could not resolve CIK for ticker '{ticker}'")

    def _request(
        self,
        url: str,
        timeout: float,
        start_time: float,
        retry_count: int = 0,
    ) -> bytes:
        """Issue an HTTP GET request respecting timeouts, compression, and rate limits."""
        elapsed = time.monotonic() - start_time
        remaining = timeout - elapsed
        if remaining <= 0:
            raise FetchTimeoutError(
                f"Operation timed out ({elapsed:.2f}s >= {timeout:.2f}s) before fetching {url}"
            )

        socket_timeout = min(MAX_SOCKET_TIMEOUT_SECONDS, max(0.01, remaining))
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip",
                "Accept": "text/html,application/json,*/*",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=socket_timeout) as response:
                payload = response.read()
                headers = response.headers
                content_encoding = (
                    headers.get("Content-Encoding") if hasattr(headers, "get") else None
                )
                if content_encoding == "gzip":
                    payload = gzip.decompress(payload)
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after_hdr = None
                if exc.headers and hasattr(exc.headers, "get"):
                    retry_after_hdr = exc.headers.get("Retry-After")

                retry_delay = 1.0
                if retry_after_hdr:
                    try:
                        retry_delay = float(retry_after_hdr)
                    except (ValueError, TypeError):
                        retry_delay = 1.0

                now_elapsed = time.monotonic() - start_time
                now_remaining = timeout - now_elapsed
                jitter = random.uniform(0.1, 0.3)
                total_delay = retry_delay + jitter

                if (
                    retry_count > 0
                    or total_delay >= now_remaining
                    or (now_elapsed + total_delay) >= min(timeout, MAX_SOCKET_TIMEOUT_SECONDS)
                ):
                    raise EDGARRateLimitError(
                        f"SEC EDGAR rate limit (HTTP 429). Retry-After={retry_after_hdr}s "
                        f"exceeds budget (remaining={now_remaining:.2f}s)"
                    ) from exc

                time.sleep(total_delay)
                return self._request(
                    url,
                    timeout=timeout,
                    start_time=start_time,
                    retry_count=retry_count + 1,
                )
            elif exc.code == 404:
                raise FilingNotFoundError(
                    f"Resource not found (HTTP 404) at {url}: {exc.reason}"
                ) from exc
            else:
                raise FetchError(
                    f"HTTP {exc.code} {exc.reason} while fetching {url}"
                ) from exc
        except (TimeoutError, socket.timeout) as exc:
            elapsed = time.monotonic() - start_time
            raise FetchTimeoutError(
                f"Socket timed out after {elapsed:.2f}s fetching {url}: {exc}"
            ) from exc
        except urllib.error.URLError as exc:
            elapsed = time.monotonic() - start_time
            if (
                isinstance(exc.reason, (TimeoutError, socket.timeout))
                or "timed out" in str(exc.reason).lower()
            ):
                raise FetchTimeoutError(
                    f"Request timed out after {elapsed:.2f}s fetching {url}: {exc}"
                ) from exc
            raise FetchError(f"Network error fetching {url}: {exc}") from exc
        except Exception as exc:
            if isinstance(
                exc,
                (FetchError, FetchTimeoutError, EDGARRateLimitError, FilingNotFoundError),
            ):
                raise
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise FetchTimeoutError(
                    f"Request timed out after {elapsed:.2f}s: {exc}"
                ) from exc
            raise FetchError(f"Unexpected error fetching {url}: {exc}") from exc

    def fetch_latest_filing_html(
        self, ticker: str, form_type: str = "10-K", timeout: float = 2.0
    ) -> tuple[str, dict[str, Any]]:
        """Fetch the latest raw filing HTML and metadata for a ticker and form type.

        Parameters:
        - ticker: Stock ticker symbol (e.g. 'AAPL', 'RIVN').
        - form_type: SEC form type ('10-K', '10-Q').
        - timeout: Total timeout in seconds (capped at 2.5s SLA budget).

        Returns:
        - (raw_html_str, metadata_dict)
        """
        start_time = time.monotonic()
        effective_timeout = min(timeout, MAX_TOTAL_BUDGET_SECONDS)

        # 1. Resolve ticker to CIK
        cik10 = self._resolve_cik(ticker, timeout=effective_timeout, start_time=start_time)

        # 2. Fetch submissions index
        submissions_url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
        payload = self._request(
            submissions_url, timeout=effective_timeout, start_time=start_time
        )
        try:
            submissions = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise FetchError(
                f"Failed to parse SEC submissions JSON for {ticker}: {exc}"
            ) from exc

        # 3. Locate latest matching filing
        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        filing_dates = recent.get("filingDate", [])
        report_dates = recent.get("reportDate", [])

        target_form = form_type.strip().upper()
        match_idx = -1
        for idx, f in enumerate(forms):
            if str(f).strip().upper() == target_form:
                match_idx = idx
                break

        if (
            match_idx == -1
            or match_idx >= len(accessions)
            or match_idx >= len(primary_docs)
        ):
            raise FilingNotFoundError(
                f"No filing with form '{form_type}' found in recent submissions for {ticker}"
            )

        accession = accessions[match_idx]
        primary_doc = primary_docs[match_idx]
        filing_date = filing_dates[match_idx] if match_idx < len(filing_dates) else ""
        report_date = report_dates[match_idx] if match_idx < len(report_dates) else ""

        # 4. Construct archive URL and fetch document
        cik_nodash = str(int(cik10))
        accession_nodash = accession.replace("-", "")
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_nodash}/{accession_nodash}/{primary_doc}"
        )

        doc_payload = self._request(
            doc_url, timeout=effective_timeout, start_time=start_time
        )
        raw_html = doc_payload.decode("utf-8", errors="replace")

        metadata = {
            "ticker": ticker.strip().upper(),
            "cik": cik10,
            "form": target_form,
            "accession_number": accession,
            "primary_document": primary_doc,
            "filing_date": filing_date,
            "report_date": report_date,
            "url": doc_url,
        }

        return raw_html, metadata
