"""Company Registry - Central configuration for all companies in FinGraph.

The registry contains company-specific metadata ONLY. No business logic.
All ingestion logic is generic and uses this configuration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

__all__ = [
    "FiscalCalendar",
    "Company",
    "CompanyRegistry",
    "get_registry",
    "DEFAULT_REGISTRY_PATH",
]

# Default registry location
DEFAULT_REGISTRY_PATH = Path("config/companies.json")


@dataclass(frozen=True)
class FiscalCalendar:
    """Fiscal year configuration for a company."""
    
    # Month when fiscal year ends (1-12)
    year_end_month: int
    
    # Day when fiscal year ends (1-31)
    year_end_day: int
    
    # Whether the fiscal year is designated by the year it ends in
    # e.g., MSFT FY2025 ends June 30, 2025
    fiscal_year_is_calendar_year_of_end: bool = True
    
    def fiscal_year_for_date(self, d: date) -> int:
        """Calculate fiscal year for a given calendar date.
        
        The fiscal year is named by the calendar year in which it ENDS.
        For MSFT (year ends June 30):
        - June 30, 2025 -> FY2025 (last day of FY2025)
        - July 1, 2025 -> FY2026 (first day of FY2026)
        """
        if d.month > self.year_end_month or (
            d.month == self.year_end_month and d.day > self.year_end_day
        ):
            return d.year + (1 if self.fiscal_year_is_calendar_year_of_end else 0)
        return d.year + (0 if self.fiscal_year_is_calendar_year_of_end else -1)
    
    def fiscal_quarter_for_date(self, d: date) -> int:
        """Calculate fiscal quarter (1-4) for a given calendar date."""
        # Months since fiscal year start
        months_since_start = (d.month - self.year_end_month - 1) % 12 + 1
        if d.month == self.year_end_month and d.day >= self.year_end_day:
            months_since_start = 12
        return (months_since_start - 1) // 3 + 1
    
    def fiscal_period_label(self, d: date) -> str:
        """Return fiscal period label like 'FY2025-Q1'."""
        fy = self.fiscal_year_for_date(d)
        fq = self.fiscal_quarter_for_date(d)
        return f"FY{fy}-Q{fq}"


@dataclass(frozen=True)
class Company:
    """Company configuration - metadata only, no business logic."""
    
    ticker: str
    name: str
    cik: str
    fiscal_calendar: FiscalCalendar
    
    # Source configuration (optional overrides)
    sec_forms: list[str] = field(default_factory=lambda: [
        "10-K", "10-Q", "8-K", "DEF 14A", "3", "4", "5",
        "13F-HR", "SC 13D", "SC 13G", "S-3", "S-8", "424B",
        "ARS", "SD", "11-K"
    ])
    
    # Whether this company is active for ingestion
    active: bool = True
    
    # Custom source URLs (if different from standard SEC)
    investor_relations_url: str = ""
    sec_filings_url: str = ""
    
    # Additional metadata
    sector: str = ""
    industry: str = ""
    exchange: str = "NASDAQ"
    
    def __post_init__(self) -> None:
        # Normalize CIK to 10-digit zero-padded
        object.__setattr__(self, "cik", str(self.cik).zfill(10))
        object.__setattr__(self, "ticker", self.ticker.upper())
    
    @property
    def cik_no_leading_zeros(self) -> str:
        """CIK without leading zeros for SEC API calls."""
        return str(int(self.cik))
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "cik": self.cik,
            "fiscal_calendar": {
                "year_end_month": self.fiscal_calendar.year_end_month,
                "year_end_day": self.fiscal_calendar.year_end_day,
                "fiscal_year_is_calendar_year_of_end": self.fiscal_calendar.fiscal_year_is_calendar_year_of_end,
            },
            "sec_forms": self.sec_forms,
            "active": self.active,
            "investor_relations_url": self.investor_relations_url,
            "sec_filings_url": self.sec_filings_url,
            "sector": self.sector,
            "industry": self.industry,
            "exchange": self.exchange,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Company":
        fiscal_data = data.get("fiscal_calendar", {})
        fiscal_cal = FiscalCalendar(
            year_end_month=fiscal_data.get("year_end_month", 12),
            year_end_day=fiscal_data.get("year_end_day", 31),
            fiscal_year_is_calendar_year_of_end=fiscal_data.get("fiscal_year_is_calendar_year_of_end", True),
        )
        return cls(
            ticker=data["ticker"],
            name=data["name"],
            cik=data["cik"],
            fiscal_calendar=fiscal_cal,
            sec_forms=data.get("sec_forms", []),
            active=data.get("active", True),
            investor_relations_url=data.get("investor_relations_url", ""),
            sec_filings_url=data.get("sec_filings_url", ""),
            sector=data.get("sector", ""),
            industry=data.get("industry", ""),
            exchange=data.get("exchange", "NASDAQ"),
        )


class CompanyRegistry:
    """Registry of all companies known to FinGraph.
    
    This is the single source of truth for company metadata.
    The ingestion pipeline uses this to resolve company-specific configuration.
    """
    
    def __init__(self, companies: dict[str, Company] | None = None) -> None:
        self._companies: dict[str, Company] = companies or {}
    
    def register(self, company: Company) -> None:
        """Register a company in the registry."""
        self._companies[company.ticker] = company
    
    def get(self, ticker: str) -> Company | None:
        """Get company by ticker (case-insensitive)."""
        return self._companies.get(ticker.upper())
    
    def get_by_cik(self, cik: str) -> Company | None:
        """Get company by CIK."""
        cik_padded = str(cik).zfill(10)
        for company in self._companies.values():
            if company.cik == cik_padded:
                return company
        return None
    
    def all(self) -> list[Company]:
        """Get all registered companies."""
        return list(self._companies.values())
    
    def active(self) -> list[Company]:
        """Get only active companies."""
        return [c for c in self._companies.values() if c.active]
    
    def tickers(self) -> list[str]:
        """Get all registered tickers."""
        return list(self._companies.keys())
    
    def __contains__(self, ticker: str) -> bool:
        return ticker.upper() in self._companies
    
    def __len__(self) -> int:
        return len(self._companies)
    
    def to_json(self, path: Path) -> None:
        """Save registry to JSON file."""
        data = {ticker: company.to_dict() for ticker, company in self._companies.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    
    @classmethod
    def from_json(cls, path: Path) -> "CompanyRegistry":
        """Load registry from JSON file."""
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        companies = {ticker: Company.from_dict(comp_data) for ticker, comp_data in data.items()}
        return cls(companies)


# Default company configurations
# These use authoritative CIKs from SEC EDGAR
DEFAULT_COMPANIES = {
    "AAPL": Company(
        ticker="AAPL",
        name="Apple Inc.",
        cik="0000320193",
        fiscal_calendar=FiscalCalendar(
            year_end_month=9,
            year_end_day=30,
            fiscal_year_is_calendar_year_of_end=True,
        ),
        sec_forms=[
            "10-K", "10-Q", "8-K", "DEF 14A", "3", "4", "5",
            "13F-HR", "SC 13D", "SC 13G", "S-3", "S-8", "424B",
            "ARS", "SD", "11-K"
        ],
        investor_relations_url="https://investor.apple.com/",
        sec_filings_url="https://investor.apple.com/sec-filings/",
        sector="Technology",
        industry="Consumer Electronics",
        exchange="NASDAQ",
    ),
    "TSLA": Company(
        ticker="TSLA",
        name="Tesla, Inc.",
        cik="0001318605",
        fiscal_calendar=FiscalCalendar(
            year_end_month=12,
            year_end_day=31,
            fiscal_year_is_calendar_year_of_end=True,
        ),
        sec_forms=[
            "10-K", "10-Q", "8-K", "DEF 14A", "3", "4", "5",
            "13F-HR", "SC 13D", "SC 13G", "S-3", "S-8", "424B",
            "ARS", "SD", "11-K"
        ],
        investor_relations_url="https://ir.tesla.com/",
        sec_filings_url="https://ir.tesla.com/sec-filings",
        sector="Consumer Cyclical",
        industry="Auto Manufacturers",
        exchange="NASDAQ",
    ),
    "MSFT": Company(
        ticker="MSFT",
        name="Microsoft Corporation",
        cik="0000789019",
        fiscal_calendar=FiscalCalendar(
            year_end_month=6,
            year_end_day=30,
            fiscal_year_is_calendar_year_of_end=True,
        ),
        sec_forms=[
            "10-K", "10-Q", "8-K", "DEF 14A", "3", "4", "5",
            "13F-HR", "SC 13D", "SC 13G", "S-3", "S-8", "424B",
            "ARS", "SD", "11-K"
        ],
        investor_relations_url="https://www.microsoft.com/en-us/investor/",
        sec_filings_url="https://www.microsoft.com/en-us/investor/sec-filings",
        sector="Technology",
        industry="Software - Infrastructure",
        exchange="NASDAQ",
    ),
}


# Module-level registry instance
_registry: CompanyRegistry | None = None


def get_registry(path: Path | None = None) -> CompanyRegistry:
    """Get the global company registry, initializing from defaults or file."""
    global _registry
    if _registry is None:
        if path and path.exists():
            _registry = CompanyRegistry.from_json(path)
        else:
            _registry = CompanyRegistry(DEFAULT_COMPANIES)
            # Save defaults if path provided
            if path:
                _registry.to_json(path)
    return _registry


def reset_registry() -> None:
    """Reset the global registry (mainly for testing)."""
    global _registry
    _registry = None