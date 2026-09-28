"""Paths, test scope, and tunables for the sandbox pipeline.

This module holds every value the rest of the sandbox treats as configuration,
and nothing else. It has no imports from the rest of the package, so it can be
read to learn the whole shape of the run in one pass.

Test scope
----------

The sandbox ingests exactly three filings, one of each form type, resolved from
``data/``. This is a hard limit, enforced in :func:`resolve_scope`, which walks
:class:`Scope` entries and never returns more than ``Scope.limit`` paths. The
full 75-filing corpus is out of scope for the sandbox on purpose: a verification
harness that runs in a few seconds can be re-run after every change, and one
that takes an hour will not be.

Why one filing per form type: the three forms exercise genuinely different
paths through the parser. A 10-K carries three comparative years of every
statement line, so its metric nodes are period-scoped and conflict resolution
has something to resolve. A 10-Q carries a three-month and a six-month column
under the *same* period-end date, which only stays separable because the
duration banner above the date row is read. An 8-K has no financial statements
at all and only produces ``Event`` nodes. A scope with three 10-Qs would prove
none of that.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ABSENT_YEAR",
    "BUFFER_POOL_BYTES",
    "COPY_THRESHOLD",
    "ENTITY_FUZZY_MATCH",
    "ENTITY_SIMILARITY_THRESHOLD",
    "MAX_CHUNK_CHARS",
    "MAX_EVENTS",
    "MIN_CHUNK_CHARS",
    "Paths",
    "PERIOD_SCOPED_METRICS",
    "Scope",
    "SCOPE",
    "VERSION",
    "default_paths",
    "resolve_scope",
]

VERSION = "1.0.0"

#: Engine buffer pool, in **bytes**. LadybugDB takes a plain int, not a
#: quantity with a unit, so a literal ``256MB`` would silently mean 256 bytes
#: and the database would spill on a 3-filing load. This is a byte count that
#: bounds the engine's buffer manager; it is not an RSS ceiling.
BUFFER_POOL_BYTES = 256 * 1024 * 1024

#: Rows at or above which the loader uses ``COPY <table> FROM $arrow`` instead
#: of a parameterised ``UNWIND ... CREATE``. Below this a parameterised insert
#: is genuinely cheaper: ``COPY`` pays a fixed setup cost per statement that a
#: 3-row ``Company`` table does not earn back.
#:
#: This is deliberately **lower than a production threshold**. At the production
#: value of 2000, a three-filing run puts every table below the line and the
#: ``COPY`` path is never executed -- so a sandbox run would report success
#: without having tested the bulk loader at all. At 250, one run exercises both
#: paths: ``Metric``, ``REPORTS_METRIC``, ``Chunk``, and ``HAS_CHUNK`` go
#: through ``COPY``, while ``Company``, ``Filing``, ``Segment``, and ``Event``
#: go through ``UNWIND``. The run report's ``methods`` map is how you confirm
#: both fired.
COPY_THRESHOLD = 250

#: Chunking bounds. ``MAX_CHUNK_CHARS`` is a target, not a guarantee: a single
#: ``<p>`` longer than this is split at a sentence boundary near the limit.
MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 40

#: Cap on 8-K ``Item`` events per filing. Bounds a pathological document that
#: cross-references item codes in running text.
MAX_EVENTS = 32

#: ``True`` makes the reporting period part of ``Metric`` identity, written
#: into ``canonical_name`` as ``"Net Sales (FY2025)"``.
#:
#: This is a deliberate trade of node count for comparative data.
#: ``REPORTS_METRIC`` carries a single ``value``, so a 10-K showing three years
#: of Net Sales needs three distinct ``Metric`` nodes. With ``False`` you get a
#: clean one-node-per-concept taxonomy and the comparative columns overwrite
#: each other, losing two years of every line.
PERIOD_SCOPED_METRICS = True

#: Similarity score at which two labels are considered the same concept.
#: Matches the root resolver's default so the two are comparable.
ENTITY_SIMILARITY_THRESHOLD = 0.88

#: Whether similarity matching is allowed to merge two labels at all.
#:
#: **``False`` by default, and this is the measured result, not caution.**
#: ``entity_resolver`` resolves entities in three stages: exact name, exact
#: alias, then similarity. The first two are facts. The third is a guess, and on
#: this corpus it guesses wrong in ways that matter:
#:
#: * ``"...restricted cash and cash equivalents, beginning balances"`` against
#:   the same label ending ``ending balances`` scores **0.889** -- above the
#:   threshold. Turning it on merges an opening balance into a closing balance.
#: * ``Total non-current assets`` scores 0.50 against ``Total Assets`` and
#:   ``Total lease liabilities`` 0.50 against ``Total Liabilities``; at any
#:   threshold loose enough to catch the intended merges, these follow.
#:
#: A missed merge is recoverable -- a later filing can add the alias, and the
#: two nodes show up as a count that looks too high. A wrong merge is not: two
#: values become one node and nothing downstream can tell. So the third stage is
#: opt-in. The aliases it would have guessed are declared instead, in
#: ``entity_resolver.CONCEPT_ALIASES``, where a reviewer can read them.
ENTITY_FUZZY_MATCH = False

#: A year deliberately outside the three-filing scope. Benchmark 5 requires this
#: to return an empty set; a query that *errors* on absent data is a different
#: defect from one that returns rows for a year nobody ingested.
ABSENT_YEAR = 2019


@dataclass(frozen=True)
class Scope:
    """One filing to ingest, as a preference-ordered list of path fragments.

    ``candidates`` is ordered most-specific first. The first fragment that
    exists wins; if none does exactly, the last fragment is treated as a glob
    prefix and the first alphabetical match is used. That is what lets a
    renamed or re-dated filing still resolve without editing this file.
    """

    form_type: str
    candidates: tuple[str, ...]
    label: str = ""


#: The three test filings: one 10-K, one 10-Q, one 8-K.
SCOPE: tuple[Scope, ...] = (
    Scope(
        form_type="10-K",
        label="annual report, three comparative years",
        candidates=(
            "data/aapl-sec/10-K_2025-10-31_aapl-20250927.htm",
            "data/aapl-sec/10-K_2025",
            "data/aapl-sec/10-K",
        ),
    ),
    Scope(
        form_type="10-Q",
        label="quarterly report, 3M and 6M columns",
        candidates=(
            "data/aapl-sec/10-Q_2025-08-01_aapl-20250628.htm",
            "data/aapl-sec/10-Q_2025-08",
            "data/aapl-sec/10-Q_2025",
        ),
    ),
    Scope(
        form_type="8-K",
        label="current report, events only",
        candidates=(
            "data/aapl-sec/8-K_2025-10-30_aapl-20251030.htm",
            "data/aapl-sec/8-K_2025-10-30",
            "data/aapl-sec/8-K_2025-10",
        ),
    ),
)

#: Hard cap on resolved filings. :func:`resolve_scope` will not exceed this even
#: if ``SCOPE`` is extended, which is what makes "limit ingestion strictly to 3
#: test files" a property of the code rather than a property of this edit.
SCOPE_LIMIT = 3


def resolve_scope(root: Path, scope: tuple[Scope, ...] = SCOPE) -> list[Path]:
    """Resolve the test scope to existing paths, at most :data:`SCOPE_LIMIT`.

    Raises:
        FileNotFoundError: a scope entry matched no file. Failing loudly beats
            ingesting two of three forms, which would leave a benchmark passing
            for the wrong reason.
    """
    root = Path(root)
    resolved: list[Path] = []
    for entry in scope:
        if len(resolved) >= SCOPE_LIMIT:
            break
        for candidate in entry.candidates:
            path = root / candidate
            if path.is_file():
                resolved.append(path)
                break
        else:
            matches = sorted(root.glob(entry.candidates[-1] + "*"))
            if not matches:
                raise FileNotFoundError(
                    f"no filing for {entry.form_type}; tried "
                    f"{', '.join(entry.candidates)} under {root}"
                )
            resolved.append(matches[0])
    return resolved


@dataclass(frozen=True)
class Paths:
    """Every filesystem path the pipeline reads or writes.

    All sandbox output lives under :attr:`root`, so a run leaves the repository
    root untouched and ``--reset`` is a single directory removal.
    """

    root: Path
    db: Path
    staging: Path
    report: Path
    registry: Path

    @classmethod
    def under(cls, root: str | Path) -> "Paths":
        """Default layout beneath *root*.

        ``staging`` holds the Parquet spill from the buffer stage, which is the
        stage's whole point: the loader reads from disk, not from a Python
        object that never left memory. ``report`` is the JSON run report.
        ``registry`` is the canonical-entity registry -- the dedup state that
        makes a re-ingest land on the nodes the previous run created instead of
        rediscovering them.
        """
        root = Path(root).resolve()
        sandbox = root / "sandbox_engine" / "_run"
        return cls(
            root=root,
            db=sandbox / "sandbox.lbug",
            staging=sandbox / "staging",
            report=sandbox / "report.json",
            registry=sandbox / "concepts.json",
        )

    def ensure(self) -> "Paths":
        """Create the output directories. Safe to call repeatedly."""
        self.staging.mkdir(parents=True, exist_ok=True)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        return self

    def reset(self) -> None:
        """Delete the database, the staging spill, the report, and the registry.

        The database is removed as a whole directory: LadybugDB creates the
        ``.lbug`` path alongside ``.wal`` and ``.tmp`` siblings, so deleting the
        single named file leaves the transaction log behind and the next open
        reports a stale WAL.

        The registry goes with it. It is dedup *state*, not a cache: keeping it
        across a reset would let a run resolve onto entities belonging to a
        graph that no longer exists, which is the one thing a reset has to
        prevent.
        """
        import shutil

        for path in (
            self.db,
            self.db.with_name(self.db.name + ".wal"),
            self.report,
            self.registry,
        ):
            if path.is_file():
                path.unlink()
        if self.db.is_dir():
            shutil.rmtree(self.db)
        if self.staging.is_dir():
            shutil.rmtree(self.staging)
        self.ensure()


def default_paths(root: str | Path | None = None) -> Paths:
    """Paths for a run rooted at *root*, defaulting to the repository root."""
    if root is None:
        # sandbox_engine/config.py -> sandbox_engine -> repository root
        root = Path(__file__).resolve().parent.parent
    return Paths.under(root)
