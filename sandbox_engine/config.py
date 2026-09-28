"""Paths, document scope, and tunables for the sandbox pipeline.

This module holds every value the rest of the sandbox treats as configuration,
and nothing else. It has no imports from the rest of the package, so it can be
read to learn the whole shape of the run in one pass.

Document scope
--------------

The scope is the document tree itself. Filings are stored at

    sandbox_engine/data/<company>/<year>/<form>/<filing>.htm

where ``<company>`` is the issuer's folder name (``apple``), ``<year>`` is the
reporting calendar year (``2026``), and ``<form>`` is one of ``10k``, ``10q``
or ``8k``. :func:`resolve_scope` walks that tree and returns every ``.htm`` it
finds, so the pipeline is universal: any issuer can be ingested by dropping its
documents into the same shape, and the resolve step never has to know a company
by name. Adding data needs no config edit and no ``Scope`` list -- the folders
*are* the scope.

The three form folders exercise genuinely different paths through the parser. A
10-K carries three comparative years of every statement line, so its metric
nodes are period-scoped and conflict resolution has something to resolve. A
10-Q carries a three-month and a six-month column under the *same* period-end
date, which only stays separable because the duration banner above the date row
is read. An 8-K has no financial statements at all and only produces ``Event``
nodes.
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
    "DATA_DIR",
    "FORM_FOLDERS",
    "Paths",
    "PERIOD_SCOPED_METRICS",
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
#: into ``canonical_name`` as ``"Net Sales (3M-2026-03-28)"``.
#:
#: This is a deliberate trade of node count for comparative data.
#: ``REPORTS_METRIC`` carries a single ``value``, so a 10-K showing three years
#: of Net Sales needs three distinct ``Metric`` nodes. With ``False`` you get a
#: clean one-node-per-concept taxonomy and the comparative columns overwrite
#: each other, losing two years of every line.
#:
#: The period is keyed on its end date rather than the fiscal year and quarter
#: it implies, because a fiscal year holds up to four quarters: ``3M-FY2026``
#: names three different quarters at once, so the three filings reporting them
#: attach three values to one node. A table whose header printed only a year
#: has no date to key on and falls back to ``3M-FY2026``.
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

#: The document tree beneath the repository root, in the shape described in the
#: module docstring: ``<company>/<year>/<form>/<filing>.htm``.
DATA_DIR = Path("sandbox_engine") / "data"

#: The lowercase form folders under each ``<company>/<year>`` directory, in the
#: order filings are ingested. The folder is what labels the form -- the resolve
#: step never has to open the document to know what it is.
FORM_FOLDERS: tuple[str, ...] = ("10k", "10q", "8k")


def resolve_scope(root: str | Path) -> list[Path]:
    """Return every filing under ``DATA_DIR``, ordered by form folder.

    This is a filesystem walk, not a list of paths, so the scope is whatever
    the document tree contains. Adding a company means creating its folder and
    dropping in documents; removing one means deleting the folder. The resolver
    never names a company, which is what makes the pipeline universal -- the
    folder *is* the company.

    Raises:
        FileNotFoundError: the data tree holds no filings at all. A pipeline
            that ingests nothing must say so loudly rather than report an empty
            artifact as success.
    """
    root = Path(root)
    data_root = root / DATA_DIR
    if not data_root.is_dir():
        raise FileNotFoundError(f"no document tree at {data_root}")
    filings: list[Path] = []
    for form in FORM_FOLDERS:
        filings.extend(sorted(data_root.glob(f"*/*/{form}/*.htm")))
    if not filings:
        raise FileNotFoundError(
            f"no filings under {data_root}; expected "
            f"<company>/<year>/[{','.join(FORM_FOLDERS)}]/*.htm"
        )
    return filings


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
