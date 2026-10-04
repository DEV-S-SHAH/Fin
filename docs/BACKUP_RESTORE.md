# Backup and Restore

Operational procedures for the authoritative FinGraph database. Written against
LadybugDB 0.20.4 as measured on this machine, not from documentation.

## What is backed up

One authoritative file:

| | |
|---|---|
| Path | `sandbox_engine/_run/sandbox.lbug` |
| Size | 57,118,720 bytes (122,158 nodes / 189,723 relationships) |
| Format | single LadybugDB file, optionally with a `.lbug.wal` sidecar |

Each backup is a directory holding the payload and a manifest:

```
backups/20261003T140838737793Z-sandbox-a545c8faca13/
  sandbox.lbug     byte-identical copy of the database
  manifest.json    written last; its presence marks the backup complete
```

The backup ID is `<UTC timestamp>-<source name>-<first 12 of source sha256>`, so
the listing sorts chronologically and each backup names its own source.

## Why a live backup is not safe

LadybugDB exposes **no backup, snapshot, or export API**. Three measured facts
force a cold copy:

1. **A `.lbug` file is only complete once a `CHECKPOINT` has run.** Committed
   transactions live in `sandbox.lbug.wal` until `CHECKPOINT` folds them in and
   deletes the WAL. Copying the `.lbug` alone while a WAL exists produces a
   database that is missing everything committed since the last checkpoint.
2. **`CHECKPOINT` requires a read-write handle.** Run against a read-only
   handle it raises `Cannot write to file`, *and* leaves a stale WAL plus a
   "checkpoint in progress" state that makes the database refuse to reopen
   read-only until a read-write open recovers it. Using `CHECKPOINT` to reach a
   clean state would take a serving instance down.
3. **The writer lock is exclusive against writers and invisible to readers.**
   A read-write handle blocks other writers outright, but a read-only holder
   does not block a read-write open.

Consequence: a consistent backup requires a **quiesced** source. This matches
the existing single-writer ownership rule — no ingestion and no `--read-write`
server during a backup.

## Safe boundary

A backup is taken only when all of these hold:

| Gate | Failure message | Meaning |
|---|---|---|
| Source exists | `database not found` | Nothing to copy |
| No writer holds the `flock` | `another process holds the LadybugDB writer lock` | A writer could change the bytes mid-copy |
| No `.wal` sidecar | `write-ahead log present` | The `.lbug` is missing recent commits |
| Source sha256 unchanged across the copy | `source database changed during the copy` | Detects a mutation that slipped past the gates |
| Payload digest matches source | `payload digest ... does not match source` | Detects a bad copy |
| Restored fingerprint matches source | `does not match the source fingerprint` | Detects a truncated or mis-paired database |

The quiesce probe uses `fcntl.flock`, not a read-write open. Opening the
database read-write and closing it rewrites the file by checkpointing, so that
approach would mutate the authoritative database on every backup. `flock` is
released immediately and touches nothing — the source is provably unmodified,
which the test suite asserts.

**Read-only query servers may keep running during a backup.** They never write,
and the double-hash stability check covers them anyway. Ingestion and
`--read-write` servers must be stopped. Coordinate that with graceful shutdown:
`SIGTERM` drains, checkpoints, and releases the lock.

A WAL is never deleted automatically. It may hold the only copy of a committed
transaction, which is the same reasoning behind `WalRecoveryError` in
`sandbox_engine/loader.py`. Recovering it is a deliberate write to the
authoritative database, so the tool refuses and tells you to stop the writer.

## Commands

```bash
# Create a verified backup (default --backup-dir backups, --retention 14)
python -m sandbox_engine --backup create

# Inventory, newest first; INCOMPLETE entries are failed runs
python -m sandbox_engine --backup list

# Re-verify a backup: digest + fingerprint + opens as a database
python -m sandbox_engine --backup verify --backup-id <id>

# Restore. --confirm is mandatory.
python -m sandbox_engine --backup restore --backup-id <id> --confirm
```

Both `--backup-dir` and `--retention` are configurable per invocation.
`--retention 0` disables pruning.

Backup deliberately runs in its own process and short-circuits before any
ingestion stage: LadybugDB hands the writer lock to whoever opens read-write, so
a backup taken from inside a running load would race the writer it exists to
avoid.

## Restore safety

Restore is an explicit operator action and is designed to be hard to do by
accident:

- **Refuses without `--confirm`.** Exit code 2, nothing written.
- **Never deletes.** The current database is renamed to
  `<name>.pre-restore-<UTC stamp>`, not overwritten. A failed restore moves it
  back.
- **Verifies before swapping.** The payload digest is checked after staging into
  the destination, so a corrupted backup cannot reach the live path.
- **Parks a stale WAL** to `<name>.pre-restore-<stamp>.wal`. A leftover journal
  from a crashed run would otherwise be replayed on top of the restored file.
- **Validates after the swap** by fingerprint, then confirms the database opens.
- **Refuses while a writer holds the lock.**

## RPO and RTO

Measured on this machine (57 MB database):

| | |
|---|---|
| Backup | ~0.5 s |
| Verify | ~0.3 s |
| Restore | ~0.6 s |
| **RTO** | **under 1 minute** including verification |
| **RPO** | **time since the last backup** |

Backups are manual. There is no scheduler, so RPO is whatever interval you
actually run it at. Until one exists, run `--backup create` after every
ingestion run:

```bash
python -m sandbox_engine --load-only && python -m sandbox_engine --backup create
```

Retention keeps the newest 14 by default, roughly a fortnight of daily backups at
1.1 GB. Set `--retention` to match your disk budget.

## Failure scenarios

| Scenario | What happens | Recovery |
|---|---|---|
| Crash mid-copy | Directory has `.incomplete`; no `manifest.json`. Listed as `INCOMPLETE`, refused by verify and restore | `rm -rf` the directory |
| Crash after payload, before manifest | Same: manifest is the only completion marker | `rm -rf` the directory |
| Bit rot in a backup payload | `--backup verify` fails on digest or fingerprint | Restore an older backup |
| Database corrupted by a hard kill | LadybugDB refuses to open, `.wal` present | Restore the newest verified backup; the crashed file is moved aside, not deleted |
| Backup taken while a writer ran | Refused at the lock gate; or double-hash mismatch and the copy is discarded | Stop the writer, re-run |
| Backup taken while a WAL existed | Refused at the WAL gate | Clean shutdown, re-run |
| Restore interrupted | Staged copy is a `.tmp`; live database still the displaced original | Re-run `--backup restore --confirm` |
| Restored backup is wrong | Pre-restore file preserved at `<name>.pre-restore-<stamp>` | `mv` it back |

## Recovery drill

```bash
python -m sandbox_engine --backup list
python -m sandbox_engine --backup verify --backup-id <id>
mkdir -p /tmp/drill/_run
python -m sandbox_engine --db /tmp/drill/_run/sandbox.lbug \
    --backup restore --backup-id <id> --confirm
cmp sandbox_engine/_run/sandbox.lbug /tmp/drill/_run/sandbox.lbug
```

Restore into a scratch `--db` path rather than over the authoritative file, so
the drill rehearses the procedure without risking production.

## Tests

`tests/test_backup.py` — 39 tests covering the gates, atomicity, incomplete
detection, digest and fingerprint tampering, refusal without confirmation,
displacement instead of deletion, WAL parking, retention, and that a successful
backup leaves the source byte-identical. The writer-lock test holds a real
cross-process handle. No test opens the authoritative database.