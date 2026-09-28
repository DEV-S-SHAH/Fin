"""LadybugDB-backed knowledge graph: schema, idempotent writes, traversal.

The schema is domain-agnostic by construction. There is exactly one node table
and one relationship table, and all semantics live in free-text properties
discovered by the LLM:

    Node: Entity(id STRING PRIMARY KEY, name, entity_type, description)
    Rel:  CONNECTS(FROM Entity TO Entity, rel_type, description)

Idempotency is a property of the Cypher, not of the caller. Re-ingesting a
document produces the same row count, so the pipeline can be re-run after a
crash or a model change without duplicating the graph.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import ladybug as lb

from .config import GraphRAGConfig
from .resolve import (
    UNSPECIFIED,
    clean_name,
    is_usable_name,
    merge_observation,
    name_variants,
    slugify,
)

NODE_TABLE = "Entity"

# Sentinel rank for a name that only matched a non-word substring of the
# query; such candidates are dropped rather than ranked.
_RANK_NONE = 99
REL_TABLE = "CONNECTS"

_CREATE_NODE = f"""
CREATE NODE TABLE {NODE_TABLE}(
    id STRING,
    name STRING,
    entity_type STRING,
    description STRING,
    PRIMARY KEY (id)
)
"""

_CREATE_REL = f"""
CREATE REL TABLE {REL_TABLE}(
    FROM {NODE_TABLE} TO {NODE_TABLE},
    rel_type STRING,
    description STRING
)
"""

# `MERGE` on a relationship pattern that includes a property makes the edge
# identity (from, to, rel_type). Verified against the engine: merging the same
# pair under two different rel_types yields two rows, and re-merging either
# yields no growth.
_MERGE_NODE = f"""
MERGE (e:{NODE_TABLE} {{id: $id}})
ON CREATE SET e.name = $name, e.entity_type = $entity_type, e.description = $description
"""

_MERGE_EDGE = f"""
MATCH (a:{NODE_TABLE} {{id: $from_id}}), (b:{NODE_TABLE} {{id: $to_id}})
MERGE (a)-[r:{REL_TABLE} {{rel_type: $rel_type}}]->(b)
ON CREATE SET r.description = $description
ON MATCH SET r.description = CASE
    WHEN r.description IS NULL OR size(r.description) < size($description)
    THEN $description ELSE r.description END
"""


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    entity_type: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
        }


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    rel_type: str
    description: str


@dataclass
class Subgraph:
    """A retrieval result: nodes plus the edges that justify their relevance."""

    seeds: list[Entity] = field(default_factory=list)
    nodes: dict[str, Entity] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    # Hop distance from the nearest seed, used to trim by relevance rather than
    # by whatever order traversal happened to produce.
    depth: dict[str, int] = field(default_factory=dict)

    def node_list(self) -> list[Entity]:
        return sorted(self.nodes.values(), key=lambda e: e.id)

    def relevance_order(self) -> list[Entity]:
        """Nodes closest to a seed first, ties broken by id for determinism.

        Sorting by id alone would let an unrelated node evict the evidence for
        the question, because ids carry no signal about relevance.
        """
        return sorted(
            self.nodes.values(),
            key=lambda e: (self.depth.get(e.id, 1 << 30), e.id),
        )

    def is_empty(self) -> bool:
        return not self.nodes and not self.edges


class GraphStore:
    """Owns one in-process Ladybug database."""

    def __init__(
        self,
        path: str | Path,
        config: GraphRAGConfig | None = None,
        read_only: bool = False,
    ) -> None:
        self.config = config or GraphRAGConfig()
        self.path = Path(path)
        self.read_only = read_only

        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.db = lb.Database(
            str(self.path),
            buffer_pool_size=self.config.buffer_pool_size,
            max_num_threads=self.config.max_num_threads or 0,
            read_only=read_only,
        )
        self.conn = lb.Connection(self.db, num_threads=self.config.max_num_threads or 0)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.db.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema ------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the domain-agnostic tables if they are absent."""
        existing = self.tables()
        if NODE_TABLE not in existing:
            self.conn.execute(_CREATE_NODE)
        if REL_TABLE not in existing:
            self.conn.execute(_CREATE_REL)

    def tables(self) -> set[str]:
        """Names of the node and relationship tables in the database."""
        result = self.conn.execute("CALL SHOW_TABLES() RETURN *")
        columns = [c.lower() for c in result.get_column_names()]
        try:
            name_idx = columns.index("name")
            type_idx = columns.index("type")
        except ValueError:  # pragma: no cover - defensive against schema drift
            name_idx, type_idx = 1, 2
        names: set[str] = set()
        for row in result.get_all():
            if not row or len(row) <= max(name_idx, type_idx):
                continue
            # System/internal tables are reported with a different type; only
            # user node and rel tables participate in the domain-agnostic schema.
            if str(row[type_idx]).upper() not in ("NODE", "REL"):
                continue
            names.add(str(row[name_idx]))
        return names

    def reset(self) -> None:
        """Drop all data. Used by tests and the ``--reset`` flag."""
        self.conn.execute(f"MATCH (e:{NODE_TABLE}) DETACH DELETE e")

    # -- writes ------------------------------------------------------------

    def upsert_entity(self, name: str, entity_type: str, description: str) -> str | None:
        """Insert or merge one entity, returning its id (or None if unusable)."""
        if not is_usable_name(name, self.config.min_entity_name_length):
            return None
        entity_id = slugify(name)
        if not entity_id:
            return None

        display = clean_name(name) or name.strip()
        # A blank incoming type must not be recorded as the placeholder: the
        # refinement pass merges by "longer wins", and the placeholder is
        # longer than most real types, so it would overwrite them.
        kind = (entity_type or "").strip()
        desc = (description or "").strip()

        self.conn.execute(
            _MERGE_NODE,
            {
                "id": entity_id,
                "name": display,
                "entity_type": kind or UNSPECIFIED,
                "description": desc,
            },
        )

        # A second pass lets a richer observation refine a node that already
        # exists, without ever overwriting a populated field with a blank one.
        row = self._fetch_entity(entity_id)
        if row is not None:
            merged = merge_observation(
                row.as_dict(),
                {
                    "name": display,
                    "entity_type": kind,
                    "description": desc,
                },
            )
            if merged != row.as_dict():
                self.conn.execute(
                    f"MATCH (e:{NODE_TABLE} {{id: $id}}) SET "
                    "e.name = $name, e.entity_type = $entity_type, "
                    "e.description = $description",
                    {"id": entity_id, **merged},
                )
        return entity_id

    def upsert_edge(
        self, from_name: str, to_name: str, rel_type: str, description: str
    ) -> tuple[str, str] | None:
        """Insert or merge one relationship. Returns the endpoint ids."""
        src = slugify(from_name)
        dst = slugify(to_name)
        if not src or not dst or src == dst:
            return None

        relation = (rel_type or "").strip() or "related_to"
        desc = (description or "").strip()

        # Only create the edge when both endpoints already resolve to nodes;
        # otherwise a dangling edge would be written.
        if not self.entity_exists(src) or not self.entity_exists(dst):
            return None

        self.conn.execute(
            _MERGE_EDGE,
            {
                "from_id": src,
                "to_id": dst,
                "rel_type": relation,
                "description": desc,
            },
        )
        return src, dst

    # -- reads -------------------------------------------------------------

    def entity_exists(self, entity_id: str) -> bool:
        rows = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) WHERE e.id = $id RETURN e.id",
            {"id": entity_id},
        ).get_all()
        return bool(rows)

    def _fetch_entity(self, entity_id: str) -> Entity | None:
        result = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) WHERE e.id = $id "
            "RETURN e.id, e.name, e.entity_type, e.description",
            {"id": entity_id},
        )
        rows = result.get_all()
        if not rows:
            return None
        return self._to_entity(rows[0])

    @staticmethod
    def _to_entity(row: list[Any]) -> Entity:
        padded = list(row) + [None] * (4 - len(row))
        return Entity(
            id=str(padded[0] or ""),
            name=str(padded[1] or ""),
            entity_type=str(padded[2] or ""),
            description=str(padded[3] or ""),
        )

    def find_entities(self, ids: Iterable[str]) -> list[Entity]:
        wanted = [i for i in dict.fromkeys(ids) if i]
        if not wanted:
            return []
        result = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) WHERE list_contains($ids, e.id) "
            "RETURN e.id, e.name, e.entity_type, e.description",
            {"ids": wanted},
        )
        return [self._to_entity(row) for row in result.get_all()]

    def lookup_by_name(self, needle: str) -> list[Entity]:
        """Rank name matches for query-time entity linking.

        Substring matching alone is too loose: the name "Riftia" would match
        every node whose name contains it. Results are therefore ordered by how
        specific the match is, so a caller taking the first few gets the node
        actually meant rather than every relative of the name.

        A question rarely spells a name exactly as the graph stores it, so the
        search is run over :func:`name_variants` ("transformers" also tries
        "transformer") and additionally accepts a name that appears as a whole
        word *inside* the query ("transformer architecture" finds
        "Transformer"). That reverse form is ranked below every forward match,
        because it is the loosest way to hit a node.
        """
        cleaned = (needle or "").strip()
        if not cleaned:
            return []
        variants = name_variants(cleaned)
        if not variants:
            return []

        found: dict[str, Entity] = {}
        for variant in variants:
            # Forward: the stored name contains the variant.
            for entity in self._names_matching("contains(lower(e.name), lower($q))", variant):
                found.setdefault(entity.id, entity)
            # Reverse: the variant contains the stored name. Filtered to word
            # boundaries below, so "transformer" cannot drag in "trans".
            for entity in self._names_matching("contains(lower($q), lower(e.name))", variant):
                found.setdefault(entity.id, entity)

        ranked = sorted(
            found.values(),
            key=lambda e: _match_rank(e, cleaned, variants),
        )
        survivors = [
            e for e in ranked if _match_rank(e, cleaned, variants)[0] < _RANK_NONE
        ]
        if survivors:
            return survivors
        return self._match_by_tokens(cleaned)

    def _match_by_tokens(self, needle: str) -> list[Entity]:
        """Last-resort match for a query that spells no name as a substring.

        People name a thing partly and then say what kind of thing it is:
        *"multi attention mechanism"* is `multi-head attention`, whose
        entity type is `mechanism`. No substring rule can see that, so each
        significant word of the query is matched against the node's name *or*
        its type, and only nodes accounting for every such word are returned.

        Ranking prefers the node with the fewest words the query did not ask
        for, which is what separates `multi-head attention` (one extra) from
        `multi-head self-attention mechanism` (two).
        """
        tokens = _content_tokens(needle)
        if not tokens:
            return []

        clauses = " OR ".join(
            f"contains(lower(e.name), ${'t' + str(i)}) "
            f"OR contains(lower(e.entity_type), ${'t' + str(i)})"
            for i in range(len(tokens))
        )
        result = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) WHERE {clauses} "
            "RETURN e.id, e.name, e.entity_type, e.description LIMIT 200",
            {f"t{i}": t for i, t in enumerate(tokens)},
        )

        matches: list[Entity] = []
        for row in result.get_all():
            entity = self._to_entity(row)
            name_tokens = _content_tokens(entity.name)
            type_tokens = _content_tokens(entity.entity_type)
            if all(t in name_tokens or t in type_tokens for t in tokens):
                matches.append(entity)

        return sorted(matches, key=lambda e: _token_overlap_rank(e, tokens))

    def _names_matching(self, predicate: str, value: str) -> list[Entity]:
        result = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) WHERE {predicate} "
            "RETURN e.id, e.name, e.entity_type, e.description LIMIT 50",
            {"q": value},
        )
        return [self._to_entity(row) for row in result.get_all()]

    def best_name_match(self, needle: str, limit: int = 3) -> list[Entity]:
        """The most specific nodes matching *needle*."""
        return self.lookup_by_name(needle)[:limit]

    def search_entities(self, text: str, limit: int = 25) -> list[Entity]:
        return self.lookup_by_name(text)[:limit]

    def all_entities(self, limit: int = 1000) -> list[Entity]:
        result = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) RETURN e.id, e.name, e.entity_type, "
            f"e.description LIMIT {int(limit)}"
        )
        return [self._to_entity(row) for row in result.get_all()]

    # -- traversal ---------------------------------------------------------

    def neighborhood(self, seed_ids: list[str], hops: int = 2) -> Subgraph:
        """Collect the hop-limited subgraph around *seed_ids*.

        Traversal is undirected: a question about a consequence ("what does X
        depend on") needs the inbound edge just as much as the outbound one.
        """
        seeds = list(dict.fromkeys(i for i in seed_ids if i))
        graph = Subgraph()
        if not seeds:
            return graph

        graph.seeds = self.find_entities(seeds)
        for entity in graph.seeds:
            graph.nodes[entity.id] = entity
            graph.depth[entity.id] = 0

        frontier = {e.id for e in graph.seeds}
        # One set across all depths: a 2-hop path can re-surface an edge already
        # returned from the other side at depth 1.
        seen_edges: set[tuple[str, str, str]] = set()

        for depth in range(1, max(1, hops) + 1):
            found = self._edges_at_depth(frontier, depth)
            if not found:
                break
            for edge in found:
                key = (*sorted((edge.from_id, edge.to_id)), edge.rel_type)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                graph.edges.append(edge)
            discovered: set[str] = set()
            for edge in found:
                for node_id in (edge.from_id, edge.to_id):
                    if node_id not in graph.nodes:
                        discovered.add(node_id)
            if not discovered:
                break
            for entity in self.find_entities(discovered):
                graph.nodes[entity.id] = entity
                graph.depth.setdefault(entity.id, depth)
            frontier = discovered - {e.id for e in graph.seeds}
            if not frontier:
                break

        self._trim(graph)
        return graph

    def _edges_at_depth(self, frontier: set[str], depth: int) -> list[Edge]:
        params: dict[str, Any] = {"ids": sorted(frontier)}
        if depth == 1:
            query = f"""
            MATCH (a:{NODE_TABLE})-[r:{REL_TABLE}]-(b:{NODE_TABLE})
            WHERE list_contains($ids, a.id)
            RETURN a.id, b.id, r.rel_type, r.description
            """
        else:
            query = f"""
            MATCH (a:{NODE_TABLE})-[r1:{REL_TABLE}]-(m:{NODE_TABLE})-[r2:{REL_TABLE}]-(b:{NODE_TABLE})
            WHERE list_contains($ids, a.id)
              AND NOT m.id IN $ids
            RETURN a.id, m.id, r1.rel_type, r1.description
            UNION ALL
            MATCH (a:{NODE_TABLE})-[r1:{REL_TABLE}]-(m:{NODE_TABLE})-[r2:{REL_TABLE}]-(b:{NODE_TABLE})
            WHERE list_contains($ids, a.id)
              AND NOT m.id IN $ids
            RETURN m.id, b.id, r2.rel_type, r2.description
            """
        rows = self.conn.execute(query, params).get_all()
        seen: set[tuple[str, str, str]] = set()
        edges: list[Edge] = []
        for row in rows:
            padded = list(row) + [None] * (4 - len(row))
            edge = Edge(
                from_id=str(padded[0] or ""),
                to_id=str(padded[1] or ""),
                rel_type=str(padded[2] or ""),
                description=str(padded[3] or ""),
            )
            # Traversal is undirected, so an edge whose two endpoints both sit
            # in the frontier is returned from each side. Key on the unordered
            # pair to collapse that while keeping the first direction seen.
            key = (
                *sorted((edge.from_id, edge.to_id)),
                edge.rel_type,
            )
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)
        return edges

    def _trim(self, graph: Subgraph) -> None:
        """Bound the context so a dense hub cannot flood the prompt.

        Trimming used to keep whichever nodes sorted first by id, which meant a
        hub reachable at depth two could evict the edges belonging to the seeds
        themselves. A question then reached the model with its own subject
        present but attached to nothing, and the model correctly reported that
        the context held no such relationship -- a confident negative derived
        from an amputated context rather than from the graph.

        Two guarantees keep that from happening:

        * every edge incident to a seed survives, along with both endpoints;
        * the remaining budget is spent on nodes nearest a seed.

        A question about one specific thing therefore keeps the evidence for
        that thing, even when a hub is nearby.
        """
        max_nodes = self.config.max_context_nodes
        max_edges = self.config.max_context_edges
        if len(graph.nodes) <= max_nodes and len(graph.edges) <= max_edges:
            return

        seed_ids = {e.id for e in graph.seeds}

        # A seed's own relationships are the answer to a question about it.
        forced_edges = [
            e for e in graph.edges if e.from_id in seed_ids or e.to_id in seed_ids
        ]
        forced_nodes = set(seed_ids)
        for edge in forced_edges:
            forced_nodes.update((edge.from_id, edge.to_id))

        keep = {e.id for e in graph.seeds}
        for entity in graph.relevance_order():
            if len(keep) >= max_nodes:
                break
            keep.add(entity.id)
        # Direct neighbours of a seed outrank anything merely nearby.
        for node_id in forced_nodes:
            if len(keep) >= max_nodes:
                break
            keep.add(node_id)

        graph.nodes = {k: v for k, v in graph.nodes.items() if k in keep}
        surviving = [e for e in graph.edges if e.from_id in keep and e.to_id in keep]
        forced_keys = {
            (*sorted((e.from_id, e.to_id)), e.rel_type) for e in forced_edges
        }

        def rank(edge: Edge) -> tuple[int, int, str]:
            key = (*sorted((edge.from_id, edge.to_id)), edge.rel_type)
            incident = 0 if key in forced_keys else 1
            near = min(
                graph.depth.get(edge.from_id, 1 << 30),
                graph.depth.get(edge.to_id, 1 << 30),
            )
            return (incident, near, edge.rel_type)

        graph.edges = sorted(surviving, key=rank)[:max_edges]

    # -- reporting ---------------------------------------------------------

    def counts(self) -> tuple[int, int]:
        """Current (node, edge) totals, used to measure what a run added."""
        nodes = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) RETURN count(e) AS n"
        ).get_next()[0]
        edges = self.conn.execute(
            f"MATCH ()-[r:{REL_TABLE}]->() RETURN count(r) AS n"
        ).get_next()[0]
        return int(nodes), int(edges)

    def stats(self) -> dict[str, Any]:
        nodes, edges = self.counts()
        types = self.conn.execute(
            f"MATCH (e:{NODE_TABLE}) RETURN e.entity_type AS t, count(e) AS n "
            "ORDER BY n DESC LIMIT 15"
        ).get_all()
        rel_types = self.conn.execute(
            f"MATCH ()-[r:{REL_TABLE}]->() RETURN r.rel_type AS t, count(r) AS n "
            "ORDER BY n DESC LIMIT 15"
        ).get_all()
        return {
            "path": str(self.path),
            "buffer_pool_size": self.config.buffer_pool_size,
            "nodes": nodes,
            "edges": edges,
            "entity_types": [(str(t), int(n)) for t, n in types],
            "relation_types": [(str(t), int(n)) for t, n in rel_types],
        }


def default_db_path() -> Path:
    return Path(os.environ.get("GRAPHRAG_DB", "./graphrag_db.lbug"))


def _match_rank(
    entity: Entity, needle: str, variants: list[str] | None = None
) -> tuple[int, int, str]:
    """Sort key for name-match specificity: lower is better.

    Ranks an exact name match ahead of a word-boundary match, which in turn
    beats a loose substring match. Ties fall back to the shorter name, so
    "Riftia" prefers "Riftia" over "Riftia pachyptila".

    Variants (de-pluralised forms of the query) are treated as equal to the
    query itself, so "transformers" ranks the "Transformer" node as an exact
    hit. A reverse match -- the stored name occurring as a whole word inside
    the query -- ranks below every forward match, since it is the loosest.
    Returns :data:`_RANK_NONE` when the name only matched a non-word-boundary
    substring of the query, which the caller discards.
    """
    name = entity.name.lower()
    targets = [t.lower() for t in (variants or [needle])]
    if needle.lower() not in targets:
        targets.append(needle.lower())

    rank = _RANK_NONE
    for target in targets:
        if name == target:
            candidate = 0
        elif name.startswith(target + " ") or name.endswith(" " + target):
            candidate = 1
        elif f" {target} " in f" {name} ":
            candidate = 2
        elif target in name:
            candidate = 3
        elif _contains_word(target, name):
            # Reverse direction: the name is a whole word inside the query.
            candidate = 4
        else:
            continue
        rank = candidate if rank == _RANK_NONE else min(rank, candidate)
        if rank == 0:
            break

    return (rank, len(name), name)
def _contains_word(haystack: str, word: str) -> bool:
    """True when *word* appears in *haystack* on a token boundary.

    Guards the reverse match: "trans" is a substring of "transformer" but not a
    word in it, so a query of "transformer" must not surface a node named
    "trans".
    """
    if not word:
        return False
    tokens = re.split(r"[^0-9a-z]+", haystack.lower())
    return word.lower() in tokens


# Words that carry no entity signal in a question, so they must not have to be
# "found" for a node to match.
_QUESTION_WORDS = frozenset(
    """
    a an the of in on at to for from by with about into over is are was were be
    been being do does did what which who whom whose how when where why and or
    but if then than that this these those there here it its as not no nor so
    such very much many more most some any all both each other another same
    """.split()
)


def _content_tokens(text: str) -> set[str]:
    """Significant lowercase word tokens of *text*.

    Splits on anything non-alphanumeric, so "multi-head attention" and
    "multihead" both reduce to comparable tokens, and drops interrogatives so
    "what is a multi attention mechanism" reduces to the content words.
    """
    return {
        t
        for t in re.split(r"[^a-z0-9]+", (text or "").lower())
        if t and t not in _QUESTION_WORDS
    }


def _token_overlap_rank(entity: Entity, tokens: list[str]) -> tuple[int, int, str]:
    """Sort key for the token fallback: fewest unasked-for name words first.

    Only *name* words count against a node. Its type has already been consulted
    to satisfy part of the query, so charging the node again for having a type
    would penalise every classified entity equally and tell us nothing.

    This is what puts `multi-head attention` (one extra word) ahead of
    `multi-head self-attention mechanism` (two) for the query "multi attention
    mechanism".
    """
    extra = _content_tokens(entity.name) - set(tokens)
    return (len(extra), len(_content_tokens(entity.name)), entity.name.lower())


