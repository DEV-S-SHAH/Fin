"""Tests for tools/drain_staging.py — offline DB consolidation worker.

Test categories
---------------
1. Port-9000 server isolation (reachable → abort; unreachable → proceed).
2. JSONL parsing: valid lines, blank lines, malformed JSON, non-object lines.
3. Schema validation: good ExtractionPayload, bad confidence, self-loop, missing fields.
4. Batch deduplication: merge two payloads for the same ticker, prefer higher confidence.
5. Status-only records: staging files that only contain status records are skipped cleanly.
6. Dry-run mode: no DB writes, no archiving, correct summary.
7. Safe cleanup: successful commit → .jsonl moved to archive directory atomically.
8. Failed commit → original file retained, ticker appears in `failed` list.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.drain_staging import (
    DrainResult,
    StagingParseError,
    _archive_staging_file,
    _merge_payloads,
    _parse_staging_file,
    _server_is_reachable,
    _validate_extraction_records,
    drain,
)
from sandbox_engine.coldstart_schema import (
    ExtractionPayload,
    ExtractedEntity,
    ExtractedRelation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity(
    id: str = "e1",
    name: str = "Test Corp",
    entity_type: str = "Company",
) -> dict:
    return {"id": id, "name": name, "entity_type": entity_type, "properties": {}}


def _make_relation(
    source_id: str = "e1",
    target_id: str = "e2",
    relation: str = "COMPETES_WITH",
    confidence: float = 0.85,
    evidence_quote: str = "We compete directly with Test Corp",
) -> dict:
    return {
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "confidence": confidence,
        "evidence_quote": evidence_quote,
        "properties": {},
    }


def _make_extraction_record(
    ticker: str,
    entities: list[dict] | None = None,
    relationships: list[dict] | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "status": "extracted",
        "timestamp": "2026-09-29T20:00:00+00:00",
        "payload": {
            "entities": entities or [_make_entity()],
            "relationships": relationships or [_make_relation()],
            "rejected_count": 0,
            "metadata": {},
        },
    }


def _make_status_record(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "status": "staged",
        "timestamp": "2026-09-29T20:00:00+00:00",
        "metadata": {},
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# 1. Server isolation
# ---------------------------------------------------------------------------


class TestServerIsolation(unittest.TestCase):

    def test_reachable_returns_true_when_port_open(self):
        """If something binds the port, _server_is_reachable should return True."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))  # ephemeral port
            srv.listen(1)
            port = srv.getsockname()[1]
            self.assertTrue(_server_is_reachable(port=port, timeout=1.0))

    def test_unreachable_returns_false_when_port_closed(self):
        """A closed port should return False quickly."""
        # Find a definitely-closed port by binding then releasing
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        self.assertFalse(_server_is_reachable(port=closed_port, timeout=0.2))

    def test_drain_raises_when_server_reachable(self):
        """drain() must raise RuntimeError if port 9000 appears live and --force is False."""
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            with patch("tools.drain_staging._server_is_reachable", return_value=True):
                with self.assertRaises(RuntimeError) as ctx:
                    drain(
                        db_path=Path(td) / "dummy.lbug",
                        staging_dir=staging,
                        archive_dir=archive,
                        force=False,
                        dry_run=False,
                    )
                self.assertIn("port", str(ctx.exception).lower())

    def test_drain_proceeds_with_force_flag(self):
        """--force bypasses the server check even if reachable."""
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            # No .jsonl files → drain returns empty result without DB access
            with patch("tools.drain_staging._server_is_reachable", return_value=True):
                result = drain(
                    db_path=Path(td) / "dummy.lbug",
                    staging_dir=staging,
                    archive_dir=archive,
                    force=True,
                    dry_run=True,
                )
            self.assertIsInstance(result, DrainResult)


# ---------------------------------------------------------------------------
# 2. JSONL parsing
# ---------------------------------------------------------------------------


class TestJsonlParsing(unittest.TestCase):

    def test_valid_jsonl_parsed(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"a": 1}) + "\n")
            f.write(json.dumps({"b": 2}) + "\n")
            path = Path(f.name)
        try:
            records = _parse_staging_file(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["a"], 1)
        finally:
            path.unlink()

    def test_blank_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n")
            f.write(json.dumps({"x": 1}) + "\n")
            f.write("   \n")
            path = Path(f.name)
        try:
            records = _parse_staging_file(path)
            self.assertEqual(len(records), 1)
        finally:
            path.unlink()

    def test_malformed_json_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("not-json\n")
            path = Path(f.name)
        try:
            with self.assertRaises(StagingParseError):
                _parse_staging_file(path)
        finally:
            path.unlink()

    def test_non_object_json_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("[1, 2, 3]\n")
            path = Path(f.name)
        try:
            with self.assertRaises(StagingParseError):
                _parse_staging_file(path)
        finally:
            path.unlink()

    def test_empty_file_returns_empty_list(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        try:
            records = _parse_staging_file(path)
            self.assertEqual(records, [])
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# 3. Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation(unittest.TestCase):

    def test_valid_extraction_payload_accepted(self):
        records = [
            _make_status_record("RIVN"),                  # skipped (status-only)
            _make_extraction_record("RIVN"),              # valid
        ]
        payloads, rejected = _validate_extraction_records(records, "RIVN")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(rejected, 0)

    def test_bad_confidence_rejected(self):
        rec = _make_extraction_record(
            "RIVN",
            entities=[_make_entity("e1", "Corp A"), _make_entity("e2", "Corp B")],
            relationships=[
                _make_relation(confidence=1.5)  # out of range
            ],
        )
        payloads, rejected = _validate_extraction_records([rec], "RIVN")
        self.assertEqual(len(payloads), 0)
        self.assertEqual(rejected, 1)

    def test_self_loop_rejected(self):
        rec = _make_extraction_record(
            "RIVN",
            entities=[_make_entity("e1", "Corp A"), _make_entity("e2", "Corp B")],
            relationships=[
                _make_relation(source_id="e1", target_id="e1")  # self-loop
            ],
        )
        payloads, rejected = _validate_extraction_records([rec], "RIVN")
        self.assertEqual(len(payloads), 0)
        self.assertEqual(rejected, 1)

    def test_evidence_quote_too_long_rejected(self):
        long_quote = " ".join(["word"] * 31)  # 31 words, exceeds 30-word cap
        rec = _make_extraction_record(
            "RIVN",
            entities=[_make_entity("e1", "Corp A"), _make_entity("e2", "Corp B")],
            relationships=[
                _make_relation(evidence_quote=long_quote)
            ],
        )
        payloads, rejected = _validate_extraction_records([rec], "RIVN")
        self.assertEqual(len(payloads), 0)
        self.assertEqual(rejected, 1)

    def test_missing_payload_key_rejected(self):
        rec = {
            "ticker": "RIVN",
            "status": "extracted",
            "timestamp": "2026-09-29T20:00:00+00:00",
            # deliberately missing "payload"
        }
        payloads, rejected = _validate_extraction_records([rec], "RIVN")
        self.assertEqual(len(payloads), 0)
        self.assertEqual(rejected, 1)

    def test_status_only_record_not_counted_as_rejected(self):
        """Status-only records must be silently skipped, NOT counted as rejected."""
        records = [_make_status_record("RIVN")]
        payloads, rejected = _validate_extraction_records(records, "RIVN")
        self.assertEqual(payloads, [])
        self.assertEqual(rejected, 0)


# ---------------------------------------------------------------------------
# 4. Batch deduplication
# ---------------------------------------------------------------------------


class TestBatchDeduplication(unittest.TestCase):

    def test_entity_dedup_by_id_last_writer_wins(self):
        """Two payloads with the same entity id — last wins."""
        p1 = ExtractionPayload(
            entities=[ExtractedEntity(id="e1", name="Version A", entity_type="Supplier")],
            relationships=[],
        )
        p2 = ExtractionPayload(
            entities=[ExtractedEntity(id="e1", name="Version B", entity_type="Supplier")],
            relationships=[],
        )
        merged = _merge_payloads([p1, p2], "TEST")
        self.assertEqual(len(merged.entities), 1)
        self.assertEqual(merged.entities[0].name, "Version B")

    def test_relation_dedup_highest_confidence_wins(self):
        """Two payloads with the same (src, tgt, rel) triplet — highest confidence kept."""
        p1 = ExtractionPayload(
            entities=[
                ExtractedEntity(id="e1", name="Corp A", entity_type="Company"),
                ExtractedEntity(id="e2", name="Corp B", entity_type="Competitor"),
            ],
            relationships=[
                ExtractedRelation(
                    source_id="e1", target_id="e2", relation="COMPETES_WITH",
                    confidence=0.6, evidence_quote="Corp A competes with Corp B",
                )
            ],
        )
        p2 = ExtractionPayload(
            entities=[
                ExtractedEntity(id="e1", name="Corp A", entity_type="Company"),
                ExtractedEntity(id="e2", name="Corp B", entity_type="Competitor"),
            ],
            relationships=[
                ExtractedRelation(
                    source_id="e1", target_id="e2", relation="COMPETES_WITH",
                    confidence=0.95, evidence_quote="Corp A competes directly with Corp B",
                )
            ],
        )
        merged = _merge_payloads([p1, p2], "TEST")
        self.assertEqual(len(merged.relationships), 1)
        self.assertAlmostEqual(merged.relationships[0].confidence, 0.95)

    def test_rejected_count_summed(self):
        p1 = ExtractionPayload(entities=[], relationships=[], rejected_count=3)
        p2 = ExtractionPayload(entities=[], relationships=[], rejected_count=5)
        merged = _merge_payloads([p1, p2], "TEST")
        self.assertEqual(merged.rejected_count, 8)

    def test_distinct_entities_and_relations_all_preserved(self):
        p1 = ExtractionPayload(
            entities=[ExtractedEntity(id="e1", name="Corp A", entity_type="Company")],
            relationships=[],
        )
        p2 = ExtractionPayload(
            entities=[ExtractedEntity(id="e2", name="Corp B", entity_type="Supplier")],
            relationships=[],
        )
        merged = _merge_payloads([p1, p2], "TEST")
        self.assertEqual(len(merged.entities), 2)


# ---------------------------------------------------------------------------
# 5. Status-only JSONL files skipped cleanly
# ---------------------------------------------------------------------------


class TestStatusOnlySkip(unittest.TestCase):

    def test_status_only_file_skipped_not_committed(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"
            jfile = staging / "RIVN.jsonl"
            _write_jsonl(jfile, [_make_status_record("RIVN")])

            with patch("tools.drain_staging._server_is_reachable", return_value=False):
                result = drain(
                    db_path=Path(td) / "dummy.lbug",
                    staging_dir=staging,
                    archive_dir=archive,
                    force=False,
                    dry_run=True,  # dry-run: no DB access needed
                )

            self.assertIn("RIVN", result.tickers_skipped_status_only)
            self.assertNotIn("RIVN", result.tickers_committed)
            self.assertNotIn("RIVN", result.tickers_failed)
            # Original file must NOT be archived
            self.assertTrue(jfile.exists())


# ---------------------------------------------------------------------------
# 6. Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):

    def test_dry_run_no_archive_no_db_call(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            entities = [
                _make_entity("e1", "Rivian", "Company"),
                _make_entity("e2", "CATL", "Supplier"),
            ]
            relationships = [
                _make_relation(
                    source_id="e1",
                    target_id="e2",
                    relation="SOURCES_FROM",
                    confidence=0.90,
                    evidence_quote="Rivian sources battery cells from CATL",
                )
            ]
            jfile = staging / "RIVN.jsonl"
            _write_jsonl(jfile, [_make_extraction_record("RIVN", entities, relationships)])

            with patch("tools.drain_staging._server_is_reachable", return_value=False):
                with patch("tools.drain_staging._commit_payload_to_db") as mock_commit:
                    result = drain(
                        db_path=Path(td) / "dummy.lbug",
                        staging_dir=staging,
                        archive_dir=archive,
                        force=False,
                        dry_run=True,
                    )
                    mock_commit.assert_not_called()

            self.assertIn("RIVN", result.tickers_dry_run)
            self.assertNotIn("RIVN", result.tickers_committed)
            self.assertTrue(jfile.exists(), "Dry-run must not remove the staging file")
            self.assertFalse(archive.exists() and any(archive.iterdir()) if archive.exists() else False,
                             "Dry-run must not create archive entries")


# ---------------------------------------------------------------------------
# 7. Safe cleanup — successful commit → archive
# ---------------------------------------------------------------------------


class TestSafeCleanup(unittest.TestCase):

    def test_archive_moves_file_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            src = staging / "TSLA.jsonl"
            _write_jsonl(src, [_make_status_record("TSLA")])

            dest = _archive_staging_file(src, archive, "TSLA")

            self.assertFalse(src.exists(), "Original file must be removed after archiving")
            self.assertTrue(dest.exists(), "Archived file must exist at destination")
            self.assertTrue(dest.name.startswith("TSLA-"))

    def test_commit_success_archives_and_appears_in_committed(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            entities = [
                _make_entity("e1", "Tesla", "Company"),
                _make_entity("e2", "Panasonic", "Supplier"),
            ]
            relationships = [
                _make_relation(
                    source_id="e1",
                    target_id="e2",
                    relation="SOURCES_FROM",
                    confidence=0.88,
                    evidence_quote="Tesla sources battery cells from Panasonic",
                )
            ]
            jfile = staging / "TSLA.jsonl"
            _write_jsonl(jfile, [_make_extraction_record("TSLA", entities, relationships)])

            mock_commit_result = {"new_nodes": 2, "stitched_backbone_edges": 1, "ephemeral_edges": 1}
            with patch("tools.drain_staging._server_is_reachable", return_value=False):
                with patch("tools.drain_staging._commit_payload_to_db", return_value=mock_commit_result):
                    result = drain(
                        db_path=Path(td) / "dummy.lbug",
                        staging_dir=staging,
                        archive_dir=archive,
                        force=False,
                        dry_run=False,
                    )

            self.assertIn("TSLA", result.tickers_committed)
            self.assertFalse(jfile.exists(), "Staging file must be archived after successful commit")
            archived_files = list(archive.glob("TSLA-*.jsonl"))
            self.assertEqual(len(archived_files), 1, "Exactly one archive file must be created")

    def test_commit_failure_retains_staging_file(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            jfile = staging / "TSLA.jsonl"
            entities = [
                _make_entity("e1", "Tesla", "Company"),
                _make_entity("e2", "Panasonic", "Supplier"),
            ]
            relationships = [
                _make_relation(
                    source_id="e1",
                    target_id="e2",
                    relation="SOURCES_FROM",
                    confidence=0.88,
                    evidence_quote="Tesla sources battery cells from Panasonic",
                )
            ]
            _write_jsonl(jfile, [_make_extraction_record("TSLA", entities, relationships)])

            with patch("tools.drain_staging._server_is_reachable", return_value=False):
                with patch(
                    "tools.drain_staging._commit_payload_to_db",
                    side_effect=RuntimeError("Simulated DB failure"),
                ):
                    result = drain(
                        db_path=Path(td) / "dummy.lbug",
                        staging_dir=staging,
                        archive_dir=archive,
                        force=False,
                        dry_run=False,
                    )

            self.assertIn("TSLA", result.tickers_failed)
            self.assertNotIn("TSLA", result.tickers_committed)
            self.assertTrue(jfile.exists(), "Staging file must be retained on commit failure")


# ---------------------------------------------------------------------------
# 8. Multi-ticker batch processing
# ---------------------------------------------------------------------------


class TestMultiTickerBatch(unittest.TestCase):

    def test_two_tickers_processed_independently(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            archive = staging / "archive"

            for ticker, entity_name in [("RIVN", "Rivian"), ("LCID", "Lucid Motors")]:
                entities = [
                    _make_entity("e1", entity_name, "Company"),
                    _make_entity("e2", "CATL", "Supplier"),
                ]
                relationships = [
                    _make_relation(
                        source_id="e1", target_id="e2",
                        relation="SOURCES_FROM",
                        confidence=0.80,
                        evidence_quote=f"{entity_name} sources batteries from CATL",
                    )
                ]
                _write_jsonl(
                    staging / f"{ticker}.jsonl",
                    [_make_extraction_record(ticker, entities, relationships)],
                )

            mock_result = {"new_nodes": 1, "stitched_backbone_edges": 0, "ephemeral_edges": 1}
            with patch("tools.drain_staging._server_is_reachable", return_value=False):
                with patch("tools.drain_staging._commit_payload_to_db", return_value=mock_result):
                    result = drain(
                        db_path=Path(td) / "dummy.lbug",
                        staging_dir=staging,
                        archive_dir=archive,
                        force=False,
                        dry_run=False,
                    )

            self.assertIn("RIVN", result.tickers_committed)
            self.assertIn("LCID", result.tickers_committed)
            self.assertEqual(result.nodes_committed, 2)  # 1 per ticker


if __name__ == "__main__":
    unittest.main()
