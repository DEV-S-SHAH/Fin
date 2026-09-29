"""Latency SLA benchmarks and unit tests for Cold-Start JIT pipeline."""

import time
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


class TestColdStartLatency(unittest.TestCase):
    """Verify performance budgets and interactive streaming SLA."""

    def test_traversal_and_formatting_latency(self):
        """Assert total time for Traversal + Context Formatting is < 0.5s."""
        overlay = InMemoryOverlayGraph()
        # Seed graph with 50 ephemeral nodes and relationships
        entities = [
            ExtractedEntity(id=f"e_{i}", name=f"Entity_{i}", entity_type="Supplier")
            for i in range(50)
        ]
        relations = [
            ExtractedRelation(
                source_id="c1",
                target_id=f"e_{i}",
                relation="SOURCES_FROM",
                confidence=0.9,
                evidence_quote=f"Quote for entity {i}",
            )
            for i in range(50)
        ]
        payload = ExtractionPayload(
            entities=[ExtractedEntity(id="c1", name="Rivian", entity_type="Company")] + entities,
            relationships=relations[:30],
        )
        stitch_coldstart_payload(overlay, payload, target_ticker="RIVN")

        start = time.monotonic()
        traverser = HybridGraphTraverser(overlay)
        subgraph = traverser.traverse_neighborhood("RIVN", max_hops=2)
        ledger = format_provenance_ledger(subgraph["paths"])
        synthesizer = ColdStartSynthesizer()
        sys_p, usr_p = synthesizer.generate_prompts(
            target_ticker="RIVN",
            query="Analyze supply chain dependencies",
            paths=subgraph["paths"],
            filing_text="Sample filing text",
        )
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.5, f"Traversal + Formatting took {elapsed:.4f}s, expected < 0.5s")
        self.assertTrue(len(subgraph["paths"]) > 0)
        self.assertTrue(len(ledger) > 0)
        self.assertTrue(len(usr_p) > 0)

    def test_end_to_end_pipeline_interactive_sla(self):
        """Assert end-to-end pipeline (Fetch 1.0s + Extract 1.5s + Stitch 0.2s + Traversal 0.2s) streams initial token in < 4.0s."""
        # Setup controlled latency stages:
        # Stage 1: Fetch (1.0s)
        def mock_fetch(ticker, form_type="10-K", timeout=2.0):
            time.sleep(1.0)
            return "<html>Item 1. Business text</html>", {"ticker": ticker}

        # Stage 2: Extract (1.5s)
        def mock_extract(cleaned_text, target_ticker):
            time.sleep(1.5)
            return ExtractionPayload(
                entities=[
                    ExtractedEntity(id="c1", name=target_ticker, entity_type="Company"),
                    ExtractedEntity(id="s1", name="TSMC", entity_type="Supplier"),
                ],
                relationships=[
                    ExtractedRelation(
                        source_id="c1",
                        target_id="s1",
                        relation="SOURCES_FROM",
                        confidence=0.95,
                        evidence_quote="Sources from TSMC",
                    )
                ],
            )

        # Pipeline execution and timing to first token
        start_time = time.monotonic()

        # 1. Fetch
        raw_html, meta = mock_fetch("RIVN")

        # 2. Extract
        payload = mock_extract(raw_html, "RIVN")

        # 3. Stitch (simulate with 0.2s budget)
        overlay = InMemoryOverlayGraph()
        stitch_coldstart_payload(overlay, payload, target_ticker="RIVN")
        time.sleep(0.2)

        # 4. Traversal (simulate with 0.2s budget)
        traverser = HybridGraphTraverser(overlay)
        subgraph = traverser.traverse_neighborhood("RIVN", max_hops=2)
        time.sleep(0.2)

        # 5. Synthesize and capture initial token
        synthesizer = ColdStartSynthesizer()
        context = {
            "target_ticker": "RIVN",
            "query": "What are Rivian's supply risks?",
            "paths": subgraph["paths"],
            "filing_text": raw_html,
        }
        token_stream = synthesizer.stream_synthesis(context)
        first_token = next(token_stream)

        elapsed_to_first_token = time.monotonic() - start_time

        self.assertIsNotNone(first_token)
        self.assertTrue(len(first_token) > 0)
        # Verify initial token streamed well within interactive SLA (< 4.0s)
        self.assertLess(
            elapsed_to_first_token,
            4.0,
            f"Time to first token took {elapsed_to_first_token:.2f}s, exceeding target 4.0s SLA",
        )


if __name__ == "__main__":
    unittest.main()
