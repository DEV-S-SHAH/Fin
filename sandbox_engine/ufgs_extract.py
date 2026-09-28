"""UFGS extraction: as-filed facts, structural sections, and the causal layer.

This is the Universal Financial Graph Schema (UFGS-2026-09) implementation of
ingestion stages 1 through 4. :mod:`sandbox_engine.parser` produces the
question-answering graph; this module produces the universal schema from the
same bytes, so a filing is parsed once and lands in both layers.

    from sandbox_engine.ufgs_extract import extract_ufgs

    bundle = extract_ufgs(raw_html, source_path, metadata)

Zero-LLM, like the rest of the pipeline
----------------------------------------

Every extractor here is a deterministic function of the markup. The spec calls
for a domain-tuned NER model and a fine-tuned relation classifier in stage 4;
neither is available to a parser with no network and no model, so the causal
layer is a **cue-phrase classifier over a seeded gazetteer** instead. Every
relation it emits carries the sentence it came from in ``source_quote``, so a
false positive is auditable rather than merely present. The extractor is
deliberately conservative: it emits fewer relations than a model would, and
prefers a missing edge to an invented one, because a wrong causal claim in a
graph that a compliance officer queries is worse than an absent one.

What the dual track needs from the markup
-----------------------------------------

Inline XBRL is the point of this module. A Workiva filing hides its tagged
facts behind ``display:none`` and shows a rendered number in a table cell; the
tag, the context, the unit, the scale and the decimals live on the hidden
element. Reading the visible tables alone -- which is what the original graph
does -- throws the tag away, and the tag is the only thing the twenty-five
normalization rules can match on. So stage 1 walks the ``ix:`` elements, not
the tables.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import ufgs_schema as U
from .parser import clean_text, stable_id, strip_markup

__all__ = [
    "UFGSBundle",
    "extract_ufgs",
    "audit_status_for",
    "parse_ix_facts",
    "extract_sections",
    "extract_fiscal_periods",
    "extract_standardized_concepts",
    "extract_risk_factors",
    "item_slice",
    "plain_text",
]


def plain_text(value: str) -> str:
    """Cleaned, entity-resolved text for storing in a node property.

    ``clean_text`` collapses whitespace and strips tags but leaves character
    references alone, and Workiva markup uses them heavily -- 1114 non-breaking
    spaces in one Apple 10-K alone. A risk factor header stored as
    ``The Company&#8217;s operations...`` is technically the filing's text and
    useless to anyone reading the graph, so references are resolved before the
    string is stored rather than left for a query to interpret.
    """
    return unescape(clean_text(value))

log = logging.getLogger("sandbox_engine.ufgs_extract")


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


@dataclass
class UFGSBundle:
    """UFGS nodes and arcs for one filing, keyed by primary key.

    Same shape as the parser's :class:`~sandbox_engine.parser.ExtractionResult`
    but only for the universal layer, so the caller can merge it without this
    module knowing anything about the original graph.
    """

    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    footnotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_factors: dict[str, dict[str, Any]] = field(default_factory=dict)
    causal_relations: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Taxonomy members the filing's dimensional facts were qualified by.
    #: Merged with the original graph's ``Segment`` nodes rather than kept
    #: apart, so "Greater China" is one node whether it was found by reading
    #: the segment note's table or by reading a ``us-gaap`` context ref.
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    fiscal_periods: dict[str, dict[str, Any]] = field(default_factory=dict)
    restatements: dict[str, dict[str, Any]] = field(default_factory=dict)
    discontinued_segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    sector_overlays: dict[str, dict[str, Any]] = field(default_factory=dict)
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    entities: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    edges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    company: dict[str, Any] = field(default_factory=dict)
    filing: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "sections": len(self.sections),
            "raw_facts": len(self.raw_facts),
            "footnotes": len(self.footnotes),
            "risk_factors": len(self.risk_factors),
            "causal_relations": len(self.causal_relations),
            "fiscal_periods": len(self.fiscal_periods),
            "restatements": len(self.restatements),
            "discontinued_segments": len(self.discontinued_segments),
            "concepts": len(self.concepts),
            "sector_overlays": len(self.sector_overlays),
            "entities": sum(len(v) for v in self.entities.values()),
            **{name: len(rows) for name, rows in self.edges.items()},
        }


# ---------------------------------------------------------------------------
# Audit status -- a real invariant, not metadata
# ---------------------------------------------------------------------------

#: The spec's audit_status vocabulary. 10-Q statements are explicitly
#: unaudited and Chapter 7 depends on the distinction: when a 10-Q fact is
#: later restated in a 10-K/A, the original preliminary fact has to survive as
#: its own node. Collapsing "unaudited" into "audited" would erase the fact
#: that there was something to restate.
AUDIT_BY_FORM: dict[str, str] = {
    "10-K": "pcAOB_audited",
    "10-K/A": "pcAOB_audited",
    "10-Q": "unaudited",
    "10-Q/A": "unaudited",
    "8-K": "unaudited",
}


def audit_status_for(form_type: str | None) -> str:
    """Audit status for a form type, defaulting to unaudited.

    An 8-K carries no audited statements, and an unknown form is treated as
    unaudited rather than audited: the attribute exists to stop an unaudited
    number being read as audited, so an unrecognised form has to fail toward
    the stricter reading.
    """
    return AUDIT_BY_FORM.get((form_type or "").strip().upper(), "unaudited")


# ---------------------------------------------------------------------------
# Stage 1+2: structural sections
# ---------------------------------------------------------------------------

#: "Item 1A.", "ITEM 7A", "Item 1A - Risk Factors". Not matching "Items 10-16"
#: as a heading, and not matching "Item 402" (a 8-K exhibit index reference).
#:
#: The item suffix is *inside* the capture group on purpose. With the letter in
#: a non-capturing group the match text is right but ``group(1)`` is bare "1",
#: so Items 1, 1A, 1B and 1C all report the code "1" and the per-code dedup
#: below silently drops three of the four. Item 1A then has no Section, the
#: Item 1A slice comes back empty, and the risk-factor extractor falls back to
#: scanning the whole document and files the cover page as risk factors.
_ITEM_HEADING_RE = re.compile(
    r"\bITEM\s+(1[0-6](?:\s*[-–—]\s*16)?|[1-9][A-C]?)(?!\d)", re.I
)
#: A table-of-contents entry written with a dot leader, as older filers do.
_TOC_LEADER_RE = re.compile(r"^\s*[.\u2026]{2,}")
#: Consecutive item headings closer together than this are the same block, and a
#: block of several of them is a contents table. A 10-K body puts Items 1 and
#: 1A tens of thousands of characters apart; a contents page puts every item
#: within a couple of thousand.
_TOC_GAP_CHARS = 3000
#: A contents table lists several items. A lone heading is a body heading, so
#: this also keeps an 8-K -- which has no contents page at all -- from having
#: its only item heading discarded as one.
_MIN_TOC_ENTRIES = 3
#: Upper bound on sections per filing. A 10-K has 23; the cap is a guard
#: against a filing that mentions "Item" in running text producing hundreds of
#: false sections, not an expected limit.
_MAX_SECTIONS = 40


def _in_anchor(raw: str, offset: int) -> str | None:
    """The ``href`` of the ``<a>`` element containing *offset*, or ``None``.

    The enclosing element is located by the nearest preceding ``<a``, and the
    offset is inside it exactly when that tag has already closed and no
    ``</a>`` has appeared since.
    """
    anchor = raw.rfind("<a", 0, offset)
    if anchor < 0:
        return None
    tag_end = raw.find(">", anchor)
    if tag_end < 0 or tag_end > offset or "</a>" in raw[tag_end:offset]:
        return None
    href = _HREF_RE.search(raw[anchor + 2:tag_end])
    return href.group(1) if href else ""


#: Every element that Workiva stamps with an ``id``, mapped to its offset. The
#: contents page links to these; resolving the link is how a heading's position
#: in the *body* is recovered, rather than guessed from where the heading text
#: happens to appear.
_ANCHOR_ID_RE = re.compile(
    r"<[a-zA-Z][^>]*?\s(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I
)
_HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']*)[\"']", re.I)


def _anchor_index(raw: str) -> dict[str, int]:
    """``{fragment id: offset}`` for every anchor target defined in *raw*."""
    return {m.group(1): m.start() for m in _ANCHOR_ID_RE.finditer(raw)}


def _disclosed_in(
    id_by_offset: dict[int, str],
    spans: Sequence[tuple[int, int]],
    note_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """``DISCLOSED_IN`` arcs: each fact to the note whose region contains it.

    Offsets decide it, so the edge states something checkable -- "this tagged
    number appears inside Note 4". Nothing is inferred from a fact's label
    resembling a note's title, which would attach a fact to whichever note
    happened to share a word with it.
    """
    if not spans or not note_ids or not id_by_offset:
        return []
    arcs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for offset in sorted(id_by_offset):
        fact_id = id_by_offset[offset]
        for index, (start, end) in enumerate(spans):
            if not (start <= offset < end):
                continue
            note_id = note_ids[index]
            key = (fact_id, note_id)
            if key not in seen:
                seen.add(key)
                arcs.append({
                    "from": fact_id,
                    "to": note_id,
                    "detail_type": "note_detail",
                })
            break
    return arcs


def _item_code_for(form_type: str, code: str) -> str | None:
    """The Section code for *code* under *form_type*, or ``None`` if invalid.

    Form-aware on purpose. A 10-Q has no Item 1A, but a 10-Q that says "see
    Item 1A of our Form 10-K" would otherwise acquire a Risk Factors Section
    pointing into a sentence of cross-reference text, and every extractor scoped
    to that "section" would then read the surrounding narrative. Validating
    against the form's own item table is what the spec's Chapter 2 is for.

    Items 10 through 16 collapse to a single ``10-16`` code, because the spec
    treats the proxy-incorporated back half as one section type carrying a
    forwarding flag rather than as seven absent ones. One Section with
    ``section_title`` naming the range is the honest representation: the 10-K
    body does not contain them.
    """
    code = code.upper()
    if form_type == "10-K" and code.isdigit() and 10 <= int(code) <= 16:
        return "10-16"
    table = U.ITEM_TITLES.get(form_type)
    if table is None:
        return code if code else None
    if code in table:
        return code
    # 10-Q Part II items are written "1.1".."1.6"; the heading regex only
    # captures the major number, so fall back to the major part.
    head = code.split(".")[0]
    return head if head in table else None


def extract_sections(raw: str, form_type: str) -> dict[str, dict[str, Any]]:
    """Structural ``Section`` nodes for a filing, in document order.

    A heading's position is taken from the anchor its contents entry links to,
    not from where the heading text was found. Both are necessary and neither
    alone is sufficient:

    * The heading text appears twice -- once in the contents table, once in the
      body -- and in an Apple 10-K it also appears in a second divider contents
      table, so "the first match", "the last match" and any spacing heuristic
      all land on the wrong one for some filer. Resolving ``href="#doc_52"``
      to the ``<div id="doc_52">`` that defines it is unambiguous.
    * A dot-leader contents page in an older filing has no links at all, so
      the anchor route is tried first and the heading route is the fallback.

    Offsets are character positions in the *raw markup*, which is what makes
    them worth storing: a query can then slice the source document to show the
    exact bytes a fact was read from. They are not byte offsets --
    ``char_start``/``char_end`` say so -- because the documents are UTF-8 with
    multi-byte characters and a byte offset computed on the decoded string
    would not index the file.
    """
    sections: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    targets = _anchor_index(raw)

    for match in _ITEM_HEADING_RE.finditer(raw):
        if len(sections) >= _MAX_SECTIONS:
            break
        item_code = _item_code_for(form_type, match.group(1))
        if item_code is None or item_code in seen:
            continue
        seen.add(item_code)
        tail = raw[match.end():match.end() + 40]

        href = _in_anchor(raw, match.start())
        resolved = targets.get(href.lstrip("#"), -1) if href else -1
        # A back-link from the body to the contents resolves too, so require
        # the target to sit *after* the link: the contents page precedes the
        # body, so only a contents link points forward into it.
        linked = 0 < resolved > match.start()
        start = resolved if linked else match.start()

        # The title is read from wherever the heading's text actually is. When
        # the position was resolved through a link, that is the body, not the
        # contents row the match was found in -- and the contents row is a table
        # cell, so reading there yields a fragment of the cell's markup.
        title_source = raw[start:start + 160] if linked else tail
        title = plain_text(_TAG_RE.sub(" ", title_source))
        title = re.split(r"(?<=[a-z])\.\s|\s{2,}", title.strip(), maxsplit=1)[0]
        title = title[:120].strip(" .:-")
        if not _is_prose(title):
            # The spec carries an authoritative title for every item code, so a
            # heading whose text could not be read is named from the taxonomy
            # rather than stored as markup.
            title = U.item_title(form_type, item_code)

        end = start + len(match.group(0)) + len(title)
        section_id = stable_id(form_type, item_code, str(start))
        sections[section_id] = {
            "id": section_id,
            "form_type": form_type,
            "item_code": item_code,
            "section_title": title or U.item_title(form_type, item_code),
            "char_start": start,
            "char_end": end,
            "char_count": end - start,
        }

    return _drop_contents_sections(sections, raw)


#: A heading title is words, not a fragment of a ``<td>``. Checked rather than
#: assumed because stripping tags from a window that starts mid-tag leaves the
#: tag's attributes behind as text, and a Section titled ``<td colspan="3" s``
#: is worse than one titled from the taxonomy.
_PROSE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ,'’&()\-/]{3,}$")


def _is_prose(value: str) -> bool:
    return bool(value) and bool(_PROSE_RE.match(value))


def _drop_contents_sections(
    sections: dict[str, dict[str, Any]], raw: str
) -> dict[str, dict[str, Any]]:
    """Discard Sections whose offsets did not resolve to a body anchor.

    Only reached when no contents entry resolved at all -- the older dot-leader
    layout. A leading run of headings packed inside a few thousand characters
    is the contents page in that layout, and its offsets point at a table of
    contents rather than at a section. Cut repeatedly, because a 10-K can carry
    a contents table at each part divider.
    """
    ordered = sorted(sections.values(), key=lambda s: s["char_start"])
    if len(ordered) < _MIN_TOC_ENTRIES:
        return sections
    cut = 0
    while cut < len(ordered):
        run = cut
        while (run + 1 < len(ordered)
               and ordered[run + 1]["char_start"] - ordered[run]["char_start"]
               < _TOC_GAP_CHARS):
            run += 1
        if run - cut + 1 < _MIN_TOC_ENTRIES:
            break
        cut = run + 1
    if cut == 0 or cut >= len(ordered):
        return sections
    dropped = ordered[:cut]
    log.debug("dropped %d contents-page section(s): %s", len(dropped),
              [s["item_code"] for s in dropped])
    return {
        sid: section for sid, section in sections.items()
        if section["char_start"] >= ordered[cut]["char_start"]
    }


def section_for_offset(
    sections: Sequence[dict[str, Any]], offset: int
) -> dict[str, Any] | None:
    """The section whose document range contains *offset*.

    Nearest-preceding wins. Ranges are the heading, not the whole item, so the
    first fact after a heading has no containing range and would otherwise be
    orphaned -- and an orphaned fact loses the provenance the dual track exists
    to preserve.
    """
    best: dict[str, Any] | None = None
    for section in sections:
        if section["char_start"] <= offset:
            if best is None or section["char_start"] > best["char_start"]:
                best = section
    return best


def item_slice(
    raw: str, sections: dict[str, dict[str, Any]], item_code: str
) -> str:
    """The raw markup of one item, from its heading to the next one.

    ``Section.char_start`` records where an item *begins*; the span an item
    actually covers runs to the next item's heading, because the document
    order of the item codes is the only reliable terminator. Used to scope the
    narrative extractors, so Item 1A does not absorb the cover page.
    """
    ordered = sorted(sections.values(), key=lambda s: s["char_start"])
    start = None
    for section in ordered:
        if section["item_code"] == item_code:
            start = section["char_start"]
            break
    if start is None:
        return ""
    end = len(raw)
    for section in ordered:
        if section["char_start"] > start:
            end = section["char_start"]
            break
    return raw[start:end]


# ---------------------------------------------------------------------------
# Stage 1+2: inline XBRL facts
# ---------------------------------------------------------------------------

_IX_FACT_RE = re.compile(
    r"<ix:non(?P<kind>Fraction|Numeric)\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:non"
    r"(?:Fraction|Numeric)>",
    re.S | re.I,
)
_ATTR_RE = re.compile(r"([A-Za-z:]+)\s*=\s*\"([^\"]*)\"")
_TAG_RE = re.compile(r"<[^>]+>")
_NUMERIC_CLEAN_RE = re.compile(r"[^\d.eE+\-]")
_CONTEXT_RE = re.compile(
    r"<xbrli:context[^>]*\bid=[\"'](?P<id>[^\"']+)[\"'][^>]*>(?P<body>.*?)</xbrli:context>",
    re.S | re.I,
)
_CTX_START_RE = re.compile(r"<xbrli:startDate>\s*([^<]+?)\s*<", re.I)
_CTX_END_RE = re.compile(r"<xbrli:endDate>\s*([^<]+?)\s*<", re.I)
_CTX_INSTANT_RE = re.compile(r"<xbrli:instant>\s*([^<]+?)\s*<", re.I)
_CTX_DIMS_RE = re.compile(r"<xbrldi:(?:explicitMember|typedMember)[^>]*>([^<]*)<", re.I)
#: Dimension *axis* -> member, not just the member. Without the axis a fact
#: cannot be told apart: "IPhoneMember" is a product, "USMember" is a place,
#: and "CorporateNonSegmentMember" is neither -- and the same member vocabulary
#: is reused across axes, so the member text alone is ambiguous.
_CTX_AXIS_RE = re.compile(
    r'<xbrldi:explicitMember[^>]*\bdimension="([^"]+)"[^>]*>([^<]*)<', re.I
)

#: Segment-axis members -> ``(canonical segment name, segment_type)``.
#:
#: Keyed on the taxonomy's own member names rather than on the filer's wording,
#: which is the point: "iPhone", "Mac" and "Greater China" are what Apple's
#: tables *say*, while ``IPhoneMember`` and ``GreaterChinaSegmentMember`` are
#: what every filer that reports the same breakdown *tags*. A word list of
#: product names would have to be extended per issuer; this does not.
_SEGMENT_MEMBERS: dict[str, tuple[str, str]] = {
    # geography
    "AmericasSegmentMember": ("Americas", "geographic"),
    "EuropeSegmentMember": ("Europe", "geographic"),
    "GreaterChinaSegmentMember": ("Greater China", "geographic"),
    "JapanSegmentMember": ("Japan", "geographic"),
    "RestOfAsiaPacificSegmentMember": ("Rest of Asia Pacific", "geographic"),
    "RestOfWorldSegmentMember": ("Rest of World", "geographic"),
    "USMember": ("U.S", "geographic"),
    "UnitedStatesMember": ("U.S", "geographic"),
    "CNMember": ("China", "geographic"),
    "ChinaMember": ("China", "geographic"),
    "JapanCountryMember": ("Japan", "geographic"),
    "TaiwanMember": ("Taiwan", "geographic"),
    "IndiaMember": ("India", "geographic"),
    "GermanyMember": ("Germany", "geographic"),
    "UnitedKingdomMember": ("United Kingdom", "geographic"),
    "OtherCountriesMember": ("Other countries", "geographic"),
    # products and services
    "IPhoneMember": ("iPhone", "product"),
    "IPadMember": ("iPad", "product"),
    "MacMember": ("Mac", "product"),
    "WatchMember": ("Apple Watch", "product"),
    "AirPodsMember": ("AirPods", "product"),
    "WearablesHomeandAccessoriesMember": (
        "Wearables, Home and Accessories", "product",
    ),
    "ProductMember": ("Products", "product"),
    "ServiceMember": ("Services", "product"),
    "CorporateNonSegmentMember": ("Corporate", "geographic"),
    "OperatingSegmentsMember": ("Operating Segments", "geographic"),
}

#: Axes that carry a breakdown worth storing. The other 20-odd axes in a filing
#: are the fair-value hierarchy, the equity components and the debt types --
#: real structure, but not a segment taxonomy, and attaching them to
#: ``Segment`` would put "Level 2" next to "Greater China" as a peer.
_SEGMENT_AXES: tuple[str, ...] = (
    "us-gaap:StatementBusinessSegmentsAxis",
    "srt:StatementGeographicalAxis",
    "srt:ProductOrServiceAxis",
    "us-gaap:StatementGeographicalAxis",
)


def _member_segment(axis: str, member: str) -> tuple[str, str] | None:
    """``(segment name, segment_type)`` for a dimension, or ``None``.

    The member is matched on its last dotted component, because filers write
    the same taxonomy member under different namespaces and prefixes
    (``srt:AmericasSegmentMember``).
    """
    if not any(axis.endswith(known.split(":")[-1]) for known in _SEGMENT_AXES):
        return None
    leaf = member.strip().split(":")[-1].split("/")[-1]
    return _SEGMENT_MEMBERS.get(leaf)

#: Facts carrying a dimensional qualifier are segment- or product-level
#: breakdowns, not consolidated line items. They are kept, because the spec's
#: causal layer needs the product view, but counted separately so a run report
#: can tell a filer that tagged 1,100 consolidated facts from one that tagged
#: 1,100 facts almost all of which are dimensional.


def parse_ix_facts(raw: str) -> list[dict[str, Any]]:
    """Every inline-XBRL fact in *raw*, with its context resolved.

    The returned dicts are not nodes yet -- they carry the markup offsets and
    the context id so the caller can attribute a fact to a section and key it
    deterministically.
    """
    contexts: dict[str, dict[str, Any]] = {}
    for match in _CONTEXT_RE.finditer(raw):
        body = match.group("body")
        instant = _CTX_INSTANT_RE.search(body)
        start = _CTX_START_RE.search(body)
        end = _CTX_END_RE.search(body)
        if instant:
            period_type = "instant"
            period_start, period_end = "", instant.group(1)
        elif start and end:
            period_type = "duration"
            period_start, period_end = start.group(1), end.group(1)
        else:
            period_type = ""
            period_start = period_end = ""
        contexts[match.group("id")] = {
            "period_type": period_type,
            "period_start": period_start,
            "period_end": period_end,
            "dimensions": sorted(set(_CTX_DIMS_RE.findall(body))),
            "axis_members": [
                (axis, member.strip().split(":")[-1].split("/")[-1])
                for axis, member in _CTX_AXIS_RE.findall(body)
            ],
        }

    facts: list[dict[str, Any]] = []
    for match in _IX_FACT_RE.finditer(raw):
        attrs = dict(_ATTR_RE.findall(match.group("attrs")))
        tag = attrs.get("name", "")
        if not tag:
            continue
        context = contexts.get(attrs.get("contextRef", ""), {})
        is_numeric = match.group("kind").lower() == "fraction"

        text = plain_text(_TAG_RE.sub("", match.group("body")))
        value: float | None = None
        if is_numeric:
            # Only strip separators from a cell that is *entirely* a number.
            # Deleting every non-numeric character from "12abc34" yields
            # "1234", which is a confident wrong answer to a revenue question
            # rather than an obviously missing one.
            candidate = text.replace(",", "").replace(" ", "").strip()
            if candidate and not any(ch.isalpha() for ch in candidate):
                try:
                    value = float(_NUMERIC_CLEAN_RE.sub("", candidate) or candidate)
                except ValueError:
                    value = None
            if value is not None:
                if attrs.get("sign") == "-":
                    value = -value
                scale = attrs.get("scale")
                # Inline XBRL reports 4.0 with scale="12" to mean four trillion
                # dollars. Ignoring the scale stores a value twelve orders of
                # magnitude too small, which then sorts below every other fact.
                if scale:
                    try:
                        value *= 10 ** int(scale)
                    except ValueError:
                        pass
        facts.append({
            "xbrl_tag": tag,
            "context_ref": attrs.get("contextRef", ""),
            "unit_ref": attrs.get("unitRef", ""),
            # ``scale`` is absent on most facts, which means "no scaling
            # applied" -- 0, not unknown. An absent ``decimals`` is genuinely
            # unknown, so it takes the documented -1 alongside INF.
            "scale": _int_or_none(attrs.get("scale")) or 0,
            "decimals": _decimals_of(attrs.get("decimals")),
            "is_numeric": is_numeric,
            "text": text,
            "value": value,
            "offset": match.start(),
            "as_filed_label": _row_label(raw, match.start()),
            **context,
        })
    return facts


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


#: XBRL writes ``decimals="INF"`` for a fact reported to unlimited precision,
#: which is a positive statement about the number rather than a missing value.
#: It is stored as the same -1 used for "could not read", and that conflation
#: is deliberate-but-documented rather than accidental: a query that needs to
#: distinguish them can look at the tag, and a query that treats -1 as
#: "unknown or infinite" is right either way. The alternative -- letting "INF"
#: reach the integer coercion -- produced a per-row warning and a silent -1
#: without recording that the filing said INF.
INFINITE_PRECISION = -1


def _decimals_of(raw_value: str | None) -> int:
    """The ``decimals`` attribute as an int; ``-1`` for INF or unreadable."""
    if raw_value is None or str(raw_value).strip() == "":
        return INFINITE_PRECISION
    if str(raw_value).strip().upper() == "INF":
        return INFINITE_PRECISION
    parsed = _int_or_none(raw_value)
    return INFINITE_PRECISION if parsed is None else parsed


#: How far back to look for the row label. Inline XBRL puts the concept label
#: and the fact in the same <tr>, usually within a few hundred characters.
#: A three-year statement column is styled heavily enough that the label sits
#: 1-2 KB from the fact, so the window is generous and the row test below is
#: what actually keeps the label from reaching into the previous row.
_LABEL_WINDOW = 1200
#: Longest run of markup a statement row may span and still be worth reading
#: the label out of.
_MAX_ROW_CHARS = 6000


def _row_label(raw: str, offset: int) -> str:
    """The text immediately before a fact, as its as-filed label.

    Bounded by the enclosing ``<tr>`` when there is one, so a fact never
    inherits the previous row's label. The window is a compromise: inline XBRL
    puts the label and the fact in the same row but not adjacent, and reading
    the whole row would capture the neighbouring period's value too.

    The window is truncated to the last ``>`` before cutting. A window that
    starts mid-tag leaves the tag's attribute values behind as text -- the
    first fact in a filing then labels itself
    ``gov/inlineXBRL/transformation/2015-08-31" xmlns:us-gaap="http://...``,
    which is the document's XML prolog, not a concept label.
    """
    row_start = raw.rfind("<tr", 0, offset)
    from_row = row_start >= 0 and offset - row_start <= _MAX_ROW_CHARS
    if from_row:
        segment = raw[row_start:offset]
    else:
        # Fixed-width window: trim to the last tag boundary, because the cut
        # will usually land mid-tag and the tag's attribute values would
        # otherwise survive as text -- the first fact in a filing then labels
        # itself ``gov/inlineXBRL/transformation/...xmlns:us-gaap="http://...``,
        # which is the document's XML prolog, not a concept label.
        window_start = max(0, offset - _LABEL_WINDOW)
        segment = raw[window_start:offset]
        cut = segment.rfind(">")
        if cut >= 0:
            segment = segment[cut + 1:]
    # Non-breaking spaces are emitted as ``&#160;`` throughout Workiva markup.
    # They have to become spaces *before* the markup-residue test, or the
    # leading ``&`` of the entity trips it and the label is discarded for
    # containing a non-breaking space.
    text = plain_text(_TAG_RE.sub(" ", segment))
    text = re.sub(r"&#160;|&nbsp;|&#xa0;", " ", text, flags=re.I)
    text = clean_text(text)
    if not text:
        return ""
    # A label is prose, not markup: a leftover quote or an unconsumed entity
    # means the window was all attributes and there was no label to find.
    if "<" in text or "xmlns" in text or '"' in text or "&" in text:
        return ""
    # The label is everything before the first number in the row. A row reads
    # "Total net sales $ 416,161 $ 391,035 <fact>": the label is the words
    # before the first figure, so trimming only the *trailing* numerics would
    # leave "Total net sales 416,161" -- the caption plus last period's value.
    #
    # "Contains a digit" is the test, not "consists of digits". A digit-set
    # membership test has to be phrased as a scan, and phrased as a subtraction
    # it deletes every letter -- "Products" minus every character that is not a
    # digit or an exponent marker is the empty string, so the scan reports the
    # caption itself as numeric and stops before reading it.
    words: list[str] = []
    for word in text.split():
        if any(ch.isdigit() for ch in word) or word in {"$", "%", "(", ")"}:
            break
        words.append(word)
    if not words:
        return ""
    return " ".join(words)[-160:].strip(" .:-$")


#: XBRL unit ids to the spec's ``unit`` vocabulary. ``pure`` is the XBRL unit
#: for a dimensionless ratio, which is how EPS and margins are reported.
_UNIT_MAP: dict[str, str] = {
    "usd": "USD", "shares": "shares", "pure": "ratio",
    "usd/shares": "USD/shares", "iso4217:usd": "USD",
    "usd/sh": "USD/shares", "xbrli:shares": "shares",
    "iso4217:eur": "EUR", "iso4217:jpy": "JPY", "iso4217:gbp": "GBP",
    "n": "count", "decimals": "ratio", "text": "text",
}


def unit_for(unit_ref: str | None) -> str:
    """Canonical unit for a ``unitRef``, or the raw ref when unmapped."""
    if not unit_ref:
        return ""
    key = unit_ref.strip().lower()
    return _UNIT_MAP.get(key, unit_ref.strip())


#: Tags whose facts are discontinued-operations figures under ASC 205-20. The
#: spec tags these with ``continuing_ops_flag = false`` and uses the flag for
#: restatement handling, because a comparative period restated to exclude a
#: disposed business has to stay distinguishable from the original.
_DISC_OPS_TAG_RE = re.compile(
    r"DiscontinuedOperation|DiscontinuedOps|IncomeLossFromDiscontinuedOperationsNetOfTax",
    re.I,
)


def build_raw_facts(
    facts: Iterable[dict[str, Any]],
    form_type: str,
    sector: str | None,
    sections: Sequence[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[int, str],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    """``(nodes, reported_in, normalizes_to, id_by_offset, segments, breakdowns)``.

    Identity is ``(tag, context, value)``: the same tag in the same context
    with the same value is one fact, which is what keeps a 51-occurrence
    ``us-gaap:Revenues`` tag across 17 contexts from becoming 51 concepts
    while still preserving the per-context rows.

    ``id_by_offset`` is the raw-markup position of each fact mapped to the id
    it was given. It exists so a fact can be located inside a footnote's
    region later; recomputing the id at that point would risk a second,
    subtly different definition of the same key.

    ``segments`` are the taxonomy members the dimensional facts were qualified
    by, and ``breakdowns`` the arcs from fact to member. Both are keyed by
    canonical name so they resolve to the same ``Segment`` nodes the table
    parser creates from the visible segment note -- one "Greater China", not
    two.
    """
    audit = audit_status_for(form_type)
    nodes: dict[str, dict[str, Any]] = {}
    reported_in: list[dict[str, Any]] = []
    normalizes: list[dict[str, Any]] = []
    id_by_offset: dict[int, str] = {}
    segments: dict[str, dict[str, Any]] = {}
    breakdowns: list[dict[str, Any]] = []

    for fact in facts:
        tag = fact["xbrl_tag"]
        label = fact.get("as_filed_label", "")
        fact_id = stable_id(tag, fact.get("context_ref", ""),
                            str(fact.get("value")), str(fact.get("text"))[:40])
        if fact_id in nodes:
            continue
        nodes[fact_id] = {
            "id": fact_id,
            "xbrl_tag": tag,
            "as_filed_label": label,
            "reported_value": fact.get("value"),
            "unit": unit_for(fact.get("unit_ref")),
            "period_type": fact.get("period_type", ""),
            "period_start": fact.get("period_start", ""),
            "period_end": fact.get("period_end", ""),
            "audit_status": audit,
            "presentation_basis": "as_filed",
            "scale": fact.get("scale") or 0,
            "decimals": fact.get("decimals", INFINITE_PRECISION),
            "context_ref": fact.get("context_ref", ""),
            # Lower-case rather than Python's ``True``: the column is a string,
            # and ``str(True)`` writes "True" into a graph every other boolean
            # in it spells the other way. A filter written as
            # ``continuing_ops = 'true'`` has to match what is stored.
            "continuing_ops": "true" if not _DISC_OPS_TAG_RE.search(tag) else "false",
        }

        section = section_for_offset(sections, fact.get("offset", 0))
        if section is not None:
            reported_in.append({
                "from": fact_id,
                "to": section["id"],
                "item_code": section["item_code"],
            })

        for hit in U.match_rule(tag, label, sector):
            normalizes.append({
                "from": fact_id,
                "to": hit["concept_id"],
                "rule_id": hit["rule_id"],
                "transform": hit["transform"],
                "matched_on": hit["matched_on"],
                "matched_value": hit["matched_value"],
            })
        id_by_offset.setdefault(fact.get("offset", -1), fact_id)

        # Attach the taxonomy breakdown the fact was qualified by. A context
        # may carry several axes (a product within a geography, say), and each
        # is recorded, so a fact can be reached from either direction.
        for axis, member in fact.get("axis_members", ()):
            resolved = _member_segment(axis, member)
            if resolved is None:
                continue
            name, kind = resolved
            segments.setdefault(name, {"name": name, "segment_type": kind})
            arc = {
                "from": fact_id,
                "to": name,
                "axis": axis.split(":")[-1],
                "member": member,
            }
            if arc not in breakdowns:
                breakdowns.append(arc)
    return nodes, reported_in, normalizes, id_by_offset, segments, breakdowns


# ---------------------------------------------------------------------------
# Standardized concepts
# ---------------------------------------------------------------------------


def extract_standardized_concepts(sector: str | None) -> dict[str, dict[str, Any]]:
    """Materialise the applicable canonical anchors as nodes.

    The universal layer plus the filer's own overlay. A technology filer gets
    33 of the 40; the four banking-only regulatory metrics are simply absent,
    which is the overlay architecture working -- not empty nodes asserting that
    Apple reports a Tier 1 capital ratio.
    """
    return {
        concept.concept_id: {
            "concept_id": concept.concept_id,
            "name": concept.name,
            "definition": concept.definition,
            "statement_type": concept.statement_type,
            "sector_applicability": concept.sector_applicability,
        }
        for concept in U.concepts_for_sector(sector)
    }


# ---------------------------------------------------------------------------
# Fiscal periods
# ---------------------------------------------------------------------------


def extract_fiscal_periods(
    metadata: dict[str, Any], facts: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """``FiscalPeriod`` nodes for the periods a filing reports on.

    The spec makes a period a node because "fiscal year 2023" is ambiguous
    across a corpus: NVDA's year ends the last Sunday of January and AAPL's the
    last Saturday of September, so the label means a different date range per
    filer. ``calendar_year_overlap`` is the field that reconciles them, and it
    is computed from the *contexts in the filing* rather than from the calendar,
    so a 52/53-week year is handled by the filer's own dates.

    An 8-K gets none. It reports an event, not a period, and its ``fiscal_year``
    is inherited from the cover page -- so keying a period on
    ``(ticker, fiscal_year, fiscal_period)`` gives the 8-K the *same* key as the
    annual report for that year, and the two filings silently collapse into one
    FiscalPeriod carrying whichever one was loaded second.
    """
    form_type = (metadata.get("form_type") or "").upper()
    if form_type in ("8-K", "8-K/A"):
        return {}

    fiscal_year = metadata.get("fiscal_year")
    fiscal_period = metadata.get("fiscal_period") or "FY"
    ticker = metadata.get("ticker") or ""

    # Start and end must describe the *same* span. Reading the end from the
    # cover and the start from an unrelated context pairs this quarter's end
    # with last year's start, which is how a 10-Q ends up wearing a 12-month
    # window. So the end is fixed first and the start is taken from a context
    # that actually ends there.
    period_end = metadata.get("period_end") or metadata.get("period_end_date") or ""

    def _span_days(fact: dict[str, Any]) -> int:
        try:
            start = dt.date.fromisoformat(str(fact.get("period_start"))[:10])
            end = dt.date.fromisoformat(str(fact.get("period_end"))[:10])
        except (TypeError, ValueError):
            return -1
        return (end - start).days

    spans = [
        fact
        for fact in facts
        if fact.get("period_type") == "duration"
        and fact.get("period_start")
        and fact.get("period_end")
        and (not period_end or str(fact.get("period_end"))[:10] == period_end[:10])
    ]
    spans = [f for f in spans if _span_days(f) >= 0]

    if spans:
        # A 10-K's period is the year; a 10-Q's is the quarter, and the quarter
        # is the *shortest* span ending on the period end (a Q3 filing also
        # carries a nine-month column, which is longer and not the filing's
        # own period). 8-Ks never reach here.
        spans.sort(key=_span_days, reverse=(form_type == "10-K"))
        period_start = str(spans[0].get("period_start"))[:10]
        if not period_end:
            period_end = str(spans[0].get("period_end"))[:10]
    else:
        period_start = ""
    if not period_end:
        period_end = metadata.get("filing_date") or ""

    period_id = stable_id(ticker, str(fiscal_year), str(fiscal_period))
    overlap = U.calendar_year_overlap(period_start, period_end)
    reporting_lag = _reporting_lag(period_end, metadata.get("filing_date"))

    nodes = {
        period_id: {
            "id": period_id,
            "fiscal_year": fiscal_year if isinstance(fiscal_year, int) else "",
            "fiscal_quarter": fiscal_period,
            "period_end_date": period_end,
            # -1 is the fiscal-year sentinel, shared with Filing.fiscal_year, so
            # a query filtering one filters the other.
            "calendar_year_overlap": overlap if overlap is not None else -1,
            "reporting_lag_in_days": reporting_lag if reporting_lag is not None else -1,
            "period_start": period_start,
        }
    }
    return nodes


def _reporting_lag(period_end: str | None, filing_date: str | None) -> int | None:
    """Days between a period end and its filing.

    The spec uses this to adjust for information asymmetry: a fact NVDA
    reports in late February reflects a period that ended six weeks earlier
    than a bank's December-31 period reported the same morning, so the two are
    not equally current. Returns ``None`` rather than 0 when either date is
    unreadable -- 0 would claim the filing landed the day the period closed.
    """
    def _d(value: str | None) -> dt.date | None:
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    end, filed = _d(period_end), _d(filing_date)
    if end is None or filed is None:
        return None
    return (filed - end).days


# ---------------------------------------------------------------------------
# Footnotes
# ---------------------------------------------------------------------------

#: A numbered note heading. The separator is required and must not be a comma:
#: that single constraint is what separates a heading ("Note 1 - Revenue") from
#: the cross-references that pepper a filing ("see Note 7, "Income Taxes" in the
#: Notes to Consolidated Financial Statements"), which match the same
#: "Note <n>" shape and would otherwise be captured as notes of their own.
#:
#: Both dash characters are listed because filers use both, and the numeric
#: entities as well, because the markup is served ASCII-encoded: Apple's 10-K
#: writes the separator as ``&#8211;`` and the file contains no non-ASCII byte
#: at all, so a class covering only U+2013/U+2014 misses every note in it.
_NOTE_HEAD_RE = re.compile(
    r"\bNotes?\s+(\d{1,2})\b\s{0,3}"
    r"(?:[.\u2013\u2014:\-]|&#8211;|&#8212;|&#x201[34];)\s*",
    re.I,
)
#: A footnote is capped: the notes this schema cares about are the ones a fact
#: is detailed in, and an unbounded capture swallows the next note, the next
#: section, and half the filing.
_MAX_NOTE_CHARS = 1500
_MAX_NOTES = 60


def extract_footnotes(
    raw: str, form_type: str, filing_id: str
) -> tuple[dict[str, dict[str, Any]], list[tuple[int, int]]]:
    """Financial-statement footnotes, as ``(nodes, spans)``.

    Runs over the *markup* rather than the rendered text, and the spans come
    back with the nodes because they are what make ``DISCLOSED_IN`` real. A
    note is a contiguous region of the document, and an inline-XBRL fact inside
    that region is one the note explains; comparing offsets is the only way to
    say which. Working from rendered text would throw the offsets away and
    leave the fact-to-note edge unimplementable.

    Numbered-note headings are the anchor because that is the only structure
    every filer shares. ``note_type`` is inferred from the heading's subject
    rather than from position, since a note's position in the sequence is not
    its kind.
    """
    if form_type not in ("10-K", "10-K/A", "10-Q", "10-Q/A"):
        return {}, []

    notes: dict[str, dict[str, Any]] = {}
    spans: list[tuple[int, int]] = []
    matches = list(_NOTE_HEAD_RE.finditer(raw))
    for index, match in enumerate(matches):
        if len(notes) >= _MAX_NOTES:
            break
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        if end - match.end() < 400:
            # A heading with no body behind it is a cross-reference, not a note.
            continue
        number = match.group(1)
        title, body = _split_note(raw, match.end())
        if len(body) < 80:
            continue
        note_id = stable_id(filing_id, number, title[:40])
        if note_id in notes:
            continue
        notes[note_id] = {
            "id": note_id,
            "note_number": number,
            "note_title": title[:200],
            "note_text": body[:_MAX_NOTE_CHARS],
            "note_type": _note_type(title),
        }
        spans.append((match.start(), end))
    return notes, spans


_NOTE_KINDS: tuple[tuple[str, str], ...] = (
    ("segment", "segment_reporting"),
    ("revenue", "revenue_recognition"),
    ("inventor", "inventory"),
    ("property, plant", "fixed_assets"),
    ("goodwill", "goodwill_and_intangibles"),
    ("intangible", "goodwill_and_intangibles"),
    ("debt", "debt_and_credit"),
    ("borrow", "debt_and_credit"),
    ("credit facilit", "debt_and_credit"),
    ("commercial paper", "debt_and_credit"),
    ("lease", "leases"),
    ("incometax", "income_taxes"),
    ("income tax", "income_taxes"),
    ("stock-based", "stock_based_compensation"),
    ("share-based", "stock_based_compensation"),
    ("employee benefit", "retirement_and_benefits"),
    ("pension", "retirement_and_benefits"),
    ("commitment", "commitments"),
    ("equity", "stockholders_equity"),
    ("earnings per share", "earnings_per_share"),
    ("discontinued", "discontinued_operations"),
    ("restructuring", "restructuring"),
)


def _note_type(title: str) -> str:
    low = title.lower()
    for needle, kind in _NOTE_KINDS:
        if needle in low:
            return kind
    return "other"


def _split_note(raw: str, start: int) -> tuple[str, str]:
    """``(title, body)`` for a note whose heading ended at *start*.

    Both are taken from the markup and cleaned: the title is the heading's own
    line, the body everything up to the next numbered heading. Stripping tags
    from the body only after slicing keeps the ``char`` budget on readable text
    rather than on markup density.
    """
    head = plain_text(_TAG_RE.sub(" ", raw[start:start + 220]))
    # Cut at the first surviving "<". The tag pattern only matches *closed*
    # tags, so a window that ends mid-tag leaves "<ix:continuation id=..."
    # behind, and a note titled "Financial Instruments <ix:continuation id=..."
    # is worse than one titled from its own first line.
    head = head.split("<", 1)[0]
    title = re.split(r"(?<=[a-z])\.\s|\n", head.strip(), maxsplit=1)[0][:200]
    body = plain_text(_TAG_RE.sub(" ", raw[start:start + _MAX_NOTE_CHARS]))
    body = body.split("<", 1)[0]
    return title, body


# ---------------------------------------------------------------------------
# Causal layer: risk factors and entities
# ---------------------------------------------------------------------------

#: A risk factor header is a bolded run inside Item 1A. The spec describes the
#: structure as "a numbered sub-section beginning with a topic-defining header
#: ... followed by 2-6 sentences of elaboration", and filers mark the header
#: exactly that way.
#:
#: The style is matched in a lookahead with the two quote styles handled
#: separately, because a Workiva style attribute is full of single quotes --
#: ``font-family:'NVIDIA Sans',sans-serif`` -- and a single character class
#: excluding both quote types cannot span them. That is not a theoretical edge
#: case: it is every NVIDIA filing, and the class silently matched nothing.
_BOLD_STYLE = (
    r"style\s*=\s*(?:\"[^\"]*font-weight\s*:\s*(?:bold|[6-9]00)[^\"]*\""
    r"|'[^']*font-weight\s*:\s*(?:bold|[6-9]00)[^']*')"
)
_RF_HEADER_RE = re.compile(
    r"<(?:span|p|b|em|i|div)\b(?=[^>]*" + _BOLD_STYLE + r")[^>]*>"
    r"(?P<text>.*?)</(?:span|p|b|em|i|div)>",
    re.S | re.I,
)
_MAX_RISK_FACTORS = 60
_MIN_RF_CHARS = 120

#: The seven narrative entity types, as a seeded gazetteer of surface forms.
#: The spec derives these with a domain-tuned NER model; a gazetteer is the
#: zero-LLM stand-in and is biased toward high-frequency, unambiguous names --
#: a regulator or a macro driver is named the same way in every filing, while
#: a product family is not, which is why the gazetteer is strongest on
#: ``RegulatoryBody`` and ``MacroVariable`` and thinnest on ``ProductFamily``.
_GAZETTEER: dict[str, tuple[tuple[str, str | None, str], ...]] = {
    "RegulatoryBody": (
        (r"\bSecurities and Exchange Commission\b|\bSEC\b", "US", "securities_regulation"),
        (r"\bFood and Drug Administration\b|\bFDA\b", "US", "drug_safety"),
        (r"\bEnvironmental Protection Agency\b|\bEPA\b", "US", "environmental"),
        (r"\bFederal Trade Commission\b|\bFTC\b", "US", "competition"),
        (r"\bDepartment of Justice\b|\bDOJ\b", "US", "enforcement"),
        (r"\bBureau of Industry and Security\b|\bBIS\b", "US", "export_controls"),
        (r"\bFederal Open Market Committee\b|\bFOMC\b", "US", "monetary_policy"),
        (r"\bInternal Revenue Service\b|\bIRS\b", "US", "taxation"),
        (r"\bInternational Trade Commission\b|\bITC\b", "US", "trade"),
        (r"\bEuropean Union\b|\bEU\b|\bEU authorities\b", "EU", "competition"),
        (r"\bCourt of Appeals\b", "US", "litigation"),
    ),
    "MacroVariable": (
        (r"\binflation(?:ary)? (?:pressure|rate|environment)?\b", None, "inflation"),
        (r"\binterest rates?\b|\bFed Funds(?: Rate)?\b|\bmonetary polic(?:y|ies)\b",
         None, "interest_rate"),
        (r"\bexchange rates?\b|\bcurrency fluctuations?\b|\bUSD index\b",
         None, "currency"),
        (r"\bcommodity prices?\b|\bcrude oil prices?\b|\bBrent\b|\bnatural gas prices?\b",
         None, "commodity"),
        (r"\bexport controls?\b|\btariffs?\b|\btrade restrictions?\b|\bsanctions?\b",
         None, "policy"),
        (r"\bsupply chain\b", None, "supply_chain"),
        (r"\bconsumer demand\b|\bproduct demand\b|\bweaker demand\b", None, "demand"),
        (r"\brecession\b|\bearnings downturn\b", None, "macro_cycle"),
        (r"\bfluctuations? in demand\b", None, "demand"),
    ),
    "GeographicMarket": (
        (r"\bGreater China\b", "CN", "greater_china"),
        (r"\bChina\b", "CN", "country"),
        (r"\bEMEA\b", None, "region"),
        (r"\bLatin America\b", None, "region"),
        (r"\bEurope\b|\bEuropean\b", None, "region"),
        (r"\bJapan\b", "JP", "country"),
        (r"\bIndia\b", "IN", "country"),
        (r"\bTaiwan\b", "TW", "country"),
        (r"\bKorea\b", "KR", "country"),
        (r"\bHong Kong\b", "HK", "country"),
    ),
    "Supplier": (
        (r"\bTSMC\b|\bTaiwan Semiconductor\b", "TSM", "sole_source"),
        (r"\bfoundr(?:y|ies)\b", None, "primary"),
        (r"\bsupplier(?:s)?\b|\bsupply chain\b", None, "primary"),
        (r"\bthird-party (?:manufactur|supplier|vendor)", None, "primary"),
        (r"\bSK Hynix\b", None, "primary"),
        (r"\bpackaging (?:partners?|suppliers?)\b", None, "secondary"),
    ),
    "Customer": (
        (r"\bcustomers?\b", None, "disclosed"),
        (r"\bhyper?scalers?\b|\bcloud customers?\b", None, "primary"),
        (r"\benterprise customers?\b", None, "primary"),
        (r"\bconcentration\b", None, "disclosed"),
    ),
    "Competitor": (
        (r"\bcompetitors?\b|\bcompetitive\b", None, "disclosed"),
        (r"\bSamsung\b", None, "disclosed"),
        (r"\bIntel\b|\bAMD\b", None, "disclosed"),
        (r"\bMicrosoft\b|\bAmazon\b|\bGoogle\b", None, "disclosed"),
        (r"\bModerna\b", None, "disclosed"),
    ),
    "ProductFamily": (
        (r"\biPhone\b", "AAPL", "mature"),
        (r"\bMac\b|\biMac\b|\biPad\b|\bApple Watch\b|\bAirPods\b", "AAPL", "mature"),
        (r"\bH100\b|\bH200\b|\bBlackwell\b|\bGrace Hopper\b", "NVDA", "growth"),
        (r"\bdata cent(?:er|re) GPUs?\b|\baccelerated computing\b", "NVDA", "growth"),
        (r"\bInstinct\b", "AMD", "growth"),
        (r"\bcloud\b|\bcommercial cloud\b", None, "growth"),
    ),
}

#: Cue phrases -> relation type. Order matters only for reporting, not for
#: correctness: a sentence can match more than one cue and each match emits its
#: own relation, which is the honest reading of an ambiguous sentence.
_CAUSAL_CUES: tuple[tuple[str, str], ...] = (
    (r"\b(?:compress|compressed|pressure|pressure[d]?|reduce[ds]?|decline[ds]?|"
     r"lower[sd]?|contract(?:ed|ing)?|squeeze[ds]?)\b[^.]{0,80}?\b(?:margin|"
     r"profitability|gross profit)\b", "IMPACTS_MARGIN"),
    (r"\b(?:margin|gross profit|profitability)\b[^.]{0,80}?\b(?:compress|"
     r"pressure|declin|decreas|reduc|lower|contract)\w*\b", "IMPACTS_MARGIN"),
    (r"\b(?:drives?|driving|drove|driven by|result(?:s|ed)? in|led to|"
     r"contribut\w+ to)\b", "DRIVES"),
    (r"\bmitigat\w+\b|\bhelps? (?:to )?(?:mitigate|offset|reduce)\b", "MITIGATES"),
    (r"\b(?:exposes?|exposed to|exposure to|subject to the risks? of|"
     r"adversely affected by)\b", "CREATES_EXPOSURE_TO"),
    (r"\bcompounds?\b|\bamplif\w+\b|\bexacerbat\w+\b", "COMPOUNDS"),
    (r"\boffsets?\b|\boffsetting\b|\bpartially cancel\w*\b|\bcounterbalanc\w+\b",
     "OFFSETS"),
)

#: Severity is a lexical read of the header, not a model. The spec asks for a
#: trained classifier; these four cues are the ones filers use consistently
#: ("material", "significant", "could", "may").
_SEVERITY_CUES: tuple[tuple[str, str], ...] = (
    (r"\bmaterial(?:ly)?\b|\bsignificant(?:ly)?\b|\bsubstantial\b", "high"),
    (r"\bcould (?:have|materially|adversely|negatively)\b", "medium"),
    (r"\bmay\b|\bmight\b", "low"),
)
_DEFAULT_SEVERITY = "medium"

#: Cues that a factor is a *mitigation* of a risk rather than the risk itself.
#: Used to split the two relation directions the gazetteer would otherwise
#: conflate on a sentence like "long-term supply agreements mitigate supply
#: concentration risk", where the subject mitigates a risk it is named inside.
_MITIGATION_CUE_RE = re.compile(
    r"\bmitigat\w+|\bhelps? (?:to )?(?:mitigate|offset|reduce|lessen)\b|"
    r"\breduc\w+ (?:the )?risk\b",
    re.I,
)


def extract_risk_factors(
    raw: str,
    form_type: str,
    filing_id: str,
    ticker: str,
    item_slice: str = "",
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, dict[str, Any]]],
    list[tuple[str, dict[str, Any]]],
]:
    """``(risk_factors, causal_relations, entities, relation_edges)``.

    Item 1A only, and only for a 10-K: the spec notes the 2023 amendments
    changed the format and that pre- and post-2023 filings must be flagged, and
    a 10-Q has no Item 1A at all. 8-Ks have no risk factor section either --
    their events are already handled by the original graph's ``Event`` nodes.

    *item_slice* is the raw markup of Item 1A, supplied by the caller from the
    ``Section`` nodes. Scanning the whole document instead picks up every bold
    run on the cover page -- "Washington, D.C. 20549", "Table of Contents" --
    and files each as a risk factor, which is how a filing ends up with thirty
    four "risk factors" of which four are real. The structural layer is what
    makes this scopeable, so it is used.
    """
    empty: dict[str, dict[str, Any]] = {}
    if form_type not in ("10-K", "10-K/A"):
        return empty, empty, {}, []

    source = item_slice or raw
    headers = [
        (m.start(), plain_text(_TAG_RE.sub(" ", m.group("text"))))
        for m in _RF_HEADER_RE.finditer(source)
    ]
    headers = [(offset, text[:300]) for offset, text in headers if len(text) >= 15]
    if not headers:
        return empty, empty, {}, []

    risk_factors: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}
    entities: dict[str, dict[str, dict[str, Any]]] = {}
    relation_edges: list[dict[str, Any]] = []

    for index, (offset, header) in enumerate(headers[:_MAX_RISK_FACTORS]):
        end = (headers[index + 1][0] if index + 1 < len(headers)
               else offset + 4000)
        body = plain_text(_TAG_RE.sub(" ", source[offset:end]))
        if len(body) < _MIN_RF_CHARS:
            continue
        # A bold run that opens with an item heading is a contents-table entry
        # or a table header, not a risk factor.
        if _ITEM_HEADING_RE.match(body):
            continue

        fiscal_year = ""
        rf_id = stable_id(filing_id, header[:60])
        if rf_id in risk_factors:
            continue

        found = _entities_in(body, ticker)
        risk_factors[rf_id] = {
            "id": rf_id,
            "item_code": "1A",
            "rf_header": header[:300],
            "rf_text": body[:2000],
            "extracted_entities": ";".join(
                sorted(name for _table, name, _node in found)
            )[:500],
            "severity": _severity(header),
            "year_disclosed": fiscal_year,
            "year_removed": "",
        }
        for table, name, node in found:
            entities.setdefault(table, {})[name] = node

        for relation_type, subject, quote, object_type, object_name in _relations_in(
            body, found
        ):
            rel_id = stable_id(rf_id, relation_type, subject, object_name)
            if rel_id in relations:
                continue
            relations[rel_id] = {
                "id": rel_id,
                "relation_type": relation_type,
                "subject_type": subject[0],
                "subject_name": subject[1],
                "object_type": object_type,
                "object_name": object_name,
                "weight": 0.0,
                "magnitude": 0.0,
                "source_quote": quote[:400],
                "context": "risk_factor",
            }
            for table, row in _relation_edges(rel_id, subject, relation_type,
                                              object_name):
                relation_edges.append((table, row))

    return risk_factors, relations, entities, relation_edges


def _severity(header: str) -> str:
    for pattern, level in _SEVERITY_CUES:
        if re.search(pattern, header, re.I):
            return level
    return _DEFAULT_SEVERITY


def _entities_in(
    body: str, ticker: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """``(table, name, node)`` for every gazetteer entity in *body*.

    The issuer's own product families are attributed to it, so "iPhone" is
    Apple-owned rather than an unowned string. A competitor's ticker is
    recorded when the gazetteer knows it, because that is what lets a query
    join a competitor to its own filing graph later.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for table, entries in _GAZETTEER.items():
        for pattern, detail, third in entries:
            match = re.search(pattern, body, re.I)
            if not match:
                continue
            name = clean_text(match.group(0))
            # The primary key of every entity node is ``name``, and the arcs
            # are written against the same string, so the key and the stored
            # value have to be the same string. Keying on the lowercased form
            # while storing the matched casing produces an arc to a node that
            # does not exist: "google" as the subject with "Google" as the key.
            # It also folds "Google"/"google" across filings onto one entity,
            # which is what a cross-filing graph wants.
            key = name.lower()
            if table == "MacroVariable":
                node = {"name": key, "variable_type": third}
            elif table == "RegulatoryBody":
                node = {"name": key, "jurisdiction": detail or "US", "scope": third}
            elif table == "GeographicMarket":
                node = {"name": key, "iso_code": detail or ""}
            elif table == "Competitor":
                node = {"name": key, "ticker": detail or "", "relation_strength": third}
            elif table == "Supplier":
                node = {"name": key, "relationship_type": third,
                        "criticality": third}
            elif table == "Customer":
                node = {"name": key, "concentration_pct": 0.0}
            else:  # ProductFamily
                node = {"name": key, "issuer": detail or ticker,
                        "launch_date": "", "lifecycle_stage": third}
            if any(key == existing[1] for existing in found):
                continue
            found.append((table, key, node))
    return found


def _relations_in(
    body: str, entities: Sequence[tuple[str, str, dict[str, Any]]]
) -> list[tuple[str, tuple[str, str], str, str, str]]:
    """Cue-phrase relations in *body*, as ``(type, subject, quote, obj_type, obj_name)``.

    Only entities the gazetteer actually found can be subjects: attributing a
    relation to an entity the sentence never names would make the graph
    assert a causal claim the filing does not make, which is the one failure
    mode the spec's typed relations exist to prevent.
    """
    if not entities:
        return []
    out: list[tuple[str, tuple[str, str], str, str, str]] = []
    for sentence in re.split(r"(?<=[.;])\s+", body):
        if not 40 <= len(sentence) <= 600:
            continue
        for pattern, relation_type in _CAUSAL_CUES:
            if not re.search(pattern, sentence, re.I):
                continue
            subject = next(
                ((table, name) for table, name, _ in entities
                 if re.search(re.escape(name.split()[0]), sentence, re.I)),
                None,
            )
            if subject is None:
                continue
            object_name = _outcome_in(sentence, relation_type)
            out.append((relation_type, subject, sentence.strip()[:400],
                        "financial_outcome", object_name))
            break
    return out


#: Cue -> the outcome phrase a relation points at. The outcome is a string on
#: the CausalRelation node rather than a StandardizedConcept edge when no rule
#: matched, because a cue phrase names a margin or a cost, not an anchor.
_OUTCOME_CUES: tuple[tuple[str, str], ...] = (
    (r"\bgross margin\b", "GrossMargin"),
    (r"\boperating margin\b", "OperatingMargin"),
    (r"\bmargin\b", "Margin"),
    (r"\brevenue\b|\bsales\b", "Revenue"),
    (r"\bprofitability\b", "Profitability"),
    (r"\bcosts?\b", "CostOfRevenue"),
    (r"\bdemand\b", "Demand"),
    (r"\bcapital (?:allocation|expenditure)\b", "CapitalExpenditure"),
    (r"\bcredit loss\w*\b", "ProvisionForCreditLosses"),
    (r"\binventory\b", "Inventory"),
)


def _outcome_in(sentence: str, relation_type: str) -> str:
    """The outcome phrase a relation points at, or ``""``.

    Empty is the honest answer when the sentence names no outcome. Echoing the
    relation name back (``DRIVES -> DRIVES``) would fill the column with a
    value that looks like a finding and is not one; the relation node itself
    already carries the assertion, so a missing outcome loses nothing.
    """
    for pattern, outcome in _OUTCOME_CUES:
        if re.search(pattern, sentence, re.I):
            return outcome
    return ""


def _relation_edges(
    rel_id: str,
    subject: tuple[str, str],
    relation_type: str,
    object_name: str,
) -> list[tuple[str, dict[str, Any]]]:
    """``(rel_table, row)`` for one causal relation.

    Two rows. The subject edge runs from the named entity into the reified
    statement, which is what keeps ``Supplier`` -> ``CausalRelation`` ->
    ``StandardizedConcept`` traversable. The outcome edge runs out of the
    statement under the relation's own name, and is emitted only when the cue
    phrase names a canonical anchor: the six tables have ``StandardizedConcept``
    as a fixed endpoint, so pointing ``DRIVES`` at a concept the forty-concept
    list does not define would create a dangling row the loader drops without
    a word. ``GrossMargin`` and ``Margin`` therefore produce the statement node
    and no outcome edge, rather than a fabricated anchor.
    """
    table, name = subject
    rows: list[tuple[str, dict[str, Any]]] = []
    subject_table = _SUBJECT_REL_TABLE.get(table)
    if subject_table:
        rows.append((subject_table, {"from": name, "to": rel_id}))

    concept = _concept_for_outcome(object_name)
    if concept and relation_type in U.CAUSAL_RELATION_TYPES:
        rows.append((relation_type, {
            "from": rel_id, "to": concept, "weight": 0.0, "magnitude": 0.0,
        }))
    return rows


#: Narrative entity type -> the rel table that attaches it to a statement.
_SUBJECT_REL_TABLE: dict[str, str] = {
    "ProductFamily": "SUBJECT_PRODUCT_FAMILY",
    "GeographicMarket": "SUBJECT_GEOGRAPHIC_MARKET",
    "Competitor": "SUBJECT_COMPETITOR",
    "Supplier": "SUBJECT_SUPPLIER",
    "Customer": "SUBJECT_CUSTOMER",
    "RegulatoryBody": "SUBJECT_REGULATORY_BODY",
    "MacroVariable": "SUBJECT_MACRO_VARIABLE",
}


_OUTCOME_CONCEPT: dict[str, str] = {
    "Revenue": "SC-01",
    "CostOfRevenue": "SC-02",
    "ProvisionForCreditLosses": "SC-09",
    "CapitalExpenditure": "SC-32",
    "Inventory": "SC-18",
}


def _concept_for_outcome(outcome: str) -> str | None:
    """Canonical concept for an outcome phrase, or ``None``.

    Only the handful the cue vocabulary names map cleanly. "Margin" is
    deliberately absent: the forty-concept list has no margin concept, and
    guessing between ``SC-03`` GrossProfit and ``SC-06`` OperatingIncome would
    be a fabrication dressed as an inference.
    """
    return _OUTCOME_CONCEPT.get(outcome)


# ---------------------------------------------------------------------------
# Restatements and discontinued operations
# ---------------------------------------------------------------------------

_AMENDMENT_RE = re.compile(r"^10-([KQ])/A$", re.I)
_RESTATEMENT_RE = re.compile(
    r"\brestat\w+\b|\bamendment (?:no\.?\s*)?\d+\b|"
    r"\bcorrection of (?:an? )?(?:error|material|previously issued)\b|"
    r"\bitem 4\.0[2-9]\b|\bexplanatory note\b",
    re.I,
)


def detect_restatement(
    form_type: str | None, text: str, filing_id: str
) -> dict[str, dict[str, Any]] | None:
    """A ``RestatementEvent`` for an amended filing, or ``None``.

    Gated on the form type, not on the keyword: the word "restatement" appears
    in the *forward-looking* risk disclosures of ordinary 10-Ks ("a restatement
    could expose us..."), so a keyword match alone would manufacture an audit
    event out of a risk factor. An amendment is the signal; the keyword only
    supplies the reason.
    """
    if not form_type or not _AMENDMENT_RE.match(form_type):
        return None
    match = _RESTATEMENT_RE.search(text[:200_000])
    reason = clean_text(match.group(0))[:300] if match else ""
    event_id = stable_id(filing_id, form_type)
    return {
        event_id: {
            "id": event_id,
            # The spec separates an error correction (RESTATES, high severity,
            # audit trail required) from a change in principle
            # (RETROSPECTIVELY_RECASTS) and an immaterial revision
            # (REVISION_OF). Which one this is is stated in the amendment's
            # explanatory note; a keyword scan cannot decide it, so the default
            # is the conservative, high-severity classification and the reason
            # is recorded for a human to confirm.
            "restatement_type": "error",
            "materiality": "undetermined",
            "restatement_reason": reason or "amendment filed; reason not extracted",
            "effective_date": "",
            "amended_form_type": form_type,
        }
    }


_DISC_OPS_RE = re.compile(
    r"discontinued operations?\s*[—-]?\s*(?P<name>[A-Z][A-Za-z0-9 &'\-]{2,40})",
)
_DISC_OPS_REASON_RE = re.compile(
    r"discontinued operations?[^.]{0,120}?(?:sale|divest\w+|disposal|"
    r"strategic (?:exit|review)|wind(?:ing|ed) down)",
    re.I,
)


def extract_discontinued_ops(
    text: str, ticker: str
) -> dict[str, dict[str, Any]]:
    """``DiscontinuedOpsSegment`` nodes for ASC 205-20 disclosures.

    Conservative by construction: a segment is only recorded when the filing
    names it in a "discontinued operations - <Name>" heading, and the disposal
    date is left empty rather than inferred from the surrounding sentence.
    """
    segments: dict[str, dict[str, Any]] = {}
    for match in _DISC_OPS_RE.finditer(text):
        name = clean_text(match.group("name"))
        if len(name) < 3 or not name[0].isupper():
            continue
        key = f"{ticker}:{name}".lower()
        if key in segments:
            continue
        window = text[max(0, match.start() - 300):match.end() + 300]
        reason = _DISC_OPS_REASON_RE.search(window)
        segments[key] = {
            "name": key,
            "ticker": ticker,
            "disposal_date": "",
            "disposal_method": clean_text(reason.group(0))[:120] if reason else "",
        }
        if len(segments) >= 20:
            break
    return segments


# ---------------------------------------------------------------------------
# Sector overlay
# ---------------------------------------------------------------------------

_OVERLAY_META: dict[str, str] = {
    "banking": "Basel III capital, risk-weighted assets, and VaR",
    "energy": "production volumes, reserves, and realisation",
    "healthcare": "pipeline programmes and regulatory milestones",
    "technology": "segment gross margin and remaining performance obligations",
}


def extract_sector_overlay(
    sector: str | None,
) -> dict[str, dict[str, Any]]:
    """The ``SectorOverlay`` node for *sector*, or nothing.

    One node per sector, not per filer: the overlay is a schema-layer
    definition ("these are the concepts this sector adds"), so emitting it once
    per company would make eight identical nodes for eight companies. The
    ``OVERLAY_APPLIES_TO`` arcs are what attach it to filers.

    Nothing is emitted for a sector with no overlay concepts of its own. The
    forty-concept list has seven bank-only anchors and none for the other three
    sectors -- a technology filer's sector-specific measures are segment gross
    margin and remaining performance obligations, neither of which is an SC
    anchor -- so manufacturing a technology overlay would produce a node whose
    ``overlay_concept_list`` is empty. The spec is explicit that the schema
    "does not create empty nodes" for these; the universal layer alone applies.
    """
    if not sector or sector not in _OVERLAY_META:
        return {}
    concepts = [c.concept_id for c in U.STANDARDIZED_CONCEPTS
                if c.sector_applicability == sector]
    if not concepts:
        log.debug("sector %r has no overlay concepts; emitting no overlay node", sector)
        return {}
    return {
        sector: {
            "sector": sector,
            "overlay_name": f"{sector}_overlay",
            "overlay_concept_list": ",".join(concepts),
        }
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def extract_ufgs(
    raw: str, path: Path, metadata: dict[str, Any], filing_id: str
) -> UFGSBundle:
    """Run the four ingestion stages over one filing.

    The visible text is derived once and shared: the structural extractors work
    on raw offsets, the narrative ones on text, and re-deriving the text twice
    would cost two full BeautifulSoup passes on a 2 MB filing.
    """
    bundle = UFGSBundle()
    form_type = (metadata.get("form_type") or "").upper()
    ticker = metadata.get("ticker") or ""

    # Sector resolution: SIC from the markup first, then the ticker's known
    # sector. A filing that is a technology company but whose SIC tag is
    # missing still gets the right overlay rather than the 33-concept universal
    # layer with no sector at all.
    sector = U.sector_for_sic(_sic_of(raw)) or U.sector_for_ticker(ticker)
    bundle.stats["sector"] = sector
    bundle.stats["sic_code"] = _sic_of(raw)
    bundle.stats["accession_number"] = accession_of(raw)

    bundle.sections = extract_sections(raw, form_type)
    facts = parse_ix_facts(raw)
    (
        bundle.raw_facts, reported_in, normalizes, id_by_offset,
        breakdown_segments, breakdowns,
    ) = build_raw_facts(facts, form_type, sector, list(bundle.sections.values()))
    bundle.segments = breakdown_segments
    bundle.concepts = extract_standardized_concepts(sector)
    bundle.sector_overlays = extract_sector_overlay(sector)

    periods = extract_fiscal_periods(metadata, facts)
    bundle.fiscal_periods = periods

    text = _visible_text(raw)
    bundle.footnotes, note_spans = extract_footnotes(raw, form_type, filing_id)
    note_ids = list(bundle.footnotes)
    disclosed = _disclosed_in(id_by_offset, note_spans, note_ids)

    risk_factors, relations, entities, relation_edges = extract_risk_factors(
        raw, form_type, filing_id, ticker,
        item_slice=item_slice(raw, bundle.sections, "1A"),
    )
    bundle.risk_factors = risk_factors
    bundle.causal_relations = relations
    bundle.entities = entities

    bundle.discontinued_segments = extract_discontinued_ops(text, ticker)
    restatement = detect_restatement(form_type, text, filing_id)
    if restatement:
        bundle.restatements = restatement

    # ``REPORTS_FOR`` is emitted from the period the filing actually reports
    # for, not from ``metadata``: a 10-K and its following 10-Q cover different
    # periods of the same year, and both must point somewhere.
    for period_id in bundle.fiscal_periods:
        bundle.edges.setdefault("REPORTS_FOR", []).append(
            {"from": filing_id, "to": period_id}
        )

    for table, row in relation_edges:
        bundle.edges.setdefault(table, []).append(row)
    bundle.edges.setdefault("REPORTED_IN", []).extend(reported_in)
    bundle.edges.setdefault("NORMALIZES_TO", []).extend(normalizes)
    bundle.edges.setdefault("DISCLOSED_IN", []).extend(disclosed)
    bundle.edges.setdefault("BROKEN_DOWN_BY", []).extend(breakdowns)
    for section_id in bundle.sections:
        bundle.edges.setdefault("CONTAINS_SECTION", []).append(
            {"from": filing_id, "to": section_id}
        )
    if ticker:
        bundle.edges.setdefault("FILED", []).append({"from": ticker, "to": filing_id})
    if sector and bundle.sector_overlays:
        bundle.edges.setdefault("OVERLAY_APPLIES_TO", []).append(
            {"from": sector, "to": ticker}
        )

    bundle.stats.update({
        "ix_facts": len(facts),
        "contexts": len({f.get("context_ref") for f in facts}),
        "normalized_facts": len({e["from"] for e in normalizes}),
        "dimensional_facts": sum(1 for f in facts if f.get("dimensions")),
        **bundle.counts(),
    })
    return bundle


_SIC_RE = re.compile(
    r"\bSIC\s*(?:Code)?\s*[:#]?\s*(\d{4})\b", re.I
)
#: EDGAR stamps the accession number into the filing header comment. Workiva
#: documents often omit it, so an empty string is the common answer and is
#: stored as such rather than synthesised from the ticker and date -- a
#: plausible-looking accession number that does not resolve at EDGAR is worse
#: than a blank one, because it looks like provenance that exists.
_ACCESSION_RE = re.compile(
    r"ACCESSION\s+NUMBER\s*[:=]?\s*(\d{10}-\d{2}-\d{6})", re.I
)


def accession_of(raw: str) -> str:
    """EDGAR accession number from the filing header, or ``""``."""
    match = _ACCESSION_RE.search(raw[:20_000])
    return match.group(1) if match else ""


def _sic_of(raw: str) -> str:
    """SIC code from the cover page, or ``""``.

    Cover pages only: an SIC code in the body would usually be a third party's,
    cited in a competition or supplier disclosure, and adopting it would
    mis-sector the filer on the strength of somebody else's business.
    """
    match = _SIC_RE.search(raw[:60_000])
    return match.group(1) if match else ""


def _visible_text(raw: str) -> str:
    """The filing's readable text, with the inline-XBRL machinery removed."""
    try:
        return strip_markup(raw)
    except Exception as exc:  # noqa: BLE001 - never fail a filing on this
        log.warning("could not render visible text: %s", exc)
        return re.sub(r"<[^>]+>", " ", raw)


#: Filer-specific fiscal year ends, from the spec's corpus notes. Recorded on
#: ``Company`` because "which filers have a 52/53-week year" is a company
#: property, not a per-filing one, and NVDA's late-January FYE is the reason
#: ``calendar_year_overlap`` exists at all.
_FYE_BY_TICKER: dict[str, tuple[int, str]] = {
    "AAPL": (9, "last Saturday of September"),
    "NVDA": (1, "last Sunday of January"),
    "MSFT": (6, "last Sunday of June"),
    "AMZN": (12, "December 31"),
    "META": (12, "December 31"),
    "TSLA": (12, "December 31"),
}


def fiscal_year_end_rule(ticker: str | None) -> tuple[int, str]:
    """``(month, rule)`` for a filer's year end, or ``(0, "")`` if unknown.

    Unknown is left unknown rather than defaulted to December: defaulting would
    make the graph assert a December year end for a filer that has none, and
    the field exists precisely to flag the filers that do not use the calendar.
    """
    return _FYE_BY_TICKER.get((ticker or "").strip().upper(), (0, ""))
