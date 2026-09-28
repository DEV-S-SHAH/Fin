"""Stage 3: LadybugDB DDL, and the drift guard that keeps it honest.

Emits the ``CREATE`` statements for the whole graph and applies them, then
introspects what actually landed and compares it against what the buffer stage
will write. The table contract itself -- node columns, primary keys, rel
endpoints -- lives in :mod:`sandbox_engine.buffer` and is imported here, so the
Arrow schema and the DDL cannot drift apart silently.

    from sandbox_engine.ddl import schema_ddl, ensure_schema

    for statement in schema_ddl():
        print(statement)

The DDL is company-agnostic
---------------------------

No ticker, segment, or line item appears in any statement. Adding a company is
one ``Company`` node; adding a year is one ``Filing`` node plus its metrics,
events, and chunks. ``Company`` is the tenancy boundary -- everything else is
reachable from it, so a query scoped to one tenant is a query anchored on
``(c:Company {ticker: ...})``.

``REPORTS_METRIC`` carries one ``value``, so a 10-K showing three years of Net
Sales needs three distinct ``Metric`` nodes. The reporting period is therefore
part of the metric identity and is written into ``canonical_name``::

    "Net Sales" (FY2025)   "Net Sales" (FY2024)   "Net Sales" (FY2023)

A prefix match still groups the taxonomy and no content hash is exposed to
query authors. See :data:`~sandbox_engine.config.PERIOD_SCOPED_METRICS` to trade
this back for a pure one-node-per-concept taxonomy.

LadybugDB 0.20.4 syntax notes
-----------------------------

Verified against the engine rather than assumed; each of these is a place where
the obvious spelling is a parse error.

* **Rel table endpoints live inside one parenthesis pair.** The correct form is
  ``CREATE REL TABLE R (FROM A TO B, prop TYPE)``. Writing ``(FROM A TO B,
  (prop TYPE))`` is a binder error.
* **There is no ``ALTER TABLE ADD COLUMN``.** The engine supports ``RENAME`` and
  ``DROP``, so a node table missing a column is rebuilt by rename, recreate,
  copy the shared columns, drop. See :func:`rebuild_node_table`.
* **A rel table cannot be rebuilt.** Copying arcs into a fresh rel table is not
  supported, so drift on a rel table is raised as a ``SchemaDriftError`` with
  the manual fix rather than attempted. See :func:`ensure_schema`.
* **``CREATE ... IF NOT EXISTS`` accepts a drifted table silently.** It checks
  that the *name* is free, not that the columns match. This is why
  :func:`ensure_schema` introspects every table rather than trusting the
  ``IF NOT EXISTS``.
* **A ``<db>.wal`` left by a hard kill is a hard error, not a nuisance.** It is
  reported with recovery instructions; deleting it silently would discard
  whatever the interrupted transaction was carrying.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from .buffer import (
    _DATE_COLUMNS,
    _DOUBLE_COLUMNS,
    _DOUBLE_PROPS,
    _INT_COLUMNS,
    NODE_TABLES,
    PRIMARY_KEYS,
    REL_TABLES,
)

__all__ = [
    "SchemaDriftError",
    "column_type",
    "ensure_schema",
    "expected_columns",
    "existing_columns",
    "parse_date",
    "rebuild_node_table",
    "schema_ddl",
    "table_ddl",
    "table_info",
]

log = logging.getLogger("sandbox_engine.ddl")


class SchemaDriftError(RuntimeError):
    """A table exists with columns this pipeline cannot write.

    Raised rather than repaired for rel tables, because the repair the engine
    supports (rename, recreate, copy, drop) cannot move arcs, and a graph with
    silently missing relationships is worse than a run that stops.
    """


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------


def column_type(name: str) -> str:
    """DDL type for a node column name.

    ``filing_date`` is a real ``DATE`` rather than a string so a query can range
    it and compare it against a date literal without parsing anything in the
    query. ``reported_value`` is ``DOUBLE`` for the same reason: a financial
    fact held as text sorts lexicographically, so the largest revenue in the
    graph would be whichever label happens to end in a high digit.
    """
    if name in _INT_COLUMNS:
        return "INT64"
    if name in _DATE_COLUMNS:
        return "DATE"
    if name in _DOUBLE_COLUMNS:
        return "DOUBLE"
    return "STRING"


def parse_date(text: Any) -> Any:
    """``"2025-10-31"`` -> ``datetime.date``; unparseable text is returned as-is.

    A filing whose date cannot be read is stored verbatim rather than dropped:
    a wrong-looking date is recoverable, a missing filing is not.
    """
    try:
        return datetime.date.fromisoformat(str(text).strip()[:10])
    except (TypeError, ValueError):
        return text


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------


def table_ddl(table: str) -> str:
    """The single ``CREATE`` statement defining *table*."""
    if table in NODE_TABLES:
        body = ", ".join(f"{name} {column_type(name)}" for name in NODE_TABLES[table])
        return (
            f"CREATE NODE TABLE IF NOT EXISTS {table}"
            f"({body}, PRIMARY KEY({PRIMARY_KEYS[table]}))"
        )
    source, target, props = REL_TABLES[table]
    spec = f"FROM {source} TO {target}"
    for prop in props:
        spec += f", {prop} {'DOUBLE' if prop in _DOUBLE_PROPS else 'STRING'}"
    return f"CREATE REL TABLE IF NOT EXISTS {table} ({spec})"


def schema_ddl() -> list[str]:
    """``CREATE ... IF NOT EXISTS`` for every node table then every rel table.

    Node tables come first because the rel tables reference them; the engine
    rejects a rel table whose endpoints do not yet exist.
    """
    return [table_ddl(table) for table in (*NODE_TABLES, *REL_TABLES)]


def expected_columns() -> dict[str, dict[str, str]]:
    """Every table this graph expects, with its column -> type mapping.

    A rel table's ``from``/``to`` are structural and do not appear in
    ``table_info``, so only its declared properties are compared.
    """
    expected: dict[str, dict[str, str]] = {
        table: {name: column_type(name) for name in columns}
        for table, columns in NODE_TABLES.items()
    }
    for rel, (_, _, props) in REL_TABLES.items():
        expected[rel] = {
            prop: ("DOUBLE" if prop in _DOUBLE_PROPS else "STRING") for prop in props
        }
    return expected


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


#: Positions in a ``table_info`` row. Reading by position is the only option:
#: the result set exposes no column names.
_TABLE_INFO_NAME = 1
_TABLE_INFO_TYPE = 2


def table_info(
    connection: Any, table: str
) -> dict[str, str] | None:
    """``{column: type}`` for *table*, or ``None`` if it cannot be inspected.

    ``table_info`` returns one row per column shaped
    ``[ordinal, name, type, default, nullable]``. Flattening those rows into a
    set -- the obvious shortcut -- yields the ordinals, the type names, and the
    nullability flags alongside the real column names. A subset check then
    happens to pass, which is why the mistake is invisible until a table is
    misread as drifted.

    ``None`` and ``{}`` are deliberately different answers. ``None`` means the
    introspection itself failed, so nothing can be concluded and the table is
    left alone. ``{}`` means the table was read successfully and genuinely has
    no properties -- which for a rel table that declares properties *is*
    drift, and treating it as "cannot inspect" would skip the check forever.
    """
    try:
        rows = connection.execute(f"CALL table_info('{table}') RETURN *").get_all()
    except Exception:  # noqa: BLE001 - rel tables and older builds vary
        return None
    return {str(row[_TABLE_INFO_NAME]): str(row[_TABLE_INFO_TYPE]) for row in rows}


def existing_columns(connection: Any, table: str) -> set[str] | None:
    """Column names of *table*, or ``None`` when it could not be read.

    See :func:`table_info` for why the empty set is not a stand-in for failure.
    """
    info = table_info(connection, table)
    return None if info is None else set(info)


def ensure_schema(connection: Any) -> dict[str, Any]:
    """Bring the database to the expected schema, in place.

    Order is load-bearing, and it is node tables, then node drift repair, then
    rel tables:

    * A rel table cannot be created before its endpoints exist.
    * A node table cannot be rebuilt while a rel table points at it. Renaming
      ``Metric`` to a scratch name drags ``HAS_SEGMENT``'s endpoint along with
      it, and the final ``DROP TABLE`` then fails with *"Cannot delete node
      table Metric__pre_migration because it is referenced by relationship
      table HAS_SEGMENT"* -- leaving the migration half-applied. Repairing the
      nodes first, while nothing references them, is the only order in which
      the rebuild is atomic enough to recover from.

    Both missing columns and wrong column *types* count as drift. A
    ``fiscal_year`` declared ``STRING`` accepts every insert and then sorts and
    compares as text, so ``fiscal_year >= 2020`` quietly returns the wrong
    rows -- a failure that no count check would ever surface.

    Returns a report of what was created and what was rebuilt.
    """
    created: list[str] = []
    for table in NODE_TABLES:
        statement = table_ddl(table)
        connection.execute(statement)
        created.append(statement)

    # Node drift repair, before any rel table exists to reference these.
    rebuilt: list[str] = []
    retyped: dict[str, list[str]] = {}
    for table in NODE_TABLES:
        wanted = expected_columns()[table]
        info = table_info(connection, table)
        if info is None:
            continue
        if not _drift(table, wanted, info):
            continue
        log.warning("schema drift: %s: %s; rebuilding the table",
                    table, "; ".join(_drift(table, wanted, info)))
        outcome = rebuild_node_table(connection, table, wanted, info)
        rebuilt.append(table)
        if outcome["retyped_columns"]:
            retyped[table] = outcome["retyped_columns"]

    for rel in REL_TABLES:
        statement = table_ddl(rel)
        connection.execute(statement)
        created.append(statement)

    for rel in REL_TABLES:
        wanted = expected_columns()[rel]
        info = table_info(connection, rel)
        if info is None:
            continue
        problems = _drift(rel, wanted, info)
        if not problems:
            continue
        raise SchemaDriftError(
            f"schema drift: rel table {rel} has {'; '.join(problems)}. LadybugDB "
            f"cannot copy arcs into a rebuilt rel table, so this needs a manual "
            f"fix: DROP TABLE {rel}; then re-run (or delete the database file)."
        )
    return {"created": created, "rebuilt": rebuilt, "retyped_columns": retyped}


def _drift(table: str, wanted: dict[str, str], info: dict[str, str]) -> list[str]:
    """Human-readable differences between *wanted* and what *table* has.

    Only missing and mistyped columns are drift. An **extra** column is
    reported too, because it means the table was written by a different version
    of this pipeline and the extra data is being silently ignored.
    """
    problems = [
        f"missing column {name!r}" for name in sorted(set(wanted) - set(info))
    ]
    problems.extend(
        f"column {name!r} is {info[name]}, expected {dtype}"
        for name, dtype in sorted(wanted.items())
        if name in info and info[name] != dtype
    )
    return problems


def rebuild_node_table(
    connection: Any,
    table: str,
    wanted: dict[str, str],
    existing: dict[str, str],
) -> dict[str, Any]:
    """Rename, recreate, copy the compatible columns, drop the original.

    Columns split into two groups, and the distinction is forced by the binder:

    * **Compatible** -- present before and after with the *same* type. Their
      values are copied across.
    * **Retyped** -- present in both but with a *different* type. Their values
      are **not** copied, and cannot be. Copying a ``STRING`` ``fiscal_year``
      into a new ``INT64`` column is rejected by the binder
      (*"r has data type STRING but (NODE,REL,STRUCT,ANY) was expected"*), and
      coercing in Python instead would invent numbers out of text. The
      structure is repaired and the values are dropped, with a warning, because
      a table with a correct schema and null values is recoverable and a failed
      migration is not.

    The scratch name carries a ``__pre_migration`` suffix so a second concurrent
    migration is visible in ``table_info`` rather than colliding on the name.
    """
    shared = [name for name in wanted if name in existing and existing[name] == wanted[name]]
    retyped = [name for name in wanted if name in existing and existing[name] != wanted[name]]
    if retyped:
        log.warning(
            "table %s: dropping values of retyped column(s) %s; the old values "
            "are not convertible to the new type",
            table, ", ".join(f"{name}: {existing[name]}->{wanted[name]}" for name in retyped),
        )

    scratch = f"{table}__pre_migration"
    connection.execute(f"ALTER TABLE {table} RENAME TO {scratch}")
    connection.execute(table_ddl(table))
    copied = 0
    if shared:
        projection = ", ".join(f"n.{name}" for name in shared)
        rows = connection.execute(f"MATCH (n:{scratch}) RETURN {projection}").get_all()
        assignments = ", ".join(f"{name}: r.{name}" for name in shared)
        connection.execute(
            f"UNWIND $rows AS r CREATE (:{table} {{{assignments}}})",
            {"rows": [dict(zip(shared, row)) for row in rows]},
        )
        copied = len(rows)
        log.info("migrated %s: copied %d row(s) across %s", table, copied, shared)
    connection.execute(f"DROP TABLE {scratch}")
    return {"table": table, "rows_copied": copied, "retyped_columns": retyped}
