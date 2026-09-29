"""Provenance contract: every fact the system can cite carries a Source.

The rule this module exists to enforce is one-directional:

    provenance is assigned by retrieval code, never by the model.

A tag is a claim about *where a number came from*. If the model chooses its own
tags, a fabricated figure can label itself ``STATED`` and the whole contract
becomes decoration. So the model is only ever allowed to *cite* tags this module
already emitted, and the final verdict per sentence is computed here, by rule,
from the evidence block and the sentence's own content. The model's own opinion
of what it said is never consulted.

Five tags, from :data:`EVAL_SET.md`:

``STATED``    read directly off a graph node, cited to its Source.
``DERIVED``   computed by arithmetic over cited facts, with the arithmetic shown.
``INFERRED``  reasoning past disclosure; must be hedged and must lean on a STATED
              sentence, never stand alone.
``EXTERNAL``  outside the corpus entirely.
``GAP``       the corpus does not cover it. A first-class answer, not a failure.

A GAP has to be *useful*: it names what is missing and where in a filing it
would be found, so the reader learns what the corpus would have to gain rather
than just being refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "STATED",
    "DERIVED",
    "INFERRED",
    "EXTERNAL",
    "GAP",
    "Source",
    "Evidence",
    "Verdict",
    "GradedAnswer",
    "SourceResolver",
    "build_evidence",
    "serialise_evidence",
    "grade_answer",
    "render_gap",
    "normalise_number",
    "extract_figures",
    "citation_tags",
    "LEGAL_NAME_NOISE",
    "issuer_forms",
    "issuer_in_text",
    "SUPPORTED",
    "QUALIFIED",
    "REFUSED",
    "provenance_verdict",
]

STATED = "STATED"
DERIVED = "DERIVED"
INFERRED = "INFERRED"
EXTERNAL = "EXTERNAL"
GAP = "GAP"

_ALL_TAGS = (STATED, DERIVED, INFERRED, EXTERNAL, GAP)

#: The three states an *answer* can be in, as distinct from the five states a
#: *sentence* can be in. An answer is a collection of sentences, and the
#: collection has to be reduced to one thing a reader can be told, because a
#: reader cannot act on a histogram.
SUPPORTED = "SUPPORTED"
QUALIFIED = "QUALIFIED"
REFUSED = "REFUSED"

# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

#: What may sit between two tags inside one pair of brackets. The comma is the
#: list form the grader accepts; the arrows are the pairing form the model
#: writes to put a line item next to its value.
_CITATION_SEP = r"\s*(?:,|[-]{1,2}(?:>|→)|=>|→)\s*"

#: One grammar for a citation, in every form a model writes one: a bare tag
#: ``[E1]``, a list ``[E1, E3]``, and a pairing ``[E2->E15]`` or ``[E2 -> E15]``.
#:
#: It has to be *one*. It was three -- the mask in :func:`extract_figures`, the
#: grader's own pattern, and ``query_ui._CITATION_RE`` -- and they had drifted
#: apart, which is only visible in the two ways that matter: the mask did not
#: cover the comma form, so ``[E158, E200]`` reported the figures ``158`` and
#: ``200`` and failed a perfectly good sentence as fabrication, while the UI's
#: pattern did not cover it either and read a correctly cited sentence as
#: citing nothing at all. The three callers now share this one.
_CITATION = re.compile(rf"\[(E\d+(?:{_CITATION_SEP}E\d+)*)\]")


def citation_tags(text: str) -> list[str]:
    """Every tag cited in *text*, in order of first appearance, once each.

    Split on the very separator the pattern above was built from, so what this
    returns and what :func:`extract_figures` blanks out cannot disagree about
    where one citation ends and the next begins.
    """
    found: list[str] = []
    for group in _CITATION.findall(text or ""):
        for part in re.split(_CITATION_SEP, group):
            part = part.strip()
            if part and part not in found:
                found.append(part)
    return found


#: Where a sentence says it did arithmetic. Without one of these, a number that
#: is not in the evidence is not a derivation, it is an invention.
_ARITH = re.compile(
    r"(?:[-−+*/×÷]\s*\d)|(?:=\s*\d)|(?:\d+\s*(?:/|÷)\s*\d)"
    r"|\b(?:subtract|subtracted|minus|divided by|times|less|equals|computed|"
    r"calculated|derived from|which is|giving|therefore|implies a)\b",
    re.IGNORECASE,
)

#: Hedges. An unhedged forward-looking claim is a GAP; a hedged one may be
#: INFERRED, but only while it leans on something STATED.
_HEDGE = re.compile(
    r"\b(?:may|might|could|would|likely|unlikely|suggests?|suggesting|implies?|"
    r"appears?|seems?|potentially|arguably|plausibly|perhaps|possibly|"
    r"tends? to|is likely|is expected|points? to|suggests that)\b",
    re.IGNORECASE,
)

#: Claims that can only be settled by something outside the filed corpus.
_EXTERNAL_MARKERS = re.compile(
    r"\b(?:news(?:wire)?|this week|today|yesterday|press (?:release|reports?)|"
    r"analyst|consensus estimate|market share|third[- ]party|"
    r"according to (?:reports|news|analysts)|currently|as of today)\b",
    re.IGNORECASE,
)

#: Numbers that are structural rather than reported: form types, fiscal years,
#: item codes, dates, and small counts. Treating these as "figures" would flag
#: every correctly-cited sentence that happens to say "the 10-K".
_STRUCTURAL = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}"          # ISO date
    r"|FY\d{2,4}"                  # fiscal year
    r"|\d{1,2}[KQ]"                # 10-K, 10-Q, 8-K -> handled below too
    r"|\d{4}$"                     # a bare year
    r"|0\d{6,9}$"                  # a zero-padded CIK (10 digits)
    r"|\d{1,2}$"                   # an item code / a small ordinal
    r")$"
)
_FORM_TOKEN = re.compile(r"(?:10-[KQ]|8-K|10-K/A|10-Q/A)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def normalise_number(token: str) -> float | None:
    """Parse a figure written the way answers write them.

    ``$416,161M``, ``416,161``, ``31.97%`` and ``1.2B`` are the same kind of
    thing, and the evidence usually spells them differently from the answer.
    Comparing raw strings would flag a correct citation as fabricated, which
    would make the contract cry wolf and get switched off.
    """
    if token is None:
        return None
    t = str(token).strip()
    if not t:
        return None
    t = _FORM_TOKEN.sub("", t)                    # 10-K -> ''
    negative = t.startswith(("(", "-", "−"))
    t = re.sub(r"[^\d.]", "", t.replace(",", ""))
    if not t or t == ".":
        return None
    try:
        val = float(t)
    except ValueError:
        return None
    return -val if negative else val


def extract_figures(text: str) -> list[str]:
    """Numeric tokens in ``text``, minus the structural ones.

    A ``10-K``, an ``FY2026``, an ``Item 1`` and a filing date are not reported
    figures and must not be checked against the evidence; a CIK is an
    identifier, and a one-or-two digit token is far more often an item code
    than a number someone reported.
    """
    if not text:
        return []
    # A citation tag is a pointer this module issued, not a claim about
    # magnitude, so it has to be masked before tokenising. Otherwise ``[E158]``
    # yields the figure ``158``, which no evidence item can ever ground, and a
    # perfectly good sentence is rejected as fabrication. The list and pairing
    # forms are masked by the same grammar the grader parses tags with, because
    # a mask that recognises fewer citations than the grader does turns a
    # correct answer into a fabrication report.
    masked = _CITATION.sub(" ", text)
    # Dates have to go before tokenising: ``2025-10-31`` otherwise yields the
    # two figures ``-10`` and ``-31``, and an answer that merely mentions when
    # a filing was filed would be reported as inventing numbers.
    masked = re.sub(r"\d{4}-\d{2}-\d{2}", " ", masked)
    masked = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", masked)


    out: list[str] = []
    for raw in re.findall(r"\(?-?[\d][\d,]*\.?\d*\)?%?", masked):
        # A thousands separator is not a trailing comma in the sentence: "was
        # 120,451, in Europe" must yield 120451 and not "120,451,".
        stripped = raw.strip().rstrip(",")
        core = stripped.strip("()%").replace(",", "")
        if not core or _FORM_TOKEN.fullmatch(stripped):
            continue
        if _STRUCTURAL.match(core):
            continue
        out.append(stripped)
    return out


def _figure_keys(text: str) -> set[float]:
    """Every figure in ``text`` as a comparable float."""
    keys: set[float] = set()
    for tok in extract_figures(text):
        v = normalise_number(tok)
        if v is not None:
            keys.add(v)
    return keys


_SCALE_STEPS = (1.0, 1e3, 1e6, 1e9)


def _restates(figure: str, grounded: set[float]) -> bool:
    """Is ``figure`` a rounded restatement of a grounded figure?

    A filed figure is reported in the unit the filing used, and a model
    restating it in a friendlier unit -- 416,161 million as "$416.2 billion" --
    is repeating the cited fact, not inventing one. Compared numerically with no
    unit or rounding awareness, that reads as a fabrication and the whole
    answer is thrown away, which is worse than saying nothing: the model was
    right and cited the right item.

    Matched on purpose only when the grounded value rounds to the figure *at
    the precision the figure itself shows*, so "$416.2 billion" is supported by
    416161 while "$999.9 billion" is not, and a claimed precision the source
    cannot support ("416.16123 billion") is still refused.
    """
    value = normalise_number(figure)
    if value is None or not grounded:
        return False
    core = figure.strip().rstrip("%").replace(",", "")
    if "." in core:
        decimals = len(core.split(".", 1)[1])
    else:
        decimals = 0
    if decimals > 4:
        # More precision than a filing-scale restatement can justify.
        return False
    for base in grounded:
        for step in _SCALE_STEPS:
            try:
                if round(base / step, decimals) == value:
                    return True
            except (OverflowError, ValueError):
                continue
    return False


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """Where a fact is filed. One of these exists for every citable fact.

    The fields are the ones an auditor would ask for, and every one of them is
    read off the graph rather than inferred: who filed it, which filing, what
    form, when it was filed, and which item inside it. ``accession`` is the SEC's
    own id for the document; ``section`` is the human-facing place in it.

    ``ticker`` and ``cik`` are the filer, and they are the field a corpus with
    more than one issuer cannot do without: every issuer files a 10-K, so a
    citation that names only form, fiscal year and filing date describes three
    different annual reports equally well, and a figure read off one of them
    cannot be told from a figure read off another. "How many times bigger is
    Apple's revenue than Microsoft's" is unanswerable while a citation cannot
    say whose revenue it is citing -- the model is right to refuse, because
    nothing it was shown carried the attribution.
    """

    node_id: str
    label: str
    form_type: str = ""
    filing_date: str = ""
    accession: str = ""
    item_code: str = ""
    section: str = ""
    period_end: str = ""
    fiscal_year: str = ""
    fiscal_period: str = ""
    ticker: str = ""
    cik: str = ""

    @property
    def short(self) -> str:
        bits = [b for b in (self.ticker, self.form_type, self.section or self.item_code) if b]
        return " · ".join(bits) or self.label

    def cite(self) -> str:
        """One-line reference, e.g. ``AAPL · 10-K FY2025 (FY) filed 2025-10-31, Item 1 Business``."""
        parts: list[str] = []
        if self.ticker:
            parts.append(self.ticker)
        head = self.form_type
        if self.fiscal_year:
            head += f" FY{self.fiscal_year}"
            if self.fiscal_period:
                head += f" ({self.fiscal_period})"
        if head:
            parts.append(head)
        if self.filing_date:
            parts.append(f"filed {self.filing_date}")
        if self.item_code and self.section:
            parts.append(f"Item {self.item_code} \u2014 {self.section}")
        elif self.section:
            parts.append(self.section)
        elif self.item_code:
            parts.append(f"Item {self.item_code}")
        return ", ".join(parts) or self.label

    # No ``to_dict``: a Source reaches the reader as the string it writes itself
    # in :meth:`cite`, which is what ``serialise_evidence`` and the block
    # renderer both use. A second serialisation would be a second thing to keep
    # in step with the twelve fields above, for no reader.


@dataclass
class Evidence:
    """One fact, the tag the model may cite, and the Source behind it.

    ``provenance`` is set here, by code, before the model sees anything. The
    model gets to reference ``tag``; it never gets to assign ``provenance``.
    """

    tag: str
    text: str
    source: Source
    provenance: str = STATED
    kind: str = ""

    def block(self) -> str:
        """The line as it appears in the prompt: tag first, then fact, then Source."""
        return f"[{self.tag}] {self.provenance} · {self.text}  ⟵ {self.source.cite()}"


# ---------------------------------------------------------------------------
# Who filed it
# ---------------------------------------------------------------------------

#: The legal name a filing carries and the name a person writes it under differ
#: only by a corporate suffix -- "Microsoft Corporation" is filed, "Microsoft"
#: is written -- so an issuer is matched on the distinctive remainder, with the
#: same suffixes ``query_ui`` drops when it reads a question. Shared rather than
#: repeated, for the same reason the citation grammar is: two copies of one
#: grammar is how the two readers of a citation stopped agreeing.
#:
#: A remainder of four characters or more is matched case-insensitively, because
#: at that length an ordinary word collision is not a practical worry. Below
#: four it is matched **case-sensitively** instead, which is what keeps short
#: issuer names honest without exempting them: "3M", "IBM" and "GE" are written
#: that way and are written nowhere else, whereas dropping the length floor
#: entirely would let the remainder of any three-letter name -- "GE" reduced from
#: "General Electric", or a sentence containing the word "net" -- match every
#: time it appeared. An issuer whose only distinct name is a lowercase word is
#: not detectable by name at all, and that is a limit worth stating rather than
#: papering over.
LEGAL_NAME_NOISE = re.compile(
    r"[,\.]|\s+(?:inc|corporation|corp|ltd|plc|co|company|holdings)\b",
    # Case-insensitive because the input is no longer lowercased before the
    # split: a candidate has to keep the casing it was filed with, or a short
    # alias has nothing left to be matched case-sensitively on. The pattern has
    # to see through "Apple Inc" as well as "apple inc" for the same reason.
    re.IGNORECASE,
)

#: Below this length an alias is matched against the text's original casing.
_ALIAS_CASE_SENSITIVE_BELOW = 4


def _issuer_of(ev: Evidence) -> str:
    """The issuer an evidence line belongs to, or ``""`` when it names none.

    A Company node is keyed on its own ticker, so that line already says which
    issuer it is; every other line carries the filer's ticker, read across
    ``Company-[:SUBMITTED]->Filing``. An untraced line names nobody, and that
    is not an issuer to hold a sentence against.
    """
    src = ev.source
    if ev.kind == "Company":
        return src.node_id or src.ticker
    return src.ticker


def _alias_case_sensitive(form: str) -> bool:
    return len(form) < _ALIAS_CASE_SENSITIVE_BELOW


@dataclass(frozen=True)
class _Alias:
    """One spelling the corpus knows an issuer by."""

    form: str          # lowercased, for a case-insensitive match
    originals: frozenset[str]   # as written, for a short case-sensitive match
    issuers: frozenset[str]

    @property
    def case_sensitive(self) -> bool:
        return _alias_case_sensitive(self.form)


def issuer_in_text(haystack: str, alias: _Alias, short_names: bool = True) -> bool:
    """Whether *haystack* refers to *alias*, on word boundaries.

    A long alias is matched case-insensitively, because at four characters and
    up an ordinary-word collision is not a practical worry and a reader who
    writes "ibm" means IBM. A short one is matched against the casing it was
    written with, which is the only thing that distinguishes "3M" from a word.

    ``short_names=False`` restores the length floor -- a short alias is skipped
    entirely rather than matched case-sensitively, which is the old behaviour and
    the reason "3M miles" does not scope a question to 3M. The two readers of an
    issuer name want opposite policies, and the difference is not a detail:

    * The **grader** reads an answer, which is prose about companies, and a
      short alias matching is a net win -- it catches a real misattribution to
      a filer whose only distinct name is short.
    * **Retrieval scoping** reads a question, and "traveled 3M miles" is a
      question. Scoping that to 3M would pull the wrong filer into the prompt,
      so a reader of a question does not take short names.

    So the grader passes ``True`` and ``query_ui`` passes ``False``, and both
    keep their own reason for it rather than one of them being wrong.
    """
    if short_names and alias.case_sensitive:
        needle = alias.originals
        subject = haystack
    else:
        if alias.case_sensitive:
            # Below the floor and the reader does not take short names.
            return False
        needle = (alias.form,)
        subject = haystack.lower()
    return any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", subject)
        for n in needle
    )


def issuer_forms(name: str) -> list[str]:
    """The spellings a sentence may use to refer to *name*, as written.

    The case is kept: a short alias is matched case-sensitively, so lowercasing
    here would throw away the only thing that tells "3M" from an ordinary word.
    """
    out: list[str] = []
    for candidate in LEGAL_NAME_NOISE.split(str(name or "").strip()):
        candidate = candidate.strip()
        if candidate and candidate.lower() not in {c.lower() for c in out}:
            out.append(candidate)
    return out


def _issuer_index(evidence: Iterable[Evidence]) -> dict[str, _Alias]:
    """Every spelling the corpus knows an issuer by -> the issuers claiming it.

    Read off the Sources rather than off a hard-coded list of companies, so it
    holds for whichever issuers a graph actually holds. A Company line is where
    the two spellings of one issuer meet: its key is the ticker and its label
    is the legal name, so "apple" and "AAPL" resolve to the same issuer without
    anything having to parse the evidence text.

    **The index is only as complete as the evidence.** The legal-name spellings
    come from ``Company`` lines, and nothing else in the graph carries one: a
    metric line knows the filer's ticker, not the company. So a block with no
    Company evidence cannot name an issuer, cannot detect a misattribution, and
    gives no sign of it. Retrieval always includes the Company nodes, which is
    what makes that safe today; the dependency is real and is worth a test of
    its own rather than an assumption.
    """
    originals: dict[str, set[str]] = {}
    owners: dict[str, set[str]] = {}
    for ev in evidence:
        src = ev.source
        issuer = _issuer_of(ev)
        if not issuer:
            continue
        if ev.kind == "Company":
            names = (src.label, src.ticker, src.node_id)
        else:
            # A filer's ticker and CIK sit on the same line, so they are the
            # same issuer by construction.
            names = (src.ticker, src.cik)
        for name in names:
            for spelling in issuer_forms(name):
                key = spelling.lower()
                originals.setdefault(key, set()).add(spelling)
                owners.setdefault(key, set()).add(issuer)
    return {
        key: _Alias(
            form=key,
            originals=frozenset(originals[key]),
            issuers=frozenset(owners[key]),
        )
        for key in originals
    }


def _issuers_named(sentence: str, index: dict[str, _Alias]) -> set[str]:
    """The known issuers *sentence* refers to, by name or by ticker.

    On word boundaries, so "us" never fires inside "because" and a
    ticker-shaped fragment of a longer word is not an issuer.
    """
    named: set[str] = set()
    for alias in index.values():
        if issuer_in_text(sentence, alias):
            named |= alias.issuers
    return named


def _misattribution(
    sentence: str,
    cited: list[str],
    by_tag: dict[str, Evidence],
    index: dict[str, set[str]],
    question_issuers: set[str],
) -> list[str]:
    """Issuers a sentence credits a cited figure to that the cited sources are
    not filed by.

    Two ways to get this wrong, and the first one is the easier to commit.

    **Naming the wrong issuer** is obvious: "Microsoft's net sales were
    416,161 [E1]" where E1 is Apple's line. That is caught by comparing what the
    sentence names against what the cited evidence carries.

    **Naming no issuer at all** reads as harmless and is the actual hole. "Net
    sales were 416,161 million [E1]" states a figure off Apple's line and
    attributes it to nobody, so the comparison finds nothing to compare. A
    reader takes it to be about whatever the question was about -- so the
    question's own issuers are the comparison. If the question names an issuer
    the cited evidence is not filed by, a figure-carrying sentence that names
    no issuer is being handed to that reader as an answer about them. This is
    the D-a5 shape: the corpus cannot support the comparison, and the sentence
    that appears to supply it is the failure.

    Returns ``[]`` when there is nothing to compare against, which is the case
    whenever the cited evidence names no filer at all -- an untraced line says
    nothing about who filed it, and a claim about a company the block never
    mentions is already refused for having no figure.
    """
    cited_issuers = {
        issuer for issuer in (_issuer_of(by_tag[c]) for c in cited) if issuer
    }
    if not cited_issuers:
        return []
    named = _issuers_named(sentence, index)
    if named:
        return sorted(named - cited_issuers)
    if not question_issuers:
        return []
    # A sentence that asserts a figure, names no issuer, and sits under a
    # question about an issuer the evidence is not filed by.
    if not extract_figures(sentence):
        return []
    return sorted(question_issuers - cited_issuers)


@dataclass
class Verdict:
    """What the rules decided about one sentence, and why."""

    text: str
    provenance: str
    cites: list[str] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)
    reason: str = ""
    unknown_cites: list[str] = field(default_factory=list)
    misattributed: list[str] = field(default_factory=list)


@dataclass
class GradedAnswer:
    """The whole answer after rule-based grading."""

    verdicts: list[Verdict]
    text: str
    gap: bool = False
    #: No ``cited_tags``: every cited tag is already on the verdict that cited
    #: it, and :func:`serialise_evidence` walks the verdicts. A second copy
    #: would be a list that can disagree with the verdicts it summarises.
    invented_tags: list[str] = field(default_factory=list)
    ungrounded_figures: list[str] = field(default_factory=list)
    misattributed: list[str] = field(default_factory=list)

    @property
    def mix(self) -> dict[str, int]:
        counts = {t: 0 for t in _ALL_TAGS}
        for v in self.verdicts:
            counts[v.provenance] = counts.get(v.provenance, 0) + 1
        return {k: n for k, n in counts.items() if n}

    def violations(self) -> list[str]:
        """Everything a reader must be told, in plain words."""
        out: list[str] = []
        if self.invented_tags:
            out.append(
                "cited tags that were never issued: " + ", ".join(sorted(set(self.invented_tags)))
            )
        if self.ungrounded_figures:
            uniq = sorted(set(self.ungrounded_figures))
            out.append("figures not present in any cited source: " + ", ".join(uniq))
        if self.misattributed:
            uniq = sorted(set(self.misattributed))
            out.append(
                "claims attributed to an issuer no cited source was filed by: "
                + ", ".join(uniq)
            )
        return out

    @property
    def verdict(self) -> str:
        """The answer as one of :data:`SUPPORTED`, :data:`QUALIFIED`, :data:`REFUSED`."""
        return provenance_verdict(self)


def provenance_verdict(graded: GradedAnswer) -> str:
    """Reduce a graded answer to the one thing a reader can be told.

    The grader produces a verdict per sentence. A reader cannot act on a
    histogram, and something has to reduce them -- and it has to be this
    module, because every consumer needs the same reduction and a caller that
    derives its own gets a different answer.

    **Refusal dominates.** An answer with one ``GAP`` sentence among nine
    ``STATED`` ones is :data:`REFUSED`, not :data:`QUALIFIED`. A partial pass is
    the failure mode this whole module exists to prevent: the reader takes the
    nine good sentences and misses the one that was invented. So the refusal
    conditions are checked first and nothing weakens them:

    * any violation -- an ungrounded figure, a misattributed issuer, a tag
      that was never issued;
    * any single sentence graded :data:`GAP`. Note that this is not the same as
      ``graded.gap``, which is true only when *every* sentence failed. Reading
      the two as one is the hole this had on its first implementation: an
      answer of nine ``STATED`` sentences and one ``GAP`` sentence reports
      ``gap=False``, and the mixed case -- the case this rule exists for --
      sails through as supported.
    * no verdicts at all, which is what an empty answer is. An empty answer is
      not a pass by default; defaulting a missing judgement to the best
      outcome is the bug this function exists to stop.

    :data:`QUALIFIED` is what is left when the answer holds up but part of it
    is hedged (:data:`INFERRED`) or reaches past the filings
    (:data:`EXTERNAL`). Nothing failed, and the reader should still know which
    part did not come straight from a cited fact.

    :data:`SUPPORTED` means every sentence is :data:`STATED` or
    :data:`DERIVED` -- asserted, and shown arithmetic over cited facts.
    """
    if not graded.verdicts:
        # An answer the grader never judged cannot be reported as supported.
        return REFUSED
    if graded.violations():
        return REFUSED
    if any(v.provenance == GAP for v in graded.verdicts):
        return REFUSED
    if any(v.provenance in (INFERRED, EXTERNAL) for v in graded.verdicts):
        return QUALIFIED
    return SUPPORTED


# ---------------------------------------------------------------------------
# Resolving a node to its Source
# ---------------------------------------------------------------------------

class SourceResolver:
    """Walks the graph from a node id back to the filing and item it came from.

    Batched, not per-node: a broad question retrieves several hundred nodes,
    and one query per node turns a 0.1s retrieval into a 30s one. The traversal
    mirrors how the graph is actually wired --
    ``Company -> Filing -> {Metric, Chunk, Event}`` and ``Metric -> Section`` --
    so a Source is always read, never guessed.
    """

    def __init__(self, kg: Any) -> None:
        self.kg = kg
        self._filings: dict[str, dict[str, str]] | None = None
        self._by_node: dict[str, Source] | None = None
        self._unresolved: set[str] = set()

    # -- filings ----------------------------------------------------------
    def _filing_index(self) -> dict[str, dict[str, str]]:
        if self._filings is None:
            idx: dict[str, dict[str, str]] = {}
            rows = self.kg.execute(
                "MATCH (f:Filing) RETURN f.id, f.form_type, f.filing_date, "
                "f.accession_number, f.period_end_date, f.fiscal_year, f.fiscal_period"
            )
            for r in rows:
                fid = r[0]
                if not fid:
                    continue
                # The same document can be reached by more than one id, so keep
                # whichever copy carries the most information.
                cand = {
                    "form_type": r[1] or "",
                    "filing_date": r[2] or "",
                    "accession": r[3] or "",
                    "period_end": r[4] or "",
                    "fiscal_year": str(r[5] or ""),
                    "fiscal_period": r[6] or "",
                }
                if not idx.get(fid) or len(cand["accession"]) > len(idx[fid]["accession"]):
                    idx[fid] = cand
            self._filings = self._with_filers(idx)
        return self._filings

    def _with_filers(self, idx: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        """Attach the submitting issuer to every filing in *idx*.

        The filer is a fact, not a label: ``Company-[:SUBMITTED]->Filing`` is the
        only edge into a filing, so the ticker is read across it rather than
        parsed out of a metric's name. ``query_ui`` independently builds the same
        accession->ticker map for the graph view and stops there, which is why a
        filing reached from three issuers rendered as three identical boxes and
        every figure off one of them reached the prompt unattributed.
        """
        try:
            rows = self.kg.execute(
                "MATCH (c:Company)-[:SUBMITTED]->(f:Filing) "
                "RETURN f.id, c.ticker, c.cik"
            )
        except Exception:
            rows = []
        for fid, ticker, cik in rows:
            entry = idx.get(fid)
            if entry is None or not ticker:
                continue
            entry["ticker"] = str(ticker or "")
            entry["cik"] = str(cik or "")
        for entry in idx.values():
            entry.setdefault("ticker", "")
            entry.setdefault("cik", "")
        return idx

    def _filing_for(self, node_ids: Iterable[str]) -> dict[str, str]:
        """node id -> the filing that reports it, for the ids we can reach."""
        ids = {i for i in node_ids if i}
        out: dict[str, str] = {}
        if not ids:
            return out
        # The id list goes in the query, not in the loop. A broad question
        # retrieves a few hundred nodes out of a graph where every filing
        # reaches every metric, segment, event and chunk it reports: the
        # unfiltered traversal transfers ~9,700 rows to pick out a few hundred,
        # which is the dominant cost of resolving sources and was paid on every
        # question. Batching kept it from being one query per node; without the
        # WHERE it was still one query that looked at everything.
        q = (
            "MATCH (f:Filing)-[r]->(n) WHERE n.id IN $ids "
            "RETURN n.id, f.id, coalesce(r.item_code, '')"
        )
        try:
            rows = self.kg.execute(q, {"ids": sorted(ids)})
        except Exception:
            return out
        for node_id, filing_id, item_code in rows:
            if node_id in ids and node_id not in out and filing_id:
                out[node_id] = filing_id

        # A Segment is reported by a filing through the metric that breaks it
        # out, two hops away. That intermediate metric is usually not itself
        # retrieved, so the hop cannot be resolved by looking at the retrieved
        # set -- the path has to be walked in the graph. Without this, all 22
        # segments come back untraced, which is most of the evidence behind
        # every segment question.
        #
        # Keyed on ``name``, not ``id``: a Segment node carries only ``name``
        # and ``segment_type``, so there is no id property to join on. The
        # retriever already uses the name as the node id (the names are unique
        # across the corpus, 22 of 22), so the join matches what the caller
        # actually holds.
        #
        # There are two ways a segment is reached -- ``HAS_SEGMENT`` from a
        # Metric, and ``BROKEN_DOWN_BY`` from a RawFact (which reaches a filing
        # through the Section it was reported in) -- and the corpus splits
        # across them: 16 segments on one path, 6 on the other. Both are needed.
        #
        # These two traversals are the *whole* of segment resolution. A third
        # used to sit in :meth:`sources_for`, keyed on ``s.id`` through a
        # ``DISAGGREGATED_BY`` edge, and it never ran: that edge is named
        # ``HAS_SEGMENT`` on the engine schema and ``Segment`` has no ``id`` at
        # all, so the query raised on every database and the ``except: pass``
        # around it reported the fallback as working. Keying on ``name`` is
        # what makes the join real, and the same key is used here.
        #
        # A segment is reported by several filings, so "which filing" needs a
        # rule. Prefer the most recent, which is the filing a reader would
        # check first; the citation is still true for every filing that carries
        # the segment, so this is a tie-break, not a guess.
        segment_filings: dict[str, list[tuple[str, str]]] = {}
        segment_queries = (
            "MATCH (f:Filing)-[:REPORTS_METRIC]->(m)-[:HAS_SEGMENT]->(s:Segment) "
            "RETURN s.name, f.id, f.filing_date",
            "MATCH (f:Filing)-[:CONTAINS_SECTION]->(sec:Section)<-[:REPORTED_IN]-"
            "(r:RawFact)-[:BROKEN_DOWN_BY]->(s:Segment) "
            "RETURN s.name, f.id, f.filing_date",
        )
        for query in segment_queries:
            try:
                rows = self.kg.execute(query)
            except Exception:
                continue
            for seg_name, filing_id, filing_date in rows:
                if seg_name in ids and filing_id:
                    segment_filings.setdefault(seg_name, []).append(
                        (str(filing_date or ""), filing_id)
                    )
        for seg_name, candidates in segment_filings.items():
            if seg_name in out:
                continue
            candidates.sort(reverse=True)
            out[seg_name] = candidates[0][1]
        return out

    def _section_for(self, node_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
        """node id -> (item_code, section_title), for the ids that have one.

        Only some node types carry a place-in-the-document, and the graph only
        asserts it where it truly is:

        * ``DisclosureEvent`` carries ``item_code`` directly (8-K Item 5.02).
        * ``Section`` is the item index itself.
        * ``DocumentChunk`` carries a ``section`` name.
        * ``RawFact`` reaches a Section through ``REPORTED_IN``, and the item
          code lives on that relationship.

        ``FinancialMetric`` deliberately gets nothing here. It has exactly two
        relationships -- ``Filing-[REPORTS_METRIC]->`` in and ``HAS_SEGMENT->``
        out -- and no path to a Section at all, so there is no item code to
        read. Inventing one from the filing's section list would be the exact
        failure this module exists to prevent: a plausible-looking citation
        that no document supports.
        """
        ids = {i for i in node_ids if i}
        out: dict[str, tuple[str, str]] = {}
        if not ids:
            return out
        queries = (
            "MATCH (e:DisclosureEvent) RETURN e.id, coalesce(e.item_code, ''), coalesce(e.item_title, '')",
            "MATCH (s:Section) RETURN s.id, coalesce(s.item_code, ''), coalesce(s.section_title, '')",
            "MATCH (c:DocumentChunk) RETURN c.id, '', coalesce(c.section, '')",
            "MATCH (r:RawFact)-[x:REPORTED_IN]->(s:Section) "
            "RETURN r.id, coalesce(x.item_code, ''), coalesce(s.section_title, '')",
        )
        for query in queries:
            try:
                rows = self.kg.execute(query)
            except Exception:
                continue
            for node_id, item_code, title in rows:
                if node_id in ids and node_id not in out and (item_code or title):
                    out[node_id] = (item_code or "", title or "")
        return out

    # -- public -----------------------------------------------------------
    def sources_for(self, nodes: list[dict[str, Any]]) -> dict[str, Source]:
        """node id -> Source, for every node that can be traced to a filing.

        A node with no filing behind it is *not* given a Source, and callers
        treat that as unciteable. That is the point: a fact that cannot be
        traced cannot be called STATED.
        """
        if self._by_node is not None and not self._unresolved:
            return self._by_node

        filings = self._filing_index()
        node_ids = [n.get("id", "") for n in nodes if n.get("id")]
        filing_of = self._filing_for(node_ids)
        section_of = self._section_for(node_ids)

        out: dict[str, Source] = {}
        for node in nodes:
            nid = node.get("id", "")
            if not nid:
                continue
            label = node.get("name", "")
            node_type = node.get("type", "")

            # A Filing is its own Source. Nothing points *at* a filing except a
            # Company's SUBMITTED edge, so the traversal above cannot reach it,
            # and leaving 30 of 41 evidence lines "untraced" would make the
            # contract refuse to cite the very documents everything else is
            # measured against.
            own = filings.get(nid)
            if own is not None:
                out[nid] = Source(
                    node_id=nid,
                    label=label or own.get("form_type", ""),
                    ticker=own.get("ticker", ""),
                    cik=own.get("cik", ""),
                    form_type=own.get("form_type", ""),
                    filing_date=own.get("filing_date", ""),
                    accession=own.get("accession", ""),
                    period_end=own.get("period_end", ""),
                    fiscal_year=own.get("fiscal_year", ""),
                    fiscal_period=own.get("fiscal_period", ""),
                )
                continue

            fid = filing_of.get(nid)
            meta = filings.get(fid or "", {})
            item_code, title = section_of.get(nid, ("", ""))
            # A chunk knows its own section; prefer it over a structural guess.
            if not title and node_type == "DocumentChunk":
                title = (label or "").split(" chunk:")[0]
            if not meta and not item_code and not title:
                # Company nodes have a filing behind them via SUBMITTED, which
                # is many-to-one; a company is still citeable, so fall back to
                # its own identity rather than dropping it. A Company's own key
                # is its ticker, so the filer fields are its identity.
                if node_type == "Company":
                    out[nid] = Source(
                        node_id=nid,
                        label=label,
                        ticker=label,
                        form_type="",
                        section="",
                    )
                else:
                    self._unresolved.add(nid)
                continue
            out[nid] = Source(
                node_id=nid,
                label=label,
                ticker=meta.get("ticker", ""),
                cik=meta.get("cik", ""),
                form_type=meta.get("form_type", ""),
                filing_date=meta.get("filing_date", ""),
                accession=meta.get("accession", ""),
                item_code=item_code,
                section=title,
                period_end=meta.get("period_end", ""),
                fiscal_year=meta.get("fiscal_year", ""),
                fiscal_period=meta.get("fiscal_period", ""),
            )

        self._by_node = out
        return out


# ---------------------------------------------------------------------------
# Evidence blocks
# ---------------------------------------------------------------------------

def build_evidence(
    nodes: list[dict[str, Any]],
    kg: Any,
    tag_map: dict[str, str] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> list[Evidence]:
    """Turn retrieved nodes into Evidence, resolving a Source for each.

    The tag order is the retrieval order, so ``E1`` means the same thing in the
    prompt, in the citation check and in the UI. ``tag_map`` is the retriever's
    ``tag -> node_id`` map, so it is inverted here: a node takes the tag the
    retriever already gave it, and falls back to its position if it has none.

    ``edges`` matters more than it looks. A figure is not a property of a metric
    node -- it is on the edge that reports it (``REPORTS_METRIC`` carries
    ``value``, ``scale`` and ``period``), so a node-only evidence block would
    show the model a metric name with no number. Every edge description is
    folded into the text of the node it points at, which is what lets the rule
    engine later check a figure against what was actually supplied.
    """
    resolver = SourceResolver(kg)
    sources = resolver.sources_for(nodes)
    tag_for_id = {nid: tag for tag, nid in (tag_map or {}).items()}
    facts_for_id: dict[str, list[str]] = {}
    for edge in edges or []:
        target = edge.get("target")
        desc = edge.get("description")
        if target and desc:
            facts_for_id.setdefault(target, []).append(desc)
    out: list[Evidence] = []
    for idx, node in enumerate(nodes, start=1):
        nid = node.get("id", "")
        tag = tag_for_id.get(nid, f"E{idx}")
        source = sources.get(nid)
        if source is None:
            # Not traceable to a filing. It is still shown, because hiding it
            # would make the model guess, but it carries no Source and so can
            # never support a STATED claim.
            source = Source(node_id=nid, label=node.get("name", ""), section="untraced")
        bits: list[str] = []
        if node.get("description"):
            bits.append(node["description"])
        bits.extend(facts_for_id.get(nid, []))
        text = node.get("name", "")
        if bits:
            joined = "; ".join(bits)
            text = f"{text} — {joined}" if text else joined
        out.append(
            Evidence(
                tag=tag,
                text=text,
                source=source,
                provenance=STATED,
                kind=node.get("type", ""),
            )
        )
    return out


def serialise_evidence(evidence: list[Evidence], max_chars: int = 60000) -> str:
    """The evidence block handed to the model.

    Every line carries its own tag and its own Source, because the model's only
    permitted move is to cite a tag that is already here. A tag it has not been
    given does not exist, and inventing one is caught downstream.
    """
    lines = [
        "EVIDENCE — every line below is quoted from a filing and carries a tag.",
        "You may ONLY cite tags that appear here. Do not invent a tag.",
        "",
    ]
    by_source: dict[str, list[Evidence]] = {}
    for ev in evidence:
        by_source.setdefault(ev.source.short or "untraced", []).append(ev)
    for source_key, group in by_source.items():
        lines.append(f"--- {source_key} ---")
        for ev in group:
            lines.append(ev.block())
        lines.append("")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[evidence truncated]\n"
    return text


# ---------------------------------------------------------------------------
# The rule engine
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences without breaking on decimals or citations.

    ``$416,161.00`` is one number, not two sentences, and ``[E1]`` is not a
    boundary -- a naive split on ``.`` would grade half of each as unsupported.
    """
    if not text:
        return []
    protected = re.sub(r"(\d)\.(\d)", r"\1<DOT>\2", text)
    protected = re.sub(r"\b([A-Za-z])\.(?=\s|$)", r"\1<DOT>", protected)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\[*\"'(])", protected)
    out: list[str] = []
    for p in parts:
        p = p.replace("<DOT>", ".").strip()
        if p:
            out.append(p)
    return out


def grade_answer(
    answer: str,
    evidence: list[Evidence],
    question: str = "",
) -> GradedAnswer:
    """Assign a provenance tag to every sentence, by rule.

    The model produced the prose. This function decides what it is allowed to
    have said, and it never asks the model. The rules, in the order they are
    applied to each sentence:

    1. cites a tag that was never issued      -> GAP (it is citing fiction)
    2. names an issuer no cited source was filed by, whether it names the wrong
       one or names none under a question about another
                                                -> GAP (misattribution)
    3. outside-corpus markers                  -> EXTERNAL
    4. a figure in it that is in no cited fact, and no arithmetic shown
                                                -> GAP (fabrication)
    5. arithmetic shown over cited facts       -> DERIVED
    6. cites real evidence and asserts a fact  -> STATED
    7. hedged, cites nothing                   -> INFERRED, but only if a STATED
                                                  sentence precedes it; else GAP
    8. nothing supports it                     -> GAP

    Misattribution is checked before the outside-corpus markers rather than
    after: "Currently, Microsoft's net sales were 416,161 [E1]" carries a claim
    about a filer the cited line is not filed by, and reading that as merely
    EXTERNAL lets it through.
    """
    by_tag = {ev.tag: ev for ev in evidence}
    issuers = _issuer_index(evidence)
    question_issuers = _issuers_named(question or "", issuers)

    # Pass 1: provisional verdicts, so rule 7 can see whether a STATED sentence
    # exists to lean on.
    verdicts: list[Verdict] = []
    for sentence in _split_sentences(answer):
        cites = citation_tags(sentence)
        known = [c for c in cites if c in by_tag]
        unknown = [c for c in cites if c not in by_tag]
        figures = extract_figures(sentence)
        # Grounding is the figures of the evidence *this sentence cites*, and
        # of nothing else. The whole block used to be unioned in, which made
        # any tag a laundering device: cite any one line and every figure
        # anywhere in the evidence became groundable, so a sentence could carry
        # a number off a filing it never cited. The message
        # :meth:`GradedAnswer.violations` reports -- "not present in any cited
        # source" -- is only true if the check is scoped to the cited sources.
        cited_ground: set[float] = set()
        cited_issuers: set[str] = set()
        for c in known:
            ev = by_tag[c]
            cited_ground |= _figure_keys(ev.text)
            cited_ground |= _figure_keys(ev.source.cite())
            issuer = _issuer_of(ev)
            if issuer:
                cited_issuers.add(issuer)
        ungrounded = [
            f for f in figures
            if (normalise_number(f) is not None
                and normalise_number(f) not in cited_ground
                and not _restates(f, cited_ground))
        ]
        # A figure check cannot see the worst error there is. Apple's net sales
        # restated as Microsoft's is not an ungrounded number -- it is a real
        # one, read off a real line -- so a sentence that names an issuer the
        # cited sources were not filed by is refused on its own account, even
        # though the number checks out. Only assessed when the cited evidence
        # A figure check cannot see the worst error there is. Apple's net sales
        # restated as Microsoft's is not an ungrounded number -- it is a real
        # one, read off a real line -- so attribution is its own rule, and it
        # catches both naming the wrong issuer and naming none at all under a
        # question about a different one.
        misattributed = _misattribution(
            sentence, known, by_tag, issuers, question_issuers
        )
        verdicts.append(
            Verdict(
                text=sentence,
                provenance=GAP,
                cites=known,
                figures=figures,
                ungrounded=ungrounded,
                unknown_cites=unknown,
                misattributed=misattributed,
            )
        )

    # Pass 2: apply the rules.
    stated_seen = False
    for v in verdicts:
        s = v.text
        if v.unknown_cites:
            v.provenance = GAP
            v.reason = "cites a tag that was never issued"
            continue
        if v.misattributed:
            # Before the outside-corpus markers and before anything that would
            # launder it. A correct figure on the wrong filer is still the wrong
            # filer: neither shown arithmetic nor a real tag nor a passing
            # "currently" makes it STATED, DERIVED, or EXTERNAL.
            cited_by = sorted({_issuer_of(by_tag[c]) for c in v.cites if c in by_tag} - {""})
            v.provenance = GAP
            v.reason = (
                "attributed to "
                + ", ".join(v.misattributed)
                + ", which no cited source was filed by (cited: "
                + ", ".join(cited_by)
                + ")"
            )
            continue
        if _EXTERNAL_MARKERS.search(s):
            v.provenance = EXTERNAL
            v.reason = "asserts something outside the filed corpus"
            continue

        hedged = bool(_HEDGE.search(s))
        shows_arith = bool(_ARITH.search(s))

        if v.ungrounded and not shows_arith:
            v.provenance = GAP
            v.reason = (
                "figure(s) not present in any cited source and no arithmetic shown: "
                + ", ".join(v.ungrounded)
            )
            continue
        if v.ungrounded and shows_arith and not v.cites:
            v.provenance = GAP
            v.reason = "arithmetic asserted over no cited fact"
            continue
        if shows_arith and v.cites:
            v.provenance = DERIVED
            v.reason = "arithmetic over cited facts"
            continue
        if v.cites and v.figures:
            v.provenance = STATED
            v.reason = "cited figure present in the cited source"
            stated_seen = True
            continue
        if v.cites:
            v.provenance = STATED
            v.reason = "cited to a source that carries the claim"
            stated_seen = True
            continue
        if hedged:
            if stated_seen:
                v.provenance = INFERRED
                v.reason = "hedged, and leans on an earlier STATED sentence"
            else:
                v.provenance = GAP
                v.reason = "hedged but nothing STATED to lean on"
            continue
        v.provenance = GAP
        v.reason = "no citation and no figure tying it to the corpus"

    graded = GradedAnswer(
        verdicts=verdicts,
        text=answer,
        gap=all(v.provenance == GAP for v in verdicts) if verdicts else True,
        invented_tags=sorted({c for v in verdicts for c in v.unknown_cites}),
        ungrounded_figures=sorted({f for v in verdicts for f in v.ungrounded}),
        misattributed=sorted({i for v in verdicts for i in v.misattributed}),
    )
    return graded


# ---------------------------------------------------------------------------
# GAP rendering
# ---------------------------------------------------------------------------

def render_gap(question: str, evidence: list[Evidence], where_to_look: list[str]) -> str:
    """A GAP as a real answer: what is missing, and what would supply it.

    Refusing is easy and nearly useless. The reader's next question is almost
    always "so where *would* that be?", so this answers that too, from the
    forms actually present in the corpus.
    """
    forms: list[str] = []
    for ev in evidence:
        f = ev.source.form_type
        if f and f not in forms:
            forms.append(f)
    forms.sort()
    lines = [
        f"**GAP — the corpus does not cover this.**\n",
        f"Question: *{question}*\n",
    ]
    if evidence:
        lines.append(
            "Retrieved evidence is tagged below, and none of it states the answer. "
            "Nothing here supports a figure, so none is given:\n"
        )
        for ev in evidence[:8]:
            lines.append(f"- `[{ev.tag}]` {ev.text[:110]}  ⟵ {ev.source.cite()}")
    else:
        lines.append("No tagged evidence was retrieved for this question at all.\n")

    if where_to_look:
        lines.append("\n**Where this would be found:**\n")
        for w in where_to_look:
            lines.append(f"- {w}")
    if forms:
        lines.append(
            f"\nForms available in this corpus: {', '.join(forms)}. "
            f"Nothing in {' or '.join(forms)} carries it, which is why this is a GAP "
            f"rather than a low-confidence answer."
        )
    lines.append(
        "\nThis is a provenance limit, not a failure to read the filings. "
        "Answering it would require a source outside the corpus."
    )
    return "\n".join(lines)
