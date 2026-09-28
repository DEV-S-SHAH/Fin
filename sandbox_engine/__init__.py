"""Standalone sandbox ingestion and query engine for SEC filings.

A self-contained verification harness for one pipeline, isolated from the
repository's root-level scripts so a change here cannot break them and a bug in
them cannot hide a bug here::

    HTML  --parser-->  dicts  --buffer-->  Arrow/Parquet  --ddl-->  schema
          --loader-->  LadybugDB  --benchmarks-->  5 Cypher verdicts

Stages, one module each:

============  ==========================================  ==================
Module        Responsibility                              Imports
============  ==========================================  ==================
``config``    Paths, the 3-filing scope, tunables         stdlib only
``parser``    Zero-LLM HTML extraction                    pandas, bs4
``entity_resolver``  Canonical identity, deduplication    stdlib only
``buffer``    Arrow RecordBatches, Parquet spill          pyarrow
``ddl``       LadybugDB ``CREATE`` statements, drift      (buffer contract)
``loader``    ``COPY <table> FROM $arrow`` bulk load      ladybug
``benchmarks`` 5 Cypher verification benchmarks           (buffer contract)
============  ==========================================  ==================

Entity identity
---------------

No two nodes mean the same thing. :mod:`sandbox_engine.entity_resolver` owns
what a node's identity *is*: :func:`~sandbox_engine.entity_resolver.
canonical_concept` folds the surface variations a filing uses for one concept
(``"iPhone"`` and ``"iPhone®"``, ``"China (1)"`` and ``"China"``) into one
name, and :class:`~sandbox_engine.entity_resolver.ConceptRegistry` mints one
node id per ``(kind, period)`` and links every filing in a run to it. A segment
named in both the 10-K and the 10-Q is therefore one ``Segment`` node, not two.

The registry is shared for the whole run and persisted next to the report, so a
re-ingest resolves onto the nodes the previous run created instead of building a
second copy.

The table contract -- column order, primary keys, rel endpoints -- is declared
once in :mod:`sandbox_engine.buffer` and imported by both ``ddl`` and
``loader``. ``COPY`` maps Arrow columns to table columns by position, so a
column inserted in the wrong place writes values into the wrong properties; a
single declaration is the only way to make that impossible rather than merely
unlikely.

Scope is the document tree, not a list
--------------------------------------

:func:`sandbox_engine.config.resolve_scope` walks
``sandbox_engine/data/<company>/<year>/<form>/`` and returns every filing it
finds, so the pipeline is universal: a new issuer is a new folder, no config
edit. The verification harness re-runs in seconds after every change, and an
empty tree fails loudly rather than reporting an empty artifact as success.

Dependencies: ``ladybug>=0.20`` (the Kuzu fork; the import name is ``ladybug``,
not ``kuzu``), ``pyarrow``, ``pandas``, ``beautifulsoup4``, ``lxml``.
"""

from __future__ import annotations

from .benchmarks import BenchmarkResult, period_of, run_all
from .buffer import (
    NODE_TABLES,
    PRIMARY_KEYS,
    REL_TABLES,
    StageBuffer,
    arrow_schema,
    to_arrow,
)
from .config import (
    ABSENT_YEAR,
    COPY_THRESHOLD,
    VERSION,
    Paths,
    default_paths,
    resolve_scope,
)
from .ddl import SchemaDriftError, ensure_schema, schema_ddl
from .entity_resolver import (
    CanonicalEntity,
    Concept,
    ConceptRegistry,
    EntityRegistry,
    Resolution,
    canonical_concept,
    resolve_subgraph,
)
from .loader import BulkLoader, LoadReport, WalRecoveryError
from .parser import ExtractionResult, FilingParser

__all__ = [
    "ABSENT_YEAR",
    "BenchmarkResult",
    "BulkLoader",
    "COPY_THRESHOLD",
    "CanonicalEntity",
    "Concept",
    "ConceptRegistry",
    "EntityRegistry",
    "ExtractionResult",
    "FilingParser",
    "LoadReport",
    "NODE_TABLES",
    "Paths",
    "PRIMARY_KEYS",
    "REL_TABLES",
    "Resolution",
    "SchemaDriftError",
    "StageBuffer",
    "VERSION",
    "WalRecoveryError",
    "arrow_schema",
    "canonical_concept",
    "default_paths",
    "ensure_schema",
    "period_of",
    "resolve_scope",
    "resolve_subgraph",
    "run_all",
    "schema_ddl",
    "to_arrow",
]
