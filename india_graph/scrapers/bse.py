"""BSE India scraper.

BSE (bseindia.com) carries corporate announcements and financials. Like NSE it
has no documented public API and protects against scraping; this scraper hits
BSE's public endpoints with rotated User-Agents, jitter, Retry-After respect
and one jittered retry on 429. Blocked requests return errors, never fabricated
data.
"""

from __future__ import annotations

from typing import Any

from .base import BaseScraper, ScrapedRecord, ScrapeResult


class BSEScraper(BaseScraper):
    """Scraper for BSE India (bseindia.com)."""

    source_name = "BSE"
    allowed_hosts = ("www.bseindia.com", "bseindia.com")

    HOMEPAGE = "https://www.bseindia.com"
    ANNOUNCEMENTS = "https://www.bseindia.com/api/v1/announcement?scrip={symbol}"
    FINANCIALS = "https://www.bseindia.com/api/v1/financials?scrip={symbol}"

    def __init__(self, config=None):
        super().__init__(config)
        self._cookies: dict[str, str] = {}

    def _ensure_session(self, symbol: str) -> None:
        try:
            resp = self._get(self.HOMEPAGE, timeout=10.0)
            self._cookies = dict(resp.cookies)
        except Exception:  # noqa: BLE001 — graceful session setup failure
            self._cookies = {}

    def fetch_company(self, cin: str) -> ScrapeResult:
        """BSE does not key on CIN. MCA is the CIN source."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="BSE does not key on CIN; use MCA for company master data",
            source=self.source_name,
        )

    def fetch_announcements(self, cin: str, limit: int = 50) -> ScrapeResult:
        symbol = cin
        self._ensure_session(symbol)
        url = self.ANNOUNCEMENTS.format(symbol=symbol)
        try:
            resp = self._get(url, timeout=10.0)
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
            payload = resp.json()
        except ValueError:
            return ScrapeResult(
                ok=False,
                error="BSE announcements response was not JSON",
                source=self.source_name,
                http_status=resp.status_code,
            )
        records: list[ScrapedRecord] = []
        data = payload.get("data", payload.get("Announcements", [])) if isinstance(payload, dict) else []
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
        symbol = cin
        self._ensure_session(symbol)
        url = self.FINANCIALS.format(symbol=symbol)
        try:
            resp = self._get(url, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 — graceful request failure
            return ScrapeResult(
                ok=False,
                error=f"BSE financials request failed: {exc}",
                source=self.source_name,
            )
        if resp.status_code != 200:
            return ScrapeResult(
                ok=False,
                error=f"BSE financials HTTP {resp.status_code}",
                source=self.source_name,
                http_status=resp.status_code,
            )
        try:
            payload = resp.json()
        except ValueError:
            return ScrapeResult(
                ok=False,
                error="BSE financials response was not JSON",
                source=self.source_name,
                http_status=resp.status_code,
            )
        records: list[ScrapedRecord] = []
        data = payload.get("data", []) if isinstance(payload, dict) else []
        for item in data[:limit]:
            rec = self.normalize(item, "financial")
            if rec:
                records.append(rec)
        return ScrapeResult(
            ok=True,
            records=records,
            source=self.source_name,
            http_status=resp.status_code,
        )

    def normalize(self, raw: Any, record_type: str) -> ScrapedRecord | None:
        if not isinstance(raw, dict):
            return None
        if record_type == "announcement":
            return ScrapedRecord(
                record_type="announcement",
                source=self.source_name,
                source_id=str(raw.get("id", raw.get("announcement_id", ""))),
                data={
                    "title": raw.get("heading", raw.get("subject", "")),
                    "date": raw.get("dissemination_date", raw.get("date", "")),
                    "category": raw.get("category", ""),
                    "attachment": raw.get("attachment", ""),
                    "symbol": raw.get("scrip", raw.get("symbol", "")),
                },
            )
        if record_type == "financial":
            return ScrapedRecord(
                record_type="financial",
                source=self.source_name,
                source_id=str(raw.get("symbol", raw.get("scrip", ""))),
                data={
                    "symbol": raw.get("symbol", raw.get("scrip", "")),
                    "company_name": raw.get("company_name", ""),
                    "revenue": raw.get("revenue"),
                    "profit_after_tax": raw.get("profit_after_tax"),
                    "eps": raw.get("eps"),
                    "period": raw.get("period", ""),
                    "fiscal_year": raw.get("fiscal_year", ""),
                },
            )
        return None
