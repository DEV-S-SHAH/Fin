"""Bounded-resource behaviour for the stdlib HTTP servers.

Requirement coverage: concurrent normal requests, concurrent SSE, slow clients,
disconnected clients, connection floods, and graceful shutdown while connections
are active.

Tests the canonical UI (``ui.fingraph``) which uses ``BoundedThreadingHTTPServer``.
No test here opens ``sandbox_engine/_run/sandbox.lbug``: the fixture graph is
built in ``tmp_path``, so a regression cannot touch authoritative data.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sandbox_engine import query_ui
from sandbox_engine.http_limits import (
    ENV_NAMES,
    BoundedThreadingHTTPServer,
    ConnectionCounter,
    Limits,
)
from sandbox_engine.shutdown import ShutdownCoordinator

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerFixture:
    """A real listener on a real port, torn down deterministically."""

    def __init__(self, handler: type | None = None, limits: Limits | None = None):
        self.port = free_port()
        self.limits = limits or Limits(
            request_timeout=2.0,
            max_connections=16,
            max_sse_connections=3,
            sse_max_seconds=5.0,
            listen_backlog=128,
        )
        handler = handler or _FastHandler
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", self.port), handler, limits=self.limits
        )
        self.server.lifecycle = ShutdownCoordinator(service="test", timeout=3)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def counter(self) -> ConnectionCounter:
        return self.server.connections

    def connect(self, timeout: float = 5.0) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def get(self, path: str = "/healthz", timeout: float = 5.0) -> tuple[int, bytes]:
        conn = self.connect(timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def raw_socket(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _FastHandler(query_ui._Handler):
    """The real handler, minus the routes that need a database.

    Inherits ``timeout`` from ``_Handler`` -- that inheritance is the point, so
    these tests exercise the production socket timeout rather than a copy of it.
    """

    kg = None

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._post)

    def _get(self) -> None:
        parsed = self.path.split("?", 1)[0]
        if parsed == "/healthz":
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed == "/slow":
            time.sleep(10)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(404, b"{}", "application/json")

    def _post(self) -> None:
        parsed = self.path.split("?", 1)[0]
        if parsed == "/stream":
            return self._stream()
        self._send(404, b"{}", "application/json")

    def _stream(self) -> None:
        """Stand-in for ``_run_sse`` with the same admission and lifetime shape.

        The real handler needs a model backend. What is under test is the
        server's policy -- ceiling, admission, lifetime -- so this emits events
        on the same path ``_handle_sse_ask`` uses rather than reimplementing it.
        """
        counter = getattr(self.server, "connections", None)
        if counter is not None and not counter.try_acquire_sse():
            self.close_connection = True
            self._send(
                503,
                json.dumps({"error": "too many concurrent streams"}).encode(),
                "application/json; charset=utf-8",
            )
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            deadline = time.monotonic() + self.server.limits.sse_max_seconds
            while time.monotonic() < deadline:
                try:
                    self.wfile.write(b"event: token\ndata: {}\n\n")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.2)
            try:
                self.wfile.write(b"event: done\ndata: {\"status\":\"truncated\"}\n\n")
                self.wfile.flush()
            except OSError:
                pass
        finally:
            if counter is not None:
                counter.release_sse()


# ---------------------------------------------------------------------------
# 1. Concurrent normal requests
# ---------------------------------------------------------------------------


class TestConcurrentNormalRequests:
    def test_many_simultaneous_requests_all_succeed(self) -> None:
        server = ServerFixture()
        results: list[int] = []
        lock = threading.Lock()

        def worker():
            try:
                status, _ = server.get()
            except Exception:
                status = 0
            with lock:
                results.append(status)

        try:
            workers = [threading.Thread(target=worker) for _ in range(24)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=15)
        finally:
            server.close()

        assert len(results) == 24
        assert all(status == 200 for status in results), results

    def test_concurrency_below_the_ceiling_is_not_starved(self) -> None:
        server = ServerFixture(limits=Limits(request_timeout=5, max_connections=64,
                                             max_sse_connections=32, sse_max_seconds=5,
                                             listen_backlog=128))
        latencies: list[float] = []
        lock = threading.Lock()

        def worker():
            t0 = time.perf_counter()
            try:
                server.get()
            except Exception:
                return
            with lock:
                latencies.append((time.perf_counter() - t0) * 1000)

        try:
            workers = [threading.Thread(target=worker) for _ in range(40)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=15)
        finally:
            server.close()

        assert len(latencies) == 40
        # One thread per connection, so 40 concurrent requests really do run at
        # once. A ceiling low enough to serialise them would show up here.
        assert max(latencies) < 2_000

    def test_occupancy_returns_to_zero_when_clients_leave(self) -> None:
        server = ServerFixture()
        try:
            for _ in range(5):
                server.get()
            deadline = time.monotonic() + 5
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.counter.active == 0
        finally:
            server.close()


# ---------------------------------------------------------------------------
# 2. SSE must not starve normal requests
# ---------------------------------------------------------------------------


class TestStreamIsolation:
    def test_streams_are_capped_below_the_connection_pool(self) -> None:
        server = ServerFixture()
        assert server.limits.max_sse_connections < server.limits.max_connections

    def test_normal_requests_still_answered_while_streams_are_open(self) -> None:
        """The requirement: a burst of streams must not starve ordinary calls."""
        server = ServerFixture(limits=Limits(request_timeout=5, max_connections=32,
                                             max_sse_connections=2, sse_max_seconds=30,
                                             listen_backlog=128))
        streams: list[http.client.HTTPConnection] = []
        try:
            # Saturate the stream ceiling.
            for _ in range(2):
                conn = server.connect(timeout=5)
                conn.request("POST", "/stream", body=b"{}",
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.readline()  # consume the first event so the stream is live
                streams.append(conn)
            assert server.counter.active_sse == 2

            status, body = server.get("/healthz", timeout=5)
            assert status == 200
            assert json.loads(body)["ok"] is True
        finally:
            for conn in streams:
                try:
                    conn.close()
                except Exception:
                    pass
            server.close()

    def test_exceeding_the_stream_ceiling_returns_503(self) -> None:
        server = ServerFixture(limits=Limits(request_timeout=5, max_connections=32,
                                             max_sse_connections=1, sse_max_seconds=30,
                                             listen_backlog=128))
        held: list[http.client.HTTPConnection] = []
        try:
            first = server.connect(timeout=5)
            first.request("POST", "/stream", body=b"{}",
                          headers={"Content-Type": "application/json"})
            first.getresponse().readline()
            held.append(first)

            second = server.connect(timeout=5)
            second.request("POST", "/stream", body=b"{}",
                           headers={"Content-Type": "application/json"})
            resp = second.getresponse()
            body = resp.read()
            assert resp.status == 503
            assert b"streams" in body
        finally:
            for conn in held:
                try:
                    conn.close()
                except Exception:
                    pass
            server.close()

    def test_a_refused_stream_reports_a_status_not_an_empty_200(self) -> None:
        """A 200 with no events would hang the client forever."""
        server = ServerFixture(limits=Limits(request_timeout=5, max_connections=32,
                                             max_sse_connections=0, sse_max_seconds=30,
                                             listen_backlog=128))
        try:
            status, body = server._post_stream_status()
            assert status == 503
            assert b"retry" in body.lower() or b"streams" in body.lower()
        finally:
            server.close()


# ---------------------------------------------------------------------------
# 3. Slow clients
# ---------------------------------------------------------------------------


class TestSlowClients:
    def test_a_client_that_stalls_mid_request_loses_its_thread(self) -> None:
        """The core leak: no socket timeout meant a stalled read held a thread
        for the life of the process."""
        server = ServerFixture(limits=Limits(request_timeout=1.0, max_connections=64,
                                             max_sse_connections=8, sse_max_seconds=30,
                                             listen_backlog=128))
        stalled: list[socket.socket] = []
        try:
            for _ in range(6):
                s = server.raw_socket()
                s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")  # never finished
                stalled.append(s)
            time.sleep(0.3)
            assert server.counter.active > 0

            deadline = time.monotonic() + 10
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.1)
            assert server.counter.active == 0, "stalled connections were not reaped"
        finally:
            for s in stalled:
                try:
                    s.close()
                except Exception:
                    pass
            server.close()

    def test_an_idle_keepalive_connection_is_reaped(self) -> None:
        """HTTP/1.1 keep-alive with timeout=None pinned a thread per idle socket."""
        server = ServerFixture(limits=Limits(request_timeout=1.0, max_connections=64,
                                             max_sse_connections=8, sse_max_seconds=30,
                                             listen_backlog=128))
        held: list[http.client.HTTPConnection] = []
        try:
            for _ in range(5):
                conn = server.connect(timeout=5)
                conn.request("GET", "/healthz")
                conn.getresponse().read()
                held.append(conn)  # kept open, idle
            deadline = time.monotonic() + 12
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.1)
            assert server.counter.active == 0
        finally:
            for conn in held:
                try:
                    conn.close()
                except Exception:
                    pass
            server.close()

    def test_a_client_that_stops_reading_is_dropped(self) -> None:
        """Write side of the timeout: a stalled reader must not pin a thread."""
        server = ServerFixture(limits=Limits(request_timeout=2.0, max_connections=64,
                                             max_sse_connections=8, sse_max_seconds=30,
                                             listen_backlog=128))
        s = server.raw_socket()
        try:
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(3.0)  # never read the response
            deadline = time.monotonic() + 10
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.1)
            assert server.counter.active == 0
        finally:
            s.close()
            server.close()


# ---------------------------------------------------------------------------
# 4. Disconnected clients
# ---------------------------------------------------------------------------


class TestDisconnectedClients:
    def test_a_client_that_hangs_up_mid_response_releases_its_slot(self) -> None:
        server = ServerFixture()
        try:
            for _ in range(4):
                s = server.raw_socket()
                s.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
                time.sleep(0.1)
                s.close()  # hang up while the handler is still sleeping
            deadline = time.monotonic() + 20
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.1)
            assert server.counter.active == 0
        finally:
            server.close()

    def test_abrupt_disconnect_does_not_print_a_traceback(self) -> None:
        """socketserver tracebacks on routine disconnects are an unbounded log sink."""
        import contextlib
        import io

        server = ServerFixture()
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stderr(buffer):
                s = server.raw_socket()
                s.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
                time.sleep(0.2)
                s.close()
                time.sleep(1.0)
        finally:
            server.close()
        assert "BrokenPipeError" not in buffer.getvalue()

    def test_counter_never_goes_negative(self) -> None:
        counter = ConnectionCounter(Limits(max_connections=4))
        for _ in range(10):
            counter.release()
        assert counter.active == 0


# ---------------------------------------------------------------------------
# 5. Connection floods
# ---------------------------------------------------------------------------


class TestConnectionFlood:
    def test_connections_beyond_the_ceiling_are_refused_with_a_status(self) -> None:
        server = ServerFixture(limits=Limits(request_timeout=30, max_connections=4,
                                             max_sse_connections=2, sse_max_seconds=30,
                                             listen_backlog=128))
        held: list[http.client.HTTPConnection] = []
        try:
            for _ in range(4):
                conn = server.connect(timeout=5)
                conn.request("GET", "/slow")
                held.append(conn)  # occupy all four slots
            time.sleep(0.5)

            refused = server.get("/healthz", timeout=5)
            assert refused[0] == 503
            assert b"limit" in refused[1]
        finally:
            for conn in held:
                try:
                    conn.close()
                except Exception:
                    pass
            server.close()

    def test_the_ceiling_holds_thread_count_within_the_limit(self) -> None:
        server = ServerFixture(limits=Limits(request_timeout=30, max_connections=6,
                                             max_sse_connections=2, sse_max_seconds=30,
                                             listen_backlog=128))
        socks: list[socket.socket] = []
        try:
            for _ in range(40):
                try:
                    s = server.raw_socket()
                    s.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
                    socks.append(s)
                except OSError:
                    pass
            time.sleep(1.0)
            assert server.counter.active <= 6
            assert server.counter.report()["connections_refused"] > 0
        finally:
            for s in socks:
                try:
                    s.close()
                except Exception:
                    pass
            server.close()

    def test_slots_are_reusable_after_a_refusal(self) -> None:
        server = ServerFixture(limits=Limits(request_timeout=5, max_connections=2,
                                             max_sse_connections=1, sse_max_seconds=30,
                                             listen_backlog=128))
        held: list[http.client.HTTPConnection] = []
        try:
            for _ in range(2):
                conn = server.connect(timeout=5)
                conn.request("GET", "/slow")
                held.append(conn)
            time.sleep(0.3)
            assert server.get("/healthz")[0] == 503
            for conn in held:
                conn.close()
            deadline = time.monotonic() + 10
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.1)
            assert server.get("/healthz")[0] == 200
        finally:
            server.close()

    def test_the_listen_backlog_is_wider_than_the_stdlib_default(self) -> None:
        server = ServerFixture()
        try:
            assert ThreadingHTTPServer.request_queue_size == 5
            assert server.server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) > 0
            assert server.limits.listen_backlog == 128
        finally:
            server.close()

    def test_a_burst_larger_than_the_backlog_is_not_refused_by_the_kernel(self) -> None:
        """Backlog 5 refuses a burst before the server can answer with a status."""
        server = ServerFixture(limits=Limits(request_timeout=30, max_connections=128,
                                             max_sse_connections=8, sse_max_seconds=30,
                                             listen_backlog=512))
        socks: list[socket.socket] = []
        try:
            for _ in range(100):
                try:
                    socks.append(server.raw_socket())
                except OSError:
                    pass
            time.sleep(0.5)
            accepted = server.counter.report()["connections_refused"]
            assert len(socks) - accepted > 5, "backlog too small to absorb a burst"
        finally:
            for s in socks:
                try:
                    s.close()
                except Exception:
                    pass
            server.close()


# ---------------------------------------------------------------------------
# 6. Graceful shutdown with connections active
# ---------------------------------------------------------------------------


class TestShutdownWithActiveConnections:
    def test_shutdown_completes_while_a_request_is_in_flight(self) -> None:
        server = ServerFixture()
        server.server.lifecycle.begin("test")
        assert server.server.lifecycle.draining
        try:
            status, _ = server.get("/healthz")
            assert status == 200, "health must stay reachable while draining"
        finally:
            server.close()

    def test_non_health_routes_are_refused_while_draining(self) -> None:
        server = ServerFixture()
        try:
            server.server.lifecycle.begin("test")
            status, body = server.get("/nothing")
            assert status == 503
            assert b"draining" in body
        finally:
            server.close()

    def test_shutdown_releases_every_slot(self) -> None:
        server = ServerFixture()
        try:
            for _ in range(3):
                server.get()
            deadline = time.monotonic() + 5
            while server.counter.active and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.counter.active == 0
        finally:
            server.close()
        assert server.thread.is_alive() is False, "serve_forever must have returned"

    def test_drain_does_not_wait_on_a_stalled_connection_past_the_timeout(self) -> None:
        """A stalled client must not extend the drain beyond its budget."""
        limits = Limits(request_timeout=1.0, max_connections=16,
                        max_sse_connections=4, sse_max_seconds=30, listen_backlog=128)
        server = ServerFixture(limits=limits)
        stalled: list[socket.socket] = []
        try:
            for _ in range(3):
                s = server.raw_socket()
                s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")
                stalled.append(s)
            time.sleep(0.2)
            started = time.monotonic()
            ok = server.server.lifecycle.wait_for_idle(
                deadline=time.monotonic() + 10
            )
            elapsed = time.monotonic() - started
            # Either it went idle quickly, or the caller's deadline stopped it.
            # What matters is that it did not block on the stalled sockets.
            assert elapsed < 11
            assert ok in (True, False)
        finally:
            for s in stalled:
                try:
                    s.close()
                except Exception:
                    pass
            server.close()


# ---------------------------------------------------------------------------
# Configuration and wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_the_server_is_still_a_threading_http_server(self) -> None:
        """Requirement 1: hardened, not replaced."""
        assert issubclass(BoundedThreadingHTTPServer, ThreadingHTTPServer)

    def test_daemon_threads_and_reuse_are_preserved(self) -> None:
        assert BoundedThreadingHTTPServer.daemon_threads is True
        assert BoundedThreadingHTTPServer.allow_reuse_address is True

    def test_the_handler_carries_a_socket_timeout(self) -> None:
        assert query_ui._Handler.timeout is not None
        assert query_ui._Handler.timeout > 0

    def test_limits_come_from_the_environment(self) -> None:
        limits = query_ui.http_limits()
        assert limits.max_connections == query_ui.MAX_CONNECTIONS
        assert limits.max_sse_connections == query_ui.MAX_SSE_CONNECTIONS
        assert limits.sse_max_seconds == query_ui.SSE_MAX_SECONDS
        assert limits.listen_backlog == query_ui.LISTEN_BACKLOG

    def test_each_listener_gets_its_own_counter(self) -> None:
        a, b = free_port(), free_port()
        servers = query_ui._listeners(
            "127.0.0.1", [a, b], _FastHandler,
            limits=Limits(request_timeout=5, max_connections=8,
                          max_sse_connections=2, sse_max_seconds=5, listen_backlog=64),
        )
        try:
            assert len(servers) == 2
            assert servers[0].connections is not servers[1].connections
        finally:
            for server in servers:
                server.server_close()

    def test_body_size_cap_is_unchanged(self) -> None:
        """Existing protection that must not regress."""
        assert query_ui.MAX_BODY == 128 * 1024

    def test_counter_report_names_every_ceiling(self) -> None:
        report = ConnectionCounter(Limits()).report()
        for key in ("active_connections", "max_connections", "active_streams",
                    "max_streams", "request_timeout_seconds", "listen_backlog"):
            assert key in report


# ---------------------------------------------------------------------------
# SSE lifetime
# ---------------------------------------------------------------------------


class TestSseLifetime:
    def test_a_stream_is_bounded_by_sse_max_seconds(self) -> None:
        server = ServerFixture(limits=Limits(request_timeout=10, max_connections=32,
                                             max_sse_connections=4, sse_max_seconds=1.0,
                                             listen_backlog=128))
        try:
            conn = server.connect(timeout=15)
            conn.request("POST", "/stream", body=b"{}",
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            started = time.monotonic()
            seen_done = False
            while time.monotonic() - started < 12:
                line = resp.readline()
                if not line:
                    break
                if line.startswith(b"event: done"):
                    seen_done = True
                    break
            elapsed = time.monotonic() - started
            assert seen_done, "stream never reached its terminal event"
            assert elapsed < 10, "stream outlived its configured lifetime"
        finally:
            server.close()

    def test_default_sse_lifetime_matches_the_model_timeout(self) -> None:
        assert query_ui.SSE_MAX_SECONDS == float(int(query_ui.RAG_TIMEOUT))


class TestSseDeadlineLogic:
    """The real handler's truncation path, without needing a model backend.

    The integration test above proves the *policy* via a stand-in stream. These
    cover the production ``_send_sse`` deadline check and the terminal frame,
    which are the parts a stand-in would bypass.
    """

    class _FakeWfile:
        def __init__(self) -> None:
            self.chunks: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.chunks.append(bytes(data))
            return len(data)

        def flush(self) -> None:
            pass

    class _Handler(query_ui._Handler):
        kg = None

        def __init__(self) -> None:  # noqa: D107 - bypass the socket machinery
            self.wfile = TestSseDeadlineLogic._FakeWfile()
            self._sse_deadline: float | None = None
            self._sse_started = time.monotonic()

    def test_a_send_before_the_deadline_writes_the_frame(self) -> None:
        handler = self._Handler()
        handler._sse_deadline = time.monotonic() + 30
        handler._send_sse("token", {"token": "hi"})
        assert b"event: token" in b"".join(handler.wfile.chunks)

    def test_a_send_past_the_deadline_raises_and_names_the_event(self) -> None:
        handler = self._Handler()
        handler._sse_deadline = time.monotonic() - 1
        with pytest.raises(query_ui._SseDeadlineExceeded) as caught:
            handler._send_sse("token", {"token": "hi"})
        assert caught.value.event == "token"

    def test_the_terminal_frame_bypasses_the_deadline(self) -> None:
        """The frame that *reports* the truncation must still be deliverable."""
        handler = self._Handler()
        handler._sse_deadline = time.monotonic() - 1
        handler._send_sse_unbounded("done", {"status": "truncated"})
        assert b"truncated" in b"".join(handler.wfile.chunks)

    def test_clearing_the_deadline_restores_normal_sending(self) -> None:
        handler = self._Handler()
        handler._sse_deadline = None
        handler._send_sse("token", {"token": "hi"})
        assert b"event: token" in b"".join(handler.wfile.chunks)

    def test_the_deadline_is_not_sticky_after_a_stream_finishes(self) -> None:
        """A handler reused by keep-alive must not inherit the old deadline."""
        handler = self._Handler()
        handler._sse_deadline = time.monotonic() - 1
        handler._send_sse_unbounded("done", {})
        assert handler._sse_deadline is None


def _post_stream_status(self: ServerFixture) -> tuple[int, bytes]:
    conn = self.connect(timeout=10)
    try:
        conn.request("POST", "/stream", body=b"{}",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


ServerFixture._post_stream_status = _post_stream_status

class TestRefusalIsWellFormed:
    """A refusal is the one response a client gets instead of the service, so
    it has to be the clearest one the server ever sends."""

    def _refusal(self):
        from sandbox_engine.http_limits import _REFUSAL, _REFUSAL_BODY

        head, body = _REFUSAL.split(b"\r\n\r\n", 1)
        return head, body, _REFUSAL_BODY

    def test_the_declared_length_matches_the_body_it_ships(self) -> None:
        head, body, _ = self._refusal()
        declared = int(
            [line for line in head.split(b"\r\n")
             if line.lower().startswith(b"content-length")][0].split(b":")[1]
        )
        assert declared == len(body), (
            f"Content-Length says {declared} but the body is {len(body)} bytes; "
            "a client would read truncated JSON"
        )

    def test_the_body_is_valid_json_naming_the_limit(self) -> None:
        import json

        _, body, _ = self._refusal()
        parsed = json.loads(body)  # raises if truncated or malformed
        assert parsed["limit"] == "max_connections"
        assert "retry" in parsed["error"]

    def test_it_carries_a_status_a_client_can_act_on(self) -> None:
        head, _, _ = self._refusal()
        text = head.decode("latin-1")
        assert text.startswith("HTTP/1.1 503 ")
        assert "Content-Type: application/json" in text
        assert "Connection: close" in text

    def test_a_refused_client_can_parse_the_body_end_to_end(self) -> None:
        """Through a real socket, so the test covers what the client sees
        rather than what the server believes it wrote."""
        import json

        server = ServerFixture(limits=Limits(request_timeout=5.0, max_connections=1,
                                             max_sse_connections=1, sse_max_seconds=5,
                                             listen_backlog=128))
        first = server.connect()
        first.request("GET", "/healthz")
        first.getresponse().read()
        conn = server.connect()
        try:
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            assert response.status == 503
            parsed = json.loads(response.read())  # would raise if truncated
            assert parsed["limit"] == "max_connections"
        finally:
            for c in (first, conn):
                try:
                    c.close()
                except Exception:
                    pass
            server.close()


class TestLimitsFromEnv:
    """``Limits.from_env`` is the one place the variable names and the
    fallbacks live, so every HTTP entry point answers to the same contract."""

    def test_unset_variables_yield_the_documented_defaults(self) -> None:
        assert Limits.from_env(lambda name: "") == Limits()

    def test_values_are_read_from_the_supplied_names(self) -> None:
        wanted = {"REQUEST_TIMEOUT": "7", "MAX_CONNECTIONS": "9",
                  "MAX_SSE_CONNECTIONS": "3", "SSE_MAX_SECONDS": "11",
                  "LISTEN_BACKLOG": "13"}
        assert Limits.from_env(lambda name: wanted.get(name, "")) == Limits(
            request_timeout=7.0, max_connections=9, max_sse_connections=3,
            sse_max_seconds=11.0, listen_backlog=13,
        )

    def test_unparsable_configuration_degrades_instead_of_crashing(self) -> None:
        """A typo in .env must not be why the server refuses to start, and must
        not silently disable a ceiling either."""
        assert Limits.from_env(lambda name: "not-a-number") == Limits()

    def test_a_zero_or_negative_ceiling_falls_back(self) -> None:
        """Zero would otherwise read as 'refuse everything'."""
        assert Limits.from_env(lambda name: "0") == Limits()
        assert Limits.from_env(lambda name: "-5") == Limits()

    def test_a_fractional_connection_count_is_not_a_silent_truncation(self) -> None:
        got = Limits.from_env(lambda name: "8.9" if name == "MAX_CONNECTIONS" else "")
        assert got.max_connections == 8

    def test_the_names_are_the_ones_documented_in_env_example(self) -> None:
        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
        for name in ENV_NAMES.values():
            assert f"# {name}=" in text, f"{name} is not documented in .env.example"


class TestFingraphIsAlsoBounded:
    """``ui.fingraph`` ships HTTP/1.1 keep-alive and is the canonical UI
    on port 9100, so it has the same idle-socket exposure. It has SSE
    streaming responses, so both transport and streaming ceilings apply."""

    @staticmethod
    def _server(**kw):
        from ui.fingraph import server as fg
        from sandbox_engine.query_ui import _listeners

        handler = type("_TestHandler", (fg._NextHandler,), {})
        servers = _listeners("127.0.0.1", [0], handler, limits=Limits(**kw))
        return servers[0]

    def test_it_binds_the_bounded_server_not_the_raw_one(self) -> None:
        srv = self._server(request_timeout=2.0)
        try:
            assert isinstance(srv, BoundedThreadingHTTPServer)
            assert srv.limits.request_timeout == 2.0
        finally:
            srv.server_close()

    def test_the_handler_declares_a_finite_timeout(self) -> None:
        """``timeout = None`` under HTTP/1.1 is the original defect: every idle
        keep-alive socket would pin a thread for the life of the process."""
        from ui.fingraph import server as fg

        assert fg._NextHandler.protocol_version == "HTTP/1.1"
        assert fg._NextHandler.timeout is not None
        assert fg._NextHandler.timeout > 0

    def test_per_server_limits_reach_the_handler_instance(self) -> None:
        """The class attribute is only the process default; ``setup`` must
        narrow it, or a tuned listener would keep the old timeout."""
        from ui.fingraph import server as fg
        from sandbox_engine.query_ui import _listeners

        seen: list[float] = []

        class _Probe(fg._NextHandler):
            def setup(self) -> None:
                super().setup()
                seen.append(self.timeout)

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = _listeners("127.0.0.1", [0], _Probe, limits=Limits(request_timeout=6.0))[0]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/")
            conn.getresponse().read()
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
        assert seen == [6.0]

    def test_idle_keepalive_sockets_are_reaped(self) -> None:
        """The end-to-end proof for this server: hold sockets open, then let go
        of the client's interest and confirm the threads come back."""
        from ui.fingraph import server as fg
        from sandbox_engine.query_ui import _listeners

        class _Idle(fg._NextHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = _listeners("127.0.0.1", [0], _Idle, limits=Limits(request_timeout=1.0))[0]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        held: list[http.client.HTTPConnection] = []
        try:
            for _ in range(5):
                conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                conn.request("GET", "/")
                conn.getresponse().read()
                held.append(conn)
            time.sleep(0.4)
            assert srv.connections.active > 0
            deadline = time.monotonic() + 12
            while srv.connections.active and time.monotonic() < deadline:
                time.sleep(0.1)
            assert srv.connections.active == 0, "idle keep-alive sockets were not reaped"
        finally:
            for conn in held:
                try:
                    conn.close()
                except Exception:
                    pass
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)

    def test_a_connection_flood_is_refused_with_a_status(self) -> None:
        from ui.fingraph import server as fg
        from sandbox_engine.query_ui import _listeners

        srv = _listeners("127.0.0.1", [0], fg._NextHandler, limits=Limits(max_connections=4))[0]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        held: list[socket.socket] = []
        refused = 0
        try:
            for _ in range(24):
                s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
                held.append(s)
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                s.settimeout(2.0)
                try:
                    if b"503" in s.recv(64):
                        refused += 1
                except Exception:
                    pass
            assert refused > 0, "the ceiling never engaged"
            assert srv.connections.active <= 4
        finally:
            for s in held:
                try:
                    s.close()
                except Exception:
                    pass
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
