"""Tests for backend resolution in :mod:`sandbox_engine.query_ui`.

The backend used to be a single module constant read at import, so a reader
with a working key in ``.env`` still got the local model unless they also knew
to set ``RAG_BACKEND=nvidia`` -- and the only remedy was editing a file and
restarting. These tests pin the replacement: the backend is a function of
observable state, re-evaluated per request.

The properties that matter, in order: a key in a file must be used without
configuration; a refused key must not be offered again; a key typed into the
browser must beat a stored one; and with no key and no local server the app must
refuse to answer rather than pick something silently.

Run with::

    .venv/bin/python -m unittest sandbox_engine.test_rag_backends
"""

from __future__ import annotations

import unittest
from unittest import mock

from sandbox_engine import query_ui
from sandbox_engine.query_ui import RagBackends


class ResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backends = RagBackends()
        self.backends._forced = ""

    def resolve_with(self, stored_key: str, ollama: tuple[bool, list[str]]):
        with mock.patch.object(query_ui, "_load_api_key", return_value=stored_key), \
             mock.patch.object(self.backends, "ollama_models", return_value=ollama):
            return self.backends.resolve()

    def test_a_key_in_a_file_is_used_with_no_configuration(self) -> None:
        # The bug: the default was ollama, so this key sat unused and the reader
        # was told to start a server they did not need.
        state = self.resolve_with("nvapi-stored", (False, []))
        self.assertEqual(state["backend"], "nvidia")
        self.assertEqual(state["key_source"], "file")
        self.assertEqual(state["key_present"], True)

    def test_no_key_and_no_local_server_refuses_to_answer(self) -> None:
        state = self.resolve_with("", (False, []))
        self.assertEqual(state["backend"], "none")
        self.assertEqual(state["model"], "")
        self.assertIn("no local model server", state["reason"])

    def test_no_key_falls_back_to_the_local_model(self) -> None:
        state = self.resolve_with("", (True, ["llama3.2"]))
        self.assertEqual(state["backend"], "ollama")
        self.assertEqual(state["model"], "llama3.2")

    def test_a_key_beats_a_reachable_local_server(self) -> None:
        state = self.resolve_with("nvapi-stored", (True, ["llama3.2"]))
        self.assertEqual(state["backend"], "nvidia")

    def test_unavailable_payload_names_both_ways_out(self) -> None:
        state = self.resolve_with("", (False, []))
        payload = query_ui._unavailable_answer(state)
        self.assertTrue(payload["needs_input"])
        self.assertIn("NVIDIA API key", payload["error"])
        self.assertIn("ollama serve", payload["error"])
        self.assertIn("graph explorer below does not need either", payload["error"])


class RefusedKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backends = RagBackends()
        self.backends._forced = ""

    def resolve_with(self, stored_key: str, ollama: tuple[bool, list[str]]):
        with mock.patch.object(query_ui, "_load_api_key", return_value=stored_key), \
             mock.patch.object(self.backends, "ollama_models", return_value=ollama):
            return self.backends.resolve()

    def test_a_refused_stored_key_is_not_offered_again(self) -> None:
        # Otherwise the reader gets the identical 403 once per question, with
        # no indication that anything about the situation changed.
        self.assertEqual(self.resolve_with("nvapi-bad", (False, []))["backend"], "nvidia")
        self.backends.reject_key()
        self.assertEqual(self.resolve_with("nvapi-bad", (False, []))["backend"], "none")

    def test_a_refused_stored_key_falls_back_to_the_local_model(self) -> None:
        self.backends.reject_key()
        state = self.resolve_with("nvapi-bad", (True, ["llama3.2"]))
        self.assertEqual(state["backend"], "ollama")
        self.assertIn("refused", state["reason"])

    def test_rejecting_reports_which_key_it_was(self) -> None:
        with mock.patch.object(query_ui, "_load_api_key", return_value="nvapi-bad"):
            where = self.backends.reject_key()
        self.assertIn(".env", where)

    def test_a_refused_browser_key_is_cleared_and_reported(self) -> None:
        self.backends.set_session_key("nvapi-typed")
        where = self.backends.reject_key()
        self.assertIn("browser", where)
        with mock.patch.object(self.backends, "ollama_models", return_value=(False, [])), \
             mock.patch.object(query_ui, "_load_api_key", return_value=""):
            self.assertEqual(self.backends.resolve()["backend"], "none")


class SessionKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backends = RagBackends()
        self.backends._forced = ""

    def resolve_with(self, stored_key: str, ollama: tuple[bool, list[str]]):
        with mock.patch.object(query_ui, "_load_api_key", return_value=stored_key), \
             mock.patch.object(self.backends, "ollama_models", return_value=ollama):
            return self.backends.resolve()

    def test_a_browser_key_beats_a_stored_one(self) -> None:
        self.backends.set_session_key("nvapi-typed")
        state = self.resolve_with("nvapi-stored", (True, ["llama3.2"]))
        self.assertEqual(state["backend"], "nvidia")
        self.assertEqual(state["key_source"], "browser")

    def test_a_browser_key_rescues_a_refused_stored_one(self) -> None:
        self.backends.reject_key()
        self.backends.set_session_key("nvapi-typed")
        self.assertEqual(self.resolve_with("nvapi-bad", (False, []))["backend"], "nvidia")

    def test_the_key_is_used_for_the_call(self) -> None:
        self.backends.set_session_key("nvapi-typed")
        api_key, base_url = self.backends.credentials("nvidia")
        self.assertEqual(api_key, "nvapi-typed")
        self.assertEqual(base_url, query_ui.NVIDIA_BASE_URL)

    def test_the_key_never_appears_in_the_state_sent_to_the_browser(self) -> None:
        # A response echoing the credential would put it in devtools history and
        # in any proxy log between the server and the tab.
        self.backends.set_session_key("nvapi-super-secret")
        self.assertNotIn("nvapi-super-secret", str(self.resolve_with("", (False, []))))

    def test_clearing_the_key_returns_to_auto(self) -> None:
        self.backends.set_session_key("nvapi-typed")
        self.backends.set_session_key("")
        self.assertEqual(self.resolve_with("", (True, ["llama3.2"]))["backend"], "ollama")

    def test_the_local_backend_gets_a_placeholder_not_a_real_key(self) -> None:
        self.backends.set_session_key("nvapi-typed")
        self.assertEqual(self.backends.credentials("ollama")[0], "ollama")


class PinnedBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backends = RagBackends()

    def resolve_with(self, stored_key: str, ollama: tuple[bool, list[str]]):
        with mock.patch.object(query_ui, "_load_api_key", return_value=stored_key), \
             mock.patch.object(self.backends, "ollama_models", return_value=ollama):
            return self.backends.resolve()

    def test_a_pin_overrides_a_present_key(self) -> None:
        # A reader with a working key can still force the local model, which
        # costs nothing and needs no network.
        self.backends.set_backend("ollama")
        self.assertEqual(self.resolve_with("nvapi-stored", (True, []))["backend"], "ollama")

    def test_a_pin_to_nvidia_without_a_key_still_refuses(self) -> None:
        self.backends.set_backend("nvidia")
        state = self.resolve_with("", (True, ["llama3.2"]))
        self.assertEqual(state["backend"], "none")
        self.assertIn("RAG_BACKEND=nvidia", state["reason"])

    def test_auto_hands_the_choice_back(self) -> None:
        self.backends.set_backend("nvidia")
        self.backends.set_backend("auto")
        self.assertEqual(self.resolve_with("nvapi-stored", (False, []))["backend"], "nvidia")

    def test_an_unknown_name_is_treated_as_auto(self) -> None:
        self.backends.set_backend("gemini")
        self.assertEqual(self.resolve_with("nvapi-stored", (False, []))["backend"], "nvidia")


class OllamaProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backends = RagBackends()

    def test_a_refused_connection_reads_as_unreachable(self) -> None:
        import urllib.error
        with mock.patch.object(query_ui.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            self.assertEqual(self.backends._probe_ollama(), (False, []))

    def test_a_pulled_model_is_listed(self) -> None:
        import io
        import json
        body = json.dumps({"data": [{"id": "llama3.2"}, {"id": "qwen2.5"}]}).encode()
        response = mock.MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        with mock.patch.object(query_ui.urllib.request, "urlopen", return_value=response):
            reachable, models = self.backends._probe_ollama()
        self.assertTrue(reachable)
        self.assertEqual(models, ["llama3.2", "qwen2.5"])

    def test_a_malformed_body_reads_as_unreachable(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b"not json"
        response.__enter__.return_value = response
        with mock.patch.object(query_ui.urllib.request, "urlopen", return_value=response):
            self.assertEqual(self.backends._probe_ollama(), (False, []))

    def test_the_result_is_reused_within_the_ttl(self) -> None:
        # Every question would otherwise pay a connect attempt against a server
        # that is not there.
        with mock.patch.object(self.backends, "_probe_ollama",
                               return_value=(False, [])) as probe:
            self.backends.ollama_models()
            self.backends.ollama_models()
        probe.assert_called_once()

    def test_a_refresh_reprobes(self) -> None:
        # Starting ``ollama serve`` after the UI is already up should be noticed
        # without a restart.
        with mock.patch.object(self.backends, "_probe_ollama",
                               return_value=(False, [])) as probe:
            self.backends.ollama_models()
            self.backends.ollama_models(refresh=True)
        self.assertEqual(probe.call_count, 2)


class ErrorMessageTest(unittest.TestCase):
    def test_a_refused_key_explains_the_next_step(self) -> None:
        backends = mock.Mock()
        backends.reject_key.return_value = "the key in .env or the environment"
        backends.resolve.return_value = {"backend": "none"}
        with mock.patch.object(query_ui, "BACKENDS", backends):
            message = query_ui._explain_api_error(
                mock.Mock(status_code=403), {"backend": "nvidia"}
            )
        backends.reject_key.assert_called_once()
        self.assertIn("build.nvidia.com", message)
        self.assertIn("graph explorer below does not need it", message)

    def test_an_unreachable_local_server_names_the_command(self) -> None:
        exc = type("APIConnectionError", (Exception,), {})()
        with mock.patch.object(query_ui, "BACKENDS", mock.Mock()):
            message = query_ui._explain_api_error(exc, {"backend": "ollama"})
        self.assertIn("ollama serve", message)
        self.assertIn(query_ui.OLLAMA_MODEL, message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
