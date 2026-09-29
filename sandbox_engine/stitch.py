"""In-memory overlay graph and backbone stitching for Tier 1 Cold Start.

Maintains an ephemeral networkx.DiGraph layered on top of the read-only LadybugDB backbone.
Resolves extracted entities into canonical nodes via ConceptRegistry and stable_id,
bridges cold-start entities to persistent backbone nodes, and deduplicates arcs in memory.
"""

from __future__ import annotations

from typing import Any, Optional
import networkx as nx

from .coldstart_schema import ExtractionPayload
from .entity_resolver import CONCEPT_ALIASES, ConceptRegistry
from .parser import stable_id
from .router import COMPANY_ALIASES

#: Seed aliases for common financial and supply chain entities
DEFAULT_COLDSTART_ALIASES: dict[str, tuple[str, ...]] = {
    "TSMC": (
        "taiwan semiconductor",
        "taiwan semiconductor manufacturing company",
        "taiwan semiconductor manufacturing co",
        "taiwan semiconductor manufacturing co., ltd.",
        "tsmc ltd",
    ),
    "Foxconn": (
        "hon hai precision industry",
        "hon hai",
        "foxconn technology group",
    ),
    "Samsung Electronics": (
        "samsung",
        "samsung electronics co",
        "samsung electronics co., ltd.",
    ),
    "Qualcomm": (
        "qualcomm inc",
        "qualcomm incorporated",
    ),
    "Broadcom": (
        "broadcom inc",
        "broadcom corporation",
    ),
    "ASML": (
        "asml holding",
        "asml holding nv",
    ),
}


class InMemoryOverlayGraph:
    """Ephemeral in-memory graph layered over the persistent read-only LadybugDB backbone."""

    def __init__(self, kg_connection: Any = None) -> None:
        self.kg = kg_connection
        self.graph = nx.DiGraph()

        # Combine domain aliases, supply chain aliases, and company aliases
        combined_seeds = dict(CONCEPT_ALIASES)
        combined_seeds.update(DEFAULT_COLDSTART_ALIASES)
        for ticker, info in COMPANY_ALIASES.items():
            combined_seeds[ticker] = info[1]

        self.registry = ConceptRegistry(stable_id, alias_seeds=combined_seeds)

    def check_backbone_presence(self, identifier: str, entity_type: str = "") -> bool:
        """Check if an entity exists in the read-only LadybugDB backbone without locking."""
        if self.kg is None:
            return False

        clean = identifier.strip()
        if not clean:
            return False

        # 1. Direct has_company check if available
        if hasattr(self.kg, "has_company"):
            try:
                if self.kg.has_company(clean.upper()):
                    return True
            except Exception:
                pass

        # 2. Check mock or known entities set
        if hasattr(self.kg, "known_entities"):
            known = self.kg.known_entities
            if clean in known or clean.upper() in known:
                return True

        # 3. Read-only Cypher query if execute method available
        if hasattr(self.kg, "execute"):
            try:
                cypher = (
                    "MATCH (c:Company) WHERE toLower(c.ticker) = toLower($val) "
                    "OR toLower(c.name) = toLower($val) RETURN c.ticker LIMIT 1"
                )
                rows = self.kg.execute(cypher, {"val": clean})
                if rows and len(rows) > 0:
                    return True
            except Exception:
                pass

        return False

    def get_node_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Find the attributes of a node by canonical name, ticker, or alias."""
        clean_name = name.strip().lower()
        for node_id, data in self.graph.nodes(data=True):
            if str(data.get("name", "")).strip().lower() == clean_name:
                return {"id": node_id, **data}
            if str(data.get("ticker", "")).strip().lower() == clean_name:
                return {"id": node_id, **data}

        # Resolve via registry partition lookup
        for part in self.registry._partitions.values():
            canon_id = part.lookup(name)
            if canon_id and canon_id in self.graph:
                return {"id": canon_id, **self.graph.nodes[canon_id]}

        return None

    def has_edge_between(self, source_name: str, target_name: str) -> bool:
        """Check if an edge exists between two nodes by their names."""
        src = self.get_node_by_name(source_name)
        tgt = self.get_node_by_name(target_name)
        if not src or not tgt:
            return False
        return self.graph.has_edge(src["id"], tgt["id"])


def stitch_coldstart_payload(
    overlay: InMemoryOverlayGraph,
    payload: ExtractionPayload,
    target_ticker: str,
) -> dict[str, int]:
    """Resolve extracted entities, attach backbone nodes, and stitch into overlay graph.

    Parameters:
    - overlay: In-memory overlay graph.
    - payload: Validated ExtractionPayload from triple extractor.
    - target_ticker: Stock ticker of the cold-start company.

    Returns:
    - Stitching summary: {"new_nodes": int, "stitched_backbone_edges": int, "ephemeral_edges": int}
    """
    initial_nodes = set(overlay.graph.nodes())

    # 1. Canonical registration of target company
    target_clean = target_ticker.strip().upper()
    target_res = overlay.registry.register("Company", target_clean)
    target_canonical_name = (
        overlay.registry.partition("Company").get(target_res.canonical_id).name
    )
    target_canonical_id = target_res.canonical_id

    # Add or update target company node
    overlay.graph.add_node(
        target_canonical_id,
        id=target_canonical_id,
        name=target_canonical_name,
        ticker=target_clean,
        entity_type="Company",
        is_cold_start=True,
    )

    # 2. Map payload entity IDs to canonical nodes
    id_to_canonical: dict[str, tuple[str, str, str]] = {}
    id_to_canonical[target_ticker] = (target_canonical_id, target_canonical_name, "Company")
    id_to_canonical[target_clean] = (target_canonical_id, target_canonical_name, "Company")

    for entity in payload.entities:
        res = overlay.registry.register(entity.entity_type, entity.name)
        canon_name = (
            overlay.registry.partition(entity.entity_type).get(res.canonical_id).name
        )
        canon_id = res.canonical_id

        id_to_canonical[entity.id] = (canon_id, canon_name, entity.entity_type)
        id_to_canonical[entity.name] = (canon_id, canon_name, entity.entity_type)
        id_to_canonical[canon_name] = (canon_id, canon_name, entity.entity_type)

        node_attrs = dict(entity.properties)
        node_attrs.update({
            "id": canon_id,
            "name": canon_name,
            "entity_type": entity.entity_type,
            "is_cold_start": True,
        })
        overlay.graph.add_node(canon_id, **node_attrs)

    # 3. Stitch relationships into overlay graph
    stitched_backbone_edges = 0
    ephemeral_edges = 0

    for rel in payload.relationships:
        # Resolve source node
        if rel.source_id in id_to_canonical:
            src_id, src_name, src_type = id_to_canonical[rel.source_id]
        else:
            s_res = overlay.registry.register("Company", rel.source_id)
            src_name = overlay.registry.partition("Company").get(s_res.canonical_id).name
            src_id = s_res.canonical_id
            src_type = "Company"
            overlay.graph.add_node(
                src_id,
                id=src_id,
                name=src_name,
                entity_type=src_type,
                is_cold_start=True,
            )
            id_to_canonical[rel.source_id] = (src_id, src_name, src_type)

        # Resolve target node
        if rel.target_id in id_to_canonical:
            tgt_id, tgt_name, tgt_type = id_to_canonical[rel.target_id]
        else:
            t_res = overlay.registry.register("Company", rel.target_id)
            tgt_name = overlay.registry.partition("Company").get(t_res.canonical_id).name
            tgt_id = t_res.canonical_id
            tgt_type = "Company"
            overlay.graph.add_node(
                tgt_id,
                id=tgt_id,
                name=tgt_name,
                entity_type=tgt_type,
                is_cold_start=True,
            )
            id_to_canonical[rel.target_id] = (tgt_id, tgt_name, tgt_type)

        # Disallow self-loops
        if src_id == tgt_id:
            continue

        # Check backbone attachment: is target or source in persistent backbone?
        src_in_backbone = overlay.check_backbone_presence(src_name, src_type)
        tgt_in_backbone = overlay.check_backbone_presence(tgt_name, tgt_type)
        is_backbone_stitch = src_in_backbone or tgt_in_backbone

        if is_backbone_stitch:
            stitched_backbone_edges += 1
        else:
            ephemeral_edges += 1

        # Enforce arc deduplication in memory via networkx.DiGraph
        edge_attrs = dict(rel.properties)
        edge_attrs.update({
            "relation": rel.relation,
            "confidence": rel.confidence,
            "evidence_quote": rel.evidence_quote,
            "is_backbone_stitch": is_backbone_stitch,
        })
        overlay.graph.add_edge(src_id, tgt_id, **edge_attrs)

    current_nodes = set(overlay.graph.nodes())
    new_nodes = len(current_nodes - initial_nodes)

    return {
        "new_nodes": new_nodes,
        "stitched_backbone_edges": stitched_backbone_edges,
        "ephemeral_edges": ephemeral_edges,
    }
