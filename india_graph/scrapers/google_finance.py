"""Google Finance scraper.

Google Finance (google.com/finance) embeds quote and financial data in
`AF_initDataCallback()` JavaScript calls on the quote page. This scraper:

1. Fetches the quote page for `{symbol}:{exchange}` (e.g. HDFCBANK:NSE).
2. Parses the embedded JSON-like callbacks to extract:
   - Current price, change, change percent, previous close
   - Day high/low, volume, market cap
   - Annual/quarterly revenue (where available)
3. Tags all records with source "Google Finance" (EXTERNAL provenance) so
   they are never mislabeled as NSE/BSE STATED data.

SSRF allow-list: www.google.com, google.com.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import BaseScraper, ScrapedRecord, ScrapeResult


class GoogleFinanceScraper(BaseScraper):
    """Scraper for Google Finance (google.com/finance)."""

    source_name = "Google Finance"
    allowed_hosts = ("www.google.com", "google.com")

    QUOTE_URL = "https://www.google.com/finance/quote/{symbol}:{exchange}"

    def __init__(self, config=None):
        super().__init__(config)

    def normalize(self, raw: Any, record_type: str) -> ScrapedRecord | None:
        """Normalize a raw Google Finance payload into a ScrapedRecord."""
        if not isinstance(raw, dict):
            return None
        if record_type not in ("financial", "company", "announcement"):
            return None
        symbol = raw.get("symbol", "")
        exchange = raw.get("exchange", "")
        source_id = f"{symbol}:{exchange}" if exchange else symbol
        return ScrapedRecord(
            record_type=record_type,
            source=self.source_name,
            source_id=source_id,
            data=raw,
        )

    # -- public interface ----------------------------------------------------

    def fetch_company(self, cin: str) -> ScrapeResult:
        """Google Finance does not key on CIN."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="Google Finance does not key on CIN; use MCA for company master data",
            source=self.source_name,
        )

    def fetch_announcements(self, cin: str, limit: int = 50) -> ScrapeResult:
        """Google Finance does not provide corporate announcements."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="Google Finance does not provide corporate announcements",
            source=self.source_name,
        )

    def fetch_financials(self, cin: str, limit: int = 8) -> ScrapeResult:
        """Fetch price and financials for a company.

        *cin* is expected to be a ticker symbol (e.g. "HDFCBANK"). The
        exchange is detected by trying NSE first, then BOM.
        """
        symbol = cin.strip().upper()

        for exchange in ("NSE", "BOM"):
            result = self._fetch_quote(symbol, exchange)
            if result.ok and result.records:
                return result

        return ScrapeResult(
            ok=False,
            error=f"Google Finance: no data found for {symbol} on NSE or BOM",
            source=self.source_name,
        )

    # -- internal ------------------------------------------------------------

    def _fetch_quote(self, symbol: str, exchange: str) -> ScrapeResult:
        url = self.QUOTE_URL.format(symbol=symbol, exchange=exchange)
        try:
            resp = self._get(url, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 — graceful request failure
            return ScrapeResult(
                ok=False,
                error=f"Google Finance request failed: {exc}",
                source=self.source_name,
            )
        if resp.status_code != 200:
            return ScrapeResult(
                ok=False,
                error=f"Google Finance HTTP {resp.status_code}",
                source=self.source_name,
                http_status=resp.status_code,
            )

        html = resp.text

        # Extract all AF_initDataCallback payloads.
        callbacks = self._extract_callbacks(html)
        if not callbacks:
            return ScrapeResult(
                ok=False,
                error="Google Finance page contained no data callbacks",
                source=self.source_name,
                http_status=resp.status_code,
            )

        price = self._extract_price(callbacks, symbol, exchange)
        financials = self._extract_financials(callbacks)

        if price is None and financials is None:
            return ScrapeResult(
                ok=False,
                error="Google Finance: could not parse price or financials",
                source=self.source_name,
                http_status=resp.status_code,
            )

        data: dict[str, Any] = {
            "symbol": symbol,
            "exchange": exchange,
            "company_name": price.get("company_name") if price else symbol,
            "currency": price.get("currency", "INR") if price else "INR",
        }
        if price:
            data.update(
                {
                    "last_price": price.get("last_price"),
                    "change": price.get("change"),
                    "p_change": price.get("p_change"),
                    "previous_close": price.get("previous_close"),
                    "day_high": price.get("day_high"),
                    "day_low": price.get("day_low"),
                    "volume": price.get("volume"),
                    "market_cap": price.get("market_cap"),
                }
            )
        if financials:
            data.update(financials)

        rec = ScrapedRecord(
            record_type="financial",
            source=self.source_name,
            source_id=f"{symbol}:{exchange}",
            data=data,
        )
        return ScrapeResult(
            ok=True,
            records=[rec],
            source=self.source_name,
            http_status=resp.status_code,
        )

    @staticmethod
    def _extract_callbacks(html: str) -> list[Any]:
        """Extract the data payloads from AF_initDataCallback() calls.

        Google Finance embeds data like:
          AF_initDataCallback({key: 'ds:2', hash: '7', data:[[...]], sideChannel: {}});
        We capture everything between AF_initDataCallback( and ); then extract
        the JSON array that follows `data:`.
        """
        callbacks: list[Any] = []
        outer = re.compile(r"AF_initDataCallback\((.*?)\);", re.DOTALL)
        for m in outer.finditer(html):
            inner = m.group(1)
            # Find the data payload: data:<JSON-array>
            data_match = re.search(r"\bdata:\s*(\[.*\])", inner, re.DOTALL)
            if not data_match:
                continue
            payload = data_match.group(1)
            try:
                parsed = json.loads(payload)
                callbacks.append(parsed)
            except (ValueError, json.JSONDecodeError):
                continue
        return callbacks

    @staticmethod
    def _extract_price(callbacks: list[Any], symbol: str = "", exchange: str = "") -> dict[str, Any] | None:
        """Extract price data from the main quote callback (ds:2 / ds:13 style)."""
        for cb in callbacks:
            price = GoogleFinanceScraper._parse_price_callback(cb, symbol, exchange)
            if price:
                return price
        return None

    @staticmethod
    def _parse_price_callback(cb: Any, symbol: str = "", exchange: str = "") -> dict[str, Any] | None:
        """Parse a single callback for price fields.

        Google Finance nests data at varying depths. Two shapes exist:

        ds:2 shape (stock quote):
          [kg_id, [ticker, exchange], name, ?, currency,
           [price, change, p_change, ?, ?, ?],
           null, previous_close, ...]

        ds:8 shape (alternate quote):
          [null, [ticker, exchange], null, kg_id,
           price, day_high, day_low, ?, change, ?, p_change, ?,
           currency, "SYMBOL:EX", name, previous_close,
           market_cap, volume, ...]

        We search recursively and validate that the candidate looks like a
        real stock quote (price in a sane range, currency present, ticker matches).
        """
        if not isinstance(cb, list):
            return None

        def _search(node: Any, depth: int = 0) -> dict[str, Any] | None:
            if depth > 6:
                return None
            if not isinstance(node, list):
                return None
            if len(node) >= 2 and isinstance(node[1], list) and len(node[1]) >= 2:
                ticker_exchange = node[1]
                node_symbol = ticker_exchange[0]
                node_exchange = ticker_exchange[1]
                # Only match the symbol we asked for — avoids picking up
                # sector/industry callbacks that happen to have a 2-element
                # list at index 1.
                if symbol and node_symbol != symbol:
                    return None
                if exchange and node_exchange != exchange:
                    return None

                # Shape A: node[5] is [price, change, p_change] (ds:2)
                if (
                    len(node) >= 8
                    and isinstance(node[5], list)
                    and len(node[5]) >= 3
                    and all(isinstance(x, (int, float)) for x in node[5][:3])
                ):
                    last_price = node[5][0]
                    # Sanity check: Indian stock prices are typically ₹1–₹1,00,000.
                    # Financial callbacks put revenue (₹100s of billions) here — reject those.
                    if not (1 <= last_price <= 1_000_000):
                        return None
                    return {
                        "symbol": node_symbol,
                        "exchange": node_exchange,
                        "company_name": node[2] if isinstance(node[2], str) else node_symbol,
                        "currency": node[4] if isinstance(node[4], str) else "INR",
                        "last_price": last_price,
                        "change": node[5][1],
                        "p_change": node[5][2],
                        "previous_close": node[7],
                        "day_high": None,
                        "day_low": None,
                        "volume": None,
                        "market_cap": None,
                    }

                # Shape B: node[4] is price (number), node[5] is day_high (ds:8)
                if (
                    len(node) >= 18
                    and isinstance(node[4], (int, float))
                    and isinstance(node[5], (int, float))
                    and 1 <= node[4] <= 1_000_000
                ):
                    return {
                        "symbol": node_symbol,
                        "exchange": node_exchange,
                        "company_name": (
                            node[14] if isinstance(node[14], str) else node_symbol
                        ),
                        "currency": node[12] if isinstance(node[12], str) else "INR",
                        "last_price": node[4],
                        "change": node[8],
                        "p_change": node[10],
                        "previous_close": node[15],
                        "day_high": node[5],
                        "day_low": node[6],
                        "volume": node[17],
                        "market_cap": node[16],
                    }

            # Recurse into children.
            for child in node:
                result = _search(child, depth + 1)
                if result:
                    return result
            return None

        return _search(cb)

    @staticmethod
    def _extract_financials(callbacks: list[Any]) -> dict[str, Any] | None:
        """Google Finance financial metric arrays don't have revenue in a
        predictable position, so we skip revenue extraction here. Revenue is
        sourced from yfinance (via the NSE scraper fallback) instead.

        Returns None so the orchestrator doesn't create a spurious revenue
        metric from Google Finance.
        """
        return None
