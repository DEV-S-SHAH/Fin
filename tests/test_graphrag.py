"""Test suite for the domain-agnostic GraphRAG pipeline.

The LLM is replaced by :class:`ScriptedLLM`, which returns realistic model
output (fenced JSON, prose wrappers, occasional noise). That exercises the real
extraction, resolution, ingestion, and answering code paths without network
access, and lets the tests assert on the shape a real model produces.
"""

from __future__ import annotations

import json
from collections import Counter
import io
import os
import re
import shutil
import threading
import urllib.error
import urllib.request
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphrag.config import GraphRAGConfig
from graphrag.document import chunk_document, load_pdf, tokenize
from graphrag.extract import (
    EXTRACTION_SCHEMA,
    IDENTIFY_SCHEMA,
    _slug_text,
    extract_chunk,
    identify_entities,
    is_grounded,
    parse_extraction,
)
from graphrag.ingest import IngestReport, ingest_pdf
from graphrag import llm
from graphrag.llm import HeuristicClient, LLMClient, LLMError, parse_json_object
from graphrag.qa import Answer, _lexical_seeds, ask, link_entities
from graphrag.resolve import clean_name, is_usable_name, merge_observation, slugify
from graphrag.store import Edge, Entity, GraphStore, Subgraph

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


class ScriptedLLM(LLMClient):
    """A stand-in model with a fixed, domain-specific knowledge payload."""

    name = "scripted"
    is_model = True

    BIOLOGY = {
        "entities": [
            {"name": "Riftia pachyptila", "type": "organism", "description": "A giant tubeworm lacking a mouth and gut."},
            {"name": "Candidatus Endoriftia", "type": "bacterium", "description": "Symbiotic bacteria living in the trophosome."},
            {"name": "Trophosome", "type": "organ", "description": "A specialised organ housing symbiotic bacteria."},
            {"name": "Hydrogen sulfide", "type": "compound", "description": "The energy source oxidised by vent bacteria."},
            {"name": "Rimicaris exoculata", "type": "organism", "description": "A blind shrimp grazing bacterial mats."},
        ],
        "relationships": [
            {"source": "Riftia pachyptila", "target": "Candidatus Endoriftia", "relation": "hosts", "description": "Carries the bacteria in its trophosome."},
            {"source": "Candidatus Endoriftia", "target": "Hydrogen sulfide", "relation": "oxidises", "description": "Uses hydrogen sulfide as an energy source."},
            {"source": "Candidatus Endoriftia", "target": "Trophosome", "relation": "lives_in", "description": "Occupies the trophosome organ."},
        ],
    }

    # A second, unrelated domain: proves nothing about the schema is fixed.
    COOKING = {
        "entities": [
            {"name": "Sourdough starter", "type": "preparation", "description": "A flour and water ferment of wild yeast."},
            {"name": "Wild yeast", "type": "organism", "description": "Yeast that ferments the starter."},
            {"name": "Dough", "type": "preparation", "description": "Starter combined with flour, water, and salt."},
        ],
        "relationships": [
            {"source": "Sourdough starter", "target": "Wild yeast", "relation": "fermented_by", "description": "The starter ferments using wild yeast."},
            {"source": "Dough", "target": "Sourdough starter", "relation": "made_from", "description": "Dough is built from the starter."},
        ],
    }

    def __init__(
        self,
        payload: dict | None = None,
        fence: bool = True,
        identify: list[str] | None = None,
    ) -> None:
        self.payload = payload if payload is not None else self.BIOLOGY
        self.fence = fence
        # Names returned for the question-linking call. Defaults to every
        # entity in the payload, which is what a model asked to name the
        # entities of these passages would say.
        self.identify = identify if identify is not None else [
            e["name"] for e in self.payload["entities"]
        ]
        self.calls: list[tuple[str, str]] = []
        self.schemas: list[dict | None] = []

    def complete_json(
        self, system: str, user: str, schema: dict | None = None
    ) -> dict:
        self.calls.append((system, user))
        # A provider with constrained decoding is handed the requested shape;
        # record it so tests can assert the schemas are actually plumbed.
        self.schemas.append(schema)
        # Dispatch on the system prompt the way a real model would: the
        # identify step and the extraction step ask for different shapes.
        if "resolve a user question" in system:
            return {"entities": list(self.identify)}
        body = json.dumps(self.payload)
        if self.fence:
            body = f"Here is the result.\n```json\n{body}\n```"
        return parse_json_object(body)

    def complete_text(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if "Candidatus Endoriftia" in user:
            return (
                "Endoriftia oxidises hydrogen sulfide to produce organic carbon "
                "[E2] [E3]. It lives inside the trophosome of Riftia pachyptila [E1] [E4]."
            )
        return "The graph does not state that. [E1]"


def temp_store(config: GraphRAGConfig | None = None) -> tuple[GraphStore, str]:
    path = os.path.join(tempfile.mkdtemp(), "test.lbug")
    return GraphStore(path, config), path


# -- entity resolution ---------------------------------------------------


class ResolveTests(unittest.TestCase):
    def test_slug_is_lowercase_alphanumeric(self):
        for raw in ["Riftia pachyptila", "RIFTIA PACHYPTILA!", "Riftia  pachyptila",
                    "Riftia-pachyptila", "Riftia_pachyptila"]:
            self.assertEqual(slugify(raw), "riftiapachyptila", raw)

    def test_slug_folds_accents_and_punctuation(self):
        self.assertEqual(slugify("Café Müller"), "cafemuller")

    def test_slug_keeps_distinct_names_distinct(self):
        # A trailing descriptor is identity-bearing; stripping it would merge
        # genuinely different entities.
        self.assertNotEqual(slugify("Arctic Ocean"), slugify("Pacific Ocean"))

    def test_possessive_and_parenthetical_stripped(self):
        self.assertEqual(slugify("Bell's telescope"), "bellstelescope")
        self.assertEqual(slugify("Mercury (planet)"), "mercury")

    def test_honorific_stripped_but_regnal_title_kept(self):
        self.assertEqual(slugify("Dr Ada Lovelace"), "adalovelace")
        self.assertEqual(slugify("Queen Elizabeth"), "queenelizabeth")

    def test_empty_slug_rejected(self):
        for raw in ["", "   ", "...", "!!!", "\u2014"]:
            self.assertEqual(slugify(raw), "", raw)
        # A function word still slugs, but must never become a node.
        self.assertFalse(is_usable_name("the"))
        self.assertFalse(is_usable_name("and"))

    def test_placeholders_rejected(self):
        for raw in ["unknown", "N/A", "none", "TBD", "placeholder", "various"]:
            self.assertFalse(is_usable_name(raw), raw)

    def test_merge_never_overwrites_with_blank(self):
        merged = merge_observation(
            {"name": "X", "entity_type": "organism", "description": "a real description"},
            {"name": "", "entity_type": "", "description": ""},
        )
        self.assertEqual(merged["name"], "X")
        self.assertEqual(merged["description"], "a real description")

    def test_clean_name_preserves_internal_spacing(self):
        self.assertEqual(clean_name("  Hello   World  "), "Hello World")


# -- chunking ------------------------------------------------------------


class ChunkTests(unittest.TestCase):
    def _doc(self, words: int) -> "object":
        from graphrag.document import Document

        pages = []
        per_page = max(1, words // 3)
        made = 0
        while made < words:
            take = min(per_page, words - made)
            pages.append(" ".join(f"w{made + i}" for i in range(take)))
            made += take
        return Document(path=Path("mem.pdf"), pages=pages)

    def test_tokenizer_counts_words_and_punctuation(self):
        # Punctuation is tokenised separately from words.
        self.assertEqual(tokenize("Hello, world."), ["Hello", ",", "world", "."])
        # Internal hyphens/apostrophes keep a compound together.
        self.assertEqual(tokenize("a-b c'd"), ["a-b", "c'd"])

    def test_window_size_and_overlap(self):
        doc = self._doc(2000)
        chunks = chunk_document(doc, chunk_tokens=800, overlap_tokens=100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertEqual(chunk.token_count, 800)
        self.assertEqual(chunks[0].token_count, 800)

    def test_consecutive_chunks_overlap(self):
        doc = self._doc(2000)
        chunks = chunk_document(doc, chunk_tokens=800, overlap_tokens=100)
        # 100-token overlap: the tail of chunk N reappears at the head of N+1.
        tail = set(tokenize(chunks[0].text)[-100:])
        head = set(tokenize(chunks[1].text)[:100])
        self.assertTrue(tail & head)

    def test_stride_advances_by_chunk_minus_overlap(self):
        doc = self._doc(3000)
        chunks = chunk_document(doc, chunk_tokens=800, overlap_tokens=100)
        total = doc.token_count
        expected_windows = 1 + -(-(total - 800) // 700) if total > 800 else 1
        self.assertEqual(len(chunks), expected_windows)

    def test_every_token_is_covered(self):
        doc = self._doc(1500)
        chunks = chunk_document(doc, chunk_tokens=400, overlap_tokens=50)
        covered = " ".join(c.text for c in chunks)
        for marker in ("w0", f"w{doc.token_count - 1}"):
            self.assertIn(marker, covered)

    def test_page_attribution(self):
        doc = self._doc(300)
        chunks = chunk_document(doc, chunk_tokens=50, overlap_tokens=10)
        self.assertTrue(all(1 <= c.page_start <= c.page_end <= 3 for c in chunks))

    def test_invalid_overlap_rejected(self):
        doc = self._doc(100)
        with self.assertRaises(ValueError):
            chunk_document(doc, chunk_tokens=100, overlap_tokens=100)


# -- LLM JSON parsing ----------------------------------------------------


class JsonParsingTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_block(self):
        self.assertEqual(
            parse_json_object('text\n```json\n{"a": 2}\n```\nmore'), {"a": 2}
        )

    def test_prose_wrapped_object(self):
        self.assertEqual(
            parse_json_object('Sure! {"a": 3} hope that helps'), {"a": 3}
        )

    def test_top_level_array_wrapped(self):
        self.assertEqual(parse_json_object("[1, 2]"), {"items": [1, 2]})

    def test_garbage_raises(self):
        with self.assertRaises(LLMError):
            parse_json_object("no json here")

    def test_empty_raises(self):
        with self.assertRaises(LLMError):
            parse_json_object("")


# -- extraction validation ------------------------------------------------


class ExtractionValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = GraphRAGConfig()

    def test_drops_relationship_with_unknown_endpoint(self):
        payload = {
            "entities": [{"name": "Alpha", "type": "x", "description": "desc"}],
            "relationships": [
                {"source": "Alpha", "target": "Ghost", "relation": "r", "description": ""}
            ],
        }
        result = parse_extraction(payload, self.config)
        self.assertEqual(len(result.entities), 1)
        self.assertEqual(result.relationships, [])

    def test_drops_self_loop(self):
        payload = {
            "entities": [{"name": "Alpha", "type": "x", "description": "desc"}],
            "relationships": [
                {"source": "Alpha", "target": "Alpha", "relation": "r", "description": ""}
            ],
        }
        self.assertEqual(parse_extraction(payload, self.config).relationships, [])

    def test_drops_placeholder_entities(self):
        payload = {
            "entities": [
                {"name": "unknown", "type": "x", "description": "d"},
                {"name": "N/A", "type": "x", "description": "d"},
                {"name": "Real Thing", "type": "x", "description": "d"},
            ],
            "relationships": [],
        }
        result = parse_extraction(payload, self.config)
        self.assertEqual([e.name for e in result.entities], ["Real Thing"])

    def test_dedupes_entities_by_slug_within_chunk(self):
        payload = {
            "entities": [
                {"name": "Riftia pachyptila", "type": "organism", "description": ""},
                {"name": "riftia  pachyptila", "type": "", "description": "rich text here"},
            ],
            "relationships": [],
        }
        result = parse_extraction(payload, self.config)
        self.assertEqual(len(result.entities), 1)
        self.assertEqual(result.entities[0].description, "rich text here")

    def test_survives_hostile_payload(self):
        for payload in ({}, {"entities": None}, {"entities": "text"},
                        {"entities": [None, 3, "x"]}, {"relationships": {}}):
            result = parse_extraction(payload, self.config)
            self.assertEqual(result.entities, [])

    def test_respects_per_chunk_cap(self):
        config = GraphRAGConfig(max_entities_per_chunk=3)
        payload = {
            "entities": [
                {"name": f"Entity {i}", "type": "x", "description": "d"} for i in range(20)
            ],
            "relationships": [],
        }
        self.assertEqual(len(parse_extraction(payload, config).entities), 3)


# -- store: schema and idempotency ---------------------------------------


class StoreTests(unittest.TestCase):
    def test_buffer_pool_is_256mb(self):
        config = GraphRAGConfig()
        self.assertEqual(config.buffer_pool_size, 256 * 1024 * 1024)

    def test_schema_matches_specification(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            tables = store.tables()
            self.assertIn("Entity", tables)
            self.assertIn("CONNECTS", tables)
            # ensure_schema is idempotent.
            store.ensure_schema()
        finally:
            store.close()

    def test_node_and_edge_merge_are_idempotent(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            for _ in range(3):
                store.upsert_entity("Riftia pachyptila", "organism", "tubeworm")
                store.upsert_entity("Endoriftia", "bacterium", "symbiotic")
                store.upsert_edge("Riftia pachyptila", "Endoriftia", "hosts", "in trophosome")
            stats = store.stats()
            self.assertEqual(stats["nodes"], 2)
            self.assertEqual(stats["edges"], 1)
        finally:
            store.close()

    def test_same_pair_supports_distinct_relation_types(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Alpha", "t", "")
            store.upsert_entity("Beta", "t", "")
            store.upsert_edge("Alpha", "Beta", "hosts", "")
            store.upsert_edge("Alpha", "Beta", "produces", "")
            store.upsert_edge("Alpha", "Beta", "hosts", "")
            self.assertEqual(store.stats()["edges"], 2)
        finally:
            store.close()

    def test_edge_requires_both_endpoints(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Alpha", "t", "")
            self.assertIsNone(store.upsert_edge("Alpha", "Missing", "r", ""))
            self.assertEqual(store.stats()["edges"], 0)
        finally:
            store.close()

    def test_name_variants_merge_across_chunks(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Candidatus Endoriftia", "bacterium", "first")
            store.upsert_entity("candidatus  endoriftia!", "", "")
            store.upsert_entity("Candidatus Endoriftia", "bacterium", "a much richer description")
            stats = store.stats()
            self.assertEqual(stats["nodes"], 1)
            node = store.all_entities()[0]
            self.assertEqual(node.entity_type, "bacterium")
            self.assertIn("richer", node.description)
        finally:
            store.close()

    def test_two_hop_traversal_reaches_grandchild(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            for name in ("Alpha", "Beta", "Gamma"):
                store.upsert_entity(name, "t", "")
            store.upsert_edge("Alpha", "Beta", "links", "")
            store.upsert_edge("Beta", "Gamma", "links", "")
            one = store.neighborhood(["alpha"], hops=1)
            self.assertEqual(set(one.nodes), {"alpha", "beta"})
            two = store.neighborhood(["alpha"], hops=2)
            self.assertEqual(set(two.nodes), {"alpha", "beta", "gamma"})
        finally:
            store.close()

    def test_traversal_is_undirected(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Alpha", "t", "")
            store.upsert_entity("Beta", "t", "")
            store.upsert_edge("Alpha", "Beta", "links", "")
            graph = store.neighborhood(["beta"], hops=1)
            self.assertEqual(set(graph.nodes), {"alpha", "beta"})
        finally:
            store.close()

    def test_dense_hub_context_is_bounded(self):
        config = GraphRAGConfig(max_context_nodes=10, max_context_edges=20)
        store, _ = temp_store(config)
        try:
            store.ensure_schema()
            store.upsert_entity("Hub", "t", "")
            for i in range(60):
                store.upsert_entity(f"Leaf{i}", "t", "")
                store.upsert_edge("Hub", f"Leaf{i}", "links", "")
            graph = store.neighborhood(["hub"], hops=1)
            self.assertLessEqual(len(graph.nodes), config.max_context_nodes)
            self.assertLessEqual(len(graph.edges), config.max_context_edges)
            # The seed must survive, and no rendered edge may point at a node
            # that was trimmed away.
            self.assertIn("hub", graph.nodes)
            for edge in graph.edges:
                self.assertIn(edge.from_id, graph.nodes)
                self.assertIn(edge.to_id, graph.nodes)
        finally:
            store.close()

    def test_seed_edges_survive_trim_against_a_hub(self):
        # Trimming once kept whichever nodes sorted first by id, so a hub
        # reachable from the seeds could evict the seeds' own relationships.
        # The question's subject then reached the model as an isolated node and
        # produced a confident "no such relationship" answer.
        config = GraphRAGConfig(max_context_nodes=20, max_context_edges=40)
        store, _ = temp_store(config)
        try:
            store.ensure_schema()
            store.upsert_entity("gross margin", "financial metric", "")
            store.upsert_entity("tariff costs", "concept", "")
            store.upsert_edge("gross margin", "tariff costs", "partially_offset_by", "")
            store.upsert_entity("Tariffs", "concept", "")
            store.upsert_edge("Tariffs", "gross margin", "impacts", "")
            # A hub connected to the seeds, wide enough to swamp the budget.
            store.upsert_entity("Apple Inc", "organization", "")
            store.upsert_edge("Tariffs", "Apple Inc", "impact", "")
            for i in range(200):
                store.upsert_entity(f"Noise{i}", "t", "")
                store.upsert_edge("Apple Inc", f"Noise{i}", "links", "")
            graph = store.neighborhood(["tariffs", "grossmargin"], hops=2)
            self.assertLessEqual(len(graph.nodes), config.max_context_nodes)
            # Every relationship the seeds actually have must still be here.
            surviving = {
                (e.from_id, e.rel_type, e.to_id)
                for e in graph.edges
                if e.from_id in {"tariffs", "grossmargin"}
                or e.to_id in {"tariffs", "grossmargin"}
            }
            self.assertIn(
                ("grossmargin", "partially_offset_by", "tariffcosts"), surviving
            )
            self.assertIn(("tariffs", "impacts", "grossmargin"), surviving)
        finally:
            store.close()

    def test_relevance_order_prefers_nodes_near_a_seed(self):
        store, _ = temp_store()
        try:
            graph = Subgraph()
            # The far node sorts first by id, so only depth can explain the
            # order: a plain id sort would keep the far node instead.
            far = Entity("aaa_far", "far", "t", "")
            near = Entity("zzz_near", "near", "t", "")
            graph.nodes = {"aaa_far": far, "zzz_near": near}
            graph.depth = {"aaa_far": 2, "zzz_near": 1}
            self.assertEqual(
                [e.id for e in graph.relevance_order()], ["zzz_near", "aaa_far"]
            )
            # Equal depth falls back to id, so the order is deterministic.
            graph.depth = {"aaa_far": 1, "zzz_near": 1}
            self.assertEqual(
                [e.id for e in graph.relevance_order()], ["aaa_far", "zzz_near"]
            )
            # An unvisited node sorts last rather than winning by accident.
            graph.depth = {"zzz_near": 1}
            self.assertEqual(
                [e.id for e in graph.relevance_order()], ["zzz_near", "aaa_far"]
            )
        finally:
            store.close()

    def test_question_words_seed_when_the_model_names_nothing(self):
        # The identify prompt tells the model an empty list is correct when the
        # question names nothing findable, so a plainly-worded question could
        # link to nothing even though the graph held the answer.
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Principal executive officer", "person", "")
            store.upsert_entity("Principal financial officer", "person", "")
            store.upsert_entity(
                "Rule 13a-14 Certification of Chief Executive Officer", "document", ""
            )
            client = ScriptedLLM(identify=[])
            linked = link_entities(store, client, "Which officers signed the certifications?")
            names = [e.name for e in linked]
            self.assertTrue(names, "the fallback must find something")
            self.assertIn("Principal executive officer", names)
            self.assertTrue(
                any("Certification" in n for n in names), names
            )
        finally:
            store.close()

    def test_lexical_fallback_skips_question_words(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Hydrogen sulfide", "compound", "")
            seeds = _lexical_seeds(store, "What is hydrogen sulfide?")
            self.assertEqual([e.id for e in seeds], ["hydrogensulfide"])
        finally:
            store.close()

    def test_name_lookup_prefers_specific_match(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Endoriftia", "bacterium", "")
            store.upsert_entity("Candidatus Endoriftia", "bacterium", "")
            best = store.best_name_match("Endoriftia", limit=1)
            self.assertEqual(best[0].name, "Endoriftia")
        finally:
            store.close()


# -- end to end with a scripted model ------------------------------------


class EndToEndTests(unittest.TestCase):
    def test_ingest_then_answer_across_two_domains(self):
        for pdf, payload, needle in (
            ("marine_biology.pdf", ScriptedLLM.BIOLOGY, "Endoriftia"),
            ("cooking_methods.pdf", ScriptedLLM.COOKING, "Sourdough starter"),
        ):
            with self.subTest(pdf=pdf):
                store, _ = temp_store()
                client = ScriptedLLM(payload)
                try:
                    store.ensure_schema()
                    report = ingest_pdf(SAMPLES / pdf, store, client)
                    self.assertTrue(report.ok, report.failures)
                    self.assertGreater(report.nodes_added, 0)
                    self.assertGreater(report.edges_added, 0)

                    answer = ask(
                        f"What does {needle} relate to?",
                        store,
                        client,
                    )
                    self.assertTrue(answer.used_tags, answer.text)
                    self.assertTrue(answer.grounded, answer.note)
                finally:
                    store.close()

    def test_reingesting_is_idempotent(self):
        store, _ = temp_store()
        client = ScriptedLLM()
        try:
            store.ensure_schema()
            ingest_pdf(SAMPLES / "marine_biology.pdf", store, client)
            first = store.stats()
            for _ in range(3):
                ingest_pdf(SAMPLES / "marine_biology.pdf", store, client)
            self.assertEqual(store.stats()["nodes"], first["nodes"])
            self.assertEqual(store.stats()["edges"], first["edges"])
        finally:
            store.close()

    def test_question_outside_graph_is_reported_not_hallucinated(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            ingest_pdf(SAMPLES / "marine_biology.pdf", store, ScriptedLLM())
            client = ScriptedLLM(identify=[])
            answer = ask("Who won the 1998 FIFA World Cup final?", store, client)
            self.assertFalse(answer.grounded)
            self.assertIn("does not name any entity", answer.text)
        finally:
            store.close()

    def test_hallucinated_citation_is_flagged(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            ingest_pdf(SAMPLES / "marine_biology.pdf", store, ScriptedLLM())

            class Hallucinating(ScriptedLLM):
                def complete_text(self, system, user):
                    return "Invented claim [E999]."

            answer = ask("What does Endoriftia relate to?", store, Hallucinating())
            self.assertFalse(answer.grounded)
            self.assertIn("E999", answer.note)
        finally:
            store.close()

    def test_identify_entities_tolerates_dict_payload(self):
        class Dictish(ScriptedLLM):
            def complete_json(self, system, user, schema=None):
                return {"entities": [{"name": "Riftia pachyptila"}, "Endoriftia", ""]}

        self.assertEqual(
            identify_entities(Dictish(), "What about Riftia pachyptila?"),
            ["Riftia pachyptila", "Endoriftia"],
        )

    def test_failed_chunk_does_not_abort_ingestion(self):
        class Flaky(LLMClient):
            name = "flaky"

            def complete_json(self, system, user, schema=None):
                if "PASSAGE" in user and "Riftia" in user:
                    raise LLMError("simulated provider outage")
                return ScriptedLLM.complete_json(self, system, user, schema)

            def complete_text(self, system, user):
                return "ok [E1]"

        store, _ = temp_store()
        try:
            store.ensure_schema()
            report = ingest_pdf(SAMPLES / "marine_biology.pdf", store, Flaky())
            self.assertFalse(report.ok)
            self.assertTrue(report.failures)
        finally:
            store.close()

    def test_large_document_chunks_and_ingests(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            client = ScriptedLLM()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                from tools.make_fixture_pdf import build_pdf

                pages = [
                    f"Section {i} discusses Riftia pachyptila and Candidatus "
                    f"Endoriftia. " * 40
                    for i in range(6)
                ]
                handle.write(build_pdf(pages))
                temp_path = handle.name
            try:
                report = ingest_pdf(temp_path, store, client)
                self.assertGreater(report.chunks, 1, "expected multi-chunk ingestion")
                # Repeated content collapses onto the same nodes.
                self.assertLessEqual(store.stats()["nodes"], 6)
            finally:
                os.unlink(temp_path)
        finally:
            store.close()


# -- offline fallback ----------------------------------------------------


class HeuristicTests(unittest.TestCase):
    def test_finds_multiword_names(self):
        client = HeuristicClient()
        names = [s for s, _, _ in client._spans("Riftia pachyptila lives near vents.")]
        self.assertIn("Riftia pachyptila", names)

    def test_does_not_span_sentence_boundary(self):
        client = HeuristicClient()
        spans = [s for s, _, _ in client._spans("Heat reaches 350 degrees Celsius. Nearby vents glow.")]
        # No single span may straddle the full stop.
        for surface in spans:
            self.assertNotIn(".", surface)
            self.assertFalse("Celsius" in surface and "Nearby" in surface, surface)

    def test_reports_itself_as_non_model(self):
        self.assertFalse(HeuristicClient().is_model)

    def test_question_scan_excludes_trailing_verb(self):
        client = HeuristicClient()
        names = [s for s, _, _ in client._spans("What does Vesicles relate to?", 0)]
        self.assertEqual(names, ["Vesicles"])


class PdfTests(unittest.TestCase):
    def test_extracts_text_from_fixture(self):
        document = load_pdf(SAMPLES / "marine_biology.pdf")
        self.assertEqual(document.page_count, 3)
        self.assertIn("Riftia", document.text)
        self.assertGreater(document.token_count, 100)

    def test_missing_file_raises(self):
        from graphrag.document import PDFExtractionError

        with self.assertRaises(PDFExtractionError):
            load_pdf(SAMPLES / "does_not_exist.pdf")

    def test_non_pdf_raises(self):
        from graphrag.document import PDFExtractionError

        with self.assertRaises(PDFExtractionError):
            load_pdf(ROOT / "README.md")


class ConfigTests(unittest.TestCase):
    def test_stride_and_validation(self):
        config = GraphRAGConfig(chunk_tokens=800, chunk_overlap_tokens=100)
        self.assertEqual(config.stride_tokens, 700)
        with self.assertRaises(ValueError):
            GraphRAGConfig(chunk_tokens=100, chunk_overlap_tokens=100).stride_tokens

    def test_env_overrides(self):
        os.environ["GRAPHRAG_HOPS"] = "3"
        try:
            self.assertEqual(GraphRAGConfig.from_env().hops, 3)
        finally:
            del os.environ["GRAPHRAG_HOPS"]


class EnvFileTests(unittest.TestCase):
    """The ``.env`` loader that puts credentials in the environment.

    The precedence rule is the contract worth protecting: a real environment
    variable must survive a file that disagrees with it, or a one-off override
    would silently do nothing.
    """

    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved)))
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)

    def _write(self, text: str) -> str:
        path = os.path.join(self._dir, ".env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_parses_quotes_comments_and_export(self):
        from graphrag.envfile import load_env_file

        path = self._write(
            "# a comment\n"
            "\n"
            "OPENAI_API_KEY=nvapi-abc123\n"
            'OPENAI_BASE_URL="https://integrate.api.nvidia.com/v1"\n'
            "export GRAPHRAG_MODEL=openai/gpt-oss-20b\n"
            "GRAPHRAG_HOPS=2 # trailing comment\n"
        )
        applied = load_env_file(path)
        self.assertEqual(applied["OPENAI_API_KEY"], "nvapi-abc123")
        self.assertEqual(
            applied["OPENAI_BASE_URL"], "https://integrate.api.nvidia.com/v1"
        )
        self.assertEqual(applied["GRAPHRAG_MODEL"], "openai/gpt-oss-20b")
        self.assertEqual(applied["GRAPHRAG_HOPS"], "2")

    def test_existing_environment_wins(self):
        from graphrag.envfile import load_env_file

        os.environ["OPENAI_API_KEY"] = "from-the-shell"
        path = self._write("OPENAI_API_KEY=from-the-file\n")
        applied = load_env_file(path)
        self.assertEqual(applied, {})
        self.assertEqual(os.environ["OPENAI_API_KEY"], "from-the-shell")

    def test_missing_file_is_not_an_error(self):
        from graphrag.envfile import load_env_file

        missing = os.path.join(self._dir, "nope", ".env")
        self.assertEqual(load_env_file(missing), {})

    def test_env_file_var_selects_the_path(self):
        from graphrag.envfile import load_env_file

        path = self._write("GRAPHRAG_MODEL=from-env-file-var\n")
        os.environ["GRAPHRAG_ENV_FILE"] = path
        self.assertEqual(load_env_file()["GRAPHRAG_MODEL"], "from-env-file-var")

    def test_quoted_value_keeps_hash(self):
        from graphrag.envfile import load_env_file

        path = self._write('OPENAI_API_KEY="abc#123"\n')
        self.assertEqual(load_env_file(path)["OPENAI_API_KEY"], "abc#123")

    def test_malformed_lines_are_skipped(self):
        from graphrag.envfile import load_env_file

        path = self._write("not a pair\n=novalue\nGOOD=1\n")
        self.assertEqual(load_env_file(path), {"GOOD": "1"})

    def test_cli_entry_point_loads_the_file(self):
        from graphrag.cli import main

        path = self._write("GRAPHRAG_PROVIDER=heuristic\n")
        os.environ["GRAPHRAG_ENV_FILE"] = path
        os.environ.pop("GRAPHRAG_PROVIDER", None)
        missing = os.path.join(self._dir, "absent.lbug")
        with unittest.mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(main(["ask", "who?", "--db", missing]), 2)
        self.assertEqual(os.environ["GRAPHRAG_PROVIDER"], "heuristic")


class _StubOpenAI:
    """Installs a fake ``openai`` module so clients can be built without network.

    Used as a mixin by every provider contract test: the constructor kwargs are
    recorded too, so tests can assert *where* a request would have gone and with
    which credential.
    """

    _openai_module = None

    def _stub_openai(self, recorder: dict) -> None:
        import types

        class Completions:
            def create(self, **kwargs):
                recorder["kwargs"] = kwargs

                class Message:
                    content = '{"entities": [], "relationships": []}'

                class Choice:
                    message = Message()

                class Response:
                    choices = [Choice()]

                return Response()

        class Chat:
            completions = Completions()

        class OpenAI:
            def __init__(self, **kwargs):
                recorder["init"] = kwargs
                recorder["api_key"] = kwargs.get("api_key")
                recorder["base_url"] = kwargs.get("base_url")
                self.chat = Chat()

        module = types.ModuleType("openai")
        module.OpenAI = OpenAI
        self._openai_module = sys.modules.get("openai")
        sys.modules["openai"] = module

    def _restore_openai(self) -> None:
        if self._openai_module is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = self._openai_module

    def _no_key_env(self) -> None:
        # NVIDIA_API_KEY is cleared too: a developer machine will have it set,
        # and auto-detection would otherwise pick a real provider in tests
        # that intend to exercise the no-credentials path.
        for key in (
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "NVIDIA_API_KEY",
        ):
            os.environ.pop(key, None)


class ProviderContractTests(_StubOpenAI, unittest.TestCase):

    """Assert the real provider clients build the requests they promise.

    The network is never touched: a stub SDK records the request so the JSON
    mode contract can be checked directly.
    """

    def setUp(self):
        self._saved = {k: v for k, v in os.environ.items() if "API_KEY" in k}
        for key in list(self._saved):
            del os.environ[key]

    def tearDown(self):
        for key in list(os.environ):
            if "API_KEY" in key:
                del os.environ[key]
        os.environ.update(self._saved)

    def _stub_openai(self, recorder: dict) -> None:
        super()._stub_openai(recorder)

    def test_openai_uses_json_mode_for_structured_calls(self):
        from graphrag.llm import OpenAIChatClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["OPENAI_API_KEY"] = "test-key"
        client = OpenAIChatClient(model="gpt-test")
        payload = client.complete_json("system rules", "user passage")
        self.assertEqual(payload, {"entities": [], "relationships": []})
        kwargs = recorder["kwargs"]
        self.assertEqual(kwargs["model"], "gpt-test")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(
            kwargs["messages"],
            [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "user passage"},
            ],
        )

    def test_openai_omits_json_mode_for_prose_calls(self):
        from graphrag.llm import OpenAIChatClient

        recorder: dict = {}
        self._stub_openai(recorder)
        self.addCleanup(self._restore_openai)
        os.environ["OPENAI_API_KEY"] = "test-key"
        OpenAIChatClient().complete_text("system", "user")
        self.assertNotIn("response_format", recorder["kwargs"])

    def test_missing_key_raises_clear_error(self):
        from graphrag.llm import OpenAIChatClient

        with self.assertRaises(LLMError) as ctx:
            OpenAIChatClient()
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_resolve_client_falls_back_to_heuristic(self):
        from graphrag.llm import resolve_client

        # The auto path probes localhost:11434 before giving up, so on a
        # machine with a local model running this asserted the machine rather
        # than the resolver, and passed or failed with Ollama's state. The
        # probe is what the test has to control: refusing the connection is
        # what "no credentials present" means here.
        with unittest.mock.patch("graphrag.llm._ollama_reachable", return_value=False):
            self.assertEqual(resolve_client().name, "heuristic")

    def test_explicit_heuristic_provider(self):
        from graphrag.llm import resolve_client

        os.environ["OPENAI_API_KEY"] = "test-key"
        self.assertEqual(resolve_client("heuristic").name, "heuristic")

    def test_unknown_provider_rejected(self):
        from graphrag.llm import resolve_client

        with self.assertRaises(LLMError):
            resolve_client("not-a-provider")


class GeminiProviderTests(_StubOpenAI, unittest.TestCase):
    """Gemini reuses the OpenAI transport, so these lock in the differences."""

    def setUp(self):
        self._no_key_env()
        self.addCleanup(self._restore_openai)
        self.addCleanup(self._no_key_env)

    def test_gemini_merges_system_into_user_turn(self):
        from graphrag.llm import GeminiClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        client = GeminiClient(model="gemini-test")
        client.complete_json("system rules", "user passage")
        kwargs = recorder["kwargs"]
        # No system role on the OpenAI-compatible surface.
        self.assertEqual(len(kwargs["messages"]), 1)
        self.assertEqual(kwargs["messages"][0]["role"], "user")
        self.assertEqual(
            kwargs["messages"][0]["content"], "system rules\n\nuser passage"
        )

    def test_gemini_targets_its_own_base_url(self):
        from graphrag.llm import GEMINI_BASE_URL, GeminiClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["GOOGLE_API_KEY"] = "test-google-key"
        GeminiClient().complete_json("s", "u")
        # The key is routed to the Gemini host, not a stray OpenAI base_url.
        self.assertEqual(recorder["base_url"], GEMINI_BASE_URL)
        self.assertEqual(recorder["api_key"], "test-google-key")
        self.addCleanup(self._restore_openai)

    def test_gemini_missing_key_names_the_env_var(self):
        from graphrag.llm import GeminiClient

        with self.assertRaises(LLMError) as ctx:
            GeminiClient()
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))

    def test_gemini_omits_json_mode_for_prose_calls(self):
        from graphrag.llm import GeminiClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        GeminiClient().complete_text("system", "user")
        self.assertNotIn("response_format", recorder["kwargs"])

    def test_auto_prefers_gemini_when_its_key_is_present(self):
        from graphrag.llm import resolve_client

        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        self.assertEqual(resolve_client().name, "gemini")


class NvidiaProviderTests(_StubOpenAI, unittest.TestCase):
    """NVIDIA NIM, added because it is the key this repository actually ships.

    Three behaviours are locked in here, each of which was a live failure:
    auto-detection ignoring the key, the reasoning trace truncating ``content``
    in JSON mode, and the schema flag being advisory but never consulted.
    """

    def setUp(self):
        self._no_key_env()
        self.addCleanup(self._restore_openai)
        self.addCleanup(self._no_key_env)

    def _client(self, recorder: dict, **kwargs):
        from graphrag.llm import NvidiaClient

        self._stub_openai(recorder)
        os.environ["NVIDIA_API_KEY"] = "test-nvidia-key"
        return NvidiaClient(**kwargs)

    def test_auto_selects_nvidia_when_only_its_key_is_present(self):
        from graphrag.llm import resolve_client

        # A default checkout has NVIDIA_API_KEY and nothing else. Without this
        # the service silently answered every question with the heuristic
        # provider, which restates the graph instead of answering it.
        os.environ["NVIDIA_API_KEY"] = "test-nvidia-key"
        self.assertEqual(resolve_client().name, "nvidia")

    def test_nvidia_keeps_the_openai_key_out_of_the_credential(self):
        from graphrag.llm import NvidiaClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["OPENAI_API_KEY"] = "an-openai-key"
        with self.assertRaises(LLMError) as ctx:
            NvidiaClient()
        # NIM rejects an OpenAI key with a confusing 401, so borrowing the
        # variable would make a missing NVIDIA key look like a bad one.
        self.assertIn("NVIDIA_API_KEY", str(ctx.exception))

    def test_nvidia_targets_its_own_base_url(self):
        from graphrag.llm import NVIDIA_BASE_URL, NvidiaClient

        recorder: dict = {}
        self._client(recorder, model="nvidia-test")
        self.assertEqual(str(recorder["base_url"]), NVIDIA_BASE_URL)

    def test_nvidia_disables_the_reasoning_trace(self):
        recorder: dict = {}
        client = self._client(recorder, model="nvidia-test")
        client.complete_text("system", "user")
        # Nemotron Ultra is a reasoning model. In JSON mode it spent 83
        # completion tokens to emit 35 characters of answer, which never
        # parsed. The flag must reach the request, via extra_body because
        # chat_template_kwargs is outside the SDK's typed surface.
        kwargs = recorder["kwargs"]
        self.assertEqual(
            kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"], False
        )

    def test_nvidia_declines_schema_constrained_decoding(self):
        from graphrag.llm import NvidiaClient

        self.assertFalse(NvidiaClient.supports_schema)

    def test_nvidia_uses_json_object_when_given_a_schema(self):
        recorder: dict = {}
        client = self._client(recorder, model="nvidia-test")
        client.complete_json("system", "user", schema={"type": "object"})
        # This endpoint accepts json_schema but returns the answer nested under
        # the schema's own top-level key and truncated, which surfaces as an
        # unparseable-JSON error rather than as a provider fault. json_object
        # plus the caller's own validation is the working combination.
        self.assertEqual(recorder["kwargs"]["response_format"], {"type": "json_object"})

    def test_openai_still_sends_the_schema_it_advertises(self):
        from graphrag.llm import OpenAIChatClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        OpenAIChatClient().complete_json("system", "user", schema={"type": "object"})
        self.assertEqual(
            recorder["kwargs"]["response_format"]["type"], "json_schema"
        )

    def test_ollama_probe_does_not_require_the_httpx_sdk(self):
        from graphrag.llm import _ollama_reachable

        # The probe used to `import httpx`, so an absent optional package was
        # indistinguishable from an absent local server: both fell through to
        # the heuristic provider with no message. urllib is always available.
        self.assertIsInstance(_ollama_reachable(timeout=0.05), bool)


class PaddingLLM(LLMClient):
    """Emits a deliberately lopsided relationship list to exercise the warning.

    *target* is named on *pad* extra edges from a single source, reproducing the
    "model fills the array instead of omitting" failure observed with small
    local models.
    """

    name = "padding"
    is_model = True

    def __init__(self, source: str, relation: str, pad: int, spread: int):
        self.source = source
        self.relation = relation
        self.pad = pad
        self.spread = spread
        self._chunk = 0

    def complete_json(
        self, system: str, user: str, schema: dict | None = None
    ) -> dict:
        if "resolve a user question" in system:
            return {"entities": [self.source]}
        self._chunk += 1
        relationships = [
            {
                "source": self.source,
                "target": f"Station {i}",
                "relation": self.relation,
                "description": "measured at station",
            }
            for i in range(self.pad)
        ]
        # Distinct sources cannot form one fan-out group, so they dilute it.
        relationships += [
            {
                "source": f"Sensor {i}",
                "target": f"Station {i}",
                "relation": "logged_at",
                "description": "logged",
            }
            for i in range(self.spread)
        ]
        return {"entities": [], "relationships": relationships}

    def complete_text(self, system: str, user: str) -> str:
        return "Answer [E1]."


class RetryAndThrottleTests(unittest.TestCase):
    """Rate limits are the normal case on free tiers, so backoff must be real."""

    def setUp(self):
        self.slept: list[float] = []
        self._real_sleep = llm.time.sleep
        llm.time.sleep = self.slept.append
        # The rate-limit path logs a warning by design; keep test output clean.
        self._logging = llm.log.disabled
        llm.log.disabled = True
        self.addCleanup(self._restore_logging)

    def _restore_logging(self) -> None:
        llm.log.disabled = self._logging

    def tearDown(self):
        llm.time.sleep = self._real_sleep

    class _Bare(llm._RetryingClient):
        """Concrete shell so the shared retry policy can be tested directly."""

        name = "test"

        def complete_json(self, system, user, schema=None):
            return self._retry(lambda: "{}", "json")

        def complete_text(self, system, user):
            return self._retry(lambda: "text", "text")

    @classmethod
    def _client(cls, **kwargs) -> llm._RetryingClient:
        return cls._Bare(max_retries=3, min_interval=0.0, **kwargs)

    def test_first_attempt_succeeds_without_sleeping(self):
        client = self._client()
        self.assertEqual(client._retry(lambda: "ok", "call"), "ok")
        self.assertEqual(self.slept, [])

    def test_transient_failure_is_retried(self):
        client = self._client()
        attempts = []

        def call():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("transient upstream blip")
            return "ok"

        self.assertEqual(client._retry(call, "call"), "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.slept), 2)

    def test_backoff_grows_between_attempts(self):
        # The sleep is jittered by a factor in [0.5, 1.5), so the raw sleep
        # times can legitimately invert even though the backoff grows -- attempt
        # 0 sleeps in [0.5, 1.5) and attempt 1 in [1.0, 3.0). Pinning the jitter
        # makes the growth assertion deterministic; the jitter itself is
        # covered by the bounds test below.
        real_random = llm.random.random
        llm.random.random = lambda: 0.0
        self.addCleanup(lambda: setattr(llm.random, "random", real_random))

        client = self._client(backoff=2.0)
        with self.assertRaises(LLMError):
            client._retry(lambda: (_ for _ in ()).throw(RuntimeError("nope")), "call")
        self.assertEqual(len(self.slept), 2)
        self.assertGreater(self.slept[1], self.slept[0])

    def test_backoff_jitter_stays_within_half_to_double(self):
        # Whatever the jitter draws, each sleep must stay inside the documented
        # 0.5x-1.5x band around the exponential delay.
        client = self._client(backoff=2.0)
        with self.assertRaises(LLMError):
            client._retry(lambda: (_ for _ in ()).throw(RuntimeError("nope")), "call")
        self.assertEqual(len(self.slept), 2)
        for slept, base in zip(self.slept, (1.0, 2.0)):
            self.assertGreaterEqual(slept, base * 0.5)
            self.assertLess(slept, base * 1.5)

    def test_rate_limit_slows_down_subsequent_requests(self):
        client = self._client()
        with self.assertRaises(LLMError):
            client._retry(
                lambda: (_ for _ in ()).throw(RuntimeError("429 quota exceeded")),
                "call",
            )
        # The interval is raised so later chunks do not re-trip the quota.
        self.assertGreater(client.min_interval, 0.0)

    def test_non_rate_limit_failure_does_not_raise_interval(self):
        client = self._client()
        with self.assertRaises(LLMError):
            client._retry(
                lambda: (_ for _ in ()).throw(RuntimeError("invalid request")),
                "call",
            )
        self.assertEqual(client.min_interval, 0.0)

    def test_error_message_is_truncated(self):
        client = self._client()
        noisy = "x" * 5000
        with self.assertRaises(LLMError) as ctx:
            client._retry(
                lambda: (_ for _ in ()).throw(RuntimeError(noisy)), "call"
            )
        self.assertLess(len(str(ctx.exception)), 400)
        self.assertIn("…", str(ctx.exception))

    def test_min_interval_enforces_spacing(self):
        client = self._Bare(max_retries=1, min_interval=5.0)
        self.assertEqual(client.min_interval, 5.0)
        client._throttle()
        # The first call sets the clock; a second must wait for the remainder.
        client._throttle()
        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 0.0)

    def test_zero_interval_disables_throttling(self):
        client = self._Bare(max_retries=1, min_interval=0.0)
        client._throttle()
        client._throttle()
        self.assertEqual(self.slept, [])

    def test_gemini_defaults_to_a_free_tier_safe_interval(self):
        self.assertGreaterEqual(llm.GeminiClient.default_min_interval, 12.0)

    def test_env_var_overrides_the_interval(self):
        os.environ["GRAPHRAG_MIN_INTERVAL"] = "0"
        try:
            self.assertEqual(self._Bare().min_interval, 0.0)
        finally:
            del os.environ["GRAPHRAG_MIN_INTERVAL"]


class UnavailableLLM(LLMClient):
    """A provider that is down, e.g. an exhausted quota."""

    name = "unavailable"
    is_model = True

    def __init__(self, message="429 quota exhausted"):
        self.message = message

    def complete_text(self, system, user, schema=None):
        raise LLMError(self.message)

    def complete_json(self, system, user, schema=None):
        raise LLMError(self.message)


class _Served:
    """A running UI server plus a store, cleaned up together."""

    def __init__(self, store, provider="heuristic", llm_factory=None):
        from graphrag import web

        self.store = store
        self.server = (
            _server_with(store, llm_factory)
            if llm_factory is not None
            else web.make_server(store, host="127.0.0.1", port=0, provider=provider)
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self, path, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()


def _server_with(store, llm_factory):
    """A UI server whose /api/ask uses a caller-supplied client factory."""
    import threading as _threading

    from graphrag import web

    handler = type(
        "_BoundHandler",
        (web._Handler,),
        {
            "graph_store": store,
            "config": store.config,
            "lock": _threading.Lock(),
            "llm_factory": staticmethod(llm_factory),
        },
    )
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    return server


class AnswerFlowTests(unittest.TestCase):
    """The flow trace is what makes an answer auditable, so it is tested."""

    def _subgraph(self):


        def ent(i, n, t="concept"):
            return Entity(id=i, name=n, entity_type=t, description="")

        nodes = [
            ent("multiheadattention", "Multi-head attention"),
            ent("attentionfunction", "attention function", "method"),
            ent("encoder", "encoder", "component"),
            ent("decoder", "decoder", "component"),
        ]
        return Subgraph(
            seeds=[nodes[0]],
            nodes={n.id: n for n in nodes},
            edges=[
                Edge("multiheadattention", "attentionfunction", "uses", "d"),
                Edge("multiheadattention", "encoder", "inside", "d"),
                Edge("multiheadattention", "decoder", "contrasts", "d"),
            ],
        )

    def _answer(self, **kwargs):
        defaults = dict(
            question="What is multi-head attention?",
            text="It attends in parallel. [E1]",
            used_tags=["E1"],
            linked=["Multi-head attention"],
            context_entities=4,
            context_edges=3,
            grounded=True,
            graph=self._subgraph(),
            tag_map={
                "E1": "multiheadattention",
                "E2": "attentionfunction",
                "E3": "encoder",
                "E4": "decoder",
            },
        )
        defaults.update(kwargs)
        return Answer(**defaults)

    def test_flow_traces_question_to_seeds_to_edges_to_citations(self):
        flow = self._answer().render_flow()
        self.assertIn("What is multi-head attention?", flow)
        self.assertIn("[E1] Multi-head attention", flow)
        self.assertIn("--uses--> [E2] attention function", flow)
        self.assertIn("retrieved 4 nodes, 3 relationships", flow)
        self.assertIn("answered using [E1]", flow)
        # The trace must be a connected path, not a flat list.
        self.assertIn("├─", flow)
        self.assertIn("└─", flow)

    def test_flow_is_the_last_thing_rendered(self):
        text = self._answer().render()
        self.assertLess(text.index("cited"), text.index("FLOW"))
        self.assertTrue(text.rstrip().endswith("answered using [E1]"))

    def test_incoming_edges_are_shown_pointing_back(self):
        graph = self._subgraph()
        graph.edges.append(Edge("encoder", "multiheadattention", "feeds", "d"))
        flow = self._answer(graph=graph, context_edges=4).render_flow()
        self.assertIn("<--feeds-- [E3] encoder", flow)

    def test_busy_seed_reports_the_remainder(self):
        graph = self._subgraph()
        for i in range(9):
            node = Entity(
                id=f"n{i}", name=f"node {i}", entity_type="concept", description=""
            )
            graph.nodes[node.id] = node
            graph.edges.append(Edge("multiheadattention", node.id, "relates", "d"))
        flow = self._answer(graph=graph).render_flow()
        self.assertIn("... and 6 more relationship(s)", flow)
        # The cap keeps the trace readable rather than dumping every edge.
        self.assertLessEqual(flow.count("--relates-->"), 6)

    def test_ungrounded_answer_explains_where_it_stopped(self):
        flow = self._answer(
            grounded=False, note="no entity could be linked to a graph node"
        ).render_flow()
        self.assertIn("stopped:", flow)
        self.assertIn("no entity could be linked", flow)
        self.assertNotIn("answered using", flow)

    def test_grounded_answer_without_a_graph_renders_nothing(self):
        self.assertEqual(self._answer(graph=None).render_flow(), "")
        self.assertEqual(self._answer(graph=Subgraph()).render_flow(), "")
        self.assertNotIn("FLOW", self._answer(graph=None).render())

    def test_ungrounded_answer_shows_where_it_stopped_even_without_a_graph(self):
        # The "no entity linked" path never builds a subgraph, which is exactly
        # when the reader most needs to see that retrieval stopped there.
        flow = Answer(
            question="What is the airspeed velocity of an unladen swallow?",
            text="The question does not name any entity present in the graph.",
            grounded=False,
            note="no entity could be linked to a graph node",
        ).render_flow()
        self.assertIn("FLOW", flow)
        self.assertIn("stopped:", flow)
        self.assertIn("no entity could be linked", flow)


class QaContextTests(unittest.TestCase):
    """The model must be given the subgraph, not just the question."""

    def _client(self):
        return ScriptedLLM(payload=ScriptedLLM.BIOLOGY, identify=["Riftia pachyptila"])

    def _store(self):
        store, _ = temp_store()
        store.ensure_schema()
        store.upsert_entity("Riftia pachyptila", "organism", "giant tubeworm")
        store.upsert_entity("Candidatus Endoriftia", "bacterium", "symbiont")
        store.upsert_entity("Trophosome", "organ", "symbiosis organ")
        store.upsert_edge("Riftia pachyptila", "Candidatus Endoriftia", "hosts", "in trophosome")
        store.upsert_edge("Candidatus Endoriftia", "Trophosome", "lives_in", "occupies it")
        return store

    def test_answer_prompt_puts_context_before_the_question(self):
        from graphrag.qa import build_answer_prompt

        prompt = build_answer_prompt("What is attention?", "ENTITIES:\n[E1] foo")
        self.assertLess(prompt.index("CONTEXT"), prompt.index("QUESTION"))
        self.assertIn("[E1] foo", prompt)
        self.assertIn("citing tags like [E1]", prompt)

    def test_ask_sends_the_subgraph_not_only_the_question(self):
        store, client = self._store(), self._client()
        ask("what does Riftia pachyptila host", store, client)

        self.assertTrue(client.calls, "the model was never called")
        system, user = client.calls[-1]

        # The retrieved subgraph, not the question, is the substance of the prompt.
        self.assertIn("ENTITIES:", user)
        self.assertIn("RELATIONSHIPS:", user)
        self.assertIn("[E1]", user)
        self.assertIn("using only a retrieved subgraph", system)
        question = "what does Riftia pachyptila host"
        self.assertLess(len(question), len(user) / 4)

    def test_context_carries_the_retrieved_relationships(self):
        store, client = self._store(), self._client()
        ask("what does Riftia pachyptila host", store, client)
        _, user = client.calls[-1]
        self.assertIn("-->", user)
        self.assertIn("hosts", user)

    def test_question_is_not_sent_on_its_own_before_retrieval(self):
        # The identify step sees only the question; the answer step must be the
        # last call, and it must be the one carrying the context.
        store, client = self._store(), self._client()
        ask("what does Riftia pachyptila host", store, client)
        self.assertGreaterEqual(len(client.calls), 2)
        self.assertIn("ENTITIES:", client.calls[-1][1])


class WebUITests(unittest.TestCase):
    """The viewer is a real HTTP surface, so exercise it over a real socket."""

    @classmethod
    def setUpClass(cls):
        from graphrag import web

        cls.web = web
        store, _ = temp_store()
        store.ensure_schema()
        store.upsert_entity("Riftia pachyptila", "organism", "giant tubeworm")
        store.upsert_entity("Candidatus Endoriftia", "bacterium", "symbiont")
        store.upsert_entity("hydrogen sulfide", "compound", "energy source")
        store.upsert_edge(
            "Riftia pachyptila", "Candidatus Endoriftia", "hosts", "in trophosome"
        )
        store.upsert_edge(
            "Candidatus Endoriftia", "hydrogen sulfide", "oxidises", "energy"
        )
        cls.served = _Served(store)
        cls.store = store
        cls.base = cls.served.base

    @classmethod
    def tearDownClass(cls):
        cls.served.close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read(), r.headers

    def post(self, path, payload, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # -- routing ----------------------------------------------------------

    def test_index_page_is_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<svg", body)

    def test_stats_reports_counts_and_types(self):
        status, body, _ = self.get("/api/stats")
        data = json.loads(body)
        self.assertEqual(data["nodes"], 3)
        self.assertEqual(data["edges"], 2)
        self.assertIn(["organism", 1], data["entity_types"])

    def test_entity_search_filters(self):
        status, body, _ = self.get("/api/entities?q=endoriftia")
        names = [e["name"] for e in json.loads(body)["entities"]]
        self.assertEqual(names, ["Candidatus Endoriftia"])

    def test_entity_search_with_no_match_is_empty_not_error(self):
        status, body, _ = self.get("/api/entities?q=zzzznothing")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["entities"], [])

    def test_graph_without_seed_returns_whole_graph(self):
        status, body, _ = self.get("/api/graph")
        data = json.loads(body)
        self.assertEqual(len(data["nodes"]), 3)
        self.assertEqual(len(data["edges"]), 2)
        self.assertEqual(len(data["seeds"]), 3)

    def test_graph_with_seed_is_hop_limited_and_reports_seeds(self):
        status, body, _ = self.get("/api/graph?seed=candidatusendoriftia&hops=1")
        data = json.loads(body)
        self.assertEqual(data["seeds"], ["candidatusendoriftia"])
        # 1 hop from Endoriftia: its two neighbours, not the whole graph.
        self.assertEqual(len(data["nodes"]), 3)

    def test_graph_drops_edges_with_trimmed_endpoints(self):
        # A hub wide enough to be trimmed by the store. Uses its own store so
        # the 80 filler nodes cannot leak into the shared fixture.
        store, _ = temp_store()
        served = _Served(store)
        try:
            store.ensure_schema()
            store.upsert_entity("Hub", "hub", "")
            for i in range(200):
                store.upsert_entity(f"Leaf {i}", "leaf", "")
                store.upsert_edge("Hub", f"Leaf {i}", "linked", "")
            with urllib.request.urlopen(
                served.base + "/api/graph?seed=hub&hops=1", timeout=10
            ) as r:
                data = json.loads(r.read())
            ids = {n["id"] for n in data["nodes"]}
            self.assertLess(len(ids), 201, "the hub should have been trimmed")
            self.assertIn("hub", ids, "the seed must survive trimming")
            for edge in data["edges"]:
                self.assertIn(edge["source"], ids)
                self.assertIn(edge["target"], ids)
        finally:
            served.close()

    def test_out_of_range_params_are_clamped(self):
        # hops/limit are bounded server-side so a bad value cannot ask the
        # store for an unbounded traversal.
        status, body, _ = self.get("/api/graph?hops=9999&limit=-3")
        self.assertEqual(status, 200)
        self.assertIn("nodes", json.loads(body))

    def test_non_numeric_param_falls_back_to_default(self):
        status, body, _ = self.get("/api/graph?hops=banana")
        self.assertEqual(status, 200)
        self.assertIn("nodes", json.loads(body))

    def test_unknown_route_is_404(self):
        try:
            self.get("/api/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # -- ask --------------------------------------------------------------

    def test_ask_returns_answer_with_tag_map_and_graph(self):
        status, data = self.post("/api/ask", {"question": "What does Endoriftia oxidise?"})
        self.assertEqual(status, 200)
        for key in ("question", "text", "grounded", "used_tags", "tag_map", "graph"):
            self.assertIn(key, data)
        # Every cited tag must resolve to a node that was actually supplied.
        node_ids = {n["id"] for n in data["graph"]["nodes"]}
        for tag in data["used_tags"]:
            self.assertIn(data["tag_map"][tag], node_ids)

    def test_ask_rejects_missing_question(self):
        status, data = self.post("/api/ask", {})
        self.assertEqual(status, 400)
        self.assertIn("question", data["error"])

    def test_ask_rejects_blank_question(self):
        status, data = self.post("/api/ask", {"question": "   "})
        self.assertEqual(status, 400)

    def test_ask_rejects_malformed_json(self):
        status, data = self.post("/api/ask", None, raw=b"{not json")
        self.assertEqual(status, 400)

    def test_provider_failure_is_reported_as_such_not_as_no_match(self):
        # A dead provider must never masquerade as "the question names no
        # entity": that sends the operator hunting for a graph problem.
        store, _ = temp_store()
        store.ensure_schema()
        served = _Served(store, llm_factory=lambda: UnavailableLLM())
        try:
            status, data = served.post("/api/ask", {"question": "What oxidises?"})
            self.assertEqual(status, 200)
            self.assertIn("429", data["note"] or "")
            self.assertNotIn("no entity could be linked", data["note"] or "")
            self.assertFalse(data["grounded"])
        finally:
            served.close()

    def test_unexpected_ask_failure_returns_json_not_a_dropped_connection(self):
        # A failure *inside* ask (here: a broken store) must still come back as
        # JSON, not a dropped connection with a bare traceback on stderr.
        store, _ = temp_store()
        store.ensure_schema()
        store.upsert_entity("Hydrogen sulfide", "compound", "energy source")
        store.upsert_entity("Candidatus Endoriftia", "bacterium", "symbiont")
        store.upsert_edge(
            "Candidatus Endoriftia", "Hydrogen sulfide", "oxidises", ""
        )
        served = _Served(store)

        def broken(*_args, **_kwargs):
            raise RuntimeError("kaboom")

        store.neighborhood = broken
        try:
            status, data = served.post(
                "/api/ask", {"question": "What does Hydrogen sulfide relate to?"}
            )
            self.assertEqual(status, 500)
            self.assertIn("kaboom", data["error"])
        finally:
            served.close()

    def test_unavailable_client_factory_is_503(self):
        store, _ = temp_store()
        served = _Served(store)

        def boom():
            raise RuntimeError("no api key configured")

        served.server.RequestHandlerClass.llm_factory = staticmethod(boom)
        try:
            status, data = served.post("/api/ask", {"question": "What oxidises?"})
            self.assertEqual(status, 503)
            self.assertIn("no api key configured", data["error"])
        finally:
            served.close()

    def test_ask_rejects_non_object_body(self):
        status, data = self.post("/api/ask", None, raw=b"[1,2,3]")
        self.assertEqual(status, 400)

    def test_ask_on_empty_graph_is_not_an_error(self):
        empty_store, _ = temp_store()
        try:
            empty_store.ensure_schema()
            from graphrag.qa import ask as ask_fn

            answer = ask_fn("anything?", empty_store, ScriptedLLM())
            self.assertFalse(answer.grounded)
        finally:
            empty_store.close()

    # -- frontend/backend contract ----------------------------------------

    def test_every_endpoint_the_page_calls_is_routed(self):
        page = (self.web.STATIC_DIR / "index.html").read_text()
        called = set(re.findall(r'["`\'](/api/[a-z]+)', page))
        self.assertTrue(called, "page should call at least one API route")
        handler = self.web._Handler
        for path in called:
            self.assertTrue(
                hasattr(handler, f"_api_{path.rsplit('/', 1)[-1]}"),
                f"page calls {path} but no handler exists",
            )

    def test_page_has_no_unresolvable_dom_ids(self):
        page = (self.web.STATIC_DIR / "index.html").read_text()
        markup, script = page.split("<script>", 1)
        ids = set(re.findall(r'\bid="([^"]+)"', markup))
        refs = set(re.findall(r'\$\("([^"]+)"\)', script))
        self.assertEqual(refs - ids, set(), "JS references ids absent from the markup")

    def test_page_uses_no_remote_assets(self):
        # A CDN dependency would break the offline story the project keeps.
        page = (self.web.STATIC_DIR / "index.html").read_text()
        for pattern in ("http://", "https://"):
            for hit in re.findall(pattern + r'[^"\')\s]*', page):
                self.assertIn("www.w3.org", hit, f"remote asset referenced: {hit}")


class NameVariantTests(unittest.TestCase):
    """Questions are phrased in the asker's words, not the graph's."""

    def test_singularize_strips_only_unambiguous_endings(self):
        from graphrag.resolve import singularize

        for plural, singular in [
            ("transformers", "transformer"),
            ("frames", "frame"),
            ("networks", "network"),
            ("vectors", "vector"),
            ("bodies", "body"),
            ("classes", "class"),
            ("batches", "batch"),
            ("boxes", "box"),
        ]:
            self.assertEqual(singularize(plural), singular, plural)

    def test_singularize_leaves_genuine_singulars_alone(self):
        from graphrag.resolve import singularize

        # A naive strip-the-s would turn these into non-words and could
        # manufacture a bogus match.
        for word in ["bias", "gas", "analysis", "basis", "corpus", "status", "less"]:
            self.assertEqual(singularize(word), word, word)

    def test_name_variants_keeps_original_first(self):
        from graphrag.resolve import name_variants

        self.assertEqual(name_variants("transformers")[0], "transformers")
        self.assertIn("transformer", name_variants("transformers"))

    def test_name_variants_deduplicates(self):
        from graphrag.resolve import name_variants

        self.assertEqual(name_variants("transformer"), ["transformer"])
        self.assertEqual(name_variants(""), [])
        self.assertEqual(name_variants("   "), [])


class EntityLinkingVariantTests(unittest.TestCase):
    """Plurals and descriptive tails must still reach the right node."""

    def setUp(self):
        self.store, _ = temp_store()
        self.store.ensure_schema()
        self.store.upsert_entity("Transformer", "model", "attention architecture")
        self.store.upsert_entity("Riftia pachyptila", "organism", "tubeworm")
        self.store.upsert_entity("Candidatus Endoriftia", "bacterium", "symbiont")
        self.store.upsert_entity("Frame Checkpoint", "component", "validation")

    def tearDown(self):
        self.store.close()

    def names(self, needle):
        return [e.name for e in self.store.lookup_by_name(needle)]

    def test_plural_query_finds_singular_node(self):
        self.assertEqual(self.names("transformers"), ["Transformer"])

    def test_plural_query_case_insensitive(self):
        for needle in ["transformers", "Transformers", "TRANSFORMERS"]:
            self.assertEqual(self.names(needle), ["Transformer"], needle)

    def test_full_question_reaches_the_node(self):
        # The reported failure: "what are transformers" found nothing.
        self.assertEqual(self.names("what are transformers"), ["Transformer"])

    def test_plural_compound(self):
        self.assertEqual(self.names("frame checkpoints"), ["Frame Checkpoint"])

    def test_leading_article_does_not_block(self):
        self.assertIn("Transformer", self.names("the Transformers"))

    def test_descriptive_tail_finds_node_on_word_boundary(self):
        self.assertIn("Transformer", self.names("transformer architecture"))

    def test_reverse_match_respects_word_boundaries(self):
        # "trans" is a substring of "Transformer" but not a word in it, so a
        # query for "transformer" must not surface a node named "trans".
        self.store.upsert_entity("trans", "artifact", "a substring decoy")
        self.assertNotIn("trans", self.names("transformer"))

    def test_exact_match_still_outranks_loose_substring(self):
        # The pre-existing specificity guarantee must survive the change.
        self.store.upsert_entity("Riftia", "genus", "a relative of the tubeworm")
        self.assertEqual(self.names("Riftia")[0], "Riftia")

    def test_specific_full_name_beats_genus(self):
        self.assertEqual(self.names("Riftia pachyptila"), ["Riftia pachyptila"])

    def test_search_entities_shares_the_plural_fix(self):
        # The UI search box routes through the same lookup.
        found = [e["name"] for e in [x.as_dict() for x in self.store.search_entities("transformers")]]
        self.assertEqual(found, ["Transformer"])

    def test_no_match_stays_empty(self):
        self.assertEqual(self.names("photosynthesis"), [])

    def test_blank_needle_is_empty(self):
        self.assertEqual(self.names(""), [])
        self.assertEqual(self.names("   "), [])


class TokenFallbackTests(unittest.TestCase):
    """A query may name a thing partly and then say what kind of thing it is."""

    def setUp(self):
        self.store, _ = temp_store()
        self.store.ensure_schema()
        self.store.upsert_entity("multi-head attention", "mechanism", "attention over several heads")
        self.store.upsert_entity("multi-head self-attention mechanism", "component", "in the encoder")
        self.store.upsert_entity("scaled dot-product attention", "concept", "the scoring function")
        self.store.upsert_entity("Transformer", "model", "attention architecture")

    def tearDown(self):
        self.store.close()

    def names(self, needle):
        return [e.name for e in self.store.lookup_by_name(needle)]

    def test_type_word_satisfies_the_query(self):
        # "mechanism" is multi-head attention's entity type, not part of its
        # name, so no substring rule can see the match.
        self.assertEqual(self.names("multi attention mechanism")[0], "multi-head attention")

    def test_least_surplus_name_wins(self):
        ranked = self.names("multi attention mechanism")
        self.assertEqual(ranked[0], "multi-head attention")
        # The longer sibling is still offered rather than silently dropped.
        self.assertIn("multi-head self-attention mechanism", ranked)

    def test_interrogatives_are_ignored(self):
        self.assertEqual(
            self.names("what is multi attention mechanism")[0], "multi-head attention"
        )

    def test_hyphen_and_space_are_interchangeable(self):
        self.assertEqual(
            self.names("scaled dot product attention")[0], "scaled dot-product attention"
        )

    def test_exact_match_still_wins_outright(self):
        self.assertEqual(self.names("multi-head attention"), ["multi-head attention"])

    def test_unrelated_query_stays_empty(self):
        # The fallback must not invent a match from loose similarity.
        for q in ["photosynthesis", "quantum chromodynamics", "the stock market"]:
            self.assertEqual(self.names(q), [], q)

    def test_fallback_does_not_fire_when_a_real_match_exists(self):
        # "Transformer" matches exactly, so the loose stage never runs and the
        # result stays narrow.
        self.assertEqual(self.names("transformers"), ["Transformer"])

    def test_name_match_outranks_a_type_only_match(self):
        # The type-aware stage is a last resort, so a real name match wins even
        # when another node is only matched by its type.
        self.assertEqual(self.names("mechanism"), ["multi-head self-attention mechanism"])

    def test_type_is_consulted_once_names_fail(self):
        # With no name containing it, the type is what makes the match possible.
        self.store.upsert_entity("residual stream", "mechanism", "the residual path")
        try:
            self.assertIn("residual stream", self.names("residual mechanism"))
        finally:
            self.store.close()
            self.store, _ = temp_store()
            self.store.ensure_schema()


class ProviderFailureTests(unittest.TestCase):
    """A provider that is down must not be reported as an empty graph."""

    def test_identify_entities_propagates_provider_failure(self):
        from graphrag.extract import identify_entities

        with self.assertRaises(LLMError):
            identify_entities(UnavailableLLM("429 quota exhausted"), "What?")

    def test_ask_reports_provider_failure_instead_of_no_entity_linked(self):
        store, _ = temp_store()
        try:
            store.ensure_schema()
            store.upsert_entity("Hydrogen sulfide", "compound", "")
            store.upsert_entity("Candidatus Endoriftia", "bacterium", "")
            store.upsert_edge(
                "Candidatus Endoriftia", "Hydrogen sulfide", "oxidises", ""
            )
            answer = ask(
                "Which organism oxidises hydrogen sulfide?",
                store,
                UnavailableLLM("429 quota exhausted"),
            )
            self.assertFalse(answer.grounded)
            self.assertIn("429", answer.note or "")
            self.assertNotIn("no entity could be linked", answer.note or "")
        finally:
            store.close()

    def test_malformed_identify_payload_yields_no_names(self):
        # A successful call that returns junk is legitimately "nothing found",
        # which is different from the call failing.
        from graphrag.extract import identify_entities

        class Junk(LLMClient):
            name = "junk"
            is_model = True

            def complete_text(self, system, user, schema=None):
                return ""

            def complete_json(self, system, user, schema=None):
                return {"entities": "not a list"}

        self.assertEqual(identify_entities(Junk(), "What?"), [])


class GroundingTests(unittest.TestCase):
    """A relationship must be supported by the passage it came from."""

    PASSAGE = (
        "Riftia pachyptila has no mouth or gut. Instead its trophosome hosts "
        "Candidatus Endoriftia, which oxidises hydrogen sulfide to produce "
        "organic carbon."
    )

    def _parse(self, relationships, entities=None):
        # Endpoints must appear among the extracted entities, so the fixture
        # declares them explicitly.
        return parse_extraction(
            {
                "entities": entities
                if entities is not None
                else [{"name": n, "type": "t", "description": "d"}
                      for n in ("Riftia pachyptila", "Candidatus Endoriftia",
                                "hydrogen sulfide", "organic carbon")],
                "relationships": relationships,
            },
            GraphRAGConfig(),
            passage=self.PASSAGE,
        )

    def test_grounded_relation_is_kept(self):
        result = self._parse([
            {
                "source": "Candidatus Endoriftia",
                "target": "hydrogen sulfide",
                "relation": "oxidises",
                "description": "d",
            }
        ])
        self.assertEqual(len(result.relationships), 1)
        self.assertEqual(result.rejected, 0)

    def test_invented_entity_is_rejected(self):
        result = self._parse(
            [
                {
                    "source": "Sourdough starter",
                    "target": "hydrogen sulfide",
                    "relation": "oxidises",
                    "description": "d",
                }
            ],
            entities=[
                {"name": "Sourdough starter", "type": "t", "description": "d"},
                {"name": "hydrogen sulfide", "type": "t", "description": "d"},
            ],
        )
        self.assertEqual(result.relationships, [])
        self.assertEqual(result.rejected, 1)

    def test_composed_name_accepted_when_every_word_present(self):
        # "Sourdough starter" was wrongly rejected by a strict contiguous match;
        # the trophosome case is the same shape of problem.
        passage = "A sourdough starter ferments flour into levain."
        result = parse_extraction(
            {"entities": [
                {"name": "sourdough starter", "type": "t", "description": "d"},
                {"name": "levain", "type": "t", "description": "d"}],
             "relationships": [
                {"source": "sourdough starter", "target": "levain",
                 "relation": "ferments", "description": "d"}]},
            GraphRAGConfig(),
            passage=passage,
        )
        self.assertEqual(len(result.relationships), 1)

    def test_is_grounded_requires_every_significant_word(self):
        slugs = _slug_text("Riftia pachyptila hosts Candidatus Endoriftia.")
        self.assertTrue(is_grounded("Riftia pachyptila", slugs))
        self.assertTrue(is_grounded("riftia pachyptila", slugs))
        self.assertFalse(is_grounded("Riftia pachyptila EXTRA", slugs))
        self.assertFalse(is_grounded("Krill", slugs))

    def test_short_and_punctuation_names_rejected(self):
        # A one-character or punctuation-only "name" is not an entity.
        self.assertFalse(is_grounded("x", _slug_text("x marks the spot")))
        self.assertFalse(is_grounded("...", _slug_text("...")))


class FanOutWarningTests(unittest.TestCase):
    """Padding detection must reflect the current run, not the whole graph."""

    @staticmethod
    def _report(edges_added: int) -> IngestReport:
        report = IngestReport(document="d")
        report.edges_added = edges_added
        return report

    def test_repeated_relation_from_one_source_warns(self):
        from graphrag.ingest import _quality_warnings

        fan_out = Counter({("hydrogen sulfide", "measured_in"): 6})
        for i in range(7):
            fan_out[(f"Sensor {i}", "logged_at")] = 1
        warnings = _quality_warnings(fan_out, self._report(13))
        self.assertTrue(any("padding" in w for w in warnings), warnings)

    def test_spread_out_relationships_do_not_warn(self):
        from graphrag.ingest import _quality_warnings

        fan_out = Counter({("hydrogen sulfide", "measured_in"): 6})
        for i in range(40):
            fan_out[(f"Sensor {i}", "logged_at")] = 1
        self.assertEqual(_quality_warnings(fan_out, self._report(46)), [])

    def test_below_count_threshold_does_not_warn(self):
        from graphrag.ingest import _quality_warnings

        # Four is under the count threshold even though it is a large share.
        fan_out = Counter({("a", "measured_in"): 4})
        for i in range(20):
            fan_out[(f"s{i}", "logged_at")] = 1
        self.assertEqual(_quality_warnings(fan_out, self._report(24)), [])

    def test_no_warning_when_nothing_was_added(self):
        from graphrag.ingest import _quality_warnings

        fan_out = Counter({("a", "measured_in"): 50})
        self.assertEqual(_quality_warnings(fan_out, self._report(0)), [])

    def test_warning_is_scoped_to_this_run(self):
        # Counts come from the run rather than the stored graph, so a
        # pre-populated graph can neither manufacture nor mask a warning.
        from graphrag.ingest import _quality_warnings

        fan_out = Counter({("a", "r"): 1})
        self.assertEqual(_quality_warnings(fan_out, self._report(1)), [])

    def test_padded_run_is_rejected_by_grounding(self):
        # A model inventing "Station 0..5" from a biology passage has those
        # relationships dropped outright. This is the backstop that catches
        # padding even when the fan-out heuristic would not fire.
        client = PaddingLLM("hydrogen sulfide", "measured_in", 6, 0)
        store, _ = temp_store()
        try:
            store.ensure_schema()
            report = ingest_pdf(SAMPLES / "marine_biology.pdf", store, client)
            self.assertEqual(report.edges_added, 0)
            # "seen" counts validated records, so nothing survived; every
            # fabricated endpoint landed in the rejected tally instead.
            self.assertEqual(report.relationships_seen, 0)
            self.assertGreaterEqual(report.rejected, 6)
        finally:
            store.close()


class SchemaPlumbingTests(_StubOpenAI, unittest.TestCase):
    """The pipeline must ask for constrained decoding, not just prompt for it."""

    def setUp(self):
        self._no_key_env()
        self.addCleanup(self._restore_openai)
        self.addCleanup(self._no_key_env)

    def test_extraction_passes_the_extraction_schema(self):
        client = ScriptedLLM()
        extract_chunk(client, "Riftia pachyptila hosts Endoriftia.", GraphRAGConfig())
        self.assertIn(EXTRACTION_SCHEMA, client.schemas)

    def test_identify_passes_the_identify_schema(self):
        client = ScriptedLLM()
        identify_entities(client, "What does Endoriftia do?")
        self.assertIn(IDENTIFY_SCHEMA, client.schemas)

    def test_schemas_are_strict_json_schema_subset(self):
        # Strict mode rejects schemas that are not fully specified, so assert
        # the invariants rather than trusting the literals.
        for schema in (EXTRACTION_SCHEMA, IDENTIFY_SCHEMA):
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(
                set(schema["required"]), set(schema["properties"])
            )
            for spec in schema["properties"].values():
                item = spec.get("items")
                if item and item.get("type") == "object":
                    self.assertFalse(item["additionalProperties"])
                    self.assertEqual(set(item["required"]), set(item["properties"]))

    def test_openai_forwards_schema_as_json_schema(self):
        from graphrag.llm import OpenAIChatClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["OPENAI_API_KEY"] = "test-key"
        OpenAIChatClient().complete_json("s", "u", EXTRACTION_SCHEMA)
        fmt = recorder["kwargs"]["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertEqual(fmt["json_schema"]["schema"], EXTRACTION_SCHEMA)
        self.addCleanup(self._restore_openai)

    def test_json_mode_without_schema_stays_plain(self):
        from graphrag.llm import OpenAIChatClient

        recorder: dict = {}
        self._stub_openai(recorder)
        os.environ["OPENAI_API_KEY"] = "test-key"
        # No schema still needs json_object; it is not a schema request.
        OpenAIChatClient().complete_json("s", "u")
        self.assertEqual(
            recorder["kwargs"]["response_format"], {"type": "json_object"}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
