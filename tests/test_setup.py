"""Tests for :mod:`setup`, the first-run key helper.

The key this writes must never reach a tracked file, and the helper must not
change a setting the reader did not ask it to. Both were wrong at once: ``--check``
is documented as verify-only but rewrote ``.env`` and pinned RAG_BACKEND, which
silently stopped the UI falling back to a local model when the key was later gone.

Run with::

    .venv/bin/python -m unittest test_setup
"""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setup as setup_module


FAKE_KEY = "nvapi-0000000000000000000000000000fake"


class EnvWritingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        self.patches = [mock.patch.object(setup_module, "ENV_PATH", self.env)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # The key must come from the temp .env, not the real one or the real
        # environment, or these tests read and potentially rewrite it.
        for name in ("NVIDIA_API_KEY", "OPENAI_API_KEY", "NVIDIA_MODEL", "NVIDIA_BASE_URL"):
            patch = mock.patch.dict(os.environ, {name: ""})
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = setup_module.main(argv)
        return code, buffer.getvalue()

    def test_a_key_is_written_with_mode_restricted(self) -> None:
        self.env.write_text(f"NVIDIA_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        self.run_main(["--check", "--no-verify"])
        self.assertIn("NVIDIA_API_KEY", self.env.read_text(encoding="utf-8"))
        self.assertEqual(oct(self.env.stat().st_mode)[-3:], "600")

    def test_check_does_not_pin_the_backend(self) -> None:
        # The regression: --check says it only verifies, but it rewrote the file
        # and pinned RAG_BACKEND=nvidia, so a later missing key refused to fall
        # back to a local model instead.
        self.env.write_text(f"NVIDIA_API_KEY={FAKE_KEY}\nRAG_BACKEND=auto\n", encoding="utf-8")
        self.run_main(["--check", "--no-verify"])
        self.assertIn("RAG_BACKEND=auto", self.env.read_text(encoding="utf-8"))

    def test_check_does_not_erase_an_existing_pin(self) -> None:
        self.env.write_text(f"NVIDIA_API_KEY={FAKE_KEY}\nRAG_BACKEND=ollama\n", encoding="utf-8")
        self.run_main(["--check", "--no-verify"])
        self.assertIn("RAG_BACKEND=ollama", self.env.read_text(encoding="utf-8"))

    def test_a_fresh_write_leaves_the_backend_on_auto(self) -> None:
        with mock.patch("getpass.getpass", return_value=FAKE_KEY):
            self.run_main(["--no-verify"])
        self.assertIn("RAG_BACKEND=auto", self.env.read_text(encoding="utf-8"))

    def test_a_second_run_refuses_to_overwrite_a_key_silently(self) -> None:
        # Otherwise a re-run cannot replace a rotated key, and the reader has to
        # delete the file by hand with no hint that they should.
        self.env.write_text(f"NVIDIA_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        code, out = self.run_main(["--no-verify"])
        self.assertEqual(code, 1)
        self.assertIn("--check", out)

    def test_an_implausible_key_is_refused_and_nothing_is_written(self) -> None:
        with mock.patch("getpass.getpass", return_value="sk-not-an-nvidia-key"):
            code, out = self.run_main([])
        self.assertEqual(code, 1)
        self.assertFalse(self.env.exists())
        self.assertIn("build.nvidia.com", out)

    def test_the_key_is_never_printed(self) -> None:
        self.env.write_text(f"NVIDIA_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        with mock.patch.object(setup_module, "check_key", return_value=(True, "OK")):
            code, out = self.run_main(["--check"])
        self.assertEqual(code, 0)
        self.assertNotIn(FAKE_KEY, out)

    def test_no_key_means_nothing_is_written(self) -> None:
        code, out = self.run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertFalse(self.env.exists())
        self.assertIn("local model", out)


class KeyShapeTest(unittest.TestCase):
    def test_an_nvidia_key_is_recognised(self) -> None:
        self.assertTrue(setup_module.looks_like_a_key(FAKE_KEY))

    def test_other_providers_keys_are_refused(self) -> None:
        for value in ("sk-abc", "gsk_abc", "AIzaSy", "nvapi-short", ""):
            self.assertFalse(setup_module.looks_like_a_key(value), value)

    def test_a_refused_key_still_gets_saved(self) -> None:
        # A key with no inference entitlement is still the right key for the
        # reader to keep and rotate; refusing to save it would mean re-pasting.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with mock.patch.object(setup_module, "check_key", return_value=(False, "403 refused")), \
                 mock.patch.object(setup_module, "ENV_PATH", path), \
                 mock.patch("getpass.getpass", return_value=FAKE_KEY):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = setup_module.main([])
            self.assertEqual(code, 1)
            self.assertIn("NVIDIA_API_KEY", path.read_text(encoding="utf-8"))
            self.assertIn("403 refused", out.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
