"""Transport-level behaviour of the query server.

The question box showed ``Failed to fetch`` and nothing else. That string is
the browser's, not the server's: it means ``fetch`` rejected, so the request
never produced a status, a body, or a message. Two very different faults look
identical from there -- the server is not running, or the server raised and
``socketserver`` closed the socket without answering -- and they need opposite
fixes.

These tests pin the half the server controls: an internal error must arrive as
a JSON body the UI can display, never as a dropped connection.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from sandbox_engine import query_ui


class _StubGraph:
    """Stands in for KnowledgeGraph; every route here is overridden anyway."""

    def stats(self):
        return {"nodes": 0, "edges": 0}


class AskTransportTests(unittest.TestCase):
    """POST /api/ask must answer, whatever happens inside."""

    @classmethod
    def setUpClass(cls):
        cls.saved_ask = query_ui.ask_rag
        handler = type("H", (query_ui._Handler,), {"kg": _StubGraph()})
        # Port 0 lets the OS pick a free port, so the suite never collides with
        # a server the developer already has on 9000.
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        query_ui.ask_rag = cls.saved_ask
        cls.server.shutdown()
        cls.server.server_close()

    def _post_ask(self, question="net sales?"):
        """Return ``(status, body)`` for any answered response.

        ``urlopen`` raises ``HTTPError`` for a 4xx/5xx, but that is still an
        answer: a status line and a body arrived. The failure this module is
        about is the absence of any response at all, which surfaces as
        ``RemoteDisconnected`` or a bare ``URLError``.
        """
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/ask",
            data=json.dumps({"question": question}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_internal_error_is_reported_not_dropped(self):
        """The regression: a raise inside the ask path used to close the socket.

        ``socketserver`` prints the traceback to stderr and writes no response,
        which the browser renders as ``Failed to fetch``. The page gave the
        reader no status and no cause, and the only real diagnosis sat in the
        terminal running the server.
        """
        def explode(kg, question):
            raise RuntimeError("ladybug: cannot bind DISAGGREGATED_BY")

        query_ui.ask_rag = explode
        self.addCleanup(setattr, query_ui, "ask_rag", self.saved_ask)

        try:
            status, body = self._post_ask()
        except Exception as exc:  # pragma: no cover - the bug being fixed
            self.fail(
                f"connection dropped instead of answered ({type(exc).__name__}); "
                f"the browser would show 'Failed to fetch'"
            )
        self.assertEqual(status, 500)
        # The exception text has to survive to the page, or the message is a
        # status code with nothing to act on.
        self.assertIn("cannot bind DISAGGREGATED_BY", body["error"])
        self.assertIn("RuntimeError", body["error"])

    def test_normal_path_still_answers(self):
        """The guard must not swallow the ordinary result."""
        def ok(kg, question):
            return {"question": question, "text": "Net sales were $46.7B.", "grounded": True}

        query_ui.ask_rag = ok
        self.addCleanup(setattr, query_ui, "ask_rag", self.saved_ask)

        status, body = self._post_ask()
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "Net sales were $46.7B.")

    def test_bad_request_still_validated_before_the_guard(self):
        """Validation errors are 4xx, not swallowed into a 500."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/ask",
            data=json.dumps({"question": "   "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=20)
        self.assertEqual(ctx.exception.code, 400)


class ClientMessageTests(unittest.TestCase):
    """The page has to explain a transport failure, not echo the browser's."""

    @classmethod
    def setUpClass(cls):
        cls.html = query_ui._HTML

    def test_api_wraps_fetch_and_explains(self):
        self.assertIn("catch (e)", self.html)
        self.assertIn("describeTransportFailure", self.html)
        # The bare browser string must never be what the reader is shown.
        self.assertNotIn("Query failed: ${esc(e.message)}${", self.html)

    def test_transport_message_distinguishes_the_two_causes(self):
        """Probing a cheap endpoint is what separates "server is gone" from
        "server dropped the request"; without it both read as Failed to fetch.
        """
        self.assertIn("/api/stats", self.html)
        self.assertIn("not running", self.html)
        self.assertIn("without sending a response", self.html)

    def test_ask_surface_reports_the_error(self):
        self.assertIn("Query failed: ${esc(e.message)}", self.html)


class RetrievalRobustnessTests(unittest.TestCase):
    """Absent properties are ordinary in a property graph, not an error."""

    def test_retrieval_tolerates_missing_fields(self):
        """Every match below ran `.lower()` or `in` on a value straight from the
        database, so one NULL segment name killed the whole question. The
        corpus happens to have none today, which is exactly why it survived.
        """
        class G:
            def execute(self, q):
                return {
                    "MATCH (c:Company) RETURN c.ticker, c.legal_name, c.cik": [("AAPL", "Apple Inc.", "1")],
                    "MATCH (c:Company)-[:SUBMITTED]->(f:Filing)": [],
                    "MATCH (f:Filing) RETURN": [("acc-1", "10-K", 2025, "FY", "2025-09-27")],
                    "MATCH (m:FinancialMetric) RETURN": [],
                    "MATCH (s:Segment) RETURN": [("s1", None, None)],
                    "MATCH (e:DisclosureEvent) RETURN": [("e1", None, None, None)],
                    "CONTAINS_CHUNK": [("acc-1", "c1", None, "Item 1")],
                }.get(_shape(q), [])

        def _shape(query):
            for key in (
                "MATCH (c:Company) RETURN c.ticker, c.legal_name, c.cik",
                "MATCH (c:Company)-[:SUBMITTED]->(f:Filing)",
                "MATCH (f:Filing) RETURN",
                "MATCH (m:FinancialMetric) RETURN",
                "MATCH (s:Segment) RETURN",
                "MATCH (e:DisclosureEvent) RETURN",
            ):
                if key in query:
                    return key
            return "CONTAINS_CHUNK" if "CONTAINS_CHUNK" in query else ""

        ctx, nodes, edges, tag_map, seeds = query_ui.retrieve_financial_context(
            G(), "what are the segments and risks"
        )
        self.assertIsInstance(ctx, str)


if __name__ == "__main__":
    unittest.main()
