"""Tests for India scrapers — normalization and error handling (no real network)."""

import time
from typing import Any

from india_graph.config import IndiaConfig
from india_graph.scrapers.base import BaseScraper, ScrapeResult
from india_graph.scrapers.bse import BSEScraper
from india_graph.scrapers.google_finance import GoogleFinanceScraper
from india_graph.scrapers.mca import MCAScraper
from india_graph.scrapers.nse import NSEScraper


class _ConcreteScraper(BaseScraper):
    """Concrete BaseScraper subclass for testing HTTP helpers (no network)."""

    source_name = "TEST"
    allowed_hosts = ("example.com",)

    def fetch_company(self, cin: str):  # noqa: ARG002
        ...

    def fetch_announcements(self, cin: str, limit: int = 50):  # noqa: ARG002
        ...

    def fetch_financials(self, cin: str, limit: int = 8):  # noqa: ARG002
        ...

    def normalize(self, raw: Any, record_type: str):  # noqa: ARG002
        return None


def test_nse_normalize_announcement():
    s = NSEScraper()
    rec = s.normalize({
        "announcementId": "ANN123",
        "subject": "Board meeting outcome",
        "announcementDate": "2025-11-01",
        "category": "Board",
        "attachment": "https://nseindia.com/attach.pdf",
        "symbol": "RELIANCE",
    }, "announcement")
    assert rec is not None
    assert rec.source == "NSE"
    assert rec.record_type == "announcement"
    assert rec.source_id == "ANN123"
    assert rec.data["title"] == "Board meeting outcome"
    assert rec.data["symbol"] == "RELIANCE"


def test_nse_normalize_financial():
    s = NSEScraper()
    rec = s.normalize({
        "symbol": "RELIANCE",
        "priceInfo": {"lastPrice": 2950.50, "change": 12.5, "pChange": 0.43},
        "info": {"companyName": "Reliance Industries Ltd", "sector": "Energy", "industry": "Refineries"},
    }, "financial")
    assert rec is not None
    assert rec.data["last_price"] == 2950.50
    assert rec.data["company_name"] == "Reliance Industries Ltd"


def test_nse_normalize_unknown_type():
    s = NSEScraper()
    assert s.normalize({"foo": "bar"}, "unknown") is None
    assert s.normalize("not a dict", "announcement") is None


def test_bse_normalize_announcement():
    s = BSEScraper()
    rec = s.normalize({
        "title": "Dividend declared",
        "pubDate": "2025-10-15",
        "category": "Corporate Action",
        "scripcode": "500325",
        "symbol": "RELIANCE",
        "description": "Final dividend for FY2025",
    }, "announcement")
    assert rec is not None
    assert rec.source == "BSE"
    assert rec.data["title"] == "Dividend declared"
    assert rec.data["symbol"] == "RELIANCE"


def test_mca_normalize_company():
    s = MCAScraper()
    html = '<html>Company Name: Reliance Industries Limited<br>CIN: L17110MH1973PLC019786</html>'
    rec = s.normalize(html, "company")
    assert rec is not None
    assert rec.source == "MCA"
    assert rec.record_type == "company"
    assert rec.data["cin"] == "L17110MH1973PLC019786"


def test_mca_normalize_unknown_type():
    s = MCAScraper()
    assert s.normalize("x", "financial") is None


def test_scraper_ssrf_blocked():
    """A scraper must reject non-India hosts."""
    s = NSEScraper()
    try:
        s._validate_url("https://www.sec.gov/filings")
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "SSRF" in str(exc)

    s2 = BSEScraper()
    try:
        s2._validate_url("https://evil.com/x")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_google_finance_normalize():
    s = GoogleFinanceScraper()
    rec = s.normalize(
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "last_price": 1377.5,
            "company_name": "Reliance Industries Ltd",
            "currency": "INR",
        },
        "financial",
    )
    assert rec is not None
    assert rec.source == "Google Finance"
    assert rec.record_type == "financial"
    assert rec.source_id == "RELIANCE:NSE"
    assert rec.data["last_price"] == 1377.5
    assert rec.data["company_name"] == "Reliance Industries Ltd"


def test_google_finance_normalize_unknown_type():
    s = GoogleFinanceScraper()
    assert s.normalize({"foo": "bar"}, "unknown") is None
    assert s.normalize("not a dict", "financial") is None


def test_google_finance_ssrf_allowed():
    """Google Finance hosts must be on the SSRF allow-list."""
    s = GoogleFinanceScraper()
    # Should not raise — google.com is allow-listed.
    s._validate_url("https://www.google.com/finance/quote/RELIANCE:NSE")
    s._validate_url("https://google.com/finance/quote/RELIANCE:NSE")


def test_google_finance_ssrf_blocked():
    """Google Finance scraper must reject non-allow-listed hosts."""
    s = GoogleFinanceScraper()
    try:
        s._validate_url("https://www.sec.gov/filings")
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "SSRF" in str(exc)


def _test_scraper() -> _ConcreteScraper:
    return _ConcreteScraper(config=IndiaConfig(allowed_hosts=frozenset({"example.com"})))


def test_get_records_latency_and_snapshot_is_a_copy():
    s = _test_scraper()
    s._record_latency("example.com", 0.2)
    s._record_latency("example.com", 0.5)
    s._record_latency("other.com", 0.1)
    lats = s.endpoint_latencies()
    assert lats["example.com"] == [0.2, 0.5]
    assert lats["other.com"] == [0.1]
    # Mutating the returned snapshot must not corrupt stored samples.
    lats["example.com"].append(999.0)
    assert s.endpoint_latencies()["example.com"] == [0.2, 0.5]


def test_rate_limited_hosts_respects_cooldown():
    s = _test_scraper()
    now = time.monotonic()
    s._rate_limited_until["example.com"] = now + 100  # still cooling down
    s._rate_limited_until["expired.com"] = now - 1  # already cooled down
    rl = s.rate_limited_hosts()
    assert "example.com" in rl
    assert "expired.com" not in rl
    assert s.rate_limited_hosts() == rl  # snapshot stability


def test_select_endpoint_fastest_non_rate_limited_first():
    s = _test_scraper()
    s._record_latency("slow.com", 2.0)
    s._record_latency("fast.com", 0.1)
    ordered = s.select_endpoint(["http://slow.com/a", "http://fast.com/b"])
    assert ordered == ["http://fast.com/b", "http://slow.com/a"]


def test_select_endpoint_rate_limited_host_moved_last():
    s = _test_scraper()
    now = time.monotonic()
    s._rate_limited_until["fast.com"] = now + 100
    s._record_latency("fast.com", 0.1)
    s._record_latency("slow.com", 2.0)
    ordered = s.select_endpoint(["http://fast.com/a", "http://slow.com/b"])
    assert ordered == ["http://slow.com/b", "http://fast.com/a"]


def test_select_endpoint_deterministic_without_latency_data():
    s = _test_scraper()
    urls = ["http://b.com/a", "http://a.com/b"]
    # No latency measured yet -> stable, original URL order (deterministic).
    assert s.select_endpoint(urls) == urls


def test_google_finance_tries_nse_first_when_no_latency_data():
    s = GoogleFinanceScraper()
    calls: list[str] = []

    def fake(symbol: str, exchange: str):  # noqa: ARG005
        calls.append(exchange)
        return ScrapeResult(ok=True, records=[object()], source="Google Finance")

    s._fetch_quote = fake  # type: ignore[method-assign]
    s.fetch_financials("RELIANCE")
    assert calls == ["NSE"]


def test_google_finance_falls_back_to_bom_when_nse_fails():
    s = GoogleFinanceScraper()
    calls: list[str] = []

    def fake(symbol: str, exchange: str):  # noqa: ARG005
        calls.append(exchange)
        if exchange == "NSE":
            return ScrapeResult(ok=False, records=[], error="none", source="Google Finance")
        return ScrapeResult(ok=True, records=[object()], source="Google Finance")

    s._fetch_quote = fake  # type: ignore[method-assign]
    res = s.fetch_financials("RELIANCE")
    assert calls == ["NSE", "BOM"]
    assert res.ok
