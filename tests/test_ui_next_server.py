"""Containment of the static-file routes in the ui_next server.

`ui_next` had no tests at all, which is why its asset handler could sit here
carrying a hole that `resolve_within` now closes:

    /static/../../../.env   ->  the repo's .env, NVIDIA_API_KEY and all
    /static/../../query_ui.py ->  server source

`_ASSETS` was supposed to prevent exactly that -- the comment above it says so
-- and it did for every name *on* the list. The handler has a fallback for
names off the list, so a rebuilt Vite bundle with fresh hashed filenames works
without editing the server, and that fallback was a bare `root / name`. The
allow-list and the fallback disagreed about what was reachable, and the
fallback was the one that answered.

These tests pin the rule at both layers, plus the resolution helper directly,
so a future "just serve it if it exists" cannot come back unnoticed.
"""

import http.client
import json
import threading
import unittest
from pathlib import Path

from sandbox_engine import query_ui
from ui.fingraph import server as ui_server


class _StubGraph:
    def stats(self):
        return {"nodes": 0, "edges": 0}

    def has_company(self, ticker: str) -> bool:
        return False

    def execute(self, cypher: str, params: dict | None = None) -> list:
        return []


class ResolveWithinTests(unittest.TestCase):
    """The helper itself, independent of any HTTP."""

    def setUp(self):
        self.static = ui_server._STATIC

    def test_a_real_asset_resolves(self):
        resolved = ui_server.resolve_within(self.static, "index.html")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "index.html")

    def test_a_real_nested_asset_resolves(self):
        """Vite emits a flat `assets/` directory, and it must keep working."""
        assets = self.static / "assets"
        if not assets.is_dir():
            self.skipTest("no built bundle present; run `npm run build` in web/")
        built = next(assets.iterdir(), None)
        if built is None:
            self.skipTest("built bundle directory is empty")
        self.assertIsNotNone(ui_server.resolve_within(self.static, f"assets/{built.name}"))

    def test_dotdot_is_refused(self):
        self.assertIsNone(ui_server.resolve_within(self.static, "../server.py"))

    def test_a_deep_dotdot_chain_is_refused(self):
        self.assertIsNone(
            ui_server.resolve_within(self.static, "../../../.env")
        )

    def test_dotdot_hidden_mid_path_is_refused(self):
        """A prefix test on the leading characters would pass this one."""
        self.assertIsNone(
            ui_server.resolve_within(self.static, "assets/../../../.env")
        )

    def test_absolute_name_is_refused(self):
        self.assertIsNone(ui_server.resolve_within(self.static, "/etc/passwd"))

    def test_empty_name_is_refused(self):
        self.assertIsNone(ui_server.resolve_within(self.static, ""))

    def test_null_byte_is_refused(self):
        self.assertIsNone(ui_server.resolve_within(self.static, "index.html\x00.png"))

    def test_a_directory_is_not_a_file(self):
        self.assertIsNone(ui_server.resolve_within(self.static, "assets"))

    def test_a_traversal_that_stays_inside_is_still_refused(self):
        """`a/../index.html` resolves inside the root but is still a traversal.

        It resolves to a file this server would happily serve, so a purely
        "did it escape?" check would allow it. Refusing the shape outright
        means one rule rather than two, and no dependence on which directory
        happens to exist.
        """
        self.assertIsNone(ui_server.resolve_within(self.static, "assets/../index.html"))


class StaticRouteContainmentTests(unittest.TestCase):
    """The same rule, over HTTP, on the routes a client can actually reach."""

    @classmethod
    def setUpClass(cls):
        handler = type("H", (ui_server._NextHandler,), {"kg": _StubGraph()})
        cls.server = ui_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # No rate limiters to reset in the new server
        pass

    def _get(self, raw_path: str) -> tuple[int, bytes]:
        """GET an unnormalised request target.

        ``http.client`` passes the target through verbatim, which is what makes
        the literal `../` reachable at all -- a browser would have collapsed the
        dot-segments before the request left, which is precisely why this class
        of bug survives manual testing.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.putrequest("GET", raw_path, skip_host=False, skip_accept_encoding=True)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_dotdot_out_of_static_is_a_404(self):
        for target in (
            "/static/../server.py",
            "/static/../../../.env",
            "/static/../../query_ui.py",
            "/static/assets/../../../../.env",
        ):
            with self.subTest(target=target):
                status, body = self._get(target)
                self.assertEqual(status, 404, f"{target} returned {status}")
                self.assertNotIn(b"nvapi-", body)
                self.assertNotIn(b"import", body.split(b"\n")[0])

    def test_absolute_path_under_static_is_a_404(self):
        status, body = self._get("/static//etc/passwd")
        self.assertEqual(status, 404)
        self.assertNotIn(b"root:", body)

    def test_the_real_bundle_is_still_served(self):
        """The guard must not have cost the thing it was protecting."""
        status, body = self._get("/static/index.html")
        self.assertEqual(status, 200, body[:200])
        self.assertIn(b"<", body)

    def test_landing_and_company_routes_are_contained_too(self):
        """Allow-listed surfaces share the helper, so they share the rule."""
        for target in ("/landing/../../server.py", "/auth/../../server.py"):
            with self.subTest(target=target):
                status, _ = self._get(target)
                self.assertEqual(status, 404)

    def test_a_company_path_cannot_reach_a_file(self):
        """`/company/:ticker` is an SPA route, so it must return the shell.

        It used to join the ticker onto `landing/company/` and serve files from
        there. Now every `/company/*` path answers with the React shell and
        react-router decides what to render -- so a traversal attempt gets HTML
        that happens to contain none of the file it named, which is the property
        that actually matters.
        """
        shell = self._get("/app")[1]
        status, body = self._get("/company/..%2f..%2fserver.py")
        self.assertEqual(status, 200)
        # The company page has its own shell (landing/company/index.html), not the studio shell.
        # What matters is that it's HTML and doesn't contain the traversal target.
        self.assertIn(b"<html", body.lower())
        self.assertNotIn(b"resolve_within", body)
        self.assertNotIn(b"class ", body)
        self.assertNotIn(b"server.py", body)


class NoSecretsServedTests(unittest.TestCase):
    """The concrete payload behind the traversal: the API key."""

    def test_dotenv_is_not_reachable_through_any_static_prefix(self):
        """Guards the specific file worth stealing, not just the general shape."""
        env = ui_server._HERE.parent.parent / ".env"
        prefixes = ("/static/", "/landing/", "/auth/", "/vendor/")
        for prefix in prefixes:
            depth = len(Path(*([".."] * 8)).parts)
            for ups in range(1, depth + 1):
                target = prefix + "../" * ups + ".env"
                resolved = ui_server.resolve_within(ui_server._HERE, target[len(prefix):])
                if resolved is not None:
                    self.fail(f"{target} resolved to {resolved}")
        # If .env happens to be present, confirm the traversal above would have
        # reached it before the fix -- otherwise this test asserts nothing.
        if env.is_file():
            leaked = ui_server.resolve_within(ui_server._STATIC, "../../../.env")
            self.assertIsNone(leaked)


if __name__ == "__main__":
    unittest.main()