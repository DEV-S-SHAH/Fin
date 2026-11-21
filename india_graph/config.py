"""India Company Graph configuration — India-only, localhost-first.

No US SEC concepts appear here. Company identity is the MCA CIN (Corporate
Identification Number); NSE/BSE symbols and ISINs are aliases. Default fiscal
year end is March 31 (India's standard), overridable per company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: Hosts the scrapers are allowed to contact. SSRF protection: nothing outside
#: this list may be fetched, even on localhost.
SSRF_ALLOWED_HOSTS = {
    "www.nseindia.com",
    "nseindia.com",
    "www.bseindia.com",
    "bseindia.com",
    "www.mca.gov.in",
    "mca.gov.in",
    "www.sebi.gov.in",
    "sebi.gov.in",
    "www.google.com",
    "google.com",
}

#: Default fiscal year end for Indian companies (March 31).
DEFAULT_FYE_MONTH = 3
DEFAULT_FYE_DAY = 31
DEFAULT_FYE = (DEFAULT_FYE_MONTH, DEFAULT_FYE_DAY)

#: Per-session graph lifetime: destroyed after 30 min of inactivity.
SESSION_TTL_SECONDS = 1800

#: Day-one launch scope: NIFTY 50.
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "BAJFINANCE", "KOTAKBANK", "LT",
    "AXISBANK", "ASIANPAINT", "HCLTECH", "MARUTI", "SUNPHARMA",
    "TITAN", "TATAMOTORS", "ULTRACEMCO", "NESTLEIND", "WIPRO",
    "M&M", "NTPC", "TECHM", "POWERGRID", "TATACONSUM", "ADANIENT",
    "ADANIPORTS", "BAJAJ-AUTO", "BAJFINANCE", "BPCL", "BRITANNIA",
    "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "GRASIM", "HEROMOTOCO", "HINDALCO", "HDFCLIFE", "ICICIPRULI",
    "INDUSINDBK", "JSWSTEEL", "ONGC", "SBILIFE", "SHRIRAMFIN",
    "TATACONSUM", "TATAMOTORS", "TATAPOWER", "TATASTEEL",
    "UPL", "VEDL",
]
# De-duplicate while preserving order
NIFTY_50 = list(dict.fromkeys(NIFTY_50))

#: Cache file for the ticker -> CIN resolver mapping (metadata only, refreshed
#: weekly — not user data, not per-session).
RESOLVER_CACHE_PATH = Path("data/india/ticker_cin_cache.json")

#: Directory for per-session ephemeral artifacts (none persisted between sessions).
SESSION_DATA_DIR = Path("data/india/sessions")


@dataclass(frozen=True)
class IndiaConfig:
    """Configuration for the India Company Graph product."""

    fye_month: int = DEFAULT_FYE_MONTH
    fye_day: int = DEFAULT_FYE_DAY
    session_ttl_seconds: int = SESSION_TTL_SECONDS
    allowed_hosts: frozenset[str] = frozenset(SSRF_ALLOWED_HOSTS)
    resolver_cache_path: Path = RESOLVER_CACHE_PATH
    session_data_dir: Path = SESSION_DATA_DIR
    launch_scope: list[str] = field(default_factory=lambda: list(NIFTY_50))

    def is_allowed_host(self, host: str) -> bool:
        """True if *host* is on the SSRF allow-list."""
        return host.lower() in {h.lower() for h in self.allowed_hosts}
