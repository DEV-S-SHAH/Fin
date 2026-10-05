"""Restart persistence tests for FinGraph.

These tests verify that the authoritative LadybugDB, concept registry,
ingestion staging, and cache state survive process restarts correctly.

Test pattern: START -> write data -> STOP -> START -> verify same data available.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sandbox_engine.background import BackgroundIngestQueue
from sandbox_engine.config import FINGRAPH_DATA_DIR, Paths, default_paths, resolve_scope
from sandbox_engine.parser import stable_id
from sandbox_engine.query_ui import KnowledgeGraph, resolve_db_path


class TestPersistentDataDirectory:
    """Tests for the FINGRAPH_DATA_DIR configuration."""

    def test_fingraph_data_dir_defaults_to_local_data(self) -> None:
        """Default persistent directory is ./data relative to repo root."""
        # The default is set in config.py via os.environ.get
        assert FINGRAPH_DATA_DIR.name == "data"

    def test_fingraph_data_dir_respects_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FINGRAPH_DATA_DIR environment variable overrides the default."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom_persistent"
            monkeypatch.setenv("FINGRAPH_DATA_DIR", str(custom))
            # Re-import to pick up the new env var
            import importlib
            import sandbox_engine.config as config
            importlib.reload(config)
            assert config.FINGRAPH_DATA_DIR == custom.resolve()

    def test_paths_under_uses_persistent_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paths.under() places db, staging, registry in FINGRAPH_DATA_DIR."""
        with tempfile.TemporaryDirectory() as tmp:
            persistent = Path(tmp) / "persistent"
            monkeypatch.setenv("FINGRAPH_DATA_DIR", str(persistent))
            import importlib
            import sandbox_engine.config as config
            importlib.reload(config)

            paths = config.Paths.under("/tmp/some_root")
            # Use resolve() to handle macOS /private/var vs /var symlink
            assert paths.db.resolve() == (persistent / "sandbox.lbug").resolve()
            assert paths.staging.resolve() == (persistent / "staging").resolve()
            assert paths.report.resolve() == (persistent / "report.json").resolve()
            assert paths.registry.resolve() == (persistent / "concepts.json").resolve()

    def test_paths_reset_never_deletes_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paths.reset() clears staging/report/registry but NEVER the database."""
        with tempfile.TemporaryDirectory() as tmp:
            persistent = Path(tmp) / "persistent"
            monkeypatch.setenv("FINGRAPH_DATA_DIR", str(persistent))
            import importlib
            import sandbox_engine.config as config
            importlib.reload(config)

            paths = config.Paths.under("/tmp/some_root")
            paths.ensure()

            # Create all files
            paths.db.write_text("fake db")
            paths.db.with_name("sandbox.lbug.wal").write_text("fake wal")
            paths.staging.mkdir(parents=True, exist_ok=True)
            (paths.staging / "test.parquet").write_text("data")
            paths.report.write_text("{}")
            paths.registry.write_text("{}")

            # Reset should only clear staging, report, registry
            paths.reset()

            assert paths.db.exists(), "Database must survive reset"
            assert paths.db.with_name("sandbox.lbug.wal").exists(), "WAL must survive reset"
            assert not paths.staging.exists() or not any(paths.staging.iterdir()), "Staging cleared"
            assert not paths.report.exists(), "Report cleared"
            assert not paths.registry.exists(), "Registry cleared"


class TestLadybugDBPersistence:
    """Tests that the LadybugDB survives restarts."""

    def setup_method(self) -> None:
        """Create a temporary persistent directory for each test."""
        self.tmpdir = tempfile.mkdtemp(prefix="fingraph_persist_test_")
        self.persistent_dir = Path(self.tmpdir) / "data"
        os.environ["FINGRAPH_DATA_DIR"] = str(self.persistent_dir)

        # Reload config to pick up new env var
        import importlib
        import sandbox_engine.config as config
        importlib.reload(config)

        self.paths = config.Paths.under("/tmp/test_root")
        self.paths.ensure()

    def teardown_method(self) -> None:
        """Clean up temporary directory."""
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_knowledge_graph_opens_existing_db(self) -> None:
        """KnowledgeGraph opens existing database without re-initializing."""
        # Create a minimal LadybugDB with the expected schema
        import ladybug as lb

        db = lb.Database(str(self.paths.db))
        conn = lb.Connection(db)
        try:
            # Create the schema that KnowledgeGraph expects
            conn.execute("CREATE NODE TABLE IF NOT EXISTS Company (ticker STRING PRIMARY KEY, name STRING, sector STRING)")
            conn.execute("CREATE NODE TABLE IF NOT EXISTS Filing (accession_number STRING PRIMARY KEY, form_type STRING)")
            conn.execute("CREATE REL TABLE IF NOT EXISTS SUBMITTED (FROM Company TO Filing)")
            # Insert a test company
            conn.execute("MERGE (c:Company {ticker: 'TEST'}) SET c.name = 'Test Corp', c.sector = 'Testing'")
        finally:
            conn.close()
            db.close()

        # First open - should detect schema and work
        kg1 = KnowledgeGraph(self.paths.db, read_only=True)
        try:
            rows = kg1.execute("MATCH (c:Company) RETURN c.ticker, c.name")
            assert len(rows) == 1
            assert rows[0][0] == "TEST"
            assert rows[0][1] == "Test Corp"
            schema1 = kg1.schema
        finally:
            kg1.close()

        # Second open (simulating restart) - should open same DB
        kg2 = KnowledgeGraph(self.paths.db, read_only=True)
        try:
            rows = kg2.execute("MATCH (c:Company) RETURN c.ticker, c.name")
            assert len(rows) == 1
            assert rows[0][0] == "TEST"
            schema2 = kg2.schema
        finally:
            kg2.close()

        # Schema detection should be consistent
        assert schema1 == schema2

    def test_knowledge_graph_read_write_isolation(self) -> None:
        """Read-only handles don't conflict; read-write is single-slot."""
        import ladybug as lb

        db = lb.Database(str(self.paths.db))
        conn = lb.Connection(db)
        try:
            conn.execute("CREATE NODE TABLE IF NOT EXISTS Company (ticker STRING PRIMARY KEY, name STRING)")
        finally:
            conn.close()
            db.close()

        # Multiple read-only handles should work
        kg_ro1 = KnowledgeGraph(self.paths.db, read_only=True)
        kg_ro2 = KnowledgeGraph(self.paths.db, read_only=True)
        try:
            kg_ro1.execute("MATCH (c:Company) RETURN count(c)")
            kg_ro2.execute("MATCH (c:Company) RETURN count(c)")
        finally:
            kg_ro1.close()
            kg_ro2.close()

        # Read-write handle gets single slot
        kg_rw = KnowledgeGraph(self.paths.db, read_only=False)
        try:
            assert kg_rw.pool.max_size == 1
        finally:
            kg_rw.close()


class TestConceptRegistryPersistence:
    """Tests that the concept registry (dedup state) survives restarts."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="fingraph_concept_test_")
        self.persistent_dir = Path(self.tmpdir) / "data"
        os.environ["FINGRAPH_DATA_DIR"] = str(self.persistent_dir)

        import importlib
        import sandbox_engine.config as config
        importlib.reload(config)

        self.paths = config.Paths.under("/tmp/test_root")
        self.paths.ensure()

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concept_registry_survives_restart(self) -> None:
        """ConceptRegistry saved to persistent dir is loaded on next run.

        Follows the same pattern as test_round_trip_preserves_identity:
        register canonical with alias, then register the alias name to trigger merge.
        """
        from sandbox_engine.entity_resolver import ConceptRegistry

        # Create and populate registry (using "metric" kind as per test conventions)
        registry1 = ConceptRegistry(stable_id)
        # Register "Net Sales" as canonical with alias "Total Revenue"
        registry1.register("metric", "Net Sales", scope="FY2025", aliases=["Total Revenue"])
        # Register the alias name to trigger merge
        registry1.register("metric", "Total Revenue", scope="FY2025")
        registry1.save(self.paths.registry)

        # Verify saved
        assert self.paths.registry.exists()

        # Load in new registry (simulating restart)
        registry2 = ConceptRegistry.load(self.paths.registry, stable_id)

        # Should have same entities - both names resolve to same ID
        entities1 = registry1.entities("metric", "FY2025")
        entities2 = registry2.entities("metric", "FY2025")
        ids1 = {e.id for e in entities1}
        ids2 = {e.id for e in entities2}
        assert ids1 == ids2
        # "Net Sales" and "Total Revenue" should resolve to same ID (merged via alias)
        assert len(ids1) == 1, f"Expected 1 entity, got {len(ids1)}: {ids1}"


class TestIngestionStagingPersistence:
    """Tests that ingestion staging state survives restarts."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="fingraph_staging_test_")
        self.persistent_dir = Path(self.tmpdir) / "data"
        os.environ["FINGRAPH_DATA_DIR"] = str(self.persistent_dir)

        import importlib
        import sandbox_engine.config as config
        importlib.reload(config)
        import sandbox_engine.background as background
        importlib.reload(background)

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_staged_ticker_recovers_as_in_flight(self) -> None:
        """A ticker with 'staged' status becomes in-flight on restart."""
        queue = BackgroundIngestQueue(max_workers=1)

        # Manually create a staging file with "staged" status
        staging_file = queue.staging_dir / "AAPL.jsonl"
        staged_record = {
            "ticker": "AAPL",
            "status": "staged",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        staging_file.write_text(json.dumps(staged_record) + "\n")

        # Create new queue (simulating restart)
        queue2 = BackgroundIngestQueue(max_workers=1)

        # The ticker should be marked as in-flight
        assert queue2.is_in_flight("AAPL")

    def test_extracted_ticker_not_in_flight(self) -> None:
        """A ticker with 'extracted' status is ready for next stage, not in-flight."""
        queue = BackgroundIngestQueue(max_workers=1)

        staging_file = queue.staging_dir / "MSFT.jsonl"
        extracted_record = {
            "ticker": "MSFT",
            "status": "extracted",
            "timestamp": "2024-01-01T00:00:00Z",
            "payload": {"nodes": [], "edges": []},
        }
        staging_file.write_text(json.dumps(extracted_record) + "\n")

        queue2 = BackgroundIngestQueue(max_workers=1)

        # Should NOT be in-flight; is_staged should return True (extracted = complete)
        assert not queue2.is_in_flight("MSFT")
        assert queue2.is_staged("MSFT")  # extracted = staged/complete

    def test_failed_ticker_available_for_retry(self) -> None:
        """A ticker with 'failed' status is not staged and can be retried."""
        queue = BackgroundIngestQueue(max_workers=1)

        staging_file = queue.staging_dir / "GOOGL.jsonl"
        failed_record = {
            "ticker": "GOOGL",
            "status": "failed",
            "timestamp": "2024-01-01T00:00:00Z",
            "error": "timeout",
        }
        staging_file.write_text(json.dumps(failed_record) + "\n")

        queue2 = BackgroundIngestQueue(max_workers=1)

        # Failed should not be considered staged, allowing retry
        assert not queue2.is_staged("GOOGL")
        assert not queue2.is_in_flight("GOOGL")


class TestCacheThreadSafety:
    """Tests for thread-safe cache behavior."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="fingraph_cache_test_")
        self.persistent_dir = Path(self.tmpdir) / "data"
        os.environ["FINGRAPH_DATA_DIR"] = str(self.persistent_dir)

        import importlib
        import sandbox_engine.config as config
        importlib.reload(config)
        import ui.fingraph.server as server
        importlib.reload(server)

        self.server_module = server

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ttl_cache_basic_operations(self) -> None:
        """Basic get/set/eviction works."""
        cache = self.server_module._TTLCache(ttl_seconds=1.0, max_size=3)

        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.get("a") == 1
        assert cache.get("b") == 2
        assert cache.get("c") is None

    def test_ttl_cache_expiration(self) -> None:
        """Entries expire after TTL."""
        cache = self.server_module._TTLCache(ttl_seconds=0.1, max_size=10)

        cache.set("x", 100)
        assert cache.get("x") == 100
        time.sleep(0.15)
        assert cache.get("x") is None  # Expired

    def test_ttl_cache_lru_eviction(self) -> None:
        """Oldest entries evicted when at capacity."""
        cache = self.server_module._TTLCache(ttl_seconds=60.0, max_size=2)

        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)  # Should evict "a"

        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_ttl_cache_thread_safety(self) -> None:
        """Concurrent access doesn't corrupt cache."""
        cache = self.server_module._TTLCache(ttl_seconds=60.0, max_size=100)
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                for i in range(start, start + 50):
                    cache.set(f"key{i}", i)
            except Exception as e:
                errors.append(e)

        def reader() -> None:
            try:
                for _ in range(100):
                    cache.get("key0")
                    cache.get("key99")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(50,)),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Thread safety errors: {errors}"

    def test_market_cache_bounded(self) -> None:
        """Market cache is bounded to 1 entry (the batch)."""
        # The cache is created at module level; just verify it exists
        assert isinstance(self.server_module._markets_cache, self.server_module._TTLCache)
        assert self.server_module._markets_cache._max_size == 1

    def test_company_caches_bounded(self) -> None:
        """Company detail/quote caches have reasonable bounds."""
        assert isinstance(self.server_module._company_detail_cache, self.server_module._TTLCache)
        assert isinstance(self.server_module._company_quote_cache, self.server_module._TTLCache)
        assert self.server_module._company_detail_cache._max_size == 128
        assert self.server_module._company_quote_cache._max_size == 256

    def test_apple_jwks_cache_bounded(self) -> None:
        """Apple JWKS cache is bounded to 1 entry."""
        assert isinstance(self.server_module._apple_jwks_cache, self.server_module._TTLCache)
        assert self.server_module._apple_jwks_cache._max_size == 1


class TestSessionPersistence:
    """Tests for session cookie persistence across restarts."""

    def test_auth_secret_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTH_SECRET is read from FINGRAPH_AUTH_SECRET env var."""
        monkeypatch.setenv("FINGRAPH_AUTH_SECRET", "test-secret-123")

        import importlib
        import ui.fingraph.server as server
        importlib.reload(server)

        assert server.AUTH_SECRET == "test-secret-123"

    def test_auth_secret_fallback_random(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without env var, a random secret is generated (not for production)."""
        monkeypatch.delenv("FINGRAPH_AUTH_SECRET", raising=False)

        import importlib
        import ui.fingraph.server as server
        importlib.reload(server)

        # Should be a 64-char hex string (32 bytes)
        assert len(server.AUTH_SECRET) == 64
        assert all(c in "0123456789abcdef" for c in server.AUTH_SECRET)

    def test_session_cookie_survives_restart_with_fixed_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session cookie verifies with same secret across restarts."""
        monkeypatch.setenv("FINGRAPH_AUTH_SECRET", "fixed-secret-for-testing")
        monkeypatch.setenv("FINGRAPH_DEV_LOGIN", "1")

        import importlib
        import ui.fingraph.server as server
        importlib.reload(server)

        # Create a session token
        token1 = server._session_token("dev")
        provider1 = server._session_provider(token1)
        assert provider1 == "dev"

        # Simulate restart: reload module (same secret)
        importlib.reload(server)

        # Token should still verify
        provider2 = server._session_provider(token1)
        assert provider2 == "dev"

    def test_session_cookie_fails_with_different_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session cookie fails verification if secret changes."""
        monkeypatch.setenv("FINGRAPH_AUTH_SECRET", "secret-one")

        import importlib
        import ui.fingraph.server as server
        importlib.reload(server)

        token = server._session_token("dev")

        # Change secret (simulating restart without fixed secret)
        monkeypatch.setenv("FINGRAPH_AUTH_SECRET", "secret-two")
        importlib.reload(server)

        # Should fail verification
        provider = server._session_provider(token)
        assert provider is None


class TestEndToEndRestart:
    """Full end-to-end restart simulation."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="fingraph_e2e_test_")
        self.persistent_dir = Path(self.tmpdir) / "data"
        os.environ["FINGRAPH_DATA_DIR"] = str(self.persistent_dir)

        import importlib
        import sandbox_engine.config as config
        importlib.reload(config)

        self.paths = config.Paths.under("/tmp/test_root")
        self.paths.ensure()

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_restart_cycle(self) -> None:
        """Simulate full START -> STOP -> START cycle with data persistence."""
        import ladybug as lb

        # === START 1: Create database and insert data ===
        db = lb.Database(str(self.paths.db))
        conn = lb.Connection(db)
        try:
            conn.execute("CREATE NODE TABLE IF NOT EXISTS Company (ticker STRING PRIMARY KEY, name STRING)")
            conn.execute("MERGE (c:Company {ticker: 'AAPL'}) SET c.name = 'Apple Inc.'")
            conn.execute("MERGE (c:Company {ticker: 'MSFT'}) SET c.name = 'Microsoft Corp'")
        finally:
            conn.close()
            db.close()

        # Verify data exists
        kg1 = KnowledgeGraph(self.paths.db, read_only=True)
        try:
            companies = kg1.execute("MATCH (c:Company) RETURN c.ticker, c.name ORDER BY c.ticker")
            assert len(companies) == 2
            assert companies[0] == ["AAPL", "Apple Inc."]
            assert companies[1] == ["MSFT", "Microsoft Corp"]
        finally:
            kg1.close()

        # === STOP: Close handles ===
        # (kg1.close() already called)

        # === START 2: Reopen (simulating process restart) ===
        kg2 = KnowledgeGraph(self.paths.db, read_only=True)
        try:
            companies = kg2.execute("MATCH (c:Company) RETURN c.ticker, c.name ORDER BY c.ticker")
            assert len(companies) == 2
            assert companies[0] == ["AAPL", "Apple Inc."]
            assert companies[1] == ["MSFT", "Microsoft Corp"]
        finally:
            kg2.close()

        # === Verify concept registry also persists ===
        from sandbox_engine.entity_resolver import ConceptRegistry

        registry1 = ConceptRegistry(stable_id)
        registry1.register("metric", "Net Sales", scope="FY2025")
        registry1.save(self.paths.registry)

        registry2 = ConceptRegistry.load(self.paths.registry, stable_id)
        entities1 = registry1.entities("metric", "FY2025")
        entities2 = registry2.entities("metric", "FY2025")
        assert {e.id for e in entities1} == {e.id for e in entities2}

        # === Verify BackgroundIngestQueue staging persists ===
        from sandbox_engine.background import BackgroundIngestQueue
        queue1 = BackgroundIngestQueue()
        # Simulate an extracted ticker
        staging_file = queue1.staging_dir / "TSLA.jsonl"
        staging_file.write_text(json.dumps({
            "ticker": "TSLA",
            "status": "extracted",
            "timestamp": "2024-01-01T00:00:00Z",
            "payload": {},
        }) + "\n")

        queue2 = BackgroundIngestQueue()
        assert queue2.is_staged("TSLA")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])