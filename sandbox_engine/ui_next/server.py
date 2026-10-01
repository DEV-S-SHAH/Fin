"""Server for the redesigned UI, served from :mod:`sandbox_engine.ui_next`.

The old three-pane viewer lives in :mod:`sandbox_engine.query_ui` and is left
exactly as it is. This module does not reimplement any of its behaviour: it
subclasses the handler, so the graph queries, the RAG pipeline, the grader and
the SSE stream are the *same code paths* the old page already runs. What is new
is everything in front of them -- the markup, the stylesheet and the ES modules
under ``static/`` -- plus two small read-only endpoints described below.

Why a subclass rather than a second handler
-------------------------------------------

Duplicating the request router would let the two UIs drift: a fix to the
citation grammar or the verdict payload would land in one and not the other, and
the "new" UI would quietly go stale while looking newer. Subclassing makes drift
impossible, because there is only one implementation of ``/api/ask``.

Two additions
-------------

``GET /api/companies``
    An overview of the issuers actually in the graph -- filings per ticker, the
    forms on file, the newest period. The old page hard-codes eight Apple
    questions, which is wrong the moment the graph holds a second issuer; this
    endpoint lets the new page build its sample questions from the data.

``GET /api/route?q=...``
    Which of the three retrieval routes a question would take: ``KNOWN`` from the
    stored graph, ``COLD_START`` through the live fetch pipeline, or ``AMBIGUOUS``
    when the question names more than one issuer. It costs one graph query and no
    model call, and it is what lets the new page tell the reader which of those
    is about to happen before it spends anything.

``GET /api/markets``
    Live quotes for the ticker strip and the market cards on the landing page,
    pulled from Yahoo Finance via yfinance. Results are cached for a minute so a
    page full of visitors costs one upstream call per minute, not one per visit.

Nothing here writes. The knowledge graph is opened read-only, exactly as the old
server opens it.
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

from .. import query_ui as _legacy
from ..query_ui import (
    KnowledgeGraph,
    _int_param,
    _listeners,
    parse_ports,
    resolve_db_path,
)
from ..router import route_query

log = logging.getLogger("graphrag_ui_next")

_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "static"
_LANDING = _HERE / "landing"
_AUTH = _HERE / "auth"

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
    "graph.js": "text/javascript; charset=utf-8",
    "answer.js": "text/javascript; charset=utf-8",
    "process.js": "text/javascript; charset=utf-8",
    "reports.js": "text/javascript; charset=utf-8",
    "util.js": "text/javascript; charset=utf-8",
}

_AUTH_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "callback.html": "text/html; charset=utf-8",
}

_VENDOR_ASSETS: dict[str, str] = {
    "gsap.min.js": "text/javascript; charset=utf-8",
    "d3.v7.min.js": "text/javascript; charset=utf-8",
}


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
             "period": fp, "period_end": period_end}
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

#: How long a quote batch is reused. Yahoo's own polling guidance is one call
#: per symbol per minute for anything near real time; a minute of cache keeps
#: the page live without turning it into a request amplifier.
_MARKETS_TTL = 60.0

_markets_cache: tuple[float, list[dict[str, Any]]] | None = None


def _fetch_one(ticker: str) -> dict[str, Any] | None:
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
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
    global _markets_cache

    now = time.monotonic()
    if _markets_cache is not None and now - _markets_cache[0] < _MARKETS_TTL:
        return _markets_cache[1]

    rows: list[dict[str, Any]] = []
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_fetch_one, MARKET_TICKERS))
        rows = [r for r in results if r is not None]
    except Exception as exc:
        log.warning("market quotes unavailable: %s", exc)
        if _markets_cache is not None:
            return _markets_cache[1]
        return []

    _markets_cache = (now, rows)
    return rows


# ── company detail data ────────────────────────────────────────────────────────

#: Cache for company detail data (TTL: 5 minutes for detail, 1 minute for quotes)
_COMPANY_DETAIL_TTL = 300.0
_COMPANY_QUOTE_TTL = 60.0

_company_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_company_quote_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _fetch_company_quote(ticker: str) -> dict[str, Any] | None:
    """Fetch basic quote data from Yahoo Finance chart API."""
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    try:
        resp = requests.get(url, timeout=10, headers=_YAHOO_HEADERS)
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


def _fetch_with_retry(url: str, max_retries: int = 3, base_delay: float = 1.0):
    """Fetch URL with exponential backoff retry."""
    import requests
    import time

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=15, headers=_YAHOO_HEADERS)
            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning("Rate limited (429), retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, max_retries)
                    time.sleep(delay)
                    continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                log.warning("Request failed after %d attempts: %s", max_retries, exc)
                return None
            delay = base_delay * (2 ** attempt)
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
    global _company_detail_cache, _company_quote_cache

    ticker = ticker.upper().strip()
    now = time.monotonic()

    # Check quote cache (short TTL)
    quote = None
    if ticker in _company_quote_cache:
        cached_time, cached_data = _company_quote_cache[ticker]
        if now - cached_time < _COMPANY_QUOTE_TTL:
            quote = cached_data

    if quote is None:
        quote = _fetch_company_quote(ticker)
        if quote:
            _company_quote_cache[ticker] = (now, quote)

    # Check detail cache (longer TTL)
    detail_cached = None
    if ticker in _company_detail_cache:
        cached_time, cached_data = _company_detail_cache[ticker]
        if now - cached_time < _COMPANY_DETAIL_TTL:
            detail_cached = cached_data

    if detail_cached is None:
        detail_raw = _fetch_company_detail(ticker)
        if detail_raw:
            fundamentals = _extract_fundamentals(detail_raw)
            detail_cached = {"fundamentals": fundamentals, "raw": detail_raw}
            _company_detail_cache[ticker] = (now, detail_cached)

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
        from ..query_ui import KnowledgeGraph, resolve_db_path
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

#: Providers the server will issue a session for. ``dev`` is the local
#: development sign-in the page falls back to when no OAuth is configured.
_ALLOWED_PROVIDERS = frozenset(AUTH_PROVIDERS) | {"dev"}


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
    _CORS_ORIGIN = os.environ.get("FINGRAPH_CORS_ORIGIN", "http://localhost:5173").strip()
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode("utf-8"))
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
        return self._serve_file(name, _STATIC, _VENDOR_ASSETS)

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
        if p in ("/favicon.svg", "/favicon.ico"):
            # Inline SVG, so the browser stops asking for a file that is not here
            # and no binary asset has to be checked in beside the source.
            svg = (
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                b'<rect width="32" height="32" rx="9" fill="#030303"/>'
                b'<circle cx="16" cy="19" r="6.4" fill="#FF3C00"/>'
                b'<circle cx="6.5" cy="9" r="3.2" fill="#FF6B35"/>'
                b'<circle cx="25.5" cy="9" r="3.2" fill="#FF551C"/>'
                b'<circle cx="25" cy="22" r="2.6" fill="#FFA07A"/>'
                b'<path d="M16 19 6.5 9M16 19l9.5-10M16 19l9 3" stroke="#F5F5F7" '
                b'stroke-width="1.3" opacity=".6" fill="none"/></svg>'
            )
            return self._send(200, svg, "image/svg+xml")
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
            # Handle /company/{ticker} and /company/{ticker}/{asset}
            # Path format: /company/AAPL or /company/AAPL/styles.css
            remainder = p[len("/company/"):].split("?")[0]
            if "/" in remainder:
                ticker, asset_name = remainder.split("/", 1)
            else:
                ticker, asset_name = remainder, "index.html"
            ticker = ticker.upper()
            if ticker and ticker.isalpha():
                return self._serve_company(ticker, asset_name)
        if p == "/api/auth/config":
            # Which providers have a client ID configured. The IDs themselves
            # never leave the server; the page only learns which buttons to show.
            return self._json({k: bool(v) for k, v in AUTH_PROVIDERS.items()})
        if p == "/api/auth/session":
            token = self._cookie_value()
            provider = _session_provider(token)
            return self._json({"provider": provider, "authenticated": provider is not None})
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
        # Protected API endpoints require authentication
        # Exploration endpoints (/api/graph, /api/entities, /api/stats) are public like the legacy UI
        # Only write endpoints and reports require auth
        if p.startswith("/api/reports") or p.startswith("/api/ingestion"):
            if self._require_auth() is None:
                return True
        # Everything else -- /api/rag, /vendor/* -- is the old router, unchanged.
        return super()._get()

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

        The provider token is not re-validated against the provider here: that
        needs each provider's server-side credentials, which a local server
        does not have. The state parameter -- checked by the callback page --
        is what binds the redirect to this browser session.
        """
        try:
            payload = _read_json_body(self)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._err(400, str(exc))

        provider = str(payload.get("provider") or "").strip().lower()
        if provider not in _ALLOWED_PROVIDERS:
            return self._err(400, f"unknown provider: {provider!r}")
        if provider != "dev" and not AUTH_PROVIDERS.get(provider):
            return self._err(400, f"provider not configured: {provider}")
        if not str(payload.get("token") or "").strip():
            return self._err(400, "token is required")

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

    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True,
                         name=f"http-{server.server_address[1]}").start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        kg.close()


def main() -> None:
    import argparse

    _legacy._configure_logging()
    p = argparse.ArgumentParser(
        prog="python -m sandbox_engine.ui_next",
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
