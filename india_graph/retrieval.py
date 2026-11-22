"""Deterministic evidence retrieval over the in-memory session graph.

Given a natural-language question and a built SessionGraph, score each
node by keyword overlap with the question and return the top matches as
Evidence. This is the retrieval half of the Q&A loop; provenance tags
(STATED by default) are assigned by build_evidence, never by the model.

No LLM is required. If an LLM key is configured later, this module's
output can be handed to a synthesis step — but the retrieval itself stays
deterministic so answers remain verifiable and grounded.
"""

from __future__ import annotations

import re
from typing import Any

from .graph import SessionGraph
from .provenance import Evidence, build_evidence

#: Common stop words dropped before keyword scoring.
_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "to", "of", "in", "for", "on", "at",
    "by", "with", "from", "as", "or", "and", "what", "which", "who",
    "where", "when", "how", "tell", "me", "give", "show", "about",
    "reliance", "tcs", "infy", "wipro", "hdfcbank", "icicibank",
}

#: Question intent hints mapped to node fields they should boost.
_INTENT_HINTS = {
    "price": ("metric_name", ("last price", "quote")),
    "last price": ("metric_name", ("last price", "quote")),
    "revenue": ("metric_name", ("revenue",)),
    "profit": ("metric_name", ("profit", "pat", "profit_after_tax")),
    "eps": ("metric_name", ("eps",)),
    "board": ("category", ("board",)),
    "dividend": ("category", ("dividend",)),
    "announcement": ("category", ()),
    "filing": ("form_type", ()),
    "result": ("form_type", ()),
}


def _keywords(question: str) -> list[str]:
    """Return lowercased content keywords from *question*."""
    words = re.findall(r"[A-Za-z0-9]+", question.lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def _score_node(node: dict[str, Any], keywords: list[str]) -> int:
    """Relevance score of a graph node for the question keywords."""
    score = 0
    text_fields = [
        str(node.get("name", "")),
        str(node.get("label", "")),
        str(node.get("title", "")),
        str(node.get("metric_name", "")),
        str(node.get("category", "")),
        str(node.get("form_type", "")),
        str(node.get("source", "")),
        str(node.get("ticker", "")),
        str(node.get("cin", "")),
    ]
    blob = " ".join(text_fields).lower()
    for kw in keywords:
        if kw in blob:
            score += 1
    # Intent hints: boost exact matches on the most relevant field.
    for kw in keywords:
        field, values = _INTENT_HINTS.get(kw, (None, ()))
        if field:
            field_val = str(node.get(field, "")).lower()
            if any(v in field_val for v in values):
                score += 2
    return score


def retrieve(
    graph: SessionGraph,
    question: str,
    top_k: int = 6,
) -> list[Evidence]:
    """Return the top-*top_k* Evidence nodes most relevant to *question*.

    Nodes are scored by keyword overlap with their labels/titles/metric
    names. Only nodes with a positive score are returned when keywords match.
    When the question is generic (no keywords match), all nodes are
    returned in graph order up to *top_k* so the user still gets a picture
    of what's available.
    """
    keywords = _keywords(question)
    nodes = graph.as_nodes()
    edges = graph.as_edges()
    tag_map = graph.tag_map()

    scored = [( _score_node(n, keywords), i, n) for i, n in enumerate(nodes)]
    scored.sort(key=lambda t: (-t[0], t[1]))

    if keywords and scored and scored[0][0] > 0:
        # Only include nodes with positive relevance scores
        selected = [n for score, _, n in scored[:top_k] if score > 0]
    else:
        selected = nodes[:top_k]

    return build_evidence(selected, edges, tag_map)
