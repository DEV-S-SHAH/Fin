"""Tests for the display payload's identity rules in :mod:`sandbox_engine.query_ui`.

The renderer draws one box per ``nodes`` entry, labelled ``name``, and one arrow
per ``edges`` entry. So a payload that repeats an id draws a duplicated box, and
a payload whose nodes share a name draws several indistinguishable boxes. Both
are visible defects; only one of them is fixed by merging.

Run with::

    .venv/bin/python -m unittest sandbox_engine.test_graph_payload
"""

from __future__ import annotations

import unittest

from sandbox_engine.query_ui import merge_edges, merge_nodes


class MergeNodesTest(unittest.TestCase):
    def test_same_id_is_emitted_once(self) -> None:
        nodes = [
            {"id": "AAPL", "name": "Apple Inc"},
            {"id": "AAPL", "name": "Apple Inc"},
        ]
        self.assertEqual([n["id"] for n in merge_nodes(nodes)], ["AAPL"])

    def test_keeps_the_richer_description(self) -> None:
        nodes = [
            {"id": "AAPL", "name": "Apple Inc", "description": "Ticker: AAPL, CIK: 0000320193"},
            {"id": "AAPL", "name": "Apple Inc", "description": ""},
        ]
        merged = merge_nodes(nodes)
        self.assertEqual(len(merged), 1)
        self.assertIn("CIK", merged[0]["description"])

    def test_same_name_does_not_merge_distinct_entities(self) -> None:
        # Six issuers sharing one legal name must stay six nodes. Merging on
        # name is the data-loss case this whole module exists to prevent.
        nodes = [{"id": t, "name": "Apple Inc"} for t in ("AAPL", "NVDA", "MSFT", "TSLA", "META", "AMZN")]
        merged = merge_nodes(nodes)
        self.assertEqual(len(merged), 6)
        self.assertEqual({n["id"] for n in merged}, {"AAPL", "NVDA", "MSFT", "TSLA", "META", "AMZN"})

    def test_colliding_names_are_disambiguated_by_hint(self) -> None:
        nodes = [
            {"id": "AAPL", "name": "Apple Inc", "label_hint": "AAPL"},
            {"id": "NVDA", "name": "Apple Inc", "label_hint": "NVDA"},
        ]
        merged = merge_nodes(nodes)
        self.assertEqual(sorted(n["name"] for n in merged), ["Apple Inc · AAPL", "Apple Inc · NVDA"])

    def test_unique_names_are_left_alone_and_hint_is_stripped(self) -> None:
        merged = merge_nodes([{"id": "AAPL", "name": "Apple Inc", "label_hint": "AAPL"}])
        self.assertEqual(merged[0]["name"], "Apple Inc")
        self.assertNotIn("label_hint", merged[0])

    def test_colliding_names_without_a_hint_fall_back_to_id(self) -> None:
        merged = merge_nodes([{"id": "x1", "name": "10-K FY2026 (FY)"}, {"id": "x2", "name": "10-K FY2026 (FY)"}])
        self.assertEqual(sorted(n["name"] for n in merged), ["10-K FY2026 (FY) · x1", "10-K FY2026 (FY) · x2"])

    def test_every_returned_label_is_unique(self) -> None:
        nodes = [
            {"id": "a", "name": "Apple Inc", "label_hint": "AAPL"},
            {"id": "b", "name": "Apple Inc", "label_hint": "NVDA"},
            {"id": "c", "name": "Tesla, Inc", "label_hint": "TSLA"},
            {"id": "d", "name": "10-K FY2026 (FY)", "label_hint": "NVDA"},
            {"id": "e", "name": "10-K FY2026 (FY)", "label_hint": "AMZN"},
        ]
        names = [n["name"] for n in merge_nodes(nodes)]
        self.assertEqual(len(names), len(set(names)))

    def test_does_not_mutate_the_input(self) -> None:
        nodes = [{"id": "AAPL", "name": "Apple Inc", "label_hint": "AAPL"}]
        merge_nodes(nodes)
        self.assertEqual(nodes[0]["label_hint"], "AAPL")

    def test_node_without_an_id_is_dropped(self) -> None:
        self.assertEqual(merge_nodes([{"name": "orphan"}]), [])


class MergeEdgesTest(unittest.TestCase):
    def test_repeated_triples_collapse(self) -> None:
        edges = [
            {"source": "a", "target": "b", "relation": "R", "description": "first"},
            {"source": "a", "target": "b", "relation": "R", "description": "again"},
        ]
        merged = merge_edges(edges)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["description"], "first")

    def test_distinct_relations_are_kept(self) -> None:
        edges = [
            {"source": "a", "target": "b", "relation": "R1"},
            {"source": "a", "target": "b", "relation": "R2"},
            {"source": "b", "target": "a", "relation": "R1"},
        ]
        self.assertEqual(len(merge_edges(edges)), 3)

    def test_every_returned_triple_is_unique(self) -> None:
        edges = [{"source": s, "target": t, "relation": "R"}
                 for s in "ab" for t in "cd"] * 3
        merged = merge_edges(edges)
        triples = [(e["source"], e["target"], e["relation"]) for e in merged]
        self.assertEqual(len(triples), len(set(triples)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
