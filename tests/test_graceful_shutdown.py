"""Graceful shutdown: the server stops on a signal without cutting live work short.

What is being pinned, and why each case earns its place:

* **Idle shutdown** -- the common case must be quick and must leave nothing behind.
* **Active request** -- a request already running is *finished*, not refused. The
  ordering that makes this true is the whole point: ``kg.close()`` must not run
  while a query is still using the handle. The daemon-thread race in
  :mod:`sandbox_engine.shutdown` makes this the test most likely to catch a
  regression that no signature check would notice.
* **Active SSE** -- a streaming answer holds its thread for as long as the model
  thinks, which is the longest-lived request the server has.
* **Active ingestion** -- a background task must not be started after the signal,
  and a queued one must be dropped rather than silently held open.
* **Shutdown during a database operation** -- the handle is closed last.
* **Repeated SIGTERM** -- the ordinary double-signal path must not double-close a
  socket or a LadybugDB handle.
* **Timeout** -- the deadline is real: it is waited on, then the drain finishes
  anyway. A drain that only ever succeeds on a quiet server is not a drain.

Real ``SIGTERM``/``SIGINT`` delivery is exercised in a subprocess, because
``signal.signal`` only installs from the main thread and these tests run under a
test runner that is not it. The rest drive the coordinator directly, which is the
same entry point the signal handler calls.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ladybug as lb

from sandbox_engine import ddl, query_ui
from sandbox_engine.background import BackgroundIngestQueue
from sandbox_engine.shutdown import (
    DatabaseSealed,
    DRAIN_EXEMPT_PATHS,
    ShutdownCoordinator,
    ShutdownStarted,
    is_mutation,
    serve_until_signalled,
)

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout: float = 10.0) -> tuple[int, dict]:
    """GET *url*, returning (status, parsed-or-raw body). Never raises on 503."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read())


def _maybe_json(raw: bytes):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _temp_db(directory: Path) -> Path:
    """A real schema'd LadybugDB to serve, so handle behaviour is genuine."""
    path = directory / "shutdown.lbug"
    db = lb.Database(str(path))
    conn = lb.Connection(db)
    try:
        ddl.ensure_schema(conn)
    finally:
        conn.close()
        db.close()
    return path


class _RecordingGraph:
    """Stands in for KnowledgeGraph, recording the order closures happen in."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.sealed = False

    def begin_shutdown(self) -> None:
        self.events.append("seal")
        self.sealed = True

    def close(self) -> None:
        self.events.append("close")


class _RecordingQueue:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.closed = False

    def begin_shutdown(self) -> None:
        self.events.append("close_door")

    def shutdown(self) -> None:
        self.events.append("stop_workers")
        self.closed = True


class _SlowHandler(query_ui._Handler):
    """The real handler with three routes that let a test hold a request open.

    Subclasses rather than mocks so the accounting, the 503 refusal and the
    Content-Length handling under test are the production ones.
    """

    def _get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)  # noqa: F821 - see below
        path = parsed.path
        if path == "/slow":
            time.sleep(float(urllib.parse.parse_qs(parsed.query).get("s", ["0.5"])[0]))
            return self._json({"slept": True})
        if path == "/counted":
            return self._json({"ok": True})
        if path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            frames = int(urllib.parse.parse_qs(parsed.query).get("frames", ["5"])[0])
            gap = float(urllib.parse.parse_qs(parsed.query).get("gap", ["0.15"])[0])
            try:
                for i in range(frames):
                    self.wfile.write(f"event: token\ndata: {i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(gap)
                self.wfile.write(b"event: done\ndata: {}\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # The drain closed the stream; that is a legitimate outcome.
                pass
            return
        if path == "/echo-query":
            # Runs a real read through the graph handle while draining.
            companies = self.kg.execute(
                "MATCH (n:Company) RETURN count(n)")[0][0]
            hook = getattr(self, "queried_hook", None)
            if hook is not None:
                hook.set()
            return self._json({"companies": companies})
        return super()._get()


import urllib.parse  # noqa: E402 - imported after the class body uses it above


# ── is_mutation ─────────────────────────────────────────────────────────────

class MutationDetectionTests(unittest.TestCase):
    """The write seal is only as good as this predicate."""

    def test_reads_are_not_writes(self):
        for cypher in (
            "MATCH (n:Company) RETURN count(n)",
            "MATCH (a)-[:HAS_CHUNK]->(b) RETURN a, b",
            "MATCH (n:Company {ticker: $t}) RETURN n.ticker LIMIT 1",
            "MATCH (n) WHERE n.period = 'FY2024' RETURN n",
        ):
            with self.subTest(cypher=cypher):
                self.assertFalse(is_mutation(cypher))

    def test_every_write_shape_is_caught(self):
        for cypher in (
            "CREATE (n:Company)",
            "MERGE (n:Company {ticker: 'AAPL'})",
            "MATCH (n) SET n.x = 1",          # not the leading keyword
            "MATCH (a) WITH a DELETE a",       # second clause
            "UNWIND $rows AS r CREATE (:Company)",
            "MATCH (n) DETACH DELETE n",
            "DROP TABLE Company",
            "COPY Company TO 'out.parquet'",
        ):
            with self.subTest(cypher=cypher):
                self.assertTrue(is_mutation(cypher))

    def test_a_write_word_inside_a_string_is_not_a_write(self):
        # False positives are worse than they look: this runs inside execute(),
        # so a read misread as a write gets refused from a request the drain is
        # supposed to be letting finish.
        for cypher in (
            "MATCH (n) WHERE n.tag = 'SET' RETURN n",
            'MATCH (n) WHERE n.note = "merge this" RETURN n',
            "MATCH (n) WHERE n.x = 'CREATE (a)' RETURN n",
        ):
            with self.subTest(cypher=cypher):
                self.assertFalse(is_mutation(cypher))


# ── coordinator state machine ───────────────────────────────────────────────

class CoordinatorStateTests(unittest.TestCase):
    def setUp(self):
        self.c = ShutdownCoordinator(service="test", timeout=2.0)

    def test_a_fresh_server_admits_everything(self):
        self.assertFalse(self.c.draining)
        self.assertTrue(self.c.enter_request("/api/stats"))

    def test_a_draining_server_refuses_work_but_still_answers_probes(self):
        self.c.begin("test")
        for path in ("/api/stats", "/api/ask", "/", "/app/company/aapl"):
            with self.subTest(path=path):
                with self.assertRaises(ShutdownStarted):
                    self.c.enter_request(path)
        # Probes are how a balancer learns to stop routing here; refusing them
        # would leave it routing here until the socket vanished.
        for path in DRAIN_EXEMPT_PATHS:
            with self.subTest(path=path):
                self.assertTrue(self.c.enter_request(path))
                self.c.exit_request()

    def test_begin_is_idempotent_so_a_second_signal_cannot_double_close(self):
        self.assertTrue(self.c.begin("first"))
        self.assertFalse(self.c.begin("second"))
        self.assertEqual(self.c.report()["shutdown_reason"], "first")

    def test_escalate_is_idempotent_and_stops_the_wait(self):
        # Something must actually be in flight, or there is nothing to wait on.
        self.c.enter_request("/slow")
        self.assertFalse(self.c.wait_for_idle(time.monotonic() + 0.1))
        self.assertTrue(self.c.escalate("second SIGTERM"))
        self.assertFalse(self.c.escalate("third SIGTERM"))
        # The escalated flag is what makes the wait give up, so it must return
        # at once even with work outstanding.
        started = time.monotonic()
        self.assertFalse(self.c.wait_for_idle(time.monotonic() + 30))
        self.assertLess(time.monotonic() - started, 1.0)
        self.c.exit_request()

    def test_a_request_that_raises_still_counts_itself_out(self):
        # The count is the only thing the drain can wait on, so a leaked
        # increment would spend the whole deadline on a request that is gone.
        self.c.enter_request("/boom")
        try:
            raise ValueError("handler blew up")
        except ValueError:
            pass
        finally:
            self.c.exit_request()
        self.assertEqual(self.c.inflight, 0)
        self.assertTrue(self.c.wait_for_idle(time.monotonic() + 1))

    def test_the_count_never_goes_negative(self):
        self.c.exit_request()
        self.c.exit_request()
        self.assertEqual(self.c.inflight, 0)


class SignalRecordingTests(unittest.TestCase):
    def test_the_first_signal_starts_the_drain(self):
        c = ShutdownCoordinator(service="test", timeout=1.0)
        self.assertEqual(c.record_signal(signal.SIGTERM), "began")
        self.assertTrue(c.draining)
        self.assertIn("SIGTERM", c.report()["shutdown_reason"])

    def test_the_second_signal_escalates_and_the_third_is_ignored(self):
        c = ShutdownCoordinator(service="test", timeout=1.0)
        self.assertEqual(c.record_signal(signal.SIGTERM), "began")
        self.assertEqual(c.record_signal(signal.SIGTERM), "escalated")
        self.assertTrue(c.escalated)
        # Escalation happens once. Reporting "escalated" for every later signal
        # would make the log claim a decision that is no longer being taken.
        self.assertEqual(c.record_signal(signal.SIGTERM), "ignored")
        self.assertTrue(c.escalated)

    def test_a_signal_after_the_drain_finished_is_swallowed(self):
        # The window this protects is the handful of instructions between the
        # drain completing and the interpreter exiting. Falling back to SIG_DFL
        # there lets a supervisor's retry kill a process that has already closed
        # its graph and released its ports.
        c = ShutdownCoordinator(service="test", timeout=1.0)
        c.drain(servers=[], graph=None, background=None)
        self.assertTrue(c.wait_finished(1))
        self.assertEqual(c.record_signal(signal.SIGTERM), "ignored")
        self.assertIn(("signal_after_drain", "SIGTERM (signal 15)"), c.steps)

    def test_sigint_and_sigterm_are_treated_alike(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig.name):
                c = ShutdownCoordinator(service="test", timeout=1.0)
                self.assertEqual(c.record_signal(sig), "began")
                self.assertIn(sig.name, c.report()["shutdown_reason"])


# ── the drain sequence ──────────────────────────────────────────────────────

class DrainSequenceTests(unittest.TestCase):
    def setUp(self):
        self.graph = _RecordingGraph()
        self.queue = _RecordingQueue()
        self.servers = []
        self.c = ShutdownCoordinator(service="test", timeout=5.0)

    def tearDown(self):
        for s in self.servers:
            try:
                s.server_close()
            except Exception:
                pass

    def _serve(self) -> None:
        port = _free_port()
        handler = type("_H", (_SlowHandler,), {"kg": self.graph})
        self.servers = query_ui._listeners("127.0.0.1", [port], handler)
        self.servers[0].lifecycle = self.c
        threading.Thread(target=self.servers[0].serve_forever, daemon=True).start()

    def test_idle_shutdown_closes_everything_in_a_safe_order(self):
        self._serve()
        self.c.begin("test")
        summary = self.c.drain(servers=self.servers, graph=self.graph,
                               background=self.queue)

        self.assertFalse(summary["forced"])
        self.assertEqual(summary["dropped_requests"], 0)
        self.assertTrue(summary["graph_closed"])
        self.assertTrue(summary["background_stopped"])
        # The graph is the one irreversible thing, so it must be sealed and
        # closed after the queue is told to stop and after nothing is in flight.
        self.assertEqual(self.graph.events, ["seal", "close"])
        self.assertEqual(self.queue.events, ["close_door", "stop_workers"])
        steps = [s for s, _ in summary["steps"]]
        self.assertLess(steps.index("ingestion_closed"), steps.index("graph_closed"))
        self.assertLess(steps.index("drain_inflight"), steps.index("graph_closed"))

    def test_the_graph_is_closed_only_after_the_last_request_finishes(self):
        # The regression this whole module exists for: daemon_threads=True makes
        # socketserver track nothing, so an unguarded kg.close() lands while a
        # query is still using the handle.
        self._serve()
        port = self.servers[0].server_address[1]
        finished = threading.Event()
        observed: list[str] = []

        def hold() -> None:
            try:
                _get(f"http://127.0.0.1:{port}/slow?s=1.0", timeout=15)
                observed.append("request_done")
            finally:
                finished.set()

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while self.c.inflight == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.c.inflight, 1, "the request never registered")

        self.c.begin("SIGTERM")
        summary = self.c.drain(servers=self.servers, graph=self.graph,
                               background=self.queue)
        worker.join(10)

        self.assertTrue(finished.is_set(), "the in-flight request was cut short")
        self.assertEqual(observed, ["request_done"])
        self.assertFalse(summary["forced"])
        self.assertEqual(self.c.inflight, 0)

    def test_shutdown_during_a_database_operation_waits_for_the_query(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = _temp_db(Path(tmp))
            kg = query_ui.KnowledgeGraph(db, read_only=True)
            self.addCleanup(kg.close)

            holding = threading.Event()
            release = threading.Event()
            queried = threading.Event()

            # Borrow every slot rather than taking a lock: a query now waits for
            # a free connection, so holding the whole pool is what makes the
            # request below genuinely queued instead of merely slow.
            borrowed: list = []

            def hold_pool() -> None:
                borrowed.extend(kg.pool.acquire(timeout=5)
                                for _ in range(kg.pool.max_size))
                holding.set()
                release.wait(10)
                for connection in borrowed:
                    if connection is not None:
                        kg.pool.release(connection)

            holder = threading.Thread(target=hold_pool, daemon=True)
            holder.start()
            try:
                self.assertTrue(holding.wait(5))
                self.assertTrue(all(c is not None for c in borrowed))
                port = _free_port()
                handler = type("_H", (_SlowHandler,),
                               {"kg": kg, "queried_hook": queried})
                servers = query_ui._listeners("127.0.0.1", [port], handler)
                self.servers.extend(servers)
                servers[0].lifecycle = self.c
                threading.Thread(target=servers[0].serve_forever, daemon=True).start()

                result: list = []

                def ask() -> None:
                    try:
                        result.append(_get(f"http://127.0.0.1:{port}/echo-query", timeout=15))
                    except Exception as exc:
                        result.append(exc)

                asker = threading.Thread(target=ask, daemon=True)
                asker.start()
                # Wait until the request is queued behind the lock we hold.
                time.sleep(0.4)
                self.assertEqual(self.c.inflight, 1)

                self.c.begin("SIGTERM")
                drain = threading.Thread(
                    target=lambda: self.c.drain(servers=servers, graph=kg,
                                                background=self.queue),
                    daemon=True)
                drain.start()
                time.sleep(0.3)
                self.assertTrue(drain.is_alive(),
                                "the drain closed the database mid-query")
                release.set()
                holder.join(5)
                asker.join(10)
                drain.join(10)

                self.assertTrue(queried.is_set(),
                                "the queued query never completed during the drain")
                self.assertEqual(len(result), 1)
                self.assertNotIsInstance(result[0], Exception)
                status, body = result[0]
                self.assertEqual(status, 200)
                self.assertIn("companies", body)
            finally:
                release.set()

    def test_the_timeout_is_waited_on_and_then_the_drain_still_finishes(self):
        c = ShutdownCoordinator(service="test", timeout=1.0)
        c.enter_request("/slow")
        c.begin("SIGTERM")
        started = time.monotonic()
        summary = c.drain(servers=[], graph=self.graph, background=self.queue)
        elapsed = time.monotonic() - started

        self.assertTrue(summary["forced"], "the deadline was not honoured")
        self.assertEqual(summary["dropped_requests"], 1)
        self.assertGreaterEqual(elapsed, 1.0, "returned before the deadline")
        self.assertLess(elapsed, 6.0, "overshot the deadline")
        # Even when it gives up waiting, the irreversible step still happens in
        # order: a drain that abandons cleanup is not a bounded drain.
        self.assertTrue(summary["graph_closed"])
        self.assertEqual(self.graph.events, ["seal", "close"])

        c.exit_request()

    def test_escalation_shortens_the_wait_without_skipping_cleanup(self):
        self.c.enter_request("/slow")
        self.c.begin("SIGTERM")
        self.c.escalate("second SIGTERM")
        started = time.monotonic()
        summary = self.c.drain(servers=[], graph=self.graph, background=self.queue)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(summary["forced"])
        self.assertTrue(summary["graph_closed"])
        self.c.exit_request()

    def test_a_failing_step_does_not_abort_the_rest_of_the_drain(self):
        class Exploding(_RecordingGraph):
            def begin_shutdown(self):
                super().begin_shutdown()
                raise RuntimeError("seal failed")

        graph = Exploding()
        summary = self.c.drain(servers=[], graph=graph, background=self.queue)
        # close() still ran even though seal() raised.
        self.assertIn("close", graph.events)
        self.assertTrue(summary["graph_closed"])

    def test_a_stuck_worker_does_not_hold_the_drain_open(self):
        class Stuck(_RecordingQueue):
            def shutdown(self):
                time.sleep(30)

        c = ShutdownCoordinator(service="test", timeout=1.0)
        c.begin("SIGTERM")
        started = time.monotonic()
        summary = c.drain(servers=[], graph=self.graph, background=Stuck())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0, "an uninterruptible worker held the drain")
        self.assertFalse(summary["background_stopped"])


# ── live HTTP behaviour during a drain ──────────────────────────────────────

class DrainHttpTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db = _temp_db(Path(self._tmp.name))
        self.kg = query_ui.KnowledgeGraph(self.db, read_only=True)
        self.addCleanup(self.kg.close)
        self.c = ShutdownCoordinator(service="test", timeout=10.0)
        self.ports: list = []
        self.thread = None

    def tearDown(self):
        if self.thread is not None:
            try:
                self.c.begin("teardown")
                self.c.drain(servers=self.ports, graph=None, background=None, timeout=5)
            except Exception:
                pass
        self._tmp.cleanup()

    def _start(self, handler=None):
        port = _free_port()
        handler = handler or type("_H", (_SlowHandler,), {"kg": self.kg})
        server = query_ui._listeners("127.0.0.1", [port], handler)[0]
        server.lifecycle = self.c
        self.kg.lifecycle = self.c
        self.ports.append(server)
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        return port

    def test_new_requests_are_refused_with_503_while_probes_still_answer(self):
        port = self._start()
        self.assertEqual(_get(f"http://127.0.0.1:{port}/counted")[0], 200)

        self.c.begin("SIGTERM")

        status, body = _get(f"http://127.0.0.1:{port}/counted")
        self.assertEqual(status, 503)
        self.assertIsInstance(body, dict)
        self.assertTrue(body["draining"])
        self.assertIn("error", body)

    def test_health_stays_200_and_readyz_reports_draining(self):
        # The pair a load balancer and a supervisor read. Liveness must not fall
        # while draining, or the supervisor escalates to SIGKILL and destroys the
        # in-flight requests the grace period exists to protect.
        port = self._start()
        self.c.begin("SIGTERM")

        status, body = _get(f"http://127.0.0.1:{port}/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["draining"])
        self.assertTrue(body["inflight_requests"] >= 0)

        status, body = _get(f"http://127.0.0.1:{port}/readyz")
        self.assertEqual(status, 503)
        self.assertFalse(body["ready"])
        self.assertEqual(body["status"], "draining")
        self.assertIn("draining", body["failed_checks"])

    def test_the_503_carries_a_content_length_so_keep_alive_does_not_hang(self):
        # Without Content-Length the client waits on a body that never comes and
        # reports the drain as a network failure rather than as a refusal.
        port = self._start()
        self.c.begin("SIGTERM")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"GET /counted HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.settimeout(10)
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        self.assertIn(b"503", head.split(b"\r\n")[0])
        self.assertIn(b"Content-Length:", head)
        declared = int(head.split(b"Content-Length:")[1].split(b"\r\n")[0])
        self.assertEqual(declared, len(body))
        self.assertTrue(json.loads(body))

    def test_an_sse_answer_in_flight_is_carried_to_completion(self):
        port = self._start()
        frames = []

        def read_stream() -> None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/sse?frames=6&gap=0.2", timeout=20
                ) as resp:
                    for line in resp:
                        if line.startswith(b"data: "):
                            frames.append(line.strip())
            except Exception as exc:
                frames.append(exc)

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        deadline = time.monotonic() + 5
        while self.c.inflight == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.c.inflight, 1, "the SSE request never registered")

        self.c.begin("SIGTERM")
        summary = self.c.drain(servers=self.ports, graph=self.kg,
                               background=None, timeout=15)
        reader.join(10)

        self.assertFalse(summary["forced"], "a 1.4s SSE should not hit the deadline")
        # 6 tokens plus the done event: the answer was not truncated.
        self.assertEqual(len(frames), 7, f"stream was cut: {frames}")

    def test_the_graph_seal_refuses_writes_but_still_allows_reads(self):
        self.c.begin("SIGTERM")
        self.kg.lifecycle = self.c
        self.kg.begin_shutdown()
        # An in-flight request must still be able to finish its reads.
        self.assertEqual(
            self.kg.execute("MATCH (n:Company) RETURN count(n)")[0][0], 0)
        with self.assertRaises(DatabaseSealed):
            self.kg.execute("CREATE (n:Company {ticker: 'X'})")
        with self.assertRaises(DatabaseSealed):
            self.kg.execute("MERGE (n:Company {ticker: 'X'})")
        # The handle is still open and usable; only writes are refused.
        self.assertEqual(
            self.kg.execute("MATCH (n:Company) RETURN count(n)")[0][0], 0)


# ── background ingestion ────────────────────────────────────────────────────

class IngestionDrainTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.queue = BackgroundIngestQueue(
            staging_dir=Path(self.tmp.name) / "staging")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.queue.shutdown, False)

    def test_the_door_closes_before_the_workers_are_stopped(self):
        self.queue.begin_shutdown()
        self.assertFalse(
            self.queue.enqueue_coldstart_sync("AAPL"),
            "a ticker was accepted after the drain began")
        self.assertTrue(self.queue.draining)

    def test_the_shutdown_method_closes_the_door_too(self):
        # One method to reach for, so a caller cannot stop the workers and leave
        # the queue accepting.
        self.queue.shutdown(wait=False)
        self.assertTrue(self.queue.draining)

    def test_begin_shutdown_is_idempotent(self):
        self.queue.begin_shutdown()
        self.queue.begin_shutdown()
        self.assertTrue(self.queue.draining)

    def test_a_worker_running_at_shutdown_time_is_waited_for(self):
        started = threading.Event()
        finished = threading.Event()
        self.queue.executor.submit(self._hold, started, finished)
        self.assertTrue(started.wait(5))
        self.queue.begin_shutdown()
        self.queue.shutdown(wait=True)
        self.assertTrue(finished.is_set(), "a running task was abandoned")

    def test_queued_work_is_cancelled_rather_than_started(self):
        # A staged ticker is re-enqueueable after a restart, so abandoning a
        # queued extraction loses nothing -- and NOT cancelling it would make
        # shutdown(wait=True) block behind a result nobody will read.
        # One worker, so the second task is genuinely queued behind the first
        # rather than running beside it.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        queue = BackgroundIngestQueue(
            max_workers=1, staging_dir=Path(tmp) / "staging")
        self.addCleanup(queue.shutdown, False)

        gate = threading.Event()
        started = threading.Event()

        def block() -> None:
            started.set()
            gate.wait(30)

        queue.executor.submit(block)
        self.assertTrue(started.wait(5), "the blocking task never started")

        ran = threading.Event()
        queued = queue.executor.submit(ran.set)
        self.assertFalse(queued.running())

        queue.begin_shutdown()
        queue.shutdown(wait=True)
        gate.set()
        time.sleep(0.2)
        self.assertFalse(ran.is_set(), "queued work started during the drain")
        self.assertTrue(queued.cancelled())

    @staticmethod
    def _hold(started: threading.Event, finished: threading.Event) -> None:
        started.set()
        time.sleep(0.3)
        finished.set()


# ── real signals, in a real process ─────────────────────────────────────────

_SERVER_SCRIPT = textwrap.dedent(
    """
    import sys, threading, time
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from http.server import ThreadingHTTPServer
    from sandbox_engine import query_ui
    from sandbox_engine.shutdown import (
        ShutdownCoordinator, install_signal_handlers, serve_until_signalled)

    class Slow(query_ui._Handler):
        def _get(self):
            if self.path.startswith("/slow"):
                time.sleep(float(self.path.split("=")[-1]))
                return self._json({{"slept": True}})
            return super()._get()

    port = int(sys.argv[1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Slow)
    server.daemon_threads = True
    coord = ShutdownCoordinator(service="subproc", timeout=float(sys.argv[3]))
    install_signal_handlers(coord)
    serve_until_signalled([server], coord)
    print("DRAINED forced=%s" % coord.report()["escalated"], flush=True)
    """
)


class RealSignalTests(unittest.TestCase):
    """SIGTERM and SIGINT delivered by the kernel to a live server process."""

    def _spawn(self, timeout: str = "20") -> tuple[subprocess.Popen, int]:
        port = _free_port()
        script = _SERVER_SCRIPT.format(repo=str(REPO))
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(port), "unused", timeout],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"server exited early:\n{proc.stdout.read()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return proc, port
            except OSError:
                time.sleep(0.1)
        proc.kill()
        self.fail("server never bound its port")

    def _request_in_background(self, port: int, seconds: float) -> threading.Thread:
        def go() -> None:
            try:
                _get(f"http://127.0.0.1:{port}/slow?s={seconds}", timeout=60)
            except Exception:
                pass

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _wait_port_free(port: int, budget: float = 15.0) -> bool:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    time.sleep(0.1)
            except OSError:
                return True
        return False

    def test_sigterm_shuts_down_cleanly_and_releases_the_port(self):
        proc, port = self._spawn()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("DRAINED", out)
        self.assertTrue(self._wait_port_free(port), "the port stayed bound")

    def test_sigint_shuts_down_cleanly(self):
        proc, port = self._spawn()
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("DRAINED", out)

    def test_an_active_request_completes_across_a_sigterm(self):
        proc, port = self._spawn()
        worker = self._request_in_background(port, 1.5)
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=40)
        worker.join(15)
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("DRAINED", out)

    def test_repeated_sigterm_does_not_corrupt_or_hang(self):
        proc, port = self._spawn()
        self._request_in_background(port, 2.0)
        time.sleep(0.4)
        for _ in range(5):
            proc.send_signal(signal.SIGTERM)
            time.sleep(0.05)
        out, _ = proc.communicate(timeout=40)
        # Exit status may be 0 or -SIGTERM, and the reason is CPython rather
        # than this code: the interpreter restores the default disposition while
        # finalising, so a signal arriving after the work is done kills the
        # process no matter what handler was installed. Reproducible with no
        # FinGraph code at all. What must hold is that the drain completed and
        # nothing was left behind -- so those are what gets asserted.
        self.assertIn(proc.returncode, (0, -signal.SIGTERM), out)
        self.assertIn("DRAINED", out)          # printed only after drain() returned
        self.assertNotIn("Traceback", out)
        self.assertTrue(self._wait_port_free(port))

    def test_the_deadline_is_honoured_against_a_hung_request(self):
        proc, port = self._spawn(timeout="2")
        self._request_in_background(port, 30)
        time.sleep(0.5)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=40)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, out)
        self.assertLess(elapsed, 25, f"drain overran its deadline: {elapsed:.1f}s")
        self.assertGreaterEqual(elapsed, 1.5, "gave up before the deadline")
        self.assertIn("DRAINED", out)


if __name__ == "__main__":
    unittest.main()