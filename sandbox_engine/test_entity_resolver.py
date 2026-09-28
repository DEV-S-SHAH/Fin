"""Regression tests for :mod:`sandbox_engine.entity_resolver`.

These cover the duplicate-node defect this module was written to fix: the same
concept reaching the graph under several names, once per filing, with the
loader dropping the repeats only at insert time.

Run with::

    .venv/bin/python -m unittest sandbox_engine.test_entity_resolver
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sandbox_engine.config import ENTITY_FUZZY_MATCH
from sandbox_engine.entity_resolver import (
    CONCEPT_ALIASES,
    ConceptRegistry,
    EntityRegistry,
    canonical_concept,
    polarity_conflict,
    resolve_subgraph,
)
from sandbox_engine.parser import stable_id


def metric_registry(**kwargs) -> EntityRegistry:
    kwargs.setdefault("kind", "metric")
    kwargs.setdefault("id_factory", stable_id)
    return EntityRegistry(**kwargs)


def concept_registry(**kwargs) -> ConceptRegistry:
    kwargs.setdefault("id_factory", stable_id)
    return ConceptRegistry(**kwargs)


class TestCanonicalisation(unittest.TestCase):
    """The label rewrite, checked against the pairs actually observed."""

    def test_folds_trademark_marks(self) -> None:
        # 12 of the 16 duplicate pairs found in the corpus were this one mark.
        for marked, plain in (("iPhone®", "iPhone"), ("Mac®", "Mac"), ("iPad®", "iPad")):
            with self.subTest(marked=marked):
                self.assertEqual(canonical_concept(marked).name, canonical_concept(plain).name)

    def test_folds_footnote_markers(self) -> None:
        self.assertEqual(
            canonical_concept("Services (1)").name, canonical_concept("Services").name
        )

    def test_folds_par_value_and_share_counts(self) -> None:
        # The 10-K balance sheet and the 10-Q note spelled this differently.
        short = "Common stock and additional paid-in capital, $0.00"
        long = (
            "Common stock and additional paid-in capital, $0.00001 par value: "
            "50,400,000 shares authorized; 14,773,260 and 15,116,786 shares issued"
        )
        self.assertEqual(canonical_concept(short).name, canonical_concept(long).name)
        self.assertEqual(
            canonical_concept(short).name, "Common stock and additional paid-in capital"
        )

    def test_keeps_labels_whose_identity_is_numeric(self) -> None:
        for label in ("2026 Plan, $5,000", "Net sales, $1,234"):
            with self.subTest(label=label):
                self.assertEqual(canonical_concept(label).name, label)

    def test_folds_unit_qualifiers(self) -> None:
        for label in (
            "Net sales (in millions)",
            "Net sales ($ in millions)",
            "Net sales (in thousands)",
            "Net Sales (USD)",
            "Net Sales (in USD)",
            "Net sales ($)",
        ):
            with self.subTest(label=label):
                self.assertEqual(canonical_concept(label).name.lower(), "net sales")

    def test_preserves_meaningful_qualifiers(self) -> None:
        # The unit rule must not eat "(%)" -- a margin is not a balance -- and
        # the footnote rule must not eat "(basic)".
        for label in (
            "Gross Margin (%)",
            "Earnings per share (basic)",
            "Diluted (net)",
            "Earnings (loss) per share",
        ):
            with self.subTest(label=label):
                self.assertEqual(canonical_concept(label).name, label)

    def test_preserves_distinct_concepts(self) -> None:
        for a, b in (("Net Sales", "Cost of Sales"), ("Mac", "iPhone")):
            with self.subTest(a=a):
                self.assertNotEqual(canonical_concept(a).name, canonical_concept(b).name)

    def test_blank_labels_stay_blank(self) -> None:
        self.assertEqual(canonical_concept("   ").name, "")

    def test_is_idempotent(self) -> None:
        # A rewrite that is not a fixed point would re-fold on every re-ingest.
        for label in (
            "iPhone®",
            "Services (1)",
            "Net sales ($ in millions)",
            "Common stock and additional paid-in capital, $0.00001 par value: 50,000 shares",
            "Gross Margin (%)",
        ):
            with self.subTest(label=label):
                once = canonical_concept(label).name
                self.assertEqual(canonical_concept(once).name, once)

    def test_preserves_surface_case(self) -> None:
        # Title-casing would destroy "iPhone", "eBay" and "MacBooks".
        for label in ("iPhone", "eBay", "MacBook Pro", "Net Sales"):
            with self.subTest(label=label):
                self.assertEqual(canonical_concept(label).name, label)


class TestEntityResolution(unittest.TestCase):
    def test_same_name_resolves_to_same_id(self) -> None:
        reg = metric_registry()
        first = reg.add({"name": "Net Sales", "category": "income_statement"})
        second = reg.add({"name": "Net Sales", "category": "income_statement"})
        self.assertEqual(first.decision, "created")
        self.assertEqual(second.decision, "merged")
        self.assertEqual(first.canonical_id, second.canonical_id)
        self.assertEqual(second.evidence, "name")

    def test_case_differences_merge(self) -> None:
        # Case divergence is the registry's job, not the rewrite's: the index
        # normalises, so two spellings land on one node.
        reg = metric_registry()
        self.assertEqual(
            reg.add({"name": "Net Sales"}).canonical_id,
            reg.add({"name": "Net sales"}).canonical_id,
        )

    def test_alias_resolves_to_owner(self) -> None:
        reg = metric_registry()
        owner = reg.add({"name": "Revenue", "aliases": ["Turnover"]})
        hit = reg.add({"name": "Turnover"})
        self.assertEqual(hit.canonical_id, owner.canonical_id)
        self.assertEqual(hit.evidence, "alias")

    def test_exact_name_beats_alias(self) -> None:
        reg = metric_registry()
        alias_owner = reg.add({"name": "Turnover"})
        real = reg.add({"name": "Revenue", "aliases": ["Turnover"]})
        self.assertNotEqual(real.canonical_id, alias_owner.canonical_id)
        # The name's original owner keeps it, even though the new entity also
        # claims it as an alias.
        self.assertEqual(reg.add({"name": "Turnover"}).canonical_id, alias_owner.canonical_id)

    def test_contested_alias_is_rejected(self) -> None:
        reg = metric_registry()
        first = reg.add({"name": "Revenue", "aliases": ["Turnover"]})
        contested = reg.add({"name": "Sales", "aliases": ["Turnover"]})
        self.assertNotEqual(first.canonical_id, contested.canonical_id)
        self.assertIn("Turnover", contested.rejected_aliases)
        self.assertEqual(reg.stats["alias_conflicts"], 1)
        self.assertEqual(reg.get(first.canonical_id).name, "Revenue")

    def test_canonical_name_is_immutable(self) -> None:
        # The first spelling wins: later mentions accumulate onto it and never
        # rewrite it, so a node's name does not depend on ingestion order.
        reg = metric_registry()
        original = reg.add({"name": "iPhone®"})
        second = reg.add({"name": "iPhone"})
        entity = reg.get(original.canonical_id)
        self.assertEqual(entity.name, "iPhone®")
        self.assertEqual(second.canonical_id, original.canonical_id)
        self.assertEqual(entity.mentions, 2)

    def test_beginning_and_ending_never_merge(self) -> None:
        # The 10-K balance sheet carries both of these in the same period, and
        # this pair scores a perfect 1.000 because the labels differ by one stem.
        # No threshold and no blocking setting may merge them.
        for fuzzy in (False, True):
            for blocking in ("token", "none"):
                for threshold in (0.01, 0.88, 1.0):
                    with self.subTest(fuzzy=fuzzy, blocking=blocking, threshold=threshold):
                        reg = metric_registry(
                            fuzzy=fuzzy, blocking=blocking, threshold=threshold
                        )
                        self.assertNotEqual(
                            reg.add(
                                {"name": "Common stock outstanding, beginning balances"}
                            ).canonical_id,
                            reg.add(
                                {"name": "Common stock outstanding, ending balances"}
                            ).canonical_id,
                        )

    def test_polarity_pairs_are_detected(self) -> None:
        for left, right in (
            ("beginning balances", "ending balances"),
            ("opening balance", "closing balance"),
            ("cash at start of period", "cash at end of period"),
            ("Net income", "Gross profit"),
        ):
            with self.subTest(left=left):
                self.assertTrue(polarity_conflict(left, right))

    def test_polarity_guard_does_not_block_real_variants(self) -> None:
        for left, right in (
            ("Total Revenues", "Revenues, Total"),
            ("Net cash provided by operating activities", "Net cash provided by operations"),
        ):
            with self.subTest(left=left):
                self.assertFalse(polarity_conflict(left, right))

    def test_polarity_conflict_is_counted(self) -> None:
        reg = metric_registry(fuzzy=True, threshold=0.01)
        reg.add({"name": "Common stock outstanding, beginning balances"})
        reg.add({"name": "Common stock outstanding, ending balances"})
        self.assertEqual(reg.stats["polarity_conflicts"], 1)

    def test_fuzzy_merges_genuine_variants_when_enabled(self) -> None:
        reg = metric_registry(fuzzy=True, threshold=0.90)
        first = reg.add({"name": "Total Revenues"})
        self.assertEqual(first.decision, "created")
        hit = reg.add({"name": "Revenues, Total"})
        self.assertEqual(hit.decision, "merged")
        self.assertEqual(hit.evidence, "fuzzy")
        self.assertEqual(hit.canonical_id, first.canonical_id)

    def test_fuzzy_is_off_by_default(self) -> None:
        reg = metric_registry()
        self.assertNotEqual(
            reg.add({"name": "Total Revenues"}).canonical_id,
            reg.add({"name": "Total Revenue"}).canonical_id,
        )

    def test_fuzzy_off_by_default_in_config(self) -> None:
        # Not a style preference. Replaying the real corpus with fuzzy on
        # produced four merges, all of them wrong: Apple's balance-sheet line
        # "Other non-current assets" against the cash-flow line "Other current
        # and non-current assets" (score 0.980), and the same pair for
        # liabilities. The labels are lexically near-identical and semantically
        # distinct, and no token guard separates them.
        self.assertFalse(ENTITY_FUZZY_MATCH)
        self.assertFalse(metric_registry().fuzzy)

    def test_scope_qualifiers_are_not_merged(self) -> None:
        # The false merge above, asserted in the shipped configuration. These
        # are different statements' lines, not two names for one line.
        reg = metric_registry()
        self.assertNotEqual(
            reg.add({"name": "Other non-current assets"}).canonical_id,
            reg.add({"name": "Other current and non-current assets"}).canonical_id,
        )
        self.assertNotEqual(
            reg.add({"name": "Other non-current liabilities"}).canonical_id,
            reg.add({"name": "Other current and non-current liabilities"}).canonical_id,
        )

    def test_ambiguous_merge_is_refused(self) -> None:
        # Three one-token names within the margin: no single winner.
        reg = metric_registry(fuzzy=True, threshold=0.70)
        reg.add({"name": "Goodwill"})
        reg.add({"name": "Goodwil"})
        reg.add({"name": "Goodwind"})
        self.assertEqual(reg.stats["ambiguous_merges"], 0)
        self.assertEqual(len(reg.entities()), 3)


class TestConceptRegistry(unittest.TestCase):
    def test_period_is_part_of_metric_identity(self) -> None:
        reg = concept_registry()
        fy25 = reg.register("metric", "Net Sales", scope="FY2025")
        fy24 = reg.register("metric", "Net Sales", scope="FY2024")
        self.assertNotEqual(fy25.canonical_id, fy24.canonical_id)
        self.assertEqual(
            reg.register("metric", "Net Sales", scope="FY2025").canonical_id, fy25.canonical_id
        )

    def test_segments_dedupe_across_filings(self) -> None:
        reg = concept_registry()
        ten_k = reg.register("segment", "iPhone", category="product")
        ten_q = reg.register("segment", "iPhone", category="product")
        self.assertEqual(ten_k.canonical_id, ten_q.canonical_id)
        self.assertEqual(reg.stats["created"], 1)
        self.assertEqual(reg.stats["merged"], 1)
        self.assertEqual(len(reg.entities("segment")), 1)

    def test_alias_seed_table_merges(self) -> None:
        reg = concept_registry()
        long_name, variants = next(iter(CONCEPT_ALIASES.items()))
        for variant in variants:
            with self.subTest(variant=variant):
                first = reg.register("metric", long_name, scope="FY2025")
                self.assertEqual(
                    reg.register("metric", variant, scope="FY2025").canonical_id,
                    first.canonical_id,
                )

    def test_counters_are_derived_not_accumulated(self) -> None:
        reg = concept_registry()
        for scope in ("FY2020", "FY2021", "FY2022", "FY2023", "FY2024", "FY2025"):
            for name in ("Net Sales", "Cost of Sales", "Gross Profit"):
                reg.register("metric", name, scope=scope)
        stats = reg.stats
        self.assertEqual(stats["created"], 18)
        self.assertEqual(stats["merged"], 0)
        # An accumulator that re-added each partition's *running* total would
        # report thousands of creations for eighteen entities.
        self.assertLess(stats["created"], 100)
        # Derived counters cannot drift between two reads.
        self.assertEqual(reg.stats, reg.stats)

    def test_mentions_accumulate_across_registrations(self) -> None:
        reg = concept_registry()
        for _ in range(3):
            reg.register("metric", "Net Sales", scope="FY2025")
        self.assertEqual(len(reg.entities("metric", "FY2025")), 1)
        self.assertEqual(reg.entities("metric", "FY2025")[0].mentions, 3)

    def test_lookups_are_kind_and_scope_scoped(self) -> None:
        reg = concept_registry()
        metric = reg.register("metric", "Net Sales", scope="FY2025")
        segment = reg.register("segment", "Net Sales")
        self.assertNotEqual(metric.canonical_id, segment.canonical_id)
        self.assertEqual(reg.lookup("metric", "Net Sales", scope="FY2025"), metric.canonical_id)
        self.assertIsNone(reg.lookup("metric", "Net Sales", scope="FY1999"))

    def test_no_placeholder_counters(self) -> None:
        reg = concept_registry()
        for key in reg.stats:
            with self.subTest(key=key):
                self.assertIn(key, EntityRegistry().stats)


class TestPersistence(unittest.TestCase):
    def build(self) -> tuple[Path, ConceptRegistry]:
        reg = concept_registry()
        reg.register("metric", "Revenue", scope="FY2025", aliases=["Turnover"])
        reg.register("metric", "Turnover", scope="FY2025")
        reg.register("metric", "Net Sales", scope="FY2025")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "concepts.json"
        reg.save(path)
        return path, reg

    def test_round_trip_preserves_identity(self) -> None:
        path, original = self.build()
        reloaded = ConceptRegistry.load(path, stable_id)
        self.assertEqual(
            {e.id for e in reloaded.entities("metric", "FY2025")},
            {e.id for e in original.entities("metric", "FY2025")},
        )
        merged = [e for e in reloaded.entities("metric", "FY2025") if len(e.aliases)]
        self.assertEqual(len(merged), 1)
        self.assertIn("Turnover", merged[0].aliases)

    def test_reingest_after_reload_is_idempotent(self) -> None:
        path, _ = self.build()
        reloaded = ConceptRegistry.load(path, stable_id)
        before = {e.id for e in reloaded.entities("metric", "FY2025")}
        reloaded.register("metric", "Revenue", scope="FY2025", aliases=["Turnover"])
        reloaded.register("metric", "Turnover", scope="FY2025")
        reloaded.register("metric", "Net Sales", scope="FY2025")
        self.assertEqual({e.id for e in reloaded.entities("metric", "FY2025")}, before)
        self.assertEqual(reloaded.stats["created"], 0)
        self.assertEqual(reloaded.stats["merged"], 3)

    def test_counters_start_clean_after_load(self) -> None:
        path, _ = self.build()
        reloaded = ConceptRegistry.load(path, stable_id)
        # The file records past merges; the process reports only its own work.
        self.assertEqual(reloaded.stats["created"], 0)
        self.assertEqual(reloaded.stats["merged"], 0)

    def test_overrides_win_over_saved_settings(self) -> None:
        path, _ = self.build()
        tightened = ConceptRegistry.load(path, stable_id, fuzzy=True, threshold=0.5)
        self.assertTrue(tightened.fuzzy)
        self.assertEqual(tightened.threshold, 0.5)
        from_file = ConceptRegistry.load(path, stable_id)
        self.assertFalse(from_file.fuzzy)
        self.assertEqual(from_file.threshold, 0.88)

    def test_entity_registry_load_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.json"
            metric_registry().save(path)
            self.assertEqual(EntityRegistry.load(path).kind, "metric")
            self.assertEqual(EntityRegistry.load(path, kind="segment").kind, "segment")
            self.assertTrue(EntityRegistry.load(path, fuzzy=True).fuzzy)

    def test_saved_file_is_versioned_json(self) -> None:
        path, _ = self.build()
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], ConceptRegistry.VERSION)
        self.assertIn("partitions", payload)
        self.assertEqual(len(payload["partitions"]), 1)


class TestResolveSubgraph(unittest.TestCase):
    """The live path builds its edges itself; this covers the reusable path."""

    def setUp(self) -> None:
        self.registry = concept_registry()

    def arc(self, source: str, target: str, **extra) -> dict:
        return {
            "source_kind": "metric",
            "source": source,
            "source_scope": "FY2025",
            "target_kind": "segment",
            "target": target,
            "action": "HAS_SEGMENT",
            **extra,
        }

    def nodes_named(self, result: dict, name: str) -> dict:
        return next(n for n in result["nodes"] if n["name"] == name)

    def test_cross_kind_arcs_supported(self) -> None:
        # A metric points at a segment, so the two endpoints are different
        # kinds. A single shared "kind" cannot express this graph's main arc.
        result = resolve_subgraph(
            {
                "entities": [
                    {"kind": "metric", "name": "Net Sales", "period": "FY2025"},
                    {"kind": "segment", "name": "iPhone", "period": ""},
                ],
                "relationships": [self.arc("Net Sales", "iPhone")],
            },
            self.registry,
        )
        self.assertEqual(len(result["edges"]), 1)
        edge = result["edges"][0]
        self.assertEqual(edge["source"], self.nodes_named(result, "Net Sales")["id"])
        self.assertEqual(edge["target"], self.nodes_named(result, "iPhone")["id"])

    def test_shared_kind_shape_still_works(self) -> None:
        result = resolve_subgraph(
            {
                "entities": [{"kind": "segment", "name": "iPhone", "period": ""}],
                "relationships": [
                    {"kind": "segment", "source": "iPhone", "target": "iPhone",
                     "action": "PART_OF"}
                ],
            },
            self.registry,
        )
        self.assertEqual(result["edges"], [])

    def test_endpoints_rewritten_and_edges_deduped(self) -> None:
        result = resolve_subgraph(
            {
                "entities": [
                    {"kind": "metric", "name": "Net Sales", "period": "FY2025"},
                    {"kind": "segment", "name": "iPhone", "period": ""},
                    {"kind": "segment", "name": "iPhone®", "period": ""},
                ],
                "relationships": [
                    self.arc("Net Sales", "iPhone"),
                    self.arc("Net Sales", "iPhone®"),
                ],
            },
            self.registry,
        )
        # The two spellings of the segment land on one node.
        self.assertEqual({n["name"] for n in result["nodes"]}, {"Net Sales", "iPhone"})
        # Two arcs differing only in spelling collapse to one edge.
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["stats"]["self_loops_dropped"], 0)

    def test_strict_raises_on_unresolved_endpoint(self) -> None:
        with self.assertRaises(KeyError):
            resolve_subgraph(
                {
                    "entities": [
                        {"kind": "metric", "name": "Net Sales", "period": "FY2025"}
                    ],
                    "relationships": [self.arc("Net Sales", "Never Registered")],
                },
                self.registry,
            )

    def test_non_strict_drops_arcs_to_unknown_nodes(self) -> None:
        result = resolve_subgraph(
            {
                "entities": [
                    {"kind": "metric", "name": "Net Sales", "period": "FY2025"}
                ],
                "relationships": [self.arc("Net Sales", "Never Registered")],
            },
            self.registry,
            strict=False,
        )
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["stats"]["orphans_dropped"], 1)

    def test_self_loops_removed(self) -> None:
        result = resolve_subgraph(
            {
                "entities": [{"kind": "segment", "name": "iPhone", "period": ""}],
                "relationships": [
                    {"kind": "segment", "source": "iPhone", "target": "iPhone",
                     "action": "PART_OF"}
                ],
            },
            self.registry,
        )
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["stats"]["self_loops_dropped"], 1)

    def test_duplicate_arc_keeps_the_longer_context(self) -> None:
        result = resolve_subgraph(
            {
                "entities": [
                    {"kind": "segment", "name": "iPhone", "period": ""},
                    {"kind": "segment", "name": "Mac", "period": ""},
                ],
                "relationships": [
                    {"kind": "segment", "source": "iPhone", "target": "Mac",
                     "action": "COMPETES_WITH", "context": "short"},
                    {"kind": "segment", "source": "iPhone", "target": "Mac",
                     "action": "COMPETES_WITH", "context": "a much longer explanation"},
                ],
            },
            self.registry,
        )
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["context"], "a much longer explanation")


class TestAliasSeedTable(unittest.TestCase):
    def test_seed_values_are_usable(self) -> None:
        # A seed that canonical_concept rewrites on one side but not the other
        # can never match, because lookups are canonicalised first.
        for canonical, variants in CONCEPT_ALIASES.items():
            with self.subTest(canonical=canonical):
                self.assertTrue(canonical_concept(canonical).name)
                for variant in variants:
                    self.assertTrue(variant.strip())
                    self.assertNotEqual(canonical_concept(variant).name, "")


if __name__ == "__main__":
    unittest.main()
