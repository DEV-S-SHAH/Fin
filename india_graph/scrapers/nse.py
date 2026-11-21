"""NSE India scraper.

NSE (nseindia.com) does not publish a documented public API for historical
financials. This scraper hits the same JSON endpoints the NSE website uses,
after first loading the homepage to obtain session cookies. It rotates
User-Agents, jitters between requests, respects Retry-After, and retries once
on 429. If NSE blocks the request (anti-bot), the result is returned as an
error rather than fabricating data — provenance integrity comes first.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseScraper, ScrapedRecord, ScrapeResult


class NSEScraper(BaseScraper):
    """Scraper for NSE India (nseindia.com)."""

    source_name = "NSE"
    allowed_hosts = ("www.nseindia.com", "nseindia.com")

    # Endpoints used by the NSE website frontend.
    HOMEPAGE = "https://www.nseindia.com"
    CORPORATE_ANNOUNCEMENTS = (
        "https://www.nseindia.com/api/corporates-pit?index=equities&symbol={symbol}"
    )
    COMPANY_INFO = "https://www.nseindia.com/api/company-info?symbol={symbol}"
    QUOTE_EQUITY = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"

    def __init__(self, config=None):
        super().__init__(config)
        self._cookies: dict[str, str] = {}

    def _ensure_session(self, symbol: str) -> None:
        """Load the NSE homepage once to obtain session cookies."""
        try:
            resp = self._get(self.HOMEPAGE, timeout=10.0)
            self._cookies = dict(resp.cookies)
        except Exception:  # noqa: BLE001 — graceful session setup failure
            # Homepage load failed — we still try the API with empty cookies.
            self._cookies = {}

    def _api_headers(self) -> dict[str, str]:
        h = self._headers()
        h.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Host": "www.nseindia.com",
        })
        return h

    def fetch_company(self, cin: str) -> ScrapeResult:
        """NSE does not key on CIN. Return an empty result — MCA is the CIN source."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="NSE does not key on CIN; use MCA for company master data",
            source=self.source_name,
        )

    def fetch_announcements(self, cin: str, limit: int = 50) -> ScrapeResult:
        """Recent corporate announcements for the company.

        NSE's API is keyed by symbol, not CIN. The caller must have already
        resolved symbol -> CIN via the resolver. This method takes cin for
        interface consistency but needs the symbol; the resolver caches
        symbol->cin so we reverse-lookup here if needed.
        """
        # The resolver maps symbol->cin. We need symbol. For now, NSE scraper
        # is called with a symbol in practice; cin is the canonical key stored
        # on the record.
        symbol = cin  # caller passes symbol; cin resolution is separate
        self._ensure_session(symbol)
        url = self.CORPORATE_ANNOUNCEMENTS.format(symbol=symbol)
        try:
            resp = self._get(url, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 — graceful request failure
            return ScrapeResult(
                ok=False,
                error=f"NSE announcements request failed: {exc}",
                source=self.source_name,
            )
        if resp.status_code != 200:
            return ScrapeResult(
                ok=False,
                error=f"NSE announcements HTTP {resp.status_code}",
                source=self.source_name,
                http_status=resp.status_code,
            )
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError):
            return ScrapeResult(
                ok=False,
                error="NSE announcements response was not JSON",
                source=self.source_name,
                http_status=resp.status_code,
            )
        records: list[ScrapedRecord] = []
        data = payload.get("data", []) if isinstance(payload, dict) else []
        for item in data[:limit]:
            rec = self.normalize(item, "announcement")
            if rec:
                records.append(rec)
        return ScrapeResult(
            ok=True,
            records=records,
            source=self.source_name,
            http_status=resp.status_code,
        )

    def fetch_financials(self, cin: str, limit: int = 8) -> ScrapeResult:
        """Financial results for the company (quote endpoint for latest)."""
        symbol = cin
        self._ensure_session(symbol)
        url = self.QUOTE_EQUITY.format(symbol=symbol)
        try:
            resp = self._get(url, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 — graceful request failure
            return ScrapeResult(
                ok=False,
                error=f"NSE financials request failed: {exc}",
                source=self.source_name,
            )
        if resp.status_code != 200:
            return ScrapeResult(
                ok=False,
                error=f"NSE financials HTTP {resp.status_code}",
                source=self.source_name,
                http_status=resp.status_code,
            )
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError):
            return ScrapeResult(
                ok=False,
                error="NSE financials response was not JSON",
                source=self.source_name,
                http_status=resp.status_code,
            )
        records: list[ScrapedRecord] = []
        # NSE quote-equity returns price info; historical financials require
        # additional endpoints. Extract what's available as a financial record.
        if isinstance(payload, dict):
            rec = self.normalize(payload, "financial")
            if rec:
                records.append(rec)
        return ScrapeResult(
            ok=True,
            records=records,
            source=self.source_name,
            http_status=resp.status_code,
        )

    def normalize(self, raw: Any, record_type: str) -> ScrapedRecord | None:
        """Normalize a NSE JSON payload into a ScrapedRecord."""
        if not isinstance(raw, dict):
            return None
        if record_type == "announcement":
            return ScrapedRecord(
                record_type="announcement",
                source=self.source_name,
                source_id=str(raw.get("announcementId", raw.get("id", ""))),
                data={
                    "title": raw.get("subject", ""),
                    "date": raw.get("announcementDate", raw.get("date", "")),
                    "category": raw.get("category", ""),
                    "attachment": raw.get("attachment", ""),
                    "symbol": raw.get("symbol", ""),
                },
            )
        if record_type == "financial":
            return ScrapedRecord(
                record_type="financial",
                source=self.source_name,
                source_id=str(raw.get("symbol", "")),
                data={
                    "symbol": raw.get("symbol", ""),
                    "last_price": raw.get("priceInfo", {}).get("lastPrice"),
                    "change": raw.get("priceInfo", {}).get("change"),
                    "p_change": raw.get("priceInfo", {}).get("pChange"),
                    "previous_close": raw.get("priceInfo", {}).get("previousClose"),
                    "vwap": raw.get("priceInfo", {}).get("vwap"),
                    "lower_circuit": raw.get("priceInfo", {}).get("low"),
                    "upper_circuit": raw.get("priceInfo", {}).get("high"),
                    "total_traded_volume": raw.get("securityWAP", {}).get("quantityTraded"),
                    "market_cap": raw.get("marketInfo", {}).get("marketCap"),
                    "company_name": raw.get("info", {}).get("companyName"),
                    "sector": raw.get("info", {}).get("sector"),
                    "industry": raw.get("info", {}).get("industry"),
                    "listing_date": raw.get("info", {}).get("listingDate"),
                    "face_value": raw.get("info", {}).get("faceValue"),
                },
            )
        return None
