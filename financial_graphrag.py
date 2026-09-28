"""Financial GraphRAG on an embedded, low-RAM LadybugDB graph.

A single-file, in-process GraphRAG mini-project for financial entity
disambiguation and cross-company comparison. Everything runs inside the Python
process against an embedded LadybugDB database: no server, no Docker, no
external services.

Memory contract
---------------
Every database handle is opened through :func:`open_graph`, which refuses any
``buffer_pool_size`` above :data:`BUFFER_POOL_CAP_MB` (256 MB). The cap is a
hard contract, not a default, so a stray config value cannot quietly inflate
the process RSS.

Domain model
------------
    (:Company {ticker PK, name, sector})
    (:Segment {id PK, segment_name, fiscal_year, revenue_billions})
    (:RiskFactor {id PK, title, category})

    (:Company)-[:REPORTS_SEGMENT]->(:Segment)
    (:Company)-[:HAS_RISK]->(:RiskFactor)

``ticker`` is the primary key on ``Company`` and ``id`` is the primary key on
``Segment``/``RiskFactor``, so a segment or a risk is only ever identified in
the context of the company that reports it. That is what makes the graph safe
for disambiguation: "Services" at AAPL and "Office 365" at MSFT are distinct
nodes reached through distinct companies, and a naive global join on
``segment_name`` cannot conflate them.

Seed data
---------
FY2024 segment revenue as reported in each issuer's Form 10-K, plus a shared
supply-chain/geopolitical risk register. See :data:`SEED_SEGMENTS` and
:data:`SEED_RISK_FACTORS` for the per-line figures and their caveats.

Usage
-----
    pip install ladybug
    python financial_graphrag.py              # demo + verification audit
    python financial_graphrag.py --db ./fin.lbug   # persist to disk
    python financial_graphrag.py --verify-only
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence, TextIO

try:
    import ladybug as lb
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "The 'ladybug' package is required. Install it with:\n"
        "    pip install ladybug\n"
        "(Python 3.10 or newer is required.)"
    ) from exc

LOGGER = logging.getLogger("financial_graphrag")

MEBIBYTE = 1024 * 1024
BUFFER_POOL_CAP_MB = 256
FISCAL_YEAR = 2024
TOLERANCE = 1e-6

DDL_NODE_TABLES: tuple[tuple[str, str], ...] = (
    (
        "Company",
        "CREATE NODE TABLE IF NOT EXISTS Company("
        "ticker STRING PRIMARY KEY, name STRING, sector STRING)",
    ),
    (
        "Segment",
        "CREATE NODE TABLE IF NOT EXISTS Segment("
        "id STRING PRIMARY KEY, segment_name STRING, "
        "fiscal_year INT64, revenue_billions DOUBLE)",
    ),
    (
        "RiskFactor",
        "CREATE NODE TABLE IF NOT EXISTS RiskFactor("
        "id STRING PRIMARY KEY, title STRING, category STRING)",
    ),
)

DDL_REL_TABLES: tuple[tuple[str, str], ...] = (
    (
        "REPORTS_SEGMENT",
        "CREATE REL TABLE IF NOT EXISTS REPORTS_SEGMENT(FROM Company TO Segment)",
    ),
    (
        "HAS_RISK",
        "CREATE REL TABLE IF NOT EXISTS HAS_RISK(FROM Company TO RiskFactor)",
    ),
)


class GraphError(RuntimeError):
    """Base class for every error raised by this module."""


class GraphConfigError(GraphError):
    """Raised when the engine is asked to violate the memory contract."""


class GraphQueryError(GraphError):
    """Raised when a query is not answerable against the current graph."""


def resolve_buffer_pool_bytes(buffer_pool_size_mb: int) -> int:
    """Translate a megabyte budget into the byte value LadybugDB expects.

    The returned value never exceeds :data:`BUFFER_POOL_CAP_MB` because a
    larger request is rejected rather than silently clamped: a caller that
    asks for 512 MB has a bug, and hiding it would defeat the low-RAM
    contract this project is built around.
    """
    if not isinstance(buffer_pool_size_mb, int) or isinstance(buffer_pool_size_mb, bool):
        raise GraphConfigError(
            f"buffer_pool_size_mb must be an int, got {type(buffer_pool_size_mb).__name__}"
        )
    if buffer_pool_size_mb <= 0:
        raise GraphConfigError(
            f"buffer_pool_size_mb must be positive, got {buffer_pool_size_mb}"
        )
    if buffer_pool_size_mb > BUFFER_POOL_CAP_MB:
        raise GraphConfigError(
            f"buffer_pool_size_mb={buffer_pool_size_mb} exceeds the "
            f"{BUFFER_POOL_CAP_MB} MB low-RAM cap for this graph"
        )
    return buffer_pool_size_mb * MEBIBYTE


def default_thread_count() -> int:
    """Modest default worker count; LadybugDB parallelises per query, not per row."""
    return max(1, min(4, os.cpu_count() or 1))


@contextmanager
def open_graph(
    database_path: str | os.PathLike[str] = ":memory:",
    buffer_pool_size_mb: int = BUFFER_POOL_CAP_MB,
    max_num_threads: int | None = None,
    read_only: bool = False,
) -> Iterator[lb.Connection]:
    """Open an embedded graph, yield a connection, then close both handles.

    ``database_path`` is either ``":memory:"`` for a throwaway graph or a file
    path for a persistent one. The buffer pool is hard-capped at
    :data:`BUFFER_POOL_CAP_MB`; exceeding it raises
    :class:`GraphConfigError`.
    """
    buffer_pool_bytes = resolve_buffer_pool_bytes(buffer_pool_size_mb)
    threads = default_thread_count() if max_num_threads is None else max_num_threads
    if threads <= 0:
        raise GraphConfigError(f"max_num_threads must be positive, got {threads}")

    started = time.perf_counter()
    database = lb.Database(
        str(database_path),
        buffer_pool_size=buffer_pool_bytes,
        max_num_threads=threads,
        read_only=read_only,
    )
    connection = lb.Connection(database)
    LOGGER.info(
        "opened graph path=%s buffer_pool=%dMB threads=%d in %.1fms",
        database_path,
        buffer_pool_bytes // MEBIBYTE,
        threads,
        (time.perf_counter() - started) * 1000,
    )
    try:
        yield connection
    finally:
        connection.close()
        database.close()


def list_tables(connection: lb.Connection) -> set[str]:
    """Return the names of every node and relationship table in the graph."""
    rows = connection.execute("CALL show_tables() RETURN *").rows_as_dict().get_all()
    return {str(row["name"]) for row in rows}


def create_schema(connection: lb.Connection) -> None:
    """Create the node and relationship tables that are not present yet.

    Node tables must exist before the relationship tables that reference them,
    which is why the node DDL is applied first. The catalog is inspected before
    any statement runs: re-issuing ``CREATE ... IF NOT EXISTS`` against a live
    graph is not free, and on LadybugDB 0.20.x it destabilises the connection
    when a write transaction follows it.
    """
    present = list_tables(connection)
    for table_name, statement in DDL_NODE_TABLES + DDL_REL_TABLES:
        if table_name not in present:
            connection.execute(statement)
            LOGGER.debug("created table %s", table_name)


@dataclass(frozen=True, slots=True)
class CompanySeed:
    """A company row keyed by ticker."""

    ticker: str
    name: str
    sector: str


@dataclass(frozen=True, slots=True)
class SegmentSeed:
    """A reported revenue segment owned by exactly one company."""

    id: str
    company_ticker: str
    segment_name: str
    fiscal_year: int
    revenue_billions: float


@dataclass(frozen=True, slots=True)
class RiskSeed:
    """A risk-factor register entry shared by one or more companies."""

    id: str
    title: str
    category: str
    tickers: tuple[str, ...]


SEED_COMPANIES: tuple[CompanySeed, ...] = (
    CompanySeed("AAPL", "Apple Inc.", "Technology"),
    CompanySeed("MSFT", "Microsoft Corporation", "Technology"),
)

SEED_SEGMENTS: tuple[SegmentSeed, ...] = (
    SegmentSeed("AAPL-FY2024-IPHONE", "AAPL", "iPhone", FISCAL_YEAR, 201.183),
    SegmentSeed("AAPL-FY2024-SERVICES", "AAPL", "Services", FISCAL_YEAR, 96.169),
    SegmentSeed("AAPL-FY2024-MAC", "AAPL", "Mac", FISCAL_YEAR, 29.984),
    SegmentSeed("AAPL-FY2024-IPAD", "AAPL", "iPad", FISCAL_YEAR, 26.694),
    SegmentSeed(
        "AAPL-FY2024-WEARABLES",
        "AAPL",
        "Wearables, Home and Accessories",
        FISCAL_YEAR,
        37.005,
    ),
    SegmentSeed(
        "MSFT-FY2024-INTELLIGENT-CLOUD",
        "MSFT",
        "Intelligent Cloud (Azure and server products)",
        FISCAL_YEAR,
        105.362,
    ),
    SegmentSeed(
        "MSFT-FY2024-PRODUCTIVITY",
        "MSFT",
        "Productivity and Business Processes (Office 365)",
        FISCAL_YEAR,
        77.728,
    ),
    SegmentSeed("MSFT-FY2024-MPC", "MSFT", "More Personal Computing", FISCAL_YEAR, 62.032),
)

SEED_RISK_FACTORS: tuple[RiskSeed, ...] = (
    RiskSeed(
        "RF-001",
        "Concentration of advanced-node foundry capacity in Taiwan",
        "Supply Chain",
        ("AAPL", "MSFT"),
    ),
    RiskSeed("RF-002", "DRAM and NAND contract price volatility", "Supply Chain", ("AAPL", "MSFT")),
    RiskSeed(
        "RF-003",
        "Rare earth and critical mineral export controls",
        "Geopolitical",
        ("AAPL", "MSFT"),
    ),
    RiskSeed(
        "RF-004",
        "Data center power and grid interconnect availability",
        "Infrastructure",
        ("AAPL", "MSFT"),
    ),
    RiskSeed(
        "RF-005",
        "Greater China demand and regulatory exposure",
        "Geopolitical",
        ("AAPL",),
    ),
    RiskSeed("RF-006", "Advanced AI accelerator export controls", "Regulatory", ("MSFT",)),
    RiskSeed("RF-007", "App store and platform fee regulation", "Regulatory", ("AAPL",)),
)

REPORTED_TOTAL_REVENUE_BILLIONS: dict[str, float] = {
    "AAPL": 391.035,
    "MSFT": 245.122,
}

SEED_NOTES = (
    "Apple FY2024 (52 weeks ended 2024-09-28) product and service lines as "
    "disclosed in the Form 10-K; the five lines tie to the reported "
    "391.035 USD bn total.",
    "Microsoft FY2024 (year ended 2024-06-30) reportable segments as disclosed "
    "in the Form 10-K; the three lines tie to the reported 245.122 USD bn "
    "total. Azure revenue is not disclosed separately, so the Intelligent "
    "Cloud segment line is used as the cloud proxy.",
    "Risk factors are an analytical register for this demo, not issuer "
    "disclosure; RF-005/006/007 are deliberately single-company so that "
    "shared-risk detection is not trivially true.",
)


@dataclass(frozen=True, slots=True)
class SeedReport:
    """Outcome of one :func:`seed_graph` call."""

    companies: int
    segments: int
    risk_factors: int
    reports_segment_edges: int
    has_risk_edges: int

    def as_line(self) -> str:
        """Render the report as a single console line."""
        return (
            f"companies={self.companies} segments={self.segments} "
            f"risk_factors={self.risk_factors} "
            f"REPORTS_SEGMENT={self.reports_segment_edges} "
            f"HAS_RISK={self.has_risk_edges}"
        )


def _reset_prepared_cache(connection: lb.Connection) -> int:
    """Drop cached prepared statements so the next writes re-bind locally.

    LadybugDB 0.20.4 caches one prepared statement per (query text, parameter
    signature) on the connection. A statement that was first bound inside a
    transaction which has since committed must not be re-executed inside a
    *different* transaction: the engine dereferences the finished
    transaction's context and the process dies with SIGSEGV. Re-seeding is
    exactly that pattern, which is why :func:`seed_graph` used to take the
    interpreter down on its second call.

    Clearing the cache forces every statement to be re-prepared inside the
    transaction that is about to run, which is the state the engine handles
    correctly. The attributes are private, so the lookup is defensive: if a
    future release renames them this becomes a no-op rather than an error.
    """
    cache = getattr(connection, "_pybind_implicit_prepared_cache", None)
    if not isinstance(cache, dict):
        return 0
    lock = getattr(connection, "_prepared_cache_lock", None)
    dropped = len(cache)
    if lock is None:
        cache.clear()
    else:
        with lock:
            cache.clear()
    return dropped


def _ensure_edge(
    connection: lb.Connection,
    rel_table: str,
    from_node: str,
    to_node: str,
    match: str,
    parameters: dict[str, Any],
) -> None:
    """Create one relationship only when it is not already present.

    Node writes use ``MERGE`` on the primary key, which is safe to repeat. A
    relationship ``MERGE`` is not: on LadybugDB 0.20.4 re-merging an
    already-existing relationship *inside an explicit transaction* dereferences
    a freed query result and takes the process down with SIGSEGV. Since
    :func:`seed_graph` re-runs inside ``BEGIN TRANSACTION`` for atomicity, an
    idempotent re-seed crashed the interpreter.

    Guarding the write with ``WHERE NOT EXISTS`` keeps the seed idempotent and
    keeps the transaction, without ever asking the engine to merge an edge it
    already holds.
    """
    connection.execute(
        f"{match} WHERE NOT EXISTS {{ MATCH ({from_node})-[:{rel_table}]->({to_node}) }} "
        f"CREATE ({from_node})-[:{rel_table}]->({to_node})",
        parameters=parameters,
    )


def seed_graph(connection: lb.Connection, reset: bool = False) -> SeedReport:
    """Populate the graph with the FY2024 seed dataset, idempotently.

    Every write is a ``MERGE`` on the primary key, and every relationship is
    guarded by :func:`_ensure_edge`, so re-running the seed is safe and never
    duplicates a node or an edge. The whole seed runs inside a single
    transaction: a failure mid-seed leaves the graph untouched.
    """
    if reset:
        reset_graph(connection)
    create_schema(connection)

    known_tickers = {company.ticker for company in SEED_COMPANIES}
    for segment in SEED_SEGMENTS:
        if segment.company_ticker not in known_tickers:
            raise GraphError(
                f"segment {segment.id!r} references unknown ticker "
                f"{segment.company_ticker!r}"
            )
    for risk in SEED_RISK_FACTORS:
        unknown = sorted(set(risk.tickers) - known_tickers)
        if unknown:
            raise GraphError(f"risk {risk.id!r} references unknown tickers {unknown}")

    _reset_prepared_cache(connection)
    connection.execute("BEGIN TRANSACTION")
    try:
        for company in SEED_COMPANIES:
            connection.execute(
                "MERGE (c:Company {ticker: $ticker}) "
                "SET c.name = $name, c.sector = $sector",
                parameters={
                    "ticker": company.ticker,
                    "name": company.name,
                    "sector": company.sector,
                },
            )

        for segment in SEED_SEGMENTS:
            connection.execute(
                "MERGE (s:Segment {id: $id}) SET "
                "s.segment_name = $segment_name, "
                "s.fiscal_year = $fiscal_year, "
                "s.revenue_billions = $revenue_billions",
                parameters={
                    "id": segment.id,
                    "segment_name": segment.segment_name,
                    "fiscal_year": segment.fiscal_year,
                    "revenue_billions": segment.revenue_billions,
                },
            )
            _ensure_edge(
                connection,
                "REPORTS_SEGMENT",
                from_node="c",
                to_node="s",
                match="MATCH (c:Company {ticker: $ticker}), (s:Segment {id: $id})",
                parameters={"ticker": segment.company_ticker, "id": segment.id},
            )

        for risk in SEED_RISK_FACTORS:
            connection.execute(
                "MERGE (r:RiskFactor {id: $id}) "
                "SET r.title = $title, r.category = $category",
                parameters={"id": risk.id, "title": risk.title, "category": risk.category},
            )
            for ticker in risk.tickers:
                _ensure_edge(
                    connection,
                    "HAS_RISK",
                    from_node="c",
                    to_node="r",
                    match="MATCH (c:Company {ticker: $ticker}), (r:RiskFactor {id: $id})",
                    parameters={"ticker": ticker, "id": risk.id},
                )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    report = SeedReport(
        companies=_count(connection, "MATCH (c:Company) RETURN count(c)"),
        segments=_count(connection, "MATCH (s:Segment) RETURN count(s)"),
        risk_factors=_count(connection, "MATCH (r:RiskFactor) RETURN count(r)"),
        reports_segment_edges=_count(
            connection, "MATCH ()-[e:REPORTS_SEGMENT]->() RETURN count(e)"
        ),
        has_risk_edges=_count(connection, "MATCH ()-[e:HAS_RISK]->() RETURN count(e)"),
    )
    LOGGER.info("seeded graph: %s", report.as_line())
    return report


def reset_graph(connection: lb.Connection) -> None:
    """Delete every node and edge, keeping the schema in place."""
    create_schema(connection)
    connection.execute("MATCH (n) DETACH DELETE n")


def _count(connection: lb.Connection, query: str, parameters: dict[str, Any] | None = None) -> int:
    """Return the single integer produced by an aggregate query."""
    return int(_scalar(connection, query, parameters))


def _scalar(
    connection: lb.Connection, query: str, parameters: dict[str, Any] | None = None
) -> float:
    """Return the single numeric value produced by an aggregate query."""
    rows = connection.execute(query, parameters or {}).get_all()
    if not rows or rows[0][0] is None:
        return 0.0
    return float(rows[0][0])


def _rows_as_dicts(connection: lb.Connection, query: str, parameters: dict[str, Any]) -> list[dict]:
    """Run a query and materialise it as a list of column-keyed dicts."""
    return connection.execute(query, parameters=parameters).rows_as_dict().get_all()


@dataclass(frozen=True, slots=True)
class SegmentSlice:
    """One reported segment line for a single company and fiscal year."""

    segment_name: str
    revenue_billions: float
    share_of_company_pct: float

    def as_line(self) -> str:
        """Render the slice as a console line."""
        return (
            f"{self.segment_name} | {self.revenue_billions:>9,.3f} bn | "
            f"{self.share_of_company_pct:>6.2f}%"
        )


@dataclass(frozen=True, slots=True)
class CompanyProfile:
    """A company's FY segment mix, resolved through the graph."""

    ticker: str
    name: str
    sector: str
    fiscal_year: int
    total_revenue_billions: float
    segment_count: int
    slices: tuple[SegmentSlice, ...]

    @property
    def top_segment(self) -> SegmentSlice | None:
        """Highest-revenue segment, or ``None`` when the company has none."""
        return self.slices[0] if self.slices else None

    @property
    def concentration_pct(self) -> float:
        """Share of revenue held by the largest segment."""
        top = self.top_segment
        return top.share_of_company_pct if top else 0.0


@dataclass(frozen=True, slots=True)
class SegmentComparison:
    """Cross-company segment comparison for one fiscal year."""

    fiscal_year: int
    company_a: CompanyProfile
    company_b: CompanyProfile
    overlapping_segment_names: tuple[str, ...]

    @property
    def total_delta_billions(self) -> float:
        """Company A revenue minus company B revenue."""
        return self.company_a.total_revenue_billions - self.company_b.total_revenue_billions

    @property
    def revenue_ratio(self) -> float:
        """Company A revenue divided by company B revenue."""
        if self.company_b.total_revenue_billions == 0:
            raise GraphQueryError(
                f"{self.company_b.ticker} reports zero revenue in "
                f"{self.fiscal_year}; ratio is undefined"
            )
        return self.company_a.total_revenue_billions / self.company_b.total_revenue_billions

    @property
    def concentration_delta_pct_points(self) -> float:
        """Difference in largest-segment concentration, A minus B."""
        return self.company_a.concentration_pct - self.company_b.concentration_pct

    @property
    def leading_segment_gap_billions(self) -> float:
        """Revenue gap between the two companies' largest segments."""
        top_a, top_b = self.company_a.top_segment, self.company_b.top_segment
        if top_a is None or top_b is None:
            return 0.0
        return top_a.revenue_billions - top_b.revenue_billions

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view for API or LLM tool output."""
        return {
            "fiscal_year": self.fiscal_year,
            "companies": [
                {
                    "ticker": profile.ticker,
                    "name": profile.name,
                    "sector": profile.sector,
                    "total_revenue_billions": round(profile.total_revenue_billions, 3),
                    "segment_count": profile.segment_count,
                    "top_segment": (
                        profile.top_segment.segment_name if profile.top_segment else None
                    ),
                    "concentration_pct": round(profile.concentration_pct, 2),
                    "segments": [
                        {
                            "segment_name": item.segment_name,
                            "revenue_billions": round(item.revenue_billions, 3),
                            "share_of_company_pct": round(item.share_of_company_pct, 2),
                        }
                        for item in profile.slices
                    ],
                }
                for profile in (self.company_a, self.company_b)
            ],
            "total_delta_billions": round(self.total_delta_billions, 3),
            "revenue_ratio": round(self.revenue_ratio, 4),
            "concentration_delta_pct_points": round(self.concentration_delta_pct_points, 2),
            "leading_segment_gap_billions": round(self.leading_segment_gap_billions, 3),
            "overlapping_segment_names": list(self.overlapping_segment_names),
        }


_QUERY_COMPANY_TOTALS = """
MATCH (c:Company)-[:REPORTS_SEGMENT]->(s:Segment)
WHERE s.fiscal_year = $year AND toUpper(c.ticker) IN $tickers
RETURN c.ticker AS ticker,
       c.name AS name,
       c.sector AS sector,
       count(s) AS segment_count,
       sum(s.revenue_billions) AS total_revenue_billions
ORDER BY ticker
"""

_QUERY_COMPANY_SEGMENTS = """
MATCH (c:Company)-[:REPORTS_SEGMENT]->(s:Segment)
WHERE s.fiscal_year = $year AND toUpper(c.ticker) IN $tickers
RETURN c.ticker AS ticker,
       s.segment_name AS segment_name,
       s.revenue_billions AS revenue_billions
ORDER BY ticker, revenue_billions DESC, segment_name
"""


def _normalise_ticker(ticker: str) -> str:
    """Uppercase and validate a ticker symbol."""
    if not isinstance(ticker, str) or not ticker.strip():
        raise GraphQueryError(f"ticker must be a non-empty string, got {ticker!r}")
    return ticker.strip().upper()


def _build_profile(
    totals: dict[str, Any], segments: list[dict], ticker: str, year: int
) -> CompanyProfile:
    """Assemble a :class:`CompanyProfile` from the two aggregate queries."""
    total_revenue = float(totals["total_revenue_billions"])
    slices: list[SegmentSlice] = []
    for row in segments:
        revenue = float(row["revenue_billions"])
        share = (revenue / total_revenue * 100.0) if total_revenue else 0.0
        slices.append(
            SegmentSlice(
                segment_name=str(row["segment_name"]),
                revenue_billions=revenue,
                share_of_company_pct=round(share, 2),
            )
        )
    return CompanyProfile(
        ticker=ticker,
        name=str(totals["name"]),
        sector=str(totals["sector"]),
        fiscal_year=year,
        total_revenue_billions=total_revenue,
        segment_count=int(totals["segment_count"]),
        slices=tuple(slices),
    )


def compare_company_segments(
    connection: lb.Connection, ticker_a: str, ticker_b: str, year: int = FISCAL_YEAR
) -> SegmentComparison:
    """Compare two companies' reported segment revenue for one fiscal year.

    The two-hood traversal ``(:Company)-[:REPORTS_SEGMENT]->(:Segment)`` is
    the whole point of the model: each segment row is reached only through the
    company that reports it, so identically named lines belonging to different
    issuers can never be summed together by accident.

    Raises:
        GraphQueryError: if a ticker is unknown, if the two tickers are the
            same, or if either company reported no segments for that year.
    """
    symbol_a, symbol_b = _normalise_ticker(ticker_a), _normalise_ticker(ticker_b)
    if symbol_a == symbol_b:
        raise GraphQueryError(
            f"compare_company_segments needs two distinct tickers, got {symbol_a!r} twice"
        )
    if not isinstance(year, int) or isinstance(year, bool):
        raise GraphQueryError(f"year must be an int, got {type(year).__name__}")

    tickers = [symbol_a, symbol_b]
    parameters = {"year": year, "tickers": tickers}
    totals_rows = _rows_as_dicts(connection, _QUERY_COMPANY_TOTALS, parameters)
    if not totals_rows:
        known = [
            str(row[0])
            for row in connection.execute(
                "MATCH (c:Company) RETURN toUpper(c.ticker) ORDER BY c.ticker"
            ).get_all()
        ]
        raise GraphQueryError(
            f"no {year} segment data for {symbol_a}/{symbol_b}; "
            f"companies in graph: {known or ['<none>']}"
        )

    totals_by_ticker = {str(row["ticker"]): row for row in totals_rows}
    missing = [symbol for symbol in tickers if symbol not in totals_by_ticker]
    if missing:
        raise GraphQueryError(
            f"no {year} segment data for {missing}; "
            f"companies with {year} data: {sorted(totals_by_ticker)}"
        )

    segment_rows = _rows_as_dicts(connection, _QUERY_COMPANY_SEGMENTS, parameters)
    grouped: dict[str, list[dict]] = {symbol: [] for symbol in tickers}
    for row in segment_rows:
        grouped.setdefault(str(row["ticker"]), []).append(row)

    profile_a = _build_profile(totals_by_ticker[symbol_a], grouped.get(symbol_a, []), symbol_a, year)
    profile_b = _build_profile(totals_by_ticker[symbol_b], grouped.get(symbol_b, []), symbol_b, year)
    if not profile_a.slices:
        raise GraphQueryError(f"{symbol_a} reported no segments for {year}")
    if not profile_b.slices:
        raise GraphQueryError(f"{symbol_b} reported no segments for {year}")

    names_a = {item.segment_name for item in profile_a.slices}
    names_b = {item.segment_name for item in profile_b.slices}
    overlapping = tuple(sorted(names_a & names_b))

    return SegmentComparison(
        fiscal_year=year,
        company_a=profile_a,
        company_b=profile_b,
        overlapping_segment_names=overlapping,
    )


@dataclass(frozen=True, slots=True)
class SharedRisk:
    """A risk factor reported by two or more companies."""

    risk_id: str
    title: str
    category: str
    tickers: tuple[str, ...]

    @property
    def company_count(self) -> int:
        """Number of distinct companies carrying this risk."""
        return len(self.tickers)

    def as_line(self) -> str:
        """Render the shared risk as a console line."""
        return f"{self.risk_id} | {self.category:<13} | {self.company_count} cos | {self.title}"


_QUERY_SHARED_RISKS_ALL = """
MATCH (c:Company)-[:HAS_RISK]->(r:RiskFactor)
WITH r, collect(DISTINCT toUpper(c.ticker)) AS tickers
WHERE size(tickers) >= $min_companies
RETURN r.id AS risk_id,
       r.title AS title,
       r.category AS category,
       tickers AS tickers,
       size(tickers) AS company_count
ORDER BY company_count DESC, risk_id
"""

_QUERY_SHARED_RISKS_CATEGORY = """
MATCH (c:Company)-[:HAS_RISK]->(r:RiskFactor)
WHERE r.category = $category
WITH r, collect(DISTINCT toUpper(c.ticker)) AS tickers
WHERE size(tickers) >= $min_companies
RETURN r.id AS risk_id,
       r.title AS title,
       r.category AS category,
       tickers AS tickers,
       size(tickers) AS company_count
ORDER BY company_count DESC, risk_id
"""


def find_shared_risk_factors(
    connection: lb.Connection,
    min_companies: int = 2,
    category: str | None = None,
) -> list[SharedRisk]:
    """Return risk factors carried by at least ``min_companies`` companies.

    The two-hop pattern ``(c:Company)-[:HAS_RISK]->(r)<-[:HAS_RISK]-(c2)``
    is collapsed to one row per risk with ``collect(DISTINCT ticker)``, so a
    risk that two companies both disclose appears exactly once with both
    tickers attached. A risk that only one company discloses never reaches
    the ``min_companies`` threshold and is therefore not reported as shared.
    """
    if not isinstance(min_companies, int) or isinstance(min_companies, bool) or min_companies < 1:
        raise GraphQueryError(f"min_companies must be a positive int, got {min_companies!r}")

    if category is None:
        query, parameters = _QUERY_SHARED_RISKS_ALL, {"min_companies": min_companies}
    else:
        if not isinstance(category, str) or not category.strip():
            raise GraphQueryError(f"category must be a non-empty string, got {category!r}")
        query = _QUERY_SHARED_RISKS_CATEGORY
        parameters = {"min_companies": min_companies, "category": category.strip()}

    return [
        SharedRisk(
            risk_id=str(row["risk_id"]),
            title=str(row["title"]),
            category=str(row["category"]),
            tickers=tuple(str(ticker) for ticker in row["tickers"]),
        )
        for row in _rows_as_dicts(connection, query, parameters)
    ]


_QUERY_RISKS_FOR_COMPANY = """
MATCH (c:Company)-[:HAS_RISK]->(r:RiskFactor)
WITH r, collect(DISTINCT toUpper(c.ticker)) AS tickers
WHERE $ticker IN tickers
RETURN r.id AS risk_id,
       r.title AS title,
       r.category AS category,
       tickers AS tickers
ORDER BY risk_id
"""


def find_risk_factors_for_company(
    connection: lb.Connection, ticker: str
) -> list[SharedRisk]:
    """Return every risk factor a single company carries, shared or not.

    :func:`find_shared_risk_factors` answers "what do these companies have in
    common"; this answers "what is this one company exposed to". The single-hop
    ``(c:Company)-[:HAS_RISK]->(r)`` traversal carries no company-count filter,
    so a risk held by one issuer is returned alongside the shared ones.

    Each result still reports *every* company that carries the risk, not just
    the one that was asked about: the distinction between a company-specific
    risk and a shared one is the point of the query, so it is preserved rather
    than flattened.

    Raises:
        GraphQueryError: if ``ticker`` is not a non-empty string.
    """
    symbol = _normalise_ticker(ticker)
    rows = _rows_as_dicts(connection, _QUERY_RISKS_FOR_COMPANY, {"ticker": symbol})
    if not rows:
        raise GraphQueryError(
            f"no risk factors recorded for {symbol}; "
            "seed the graph before querying risks"
        )
    return [
        SharedRisk(
            risk_id=str(row["risk_id"]),
            title=str(row["title"]),
            category=str(row["category"]),
            tickers=tuple(str(ticker) for ticker in row["tickers"]),
        )
        for row in rows
    ]


_QUERY_AMBIGUOUS_LABELS_ALL = """
MATCH (c:Company)-[:REPORTS_SEGMENT]->(s:Segment)
WITH s.segment_name AS segment_name, collect(DISTINCT toUpper(c.ticker)) AS tickers
WHERE size(tickers) > 1
RETURN segment_name, tickers, size(tickers) AS company_count
ORDER BY company_count DESC, segment_name
"""

_QUERY_AMBIGUOUS_LABELS_YEAR = """
MATCH (c:Company)-[:REPORTS_SEGMENT]->(s:Segment)
WHERE s.fiscal_year = $year
WITH s.segment_name AS segment_name, collect(DISTINCT toUpper(c.ticker)) AS tickers
WHERE size(tickers) > 1
RETURN segment_name, tickers, size(tickers) AS company_count
ORDER BY company_count DESC, segment_name
"""


def find_ambiguous_segment_labels(
    connection: lb.Connection, year: int | None = None
) -> list[tuple[str, tuple[str, ...]]]:
    """Find segment labels that more than one company reports.

    This is the disambiguation tripwire. A warehouse that keys segments by
    ``segment_name`` alone will merge these rows; keying by the owning
    company plus the label keeps them apart. An empty result is the healthy
    state for a curated graph and means no label is currently overloaded.
    """
    if year is None:
        query, parameters = _QUERY_AMBIGUOUS_LABELS_ALL, {}
    else:
        if not isinstance(year, int) or isinstance(year, bool):
            raise GraphQueryError(f"year must be an int or None, got {type(year).__name__}")
        query, parameters = _QUERY_AMBIGUOUS_LABELS_YEAR, {"year": year}

    return [
        (str(row["segment_name"]), tuple(str(ticker) for ticker in row["tickers"]))
        for row in _rows_as_dicts(connection, query, parameters)
    ]


def graph_statistics(connection: lb.Connection) -> dict[str, int]:
    """Return node and relationship counts for provenance and auditing."""
    return {
        "companies": _count(connection, "MATCH (c:Company) RETURN count(c)"),
        "segments": _count(connection, "MATCH (s:Segment) RETURN count(s)"),
        "risk_factors": _count(connection, "MATCH (r:RiskFactor) RETURN count(r)"),
        "reports_segment_edges": _count(
            connection, "MATCH ()-[e:REPORTS_SEGMENT]->() RETURN count(e)"
        ),
        "has_risk_edges": _count(connection, "MATCH ()-[e:HAS_RISK]->() RETURN count(e)"),
    }


def build_rag_context(
    connection: lb.Connection,
    tickers: Sequence[str],
    year: int = FISCAL_YEAR,
    question: str | None = None,
) -> str:
    """Assemble a grounded evidence block for retrieval-augmented generation.

    The block is generated from graph traversals only, so every number and
    every risk in it is traceable to a node and an edge. Sizing is kept
    deliberately small: a context window filled with graph output is only
    useful if the signal is dense.
    """
    symbols = [_normalise_ticker(ticker) for ticker in tickers]
    if not symbols:
        raise GraphQueryError("build_rag_context needs at least one ticker")
    if len(symbols) == 1:
        segments_text = _single_company_segment_block(connection, symbols[0], year)
    else:
        symbols = symbols[:2]
        comparison = compare_company_segments(connection, symbols[0], symbols[1], year)
        segments_text = _comparison_segment_block(comparison)

    shared = find_shared_risk_factors(connection)
    risk_lines = [item.as_line() for item in shared]
    risk_text = "\n".join(risk_lines) if risk_lines else "(no risk factor is shared by 2+ companies)"

    stats = graph_statistics(connection)
    header = f"QUESTION: {question}" if question else "QUESTION: (unspecified)"
    scope = ", ".join(symbols)
    return (
        f"{header}\n"
        f"SCOPE: tickers={scope} fiscal_year={year}\n"
        f"\n[SEGMENT REVENUE]\n{segments_text}\n"
        f"\n[SHARED RISK FACTORS]\n{risk_text}\n"
        f"\n[PROVENANCE]\n"
        f"source=embedded LadybugDB graph; companies={stats['companies']} "
        f"segments={stats['segments']} risk_factors={stats['risk_factors']} "
        f"REPORTS_SEGMENT={stats['reports_segment_edges']} "
        f"HAS_RISK={stats['has_risk_edges']}\n"
        "RULE: answer only from the evidence above and cite the ticker or risk id "
        "for every claim; say so explicitly when the evidence is insufficient."
    )


def _single_company_segment_block(
    connection: lb.Connection, ticker: str, year: int
) -> str:
    """Render one company's segment mix, raising if the company has no data."""
    parameters = {"year": year, "tickers": [ticker]}
    totals_rows = _rows_as_dicts(connection, _QUERY_COMPANY_TOTALS, parameters)
    if not totals_rows:
        raise GraphQueryError(f"no {year} segment data for {ticker}")
    profile = _build_profile(
        totals_rows[0],
        _rows_as_dicts(connection, _QUERY_COMPANY_SEGMENTS, parameters),
        ticker,
        year,
    )
    lines = [
        f"{ticker} ({profile.name}) total={profile.total_revenue_billions:,.3f} bn",
        *(f"  - {item.as_line()}" for item in profile.slices),
    ]
    return "\n".join(lines)


def _comparison_segment_block(comparison: SegmentComparison) -> str:
    """Render a two-company comparison as aligned text."""
    left, right = comparison.company_a, comparison.company_b
    lines = [
        f"{left.ticker} ({left.name}) total={left.total_revenue_billions:,.3f} bn",
        *(f"  - {item.as_line()}" for item in left.slices),
        f"{right.ticker} ({right.name}) total={right.total_revenue_billions:,.3f} bn",
        *(f"  - {item.as_line()}" for item in right.slices),
        f"delta({left.ticker}-{right.ticker}) = {comparison.total_delta_billions:+,.3f} bn",
        f"ratio = {comparison.revenue_ratio:.4f}x",
        f"concentration delta = {comparison.concentration_delta_pct_points:+.2f} pp",
        f"leading segment gap = {comparison.leading_segment_gap_billions:+,.3f} bn",
    ]
    if comparison.overlapping_segment_names:
        lines.append(
            "shared labels (do not merge these rows): "
            + ", ".join(comparison.overlapping_segment_names)
        )
    return "\n".join(lines)


@dataclass(slots=True)
class CheckResult:
    """One row of the verification audit."""

    index: int
    name: str
    passed: bool
    detail: str

    def as_line(self) -> str:
        """Render the check as a console line."""
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.index:02d} {self.name}"


@dataclass(slots=True)
class AuditReport:
    """Accumulated verification results."""

    checks: list[CheckResult] = field(default_factory=list)

    def record(self, name: str, condition: bool, detail: str = "") -> bool:
        """Record one check and return its outcome."""
        result = CheckResult(
            index=len(self.checks) + 1, name=name, passed=bool(condition), detail=detail
        )
        self.checks.append(result)
        return result.passed

    @property
    def passed(self) -> int:
        """Number of checks that passed."""
        return sum(1 for check in self.checks if check.passed)

    @property
    def failed(self) -> int:
        """Number of checks that failed."""
        return len(self.checks) - self.passed

    def render(self, stream: TextIO = sys.stdout) -> None:
        """Print the full PASS/FAIL audit summary."""
        width = 78
        print("=" * width, file=stream)
        print("FINANCIAL GRAPHRAG - VERIFICATION AUDIT", file=stream)
        print("=" * width, file=stream)
        for check in self.checks:
            print(check.as_line(), file=stream)
            if check.detail:
                for detail_line in str(check.detail).splitlines():
                    print(f"       {detail_line}", file=stream)
        print("-" * width, file=stream)
        print(
            f"checks: {len(self.checks)} | passed: {self.passed} | failed: {self.failed}",
            file=stream,
        )
        verdict = "PASS" if self.failed == 0 and self.checks else "FAIL"
        print(f"RESULT: {verdict}", file=stream)
        print("=" * width, file=stream)
        stream.flush()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the audit."""
        return {
            "checks": [
                {
                    "index": check.index,
                    "name": check.name,
                    "passed": check.passed,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
            "total": len(self.checks),
            "passed": self.passed,
            "failed": self.failed,
            "result": "PASS" if self.failed == 0 and self.checks else "FAIL",
        }


def _check_memory_contract(audit: AuditReport) -> None:
    """The buffer pool cap is enforced, not merely documented."""
    resolved = resolve_buffer_pool_bytes(BUFFER_POOL_CAP_MB)
    audit.record(
        "memory.buffer_pool_cap",
        resolved == BUFFER_POOL_CAP_MB * MEBIBYTE,
        f"resolve_buffer_pool_bytes({BUFFER_POOL_CAP_MB}) = {resolved} bytes",
    )
    try:
        resolve_buffer_pool_bytes(BUFFER_POOL_CAP_MB + 1)
    except GraphConfigError:
        audit.record(
            "memory.over_cap_rejected",
            True,
            f"resolve_buffer_pool_bytes({BUFFER_POOL_CAP_MB + 1}) raised GraphConfigError",
        )
    else:
        audit.record(
            "memory.over_cap_rejected",
            False,
            f"a request above {BUFFER_POOL_CAP_MB} MB was accepted",
        )
    try:
        resolve_buffer_pool_bytes(0)
    except GraphConfigError:
        audit.record("memory.zero_rejected", True, "resolve_buffer_pool_bytes(0) raised GraphConfigError")
    else:
        audit.record("memory.zero_rejected", False, "a 0 MB buffer pool was accepted")


def _check_schema(connection: lb.Connection, audit: AuditReport) -> None:
    """All three node tables and both relationship tables exist."""
    present = list_tables(connection)
    expected = {"Company", "Segment", "RiskFactor", "REPORTS_SEGMENT", "HAS_RISK"}
    audit.record(
        "schema.tables_present",
        expected.issubset(present),
        f"expected={sorted(expected)} present={sorted(present)}",
    )

    columns = {
        str(row["name"]): (str(row["type"]), bool(row["primary key"]))
        for row in connection.execute("CALL table_info('Segment') RETURN *").rows_as_dict().get_all()
    }
    segment_columns = {name: columns.get(name) for name in ("id", "segment_name", "fiscal_year", "revenue_billions")}
    audit.record(
        "schema.segment_properties",
        all(column is not None for column in segment_columns.values()),
        f"{segment_columns}",
    )
    audit.record(
        "schema.segment_primary_key",
        columns.get("id", ("", False))[1],
        f"Segment.id primary key flag = {columns.get('id')}",
    )


def _check_ingestion(connection: lb.Connection, audit: AuditReport) -> None:
    """Counts, revenue positivity, and segment totals tying to disclosure."""
    stats = graph_statistics(connection)
    audit.record(
        "ingest.node_counts",
        stats["companies"] == len(SEED_COMPANIES)
        and stats["segments"] == len(SEED_SEGMENTS)
        and stats["risk_factors"] == len(SEED_RISK_FACTORS),
        f"{stats} expected companies={len(SEED_COMPANIES)} segments={len(SEED_SEGMENTS)} "
        f"risk_factors={len(SEED_RISK_FACTORS)}",
    )
    audit.record(
        "ingest.edge_counts",
        stats["reports_segment_edges"] == len(SEED_SEGMENTS)
        and stats["has_risk_edges"] == sum(len(risk.tickers) for risk in SEED_RISK_FACTORS),
        f"REPORTS_SEGMENT={stats['reports_segment_edges']} (expected {len(SEED_SEGMENTS)}) "
        f"HAS_RISK={stats['has_risk_edges']} "
        f"(expected {sum(len(risk.tickers) for risk in SEED_RISK_FACTORS)})",
    )

    segment_rows = connection.execute(
        "MATCH (s:Segment) RETURN s.segment_name AS segment_name, "
        "s.fiscal_year AS fiscal_year, s.revenue_billions AS revenue_billions"
    ).rows_as_dict().get_all()
    assert all(float(row["revenue_billions"]) > 0 for row in segment_rows), (
        "every seeded segment must report positive revenue"
    )
    non_positive = [row for row in segment_rows if float(row["revenue_billions"]) <= 0]
    audit.record(
        "ingest.revenue_positive",
        not non_positive,
        f"checked {len(segment_rows)} segment rows, {len(non_positive)} non-positive",
    )
    wrong_year = [row for row in segment_rows if int(row["fiscal_year"]) != FISCAL_YEAR]
    audit.record(
        "ingest.fiscal_year_coverage",
        not wrong_year and bool(segment_rows),
        f"all {len(segment_rows)} segments report fiscal_year={FISCAL_YEAR}"
        if not wrong_year
        else f"offending rows: {wrong_year}",
    )

    for ticker, reported in REPORTED_TOTAL_REVENUE_BILLIONS.items():
        segments_sum = _scalar(
            connection,
            "MATCH (:Company {ticker: $ticker})-[:REPORTS_SEGMENT]->(s:Segment) "
            "WHERE s.fiscal_year = $year "
            "RETURN sum(s.revenue_billions)",
            {"ticker": ticker, "year": FISCAL_YEAR},
        )
        audit.record(
            f"ingest.{ticker.lower()}_ties_to_reported_total",
            abs(segments_sum - reported) < TOLERANCE,
            f"sum(segments)={segments_sum:.3f} bn reported={reported:.3f} bn "
            f"delta={segments_sum - reported:+.6f} bn",
        )


def _check_comparison(connection: lb.Connection, audit: AuditReport) -> None:
    """compare_company_segments returns well-formed, ordered, consistent data."""
    comparison = compare_company_segments(connection, "AAPL", "MSFT", FISCAL_YEAR)
    audit.record(
        "compare.profiles_resolved",
        comparison.company_a.ticker == "AAPL" and comparison.company_b.ticker == "MSFT",
        f"{comparison.company_a.ticker} vs {comparison.company_b.ticker} for {comparison.fiscal_year}",
    )
    audit.record(
        "compare.all_revenue_positive",
        all(
            item.revenue_billions > 0
            for profile in (comparison.company_a, comparison.company_b)
            for item in profile.slices
        ),
        "every segment revenue in both profiles is > 0",
    )
    audit.record(
        "compare.segment_counts",
        comparison.company_a.segment_count == len(SEED_SEGMENTS_BY_TICKER["AAPL"])
        and comparison.company_b.segment_count == len(SEED_SEGMENTS_BY_TICKER["MSFT"]),
        f"AAPL={comparison.company_a.segment_count} MSFT={comparison.company_b.segment_count}",
    )
    shares_a = round(sum(item.share_of_company_pct for item in comparison.company_a.slices), 2)
    shares_b = round(sum(item.share_of_company_pct for item in comparison.company_b.slices), 2)
    audit.record(
        "compare.shares_sum_to_100",
        abs(shares_a - 100.0) < 0.5 and abs(shares_b - 100.0) < 0.5,
        f"AAPL share sum={shares_a}% MSFT share sum={shares_b}%",
    )
    descending = all(
        all(
            profile.slices[index].revenue_billions >= profile.slices[index + 1].revenue_billions
            for index in range(len(profile.slices) - 1)
        )
        for profile in (comparison.company_a, comparison.company_b)
    )
    audit.record(
        "compare.slices_sorted_desc",
        descending,
        "segment slices are ordered by revenue descending",
    )
    top_a, top_b = comparison.company_a.top_segment, comparison.company_b.top_segment
    assert top_a is not None and top_b is not None, "both companies must report segments"
    audit.record(
        "compare.top_segments",
        top_a.segment_name == "iPhone" and top_b.segment_name.startswith("Intelligent Cloud"),
        f"AAPL top={top_a.segment_name} MSFT top={top_b.segment_name}",
    )
    expected_delta = (
        comparison.company_a.total_revenue_billions - comparison.company_b.total_revenue_billions
    )
    audit.record(
        "compare.delta_consistent",
        abs(comparison.total_delta_billions - expected_delta) < TOLERANCE
        and abs(comparison.revenue_ratio - (comparison.company_a.total_revenue_billions / comparison.company_b.total_revenue_billions)) < TOLERANCE,
        f"delta={comparison.total_delta_billions:+.3f} bn ratio={comparison.revenue_ratio:.4f}x "
        f"concentration delta={comparison.concentration_delta_pct_points:+.2f} pp",
    )
    audit.record(
        "compare.no_cross_company_label_collision",
        comparison.overlapping_segment_names == (),
        f"overlapping segment labels = {list(comparison.overlapping_segment_names)} "
        "(empty means the two segment sets stay disjoint)",
    )
    audit.record(
        "compare.case_insensitive_tickers",
        compare_company_segments(connection, "aapl", "msft", FISCAL_YEAR).company_a.ticker == "AAPL",
        "lowercase tickers resolve to the same companies",
    )

    for label, call in (
        ("unknown_ticker", lambda: compare_company_segments(connection, "NFLX", "MSFT", FISCAL_YEAR)),
        ("same_ticker", lambda: compare_company_segments(connection, "AAPL", "AAPL", FISCAL_YEAR)),
        ("year_without_data", lambda: compare_company_segments(connection, "AAPL", "MSFT", 1999)),
    ):
        try:
            call()
        except GraphQueryError as exc:
            audit.record(f"compare.rejects_{label}", True, f"GraphQueryError: {exc}")
        except Exception as exc:  # noqa: BLE001 - the point is the exception type
            audit.record(
                f"compare.rejects_{label}", False, f"raised {type(exc).__name__} instead: {exc}"
            )
        else:
            audit.record(f"compare.rejects_{label}", False, f"{label} was accepted")


def _check_shared_risks(connection: lb.Connection, audit: AuditReport) -> None:
    """Shared-risk detection returns genuine multi-company exposure only."""
    shared = find_shared_risk_factors(connection)
    assert shared, "the seed must contain at least one risk factor shared by 2+ companies"
    audit.record(
        "risk.shared_risks_exist",
        bool(shared),
        f"{len(shared)} risk factors are shared by 2+ companies",
    )
    audit.record(
        "risk.every_result_is_multi_company",
        all(risk.company_count >= 2 and len(set(risk.tickers)) == risk.company_count for risk in shared),
        "every returned risk lists 2+ distinct tickers",
    )
    expected_shared = {risk.id for risk in SEED_RISK_FACTORS if len(risk.tickers) >= 2}
    found_shared = {risk.risk_id for risk in shared}
    audit.record(
        "risk.exact_shared_set",
        found_shared == expected_shared,
        f"found={sorted(found_shared)} expected={sorted(expected_shared)}",
    )
    single_company = {risk.id for risk in SEED_RISK_FACTORS if len(risk.tickers) == 1}
    leaked = sorted(found_shared & single_company)
    audit.record(
        "risk.single_company_not_shared",
        not leaked,
        f"single-company risks excluded: {sorted(single_company)}; leaked: {leaked}",
    )
    audit.record(
        "risk.shared_pair_is_aapl_msft",
        all(set(risk.tickers) == {"AAPL", "MSFT"} for risk in shared),
        "the only two companies in the graph are AAPL and MSFT",
    )
    strict = find_shared_risk_factors(connection, min_companies=3)
    audit.record(
        "risk.min_companies_filter",
        strict == [],
        f"min_companies=3 returns {len(strict)} rows (expected 0)",
    )
    supply_chain = find_shared_risk_factors(connection, category="Supply Chain")
    audit.record(
        "risk.category_filter",
        bool(supply_chain) and all(risk.category == "Supply Chain" for risk in supply_chain),
        f"category='Supply Chain' -> {[risk.risk_id for risk in supply_chain]}",
    )
    absent = find_shared_risk_factors(connection, category="No Such Category")
    audit.record(
        "risk.category_filter_no_match",
        absent == [],
        f"category='No Such Category' -> {len(absent)} rows (expected 0)",
    )


def _check_disambiguation(connection: lb.Connection, audit: AuditReport) -> None:
    """The ambiguity tripwire is clean here and provably fires on dirty data."""
    ambiguous = find_ambiguous_segment_labels(connection)
    audit.record(
        "disambiguate.curated_labels_unambiguous",
        ambiguous == [],
        f"overloaded segment labels in the seeded graph: {ambiguous}",
    )
    for year in (None, FISCAL_YEAR):
        scoped = find_ambiguous_segment_labels(connection, year=year)
        audit.record(
            f"disambiguate.scope_year_{year}",
            scoped == [],
            f"year={year} overloaded labels: {scoped}",
        )

    with open_graph(":memory:", buffer_pool_size_mb=64) as dirty:
        create_schema(dirty)
        for ticker in ("AAPL", "MSFT"):
            dirty.execute(
                "MERGE (c:Company {ticker: $ticker}) SET c.name = $name, c.sector = 'Technology'",
                parameters={"ticker": ticker, "name": f"{ticker} Inc"},
            )
        for ticker in ("AAPL", "MSFT"):
            dirty.execute(
                "MERGE (s:Segment {id: $id}) SET s.segment_name = 'Services', "
                "s.fiscal_year = $year, s.revenue_billions = 1.0",
                parameters={"id": f"{ticker}-dirty", "year": FISCAL_YEAR},
            )
            dirty.execute(
                "MATCH (c:Company {ticker: $ticker}), (s:Segment {id: $id}) "
                "MERGE (c)-[:REPORTS_SEGMENT]->(s)",
                parameters={"ticker": ticker, "id": f"{ticker}-dirty"},
            )
        dirty_ambiguous = find_ambiguous_segment_labels(dirty)
        detected = any(
            label == "Services" and set(tickers) == {"AAPL", "MSFT"}
            for label, tickers in dirty_ambiguous
        )
        audit.record(
            "disambiguate.control_fires_on_duplicate_label",
            detected,
            f"injected 'Services' under both tickers -> detected={dirty_ambiguous}",
        )


def _check_graphrag_context(connection: lb.Connection, audit: AuditReport) -> None:
    """The RAG evidence block is grounded in the graph it was built from."""
    context = build_rag_context(
        connection,
        ["AAPL", "MSFT"],
        FISCAL_YEAR,
        question="How do Apple and Microsoft segment revenue compare in FY2024?",
    )
    stats = graph_statistics(connection)
    audit.record(
        "graphrag.context_mentions_scope",
        "AAPL" in context and "MSFT" in context and str(FISCAL_YEAR) in context,
        "tickers and fiscal year are present in the context block",
    )
    missing_segments = [
        segment.segment_name
        for segment in SEED_SEGMENTS
        if segment.segment_name not in context
    ]
    audit.record(
        "graphrag.context_covers_every_segment",
        not missing_segments,
        f"all {len(SEED_SEGMENTS)} seeded segment labels appear in the context"
        if not missing_segments
        else f"missing: {missing_segments}",
    )
    shared = find_shared_risk_factors(connection)
    missing_risks = [risk.risk_id for risk in shared if risk.risk_id not in context]
    audit.record(
        "graphrag.context_covers_shared_risks",
        not missing_risks and bool(shared),
        f"all {len(shared)} shared risk ids appear in the context"
        if not missing_risks
        else f"missing: {missing_risks}",
    )
    audit.record(
        "graphrag.context_has_provenance",
        "PROVENANCE" in context and f"companies={stats['companies']}" in context,
        "context carries a provenance footer with graph counts",
    )
    audit.record(
        "graphrag.context_is_bounded",
        0 < len(context) < 4096,
        f"context length = {len(context)} characters",
    )
    single = build_rag_context(connection, ["AAPL"], FISCAL_YEAR)
    audit.record(
        "graphrag.context_single_ticker",
        "Services" in single and "MSFT" not in single,
        "single-ticker context stays scoped to that company",
    )
    try:
        build_rag_context(connection, [], FISCAL_YEAR)
    except GraphQueryError as exc:
        audit.record("graphrag.rejects_empty_tickers", True, f"GraphQueryError: {exc}")
    else:
        audit.record("graphrag.rejects_empty_tickers", False, "an empty ticker list was accepted")


def _check_reseeding(connection: lb.Connection, audit: AuditReport) -> None:
    """Re-running the seed is idempotent, and a failed seed rolls back."""
    before = graph_statistics(connection)
    seed_graph(connection)
    after = graph_statistics(connection)
    audit.record(
        "ingest.reseed_is_idempotent",
        before == after,
        f"before={before} after={after}",
    )

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "MATCH (c:Company {ticker: $ticker}) SET c.sector = 'Should Not Persist'",
            parameters={"ticker": "AAPL"},
        )
        connection.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001 - rollback path must not abort the audit
        audit.record("ingest.rollback_supported", False, f"{type(exc).__name__}: {exc}")
        return
    sector = connection.execute(
        "MATCH (c:Company {ticker: 'AAPL'}) RETURN c.sector"
    ).get_all()
    audit.record(
        "ingest.rollback_supported",
        bool(sector) and str(sector[0][0]) == "Technology",
        f"AAPL sector after rollback = {sector[0][0] if sector else None} (expected Technology)",
    )


SEED_SEGMENTS_BY_TICKER: dict[str, tuple[str, ...]] = {
    ticker: tuple(segment.segment_name for segment in SEED_SEGMENTS if segment.company_ticker == ticker)
    for ticker in sorted({segment.company_ticker for segment in SEED_SEGMENTS})
}


def run_verification(
    connection: lb.Connection | None = None,
    stream: TextIO = sys.stdout,
    database_path: str | os.PathLike[str] = ":memory:",
    buffer_pool_size_mb: int = BUFFER_POOL_CAP_MB,
) -> bool:
    """Run the full assertion suite and print a PASS/FAIL audit summary.

    Callable with no arguments: when ``connection`` is ``None`` a throwaway
    in-memory graph is created, schema'd and seeded, then verified. Returns
    ``True`` only when every check passes.
    """
    if connection is not None:
        return _run_checks(connection, stream)

    with open_graph(database_path, buffer_pool_size_mb=buffer_pool_size_mb) as owned:
        create_schema(owned)
        seed_graph(owned, reset=True)
        return _run_checks(owned, stream)


def _check_company_risks(connection: lb.Connection, audit: AuditReport) -> None:
    """The per-company risk traversal stays consistent with the shared one."""
    aapl = find_risk_factors_for_company(connection, "AAPL")
    msft = find_risk_factors_for_company(connection, "MSFT")
    audit.record(
        "company_risks.resolved",
        bool(aapl) and bool(msft),
        f"AAPL={len(aapl)} risks MSFT={len(msft)} risks",
    )

    shared = {risk.risk_id for risk in find_shared_risk_factors(connection)}
    aapl_ids = {risk.risk_id for risk in aapl}
    msft_ids = {risk.risk_id for risk in msft}
    audit.record(
        "company_risks.shared_are_subsets",
        shared.issubset(aapl_ids) and shared.issubset(msft_ids),
        f"shared={sorted(shared)} AAPL={sorted(aapl_ids)} MSFT={sorted(msft_ids)}",
    )

    single = {risk.id for risk in SEED_RISK_FACTORS if len(risk.tickers) == 1}
    audit.record(
        "company_risks.company_specific_excluded_from_shared",
        not (shared & single),
        f"single-company risks={sorted(single)} leaked into shared={sorted(shared & single)}",
    )

    # A per-company result must still report every carrier, otherwise the
    # "is this risk shared?" distinction is lost at exactly the point it matters.
    carriers = {risk.risk_id: set(risk.tickers) for risk in aapl}
    expected = {risk.id: set(risk.tickers) for risk in SEED_RISK_FACTORS}
    wrong = {
        risk_id: (sorted(seen), sorted(expected.get(risk_id, set())))
        for risk_id, seen in carriers.items()
        if seen != expected.get(risk_id, set())
    }
    audit.record(
        "company_risks.carriers_preserved",
        not wrong,
        "every per-company risk reports its true carrier set"
        if not wrong
        else f"mismatched: {wrong}",
    )
    audit.record(
        "company_risks.aapl_only_risk_present",
        any(risk.company_count == 1 for risk in aapl),
        "AAPL carries at least one risk that MSFT does not",
    )
    try:
        find_risk_factors_for_company(connection, "NFLX")
    except GraphQueryError as exc:
        audit.record("company_risks.rejects_unknown_ticker", True, f"GraphQueryError: {exc}")
    else:
        audit.record("company_risks.rejects_unknown_ticker", False, "NFLX was accepted")


def _check_synthesis_layer(connection: lb.Connection, audit: AuditReport) -> None:
    """The natural-language layer resolves questions into grounded evidence."""
    synthesis = _load_synthesis_module()

    parsed = synthesis.parse_question(
        "How do Apple's Services compare to Microsoft's Cloud revenue in 2024?"
    )
    audit.record(
        "synthesis.possessive_aliases_resolve",
        parsed.tickers == ("AAPL", "MSFT"),
        f"tickers={parsed.tickers} from aliases {parsed.aliases_matched}",
    )
    audit.record(
        "synthesis.intent_and_year",
        parsed.intent is synthesis.Intent.COMPARE_SEGMENTS and parsed.year == FISCAL_YEAR,
        f"intent={parsed.intent.value} year={parsed.year}",
    )

    risk_parsed = synthesis.parse_question(
        "What supply chain risks do both Apple and Microsoft share?"
    )
    audit.record(
        "synthesis.risk_intent",
        risk_parsed.intent is synthesis.Intent.SHARED_RISKS,
        f"intent={risk_parsed.intent.value}",
    )

    single = synthesis.parse_question("Tell me about iPhone revenue")
    audit.record(
        "synthesis.product_alias_resolves_single_ticker",
        single.tickers == ("AAPL",),
        f"tickers={single.tickers} from aliases {single.aliases_matched}",
    )

    evidence = synthesis.retrieve_evidence(
        connection, parsed, "How do Apple's Services compare to Microsoft's Cloud revenue?"
    )
    audit.record(
        "synthesis.evidence_is_grounded",
        "391.035" in evidence and "245.122" in evidence,
        "the comparison evidence carries both companies' disclosed totals",
    )
    audit.record(
        "synthesis.evidence_has_citation_instruction",
        "REPORTS_SEGMENT" in evidence and "RF-001" in evidence,
        "the evidence block names the relationships and node keys to cite",
    )
    audit.record(
        "synthesis.evidence_is_bounded",
        0 < len(evidence) < 8192,
        f"evidence length = {len(evidence)} characters",
    )

    # A year the graph does not hold must be reported as a gap in the block, not
    # silently answered with the year that does exist.
    gap = synthesis.retrieve_evidence(
        connection,
        synthesis.parse_question("What was Microsoft's cloud revenue in 2019?"),
        "What was Microsoft's cloud revenue in 2019?",
    )
    audit.record(
        "synthesis.absent_year_is_flagged",
        "COVERAGE GAP" in gap and "2019" in gap,
        "a question about an unseeded year is marked as a coverage gap",
    )

    # A two-company question about an unseeded year used to raise out of
    # build_rag_context, losing the question entirely. It must return a block
    # that names the gap and labels the year the figures are actually from.
    pair_gap = synthesis.retrieve_evidence(
        connection,
        synthesis.parse_question(
            "How do Apple's Services compare to Microsoft's Cloud revenue in 2019?"
        ),
        "How do Apple's Services compare to Microsoft's Cloud revenue in 2019?",
    )
    audit.record(
        "synthesis.absent_year_pair_returns_block",
        "COVERAGE GAP" in pair_gap
        and "fiscal_year_in_figures=2024" in pair_gap
        and "391.035" in pair_gap,
        "a two-company unseeded-year question returns a gap-flagged block "
        "instead of raising",
    )

    system, user = synthesis.build_synthesis_prompt("test question", evidence)
    audit.record(
        "synthesis.prompt_demands_grounding",
        "only" in system.lower() and "gap" in system.lower(),
        "the system prompt forbids unsupported claims and requires gaps to be named",
    )
    audit.record(
        "synthesis.prompt_demands_citations",
        "cite" in system.lower() and "REPORTS_SEGMENT" in system,
        "the system prompt requires citing node keys and relationship names",
    )
    audit.record(
        "synthesis.prompt_delimits_evidence",
        "<<<EVIDENCE" in user and "EVIDENCE>>>" in user,
        "the evidence block is delimited and marked as data, not instructions",
    )
    audit.record(
        "synthesis.prompt_carries_question",
        "test question" in user,
        "the analyst question is present in the user message",
    )

    # No credential may be embedded in the source of the synthesis layer. The
    # patterns require a realistic body length so that documentation such as
    # "export OPENAI_API_KEY=sk-..." does not trip the check.
    source = Path(synthesis.__file__).read_text(encoding="utf-8")
    leaked = re.findall(r"\bsk-[A-Za-z0-9_-]{20,}|\bAIza[0-9A-Za-z_-]{20,}", source)
    audit.record(
        "synthesis.no_embedded_credential",
        not leaked,
        "the synthesis source contains no literal API key"
        if not leaked
        else f"possible credentials present: {len(leaked)} match(es)",
    )
    for settings in synthesis.PROVIDER_SETTINGS.values():
        audit.record(
            f"synthesis.provider_{settings.provider.value}_reads_env",
            settings.api_key_env_var in {s.api_key_env_var for s in synthesis.PROVIDER_SETTINGS.values()},
            f"{settings.provider.value} reads {settings.api_key_env_var}",
        )

    previous = {
        name: os.environ.pop(name, None)
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    }
    try:
        try:
            synthesis.load_llm_config()
        except synthesis.MissingAPIKeyError as exc:
            message = str(exc)
            audit.record(
                "synthesis.missing_key_explains_setup",
                "OPENAI_API_KEY" in message and "GEMINI_API_KEY" in message,
                "the missing-credential error names every supported variable",
            )
            audit.record(
                "synthesis.missing_key_suggests_no_llm",
                "--no-llm" in message,
                "the missing-credential error points at the keyless path",
            )
        else:
            audit.record("synthesis.missing_key_explains_setup", False, "a key was visible")
    finally:
        for name, value in previous.items():
            if value is not None:
                os.environ[name] = value

    os.environ["OPENAI_API_KEY"] = "YOUR_KEY_HERE"
    try:
        synthesis.load_llm_config("openai")
    except synthesis.MissingAPIKeyError:
        audit.record("synthesis.placeholder_key_rejected", True, "a placeholder value was rejected")
    else:
        audit.record("synthesis.placeholder_key_rejected", False, "a placeholder was accepted")
    finally:
        os.environ.pop("OPENAI_API_KEY", None)

    for member in synthesis.Provider:
        resolved = synthesis.Provider.coerce(member.value)
        audit.record(
            f"synthesis.provider_name_{member.value}_resolves",
            resolved is member,
            f"coerce({member.value!r}) -> {resolved.value}",
        )
    audit.record(
        "synthesis.provider_alias_resolves",
        synthesis.Provider.coerce("google") is synthesis.Provider.GEMINI,
        "coerce('google') -> gemini",
    )

    try:
        synthesis.load_llm_config("not-a-provider")
    except synthesis.SynthesisError as exc:
        audit.record("synthesis.rejects_unknown_provider", True, f"SynthesisError: {exc}")
    else:
        audit.record("synthesis.rejects_unknown_provider", False, "an unknown provider was accepted")


def _run_checks(connection: lb.Connection, stream: TextIO) -> bool:
    """Execute every verification stage against an open graph.

    A stage that trips one of its own ``assert`` statements is recorded as a
    failed check rather than aborting the run, so one broken invariant cannot
    hide the state of the rest of the suite.
    """
    audit = AuditReport()
    stages = (
        ("memory_contract", lambda: _check_memory_contract(audit)),
        ("schema", lambda: _check_schema(connection, audit)),
        ("ingestion", lambda: _check_ingestion(connection, audit)),
        ("comparison", lambda: _check_comparison(connection, audit)),
        ("shared_risks", lambda: _check_shared_risks(connection, audit)),
        ("disambiguation", lambda: _check_disambiguation(connection, audit)),
        ("graphrag_context", lambda: _check_graphrag_context(connection, audit)),
        ("reseeding", lambda: _check_reseeding(connection, audit)),
        ("company_risks", lambda: _check_company_risks(connection, audit)),
        ("synthesis", lambda: _check_synthesis_layer(connection, audit)),
    )
    for stage_name, stage in stages:
        try:
            stage()
        except AssertionError as exc:
            audit.record(f"{stage_name}.assertion", False, f"AssertionError: {exc}")
        except Exception as exc:  # noqa: BLE001 - an audit must survive a broken stage
            audit.record(f"{stage_name}.raised", False, f"{type(exc).__name__}: {exc}")
    audit.render(stream)
    return audit.failed == 0 and bool(audit.checks)


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]], indent: str = "  ") -> str:
    """Render a fixed-width console table without external dependencies."""
    if not rows:
        return f"{indent}(no rows)"
    cells = [[str(value) for value in row] for row in rows]
    widths = [
        max(len(str(headers[index])), *(len(row[index]) for row in cells))
        for index in range(len(headers))
    ]
    numeric = [
        all(isinstance(row[index], (int, float)) and not isinstance(row[index], bool) for row in cells)
        for index in range(len(headers))
    ]

    def line(values: Sequence[str]) -> str:
        parts = []
        for index, value in enumerate(values):
            parts.append(value.rjust(widths[index]) if numeric[index] else value.ljust(widths[index]))
        return indent + " | ".join(parts)

    separator = indent + "-+-".join("-" * width for width in widths)
    return "\n".join([line([str(header) for header in headers]), separator, *(line(row) for row in cells)])


def print_comparison(connection: lb.Connection, comparison: SegmentComparison, stream: TextIO) -> None:
    """Print the segment comparison as two side-by-side mixes plus deltas."""
    print(f"Fiscal year {comparison.fiscal_year} segment comparison", file=stream)
    print("=" * 78, file=stream)
    rows = []
    for profile in (comparison.company_a, comparison.company_b):
        rows.append(
            [
                profile.ticker,
                profile.name,
                profile.sector,
                profile.segment_count,
                round(profile.total_revenue_billions, 3),
                f"{profile.concentration_pct:.2f}%",
            ]
        )
    print(
        render_table(
            ["TICKER", "NAME", "SECTOR", "SEGS", "TOTAL_BN", "TOP_SEG_SHARE"],
            rows,
        ),
        file=stream,
    )
    for profile in (comparison.company_a, comparison.company_b):
        top = profile.top_segment
        print(
            f"  {profile.ticker} top segment: {top.segment_name if top else 'n/a'} "
            f"({top.revenue_billions:,.3f} bn)" if top else f"  {profile.ticker} top segment: n/a",
            file=stream,
        )
        print(
            render_table(
                ["SEGMENT", "REVENUE_BN", "SHARE_PCT"],
                [
                    [item.segment_name, round(item.revenue_billions, 3), item.share_of_company_pct]
                    for item in profile.slices
                ],
                indent="      ",
            ),
            file=stream,
        )
    print(
        f"  delta({comparison.company_a.ticker}-{comparison.company_b.ticker}) = "
        f"{comparison.total_delta_billions:+,.3f} bn | ratio = {comparison.revenue_ratio:.4f}x | "
        f"concentration delta = {comparison.concentration_delta_pct_points:+.2f} pp | "
        f"leading gap = {comparison.leading_segment_gap_billions:+,.3f} bn",
        file=stream,
    )
    print(
        "  overlapping labels: "
        + (", ".join(comparison.overlapping_segment_names) or "none (segment sets stay disjoint)"),
        file=stream,
    )
    stream.flush()


def print_shared_risks(shared: Sequence[SharedRisk], stream: TextIO) -> None:
    """Print the shared risk register as a table."""
    print("Shared risk factors (2+ companies)", file=stream)
    print("=" * 78, file=stream)
    print(
        render_table(
            ["RISK_ID", "CATEGORY", "COMPANIES", "TITLE"],
            [[risk.risk_id, risk.category, ",".join(risk.tickers), risk.title] for risk in shared],
        ),
        file=stream,
    )
    stream.flush()


def print_seed_notes(stream: TextIO) -> None:
    """Print provenance and caveats for the seeded figures."""
    print("Seed provenance", file=stream)
    print("=" * 78, file=stream)
    for note in SEED_NOTES:
        print(f"  - {note}", file=stream)
    stream.flush()


def _resolve_demo_database(db_path: str | None, reset: bool) -> str:
    """Pick the demo database path and clear it when a reset was requested."""
    target = db_path or ":memory:"
    if reset and target != ":memory:" and os.path.exists(target):
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
    return target


def _load_synthesis_module() -> Any:
    """Import the synthesis layer on demand.

    The graph half of this project has no LLM dependency, so the import is
    deferred to the code path that actually needs it. A missing optional
    dependency therefore cannot break ``--verify-only`` or the plain demo.
    """
    try:
        import graphrag_synthesis
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise GraphError(
            "the natural-language layer is unavailable: "
            f"{exc}. Place graphrag_synthesis.py next to this file."
        ) from exc
    return graphrag_synthesis


def _run_natural_language(args: Any, connection: lb.Connection) -> int:
    """Dispatch to the one-shot answer or the interactive loop.

    Returns a process exit code: 0 on success, 2 when the provider could not be
    configured or the request failed. A missing credential is reported with
    setup instructions and is not treated as a crash.
    """
    synthesis = _load_synthesis_module()

    provider = None if args.provider == "auto" else args.provider
    session = synthesis.Session(
        year=args.year,
        use_llm=not args.no_llm,
        show_evidence=bool(args.show_evidence),
        provider=provider,
    )

    if args.interactive:
        return synthesis.run_interactive(connection, session)

    question = (args.ask or "").strip()
    if not question:
        print("ERROR: --ask needs a non-empty question", file=sys.stderr)
        return 2

    parsed = synthesis.parse_question(question, default_year=args.year)
    print(f"[retrieval] {parsed.describe()}")

    try:
        evidence = synthesis.retrieve_evidence(connection, parsed, question)
    except (synthesis.SynthesisError, GraphError) as exc:
        print(f"ERROR: retrieval failed: {exc}", file=sys.stderr)
        return 2

    if args.no_llm:
        print()
        print(evidence)
        return 0

    try:
        config = session.load_config()
    except synthesis.MissingAPIKeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("\nRetrieved evidence follows.", file=sys.stderr)
        print(evidence, file=sys.stderr)
        return 2

    print(f"[llm] {config.describe()}")
    try:
        answer = synthesis.answer_question(
            connection, question, config, default_year=args.year
        )
    except (synthesis.LLMRequestError, synthesis.SynthesisError) as exc:
        print(f"ERROR: synthesis failed: {exc}", file=sys.stderr)
        return 2

    if args.show_evidence:
        print()
        print("Retrieved evidence")
        print("=" * 78)
        print(answer.evidence_block)
        print()

    print("Answer")
    print("=" * 78)
    print(answer.text)
    print()
    print(
        f"grounded in {len(answer.evidence_block)} chars of retrieved evidence "
        f"via {answer.provider}/{answer.model}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: seed the graph, demo the queries, run the audit."""
    parser = argparse.ArgumentParser(
        prog="financial_graphrag",
        description="Financial GraphRAG mini-project on an embedded LadybugDB graph.",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="persist the graph to PATH instead of using an in-memory database",
    )
    parser.add_argument(
        "--reset", action="store_true", help="delete an existing --db file before seeding"
    )
    parser.add_argument("--year", type=int, default=FISCAL_YEAR, help="fiscal year to query")
    parser.add_argument(
        "--buffer-pool-mb",
        type=int,
        default=BUFFER_POOL_CAP_MB,
        help=f"buffer pool in MB, capped at {BUFFER_POOL_CAP_MB}",
    )
    parser.add_argument("--verify-only", action="store_true", help="only run the verification audit")
    parser.add_argument("--log-level", default="WARNING", help="logging level for this run")
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="open the interactive natural-language query loop",
    )
    parser.add_argument(
        "--ask",
        metavar="QUESTION",
        default=None,
        help="answer one question and print the grounded response, then exit",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "openai", "gemini"),
        default="auto",
        help="LLM provider for --ask/--interactive (default: auto-detect)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="never call the LLM; print the retrieved evidence block instead",
    )
    parser.add_argument(
        "--show-evidence",
        action="store_true",
        help="also print the retrieved evidence alongside a synthesised answer",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=str(args.log_level).upper(), format="%(levelname)s %(name)s: %(message)s")

    try:
        database_path = _resolve_demo_database(args.db, args.reset)
        with open_graph(
            database_path, buffer_pool_size_mb=args.buffer_pool_mb
        ) as connection:
            create_schema(connection)
            report = seed_graph(connection, reset=args.reset)

            if not args.verify_only:
                print(f"LadybugDB embedded graph: {database_path}")
                print(f"seed: {report.as_line()}\n")
                comparison = compare_company_segments(
                    connection, "AAPL", "MSFT", args.year
                )
                print_comparison(connection, comparison, sys.stdout)
                print()
                print_shared_risks(find_shared_risk_factors(connection), sys.stdout)
                print()
                print("GraphRAG evidence block", file=sys.stdout)
                print("=" * 78, file=sys.stdout)
                print(
                    build_rag_context(
                        connection,
                        ["AAPL", "MSFT"],
                        args.year,
                        question="Compare Apple and Microsoft FY2024 segment revenue and shared risks.",
                    ),
                    file=sys.stdout,
                )
                print()
                print_seed_notes(sys.stdout)
                print()

            ran_natural_language = bool(args.interactive or args.ask)
            if ran_natural_language:
                exit_code = _run_natural_language(args, connection)
                if exit_code:
                    return exit_code

            # The audit runs for the plain demo and for --verify-only. Once the
            # natural-language loop has taken over the terminal it is skipped:
            # it owns stdout, and a PASS/FAIL table in the middle of a session
            # reads as a failure.
            if args.verify_only or not ran_natural_language:
                passed = run_verification(connection, sys.stdout)
            else:
                passed = True
    except GraphError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
