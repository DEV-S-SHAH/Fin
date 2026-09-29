"""Unit tests for Slice 5: Asynchronous Background Ingestion & Graph Community Detection."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
import networkx as nx

from unittest.mock import patch

from sandbox_engine.background import BackgroundIngestQueue
from sandbox_engine.community import CommunityDetector


class TestBackgroundCommunity(unittest.TestCase):
    """Test suite for background ingestion queuing and Louvain community detection."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="coldstart_staging_test_")
        self.staging_path = Path(self.temp_dir)
        self.queue = BackgroundIngestQueue(staging_dir=self.staging_path)
        sample_10k = (
            "<html><body>"
            "<div>Item 1. Business</div>"
            "<p>Tesla designs, develops, manufactures, and sells electric vehicles and energy systems.</p>"
            "<div>Item 1A. Risk Factors</div>"
            "</body></html>"
        )
        self.fetch_patcher = patch(
            "sandbox_engine.tier1_fetch.SECRuntimeFetcher.fetch_latest_filing_html",
            return_value=(sample_10k, {"form": "10-K"}),
        )
        self.fetch_patcher.start()

    def tearDown(self):
        self.fetch_patcher.stop()
        self.queue.shutdown(wait=True)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_background_queue_deduplication(self):
        """Enqueuing the same ticker twice only schedules one worker."""
        first_call = self.queue.enqueue_coldstart_sync("RIVN")
        second_call = self.queue.enqueue_coldstart_sync("RIVN")

        self.assertTrue(first_call, "First enqueue call must succeed")
        self.assertFalse(second_call, "Second enqueue call must be deduplicated and return False")

        # Allow worker to finish
        self.queue.shutdown(wait=True)

        # Re-enqueuing an already staged ticker without force must also return False
        third_call = self.queue.enqueue_coldstart_sync("RIVN")
        self.assertFalse(third_call, "Already staged ticker must be deduplicated")

    def test_atomic_staging_write(self):
        """Verify background job writes staging JSONL using atomic rename with zero partial files."""
        enqueued = self.queue.enqueue_coldstart_sync("TSLA", metadata={"source": "10-K", "hops": 2})
        self.assertTrue(enqueued)

        # Wait for worker completion
        self.queue.shutdown(wait=True)

        target_file = self.staging_path / "TSLA.jsonl"
        self.assertTrue(target_file.exists(), f"Staging file {target_file} was not created")

        # Verify contents
        with open(target_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 1)
        record = lines[0]
        self.assertEqual(record["ticker"], "TSLA")
        self.assertEqual(record["status"], "extracted")
        self.assertIn("timestamp", record)
        self.assertIn("payload", record)

        # Verify zero temporary files remain
        tmp_files = list(self.staging_path.glob(".*.tmp.*"))
        self.assertEqual(len(tmp_files), 0, f"Dangling temporary files found: {tmp_files}")

    def test_background_worker_error_handling(self):
        """Assert background worker captures fetch/extract exceptions and writes status: failed."""
        with patch(
            "sandbox_engine.tier1_fetch.SECRuntimeFetcher.fetch_latest_filing_html",
            side_effect=RuntimeError("SEC EDGAR Network Down"),
        ):
            enqueued = self.queue.enqueue_coldstart_sync("FAIL")
            self.assertTrue(enqueued)
            self.queue.shutdown(wait=True)

        target_file = self.staging_path / "FAIL.jsonl"
        self.assertTrue(target_file.exists())
        with open(target_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 1)
        record = lines[0]
        self.assertEqual(record["ticker"], "FAIL")
        self.assertEqual(record["status"], "failed")
        self.assertIn("SEC EDGAR Network Down", record["error"])

    def test_non_blocking_behavior(self):
        """Assert enqueue_coldstart_sync returns in < 10ms without waiting for worker completion."""
        start = time.monotonic()
        enqueued = self.queue.enqueue_coldstart_sync("GOOGL")
        elapsed = time.monotonic() - start

        self.assertTrue(enqueued)
        self.assertLess(elapsed, 0.010, f"enqueue call took {elapsed * 1000:.2f}ms, expected < 10ms")

    def test_louvain_community_detection(self):
        """Pass synthetic graph with 3 distinct clusters; assert 3 clusters returned and hubs ranked."""
        G = nx.Graph()

        # Cluster 1: Semiconductor Foundry (TSMC hub)
        semis = ["TSMC", "Foxconn", "Apple", "Broadcom"]
        for node in semis:
            G.add_node(node, name=node)
        G.add_edge("TSMC", "Apple", relation="SOURCES_FROM")
        G.add_edge("TSMC", "Broadcom", relation="SOURCES_FROM")
        G.add_edge("TSMC", "Foxconn", relation="SOURCES_FROM")
        G.add_edge("Foxconn", "Apple", relation="SOURCES_FROM")

        # Cluster 2: AI Hardware & EDA (NVDA hub)
        ai_hw = ["NVDA", "ASML", "Synopsys", "Cadence"]
        for node in ai_hw:
            G.add_node(node, name=node)
        G.add_edge("NVDA", "ASML", relation="SOURCES_FROM")
        G.add_edge("NVDA", "Synopsys", relation="SOURCES_FROM")
        G.add_edge("NVDA", "Cadence", relation="SOURCES_FROM")
        G.add_edge("Synopsys", "Cadence", relation="COMPETES_WITH")

        # Cluster 3: EV Automakers (Rivian / Lucid / Tesla)
        evs = ["RIVN", "LCID", "TSLA"]
        for node in evs:
            G.add_node(node, name=node)
        G.add_edge("RIVN", "LCID", relation="COMPETES_WITH")
        G.add_edge("RIVN", "TSLA", relation="COMPETES_WITH")
        G.add_edge("LCID", "TSLA", relation="COMPETES_WITH")

        # Sparse cross-community bridges
        G.add_edge("Apple", "NVDA", relation="COMPETES_WITH")
        G.add_edge("RIVN", "TSMC", relation="SOURCES_FROM")

        detector = CommunityDetector(G)
        communities = detector.detect_communities(seed=42)

        # Assert 3 distinct clusters detected
        self.assertEqual(len(communities), 3)

        # Find the semiconductor cluster (containing TSMC)
        semi_cluster = next((c for c in communities if "TSMC" in c["members"]), None)
        self.assertIsNotNone(semi_cluster)
        self.assertEqual(semi_cluster["hub_nodes"][0], "TSMC", "TSMC must rank as highest centrality hub")
        self.assertEqual(semi_cluster["dominant_relation"], "SOURCES_FROM")

        # Find the EV cluster (containing RIVN)
        ev_cluster = next((c for c in communities if "RIVN" in c["members"]), None)
        self.assertIsNotNone(ev_cluster)
        self.assertEqual(ev_cluster["dominant_relation"], "COMPETES_WITH")

    def test_community_brief_generation(self):
        """Assert output strings contain cluster ID, hub nodes, and dominant relation."""
        community = {
            "community_id": 1,
            "members": ["TSMC", "NVDA", "AAPL"],
            "hub_nodes": ["TSMC", "NVDA", "AAPL"],
            "dominant_relation": "SOURCES_FROM",
            "size": 3,
        }

        brief = CommunityDetector.generate_community_brief(community)

        # Verify required elements are present in brief
        self.assertIn("Cluster 1", brief)
        self.assertIn("TSMC", brief)
        self.assertIn("NVDA", brief)
        self.assertIn("AAPL", brief)
        self.assertIn("SOURCES_FROM", brief)


if __name__ == "__main__":
    unittest.main()
