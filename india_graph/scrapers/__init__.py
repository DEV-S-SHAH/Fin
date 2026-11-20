"""India exchange / registry scrapers.

Each exchange (NSE, BSE, MCA) has its own module implementing the shared
:class:`BaseScraper` interface. NSE and BSE are the authoritative sources for
announcements and financials; MCA provides the canonical CIN key and master data.
"""

from .base import BaseScraper, ScrapedRecord, ScrapeResult

__all__ = ["BaseScraper", "ScrapeResult", "ScrapedRecord"]
