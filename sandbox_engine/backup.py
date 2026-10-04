"""Crash-safe backup and explicitly-confirmed restore of the LadybugDB graph.

Why this is a cold copy
-----------------------
LadybugDB 0.20.4 exposes no backup, snapshot, or export API. The on-disk
representation is a single ``.lbug`` file plus an optional ``.lbug.wal``. A
committed transaction is durable in the WAL until a ``CHECKPOINT`` folds it
into the main file and removes the WAL. Three measured facts decide the design:

1. Copying ``.lbug`` while a ``.wal`` exists yields a database that is missing
   every transaction committed since the last checkpoint.
2. ``CHECKPOINT`` needs a read-write handle. Run against a read-only handle it
   raises, *and* leaves a stale WAL plus a "checkpoint in progress" state that
   makes the database refuse to reopen read-only until a read-write open
   recovers it. Probing with CHECKPOINT would take a serving instance down.
3. The writer lock is exclusive against other writers but invisible to readers.
   A read-only handle may be held while a read-write open still succeeds, and a
   writer blocks other writers outright.

So a backup is only taken against a *quiesced* source: no WAL sidecar, no
active writer, and the source bytes proven unchanged across the copy. That
matches the existing single-writer ownership rule -- back up while no ingestion
or ``--read-write`` server is running. Read-only query servers may keep running,
because they never write; the double-hash stability check covers even that.

Incomplete backups are detectable: a run writes ``.incomplete`` first and
removes it only after the payload is verified and ``manifest.json`` is durably
renamed into place.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "BackupError",
    "BackupResult",
    "RestoreResult",
    "create_backup",
    "list_backups",
    "prune_backups",
    "restore_backup",
    "verify_backup",
]

#: Bumped when the manifest layout changes incompatibly.
MANIFEST_VERSION = 1

PAYLOAD_NAME = "sandbox.lbug"
MANIFEST_NAME = "manifest.json"
INCOMPLETE_MARKER = ".incomplete"
TEMP_SUFFIX = ".tmp"

DEFAULT_BACKUP_ROOT = "backups"
DEFAULT_RETENTION = 14

_CHUNK = 1 << 20


class BackupError(RuntimeError):
    """A backup or restore precondition failed. Never leaves data half-written."""


# ---------------------------------------------------------------------------
# Hashing / durability helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _fsync_dir(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Durably publish JSON so a reader sees either the old file or the new one."""
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def wal_path(db_path: Path) -> Path:
    """Sidecar write-ahead log for ``db_path``."""
    return db_path.with_name(db_path.name + ".wal")


# ---------------------------------------------------------------------------
# Source identity
# ---------------------------------------------------------------------------


def _count(connection: Any, label: str) -> int:
    rows = connection.execute(f"MATCH (n:`{label}`) RETURN count(n)").get_all()
    return int(list(rows[0])[0])


def fingerprint(db_path: Path) -> dict[str, Any]:
    """Read-only semantic fingerprint of a database.

    Cheap enough to run against both source and restored copy, and specific
    enough that a truncated or mis-paired database fails loudly instead of
    passing a byte count.
    """
    import ladybug as lb

    database = lb.Database(str(db_path), read_only=True)
    try:
        connection = lb.Connection(database)
        try:
            node_tables = {
                label: _count(connection, label)
                for label in sorted(connection._get_node_table_names())
            }
            rel_tables = {}
            for entry in sorted(
                connection._get_rel_table_names(),
                key=lambda item: (item["name"], item["src"], item["dst"]),
            ):
                key = f"{entry['src']}-{entry['name']}->{entry['dst']}"
                rows = connection.execute(
                    f"MATCH (a)-[:`{entry['name']}`]->(b) RETURN count(*)"
                ).get_all()
                rel_tables[key] = int(list(rows[0])[0])
            return {
                "ladybug_version": str(database.get_version()),
                "storage_version": int(database.get_storage_version()),
                "node_tables": node_tables,
                "rel_tables": rel_tables,
                "node_rows": sum(node_tables.values()),
                "rel_rows": sum(rel_tables.values()),
            }
        finally:
            connection.close()
    finally:
        database.close()


def _fingerprint_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("node_tables", "rel_tables"))


# ---------------------------------------------------------------------------
# Quiesce gates
# ---------------------------------------------------------------------------


def _writer_lock_is_free(db_path: Path) -> bool:
    """True when no other process holds the LadybugDB writer lock.

    LadybugDB takes an exclusive ``flock`` on the database file for the lifetime
    of a read-write handle and denies other writers outright. Probing that lock
    with ``flock`` rather than by opening the database is deliberate: a
    read-write open followed by a close rewrites the file (it checkpoints), so
    probing that way would silently mutate the authoritative database on every
    backup. ``flock`` is released immediately and touches nothing.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX only
        raise BackupError(
            "cannot verify writer quiescence: fcntl is unavailable on this "
            "platform, and the only alternative probe would mutate the database"
        ) from None

    handle = os.open(db_path, os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(handle)
    return True


def _assert_quiesced(db_path: Path) -> None:
    """Refuse to copy a source that could change under us, or is incomplete."""
    if not db_path.is_file():
        raise BackupError(f"database not found: {db_path}")

    if not _writer_lock_is_free(db_path):
        raise BackupError(
            f"another process holds the LadybugDB writer lock on {db_path}\n"
            "  Stop ingestion and any --read-write server (graceful SIGTERM drains "
            "and checkpoints), then re-run. Read-only query servers are fine."
        )

    wal = wal_path(db_path)
    if wal.exists():
        raise BackupError(
            f"write-ahead log present: {wal}\n"
            "  The .lbug file does not yet contain recently committed transactions.\n"
            "  Stop every writer, then re-run. A clean shutdown checkpoints and "
            "removes the WAL.\n"
            "  Replaying it is a deliberate write to the authoritative database "
            "and is not done implicitly."
        )


# ---------------------------------------------------------------------------
# Backup records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    path: Path
    manifest: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def sha256(self) -> str:
        return self.manifest["payload"]["sha256"]

    @property
    def rows(self) -> int:
        return self.manifest["source"]["fingerprint"]["node_rows"]


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    restored: Path
    displaced: Path | None
    manifest: dict[str, Any] = field(repr=False, default_factory=dict)


def _unique_backup_dir(backup_root: Path, db_path: Path, digest: str) -> Path:
    """A free backup directory whose name is sortable and self-identifying.

    Microsecond precision keeps ordinary backups distinct; the loop covers the
    pathological case of two identical databases backed up within one
    microsecond, which would otherwise collide and abort the second run.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = f"{stamp}-{db_path.stem}-{digest[:12]}"
    candidate = backup_root / base
    attempt = 2
    while candidate.exists():
        candidate = backup_root / f"{base}-{attempt}"
        attempt += 1
    return candidate


def _is_complete(path: Path) -> bool:
    return (path / MANIFEST_NAME).is_file() and not (path / INCOMPLETE_MARKER).exists()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_backup(
    db_path: Path,
    backup_root: Path,
    *,
    retention: int = DEFAULT_RETENTION,
    prune: bool = True,
    host: str | None = None,
) -> BackupResult:
    """Copy a quiesced database into a timestamped, self-verifying backup.

    The copy is published atomically: the payload lands under a temp name and is
    renamed, and ``manifest.json`` -- the completion marker -- is written last.
    A crash at any point leaves a directory that :func:`verify_backup` rejects.
    """
    import ladybug as lb  # noqa: F401  (fail fast before creating any directory)

    db_path = Path(db_path)
    backup_root = Path(backup_root)
    _assert_quiesced(db_path)

    source_fingerprint = fingerprint(db_path)
    pre_sha = _sha256(db_path)

    backup_root.mkdir(parents=True, exist_ok=True)
    target = _unique_backup_dir(backup_root, db_path, pre_sha)
    target.mkdir()
    marker = target / INCOMPLETE_MARKER
    marker.write_text("backup in progress\n", encoding="utf-8")

    try:
        payload = target / PAYLOAD_NAME
        tmp_payload = target / (PAYLOAD_NAME + TEMP_SUFFIX)
        with db_path.open("rb") as src, tmp_payload.open("wb") as dst:
            shutil.copyfileobj(src, dst, _CHUNK)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_payload, payload)
        _fsync_file(payload)
        _fsync_dir(target)

        post_sha = _sha256(db_path)
        if post_sha != pre_sha:
            raise BackupError(
                "source database changed during the copy "
                f"({pre_sha[:12]} -> {post_sha[:12]}); the backup was discarded"
            )

        payload_sha = _sha256(payload)
        if payload_sha != pre_sha:
            raise BackupError(
                f"payload digest {payload_sha[:12]} does not match source "
                f"{pre_sha[:12]}; the backup was discarded"
            )

        restored_fingerprint = fingerprint(payload)
        if not _fingerprint_matches(source_fingerprint, restored_fingerprint):
            raise BackupError(
                "restored copy does not match the source fingerprint; "
                "the backup was discarded"
            )

        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "backup_id": target.name,
            "complete": True,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": host or socket.gethostname(),
            "source": {
                "path": str(db_path.resolve()),
                "name": db_path.name,
                "size_bytes": db_path.stat().st_size,
                "sha256": pre_sha,
                "wal_absent": True,
                "writer_lock_free": True,
                "fingerprint": source_fingerprint,
            },
            "payload": {
                "name": PAYLOAD_NAME,
                "size_bytes": payload.stat().st_size,
                "sha256": payload_sha,
            },
        }
        _write_json_atomic(target / MANIFEST_NAME, manifest)
        marker.unlink()
        _fsync_dir(target)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise

    if prune and retention > 0:
        prune_backups(backup_root, retention)

    return BackupResult(backup_id=target.name, path=target, manifest=manifest)


# ---------------------------------------------------------------------------
# Inspect / verify / prune
# ---------------------------------------------------------------------------


def list_backups(backup_root: Path) -> list[dict[str, Any]]:
    """Newest-first inventory, separating complete backups from incomplete ones."""
    backup_root = Path(backup_root)
    if not backup_root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(backup_root.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / MANIFEST_NAME
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
        found.append(
            {
                "backup_id": entry.name,
                "path": entry,
                "complete": _is_complete(entry),
                "created_utc": manifest.get("created_utc", ""),
                "size_bytes": manifest.get("payload", {}).get("size_bytes"),
                "rows": manifest.get("source", {}).get("fingerprint", {}).get("node_rows"),
            }
        )
    found.sort(key=lambda item: item["backup_id"], reverse=True)
    return found


def verify_backup(backup_root: Path, backup_id: str) -> dict[str, Any]:
    """Re-check a backup end to end and prove it can be opened as a database."""
    target = Path(backup_root) / backup_id
    if not target.is_dir():
        raise BackupError(f"no such backup: {target}")

    if (target / INCOMPLETE_MARKER).exists():
        raise BackupError(f"backup is incomplete (marker present): {backup_id}")
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BackupError(
            f"backup is incomplete (no {MANIFEST_NAME}); it cannot be restored: {backup_id}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackupError(f"manifest is unreadable for {backup_id}: {exc}") from exc
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise BackupError(
            f"unsupported manifest version {manifest.get('manifest_version')!r} "
            f"for {backup_id}"
        )

    payload = target / manifest["payload"]["name"]
    if not payload.is_file():
        raise BackupError(f"payload missing for {backup_id}: {payload}")

    actual = _sha256(payload)
    if actual != manifest["payload"]["sha256"]:
        raise BackupError(
            f"payload digest mismatch for {backup_id}: "
            f"expected {manifest['payload']['sha256'][:12]}, got {actual[:12]}"
        )

    actual_fingerprint = fingerprint(payload)
    if not _fingerprint_matches(manifest["source"]["fingerprint"], actual_fingerprint):
        raise BackupError(f"fingerprint mismatch for {backup_id}: content does not match manifest")

    return {"backup_id": backup_id, "manifest": manifest, "fingerprint": actual_fingerprint}


def prune_backups(backup_root: Path, retention: int) -> list[str]:
    """Delete complete backups beyond the newest ``retention``; report the rest."""
    if retention <= 0:
        return []
    entries = [item for item in list_backups(backup_root) if item["complete"]]
    removed: list[str] = []
    for item in entries[retention:]:
        shutil.rmtree(item["path"], ignore_errors=True)
        removed.append(item["backup_id"])
    return removed


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_backup(
    db_path: Path,
    backup_root: Path,
    backup_id: str,
    *,
    confirm: bool = False,
    on_progress: Callable[[str], None] = lambda message: None,
) -> RestoreResult:
    """Install a verified backup, moving any existing database aside.

    Never overwrites silently: the current database is renamed to
    ``<name>.pre-restore-<stamp>`` rather than deleted, and a stale WAL is moved
    with it so a recovered journal cannot be replayed into the restored file.
    The live database is validated after the swap.
    """
    import ladybug as lb

    db_path = Path(db_path)
    if not confirm:
        raise BackupError(
            "restore is a destructive operator action; re-run with confirm=True "
            "(CLI: --confirm)"
        )

    checked = verify_backup(Path(backup_root), backup_id)
    manifest = checked["manifest"]

    db_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    displaced: Path | None = None

    if db_path.exists():
        if _writer_lock_is_free(db_path) is False:
            raise BackupError(
                f"a process is using {db_path}; stop it (graceful SIGTERM drains and "
                "checkpoints) before restoring"
            )
        displaced = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}")
        os.replace(db_path, displaced)
        on_progress(f"moved existing database aside: {displaced}")

    wal = wal_path(db_path)
    if wal.exists():
        parked = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}.wal")
        os.replace(wal, parked)
        displaced = displaced or parked
        on_progress(f"moved stale write-ahead log aside: {parked}")

    try:
        staged = db_path.with_name(db_path.name + TEMP_SUFFIX)
        shutil.copyfile(Path(backup_root) / backup_id / manifest["payload"]["name"], staged)
        staged_sha = _sha256(staged)
        if staged_sha != manifest["payload"]["sha256"]:
            raise BackupError(
                f"staged copy digest mismatch: expected "
                f"{manifest['payload']['sha256'][:12]}, got {staged_sha[:12]}"
            )
        os.replace(staged, db_path)
        _fsync_file(db_path)
        _fsync_dir(db_path.parent)
    except BaseException:
        if displaced is not None and not db_path.exists():
            os.replace(displaced, db_path)
        raise

    try:
        live = fingerprint(db_path)
    except Exception as exc:
        raise BackupError(
            f"restored database failed to open: {exc}. The pre-restore copy is at "
            f"{displaced or 'n/a'}"
        ) from exc
    if not _fingerprint_matches(manifest["source"]["fingerprint"], live):
        raise BackupError(
            f"restored database fingerprint does not match the backup manifest; "
            f"the pre-restore copy is at {displaced or 'n/a'}"
        )

    database = lb.Database(str(db_path), read_only=True)
    database.close()
    on_progress("restored database opens cleanly")

    return RestoreResult(
        backup_id=backup_id,
        restored=db_path,
        displaced=displaced,
        manifest=manifest,
    )