"""LadybugDB-backed knowledge graph store with a pivot search index.

Self-contained: depends only on ``ladybug`` (pip install ladybug) and the
standard library. It is the storage half of the extraction pipeline --
``graph_extractor`` produces raw triples, ``entity_resolver`` canonicalises
them, and this module persists the result and makes it searchable.

    import graph_store as gs

    with gs.GraphStore("kg.lbug") as store:
        store.ingest_graph(resolved_subgraph)        # resolve_subgraph() output
        pivots = gs.find_pivot_nodes(store.conn, "how does attention work?")
        context = gs.expand_relevance(store.conn, pivots, max_hops=2)

Why the search is built this way
--------------------------------

LadybugDB has no secondary-index DDL. ``CREATE INDEX`` is a parser error in
0.20.4, and ``SHOW_INDEXES()`` reports only the primary-key hash on ``Entity.id``
-- there is no index you can declare on ``name`` or ``aliases``. A "pivot search
index" is therefore not something the database can hold for you; it is an access
pattern this module implements and maintains.

That constraint shapes the design. There is no inverted index to consult, so
``find_pivot_nodes`` does a bounded scan: Cypher ``CONTAINS`` narrows the
candidate set down in the engine, then Python does the fuzzy ranking that the
engine has no function for. The engine has no edit-distance or similarity
scalar (the full ``SHOW_FUNCTIONS()`` inventory has none), so scoring cannot be
pushed into the query.

The scan is bounded on purpose. ``CONTAINS`` is a full table scan, so an
unbounded query would materialise every row in the process on a machine that was
supposed to be memory-constrained. ``max_candidates`` caps the rows pulled back,
and rows are streamed rather than materialised as one list.

Engine behaviours this module is written around
-----------------------------------------------

Each of these was verified against ladybug 0.20.4 rather than assumed, and each
one has broken a plausible-looking implementation at least once:

* ``UNWIND $list AS x`` raises a binder error when ``$list`` is empty -- and it
  does so even with a ``MATCH``, not only with ``MERGE``. Every UNWIND in this
  module is guarded against empty input. See ``_guarded_unwind``.
* List comprehensions (``[t IN $list | t]``) are not supported, with or without
  an inline ``WHERE``. Per-token matching uses the ``ANY(t IN $list WHERE ...)``
  quantified form instead.
* Property access over a variable-length relationship is a binder error, so the
  2-hop expansion is two explicit ``MATCH`` clauses rather than
  ``[x IN RELATIONSHIPS(r) | x.action]``.
* A failed statement inside ``BEGIN TRANSACTION`` aborts the whole transaction
  and leaves none active, so a subsequent ``ROLLBACK`` fails with "No active
  transaction". Batch failure is reported without issuing a ``ROLLBACK``.
* ``MERGE`` on a bare relationship pattern matches *any* edge between the pair,
  which would silently swallow a second, different action between the same two
  nodes. The action is part of the ``MERGE`` pattern so distinct actions
  coexist and re-ingesting the same edge is still a no-op.
* ``COPY ... FROM 'f.csv'`` defaults to ``HEADER=false`` and will happily ingest
  the header row as data, and it is not idempotent -- a second copy violates the
  primary key. Bulk copy is available via ``copy_from_csv`` for a first load;
  the default ingest path is a batched upsert that is safe to re-run.

Memory
------

``buffer_pool_size`` is an integer count of bytes, and it is the engine's
buffer manager, not a hard RSS ceiling: query working memory is additional.
256 MB is the configured default here. The other caps that keep a low-RAM
machine alive are the ingest batch size, ``max_candidates`` on the search
prefilter, and ``limit_per_hop`` on the expansion.

One handle per path
-------------------

Open a database path with exactly one :class:`lb.Database` at a time, and share
it. A second handle on the same path is **not** rejected: it opens, reads the
committed state, and then silently diverges. Writes through the second handle are
invisible to the first, and each keeps counting only what it has itself seen.
Verified on 0.20.4 -- two handles, one write each, each reporting a different
count with no error raised. There is no lock to catch this, so treat
``open_store`` on a live path as a programming error and hold the store in one
place. Long-lived servers should open once at startup and keep the handle.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Iterable, Iterator, Mapping, Sequence

import ladybug as lb
import pyarrow as pa

__all__ = [
    "BUFFER_POOL_MB",
    "DEFAULT_BATCH_SIZE",
    "GraphStore",
    "GraphStoreError",
    "IngestReport",
    "PivotHit",
    "RelevancePath",
    "expand_paths",
    "expand_relevance",
    "find_pivot_nodes",
    "find_pivot_nodes_detailed",
    "main",
    "open_store",
    "schema_ddl",
    "store_stats",
]

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Default engine buffer pool. This is a byte count: ladybug takes an int, not
#: a quantity with a unit, so a "256MB" literal would silently mean 256 bytes.
BUFFER_POOL_MB = 256
BUFFER_POOL_BYTES = BUFFER_POOL_MB * 1024 * 1024

#: Rows per ingest batch. Bounds the parameter payload and the transaction's
#: working set, which is the other half of the memory budget.
DEFAULT_BATCH_SIZE = 500

#: Ceiling on traversal depth. 2 hops already fans out quadratically on a dense
#: graph, so this is a backstop against a caller asking for the whole database.
MAX_HOPS = 3

#: Rows pulled back from the CONTAINS prefilter before Python-side ranking.
DEFAULT_MAX_CANDIDATES = 5000

#: Rows read per hop in the expansion, per pivot set.
DEFAULT_LIMIT_PER_HOP = 5000

#: Default cap on returned pivots.
DEFAULT_PIVOT_LIMIT = 20

#: Minimum score for an entity to be called a pivot. Deliberately low: a pivot
#: set is a *recall* stage feeding a traversal, and the traversal is cheap
#: compared to a missed entity that would have anchored the answer.
DEFAULT_MIN_SCORE = 0.30

#: Alias lists are stored as one comma-separated STRING, per the schema. A comma
#: inside an alias term therefore cannot round-trip. Writing uses a comma *and* a
#: space so the stored value stays readable; parsing accepts either.
ALIAS_SEPARATOR = ","
_ALIAS_JOIN = ", "

ENTITY_TABLE = "Entity"
RELATION_TABLE = "RELATION"

#: Column order of each table, which is also the order ``COPY`` maps by position.
#: Named rather than derived from the DDL so the reader, the writer and the
#: bulk loader cannot drift apart without a name to grep for.
ENTITY_COLUMNS = ("id", "name", "category", "description", "aliases")
RELATION_COLUMNS = ("from", "to", "action", "context")

_NODE_DDL = (
    f"CREATE NODE TABLE IF NOT EXISTS {ENTITY_TABLE} ("
    "id STRING, name STRING, category STRING, description STRING, "
    f"aliases STRING, PRIMARY KEY (id))"
)
_REL_DDL = (
    f"CREATE REL TABLE IF NOT EXISTS {RELATION_TABLE} "
    f"(FROM {ENTITY_TABLE} TO {ENTITY_TABLE}, action STRING, context STRING)"
)


def schema_ddl() -> tuple[str, str]:
    """Return the ``(node, relationship)`` DDL statements this module relies on."""
    return (_NODE_DDL, _REL_DDL)


class GraphStoreError(RuntimeError):
    """Raised for schema, ingest, and query failures that callers should see."""


# --------------------------------------------------------------------------
# Text handling: tokenisation, normalisation, similarity
#
# Deliberately self-contained. The same discipline is applied in
# entity_resolver.py, where a wrong merge is far more expensive than a missed
# one; here a wrong pivot wastes a traversal, and a missed pivot loses the node
# that would have anchored the answer.
# --------------------------------------------------------------------------

#: Small, domain-neutral English stop list. Kept short on purpose -- dropping a
#: content word costs recall, and every function word that reaches the scorer
#: only has to fail to match, which it will.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by do does did for from had has have how in into is
    it its of on or that the their them then there these they this to was were
    what when where which who why will with you your
    """.split()
)

_MIN_TOKEN = 2

#: Queries this short are treated as a deliberate lookup rather than noise, even
#: though they fall below ``_MIN_TOKEN``. Single-character aliases are real
#: ("L" for Lagrangian, "B" for a bond), and dropping them would throw away an
#: exact hit the user typed on purpose.
_MAX_SHORT_QUERY = 3

# Token-affinity tiers, highest trust first. See :func:`_token_affinity`.
_PREFIX_SCORE = 0.92
_SUBSTRING_SCORE = 0.85
_SUBSTRING_COVERAGE = 0.4
_RATIO_FLOOR = 0.86

#: Minimum whole-string resemblance for the fuzzy tier. Below this the candidate
#: is dropped rather than ranked low, because every prefix and substantial-
#: substring match has already been scored by a token tier. Measured: "act" in
#: "transaction" is 0.43, "ab" in "abcabcabcabc" is 0.29.
_FUZZY_FLOOR = 0.7

#: Best single-token affinity that keeps a candidate regardless of coverage.
#: Exact (1.0) and prefix-grade (0.92) matches clear it; the substring tier
#: (0.85) and fuzzy do not, so `min_score` still governs those.
_MIN_PIVOT_TOKEN = 0.9


def _fold(text: Any) -> str:
    """Casefold and strip accents, keeping alphanumerics and collapsing the rest.

    Accents are decomposed then dropped rather than transliterated, so ``Émile``
    and ``Emile`` collapse together without a language-specific table.

    Alphanumeric is decided by ``str.isalnum`` rather than an ASCII allowlist, so
    Cyrillic, Greek and CJK survive folding. That matters because
    entity_resolver deliberately mints ``entity-<sha1>`` ids for non-Latin names
    and keeps the original spelling in ``name`` -- an ASCII-only fold would make
    those entities unsearchable by the only text a reader has.
    """
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    out: list[str] = []
    for char in decomposed.casefold():
        if unicodedata.combining(char):
            continue
        out.append(char if char.isalnum() else " ")
    return " ".join("".join(out).split())



def _tokenize(text: Any) -> tuple[str, ...]:
    """Fold, split, drop stopwords and single characters, preserving order.

    Order is preserved and duplicates are removed, so a query of
    "attention attention" tokenises once rather than scoring itself twice.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _fold(text).split():
        if len(token) < _MIN_TOKEN or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tuple(tokens)


def _query_tokens(user_query: Any) -> tuple[str, ...]:
    """Tokens to search with, with a deliberate-lookup fallback.

    Tokenising normally drops anything under ``_MIN_TOKEN``, which is right for a
    question ("what is the ...") and wrong for a lookup. ``"L"`` tokenises to
    nothing, and yet ``L`` is a real alias of Lagrangian -- a user typing it meant
    it. So a query that tokenises to nothing but folds to something short is used
    as a single token. A long stopword sentence still returns nothing and never
    reaches the database.
    """
    tokens = _tokenize(user_query)
    if tokens:
        return tokens
    folded = _fold(user_query)
    return (folded,) if 0 < len(folded) <= _MAX_SHORT_QUERY else ()


@lru_cache(maxsize=8192)
def _ratio(left: str, right: str) -> float:
    """Symmetric character similarity in ``[0, 1]``.

    ``SequenceMatcher.ratio`` is not symmetric -- it matches its longest
    contiguous blocks, and which blocks those are depends on which string it
    starts from. ``("ddaceddcd", "ebebc")`` scores 0.14 one way and 0.29 the
    other. A score that depends on argument order cannot decide a merge, so
    both directions are averaged.
    """
    if not left or not right:
        return 0.0
    forward = SequenceMatcher(None, left, right).ratio()
    reverse = SequenceMatcher(None, right, left).ratio()
    return (forward + reverse) / 2.0


def _token_affinity(query_token: str, entity_tokens: frozenset[str]) -> float:
    """How well one query token is accounted for by an entity's tokens.

    Four ways to count, in descending order of trust:

    1. Exact equality, 1.0.
    2. A prefix relationship at four characters or more, 0.92. This is what lets
       ``lagrang`` reach ``lagrangian``, and ``equation`` reach ``equations`` --
       the CONTAINS prefilter already admits those rows, so refusing to score
       them would discard the hit the database just found for us.
    3. A substring that is not a prefix, 0.85, and only when the query token
       covers at least ``_SUBSTRING_COVERAGE`` of the candidate. Scripts without
       word delimiters make this the *primary* relation: ``多模态`` is a substring
       of ``多模态大模型``, not a sibling token, so without it CJK search finds
       nothing. The coverage floor is what keeps it safe for the rest -- ``act``
       is a substring of ``transaction`` but covers only 27% of it, and is
       rejected, while ``多模态`` covers 50% of ``多模态大模型`` and is not.
    4. A character ratio, and only above 0.86: one near-miss token out of several
       cannot manufacture a pivot on its own.
    """
    if query_token in entity_tokens:
        return 1.0
    best = 0.0
    for candidate in entity_tokens:
        if len(query_token) >= 4 and (
            candidate.startswith(query_token) or query_token.startswith(candidate)
        ):
            if _PREFIX_SCORE > best:
                best = _PREFIX_SCORE
            continue
        shorter, longer = sorted((query_token, candidate), key=len)
        if (
            len(shorter) >= 2
            and shorter in longer
            and len(shorter) / len(longer) >= _SUBSTRING_COVERAGE
            and _SUBSTRING_SCORE > best
        ):
            best = _SUBSTRING_SCORE
            continue
        score = _ratio(query_token, candidate)
        if score >= _RATIO_FLOOR and score > best:
            best = score
    return best


def _split_aliases(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, str):
        return ()
    return tuple(
        part.strip() for part in raw.split(ALIAS_SEPARATOR) if part.strip()
    )


@dataclass(frozen=True)
class PivotHit:
    """One scored pivot candidate.

    Returned by :func:`find_pivot_nodes_detailed`. :func:`find_pivot_nodes`
    discards the evidence and returns ids only, but the evidence is what makes
    a bad ranking debuggable rather than merely wrong.
    """

    entity_id: str
    score: float
    evidence: str
    surface: str
    matched_tokens: tuple[str, ...] = ()
    best_token: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.entity_id,
            "score": round(self.score, 4),
            "evidence": self.evidence,
            "surface": self.surface,
            "matched_tokens": list(self.matched_tokens),
            "best_token": round(self.best_token, 4),
        }


@dataclass(frozen=True)
class RelevancePath:
    """A traversed connection between a pivot and a reached node."""

    pivot: str
    node: str
    hops: int
    actions: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    via: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pivot": self.pivot,
            "node": self.node,
            "hops": self.hops,
            "via": self.via,
            "actions": list(self.actions),
            "contexts": list(self.contexts),
        }


@dataclass
class IngestReport:
    """Outcome of an ingest, including what was refused and why.

    Refusals are counted rather than raised by default. A silently dropped edge
    is indistinguishable from an edge that was never extracted, and that
    distinction is exactly what you need when a graph comes back empty.
    """

    entities: int = 0
    relations: int = 0
    relations_skipped_orphan: int = 0
    relations_skipped_duplicate: int = 0
    relations_skipped_self_loop: int = 0
    entities_rejected: list[str] = field(default_factory=list)
    missing_endpoints: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entities": self.entities,
            "relations": self.relations,
            "relations_skipped_orphan": self.relations_skipped_orphan,
            "relations_skipped_duplicate": self.relations_skipped_duplicate,
            "relations_skipped_self_loop": self.relations_skipped_self_loop,
            "entities_rejected": list(self.entities_rejected),
            "missing_endpoints": list(self.missing_endpoints),
        }


# --------------------------------------------------------------------------
# Query plumbing
# --------------------------------------------------------------------------


def _execute(conn: Any, query: str, params: Mapping[str, Any] | None = None) -> Any:
    """Run a statement that returns nothing useful (DDL, DML, transactions)."""
    result = conn.execute(query, dict(params) if params else None)
    # Multi-statement calls come back as a list; nothing to collect.
    if isinstance(result, list):
        return result
    return result


def _fetch(
    conn: Any, query: str, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Run a query and return rows as dicts, streaming the result.

    Streamed rather than ``get_all()`` so peak memory is one row plus the output
    list, which is what the candidate cap is able to bound. Every query in this
    module aliases its projections, because ``get_column_names`` is only
    predictable for named columns.
    """
    result = conn.execute(query, dict(params) if params else None)
    if isinstance(result, list):
        result = result[-1]
    columns = result.get_column_names()
    rows: list[dict[str, Any]] = []
    while result.has_next():
        rows.append(dict(zip(columns, result.get_next())))
    result.close()
    return rows


def _guarded_unwind(values: Sequence[str]) -> list[str] | None:
    """Return ``values``, or ``None`` if a query using UNWIND must not run.

    ``UNWIND`` over an empty parameter list is a binder error in this engine,
    and the error is not about emptiness -- it misreports the bound variable's
    type. An empty pivot set or an empty batch is ordinary, so it is filtered
    here instead of being allowed to reach the engine as a crash.
    """
    return list(values) if values else None


def _sql_int(value: Any, name: str) -> str:
    """Render a validated integer literal for inline use in SQL.

    ``LIMIT $param`` does not work in LadybugDB 0.20.4 and fails silently, which
    is the worst failure mode available. Measured on 50 rows: a bound limit of 1,
    2, 3, 5, 7, 10, 20 or 50 each returned **1** row, and 200 or 5000 each
    returned all 50. The bound value was ignored in every case -- the parameter
    name is irrelevant, since ``$lim`` and ``$cap`` behaved identically, and the
    plan matters too, since the same binder returned 5 rows for a query with a
    ``WHERE``. An inline literal was exact at 1, 5, 7, 50, 200 and 5000.

    So the limit is interpolated instead of bound. That is only safe because this
    function accepts an ``int``, validates it, and returns digits -- an
    unvalidated f-string of a caller-supplied value would be an injection hole.
    Non-integral input is rejected rather than coerced, because silently turning
    ``2.7`` into ``2`` hides a caller bug.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return str(value)


def _read_csv(path: Any, columns: Sequence[str], header: bool = True) -> pa.Table:
    """Read a CSV into an Arrow table shaped for a bound ``COPY``.

    This engine's ``COPY`` has exactly one form, ``COPY <table> FROM $data``,
    and $data must be an Arrow table. There is no ``FROM 'path'`` to fall back
    on: the grammar stops at the keyword -- ``Parser exception: Invalid input
    <COPY Entity FROM ">: expected rule oC_Statement`` -- so a loader written
    against the path form does not degrade, it fails on the first call. Every
    other bulk load in this repository already uses the bound form.

    Reading the file here rather than letting the engine do it has two
    consequences worth stating, because they are the whole reason the path
    quoting this function used to need is gone:

    * **The path is never part of the SQL.** It is a Python string handed to
      ``open``, so no path can inject a statement and no path needs a quoting
      scheme. A path may contain any character the filesystem accepts, which is
      why a file called ``it's data`` or ``we"ird`` is no longer special.
    * **The header is Python's decision.** The engine's own ``COPY`` defaults to
      ``HEADER=false`` and ingests the header row as data, which put an entity
      literally named ``name`` into the store. Reading the row here means the
      flag cannot be forgotten, mis-cased or silently defaulted.

    Every column is a STRING because both tables are all-STRING, and ``COPY``
    maps by position: the file must hold exactly *columns*, in order, and a
    short row is padded rather than shifted.
    """
    if not isinstance(path, str):
        raise TypeError(f"path must be a str, got {type(path).__name__}")
    if not path:
        raise ValueError("path must not be empty")
    if "\x00" in path:
        # A NUL truncates the name in the OS layer, so the caller would be
        # reading a different file than the one it named.
        raise ValueError("path must not contain a NUL byte")
    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if header and rows:
        rows = rows[1:]
    try:
        data = {
            name: [row[i] if i < len(row) else "" for row in rows]
            for i, name in enumerate(columns)
        }
    except IndexError:  # pragma: no cover - rows are padded above
        raise ValueError(f"{path} has no column {i + 1} of {len(columns)}") from None
    return pa.table({name: pa.array(values, type=pa.string()) for name, values in data.items()})


def _rows_of(conn: Any, query: str, params: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Yield rows as dicts without buffering the whole result."""

    """Yield rows as dicts without buffering the whole result."""
    result = conn.execute(query, dict(params) if params else None)
    if isinstance(result, list):
        result = result[-1]
    columns = result.get_column_names()
    try:
        while result.has_next():
            yield dict(zip(columns, result.get_next()))
    finally:
        result.close()


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

_UPSERT_ENTITY = f"""
UNWIND $rows AS row
MERGE (e:{ENTITY_TABLE} {{id: row.id}})
ON MATCH SET e.name = row.name,
              e.category = row.category,
              e.description = row.description,
              e.aliases = row.aliases
ON CREATE SET e.name = row.name,
              e.category = row.category,
              e.description = row.description,
              e.aliases = row.aliases
"""

_UPSERT_RELATION = f"""
UNWIND $rows AS row
MATCH (src:{ENTITY_TABLE} {{id: row.src}})
MATCH (dst:{ENTITY_TABLE} {{id: row.dst}})
MERGE (src)-[r:{RELATION_TABLE} {{action: row.action}}]->(dst)
ON MATCH SET r.context = row.context
ON CREATE SET r.context = row.context
"""


class GraphStore:
    """A LadybugDB database holding one Entity/RELATION graph.

    Owns the database and one connection, and closes both. Re-entrant ingest
    methods are safe to call repeatedly: entity and relationship writes are
    upserts keyed on the values that identify them, so re-ingesting a document
    updates rather than duplicates.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        buffer_pool_mb: int = BUFFER_POOL_MB,
        read_only: bool = False,
        max_num_threads: int = 0,
        compression: bool = True,
    ) -> None:
        if buffer_pool_mb <= 0:
            raise ValueError("buffer_pool_mb must be positive")
        self.path = os.fspath(path)
        self.buffer_pool_mb = int(buffer_pool_mb)
        self.database = lb.Database(
            self.path,
            buffer_pool_size=self.buffer_pool_mb * 1024 * 1024,
            max_num_threads=max_num_threads,
            compression=compression,
            read_only=read_only,
        )
        self.connection = lb.Connection(self.database)
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the connection and database. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        for closable in (self.connection, self.database):
            close = getattr(closable, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - close is best effort
                    pass

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def conn(self) -> Any:
        """The live connection, for the module-level query functions."""
        if self._closed:
            raise GraphStoreError("store is closed")
        return self.connection

    # -- schema ------------------------------------------------------------

    def create_schema(self, *, recreate: bool = False) -> None:
        """Create the Entity and RELATION tables if they are absent.

        Idempotent: both statements use ``IF NOT EXISTS``, so this is safe on an
        existing database and leaves the data alone.
        """
        if self._closed:
            raise GraphStoreError("store is closed")
        if recreate:
            self._execute(f"DROP TABLE IF EXISTS {RELATION_TABLE}")
            self._execute(f"DROP TABLE IF EXISTS {ENTITY_TABLE}")
        self._execute(_NODE_DDL)
        self._execute(_REL_DDL)

    def _execute(self, query: str, params: Mapping[str, Any] | None = None) -> None:
        _execute(self.conn, query, params)

    def has_schema(self) -> bool:
        """True when both tables exist."""
        try:
            rows = _fetch(self.conn, "CALL show_tables() RETURN *")
        except Exception as exc:  # pragma: no cover - catalog failure
            raise GraphStoreError(f"cannot read catalog: {exc}") from exc
        # show_tables() projects an 'id', a 'name' and a 'type'; the table name is
        # the 'name' column, not position 1.
        tables = {str(row.get("name", "")) for row in rows}
        return {ENTITY_TABLE, RELATION_TABLE} <= tables

    def require_schema(self) -> None:
        """Raise unless the schema is present, rather than failing mid-query."""
        if not self.has_schema():
            raise GraphStoreError(
                "schema not initialised; call GraphStore.create_schema() first"
            )

    def index_report(self) -> list[dict[str, Any]]:
        """What the engine actually indexes.

        Included because "Pivot Search Index" is easy to misread as a database
        index. On 0.20.4 this lists only the primary-key hash: there is no
        index on ``name`` or ``aliases`` to lean on.
        """
        return _fetch(self.conn, "CALL show_indexes() RETURN *")

    # -- ingest ------------------------------------------------------------

    def ingest_entities(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        report: IngestReport | None = None,
    ) -> int:
        """Upsert entities. Returns the number of rows written.

        Accepts ``resolve_subgraph`` node dicts directly, or raw extractor
        entities. Missing fields become empty strings rather than failing the
        batch, because a partially-described entity is still worth keeping and
        a rejected batch is not.
        """
        self.require_schema()
        prepared = self._prepare_entities(rows, report)
        if not prepared:
            return 0
        written = 0
        for batch in _batched(prepared, batch_size):
            self._transaction(_UPSERT_ENTITY, {"rows": batch}, "entity upsert")
            written += len(batch)
        if report is not None:
            report.entities += written
        return written

    def _prepare_entities(
        self,
        rows: Iterable[Mapping[str, Any]],
        report: IngestReport | None,
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                if report is not None:
                    report.entities_rejected.append(repr(raw)[:120])
                continue
            entity_id = raw.get("id") or ""
            if not isinstance(entity_id, str):
                entity_id = str(entity_id)
            entity_id = entity_id.strip()
            if not entity_id:
                # An id is the primary key and the join key for every edge; a
                # row without one cannot be addressed later.
                if report is not None:
                    report.entities_rejected.append(
                        f"{raw.get('name', '<unnamed>')!r}: missing id"
                    )
                continue
            aliases = raw.get("aliases")
            if isinstance(aliases, (list, tuple, set)):
                alias_text = _ALIAS_JOIN.join(
                    str(part).strip() for part in aliases if str(part).strip()
                )
            else:
                alias_text = "" if aliases is None else str(aliases).strip()
            prepared.append(
                {
                    "id": entity_id,
                    "name": _text(raw.get("name")),
                    "category": _text(raw.get("category")),
                    "description": _text(raw.get("description")),
                    "aliases": alias_text,
                }
            )
        return prepared

    def ingest_relations(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        on_orphan: str = "skip",
        report: IngestReport | None = None,
    ) -> int:
        """Upsert relationships between entities that already exist.

        ``on_orphan`` is ``"skip"`` (default) or ``"raise"``. Orphans are
        checked before the write rather than being lost to the inner ``MATCH``:
        a relationship whose endpoint is missing is usually an ingest-order bug,
        and a silent skip hides it. Skipped endpoints are collected in
        ``report.missing_endpoints`` either way.

        The ``MERGE`` pattern includes ``action``, so two different actions
        between the same pair of entities are two distinct relationships, and
        re-running with the same action updates the context instead of
        duplicating the edge.
        """
        self.require_schema()
        if on_orphan not in {"skip", "raise"}:
            raise ValueError("on_orphan must be 'skip' or 'raise'")

        prepared: list[dict[str, Any]] = []
        skipped_orphan = skipped_duplicate = skipped_self_loop = 0
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            src = _text(raw.get("source") or raw.get("src") or raw.get("from"))
            dst = _text(raw.get("target") or raw.get("dst") or raw.get("to"))
            action = _text(raw.get("action")).upper()
            context = _text(raw.get("context"))
            if not src or not dst or not action:
                continue
            if src == dst:
                # Resolution can collapse two names the extractor kept distinct,
                # which turns a real edge into a self-loop. entity_resolver
                # already drops these; this is the second line of defence.
                skipped_self_loop += 1
                continue
            prepared.append(
                {"src": src, "dst": dst, "action": action, "context": context}
            )

        # Collapse duplicate (src, dst, action) triples within the batch. MERGE
        # is idempotent but the last row wins, so doing it here makes the
        # winner explicit and the count honest.
        deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in prepared:
            key = (row["src"], row["dst"], row["action"])
            if key in deduped:
                skipped_duplicate += 1
                if len(row["context"]) > len(deduped[key]["context"]):
                    deduped[key] = row
            else:
                deduped[key] = row
        prepared = list(deduped.values())

        existing = self._existing_ids({r["src"] for r in prepared} | {r["dst"] for r in prepared})
        if on_orphan == "raise":
            missing = sorted(
                {r["src"] for r in prepared} | {r["dst"] for r in prepared} - existing
            )
            if missing:
                raise GraphStoreError(
                    f"{len(missing)} relationship endpoint(s) not in {ENTITY_TABLE}: "
                    f"{missing[:10]}"
                )

        writable: list[dict[str, Any]] = []
        for row in prepared:
            if row["src"] in existing and row["dst"] in existing:
                writable.append(row)
            else:
                skipped_orphan += 1
                for endpoint in (row["src"], row["dst"]):
                    if endpoint not in existing:
                        if report is not None and endpoint not in report.missing_endpoints:
                            report.missing_endpoints.append(endpoint)

        written = 0
        for batch in _batched(writable, batch_size):
            self._transaction(_UPSERT_RELATION, {"rows": batch}, "relation upsert")
            written += len(batch)

        if report is not None:
            report.relations += written
            report.relations_skipped_orphan += skipped_orphan
            report.relations_skipped_duplicate += skipped_duplicate
            report.relations_skipped_self_loop += skipped_self_loop
        return written

    def ingest_graph(
        self,
        graph: Mapping[str, Any],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        on_orphan: str = "skip",
    ) -> IngestReport:
        """Ingest a ``resolve_subgraph`` result: nodes first, then edges.

        Ordering is not cosmetic. Relationships are inserted after nodes and
        validated against them, so a graph arrives whole or reports what was
        missing, rather than depending on the order edges happened to appear in
        the extraction chunks.
        """
        self.require_schema()
        report = IngestReport()
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        self.ingest_entities(nodes, batch_size=batch_size, report=report)
        self.ingest_relations(edges, batch_size=batch_size, on_orphan=on_orphan, report=report)
        return report

    def _existing_ids(self, wanted: set[str]) -> set[str]:
        """Which of ``wanted`` already exist. Chunked to bound the UNWIND size."""
        found: set[str] = set()
        for chunk in _batched(sorted(wanted), 500):
            if not _guarded_unwind(chunk):
                continue
            rows = _fetch(
                self.conn,
                f"UNWIND $ids AS wanted MATCH (e:{ENTITY_TABLE} {{id: wanted}}) RETURN e.id AS id",
                {"ids": list(chunk)},
            )
            found.update(str(row["id"]) for row in rows)
        return found

    def _transaction(self, query: str, params: Mapping[str, Any], label: str) -> None:
        """Run one batch inside an explicit transaction.

        On failure the engine has already aborted the transaction and left none
        active, so no ``ROLLBACK`` is issued -- attempting one raises "No active
        transaction" and would mask the real error. The transaction is atomic
        either way: the batch lands whole or not at all.
        """
        if not _guarded_unwind(params.get("rows", [])):  # type: ignore[union-attr]
            return
        self._execute("BEGIN TRANSACTION")
        try:
            self._execute(query, params)
            self._execute("COMMIT")
        except Exception as exc:
            raise GraphStoreError(f"{label} failed and was rolled back: {exc}") from exc

    def copy_from_csv(
        self,
        entity_csv: str,
        relation_csv: str | None = None,
        *,
        header: bool = True,
    ) -> None:
        """Bulk load from CSV files.

        The first-load path. ``COPY`` is not idempotent -- running it twice
        violates the primary key -- so prefer :meth:`ingest_entities` for anything
        that may run twice. It is faster than the batched upsert and skips
        normalisation, so the CSV must already hold exactly the schema's columns
        in order.

        The CSV is read here and handed over as a bound Arrow table, because that
        is the only ``COPY`` form this engine's grammar has; see :func:`_read_csv`
        for what that buys and why the path no longer needs quoting. The header
        row is this function's decision rather than a string literal in the
        statement, because the engine's own default is ``HEADER=false`` and would
        ingest it as data.
        """
        self.require_schema()
        self._execute(
            f"COPY {ENTITY_TABLE} FROM $data",
            {"data": _read_csv(entity_csv, ENTITY_COLUMNS, header)},
        )
        if relation_csv:
            self._execute(
                f"COPY {RELATION_TABLE} FROM $data",
                {"data": _read_csv(relation_csv, RELATION_COLUMNS, header)},
            )

    # -- introspection -----------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Entity and relationship counts."""
        self.require_schema()
        entities = _fetch(
            self.conn, f"MATCH (e:{ENTITY_TABLE}) RETURN count(e) AS n"
        )
        relations = _fetch(
            self.conn, f"MATCH ()-[r:{RELATION_TABLE}]->() RETURN count(r) AS n"
        )
        return {
            "entities": int(entities[0]["n"]) if entities else 0,
            "relations": int(relations[0]["n"]) if relations else 0,
        }

    def disk_bytes(self) -> int:
        """On-disk size across all storage files, in bytes."""
        total = 0
        for row in _fetch(self.conn, "CALL disk_size_info() RETURN *"):
            for value in row.values():
                if isinstance(value, int):
                    total += value
        return total


def open_store(
    path: str | os.PathLike[str],
    *,
    buffer_pool_mb: int = BUFFER_POOL_MB,
    create: bool = True,
    read_only: bool = False,
    max_num_threads: int = 0,
) -> GraphStore:
    """Open (and by default initialise) a store.

    ``buffer_pool_mb`` defaults to 256 MB. It bounds the engine's buffer
    manager; it is not a process memory ceiling, and the ingest batch size and
    query candidate caps account for the rest.
    """
    store = GraphStore(
        path,
        buffer_pool_mb=buffer_pool_mb,
        read_only=read_only,
        max_num_threads=max_num_threads,
    )
    if create and not read_only:
        store.create_schema()
    return store


# --------------------------------------------------------------------------
# Pivot search
# --------------------------------------------------------------------------

def _prefilter_terms(user_query: Any, tokens: Sequence[str]) -> list[str]:
    """Terms to hand Cypher's ``CONTAINS``: each folded token, plus the term as typed.

    Stored text keeps the author's original spelling, but a query is folded and so
    has had its accents stripped. Cypher's ``lower()`` does not decompose accents,
    so ``lower(name) CONTAINS 'cote'`` misses ``Côté`` -- and, worse, the *exact*
    query ``Côté`` misses too, because its folded form is what gets sent. Without
    this, an accented name cannot be found by typing it. This engine has no
    ``unaccent()``, so the answer is to ask with both spellings: the folded form,
    which reaches ASCII text and ids, and the term as typed, which reaches
    accented text.

    The gap that remains is an *unaccented* query against an accented name whose id
    is not itself ASCII-folded. entity_resolver mints ASCII slugs, so its ids
    rescue that case in practice; a ``entity-<sha1>`` id with an accented name does
    not. Generating accented variants to close this was rejected: every extra term
    is another full substring scan, and this module targets a small low-RAM
    machine.
    """
    terms = set(tokens)
    if isinstance(user_query, str):
        for piece in re.split(r"[\W_]+", user_query):
            piece = piece.casefold()
            if piece and (len(piece) >= _MIN_TOKEN or len(piece) <= _MAX_SHORT_QUERY):
                terms.add(piece)
    return sorted(terms)


def _pivot_prefilter(max_candidates: int) -> str:
    """Narrow the table to rows whose name, id or aliases contain a query token.

    ``CONTAINS`` is the only text predicate this engine has -- there is no
    ``CREATE INDEX`` to build and no trigram or full-text extension -- so the
    substring scan is unavoidable and ``max_candidates`` is what keeps it bounded.
    The limit is inlined; see :func:`_sql_int`.
    """
    return (
        f"MATCH (e:{ENTITY_TABLE})\n"
        f"WHERE ANY(t IN $tokens WHERE lower(e.name) CONTAINS t\n"
        f"                          OR lower(e.id) CONTAINS t\n"
        f"                          OR lower(e.aliases) CONTAINS t)\n"
        f"RETURN e.id AS id, e.name AS name, e.aliases AS aliases, e.category AS category\n"
        f"LIMIT {_sql_int(max_candidates, 'max_candidates')}"
    )


def _score_candidate(
    query_folded: str,
    query_tokens: tuple[str, ...],
    entity_id: str,
    name: str,
    aliases: tuple[str, ...],
) -> PivotHit | None:
    """Rank one candidate against the query.

    Three tiers, most trustworthy first, so that a character-level resemblance
    can never outrank a real token match:

    1. Whole-string identity against the id, then the name, then an alias.
    2. Token coverage over name, aliases and id -- the fraction of query tokens
       the entity accounts for, with a prefix tolerance so ``lagrang`` reaches
       ``lagrangian``.
    3. Character similarity, and only when no query token matched anything at
       all. This mirrors entity_resolver's rule that a character ratio is
       trustworthy only where token overlap gives it nothing to contradict. Below
       :data:`_FUZZY_FLOOR` resemblance there is no candidate at all.

    Returns a :class:`PivotHit` with the evidence attached, or ``None`` when the
    candidate matches nothing worth pivoting on.
    """
    surfaces: list[tuple[str, str]] = [("id", entity_id), ("name", name)]
    surfaces.extend(("alias", alias) for alias in aliases)

    folded_id = _fold(entity_id)
    if query_folded and query_folded == folded_id:
        return PivotHit(entity_id, 1.0, "id", entity_id, query_tokens, 1.0)

    for label, surface in surfaces:
        if label == "id":
            continue
        if query_folded and query_folded == _fold(surface):
            score = 0.98 if label == "name" else 0.95
            return PivotHit(entity_id, score, label, surface, query_tokens, 1.0)

    best_coverage = 0.0
    best_token = 0.0
    best_label = "token"
    best_surface = name or entity_id
    best_matched: tuple[str, ...] = ()
    for label, surface in surfaces:
        entity_tokens = frozenset(_tokenize(surface))
        if not entity_tokens:
            continue
        per_token = [(qt, _token_affinity(qt, entity_tokens)) for qt in query_tokens]
        coverage = sum(score for _, score in per_token) / len(query_tokens)
        strongest = max((score for _, score in per_token), default=0.0)
        if coverage > best_coverage:
            best_coverage = coverage
            best_token = strongest
            best_label = label
            best_surface = surface
            best_matched = tuple(qt for qt, score in per_token if score > 0)
    if best_coverage > 0.0:
        return PivotHit(
            entity_id,
            0.90 * best_coverage,
            f"{best_label}:token",
            best_surface,
            best_matched,
            best_token,
        )

    best_fuzzy = 0.0
    best_surface = name or entity_id
    for _label, surface in surfaces:
        score = _ratio(query_folded, _fold(surface))
        if score > best_fuzzy:
            best_fuzzy = score
            best_surface = surface
    if best_fuzzy < _FUZZY_FLOOR:
        # No floor here is a precision leak, not a lenient default. The token
        # tiers already handle every match that is a prefix or a substantial
        # substring, so anything reaching this point got here by being a *short*
        # substring of a long name: "act" in "transaction" is 0.43, "ab" in
        # "abcabcabcabc" is 0.29. Those are coincidences, not identity. It matters
        # more than a bad ranking, because a spurious pivot is precisely what
        # 1- and 2-hop expansion fans out from, so one weak hit drags in a whole
        # neighbourhood. Note the prefilter admits a misspelling that is a
        # substring ("lagranger" is not one) and then refuses to score it, so
        # typos are the prefilter's blind spot, not this tier's.
        return None
    # Capped below the token tier so disjoint-token resemblance ranks last.
    return PivotHit(
        entity_id, min(0.85, 0.85 * best_fuzzy), "fuzzy", best_surface, (), best_fuzzy
    )


def _retained(hit: PivotHit, min_score: float) -> bool:
    """Whether a scored candidate survives :func:`find_pivot_nodes`.

    Coverage alone is the wrong floor for a question. ``score`` is
    ``0.90 x (fraction of the query's tokens the entity accounts for)``, so the
    words of a question that name no entity -- *relationship*, *explain*,
    *principle* -- divide the score of an entity the query names exactly. That is
    fine for **ranking** candidates against each other, and wrong for **keeping**
    them: measured on a three-entity graph, "what is the relationship between the
    Lagrangian and Hamiltonian mechanics" scored a perfect 1.0 pivot at 0.180 and
    returned nothing at the default floor, and "explain the variational principle
    behind the Lagrangian and the Hamiltonian equations" did the same. The module's
    whole purpose is answering questions, so a question that names an entity must
    not silently fail to find it.

    So a candidate is kept when it clears ``min_score`` *or* when its single best
    token reaches :data:`_MIN_PIVOT_TOKEN` -- i.e. the entity is named outright.
    The substring tier (0.85) and fuzzy do not reach it, so ``min_score`` still
    governs weak matches and remains meaningful.
    """
    return hit.score >= min_score or hit.best_token >= _MIN_PIVOT_TOKEN


def find_pivot_nodes_detailed(
    conn: Any,
    user_query: str,
    *,
    limit: int = DEFAULT_PIVOT_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[PivotHit]:
    """Score pivot candidates and return them with their evidence.

    :func:`find_pivot_nodes` is the id-only view of this. The evidence is worth
    keeping when a ranking looks wrong: ``token:name`` says the name's tokens
    covered the query, ``fuzzy`` says nothing matched and only characters did,
    and those two want very different fixes.

    ``max_candidates`` bounds how many rows the prefilter returns. When it
    truncates, *which* rows survive is not determined -- the prefilter has no
    ``ORDER BY``, because ordering on a substring predicate is not something this
    engine can do cheaply. Set it well above the expected match count and treat it
    as a memory guard, not as a relevance cut-off. A query that matches more
    entities than the cap returns an arbitrary subset of them, which is why the
    default is 5000 rather than something tighter.
    """
    tokens = _query_tokens(user_query)
    if not tokens:
        return []
    if limit <= 0:
        return []

    candidates = list(
        _rows_of(
            conn,
            _pivot_prefilter(max(1, int(max_candidates))),
            {"tokens": _prefilter_terms(user_query, tokens)},
        )
    )
    if not candidates:
        return []

    query_folded = _fold(user_query)
    hits: list[PivotHit] = []
    for row in candidates:
        entity_id = _text(row.get("id"))
        if not entity_id:
            continue
        hit = _score_candidate(
            query_folded,
            tokens,
            entity_id,
            _text(row.get("name")),
            _split_aliases(row.get("aliases")),
        )
        if hit is not None and _retained(hit, min_score):
            hits.append(hit)

    # Deterministic order: score descending, then shortest name, then id, so the
    # same query returns the same list across runs and processes.
    hits.sort(key=lambda h: (-h.score, len(_text(h.surface)), h.entity_id))
    return hits[:limit]


def find_pivot_nodes(
    conn: Any,
    user_query: str,
    *,
    limit: int = DEFAULT_PIVOT_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[str]:
    """Return canonical ids of the entities a query pivots on.

    Tokenises the query, narrows candidates with Cypher ``CONTAINS`` across
    ``Entity.name``, ``Entity.id`` and ``Entity.aliases``, then ranks them with
    token coverage and fuzzy matching. Returns ids only, best first.

    An empty or stopword-only query returns ``[]`` without touching the
    database.
    """
    return [
        hit.entity_id
        for hit in find_pivot_nodes_detailed(
            conn,
            user_query,
            limit=limit,
            min_score=min_score,
            max_candidates=max_candidates,
        )
    ]


# --------------------------------------------------------------------------
# Relevance expansion
# --------------------------------------------------------------------------


def _hop_query(hops: int, directed: bool, limit: int) -> str:
    """Build the N-hop traversal for ``hops``.

    Generated rather than hand-written per depth. Two hand-written variants is
    where the bug lives: an earlier version allowed ``max_hops=3`` while only
    ever issuing 1- and 2-hop queries, so a request for 3 hops silently returned
    2-hop results. A caller cannot tell that from the answer.

    ``DISTINCT`` matters here. In an undirected traversal a cycle or a diamond
    yields the same path many times, and at 2 hops the fan-out is already
    quadratic in the neighbourhood.

    The ``LIMIT`` is inlined rather than bound -- see :func:`_sql_int`.
    """
    arrow = "->" if directed else "-"
    pattern = f"(pivot:{ENTITY_TABLE})"
    projection = ["pivot.id AS pivot"]
    for step in range(1, hops + 1):
        last = step == hops
        alias = "node" if last else f"v{step}"
        pattern += f"-[r{step}:{RELATION_TABLE}]{arrow}({alias}:{ENTITY_TABLE})"
        if not last:
            projection.append(f"{alias}.id AS v{step}")
    projection.append("node.id AS node")
    for step in range(1, hops + 1):
        projection.append(f"r{step}.action AS action{step}")
        projection.append(f"r{step}.context AS context{step}")
    return (
        f"MATCH {pattern}\n"
        f"WHERE pivot.id IN $pivots AND pivot.id <> node.id\n"
        f"RETURN DISTINCT {', '.join(projection)}\n"
        f"LIMIT {_sql_int(limit, 'limit')}"
    )


def expand_paths(
    conn: Any,
    pivot_ids: Sequence[str],
    max_hops: int = 2,
    *,
    directed: bool = False,
    limit_per_hop: int = DEFAULT_LIMIT_PER_HOP,
) -> list[RelevancePath]:
    """Traverse outward from the pivots and return the paths with their evidence.

    Undirected by default, because relevance is not directional: the node two
    steps upstream of a pivot is exactly as relevant to explaining it as the one
    two steps downstream. Pass ``directed=True`` to follow ``source -> target``
    only.

    Each returned path carries the ``action`` and ``context`` of every hop, which
    is the part that makes it usable as grounding: the answer needs the claim
    ("ELE minimises the Lagrangian"), not just the two node names.

    Bounded by ``limit_per_hop`` at each depth, because expansion fans out
    combinatorially in the neighbourhood size. Reaching the cap truncates the
    traversal, and truncation is observable -- the number of returned paths drops
    rather than the caller believing the neighbourhood is exhausted.
    """
    if max_hops < 1:
        raise ValueError("max_hops must be at least 1")
    if max_hops > MAX_HOPS:
        raise ValueError(f"max_hops must not exceed {MAX_HOPS}")
    pivots = _guarded_unwind(sorted({_text(p) for p in pivot_ids if _text(p)}))
    if pivots is None:
        return []

    cap = max(1, int(limit_per_hop))
    paths: list[RelevancePath] = []

    for hops in range(1, max_hops + 1):
        for row in _rows_of(conn, _hop_query(hops, directed, cap), {"pivots": pivots}):
            actions = tuple(_text(row.get(f"action{step}")) for step in range(1, hops + 1))
            contexts = tuple(_text(row.get(f"context{step}")) for step in range(1, hops + 1))
            via = "".join(_text(row.get(f"v{step}")) + " -> " for step in range(1, hops))
            paths.append(
                RelevancePath(
                    pivot=_text(row.get("pivot")),
                    node=_text(row.get("node")),
                    hops=hops,
                    actions=actions,
                    contexts=contexts,
                    via=via.rstrip(" -> "),
                )
            )

    return paths


def expand_relevance(
    conn: Any,
    pivot_ids: Sequence[str],
    max_hops: int = 2,
    *,
    directed: bool = False,
    limit_per_hop: int = DEFAULT_LIMIT_PER_HOP,
    include_pivots: bool = False,
) -> list[str]:
    """Return the canonical ids relevant to a pivot set, nearest first.

    Walks 1 hop and then 2 hops out from every pivot and returns the reached
    node ids, ordered by hop distance and then by id. Pivots are excluded by
    default, since they are already known; pass ``include_pivots=True`` to get
    one self-contained context set with the pivots first.

    A node that is itself a pivot never appears in the expansion, so overlapping
    pivot sets do not report each other back as findings.

    :func:`expand_paths` returns the same traversal with the ``action`` and
    ``context`` of each hop attached, which is what to use when building
    grounded context for a reader.
    """
    if max_hops < 1:
        raise ValueError("max_hops must be at least 1")
    if max_hops > MAX_HOPS:
        raise ValueError(f"max_hops must not exceed {MAX_HOPS}")
    pivots = {p for p in (_text(pid) for pid in pivot_ids) if p}
    if not pivots:
        return []

    paths = expand_paths(
        conn,
        sorted(pivots),
        max_hops,
        directed=directed,
        limit_per_hop=limit_per_hop,
    )

    # A node reached at 1 hop stays at 1 hop: a 2-hop path to it is not new
    # information, and listing it twice would inflate its apparent relevance.
    nearest: dict[str, int] = {}
    for path in paths:
        if path.node in pivots:
            continue
        current = nearest.get(path.node)
        if current is None or path.hops < current:
            nearest[path.node] = path.hops

    ordered = sorted(nearest.items(), key=lambda item: (item[1], item[0]))
    reached = [node for node, _ in ordered]
    if include_pivots:
        return sorted(pivots) + reached
    return reached


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def store_stats(store: GraphStore) -> dict[str, Any]:
    """Counts, on-disk size, and the indexes that actually exist."""
    stats: dict[str, Any] = {
        "path": store.path,
        "buffer_pool_mb": store.buffer_pool_mb,
        "counts": store.counts(),
        "disk_bytes": store.disk_bytes(),
    }
    try:
        stats["indexes"] = store.index_report()
    except Exception:  # pragma: no cover - catalog dependent
        stats["indexes"] = []
    return stats


# --------------------------------------------------------------------------
# Self-test and CLI
# --------------------------------------------------------------------------

_SAMPLE_GRAPH: dict[str, Any] = {
    "nodes": [
        {
            "id": "euler-lagrange-equation",
            "name": "Euler-Lagrange equation",
            "category": "EQUATION",
            "description": "Stationarity condition for an action.",
            "aliases": ["ELE", "Euler Lagrange equations"],
        },
        {
            "id": "lagrangian",
            "name": "Lagrangian",
            "category": "QUANTITY",
            "description": "Kinetic energy minus potential energy.",
            "aliases": ["L", "kinetic minus potential"],
        },
        {
            "id": "hamilton-equations",
            "name": "Hamilton's equations of motion",
            "category": "EQUATION",
            "description": "First-order equations of motion.",
            "aliases": ["Hamilton equations"],
        },
        {
            "id": "action",
            "name": "Action",
            "category": "QUANTITY",
            "description": "Time integral of the Lagrangian.",
            "aliases": [],
        },
    ],
    "edges": [
        {
            "source": "action",
            "target": "lagrangian",
            "action": "INTEGRATES",
            "context": "The action is the integral of the Lagrangian over time.",
        },
        {
            "source": "euler-lagrange-equation",
            "target": "lagrangian",
            "action": "MINIMIZES",
            "context": "Its extremum is the Euler-Lagrange equation.",
        },
        {
            "source": "euler-lagrange-equation",
            "target": "hamilton-equations",
            "action": "RECOVERS",
            "context": "Setting the variation to zero recovers Hamilton's equations.",
        },
    ],
}


def _selftest(verbose: bool = True) -> int:
    """Build a throwaway database and exercise ingest, search and expansion."""
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            if verbose:
                print(f"  ok   {label}")
        else:
            failures.append(label)
            print(f"  FAIL {label} {detail}")

    def top(result: list, index: int = 0) -> Any:
        """Element at ``index``, or None.

        A selftest exists to report the first thing that broke. Indexing a
        result that came back short raises IndexError and replaces every
        remaining check with a traceback, so the one broken assumption hides
        all the others behind it.
        """
        return result[index] if len(result) > index else None

    def before(result: list, earlier: str, later: str) -> bool:
        """True when ``earlier`` precedes ``later`` and both are present."""
        return (
            earlier in result
            and later in result
            and result.index(earlier) < result.index(later)
        )

    with tempfile.TemporaryDirectory(prefix="graph-store-selftest-") as tmp:
        path = os.path.join(tmp, "selftest.lbug")
        with open_store(path) as store:
            check("schema created", store.has_schema())
            report = store.ingest_graph(_SAMPLE_GRAPH)
            counts = store.counts()
            check("entities ingested", counts["entities"] == 4, str(counts))
            check("relations ingested", counts["relations"] == 3, str(counts))
            check("no endpoints missing", not report.missing_endpoints, str(report.missing_endpoints))

            # Idempotency: the same graph again must not grow the database.
            store.ingest_graph(_SAMPLE_GRAPH)
            check("re-ingest is idempotent", store.counts() == counts, str(store.counts()))

            # Self-loop and orphan refusal.
            second = store.ingest_graph(
                {
                    "nodes": [],
                    "edges": [
                        {"source": "action", "target": "action", "action": "LOOPS", "context": ""},
                        {"source": "action", "target": "ghost", "action": "X", "context": ""},
                    ],
                }
            )
            check("self-loop refused", second.relations_skipped_self_loop == 1, str(second.as_dict()))
            check("orphan refused", second.relations_skipped_orphan == 1, str(second.as_dict()))
            check("orphan reported", second.missing_endpoints == ["ghost"], str(second.missing_endpoints))

            # Pivot search.
            pivots = find_pivot_nodes(store.conn, "Euler-Lagrange equation")
            check("pivot by name", "euler-lagrange-equation" in pivots, str(pivots))
            check("pivot by id slug", "euler-lagrange-equation" in find_pivot_nodes(store.conn, "euler lagrange"), "")
            check("pivot by alias", "euler-lagrange-equation" in find_pivot_nodes(store.conn, "what is ELE"), "")
            check("pivot by case-insensitive substring", "lagrangian" in find_pivot_nodes(store.conn, "LAGRANG"), "")
            check("stopword-only query is empty", find_pivot_nodes(store.conn, "what is the of") == [], "")
            check("empty query is empty", find_pivot_nodes(store.conn, "") == [], "")
            check("no-match query is empty", find_pivot_nodes(store.conn, "zzzzqqq") == [], "")

            hits = find_pivot_nodes_detailed(store.conn, "kinetic minus potential")
            check("alias detail present", any(h.evidence == "alias" for h in hits), str([h.as_dict() for h in hits]))
            check(
                "id exact outranks token",
                top(find_pivot_nodes(store.conn, "lagrangian")) == "lagrangian",
                str(find_pivot_nodes(store.conn, "lagrangian")),
            )

            # Expansion.
            reached = expand_relevance(store.conn, ["action"], max_hops=2)
            check("1-hop from action", "lagrangian" in reached, str(reached))
            check("2-hop from action", "euler-lagrange-equation" in reached, str(reached))
            check(
                "nearest hop ordered first",
                before(reached, "lagrangian", "euler-lagrange-equation"),
                str(reached),
            )
            check(
                "3 hops reaches hamilton",
                "hamilton-equations" in expand_relevance(store.conn, ["action"], max_hops=3),
                str(expand_relevance(store.conn, ["action"], max_hops=3)),
            )
            check(
                "pivots excluded by default",
                "action" not in expand_relevance(store.conn, ["action"], max_hops=2),
                "",
            )
            check(
                "include_pivots puts them first",
                top(expand_relevance(store.conn, ["action"], max_hops=2, include_pivots=True)) == "action",
                str(expand_relevance(store.conn, ["action"], max_hops=2, include_pivots=True)),
            )
            check("empty pivots is empty", expand_relevance(store.conn, [], max_hops=2) == [], "")
            check("max_hops=1 stops at 1", "euler-lagrange-equation" not in expand_relevance(store.conn, ["action"], 1), "")

            for bad in (0, -1, MAX_HOPS + 1):
                try:
                    expand_relevance(store.conn, ["action"], bad)
                except ValueError:
                    pass
                else:
                    check(f"max_hops={bad} rejected", False, "no ValueError")

            # Undirected reachability is the default.
            check(
                "undirected finds the source",
                "euler-lagrange-equation" in expand_relevance(store.conn, ["lagrangian"], max_hops=1),
                str(expand_relevance(store.conn, ["lagrangian"], max_hops=1)),
            )
            check(
                "directed respects direction",
                "euler-lagrange-equation" not in expand_relevance(
                    store.conn, ["lagrangian"], max_hops=1, directed=True
                ),
                "",
            )

            # Paths carry action and context.
            paths = expand_paths(store.conn, ["action"], max_hops=1)
            check("path has action", all(p.actions and p.actions[0] for p in paths), str([p.as_dict() for p in paths]))
            check("path has context", all(p.contexts and p.contexts[0] for p in paths), str([p.as_dict() for p in paths]))
            two_hop = [p for p in expand_paths(store.conn, ["action"], max_hops=2) if p.hops == 2]
            check("2-hop path has two actions", all(len(p.actions) == 2 for p in two_hop), str([p.as_dict() for p in two_hop]))
            check("2-hop path records the middle node", all(p.via for p in two_hop), "")

            # End to end, as the pipeline would use it.
            pivots = find_pivot_nodes(store.conn, "what minimises the lagrangian?")
            context_ids = expand_relevance(store.conn, pivots, max_hops=2, include_pivots=True)
            check("end-to-end context is non-empty", bool(context_ids), str(context_ids))
            check(
                "end-to-end context is in the database",
                all(i in {n["id"] for n in _SAMPLE_GRAPH["nodes"]} for i in context_ids),
                str(context_ids),
            )

            check("indexes are primary-key only", len(store.index_report()) >= 1, str(store.index_report()))
            stats = store_stats(store)
            check("stats has counts", stats["counts"]["entities"] == 4, str(stats))
            check("stats has disk size", stats["disk_bytes"] > 0, str(stats["disk_bytes"]))

    if failures:
        print(f"\n{len(failures)} check(s) failed: {failures}")
        return 1
    print("\nall checks passed")
    return 0


def _load_graph(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise GraphStoreError(f"{path}: expected a JSON object with 'nodes' and 'edges'")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="graph_store",
        description="LadybugDB knowledge graph store with a pivot search index.",
    )
    sub = parser.add_subparsers(dest="command")

    selftest = sub.add_parser("selftest", help="run the built-in checks in a temp database")
    selftest.set_defaults(func=lambda a: _selftest())

    ingest = sub.add_parser("ingest", help="ingest a resolve_subgraph JSON file")
    ingest.add_argument("database")
    ingest.add_argument("graph", help="JSON with 'nodes' and 'edges'")
    ingest.add_argument("--buffer-pool-mb", type=int, default=BUFFER_POOL_MB)
    ingest.add_argument(
        "--on-orphan", choices=("skip", "raise"), default="skip",
        help="what to do when a relationship endpoint is not in the database",
    )
    ingest.set_defaults(func=_cmd_ingest)

    search = sub.add_parser("search", help="find pivot nodes for a query")
    search.add_argument("database")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=DEFAULT_PIVOT_LIMIT)
    search.add_argument("--explain", action="store_true", help="show scores and evidence")
    search.set_defaults(func=_cmd_search)

    expand = sub.add_parser("expand", help="expand relevance around pivot ids")
    expand.add_argument("database")
    expand.add_argument("pivot", nargs="+")
    expand.add_argument("--max-hops", type=int, default=2)
    expand.add_argument("--directed", action="store_true")
    expand.add_argument("--paths", action="store_true", help="show the paths with actions and contexts")
    expand.set_defaults(func=_cmd_expand)

    stats = sub.add_parser("stats", help="show counts, size and indexes")
    stats.add_argument("database")
    stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except GraphStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_ingest(args: argparse.Namespace) -> int:
    with open_store(args.database, buffer_pool_mb=args.buffer_pool_mb) as store:
        report = store.ingest_graph(_load_graph(args.graph), on_orphan=args.on_orphan)
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    with open_store(args.database, create=False) as store:
        store.require_schema()
        if args.explain:
            hits = find_pivot_nodes_detailed(store.conn, args.query, limit=args.limit)
            print(json.dumps([h.as_dict() for h in hits], indent=2))
        else:
            print(json.dumps(find_pivot_nodes(store.conn, args.query, limit=args.limit), indent=2))
    return 0


def _cmd_expand(args: argparse.Namespace) -> int:
    with open_store(args.database, create=False) as store:
        store.require_schema()
        if args.paths:
            paths = expand_paths(
                store.conn, args.pivot, args.max_hops, directed=args.directed
            )
            print(json.dumps([p.as_dict() for p in paths], indent=2))
        else:
            print(
                json.dumps(
                    expand_relevance(
                        store.conn, args.pivot, args.max_hops, directed=args.directed
                    ),
                    indent=2,
                )
            )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with open_store(args.database, create=False) as store:
        store.require_schema()
        print(json.dumps(store_stats(store), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
