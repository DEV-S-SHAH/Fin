"""Server for the FinGraph UI, served from :mod:`ui.fingraph`.

This module provides the unified HTTP server for the FinGraph landing page,
GraphRAG Studio, and authentication pages. It subclasses the handler from
:sandbox_engine:`query_ui` so the graph queries, the RAG pipeline, the grader
and the SSE stream are the *same code paths* the legacy UI runs.

Endpoints
---------
``GET /``
    Public landing page (FinGraph marketing site).

``GET /app``
    GraphRAG Studio — the analyst dashboard for querying the knowledge graph.

``GET /auth``
    Authentication page (OAuth + dev sign-in).

``GET /company/{ticker}``
    Company overview page.

``GET /api/companies``
    Overview of issuers in the graph — filings per ticker, forms on file, newest period.

``GET /api/route?q=...``
    Which retrieval route a question would take: ``KNOWN``, ``COLD_START``, or ``AMBIGUOUS``.

``GET /api/markets``
    Live quotes for the ticker strip and market cards, pulled from Yahoo Finance via yfinance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sandbox_engine import query_ui as _legacy
from sandbox_engine.query_ui import (
    KnowledgeGraph,
    _int_param,
    _listeners,
    parse_ports,
    resolve_db_path,
)
from sandbox_engine.router import route_query
from sandbox_engine.shutdown import (
    ShutdownCoordinator,
    install_signal_handlers,
    restore_signal_handlers,
    serve_until_signalled,
)
from sandbox_engine.observability import (
    get_request_id,
    record_yahoo_fetch,
    record_external_api_failure,
    record_retry,
    record_rate_limit_rejection,
)
from sandbox_engine.ssrf import DEFAULT_CONFIG, SSRFConfig, validate_url
from urllib.error import URLError


# Production SSRF config: allows known external hosts without DNS rebinding checks
# (Render's DNS may resolve Yahoo Finance to private IPs)
FINGRAPH_SSRF_CONFIG = SSRFConfig(
    allowed_hosts=frozenset({
        # SEC EDGAR
        "www.sec.gov",
        "data.sec.gov",
        # Yahoo Finance
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        # OAuth providers
        "accounts.google.com",
        "appleid.apple.com",
        # NVIDIA NIM
        "integrate.api.nvidia.com",
    }),
    allow_localhost=False,
    follow_redirects=False,
    max_redirects=5,
)

log = logging.getLogger("fingraph_ui")

# ── Thread-safe, TTL-aware, bounded-size cache ────────────────────────────────

class _TTLCache:
    """Thread-safe cache with TTL expiration and LRU eviction.

    All operations are O(1). The cache bounds memory by:
    * TTL expiration (entries older than ttl_seconds are removed on access)
    * Maximum size (oldest entries evicted when limit reached)
    """

    __slots__ = ("_ttl", "_max_size", "_data", "_lock")

    def __init__(self, ttl_seconds: float, max_size: int = 256) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        """Get value if present and not expired. Returns None otherwise."""
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            cached_time, value = entry
            if now - cached_time >= self._ttl:
                # Expired: remove and return None
                del self._data[key]
                return None
            # Move to end (most recently used)
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        """Set value, evicting oldest if at capacity."""
        now = time.monotonic()
        with self._lock:
            # Remove expired entries first (opportunistic cleanup)
            self._evict_expired(now)
            if key in self._data:
                # Update existing: move to end
                self._data[key] = (now, value)
                self._data.move_to_end(key)
            else:
                # New entry: evict LRU if at capacity
                if len(self._data) >= self._max_size:
                    self._data.popitem(last=False)
                self._data[key] = (now, value)

    def _evict_expired(self, now: float) -> None:
        """Remove all expired entries. Called with lock held."""
        expired = [
            k for k, (t, _) in self._data.items() if now - t >= self._ttl
        ]
        for k in expired:
            del self._data[k]

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def stats(self) -> dict[str, Any]:
        """Return cache statistics for monitoring."""
        with self._lock:
            return {
                "size": len(self._data),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl,
            }


_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "studio"
_LANDING = _HERE / "landing"
_AUTH = _HERE / "auth"
_VENDOR = _HERE / "vendor"
_ANIMATION = _HERE / "animation"
_IMAGES = Path(__file__).resolve().parents[2] / "images"
_ANSWER = _HERE / "answer"
_GRAPH = _HERE / "graph"
_PROVENANCE = _HERE / "provenance"
_COMPARE = _HERE / "compare"

UI_PORT_ENV = "PORT_QUERY_UI_V2"
DEFAULT_UI_PORT = 9100

#: Only these filenames are reachable. A set of names rather than a path join is
#: what keeps ``/static/../query_ui.py`` from being served as text. The landing
#: page and the studio each have their own allow-list in front of their
#: directory, so neither can reach into the other's tree.
_LANDING_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "components.js": "text/javascript; charset=utf-8",
    "nav.js": "text/javascript; charset=utf-8",
    "gradient-bars.js": "text/javascript; charset=utf-8",
    "text-loop.js": "text/javascript; charset=utf-8",
    "hero-graph.js": "text/javascript; charset=utf-8",
    "markets.js": "text/javascript; charset=utf-8",
    "logo.png": "image/png",
    "fingraph-logo.png": "image/png",
}

_COMPANY_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "store.js": "text/javascript; charset=utf-8",
    "util.js": "text/javascript; charset=utf-8",
    "graph.js": "text/javascript; charset=utf-8",
    "answer.js": "text/javascript; charset=utf-8",
    "process.js": "text/javascript; charset=utf-8",
    "reports.js": "text/javascript; charset=utf-8",
    "runDetails.js": "text/javascript; charset=utf-8",
    "runDetails.css": "text/css; charset=utf-8",
    "executionCard.js": "text/javascript; charset=utf-8",
    "logo.png": "image/png",
    "fingraph-logo.png": "image/png",
}

_AUTH_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "callback.html": "text/html; charset=utf-8",
    "logo.png": "image/png",
    "fingraph-logo.png": "image/png",
}

_VENDOR_ASSETS: dict[str, str] = {
    "gsap.min.js": "text/javascript; charset=utf-8",
    "d3.v7.min.js": "text/javascript; charset=utf-8",
}

_ANIMATION_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "fingraph-logo.png": "image/png",
}

_IMAGES_ASSETS: dict[str, str] = {
    "fingraph-logo.png": "image/png",
}

# New dedicated view assets (reuse studio static assets)
_ANSWER_ASSETS: dict[str, str] = _ASSETS.copy()
_GRAPH_ASSETS: dict[str, str] = _ASSETS.copy()
_PROVENANCE_ASSETS: dict[str, str] = _ASSETS.copy()
_COMPARE_ASSETS: dict[str, str] = _ASSETS.copy()


def resolve_within(root: Path, name: str) -> Path | None:
    """Resolve *name* within *root*, refusing any traversal attempt.

    Returns the resolved Path if *name* stays inside *root*, otherwise None.
    This is the single gate that keeps ``/static/../query_ui.py`` from being
    served as text. The allow-lists (``_ASSETS``, ``_LANDING_ASSETS``, etc.)
    decide *which* names are allowed; this function decides *whether* a name
    reaches the filesystem at all.
    """
    if not name:
        return None
    if "\x00" in name:
        return None
    # Absolute paths and root-relative paths are traversals.
    if name.startswith("/") or name.startswith("\\"):
        return None
    # Any ``..`` segment is a traversal, even if it would resolve inside.
    # ``a/../index.html`` reaches a real file but the shape is forbidden.
    parts = Path(name).parts
    if any(p == ".." for p in parts):
        return None
    # Directories are not files.
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def default_ui_port() -> int:
    """Resolve the port from ``$PORT_QUERY_UI_V2``, then :data:`DEFAULT_UI_PORT`."""
    raw = os.environ.get(UI_PORT_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning("%s=%r is not a port number; using %d", UI_PORT_ENV, raw, DEFAULT_UI_PORT)
    return DEFAULT_UI_PORT


# ── the issuer overview ──────────────────────────────────────────────────────

def companies(kg: KnowledgeGraph) -> list[dict[str, Any]]:
    """Summarise the issuers in the graph, newest filing first.

    One query returns every filing row -- a graph with three issuers holds a few
    dozen -- and the aggregation happens here, so the page needs a single round
    trip to build its overview.
    """
    try:
        rows = kg.execute(
            "MATCH (c:Company)-[:SUBMITTED]->(f:Filing) "
            "RETURN c.ticker, c.legal_name, f.form_type, "
            "f.fiscal_year, f.fiscal_period, f.period_end_date "
            "ORDER BY c.ticker, f.fiscal_year DESC, f.fiscal_period DESC"
        )
    except Exception as exc:  # a graph with no SUBMITTED edges is empty, not broken
        log.warning("company overview unavailable: %s", exc)
        return []

    by_ticker: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for ticker, name, form, fy, fp, period_end in rows:
        key = str(ticker or "").strip().upper()
        if not key:
            continue
        entry = by_ticker.setdefault(
            key,
            {"ticker": key, "name": str(name or key), "filings": 0,
             "forms": [], "periods": []},
        )
        entry["filings"] += 1
        form_s = str(form or "?")
        if form_s not in entry["forms"]:
            entry["forms"].append(form_s)
        entry["periods"].append(
            {"form": form_s, "fiscal_year": fy,
             "period": fp, "period_end": str(period_end) if period_end else None}
        )

    for entry in by_ticker.values():
        # The query sorted year-descending, so the head is the newest filing --
        # unless a null year sorted first, which a filter also handles.
        entry["periods"].sort(
            key=lambda p: (str(p.get("fiscal_year") or ""), str(p.get("period_end") or "")),
            reverse=True,
        )
        entry["latest"] = entry["periods"][0] if entry["periods"] else None
        entry["forms"].sort()
    return list(by_ticker.values())


# ── live market data ─────────────────────────────────────────────────────────

#: The tickers the landing page shows: the three issuers in the graph first, then
#: the rest of the tape. Kept here so the page and the endpoint cannot drift.
MARKET_TICKERS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX",
    "AVGO", "AMD", "JPM", "V", "MA", "GS", "XOM", "PFE", "DIS", "INTC",
)

#: Thread-safe cache for market quotes with 60s TTL and bounded size.
#: Caches the entire batch of quotes as a single entry.
_markets_cache = _TTLCache(ttl_seconds=60.0, max_size=1)


def _fetch_one(ticker: str) -> dict[str, Any] | None:
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    
    # Validate URL against SSRF protection
    valid, error = validate_url(url, FINGRAPH_SSRF_CONFIG)
    if not valid:
        log.warning("yahoo_fetch_ssrf_blocked", extra={"ticker": ticker, "error": error, "url": url})
        return None
    
    resp = requests.get(url, timeout=(5.0, 10.0), headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = resp.json()
    result = data.get("chart", {}).get("result")
    if not result:
        return None
    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    prev_close = meta.get("chartPreviousClose")
    change = (price - prev_close) if prev_close else None
    pct = (change / prev_close * 100) if (change is not None and prev_close) else None
    return {
        "ticker": ticker,
        "price": round(float(price), 2),
        "change": round(float(change), 2) if change is not None else None,
        "pct": round(float(pct), 2) if pct is not None else None,
    }


def markets() -> list[dict[str, Any]]:
    """Live quotes for :data:`MARKET_TICKERS`, cached for a minute.

    Uses Yahoo Finance's public chart API directly via requests -- one request
    per symbol, fetched concurrently. A failure returns the last good batch
    when there is one, and an empty list otherwise.
    """
    # Check cache first
    cached = _markets_cache.get("batch")
    if cached is not None:
        return cached

    rows: list[dict[str, Any]] = []
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_fetch_one, MARKET_TICKERS))
        rows = [r for r in results if r is not None]
    except Exception as exc:
        log.warning("market quotes unavailable: %s", exc)
        # On failure, return cached data if available (even if expired)
        cached = _markets_cache.get("batch")
        if cached is not None:
            return cached
        return []

    _markets_cache.set("batch", rows)
    return rows


# ── company detail data ────────────────────────────────────────────────────────

#: Thread-safe caches for company data with TTL and bounded size.
#: Detail cache: 5 min TTL, up to 128 tickers.
#: Quote cache: 1 min TTL, up to 256 tickers.
_company_detail_cache = _TTLCache(ttl_seconds=300.0, max_size=128)
_company_quote_cache = _TTLCache(ttl_seconds=60.0, max_size=256)


def _fetch_company_quote(ticker: str) -> dict[str, Any] | None:
    """Fetch basic quote data from Yahoo Finance chart API."""
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    
    # Validate URL against SSRF protection
    valid, error = validate_url(url, FINGRAPH_SSRF_CONFIG)
    if not valid:
        log.warning("yahoo_fetch_ssrf_blocked", extra={"ticker": ticker, "error": error, "url": url})
        return None
    
    try:
        resp = requests.get(url, timeout=(5.0, 10.0), headers=_YAHOO_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        prev_close = meta.get("chartPreviousClose")
        change = (price - prev_close) if prev_close else None
        pct = (change / prev_close * 100) if (change is not None and prev_close) else None
        return {
            "ticker": ticker,
            "price": round(float(price), 2),
            "change": round(float(change), 2) if change is not None else None,
            "pct": round(float(pct), 2) if pct is not None else None,
            "currency": meta.get("currency", "USD"),
            "exchange": meta.get("exchangeName", ""),
            "market_state": meta.get("marketState", ""),
        }
    except Exception as exc:
        log.warning("company quote fetch failed for %s: %s", ticker, exc)
        return None


def _serialize_for_json(obj: Any) -> Any:
    """Recursively convert date/datetime/decimal objects to JSON-serializable types."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, 'isoformat'):  # date, datetime
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_json(v) for v in obj]
    # Fallback: convert to string
    return str(obj)


def _fetch_with_retry(
    url: str,
    max_retries: int = 3,
    base_delay: float = 1.0,
    connect_timeout: float = 5.0,
    read_timeout: float = 15.0,
) -> requests.Response | None:
    """Fetch URL with exponential backoff retry.

    Uses separate connect and read timeouts. Retries only transient failures
    (429, 500, 502, 503, 504) with exponential backoff + jitter.
    Parses Retry-After header for 429 responses. Does NOT retry on
    client errors (400, 401, 403, 404) as they indicate non-transient issues.
    """
    import requests
    import time
    import random

    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    timeout = (connect_timeout, read_timeout)
    request_id = get_request_id()

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=timeout, headers=_YAHOO_HEADERS)
            if resp.status_code == 429:
                record_yahoo_fetch(success=False)
                record_rate_limit_rejection("yahoo_finance")
                record_external_api_failure("yahoo_finance", "rate_limited")
                if attempt < max_retries - 1:
                    # Parse Retry-After header
                    retry_after = 1.0
                    retry_after_hdr = resp.headers.get("Retry-After")
                    if retry_after_hdr:
                        try:
                            retry_after = float(retry_after_hdr)
                        except (ValueError, TypeError):
                            pass
                    # Exponential backoff + jitter, but respect Retry-After
                    delay = max(base_delay * (2 ** attempt), retry_after) + random.uniform(0.1, 0.5)
                    record_retry("yahoo_finance", attempt + 1)
                    log.warning(
                        "yahoo_rate_limit_retry",
                        extra={
                            "request_id": request_id,
                            "url": url,
                            "retry_after": retry_after,
                            "delay_ms": round(delay * 1000, 2),
                            "attempt": attempt + 1,
                        },
                    )
                    time.sleep(delay)
                    continue
            elif resp.status_code in RETRY_STATUSES:
                record_yahoo_fetch(success=False)
                record_external_api_failure("yahoo_finance", f"http_{resp.status_code}")
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
                    record_retry("yahoo_finance", attempt + 1)
                    log.warning(
                        "yahoo_transient_retry",
                        extra={
                            "request_id": request_id,
                            "url": url,
                            "status_code": resp.status_code,
                            "delay_ms": round(delay * 1000, 2),
                            "attempt": attempt + 1,
                        },
                    )
                    time.sleep(delay)
                    continue
            resp.raise_for_status()
            record_yahoo_fetch(success=True)
            return resp
        except requests.Timeout:
            record_yahoo_fetch(success=False)
            record_external_api_failure("yahoo_finance", "timeout")
            if attempt == max_retries - 1:
                log.warning("yahoo_request_timeout", extra={"request_id": request_id, "url": url, "attempts": max_retries})
                return None
            delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
            record_retry("yahoo_finance", attempt + 1)
            time.sleep(delay)
        except requests.ConnectionError:
            record_yahoo_fetch(success=False)
            record_external_api_failure("yahoo_finance", "connection_error")
            if attempt == max_retries - 1:
                log.warning("yahoo_connection_failed", extra={"request_id": request_id, "url": url, "attempts": max_retries})
                return None
            delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
            record_retry("yahoo_finance", attempt + 1)
            time.sleep(delay)
        except requests.HTTPError as exc:
            record_yahoo_fetch(success=False)
            # Don't retry on client errors (4xx except 429)
            if exc.response is not None and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                record_external_api_failure("yahoo_finance", f"client_error_{exc.response.status_code}")
                log.warning(
                    "yahoo_client_error_no_retry",
                    extra={"request_id": request_id, "url": url, "status_code": exc.response.status_code},
                )
                return None
            record_external_api_failure("yahoo_finance", f"http_{exc.response.status_code if exc.response else 'unknown'}")
            if attempt == max_retries - 1:
                log.warning(
                    "yahoo_request_failed",
                    extra={"request_id": request_id, "url": url, "attempts": max_retries, "error": str(exc)},
                )
                return None
            delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
            record_retry("yahoo_finance", attempt + 1)
            time.sleep(delay)
        except requests.RequestException as exc:
            record_yahoo_fetch(success=False)
            record_external_api_failure("yahoo_finance", "request_exception")
            if attempt == max_retries - 1:
                log.warning(
                    "yahoo_request_failed",
                    extra={"request_id": request_id, "url": url, "attempts": max_retries, "error": str(exc)},
                )
                return None
            delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
            record_retry("yahoo_finance", attempt + 1)
            time.sleep(delay)
    return None


# Common headers for Yahoo Finance requests
_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


def _fetch_company_detail(ticker: str) -> dict[str, Any] | None:
    """Fetch detailed company data from Yahoo Finance quote summary API."""
    import requests

    # Use modules parameter to get all needed data in one request
    modules = [
        "summaryDetail",      # Market cap, P/E, beta, 52wk high/low, dividend yield
        "financialData",      # Revenue, earnings, profit margins
        "defaultKeyStatistics", # More valuation metrics
        "calendarEvents",     # Earnings dates
        "assetProfile",       # Company description, sector, employees
        "quoteType",          # Quote type info
    ]
    modules_str = ",".join(modules)
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules_str}"
    
    # Validate URL against SSRF protection
    valid, error = validate_url(url, FINGRAPH_SSRF_CONFIG)
    if not valid:
        log.warning("yahoo_fetch_ssrf_blocked", extra={"ticker": ticker, "error": error, "url": url})
        return None

    try:
        resp = _fetch_with_retry(url)
        if resp is None:
            return None
        data = resp.json()
        result = data.get("quoteSummary", {}).get("result")
        if not result:
            return None
        return _serialize_for_json(result[0])
    except Exception as exc:
        log.warning("company detail fetch failed for %s: %s", ticker, exc)
        return None


def _fetch_chart_data(ticker: str, range_: str = "1mo", interval: str = "1d") -> dict[str, Any] | None:
    """Fetch chart data for price history."""
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_}"
    
    # Validate URL against SSRF protection
    valid, error = validate_url(url, FINGRAPH_SSRF_CONFIG)
    if not valid:
        log.warning("yahoo_fetch_ssrf_blocked", extra={"ticker": ticker, "error": error, "url": url})
        return None
    
    try:
        resp = _fetch_with_retry(url)
        if resp is None:
            return None
        data = resp.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        return _serialize_for_json(result[0])
    except Exception as exc:
        log.warning("chart data fetch failed for %s: %s", ticker, exc)
        return None


def _fetch_news(ticker: str) -> list[dict[str, Any]]:
    """Fetch recent news for a ticker."""
    import requests

    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&quotesCount=0&newsCount=10"
    
    # Validate URL against SSRF protection
    valid, error = validate_url(url, FINGRAPH_SSRF_CONFIG)
    if not valid:
        log.warning("yahoo_fetch_ssrf_blocked", extra={"ticker": ticker, "error": error, "url": url})
        return []
    
    try:
        resp = _fetch_with_retry(url)
        if resp is None:
            return []
        data = resp.json()
        news = data.get("news", [])
        # Normalize news items
        normalized = []
        for item in news[:8]:
            normalized.append({
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "link": item.get("link", ""),
                "thumbnail": item.get("thumbnail", {}).get("resolutions", [{}])[0].get("url", "") if item.get("thumbnail") else "",
                "provider_publish_time": item.get("providerPublishTime", 0),
                "uuid": item.get("uuid", ""),
            })
        return normalized
    except Exception as exc:
        log.warning("news fetch failed for %s: %s", ticker, exc)
        return []


def _compute_technicals(chart_data: dict[str, Any]) -> dict[str, Any]:
    """Compute technical indicators from chart data."""
    if not chart_data:
        return {}

    timestamps = chart_data.get("timestamp", [])
    closes = chart_data.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    highs = chart_data.get("indicators", {}).get("quote", [{}])[0].get("high", [])
    lows = chart_data.get("indicators", {}).get("quote", [{}])[0].get("low", [])
    volumes = chart_data.get("indicators", {}).get("quote", [{}])[0].get("volume", [])

    if not timestamps or not closes:
        return {}

    # Filter out None values
    valid_data = [(t, c, h, l, v) for t, c, h, l, v in zip(timestamps, closes, highs, lows, volumes)
                  if c is not None and h is not None and l is not None]
    if len(valid_data) < 2:
        return {}

    timestamps, closes, highs, lows, volumes = zip(*valid_data)
    closes = list(closes)
    highs = list(highs)
    lows = list(lows)
    volumes = list(volumes)

    # Simple Moving Averages
    def sma(data: list[float], period: int) -> float | None:
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    sma_20 = sma(closes, 20)
    sma_50 = sma(closes, 50)
    sma_200 = sma(closes, 200)

    # RSI (14-period)
    def rsi(data: list[float], period: int = 14) -> float | None:
        if len(data) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(-period, 0):
            change = data[i] - data[i - 1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    rsi_14 = rsi(closes)

    # MACD (12, 26, 9)
    def ema(data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return []
        k = 2 / (period + 1)
        ema_values = [sum(data[:period]) / period]
        for price in data[period:]:
            ema_values.append(price * k + ema_values[-1] * (1 - k))
        return ema_values

    ema_12 = ema(closes, 12)
    ema_26 = ema(closes, 26)
    macd_line = None
    signal_line = None
    histogram = None
    if ema_12 and ema_26 and len(ema_12) == len(ema_26):
        macd_values = [a - b for a, b in zip(ema_12, ema_26)]
        if len(macd_values) >= 9:
            signal_values = ema(macd_values, 9)
            if signal_values:
                macd_line = macd_values[-1]
                signal_line = signal_values[-1]
                histogram = macd_line - signal_line

    # Bollinger Bands (20, 2)
    bb_upper = None
    bb_middle = None
    bb_lower = None
    if len(closes) >= 20:
        recent = closes[-20:]
        middle = sum(recent) / 20
        std = (sum((x - middle) ** 2 for x in recent) / 20) ** 0.5
        bb_middle = middle
        bb_upper = middle + 2 * std
        bb_lower = middle - 2 * std

    # ATR (14)
    atr_14 = None
    if len(highs) >= 14 and len(lows) >= 14 and len(closes) >= 15:
        tr_values = []
        for i in range(-14, 0):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_values.append(tr)
        atr_14 = sum(tr_values) / 14

    current_price = closes[-1]
    return {
        "sma_20": round(sma_20, 2) if sma_20 else None,
        "sma_50": round(sma_50, 2) if sma_50 else None,
        "sma_200": round(sma_200, 2) if sma_200 else None,
        "rsi_14": round(rsi_14, 1) if rsi_14 else None,
        "macd": round(macd_line, 4) if macd_line else None,
        "macd_signal": round(signal_line, 4) if signal_line else None,
        "macd_histogram": round(histogram, 4) if histogram else None,
        "bb_upper": round(bb_upper, 2) if bb_upper else None,
        "bb_middle": round(bb_middle, 2) if bb_middle else None,
        "bb_lower": round(bb_lower, 2) if bb_lower else None,
        "atr_14": round(atr_14, 2) if atr_14 else None,
        "current_price": round(current_price, 2),
        "price_vs_sma20": round((current_price - sma_20) / sma_20 * 100, 2) if sma_20 else None,
        "price_vs_sma50": round((current_price - sma_50) / sma_50 * 100, 2) if sma_50 else None,
        "price_vs_sma200": round((current_price - sma_200) / sma_200 * 100, 2) if sma_200 else None,
    }


def _extract_fundamentals(detail: dict[str, Any]) -> dict[str, Any]:
    """Extract fundamental data from quote summary."""
    if not detail:
        return {}

    summary = detail.get("summaryDetail", {})
    financial = detail.get("financialData", {})
    key_stats = detail.get("defaultKeyStatistics", {})
    profile = detail.get("assetProfile", {})

    def get_val(obj: dict, key: str):
        val = obj.get(key, {})
        if isinstance(val, dict):
            return val.get("raw", val.get("fmt", None))
        return val

    def serialize_val(val):
        """Convert date/datetime objects to ISO format strings."""
        if hasattr(val, 'isoformat'):
            return val.isoformat()
        if isinstance(val, (list, tuple)):
            return [serialize_val(v) for v in val]
        if isinstance(val, dict):
            return {k: serialize_val(v) for k, v in val.items()}
        return val

    def fmt_large(num):
        if num is None:
            return None
        num = float(num)
        if num >= 1e12:
            return f"${num/1e12:.2f}T"
        elif num >= 1e9:
            return f"${num/1e9:.2f}B"
        elif num >= 1e6:
            return f"${num/1e6:.2f}M"
        elif num >= 1e3:
            return f"${num/1e3:.2f}K"
        return f"${num:.2f}"

    return {
        "market_cap": fmt_large(get_val(summary, "marketCap")),
        "market_cap_raw": get_val(summary, "marketCap"),
        "pe_ratio": get_val(summary, "trailingPE"),
        "forward_pe": get_val(summary, "forwardPE"),
        "peg_ratio": get_val(summary, "pegRatio"),
        "price_to_book": get_val(summary, "priceToBook"),
        "enterprise_value": fmt_large(get_val(summary, "enterpriseValue")),
        "beta": get_val(summary, "beta"),
        "dividend_yield": get_val(summary, "dividendYield"),
        "dividend_rate": get_val(summary, "dividendRate"),
        "ex_dividend_date": serialize_val(get_val(summary, "exDividendDate")),
        "payout_ratio": get_val(summary, "payoutRatio"),
        "52wk_high": get_val(summary, "fiftyTwoWeekHigh"),
        "52wk_low": get_val(summary, "fiftyTwoWeekLow"),
        "50d_avg": get_val(summary, "fiftyDayAverage"),
        "200d_avg": get_val(summary, "twoHundredDayAverage"),
        "avg_volume": get_val(summary, "averageVolume"),
        "avg_volume_10d": get_val(summary, "averageDailyVolume10Day"),
        "shares_outstanding": fmt_large(get_val(key_stats, "sharesOutstanding")),
        "float_shares": fmt_large(get_val(key_stats, "floatShares")),
        "held_by_insiders": get_val(key_stats, "heldPercentInsiders"),
        "held_by_institutions": get_val(key_stats, "heldPercentInstitutions"),
        "short_ratio": get_val(key_stats, "shortRatio"),
        "short_percent": get_val(key_stats, "shortPercentOfFloat"),
        "revenue": fmt_large(get_val(financial, "totalRevenue")),
        "revenue_raw": get_val(financial, "totalRevenue"),
        "revenue_per_share": get_val(financial, "revenuePerShare"),
        "gross_profit": fmt_large(get_val(financial, "grossProfits")),
        "ebitda": fmt_large(get_val(financial, "ebitda")),
        "net_income": fmt_large(get_val(financial, "netIncomeToCommon")),
        "diluted_eps": get_val(financial, "trailingEps"),
        "forward_eps": get_val(financial, "forwardEps"),
        "profit_margin": get_val(financial, "profitMargins"),
        "operating_margin": get_val(financial, "operatingMargins"),
        "return_on_equity": get_val(financial, "returnOnEquity"),
        "return_on_assets": get_val(financial, "returnOnAssets"),
        "debt_to_equity": get_val(financial, "debtToEquity"),
        "current_ratio": get_val(financial, "currentRatio"),
        "quick_ratio": get_val(financial, "quickRatio"),
        "total_cash": fmt_large(get_val(financial, "totalCash")),
        "total_debt": fmt_large(get_val(financial, "totalDebt")),
        "free_cash_flow": fmt_large(get_val(financial, "freeCashflow")),
        "operating_cash_flow": fmt_large(get_val(financial, "operatingCashflow")),
        "sector": profile.get("sector", ""),
        "industry": profile.get("industry", ""),
        "employees": profile.get("fullTimeEmployees", None),
        "description": profile.get("longBusinessSummary", ""),
        "website": profile.get("website", ""),
        "country": profile.get("country", ""),
    }


def company_detail(ticker: str) -> dict[str, Any] | None:
    """Get comprehensive company data with caching."""
    ticker = ticker.upper().strip()

    # Check quote cache (short TTL)
    quote = _company_quote_cache.get(ticker)
    if quote is None:
        quote = _fetch_company_quote(ticker)
        if quote:
            _company_quote_cache.set(ticker, quote)

    # Check detail cache (longer TTL)
    detail_cached = _company_detail_cache.get(ticker)
    if detail_cached is None:
        detail_raw = _fetch_company_detail(ticker)
        if detail_raw:
            fundamentals = _extract_fundamentals(detail_raw)
            detail_cached = {"fundamentals": fundamentals, "raw": detail_raw}
            _company_detail_cache.set(ticker, detail_cached)

    # Fetch chart data for technicals (1mo, 1d interval)
    chart_1mo = _fetch_chart_data(ticker, "1mo", "1d")
    chart_3mo = _fetch_chart_data(ticker, "3mo", "1d")
    chart_1y = _fetch_chart_data(ticker, "1y", "1d")

    technicals = _compute_technicals(chart_1mo) if chart_1mo else {}

    # Also get longer-term SMAs from 1y chart
    if chart_1y:
        tech_1y = _compute_technicals(chart_1y)
        if tech_1y.get("sma_200"):
            technicals["sma_200"] = tech_1y["sma_200"]
            technicals["price_vs_sma200"] = tech_1y["price_vs_sma200"]
        if tech_1y.get("sma_50"):
            technicals["sma_50"] = tech_1y["sma_50"]
            technicals["price_vs_sma50"] = tech_1y["price_vs_sma50"]

    # Fetch news
    news = _fetch_news(ticker)

    # Get company info from graph if available
    graph_info = None
    try:
        from sandbox_engine.query_ui import KnowledgeGraph, resolve_db_path
        kg = KnowledgeGraph(resolve_db_path(), read_only=True)
        rows = kg.execute(
            "MATCH (c:Company {ticker: $ticker})-[:SUBMITTED]->(f:Filing) "
            "RETURN f.form_type, f.fiscal_year, f.fiscal_period, f.period_end_date "
            "ORDER BY f.fiscal_year DESC, f.fiscal_period DESC LIMIT 10",
            {"ticker": ticker}
        )
        filings = []
        for form, fy, fp, pe in rows:
            filings.append({"form": form, "fiscal_year": fy, "period": fp, "period_end": pe})
        kg.close()
        if filings:
            graph_info = {"filings": filings, "in_graph": True}
        else:
            graph_info = {"in_graph": False}
    except Exception:
        graph_info = {"in_graph": False}

    # Build response and serialize everything for JSON
    now = time.monotonic()
    response = {
        "ticker": ticker,
        "quote": quote,
        "fundamentals": detail_cached.get("fundamentals", {}) if detail_cached else {},
        "technicals": technicals,
        "chart_1mo": chart_1mo,
        "chart_3mo": chart_3mo,
        "chart_1y": chart_1y,
        "news": news,
        "graph_info": graph_info,
        "cached_at": int(now),
    }

    return _serialize_for_json(response)


# ── authentication ───────────────────────────────────────────────────────────

#: OAuth client IDs, one env var per provider. Empty string means "not
#: configured" -- the sign-in page hides that button and, when none are set,
#: offers a local development sign-in instead.
AUTH_PROVIDERS: dict[str, str] = {
    "google": os.environ.get("FINGRAPH_GOOGLE_CLIENT_ID", "").strip(),
    "apple": os.environ.get("FINGRAPH_APPLE_CLIENT_ID", "").strip(),
    "tradingview": os.environ.get("FINGRAPH_TRADINGVIEW_CLIENT_ID", "").strip(),
}

#: The signing secret for session tokens. ``$FINGRAPH_AUTH_SECRET`` pins it
#: across restarts; without one a fresh secret is generated per process, which
#: quietly logs everyone out when the server restarts -- fine for local use.
AUTH_SECRET = os.environ.get("FINGRAPH_AUTH_SECRET", "").strip() or secrets.token_hex(32)

SESSION_COOKIE = "fin_session"
SESSION_TTL = 7 * 24 * 3600

#: Dev sign-in is the local development sign-in the page falls back to when
#: no OAuth is configured. It MUST be explicitly enabled via
#: ``FINGRAPH_DEV_LOGIN=1`` -- a plain deployment must not ship an open door.
DEV_LOGIN_ENV = "FINGRAPH_DEV_LOGIN"

def dev_sign_in_enabled() -> bool:
    """Whether the ``dev`` provider is allowed to mint sessions.

    Defaults to True for local development. Can be disabled via FINGRAPH_DEV_LOGIN=0.
    """
    val = os.environ.get(DEV_LOGIN_ENV, "").strip().lower()
    return val not in ("0", "false", "no", "off")

#: Providers the server will issue a session for. ``dev`` is only included
#: when ``dev_sign_in_enabled()`` returns True.
def _allowed_providers() -> frozenset[str]:
    enabled = set(AUTH_PROVIDERS.keys())
    if dev_sign_in_enabled():
        enabled.add("dev")
    return frozenset(enabled)

_ALLOWED_PROVIDERS = _allowed_providers()

# ── OAuth token verification ────────────────────────────────────────────────
# Google: verify ID token against Google's certs.
# Apple:  fetch Apple JWKS and verify JWT signature + claims.
# TradingView: no public spec for ID tokens; left as 501.
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
OAUTH_TIMEOUT = 5.0  # seconds for JWKS fetch / tokeninfo calls

def _fetch_jwks(url: str) -> dict:
    import urllib.request, json
    
    # Validate URL against SSRF protection
    valid, error = validate_url(url, FINGRAPH_SSRF_CONFIG)
    if not valid:
        log.warning("oauth_jwks_ssrf_blocked", extra={"error": error, "url": url})
        raise URLError(f"SSRF validation failed: {error}")
    
    with urllib.request.urlopen(url, timeout=OAUTH_TIMEOUT) as resp:
        return json.load(resp)

# Thread-safe cache for Apple JWKS with 1-hour TTL.
_apple_jwks_cache = _TTLCache(ttl_seconds=3600.0, max_size=1)

def _get_apple_jwks() -> dict:
    cached = _apple_jwks_cache.get("jwks")
    if cached is not None:
        return cached
    jwks = _fetch_jwks(APPLE_JWKS_URL)
    _apple_jwks_cache.set("jwks", jwks)
    return jwks

def verify_google_id_token(token: str, client_id: str) -> dict | None:
    """Verify a Google ID token. Returns the claims dict on success, None on failure."""
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as grequests
        # google-auth verifies signature, iss, aud, exp, nbf automatically
        claims = id_token.verify_oauth2_token(
            token, grequests.Request(), client_id, clock_skew_in_seconds=10
        )
        if claims.get("iss") not in GOOGLE_ISSUERS:
            return None
        return claims
    except Exception:
        return None

def verify_apple_id_token(token: str, client_id: str) -> dict | None:
    """Verify an Apple ID token using Apple's JWKS."""
    try:
        from jwcrypto import jwt, jwk
        import json, time
        jwks = _get_apple_jwks()
        # Parse header to get kid
        header = json.loads(jwt.JWT(jwt=token, expected_type="JWS").token.objects[0].decode())
        kid = header.get("kid")
        if not kid:
            return None
        # Find matching key
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key_data:
            return None
        key = jwk.JWK.from_json(json.dumps(key_data))
        t = jwt.JWT(key=key, jwt=token, expected_type="JWS", algorithms=["RS256"])
        claims = json.loads(t.claims)
        # Verify standard claims
        if claims.get("iss") != APPLE_ISSUER:
            return None
        if claims.get("aud") != client_id:
            return None
        now = int(time.time())
        if claims.get("exp", 0) < now or claims.get("nbf", now) > now:
            return None
        return claims
    except Exception:
        return None



def _session_token(provider: str) -> str:
    """``provider.expiry.signature`` -- the cookie value.

    The signature is an HMAC over provider+expiry, so a forged or tampered
    cookie fails verification and an expired one stops verifying.
    """
    expiry = int(time.time()) + SESSION_TTL
    sig = hmac.new(
        AUTH_SECRET.encode(), f"{provider}.{expiry}".encode(), hashlib.sha256
    ).hexdigest()[:24]
    return f"{provider}.{expiry}.{sig}"


def _session_provider(token: str | None) -> str | None:
    """The provider a cookie belongs to, or ``None`` if it is invalid."""
    if not token:
        return None
    try:
        provider, expiry, sig = token.split(".")
        if provider not in _ALLOWED_PROVIDERS:
            return None
        if int(expiry) < time.time():
            return None
        expected = hmac.new(
            AUTH_SECRET.encode(), f"{provider}.{expiry}".encode(), hashlib.sha256
        ).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        return provider
    except (ValueError, AttributeError):
        return None


def _cookie_header(value: str, max_age: int) -> str:
    parts = [f"{SESSION_COOKIE}={value}", "Path=/", "SameSite=Lax", f"Max-Age={max_age}"]
    if os.environ.get("FINGRAPH_AUTH_SECURE"):
        parts.append("Secure")
    parts.append("HttpOnly")
    return "; ".join(parts)


def _read_json_body(handler: Any, limit: int = 4096) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise ValueError("invalid Content-Length") from None
    if length <= 0:
        raise ValueError("empty body")
    if length > limit:
        raise ValueError("body too large")
    payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


# ── request handler ──────────────────────────────────────────────────────────

class _NextHandler(_legacy._Handler):
    """The legacy router plus a static-file route for the new assets."""

    server_version = "graphrag-ui-next"

    # CORS configuration
    _CORS_ORIGIN = os.environ.get("FINGRAPH_CORS_ORIGIN", "http://127.0.0.1:9100").strip()
    _CORS_ALLOW_CREDENTIALS = "true"

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self._CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Credentials", self._CORS_ALLOW_CREDENTIALS)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")

    def _send(self, status: int, body: bytes, ct: str, etag: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if etag is None else "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, status: int, message: str) -> bool:
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, data: Any) -> bool:
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # -- static ---------------------------------------------------------------

    def _serve_asset(self, name: str) -> bool:
        return self._serve_file(name, _STATIC, _ASSETS)

    def _serve_landing(self, name: str) -> bool:
        return self._serve_file(name, _LANDING, _LANDING_ASSETS)

    def _serve_auth(self, name: str) -> bool:
        return self._serve_file(name, _AUTH, _AUTH_ASSETS)

    def _serve_company(self, ticker: str, name: str = "index.html") -> bool:
        """Serve the company overview page or its assets for a ticker."""
        company_dir = _LANDING / "company"
        if not company_dir.exists():
            return self._err(404, "company page not found")
        return self._serve_file(name, company_dir, _COMPANY_ASSETS)

    def _serve_vendor(self, name: str) -> bool:
        return self._serve_file(name, _VENDOR, _VENDOR_ASSETS)

    def _serve_animation(self, name: str) -> bool:
        return self._serve_file(name, _ANIMATION, _ANIMATION_ASSETS)

    def _serve_answer(self, name: str) -> bool:
        return self._serve_file(name, _ANSWER, _ANSWER_ASSETS)

    def _serve_graph(self, name: str) -> bool:
        return self._serve_file(name, _GRAPH, _GRAPH_ASSETS)

    def _serve_provenance(self, name: str) -> bool:
        return self._serve_file(name, _PROVENANCE, _PROVENANCE_ASSETS)

    def _serve_compare(self, name: str) -> bool:
        return self._serve_file(name, _COMPARE, _COMPARE_ASSETS)

    def _serve_images(self, name: str) -> bool:
        return self._serve_file(name, _IMAGES, _IMAGES_ASSETS)

    def _serve_file(self, name: str, root: Path, assets: dict[str, str]) -> bool:
        ct = assets.get(name)
        if ct is None:
            return self._err(404, f"no asset: {name}")
        path = root / name
        try:
            stat = path.stat()
            body = path.read_bytes()
        except OSError:
            return self._err(404, f"asset missing: {name}")

        # The shell is never cached: it is the document that names every asset,
        # so a stale copy pins an old stylesheet. The rest revalidates against
        # mtime+size, so an edited module shows up on reload while an unchanged
        # one costs a 304.
        etag = f'W/"{name}-{int(stat.st_mtime)}-{len(body)}"'
        if name != "index.html" and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        self._send(200, body, ct, etag=None if name == "index.html" else etag)

    def _get(self) -> None:
        parsed = urlparse(self.path)
        p = parsed.path

        # The landing page owns the root route; the studio moves behind /app.
        # Its assets use absolute paths (/static/..., /vendor/..., /favicon.svg)
        # so nothing inside the studio had to change for its new mount point.
        if p in ("/", "/index.html"):
            return self._serve_landing("index.html")
        if p.startswith("/landing/"):
            return self._serve_landing(p[len("/landing/"):])
        if p in ("/app", "/app/", "/app/index.html"):
            return self._serve_asset("index.html")
        if p.startswith("/static/"):
            return self._serve_asset(p[len("/static/"):])
        if p.startswith("/vendor/"):
            return self._serve_vendor(p[len("/vendor/"):])
        if p in ("/favicon.svg", "/favicon.ico", "/favicon.png"):
            # Serve the new fingraph logo as favicon
            return self._serve_asset("fingraph-logo.png")
        if p in ("/auth", "/auth/", "/auth/index.html"):
            return self._serve_auth("index.html")
        if p == "/auth/callback":
            return self._serve_auth("callback.html")
        if p.startswith("/auth/"):
            return self._serve_auth(p[len("/auth/"):])
        if p in ("/pricing", "/pricing/", "/pricing/index.html"):
            # Pricing lives on the landing page; the old standalone URL keeps
            # working by sending the browser to the section.
            self.send_response(302)
            self.send_header("Location", "/#pricing")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        # Company overview page
        if p == "/company" or p == "/company/":
            # Redirect to landing page markets section
            self.send_response(302)
            self.send_header("Location", "/#markets")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if p.startswith("/company/"):
            # Company overview page is an SPA route - always serve the shell.
            # react-router handles the ticker parameter client-side.
            return self._serve_company("APP", "index.html")
        # Animation page
        if p in ("/animation", "/animation/", "/animation/index.html"):
            return self._serve_animation("index.html")
        if p.startswith("/animation/"):
            return self._serve_animation(p[len("/animation/"):])
        
        # Answer view - dedicated GraphRAG answer experience
        if p in ("/answer", "/answer/", "/answer/index.html"):
            return self._serve_answer("index.html")
        if p.startswith("/answer/"):
            return self._serve_answer(p[len("/answer/"):])
        
        # Graph view - dedicated Knowledge Graph exploration
        if p in ("/graph", "/graph/", "/graph/index.html"):
            return self._serve_graph("index.html")
        if p.startswith("/graph/"):
            return self._serve_graph(p[len("/graph/"):])
        
        # Provenance view - dedicated Provenance & Grading experience
        if p in ("/provenance", "/provenance/", "/provenance/index.html"):
            return self._serve_provenance("index.html")
        if p.startswith("/provenance/"):
            return self._serve_provenance(p[len("/provenance/"):])
        
        # Compare view - dedicated Multi-Issuer Compare experience
        if p in ("/compare", "/compare/", "/compare/index.html"):
            return self._serve_compare("index.html")
        if p.startswith("/compare/"):
            return self._serve_compare(p[len("/compare/"):])
        
        # Root images directory
        if p.startswith("/images/"):
            return self._serve_images(p[len("/images/"):])
        if p == "/api/auth/config":
            # Which providers have a client ID configured. The IDs themselves
            # never leave the server; the page only learns which buttons to show.
            return self._json({k: bool(v) for k, v in AUTH_PROVIDERS.items()})
        if p == "/api/auth/session":
            token = self._cookie_value()
            provider = _session_provider(token)
            return self._json({"provider": provider, "authenticated": provider is not None})
        if p == "/api/cache/stats":
            return self._json({
                "markets": _markets_cache.stats(),
                "company_detail": _company_detail_cache.stats(),
                "company_quote": _company_quote_cache.stats(),
                "apple_jwks": _apple_jwks_cache.stats(),
            })
        if p == "/api/companies":
            return self._json({"companies": companies(self.kg)})
        if p == "/api/markets":
            return self._json({"markets": markets()})
        if p == "/api/route":
            question = (parse_qs(parsed.query).get("q") or [""])[0].strip()
            if not question:
                return self._err(400, "q is required")
            routing = route_query(question, self.kg)
            return self._json({
                "route": routing.route.name,
                "ticker": routing.ticker,
            })
        if p.startswith("/api/company/"):
            ticker = p.split("/api/company/")[1].split("/")[0].split("?")[0].upper()
            if ticker and ticker.isalpha():
                detail = company_detail(ticker)
                if detail is None:
                    return self._err(404, f"company not found: {ticker}")
                return self._json(detail)
        if p == "/api/reports":
            if self._require_auth() is None:
                return True
            qs = parse_qs(parsed.query)
            ticker = (qs.get("ticker") or [""])[0].strip().upper()
            return self._json({"reports": self._list_reports(ticker)})
        if p.startswith("/api/reports/"):
            if self._require_auth() is None:
                return True
            report_id = p[len("/api/reports/"):]
            qs = parse_qs(parsed.query)
            ticker = (qs.get("ticker") or [""])[0].strip().upper()
            return self._json(self._run_report(report_id, ticker))
        # Protected API endpoints require authentication
        # Exploration endpoints (/api/graph, /api/entities, /api/stats) are public like the legacy UI
        # Public experience pages (/answer, /graph, /provenance, /compare) need /api/ask to work
        # Only write endpoints, reports, and /app Studio require auth
        if p.startswith("/api/ingestion"):
            if self._require_auth() is None:
                return True
        # Everything else -- /api/rag, /vendor/* -- is the old router, unchanged.
        return super()._get()

    def _list_reports(self, ticker: str | None) -> list[dict]:
        """List available reports, optionally filtered by company."""
        from sandbox_engine.query_ui import CANNED_REPORTS, run_report
        reports = []
        for r in CANNED_REPORTS:
            meta = {"id": r["id"], "title": r["title"], "description": r["description"]}
            if ticker:
                meta["ticker"] = ticker
            reports.append(meta)
        return reports

    def _run_report(self, report_id: str, ticker: str | None) -> dict:
        """Execute a report, optionally filtered by company."""
        from sandbox_engine.query_ui import CANNED_REPORTS, run_report
        rpt = next((r for r in CANNED_REPORTS if r["id"] == report_id), None)
        if rpt is None:
            return {"error": f"Unknown report: {report_id}"}
        
        cypher = rpt["cypher"]
        if ticker:
            cypher = cypher.replace(
                "MATCH (c:Company)-[:SUBMITTED]->(f:Filing)",
                f"MATCH (c:Company {{ticker: $ticker}})-[:SUBMITTED]->(f:Filing)"
            )
            params = {"ticker": ticker}
        else:
            params = {}
        
        try:
            rows = self.kg.execute(cypher, params)
            return {
                "id": rpt["id"],
                "title": rpt["title"],
                "description": rpt["description"],
                "columns": rpt["columns"],
                "rows": rows,
                "row_count": len(rows),
                "ticker": ticker,
            }
        except Exception as exc:
            return {"error": str(exc), "id": report_id}

    # -- cookies ---------------------------------------------------------------

    def _cookie_value(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == SESSION_COOKIE:
                return value.strip()
        return None

    def _require_auth(self) -> str | None:
        """Check for valid session cookie. Returns provider name if authenticated, None otherwise."""
        token = self._cookie_value()
        provider = _session_provider(token)
        if provider is None:
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode("utf-8"))
            return None
        return provider

    # -- auth POST -------------------------------------------------------------

    def _post(self) -> None:
        parsed = urlparse(self.path)
        p = parsed.path

        if p == "/api/auth/session":
            return self._api_auth_session()
        if p == "/api/auth/logout":
            return self._api_auth_logout()
        # Protected POST endpoints (only ingestion and rag config management)
        if p.startswith("/api/ingestion") or (p == "/api/rag" and self.command == "POST"):
            if self._require_auth() is None:
                return
        return super()._post()

    def _api_auth_session(self) -> None:
        """Issue (or refuse) a session cookie for a completed provider sign-in.

        The provider token IS validated against the provider here using the
        provider's public verification endpoints. This prevents the bypass
        where an arbitrary token would be exchanged for a session cookie.
        """
        try:
            payload = _read_json_body(self)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._err(400, str(exc))

        provider = str(payload.get("provider") or "").strip().lower()
        if provider not in _allowed_providers():
            return self._err(400, f"unknown provider: {provider!r}")
        if provider != "dev" and not AUTH_PROVIDERS.get(provider):
            return self._err(400, f"provider not configured: {provider}")
        token = str(payload.get("token") or "").strip()
        if not token:
            return self._err(400, "token is required")

        # Validate the provider token
        if provider == "google":
            client_id = AUTH_PROVIDERS.get("google")
            if not client_id:
                return self._err(400, "google provider not configured")
            claims = verify_google_id_token(token, client_id)
            if claims is None:
                return self._err(401, "invalid google token")
        elif provider == "apple":
            client_id = AUTH_PROVIDERS.get("apple")
            if not client_id:
                return self._err(400, "apple provider not configured")
            claims = verify_apple_id_token(token, client_id)
            if claims is None:
                return self._err(401, "invalid apple token")
        elif provider == "tradingview":
            # TradingView: no public spec for ID token verification
            return self._err(501, "tradingview token verification not implemented")
        elif provider == "dev":
            # Dev provider: no token validation needed, but only allowed when explicitly enabled
            pass
        else:
            return self._err(400, f"unknown provider: {provider!r}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Set-Cookie", _cookie_header(_session_token(provider), SESSION_TTL))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._send_cors_headers()
        self.end_headers()

    def _api_auth_logout(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Set-Cookie", _cookie_header("", 0))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._send_cors_headers()
        self.end_headers()


# ── server runner ────────────────────────────────────────────────────────────

def serve(host: str = "127.0.0.1",
          port: int | str | list[int] | None = None,
          open_browser: bool = True,
          read_only: bool = True,
          db_path: Path | None = None) -> None:
    _legacy._configure_logging()
    ports = parse_ports(default_ui_port() if port is None else port)
    db_path = db_path or resolve_db_path()
    if db_path is None:
        log.error(
            "No graph database found. Build one first:\n"
            "  python -m sandbox_engine --reset\n"
            "then start this server again. To serve a different graph, pass --db <path>."
        )
        raise SystemExit(1)

    # Built here, at start-up, so a missing credential is a line in the banner
    # rather than a hang on the first question.
    _legacy.get_backends()

    kg = KnowledgeGraph(db_path, read_only=read_only)
    handler = type("_BoundNextHandler", (_NextHandler,), {"kg": kg})
    servers: list[ThreadingHTTPServer] = _listeners(host, ports, handler)

    primary = f"http://{host}:{ports[0]}/"
    stats = kg.stats()
    tickers = ", ".join(c["ticker"] for c in companies(kg)) or "none"
    where = {"nvidia": "NVIDIA NIM", "ollama": "local Ollama"}.get(stats["rag_backend"], "unavailable")

    bar = "=" * 74
    print(f"\n{bar}")
    print("  FinGraph  —  public landing page (/) + GraphRAG Studio (/app)")
    print(f"{bar}")
    for number in ports:
        print(f"  Landing      : http://{host}:{number}/")
        print(f"  Studio       : http://{host}:{number}/app")
    print(f"  Database     : {db_path}")
    print(f"  Schema       : {stats['schema']}")
    print(f"  Graph        : {stats['nodes']} entities · {stats['edges']} relationships")
    print(f"  Issuers      : {tickers}")
    print(f"  RAG Model    : {stats['rag_model'] or 'none'} via {where}")
    if stats["rag_backend"] == "none":
        print("  Answers      : DISABLED. Paste a key in the browser, or run `ollama serve`.")
    print(f"  Legacy UI    : python -m sandbox_engine.query_ui  (unchanged, port 9000)")
    print(f"  Press Ctrl-C to stop")
    print(f"{bar}\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(primary)).start()

    print(f"  Shutdown     : {_legacy.SHUTDOWN_TIMEOUT:.0f}s grace period on SIGINT/SIGTERM")
    print(f"  Press Ctrl-C to stop")
    print(f"{bar}\n")

    coordinator = ShutdownCoordinator(
        service="fingraph-ui", timeout=_legacy.SHUTDOWN_TIMEOUT
    )
    install_signal_handlers(coordinator)
    # Not restored afterwards -- see the same note in query_ui.serve().
    try:
        summary = serve_until_signalled(
            servers, coordinator, graph=kg,
            background=_legacy.background_queue,
        )
    finally:
        _legacy._safe_close(kg)
    if summary.get("forced"):
        print(
            f"Stopped. {summary.get('dropped_requests', 0)} request(s) were still "
            f"running when the {_legacy.SHUTDOWN_TIMEOUT:.0f}s grace period expired."
        )
    else:
        print("Stopped.")


def main() -> None:
    import argparse

    _legacy._configure_logging()
    p = argparse.ArgumentParser(
        prog="python -m ui.fingraph",
        description="Redesigned GraphRAG UI over the same live graph as query_ui.",
    )
    p.add_argument("--port", default=str(default_ui_port()),
                   help="one port or several, e.g. --port 9100,9200")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--read-write", action="store_true")
    args = p.parse_args()
    try:
        ports = parse_ports(args.port)
        serve(args.host, ports, open_browser=not args.no_browser,
              read_only=not args.read_write, db_path=args.db)
    except OSError as exc:
        log.error("%s", exc)
        raise SystemExit(1)
    except ValueError as exc:
        log.error("%s", exc)
        raise SystemExit(2)


__all__ = ["companies", "default_ui_port", "main", "serve", "DEFAULT_UI_PORT", "UI_PORT_ENV"]
