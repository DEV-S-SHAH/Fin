"""Tests for India scrapers — normalization and error handling (no real network)."""

from india_graph.scrapers.bse import BSEScraper
from india_graph.scrapers.mca import MCAScraper
from india_graph.scrapers.nse import NSEScraper


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
