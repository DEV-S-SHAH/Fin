"""Tests for India Company Graph configuration."""

from india_graph.config import (
    DEFAULT_FYE_DAY,
    DEFAULT_FYE_MONTH,
    NIFTY_50,
    SESSION_TTL_SECONDS,
    SSRF_ALLOWED_HOSTS,
    IndiaConfig,
)


def test_ssrf_allowlist_india_only():
    """No US SEC hosts allowed — India sources only."""
    assert "www.sec.gov" not in SSRF_ALLOWED_HOSTS
    assert "data.sec.gov" not in SSRF_ALLOWED_HOSTS
    assert "www.nseindia.com" in SSRF_ALLOWED_HOSTS
    assert "www.bseindia.com" in SSRF_ALLOWED_HOSTS
    assert "www.mca.gov.in" in SSRF_ALLOWED_HOSTS
    assert "www.sebi.gov.in" in SSRF_ALLOWED_HOSTS


def test_config_is_allowed_host():
    cfg = IndiaConfig()
    assert cfg.is_allowed_host("www.nseindia.com")
    assert cfg.is_allowed_host("nseindia.com")
    assert cfg.is_allowed_host("www.mca.gov.in")
    assert not cfg.is_allowed_host("www.sec.gov")
    assert not cfg.is_allowed_host("evil.com")
    assert not cfg.is_allowed_host("192.168.1.1")


def test_default_fye_march_31():
    assert DEFAULT_FYE_MONTH == 3
    assert DEFAULT_FYE_DAY == 31


def test_session_ttl_30min():
    assert SESSION_TTL_SECONDS == 1800


def test_nifty_50_nonempty():
    assert len(NIFTY_50) > 0
    assert "RELIANCE" in NIFTY_50
    assert "TCS" in NIFTY_50


def test_config_defaults():
    cfg = IndiaConfig()
    assert cfg.fye_month == 3
    assert cfg.fye_day == 31
    assert cfg.session_ttl_seconds == 1800
