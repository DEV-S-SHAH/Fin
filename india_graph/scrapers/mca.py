"""MCA (Ministry of Corporate Affairs) scraper.

MCA is the canonical source for the CIN (Corporate Identification Number) and
company master data. Public company-master pages are fetched by CIN. Like the
exchange scrapers this rotates User-Agents, jitters, respects Retry-After and
retries once on 429; blocked requests return errors, never fabricated data.
"""

from __future__ import annotations

from typing import Any

from .base import BaseScraper, ScrapedRecord, ScrapeResult


class MCAScraper(BaseScraper):
    """Scraper for MCA India (mca.gov.in)."""

    source_name = "MCA"
    allowed_hosts = ("www.mca.gov.in", "mca.gov.in")

    HOMEPAGE = "https://www.mca.gov.in"
    COMPANY_MASTER = "https://www.mca.gov.in/ministry/pdf/masterData/{cin}"

    def __init__(self, config=None):
        super().__init__(config)
        self._cookies: dict[str, str] = {}

    def fetch_company(self, cin: str) -> ScrapeResult:
        """Company master record keyed on MCA CIN — the canonical identifier."""
        self._ensure_session()
        url = self.COMPANY_MASTER.format(cin=cin)
        try:
            resp = self._get(url, timeout=15.0)
        except Exception as exc:
            return ScrapeResult(
                ok=False,
                error=f"MCA company master request failed: {exc}",
                source=self.source_name,
            )
        if resp.status_code != 200:
            return ScrapeResult(
                ok=False,
                error=f"MCA company master HTTP {resp.status_code}",
                source=self.source_name,
                http_status=resp.status_code,
            )
        content = resp.text
        rec = self.normalize(content, "company")
        if rec is None:
            return ScrapeResult(
                ok=False,
                error="MCA company master response could not be parsed",
                source=self.source_name,
                http_status=resp.status_code,
            )
        return ScrapeResult(
            ok=True,
            records=[rec],
            source=self.source_name,
            http_status=resp.status_code,
        )

    def fetch_announcements(self, cin: str, limit: int = 50) -> ScrapeResult:
        """MCA does not publish announcements via a public API keyed on CIN."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="MCA does not publish announcements; use NSE/BSE",
            source=self.source_name,
        )

    def fetch_financials(self, cin: str, limit: int = 8) -> ScrapeResult:
        """MCA financials are filed documents; public download requires login."""
        return ScrapeResult(
            ok=True,
            records=[],
            error="MCA financial filings require authenticated download; use NSE/BSE",
            source=self.source_name,
        )

    def normalize(self, raw: Any, record_type: str) -> ScrapedRecord | None:
        """Normalize MCA master data (HTML or JSON) into a ScrapedRecord."""
        if record_type != "company":
            return None
        text = raw if isinstance(raw, str) else str(raw)
        data: dict[str, Any] = {
            "cin": "",
            "company_name": "",
            "roc": "",
            "registration_number": "",
            "company_status": "",
            "paid_up_capital": "",
            "authorized_capital": "",
            "date_of_incorporation": "",
            "activity_description": "",
            "email": "",
            "registered_office_address": "",
        }
        # Very light extraction: look for CIN and company name patterns in HTML.
        # MCA's public pages are HTML tables; a full parser is Phase 2. We
        # extract what we can with regex and mark the rest as unextracted.
        cin_match = __import__("re").search(r"\b[LUA][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}\b", text)
        if cin_match:
            data["cin"] = cin_match.group(0)
        name_match = __import__("re").search(r"Company Name[:\s]*([^\n<]{3,120})", text, __import__("re").IGNORECASE)
        if name_match:
            data["company_name"] = name_match.group(1).strip()
        return ScrapedRecord(
            record_type="company",
            source=self.source_name,
            source_id=data["cin"],
            data=data,
        )

    # -- internal -----------------------------------------------------------

    def _ensure_session(self) -> None:
        try:
            resp = self._get(self.HOMEPAGE, timeout=10.0)
            self._cookies = dict(resp.cookies)
        except Exception:
            self._cookies = {}
