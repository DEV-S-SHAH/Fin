"""What `POST /api/auth/session` will and will not mint a session for.

The endpoint existed with no tests, and it read like this:

    provider = payload["provider"]
    ...
    self.send_response(200)
    self._send_cors_headers()
    self.send_header("Set-Cookie", _cookie_header(_session_token(provider), ...))

There is nothing between the request and the cookie. So `curl -d
'{"provider":"dev","token":"x"}'` returns a signed session cookie, and so does
`{"provider":"google","token":"x"}` the moment `FINGRAPH_GOOGLE_CLIENT_ID` is set
-- the handler's own docstring admits the provider token "is not re-validated
against the provider here", but it issues the cookie anyway.

`dev` was the worse half, because it was unconditional: `_ALLOWED_PROVIDERS` adds
it to the set, and the `provider != "dev"` guard skips the one configured-provider
check. A deployment with Google sign-in fully configured could still be walked
into with `{"provider":"dev","token":"anything"}`. That is an authentication
bypass, not a gap in polish, and it is the reason this file exists.

These tests assert the refusals, so the bypass cannot come back as a refactor.
"""

import http.client
import json
import threading
import unittest

from sandbox_engine import query_ui
from ui.fingraph import server as ui_server


class _StubGraph:
    def stats(self):
        return {"nodes": 0, "edges": 0}

    def has_company(self, ticker: str) -> bool:
        return False

    def execute(self, cypher: str, params: dict | None = None) -> list:
        return []


class SessionMintingTests(unittest.TestCase):
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

    def _post_session(self, payload: dict) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            body = json.dumps(payload)
            conn.request(
                "POST",
                "/api/auth/session",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_an_arbitrary_token_mints_no_session_for_a_configured_provider(self):
        """The bypass this file is about.

        `google` is written into `AUTH_PROVIDERS` for the duration of the test so
        the provider is "configured" -- the state a real deployment is in -- and
        the body is then what any stranger would send.
        """
        original = ui_server.AUTH_PROVIDERS["google"]
        ui_server.AUTH_PROVIDERS["google"] = "configured-client-id.apps.example"
        self.addCleanup(ui_server.AUTH_PROVIDERS.__setitem__, "google", original)

        status, _ = self._post_session({"provider": "google", "token": "not-a-real-token"})

        self.assertNotEqual(
            status,
            200,
            "an unvalidated token was exchanged for a session cookie",
        )

    def test_an_arbitrary_token_mints_no_session_via_the_dev_provider(self):
        """`dev` is the unconditional half, and it is the one that reaches production."""
        status, _ = self._post_session({"provider": "dev", "token": "anything"})

        self.assertNotEqual(
            status,
            200,
            "the dev sign-in minted a session in an environment that has not opted in",
        )

    def test_the_response_carries_no_cookie_when_it_refuses(self):
        """A refusal that still sets a cookie is not a refusal."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            body = json.dumps({"provider": "dev", "token": "anything"})
            conn.request(
                "POST",
                "/api/auth/session",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            resp.read()
            cookies = resp.getheader("Set-Cookie") or ""
        finally:
            conn.close()

        self.assertNotIn(ui_server.SESSION_COOKIE, cookies)

    def test_an_unknown_provider_is_refused(self):
        status, _ = self._post_session({"provider": "not-a-provider", "token": "x"})
        self.assertEqual(status, 400)

    def test_the_config_endpoint_still_reports_which_providers_exist(self):
        """Reading config must not have broken the button-visibility contract."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("GET", "/api/auth/config")
            resp = conn.getresponse()
            body = json.loads(resp.read())
        finally:
            conn.close()

        self.assertEqual(set(body), set(ui_server.AUTH_PROVIDERS))
        for key, value in body.items():
            self.assertIsInstance(value, bool, key)


class DevSignInGateTests(unittest.TestCase):
    """The gate is a decision, so the decision is pinned rather than implied."""

    def test_dev_sign_in_requires_an_explicit_opt_in(self):
        self.assertFalse(
            ui_server.dev_sign_in_enabled(),
            "dev sign-in defaults to on, so a plain deployment ships an open door",
        )

    def test_the_env_var_opens_it(self):
        with _env(ui_server.DEV_LOGIN_ENV, "1"):
            self.assertTrue(ui_server.dev_sign_in_enabled())

    def test_a_bogus_value_does_not_open_it(self):
        for value in ("", "0", "false", "no", "off", "maybe"):
            with _env(ui_server.DEV_LOGIN_ENV, value):
                self.assertFalse(
                    ui_server.dev_sign_in_enabled(),
                    f"{value!r} must not enable the dev sign-in",
                )


def _env(key: str, value: str):
    import os
    from contextlib import contextmanager

    @contextmanager
    def cm():
        previous = os.environ.get(key)
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    return cm()


if __name__ == "__main__":
    unittest.main()