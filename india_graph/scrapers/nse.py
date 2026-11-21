"""NSE India scraper.

NSE (nseindia.com) is protected by aggressive Akamai anti-bot measures and
frequently returns 403 Access Denied from cloud/IP ranges. This scraper:

1. Tries the live NSE JSON endpoints first (with rotated UA, jitter, cookies).
2. Falls back to yfinance (RELIANCE.NS) for live price data when NSE is blocked.
3. Never fabricates data — if both sources fail, returns an error and the
   orchestrator falls back to clearly-labeled offline fixtures.

Provenance integrity comes first: blocked sources return errors, never guessed
numbers.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseScraper, ScrapedRecord, ScrapeResult


class NSEScraper(BaseScraper):
    """Scraper for NSE India (nseindia.com) with yfinance price fallback."""

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
        # yfinance ticker symbol for this company's exchange suffix (.NS)
        self._yf_symbol: str | None = None

    # -- yfinance fallback ---------------------------------------------------

    def _yf_financials(self, symbol: str) -> ScrapedRecord | None:
        """Fetch latest price + revenue from yfinance when NSE is blocked.

        yfinance is an external (non-exchange) source, so records are tagged
        with source "Yahoo Finance" rather than "NSE" so provenance stays
        honest.

        Indian companies report quarterly (not monthly), so "last month"
        revenue is not a standard disclosed figure. We return the latest
        quarterly revenue from yfinance's quarterly financials, falling back
        to the annual totalRevenue from .info.
        """
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return None

        yf_symbol = f"{symbol}.NS"
        try:
            ticker = yf.Ticker(yf_symbol)
            info = ticker.info or {}
            if not isinstance(info, dict):
                info = {}

            last_price = info.get("regularMarketPrice")
            if last_price is None:
                fast = ticker.fast_info or {}
                last_price = fast.get("last_price") or fast.get("regular_market_price")

            # Revenue: prefer latest quarter, fall back to annual totalRevenue.
            revenue = None
            revenue_period = ""
            try:
                qf = ticker.quarterly_financials
                if qf is not None and not qf.empty:
                    rev_row = None
                    for label in qf.index:
                        lbl = str(label).lower()
                        if "total revenue" in lbl or "operating revenue" in lbl:
                            rev_row = label
                            break
                    if rev_row is not None:
                        latest_col = qf.columns[0]
                        revenue = qf.loc[rev_row, latest_col]
                        revenue_period = str(latest_col.date())[:10]
            except Exception:  # noqa: BLE001 — quarterly financials may be unavailable
                revenue = None

            if revenue is None:
                revenue = info.get("totalRevenue")
                revenue_period = "latest fiscal year"

            if last_price is None and revenue is None:
                return None

            return ScrapedRecord(
                record_type="financial",
                source="Yahoo Finance",
                source_id=yf_symbol,
                data={
                    "symbol": symbol,
                    "last_price": last_price,
                    "change": info.get("regularMarketChange"),
                    "p_change": info.get("regularMarketChangePercent"),
                    "previous_close": info.get("previousClose"),
                    "vwap": None,
                    "lower_circuit": None,
                    "upper_circuit": None,
                    "total_traded_volume": None,
                    "market_cap": info.get("marketCap"),
                    "company_name": info.get("shortName") or symbol,
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "listing_date": info.get("firstTradeDateEpochUtc", ""),
                    "face_value": "",
                    "revenue": revenue,
                    "revenue_period": revenue_period,
                },
            )
        except Exception:  # noqa: BLE001 — yfinance may fail for many reasons
            return None

    # -- HTTP ---------------------------------------------------------------

    def _ensure_session(self, symbol: str) -> None:
        """Load the NSE homepage once to obtain session cookies."""
        try:
            resp = self._get(self.HOMEPAGE, timeout=10.0)
            self._cookies = dict(resp.cookies)
        except Exception:  # noqa: BLE001 — graceful session setup failure
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

    # -- public interface ---------------------------------------------------

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
        resolved symbol -> CIN via the resolver.
        """
        symbol = cin
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
            # NSE blocked (403 Akamai, etc.) — return error, not fabricated data.
            return ScrapeResult(
                ok=False,
                error=f"NSE announcements HTTP {resp.status_code} (NSE may be blocking this network)",
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
        """Financial results for the company (latest price).

        Tries live NSE quote-equity first. If NSE is blocked (403, non-JSON),
        falls back to yfinance for live price data. Returns an error (not
        fabricated data) if both sources fail.
        """
        symbol = cin

        # --- Try live NSE first ---
        self._ensure_session(symbol)
        url = self.QUOTE_EQUITY.format(symbol=symbol)
        resp = None
        try:
            resp = self._get(url, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 — graceful request failure
            nse_error = f"NSE financials request failed: {exc}"
        else:
            nse_error = None
            try:
                payload = resp.json()
            except (ValueError, json.JSONDecodeError):
                payload = None
                nse_error = nse_error or "NSE financials response was not JSON"

            if isinstance(payload, dict):
                rec = self.normalize(payload, "financial")
                if rec:
                    return ScrapeResult(
                        ok=True,
                        records=[rec],
                        source=self.source_name,
                        http_status=resp.status_code,
                    )
                nse_error = nse_error or "NSE financials payload contained no usable data"

        # --- Fall back to yfinance ---
        yf_rec = self._yf_financials(symbol)
        if yf_rec is not None:
            return ScrapeResult(
                ok=True,
                records=[yf_rec],
                source=self.source_name,
                error=nse_error,  # carry the NSE error as context
                http_status=getattr(resp, "status_code", None),
            )

        # --- Both failed ---
        return ScrapeResult(
            ok=False,
            error=nse_error or "NSE financials request failed and yfinance fallback unavailable",
            source=self.source_name,
            http_status=getattr(resp, "status_code", None),
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
                    "revenue": raw.get("revenue"),
                    "revenue_period": raw.get("revenue_period"),
                },
            )
        return None
