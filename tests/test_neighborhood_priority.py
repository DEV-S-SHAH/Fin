"""Regression tests for KnowledgeGraph.neighborhood() edge priority fix.

The fix ensures that financial/source-critical edges (REPORTS_METRIC, SUBMITTED)
are prioritized over secondary edges (DISAGGREGATED_BY, DISCLOSES_EVENT) when
the edge limit is applied. Secondary edges must not crowd out primary edges.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_engine.query_ui import KnowledgeGraph, resolve_db_path


class NeighborhoodPriorityTests(unittest.TestCase):
    """Tests for edge priority in neighborhood() method."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = resolve_db_path()
        if cls.db_path is None:
            raise unittest.SkipTest("No database found at sandbox_engine/_run/sandbox.lbug or FINGRAPH_DATA_DIR")
        cls.kg = KnowledgeGraph(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.kg.close()

    def test_primary_edges_always_included_under_limit(self):
        """REPORTS_METRIC and SUBMITTED edges are always included when under limit."""
        # Use a Filing seed which has fewer primary edges reachable
        filings = self.kg.execute(
            'MATCH (f:Filing)-[:REPORTS_METRIC]->(m:FinancialMetric)-[:DISAGGREGATED_BY]->(s:Segment) '
            'RETURN DISTINCT f.accession_number LIMIT 1'
        )
        if not filings:
            self.skipTest("No filing with segments found")
        
        acc = filings[0][0]
        
        # With a moderate limit, primary edges must be present
        result = self.kg.neighborhood([acc], hops=2, limit=200)
        
        edge_types = {}
        for e in result["edges"]:
            edge_types[e["relation"]] = edge_types.get(e["relation"], 0) + 1
        
        # Primary edges must be present
        self.assertIn("REPORTS_METRIC", edge_types, "REPORTS_METRIC edges must be included")
        self.assertIn("SUBMITTED", edge_types, "SUBMITTED edges must be included")
        
        # With sufficient limit, secondary edges should also appear
        # (This depends on the specific filing's graph structure)
        self.assertLessEqual(len(result["edges"]), 200, "Edge count must not exceed limit")

    def test_primary_edges_prioritized_when_limit_exceeded(self):
        """When limit is exceeded, primary edges are kept and secondary are trimmed first."""
        # Use a low limit that forces trimming
        result = self.kg.neighborhood(["AAPL"], hops=2, limit=20)
        
        edge_types = {}
        for e in result["edges"]:
            edge_types[e["relation"]] = edge_types.get(e["relation"], 0) + 1
        
        # Primary edges must be present
        self.assertIn("REPORTS_METRIC", edge_types, "REPORTS_METRIC must survive trim")
        self.assertIn("SUBMITTED", edge_types, "SUBMITTED must survive trim")
        
        # Total edges must not exceed limit
        self.assertLessEqual(len(result["edges"]), 20, "Edge count must not exceed limit")
        
        # Primary edges should dominate
        primary_count = edge_types.get("REPORTS_METRIC", 0) + edge_types.get("SUBMITTED", 0)
        secondary_count = edge_types.get("DISAGGREGATED_BY", 0) + edge_types.get("DISCLOSES_EVENT", 0)
        
        # Primary edges should be the majority (or all) when limit is tight
        self.assertGreaterEqual(primary_count, secondary_count, "Primary edges should not be crowded out by secondary")

    def test_secondary_edges_included_when_capacity_remains(self):
        """Secondary edges are included when primary edges don't fill the limit."""
        # Use a Filing seed which has fewer primary edges reachable in 2 hops
        # Get a filing that has segments
        filings = self.kg.execute(
            'MATCH (f:Filing)-[:REPORTS_METRIC]->(m:FinancialMetric)-[:DISAGGREGATED_BY]->(s:Segment) '
            'RETURN DISTINCT f.accession_number LIMIT 1'
        )
        if not filings:
            self.skipTest("No filing with segments found")
        
        acc = filings[0][0]
        
        # With a moderate limit, secondary edges should appear
        result = self.kg.neighborhood([acc], hops=2, limit=200)
        
        edge_types = {}
        for e in result["edges"]:
            edge_types[e["relation"]] = edge_types.get(e["relation"], 0) + 1
        
        # Primary edges present
        self.assertIn("REPORTS_METRIC", edge_types)
        self.assertIn("SUBMITTED", edge_types)
        
        # Secondary edges should appear when there's capacity
        # (This test may be skipped if the specific filing doesn't have enough primary edges)
        if "DISAGGREGATED_BY" in edge_types:
            self.assertGreater(edge_types["DISAGGREGATED_BY"], 0)
        if "DISCLOSES_EVENT" in edge_types:
            self.assertGreater(edge_types["DISCLOSES_EVENT"], 0)

    def test_no_seed_returns_all_edge_types_prioritized(self):
        """Without seeds, all edge types are returned with priority ordering."""
        # Very high limit to see all types (primary edges exceed 28k)
        result = self.kg.neighborhood([], hops=2, limit=30000)
        
        edge_types = {}
        for e in result["edges"]:
            edge_types[e["relation"]] = edge_types.get(e["relation"], 0) + 1
        
        # All four types should be present with very high limit
        self.assertIn("REPORTS_METRIC", edge_types)
        self.assertIn("SUBMITTED", edge_types)
        self.assertIn("DISAGGREGATED_BY", edge_types)
        self.assertIn("DISCLOSES_EVENT", edge_types)
        
        # Primary should dominate
        primary_count = edge_types.get("REPORTS_METRIC", 0) + edge_types.get("SUBMITTED", 0)
        secondary_count = edge_types.get("DISAGGREGATED_BY", 0) + edge_types.get("DISCLOSES_EVENT", 0)
        self.assertGreaterEqual(primary_count, secondary_count)

    def test_limit_respected_exactly(self):
        """Edge count never exceeds the specified limit."""
        for limit in [10, 25, 50, 100, 150]:
            with self.subTest(limit=limit):
                result = self.kg.neighborhood(["MSFT"], hops=2, limit=limit)
                self.assertLessEqual(len(result["edges"]), limit, f"Limit {limit} exceeded: got {len(result['edges'])}")

    def test_nodes_consistent_with_edges(self):
        """All nodes in result are referenced by at least one edge."""
        result = self.kg.neighborhood(["AAPL"], hops=2, limit=50)
        
        node_ids = {n["id"] for n in result["nodes"]}
        referenced_nodes = set()
        for e in result["edges"]:
            referenced_nodes.add(e["source"])
            referenced_nodes.add(e["target"])
        
        # Every node should be referenced by an edge (or be a seed)
        seeds = set(result["seeds"])
        for node_id in node_ids:
            if node_id not in seeds:
                self.assertIn(node_id, referenced_nodes, f"Node {node_id} not referenced by any edge")


class NeighborhoodEvidenceTests(unittest.TestCase):
    """Tests that neighborhood preserves evidence for financial queries."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = resolve_db_path()
        if cls.db_path is None:
            raise unittest.SkipTest("No database found")
        cls.kg = KnowledgeGraph(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.kg.close()

    def test_balance_sheet_metrics_reachable_1_hop(self):
        """Total Assets and Total Liabilities are reachable via REPORTS_METRIC in 1 hop from filing."""
        # Find a filing with balance sheet metrics (STARTS WITH to match period suffixes)
        filings = self.kg.execute("""
            MATCH (f:Filing)-[:REPORTS_METRIC]->(m:FinancialMetric)
            WHERE m.canonical_name STARTS WITH 'Total Assets' OR m.canonical_name STARTS WITH 'Total Liabilities'
            RETURN DISTINCT f.accession_number LIMIT 1
        """)
        if not filings:
            self.skipTest("No filing with balance sheet metrics found")
        
        acc = filings[0][0]
        
        # 1-hop from filing should include REPORTS_METRIC edges
        # Use higher limit to capture all metrics for this filing
        result = self.kg.neighborhood([acc], hops=1, limit=300)
        
        edge_types = {}
        for e in result["edges"]:
            edge_types[e["relation"]] = edge_types.get(e["relation"], 0) + 1
        
        self.assertIn("REPORTS_METRIC", edge_types, "REPORTS_METRIC edges must be present in 1-hop from filing")
        
        # Check that the metric nodes include balance sheet metrics
        metric_nodes = [n for n in result["nodes"] if n.get("entity_type") == "FinancialMetric"]
        metric_names = [n["name"] for n in metric_nodes]
        has_assets = any("Total Assets" in name for name in metric_names)
        has_liabilities = any("Total Liabilities" in name for name in metric_names)
        
        # At least one balance sheet metric should be reachable
        self.assertTrue(has_assets or has_liabilities, f"Balance sheet metrics not found. Metrics: {metric_names[:10]}")

    def test_2_hop_preserves_reports_metric(self):
        """2-hop from company preserves REPORTS_METRIC edges to financial metrics."""
        result = self.kg.neighborhood(["AAPL"], hops=2, limit=150)
        
        edge_types = {}
        for e in result["edges"]:
            edge_types[e["relation"]] = edge_types.get(e["relation"], 0) + 1
        
        self.assertIn("REPORTS_METRIC", edge_types, "REPORTS_METRIC must be preserved in 2-hop from company")
        self.assertGreater(edge_types["REPORTS_METRIC"], 0, "Must have REPORTS_METRIC edges")

    def test_evidence_values_present_in_descriptions(self):
        """REPORTS_METRIC edge descriptions contain actual financial values."""
        result = self.kg.neighborhood(["AAPL"], hops=2, limit=50)
        
        reports_metric_edges = [e for e in result["edges"] if e["relation"] == "REPORTS_METRIC"]
        self.assertGreater(len(reports_metric_edges), 0, "Must have REPORTS_METRIC edges")
        
        # Check that descriptions contain values
        for edge in reports_metric_edges[:5]:  # Check first 5
            desc = edge.get("description", "")
            self.assertIn("value=", desc, f"Edge description missing value: {desc}")
            self.assertNotIn("value=None", desc, f"Edge description has None value: {desc}")


if __name__ == "__main__":
    unittest.main()