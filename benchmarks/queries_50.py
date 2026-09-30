"""Golden query set for the 50-query Graph RAG evaluation benchmark.

Five categories, each stress-testing a different way the system can be wrong:

- ``seeded_multi_hop`` (10): the pre-indexed backbone. The entity resolves from
  the graph, so a failure here is a retrieval or traversal failure, not a
  routing one.
- ``cold_start`` (15): an unindexed issuer, which must be fetched from EDGAR,
  sliced, triple-extracted, stitched and traversed entirely in the request.
- ``executive_transition`` (10): CEO succession and key-person attrition.
  Speculative by construction: the answer is a reasoned projection over filed
  exposure, not a fact the filing states.
- ``contagion`` (10): a shock propagating across two or more issuers. Scored on
  path depth, because a contagion answered from a single issuer's own filing
  has not actually been traced.
- ``negative_control`` (5): questions with no answer. The only correct outcome
  is a refusal, and the only unforgivable one is a confident wrong answer.

Scope note on expected routes
-----------------------------
``expected_route`` is asserted against :func:`sandbox_engine.router.route_query`
against a graph that holds the seeded backbone only. Two consequences follow
from the router's current alias coverage, and both are deliberate rather than
papered over:

1. Category B and D name issuers (``ASML``, ``PLTR``, ...) the router does not
   carry an alias for. Those cases are written with a ``$TICKER`` cashtag,
   which is how a user actually disambiguates an unknown issuer. Cases written
   as bare company names ("JPMorgan Chase", "Rivian") resolve only because those
   two are in ``COMPANY_NAME_TO_TICKER``.
2. Several Category D cases would prefer ``KNOWN`` semantically -- the query is
   about NVDA, MSFT or AAPL -- but name a cold-start counterparty first, and
   routing is single-entity by construction. Those are recorded as
   ``COLD_START`` with the counterparty as ``expected_ticker``, because that is
   the honest route, and the traversal depth check still measures the
   contagion the query asked about.

A case whose ``expected_route`` disagrees with the shipped router is a finding
about the router, not a reason to edit the expectation. ``--mock-llm`` prints
every routing mismatch so that disagreement is visible rather than buried.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

__all__ = [
    "APPLE_LEAK_TERMS",
    "BENCHMARK_QUERIES",
    "CASES",
    "CATEGORIES",
    "CATEGORY_LABELS",
    "BenchmarkCase",
    "cases_for_category",
]

#: Terms that identify Apple specifically. A query about any other issuer must
#: not surface them: their presence means the answer was built from Apple's
#: context, which is the exact failure the anti-leak invariant exists to catch.
#: Matched on word boundaries, so "Mac" does not fire on "MacBook" or "macro".
APPLE_LEAK_TERMS: tuple[str, ...] = (
    "iPhone",
    "iPad",
    "iMac",
    "MacBook",
    "Mac",
    "Tim Cook",
    "AppleCare",
    "App Store",
    "Services segment",
    "iPod",
    "Watch",
)

CATEGORIES: tuple[str, ...] = (
    "seeded_multi_hop",
    "cold_start",
    "executive_transition",
    "contagion",
    "negative_control",
)

CATEGORY_LABELS: dict[str, str] = {
    "seeded_multi_hop": "A. Seeded Backbone Multi-Hop Reasoning",
    "cold_start": "B. Cold-Start JIT Dynamic Ingestion",
    "executive_transition": "C. Executive Transitions & Governance Shocks",
    "contagion": "D. Cross-Entity Contagion & Second-Order Shocks",
    "negative_control": "E. Ambiguity, Edge Cases & Negative Controls",
}


@dataclass
class BenchmarkCase:
    """One golden query and everything scored against it.

    ``forbidden_terms`` and ``required_concepts`` are matched case-insensitively
    on word boundaries against the answer text. ``forbidden_terms`` also carries
    the anti-leak invariant: for a case whose ``expected_ticker`` is not AAPL,
    every Apple term is forbidden whether or not it is listed here.

    ``min_hops`` is the depth the traversed paths must reach. A contagion query
    with ``min_hops=2`` that returns a single hop has not traced the
    contagion, so it fails even when the answer reads well.
    """

    id: str
    category: str
    query: str
    expected_route: str
    expected_ticker: Optional[str]
    forbidden_terms: list[str]
    required_concepts: list[str]
    min_hops: int = 1
    #: Named people the answer must engage with. Governance cases are about who
    #: holds a role, so losing the name loses the answer.
    key_personnel: list[str] = field(default_factory=list)
    #: Set for negative controls: the answer must contain no filed figure.
    expect_refusal: bool = False
    note: str = ""


def _case(
    cid: str,
    category: str,
    query: str,
    expected_route: str,
    expected_ticker: Optional[str],
    *,
    forbidden: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    min_hops: int = 1,
    key_personnel: tuple[str, ...] = (),
    expect_refusal: bool = False,
    note: str = "",
) -> BenchmarkCase:
    return BenchmarkCase(
        id=cid,
        category=category,
        query=query,
        expected_route=expected_route,
        expected_ticker=expected_ticker,
        forbidden_terms=list(forbidden),
        required_concepts=list(required),
        min_hops=min_hops,
        key_personnel=list(key_personnel),
        expect_refusal=expect_refusal,
        note=note,
    )


# Apple-specific terms repeated per case would be noise in the dataset, so the
# invariant is applied centrally at scoring time. These lists carry the terms
# that are specific to *this* case rather than to Apple's identity generally.
_NO_APPLE = ("iPhone", "Tim Cook", "Apple Watch", "Mac revenue")

CASES: list[BenchmarkCase] = [
    # ── Category A: Seeded Backbone Multi-Hop Reasoning ──────────────────
    _case(
        "A01", "seeded_multi_hop",
        "How do TSMC advanced packaging yield constraints impact Nvidia's datacenter gross margins?",
        "KNOWN", "NVDA",
        required=("TSMC", "packaging"),
        min_hops=2,
        note="Canonical tier-1 bottleneck: single foundry, single packaging path.",
    ),
    _case(
        "A02", "seeded_multi_hop",
        "What is Apple's iPhone supply chain concentration and which tier-1 suppliers are exposed?",
        "KNOWN", "AAPL",
        required=("supply chain",),
        min_hops=2,
        forbidden=("Rivian", "JPMorgan"),
    ),
    _case(
        "A03", "seeded_multi_hop",
        "How does Microsoft's Azure datacenter capital expenditure pressure Intelligent Cloud operating margins?",
        "KNOWN", "MSFT",
        required=("Azure", "capital"),
        min_hops=2,
    ),
    _case(
        "A04", "seeded_multi_hop",
        "What two-hop dependencies constrain Nvidia H100 supply and extend delivery lead times?",
        "KNOWN", "NVDA",
        required=("H100",),
        min_hops=2,
    ),
    _case(
        "A05", "seeded_multi_hop",
        "Compare Apple's Services and Products segment gross margin exposure to hardware cycle risk.",
        "KNOWN", "AAPL",
        required=("Services", "Products"),
        min_hops=2,
    ),
    _case(
        "A06", "seeded_multi_hop",
        "How does Microsoft's More Personal Computing segment offset Intelligent Cloud margin dilution?",
        "KNOWN", "MSFT",
        required=("Intelligent Cloud",),
        min_hops=2,
    ),
    _case(
        "A07", "seeded_multi_hop",
        "Trace Nvidia's CoWoS advanced packaging allocation through TSMC to accelerator shipment capacity.",
        "KNOWN", "NVDA",
        required=("CoWoS", "TSMC"),
        min_hops=2,
    ),
    _case(
        "A08", "seeded_multi_hop",
        "What is Apple's Greater China revenue exposure and which segments carry the tariff risk?",
        "KNOWN", "AAPL",
        required=("China",),
        min_hops=2,
    ),
    _case(
        "A09", "seeded_multi_hop",
        "How does Microsoft's AI datacenter investment phase change near-term operating income?",
        "KNOWN", "MSFT",
        required=("operating income",),
        min_hops=2,
    ),
    _case(
        "A10", "seeded_multi_hop",
        "How much Nvidia China datacenter revenue is exposed to US export control tightening?",
        "KNOWN", "NVDA",
        required=("export",),
        min_hops=2,
    ),

    # ── Category B: Cold-Start JIT Dynamic Ingestion ──────────────────────
    _case(
        "B01", "cold_start",
        "What does JP MORGAN DO and what are its primary regulatory capital risks?",
        "COLD_START", "JPM",
        forbidden=_NO_APPLE,
        required=("capital",),
        min_hops=1,
        note="Name form, no cashtag: the alias table is the only way this resolves.",
    ),
    _case(
        "B02", "cold_start",
        "What are $RIVN delivery numbers and gross margin trajectory?",
        "COLD_START", "RIVN",
        forbidden=_NO_APPLE,
    ),
    _case(
        "B03", "cold_start",
        "Describe $LCID production capacity and its path to sustained positive gross margin.",
        "COLD_START", "LCID",
        forbidden=_NO_APPLE,
        required=("production",),
    ),
    _case(
        "B04", "cold_start",
        "What proportion of $PLTR revenue comes from US government contracts, and what is the concentration risk?",
        "COLD_START", "PLTR",
        forbidden=_NO_APPLE,
        required=("government",),
    ),
    _case(
        "B05", "cold_start",
        "Explain $ARM's licensing and royalty model and what drives its revenue per design win.",
        "COLD_START", "ARM",
        forbidden=_NO_APPLE,
        required=("licensing",),
    ),
    _case(
        "B06", "cold_start",
        "What is $ASML's EUV lithography position and which customers depend on it for leading-edge nodes?",
        "COLD_START", "ASML",
        forbidden=_NO_APPLE,
        required=("EUV",),
    ),
    _case(
        "B07", "cold_start",
        "How does $SNOW's consumption-based revenue model differ from a seat-based subscription?",
        "COLD_START", "SNOW",
        forbidden=_NO_APPLE,
        required=("consumption",),
    ),
    _case(
        "B08", "cold_start",
        "What are JPMorgan Chase's primary regulatory capital requirements under stress testing?",
        "COLD_START", "JPM",
        forbidden=_NO_APPLE,
        required=("capital",),
    ),
    _case(
        "B09", "cold_start",
        "What is Rivian's manufacturing footprint and which suppliers are single-sourced?",
        "COLD_START", "RIVN",
        forbidden=_NO_APPLE,
        required=("manufacturing",),
    ),
    _case(
        "B10", "cold_start",
        "What does $LCID's Item 1 describe about demand and its direct competitive set?",
        "COLD_START", "LCID",
        forbidden=_NO_APPLE,
    ),
    _case(
        "B11", "cold_start",
        "What royalty rate structure does $ARM disclose, and how concentrated is revenue by licensee?",
        "COLD_START", "ARM",
        forbidden=_NO_APPLE,
        required=("royalt",),
    ),
    _case(
        "B12", "cold_start",
        "What are $SNOW's remaining performance obligations and net revenue retention?",
        "COLD_START", "SNOW",
        forbidden=_NO_APPLE,
        required=("performance obligation",),
    ),
    _case(
        "B13", "cold_start",
        "How does $PLTR describe its customer concentration and deployment risk in Item 1A?",
        "COLD_START", "PLTR",
        forbidden=_NO_APPLE,
        required=("concentration",),
    ),
    _case(
        "B14", "cold_start",
        "What export control and geopolitical risks does $ASML disclose for its EUV business?",
        "COLD_START", "ASML",
        forbidden=_NO_APPLE,
        required=("export",),
    ),
    _case(
        "B15", "cold_start",
        "What are $JPM's stress test capital buffers and CET1 sensitivity to credit losses?",
        "COLD_START", "JPM",
        forbidden=_NO_APPLE,
        required=("capital",),
    ),

    # ── Category C: Executive Transitions & Governance Shocks ─────────────
    _case(
        "C01", "executive_transition",
        "If John Ternus succeeds Tim Cook as Apple CEO, what happens to hardware engineering CapEx and supplier renegotiation leverage?",
        "KNOWN", "AAPL",
        key_personnel=("John Ternus", "Tim Cook"),
        required=("CapEx",),
        min_hops=2,
        note="Speculative: the filing states neither succession nor its effects.",
    ),
    _case(
        "C02", "executive_transition",
        "What key person risk does Apple disclose around senior engineering and design leadership attrition?",
        "KNOWN", "AAPL",
        required=("key person",),
        min_hops=2,
    ),
    _case(
        "C03", "executive_transition",
        "How would a Microsoft AI division leadership transition affect Azure roadmap execution risk?",
        "KNOWN", "MSFT",
        required=("Azure",),
        min_hops=2,
    ),
    _case(
        "C04", "executive_transition",
        "What is Nvidia's disclosed dependence on Jensen Huang and what succession disclosure exists?",
        "KNOWN", "NVDA",
        key_personnel=("Jensen Huang",),
        required=("Huang",),
        min_hops=2,
    ),
    _case(
        "C05", "executive_transition",
        "How does Apple mitigate supply chain dependency on individual supplier executives?",
        "KNOWN", "AAPL",
        required=("supply chain",),
        min_hops=2,
    ),
    _case(
        "C06", "executive_transition",
        "What executive turnover risk does Microsoft disclose in its cloud and AI businesses?",
        "KNOWN", "MSFT",
        required=("executive",),
        min_hops=2,
    ),
    _case(
        "C07", "executive_transition",
        "How would a Nvidia CFO transition affect guidance continuity and disclosure quality?",
        "KNOWN", "NVDA",
        required=("guidance",),
        min_hops=2,
    ),
    _case(
        "C08", "executive_transition",
        "What patent and silicon engineering lineage does Apple disclose, and who holds that expertise?",
        "KNOWN", "AAPL",
        required=("patent",),
        min_hops=2,
    ),
    _case(
        "C09", "executive_transition",
        "How concentrated is Microsoft's AI strategy in named senior leadership and partner relationships?",
        "KNOWN", "MSFT",
        required=("leadership",),
        min_hops=2,
    ),
    _case(
        "C10", "executive_transition",
        "What does Nvidia disclose about founder dependence and board succession planning?",
        "KNOWN", "NVDA",
        key_personnel=("Jensen Huang",),
        required=("succession",),
        min_hops=2,
    ),

    # ── Category D: Cross-Entity Contagion & Second-Order Shocks ─────────
    _case(
        "D01", "contagion",
        "How does a rare gas export restriction in Eastern Europe propagate through $ASML to affect TSMC's 3nm fab capacity?",
        "COLD_START", "ASML",
        required=("gas",),
        min_hops=2,
        note="Routes to the cold-start counterparty; routing is single-entity.",
    ),
    _case(
        "D02", "contagion",
        "How does a TSMC yield shock propagate to Nvidia's data center revenue two hops out?",
        "KNOWN", "NVDA",
        required=("TSMC",),
        min_hops=2,
    ),
    _case(
        "D03", "contagion",
        "How would a China assembly disruption propagate to Apple's iPhone unit economics through its contract manufacturers?",
        "KNOWN", "AAPL",
        required=("China",),
        min_hops=2,
    ),
    _case(
        "D04", "contagion",
        "How does an Azure AI capacity shortfall transmit into Nvidia accelerator demand and Microsoft's cloud backlog?",
        "KNOWN", "MSFT",
        required=("Azure",),
        min_hops=2,
    ),
    _case(
        "D05", "contagion",
        "How would a regional bank funding shock propagate to $JPM's trading and lending revenue?",
        "COLD_START", "JPM",
        min_hops=2,
    ),
    _case(
        "D06", "contagion",
        "How does an EV tax credit repeal propagate to $RIVN's unit demand and cash burn?",
        "COLD_START", "RIVN",
        required=("tax credit",),
        min_hops=2,
    ),
    _case(
        "D07", "contagion",
        "How would a data warehouse consolidation cycle propagate to $SNOW's consumption revenue?",
        "COLD_START", "SNOW",
        min_hops=2,
    ),
    _case(
        "D08", "contagion",
        "How does a smartphone downturn propagate to $ARM's royalty revenue through handset design win timing?",
        "COLD_START", "ARM",
        required=("royalt",),
        min_hops=2,
    ),
    _case(
        "D09", "contagion",
        "How would a federal IT budget contraction propagate to $PLTR's remaining performance obligations?",
        "COLD_START", "PLTR",
        min_hops=2,
    ),
    _case(
        "D10", "contagion",
        "How does a lithium carbonate price spike propagate to $LCID's bill of materials and gross margin?",
        "COLD_START", "LCID",
        required=("lithium",),
        min_hops=2,
    ),

    # ── Category E: Ambiguity, Edge Cases & Negative Controls ─────────────
    _case(
        "E01", "negative_control",
        "How do I bake a chocolate cake?",
        "AMBIGUOUS", None,
        expect_refusal=True,
        note="No issuer, no financial content.",
    ),
    _case(
        "E02", "negative_control",
        "Tell me about the stock market today",
        "AMBIGUOUS", None,
        expect_refusal=True,
        note="Market-level question: names no issuer, so there is nothing to analyze.",
    ),
    _case(
        "E03", "negative_control",
        "Analyze $XYZFAKE and summarize its latest filing",
        "AMBIGUOUS", None,
        expect_refusal=True,
        note="Seven letters: no cashtag match, so it must not be treated as a ticker.",
    ),
    _case(
        "E04", "negative_control",
        "What is the weather in Tokyo this week?",
        "AMBIGUOUS", None,
        expect_refusal=True,
    ),
    _case(
        "E05", "negative_control",
        "net sales and operating income trends",
        "AMBIGUOUS", None,
        expect_refusal=True,
        note="Metrics with no issuer: the anti-hallucination case that used to leak AAPL/MSFT.",
    ),
]


def cases_for_category(category: str) -> list[BenchmarkCase]:
    """Cases in ``category``, or all of them when ``category`` is empty."""
    if not category:
        return list(CASES)
    return [c for c in CASES if c.category == category]


#: Dict view of the dataset, for callers that want the raw records. The
#: dataclass is the source of truth; this is a projection, not a second copy.
BENCHMARK_QUERIES: list[dict[str, Any]] = [asdict(c) for c in CASES]
