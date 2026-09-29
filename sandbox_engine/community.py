"""Graph community detection and profile generation for Cold-Start JIT Graph RAG.

Utilizes NetworkX Louvain modularity clustering to group connected suppliers,
competitors, and companies, extracting hub nodes and dominant relationships.
"""

from __future__ import annotations

import collections
from typing import Any, Union
import networkx as nx
from networkx.algorithms.community import louvain_communities


class CommunityDetector:
    """Detects modular communities and generates analytical profile briefs."""

    def __init__(self, graph: Union[nx.Graph, nx.DiGraph]) -> None:
        self.graph = graph

    def detect_communities(self, seed: int = 42) -> list[dict[str, Any]]:
        """Identify communities using Louvain modularity clustering.

        Returns a list of community dictionaries:
        - community_id: Integer index.
        - members: List of node IDs in the cluster.
        - hub_nodes: Top 3 nodes with highest degree centrality in the cluster.
        - dominant_relation: Most frequent relationship type inside the cluster.
        - size: Number of nodes in the cluster.
        """
        if len(self.graph) == 0:
            return []

        # Convert to undirected view for Louvain modularity
        undirected_g = self.graph.to_undirected(as_view=False)
        clusters = louvain_communities(undirected_g, seed=seed)

        # Sort clusters by size descending
        clusters = sorted(clusters, key=len, reverse=True)

        results: list[dict[str, Any]] = []
        for idx, members in enumerate(clusters, start=1):
            members_list = sorted(list(members), key=str)
            sub = self.graph.subgraph(members)

            # Calculate centrality inside the cluster subgraph
            sub_undirected = sub.to_undirected(as_view=True)
            centrality = nx.degree_centrality(sub_undirected)

            # Sort nodes by centrality descending, then degree
            sorted_nodes = sorted(
                centrality.keys(),
                key=lambda n: (centrality[n], sub_undirected.degree(n)),
                reverse=True,
            )

            # Extract top 3 hub nodes with names if present
            hub_ids = sorted_nodes[:3]
            hub_names = [
                self.graph.nodes[n].get("name", str(n))
                for n in hub_ids
            ]

            # Determine dominant relation within the cluster
            rel_counts: collections.Counter[str] = collections.Counter()
            for _, _, edge_data in sub.edges(data=True):
                rel = edge_data.get("relation") or edge_data.get("type") or "CONNECTED_TO"
                rel_counts[rel] += 1

            if rel_counts:
                dominant_relation = rel_counts.most_common(1)[0][0]
            else:
                dominant_relation = "CONNECTED_TO"

            results.append({
                "community_id": idx,
                "members": members_list,
                "hub_nodes": hub_names,
                "dominant_relation": dominant_relation,
                "size": len(members_list),
            })

        return results

    @staticmethod
    def generate_community_brief(community: dict[str, Any]) -> str:
        """Format a concise narrative summary of the community's structural role."""
        cid = community.get("community_id", 1)
        hubs = community.get("hub_nodes", [])
        hubs_str = ", ".join(hubs) if hubs else "None"
        dom_rel = community.get("dominant_relation", "CONNECTED_TO")
        size = community.get("size", len(community.get("members", [])))

        return (
            f"Cluster {cid} [Hubs: {hubs_str}]: "
            f"Dominant relation {dom_rel} across {size} entities"
        )
