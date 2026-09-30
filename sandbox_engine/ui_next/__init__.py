"""A modern replacement front end for the sandbox knowledge graph.

This package is additive. It imports the backend from :mod:`sandbox_engine.query_ui`
and serves its own assets, so the original page keeps working untouched::

    python -m sandbox_engine.ui_next --port 9200

The only server-side additions are two read-only endpoints the old page did
not need: ``/api/companies`` and ``/api/route``. Everything else
— graph queries, the RAG pipeline, grading, canned reports, the SSE transport —
is inherited rather than reimplemented, so the two front ends cannot drift apart
in their answers.
"""

from .server import main, serve

__all__ = ["main", "serve"]
