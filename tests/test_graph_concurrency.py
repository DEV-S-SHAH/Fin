"""Concurrency tests for the graph read path and the LLM backend registry.

Two separate claims are under test.

The first is that ``RagBackends`` -- named by the audit as a process-wide
serializer -- is not one. Its lock protects real mutable state and is kept, but
it is held for microseconds and never across I/O, so independent requests are
already free to run concurrently. These tests pin that down so a future change
cannot quietly widen the critical section.

The second is that the connection which *did* serialise reads has been replaced
by a bounded pool, and that the pool behaves when pushed on: it never exceeds
its size, it hands out work in parallel, it recycles connections, and it gives
back a slot that failed rather than leaking the capacity.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import ladybug as lb

from sandbox_engine import ddl, query_ui
from sandbox_engine.query_ui import KnowledgeGraph, RagBackends, _ConnectionPool


def _temp_graph(read_only: bool = True) -> tuple[TemporaryDirectory, KnowledgeGraph]:
    """A throwaway database with the blueprint schema, as the UI expects."""
    tmp = TemporaryDirectory(prefix="graph-concurrency-")
    path = Path(tmp.name) / "g.lbug"
    db = lb.Database(str(path))
    conn = lb.Connection(db)
    try:
        ddl.ensure_schema(conn)
    finally:
        conn.close()
        db.close()
    kg = KnowledgeGraph(path, read_only=read_only)
    return tmp, kg


class RagBackendsIsNotASerializer(unittest.TestCase):
    """The audit finding this module exists to refute, as executable assertions."""

    def setUp(self) -> None:
        self.backends = RagBackends()

    def test_the_lock_is_not_held_across_the_ollama_probe(self) -> None:
        """A network call inside the critical section would serialise every
        request in the process for the length of a probe."""
        source = inspect_source(query_ui.RagBackends.ollama_models)
        probe_call = source.index("self._probe_ollama()")
        first_lock = source.index("with self._lock")
        last_lock = source.rindex("with self._lock")
        self.assertLess(
            first_lock, probe_call,
            "the probe must start outside the lock",
        )
        self.assertLess(
            probe_call, last_lock,
            "the probe must finish before the lock is retaken to cache it",
        )

    def test_resolution_does_not_block_other_threads(self) -> None:
        """Many threads resolving at once should all finish in roughly the time
        one takes. A real lock over resolution would scale linearly."""
        self.backends.ollama_models = lambda refresh=False: (False, [])
        start = time.perf_counter()
        threads = [
            threading.Thread(target=self.backends.resolve)
            for _ in range(200)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        elapsed = time.perf_counter() - start
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertLess(elapsed, 5.0, "resolve() appears to serialise")

    def test_concurrent_setters_and_readers_stay_consistent(self) -> None:
        """The lock is what makes a forced backend and the key it was set with
        agree, so concurrent mutation must not tear."""
        self.backends.ollama_models = lambda refresh=False: (False, [])
        self.backends.set_session_key("k" * 64)
        errors: list[str] = []

        def flip() -> None:
            try:
                for i in range(150):
                    self.backends.set_backend("ollama" if i % 2 else "nvidia")
                    self.backends.credentials("nvidia")
                    self.backends.resolve()
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=flip) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])
        state = self.backends.resolve()
        self.assertIn(state["forced"], ("nvidia", "ollama"))

    def test_a_key_typed_in_the_browser_is_never_lost_under_contention(self) -> None:
        self.backends.ollama_models = lambda refresh=False: (False, [])
        self.backends.set_session_key("secret-key-value")
        seen: list[str] = []
        stop = threading.Event()

        def read() -> None:
            while not stop.is_set():
                key, _url = self.backends.credentials("nvidia")
                if key:
                    seen.append(key)

        readers = [threading.Thread(target=read) for _ in range(4)]
        for t in readers:
            t.start()
        try:
            time.sleep(0.3)
        finally:
            stop.set()
            for t in readers:
                t.join(5)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"secret-key-value"})


def inspect_source(func) -> str:
    import inspect

    return inspect.getsource(func)


class PoolRespectsItsCeiling(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.kg = _temp_graph()
        self.addCleanup(self.kg.close)
        self.addCleanup(self.tmp.cleanup)

    def test_concurrent_queries_never_exceed_the_configured_size(self) -> None:
        """The whole point of the ceiling: N threads must not become N handles."""
        peak = 0
        samples: list[int] = []
        stop = threading.Event()

        def sample() -> None:
            nonlocal peak
            while not stop.is_set():
                in_use = self.kg.pool.in_use
                samples.append(in_use)
                peak = max(peak, in_use)

        watcher = threading.Thread(target=sample, daemon=True)
        watcher.start()
        try:
            def query() -> None:
                for _ in range(15):
                    self.kg.execute("MATCH (n:Company) RETURN count(n)")

            threads = [threading.Thread(target=query) for _ in range(24)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        finally:
            stop.set()
            watcher.join(5)
        self.assertLessEqual(peak, self.kg.pool.max_size)
        self.assertLessEqual(self.kg.pool.in_use, self.kg.pool.max_size)

    def test_reads_actually_overlap_rather_than_queueing(self) -> None:
        """Proved by slot arithmetic, not by timing, so it cannot flake.

        Holding all but one slot and still completing a query is only possible
        if a second query runs at the same time as the held ones. Holding every
        slot must then block, which is what readiness keys off.
        """
        self.assertGreaterEqual(
            self.kg.pool.max_size, 2,
            "a pool of one is the serialisation this replaced",
        )
        everything_but_one = [
            self.kg.pool.acquire(timeout=5)
            for _ in range(self.kg.pool.max_size - 1)
        ]
        try:
            started = time.perf_counter()
            result = self.kg.execute("MATCH (n:Company) RETURN count(n)")
            self.assertLess(
                time.perf_counter() - started, 2.0,
                "a query blocked with a slot still free; reads are serialised",
            )
            self.assertTrue(result)
        finally:
            for connection in everything_but_one:
                self.kg.pool.release(connection)

    def test_a_fully_held_pool_blocks_a_new_query(self) -> None:
        """The complement: with no slot free, a query must wait rather than
        open an extra connection behind the ceiling's back."""
        held = [self.kg.pool.acquire(timeout=5) for _ in range(self.kg.pool.max_size)]
        try:
            done = threading.Event()

            def query() -> None:
                try:
                    self.kg.execute("MATCH (n:Company) RETURN count(n)")
                finally:
                    done.set()

            worker = threading.Thread(target=query, daemon=True)
            worker.start()
            self.assertFalse(
                done.wait(0.5),
                "a query completed with every slot held; the ceiling leaked",
            )
        finally:
            for connection in held:
                self.kg.pool.release(connection)
        self.assertTrue(done.wait(10), "query did not resume once slots freed")

    def test_connections_are_created_lazily_and_reused(self) -> None:
        """A server that only ever answers one question at a time must not pay
        for handles it never uses."""
        with self.kg.pool.slot():
            pass
        made_after_one = self.kg.pool._made
        self.assertLessEqual(made_after_one, 1)
        for _ in range(5):
            with self.kg.pool.slot():
                pass
        self.assertEqual(self.kg.pool._made, made_after_one,
                         "sequential queries should reuse one connection")

    def test_results_are_correct_when_reads_overlap(self) -> None:
        """Overlap is only safe if nothing is torn. Same question, many threads,
        every answer identical."""
        expected = self.kg.execute("MATCH (n:Company) RETURN count(n)")
        answers: list[list] = []
        guard = threading.Lock()

        def query() -> None:
            got = self.kg.execute("MATCH (n:Company) RETURN count(n)")
            with guard:
                answers.append(got)

        threads = [threading.Thread(target=query) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(answers), 32)
        self.assertTrue(all(a == expected for a in answers))


class PoolFailureAndLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.kg = _temp_graph()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.kg.close)

    def test_exhausted_pool_reports_none_rather_than_blocking_forever(self) -> None:
        held = [self.kg.pool.acquire(timeout=5) for _ in range(self.kg.pool.max_size)]
        try:
            self.assertTrue(all(c is not None for c in held))
            started = time.perf_counter()
            self.assertIsNone(self.kg.pool.acquire(timeout=0.05))
            self.assertLess(time.perf_counter() - started, 2.0)
        finally:
            for connection in held:
                self.kg.pool.release(connection)

    def test_a_released_slot_is_reusable(self) -> None:
        connection = self.kg.pool.acquire(timeout=5)
        self.assertIsNotNone(connection)
        self.kg.pool.release(connection)
        again = self.kg.pool.acquire(timeout=5)
        self.assertIsNotNone(again)
        self.kg.pool.release(again)

    def test_a_failed_query_returns_its_slot_instead_of_leaking_it(self) -> None:
        """A broken connection must not permanently shrink the pool, and must
        not be handed to the next caller either."""
        before = self.kg.pool._made
        with self.assertRaises(Exception):
            with self.kg.pool.slot() as connection:
                connection.execute("MATCH (n:Company) RETURN count(n")
        self.assertEqual(
            self.kg.pool._made, before - 1,
            "a failed connection should be discarded, shrinking the pool by one",
        )
        # The pool can still serve, and can grow again on demand.
        with self.kg.pool.slot() as connection:
            self.assertIsNotNone(connection)
        self.assertEqual(self.kg.pool.in_use, 0)

    def test_using_a_closed_pool_raises_rather_than_hanging(self) -> None:
        self.kg.pool.close()
        with self.assertRaises(RuntimeError):
            self.kg.pool.acquire(timeout=0.05)

    def test_close_is_safe_to_call_twice(self) -> None:
        self.kg.pool.close()
        self.kg.pool.close()

    def test_readiness_reports_busy_only_when_the_pool_is_exhausted(self) -> None:
        """The semantics changed deliberately: one held slot out of several is
        ordinary work, and readiness must not flap because of it."""
        one = self.kg.pool.acquire(timeout=5)
        try:
            readable, detail, _ms = self.kg.probe_read(timeout=1.0)
            self.assertTrue(readable, f"one busy slot made readiness flap: {detail}")
        finally:
            self.kg.pool.release(one)

        everything = [self.kg.pool.acquire(timeout=5)
                      for _ in range(self.kg.pool.max_size)]
        try:
            self.assertTrue(all(c is not None for c in everything))
            readable, detail, _ms = self.kg.probe_read(timeout=0.05)
            self.assertFalse(readable)
            self.assertEqual(detail, "busy")
        finally:
            for connection in everything:
                self.kg.pool.release(connection)

    def test_probe_read_never_raises_after_close(self) -> None:
        self.kg.close()
        readable, detail, waited = self.kg.probe_read(timeout=0.05)
        self.assertFalse(readable)
        self.assertTrue(detail)
        self.assertGreaterEqual(waited, 0.0)


class ReadWriteHandlesStaySerialised(unittest.TestCase):
    """One open write transaction at a time, so a writable handle gets one slot
    no matter what GRAPH_QUERY_SLOTS says."""

    def test_a_writable_handle_gets_a_single_slot(self) -> None:
        tmp, kg = _temp_graph(read_only=False)
        try:
            self.assertEqual(kg.pool.max_size, 1)
        finally:
            kg.close()
            tmp.cleanup()

    def test_a_read_only_handle_gets_the_configured_slots(self) -> None:
        tmp, kg = _temp_graph(read_only=True)
        try:
            self.assertEqual(kg.pool.max_size, query_ui.GRAPH_QUERY_SLOTS)
            self.assertGreaterEqual(kg.pool.max_size, 2)
        finally:
            kg.close()
            tmp.cleanup()


class PoolSizeIsSane(unittest.TestCase):
    def test_zero_or_negative_slots_still_yields_one_connection(self) -> None:
        tmp, kg = _temp_graph()
        try:
            for bad in (0, -3):
                pool = _ConnectionPool(kg.db, bad)
                self.assertEqual(pool.max_size, 1)
        finally:
            kg.close()
            tmp.cleanup()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()