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
]

STATED = "STATED"
DERIVED = "DERIVED"
INFERRED = "INFERRED"
EXTERNAL = "EXTERNAL"
GAP = "GAP"

_ALL_TAGS = (STATED, DERIVED, INFERRED, EXTERNAL, GAP)

#: A citation is ``[E1]`` or ``[E1, E3]``. Anchored so ``[E12]`` does not read as
#: ``[E1]`` followed by junk.
_CITATION = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]")

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

#: Phrases that concede the corpus does not cover it. Not a shortcut for the
#: tagger -- a sentence saying "I don't know" still has to be judged on whether
#: it needed to -- but the GAP renderer uses them to recognise its own output.
_GAP_PHRASES = (
    "not disclosed",
    "not in the corpus",
    "does not disclose",
    "do not disclose",
    "no disclosure",
    "not reported",
    "cannot be determined",
    "not covered",
    "no such disclosure",
    "not available in",
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
    # Dates have to go before tokenising: ``2025-10-31`` otherwise yields the
    # two figures ``-10`` and ``-31``, and an answer that merely mentions when
    # a filing was filed would be reported as inventing numbers.
    masked = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
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


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """Where a fact is filed. One of these exists for every citable fact.

    The fields are the ones an auditor would ask for, and every one of them is
    read off the graph rather than inferred: which filing, what form, when it
    was filed, and which item inside it. ``accession`` is the SEC's own id for
    the document; ``section`` is the human-facing place in it.
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

    @property
    def short(self) -> str:
        bits = [b for b in (self.form_type, self.section or self.item_code) if b]
        return " · ".join(bits) or self.label

    def cite(self) -> str:
        """One-line reference, e.g. ``10-K FY2025 (FY) filed 2025-10-31, Item 1 Business``."""
        parts: list[str] = []
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

    def to_dict(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "form_type": self.form_type,
            "filing_date": self.filing_date,
            "accession": self.accession,
            "item_code": self.item_code,
            "section": self.section,
            "period_end": self.period_end,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
        }


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


@dataclass
class GradedAnswer:
    """The whole answer after rule-based grading."""

    verdicts: list[Verdict]
    text: str
    gap: bool = False
    cited_tags: list[str] = field(default_factory=list)
    invented_tags: list[str] = field(default_factory=list)
    ungrounded_figures: list[str] = field(default_factory=list)

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
        return out


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
            self._filings = idx
        return self._filings

    def _filing_for(self, node_ids: Iterable[str]) -> dict[str, str]:
        """node id -> the filing that reports it, for the ids we can reach."""
        ids = {i for i in node_ids if i}
        out: dict[str, str] = {}
        if not ids:
            return out
        q = "MATCH (f:Filing)-[r]->(n) RETURN n.id, f.id, coalesce(r.item_code, '')"
        try:
            rows = self.kg.execute(q)
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
        # A Segment reaches its filing through the metric that disaggregates it.
        indirect: dict[str, str] = {}
        try:
            for seg_id, m_id in self.kg.execute(
                "MATCH (m)-[:DISAGGREGATED_BY]->(s:Segment) RETURN s.id, m.id"
            ):
                if seg_id in set(node_ids) and m_id in filing_of:
                    indirect[seg_id] = filing_of[m_id]
        except Exception:
            pass

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
                    form_type=own.get("form_type", ""),
                    filing_date=own.get("filing_date", ""),
                    accession=own.get("accession", ""),
                    period_end=own.get("period_end", ""),
                    fiscal_year=own.get("fiscal_year", ""),
                    fiscal_period=own.get("fiscal_period", ""),
                )
                continue

            fid = filing_of.get(nid) or indirect.get(nid)
            meta = filings.get(fid or "", {})
            item_code, title = section_of.get(nid, ("", ""))
            # A chunk knows its own section; prefer it over a structural guess.
            if not title and node_type == "DocumentChunk":
                title = (label or "").split(" chunk:")[0]
            if not meta and not item_code and not title:
                # Company nodes have a filing behind them via SUBMITTED, which
                # is many-to-one; a company is still citeable, so fall back to
                # its own identity rather than dropping it.
                if node_type == "Company":
                    out[nid] = Source(node_id=nid, label=label, form_type="", section="")
                else:
                    self._unresolved.add(nid)
                continue
            out[nid] = Source(
                node_id=nid,
                label=label,
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


def _citations(sentence: str) -> list[str]:
    found: list[str] = []
    for group in _CITATION.findall(sentence):
        for part in re.split(r"\s*,\s*", group):
            if part and part not in found:
                found.append(part)
    return found


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
    2. outside-corpus markers                  -> EXTERNAL
    3. a figure in it that is in no cited fact, and no arithmetic shown
                                                -> GAP (fabrication)
    4. arithmetic shown over cited facts       -> DERIVED
    5. cites real evidence and asserts a fact  -> STATED
    6. hedged, cites nothing                   -> INFERRED, but only if a STATED
                                                  sentence precedes it; else GAP
    7. nothing supports it                     -> GAP
    """
    by_tag = {ev.tag: ev for ev in evidence}
    grounded = _figure_keys(" ".join(ev.text for ev in evidence))
    grounded |= _figure_keys(" ".join(ev.source.cite() for ev in evidence))

    # Pass 1: provisional verdicts, so rule 6 can see whether a STATED sentence
    # exists to lean on.
    verdicts: list[Verdict] = []
    for sentence in _split_sentences(answer):
        cites = _citations(sentence)
        known = [c for c in cites if c in by_tag]
        unknown = [c for c in cites if c not in by_tag]
        figures = extract_figures(sentence)
        cited_text = " ".join(by_tag[c].text for c in known)
        cited_ground = _figure_keys(cited_text) | grounded if known else set()
        ungrounded = [
            f for f in figures
            if (normalise_number(f) is not None and normalise_number(f) not in cited_ground)
        ]
        verdicts.append(
            Verdict(
                text=sentence,
                provenance=GAP,
                cites=known,
                figures=figures,
                ungrounded=ungrounded,
                unknown_cites=unknown,
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
        cited_tags=sorted({c for v in verdicts for c in v.cites}),
        invented_tags=sorted({c for v in verdicts for c in v.unknown_cites}),
        ungrounded_figures=sorted({f for v in verdicts for f in v.ungrounded}),
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
