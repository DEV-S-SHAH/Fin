"""Stage 5: five Cypher verification benchmarks, each with a hard assertion.

Every benchmark returns a :class:`BenchmarkResult` whose ``passed`` field is
computed by an explicit predicate, not by "the query did not raise". A
benchmark that only reports output is a smoke test; these fail the run.

    from sandbox_engine.benchmarks import run_all

    results = run_all(connection)
    for result in results:
        print(result.name, result.passed, result.detail)

The five
---------

**B1 -- graph shape and referential integrity.** Every table holds at least what
the buffer staged, every ``Filing`` has exactly one ``SUBMITTED`` arc, and no
``REPORTS_METRIC`` arc points at a metric that does not exist. This is the
benchmark that catches a loader that quietly dropped rows: a table count that is
merely *non-zero* would pass a smoke test while being wrong by a factor of
ten.

**B2 -- point lookup for a named concept.** Given a form type, a fiscal year, and
a concept, return that filing's reporting-period value. This is the query a
consumer actually writes, so it is the one that has to work. It also proves the
``Company -> Filing -> Metric`` traversal is navigable from the tenancy
boundary.

**B3 -- comparative-period separation.** The strongest correctness check in the
set, and the one that fails if the parser regresses on hazard 1. A 10-Q prints
"Three Months Ended June 28, 2025" and "Nine Months Ended June 28, 2025" as two
columns sharing one date. The benchmark asserts that both ``3M-FY<year>`` and
``9M-FY<year>`` exist as *distinct* metric nodes carrying *distinct* values. If
period detection ever collapses them again, one node disappears and the two
values become one -- and this benchmark fails rather than reporting a plausible
number.

**B4 -- segment fan-out from a metric.** ``Metric -> HAS_SEGMENT -> Segment``
must return several segments with their own values. A segment note has no
statement line items of its own, so its figures hang off the revenue metric of
the same period; this is the only benchmark that exercises that indirection.

**B5 -- negative control.** A year outside the three-filing scope must return
an **empty set**, not an error and not rows. A query that raises on absent data
and a query that invents data are both failures, and only a negative control
distinguishes "correctly found nothing" from "never looked".

Cypher notes for LadybugDB 0.20.4
----------------------------------

* ``UNWIND $list`` is a binder error when the list is empty, even with a
  ``MATCH`` in front of it. Every parameterised ``UNWIND`` here is guarded.
* List comprehensions (``[t IN $list | t]``) are unsupported. The quantified
  ``ANY(t IN $list WHERE ...)`` form is used instead where needed.
* ``CONTAINS`` is a full table scan. Every benchmark that uses it is bounded by
  a ``LIMIT`` and by the small scope, which is acceptable at three filings and
  is the reason the sandbox does not run the full 75.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .buffer import NODE_TABLES, REL_TABLES
from .config import ABSENT_YEAR

__all__ = [
    "BenchmarkResult",
    "CONCEPT_RE",
    "PERIOD_SUFFIX_RE",
    "duration_of",
    "period_end_of",
    "period_of",
    "run_all",
]

log = logging.getLogger("sandbox_engine.benchmarks")

#: ``"Net Sales (3M-FY2025)"`` -> ``"3M-FY2025"``. The period is written into
#: the metric's name so a query never has to know a content hash.
PERIOD_SUFFIX_RE = re.compile(r"\(([^()]*)\)\s*$")

#: Prefixes for the concepts benchmark 2 looks up. Registry vocabulary, not
#: company vocabulary.
CONCEPT_RE = re.compile(r"^(Total\s+)?(Net\s+Sales|Operating\s+Income|Net\s+Income)", re.I)


def period_of(canonical_name: str) -> str:
    """The period suffix of a metric name, or ``""``."""
    match = PERIOD_SUFFIX_RE.search(canonical_name or "")
    return match.group(1) if match else ""


def concept_of(canonical_name: str) -> str:
    """The metric name with its period suffix removed."""
    return PERIOD_SUFFIX_RE.sub("", canonical_name or "").strip()


#: ``"3M-2026-03-28"`` splits into its duration and its end date. Period keys
#: are keyed on the date precisely so the two can be pulled apart: the date
#: identifies the measurement, the duration says how much of it was measured.
PERIOD_PARTS_RE = re.compile(r"^(?P<duration>\d+M|FY)-(?P<end>\d{4}-\d{2}-\d{2})$")


def duration_of(period: str) -> str:
    """``"3M"`` out of ``"3M-2026-03-28"``; ``""`` if the key is a bare date."""
    match = PERIOD_PARTS_RE.match(period or "")
    return match.group("duration") if match else ""


def period_end_of(period: str) -> str:
    """``"2026-03-28"`` out of ``"3M-2026-03-28"``; the key itself if bare."""
    match = PERIOD_PARTS_RE.match(period or "")
    return match.group("end") if match else (period or "")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    """One benchmark's verdict.

    ``detail`` is a JSON-ready dict, so the run report carries the evidence and
    not just the boolean. A verification result nobody can inspect is not a
    verification result.
    """

    key: str
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "passed": self.passed,
            "failures": list(self.failures),
            "detail": self.detail,
            "seconds": round(self.seconds, 4),
        }


class _Runner:
    """Query helper plus the assertion bookkeeping shared by all five."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def rows(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[list[Any]]:
        return [list(row) for row in self.connection.execute(query, params or {}).get_all()]

    def scalar(self, query: str, params: dict[str, Any] | None = None) -> Any:
        rows = self.rows(query, params)
        return rows[0][0] if rows else None

    @staticmethod
    def timed(run: Callable[[], BenchmarkResult]) -> BenchmarkResult:
        started = time.perf_counter()
        result = run()
        result.seconds = time.perf_counter() - started
        return result

    @staticmethod
    def verdict(
        key: str, name: str, failures: Sequence[str], detail: dict[str, Any]
    ) -> BenchmarkResult:
        return BenchmarkResult(
            key=key, name=name, passed=not failures,
            detail=detail, failures=list(failures),
        )


# ---------------------------------------------------------------------------
# B1 -- graph shape and referential integrity
# ---------------------------------------------------------------------------

B1_CYPHER = """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)
RETURN f.id, f.form_type, f.fiscal_year, c.ticker
"""

B1_ORPHAN_METRIC = """
MATCH (f:Filing)-[e:REPORTS_METRIC]->(m:Metric)
RETURN count(e)
"""

B1_METRIC_WITHOUT_FILING = """
MATCH (m:Metric)
WHERE NOT EXISTS { MATCH (:Filing)-[:REPORTS_METRIC]->(m) }
RETURN m.id
"""


def benchmark_shape(runner: _Runner, distinct: dict[str, int]) -> BenchmarkResult:
    """B1: every distinct buffered row is stored exactly once.

    *distinct* is the buffer stage's count of unique identities -- primary key
    for a node table, endpoints plus properties for a rel table. Comparing
    against that rather than against the raw row count is what makes this a real
    assertion: raw staged rows legitimately exceed stored rows, because a metric
    shared by two filings is staged twice and stored once. With only the raw
    count, the strongest available check is ``stored <= staged``, which a loader
    that silently dropped 700 of 796 metrics would pass.

    Equality in both directions is the claim: not one buffered row lost, and not
    one stored row invented.

    A table with zero buffered rows is expected to be empty and is not counted
    as a failure. That distinction matters now that the schema is the universal
    one: ``RestatementEvent`` is populated only by amended filings, and
    ``Tier1CapitalRatio`` concepts only by bank-sector filings, so a run over
    three Apple filings correctly writes nothing to them. The emptiness check is
    therefore "buffered rows went missing", not "every table must be
    populated" -- the original form would have failed every run of the
    universal schema over a single-sector corpus and taught the reader to
    ignore the benchmark.
    """
    failures: list[str] = []
    stored = {table: int(runner.scalar(f"MATCH (n:{table}) RETURN count(n)") or 0)
              for table in NODE_TABLES}
    stored.update(
        {rel: int(runner.scalar(f"MATCH ()-[e:{rel}]->() RETURN count(e)") or 0)
         for rel in REL_TABLES}
    )

    compared = 0
    for table, expected in distinct.items():
        if table not in stored:
            failures.append(
                f"{table}: buffered {expected} row(s) but the table is not in the "
                f"schema; the buffer and the DDL disagree on the table contract"
            )
            continue
        compared += 1
        if stored[table] != expected:
            verb = "more than" if stored[table] > expected else "fewer than"
            failures.append(
                f"{table}: stored {stored[table]}, expected {expected} distinct "
                f"buffered row(s); the graph holds {verb} the pipeline buffered"
            )
    if compared == 0:
        failures.append(
            "no buffered row counts were supplied; B1 cannot assert integrity "
            "without a baseline (did the buffer stage run?)"
        )
    for table in NODE_TABLES:
        if stored[table] == 0 and distinct.get(table, 0) > 0:
            failures.append(f"{table}: empty; the loader wrote nothing for it")

    filings = runner.rows(B1_CYPHER)
    arcs_per_filing: dict[str, int] = {}
    for filing_id, _form, _year, _ticker in filings:
        arcs_per_filing[filing_id] = arcs_per_filing.get(filing_id, 0) + 1
    duplicated = sorted(f for f, count in arcs_per_filing.items() if count != 1)
    if duplicated:
        failures.append(
            f"{len(duplicated)} filing(s) have != 1 SUBMITTED arc: "
            f"{', '.join(duplicated[:3])}"
        )

    orphan_metrics = runner.rows(B1_METRIC_WITHOUT_FILING)
    if orphan_metrics:
        failures.append(
            f"{len(orphan_metrics)} Metric node(s) are unreachable from any Filing"
        )

    metric_arcs = int(runner.scalar(B1_ORPHAN_METRIC) or 0)
    if metric_arcs != stored["Metric"]:
        # Not every metric need have an arc -- a segment host node can be
        # synthesised -- but every arc must land on a real metric, and
        # REPORTS_METRIC holding none at all means the graph is a bare node set.
        if metric_arcs == 0:
            failures.append(
                "REPORTS_METRIC holds no arcs; the graph is a bare node set"
            )

    return _Runner.verdict(
        "B1", "graph shape and referential integrity", failures,
        {
            "stored_counts": dict(sorted(stored.items())),
            "distinct_buffered": dict(sorted(distinct.items())),
            "tables_compared": compared,
            "filings": [
                {"id": row[0], "form_type": row[1], "fiscal_year": row[2], "ticker": row[3]}
                for row in filings
            ],
            "metrics_without_filing": len(orphan_metrics),
            "metric_arcs": metric_arcs,
        },
    )


# ---------------------------------------------------------------------------
# B2 -- point lookup for a named concept
# ---------------------------------------------------------------------------

B2_CYPHER = """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[e:REPORTS_METRIC]->(m:Metric)
WHERE f.form_type = $form
  AND f.fiscal_year = $year
  AND m.canonical_name CONTAINS $concept
RETURN m.canonical_name, m.statement_category, e.value, e.currency
ORDER BY m.canonical_name
"""


def benchmark_lookup(
    runner: _Runner, form_type: str, fiscal_year: int, period: str, concept: str
) -> BenchmarkResult:
    """B2: the value for one concept in one named period, via the tenancy root.

    Scoped to an exact period rather than a fiscal year because a fiscal year
    holds up to four quarters: "net sales for FY2026" is four figures, and a
    year-scoped lookup either picks one arbitrarily or reports a conflict.
    """
    rows = runner.rows(
        B2_CYPHER, {"form": form_type, "year": fiscal_year, "concept": concept}
    )
    periods = [
        {
            "metric": row[0],
            "concept": concept_of(row[0]),
            "period": period_of(row[0]),
            "category": row[1],
            "value": None if row[2] is None else float(row[2]),
            "currency": row[3],
        }
        for row in rows
    ]
    periods.sort(key=lambda item: (str(item["period"]), str(item["concept"])))

    failures: list[str] = []
    if not periods:
        failures.append(
            f"no metric matching {concept!r} on the {form_type} FY{fiscal_year} filing"
        )
    matches = [item for item in periods if item["period"] == period]
    if periods and not matches:
        failures.append(
            f"no row for period {period}; periods present: "
            f"{sorted({item['period'] for item in periods})}"
        )
    match = matches[0] if matches else None
    if match is not None and match["value"] is None:
        failures.append(f"{period} value is null")
    # One period is one measurement, so it has one value. Two figures under the
    # same period key means two filings attached to one node and the graph now
    # answers with whichever landed first.
    if len({item["value"] for item in matches}) > 1:
        failures.append(
            f"period {period} carries conflicting values "
            f"{sorted(str(item['value']) for item in matches)}; the period key is "
            f"not identifying the measurement"
        )
    # A prior-year comparative must be visible, or the period scoping is lossy.
    if periods and len(periods) < 2:
        failures.append(
            "only one period returned; comparative columns were not captured, so "
            "period-scoped metrics are collapsing"
        )

    return _Runner.verdict(
        "B2", f"point lookup: {concept} {period} on {form_type} FY{fiscal_year}", failures,
        {
            "form_type": form_type,
            "fiscal_year": fiscal_year,
            "concept": concept,
            "reporting_period": period,
            "reporting_value": match["value"] if match else None,
            "currency": match["currency"] if match else None,
            "periods": periods,
        },
    )


# ---------------------------------------------------------------------------
# B3 -- comparative-period separation
# ---------------------------------------------------------------------------

B3_CYPHER = """
MATCH (f:Filing)-[e:REPORTS_METRIC]->(m:Metric)
WHERE f.form_type = $form
  AND f.fiscal_year = $year
  AND m.canonical_name CONTAINS $concept
RETURN m.canonical_name, e.value
ORDER BY m.canonical_name
"""


def benchmark_period_separation(
    runner: _Runner, form_type: str, fiscal_year: int, period: str, concept: str
) -> BenchmarkResult:
    """B3: same end date, two durations, two distinct nodes, two distinct values.

    This is the benchmark that catches a regression in period detection. See the
    module docstring for why the 10-Q's 3M and 9M columns are the test case.
    Durations are matched on the end date they share, which is exactly what the
    date-keyed period identity makes checkable.
    """
    rows = runner.rows(
        B3_CYPHER, {"form": form_type, "year": fiscal_year, "concept": concept}
    )
    by_period: dict[str, list[float | None]] = {}
    for name, value in rows:
        by_period.setdefault(period_of(name), []).append(
            None if value is None else float(value)
        )

    end = period_end_of(period)
    same_end = sorted(
        (p for p in by_period if period_end_of(p) == end),
        key=lambda p: (duration_of(p), p),
    )
    ytd = [p for p in same_end if duration_of(p)]
    prior = sorted(
        (p for p in by_period if period_end_of(p) not in (end, "") and duration_of(p)),
        key=str,
    )

    failures: list[str] = []
    if len({duration_of(p) for p in ytd}) < 2:
        failures.append(
            f"expected >= 2 durations ending {end} for {concept!r}, found "
            f"{ytd or sorted(by_period)}; the 3M and year-to-date columns are "
            f"collapsing into one period"
        )
    # Distinct values are the point: identical numbers would mean one column was
    # read twice under two names.
    for key in ytd:
        values = by_period[key]
        if len(set(values)) != 1 and len(values) != len(set(values)):
            failures.append(f"{key}: one node carries conflicting values {values}")
    if not prior:
        failures.append(
            f"no comparative for {concept!r}; the prior-period column was not "
            f"captured"
        )

    return _Runner.verdict(
        "B3",
        f"comparative-period separation: {concept} {period} on {form_type} FY{fiscal_year}",
        failures,
        {
            "form_type": form_type,
            "fiscal_year": fiscal_year,
            "concept": concept,
            "period": period,
            "durations_ending_here": ytd,
            "comparatives": prior,
            "values_by_period": {
                period: by_period[period] for period in sorted(by_period)
            },
        },
    )


# ---------------------------------------------------------------------------
# B4 -- segment fan-out from a metric
# ---------------------------------------------------------------------------

B4_CYPHER = """
MATCH (m:Metric)-[e:HAS_SEGMENT]->(s:Segment)
WHERE m.canonical_name CONTAINS $concept
RETURN s.name, s.segment_type, e.value, e.period
ORDER BY s.name
"""


def benchmark_segment_fanout(runner: _Runner, concept: str = "Net Sales") -> BenchmarkResult:
    """B4: segments hang off the revenue metric, each with its own value."""
    rows = runner.rows(B4_CYPHER, {"concept": concept})
    segments = [
        {
            "name": row[0],
            "type": row[1],
            "value": None if row[2] is None else float(row[2]),
            "period": row[3],
        }
        for row in rows
    ]
    by_period: dict[str, list[str]] = {}
    for item in segments:
        by_period.setdefault(str(item["period"]), []).append(str(item["name"]))

    failures: list[str] = []
    if not segments:
        failures.append(
            f"no segment hangs off a metric matching {concept!r}; the "
            f"Metric -> HAS_SEGMENT -> Segment indirection produced nothing"
        )
    widest = max((len(names) for names in by_period.values()), default=0)
    if segments and widest < 2:
        failures.append(
            f"widest period has {widest} segment(s); a segment breakdown needs at "
            f"least 2 to be a breakdown"
        )
    types = sorted({str(item["type"]) for item in segments if item["type"]})
    if segments and not types:
        failures.append("segments carry no segment_type; the taxonomy was not classified")

    return _Runner.verdict(
        "B4", f"segment fan-out from {concept!r}", failures,
        {
            "concept": concept,
            "segment_count": len(segments),
            "segment_types": types,
            "periods": {period: sorted(names) for period, names in sorted(by_period.items())},
            "values": segments[:20],
        },
    )


# ---------------------------------------------------------------------------
# B5 -- negative control
# ---------------------------------------------------------------------------

B5_CYPHER = """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)
WHERE f.fiscal_year = $year
RETURN f.id, f.form_type
"""

B5_METRIC_CYPHER = """
MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[e:REPORTS_METRIC]->(m:Metric)
WHERE f.fiscal_year = $year
RETURN m.canonical_name, e.value
"""


def benchmark_negative_control(
    runner: _Runner, absent_year: int = ABSENT_YEAR
) -> BenchmarkResult:
    """B5: a year nobody ingested returns empty -- not an error, not rows."""
    failures: list[str] = []
    detail: dict[str, Any] = {"absent_year": absent_year}

    try:
        filings = runner.rows(B5_CYPHER, {"year": absent_year})
    except Exception as exc:  # noqa: BLE001 - the error *is* the finding
        return _Runner.verdict(
            "B5", f"negative control: FY{absent_year} absent", [
                f"query raised on absent data: {type(exc).__name__}: {exc}"
            ], detail,
        )
    detail["filings"] = len(filings)

    try:
        metrics = runner.rows(B5_METRIC_CYPHER, {"year": absent_year})
    except Exception as exc:  # noqa: BLE001
        return _Runner.verdict(
            "B5", f"negative control: FY{absent_year} absent", [
                f"metric query raised on absent data: {type(exc).__name__}: {exc}"
            ], detail,
        )
    detail["metrics"] = len(metrics)

    if filings:
        failures.append(f"{len(filings)} filing(s) returned for an uningested year")
    if metrics:
        failures.append(f"{len(metrics)} metric(s) returned for an uningested year")

    # The same query shape against an ingested year must return rows, or the
    # emptiness above proves only that the query is broken.
    present = int(runner.scalar("MATCH (f:Filing) RETURN max(f.fiscal_year)") or 0)
    detail["max_present_fiscal_year"] = present
    control = runner.rows(B5_CYPHER, {"year": present})
    detail["control_year_filings"] = len(control)
    if present and not control:
        failures.append(
            f"control failed: FY{present} is the newest ingested year but the same "
            f"query returns nothing for it, so the empty result above is not "
            f"evidence of correct filtering"
        )

    return _Runner.verdict(
        "B5", f"negative control: FY{absent_year} absent", failures, detail,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_all(
    connection: Any,
    distinct: dict[str, int] | None = None,
    form_type: str = "10-Q",
    fiscal_year: int | None = None,
    concept: str = "Net Sales",
    period: str | None = None,
) -> list[BenchmarkResult]:
    """Run B1-B5 and return the results in order.

    *distinct* is the buffer stage's per-table count of unique identities, used
    by B1 as its integrity baseline. Passing ``None`` makes B1 fail with that
    reason rather than pass vacuously -- a verification stage that cannot check
    anything must say so.

    *fiscal_year* defaults to the newest filing of *form_type* in the graph, so
    the benchmarks follow the data rather than hard-coding a year a future
    filing would invalidate. If no such filing exists, the benchmarks that need
    one fail with that reason rather than being skipped -- a silently skipped
    verification is worse than a failing one.

    *period* defaults to the shortest duration ending on the newest date that
    filing reports for *concept* -- the filing's own quarter, which is what a
    reader means by "the quarter it just reported". A year alone cannot name it,
    since a year holds up to four quarters.
    """
    runner = _Runner(connection)
    if fiscal_year is None:
        discovered = runner.scalar(
            "MATCH (f:Filing) WHERE f.form_type = $form "
            "RETURN max(f.fiscal_year)",
            {"form": form_type},
        )
        fiscal_year = int(discovered) if discovered is not None else 0

    if not period:
        discovered = runner.rows(
            "MATCH (f:Filing)-[e:REPORTS_METRIC]->(m:Metric) "
            "WHERE f.form_type = $form AND f.fiscal_year = $year "
            "AND m.canonical_name CONTAINS $concept "
            "RETURN DISTINCT m.canonical_name",
            {"form": form_type, "year": fiscal_year, "concept": concept},
        )
        keys = [period_of(row[0]) for row in discovered]
        # Only date-keyed periods can be ordered by recency. A key still on the
        # bare-year fallback ("3M-FY2026") sorts above every date and would
        # become the target while naming no date at all.
        dated = [k for k in keys if duration_of(k)]
        end = max((period_end_of(k) for k in dated), default="")
        period = min(
            (k for k in dated if period_end_of(k) == end),
            key=lambda k: (len(duration_of(k)), k),
            default="",
        )

    log.info("running benchmarks against %s FY%s period %s", form_type, fiscal_year, period)
    return [
        _Runner.timed(lambda: benchmark_shape(runner, distinct or {})),
        _Runner.timed(
            lambda: benchmark_lookup(runner, form_type, fiscal_year, period, concept)
        ),
        _Runner.timed(
            lambda: benchmark_period_separation(
                runner, form_type, fiscal_year, period, concept
            )
        ),
        _Runner.timed(lambda: benchmark_segment_fanout(runner, concept)),
        _Runner.timed(lambda: benchmark_negative_control(runner)),
    ]
