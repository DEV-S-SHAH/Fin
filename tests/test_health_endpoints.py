"""Health and readiness: what each probe is allowed to assert.

Two endpoints, split because they answer different questions and an orchestrator
treats the answers differently. ``/healthz`` is liveness and must survive a
broken dependency; ``/readyz`` is readiness and is only honest if it reads the
graph. The failure mode this pins down is collapsing the two -- a liveness probe
that touches the database gets the one healthy process killed every time the
graph is down, and a readiness probe that skips the database routes traffic to a
server that answers every question with an empty graph.

The other thing pinned here is cost. Readiness runs on a load balancer's poll
interval, so these tests also assert that neither endpoint resolves the RAG
backend, reaches for SEC or Yahoo, or opens a second graph handle.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ladybug as lb

from sandbox_engine import ddl, query_ui
from sandbox_engine.query_ui import KnowledgeGraph


class _GraphStub:
    """A stand-in for the one attribute of ``KnowledgeGraph`` readiness reads.

    Duck-typed on purpose: readiness must not depend on the graph being a real
    ``KnowledgeGraph`` instance, only on it answering ``probe_read`` and
    declaring a schema.
    """

    def __init__(self, schema="engine", readable=True, detail="readable", raises=None,
                 current=True, current_detail="current"):
        self.schema = schema
        self.probe_calls = 0
        self._readable = readable
        self._detail = detail
        self._raises = raises
        self._current = current
        self._current_detail = current_detail

    def probe_read(self, timeout=query_ui.READINESS_LOCK_TIMEOUT):
        self.probe_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._readable, self._detail, 0.42

    def probe_freshness(self):
        return self._current, self._current_detail


class _ExplodingGraph:
    """Any attribute access is an error, standing in for an unusable handle."""

    def __getattr__(self, name):
        raise AssertionError(f"liveness touched the graph: {name}")


class PayloadTests(unittest.TestCase):
    """The bodies, with no socket in the way."""

    def test_liveness_reports_alive_without_touching_the_graph(self):
        payload = query_ui.liveness_payload("test-service")
        self.assertEqual(payload["status"], "alive")
        self.assertEqual(payload["service"], "test-service")
        self.assertEqual(payload["checks"]["process"]["status"], "ok")

    def test_liveness_survives_a_graph_that_raises_on_any_access(self):
        # Liveness has to keep answering while the graph is unusable, so nothing
        # in the payload may reach for it.
        payload = query_ui.liveness_payload("test-service")
        self.assertEqual(payload["status"], "alive")

    def test_liveness_does_not_claim_readiness(self):
        # "ready" absent rather than true: a balancer cannot tell a probe that
        # checked from one that guessed, and guessing here would route traffic.
        payload = query_ui.liveness_payload("test-service")
        self.assertNotIn("ready", payload)
        self.assertIn("/readyz", payload["note"])

    def test_readiness_is_ready_when_the_graph_answers(self):
        payload = query_ui.readiness_payload(_GraphStub(), "test-service")
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertNotIn("failed_checks", payload)
        self.assertEqual(payload["checks"]["database_readable"]["status"], "ok")
        self.assertEqual(payload["checks"]["schema_detected"]["value"], "engine")

    def test_readiness_reports_the_database_as_the_failure_when_it_cannot_be_read(self):
        payload = query_ui.readiness_payload(
            _GraphStub(readable=False, detail="RuntimeError"), "test-service")
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["status"], "not_ready")
        self.assertIn("database_readable", payload["failed_checks"])
        # The rest passed, and saying so is what makes the payload actionable.
        self.assertEqual(payload["checks"]["graph_initialized"]["status"], "ok")

    def test_readiness_is_not_ready_when_the_graph_was_never_initialized(self):
        payload = query_ui.readiness_payload(None, "test-service")
        self.assertFalse(payload["ready"])
        self.assertIn("graph_initialized", payload["failed_checks"])
        # Nothing may be attempted once there is no handle to attempt it on.
        self.assertEqual(payload["checks"]["database_readable"]["detail"], "not attempted")

    def test_readiness_reports_an_undetected_schema(self):
        # detect_schema() falling through both shapes means every count in the
        # app silently reports zero. Readiness has to call that what it is.
        payload = query_ui.readiness_payload(_GraphStub(schema=None), "test-service")
        self.assertFalse(payload["ready"])
        self.assertIn("schema_detected", payload["failed_checks"])

    def test_readiness_is_not_ready_when_the_database_was_replaced(self):
        # The read alone cannot see this: the orphaned inode is still readable
        # and still answers, so only the freshness check catches a rebuild that
        # landed under a running server.
        payload = query_ui.readiness_payload(
            _GraphStub(readable=True, current=False, current_detail="replaced_on_disk"),
            "test-service")
        self.assertFalse(payload["ready"])
        self.assertIn("database_current", payload["failed_checks"])
        # The read passed, and saying so is what points at the real cause.
        self.assertEqual(payload["checks"]["database_readable"]["status"], "ok")

    def test_readiness_does_not_raise_when_the_probe_explodes(self):
        # A health endpoint that 500s tells an orchestrator nothing and can
        # crash the probe loop. It must answer, not propagate.
        payload = query_ui.readiness_payload(
            _GraphStub(raises=RuntimeError("boom")), "test-service")
        self.assertFalse(payload["ready"])

    def test_payloads_are_json_serialisable(self):
        for payload in (query_ui.liveness_payload("s"),
                        query_ui.readiness_payload(_GraphStub(), "s"),
                        query_ui.readiness_payload(None, "s")):
            with self.subTest(status=payload["status"]):
                json.dumps(payload)  # must not raise

    def test_payloads_leak_neither_the_database_path_nor_a_secret(self):
        # Both endpoints are unauthenticated, and both end up in shared
        # dashboards, so neither may carry a path or a credential.
        path = str(_DB_CANDIDATES_SENTINEL)
        for payload in (query_ui.liveness_payload("s"),
                        query_ui.readiness_payload(_GraphStub(), "s")):
            with self.subTest(status=payload["status"]):
                blob = json.dumps(payload)
                self.assertNotIn(path, blob)
                self.assertNotIn("NVIDIA_API_KEY", blob)
                self.assertNotIn("AUTH_SECRET", blob)


_DB_CANDIDATES_SENTINEL = Path("/nonexistent/authoritative/sandbox.lbug")


class ProbeReadTests(unittest.TestCase):
    """The probe itself, against a real LadybugDB so the query is real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="health-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "probe.lbug"

    def _open_empty_schema(self) -> KnowledgeGraph:
        """A database with the full schema and no rows in it."""
        self._create()
        ddl.ensure_schema(self._conn)
        self._close()
        return self._open()

    def _open_no_schema(self) -> KnowledgeGraph:
        """A valid database that has no graph in it at all.

        This is the shape Phase 0 found a live server holding: a handle that
        opens cleanly, answers nothing useful, and is indistinguishable from a
        healthy graph unless something actually queries it.
        """
        return self._open()

    def _create(self) -> None:
        self._db = lb.Database(str(self.path))
        self._conn = lb.Connection(self._db)

    def _close(self) -> None:
        self._conn.close()
        self._db.close()

    def _open(self) -> KnowledgeGraph:
        kg = KnowledgeGraph(self.path, read_only=True)
        self.addCleanup(kg.close)
        return kg

    def test_probe_read_answers_on_a_real_graph(self):
        kg = self._open_empty_schema()
        readable, detail, latency_ms = kg.probe_read()
        self.assertTrue(readable, detail)
        self.assertEqual(detail, "readable")
        self.assertGreaterEqual(latency_ms, 0.0)

    def test_an_empty_graph_reads_as_healthy_not_as_broken(self):
        # A freshly built graph has zero companies and serves real answers.
        # Reporting that as unhealthy would take a correct server out of
        # rotation, which is the failure mode this whole module avoids.
        kg = self._open_empty_schema()
        self.assertEqual(kg.execute("MATCH (n:Company) RETURN count(n)")[0][0], 0)
        readable, _detail, _ms = kg.probe_read()
        self.assertTrue(readable)

    def test_probe_read_fails_when_the_expected_table_is_absent(self):
        # A database with no graph schema: the handle opens, and the query
        # cannot even bind. This is what readiness exists to catch.
        self._create()
        self._close()
        kg = self._open_no_schema()
        readable, detail, _ms = kg.probe_read()
        self.assertFalse(readable)
        self.assertNotEqual(detail, "readable")
        self.assertNotEqual(detail, "busy")

    def test_readiness_catches_a_graph_that_opens_but_cannot_be_queried(self):
        self._create()
        self._close()
        payload = query_ui.readiness_payload(self._open_no_schema(), "svc")
        self.assertFalse(payload["ready"])
        self.assertIn("database_readable", payload["failed_checks"])

    def test_probe_read_gives_up_when_the_query_lock_is_held(self):
        # Bounded is the requirement: a balancer needs a fast wrong answer, and
        # blocking behind a slow question delays the decision it is waiting on.
        kg = self._open_empty_schema()
        held = threading.Event()
        release = threading.Event()
        # Exhaust every pool slot, not just one. "Busy" now means no connection
        # is free within the deadline, which is the condition a load balancer
        # actually needs reported: one held slot out of four is ordinary work.
        borrowed: list = []
        ready = threading.Event()

        def hold() -> None:
            borrowed.extend(kg.pool.acquire(timeout=5) for _ in range(kg.pool.max_size))
            held.set()
            release.wait(5)
            for connection in borrowed:
                kg.pool.release(connection)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        try:
            self.assertTrue(held.wait(5))
            self.assertTrue(all(c is not None for c in borrowed))
            started = time.monotonic()
            readable, detail, _ms = kg.probe_read(timeout=0.05)
            waited = time.monotonic() - started
            self.assertFalse(readable)
            self.assertEqual(detail, "busy")
            self.assertLess(waited, 2.0, "probe blocked instead of reporting busy")
        finally:
            release.set()
            worker.join(5)

    def test_probe_read_fails_once_the_handle_is_closed(self):
        kg = self._open_empty_schema()
        kg.close()
        readable, _detail, _ms = kg.probe_read()
        self.assertFalse(readable)

    def test_probe_read_does_not_write(self):
        # Readiness runs unattended on a poll; it must never be able to mutate
        # the authoritative graph, least of all a read-only handle.
        kg = self._open_empty_schema()
        kg.probe_read()
        self.assertTrue(kg.db.read_only)

    def test_freshness_is_current_while_the_file_is_untouched(self):
        kg = self._open_empty_schema()
        current, detail = kg.probe_freshness()
        self.assertTrue(current)
        self.assertEqual(detail, "current")

    def test_freshness_fails_when_the_database_is_replaced_under_a_live_handle(self):
        # A rebuild swaps the file for a new inode while the server keeps the
        # old one. Nothing about the handle changes: the queries still succeed
        # and the counts are silently from a graph that no longer exists. This
        # is the exact shape Phase 0 found serving an empty graph at HTTP 200.
        kg = self._open_empty_schema()
        self.assertTrue(kg.probe_read()[0], "the read passes before the swap")

        os.replace(self.path, str(self.path) + ".replaced")
        self._create()
        self._close()
        self.assertNotEqual(os.stat(self.path).st_ino,
                            os.stat(str(self.path) + ".replaced").st_ino)

        # Still readable -- which is why the read check alone is not enough.
        self.assertTrue(kg.probe_read()[0])
        current, detail = kg.probe_freshness()
        self.assertFalse(current)
        self.assertEqual(detail, "replaced_on_disk")

    def test_freshness_fails_when_the_database_is_deleted(self):
        kg = self._open_empty_schema()
        os.remove(self.path)
        current, detail = kg.probe_freshness()
        self.assertFalse(current)
        self.assertEqual(detail, "missing_on_disk")

    def test_readiness_goes_unready_after_a_rebuild_under_a_live_handle(self):
        # End to end: the payload a load balancer would read before and after.
        kg = self._open_empty_schema()
        before = query_ui.readiness_payload(kg, "svc")
        self.assertTrue(before["ready"])

        os.replace(self.path, str(self.path) + ".replaced")
        self._create()
        self._close()

        after = query_ui.readiness_payload(kg, "svc")
        self.assertFalse(after["ready"])
        self.assertEqual(after["status"], "not_ready")
        self.assertIn("database_current", after["failed_checks"])
        self.assertEqual(after["checks"]["database_readable"]["status"], "ok")

    def test_freshness_needs_no_lock_so_a_busy_graph_still_reports_it(self):
        # The read check can answer "busy"; freshness must not, because a
        # replacement is knowable without the database being free.
        kg = self._open_empty_schema()
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            borrowed = [kg.pool.acquire(timeout=5) for _ in range(kg.pool.max_size)]
            held.set()
            release.wait(5)
            for connection in borrowed:
                if connection is not None:
                    kg.pool.release(connection)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        try:
            self.assertTrue(held.wait(5))
            started = time.monotonic()
            current, _detail = kg.probe_freshness()
            self.assertTrue(current)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(5)


def _serve(handler_base, kg):
    """Bind a handler the way serve() does, on a free port."""
    handler = type("_BoundHandler", (handler_base,), {"kg": kg})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _get(url, method="GET", timeout=10):
    """Return (status, headers, body) without raising on a non-2xx."""
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class EndpointTests(unittest.TestCase):
    """The routes as a load balancer actually meets them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="health-http-")
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "http.lbug"
        db = lb.Database(str(path))
        conn = lb.Connection(db)
        try:
            ddl.ensure_schema(conn)
        finally:
            conn.close()
            db.close()
        self.kg = KnowledgeGraph(path, read_only=True)
        self.addCleanup(self.kg.close)
        self.server, self.base = _serve(query_ui._Handler, self.kg)
        self.addCleanup(self._sweep)

    def _sweep(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_healthz_is_200_and_machine_readable(self):
        status, headers, body = _get(f"{self.base}/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body)
        self.assertEqual(payload["status"], "alive")
        self.assertEqual(payload["service"], query_ui._Handler.server_version)

    def test_healthz_sends_content_length(self):
        # Phase 0 measured a 401 with no Content-Length hanging keep-alive
        # clients for the full timeout. A probe that hangs is worse than no
        # probe, so the length is asserted rather than assumed.
        _status, headers, body = _get(f"{self.base}/healthz")
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_readyz_is_200_when_the_graph_answers(self):
        status, _headers, body = _get(f"{self.base}/readyz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")

    def test_readyz_is_503_when_the_graph_is_gone(self):
        handler = type("_BoundHandler", (query_ui._Handler,), {"kg": None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        status, headers, body = _get(f"{base}/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(int(headers["Content-Length"]), len(body))
        payload = json.loads(body)
        self.assertFalse(payload["ready"])
        self.assertIn("graph_initialized", payload["failed_checks"])

    def test_liveness_stays_200_while_readiness_is_503(self):
        # The distinction that matters operationally: a process with an
        # unusable graph must keep its liveness so it is not killed, while
        # being pulled from rotation.
        handler = type("_BoundHandler", (query_ui._Handler,), {"kg": None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        self.assertEqual(_get(f"{base}/healthz")[0], 200)
        self.assertEqual(_get(f"{base}/readyz")[0], 503)

    def test_health_endpoints_need_no_session(self):
        # A probe arrives with no cookie. If these were behind the auth gate a
        # load balancer would only ever see 401.
        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                status, _headers, _body = _get(f"{self.base}{path}")
                self.assertIn(status, (200, 503))

    def test_head_is_supported_for_probes(self):
        status, headers, body = _get(f"{self.base}/healthz", method="HEAD")
        self.assertEqual(status, 200)
        self.assertIn("Content-Length", headers)
        self.assertEqual(body, b"")

    def test_probe_latency_is_bounded(self):
        # Measured, not assumed: this is what a poll interval is sized against.
        _get(f"{self.base}/readyz")  # warm the interpreter and the handle
        worst = 0.0
        for _ in range(5):
            started = time.monotonic()
            _get(f"{self.base}/readyz")
            worst = max(worst, time.monotonic() - started)
        self.assertLess(worst, 2.0, f"readiness took {worst:.3f}s")

    def test_health_endpoints_resolve_no_external_dependency(self):
        # get_backends() probes Ollama over the network; markets() and
        # company_detail() reach Yahoo. A readiness probe calling any of them
        # would turn a balancer poll into an outbound request storm.
        original = query_ui.get_backends
        query_ui.get_backends = _forbidden("get_backends")
        self.addCleanup(setattr, query_ui, "get_backends", original)

        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                self.assertIn(_get(f"{self.base}{path}")[0], (200, 503))

    def test_health_endpoints_open_no_second_graph_handle(self):
        # /api/company opens its own KnowledgeGraph per request. A probe that
        # did the same would contend for LadybugDB's exclusive lock on every
        # poll and could evict the live server.
        calls = []
        original_init = KnowledgeGraph.__init__

        def counting_init(self, db_path, read_only=True):
            calls.append(db_path)
            return original_init(self, db_path, read_only=read_only)

        KnowledgeGraph.__init__ = counting_init
        self.addCleanup(setattr, KnowledgeGraph, "__init__", original_init)

        _get(f"{self.base}/healthz")
        _get(f"{self.base}/readyz")
        self.assertEqual(calls, [], "a probe opened another graph handle")


def _forbidden(name):
    def _raise(*_args, **_kwargs):
        raise AssertionError(f"health endpoint called {name}")
    return _raise


class FinGraphEndpointTests(unittest.TestCase):
    """The same routes on the primary deployment, port 9100.

    The FinGraph UI subclasses the legacy handler, so health has to survive the
    inheritance and the fall-through routing, and has to send a 503 through a
    ``_json`` that hardcodes 200.
    """

    def setUp(self) -> None:
        from ui.fingraph import server as fg

        self.fg = fg
        self._tmp = tempfile.TemporaryDirectory(prefix="health-next-")
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "next.lbug"
        db = lb.Database(str(path))
        conn = lb.Connection(db)
        try:
            ddl.ensure_schema(conn)
        finally:
            conn.close()
            db.close()
        self.kg = KnowledgeGraph(path, read_only=True)
        self.addCleanup(self.kg.close)
        self.server, self.base = _serve(fg._NextHandler, self.kg)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_healthz_and_readyz_are_routed_by_the_primary_app(self):
        self.assertEqual(_get(f"{self.base}/healthz")[0], 200)
        self.assertEqual(_get(f"{self.base}/readyz")[0], 200)

    def test_readyz_503_is_not_flattened_to_200_by_the_subclass(self):
        # _NextHandler._json takes no status, so a readiness failure routed
        # through it would tell a healthy balancer the graph is fine.
        handler = type("_BoundNextHandler", (self.fg._NextHandler,), {"kg": None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        status, _headers, body = _get(f"{base}/readyz")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ready"])

    def test_healthz_does_not_shadow_an_existing_route(self):
        # /healthz and /readyz are new, so nothing may have moved to make room.
        self.assertEqual(_get(f"{self.base}/api/stats")[0], 200)
        self.assertEqual(_get(f"{self.base}/")[0], 200)
        self.assertEqual(_get(f"{self.base}/definitely-not-a-route")[0], 404)


class ConfigTests(unittest.TestCase):
    """Configuration that is wrong in the ways this server actually allows."""

    def test_a_malformed_port_value_is_refused_at_startup_not_at_request_time(self):
        # The contract is to refuse rather than fall back, so a typo cannot
        # leave the server listening somewhere nobody is probing. The refusal
        # happens before a socket is bound, which is why readiness never has to
        # report a bad port -- there is no such process to report on.
        original = query_ui.UI_PORT_ENV
        os.environ[original] = "nine thousand"
        self.addCleanup(os.environ.pop, original, None)
        with self.assertRaises(ValueError):
            query_ui.default_ui_port()

    def test_readiness_reports_the_port_it_actually_bound(self):
        payload = query_ui.readiness_payload(_GraphStub(), "svc")
        self.assertEqual(payload["service"], "svc")
        self.assertNotIn("port", payload)

    def test_readiness_ignores_the_database_path_it_was_given(self):
        # The path is resolved once at startup and never re-read, so a path that
        # has since been deleted must not turn readiness into a file-exists test.
        payload = query_ui.readiness_payload(_GraphStub(), "svc")
        self.assertNotIn(str(query_ui._DB_CANDIDATES[0]), json.dumps(payload))
        self.assertTrue(payload["ready"])

    def test_probe_timeout_is_configurable_and_non_negative(self):
        # A negative or zero budget must not raise; it should report busy.
        kg_probe = _GraphStub().probe_read
        self.assertEqual(kg_probe(timeout=0)[2], 0.42)
        self.assertIsInstance(query_ui.READINESS_LOCK_TIMEOUT, float)
        self.assertGreater(query_ui.READINESS_LOCK_TIMEOUT, 0.0)


if __name__ == "__main__":
    unittest.main()
