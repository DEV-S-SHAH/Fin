"""Provenance contract for the India Company Graph.

Every citable fact carries a tag assigned by retrieval code — never by the
model. Five tags:

  STATED    read directly off a graph node, cited to its Source.
  DERIVED   computed by arithmetic over cited facts, with arithmetic shown.
  INFERRED  hedged reasoning that leans on a STATED sentence.
  EXTERNAL  outside the corpus (news, analyst, consensus, market share).
  GAP       the corpus lacks the answer — names what/where is missing.

An answer reduces to SUPPORTED / QUALIFIED / REFUSED. Any GAP sentence ->
REFUSED. Misattribution (sentence credits company X but cites company Y) ->
GAP.

This module mirrors sandbox_engine/provenance.py but works against the
in-memory SessionGraph (NetworkX), not LadybugDB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STATED = "STATED"
DERIVED = "DERIVED"
INFERRED = "INFERRED"
EXTERNAL = "EXTERNAL"
GAP = "GAP"

_ALL_TAGS = (STATED, DERIVED, INFERRED, EXTERNAL, GAP)

SUPPORTED = "SUPPORTED"
QUALIFIED = "QUALIFIED"
REFUSED = "REFUSED"

# Citation grammar: [E1], [E1, E3], [E2->E15], [E2 -> E15]
_CITATION_SEP = r"\s*(?:,|[-]{1,2}(?:>|→)|=>|→)\s*"
_CITATION = re.compile(rf"\[(E\d+(?:{_CITATION_SEP}E\d+)*)\]")

_ARITH = re.compile(
    r"(?:[-−+*/×÷]\s*\d)|(?:=\s*\d)|(?:\d+\s*(?:/|÷)\s*\d)"
    r"|\b(?:subtract|subtracted|minus|divided by|times|less|equals|computed|"
    r"calculated|derived from|which is|giving|therefore|implies a)\b",
    re.IGNORECASE,
)

_HEDGE = re.compile(
    r"\b(?:may|might|could|would|likely|unlikely|suggests?|suggesting|implies?|"
    r"appears?|seems?|potentially|arguably|plausibly|perhaps|possibly|"
    r"tends? to|is likely|is expected|points? to|suggests that)\b",
    re.IGNORECASE,
)

_EXTERNAL_MARKERS = re.compile(
    r"\b(?:news(?:wire)?|this week|today|yesterday|press (?:release|reports?)|"
    r"analyst|consensus estimate|market share|third[- ]party|"
    r"according to (?:reports|news|analysts)|currently|as of today)\b",
    re.IGNORECASE,
)

_STRUCTURAL = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}"
    r"|FY\d{2,4}"
    r"|\d{1,2}[KQ]"
    r"|\d{4}$"
    r"|0\d{6,9}$"
    r"|\d{1,2}$"
    r")$"
)
_FORM_TOKEN = re.compile(r"(?:10-[KQ]|8-K|10-K/A|10-Q/A)", re.IGNORECASE)


def citation_tags(text: str) -> list[str]:
    found: list[str] = []
    for group in _CITATION.findall(text or ""):
        for part in re.split(_CITATION_SEP, group):
            part = part.strip()
            if part and part not in found:
                found.append(part)
    return found


def normalise_number(token: str) -> float | None:
    if token is None:
        return None
    t = str(token).strip()
    if not t:
        return None
    t = _FORM_TOKEN.sub("", t)
    negative = t.startswith(("(", "-", "−"))
    t = re.sub(r"[^\d.]", "", t.replace(",", ""))
    if not t or t == ".":
        return None
    try:
        return -float(t) if negative else float(t)
    except ValueError:
        return None


def extract_figures(text: str) -> list[str]:
    if not text:
        return []
    masked = _CITATION.sub(" ", text)
    masked = re.sub(r"\d{4}-\d{2}-\d{2}", " ", masked)
    masked = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", masked)
    out: list[str] = []
    for raw in re.findall(r"\(?-?[\d][\d,]*\.?\d*\)?%?", masked):
        stripped = raw.strip().rstrip(",")
        core = stripped.strip("()%").replace(",", "")
        if not core or _FORM_TOKEN.fullmatch(stripped):
            continue
        if _STRUCTURAL.match(core):
            continue
        out.append(stripped)
    return out


@dataclass(frozen=True)
class Source:
    """Where a fact comes from — read off the graph, never inferred."""

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
    cin: str = ""
    source: str = ""  # "NSE", "BSE", "MCA"

    def cite(self) -> str:
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
        if self.section:
            parts.append(self.section)
        return ", ".join(parts) or self.label


@dataclass
class Evidence:
    tag: str
    text: str
    source: Source
    provenance: str = STATED
    kind: str = ""

    def block(self) -> str:
        return f"[{self.tag}] {self.provenance} · {self.text}  ⟵ {self.source.cite()}"


@dataclass
class Verdict:
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
    verdicts: list[Verdict]
    text: str
    gap: bool = False
    invented_tags: list[str] = field(default_factory=list)
    ungrounded_figures: list[str] = field(default_factory=list)
    misattributed: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return provenance_verdict(self)

    def violations(self) -> list[str]:
        out: list[str] = []
        if self.invented_tags:
            out.append("cited tags that were never issued: " + ", ".join(sorted(set(self.invented_tags))))
        if self.ungrounded_figures:
            out.append("figures not present in any cited source: " + ", ".join(sorted(set(self.ungrounded_figures))))
        if self.misattributed:
            out.append("claims attributed to an issuer no cited source was filed by: " + ", ".join(sorted(set(self.misattributed))))
        return out


def provenance_verdict(graded: GradedAnswer) -> str:
    if not graded.verdicts:
        return REFUSED
    if graded.violations():
        return REFUSED
    if any(v.provenance == GAP for v in graded.verdicts):
        return REFUSED
    if any(v.provenance in (INFERRED, EXTERNAL) for v in graded.verdicts):
        return QUALIFIED
    return SUPPORTED


# -- Source resolution from in-memory graph --------------------------------

def _source_for_node(node: dict[str, Any]) -> Source | None:
    """Build a Source from a graph node's attributes."""
    ntype = node.get("type", "")
    if ntype == "Company":
        return Source(
            node_id=node.get("id", ""),
            label=node.get("name", ""),
            ticker=node.get("ticker", ""),
            cin=node.get("cin", ""),
            section="",
        )
    if ntype == "Filing":
        return Source(
            node_id=node.get("id", ""),
            label=node.get("label", ""),
            form_type=node.get("form_type", ""),
            filing_date=node.get("filing_date", ""),
            accession=node.get("accession", ""),
            item_code=node.get("item_code", ""),
            section=node.get("section", ""),
            period_end=node.get("period_end", ""),
            fiscal_year=str(node.get("fiscal_year", "")),
            fiscal_period=node.get("fiscal_period", ""),
            ticker=node.get("ticker", ""),
            cin=node.get("cin", ""),
            source=node.get("source", ""),
        )
    if ntype == "FinancialMetric":
        return Source(
            node_id=node.get("id", ""),
            label=node.get("metric_name", ""),
            cin=node.get("cin", ""),
            ticker=node.get("ticker", ""),
            section="FinancialMetric",
            source=node.get("source", ""),
        )
    if ntype == "Announcement":
        return Source(
            node_id=node.get("id", ""),
            label=node.get("title", ""),
            section="Announcement",
            source=node.get("source", ""),
        )
    return None


def build_evidence(
    graph_nodes: list[dict[str, Any]],
    graph_edges: list[dict[str, Any]],
    tag_map: dict[str, str] | None = None,
) -> list[Evidence]:
    """Turn retrieved graph nodes into Evidence with Sources.

    Provenance is assigned here (STATED by default), never by the model.
    Edge descriptions (figures on REPORTS_METRIC edges) are folded into the
    target node's text so a figure is never shown without its context.
    """
    tag_for_id = {nid: tag for tag, nid in (tag_map or {}).items()}
    facts_for_id: dict[str, list[str]] = {}
    for edge in graph_edges or []:
        target = edge.get("target")
        desc = edge.get("description")
        if target and desc:
            facts_for_id.setdefault(target, []).append(desc)

    out: list[Evidence] = []
    for idx, node in enumerate(graph_nodes, start=1):
        nid = node.get("id", "")
        tag = tag_for_id.get(nid, f"E{idx}")
        source = _source_for_node(node)
        if source is None:
            source = Source(node_id=nid, label=node.get("name", ""), section="untraced")
        bits: list[str] = []
        if node.get("description"):
            bits.append(node["description"])
        bits.extend(facts_for_id.get(nid, []))
        text = node.get("name") or node.get("title") or node.get("metric_name", "")
        if bits:
            joined = "; ".join(bits)
            text = f"{text} — {joined}" if text else joined
        out.append(Evidence(tag=tag, text=text, source=source, provenance=STATED))
    return out


# -- Grading --------------------------------------------------------------

def _figure_keys(text: str) -> set[float]:
    keys: set[float] = set()
    for tok in extract_figures(text):
        v = normalise_number(tok)
        if v is not None:
            keys.add(v)
    return keys


def _issuer_of(ev: Evidence) -> str:
    return ev.source.cin or ev.source.ticker or ""


def _issuers_named(sentence: str, index: dict[str, set[str]]) -> set[str]:
    named: set[str] = set()
    for alias in index.values():
        for spelling in alias:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(spelling)}(?![A-Za-z0-9])", sentence, re.IGNORECASE):
                named |= alias  # alias is a set of issuers
    return named


def grade_answer(
    answer: str,
    evidence: list[Evidence],
    question: str = "",
) -> GradedAnswer:
    """Grade an answer against evidence, assigning verdicts per sentence.

    Provenance on each Evidence was set by retrieval. The model may only cite
    tags that appear in the evidence block. Figures are checked against cited
    evidence only — not the whole block.
    """
    by_tag: dict[str, Evidence] = {ev.tag: ev for ev in evidence}
    issued_tags = set(by_tag.keys())

    # Issuer index: spelling -> set of issuers (CINs/tickers)
    issuer_index: dict[str, set[str]] = {}
    for ev in evidence:
        issuer = _issuer_of(ev)
        if not issuer:
            continue
        for spelling in {ev.source.label, ev.source.ticker, ev.source.cin}:
            if not spelling:
                continue
            key = spelling.lower()
            issuer_index.setdefault(key, set()).add(issuer)

    question_issuers: set[str] = set()
    for key, issuers in issuer_index.items():
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", question, re.IGNORECASE):
            question_issuers |= issuers

    # Split on sentence-ending punctuation only when followed by whitespace or
    # end of string, so decimal points ("2950.50") don't fake a sentence break.
    sentences = [s.strip() for s in re.split(r"[.!?]+(?=\s|$)", answer) if s.strip()]
    verdicts: list[Verdict] = []
    invented_tags: list[str] = []
    ungrounded_figures: list[str] = []
    misattributed: list[str] = []

    for sentence in sentences:
        cited = citation_tags(sentence)
        unknown = [c for c in cited if c not in issued_tags]
        invented_tags.extend(unknown)
        figures = extract_figures(sentence)
        grounded: set[float] = set()
        for c in cited:
            if c in by_tag:
                grounded |= _figure_keys(by_tag[c].text)

        ungrounded_in_sentence = [f for f in figures if normalise_number(f) not in grounded]
        ungrounded_figures.extend(ungrounded_in_sentence)

        # Misattribution
        cited_issuers = {_issuer_of(by_tag[c]) for c in cited if c in by_tag}
        cited_issuers.discard("")
        named = _issuers_named(sentence, issuer_index)
        if named:
            bad = named - cited_issuers
            misattributed.extend(bad)
        elif question_issuers and figures and not cited_issuers:
            misattributed.extend(question_issuers)

        # Determine provenance for this sentence
        if unknown:
            prov = GAP
            reason = f"cited tags never issued: {', '.join(unknown)}"
        elif ungrounded_in_sentence:
            prov = GAP
            reason = f"figures not in cited evidence: {', '.join(ungrounded_in_sentence)}"
        elif misattributed and any(m in named for m in misattributed):
            prov = GAP
            reason = "misattributed issuer"
        elif any(c in by_tag and by_tag[c].provenance == STATED for c in cited if c in by_tag):
            prov = STATED
            reason = "cited STATED evidence"
        elif any(c in by_tag and by_tag[c].provenance in (DERIVED,) for c in cited if c in by_tag):
            prov = DERIVED
            reason = "cited DERIVED evidence"
        elif any(c in by_tag and by_tag[c].provenance == INFERRED for c in cited if c in by_tag):
            prov = INFERRED
            reason = "cited INFERRED evidence"
        elif any(c in by_tag and by_tag[c].provenance == EXTERNAL for c in cited if c in by_tag):
            prov = EXTERNAL
            reason = "cited EXTERNAL evidence"
        else:
            prov = GAP
            reason = "no usable citation"

        verdicts.append(Verdict(
            text=sentence,
            provenance=prov,
            cites=cited,
            figures=figures,
            ungrounded=ungrounded_in_sentence,
            reason=reason,
            unknown_cites=unknown,
            misattributed=list(misattributed),
        ))

    return GradedAnswer(
        verdicts=verdicts,
        text=answer,
        invented_tags=invented_tags,
        ungrounded_figures=ungrounded_figures,
        misattributed=misattributed,
    )
