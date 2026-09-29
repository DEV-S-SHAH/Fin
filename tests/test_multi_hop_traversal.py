"""Unit tests for Slice 4: Multi-Hop Hybrid Traversal and Synthesis."""

import unittest
from unittest.mock import MagicMock

from sandbox_engine.coldstart_schema import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionPayload,
)
from sandbox_engine.coldstart_synthesis import ColdStartSynthesizer
from sandbox_engine.stitch import InMemoryOverlayGraph, stitch_coldstart_payload
from sandbox_engine.traversal import HybridGraphTraverser, format_provenance_ledger


class TestMultiHopTraversal(unittest.TestCase):
    """Test suite for hybrid 2-hop traversal and synthesis prompt generation."""

    def test_hybrid_traversal_crosses_boundary(self):
        """Create ephemeral node RIVN sourcing from TSMC; mock backbone returning TSMC->Taiwan_Geopolitics."""
        mock_kg = MagicMock()
        # Mock LadybugDB returning (TSMC)-[:EXPOSED_TO]->(Taiwan_Geopolitics)
        mock_kg.execute.return_value = [
            ("TSMC", "EXPOSED_TO", "Taiwan_Geopolitics", "geo_1", ["RiskFactor"], {"description": "Geopolitical risk in Taiwan strait"}),
        ]
        mock_kg.has_company.side_effect = lambda ticker: ticker.upper() in ("TSMC", "RIVN")

        overlay = InMemoryOverlayGraph(kg_connection=mock_kg)

        payload = ExtractionPayload(
            entities=[
                ExtractedEntity(id="e1", name="Rivian", entity_type="Company"),
                ExtractedEntity(id="e2", name="Taiwan Semiconductor", entity_type="Supplier"),
            ],
            relationships=[
                ExtractedRelation(
                    source_id="e1",
                    target_id="e2",
                    relation="SOURCES_FROM",
                    confidence=0.96,
                    evidence_quote="Rivian sources semiconductors from TSMC.",
                )
            ],
        )

        stitch_coldstart_payload(overlay, payload, target_ticker="RIVN")
        traverser = HybridGraphTraverser(overlay)

        subgraph = traverser.traverse_neighborhood("RIVN", max_hops=2)

        # 1. Assert nodes include ephemeral RIVN, canonical TSMC, and backbone Taiwan_Geopolitics
        node_names = {n["name"] for n in subgraph["nodes"]}
        self.assertIn("RIVN", node_names)
        self.assertIn("TSMC", node_names)
        self.assertIn("Taiwan_Geopolitics", node_names)

        # 2. Check complete 2-hop path: RIVN -> TSMC -> Taiwan_Geopolitics
        paths = subgraph["paths"]
        two_hop_paths = [p for p in paths if len(p) == 2]
        self.assertTrue(len(two_hop_paths) >= 1)
        two_hop = two_hop_paths[0]

        self.assertEqual(two_hop[0]["source"], "RIVN")
        self.assertEqual(two_hop[0]["relation"], "SOURCES_FROM")
        self.assertEqual(two_hop[0]["target"], "TSMC")

        self.assertEqual(two_hop[1]["source"], "TSMC")
        self.assertEqual(two_hop[1]["relation"], "EXPOSED_TO")
        self.assertEqual(two_hop[1]["target"], "Taiwan_Geopolitics")
        self.assertTrue(two_hop[1]["is_backbone"])

    def test_cycle_prevention(self):
        """Verify cyclic edges A -> B -> A do not trigger infinite recursion."""
        overlay = InMemoryOverlayGraph()
        # Create a direct cycle in overlay graph
        overlay.graph.add_node("nodeA", name="Node A", entity_type="Company")
        overlay.graph.add_node("nodeB", name="Node B", entity_type="Supplier")
        overlay.graph.add_edge("nodeA", "nodeB", relation="SOURCES_FROM")
        overlay.graph.add_edge("nodeB", "nodeA", relation="COMPETES_WITH")

        traverser = HybridGraphTraverser(overlay)
        # Should terminate immediately without looping
        subgraph = traverser.traverse_neighborhood("Node A", max_hops=2)

        self.assertEqual(len(subgraph["nodes"]), 2)
        # All paths should have length <= 2 and no self-revisiting in a single path
        for path in subgraph["paths"]:
            self.assertLessEqual(len(path), 2)
            visited = [path[0]["source"]] + [edge["target"] for edge in path]
            self.assertEqual(len(visited), len(set(visited)), f"Cycle detected in path: {visited}")

    def test_provenance_ledger_formatting(self):
        """Verify paths format correctly into deterministic text chains with relation metadata."""
        paths = [
            [
                {
                    "source": "RIVN",
                    "relation": "SOURCES_FROM",
                    "target": "TSMC",
                    "evidence_quote": "Primary microcontrollers sourced from TSMC",
                },
                {
                    "source": "TSMC",
                    "relation": "EXPOSED_TO",
                    "target": "Taiwan_Strait_Risk",
                    "evidence_quote": "Geopolitical concentration in Hsinchu",
                },
            ]
        ]

        ledger = format_provenance_ledger(paths)

        expected = (
            "[RIVN] --(SOURCES_FROM: Primary microcontrollers sourced from TSMC)--> "
            "[TSMC] --(EXPOSED_TO: Geopolitical concentration in Hsinchu)--> [Taiwan_Strait_Risk]"
        )
        self.assertIn(expected, ledger)

    def test_synthesis_prompt_generation(self):
        """Verify synthesis prompt aggregates paths, quotes, and query without dropping context."""
        paths = [
            [
                {
                    "source": "RIVN",
                    "relation": "SOURCES_FROM",
                    "target": "TSMC",
                    "evidence_quote": "Silicon contract",
                }
            ]
        ]
        synthesizer = ColdStartSynthesizer()
        sys_prompt, user_prompt = synthesizer.generate_prompts(
            target_ticker="RIVN",
            query="Analyze supply chain risks for Rivian",
            paths=paths,
            filing_text="Rivian designs and manufactures electric adventure vehicles.",
        )

        # Verify system prompt requires the 5 core sections
        self.assertIn("1. Executive Summary & Thesis", sys_prompt)
        self.assertIn("2. Direct Dependencies (1-hop)", sys_prompt)
        self.assertIn("3. Second-Order Contagion", sys_prompt)
        self.assertIn("4. Capital Allocation & Margin Outlook", sys_prompt)
        self.assertIn("5. Verifiable Evidence Chain", sys_prompt)

        # Verify user prompt injects context and provenance
        self.assertIn("TARGET ENTITY: RIVN", user_prompt)
        self.assertIn("Analyze supply chain risks for Rivian", user_prompt)
        self.assertIn("[RIVN] --(SOURCES_FROM: Silicon contract)--> [TSMC]", user_prompt)
        self.assertIn("Rivian designs and manufactures", user_prompt)


if __name__ == "__main__":
    unittest.main()
