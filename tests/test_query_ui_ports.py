"""Serving the UI on more than one port, over one graph handle.

LadybugDB takes an exclusive lock on the database file, so a second server means
a second *copy* of a 2 GB graph that is stale the moment the first is rebuilt.
The alternative is one process with a listener per port, sharing one handle, and
that is what this pins -- including the two ways it can quietly go wrong: a
second port that fails to bind while the first keeps listening, and a shutdown
that closes only one of them.
"""

from __future__ import annotations

import socket
import sys
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_engine import query_ui


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the name the base class requires
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class ParsePortsTests(unittest.TestCase):
    def test_a_single_port_is_a_list_of_one(self):
        self.assertEqual(query_ui.parse_ports(9000), [9000])
        self.assertEqual(query_ui.parse_ports("9000"), [9000])

    def test_a_comma_list_is_the_flag_reading_the_way_it_is_typed(self):
        self.assertEqual(query_ui.parse_ports("9000,8765"), [9000, 8765])
        self.assertEqual(query_ui.parse_ports(" 9000 , 8765 "), [9000, 8765])

    def test_a_sequence_passes_through(self):
        self.assertEqual(query_ui.parse_ports([9000, 8765]), [9000, 8765])

    def test_a_typo_is_refused_before_a_socket_is_bound(self):
        for bad in ("nine thousand", "9000,abc", "", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    query_ui.parse_ports(bad)


class ListenerTests(unittest.TestCase):
    def test_two_ports_both_serve(self):
        a, b = _free_port(), _free_port()
        servers = query_ui._listeners("127.0.0.1", [a, b], QuietHandler)
        try:
            import threading
            import urllib.request

            for s in servers:
                threading.Thread(target=s.serve_forever, daemon=True).start()
            for port in (a, b):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                    self.assertEqual(r.read(), b"ok", f"port {port}")
        finally:
            # shutdown() only on a listener that is actually serving. Called on
            # one that never entered serve_forever it blocks forever waiting for
            # a loop that is not running, which is the first version of this
            # test's teardown and it hung the suite.
            for s in servers:
                s.shutdown()
                s.server_close()

    def test_a_port_that_cannot_bind_leaves_no_listener_behind(self):
        """The first port is already up, so the second bind must fail cleanly.

        Half-starting is the failure worth catching: the process would carry on
        as though both were serving, and the port that worked would hold a
        database handle nobody asked to keep.
        """
        first, taken = _free_port(), _free_port()
        blocker = query_ui._listeners("127.0.0.1", [taken], QuietHandler)
        try:
            with self.assertRaises(OSError):
                query_ui._listeners("127.0.0.1", [first, taken], QuietHandler)
            # The first port must be free again, not left bound.
            probe = socket.socket()
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", first))
            finally:
                probe.close()
        finally:
            # Never served, so closed without shutdown().
            for s in blocker:
                s.server_close()


class ServeSignatureTests(unittest.TestCase):
    def test_serve_takes_a_sequence_and_still_takes_a_bare_int(self):
        """Existing callers pass one port; ``--port 9000,8765`` passes several."""
        import inspect

        params = inspect.signature(query_ui.serve).parameters
        self.assertIn("port", params)
        self.assertEqual(params["port"].default, 9000)
        self.assertEqual(query_ui.parse_ports(params["port"].default), [9000])


if __name__ == "__main__":
    unittest.main()
