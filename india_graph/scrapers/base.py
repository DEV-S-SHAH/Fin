"""Base scraper interface and shared HTTP client for India sources.

All scrapers implement the same four-method interface so the graph builder can
treat NSE, BSE and MCA interchangeably:

    fetch_company(cin)          -> company master record
    fetch_announcements(cin)    -> list of announcement records
    fetch_financials(cin)       -> list of financial result records
    normalize(raw)              -> normalized ScrapedRecord dicts

HTTP is handled here: User-Agent rotation, jittered delays, Retry-After
respect, single jittered retry on 429, and SSRF allow-list validation.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from ..config import IndiaConfig


#: A single normalized record coming out of any scraper.
@dataclass
class ScrapedRecord:
    record_type: str          # "company", "announcement", "financial"
    source: str               # "NSE", "BSE", "MCA"
    source_id: str            # source-native id (NSE announcement number, MCA CIN, etc.)
    data: dict[str, Any]      # normalized fields
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")


#: Outcome of a scrape call.
@dataclass
class ScrapeResult:
    ok: bool
    records: list[ScrapedRecord] = field(default_factory=list)
    error: str | None = None
    source: str = ""
    http_status: int | None = None


class BaseScraper(ABC):
    """Abstract scraper for one India source (NSE, BSE or MCA)."""

    source_name: str = "BASE"
    allowed_hosts: tuple[str, ...] = ()

    def __init__(self, config: IndiaConfig | None = None) -> None:
        self.config = config or IndiaConfig()
        self._session = requests.Session()
        # Realistic browser UA pool — rotated per request to avoid trivial blocks.
        self._user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        ]
        self._last_request_time: float = 0.0
        self._min_delay_seconds: float = 1.0

    # -- HTTP ---------------------------------------------------------------

    def _validate_url(self, url: str) -> None:
        """SSRF guard: only allow-listed hosts may be fetched."""
        host = urlparse(url).hostname or ""
        if not self.config.is_allowed_host(host):
            raise ValueError(
                f"SSRF: host {host!r} is not on the India allow-list "
                f"({sorted(self.config.allowed_hosts)})"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(self._user_agents),
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        }

    def _jitter_delay(self) -> None:
        """Jittered delay between requests to avoid looking like a bot."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_delay_seconds:
            time.sleep(self._min_delay_seconds - elapsed + random.uniform(0, 1.0))
        self._last_request_time = time.monotonic()

    def _get(self, url: str, timeout: float = 10.0) -> requests.Response:
        """GET with SSRF check, UA rotation, jitter, and one jittered retry on 429."""
        self._validate_url(url)
        self._jitter_delay()
        attempt = 0
        while True:
            attempt += 1
            resp = self._session.get(url, headers=self._headers(), timeout=timeout)
            if resp.status_code == 429 and attempt == 1:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else random.uniform(2, 5)
                time.sleep(delay)
                continue
            return resp

    # -- abstract interface -------------------------------------------------

    @abstractmethod
    def fetch_company(self, cin: str) -> ScrapeResult:
        """Company master record for *cin* (MCA Corporate Identification Number)."""
        ...

    @abstractmethod
    def fetch_announcements(self, cin: str, limit: int = 50) -> ScrapeResult:
        """Recent announcements for the company identified by *cin*."""
        ...

    @abstractmethod
    def fetch_financials(self, cin: str, limit: int = 8) -> ScrapeResult:
        """Financial results (quarterly/annual) for the company identified by *cin*."""
        ...

    @abstractmethod
    def normalize(self, raw: Any, record_type: str) -> ScrapedRecord | None:
        """Normalize a source-native payload into a :class:`ScrapedRecord`."""
        ...
