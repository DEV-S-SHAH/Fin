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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
            + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            + "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        ]
        self._last_request_time: float = 0.0
        self._min_delay_seconds: float = 1.0
        # Per-host fetch latency (seconds) observed across responses. Used to
        # observe which endpoint is fastest and (via select_endpoint) to prefer
        # faster, non-rate-limited endpoints. Bounded to the most recent samples.
        self._endpoint_latencies: dict[str, list[float]] = {}
        self._max_latency_samples = 50
        # Hosts that returned 429 and their cool-down expiry (monotonic time).
        # While a host is cooling down it is deprioritized so we don't hammer a
        # rate-limited endpoint.
        self._rate_limited_until: dict[str, float] = {}

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
        """GET with SSRF check, UA rotation, jitter, and one jittered retry on 429.

        Records per-host fetch latency so callers can observe (and optionally
        prefer) the fastest endpoint, and enforces a short cool-down for hosts
        that returned 429 so a rate-limited host is not hammered on retry.
        """
        self._validate_url(url)
        host = urlparse(url).hostname or ""
        self._jitter_delay()
        attempt = 0
        while True:
            attempt += 1
            start = time.monotonic()
            try:
                resp = self._session.get(url, headers=self._headers(), timeout=timeout)
            finally:
                self._record_latency(host, time.monotonic() - start)
            if resp.status_code == 429 and attempt == 1:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else random.uniform(2, 5)
                self._rate_limited_until[host] = time.monotonic() + delay
                time.sleep(delay)
                continue
            return resp

    def _record_latency(self, host: str, duration: float) -> None:
        """Record an observed fetch latency for *host* (bounded sample list)."""
        samples = self._endpoint_latencies.setdefault(host, [])
        samples.append(duration)
        if len(samples) > self._max_latency_samples:
            del samples[: len(samples) - self._max_latency_samples]

    # -- endpoint selection -------------------------------------------------

    def endpoint_latencies(self) -> dict[str, list[float]]:
        """Snapshot of observed per-host fetch latencies (seconds)."""
        return {host: list(samples) for host, samples in self._endpoint_latencies.items()}

    def rate_limited_hosts(self) -> dict[str, float]:
        """Hosts currently in rate-limit cool-down: {host: expiry (monotonic time)}."""
        now = time.monotonic()
        return {
            host: expiry
            for host, expiry in self._rate_limited_until.items()
            if expiry > now
        }

    def select_endpoint(self, urls: list[str]) -> list[str]:
        """Return *urls* ordered for fetching: fastest non-rate-limited host first.

        Hosts with observed latency are sorted by average latency (lowest first);
        hosts currently in rate-limit cool-down are moved to the end. Hosts with
        no latency data keep their relative order after measured hosts, so the
        first call is deterministic (fixed URL order). This is a pure helper --
        it performs no I/O. Callers remain free to ignore the ordering and keep
        a fixed, deterministic preference.
        """
        now = time.monotonic()
        limited = {
            host for host, expiry in self._rate_limited_until.items() if expiry > now
        }

        def avg_latency(url: str) -> float:
            host = urlparse(url).hostname or ""
            samples = self._endpoint_latencies.get(host)
            if not samples:
                return float("inf")
            return sum(samples) / len(samples)

        def sort_key(url: str) -> tuple[bool, float]:
            host = urlparse(url).hostname or ""
            return (host in limited, avg_latency(url))

        return sorted(urls, key=sort_key)

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
