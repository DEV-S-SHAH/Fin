"""The Phase 0 eval set, executable.

``EVAL_SET.md`` is a specification: 30 questions, and for each one the
**provenance mix** the answer must carry -- not just the right number. It
defines a headline metric, ``provenance_match_rate``, and the score that decides
whether the contract holds. Nothing ran it. D5 in the defect register says why
that mattered: the benchmark suite went 5/5 green with D1-D3 present, because
nothing executed the questions those defects broke.

So the questions live here as the only copy that is *run*, and the markdown stays
the source of truth -- it is parsed, not transcribed. A second hand-typed list
would drift from the document, and a document nothing reads cannot be corrected.

Two halves, deliberately separate:

* **Offline** -- :func:`parse_eval_set` and :func:`score` are pure. They turn the
  document into questions and turn answers into the metrics, and they need no
  model, no backend and no database. This is the half the tests run.
* **Live** -- :func:`run` asks the system each question and scores the replies.
  It needs a reachable backend, so it is a command, not a test.

Provenance mix matching
-----------------------

Expected tags are a *set*, and the comparison is exact set equality. An answer
that is right but tagged ``STATED`` where ``GAP`` was required is a failure, and
a set comparison is what says that: it fails on a missing tag and on a tag the
contract did not ask for. Where the document says ``STATED or GAP`` the row
carries two acceptable sets and either matching one passes, because the document
is explicitly offering the choice.

Secondary metrics
-----------------

The headline is not the whole score. An answer can carry the right tags and
still be wrong in the ways the contract exists to prevent: a number that is in
no cited source, a factual sentence with no citation, a figure attributed to an
issuer the cited sources were not filed by. Those are counted separately and are
targets of zero, not ratios, because there is no acceptable number of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .provenance import DERIVED, EXTERNAL, GAP, INFERRED, REFUSED, STATED

__all__ = [
    "EVAL_SET_PATH",
    "TAGS",
    "EvalAnswer",
    "EvalQuestion",
    "EvalScore",
    "load_eval_set",
    "parse_eval_set",
    "provenance_match_rate",
    "run",
    "score",
]

EVAL_SET_PATH = Path(__file__).resolve().parent / "EVAL_SET.md"

TAGS = (STATED, DERIVED, INFERRED, EXTERNAL, GAP)

#: A table row: | S1 | question | expected | accept when | status |
_ROW = re.compile(r"^\|(?P<body>.*)\|\s*$")


def _cells(body: str) -> list[str]:
    """Split a table row's interior into trimmed cells.

    *body* is the row with its outer pipes removed, so every pipe left in it is
    a cell boundary. A cell cannot itself contain one -- a pipe would have
    ended the cell when the markdown was written -- so a plain split is exact
    and does not need a regex to find boundaries that may not be there.
    """
    return [cell.strip() for cell in body.split("|")]


@dataclass(frozen=True)
class EvalQuestion:
    """One row of the eval set.

    ``acceptable`` is a set of *tag sets*. A row that expects ``STATED`` has one
    entry; a row written ``STATED or GAP`` has two, and either one matching
    passes. Comparing a single set instead would make an "or" row unpassable.
    """

    id: str
    question: str
    expected: str
    acceptable: frozenset[frozenset[str]]
    accept_when: str
    status: str
    section: str

    @property
    def tags(self) -> frozenset[str]:
        """The tags the document names, as one set. The first alternative."""
        return next(iter(self.acceptable))

    def matches(self, emitted: Iterable[str]) -> bool:
        """Whether an emitted tag mix is one this row accepts."""
        return frozenset(emitted) in self.acceptable


@dataclass(frozen=True)
class EvalAnswer:
    """What the system said to one question, and how it was tagged."""

    question_id: str
    tags: frozenset[str] = frozenset()
    answer: str = ""
    #: The grader's one-word verdict for the answer as a whole. Empty means the
    #: server did not report one -- not "supported". Defaulting a missing
    #: judgement to the best outcome is the bug this field exists to expose, so
    #: an absent verdict is counted as unknown and never as a pass.
    verdict: str = ""
    ungrounded_figures: tuple[str, ...] = ()
    uncited_sentences: tuple[str, ...] = ()
    misattributed: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class EvalScore:
    """The metrics the document's Scoring section names."""

    total: int
    matched: int
    #: fraction of questions whose emitted tag mix matched the expected one
    provenance_match_rate: float
    #: rows whose tags were wrong, with what was emitted against what was needed
    mismatches: list[tuple[str, frozenset[str], frozenset[frozenset[str]]]] = field(
        default_factory=list
    )
    #: every figure emitted that was in no cited source -- target 0
    ungrounded_figures: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    #: every factual sentence emitted with no citation -- target 0
    uncited_sentences: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    #: every figure attributed to an issuer the cited sources were not filed by
    misattributed: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    #: how many answers the grader refused, counted apart from the tag mix.
    #: Deliberately *not* folded into ``provenance_match_rate``: the rate asks
    #: whether the tags were what the document expected, and an answer can carry
    #: exactly those tags and still be a fabrication. A run has to be able to
    #: report both without either contaminating the other.
    refused: int = 0
    #: answers whose verdict the server did not report at all
    unreported_verdict: int = 0
    #: rows the runner could not ask at all, so were never scored
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"provenance_match_rate  {self.matched}/{self.total} "
            f"= {self.provenance_match_rate:.3f}",
            f"refused by the grader  {self.refused}  (of {self.total - len(self.errors)})",
            f"ungrounded figures     {len(self.ungrounded_figures)}  (target 0)",
            f"uncited sentences      {len(self.uncited_sentences)}  (target 0)",
            f"misattributed issuers  {len(self.misattributed)}  (target 0)",
        ]
        if self.unreported_verdict:
            lines.append(
                f"verdict not reported   {self.unreported_verdict}  "
                "(the server did not send one; these are not passes)"
            )
        if self.errors:
            lines.append(f"not answered           {len(self.errors)}")
        for qid, emitted, acceptable in self.mismatches:
            wanted = " | ".join(
                "+".join(sorted(alt)) for alt in sorted(acceptable, key=sorted)
            )
            lines.append(f"  {qid:5} emitted {'+'.join(sorted(emitted)) or '-':24} wanted {wanted}")
        return "\n".join(lines)


def load_eval_set(path: str | Path | None = None) -> list[EvalQuestion]:
    """The eval set, parsed from :data:`EVAL_SET_PATH` or *path*."""
    target = Path(path) if path is not None else EVAL_SET_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"eval set not found at {target}; it is part of the package, so a "
            "missing file is a broken install rather than an unbuilt corpus"
        )
    return parse_eval_set(target.read_text(encoding="utf-8"))


def parse_eval_set(markdown: str) -> list[EvalQuestion]:
    """Read the questions out of the document's tables.

    A row is a five-cell line whose first cell looks like an id (``S1``,
    ``D-a5``, ``G7``). Everything else in the file -- prose, the defect register,
    the scoring section -- is ignored, so the document can be written for a
    reader without the parser caring.
    """
    questions: list[EvalQuestion] = []
    section = ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            section = line[3:].split("—")[0].strip()
            continue
        match = _ROW.match(line.strip())
        if not match:
            continue
        cells = _cells(match.group("body"))
        if len(cells) < 5 or not re.fullmatch(r"[A-Z]+-?a?\d+", cells[0]):
            continue
        qid, question, expected, accept_when, status = cells[:5]
        acceptable = _acceptable(expected)
        unknown = set().union(*acceptable) - set(TAGS)
        if unknown:
            raise ValueError(
                f"{qid} expects {sorted(unknown)}, which is not a provenance tag; "
                f"known tags are {list(TAGS)}"
            )
        questions.append(
            EvalQuestion(
                id=qid,
                question=question,
                expected=expected,
                acceptable=acceptable,
                accept_when=accept_when,
                status=status,
                section=section,
            )
        )
    if not questions:
        raise ValueError("no eval rows found: the document's tables have changed shape")
    return questions


def _acceptable(expected: str) -> frozenset[frozenset[str]]:
    """Parse an expected-tags cell into the sets it accepts.

    ``"STATED"`` is one set. ``"STATED or GAP"`` is two, because the document is
    offering the choice and a runner that only ever compared against the first
    would score a correct answer as a failure.
    """
    alternatives = re.split(r"\s+or\s+", expected, flags=re.IGNORECASE)
    return frozenset(
        frozenset(tag.strip() for tag in alt.split("+") if tag.strip())
        for alt in alternatives
    )


def provenance_match_rate(
    questions: Sequence[EvalQuestion],
    answers: Mapping[str, EvalAnswer],
) -> tuple[int, int]:
    """The headline metric: how many answers carried the expected tag mix.

    Returns ``(matched, scored)``. An answer the runner failed to obtain is
    counted in the denominator and cannot match -- a question nobody answered is
    not a question that passed, and dropping it would let a broken run report a
    perfect score by answering nothing.
    """
    matched = 0
    for question in questions:
        answer = answers.get(question.id)
        if answer is not None and answer.ok and question.matches(answer.tags):
            matched += 1
    return matched, len(questions)


def score(
    questions: Sequence[EvalQuestion],
    answers: Mapping[str, EvalAnswer],
) -> EvalScore:
    """Every metric the document's Scoring section names, from one set of answers."""
    matched, total = provenance_match_rate(questions, answers)
    result = EvalScore(
        total=total,
        matched=matched,
        provenance_match_rate=(matched / total) if total else 0.0,
    )
    for question in questions:
        answer = answers.get(question.id)
        if answer is None:
            result.errors.append((question.id, "never asked"))
            continue
        if not answer.ok:
            result.errors.append((question.id, answer.error or "failed"))
            continue
        if not question.matches(answer.tags):
            result.mismatches.append((question.id, answer.tags, question.acceptable))
        if answer.ungrounded_figures:
            result.ungrounded_figures.append((question.id, answer.ungrounded_figures))
        if answer.uncited_sentences:
            result.uncited_sentences.append((question.id, answer.uncited_sentences))
        if answer.misattributed:
            result.misattributed.append((question.id, answer.misattributed))
        if answer.verdict == REFUSED:
            result.refused += 1
        elif not answer.verdict:
            result.unreported_verdict += 1
    return result


def run(
    ask: Callable[[str], Mapping[str, Any]],
    questions: Sequence[EvalQuestion] | None = None,
    *,
    verbose: bool = False,
) -> EvalScore:
    """Ask every question and score the answers.

    *ask* takes a question and returns a ``/api/ask`` response body -- in
    production, ``lambda q: ask_rag(q)``. It is a parameter so the loop can be
    exercised without a backend: the interesting half of an eval is the scoring,
    and scoring must not be the half that needs a network.

    The per-sentence tag mix is read from the response's ``provenance`` block,
    which the grader fills in, so what is scored is what the grader decided and
    not a re-derivation of it here.
    """
    questions = list(questions) if questions is not None else load_eval_set()
    answers: dict[str, EvalAnswer] = {}
    for question in questions:
        try:
            response = ask(question.question)
        except Exception as exc:  # noqa: BLE001 - a failed question is a scored gap
            answers[question.id] = EvalAnswer(
                question_id=question.id, error=f"{type(exc).__name__}: {exc}"
            )
            if verbose:
                print(f"{question.id:5} ERROR {exc}")
            continue
        answers[question.id] = _to_answer(question, response)
        if verbose:
            got = "+".join(sorted(answers[question.id].tags)) or "-"
            print(f"{question.id:5} {got:24} {question.question}")
    return score(questions, answers)


def _to_answer(question: EvalQuestion, response: Mapping[str, Any]) -> EvalAnswer:
    verdicts = response.get("provenance") or []
    tags = frozenset(
        str(v.get("provenance", "")) for v in verdicts if v.get("provenance")
    )
    uncited = tuple(
        str(v.get("text", ""))
        for v in verdicts
        if v.get("provenance") in (STATED, DERIVED) and not v.get("cites")
    )
    return EvalAnswer(
        question_id=question.id,
        tags=tags,
        answer=str(response.get("answer", "") or response.get("text", "")),
        # Defaulted to "" rather than to a verdict: a server that predates the
        # field, or a stub that omits it, must be reported as *unknown* and
        # never as a pass.
        verdict=str(response.get("verdict", "") or ""),
        ungrounded_figures=tuple(response.get("ungrounded_figures") or ()),
        uncited_sentences=uncited,
        misattributed=tuple(response.get("misattributed") or ()),
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live runner
    """``python -m sandbox_engine.eval_set`` -- ask all 30 and print the score.

    Needs a reachable model backend, which is why this is a command and not a
    test: the offline half above is what CI runs.
    """
    import argparse

    from .query_ui import ask_rag

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", metavar="ID", help="ask just these rows")
    parser.add_argument("--quiet", action="store_true", help="print the metrics, not the rows")
    args = parser.parse_args(argv)

    questions = load_eval_set()
    if args.only:
        wanted = {q.strip().upper() for q in args.only}
        questions = [q for q in questions if q.id.upper() in wanted]
        if not questions:
            parser.error(f"no rows matched {sorted(wanted)}")
    result = run(ask_rag, questions, verbose=not args.quiet)
    print()
    print(result.summary())
    # The headline is a ratio, so it can be good while the contract is broken:
    # an answer can carry the right tag mix and still carry a number no source
    # supports. The secondary metrics are targets of zero, not ratios, and a run
    # that misses any of them is a failure whatever the rate came out at. A run
    # with errors is worse than a failure -- nothing was measured -- so both
    # exit non-zero.
    breaches = (
        len(result.errors)
        + len(result.ungrounded_figures)
        + len(result.uncited_sentences)
        + len(result.misattributed)
    )
    if breaches:
        print(
            f"\n{breaches} contract breach(es) outside the headline rate. "
            f"The rate alone does not decide the run."
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
