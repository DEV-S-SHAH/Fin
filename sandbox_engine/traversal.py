"""Multi-hop hybrid graph traversal across in-memory overlay and LadybugDB backbone.

Navigates ephemeral cold-start relationships and stitches across the boundary into
persistent canonical entities (e.g. suppliers, competitors, macro risks), preventing
cycles and outputting structured provenance ledgers.
"""

from __future__ import annotations

from typing import Any, Optional
from .parser import stable_id
from .stitch import InMemoryOverlayGraph


class HybridGraphTraverser:
    """Traverses hybrid neighborhoods across in-memory overlay and persistent backbone."""

    def __init__(self, overlay: InMemoryOverlayGraph) -> None:
        self.overlay = overlay

    def traverse_neighborhood(
        self,
        start_entity_id: str,
        max_hops: int = 2,
    ) -> dict[str, Any]:
        """Traverse up to max_hops from start_entity_id.

        Hop 1: Immediate neighbors in the in-memory graph.
        Hop 2: For canonical backbone neighbors (or overlay nodes), query LadybugDB connections.
        Prevents cycles and returns a structured subgraph.
        """
        # Resolve start node
        start_node_id = start_entity_id
        if start_node_id not in self.overlay.graph:
            found = self.overlay.get_node_by_name(start_entity_id)
            if found:
                start_node_id = found["id"]
            else:
                return {"nodes": [], "paths": []}

        start_data = self.overlay.graph.nodes[start_node_id]
        start_name = start_data.get("name", start_node_id)
        start_type = start_data.get("entity_type", "Company")

        nodes_dict: dict[str, dict[str, Any]] = {
            start_node_id: {
                "id": start_node_id,
                "name": start_name,
                "type": start_type,
                "is_ephemeral": bool(start_data.get("is_cold_start", True)),
            }
        }
        paths: list[list[dict[str, Any]]] = []

        # Hop 1: Overlay immediate outgoing edges
        for _, neighbor_id, edge_data in self.overlay.graph.out_edges(
            start_node_id, data=True
        ):
            neighbor_data = self.overlay.graph.nodes[neighbor_id]
            neighbor_name = neighbor_data.get("name", neighbor_id)
            neighbor_type = neighbor_data.get("entity_type", "Unknown")
            is_ephemeral = bool(neighbor_data.get("is_cold_start", True))

            nodes_dict[neighbor_id] = {
                "id": neighbor_id,
                "name": neighbor_name,
                "type": neighbor_type,
                "is_ephemeral": is_ephemeral,
            }

            hop1_edge = {
                "source": start_name,
                "source_id": start_node_id,
                "relation": edge_data.get("relation", "CONNECTED_TO"),
                "target": neighbor_name,
                "target_id": neighbor_id,
                "confidence": edge_data.get("confidence", 1.0),
                "evidence_quote": edge_data.get("evidence_quote", ""),
                "properties": {
                    k: v
                    for k, v in edge_data.items()
                    if k not in ("relation", "confidence", "evidence_quote")
                },
                "is_backbone": False,
            }
            paths.append([hop1_edge])

            if max_hops < 2:
                continue

            # Hop 2A: Further in-memory edges from neighbor
            for _, hop2_id, hop2_edge_data in self.overlay.graph.out_edges(
                neighbor_id, data=True
            ):
                # Cycle prevention
                if hop2_id in (start_node_id, neighbor_id):
                    continue

                hop2_data = self.overlay.graph.nodes[hop2_id]
                hop2_name = hop2_data.get("name", hop2_id)
                hop2_type = hop2_data.get("entity_type", "Unknown")

                nodes_dict[hop2_id] = {
                    "id": hop2_id,
                    "name": hop2_name,
                    "type": hop2_type,
                    "is_ephemeral": bool(hop2_data.get("is_cold_start", True)),
                }

                hop2_edge = {
                    "source": neighbor_name,
                    "source_id": neighbor_id,
                    "relation": hop2_edge_data.get("relation", "CONNECTED_TO"),
                    "target": hop2_name,
                    "target_id": hop2_id,
                    "confidence": hop2_edge_data.get("confidence", 1.0),
                    "evidence_quote": hop2_edge_data.get("evidence_quote", ""),
                    "properties": {
                        k: v
                        for k, v in hop2_edge_data.items()
                        if k not in ("relation", "confidence", "evidence_quote")
                    },
                    "is_backbone": False,
                }
                paths.append([hop1_edge, hop2_edge])

            # Hop 2B: Query LadybugDB backbone for connections from this neighbor
            if self.overlay.kg is not None:
                backbone_edges = self._query_backbone_connections(
                    neighbor_name, neighbor_id
                )
                for b_edge in backbone_edges:
                    tgt_name = b_edge["target"]
                    tgt_id = b_edge["target_id"]

                    # Cycle prevention
                    if tgt_id in (start_node_id, neighbor_id) or tgt_name in (
                        start_name,
                        neighbor_name,
                    ):
                        continue

                    nodes_dict[tgt_id] = {
                        "id": tgt_id,
                        "name": tgt_name,
                        "type": b_edge.get("type", "BackboneNode"),
                        "is_ephemeral": False,
                    }
                    paths.append([hop1_edge, b_edge])

        return {
            "nodes": list(nodes_dict.values()),
            "paths": paths,
        }

    def _query_backbone_connections(
        self, entity_name: str, entity_id: str
    ) -> list[dict[str, Any]]:
        """Query LadybugDB backbone for 1-hop outgoing edges from a canonical entity."""
        if not hasattr(self.overlay.kg, "execute"):
            return []

        cypher = (
            "MATCH (a)-[r]->(b) "
            "WHERE toLower(a.name) = toLower($name) "
            "OR toLower(a.ticker) = toLower($name) "
            "OR a.id = $id "
            "RETURN a.name, type(r), b.name, b.id, labels(b), properties(r)"
        )

        results: list[dict[str, Any]] = []
        try:
            rows = self.overlay.kg.execute(cypher, {"name": entity_name, "id": entity_id})
            for row in rows:
                if not row or len(row) < 3:
                    continue
                src = str(row[0] or entity_name)
                rel = str(row[1] or "CONNECTED_TO")
                tgt = str(row[2] or "Unknown")
                b_id = str(row[3]) if len(row) > 3 and row[3] else stable_id("backbone", tgt)
                labels = row[4] if len(row) > 4 and isinstance(row[4], (list, tuple)) else []
                b_type = labels[0] if labels else "BackboneNode"
                props = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}

                results.append({
                    "source": src,
                    "source_id": entity_id,
                    "relation": rel,
                    "target": tgt,
                    "target_id": b_id,
                    "type": b_type,
                    "confidence": 1.0,
                    "evidence_quote": props.get("description", props.get("evidence", "")),
                    "properties": props,
                    "is_backbone": True,
                })
        except Exception:
            # Tolerates mock variations or non-standard Cypher returns
            pass

        return results


def format_provenance_ledger(paths: list[list[dict[str, Any]]]) -> str:
    """Format traversals into a deterministic text provenance ledger.

    Format:
    [Entity A] --(RELATION: Details)--> [Entity B] --(RELATION: Details)--> [Entity C]
    """
    if not paths:
        return "No provenance paths found."

    lines: list[str] = []
    for path in paths:
        if not path:
            continue
        chain_tokens: list[str] = [f"[{path[0]['source']}]"]
        for edge in path:
            rel = edge.get("relation", "CONNECTED_TO")
            details = (
                edge.get("evidence_quote")
                or edge.get("properties", {}).get("description")
                or edge.get("properties", {}).get("details")
                or ""
            ).strip()

            if details:
                # Truncate details if overly long
                if len(details) > 60:
                    details = details[:57] + "..."
                arrow = f"--({rel}: {details})-->"
            else:
                arrow = f"--({rel})-->"

            chain_tokens.append(f"{arrow} [{edge['target']}]")

        lines.append(" ".join(chain_tokens))

    # Sort for deterministic output
    return "\n".join(sorted(set(lines)))
