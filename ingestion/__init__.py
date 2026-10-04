"""FinGraph Generic Multi-Company Ingestion Pipeline.

A unified ingestion architecture where Apple (AAPL), Tesla (TSLA), Microsoft (MSFT),
and future companies flow through the same pipeline via configuration.
"""

from .registry import Company, CompanyRegistry, get_registry
from .orchestrator import IngestionOrchestrator, IngestionConfig, run_ingestion
from .sec_acquisition import SECAcquisition, FilingManifest

__all__ = [
    "Company",
    "CompanyRegistry",
    "get_registry",
    "IngestionOrchestrator",
    "IngestionConfig",
    "SECAcquisition",
    "FilingManifest",
    "run_ingestion",
]