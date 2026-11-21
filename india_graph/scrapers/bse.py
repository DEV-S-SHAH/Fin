"""BSE India scraper.

BSE (bseindia.com) exposes public XML/RSS feeds for corporate announcements
that load without session cookies. This scraper:

1. Fetches the BSE announcements XML feed and filters items matching the
   company's scripcode or name.
2. Has no live public financial-results endpoint (the XML financial feed is
   typically empty and the JSON API subdomain is blocked), so financials come
   from NSE/yfinance instead.
3. Never fabricates data — blocked sources return errors.

SSRF allow-list: www.bseindia.com, bseindia.com.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, ClassVar

from .base import BaseScraper, ScrapedRecord, ScrapeResult


class BSEScraper(BaseScraper):
    """Scraper for BSE India (bseindia.com)."""

    source_name = "BSE"
    allowed_hosts = ("www.bseindia.com", "bseindia.com")

    HOMEPAGE = "https://www.bseindia.com"
    # Public RSS/XML feed of latest BSE corporate announcements.
    ANNOUNCEMENTS_XML = "https://www.bseindia.com/data/xml/announcements.aspx"

    # Common BSE scripcode overrides (symbol -> scripcode). Expanded at setup
    # time from the BSE security master. RELIANCE = 500325.
    _DEFAULT_SCRIPCODES: ClassVar[dict[str, str]] = {
        "RELIANCE": "500325",
        "TCS": "532540",
        "HDFCBANK": "500180",
        "ICICIBANK": "532174",
        "INFY": "500209",
        "HINDUNILVR": "500696",
        "ITC": "500875",
        "SBIN": "500112",
        "BHARTIARTL": "532454",
        "BAJFINANCE": "500034",
        "KOTAKBANK": "500247",
        "LT": "500510",
        "AXISBANK": "532215",
        "ASIANPAINT": "523297",
        "HCLTECH": "532281",
        "MARUTI": "532500",
        "SUNPHARMA": "524715",
        "TITAN": "500114",
        "TATAMOTORS": "500570",
        "ULTRACEMCO": "532538",
        "NESTLEIND": "500790",
        "WIPRO": "507688",
        "M&M": "500520",
        "NTPC": "532555",
        "TECHM": "532755",
        "POWERGRID": "533858",
        "TATACONSUM": "500800",
        "ADANIENT": "542482",
        "ADANIPORTS": "533103",
        "BAJAJ-AUTO": "532977",
        "BPCL": "500547",
        "BRITANNIA": "500055",
        "CIPLA": "500087",
        "COALINDIA": "533278",
        "DIVISLAB": "532488",
        "DRREDDY": "500124",
        "EICHERMOT": "505200",
        "GRASIM": "500300",
        "HEROMOTOCO": "500182",
        "HINDALCO": "500440",
        "HDFCLIFE": "540777",
        "ICICIPRULI": "540133",
        "INDUSINDBK": "532737",
        "JSWSTEEL": "500228",
        "ONGC": "500312",
        "SBILIFE": "540719",
        "SHRIRAMFIN": "511218",
        "TATAPOWER": "500410",
        "TATASTEEL": "500470",
        "UPL": "512088",
        "VEDL": "532302",
    }

    def __init__(self, config=None):
        super().__init__(config)
        # symbol -> scripcode, populated from _DEFAULT_SCRIPCODES and resolver.
        self._scripcode_map: dict[str, str] = dict(self._DEFAULT_SCRIPCODES)
        self._cookies: dict[str, str] = {}

    def _ensure_session(self, symbol: str) -> None:
        try:
            resp = self._get(self.HOMEPAGE, timeout=10.0)
            self._cookies = dict(resp.cookies)
        except Exception:  # noqa: BLE001 — graceful session setup failure
            self._cookies = {}

    def _scripcode_for(self, symbol: str) -> str | None:
        return self._scripcode_map.get(symbol.upper().strip())

    def register_scripcode(self, symbol: str, scripcode: str) -> None:
        self._scripcode_map[symbol.upper().strip()] = scripcode.strip()

    # -- public interface ---------------------------------------------------

    def fetch_company(self, cin: str) -> ScrapeResult:
        """BSE does not key on CIN. Return an empty result — MCA is the CIN source."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="BSE does not key on CIN; use MCA for company master data",
            source=self.source_name,
        )

    def fetch_announcements(self, cin: str, limit: int = 50) -> ScrapeResult:
        """Recent corporate announcements, filtered to the company's scripcode.

        Reads BSE's public announcements XML feed and keeps only items whose
        scripcode or title matches the company.
        """
        symbol = cin  # caller passes symbol; cin stored on the record
        self._ensure_session(symbol)

        scripcode = self._scripcode_for(symbol)
        try:
            resp = self._get(self.ANNOUNCEMENTS_XML, timeout=15.0)
        except Exception as exc:  # noqa: BLE001 — graceful request failure
            return ScrapeResult(
                ok=False,
                error=f"BSE announcements request failed: {exc}",
                source=self.source_name,
            )
        if resp.status_code != 200:
            return ScrapeResult(
                ok=False,
                error=f"BSE announcements HTTP {resp.status_code}",
                source=self.source_name,
                http_status=resp.status_code,
            )
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            return ScrapeResult(
                ok=False,
                error=f"BSE announcements response was not valid XML: {exc}",
                source=self.source_name,
                http_status=resp.status_code,
            )

        records: list[ScrapedRecord] = []
        symbol_upper = symbol.upper().strip()

        for item in root.findall(".//item"):
            title = self._item_text(item, "title")
            desc = self._item_text(item, "description")
            pub_date = self._item_text(item, "pubDate")
            item_scripcode = self._item_text(item, "scripcode")

            # Filter strictly by scripcode when we know it (avoids false
            # positives like "HDFC Charity Fund" matching "HDFCBANK"). Only
            # fall back to a loose title/name match when no scripcode mapping
            # is available for this symbol.
            if scripcode is not None:
                match = item_scripcode == scripcode
            else:
                match = (
                    symbol_upper in (title or "").upper()
                    or symbol_upper in (desc or "").upper()
                )

            if not match:
                continue

            rec = self.normalize(
                {
                    "title": title,
                    "description": desc,
                    "pubDate": pub_date,
                    "scripcode": item_scripcode,
                    "symbol": symbol,
                },
                "announcement",
            )
            if rec:
                records.append(rec)
            if len(records) >= limit:
                break

        return ScrapeResult(
            ok=True,
            records=records,
            source=self.source_name,
            http_status=resp.status_code,
        )

    def fetch_financials(self, cin: str, limit: int = 8) -> ScrapeResult:
        """BSE financial results.

        BSE's public XML financial-results feed is typically empty and the
        JSON API subdomain (api.bseindia.com) is blocked. Financials are
        sourced from NSE/yfinance instead. Return empty (not fabricated).
        """
        return ScrapeResult(
            ok=True,
            records=[],
            error="BSE public financial-results feed is empty; financials sourced from NSE/yfinance",
            source=self.source_name,
        )

    def normalize(self, raw: Any, record_type: str) -> ScrapedRecord | None:
        """Normalize a BSE announcement XML item into a ScrapedRecord."""
        if not isinstance(raw, dict):
            return None
        if record_type == "announcement":
            return ScrapedRecord(
                record_type="announcement",
                source=self.source_name,
                source_id=str(raw.get("scripcode", raw.get("title", ""))),
                data={
                    "title": raw.get("title", ""),
                    "date": raw.get("pubDate", ""),
                    "category": "Corporate Announcement",
                    "attachment": "",
                    "symbol": raw.get("symbol", ""),
                    "description": raw.get("description", ""),
                },
            )
        if record_type == "financial":
            return ScrapedRecord(
                record_type="financial",
                source=self.source_name,
                source_id=str(raw.get("symbol", "")),
                data={
                    "symbol": raw.get("symbol", ""),
                    "company_name": raw.get("company_name", ""),
                    "revenue": raw.get("revenue"),
                    "profit_after_tax": raw.get("profit_after_tax"),
                    "eps": raw.get("eps"),
                    "period": raw.get("period", ""),
                    "fiscal_year": raw.get("fiscal_year", ""),
                },
            )
        return None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _item_text(item: ET.Element, tag: str) -> str | None:
        el = item.find(tag)
        if el is None or el.text is None:
            return None
        return el.text.strip()
