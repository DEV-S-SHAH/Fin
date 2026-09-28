"""Stage 4: bulk load from the Parquet spill into LadybugDB.

Reads the Arrow tables written by :mod:`sandbox_engine.buffer` and writes them
with ``COPY <table> FROM $arrow``. One statement per table, never a per-row
loop.

    from sandbox_engine.loader import BulkLoader

    with BulkLoader("sandbox_engine/_run/sandbox.lbug") as loader:
        report = loader.load(StageBuffer(staging))

The pre-flight key read is the whole reason this module is careful
-------------------------------------------------------------------

``COPY <table> FROM $arrow`` against a primary key that **already exists
hangs forever**. Not raises -- *hangs*, inside the storage engine, with no
exception, no signal handler, and no way to interrupt it. The load cannot be
wrapped in a timeout because the process is not blocked in Python; it is
blocked in C++. So :meth:`BulkLoader._existing` reads the stored keys first and
only genuinely new rows are copied. **This check is not optional.** A loader
that skips it is a loader that hangs on the second run.

Arcs get the same treatment for a different reason. Both kinds of arc can
repeat across filings, and a duplicate arc is not merely untidy: ``MATCH`` fans
out once per duplicate, so a second run would silently double every row a join
returns. Every candidate arc is therefore checked against the stored set.

Two paths, one threshold
-------------------------

At or above :data:`~sandbox_engine.config.COPY_THRESHOLD` rows, the loader
uses ``COPY`` -- one statement, one Arrow payload. Below it, a parameterised
``UNWIND ... CREATE`` is genuinely cheaper, because ``COPY`` pays a fixed setup
cost per statement that a 3-row ``Company`` table does not earn back. A
three-filing run therefore exercises both paths rather than only the one that
scales, which is the point of a sandbox.

The connection is opened and closed per run
-------------------------------------------

A connection that has committed a sizeable write can hang on its next
parameterised read. Opening the database once per run and closing it before
returning keeps the write-then-read sequence inside one short-lived handle.
Relatedly: open a database path with exactly **one** handle at a time. The
engine does not reject a second handle -- it opens, reads the committed state,
and then silently diverges, with writes through one invisible to the other.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa

import ladybug as lb

from .buffer import (
    _DATE_COLUMNS,
    _DOUBLE_PROPS,
    NODE_TABLES,
    PRIMARY_KEYS,
    REL_TABLES,
    StageBuffer,
)
from .config import BUFFER_POOL_BYTES, COPY_THRESHOLD
from .ddl import ensure_schema, parse_date

__all__ = ["BulkLoader", "LoadReport", "WalRecoveryError"]

log = logging.getLogger("sandbox_engine.loader")


class WalRecoveryError(RuntimeError):
    """The database has a stale or corrupt write-ahead log.

    Never repaired automatically. The log may hold the only copy of a committed
    transaction, so deleting it to make the error go away can silently discard
    data -- and the error is the last signal that the previous run was
    interrupted.
    """


#: Substrings that identify a write-ahead-log failure. The engine words this
#: several ways depending on how the log is broken: a checksum failure says
#: "the WAL file is corrupted" with no ``.wal`` in the text, while an
#: interrupted replay names the path. Matching on ``.wal`` alone misses the
#: former, and the run then dies with an opaque storage exception instead of
#: recovery instructions.
_WAL_MARKERS = (".wal", "wal file", "write-ahead", "write ahead", "checksum")


def _looks_like_wal(exc: BaseException) -> bool:
    """Whether *exc* is a write-ahead-log failure rather than a real fault."""
    text = str(exc).lower()
    return any(marker in text for marker in _WAL_MARKERS)


@dataclass
class LoadReport:
    """What the load wrote, for the run report and benchmark 1."""

    inserted: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    methods: dict[str, str] = field(default_factory=dict)
    dropped_dangling: int = 0
    schema_rebuilt: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": dict(sorted(self.inserted.items())),
            "skipped_duplicates": dict(sorted(self.skipped.items())),
            "methods": dict(sorted(self.methods.items())),
            "dropped_dangling_arcs": self.dropped_dangling,
            "schema_rebuilt": list(self.schema_rebuilt),
            "seconds": round(self.seconds, 3),
        }


class BulkLoader:
    """Opens the database, ensures the schema, and bulk-loads the spill."""

    #: Keys read per query. LadybugDB binds a parameter list into a scan, so an
    #: unbounded ``list_contains`` over 800 keys is a single wide predicate; the
    #: window keeps the statement text and the binder's working set bounded.
    KEY_WINDOW = 900

    def __init__(
        self,
        path: str | Path,
        copy_threshold: int = COPY_THRESHOLD,
        buffer_pool_bytes: int = BUFFER_POOL_BYTES,
    ) -> None:
        self.path = Path(path)
        self.copy_threshold = max(1, copy_threshold)
        self.buffer_pool_bytes = buffer_pool_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.report = LoadReport()
        self._schema: dict[str, Any] = {}
        self._open()

    # -- lifecycle ---------------------------------------------------------

    def _open(self) -> None:
        try:
            self.database = lb.Database(str(self.path))
        except RuntimeError as exc:
            if _looks_like_wal(exc):
                raise WalRecoveryError(
                    f"{self.path} has a stale or corrupt write-ahead log from a run "
                    f"that did not shut down cleanly ({self.path}.wal). Replay it "
                    f"by opening the database with a matching engine version, or "
                    f"delete the .wal if that run's work is expendable. "
                    f"Original error: {exc}"
                ) from exc
            raise
        self.connection = lb.Connection(self.database)

    def close(self) -> None:
        for closable in (self.connection, self.database):
            close = getattr(closable, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best effort on teardown
                    pass

    def __enter__(self) -> "BulkLoader":
        self._schema = ensure_schema(self.connection)
        self.report.schema_rebuilt = list(self._schema.get("rebuilt", []))
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers -----------------------------------------------------------

    def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        return [list(row) for row in self.connection.execute(query, params or {}).get_all()]

    def _existing(self, table: str, key: str, keys: Sequence[str]) -> set[str]:
        """Keys already present in *table*.

        The hang-avoidance check. See the module docstring: skipping it makes a
        second run deadlock inside the engine rather than fail.
        """
        if not keys:
            return set()
        present: set[str] = set()
        for start in range(0, len(keys), self.KEY_WINDOW):
            window = [str(key) for key in keys[start : start + self.KEY_WINDOW]]
            rows = self._rows(
                f"MATCH (n:{table}) WHERE list_contains($keys, n.{key}) RETURN n.{key}",
                {"keys": window},
            )
            present.update(str(row[0]) for row in rows)
        return present

    @staticmethod
    def _pythonise(table: pa.Table) -> list[dict[str, Any]]:
        """Arrow rows as Python dicts with dates and floats unboxed.

        ``to_pylist`` already produces native Python, but it hands back
        ``datetime.date`` for ``date32``; the insert path wants the same value
        ``COPY`` would take, which is what the DDL declares.
        """
        return table.to_pylist()

    # -- node insert -------------------------------------------------------

    def _insert_nodes(self, table: str, arrow: pa.Table) -> int:
        """Insert the rows of *table* that are not already stored."""
        if arrow.num_rows == 0:
            self.report.inserted[table] = 0
            return 0
        columns = NODE_TABLES[table]
        key = PRIMARY_KEYS[table]
        rows = self._pythonise(arrow)
        present = self._existing(table, key, [str(row[key]) for row in rows])

        fresh: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            value = str(row[key])
            if value in present or value in seen:
                continue
            seen.add(value)
            fresh.append(
                {
                    # DATE columns go through parse_date so a stored string is
                    # coerced to a real date; INT64 and STRING are already the
                    # right type coming out of Arrow.
                    name: (
                        parse_date(row[name]) if name in _DATE_COLUMNS else row.get(name)
                    )
                    for name in columns
                }
            )
        self.report.skipped[table] = arrow.num_rows - len(fresh)
        if not fresh:
            self.report.inserted[table] = 0
            self.report.methods[table] = "skipped"
            return 0

        if len(fresh) >= self.copy_threshold:
            # COPY maps by position, so the Arrow table must be built in
            # NODE_TABLES[table] order. It already is, because the buffer stage
            # used the same order to build the Parquet schema.
            self.connection.execute(f"COPY {table} FROM $data", {"data": arrow_slice(arrow, fresh)})
            self.report.methods[table] = "copy"
        else:
            assignments = ", ".join(f"{name}: r.{name}" for name in columns)
            self.connection.execute(
                f"UNWIND $rows AS r CREATE (:{table} {{{assignments}}})", {"rows": fresh}
            )
            self.report.methods[table] = "unwind"
        self.report.inserted[table] = len(fresh)
        return len(fresh)

    # -- edge insert -------------------------------------------------------

    def _insert_edges(self, rel: str, arrow: pa.Table) -> int:
        """Insert arcs of *rel* whose endpoints exist and which are not stored."""
        if arrow.num_rows == 0:
            self.report.inserted[rel] = 0
            return 0
        source, target, props = REL_TABLES[rel]
        source_key, target_key = PRIMARY_KEYS[source], PRIMARY_KEYS[target]
        rows = self._pythonise(arrow)

        have_source = self._existing(source, source_key, sorted({r["from"] for r in rows}))
        have_target = self._existing(target, target_key, sorted({r["to"] for r in rows}))
        usable = [r for r in rows if r["from"] in have_source and r["to"] in have_target]
        dropped = len(rows) - len(usable)
        self.report.dropped_dangling += dropped
        if dropped:
            # A dangling arc means the buffer stage produced an edge whose
            # endpoint node was never queued. Silently dropping is correct --
            # the engine has no representation for it -- but it is a parser bug
            # and is reported rather than absorbed.
            log.warning(
                "dropped %d dangling %s arc(s): endpoint node absent from the "
                "staged nodes", dropped, rel,
            )
        if not usable:
            self.report.inserted[rel] = 0
            self.report.methods[rel] = "skipped"
            return 0

        usable = self._new_arcs(rel, usable, props)
        self.report.skipped[rel] = len(rows) - len(usable)
        if not usable:
            self.report.inserted[rel] = 0
            self.report.methods[rel] = "skipped"
            return 0

        if len(usable) >= self.copy_threshold:
            data: dict[str, Any] = {
                "from": [r["from"] for r in usable],
                "to": [r["to"] for r in usable],
            }
            for name in props:
                data[name] = [r.get(name) for r in usable]
            payload = pa.table(
                {
                    name: pa.array(
                        values,
                        type=pa.float64() if name in _DOUBLE_PROPS else pa.string(),
                    )
                    for name, values in data.items()
                }
            )
            self.connection.execute(f"COPY {rel} FROM $data", {"data": payload})
            self.report.methods[rel] = "copy"
        else:
            body = "{" + ", ".join(f"{name}: r.{name}" for name in props) + "}" if props else ""
            payload_rows = [
                {"fk": r["from"], "tk": r["to"], **{p: r.get(p) for p in props}}
                for r in usable
            ]
            self.connection.execute(
                f"UNWIND $rows AS r "
                f"MATCH (a:{source} {{{source_key}: r.fk}}), "
                f"(b:{target} {{{target_key}: r.tk}}) "
                f"CREATE (a)-[:{rel}{body}]->(b)",
                {"rows": payload_rows},
            )
            self.report.methods[rel] = "unwind"
        self.report.inserted[rel] = len(usable)
        return len(usable)

    def _new_arcs(
        self, rel: str, rows: Sequence[dict[str, Any]], props: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Filter *rows* down to arcs not already stored.

        An arc is identified by its endpoints plus its property values, because
        ``REPORTS_METRIC`` legitimately holds several arcs between one filing and
        one metric when a later filing restates it. Arcs whose endpoints *and*
        properties are unchanged are the only ones skipped.

        The query projects the endpoint key columns explicitly. Returning the
        bound nodes and reading ``a.<prop>`` off the struct is a binder error in
        0.20.4 -- ``Cannot find property value for a`` -- because a returned
        node is a struct, not a labelled pattern, and its properties are not
        resolvable by name.
        """
        if not rows:
            return []
        source, target, _ = REL_TABLES[rel]
        source_key, target_key = PRIMARY_KEYS[source], PRIMARY_KEYS[target]
        projection = [f"a.{source_key}", f"b.{target_key}"]
        projection.extend(f"r.{name}" for name in props)
        stored: set[tuple[str, ...]] = set()
        for row in self._rows(
            f"MATCH (a:{source})-[r:{rel}]->(b:{target}) "
            f"RETURN {', '.join(projection)}"
        ):
            stored.add(tuple(_scalar(value) for value in row))

        fresh: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            signature = tuple(
                [str(row["from"]), str(row["to"])]
                + [_scalar(row.get(name)) for name in props]
            )
            if signature in stored or signature in seen:
                continue
            seen.add(signature)
            fresh.append(row)
        return fresh

    # -- orchestration -----------------------------------------------------

    def load(self, buffer: StageBuffer) -> LoadReport:
        """Load every staged node table, then every staged rel table.

        Nodes first, always: an arc whose endpoint has not been created is
        dropped, and the endpoint-existence pre-flight runs against stored
        state. Loading rels before nodes would silently discard them.
        """
        started = time.perf_counter()
        for table in NODE_TABLES:
            self._insert_nodes(table, buffer.load_table(table))
        for rel in REL_TABLES:
            self._insert_edges(rel, buffer.load_table(rel))
        self.report.seconds = time.perf_counter() - started
        return self.report

    def counts(self) -> dict[str, int]:
        """Stored row count per table, read back from the engine.

        This is the number benchmark 1 asserts against, so it must come from
        ``count()`` in the engine rather than from anything this module counted
        on the way in.
        """
        shape: dict[str, int] = {}
        for table in NODE_TABLES:
            rows = self._rows(f"MATCH (n:{table}) RETURN count(n)")
            shape[table] = int(rows[0][0]) if rows else 0
        for rel in REL_TABLES:
            rows = self._rows(f"MATCH ()-[e:{rel}]->() RETURN count(e)")
            shape[rel] = int(rows[0][0]) if rows else 0
        return shape


def arrow_slice(arrow: pa.Table, rows: Sequence[dict[str, Any]]) -> pa.Table:
    """Rebuild a typed ``pa.Table`` from *rows* using *arrow*'s schema.

    Needed because the ``fresh`` list is a subset of the staged rows, and
    ``COPY`` needs a table whose columns are in the DDL's positional order. The
    schema is taken from the staged table rather than rebuilt, so the types are
    by construction the ones the Parquet schema already declared.
    """
    schema = arrow.schema
    return pa.table(
        {
            field.name: pa.array([row.get(field.name) for row in rows], type=field.type)
            for field in schema
        },
        schema=schema,
    )


def _scalar(value: Any) -> str:
    """A cell as a stable string, for set membership in Python.

    ``None`` and ``""`` both collapse to ``""`` deliberately: an arc with an
    unset property and one with an empty-string property are the same arc to
    the engine, and treating them as distinct would let a re-run insert a
    duplicate every time.
    """
    return "" if value is None else str(value)
