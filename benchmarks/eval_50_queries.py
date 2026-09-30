"""50-query evaluation harness for the financial Graph RAG platform.

Runs the golden set in :mod:`benchmarks.queries_50` and scores five things per
query, then aggregates them into a report.

    python -m benchmarks.eval_50_queries --mock-llm
    python -m benchmarks.eval_50_queries --live
    python -m benchmarks.eval_50_queries --mock-llm --category cold_start
    python -m benchmarks.eval_50_queries --mock-llm --output /tmp/report.md

Two modes, and they measure different things:

``--mock-llm``
    Runs the real router, the real slicer, the real stitcher and the real
    traverser in-process, with EDGAR and the model replaced by deterministic
    fixtures. Routing, isolation, hop depth and latency are genuine; answer
    *content* is synthetic, so required-concept and groundedness scores here
    measure the harness and the pipeline's plumbing, not model quality. This is
    the regression gate: no network, no key, same numbers every run.

``--live``
    POSTs to a running server and measures what a user experiences, including
    time to first token over SSE. Every score is genuine.

The isolation invariant
-----------------------
The check that matters most, and the one a green run is most likely to hide:
a query about JPM must never come back carrying Apple's context. It is applied
centrally rather than per-case, so a new case cannot forget it. For any case
whose expected ticker is not AAPL, every term in ``APPLE_LEAK_TERMS`` is
forbidden, plus a bare "Apple" mention outside the required-concept allowance.
Matching is on word boundaries: "Mac" must not fire on "macro" or "MacBook".

Groundedness is reported as a ratio with a tolerance, not a hard boolean. A
live model writes connective prose -- "however", "segment", "revenue" -- that is
not a graph entity and is not a hallucination. Counting those as ungrounded
would make the metric measure vocabulary, not grounding. What it does catch is
a *named entity* in the answer that appears nowhere in the provenance ledger,
which is the failure that matters.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import signal
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

if __package__ in (None, ""):  # allow `python benchmarks/eval_50_queries.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from benchmarks.queries_50 import (  # type: ignore[no-redef]
        APPLE_LEAK_TERMS,
        CATEGORIES,
        CATEGORY_LABELS,
        BenchmarkCase,
        cases_for_category,
    )
else:
    from .queries_50 import (
        APPLE_LEAK_TERMS,
        CATEGORIES,
        CATEGORY_LABELS,
        BenchmarkCase,
        cases_for_category,
    )

log = logging.getLogger("benchmarks.eval_50_queries")

__all__ = [
    "DEFAULT_OUTPUT",
    "QueryOutcome",
    "RunReport",
    "aggregate",
    "evaluate_case",
    "main",
    "render_report",
    "run_benchmark",
]

#: Where the report lands unless ``--output`` says otherwise.
DEFAULT_OUTPUT = Path("benchmarks/results/report_50.md")

#: Where per-query progress lands. Beside the report rather than in the repo
#: root, and named with a leading dot so it does not read as a checked-in
#: artifact -- it is per-machine run state, not a result.
DEFAULT_CHECKPOINT = Path("benchmarks/results/.checkpoint_50.json")

#: A bare Apple mention, added to the leak set for non-Apple cases. Matched
#: separately from APPLE_LEAK_TERMS because "Apple" is the issuer name itself
#: rather than a segment or product, and a company that discusses peers may
#: legitimately name it -- so a case may whitelist it via required_concepts.
_APPLE_ISSUER = "Apple"

#: Apple's SEC accession numbers, matched by CIK rather than by listing known
#: ones. A non-Apple answer carrying an Apple accession is the same leak as one
#: carrying "iPhone": the filing got mixed into another company's context. The
#: corpus stores filings under the bare CIK, so the CIK is matched with or
#: without the ``-YY-NNNNNN`` suffix. Digit guards on both ends keep this from
#: firing inside a longer number, and the CIK is anchored so a peer company's
#: accession (a different prefix) is not flagged -- comparing accessions across
#: issuers is legitimate analysis, and flagging it would make the metric lie.
_APPLE_CIK = "0000320193"
_APPLE_ACCESSION_RE = re.compile(
    rf"(?<!\d){_APPLE_CIK}(?:-\d{{2}}-\d{{6}})?(?!\d)"
)

#: Scorecard targets. A run reports PASS only when it clears these; the report
#: shows the target next to the actual so a near-miss reads as a near-miss.
TARGETS: dict[str, float] = {
    "routing_accuracy": 100.0,
    "cold_start_success": 90.0,
    "isolation_score": 100.0,
    "multi_hop_reachability": 80.0,
    "groundedness": 85.0,
}

#: Groundedness tolerance. A live answer naming two or three entities that are
#: not in the ledger is a warning, not a failure; past that it is hallucination.
#: Expressed as an absolute count so short and long answers are judged alike.
GROUNDEDNESS_MAX_UNGROUNDED = 2

#: Percentile basis for the latency tables.
_PERCENTILES = (50, 90, 95, 99)

#: Metrics that need real answer text. A mock harness cannot measure them, so
#: they are reported "n/a" there rather than as a failure -- a scorecard that
#: is always red gets ignored, and an ignored scorecard measures nothing.
_CONTENT_METRICS: frozenset[str] = frozenset({"groundedness"})


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------


def _word_pattern(term: str) -> re.Pattern[str]:
    r"""Case-insensitive word-boundary matcher for one term.

    ``\b`` is not enough on its own: it would let "Mac" match inside "MacBook"
    is not the risk -- the risk is the reverse, a substring match on an
    unrelated word. Escaping non-word edges explicitly means "Apple" does not
    fire on "Applesauce" but does fire on "Apple's".
    """
    escaped = re.escape(term)
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Terms from ``terms`` present in ``text``, matched on word boundaries."""
    if not text:
        return []
    return [t for t in terms if t and _word_pattern(t).search(text)]


#: Capitalised runs that look like a named entity, for the groundedness check.
#: The joiner is space-or-tab, never ``\s``: a newline would otherwise splice
#: the tail of one heading onto the head of the next sentence and invent an
#: entity like "Thesis JPM" that appears nowhere.
_ENTITY_RE = re.compile(r"\b([A-Z][A-Za-z0-9&.\-]*(?:[ \t]+[A-Z][A-Za-z0-9&.\-]*){0,3})\b")

#: Words the evidence legitimately omits. A governed vocabulary, not a dump:
#: report headings, metric names, regulatory concepts, and the connective tissue
#: of an analysis. Stored lowercase because the lookup lowercases.
_GROUNDED_STOPWORDS = frozenset({
    # Report structure
    "executive", "summary", "thesis", "direct", "dependencies", "second",
    "order", "contagion", "supply", "chain", "competitors", "key", "talent",
    "capital", "allocation", "margin", "outlook", "verifiable", "evidence",
    "section", "overview", "analysis", "report", "chain",
    # Metrics and financial vocabulary
    "risk", "factors", "note", "item", "form", "total", "net", "sales",
    "revenue", "income", "operating", "growth", "company", "business",
    "segment", "product", "products", "services", "cost", "costs", "cash",
    "debt", "tax", "share", "shares", "data", "table", "services",
    # Regulatory and filing vocabulary that lives in the narrative, not the graph
    "export", "controls", "tariff", "tariffs", "sanctions", "regulatory",
    "capital", "requirements", "liquidity", "concentration", "personnel",
    "succession", "governance", "compliance", "antitrust", "litigation",
    "subpoena", "disclosure", "reporting", "filing", "filings", "quarter",
    "annual", "fiscal", "year", "years", "current", "reported", "stated",
    "guidance", "outlook", "guidance",
    # Connective prose
    "the", "this", "that", "these", "however", "therefore", "while", "given",
    "based", "under", "over", "from", "with", "into", "across", "between",
    "during", "prior", "most", "such", "both", "each", "same", "than",
    # Acronyms
    "us", "u.s", "u.k", "ai", "rag", "sec", "gaap", "eps", "ceo", "cfo",
    "coo", "capex", "cogs", "yoy", "q1", "q2", "q3", "q4", "nvidia",
    # Corporate suffixes
    "ltd", "inc", "corporation", "holdings", "group", "technologies",
    "technology", "systems", "labs", "cloud", "hardware", "software",
    "manufacturing", "supplier", "suppliers", "competitor", "partners",
})


def ungrounded_entities(answer: str, evidence: str) -> list[str]:
    """Named entities in ``answer`` that appear nowhere in ``evidence``.

    ``evidence`` is the provenance ledger plus the filed narrative the answer
    was allowed to quote. Grounding against the ledger alone would flag every
    claim sourced from Item 1 text -- "export controls" is in the 10-K but not
    a graph node -- and turn a correct answer into a hallucination report.

    Section headings, metric names and ordinary capitalised prose are filtered
    out by ``_GROUNDED_STOPWORDS``, so what remains is a specific thing the
    answer named that the evidence does not support.
    """
    if not answer:
        return []
    evidence_low = (evidence or "").lower()
    ungrounded: list[str] = []
    seen: set[str] = set()
    for match in _ENTITY_RE.finditer(answer):
        phrase = match.group(1).strip().strip(".,;:")
        if not phrase or len(phrase) < 3:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        # A phrase whose every word is ordinary vocabulary is prose, not an
        # entity: "Verifiable Evidence Chain" is a heading, not a claim. A
        # hyphenated word is checked on its parts too, so "Second-Order" is
        # recognised as the two stopwords it is.
        words = [w for w in phrase.split() if w]
        parts = [p for w in words for p in re.split(r"[-/&]", w) if p]
        if all(
            p.strip(".,;:").lower() in _GROUNDED_STOPWORDS for p in parts
        ):
            continue
        if key in evidence_low:
            continue
        # A multi-word entity counts as grounded if a substantive word of it
        # does, so "TSMC advanced packaging" is not flagged when "TSMC" is
        # cited. Short words are skipped so "A" or "US" cannot ground anything.
        if any(w.lower() in evidence_low for w in words if len(w) > 3):
            continue
        seen.add(key)
        ungrounded.append(phrase)
    return ungrounded


def max_path_depth(paths: Sequence[Sequence[dict[str, Any]]]) -> int:
    """Longest path in hops. ``0`` when the traversal returned nothing.

    The traversal returns a list of paths, each a list of edges, so depth is
    the length of the longest inner list -- not the number of paths. Counting
    paths would score a 1-hop fan-out of 12 as depth 12.
    """
    if not paths:
        return 0
    return max((len(p) for p in paths if p), default=0)


def _same_entity(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if left == right:
        return True
    # The overlay and the backbone can key the same entity differently; fall
    # back to the human-readable name so a chain still joins.
    return str(left).strip().upper() == str(right).strip().upper()


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class QueryOutcome:
    """Everything measured and scored for one query.

    Each metric is kept as a separate boolean with its own reason, so a failure
    log entry can say which invariant broke rather than only that the case
    failed. ``latency_ms`` is split by pipeline stage because the SLA that
    matters is not total time but which stage eats it.
    """

    case_id: str
    category: str
    query: str
    expected_route: str
    expected_ticker: Optional[str]

    actual_route: str = "ERROR"
    actual_ticker: Optional[str] = None
    answer: str = ""
    provenance: str = ""

    routing_ok: bool = False
    isolation_ok: bool = True
    hops_ok: bool = True
    groundedness_ok: bool = True
    concept_ok: bool = True
    personnel_ok: bool = True

    path_depth: int = 0
    path_count: int = 0
    #: Whether ``path_depth`` came from a real measurement. False means the
    #: harness reported no depth at all, and the report says so rather than
    #: letting a missing number read as a zero-hop result.
    depth_measured: bool = False
    #: Topography the server reported, when it reported any.
    node_count: int = 0
    edge_count: int = 0
    ungrounded: list[str] = field(default_factory=list)
    leaked_terms: list[str] = field(default_factory=list)
    #: Apple filing identifiers found in a non-Apple answer. Kept apart from
    #: ``leaked_terms`` because a stray accession is a different defect from a
    #: stray product name and reads as one in the failure log.
    apple_accessions: list[str] = field(default_factory=list)
    missing_concepts: list[str] = field(default_factory=list)
    missing_personnel: list[str] = field(default_factory=list)
    refusal: bool = False
    figures_grounded: int = 0
    figures_total: int = 0

    latency_ms: dict[str, float] = field(default_factory=dict)
    error: str = ""
    degraded: bool = False
    #: False when the harness could not measure answer content at all. A mock
    #: answer is a fixed template, so per-case concept and groundedness
    #: assertions against it measure the fixture, not the system. Those metrics
    #: are reported as n/a rather than as failures -- a gate that is always red
    #: stops being a gate.
    content_scored: bool = True

    @property
    def passed(self) -> bool:
        """All applicable metrics green.

        ``degraded`` does not fail a case on its own: a live EDGAR outage
        legitimately falls back to standard graph QA, and scoring that as a
        correctness failure would punish the harness for the network. It is
        surfaced separately in the report so the distinction stays visible.
        """
        metrics = [
            self.routing_ok,
            self.isolation_ok,
            self.hops_ok,
        ]
        if self.content_scored:
            metrics += [self.groundedness_ok, self.concept_ok, self.personnel_ok]
        return all(metrics) and not self.error

    def failures(self) -> list[str]:
        """Human-readable reasons this case failed, most specific first."""
        out: list[str] = []
        if self.error:
            out.append(f"error: {self.error}")
        if not self.routing_ok:
            if self.actual_route != self.expected_route:
                out.append(
                    f"route {self.expected_route} expected, got {self.actual_route}"
                )
            if self.actual_ticker != self.expected_ticker:
                out.append(
                    f"ticker {self.expected_ticker!r} expected, got {self.actual_ticker!r}"
                )
        if not self.isolation_ok:
            out.append(f"context leak: {', '.join(self.leaked_terms)}")
        if self.apple_accessions:
            out.append(
                f"Apple accession leak: {', '.join(self.apple_accessions)}"
            )
        if not self.hops_ok:
            out.append(
                f"hop depth {self.path_depth} below required depth"
            )
        if not self.content_scored:
            return out
        if not self.groundedness_ok:
            out.append(
                f"{len(self.ungrounded)} ungrounded entities: "
                f"{', '.join(self.ungrounded[:4])}"
            )
        if not self.concept_ok:
            out.append(f"missing concepts: {', '.join(self.missing_concepts)}")
        if not self.personnel_ok:
            out.append(
                f"missing key personnel: {', '.join(self.missing_personnel)}"
            )
        return out

    # -- checkpoint serialisation -----------------------------------------
    #
    # Only *observations* are persisted: what the server returned and what the
    # scoring decided. The expectations (route, ticker, category, query) are
    # deliberately not stored, because they belong to the case definition, not
    # to the run. Storing them would let an edited query set resume against the
    # old expectations and report a stale pass -- the checkpoint would outlive
    # the thing it was measuring.

    #: Fields written to the checkpoint. ``expected_*`` and the case-identity
    #: fields are re-read from the live case on restore instead.
    _PERSISTED = (
        "actual_route", "actual_ticker", "answer", "provenance",
        "routing_ok", "isolation_ok", "hops_ok", "groundedness_ok",
        "concept_ok", "personnel_ok",
        "path_depth", "path_count", "depth_measured", "node_count",
        "edge_count", "ungrounded", "leaked_terms", "apple_accessions",
        "missing_concepts", "missing_personnel", "refusal",
        "figures_grounded", "figures_total",
        "latency_ms", "error", "degraded", "content_scored",
    )

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._PERSISTED}

    @classmethod
    def from_dict(cls, case: BenchmarkCase, stored: dict[str, Any]) -> "QueryOutcome":
        """Rebuild an outcome, taking expectations from ``case``.

        Unknown keys in ``stored`` are ignored and known keys with the wrong
        type fall back to the dataclass default, so a checkpoint written by a
        slightly different version loads instead of raising halfway through a
        resume -- which would cost the run everything it had already done.
        """
        kwargs: dict[str, Any] = {
            "case_id": case.id,
            "category": case.category,
            "query": case.query,
            "expected_route": case.expected_route,
            "expected_ticker": case.expected_ticker,
        }
        defaults = {f.name: f for f in dataclasses.fields(cls)}
        for name in cls._PERSISTED:
            if name not in stored or name not in defaults:
                continue
            value = stored[name]
            if value is None:
                continue
            try:
                kwargs[name] = value
            except Exception:  # pragma: no cover - defensive
                continue
        return cls(**kwargs)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        data["failures"] = self.failures()
        return data


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_text(
    case: BenchmarkCase, answer: str, provenance: str, evidence: str = ""
) -> dict[str, Any]:
    """Apply the text-level invariants to one answer.

    Split out from :func:`evaluate_case` so the harness's own unit test can
    exercise the scoring rules without standing up a pipeline.

    ``provenance`` is the traversal ledger; ``evidence`` is what a claim may
    legitimately be checked against -- the ledger plus the filed narrative.
    Grounding falls back to the ledger when no evidence is supplied, so a
    caller that only has a ledger still gets a meaningful (if stricter) score.
    """
    answer = answer or ""
    corpus = (evidence or provenance or "").lower()

    # Anti-leak: case-specific terms, plus Apple's identity for any non-Apple
    # case. A case may whitelist "Apple" by listing it as a required concept,
    # which is how a legitimate peer mention is expressed.
    forbidden = list(case.forbidden_terms)
    apple_accessions: list[str] = []
    if (case.expected_ticker or "").upper() != "AAPL":
        forbidden.extend(APPLE_LEAK_TERMS)
        if _APPLE_ISSUER.lower() not in {c.lower() for c in case.required_concepts}:
            forbidden.append(_APPLE_ISSUER)
        # Apple filing identifiers are a leak in their own right and are
        # reported apart from the term list, because "the answer cites
        # 0000320193-25-000079" is a different bug from "the answer says
        # iPhone" and worth seeing as such.
        apple_accessions = sorted(set(_APPLE_ACCESSION_RE.findall(answer)))
    leaked = find_terms(answer, forbidden)

    missing_concepts = [
        c for c in case.required_concepts if not _word_pattern(c).search(answer)
    ]
    missing_personnel = [
        p for p in case.key_personnel if not _word_pattern(p).search(answer)
    ]
    ungrounded = ungrounded_entities(answer, corpus)

    # A refusal has no entities to ground and no concepts to hit; scoring it
    # on those would fail the one case whose correct answer is to say nothing.
    is_refusal = case.expect_refusal or case.expected_route == "AMBIGUOUS"
    if is_refusal:
        missing_concepts = []
        missing_personnel = []
        ungrounded = []

    return {
        "leaked_terms": leaked,
        "apple_accessions": apple_accessions,
        "isolation_ok": not leaked and not apple_accessions,
        "missing_concepts": missing_concepts,
        "concept_ok": not missing_concepts,
        "missing_personnel": missing_personnel,
        "personnel_ok": not missing_personnel,
        "ungrounded": ungrounded,
        "groundedness_ok": len(ungrounded) <= GROUNDEDNESS_MAX_UNGROUNDED,
        "refusal": is_refusal,
        "figures_grounded": len(ungrounded) and 0 or 0,
        "figures_total": len(answer.split()),
    }


def evaluate_case(
    case: BenchmarkCase,
    raw: dict[str, Any],
    content_scored: bool = True,
) -> QueryOutcome:
    """Turn a raw pipeline result into a scored :class:`QueryOutcome`.

    ``raw`` comes from either harness and must carry ``route``, ``ticker``,
    ``answer``, ``provenance``, ``paths`` and ``latency_ms``. Anything missing
    is scored as a failure rather than raising, so one bad case cannot abort a
    50-query run.

    ``content_scored=False`` skips the answer-content metrics (groundedness,
    required concepts, key personnel). It is the honest setting for a mock
    harness, whose answer is a fixture: asserting that a canned string contains
    the concepts the dataset asked for would test the fixture, not the system.
    """
    outcome = QueryOutcome(
        case_id=case.id,
        category=case.category,
        query=case.query,
        expected_route=case.expected_route,
        expected_ticker=case.expected_ticker,
        content_scored=content_scored,
    )

    try:
        if raw.get("error"):
            outcome.error = str(raw["error"])
            outcome.actual_route = str(raw.get("route", "ERROR"))
            outcome.actual_ticker = raw.get("ticker")
            outcome.latency_ms = dict(raw.get("latency_ms") or {})
            return outcome

        outcome.actual_route = str(raw.get("route") or "UNKNOWN")
        outcome.actual_ticker = raw.get("ticker")
        outcome.answer = raw.get("answer") or ""
        outcome.provenance = raw.get("provenance") or ""
        outcome.latency_ms = dict(raw.get("latency_ms") or {})
        outcome.degraded = bool(raw.get("degraded"))
        wire_metrics = raw.get("graph_metrics") or {}
        outcome.node_count = int(wire_metrics.get("node_count") or 0)
        outcome.edge_count = int(wire_metrics.get("edge_count") or 0)

        paths = raw.get("paths") or []
        outcome.path_count = len(paths)
        # Depth is the server's measurement. It is never reconstructed here.
        # ``depth_measured`` records whether a depth was actually reported, so
        # an uninstrumented server reads as unmeasured rather than as a
        # zero-hop traversal -- otherwise "we forgot to instrument" would
        # silently score as "the graph could not reach the answer".
        wire_depth = (raw.get("graph_metrics") or {}).get("max_hop_depth")
        if isinstance(wire_depth, int) and not isinstance(wire_depth, bool):
            outcome.path_depth = wire_depth
            outcome.depth_measured = True
        elif paths:
            # A harness that ships whole, server-labelled paths. Not a
            # heuristic: the grouping is the server's, not this client's.
            outcome.path_depth = max_path_depth(paths)
            outcome.depth_measured = True

        outcome.routing_ok = (
            outcome.actual_route == case.expected_route
            and (outcome.actual_ticker or None) == case.expected_ticker
        )

        scored = score_text(
            case,
            outcome.answer,
            outcome.provenance,
            raw.get("evidence_corpus") or "",
        )
        outcome.leaked_terms = scored["leaked_terms"]
        outcome.apple_accessions = scored["apple_accessions"]
        outcome.isolation_ok = scored["isolation_ok"]
        outcome.missing_concepts = scored["missing_concepts"]
        outcome.concept_ok = scored["concept_ok"]
        outcome.missing_personnel = scored["missing_personnel"]
        outcome.personnel_ok = scored["personnel_ok"]
        outcome.ungrounded = scored["ungrounded"]
        outcome.groundedness_ok = scored["groundedness_ok"]
        outcome.refusal = scored["refusal"]
        outcome.figures_grounded = max(0, scored["figures_total"] - len(scored["ungrounded"]))
        outcome.figures_total = scored["figures_total"]

        if not content_scored:
            # Not measured in this mode. Parked green so they neither fail a
            # case nor appear as a real score; the report labels them n/a.
            outcome.groundedness_ok = True
            outcome.concept_ok = True
            outcome.personnel_ok = True

        outcome.hops_ok = (
            # A refusal legitimately traverses nothing; demanding depth of an
            # answer that should not exist is a broken assertion, not a strict one.
            outcome.refusal
            or outcome.path_depth >= case.min_hops
        )
    except Exception as exc:  # a scorer bug must not lose the other 49 cases
        log.exception("scoring failed for %s", case.id)
        outcome.error = f"{type(exc).__name__}: {exc}"

    return outcome


# ---------------------------------------------------------------------------
# Mock harness
# ---------------------------------------------------------------------------

#: Deterministic 10-K fixture. Shaped like a real EDGAR Item 1 slice so the
#: real ``clean_and_truncate_section`` has something to parse.
_MOCK_10K = (
    "<html><body>"
    "<div>Item 1. Business</div>"
    "<p>The Company designs, manufactures and sells advanced compute products "
    "and provides cloud software services. The Company depends on a limited "
    "number of suppliers for advanced packaging and high-bandwidth memory, and "
    "sources most of its semiconductor fabrication from a single foundry partner.</p>"
    "<p>Revenue is concentrated among a small number of customers, and export "
    "controls administered by the U.S. Department of Commerce restrict sales of "
    "certain products into certain regions.</p>"
    "<p>Item 1A. Risk Factors</p>"
    "<p>The Company is subject to supply concentration risk, key personnel "
    "dependence, and regulatory capital and liquidity requirements.</p>"
    "</body></html>"
)

#: Deterministic extraction fixture, anchored on the ticker under test.
#:
#: The relationships hang off the ticker itself rather than off a separate
#: "target" entity, because that is the contract ``stitch_coldstart_payload``
#: implements: it pre-maps ``target_ticker`` and its uppercased form onto the
#: canonical company node, so a relation whose ``source_id`` is the ticker
#: stitches onto the issuer. A payload that invented its own id for the issuer
#: would stitch onto an orphan node and the traversal from the ticker would
#: return nothing.
def _mock_payload_for(ticker: str) -> dict[str, Any]:
    """Mock payload giving the stitched graph a genuine 2-hop shape.

    company -> packaging supplier -> foundry partner, plus a risk edge, so
    ``min_hops=2`` cases are answered by a real two-edge path rather than by a
    fan-out of one-hop edges.
    """
    return {
        "entities": [
            {
                "id": "supplier",
                "name": "ADVANCED PACKAGING SUPPLIER",
                "entity_type": "Supplier",
                "properties": {},
            },
            {
                "id": "foundry",
                "name": "LEADING EDGE FOUNDRY PARTNER",
                "entity_type": "Supplier",
                "properties": {},
            },
            {
                "id": "risk",
                "name": "SUPPLY CONCENTRATION RISK",
                "entity_type": "RiskFactor",
                "properties": {},
            },
        ],
        "relationships": [
            {
                "source_id": ticker,
                "target_id": "supplier",
                "relation": "SOURCES_FROM",
                "confidence": 0.9,
                "evidence_quote": "depends on a limited number of suppliers for advanced packaging",
                "properties": {},
            },
            {
                "source_id": "supplier",
                "target_id": "foundry",
                "relation": "SOURCES_FROM",
                "confidence": 0.8,
                "evidence_quote": "sources most of its semiconductor fabrication from a single foundry partner",
                "properties": {},
            },
            {
                "source_id": ticker,
                "target_id": "risk",
                "relation": "EXPOSED_TO",
                "confidence": 0.85,
                "evidence_quote": "The Company is subject to supply concentration risk",
                "properties": {},
            },
        ],
        "rejected_count": 0,
        "metadata": {"source": "mock"},
    }

#: Entity names are resolved to whatever ticker the case asked about, so the
#: mock graph is about the right company rather than a placeholder.
_MOCK_ANSWER_TEMPLATE = """1. Executive Summary & Thesis
{co} designs and sells advanced compute products and cloud software services.
The issuer is exposed to SUPPLY CONCENTRATION RISK through a narrow supplier base.

2. Direct Dependencies (1-hop)
The company SOURCES_FROM ADVANCED PACKAGING SUPPLIER for advanced packaging capacity.

3. Second-Order Contagion (Supply Chain / Competitors / Key Talent)
ADVANCED PACKAGING SUPPLIER in turn SOURCES_FROM LEADING EDGE FOUNDRY PARTNER, so a
foundry-level disruption reaches {co} two hops out. Export controls and key personnel
dependence are the filed transmission channels.

4. Capital Allocation & Margin Outlook
Concentration in a single foundry partner and a limited set of suppliers is the
dominant margin risk, alongside regulatory capital and liquidity requirements.

5. Verifiable Evidence Chain
target -> SOURCES_FROM -> supplier -> SOURCES_FROM -> foundry
target -> EXPOSED_TO -> SUPPLY CONCENTRATION RISK
"""


def _mock_raw_for_cold_start(
    case: BenchmarkCase, ticker: str, kg: Any
) -> dict[str, Any]:
    """Run the real JIT pipeline with EDGAR and the model stubbed out.

    Everything between fetch and synthesis is the shipping code path: the real
    slicer, the real Pydantic-validated payload, the real stitcher and the real
    hybrid traverser. Only the two network/LLM edges are replaced, so a
    regression in stitching or traversal depth still shows up here.
    """
    from sandbox_engine.coldstart_schema import ExtractionPayload
    from sandbox_engine.coldstart_synthesis import ColdStartSynthesizer
    from sandbox_engine.stitch import InMemoryOverlayGraph, stitch_coldstart_payload
    from sandbox_engine.tier1_clean import clean_and_truncate_section
    from sandbox_engine.traversal import HybridGraphTraverser, format_provenance_ledger

    latency: dict[str, float] = {}
    result: dict[str, Any] = {"route": "COLD_START", "ticker": ticker}

    t0 = time.perf_counter()
    cleaned = clean_and_truncate_section(_MOCK_10K, form_type="10-K", max_tokens=6000)
    latency["fetch_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    payload = ExtractionPayload.model_validate(_mock_payload_for(ticker))
    latency["extract_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    overlay = InMemoryOverlayGraph(kg_connection=kg)
    stitch_coldstart_payload(overlay, payload, target_ticker=ticker)
    latency["stitch_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    subgraph = HybridGraphTraverser(overlay).traverse_neighborhood(ticker, max_hops=2)
    latency["traversal_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    paths = subgraph.get("paths", [])

    t0 = time.perf_counter()
    answer = _MOCK_ANSWER_TEMPLATE.format(co=ticker)
    # Generate the real prompts so the synthesizer's own path is exercised even
    # though the tokens are canned.
    ColdStartSynthesizer().generate_prompts(ticker, case.query, paths, cleaned)
    latency["synthesis_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    result.update(
        answer=answer,
        provenance=format_provenance_ledger(paths),
        # A claim may legitimately come from the Item 1 narrative the answer was
        # allowed to quote, not only from a graph node, so groundedness is
        # checked against the ledger plus the filed text.
        evidence_corpus=f"{format_provenance_ledger(paths)}\n{cleaned}",
        paths=paths,
        latency_ms=latency,
    )
    return result


def _mock_raw_for_known(case: BenchmarkCase, ticker: str, kg: Any) -> dict[str, Any]:
    """Serve a KNOWN case from a pre-seeded overlay, skipping the JIT pipeline.

    A seeded-backbone query should never trigger ingestion; running the
    extractor for it would score the wrong code path.
    """
    from sandbox_engine.coldstart_schema import ExtractionPayload
    from sandbox_engine.stitch import InMemoryOverlayGraph, stitch_coldstart_payload
    from sandbox_engine.traversal import HybridGraphTraverser, format_provenance_ledger

    latency: dict[str, float] = {}
    t0 = time.perf_counter()
    payload = ExtractionPayload.model_validate(_mock_payload_for(ticker))
    overlay = InMemoryOverlayGraph(kg_connection=kg)
    stitch_coldstart_payload(overlay, payload, target_ticker=ticker)
    latency["stitch_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    subgraph = HybridGraphTraverser(overlay).traverse_neighborhood(ticker, max_hops=2)
    latency["traversal_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    paths = subgraph.get("paths", [])

    t0 = time.perf_counter()
    answer = _MOCK_ANSWER_TEMPLATE.format(co=ticker)
    latency["synthesis_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    return {
        "route": "KNOWN",
        "ticker": ticker,
        "answer": answer,
        "provenance": format_provenance_ledger(paths),
        "paths": paths,
        "latency_ms": latency,
    }


class MockHarness:
    """Offline evaluator: real pipeline, stubbed EDGAR and model.

    ``measures_content`` is False. The answer is one canned template for all 50
    cases, so required-concept and groundedness assertions against it would
    measure the fixture. Routing, isolation, hop depth and latency are genuine
    here, and those are what this mode gates.
    """

    mode = "mock-llm"
    measures_content = False

    def __init__(self, seeded_tickers: Sequence[str] = ("AAPL", "MSFT", "NVDA")) -> None:
        self.seeded = {t.upper() for t in seeded_tickers}
        self._lock = threading.Lock()

    def execute(self, cypher: str, params: Optional[dict[str, Any]] = None) -> list[list[Any]]:
        """Minimal ``kg`` surface for the router and the stitcher.

        The router's presence probe and the stitcher's backbone lookup are both
        single-hop Company lookups keyed on ``ticker``; anything else returns
        empty rather than raising, so an unmodelled query degrades to "no
        neighbours" instead of a stack trace.
        """
        ticker = str((params or {}).get("ticker", "")).upper()
        with self._lock:
            if ticker and ticker in self.seeded:
                return [[ticker, f"{ticker} Inc.", "0000000000"]]
        return []

    def __call__(self, case: BenchmarkCase) -> dict[str, Any]:
        from sandbox_engine.router import EntityRoute, route_query

        t_start = time.perf_counter()
        try:
            routing = route_query(case.query, self)
        except Exception as exc:
            return {
                "error": f"routing failed: {type(exc).__name__}: {exc}",
                "route": "ERROR",
                "latency_ms": {"routing_ms": round((time.perf_counter() - t_start) * 1000, 2)},
            }

        routing_ms = round((time.perf_counter() - t_start) * 1000, 2)
        route = routing.route.value

        if route == EntityRoute.AMBIGUOUS.value:
            from sandbox_engine.query_ui import _ambiguous_response

            payload = _ambiguous_response(case.query)
            return {
                "route": "AMBIGUOUS",
                "ticker": None,
                "answer": payload["message"],
                "provenance": "",
                "paths": [],
                "latency_ms": {"routing_ms": routing_ms},
            }

        ticker = routing.ticker or ""
        try:
            if route == EntityRoute.COLD_START.value:
                raw = _mock_raw_for_cold_start(case, ticker, self)
            else:
                raw = _mock_raw_for_known(case, ticker, self)
        except Exception as exc:
            log.exception("mock pipeline failed for %s", case.id)
            return {
                "error": f"pipeline failed: {type(exc).__name__}: {exc}",
                "route": route,
                "ticker": ticker,
                "latency_ms": {"routing_ms": routing_ms},
            }

        raw["latency_ms"]["routing_ms"] = routing_ms
        raw["latency_ms"]["total_ms"] = round((time.perf_counter() - t_start) * 1000, 2)
        return raw


# ---------------------------------------------------------------------------
# Live harness
# ---------------------------------------------------------------------------


class LiveHarness:
    """Evaluator against a running server at ``base_url``.

    Uses the SSE transport so time to first token is real rather than
    reconstructed. The server emits ``status`` events before any content, so
    the first ``token`` event is the honest first-byte-the-user-sees moment.
    """

    mode = "live"
    measures_content = True

    def __init__(self, base_url: str = "http://127.0.0.1:9000", timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, question: str) -> dict[str, Any]:
        """POST /api/ask and fold the SSE stream into one result dict."""
        request = urllib.request.Request(
            f"{self.base_url}/api/ask",
            data=json.dumps({"question": question}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        start = time.perf_counter()
        ttft: Optional[float] = None
        tokens: list[str] = []
        fallback_seen = False
        final: dict[str, Any] = {}
        event: Optional[str] = None

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line.split(":", 1)[1].strip())
                    except json.JSONDecodeError:
                        continue
                    if event == "status":
                        # The step is a stable machine token; the message is
                        # prose for humans. Only the token is read, so rewording
                        # a status string cannot silently break the client.
                        if data.get("step") == "fallback":
                            fallback_seen = True
                    elif event == "token":
                        if ttft is None:
                            ttft = round((time.perf_counter() - start) * 1000, 2)
                        tokens.append(str(data.get("token", "")))
                    elif event == "done":
                        final = data
        except urllib.error.HTTPError as exc:
            # Reading the error body can itself fail -- the server may have
            # reset the connection after sending the status line, and urllib
            # raises from ``read()`` rather than returning a short body. That
            # second failure must not escape: losing one case's error text is
            # survivable, while an exception here propagates out of the harness
            # and kills the run, turning a 503 into no report at all.
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            return {
                "error": f"HTTP {exc.code}" + (f": {body}" if body else ""),
                "route": "ERROR",
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "route": "ERROR"}

        total_ms = round((time.perf_counter() - start) * 1000, 2)

        # Per-stage timings and hop depth are read from the ``done`` payload.
        # They used to be reconstructed here: the ticker was scraped out of a
        # human-readable status message with a regex, and depth was re-chained
        # from a flattened edge list, because the wire carried neither. Both
        # guesses were ways for this client to disagree with the server about
        # what the server did. The server now reports both, so this reads them.
        wire_stages = final.get("stage_latencies_ms") or {}
        latency = {
            f"{stage.removesuffix('_ms')}_ms": float(ms)
            for stage, ms in wire_stages.items()
            if isinstance(ms, (int, float))
        }
        if ttft is not None:
            # Client-observed, and not something the server can measure: the
            # server timestamps its own stages, this timestamps the first byte
            # arriving over the socket.
            latency["ttft_ms"] = ttft
        # The client's own end-to-end reading is kept alongside the server's,
        # and the two are allowed to differ -- the gap is transport and
        # serialisation, which is exactly what a client cannot see from inside.
        latency.setdefault("total_ms", total_ms)
        latency["client_total_ms"] = total_ms

        graph = final.get("graph") or {}
        wire_metrics = final.get("graph_metrics") or {}
        hop_depth = wire_metrics.get("max_hop_depth")
        # No re-chaining of the flat edge list into paths. That heuristic
        # guessed which hops belonged to which path by matching one edge's
        # source against the previous edge's target, which is a guess: an
        # overlay and a backbone hop can name the same entity differently, two
        # paths can share an endpoint, and a diamond legitimately has an edge
        # that chains and an edge that does not. A wrong guess scored as fact,
        # and the benchmark then disagreed with the server about what the
        # server itself traversed. Depth comes from the server's own
        # ``graph_metrics`` now, and is left unmeasured when the server sends
        # none -- see ``depth_measured`` for how that surfaces.
        paths = final.get("paths") or []

        return {
            "route": str(final.get("route") or "UNKNOWN"),
            "ticker": final.get("ticker"),
            "answer": str(final.get("answer") or "".join(tokens)),
            "provenance": final.get("provenance", ""),
            # Live mode grounds against the provenance ledger only: the server
            # does not ship the filing text over SSE. That is the stricter
            # direction for a hallucination check, so it is the safe default.
            "evidence_corpus": final.get("provenance", ""),
            "paths": paths,
            "graph": graph,
            "graph_metrics": {
                "max_hop_depth": hop_depth,
                "node_count": wire_metrics.get("node_count"),
                "edge_count": wire_metrics.get("edge_count"),
            },
            "latency_ms": latency,
            "degraded": bool(final.get("degraded")) or fallback_seen,
            "error": str(final.get("error") or ""),
        }

    def __call__(self, case: BenchmarkCase) -> dict[str, Any]:
        return self._post(case.query)

    def preflight(self) -> None:
        """Fail fast with a clear message if the server is not up.

        Better than 50 identical connection errors buried in a failure log.
        """
        try:
            urllib.request.urlopen(f"{self.base_url}/api/stats", timeout=5).read()
        except Exception as exc:
            raise SystemExit(
                f"cannot reach {self.base_url} ({type(exc).__name__}: {exc}).\n"
                f"Start the server first: python -m sandbox_engine --port 9000"
            ) from exc


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class RunReport:
    """Aggregated result of one benchmark run."""

    mode: str
    outcomes: list[QueryOutcome] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    wall_seconds: float = 0.0
    #: Whether this run's harness produced real answer text. False for a mock
    #: run, where the content metrics are meaningless rather than failing.
    measures_content: bool = True
    #: How many outcomes were restored from a checkpoint rather than run now.
    resumed: int = 0
    #: Set when SIGINT/SIGTERM ended the run early. The report is still
    #: rendered, from whatever completed, and says so on its face.
    interrupted: bool = False
    #: Set when the consecutive-failure breaker stopped the run.
    breaker_tripped: bool = False
    #: How many cases the run was asked to cover, so a partial report can say
    #: "10 of 50" rather than just "10". Set by the runner; zero in a report
    #: assembled by hand.
    cases_total: int = 0

    def applicable_targets(self) -> list[str]:
        """Scorecard rows this run can honestly report."""
        applicable = list(TARGETS)
        if not self.measures_content:
            applicable = [k for k in applicable if k not in _CONTENT_METRICS]
        if not self.measures_depth():
            # No case carried a depth, so reachability has no denominator. An
            # n/a here is the honest reading; a 0% would blame the graph for
            # the server not being instrumented.
            applicable = [k for k in applicable if k != "multi_hop_reachability"]
        return applicable

    def by_category(self) -> dict[str, list[QueryOutcome]]:
        out: dict[str, list[QueryOutcome]] = {c: [] for c in CATEGORIES}
        for o in self.outcomes:
            out.setdefault(o.category, []).append(o)
        return {k: v for k, v in out.items() if v}

    def percent(self, subset: Sequence[QueryOutcome], predicate: Callable[[QueryOutcome], bool]) -> float:
        """Percent of ``subset`` satisfying ``predicate``; 0.0 when empty.

        Returns 0.0 rather than raising so a filtered run that matches nothing
        reports a score instead of crashing the report writer.
        """
        if not subset:
            return 0.0
        return 100.0 * sum(1 for o in subset if predicate(o)) / len(subset)

    def scores(self) -> dict[str, float]:
        """The five headline numbers, each as a percentage."""
        routed = [o for o in self.outcomes if o.expected_route != "AMBIGUOUS"]
        cold = [o for o in self.outcomes if o.expected_route == "COLD_START"]
        scored = [o for o in self.outcomes if not o.refusal]
        # Reachability is only meaningful where a depth was actually reported.
        # A case whose server sent no ``graph_metrics`` is not a case that
        # failed to traverse; scoring it as one would make an uninstrumented
        # server look like a broken graph.
        measured = [o for o in scored if o.depth_measured]
        return {
            "routing_accuracy": self.percent(routed, lambda o: o.routing_ok),
            "cold_start_success": self.percent(cold, lambda o: o.passed),
            "isolation_score": self.percent(self.outcomes, lambda o: o.isolation_ok),
            "multi_hop_reachability": self.percent(measured, lambda o: o.hops_ok),
            "groundedness": self.percent(scored, lambda o: o.groundedness_ok),
        }

    def measures_depth(self) -> bool:
        """Whether any case got a depth measurement at all.

        Reported as n/a when false, so a server that sends no ``graph_metrics``
        is visibly uninstrumented rather than quietly scoring zero hops.
        """
        return any(o.depth_measured for o in self.outcomes if not o.refusal)

    def overall_pass_rate(self) -> float:
        return self.percent(self.outcomes, lambda o: o.passed)

    def latencies(self) -> list[float]:
        return [o.latency_ms.get("total_ms", 0.0) for o in self.outcomes if o.latency_ms]

    def stage_latencies(self) -> dict[str, list[float]]:
        stages: dict[str, list[float]] = {}
        for o in self.outcomes:
            for stage, ms in o.latency_ms.items():
                stages.setdefault(stage, []).append(ms)
        return stages

    def category_latency(self, stage: str = "total_ms") -> dict[str, float]:
        """Median total latency per category.

        Categories are not equally hard: a cold-start fetch and a multi-hop
        traversal are different amounts of work, and a single global percentile
        averages that away. P50 per category is the number that says whether
        the slow category is slow or the whole suite is.
        """
        out: dict[str, float] = {}
        for category, group in self.by_category().items():
            values = [o.latency_ms[stage] for o in group if stage in o.latency_ms]
            if values:
                out[category] = _percentile(values, 50)
        return out


def _percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile.

    Deliberately not interpolated: with 50 samples, an interpolated P99 invents
    precision the sample size cannot support, and the failure log is easier to
    trust when a reported number is a value that was actually observed.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = max(1, min(len(ordered), int(round(pct / 100.0 * len(ordered) + 0.5))))
    return round(ordered[rank - 1], 2)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _status(actual: float, target: float) -> str:
    return "PASS" if actual >= target else "FAIL"


def _pct_cell(value: float, has_denominator: bool) -> str:
    """Render a percentage, or ``n/a`` when nothing was measured.

    A category of pure negative controls has no non-refusal cases, so its
    depth and groundedness rates have an empty denominator. Printing ``0%``
    there would report a category that is entirely correct as entirely failing.
    """
    return f"{value:.0f}%" if has_denominator else "n/a"


def render_report(report: RunReport) -> str:
    """Render the markdown report."""
    scores = report.scores()
    applicable = set(report.applicable_targets())
    lines: list[str] = [
        "# Graph RAG — 50-Query Evaluation Report",
        "",
        f"- **Mode:** `{report.mode}`",
        f"- **Started:** {report.started_at}",
        f"- **Finished:** {report.finished_at}",
        f"- **Wall clock:** {report.wall_seconds:.1f}s",
        f"- **Cases:** {len(report.outcomes)}",
        "",
        "## 1. Executive Scorecard",
        "",
    ]

    # A partial run's scorecard is arithmetically valid and substantively
    # misleading: 10 easy cases at 100% is not a 100% suite. The banner goes
    # above the numbers, not below them, so it cannot be scrolled past.
    if report.interrupted:
        lines += [
            "> ## [INTERRUPTED]",
            ">",
            f"> This run stopped before finishing. **{len(report.outcomes)} of "
            f"{report.cases_total} case(s)** completed. The scores below cover "
            "only those cases and are **not** comparable to a complete run.",
            ">",
            "> Resume with `--resume` to run the remaining cases and merge them "
            "into one report.",
            "",
        ]
    elif report.breaker_tripped:
        lines += [
            "> ## [STOPPED — CIRCUIT BREAKER]",
            ">",
            f"> The run halted after repeated network/timeout failures. "
            f"**{len(report.outcomes)} of {report.cases_total} case(s)** "
            "completed. Treat the scores below as partial.",
            "",
        ]
    elif report.resumed:
        lines += [
            f"> Continued from a checkpoint: {report.resumed} case(s) were "
            f"restored and {len(report.outcomes) - report.resumed} ran in this "
            "session. All rows below are from the same suite.",
            "",
        ]

    lines += [
        "| Metric | Target | Actual | Status |",
        "| --- | ---: | ---: | :---: |",
    ]
    for key, target in TARGETS.items():
        if key not in applicable:
            lines.append(
                f"| {key.replace('_', ' ').title()} | {target:.0f}% | n/a | n/a |"
            )
            continue
        actual = scores.get(key, 0.0)
        lines.append(
            f"| {key.replace('_', ' ').title()} | {target:.0f}% | {actual:.1f}% | {_status(actual, target)} |"
        )
    overall = report.overall_pass_rate()
    lines += [
        f"| Overall Pass Rate | — | {overall:.1f}% | {'PASS' if overall >= 80.0 else 'FAIL'} |",
        "",
    ]

    if not report.measures_content:
        lines += [
            "> **Content metrics not measured in this mode.** `--mock-llm` serves one "
            "canned answer for all 50 cases, so groundedness and required-concept "
            "matching would score the fixture rather than the system. Routing, "
            "isolation, hop depth and latency below are real. Run `--live` to gate "
            "the content metrics.",
            "",
        ]

    if report.measures_content and not report.measures_depth():
        unmeasured = sum(
            1 for o in report.outcomes if not o.refusal and not o.depth_measured
        )
        lines += [
            f"> **Hop depth not reported by the server** for {unmeasured} case(s). "
            "`multi_hop_reachability` is shown as n/a rather than 0%: the client "
            "no longer reconstructs paths from the flat edge list, so a server "
            "that sends no `graph_metrics` cannot be scored on reachability. "
            "Check that the deployed build emits `graph_metrics.max_hop_depth` "
            "in its `done` event.",
            "",
        ]

    degraded = [o for o in report.outcomes if o.degraded]
    if degraded:
        lines += [
            f"> {len(degraded)} case(s) ran on a degraded path (pipeline fell back to "
            f"standard graph QA). Scored as real responses, flagged so an EDGAR or "
            f"model outage is not read as a correctness regression.",
            "",
        ]

    lines += [
        "## 2. Category Breakdown",
        "",
        "| Category | Cases | Pass | Route OK | Isolation | Hops OK | Grounded | Avg depth | P50 total (ms) | P95 total (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category, group in report.by_category().items():
        scored = [o for o in group if not o.refusal]
        measured = [o for o in scored if o.depth_measured]
        # Averaging in the unmeasured cases would report a depth of 0 for a
        # server that simply did not send one.
        avg_depth = (
            f"{statistics.fmean([o.path_depth for o in measured]):.1f}"
            if measured
            else "n/a"
        )
        grounded_col = (
            _pct_cell(
                report.percent(scored, lambda o: o.groundedness_ok), bool(scored)
            )
            if report.measures_content
            else "n/a"
        )
        totals = [o.latency_ms["total_ms"] for o in group if "total_ms" in o.latency_ms]
        p50 = f"{_percentile(totals, 50):.1f}" if totals else "n/a"
        p95 = f"{_percentile(totals, 95):.1f}" if totals else "n/a"
        lines.append(
            f"| {CATEGORY_LABELS.get(category, category)} "
            f"| {len(group)} "
            f"| {_pct_cell(report.percent(group, lambda o: o.passed), bool(group))} "
            f"| {_pct_cell(report.percent(group, lambda o: o.routing_ok), bool(group))} "
            f"| {_pct_cell(report.percent(group, lambda o: o.isolation_ok), bool(group))} "
            f"| {_pct_cell(report.percent(measured, lambda o: o.hops_ok), bool(measured))} "
            f"| {grounded_col} "
            f"| {avg_depth} | {p50} | {p95} |"
        )
    lines.append("")

    lines += [
        "## 3. Latency Distribution",
        "",
        "Nearest-rank percentiles over observed values. With 50 samples an "
        "interpolated P99 would imply precision the sample cannot support.",
        "",
        "| Stage | n | P50 (ms) | P90 (ms) | P95 (ms) | P99 (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for stage, values in sorted(report.stage_latencies().items()):
        cells = " | ".join(f"{_percentile(values, p):.1f}" for p in _PERCENTILES)
        lines.append(f"| {stage} | {len(values)} | {cells} |")
    lines.append("")

    lines += ["## 4. Per-Query Results", "",
              "| ID | Category | Route (exp/act) | Pass | Depth | Isolation | Grounded |",
              "| --- | --- | --- | :---: | ---: | :---: | :---: |"]
    for o in report.outcomes:
        grounded = "n/a" if not o.content_scored else ("OK" if o.groundedness_ok else "NO")
        if o.isolation_ok:
            iso = "OK"
        elif o.apple_accessions:
            iso = "ACC"
        else:
            iso = "LEAK"
        lines.append(
            f"| {o.case_id} "
            f"| {o.category} "
            f"| {o.expected_route}/{o.actual_route} "
            f"| {'Y' if o.passed else 'N'} "
            f"| {o.path_depth} "
            f"| {'OK' if o.isolation_ok else iso} "
            f"| {grounded} |"
        )
    lines.append("")

    failures = [o for o in report.outcomes if not o.passed]
    lines += ["## 5. Failure Log", ""]
    if not failures:
        lines += ["No failures. All cases passed every scored metric.", ""]
    else:
        lines += [
            "| ID | Query | Expected route | Actual route | Expected ticker | Actual ticker | Reason |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for o in failures:
            query = o.query.replace("|", "\\|")
            if len(query) > 70:
                query = query[:67] + "..."
            reason = "; ".join(o.failures()).replace("|", "\\|")
            lines.append(
                f"| {o.case_id} | {query} | {o.expected_route} | {o.actual_route} "
                f"| {o.expected_ticker or '—'} | {o.actual_ticker or '—'} | {reason} |"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class _InterruptFlag:
    """A one-way flag the signal handler sets and the run loop polls.

    The handler must not do the work of stopping the run. Raising from a signal
    handler lands wherever the interpreter happens to be -- inside a socket
    read, inside ``json.dump`` on the checkpoint, mid-``os.replace`` -- which is
    exactly the window the checkpoint exists to protect. So the handler only
    records the request, and the sequential loop checks it at a query boundary,
    where stopping is safe and the finished case is already on disk.

    A second signal escalates: the operator pressing ``Ctrl+C`` twice means the
    first one did not land, so the flag goes into hard mode and the run stops at
    the very next check without waiting for the in-flight query to return.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._hard = False
        self.signum: Optional[int] = None

    def request(self, signum: int) -> None:
        self.signum = signum
        self._event.set()

    def escalate(self, signum: int) -> None:
        self._hard = True
        self.request(signum)

    def __call__(self) -> bool:
        return self._event.is_set()

    @property
    def hard(self) -> bool:
        return self._hard

    @property
    def name(self) -> str:
        if self.signum is None:
            return "signal"
        try:
            return signal.Signals(self.signum).name
        except ValueError:
            return f"signal {self.signum}"


def _install_signal_handlers(flag: _InterruptFlag) -> dict[int, Any]:
    """Route SIGINT and SIGTERM to ``flag``. Returns the previous handlers.

    Both are trapped, not just SIGINT: this process is normally killed with
    SIGTERM by a supervisor or a CI timeout, and a handler that only knows
    about ``Ctrl+C`` is a handler that does not run in exactly the situation
    where the checkpoint matters most.

    Only installed when running on the main thread of the main interpreter.
    ``signal.signal`` raises otherwise, and a benchmark imported by a test
    runner is not on either -- so this degrades to "no signal handling" rather
    than refusing to start.
    """
    previous: dict[int, Any] = {}
    if threading.current_thread() is not threading.main_thread():
        log.debug("not the main thread; signal handling not installed")
        return previous

    def _handle(signum, _frame):
        name = "Ctrl+C" if signum == signal.SIGINT else "SIGTERM"
        if flag():
            print("\nStopping now.", file=sys.stderr, flush=True)
            flag.escalate(signum)
            return
        print(
            f"\n{name} received: finishing the current query, then saving. "
            "Press again to stop immediately.",
            file=sys.stderr, flush=True,
        )
        flag.request(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _handle)
        except (ValueError, OSError, RuntimeError) as exc:
            log.debug("could not install handler for %s: %s", sig, exc)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    """Put the original handlers back.

    Restoring matters beyond tidiness: leaving a handler installed means the
    process that imported this module keeps a reference to the flag, and the
    next run in the same interpreter inherits a stop request that was raised
    during the last one.
    """
    import signal as _signal

    for sig, handler in previous.items():
        try:
            _signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            pass


class _Interrupted(Exception):
    """Raised in the worker when SIGINT/SIGTERM asked us to stop.

    Internal control flow, never shown to a user. It exists so the interrupt
    unwinds through the same ``finally`` that a completed run uses, rather than
    escaping mid-write and leaving a half-serialised checkpoint behind.
    """


#: Errors that mean "the run cannot continue", as opposed to a case that scored
#: badly. A case that fails its metrics is a result; a socket that will not open
#: is a broken run, and continuing through 50 of those wastes the time it took
#: to notice.
_TRANSPORT_ERRORS = (
    "HTTPError",
    "URLError",
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "IncompleteRead",
    "socket.timeout",
    "timed out",
    "Connection aborted",
    "RemoteDisconnected",
)

#: HTTP statuses that mean the far end could not serve the request, as opposed
#: to statuses that mean the request itself was wrong.
#:
#: Matched against the formatted ``"HTTP <code>: ..."`` string that LiveHarness
#: produces, so the class name ``HTTPError`` never appears in the text. Checking
#: for the name instead meant a 503 -- precisely the outage this breaker exists
#: to catch -- was classified as a normal result and the run sailed through all
#: 50 cases. 4xx is excluded except for the two that are transient by
#: definition: 408 (timeout) and 429 (rate limited).
_TRANSPORT_HTTP = ("HTTP 5", "HTTP 408", "HTTP 429")


def is_transport_error(error: str) -> bool:
    """Whether ``error`` describes a network or timeout fault.

    Matched on the exception *name*, on transient HTTP statuses, and on a few
    transport phrases rather than on a fixed set of full strings, because the
    same fault reaches this client spelled several ways depending on whether
    urllib, the socket layer or the server produced it. A false positive here
    is survivable -- the run stops early and the checkpoint resumes -- so the
    bias is toward stopping.
    """
    if not error:
        return False
    if any(token in error for token in _TRANSPORT_ERRORS):
        return True
    return any(token in error for token in _TRANSPORT_HTTP)


class BenchmarkCheckpoint:
    """Crash- and interrupt-safe progress file for a long benchmark run.

    A 50-query live run is 50 sequential requests, several of which wait on
    EDGAR and on a model. Losing 40 completed queries to a ``Ctrl+C`` or a
    dropped socket is the difference between a five-minute resume and a
    twenty-minute restart, so each finished query is written through to disk
    before the next one starts.

    Written atomically via a temporary file and ``os.replace``: a checkpoint
    truncated by a kill mid-write is worse than no checkpoint, because it looks
    resumable and is not. ``os.replace`` is atomic within a filesystem, so a
    reader sees either the whole previous file or the whole new one.

    Stores the serialised outcome per case id, not the case definitions, so the
    checkpoint stays valid if the query set is edited between runs: a case whose
    id no longer exists is simply ignored on load, and a new case with a new id
    is run. That is the property that makes this a resume rather than a replay.
    """

    VERSION = 2

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.records: dict[str, dict[str, Any]] = {}

    # -- persistence ------------------------------------------------------

    def load(self) -> dict[str, dict[str, Any]]:
        """Read the checkpoint, tolerating absence and corruption.

        A checkpoint that cannot be parsed is discarded rather than raised: the
        file's whole job is to make a *later* run cheaper, so losing it costs
        time and nothing else. Refusing to start would be a worse outcome than
        starting over.
        """
        self.records = {}
        if not self.path.exists():
            return self.records
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.warning(
                "checkpoint at %s is unreadable (%s); starting a fresh run",
                self.path, type(exc).__name__,
            )
            return self.records
        if not isinstance(payload, dict):
            log.warning("checkpoint at %s is not an object; ignoring", self.path)
            return self.records
        if payload.get("version") != self.VERSION:
            log.warning(
                "checkpoint at %s is version %r, expected %r; starting fresh",
                self.path, payload.get("version"), self.VERSION,
            )
            return self.records
        records = payload.get("records")
        if isinstance(records, dict):
            self.records = {
                str(k): v for k, v in records.items() if isinstance(v, dict)
            }
        return self.records

    def save(self) -> None:
        """Flush every completed outcome, atomically.

        Called after *each* query rather than at the end, so the file is never
        more than one query behind reality. The temporary file is created in
        the destination directory, not the system temp dir, because
        ``os.replace`` is only atomic within a filesystem and crossing one
        would silently give up the property this whole class exists for.
        """
        payload = {
            "version": self.VERSION,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "completed": len(self.records),
            "records": self.records,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            # A checkpoint that cannot be written is a lost optimisation, not a
            # lost run: the in-memory report is still intact and still reports.
            log.warning("could not write checkpoint %s: %s", self.path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def reset(self) -> None:
        """Delete the checkpoint so the next run starts from nothing."""
        self.records = {}
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove checkpoint %s: %s", self.path, exc)

    # -- queries ----------------------------------------------------------

    def completed_ids(self) -> set[str]:
        return set(self.records)

    def has(self, case_id: str) -> bool:
        return case_id in self.records

    def record(self, outcome: QueryOutcome) -> None:
        self.records[outcome.case_id] = outcome.to_dict()

    def restore(self, cases: Sequence[BenchmarkCase]) -> list[QueryOutcome]:
        """Rebuild outcomes for already-completed cases, in the given order.

        A stored outcome whose case is not in ``cases`` is dropped, and a case
        with no stored outcome is absent from the result. Both are the same
        situation: that case still needs running.
        """
        by_id = {c.id: c for c in cases}
        out: list[QueryOutcome] = []
        for case in cases:
            stored = self.records.get(case.id)
            if stored:
                out.append(QueryOutcome.from_dict(case, stored))
        return out


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    harness: Any,
    concurrency: int = 1,
    on_result: Optional[Callable[[QueryOutcome], None]] = None,
    checkpoint: Optional[BenchmarkCheckpoint] = None,
    max_consecutive_failures: int = 3,
    should_stop: Optional[Callable[[], bool]] = None,
    resume: bool = True,
) -> RunReport:
    """Run ``cases`` through ``harness`` and score each one.

    ``concurrency=1`` runs sequentially, which is what you want when timing:
    concurrent cold-starts contend for the same SEC rate limit and the latency
    numbers stop meaning anything. Raise it only for throughput on a suite that
    is mostly KNOWN cases.

    With ``checkpoint`` set, the run is resumable and interruptible: each
    completed query is written through before the next begins and
    ``max_consecutive_failures`` stops a run whose network has gone away.
    ``should_stop`` is polled between queries so a signal can end the run at a
    query boundary rather than mid-write. ``resume=True`` (the default) restores
    and skips ids already in the checkpoint; ``resume=False`` runs every case
    from scratch, overwriting the checkpoint as it goes -- the distinction the
    CLI's ``--resume`` flag and its absence express.
    """
    content_scored = bool(getattr(harness, "measures_content", True))
    report = RunReport(
        mode=getattr(harness, "mode", "unknown"),
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        measures_content=content_scored,
        cases_total=len(cases),
    )
    wall_start = time.perf_counter()

    if checkpoint is not None:
        # Loaded here rather than left to the caller. ``restore`` and ``has``
        # read ``records``, and a caller who forgot to load first got a run
        # that silently ignored a checkpoint sitting on disk -- the worst
        # failure mode for a resume feature, because it looks like it worked.
        # ``load`` is idempotent, so a caller that already loaded (to print a
        # "resuming N cases" line) pays nothing for this.
        checkpoint.load()
        if resume:
            report.outcomes.extend(checkpoint.restore(cases))
            report.resumed = len(report.outcomes)
            pending = [c for c in cases if not checkpoint.has(c.id)]
            if report.resumed:
                log.info(
                    "resuming: %d case(s) already done, %d to run",
                    report.resumed, len(pending),
                )
        else:
            pending = list(cases)
    else:
        pending = list(cases)

    if not pending:
        report.wall_seconds = time.perf_counter() - wall_start
        report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return report

    def _finish_one(case: BenchmarkCase, raw: Any) -> None:
        outcome = evaluate_case(case, raw, content_scored=content_scored)
        report.outcomes.append(outcome)
        if checkpoint is not None:
            checkpoint.record(outcome)
            checkpoint.save()
        if on_result:
            on_result(outcome)

    consecutive = 0
    breaker_tripped = False
    try:
        if concurrency > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for case, raw in zip(pending, pool.map(harness, pending)):
                    _finish_one(case, raw)
                    consecutive, breaker_tripped = _track_failure(
                        report.outcomes[-1], consecutive,
                        max_consecutive_failures, breaker_tripped,
                    )
        else:
            for case in pending:
                # Polled before the request, not after: the point of a clean
                # interrupt is that the query in flight finishes and is saved,
                # and that means declining to start the next one.
                if should_stop is not None and should_stop():
                    raise _Interrupted()
                raw = harness(case)
                _finish_one(case, raw)
                consecutive, breaker_tripped = _track_failure(
                    report.outcomes[-1], consecutive,
                    max_consecutive_failures, breaker_tripped,
                )
                if breaker_tripped:
                    break
    except _Interrupted:
        report.interrupted = True
    except KeyboardInterrupt:
        report.interrupted = True

    report.wall_seconds = time.perf_counter() - wall_start
    report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    report.breaker_tripped = breaker_tripped
    # Keep the report in dataset order: resumed outcomes were restored before
    # the fresh ones, and a report whose rows are ordered by completion time
    # makes two runs of the same suite hard to diff.
    order = {c.id: i for i, c in enumerate(cases)}
    report.outcomes.sort(key=lambda o: order.get(o.case_id, len(order)))
    return report


def _track_failure(
    outcome: QueryOutcome,
    consecutive: int,
    limit: int,
    already_tripped: bool,
) -> tuple[int, bool]:
    """Advance the consecutive-transport-failure counter.

    Only transport faults count. A case that scores badly is a finding, and
    treating it as a broken run would stop the suite on exactly the results
    worth reading. A successful query resets the count, so an intermittent
    network does not accumulate toward a false trip.
    """
    if already_tripped:
        return consecutive, True
    if outcome.error and is_transport_error(outcome.error):
        consecutive += 1
        if limit > 0 and consecutive >= limit:
            log.error(
                "circuit breaker: %d consecutive transport failures, stopping",
                consecutive,
            )
            return consecutive, True
        return consecutive, False
    return 0, False


def _progress_printer(total: int, already_done: int = 0) -> Callable[[QueryOutcome], None]:
    """Per-case progress on stdout.

    ``already_done`` seeds the counter so a resumed run reports true position
    in the suite ("[ 8/50]") rather than restarting at 1 for the work it
    skipped -- which reads as though the run covered fewer cases than it did.
    """
    seen = already_done

    def _print(outcome: QueryOutcome) -> None:
        nonlocal seen
        seen += 1
        mark = "ok  " if outcome.passed else "FAIL"
        detail = f"  {outcome.error}" if outcome.error else ""
        print(
            f"  [{seen:>2}/{total}] {mark} {outcome.case_id} "
            f"{outcome.expected_route}->{outcome.actual_route} "
            f"depth={outcome.path_depth}{detail}",
            flush=True,
        )

    return _print


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_50_queries",
        description="Evaluate the Graph RAG platform on the 50 golden queries.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock-llm",
        action="store_true",
        help="Offline deterministic run: real pipeline, stubbed EDGAR and model.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Run against a live server and measure real end-to-end latency.",
    )
    parser.add_argument(
        "--category",
        choices=[*CATEGORIES, "all"],
        default="all",
        help="Restrict the run to one category.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Report path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:9000",
        help="Live server base URL (default: http://127.0.0.1:9000).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Parallel cases. Keep at 1 for meaningful latency (default: 1).",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=0.0,
        help="Exit non-zero if the overall pass rate is below this percentage.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write machine-readable per-case results to this path.",
    )
    checkpoint_group = parser.add_mutually_exclusive_group()
    checkpoint_group.add_argument(
        "--resume",
        action="store_true",
        help="Continue from the checkpoint, skipping cases already completed.",
    )
    checkpoint_group.add_argument(
        "--reset",
        action="store_true",
        help="Delete the checkpoint before running, ignoring any saved progress.",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=DEFAULT_CHECKPOINT,
        help=f"Progress file (default: {DEFAULT_CHECKPOINT}).",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=3,
        help=(
            "Stop the run after this many consecutive network/timeout errors. "
            "0 disables the breaker (default: 3)."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.mock_llm and not args.live:
        parser.error("choose a mode: --mock-llm or --live")

    category = "" if args.category == "all" else args.category
    cases = cases_for_category(category)
    if not cases:
        print(f"No cases in category '{args.category}'", file=sys.stderr)
        return 2

    if args.live:
        harness: Any = LiveHarness(base_url=args.base_url)
        harness.preflight()
    else:
        harness = MockHarness()

    # Checkpointing is always on. A 50-query run is long enough to be worth
    # protecting whether or not the operator asked for it, and a flag to turn it
    # off would be a flag nobody remembers to leave off. The three behaviours
    # are distinct:
    #
    #   --resume   : skip the ids already in the checkpoint, continue the run.
    #   --reset    : wipe the checkpoint, then run all fifty from scratch.
    #   (default)  : run all fifty from scratch. Progress is saved as it goes,
    #                so the file is not ignored -- it just is not treated as a
    #                reason to skip anything. Use --resume for that.
    #
    # Making the default resume would collapse the flag: it is exactly what a
    # plain run would then do, and the operator could never ask for a re-run of
    # the whole suite short of --reset.
    checkpoint = BenchmarkCheckpoint(args.checkpoint)
    resume_run = False
    if args.reset:
        checkpoint.reset()
        print(f"Checkpoint reset: {args.checkpoint}", flush=True)
    elif args.resume:
        done = len(checkpoint.load())
        resume_run = True
        if done:
            print(f"Resuming from {args.checkpoint} ({done} case(s) done)", flush=True)
        else:
            print(f"No usable checkpoint at {args.checkpoint}; starting fresh", flush=True)

    stop_flag = _InterruptFlag()
    previous_handlers = _install_signal_handlers(stop_flag)

    already_done = 0 if not resume_run else sum(
        1 for c in cases if checkpoint.has(c.id)
    )
    try:
        print(f"Running {len(cases)} case(s) in {harness.mode} mode", flush=True)
        report = run_benchmark(
            cases,
            harness,
            concurrency=args.concurrency,
            on_result=_progress_printer(len(cases), already_done),
            checkpoint=checkpoint,
            max_consecutive_failures=args.max_consecutive_failures,
            should_stop=stop_flag,
            resume=resume_run,
        )
    finally:
        _restore_signal_handlers(previous_handlers)
        # Last write on the way out, so a run that ended between two queries
        # still has every completed case on disk.
        checkpoint.save()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(report), encoding="utf-8")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([o.as_dict() for o in report.outcomes], indent=2),
            encoding="utf-8",
        )

    scores = report.scores()
    applicable = set(report.applicable_targets())
    print("\nScorecard")
    for key, target in TARGETS.items():
        if key not in applicable:
            print(f"  {key:<24}    n/a  (not measurable in {report.mode} mode)")
            continue
        actual = scores[key]
        print(f"  {key:<24} {actual:6.1f}%  (target {target:.0f}%)  {_status(actual, target)}")
    print(f"  {'overall pass rate':<24} {report.overall_pass_rate():6.1f}%")
    print(f"\nReport written to {args.output}")

    # Exit status. An interrupted or breaker-stopped run is a failure to
    # finish, not a pass: exit 130 for SIGINT (the shell convention) and 1 for
    # everything else, so a script wrapping this benchmark cannot mistake a
    # partial run for a clean one just because the completed cases scored well.
    if report.interrupted:
        saved = len(report.outcomes)
        print(
            f"\n[INTERRUPTED] {saved} of {len(cases)} case(s) completed and saved "
            f"to {args.checkpoint}.",
            file=sys.stderr,
        )
        print(
            f"Resume with:\n"
            f"  ./.venv/bin/python -m benchmarks.eval_50_queries "
            f"{'--live' if args.live else '--mock-llm'} --resume",
            file=sys.stderr,
        )
        return 130
    if report.breaker_tripped:
        print(
            f"\nCircuit breaker tripped: {args.max_consecutive_failures} "
            f"consecutive network/timeout failures. {len(report.outcomes)} of "
            f"{len(cases)} case(s) saved to {args.checkpoint}.",
            file=sys.stderr,
        )
        print(
            f"Resume when the server is reachable:\n"
            f"  ./.venv/bin/python -m benchmarks.eval_50_queries "
            f"{'--live' if args.live else '--mock-llm'} --resume",
            file=sys.stderr,
        )
        return 1

    if args.fail_under and report.overall_pass_rate() < args.fail_under:
        print(
            f"FAIL: pass rate {report.overall_pass_rate():.1f}% is below "
            f"--fail-under {args.fail_under:.1f}%",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
