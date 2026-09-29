"""The answer panel must not disagree with the grader.

The grader in :mod:`sandbox_engine.provenance` is the only thing in this
repository that decides what an answer was allowed to say. It runs on every
request, it produces a per-sentence verdict, an ungrounded-figure list, a
misattribution list and a plain-English violations summary, and all of it was
serialised into the ``/api/ask`` response and read by nobody.

The symptom was not a crash. It was that the one badge which *looked* like a
verdict was drawn from ``bool(used_tags)`` -- "did the model emit a tag the
retriever issued" -- so an answer the grader called a fabrication rendered
green, and ``violations()``, written in prose explicitly for a reader, reached
no reader.

Nothing about that failure is visible in a test, so it is pinned here. The
payload is parsed out of the source rather than hand-listed, so the guard
tracks the code instead of drifting from it.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_engine import query_ui

#: The fields the grader produces. Every one of them must reach a screen, or the
#: grader is decoration. A field absent from this set is not protected, so
#: adding a grader output means adding it here too.
GRADER_KEYS = frozenset({
    "provenance",       # the per-sentence Verdict records
    "provenance_mix",   # the tag histogram
    "violations",       # violations(), the reader-facing summary
    "ungrounded_figures",
    "misattributed",
    "invented_tags",
    "gap",
    "verdict",          # provenance_verdict(), added with the panel
})


def _ask_rag_payloads() -> tuple[set[str], set[str]]:
    """The key sets of the two dicts ``ask_rag`` returns.

    Parsed with ``ast`` rather than found by regex: the invariant is about the
    payload, and a regex that quietly stops matching after an unrelated edit
    would turn this file into a comment. The success payload is the last
    ``return``; the error stub is the first.
    """
    source = (Path(query_ui.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "ask_rag"
    )
    dicts = [
        {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    if len(dicts) < 2:
        raise AssertionError(
            f"ask_rag returns {len(dicts)} dict literal(s); expected a success "
            "payload and an error stub"
        )
    return dicts[0], dicts[1]


def _app_code() -> str:
    """The embedded app with ``//`` line comments removed.

    A source-level assertion that scans the raw text matches prose. The first
    version of the ``showTab`` check failed on the comment explaining which
    literal had been removed -- the test working exactly as badly as the code it
    guards. Comments say what changed; only code says what runs.
    """
    return re.sub(r"^\s*//.*$", "", query_ui._HTML, flags=re.MULTILINE)


def _is_read(key: str) -> bool:
    """Whether the embedded app accesses *key* as a property.

    Property access rather than a bare substring. A substring test passes on a
    key that happens to appear inside an unrelated word, which is a guard that
    stops guarding, and the whole point of this file is that it keeps guarding.
    """
    app = _app_code()
    return bool(
        re.search(rf"\.{re.escape(key)}\b", app)
        or re.search(rf"""\[\s*["']{re.escape(key)}["']\s*\]""", app)
    )


class GraderOutputIsRenderedTests(unittest.TestCase):
    def test_the_response_carries_every_grader_field(self):
        success, stub = _ask_rag_payloads()
        missing = GRADER_KEYS - success
        self.assertEqual(missing, set(), f"not in the success payload: {sorted(missing)}")
        missing = GRADER_KEYS - stub
        self.assertEqual(
            missing, set(),
            f"not in the error stub, so an errored request renders undefined: {sorted(missing)}",
        )

    def test_every_grader_field_is_read_by_the_panel(self):
        unread = sorted(key for key in GRADER_KEYS if not _is_read(key))
        self.assertEqual(
            unread, [],
            "the grader produced these and no screen shows them: "
            f"{unread}. Either render them, or say here why not.",
        )

    def test_the_verdict_chip_is_not_drawn_from_whether_tags_were_emitted(self):
        """``res.grounded`` is ``bool(used_tags)`` and is not a verdict.

        It answers "did the model cite something the retriever issued", which a
        model answering confidently and wrongly satisfies.

        Asserted on the *assignment* rather than on the presence of
        ``res.verdict`` anywhere: the first version of this test passed with the
        chip wired to ``res.grounded``, because the panel and the live region
        also mention ``res.verdict`` and the check did not look at which one fed
        the badge.
        """
        app = _app_code()
        assignment = re.search(r"const\s+verdict\s*=\s*([^;]+);", app)
        self.assertIsNotNone(assignment, "the chip no longer computes a verdict")
        expression = assignment.group(1)
        self.assertIn("res.verdict", expression, f"the chip reads {expression!r}")
        self.assertNotIn("res.grounded", expression, f"the chip reads {expression!r}")

    def test_the_stub_and_the_success_payload_differ_only_in_optional_detail(self):
        """Every key the error stub carries must also be in the success payload.

        Not literal equality: the stub has no ``elapsed_sec`` and the client
        guards that. What must hold is one direction -- the stub may omit
        optional detail, but must never carry a key the success path lacks,
        because that key is read unconditionally somewhere.
        """
        success, stub = _ask_rag_payloads()
        self.assertEqual(stub - success, set())

    def test_every_field_the_panel_reads_is_actually_sent(self):
        """The nested direction: what the panel reads must reach the wire.

        The top-level check asks whether a key is read; this asks the reverse
        for the per-sentence records. ``unknown_cites`` was dropped at the
        payload boundary while the panel read it, and nothing noticed, because
        "is the key read" and "is the key sent" are different questions and only
        the first was being asked.
        """
        app = _app_code()
        # Scoped to the function that owns the loop, and matched as a member of
        # the loop variable. A bare `v\.(\w+)` over the whole page also picks up
        # any other single-letter `v` in scope -- `v.toLocaleString` was the
        # first one -- and then the guard reports a field that was never a field.
        start = app.find("function renderProvenance")
        self.assertNotEqual(start, -1, "renderProvenance not found")
        end = app.find("\nfunction ", start + 10)
        body = app[start : end if end != -1 else len(app)]
        self.assertRegex(
            body, r"for\s*\(\s*const\s+v\s+of\s+", "the verdict loop is gone"
        )
        read = set(re.findall(r"\bv\.(\w+)", body))
        self.assertIn("text", read, "no per-sentence fields found; guard is inert")
        source = Path(query_ui.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "ask_rag"
        )
        # The per-verdict dict is the one carrying the grader's "reason".
        verdict_dicts = [
            {k.value for k in node.keys if isinstance(k, ast.Constant)}
            for node in ast.walk(fn)
            if isinstance(node, ast.Dict)
            and any(getattr(k, "value", None) == "reason" for k in node.keys)
        ]
        self.assertEqual(len(verdict_dicts), 1, "expected exactly one verdict dict")
        self.assertEqual(
            read - verdict_dicts[0], set(),
            "the panel reads fields the payload does not send: "
            f"{sorted(read - verdict_dicts[0])}",
        )


class PanelStructureTests(unittest.TestCase):
    def test_the_answer_tab_shows_the_violations_visibly(self):
        """``violations()`` must appear on screen, not only be announced.

        Pinned on the *guard*, not on the read. The same key is read by the
        live region as well, so "is it read" passes with the visible banner
        deleted -- verified, along with three other holes, by deleting the work
        and watching the suite stay green. A reader who is not using a screen
        reader would then be told nothing at all, which is the case the banner
        exists for.

        So this asserts the exact structure: a ``.violations`` block, gated on
        the list being non-empty, inside the answer renderer. Literal, and
        deliberately so -- a reachability claim about JavaScript cannot be made
        from a source scan any other way, and this is the cheapest honest one.
        """
        app = _app_code()
        start = app.find("function renderAnswer")
        self.assertNotEqual(start, -1, "renderAnswer not found")
        end = app.find("\nfunction ", start + 10)
        body = app[start : end if end != -1 else len(app)]
        self.assertIn(
            "res.violations", body,
            "the answer tab no longer reads res.violations; the banner is gone",
        )
        self.assertIn(
            'className = "violations"', body, "no violations block is built"
        )
        self.assertRegex(
            body, r"if\s*\(\s*violations\.length\s*\)",
            "the violations block is no longer gated on there being any",
        )
        self.assertIn(
            "answer.appendChild(box)", body,
            "the violations block is built but never shown",
        )

    def test_every_tab_button_has_a_panel_and_the_switcher_finds_them(self):
        """``showTab`` used to hold its own hardcoded panel list.

        A tab missing from that array silently never opens, so the list is
        derived from the buttons and this asserts the two still agree.
        """
        app = _app_code()
        tabs = re.findall(r'data-tab="([a-z]+)"', app)
        self.assertTrue(tabs, "no tabs found in the embedded app")
        for name in tabs:
            self.assertIn(
                f'id="tab{name.capitalize()}"', app,
                f'tab "{name}" has no panel with the id showTab would look for',
            )
        self.assertNotRegex(
            app, r'\["answer",\s*"sources",\s*"trace"\]',
            "showTab must derive its panel list from the tab buttons",
        )

    def test_the_provenance_panel_explains_the_gap_substitution(self):
        """When the corpus does not support the answer, the text is replaced.

        ``ask_rag`` swaps the model's prose for a rendered refusal and clears
        the used tags, while the verdicts still describe what the model wrote.
        Without a line saying so, the panel shows sentences the answer above
        does not contain, and a reader concludes nothing was checked.
        """
        self.assertIn("replaced", query_ui._HTML.lower())

    def test_the_provenance_panel_is_cleared_when_a_question_errors(self):
        """``renderAnswer`` runs only on the success path.

        Left alone, a second question that errors would show the *first*
        question's verdicts, attributed to nothing. Asserted inside the
        ``catch`` block specifically: the same call is also made before the
        request goes out, so a coarse "is it called anywhere" check passes with
        the error path empty.
        """
        app = _app_code()
        start = app.find("async function askQuestion")
        self.assertNotEqual(start, -1, "askQuestion not found")
        end = app.find("\nfunction ", start + 10)
        body = app[start : end if end != -1 else len(app)]
        catch = re.search(r"catch\s*\([^)]*\)\s*\{(.*?)\}\s*finally", body, re.S)
        self.assertIsNotNone(catch, "askQuestion has no catch/finally pair")
        self.assertIn(
            "clearProvenance", catch.group(1),
            "a failed question leaves the previous question's verdicts on screen",
        )


class EscapingTests(unittest.TestCase):
    def test_model_prose_is_not_written_through_innerhtml(self):
        """A verdict's text and reason are model output.

        The answer body is already ``esc``-wrapped, and the panel has to be
        too. This is a crude source-level check because there is no JS test
        runner here to check it properly, but it is better than nothing and it
        fails the moment someone concatenates a model string into markup.
        """
        app = query_ui._HTML
        start = app.find("function renderProvenance")
        self.assertNotEqual(start, -1, "renderProvenance not found")
        end = app.find("\nfunction ", start + 10)
        body = app[start : end if end != -1 else len(app)]

        sinks = re.findall(r"\.innerHTML\s*=\s*([^;]+);", body)
        self.assertTrue(
            sinks, "renderProvenance writes no markup at all, so this guards nothing"
        )
        for expression in sinks:
            self.assertRegex(
                expression,
                r"esc\(",
                f"innerHTML in renderProvenance is not escaped: {expression[:100]}",
            )


if __name__ == "__main__":
    unittest.main()
