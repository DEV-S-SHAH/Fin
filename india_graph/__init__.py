"""India Company Graph — India-only financial GraphRAG product.

Gathers Indian company documents from NSE, BSE, MCA, and SEBI into a
just-in-time, per-session graph, answers natural-language questions with
provenance-tagged evidence, then destroys the graph when the session ends.

This package is India-only. The US SEC pipeline lives in sandbox_engine/,
graphrag/ and ingestion/ and is not used here.
"""

from .config import (
    DEFAULT_FYE,
    NIFTY_50,
    SESSION_TTL_SECONDS,
    SSRF_ALLOWED_HOSTS,
    IndiaConfig,
)
from .graph import SessionGraph
from .provenance import (
    DERIVED,
    EXTERNAL,
    GAP,
    INFERRED,
    QUALIFIED,
    REFUSED,
    STATED,
    SUPPORTED,
    Evidence,
    Source,
    build_evidence,
    grade_answer,
)
from .resolver import TickerCINResolver
from .session import SessionManager

__all__ = [
    "DEFAULT_FYE",
    "DERIVED",
    "EXTERNAL",
    "GAP",
    "INFERRED",
    "NIFTY_50",
    "QUALIFIED",
    "REFUSED",
    "SESSION_TTL_SECONDS",
    "SSRF_ALLOWED_HOSTS",
    "STATED",
    "SUPPORTED",
    "Evidence",
    "IndiaConfig",
    "SessionGraph",
    "SessionManager",
    "Source",
    "TickerCINResolver",
    "build_evidence",
    "grade_answer",
]

__version__ = "0.1.0"
