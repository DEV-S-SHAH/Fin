"""Tests for graph_store: LadybugDB ingest, pivot search, relevance expansion.

Every test runs against a real on-disk database. The engine behaviours this
module works around -- empty UNWIND, missing secondary indexes, MERGE semantics
on relationship patterns -- only misbehave against a real database, so mocking
them away would test the mock.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import entity_resolver as er
import graph_store as gs


SAMPLE_GRAPH: dict = {
    "nodes": [
        {
            "id": "euler-lagrange-equation",
            "name": "Euler-Lagrange equation",
            "category": "EQUATION",
            "description": "Stationarity condition for an action.",
            "aliases": ["ELE", "Euler Lagrange equations"],
        },
        {
            "id": "lagrangian",
            "name": "Lagrangian",
            "category": "QUANTITY",
            "description": "Kinetic energy minus potential energy.",
            "aliases": ["L", "kinetic minus potential"],
        },
        {
            "id": "hamilton-equations",
            "name": "Hamilton's equations of motion",
            "category": "EQUATION",
            "description": "First-order equations of motion.",
            "aliases": ["Hamilton equations"],
        },
        {
            "id": "action",
            "name": "Action",
            "category": "QUANTITY",
            "description": "Time integral of the Lagrangian.",
            "aliases": [],
        },
    ],
    "edges": [
        {
            "source": "action",
            "target": "lagrangian",
            "action": "INTEGRATES",
            "context": "The action is the integral of the Lagrangian over time.",
        },
        {
            "source": "euler-lagrange-equation",
            "target": "lagrangian",
            "action": "MINIMIZES",
            "context": "Its extremum is the Euler-Lagrange equation.",
        },
        {
            "source": "euler-lagrange-equation",
            "target": "hamilton-equations",
            "action": "RECOVERS",
            "context": "Setting the variation to zero recovers Hamilton's equations.",
        },
    ],
}


def ent(eid: str, name: str, aliases: list[str] | None = None, **kw) -> dict:
    return {
        "id": eid,
        "name": name,
        "category": kw.get("category", "C"),
        "description": kw.get("description", ""),
        "aliases": aliases or [],
    }


def edge(src: str, dst: str, action: str, context: str = "") -> dict:
    return {"source": src, "target": dst, "action": action, "context": context}


class StoreTestCase(unittest.TestCase):
    """Base class giving each test a private database directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="graph-store-test-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "kg.lbug")

    def open(self, **kw) -> gs.GraphStore:
        return self.open_at(self.path, **kw)

    def open_at(self, path: str, **kw) -> gs.GraphStore:
        """Open ``path`` as its own store.

        A second handle on a path that is already open is refused: the engine
        takes an exclusive lock on the database file, and a second open fails
        with ``IO exception: Could not set lock on file ...``. That is the
        better of the two behaviours -- the earlier one opened and then silently
        diverged, each handle seeing only some of the writes -- but it means a
        test that needs a *different* dataset has to name a *different* path
        rather than open the same one twice. Those are the only two shapes a
        test here can want: this class's ``setUp`` already holds
        ``self.path`` open, so a second dataset is ``open_at`` on a new name.
        """
        store = gs.open_store(path, **kw)
        self.addCleanup(store.close)
        return store

    def seeded(self, **kw) -> gs.GraphStore:
        store = self.open(**kw)
        store.ingest_graph(SAMPLE_GRAPH)
        return store


class InlineLimitTests(unittest.TestCase):
    """`LIMIT` is interpolated, so the interpolator is the injection boundary."""

    def test_integers_render_verbatim(self):
        self.assertEqual(gs._sql_int(0, "x"), "0")
        self.assertEqual(gs._sql_int(7, "x"), "7")
        self.assertEqual(gs._sql_int(10**9, "x"), str(10**9))

    def test_non_integers_are_refused(self):
        # Coercing instead of raising would hide a caller bug, and a float is
        # the shape an injection payload would arrive in.
        for bad in ("5", "5; DROP TABLE Entity", 2.7, True, None, [5]):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    gs._sql_int(bad, "x")

    def test_negative_values_are_refused(self):
        with self.assertRaises(ValueError):
            gs._sql_int(-1, "x")


class SchemaTests(StoreTestCase):
    def test_schema_is_created_and_idempotent(self):
        with gs.open_store(self.path) as store:
            self.assertTrue(store.has_schema())
            store.create_schema()
            store.create_schema()
            self.assertTrue(store.has_schema())

    def test_require_schema_raises_on_empty_database(self):
        with gs.GraphStore(self.path) as store:
            with self.assertRaises(gs.GraphStoreError):
                store.require_schema()

    def test_schema_columns_match_the_specification(self):
        with gs.open_store(self.path) as store:
            info = gs._fetch(store.conn, "CALL table_info('Entity') RETURN *")
            columns = {row["name"]: row["type"] for row in info}
            self.assertEqual(
                columns,
                {
                    "id": "STRING",
                    "name": "STRING",
                    "category": "STRING",
                    "description": "STRING",
                    "aliases": "STRING",
                },
            )
            self.assertEqual(info[0]["default expression"], "NULL")
            self.assertEqual([r["name"] for r in info if r["primary key"]], ["id"])

    def test_relationship_table_connects_entity_to_entity(self):
        with gs.open_store(self.path) as store:
            row = gs._fetch(store.conn, "CALL show_connection('RELATION') RETURN *")[0]
            self.assertEqual(row["source table name"], "Entity")
            self.assertEqual(row["destination table name"], "Entity")
            self.assertEqual(row["source table primary key"], "id")
            self.assertEqual(row["destination table primary key"], "id")

    def test_buffer_pool_is_configured_in_bytes(self):
        # The engine takes an int count of bytes, so "256" would mean 256 bytes.
        self.assertEqual(gs.BUFFER_POOL_BYTES, 256 * 1024 * 1024)
        with mock.patch.object(gs.lb, "Database", wraps=gs.lb.Database) as spy:
            with gs.open_store(self.path, buffer_pool_mb=256) as store:
                self.assertEqual(store.buffer_pool_mb, 256)
        _, kwargs = spy.call_args
        self.assertEqual(kwargs["buffer_pool_size"], 256 * 1024 * 1024)

    def test_invalid_buffer_pool_is_rejected(self):
        for bad in (0, -1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gs.GraphStore(self.path, buffer_pool_mb=bad)

    def test_closed_store_rejects_use(self):
        store = gs.GraphStore(self.path)
        store.create_schema()
        store.close()
        store.close()  # idempotent
        with self.assertRaises(gs.GraphStoreError):
            _ = store.conn

    def test_only_primary_key_is_indexed(self):
        # The "Pivot Search Index" is an access pattern this module maintains,
        # not a database index: CREATE INDEX does not parse in 0.20.4.
        with self.open() as store:
            indexes = store.index_report()
            self.assertTrue(indexes)
            self.assertEqual(indexes[0]["index_name"], "_PK")
            self.assertEqual(indexes[0]["index_type"], "HASH")
            self.assertEqual(indexes[0]["property_names"], ["id"])


class BulkCopyTests(StoreTestCase):
    """`COPY` reads a bound Arrow table, so the path is data and not SQL.

    The loader used to inline the path into ``COPY Entity FROM "<path>"``, which
    needed a per-path quoting scheme and refused any path it could not express.
    This engine's grammar has no ``FROM 'literal'`` form at all -- it stops at
    the keyword -- so that call did not degrade, it raised on the first load.
    Reading the CSV here and binding the table is the form the engine has, and it
    removes the quoting question rather than answering it.
    """

    HEADER = "id,name,category,description,aliases\n"
    REL_HEADER = "FROM,TO,action,context\n"

    def write_csvs(self, dirname: str) -> tuple[str, str]:
        directory = os.path.join(self._tmp.name, dirname)
        os.makedirs(directory, exist_ok=True)
        entity_csv = os.path.join(directory, "e.csv")
        relation_csv = os.path.join(directory, "r.csv")
        with open(entity_csv, "w", encoding="utf-8") as handle:
            handle.write(self.HEADER + 'n1,Alpha,Q,first,"a, b"\nn2,Beta,Q,second,\n')
        with open(relation_csv, "w", encoding="utf-8") as handle:
            handle.write(self.REL_HEADER + "n1,n2,RELATES,ctx\n")
        return entity_csv, relation_csv

    def test_copy_loads_entities_and_relations(self):
        with self.open() as store:
            entity_csv, relation_csv = self.write_csvs("plain")
            store.copy_from_csv(entity_csv, relation_csv)
            self.assertEqual(store.counts(), {"entities": 2, "relations": 1})
            row = gs._fetch(store.conn, "MATCH (e:Entity) WHERE e.id = 'n1' RETURN e.aliases AS a")[0]
            self.assertEqual(row["a"], "a, b")

    def test_header_defaults_to_true_so_the_header_row_is_not_an_entity(self):
        # This engine's COPY defaults to HEADER=false, which ingests the header
        # row as data and produces an entity literally named "name".
        with self.open() as store:
            entity_csv, _ = self.write_csvs("hdr")
            store.copy_from_csv(entity_csv)
            names = [r["name"] for r in gs._fetch(store.conn, "MATCH (e:Entity) RETURN e.name AS name")]
            self.assertEqual(sorted(names), ["Alpha", "Beta"])

    def test_header_false_ingests_the_header_row_as_data(self):
        # The documented hazard, asserted so it cannot be mistaken for a bug in
        # the default: with HEADER=false the header row becomes an entity whose
        # id is the second column's name.
        with self.open() as store:
            entity_csv, _ = self.write_csvs("nohdr")
            with open(entity_csv, "a", encoding="utf-8") as handle:
                handle.write("n3,Gamma,Q,third,\n")
            store.copy_from_csv(entity_csv, header=False)
            self.assertEqual(store.counts()["entities"], 4)
            self.assertIn("name", {r["name"] for r in gs._fetch(store.conn, "MATCH (e:Entity) RETURN e.name AS name")})

    def test_a_path_containing_quote_characters_still_loads(self):
        # The regression: the path used to be inlined into the statement, so it
        # needed a quoting scheme and a path it could not express was refused.
        # Nothing in the path reaches SQL now, so there is nothing to quote.
        for dirname in ("it's data", 'we"ird', "both'\"kinds", "plain"):
            with self.subTest(dirname=dirname):
                target = os.path.join(self._tmp.name, f"q{dirname}.lbug")
                try:
                    os.makedirs(os.path.join(self._tmp.name, dirname), exist_ok=True)
                except OSError as exc:
                    # Windows refuses a double quote in a path component
                    # outright, so the filesystem -- not the loader -- is what
                    # rules this case out there. Skipped rather than asserted,
                    # so POSIX keeps the coverage instead of losing it.
                    self.skipTest(f"this filesystem rejects the path: {exc}")
                with self.open_at(target) as store:
                    entity_csv, relation_csv = self.write_csvs(dirname)
                    store.copy_from_csv(entity_csv, relation_csv)
                    self.assertEqual(store.counts(), {"entities": 2, "relations": 1})

    def test_an_unusable_path_is_refused_before_the_file_is_opened(self):
        # The validation that mattered when the path was SQL, and still does:
        # a NUL truncates the name in the OS layer, so the caller would read a
        # different file than the one it named.
        for bad in ("", 5, None, "a\x00b"):
            with self.subTest(bad=bad):
                with self.assertRaises((TypeError, ValueError)):
                    gs._read_csv(bad, gs.ENTITY_COLUMNS)

    def test_copy_is_not_idempotent_and_says_so(self):
        with self.open() as store:
            entity_csv, _ = self.write_csvs("twice")
            store.copy_from_csv(entity_csv)
            with self.assertRaises(Exception) as caught:
                store.copy_from_csv(entity_csv)
            self.assertIn("primary key", str(caught.exception).lower())


class IngestTests(StoreTestCase):
    def test_ingests_nodes_and_edges(self):
        with self.open() as store:
            report = store.ingest_graph(SAMPLE_GRAPH)
            self.assertEqual(report.entities, 4)
            self.assertEqual(report.relations, 3)
            self.assertEqual(store.counts(), {"entities": 4, "relations": 3})

    def test_reingest_is_idempotent(self):
        with self.open() as store:
            store.ingest_graph(SAMPLE_GRAPH)
            store.ingest_graph(SAMPLE_GRAPH)
            store.ingest_graph(SAMPLE_GRAPH)
            self.assertEqual(store.counts(), {"entities": 4, "relations": 3})

    def test_reingest_updates_properties_rather_than_ignoring_them(self):
        with self.open() as store:
            store.ingest_graph(SAMPLE_GRAPH)
            changed = json.loads(json.dumps(SAMPLE_GRAPH))
            changed["nodes"][0]["description"] = "A much better description."
            changed["nodes"][0]["aliases"] = ["ELE", "Euler-Lagrange"]
            store.ingest_graph(changed)
            rows = gs._fetch(
                store.conn,
                "MATCH (e:Entity {id: $id}) RETURN e.description AS d, e.aliases AS a",
                {"id": "euler-lagrange-equation"},
            )
            self.assertEqual(rows[0]["d"], "A much better description.")
            self.assertEqual(rows[0]["a"], "ELE, Euler-Lagrange")
            self.assertEqual(store.counts()["entities"], 4)

    def test_distinct_actions_between_the_same_pair_coexist(self):
        # MERGE on a bare relationship pattern would match the existing edge and
        # silently drop the second action.
        with self.open() as store:
            store.ingest_graph(
                {
                    "nodes": [ent("a", "A"), ent("b", "B")],
                    "edges": [edge("a", "b", "CAUSES", "c1"), edge("a", "b", "DERIVES", "c2")],
                }
            )
            rows = gs._fetch(
                store.conn,
                "MATCH (a:Entity {id:'a'})-[r:RELATION]->(b:Entity {id:'b'}) "
                "RETURN r.action AS action ORDER BY r.action",
            )
            self.assertEqual([r["action"] for r in rows], ["CAUSES", "DERIVES"])

    def test_duplicate_edge_in_one_batch_is_collapsed_keeping_longest_context(self):
        with self.open() as store:
            report = store.ingest_graph(
                {
                    "nodes": [ent("a", "A"), ent("b", "B")],
                    "edges": [
                        edge("a", "b", "CAUSES", "short"),
                        edge("a", "b", "CAUSES", "a much longer explanation"),
                    ],
                }
            )
            self.assertEqual(report.relations, 1)
            self.assertEqual(report.relations_skipped_duplicate, 1)
            rows = gs._fetch(
                store.conn,
                "MATCH ()-[r:RELATION]->() RETURN r.context AS c",
            )
            self.assertEqual(rows[0]["c"], "a much longer explanation")

    def test_self_loops_are_refused(self):
        with self.open() as store:
            report = store.ingest_graph(
                {"nodes": [ent("a", "A")], "edges": [edge("a", "a", "LOOPS")]}
            )
            self.assertEqual(report.relations_skipped_self_loop, 1)
            self.assertEqual(report.relations, 0)  # a report, not a count
            self.assertEqual(store.counts()["relations"], 0)

    def test_orphan_edges_are_skipped_and_reported(self):
        with self.open() as store:
            report = store.ingest_graph(
                {"nodes": [ent("a", "A")], "edges": [edge("a", "ghost", "X")]}
            )
            self.assertEqual(report.relations, 0)
            self.assertEqual(report.relations_skipped_orphan, 1)
            self.assertEqual(report.missing_endpoints, ["ghost"])

    def test_orphan_edges_can_raise_instead(self):
        with self.open() as store:
            with self.assertRaises(gs.GraphStoreError) as caught:
                store.ingest_graph(
                    {"nodes": [ent("a", "A")], "edges": [edge("a", "ghost", "X")]},
                    on_orphan="raise",
                )
            self.assertIn("ghost", str(caught.exception))

    def test_invalid_on_orphan_is_rejected(self):
        with self.open() as store:
            with self.assertRaises(ValueError):
                store.ingest_relations([edge("a", "b", "X")], on_orphan="explode")

    def test_entities_without_an_id_are_rejected_with_a_reason(self):
        with self.open() as store:
            report = gs.IngestReport()
            written = store.ingest_entities(
                [{"name": "No id here"}, ent("ok", "Fine"), "not a mapping", 42],
                report=report,
            )
            self.assertEqual(written, 1)
            self.assertEqual(len(report.entities_rejected), 3)
            self.assertTrue(any("missing id" in r for r in report.entities_rejected))

    def test_missing_optional_fields_become_empty_strings(self):
        with self.open() as store:
            store.ingest_entities([{"id": "bare"}])
            row = gs._fetch(
                store.conn,
                "MATCH (e:Entity {id:'bare'}) RETURN e.name AS n, e.category AS c, "
                "e.description AS d, e.aliases AS a",
            )[0]
            self.assertEqual(row, {"n": "", "c": "", "d": "", "a": ""})

    def test_aliases_are_stored_comma_separated(self):
        with self.open() as store:
            store.ingest_entities([ent("a", "A", ["one", "two", "three"])])
            row = gs._fetch(store.conn, "MATCH (e:Entity {id:'a'}) RETURN e.aliases AS a")[0]
            self.assertEqual(row["a"], "one, two, three")
            self.assertEqual(gs._split_aliases(row["a"]), ("one", "two", "three"))

    def test_aliases_accept_a_prejoined_string(self):
        with self.open() as store:
            store.ingest_entities([{"id": "a", "name": "A", "aliases": "x, y"}])
            row = gs._fetch(store.conn, "MATCH (e:Entity {id:'a'}) RETURN e.aliases AS a")[0]
            self.assertEqual(row["a"], "x, y")

    def test_empty_input_is_a_no_op_not_a_crash(self):
        # UNWIND over an empty list is a binder error in this engine.
        with self.open() as store:
            self.assertEqual(store.ingest_entities([]), 0)
            self.assertEqual(store.ingest_relations([]), 0)
            self.assertEqual(store.ingest_graph({"nodes": [], "edges": []}).entities, 0)
            self.assertEqual(store.counts(), {"entities": 0, "relations": 0})

    def test_batching_does_not_change_the_result(self):
        with self.open() as store:
            nodes = [ent(f"n{i}", f"Entity {i}") for i in range(50)]
            for batch_size in (1, 7, 50, 1000):
                with self.subTest(batch_size=batch_size):
                    store.ingest_entities(nodes, batch_size=batch_size)
            self.assertEqual(store.counts()["entities"], 50)
            for batch_size in (1, 7, 1000):
                with self.subTest(rel_batch=batch_size):
                    store.ingest_relations(
                        [edge(f"n{i}", f"n{i + 1}", "NEXT") for i in range(49)],
                        batch_size=batch_size,
                    )
            self.assertEqual(store.counts()["relations"], 49)

    def test_invalid_batch_size_is_rejected(self):
        with self.open() as store:
            with self.assertRaises(ValueError):
                store.ingest_entities([ent("a", "A")], batch_size=0)

    def test_incomplete_edges_are_dropped(self):
        with self.open() as store:
            report = store.ingest_relations(
                [
                    {"source": "a", "target": "b", "action": ""},
                    {"source": "", "target": "b", "action": "X"},
                    {"source": "a", "action": "X"},
                    "not a mapping",
                ]
            )
            self.assertEqual(report, 0)

    def test_action_is_normalised_to_upper_case(self):
        with self.open() as store:
            store.ingest_graph(
                {"nodes": [ent("a", "A"), ent("b", "B")], "edges": [edge("a", "b", "causes")]}
            )
            row = gs._fetch(store.conn, "MATCH ()-[r:RELATION]->() RETURN r.action AS a")[0]
            self.assertEqual(row["a"], "CAUSES")

    def test_from_to_alias_keys_are_accepted(self):
        with self.open() as store:
            store.ingest_relations(
                [{"from": "a", "to": "b", "action": "X", "context": "c"}],
            ) if False else None
            store.ingest_entities([ent("a", "A"), ent("b", "B")])
            written = store.ingest_relations(
                [{"from": "a", "to": "b", "action": "X", "context": "c"}]
            )
            self.assertEqual(written, 1)

    def test_ingest_is_visible_to_search_and_expansion(self):
        store = self.seeded()
        self.assertIn(
            "euler-lagrange-equation", gs.find_pivot_nodes(store.conn, "Euler-Lagrange")
        )
        self.assertIn(
            "hamilton-equations", gs.expand_relevance(store.conn, ["lagrangian"], 2)
        )


class PivotSearchTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.seeded()
        self.conn = self.store.conn

    def find(self, query: str, **kw) -> list[str]:
        return gs.find_pivot_nodes(self.conn, query, **kw)

    def test_matches_on_name(self):
        self.assertIn("euler-lagrange-equation", self.find("Euler-Lagrange equation"))

    def test_matches_on_id(self):
        self.assertIn("hamilton-equations", self.find("hamilton-equations"))

    def test_matches_on_alias(self):
        self.assertIn("euler-lagrange-equation", self.find("what is ELE"))
        self.assertIn("hamilton-equations", self.find("Hamilton equations"))

    def test_matches_case_insensitively(self):
        for query in ("LAGRANGIAN", "lagrangian", "LaGrAnGiAn"):
            with self.subTest(query=query):
                self.assertIn("lagrangian", self.find(query))

    def test_matches_a_substring_of_a_name(self):
        self.assertIn("lagrangian", self.find("lagrang"))

    def test_searches_all_three_fields(self):
        # id, name and aliases are all reachable from a query.
        for query, expected in (
            ("lagrangian", "lagrangian"),            # name
            ("action", "action"),                    # name
            ("kinetic minus potential", "lagrangian"),  # alias
            ("ELE", "euler-lagrange-equation"),     # alias
        ):
            with self.subTest(query=query):
                self.assertIn(expected, self.find(query))

    def test_exact_id_scores_one(self):
        hits = gs.find_pivot_nodes_detailed(self.conn, "lagrangian")
        top = hits[0]
        self.assertEqual((top.entity_id, top.score, top.evidence), ("lagrangian", 1.0, "id"))

    def test_id_outranks_a_partial_token_match(self):
        hits = gs.find_pivot_nodes_detailed(self.conn, "action")
        self.assertEqual(hits[0].entity_id, "action")
        self.assertEqual(hits[0].score, 1.0)

    def test_alias_match_records_alias_evidence(self):
        hits = gs.find_pivot_nodes_detailed(self.conn, "kinetic minus potential")
        self.assertTrue(any(h.evidence == "alias" for h in hits))

    def test_token_evidence_names_the_field(self):
        hits = {
            h.entity_id: h
            for h in gs.find_pivot_nodes_detailed(self.conn, "equations of motion")
        }
        self.assertIn("hamilton-equations", hits)
        self.assertIn("token", hits["hamilton-equations"].evidence)

    def test_character_fuzzy_ranks_below_any_token_match(self):
        hits = gs.find_pivot_nodes_detailed(self.conn, "lagrangin lagrangian")
        token_scores = [h.score for h in hits if "token" in h.evidence or h.evidence == "id"]
        fuzzy_scores = [h.score for h in hits if h.evidence == "fuzzy"]
        if token_scores and fuzzy_scores:
            self.assertLess(max(fuzzy_scores), min(token_scores))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.find(""), [])
        self.assertEqual(self.find("   "), [])

    def test_stopword_only_query_returns_nothing_without_querying(self):
        for query in ("what is the of", "how do you", "a an the to"):
            with self.subTest(query=query):
                self.assertEqual(self.find(query), [])

    def test_query_with_no_match_returns_nothing(self):
        self.assertEqual(self.find("zzzzqqqq nonexistent"), [])

    def test_results_are_unique(self):
        found = self.find("lagrangian action equations")
        self.assertEqual(len(found), len(set(found)))

    def test_results_are_deterministic(self):
        runs = [self.find("equations of the lagrangian") for _ in range(5)]
        self.assertEqual(len(set(map(tuple, runs))), 1)

    def test_limit_is_respected(self):
        self.assertLessEqual(len(self.find("equations", limit=1)), 1)
        self.assertEqual(len(self.find("equations", limit=0)), 0)
        self.assertLessEqual(len(self.find("equations", limit=-3)), 0)

    def test_min_score_filters_at_each_tier_boundary(self):
        with self.open_at(os.path.join(self._tmp.name, "tiers.lbug")) as store:
            store.ingest_entities(
                [
                    {"id": "exact", "name": "lagrang", "aliases": []},
                    {"id": "alias", "name": "Totally Different", "aliases": ["lagrang"]},
                    {"id": "prefix", "name": "lagrangian", "aliases": []},
                    {"id": "name", "name": "lagrang algebra", "aliases": []},
                ]
            )
            # Measured tiers for "lagrang": exact name 0.98, exact alias 0.95,
            # full token coverage 0.90, prefix tolerance 0.9 * 0.92 = 0.828.
            # Each threshold is asserted just past the tier it removes, so a
            # change to any tier's weight fails here rather than quietly
            # reordering results.
            # Ordering is by score, so this also pins the tier weights.
            self.assertEqual(
                gs.find_pivot_nodes(store.conn, "lagrang", min_score=0.0),
                ["exact", "alias", "name", "prefix"],
            )
            # All four are *named* by the query, so the best-token rule keeps
            # them however high min_score goes: a name the query states outright
            # is not a weak match just because other query words went unmatched.
            for strict in (0.85, 0.91, 0.96, 0.99, 1.01):
                with self.subTest(min_score=strict):
                    self.assertEqual(
                        len(gs.find_pivot_nodes(store.conn, "lagrang", min_score=strict)),
                        4,
                    )
            # min_score still governs a hit the query does not name outright.
            # "disciplin" reaches "interdisciplinary" on the substring tier
            # (9/17 = 53% coverage, 0.85 affinity, 0.765 score) -- under the
            # best-token floor, so min_score alone decides it. "discip" at 35%
            # does not reach the tier at all.
            with self.open_at(os.path.join(self._tmp.name, "substr.lbug")) as sub:
                sub.ingest_entities([ent("inter", "interdisciplinary", [])])
                self.assertEqual(gs._token_affinity("discip", frozenset({"interdisciplinary"})), 0.0)
                self.assertEqual(gs.find_pivot_nodes(sub.conn, "disciplin", min_score=0.0), ["inter"])
                self.assertEqual(gs.find_pivot_nodes(sub.conn, "disciplin", min_score=0.70), ["inter"])
                self.assertEqual(gs.find_pivot_nodes(sub.conn, "disciplin", min_score=0.80), [])
                self.assertEqual(gs.find_pivot_nodes(sub.conn, "disciplin", min_score=1.01), [])
                self.assertEqual(gs.find_pivot_nodes(sub.conn, "discip", min_score=0.0), [])

    def test_empty_database_returns_nothing(self):
        with self.open_at(os.path.join(self._tmp.name, "untouched.lbug")) as empty:
            self.assertEqual(empty.counts(), {"entities": 0, "relations": 0})
            self.assertEqual(gs.find_pivot_nodes(empty.conn, "lagrangian"), [])

    def test_accents_fold_together(self):
        with self.open_at(os.path.join(self._tmp.name, "folded.lbug")) as store:
            store.ingest_entities([ent("emile", "Émile Durkheim", ["Durkheim"])])
            self.assertEqual(gs.find_pivot_nodes(store.conn, "emile durkheim"), ["emile"])
            self.assertEqual(gs.find_pivot_nodes(store.conn, "Émile"), ["emile"])

    def test_an_accented_name_is_found_by_typing_it_exactly(self):
        # The id here is deliberately *not* ASCII. The prefilter sends folded
        # tokens, Cypher's lower() does not decompose accents, so before the
        # as-typed variant was added an accented name could not be found by
        # typing it -- the folded query did not match the stored spelling, and
        # the id could not cover for it.
        with self.open_at(os.path.join(self._tmp.name, "accents.lbug")) as store:
            store.ingest_entities(
                [{"id": "x-1", "name": "Émile Côté", "aliases": []}]
            )
            self.assertEqual(gs.find_pivot_nodes(store.conn, "émile côté"), ["x-1"])
            self.assertEqual(gs.find_pivot_nodes(store.conn, "Émile"), ["x-1"])
            self.assertEqual(gs.find_pivot_nodes(store.conn, "Côté"), ["x-1"])

    def test_unaccented_query_cannot_see_an_accented_name_and_does_not_pretend_to(self):
        # Documented limitation, asserted so it cannot regress into a silent
        # miss: this engine has no unaccent(), so an unaccented query cannot match
        # accented stored text. entity_resolver's ASCII slug ids rescue this in
        # the pipeline; a non-ASCII id does not.
        with self.open_at(os.path.join(self._tmp.name, "gap.lbug")) as store:
            store.ingest_entities(
                [
                    {"id": "x-1", "name": "Émile Côté", "aliases": []},
                    {"id": "emile-cote", "name": "Émile Côté", "aliases": []},
                ]
            )
            self.assertEqual(gs.find_pivot_nodes(store.conn, "cote"), ["emile-cote"])

    def test_non_latin_scripts_are_searchable(self):
        with self.open_at(os.path.join(self._tmp.name, "scripts.lbug")) as store:
            store.ingest_entities(
                [
                    {"id": "cn", "name": "多模态大模型", "aliases": []},
                    {"id": "ru", "name": "Электромагнитное поле", "aliases": []},
                    {"id": "el", "name": "Πυθαγόρειο Θεώρημα", "aliases": []},
                ]
            )
            conn = store.conn
            self.assertEqual(gs.find_pivot_nodes(conn, "多模态大模型"), ["cn"])
            # No word delimiters, so a partial term is a substring rather than a
            # prefix; that case needs the coverage-floored substring tier.
            self.assertEqual(gs.find_pivot_nodes(conn, "多模态"), ["cn"])
            self.assertEqual(gs.find_pivot_nodes(conn, "электромагнитное поле"), ["ru"])
            self.assertEqual(gs.find_pivot_nodes(conn, "Πυθαγόρειο"), ["el"])
            self.assertEqual(gs.find_pivot_nodes(conn, "Θεώρημα"), ["el"])

    def test_short_fragments_do_not_match_unrelated_names(self):
        # The substring tier is floored at 40% coverage so "act" cannot reach
        # "transaction" while a CJK fragment can reach a longer name.
        with self.open_at(os.path.join(self._tmp.name, "frag.lbug")) as store:
            store.ingest_entities(
                [
                    {"id": "t", "name": "transaction", "aliases": []},
                    {"id": "a", "name": "action", "aliases": []},
                ]
            )
            self.assertEqual(gs.find_pivot_nodes(store.conn, "act"), ["a"])
            self.assertNotIn("t", gs.find_pivot_nodes(store.conn, "act"))

    def test_max_candidates_bounds_the_scan(self):
        with self.open_at(os.path.join(self._tmp.name, "many.lbug")) as store:
            store.ingest_entities(
                [ent(f"n{i}", f"Widget {i}", [f"w{i}"]) for i in range(50)]
            )
            # The cap limits how many rows come back from the prefilter. Every
            # row matches here, so a cap of 5 truncates to 5 rather than
            # filtering to the "best" 5 -- there is no ORDER BY to be best by.
            self.assertLessEqual(
                len(gs.find_pivot_nodes(store.conn, "widget", max_candidates=5, limit=50)), 5
            )
            self.assertEqual(
                len(gs.find_pivot_nodes(store.conn, "widget", max_candidates=50, limit=50)), 50
            )

    def test_hit_serialises(self):
        payload = gs.find_pivot_nodes_detailed(self.conn, "lagrangian")[0].as_dict()
        self.assertEqual(
            set(payload),
            {"id", "score", "evidence", "surface", "matched_tokens", "best_token"},
        )
        json.dumps(payload)


class QuestionRecallTests(StoreTestCase):
    """A question that names an entity must find it.

    Every query below returned [] before the best-token retention rule. `score` is
    coverage-based, so a question's other words -- relationship, explain, principle
    -- divided the score of an entity named outright: 1.0 coverage became 0.180 and
    fell under the 0.30 default. The failure was silent and hit the module's main
    use case, which is answering questions rather than matching keywords.
    """

    def setUp(self):
        super().setUp()
        self.store = self.open_at(os.path.join(self._tmp.name, "q.lbug"))
        self.store.ingest_entities(
            [
                ent("lagrangian", "Lagrangian", ["L"]),
                # The alias is what the real pipeline provides: entity_resolver
                # canonicalises "Hamiltonian" and "Hamilton's equations" to one
                # entity and keeps both spellings, and the prefilter needs the
                # variant to be a substring of something stored.
                ent("hamilton", "Hamilton's equations", ["Hamiltonian"]),
                ent("ele", "Euler-Lagrange equation", ["ELE"]),
                ent("action", "Action", []),
            ]
        )

    def test_question_recall(self):
        cases = {
            "Lagrangian": ["lagrangian"],
            "what is the Lagrangian": ["lagrangian"],
            "what minimises the Lagrangian?": ["lagrangian"],
            "what is the relationship between the Lagrangian and Hamiltonian mechanics": [
                "lagrangian",
                "hamilton",
            ],
            "how does the Lagrangian formalism relate to the Hamiltonian formulation of mechanics": [
                "lagrangian",
                "hamilton",
            ],
            "explain the variational principle behind the Lagrangian and the Hamiltonian equations": [
                "hamilton",
                "lagrangian",
            ],
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(gs.find_pivot_nodes(self.store.conn, query), expected)

    def test_ranking_still_prefers_better_coverage_under_a_question(self):
        # Retention is not the same as ordering: the entity covering more of the
        # question still comes first even when the other is the exact match.
        query = "explain the variational principle behind the Lagrangian and the Hamiltonian equations"
        ranked = gs.find_pivot_nodes(self.store.conn, query)
        self.assertEqual(ranked[0], "hamilton")
        self.assertEqual(ranked.index("hamilton") < ranked.index("lagrangian"), True)

    def test_the_rule_does_not_resurrect_a_query_that_names_nothing(self):
        for query in ("zzzzqqq nonexistent", "", "what is the of and", "   "):
            with self.subTest(query=query):
                self.assertEqual(gs.find_pivot_nodes(self.store.conn, query), [])

    def test_a_long_question_does_not_smuggle_in_a_weak_match(self):
        # The best-token rule must not become a licence for fuzzy noise: adding
        # question padding to a weak fragment still yields nothing.
        self.assertEqual(gs.find_pivot_nodes(self.store.conn, "tell me about the act"), [])
        self.assertEqual(gs.find_pivot_nodes(self.store.conn, "act"), ["action"])

    def test_a_morphological_variant_is_invisible_without_an_alias(self):
        # The prefilter is a substring scan, so "hamiltonian" cannot reach a node
        # stored only as "Hamilton's equations" -- no amount of scoring helps,
        # because the row is never returned. entity_resolver's aliases are what
        # close this, which is why the fixtures above carry them. Asserted so the
        # boundary stays a known limitation rather than a surprise.
        with self.open_at(os.path.join(self._tmp.name, "morph.lbug")) as bare:
            bare.ingest_entities([ent("hamilton", "Hamilton's equations", [])])
            self.assertEqual(gs.find_pivot_nodes(bare.conn, "Hamiltonian"), [])
            self.assertEqual(gs._token_affinity("hamiltonian", frozenset({"hamilton"})), 0.92)
        self.assertIn("hamilton", gs.find_pivot_nodes(self.store.conn, "Hamiltonian"))

    def test_best_token_is_reported_so_the_retention_is_visible(self):
        hit = gs.find_pivot_nodes_detailed(
            self.store.conn, "what is the relationship between the Lagrangian and mechanics"
        )[0]
        self.assertEqual(hit.entity_id, "lagrangian")
        self.assertLess(hit.score, gs.DEFAULT_MIN_SCORE)  # it would have been dropped
        self.assertEqual(hit.best_token, 1.0)            # but the query names it
        self.assertEqual(hit.as_dict()["best_token"], 1.0)


class ExpansionTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.seeded()
        self.conn = self.store.conn

    def test_one_hop(self):
        self.assertEqual(
            sorted(gs.expand_relevance(self.conn, ["action"], 1)), ["lagrangian"]
        )

    def test_two_hops(self):
        reached = gs.expand_relevance(self.conn, ["action"], 2)
        self.assertIn("lagrangian", reached)
        self.assertIn("euler-lagrange-equation", reached)

    def test_three_hops(self):
        self.assertIn(
            "hamilton-equations", gs.expand_relevance(self.conn, ["action"], 3)
        )

    def test_two_hops_are_not_claimed_when_only_one_is_requested(self):
        self.assertNotIn(
            "euler-lagrange-equation", gs.expand_relevance(self.conn, ["action"], 1)
        )

    def test_nearest_hop_is_ordered_first(self):
        reached = gs.expand_relevance(self.conn, ["action"], 2)
        self.assertLess(reached.index("lagrangian"), reached.index("euler-lagrange-equation"))

    def test_node_reachable_at_several_hops_appears_once(self):
        # lagrangian is 1 hop from action and also 2 hops via a longer route.
        with self.open_at(os.path.join(self._tmp.name, "severalhops.lbug")) as store:
            store.ingest_graph(
                {
                    "nodes": [ent("a", "A"), ent("b", "B"), ent("c", "C")],
                    "edges": [edge("a", "b", "X"), edge("b", "c", "Y")],
                }
            )
            reached = gs.expand_relevance(store.conn, ["a"], 2)
            self.assertEqual(sorted(reached), ["b", "c"])
            self.assertEqual(reached[0], "b")

    def test_undirected_by_default(self):
        self.assertIn(
            "euler-lagrange-equation", gs.expand_relevance(self.conn, ["lagrangian"], 1)
        )

    def test_directed_respects_direction(self):
        self.assertNotIn(
            "euler-lagrange-equation",
            gs.expand_relevance(self.conn, ["lagrangian"], 1, directed=True),
        )
        self.assertIn(
            "lagrangian", gs.expand_relevance(self.conn, ["action"], 1, directed=True)
        )

    def test_pivots_are_excluded_by_default(self):
        reached = gs.expand_relevance(self.conn, ["action"], 2)
        self.assertNotIn("action", reached)

    def test_pivots_can_be_included_first(self):
        reached = gs.expand_relevance(self.conn, ["action"], 2, include_pivots=True)
        self.assertEqual(reached[0], "action")
        self.assertIn("lagrangian", reached)

    def test_a_pivot_is_never_reported_as_another_pivots_finding(self):
        reached = gs.expand_relevance(self.conn, ["action", "lagrangian"], 2)
        self.assertNotIn("lagrangian", reached)
        self.assertIn("euler-lagrange-equation", reached)

    def test_empty_pivots_return_nothing(self):
        self.assertEqual(gs.expand_relevance(self.conn, [], 2), [])
        self.assertEqual(gs.expand_relevance(self.conn, ["", "  "], 2), [])

    def test_unknown_pivot_returns_nothing(self):
        self.assertEqual(gs.expand_relevance(self.conn, ["does-not-exist"], 2), [])

    def test_self_loops_are_not_traversed(self):
        with self.open_at(os.path.join(self._tmp.name, "selfloop.lbug")) as store:
            store.ingest_entities([ent("a", "A")])
            store.connection.execute(
                "MATCH (a:Entity {id:'a'}) CREATE (a)-[r:RELATION]->(a) "
                "SET r.action='SELF', r.context='c'"
            )
            self.assertEqual(gs.expand_relevance(store.conn, ["a"], 2), [])

    def test_cycles_terminate(self):
        with self.open_at(os.path.join(self._tmp.name, "cycle.lbug")) as store:
            store.ingest_graph(
                {
                    "nodes": [ent(x, x.upper()) for x in ("a", "b", "c")],
                    "edges": [edge("a", "b", "X"), edge("b", "c", "Y"), edge("c", "a", "Z")],
                }
            )
            self.assertEqual(sorted(gs.expand_relevance(store.conn, ["a"], 2)), ["b", "c"])

    def test_max_hops_is_validated(self):
        for bad in (0, -1, gs.MAX_HOPS + 1, 99):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gs.expand_relevance(self.conn, ["action"], bad)
            with self.subTest(bad=bad, fn="paths"), self.assertRaises(ValueError):
                gs.expand_paths(self.conn, ["action"], bad)

    def test_paths_carry_actions_and_contexts(self):
        paths = gs.expand_paths(self.conn, ["action"], 1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].actions, ("INTEGRATES",))
        self.assertIn("integral of the Lagrangian", paths[0].contexts[0])

    def test_two_hop_paths_carry_every_hop(self):
        paths = [p for p in gs.expand_paths(self.conn, ["action"], 2) if p.hops == 2]
        self.assertTrue(paths)
        for path in paths:
            self.assertEqual(len(path.actions), 2)
            self.assertEqual(len(path.contexts), 2)
            self.assertTrue(all(path.actions))
            self.assertTrue(all(path.contexts))
            self.assertTrue(path.via)

    def test_paths_serialise(self):
        payload = [p.as_dict() for p in gs.expand_paths(self.conn, ["action"], 2)]
        self.assertTrue(payload)
        for item in payload:
            self.assertEqual(
                set(item), {"pivot", "node", "hops", "via", "actions", "contexts"}
            )
        json.dumps(payload)

    def test_empty_pivots_yield_no_paths(self):
        self.assertEqual(gs.expand_paths(self.conn, [], 2), [])

    def test_limit_per_hop_is_exact_at_every_value(self):
        # A bound `LIMIT` is silently ignored by this engine -- it returned 1 row
        # for every value under 200 -- so each cap is checked exactly, at a
        # spread of values that includes 1 and a value above the row count. An
        # assertion of "at most N" would have passed against the broken engine.
        with self.open_at(os.path.join(self._tmp.name, "fanout.lbug")) as store:
            store.ingest_entities([ent(f"n{i}", f"N{i}") for i in range(30)])
            store.ingest_relations([edge("n0", f"n{i}", "X") for i in range(1, 30)])
            for cap in (1, 2, 5, 7, 13, 29, 30, 31, 5000):
                with self.subTest(cap=cap):
                    self.assertEqual(
                        len(gs.expand_relevance(store.conn, ["n0"], 1, limit_per_hop=cap)),
                        min(cap, 29),
                    )
            # The path view is capped identically, not independently.
            self.assertEqual(
                len(gs.expand_paths(store.conn, ["n0"], 1, limit_per_hop=3)), 3
            )

    def test_expansion_on_an_empty_database(self):
        with self.open_at(os.path.join(self._tmp.name, "empty.lbug")) as store:
            self.assertEqual(gs.expand_relevance(store.conn, ["anything"], 2), [])


class TextHelperTests(unittest.TestCase):
    def test_fold_strips_accents_and_punctuation(self):
        self.assertEqual(gs._fold("Émile's Data!"), "emile s data")
        self.assertEqual(gs._fold("C++"), "c")
        self.assertEqual(gs._fold(None), "")
        self.assertEqual(gs._fold(123), "")

    def test_tokenize_drops_stopwords_and_short_tokens(self):
        self.assertEqual(gs._tokenize("What is the Laplacian of a graph?"), ("laplacian", "graph"))
        self.assertEqual(gs._tokenize("a an the"), ())
        self.assertEqual(gs._tokenize(""), ())

    def test_tokenize_deduplicates_but_preserves_order(self):
        self.assertEqual(gs._tokenize("beta alpha beta"), ("beta", "alpha"))

    def test_ratio_is_symmetric(self):
        for left, right in (("ddaceddcd", "ebebc"), ("abc", "abd"), ("x", "yy")):
            with self.subTest(pair=(left, right)):
                self.assertEqual(gs._ratio(left, right), gs._ratio(right, left))

    def test_ratio_bounds(self):
        self.assertEqual(gs._ratio("", "abc"), 0.0)
        self.assertEqual(gs._ratio("abc", "abc"), 1.0)

    def test_token_affinity_exact_prefix_and_rejection(self):
        tokens = frozenset({"lagrangian"})
        self.assertEqual(gs._token_affinity("lagrangian", tokens), 1.0)
        self.assertGreater(gs._token_affinity("lagrang", tokens), 0.9)
        self.assertEqual(gs._token_affinity("encoder", tokens), 0.0)

    def test_prefix_needs_four_characters(self):
        # "abc" against "abcdef" is too short to be trusted as a prefix match.
        self.assertLess(gs._token_affinity("abc", frozenset({"abcdef"})), 0.92)

    def test_split_aliases(self):
        self.assertEqual(gs._split_aliases("a, b ,c"), ("a", "b", "c"))
        self.assertEqual(gs._split_aliases(""), ())
        self.assertEqual(gs._split_aliases(None), ())

    def test_guarded_unwind_filters_empty(self):
        self.assertIsNone(gs._guarded_unwind([]))
        self.assertIsNone(gs._guarded_unwind(()))
        self.assertEqual(gs._guarded_unwind(["a"]), ["a"])

    def test_hop_query_generation(self):
        one = gs._hop_query(1, False, 10)
        two = gs._hop_query(2, False, 10)
        self.assertIn("action1", one)
        self.assertNotIn("action2", one)
        self.assertIn("action2", two)
        self.assertIn("v1.id AS v1", two)
        self.assertIn("DISTINCT", two)
        self.assertIn("->", gs._hop_query(1, True, 10))
        self.assertNotIn("->", gs._hop_query(1, False, 10))

    def test_schema_ddl_is_stable(self):
        node, rel = gs.schema_ddl()
        self.assertIn("PRIMARY KEY (id)", node)
        self.assertIn("FROM Entity TO Entity", rel)


class IntegrationTests(StoreTestCase):
    """The handoff the pipeline actually performs."""

    def test_resolve_subgraph_output_ingests_and_is_searchable(self):
        chunks = [
            {
                "entities": [
                    {
                        "name": "Euler-Lagrange equation",
                        "category": "EQUATION",
                        "description": "Stationarity condition.",
                        "aliases": ["ELE"],
                    },
                    {
                        "name": "Lagrangian",
                        "category": "QUANTITY",
                        "description": "Kinetic minus potential.",
                        "aliases": ["L"],
                    },
                ],
                "relationships": [
                    {
                        "source": "Euler-Lagrange equation",
                        "target": "L",
                        "action": "MINIMIZES",
                        "context": "Its extremum gives the equation.",
                    }
                ],
            },
            {
                "entities": [
                    {
                        "name": "Lagrangian",
                        "category": "QUANTITY",
                        "description": "Kinetic minus potential.",
                        "aliases": ["L"],
                    },
                    {
                        "name": "Hamilton's equations of motion",
                        "category": "EQUATION",
                        "description": "First-order equations.",
                        "aliases": [],
                    },
                ],
                "relationships": [
                    {
                        "source": "L",
                        "target": "Hamilton's equations of motion",
                        "action": "RECOVERS",
                        "context": "Recovered at the extremum.",
                    }
                ],
            },
        ]

        registry = er.EntityRegistry()
        with self.open() as store:
            nodes: dict[str, dict] = {}
            edges: dict[tuple[str, str, str], dict] = {}
            for chunk in chunks:
                graph = er.resolve_subgraph(chunk, registry=registry)
                for node in graph["nodes"]:
                    if node["id"] in nodes:
                        nodes[node["id"]]["mentions"] += node["mentions"]
                    else:
                        nodes[node["id"]] = node
                for e in graph["edges"]:
                    edges.setdefault((e["source"], e["action"], e["target"]), e)

            report = store.ingest_graph({"nodes": list(nodes.values()), "edges": list(edges.values())})
            self.assertEqual(report.relations_skipped_orphan, 0)
            self.assertEqual(store.counts(), {"entities": 3, "relations": 2})

            # Merged across chunks, and reachable by every surface form.
            self.assertEqual(store.counts()["entities"], 3)
            for query in ("Lagrangian", "L", "ELE", "Euler-Lagrange equation", "Hamilton equations"):
                with self.subTest(query=query):
                    self.assertTrue(gs.find_pivot_nodes(store.conn, query), query)

            # A question answered by grounding on the traversed edges.
            pivots = gs.find_pivot_nodes(store.conn, "what does the Euler-Lagrange equation minimise?")
            self.assertIn("euler-lagrange-equation", pivots)
            paths = gs.expand_paths(store.conn, pivots, max_hops=2)
            actions = {a for p in paths for a in p.actions}
            self.assertTrue({"MINIMIZES", "RECOVERS"} & actions)

    def test_entity_resolver_ids_are_valid_primary_keys(self):
        # entity_resolver may emit non-Latin ids; the store must accept them.
        registry = er.EntityRegistry()
        graph = er.resolve_subgraph(
            {
                "entities": [
                    {
                        "name": "多模态大模型",
                        "category": "SYSTEM",
                        "description": "Multimodal large model.",
                        "aliases": [],
                    }
                ],
                "relationships": [],
            },
            registry=registry,
        )
        with self.open() as store:
            self.assertEqual(store.ingest_graph(graph).entities, 1)
            self.assertEqual(store.counts()["entities"], 1)
            self.assertEqual(gs.find_pivot_nodes(store.conn, "多模态"), [graph["nodes"][0]["id"]])


class CommandLineTests(StoreTestCase):
    """The CLI writes to stdout; keep it out of the test report."""

    @staticmethod
    def run_cli(*argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = gs.main(list(argv))
        return code, buffer.getvalue()

    def test_selftest_passes(self):
        code, output = self.run_cli("selftest")
        self.assertEqual(code, 0, output)
        self.assertIn("all checks passed", output)

    def test_selftest_reports_failures_as_a_non_zero_exit(self):
        # A failing check must surface as an exit code, not a traceback, so a CI
        # run can gate on it.
        with mock.patch.object(gs, "_SAMPLE_GRAPH", {"nodes": [], "edges": []}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = gs._selftest()
        self.assertEqual(code, 1)
        self.assertIn("check(s) failed", out.getvalue())

    def test_no_command_prints_help(self):
        code, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage", output.lower())

    def test_ingest_search_expand_stats_round_trip(self):
        graph_file = os.path.join(self._tmp.name, "graph.json")
        with open(graph_file, "w", encoding="utf-8") as handle:
            json.dump(SAMPLE_GRAPH, handle)

        code, output = self.run_cli("ingest", self.path, graph_file)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["entities"], 4)

        code, output = self.run_cli("search", self.path, "Euler-Lagrange")
        self.assertEqual(code, 0, output)
        self.assertIn("euler-lagrange-equation", json.loads(output))

        code, output = self.run_cli("search", self.path, "ELE", "--explain")
        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)[0]["evidence"])

        code, output = self.run_cli("expand", self.path, "action", "--max-hops", "2")
        self.assertEqual(code, 0, output)
        self.assertIn("lagrangian", json.loads(output))

        code, output = self.run_cli("expand", self.path, "action", "--paths")
        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)[0]["actions"])

        code, output = self.run_cli("stats", self.path)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["counts"], {"entities": 4, "relations": 3})

    def test_search_on_an_uninitialised_database_fails_cleanly(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code, _ = self.run_cli("search", self.path, "anything")
        self.assertEqual(code, 1)
        self.assertIn("schema", stderr.getvalue())

    def test_ingest_reports_orphans_without_crashing(self):
        graph_file = os.path.join(self._tmp.name, "bad.json")
        with open(graph_file, "w", encoding="utf-8") as handle:
            json.dump({"nodes": [ent("a", "A")], "edges": [edge("a", "ghost", "X")]}, handle)
        code, output = self.run_cli("ingest", self.path, graph_file)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["missing_endpoints"], ["ghost"])

    def test_stats_on_a_real_database(self):
        with self.open() as store:
            store.ingest_graph(SAMPLE_GRAPH)
        code, output = self.run_cli("stats", self.path)
        self.assertEqual(code, 0, output)
        self.assertGreater(json.loads(output)["disk_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
