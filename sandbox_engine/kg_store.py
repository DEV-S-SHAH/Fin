"""LadybugDB-backed knowledge graph store for SEC-derived entities.

Follows the idempotent MERGE pattern from ``graphrag/store.py`` but lives in
``sandbox_engine`` to preserve the pipeline boundary (SEC ingestion owns this).

Schema:

    Node: Entity(id STRING PRIMARY KEY, name, entity_type, description, source_url)
    Rel:  CONNECTS(FROM Entity TO Entity, rel_type, description, source_url)

Re-ingesting produces the same row count, so the pipeline can be re-run after
a crash without duplicating the graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import ladybug as lb

NODE_TABLE = "Entity"
REL_TABLE = "CONNECTS"

_CREATE_NODE = f"""
CREATE NODE TABLE {NODE_TABLE}(
    id STRING,
    name STRING,
    entity_type STRING,
    description STRING,
    source_url STRING,
    PRIMARY KEY (id)
)
"""

_CREATE_REL = f"""
CREATE REL TABLE {REL_TABLE}(
    FROM {NODE_TABLE} TO {NODE_TABLE},
    rel_type STRING,
    description STRING,
    source_url STRING
)
"""

_MERGE_NODE = f"""
MERGE (e:{NODE_TABLE} {{id: $id}})
ON CREATE SET e.name = $name, e.entity_type = $entity_type, e.description = $description, e.source_url = $source_url
ON MATCH SET e.name = COALESCE(NULLIF($name, ''), e.name),
             e.entity_type = COALESCE(NULLIF($entity_type, ''), e.entity_type),
             e.description = CASE
                 WHEN e.description IS NULL OR size(e.description) < size($description)
                 THEN $description ELSE e.description END,
             e.source_url = COALESCE(NULLIF($source_url, ''), e.source_url)
"""

_MERGE_EDGE = f"""
MATCH (a:{NODE_TABLE} {{id: $from_id}}), (b:{NODE_TABLE} {{id: $to_id}})
MERGE (a)-[r:{REL_TABLE} {{rel_type: $rel_type}}]->(b)
ON CREATE SET r.description = $description, r.source_url = $source_url
ON MATCH SET r.description = CASE
    WHEN r.description IS NULL OR size(r.description) < size($description)
    THEN $description ELSE r.description END,
    r.source_url = COALESCE(NULLIF($source_url, ''), r.source_url)
"""


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    entity_type: str
    description: str
    source_url: str = ""


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    rel_type: str
    description: str
    source_url: str = ""


class KGStore:
    """Owns one in-process Ladybug database for the knowledge graph."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = lb.Database(str(self.path))
        self.conn = lb.Connection(self.db)

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.db.close()

    def __enter__(self) -> KGStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        """Create the node and relationship tables if they are absent."""
        existing = self.tables()
        if NODE_TABLE not in existing:
            self.conn.execute(_CREATE_NODE)
        if REL_TABLE not in existing:
            self.conn.execute(_CREATE_REL)

    def tables(self) -> set[str]:
        """Names of the node and relationship tables in the database."""
        result = self.conn.execute("CALL SHOW_TABLES() RETURN *")
        columns = [c.lower() for c in result.get_column_names()]
        name_idx = columns.index("name")
        type_idx = columns.index("type")
        names: set[str] = set()
        for row in result.get_all():
            if not row or len(row) <= max(name_idx, type_idx):
                continue
            if str(row[type_idx]).upper() not in ("NODE", "REL"):
                continue
            names.add(str(row[name_idx]))
        return names

    def reset(self) -> None:
        """Drop all data. Used by tests and cleanup."""
        self.conn.execute(f"MATCH (e:{NODE_TABLE}) DETACH DELETE e")

    def upsert_entity(self, entity: Entity) -> None:
        """Insert or merge one entity (idempotent by ``id``)."""
        self.conn.execute(
            _MERGE_NODE,
            {
                "id": entity.id,
                "name": entity.name,
                "entity_type": entity.entity_type,
                "description": entity.description,
                "source_url": entity.source_url,
            },
        )

    def upsert_edge(self, edge: Edge) -> None:
        """Insert or merge one edge (idempotent by ``from_id, to_id, rel_type``)."""
        self.conn.execute(
            _MERGE_EDGE,
            {
                "from_id": edge.from_id,
                "to_id": edge.to_id,
                "rel_type": edge.rel_type,
                "description": edge.description,
                "source_url": edge.source_url,
            },
        )

    def count_entities(self) -> int:
        result = self.conn.execute(f"MATCH (e:{NODE_TABLE}) RETURN count(e) AS c")
        row = result.get_all()[0]
        return int(row[0])

    def count_edges(self) -> int:
        result = self.conn.execute(f"MATCH ()-[r:{REL_TABLE}]->() RETURN count(r) AS c")
        row = result.get_all()[0]
        return int(row[0])
