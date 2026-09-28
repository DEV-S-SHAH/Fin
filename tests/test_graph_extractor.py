"""Tests for the domain-agnostic knowledge-graph extractor.

The LLM is never called. A scripted client stands in for it so the tests cover
the parts that actually break in practice: malformed JSON, schema violations,
endpoints that do not resolve to a real entity, and providers that refuse
constrained decoding.

The cross-domain tests are the point of the module. If a category enum were
introduced, mathematics would start filing equations under whatever the enum
happened to allow, and these tests would fail.
"""

from __future__ import annotations

import builtins
import importlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_extractor as ge


# ---------------------------------------------------------------------------
# Scripted LLM client
# ---------------------------------------------------------------------------


class ScriptedCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError(f"unexpected extra call: {kwargs.get('model')}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))]
        )


def scripted(*responses):
    client = SimpleNamespace(chat=SimpleNamespace(completions=ScriptedCompletions(responses)))
    return client


def payload_of(*entities, relationships=()):
    return json.dumps({"entities": list(entities), "relationships": list(relationships)})


def entity(name, category="CONCEPT", description="d", aliases=()):
    return {
        "name": name,
        "category": category,
        "description": description,
        "aliases": list(aliases),
    }


def relation(source, target, action, context="c"):
    return {"source": source, "target": target, "action": action, "context": context}


# ---------------------------------------------------------------------------


class ActionNormalisationTests(unittest.TestCase):
    def test_snake_case_input_is_left_intact(self):
        # Regression: splitting on every capital shredded this into
        # D_E_R_I_V_E_S_F_R_O_M.
        self.assertEqual(ge.normalise_action("DERIVES_FROM"), "DERIVES_FROM")
        self.assertEqual(ge.normalise_action("PART_OF"), "PART_OF")

    def test_verb_phrases_are_folded(self):
        for raw in ("derived-from", "derivedFrom", "derived_from", "Derived From"):
            self.assertEqual(ge.normalise_action(raw), "DERIVED_FROM", raw)

    def test_copula_prefix_is_kept_rather_than_guessed_at(self):
        # Stripping a leading "is" would be a guess; the verb is kept verbatim
        # and the prompt is what asks for a single verb.
        self.assertEqual(ge.normalise_action("is derived from"), "IS_DERIVED_FROM")

    def test_consecutive_capitals_stay_together(self):
        self.assertEqual(ge.normalise_action("HTTPServer"), "HTTP_SERVER")
        self.assertEqual(ge.normalise_action("Multi Head Attention"), "MULTI_HEAD_ATTENTION")

    def test_punctuation_and_whitespace_collapse(self):
        self.assertEqual(ge.normalise_action("  correlates   with  "), "CORRELATES_WITH")
        self.assertEqual(ge.normalise_action("a---b"), "A_B")

    def test_empty_and_junk_return_empty_string(self):
        for raw in ("", "   ", "!!!", "---", None, 42, ["x"]):
            self.assertEqual(ge.normalise_action(raw), "")


class JsonRecoveryTests(unittest.TestCase):
    def assertRecovers(self, raw, expect_key="entities"):
        out = ge.recover_json(raw)
        self.assertIsInstance(out, dict)
        self.assertIn(expect_key, out)
        return out

    def test_clean_json(self):
        self.assertRecovers('{"entities": [], "relationships": []}')

    def test_markdown_fence_is_stripped(self):
        out = self.assertRecovers('```json\n{"entities": [{"name": "A"}], "relationships": []}\n```')
        self.assertEqual(out["entities"][0]["name"], "A")

    def test_prose_wrapper_is_digged_out(self):
        raw = 'Sure! Here you go:\n{"entities": [], "relationships": []}\nLet me know if you need more.'
        self.assertRecovers(raw)

    def test_trailing_commas(self):
        self.assertRecovers('{"entities": [], "relationships": [],}')

    def test_python_literals(self):
        out = self.assertRecovers('{"entities": [], "relationships": [], "flag": True, "n": None}')
        self.assertIs(out["flag"], True)
        self.assertIsNone(out["n"])

    def test_single_quoted_json(self):
        self.assertRecovers("{'entities': [], 'relationships': []}")

    def test_raw_newline_inside_string(self):
        # A literal newline inside a JSON string is invalid; the field text is
        # whitespace-collapsed later anyway, so substituting a space is safe.
        raw = '{"entities": [], "x": "line1' + chr(10) + 'line2"}'
        out = self.assertRecovers(raw)
        self.assertIn("line1 line2", out["x"])

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        out = self.assertRecovers('{"entities": [], "note": "a } brace", "relationships": []}')
        self.assertEqual(out["note"], "a } brace")

    def test_truncation_keeps_completed_records(self):
        raw = (
            '{"entities": [{"name": "A", "category": "C", "description": "d", "aliases": []}, '
            '{"name": "B", "cat'
        )
        out = ge.recover_json(raw)
        self.assertEqual([e["name"] for e in out["entities"]], ["A"])

    def test_truncation_inside_a_string_value(self):
        out = ge.recover_json('{"entities": [], "relationships": [], "note": "abc')
        self.assertEqual(out["entities"], [])

    def test_unrecoverable_output_raises(self):
        for raw in (None, "", "   ", "I cannot do that", "[1, 2, 3]"):
            with self.assertRaises(ge.ExtractionError):
                ge.recover_json(raw)


class SchemaContractTests(unittest.TestCase):
    def test_schema_matches_the_promised_shape(self):
        schema = ge.build_extraction_schema()["schema"]
        self.assertEqual(
            sorted(schema["required"]), ["entities", "relationships"]
        )
        entity_schema = schema["properties"]["entities"]["items"]
        self.assertEqual(
            sorted(entity_schema["required"]), ["aliases", "category", "description", "name"]
        )
        rel_schema = schema["properties"]["relationships"]["items"]
        self.assertEqual(
            sorted(rel_schema["required"]), ["action", "context", "source", "target"]
        )

    def test_schema_is_strict_and_closed(self):
        schema = ge.build_extraction_schema()
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertFalse(schema["schema"]["properties"]["entities"]["items"]["additionalProperties"])

    def test_aliases_is_an_array_of_strings(self):
        aliases = ge.build_extraction_schema()["schema"]["properties"]["entities"]["items"][
            "properties"
        ]["aliases"]
        self.assertEqual(aliases, {"type": "array", "items": {"type": "string"}})

    def test_category_and_action_are_not_enums(self):
        # An enum here would make the "domain-agnostic" claim false.
        props = ge.build_extraction_schema()["schema"]
        category = props["properties"]["entities"]["items"]["properties"]["category"]
        action = props["properties"]["relationships"]["items"]["properties"]["action"]
        self.assertEqual(category, {"type": "string"})
        self.assertEqual(action, {"type": "string"})
        self.assertNotIn("enum", json.dumps(ge.build_extraction_schema()))

    def test_schema_serialises(self):
        json.dumps(ge.build_extraction_schema())


class ValidationTests(unittest.TestCase):
    def test_clean_payload_passes_through(self):
        result = ge.validate_payload(
            {
                "entities": [entity("EBITDA", "METRIC", "Earnings before interest.")],
                "relationships": [],
            }
        )
        self.assertEqual(len(result.entities), 1)
        self.assertEqual(result.entities[0]["category"], "METRIC")
        self.assertEqual(result.entities_dropped, 0)

    def test_placeholder_entities_are_dropped(self):
        for name in ("unknown", "N/A", "none", "various", "", "   "):
            result = ge.validate_payload({"entities": [entity(name)], "relationships": []})
            self.assertEqual(result.entities, [], name)
            self.assertEqual(result.entities_dropped, 1, name)

    def test_duplicate_names_merge_aliases(self):
        result = ge.validate_payload(
            {
                "entities": [
                    entity("ATP", "MOLECULE", "Adenosine triphosphate", ["energy currency"]),
                    entity("atp", "MOLECULE", "dup", ["adenosine triphosphate"]),
                ],
                "relationships": [],
            }
        )
        self.assertEqual(len(result.entities), 1)
        self.assertEqual(
            sorted(result.entities[0]["aliases"]), ["adenosine triphosphate", "energy currency"]
        )
        self.assertEqual(result.entities_dropped, 1)

    def test_name_is_not_repeated_in_its_own_aliases(self):
        result = ge.validate_payload(
            {"entities": [entity("ATP", "MOLECULE", "d", ["ATP", "atp"])], "relationships": []}
        )
        self.assertEqual(result.entities[0]["aliases"], [])

    def test_missing_category_defaults(self):
        result = ge.validate_payload(
            {"entities": [{"name": "X", "description": "d", "aliases": []}], "relationships": []}
        )
        self.assertEqual(result.entities[0]["category"], "CONCEPT")

    def test_alias_string_is_wrapped_in_a_list(self):
        result = ge.validate_payload(
            {"entities": [{"name": "X", "category": "C", "description": "d", "aliases": "L"}], "relationships": []}
        )
        self.assertEqual(result.entities[0]["aliases"], ["L"])

    def test_alias_objects_are_unwrapped(self):
        result = ge.validate_payload(
            {
                "entities": [
                    {"name": "X", "category": "C", "description": "d", "aliases": [{"alias": "Y"}]}
                ],
                "relationships": [],
            }
        )
        self.assertEqual(result.entities[0]["aliases"], ["Y"])

    def test_endpoint_resolves_through_an_alias(self):
        result = ge.validate_payload(
            {
                "entities": [
                    entity("Lagrangian", "QUANTITY", "d", ["L"]),
                    entity("Euler-Lagrange Equation", "EQUATION", "d"),
                ],
                "relationships": [relation("L", "Euler-Lagrange Equation", "appears in")],
            }
        )
        self.assertEqual(result.relationships_dropped, 0)
        self.assertEqual(result.relationships[0]["source"], "Lagrangian")
        self.assertEqual(result.relationships[0]["target"], "Euler-Lagrange Equation")

    def test_dangling_endpoints_are_dropped(self):
        result = ge.validate_payload(
            {
                "entities": [entity("A", "C", "d")],
                "relationships": [relation("A", "Ghost", "OWNS")],
            }
        )
        self.assertEqual(result.relationships, [])
        self.assertEqual(result.relationships_dropped, 1)

    def test_self_loops_are_dropped(self):
        result = ge.validate_payload(
            {
                "entities": [entity("A", "C", "d")],
                "relationships": [relation("A", "A", "OWNS")],
            }
        )
        self.assertEqual(result.relationships, [])
        self.assertEqual(result.relationships_dropped, 1)

    def test_relationship_without_an_action_is_dropped(self):
        result = ge.validate_payload(
            {
                "entities": [entity("A", "C", "d"), entity("B", "C", "d")],
                "relationships": [relation("A", "B", "!!!")],
            }
        )
        self.assertEqual(result.relationships, [])

    def test_duplicate_triples_collapse(self):
        result = ge.validate_payload(
            {
                "entities": [entity("A", "C", "d"), entity("B", "C", "d")],
                "relationships": [relation("A", "B", "OWNS"), relation("A", "B", "OWNS")],
            }
        )
        self.assertEqual(len(result.relationships), 1)
        self.assertEqual(result.relationships_dropped, 1)

    def test_endpoints_are_rewritten_to_canonical_names(self):
        result = ge.validate_payload(
            {
                "entities": [entity("Euler-Lagrange Equation", "EQUATION", "d")],
                "relationships": [relation("euler lagrange equation", "Euler-Lagrange Equation", "x")],
            }
        )
        self.assertEqual(result.relationships_dropped, 1)  # self-loop after resolution

    def test_case_and_whitespace_variation_resolves(self):
        result = ge.validate_payload(
            {
                "entities": [entity("Noam Shazeer", "PERSON", "d")],
                "relationships": [relation("  noam   shazeer ", "Noam Shazeer.", "employs")],
            }
        )
        # Both endpoints resolve to the same entity, so this is a self-loop.
        self.assertEqual(result.relationships_dropped, 1)

    def test_word_order_is_not_guessed_at(self):
        # Fuzzy matching must not reorder "shazeer, noam" into "Noam Shazeer";
        # that would silently invent a name the model never wrote.
        result = ge.validate_payload(
            {
                "entities": [entity("Noam Shazeer", "PERSON", "d")],
                "relationships": [relation("shazeer, noam", "Attention Is All You Need", "wrote")],
            }
        )
        self.assertEqual(result.relationships_dropped, 1)

    def test_one_bad_entity_does_not_discard_the_good_ones(self):
        result = ge.validate_payload(
            {
                "entities": [entity("Good", "C", "d"), {"name": None, "category": "C"}],
                "relationships": [],
            }
        )
        self.assertEqual([e["name"] for e in result.entities], ["Good"])
        self.assertEqual(result.entities_dropped, 1)

    def test_whitespace_is_collapsed_in_text_fields(self):
        result = ge.validate_payload(
            {
                "entities": [entity("A", "C", "spans\n  two   lines")],
                "relationships": [],
            }
        )
        self.assertEqual(result.entities[0]["description"], "spans two lines")

    def test_non_dict_payload_raises(self):
        with self.assertRaises(ge.ExtractionError):
            ge.validate_payload(["not", "a", "dict"])

    def test_entities_wrapped_in_a_dict_are_unwrapped(self):
        result = ge.validate_payload(
            {"entities": {"0": entity("A", "C", "d")}, "relationships": []}
        )
        self.assertEqual([e["name"] for e in result.entities], ["A"])

    def test_to_dict_is_the_plain_contract(self):
        result = ge.validate_payload(
            {"entities": [entity("A", "C", "d")], "relationships": []}
        )
        self.assertEqual(sorted(result.to_dict()), ["entities", "relationships"])


class ExtractTriplesTests(unittest.TestCase):
    def test_returns_the_exact_contract_shape(self):
        client = scripted(
            payload_of(
                entity(
                    "Mitochondria",
                    "BIOLOGICAL_STRUCTURE",
                    "The organelle that produces ATP.",
                    ["powerhouse of the cell"],
                ),
                entity("ATP", "MOLECULE", "Adenosine triphosphate, the cell's energy currency."),
                relationships=[
                    relation(
                        "Mitochondria",
                        "ATP",
                        "produces",
                        "By oxidative phosphorylation.",
                    )
                ],
            )
        )
        out = ge.extract_triples("Mitochondria produce ATP.", client=client, model="m")
        self.assertEqual(sorted(out), ["entities", "relationships"])
        self.assertEqual(sorted(out["entities"][0]), ["aliases", "category", "description", "name"])
        self.assertEqual(sorted(out["relationships"][0]), ["action", "context", "source", "target"])
        self.assertEqual(out["relationships"][0]["action"], "PRODUCES")
        self.assertEqual(out["relationships"][0]["source"], "Mitochondria")

    def test_empty_chunk_short_circuits_without_calling_the_model(self):
        out = ge.extract_triples("   ", client=scripted(), model="m")
        self.assertEqual(out, {"entities": [], "relationships": []})

    def test_non_string_chunk_raises(self):
        with self.assertRaises(ge.ExtractionError):
            ge.extract_triples(None, client=scripted(), model="m")

    def test_strict_schema_is_requested_first(self):
        client = scripted(payload_of())
        ge.extract_triples("text", client=client, model="m")
        first = client.chat.completions.calls[0]
        self.assertEqual(first["response_format"]["type"], "json_schema")
        self.assertTrue(first["response_format"]["json_schema"]["strict"])

    def test_system_prompt_forbids_a_fixed_vocabulary(self):
        client = scripted(payload_of())
        ge.extract_triples("text", client=client, model="m")
        system = client.chat.completions.calls[0]["messages"][0]["content"]
        self.assertIn("not a permitted list", system)
        self.assertIn("UPPER_SNAKE_CASE", system)

    def test_malformed_model_output_is_recovered(self):
        raw = 'Here:\n```json\n{"entities": [{"name": "A", "category": "C", "description": "d", "aliases": []}], "relationships": [],}\n```'
        out = ge.extract_triples("text", client=scripted(raw), model="m")
        self.assertEqual([e["name"] for e in out["entities"]], ["A"])

    def test_passage_with_nothing_extractable_is_a_success(self):
        out = ge.extract_triples("...", client=scripted(payload_of()), model="m")
        self.assertEqual(out, {"entities": [], "relationships": []})

    def test_unrecoverable_output_raises(self):
        with self.assertRaises(ge.ExtractionError):
            ge.extract_triples("text", client=scripted("sorry, no."), model="m")

    def test_credential_is_never_sent_in_the_prompt(self):
        client = scripted(payload_of())
        ge.extract_triples("secretless", client=client, model="m")
        blob = json.dumps(client.chat.completions.calls[0])
        self.assertNotIn("nvapi-", blob)
        self.assertNotIn("sk-", blob)


class ProviderFallbackTests(unittest.TestCase):
    def test_strict_rejection_falls_back_to_json_object(self):
        client = scripted(
            Exception("400 unknown parameter: response_format json_schema"),
            payload_of(entity("A", "C", "d")),
        )
        out = ge.extract_triples("text", client=client, model="m")
        self.assertEqual(len(out["entities"]), 1)
        self.assertEqual(client.chat.completions.calls[1]["response_format"]["type"], "json_object")

    def test_final_fallback_drops_the_response_format(self):
        client = scripted(
            Exception("400 bad response_format"),
            Exception("400 bad response_format"),
            payload_of(entity("A", "C", "d")),
        )
        ge.extract_triples("text", client=client, model="m")
        self.assertNotIn("response_format", client.chat.completions.calls[2])

    def test_auth_failure_raises_without_retrying(self):
        client = scripted(Exception("401 invalid api key"))
        with self.assertRaises(ge.ExtractionError):
            ge.extract_triples("text", client=client, model="m")
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_empty_completion_raises(self):
        client = scripted("   ")
        with self.assertRaises(ge.ExtractionError):
            ge.extract_triples("text", client=client, model="m")

    def test_openrouter_default_base_url(self):
        self.assertEqual(ge.DEFAULT_OPENROUTER_BASE_URL, "https://openrouter.ai/api/v1")
        self.assertEqual(ge.DEFAULT_OPENAI_BASE_URL, "https://api.openai.com/v1")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ge.ExtractionError):
            ge.create_client(provider="bedrock", api_key="x")

    def test_missing_credential_raises(self):
        saved = {k: __import__("os").environ.pop(k, None) for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY")}
        try:
            with self.assertRaises(ge.ExtractionError):
                ge.create_client()
        finally:
            for key, value in saved.items():
                if value is not None:
                    __import__("os").environ[key] = value

    def test_client_is_constructed_for_a_known_provider(self):
        client = ge.create_client(model="gpt-4o-mini", provider="openai", api_key="test-key")
        self.assertTrue(str(client.base_url).startswith("https://api.openai.com"))


class DomainAgnosticTests(unittest.TestCase):
    """The same code path has to work for four unrelated vocabularies."""

    CASES = {
        "math": (
            "The Euler-Lagrange equation minimises the Lagrangian L under a fixed "
            "boundary condition, which recovers Hamilton's equations of motion.",
            {"Euler-Lagrange Equation", "Lagrangian", "Hamilton's Equations"},
            {"EQUATION", "QUANTITY", "PRINCIPLE"},
        ),
        "biology": (
            "Mitochondria generate most ATP by oxidative phosphorylation, which "
            "occurs across the inner mitochondrial membrane.",
            {"Mitochondria", "ATP", "Oxidative Phosphorylation"},
            {"BIOLOGICAL_STRUCTURE", "MOLECULE", "PROCESS"},
        ),
        "finance": (
            "Adjusted EBITDA excludes restructuring charges from reported EBITDA, "
            "so lenders compare the adjusted figure when sizing leverage.",
            {"Adjusted EBITDA", "EBITDA", "Restructuring Charges"},
            {"METRIC", "CONCEPT"},
        ),
        "engineering": (
            "A heat exchanger transfers heat between two fluid streams, and the "
            "counterflow arrangement raises its effectiveness.",
            {"Heat Exchanger", "Counterflow Arrangement"},
            {"DEVICE", "CONFIGURATION"},
        ),
    }

    def test_every_domain_extracts_and_keeps_its_own_vocabulary(self):
        seen_categories = set()
        for domain, (text, expected_names, expected_categories) in self.CASES.items():
            # A hand-written "response" for this passage, as a model would give.
            names = list(expected_names)
            raw = payload_of(
                *[entity(name, "CATEGORY_PLACEHOLDER", "described") for name in names],
                relationships=[
                    relation(names[0], names[1], "connects to", "Because the passage says so.")
                ],
            )
            out = ge.extract_triples(text, client=scripted(raw), model="m")
            self.assertEqual([e["name"] for e in out["entities"]], names, domain)
            self.assertEqual(out["relationships"][0]["action"], "CONNECTS_TO", domain)
            seen_categories.add("CATEGORY_PLACEHOLDER")
        self.assertEqual(len(seen_categories), 1)

    def test_categories_survive_verbatim_when_the_model_invents_them(self):
        raw = payload_of(
            entity("Euler-Lagrange Equation", "EQUATION", "d"),
            entity("Lagrangian", "QUANTITY", "d"),
            entity("EBITDA", "FINANCIAL_METRIC", "d"),
            entity("Mitochondrion", "BIOLOGICAL_STRUCTURE", "d"),
            entity("Heat Exchanger", "THERMAL_DEVICE", "d"),
            entity("Laws of Motion", "PHYSICAL_LAW", "d"),
        )
        out = ge.extract_triples("mixed", client=scripted(raw), model="m")
        categories = {e["name"]: e["category"] for e in out["entities"]}
        self.assertEqual(
            categories,
            {
                "Euler-Lagrange Equation": "EQUATION",
                "Lagrangian": "QUANTITY",
                "EBITDA": "FINANCIAL_METRIC",
                "Mitochondrion": "BIOLOGICAL_STRUCTURE",
                "Heat Exchanger": "THERMAL_DEVICE",
                "Laws of Motion": "PHYSICAL_LAW",
            },
        )

    def test_no_category_is_silently_rewritten(self):
        for category in ("EQUATION", "BIOLOGICAL_STRUCTURE", "METRIC", "CONCEPT", "PERSON"):
            out = ge.validate_payload({"entities": [entity("X", category, "d")], "relationships": []})
            self.assertEqual(out.entities[0]["category"], category)


class CorpusTests(unittest.TestCase):
    def test_errors_are_collected_not_raised(self):
        client = scripted(
            payload_of(entity("A", "C", "d")),
            Exception("500 boom"),
            payload_of(entity("B", "C", "d")),
        )
        results, errors = ge.extract_corpus(["one", "two", "three"], client=client, model="m")
        self.assertEqual(len(results), 2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["index"], 1)

    def test_raise_mode_fails_fast(self):
        client = scripted(Exception("500 boom"))
        with self.assertRaises(ge.ExtractionError):
            ge.extract_corpus(["a"], on_error="raise", client=client, model="m")

    def test_invalid_on_error_raises(self):
        with self.assertRaises(ge.ExtractionError):
            ge.extract_corpus(["a"], on_error="explode", client=scripted(), model="m")

    def test_empty_corpus(self):
        results, errors = ge.extract_corpus([], client=scripted(), model="m")
        self.assertEqual((results, errors), ([], []))


class NoPydanticFallbackTests(unittest.TestCase):
    """The hand-written validator must behave like the pydantic one."""

    PAYLOAD = {
        "entities": [
            entity("EBITDA", "METRIC", "Earnings before interest.", ["earnings before interest"]),
            entity("Adjusted EBITDA", "METRIC", "Excludes one-offs."),
            entity("unknown", "X", "d"),
        ],
        "relationships": [
            relation("earnings before interest", "adjusted ebitda", "is subtracted from"),
            relation("Adjusted EBITDA", "Ghost", "OWNS"),
        ],
    }

    def _reimport_without_pydantic(self):
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "pydantic" or name.startswith("pydantic."):
                raise ImportError("pydantic blocked for this test")
            return real_import(name, *args, **kwargs)

        saved = {k: v for k, v in sys.modules.items() if k == "graph_extractor"}
        for key in saved:
            del sys.modules[key]
        builtins.__import__ = blocked
        try:
            return importlib.import_module("graph_extractor")
        finally:
            builtins.__import__ = real_import
            sys.modules.pop("graph_extractor", None)
            sys.modules.update(saved)

    def test_fallback_path_produces_the_same_result(self):
        module = self._reimport_without_pydantic()
        self.assertFalse(module.PYDANTIC_AVAILABLE)
        result = module.validate_payload(self.PAYLOAD)
        self.assertEqual([e["name"] for e in result.entities], ["EBITDA", "Adjusted EBITDA"])
        self.assertEqual(result.entities_dropped, 1)
        self.assertEqual(len(result.relationships), 1)
        self.assertEqual(result.relationships[0]["source"], "EBITDA")
        self.assertEqual(result.relationships[0]["target"], "Adjusted EBITDA")
        self.assertEqual(result.relationships[0]["action"], "IS_SUBTRACTED_FROM")
        self.assertEqual(result.relationships_dropped, 1)

    def test_fallback_path_handles_a_bad_record(self):
        module = self._reimport_without_pydantic()
        result = module.validate_payload(
            {"entities": [{"name": "Good", "category": "C"}, {"category": "C"}], "relationships": []}
        )
        self.assertEqual([e["name"] for e in result.entities], ["Good"])

    def test_both_paths_agree(self):
        with_pydantic = ge.validate_payload(self.PAYLOAD).to_dict()
        without = self._reimport_without_pydantic().validate_payload(self.PAYLOAD).to_dict()
        self.assertEqual(with_pydantic, without)


if __name__ == "__main__":
    unittest.main()
