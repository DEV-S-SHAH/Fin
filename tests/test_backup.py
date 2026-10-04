"""Backup and restore: the guarantees that make a backup worth trusting.

Every test runs against a fixture database in ``tmp_path``. Nothing here opens
``sandbox_engine/_run/sandbox.lbug``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import ladybug as lb

from sandbox_engine import backup as bk


# ---------------------------------------------------------------------------
# Fixture database
# ---------------------------------------------------------------------------


def build_database(path: Path, filings: int = 12, chunks: int = 30) -> Path:
    """A small graph with the same node/rel shape as the real database."""
    database = lb.Database(str(path))
    connection = lb.Connection(database)
    connection.execute("CREATE NODE TABLE Filing (id STRING, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE Chunk (id STRING, PRIMARY KEY(id))")
    connection.execute("CREATE REL TABLE HAS_CHUNK (FROM Filing TO Chunk)")
    connection.execute(
        "UNWIND $r AS x CREATE (:Filing {id: x})",
        {"r": [f"F{i:03d}" for i in range(filings)]},
    )
    connection.execute(
        "UNWIND $r AS x CREATE (:Chunk {id: x})",
        {"r": [f"C{i:03d}" for i in range(chunks)]},
    )
    connection.execute(
        "MATCH (f:Filing), (c:Chunk) WHERE f.id < 'F004' CREATE (f)-[:HAS_CHUNK]->(c)"
    )
    connection.close()
    database.close()
    return path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return build_database(tmp_path / "sandbox.lbug")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "backups"


# ---------------------------------------------------------------------------
# Creating a backup
# ---------------------------------------------------------------------------


class TestCreate:
    def test_backup_is_a_verified_copy_of_the_source(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)

        assert (result.path / "manifest.json").is_file()
        assert (result.path / bk.PAYLOAD_NAME).read_bytes() == source.read_bytes()

        manifest = result.manifest
        assert manifest["complete"] is True
        assert manifest["source"]["path"] == str(source.resolve())
        assert manifest["source"]["fingerprint"]["node_rows"] == 42
        assert manifest["source"]["fingerprint"]["rel_rows"] > 0

    def test_backup_id_is_timestamped_and_identifies_the_source(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)

        assert result.backup_id.startswith(
            result.manifest["created_utc"][:10].replace("-", "")
        ) or result.backup_id[:8].isdigit()
        assert "sandbox" in result.backup_id
        assert result.manifest["payload"]["sha256"][:12] in result.backup_id

    def test_backup_records_the_engine_and_storage_versions(self, source: Path, root: Path) -> None:
        manifest = bk.create_backup(source, root).manifest
        fingerprint = manifest["source"]["fingerprint"]

        assert fingerprint["ladybug_version"] == lb.Database(str(source)).get_version().__str__()
        assert isinstance(fingerprint["storage_version"], int)
        assert set(fingerprint["node_tables"]) == {"Filing", "Chunk"}

    def test_repeated_backups_do_not_collide(self, source: Path, root: Path) -> None:
        first = bk.create_backup(source, root, prune=False)
        second = bk.create_backup(source, root, prune=False)

        assert first.path != second.path
        assert len(bk.list_backups(root)) == 2

    def test_no_wal_sidecar_is_left_beside_the_backup(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)

        assert not bk.wal_path(result.path / bk.PAYLOAD_NAME).exists()

    def test_a_failed_backup_leaves_nothing_behind(
        self, source: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bk, "fingerprint", lambda _path: (_ for _ in ()).throw(RuntimeError("boom")))

        with pytest.raises(RuntimeError):
            bk.create_backup(source, root)

        assert bk.list_backups(root) == []
        assert not root.exists() or not any(root.iterdir())


# ---------------------------------------------------------------------------
# The safety gate: a live writer or an unreplayed WAL
# ---------------------------------------------------------------------------


class TestQuiesceGates:
    def test_backup_refuses_while_a_write_ahead_log_exists(self, source: Path, root: Path) -> None:
        """A .lbug next to a .wal is missing recently committed data."""
        bk.wal_path(source).write_bytes(b"\x00" * 128)

        with pytest.raises(bk.BackupError, match="write-ahead log"):
            bk.create_backup(source, root)

        assert bk.list_backups(root) == []

    def test_refusal_explains_that_the_wal_is_not_deleted(
        self, source: Path, root: Path
    ) -> None:
        wal = bk.wal_path(source)
        wal.write_bytes(b"\x00" * 128)

        with pytest.raises(bk.BackupError) as caught:
            bk.create_backup(source, root)

        assert wal.exists(), "the only copy of a committed transaction must never be deleted"
        assert "not done implicitly" in str(caught.value)

    def test_backup_refuses_while_another_process_holds_the_writer_lock(
        self, source: Path, root: Path
    ) -> None:
        # CHECKPOINT folds the WAL away so this exercises the lock gate alone
        # rather than tripping the WAL gate first.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import time, ladybug as lb
                    db = lb.Database({str(source)!r})
                    c = lb.Connection(db)
                    c.execute("CREATE (:Filing {{id: 'live'}})")
                    c.execute("CHECKPOINT")
                    print("held", flush=True)
                    time.sleep(60)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            assert not bk.wal_path(source).exists(), "WAL must be absent to isolate the lock"
            with pytest.raises(bk.BackupError, match="writer lock"):
                bk.create_backup(source, root)
        finally:
            holder.kill()
            holder.wait(timeout=30)

        assert bk.list_backups(root) == []

    def test_backup_refuses_a_missing_database(self, tmp_path: Path, root: Path) -> None:
        with pytest.raises(bk.BackupError, match="not found"):
            bk.create_backup(tmp_path / "absent.lbug", root)

    def test_source_is_untouched_by_a_successful_backup(self, source: Path, root: Path) -> None:
        before = bk._sha256(source)

        bk.create_backup(source, root)

        assert bk._sha256(source) == before
        assert not bk.wal_path(source).exists()


# ---------------------------------------------------------------------------
# Incomplete detection
# ---------------------------------------------------------------------------


class TestIncompleteDetection:
    def test_a_backup_without_a_manifest_is_incomplete(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        (result.path / bk.MANIFEST_NAME).unlink()

        entry = next(item for item in bk.list_backups(root) if item["backup_id"] == result.backup_id)
        assert entry["complete"] is False
        with pytest.raises(bk.BackupError, match="incomplete"):
            bk.verify_backup(root, result.backup_id)

    def test_the_in_progress_marker_marks_a_backup_incomplete(
        self, source: Path, root: Path
    ) -> None:
        result = bk.create_backup(source, root)
        (result.path / bk.INCOMPLETE_MARKER).write_text("in progress", encoding="utf-8")

        entry = next(item for item in bk.list_backups(root) if item["backup_id"] == result.backup_id)
        assert entry["complete"] is False
        with pytest.raises(bk.BackupError, match="incomplete"):
            bk.verify_backup(root, result.backup_id)

    def test_a_completed_backup_has_no_marker_and_a_manifest(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)

        assert not (result.path / bk.INCOMPLETE_MARKER).exists()
        assert (result.path / bk.MANIFEST_NAME).is_file()

    def test_verify_rejects_an_unknown_backup(self, root: Path) -> None:
        with pytest.raises(bk.BackupError, match="no such backup"):
            bk.verify_backup(root, "does-not-exist")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerify:
    def test_verify_passes_for_an_untouched_backup(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)

        checked = bk.verify_backup(root, result.backup_id)

        assert checked["manifest"]["payload"]["sha256"] == result.sha256
        assert checked["fingerprint"]["node_rows"] == 42

    def test_verify_detects_a_truncated_payload(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        payload = result.path / bk.PAYLOAD_NAME
        payload.write_bytes(payload.read_bytes()[:4096])

        with pytest.raises(bk.BackupError, match="digest mismatch"):
            bk.verify_backup(root, result.backup_id)

    def test_verify_detects_a_single_flipped_byte(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        payload = result.path / bk.PAYLOAD_NAME
        raw = bytearray(payload.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        payload.write_bytes(bytes(raw))

        with pytest.raises(bk.BackupError):
            bk.verify_backup(root, result.backup_id)

    def test_verify_detects_a_swapped_payload(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        other = build_database(root / "other.lbug", filings=99, chunks=1)
        (result.path / bk.PAYLOAD_NAME).write_bytes(other.read_bytes())

        with pytest.raises(bk.BackupError):
            bk.verify_backup(root, result.backup_id)

    def test_verify_detects_a_missing_payload(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        (result.path / bk.PAYLOAD_NAME).unlink()

        with pytest.raises(bk.BackupError, match="payload missing"):
            bk.verify_backup(root, result.backup_id)

    def test_verify_rejects_an_unreadable_manifest(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        (result.path / bk.MANIFEST_NAME).write_text("{ not json", encoding="utf-8")

        with pytest.raises(bk.BackupError, match="unreadable"):
            bk.verify_backup(root, result.backup_id)

    def test_verify_rejects_an_unknown_manifest_version(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        manifest_path = result.path / bk.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest["manifest_version"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(bk.BackupError, match="manifest version"):
            bk.verify_backup(root, result.backup_id)

    def test_verify_rejects_a_backup_that_is_not_a_database(self, root: Path) -> None:
        target = root / "20260101T000000Z-sandbox-deadbeefcafe"
        (target / bk.PAYLOAD_NAME).parent.mkdir(parents=True)
        (target / bk.PAYLOAD_NAME).write_bytes(b"this is not a LadybugDB file")
        digest = bk._sha256(target / bk.PAYLOAD_NAME)
        (target / bk.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "manifest_version": bk.MANIFEST_VERSION,
                    "payload": {
                        "name": bk.PAYLOAD_NAME,
                        "sha256": digest,
                        "size_bytes": 27,
                    },
                    "source": {
                        "fingerprint": {
                            "node_tables": {},
                            "rel_tables": {},
                            "node_rows": 0,
                            "rel_rows": 0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(Exception):
            bk.verify_backup(root, target.name)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_refuses_without_explicit_confirmation(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        source.unlink()

        with pytest.raises(bk.BackupError, match="confirm"):
            bk.restore_backup(source, root, result.backup_id)

        assert not source.exists()

    def test_restore_reproduces_the_source_content(self, source: Path, root: Path) -> None:
        expected = bk.fingerprint(source)
        result = bk.create_backup(source, root)
        source.unlink()

        bk.restore_backup(source, root, result.backup_id, confirm=True)

        assert bk.fingerprint(source) == expected

    def test_restore_never_deletes_the_existing_database(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        # A genuinely different database now occupies the path.
        source.unlink()
        build_database(source, filings=3, chunks=2)
        displaced_sha = bk._sha256(source)

        outcome = bk.restore_backup(source, root, result.backup_id, confirm=True)

        assert outcome.displaced is not None
        assert outcome.displaced.exists()
        assert bk._sha256(outcome.displaced) == displaced_sha

    def test_restore_parks_a_stale_wal_instead_of_replaying_it(
        self, source: Path, root: Path
    ) -> None:
        """A leftover journal must not be applied on top of the restored file."""
        result = bk.create_backup(source, root)
        wal = bk.wal_path(source)
        wal.write_bytes(b"\x00" * 64)

        outcome = bk.restore_backup(source, root, result.backup_id, confirm=True)

        assert not wal.exists()
        assert outcome.displaced is not None
        assert bk.fingerprint(source)["node_rows"] == 42

    def test_restore_refuses_an_incomplete_backup(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        (result.path / bk.INCOMPLETE_MARKER).write_text("x", encoding="utf-8")

        with pytest.raises(bk.BackupError, match="incomplete"):
            bk.restore_backup(source, root, result.backup_id, confirm=True)

    def test_restore_refuses_a_corrupted_backup(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        payload = result.path / bk.PAYLOAD_NAME
        payload.write_bytes(payload.read_bytes()[:2048])
        original = bk._sha256(source)

        with pytest.raises(bk.BackupError, match="digest mismatch"):
            bk.restore_backup(source, root, result.backup_id, confirm=True)

        assert bk._sha256(source) == original, "a failed restore must not damage the database"

    def test_restored_database_opens_and_answers_queries(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root)
        source.unlink()

        bk.restore_backup(source, root, result.backup_id, confirm=True)

        database = lb.Database(str(source), read_only=True)
        connection = lb.Connection(database)
        rows = connection.execute("MATCH (f:Filing) RETURN count(f)").get_all()
        connection.close()
        database.close()
        assert int(list(rows[0])[0]) == 12

    def test_restore_accepts_an_absent_database(self, tmp_path: Path, root: Path) -> None:
        source = build_database(tmp_path / "sandbox.lbug")
        result = bk.create_backup(source, root)
        source.unlink()

        outcome = bk.restore_backup(source, root, result.backup_id, confirm=True)

        assert outcome.displaced is None
        assert source.is_file()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    def test_retention_keeps_only_the_newest(self, source: Path, root: Path) -> None:
        made = [bk.create_backup(source, root, prune=False).backup_id for _ in range(4)]
        newest = bk.list_backups(root)[0]["backup_id"]

        bk.prune_backups(root, 2)

        remaining = [item["backup_id"] for item in bk.list_backups(root)]
        assert len(remaining) == 2
        assert newest in remaining
        assert made[0] not in remaining

    def test_pruning_does_not_delete_the_only_backup(self, source: Path, root: Path) -> None:
        result = bk.create_backup(source, root, prune=False)

        bk.prune_backups(root, 1)

        assert bk.verify_backup(root, result.backup_id)

    def test_retention_prunes_on_create_by_default(self, source: Path, root: Path) -> None:
        for _ in range(3):
            bk.create_backup(source, root, retention=2)

        assert len(bk.list_backups(root)) == 2

    def test_prune_of_an_empty_root_is_a_no_op(self, tmp_path: Path) -> None:
        assert bk.prune_backups(tmp_path / "missing", 5) == []

    def test_incomplete_backups_are_never_pruned(self, source: Path, root: Path) -> None:
        good = bk.create_backup(source, root, prune=False)
        broken = root / "20260101T000000Z-sandbox-000000000000"
        broken.mkdir()
        (broken / bk.INCOMPLETE_MARKER).write_text("x", encoding="utf-8")

        bk.prune_backups(root, 0)

        assert broken.exists()
        assert good.path.exists()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestList:
    def test_listing_a_missing_root_is_empty(self, tmp_path: Path) -> None:
        assert bk.list_backups(tmp_path / "nope") == []

    def test_listing_is_newest_first(self, source: Path, root: Path) -> None:
        made = [bk.create_backup(source, root, prune=False).backup_id for _ in range(3)]

        assert [item["backup_id"] for item in bk.list_backups(root)] == sorted(made, reverse=True)

    def test_listing_reports_rows_for_complete_backups(self, source: Path, root: Path) -> None:
        bk.create_backup(source, root)

        assert bk.list_backups(root)[0]["rows"] == 42