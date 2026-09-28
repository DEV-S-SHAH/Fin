"""Universal, domain-agnostic GraphRAG over arbitrary PDFs.

The package deliberately contains no domain vocabulary: no node types, no
relation types, and no seed data are hardcoded. Everything in the graph is
discovered by an LLM from the text of whatever PDF is supplied.
"""

from __future__ import annotations

from .config import GraphRAGConfig
from .ingest import IngestReport, ingest_pdf
from .qa import Answer, ask
from .store import GraphStore

__all__ = [
    "Answer",
    "GraphRAGConfig",
    "GraphStore",
    "IngestReport",
    "ask",
    "ingest_pdf",
]
