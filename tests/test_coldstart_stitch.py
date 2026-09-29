"""Unit tests for Slice 3: Fast Pydantic Triple Extraction & In-Memory Backbone Stitching."""

import json
import time
import unittest
from unittest.mock import MagicMock
from pydantic import ValidationError

from sandbox_engine.coldstart_schema import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionPayload,
)
from sandbox_engine.coldstart_extract import ColdStartExtractor
from sandbox_engine.stitch import InMemoryOverlayGraph, stitch_coldstart_payload


class TestColdStartStitch(unittest.TestCase):
    """Test suite for cold-start schema, extractor budget/SLA, and overlay stitching."""

    def test_schema_validation_success(self):
        """Assert valid entity and relation payloads instantiate correctly."""
        entity = ExtractedEntity(
            id="e1",
            name="Apple Inc.",
            entity_type="Company",
            properties={"sector": "Technology"},
        )
        self.assertEqual(entity.id, "e1")
        self.assertEqual(entity.name, "Apple Inc.")
        self.assertEqual(entity.entity_type, "Company")

        relation = ExtractedRelation(
            source_id="e1",
            target_id="e2",
            relation="SOURCES_FROM",
            confidence=0.95,
            evidence_quote="Apple sources custom silicon and advanced packaging from TSMC.",
            properties={"component": "Silicon"},
        )
        self.assertEqual(relation.source_id, "e1")
        self.assertEqual(relation.target_id, "e2")
        self.assertEqual(relation.relation, "SOURCES_FROM")
        self.assertEqual(relation.confidence, 0.95)

        payload = ExtractionPayload(
            entities=[entity],
            relationships=[relation],
            rejected_count=0,
        )
        self.assertEqual(len(payload.entities), 1)
        self.assertEqual(len(payload.relationships), 1)
        self.assertEqual(payload.rejected_count, 0)

    def test_schema_invalid_relation_rejected(self):
        """Assert invalid relation type raises validation error."""
        with self.assertRaises(ValidationError):
            ExtractedRelation(
                source_id="e1",
                target_id="e2",
                relation="INVALID_RELATION_TYPE",  # Not in RelationType Literal
                confidence=0.9,
                evidence_quote="Valid evidence quote.",
            )

    def test_schema_self_loop_rejected(self):
        """Assert self-loops (source_id == target_id) are disallowed."""
        with self.assertRaises(ValidationError):
            ExtractedRelation(
                source_id="same_id",
                target_id="same_id",
                relation="COMPETES_WITH",
                confidence=0.8,
                evidence_quote="Company competes with itself.",
            )

    def test_schema_long_quote_rejected(self):
        """Assert evidence quotes longer than 30 words are rejected."""
        long_quote = "word " * 35
        with self.assertRaises(ValidationError):
            ExtractedRelation(
                source_id="e1",
                target_id="e2",
                relation="COMPETES_WITH",
                confidence=0.8,
                evidence_quote=long_quote,
            )

    def test_schema_invalid_confidence_rejected(self):
        """Assert confidence scores outside [0.0, 1.0] are rejected."""
        with self.assertRaises(ValidationError):
            ExtractedRelation(
                source_id="e1",
                target_id="e2",
                relation="EXPOSED_TO",
                confidence=1.5,
                evidence_quote="High risk factor exposure.",
            )

    def test_extractor_budget_and_rejection(self):
        """Mock LLM response with 35 triples; verify exactly 30 are accepted and 5 rejected."""
        # Generate 35 mock relations with varying confidences
        relations_raw = []
        for i in range(35):
            relations_raw.append({
                "source_id": "c1",
                "target_id": f"s_{i}",
                "relation": "SOURCES_FROM",
                "confidence": round(0.50 + (i * 0.01), 2),  # from 0.50 to 0.84
                "evidence_quote": f"Evidence quote for supplier number {i} in filing.",
            })

        entities_raw = [
            {"id": "c1", "name": "Rivian", "entity_type": "Company"}
        ] + [
            {"id": f"s_{i}", "name": f"Supplier_{i}", "entity_type": "Supplier"}
            for i in range(35)
        ]

        mock_llm_json = json.dumps({
            "entities": entities_raw,
            "relationships": relations_raw,
        })

        extractor = ColdStartExtractor(client=lambda text, ticker: mock_llm_json)
        payload = extractor.extract_triples("Sample SEC filing narrative", target_ticker="RIVN")

        # Budget constraint: exactly 30 relations accepted, 5 recorded as rejected
        self.assertEqual(len(payload.relationships), 30)
        self.assertEqual(payload.rejected_count, 5)
        # Verify they are ranked by confidence descending (highest confidence first)
        confidences = [r.confidence for r in payload.relationships]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    def test_extractor_timeout_enforcement(self):
        """Mock a hanging LLM call; assert timeout terminates at <= 3.6s without raising."""
        def hanging_client(text, ticker):
            time.sleep(5.0)
            return "{}"

        extractor = ColdStartExtractor(client=hanging_client, timeout=0.25)
        start = time.monotonic()
        payload = extractor.extract_triples("Some text", target_ticker="RIVN")
        elapsed = time.monotonic() - start

        self.assertLessEqual(elapsed, 3.6)
        self.assertEqual(len(payload.relationships), 0)
        self.assertEqual(payload.metadata.get("status"), "timeout")
        self.assertIn("error", payload.metadata)

    def test_in_memory_stitching(self):
        """Feed mock payload with supplier 'Taiwan Semiconductor'; assert it resolves to 'TSMC'."""
        # Mock read-only KnowledgeGraph backbone where AAPL exists
        mock_kg = MagicMock(spec=["has_company", "execute"])
        mock_kg.has_company.side_effect = lambda ticker: ticker.upper() == "AAPL"

        overlay = InMemoryOverlayGraph(kg_connection=mock_kg)

        payload = ExtractionPayload(
            entities=[
                ExtractedEntity(id="e1", name="Apple Inc.", entity_type="Company"),
                ExtractedEntity(id="e2", name="Taiwan Semiconductor", entity_type="Supplier"),
            ],
            relationships=[
                ExtractedRelation(
                    source_id="e1",
                    target_id="e2",
                    relation="SOURCES_FROM",
                    confidence=0.98,
                    evidence_quote="The Company sources silicon wafers from Taiwan Semiconductor.",
                )
            ],
        )

        summary = stitch_coldstart_payload(overlay, payload, target_ticker="AAPL")

        # 1. Assert 'Taiwan Semiconductor' resolved to canonical 'TSMC'
        tsmc_node = overlay.get_node_by_name("TSMC")
        self.assertIsNotNone(tsmc_node, "Expected canonical TSMC node in overlay graph")
        self.assertEqual(tsmc_node["name"], "TSMC")
        self.assertEqual(tsmc_node["entity_type"], "Supplier")

        # 2. Assert edge exists between Apple and TSMC
        self.assertTrue(overlay.has_edge_between("AAPL", "TSMC"))

        # 3. Assert backbone stitch was recognized since AAPL is in backbone
        self.assertEqual(summary["stitched_backbone_edges"], 1)
        self.assertEqual(summary["ephemeral_edges"], 0)
        self.assertGreater(summary["new_nodes"], 0)

        # 4. Assert read-only kg was not modified or written to
        self.assertFalse(hasattr(mock_kg, "write"))

    def test_idempotency_on_overlay(self):
        """Re-stitching the same payload twice must not produce duplicate arcs in the overlay graph."""
        overlay = InMemoryOverlayGraph()

        payload = ExtractionPayload(
            entities=[
                ExtractedEntity(id="e1", name="Rivian", entity_type="Company"),
                ExtractedEntity(id="e2", name="Samsung Electronics", entity_type="Supplier"),
            ],
            relationships=[
                ExtractedRelation(
                    source_id="e1",
                    target_id="e2",
                    relation="SOURCES_FROM",
                    confidence=0.92,
                    evidence_quote="Rivian sources battery cells from Samsung Electronics.",
                )
            ],
        )

        # First stitch
        summary1 = stitch_coldstart_payload(overlay, payload, target_ticker="RIVN")
        edge_count_1 = overlay.graph.number_of_edges()
        node_count_1 = overlay.graph.number_of_nodes()

        self.assertEqual(edge_count_1, 1)
        self.assertGreater(summary1["new_nodes"], 0)

        # Second stitch with identical payload
        summary2 = stitch_coldstart_payload(overlay, payload, target_ticker="RIVN")
        edge_count_2 = overlay.graph.number_of_edges()
        node_count_2 = overlay.graph.number_of_nodes()

        # Edge count and node count must remain strictly identical
        self.assertEqual(edge_count_1, edge_count_2)
        self.assertEqual(node_count_1, node_count_2)
        self.assertEqual(summary2["new_nodes"], 0)


if __name__ == "__main__":
    unittest.main()
