"""Unit tests for Federated Multi-Database Architecture in FinGraph."""

import os
import shutil
import tempfile
import unittest

from sandbox_engine.federation import (
    DatabaseType,
    FederatedDatabaseManager,
    FederatedQueryCoordinator,
)


class TestFederatedMultiDatabase(unittest.TestCase):
    """Verify modular LadybugDB creation, query federation, and in-memory joining."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="fingraph_fed_test_")
        cls.manager = FederatedDatabaseManager(
            base_dir=cls.temp_dir,
            backbone_path="data/sandbox.lbug",
        )
        cls.coordinator = FederatedQueryCoordinator(cls.manager)

    @classmethod
    def tearDownClass(cls):
        cls.manager.close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_database_initialization(self):
        """Assert modular databases exist on disk with correct schemas."""
        for db_type in [
            DatabaseType.SUPPLY_CHAIN,
            DatabaseType.MACRO_ECONOMY,
            DatabaseType.GOVERNANCE,
        ]:
            db_path = self.manager.get_db_path(db_type)
            self.assertTrue(db_path.exists(), f"Expected database file at {db_path}")

            conn = self.manager.get_connection(db_type)
            self.assertIsNotNone(conn, f"Expected active connection for {db_type}")

    def test_query_classification(self):
        """Assert query classifier routes topics to correct database combinations."""
        # 1. Supply Chain only
        dbs1 = self.coordinator.classify_required_databases("Who are the foundries and suppliers for Nvidia?")
        self.assertIn(DatabaseType.SEC_FILINGS, dbs1)
        self.assertIn(DatabaseType.SUPPLY_CHAIN, dbs1)
        self.assertNotIn(DatabaseType.GOVERNANCE, dbs1)

        # 2. Macro Economy only
        dbs2 = self.coordinator.classify_required_databases("How does datacenter CapEx impact US GDP growth?")
        self.assertIn(DatabaseType.SEC_FILINGS, dbs2)
        self.assertIn(DatabaseType.MACRO_ECONOMY, dbs2)
        self.assertNotIn(DatabaseType.GOVERNANCE, dbs2)

        # 3. Governance only
        dbs3 = self.coordinator.classify_required_databases("What leadership transitions and executive changes took place?")
        self.assertIn(DatabaseType.SEC_FILINGS, dbs3)
        self.assertIn(DatabaseType.GOVERNANCE, dbs3)

        # 4. Comprehensive Cross-domain query
        dbs4 = self.coordinator.classify_required_databases(
            "Show who are the suppliers for Nvidia, how this impacts Apple and Tesla, and how this impacts US GDP and management changes."
        )
        self.assertIn(DatabaseType.SEC_FILINGS, dbs4)
        self.assertIn(DatabaseType.SUPPLY_CHAIN, dbs4)
        self.assertIn(DatabaseType.MACRO_ECONOMY, dbs4)
        self.assertIn(DatabaseType.GOVERNANCE, dbs4)

    def test_federated_cross_database_join(self):
        """Execute a cross-domain query and verify in-memory join across all 4 databases."""
        query = (
            "Who are the suppliers for Nvidia, how does this affect Apple and Tesla, "
            "what is the transmission to US GDP, and what management changes occurred?"
        )
        result = self.coordinator.execute_federated_query(query, target_ticker="NVDA")

        self.assertGreater(len(result.nodes), 10, "Expected composite nodes from multiple databases")
        self.assertGreater(len(result.edges), 8, "Expected joined edges across domains")
        self.assertGreater(len(result.evidence), 5, "Expected evidence entries across databases")

        # 1. Check Supply Chain Nodes & Suppliers
        node_names = {n.name for n in result.nodes}
        self.assertIn("TSMC", node_names)
        self.assertIn("Samsung Electronics", node_names)
        self.assertIn("Foxconn (Hon Hai)", node_names)

        # 2. Check Macro Economy Nodes
        self.assertIn("US_GDP", node_names)
        self.assertIn("AI_Datacenter_CapEx_Cycle", node_names)

        # 3. Check Governance Nodes & Transitions
        exec_names = {n.name for n in result.nodes if n.entity_type == "Executive"}
        self.assertIn("Tim Cook", exec_names)
        self.assertIn("Vaibhav Taneja", exec_names)

        # 4. Check Inter-Database Edges
        edge_relations = {(e.source, e.target, e.relation) for e in result.edges}
        # Bridge edge between TSMC and AAPL
        self.assertIn(("SUP_TSMC", "AAPL", "CAPACITY_COMPETITION"), edge_relations)
        # Inter-company customer links (TSLA -> NVDA, MSFT -> NVDA)
        self.assertIn(("TSLA", "NVDA", "CUSTOMER_OF"), edge_relations)
        self.assertIn(("MSFT", "NVDA", "CUSTOMER_OF"), edge_relations)

        # 5. Check Strict Provenance Tags
        provenance_tags = {ev.provenance for ev in result.evidence}
        self.assertIn("STATED", provenance_tags)
        self.assertIn("EXTERNAL", provenance_tags)

        # 6. Verify Answer Text Grounding
        self.assertIn("TSMC", result.answer_text)
        self.assertIn("US GDP", result.answer_text)
        self.assertIn("Tim Cook", result.answer_text)
        self.assertIn("Vaibhav Taneja", result.answer_text)


if __name__ == "__main__":
    unittest.main()
