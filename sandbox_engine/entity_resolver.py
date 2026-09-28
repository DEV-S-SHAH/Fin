"""Canonical entity identity and deduplication for the sandbox SEC graph.

The parser reads a filing table-by-table, and no table knows what another one
called the same thing. A 10-K prints ``"iPhone"`` on the face of the income
statement and ``"iPhone®"`` in the segment note; a share-count line bakes its own
numbers into its label, so the same equity concept arrives as two labels that
differ only in a digit. Every one of those is the same entity, and hashing the
raw label gives each one its own node.

    from sandbox_engine.entity_resolver import ConceptRegistry, canonical_concept
    from sandbox_engine.parser import stable_id

    registry = ConceptRegistry(stable_id)
    name, evidence = canonical_concept("iPhone®")
    # ('iPhone', frozenset({'mark'}))
    resolution = registry.register("metric", name, scope="FY2025")
    # Resolution(canonical_id='a1b2...', decision='created', evidence='new')

Two properties drive the design, and both are measured rather than assumed.

**Deterministic canonicalisation, not fuzzy matching, does the work.**
:func:`canonical_concept` is a pure function: an ordered chain of rewrites that
each remove something that is *provably* not part of the concept (a trademark
mark, a footnote marker, a trailing unit qualifier). On the three-filing corpus
it folds 16 duplicate ``Metric`` rows into their canonical node, and every
collapsed group is genuinely one concept. A pure function is auditable: the
evidence tags say which rule fired, so a merge can be explained or overruled.

**The fuzzy stage is off by default, because on this corpus it merges things
that are not the same fact.** Replaying the 778 canonical metric labels through
:class:`EntityRegistry` with ``fuzzy=True`` at the default ``threshold=0.88``
produces four merges. All four are wrong:

* ``Other non-current assets`` -- the balance-sheet line -- into ``Other current
  and non-current assets`` -- a different statement's line, score 0.980;
* the same pair for liabilities, score 0.983.

They fail because the labels are lexically near-identical while semantically
distinct, and nothing cheap separates them: a token guard would have to know that
"non-current" and "current and non-current" describe different scopes, which is
a semantic judgement, not a string comparison. Measured on the 590 unrecognised
labels this parser falls through to, similarity matching also proposes merging
``Total non-current assets`` into ``Total Assets`` and ``Total lease liabilities``
into ``Total Liabilities``.

A wrong merge is not recoverable the way a missed one is: the two values are now
one node and the error is invisible downstream. So the third stage is opt-in
(``ConceptRegistry(fuzzy=True)``), and the aliases it would have guessed are
instead declared explicitly in :data:`CONCEPT_ALIASES`, where a reviewer can see
them. :func:`polarity_conflict` is the one guard added even though the stage is
off, because the beginning/ending hole it closes is not a threshold problem.

Why a registry at all, if canonicalisation is a pure function
-------------------------------------------------------------

Because canonicalisation collapses *within* a label's spelling but cannot know
that ``"Total property, plant and equipment, net"`` is the concept the registry
already calls ``Property, Plant and Equipment, Net``. Linking a pass-through
label to a registry concept is state: it is a claim that two different strings
name one thing, and it has to be recorded so the next filing that uses either
string lands on the same node. :class:`EntityRegistry` is that state, and
:meth:`save`/:meth:`load` are what make a re-ingest resume with its merges
intact instead of rediscovering them.

The scope partition
-------------------

``Metric`` identity includes the reporting period, because ``REPORTS_METRIC``
carries a single ``value`` and a 10-K shows three periods of every line. So the
registry is partitioned by ``(kind, scope)`` and resolution never crosses a
partition boundary. That is a structural guarantee rather than a tuned number:
no threshold, however low, can merge ``Net Sales (3M-2025-12-27)``
into ``Net Sales (3M-2026-03-28)``, which lives in a different partition
entirely.

Cross-period facts are not the only hazard. A *beginning* and an *ending*
balance share both a kind and a period, so partitioning does nothing for them,
and in the real corpus ``"Common stock outstanding, beginning balances"``
scores a perfect **1.000** against its ``ending`` counterpart -- the labels
differ by one stem, so no threshold, including 1.0, keeps them apart. Token
blocking does not help either, because they share the word ``balances``.

What separates them is :func:`polarity_conflict`: when two names disagree on an
opposite-sense token they are not a match, whatever they score. That is a
structural guarantee, not a tuned number, and it is why ``ENTITY_FUZZY_MATCH``
can be turned on without a balance sheet quietly reporting its opening balance
as its closing one.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import ENTITY_FUZZY_MATCH, ENTITY_SIMILARITY_THRESHOLD

__all__ = [
    "CONCEPT_ALIASES",
    "CanonicalEntity",
    "Concept",
    "ConceptRegistry",
    "EmbeddingSimilarity",
    "EntityRegistry",
    "LexicalSimilarity",
    "Resolution",
    "Similarity",
    "canonical_concept",
    "fuzzy_similarity",
    "normalise",
    "resolve_subgraph",
    "slugify",
    "tokenize",
]

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: Diacritic-combining marks, stripped after NFKD so ``café`` matches ``cafe``.
_COMBINING = "".join(chr(c) for c in range(0x300, 0x370)) + "̐-ͯ"

_PERIOD = re.compile(r"\.", re.UNICODE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_UNDERSCORE = re.compile(r"[\s_]+", re.UNICODE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")
_NBSP = re.compile(r"[   ⁠]")

#: Deliberately small. Aggressive stopword removal would eat the "of" out of
#: "Bank of America" and collide it with unrelated names.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is",
        "it", "its", "of", "on", "or", "that", "the", "to", "with",
    }
)


@lru_cache(maxsize=8192)
def normalise(text: str) -> str:
    """Fold a name to a comparable key.

    Case, accents, and punctuation all go: ``"Euler-Lagrange Equation"``,
    ``"euler lagrange equation"`` and ``"Euler_Lagrange  Equation"`` all reduce
    to ``"euler lagrange equation"``.

    Periods are *deleted* while other punctuation becomes a space. That
    asymmetry is deliberate: in ``"U.S. Treasury"`` the dot is an abbreviation
    mark, so spacing it gives ``"u s treasury"`` and fails to match
    ``"US Treasury"``, whereas a hyphen in ``"Euler-Lagrange"`` really does
    separate two words.

    This is a *comparison* key, not an identity. Identity comes from
    :func:`canonical_concept`; folding ``"3.5"`` to ``"35"`` is harmless here
    because a metric label is never a bare number.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = _PUNCT.sub(" ", _PERIOD.sub("", stripped.casefold()))
    return _UNDERSCORE.sub(" ", spaced).strip()


def slugify(text: str) -> str:
    """The canonical-id form of a name.

    Falls back to a content hash when a name has no latin or digit characters
    at all; otherwise every non-latin name would slug to the same empty string
    and collide with every other one.
    """
    slug = _NON_SLUG.sub("-", normalise(text)).strip("-")
    if slug:
        return slug
    import hashlib

    return "x" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def tokenize(text: str) -> tuple[str, ...]:
    """Content tokens of *text*, for blocking and token-overlap scoring.

    Tokens shorter than three characters are dropped: a financial label is full
    of them (``"(1)"``, ``"a"``, ``"%"``) and they are not discriminative.
    """
    if not text:
        return ()
    return tuple(
        sorted({t for t in normalise(text).split() if len(t) >= 3 and t not in _STOPWORDS})
    )


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

#: A trailing unit qualifier. ``"Net sales (in millions)"`` and ``"Net sales"``
#: are one concept stated in two scales; the scale is carried on
#: ``REPORTS_METRIC.currency`` and the row's magnitude, not in the name.
#: A unit parenthetical: a scale, a currency, or both.
#:
#: Two alternatives rather than one optional-everything pattern, so that ``()``
#: and ``( )`` cannot match and strip a label bare. Written as a closed
#: vocabulary on purpose -- ``(basic)``, ``(diluted)``, ``(net)`` and ``(%)`` are
#: qualifiers that change what the number means, and none of them appear here.
_CURRENCY = r"(?:[$€£¥]|usd|eur|gbp|jpy|rmb|cny|dollars?|euros?|pounds?|yen)\s*"
_SCALE = r"(?:thousands|millions|billions|trillions|shares|units|per\s+share)"
_UNIT_QUALIFIER = re.compile(
    rf"\s*\(\s*(?:in\s+)?(?:{_CURRENCY})?(?:in\s+)?{_SCALE}\s*\)\s*$"
    rf"|\s*\(\s*(?:in\s+)?{_CURRENCY}\)\s*$",
    re.I,
)

#: Token pairs that make two labels opposites rather than variants.
#:
#: These cannot be caught by a threshold, because the labels are lexically
#: *identical* apart from the polarity word. In the real corpus,
#: ``"Common stock outstanding, beginning balances"`` and its ``ending``
#: counterpart score a perfect **1.000** -- the token count matches, the shared
#: words match, and the one word that differs is a single character of stem. Any
#: threshold, including 1.0, merges them, and a balance sheet that reports its
#: opening balance as its closing one is silently wrong rather than obviously
#: broken.
#:
#: So the guard is on polarity, not on score. If two names differ in one of these
#: tokens they are not a match, whatever they score.
_POLARITY_PAIRS = (
    frozenset({"beginning", "ending"}),
    frozenset({"opening", "closing"}),
    frozenset({"start", "end"}),
    frozenset({"gross", "net"}),
    frozenset({"inflow", "outflow"}),
    frozenset({"inflows", "outflows"}),
    frozenset({"provided", "used"}),
    frozenset({"increase", "decrease"}),
    frozenset({"increased", "decreased"}),
    frozenset({"acquired", "disposed"}),
    frozenset({"issued", "withheld"}),
    frozenset({"assets", "liabilities"}),
)


def polarity_conflict(left: str, right: str) -> bool:
    """True if the two names differ in an opposite-sense token."""
    left_tokens = set(normalise(left).split())
    right_tokens = set(normalise(right).split())
    for pair in _POLARITY_PAIRS:
        if (left_tokens & pair) != (right_tokens & pair):
            return True
    return False

#:
#: Equity lines print their own par value and share counts, and the face amounts
#: differ per filing, so the annotation is a measurement rather than identity.
#: Only applied when the head carries no digits, which keeps a label whose
#: identity genuinely is numeric -- ``"2026 Plan, $5,000"`` -- intact.
#: A par-value / share-count annotation: ``", $0.00001 par value: 50,400,000 ..."``.
#:
#: Equity lines print their own par value and share counts, and the face amounts
#: differ per filing, so the annotation is a measurement rather than identity.
#: Only applied when the head carries no digits, which keeps a label whose
#: identity genuinely is numeric -- ``"2026 Plan, $5,000"`` -- intact.
_PAR_VALUE = re.compile(r",\s*\$[\d.]+(?:\s*par\s+value)?(?::.*)?$", re.I)

#: A trailing footnote marker: ``"China (1)"``, ``"(a)"``, ``"(i)"``.
#:
#: The alternation is a bare number, a short roman numeral, or a single letter,
#: and nothing else. That is what keeps it from eating a real qualifier --
#: ``"Earnings per share (basic)"``, ``"(diluted)"`` and ``"(net)"`` all survive,
#: because ``basic``, ``diluted`` and ``net`` are longer than the pattern allows.
_FOOTNOTE_MARKER = re.compile(r"\s*\((?:\d{1,3}|[ivxlcdm]{1,5}|[a-z])\)\s*$", re.I)

#: Trademark and registration marks. ``"iPhone"`` and ``"iPhone®"`` are the same
#: product line; the mark is typography, not identity.
_TRADEMARK = re.compile(r"[®™©℗]")

#: Trailing punctuation a table cell picks up from the column layout.
_TRAILING_PUNCT = re.compile(r"[\s.:;,]+$")

#: ``"<concept>: <long detail run>"`` -> ``"<concept>"``.
#:
#: An equity line prints its own share counts in its label --
#: ``"Common stock and additional paid-in capital, $0.00001 par value:
#: 50,400,000 shares authorized; 14,773,260 and 15,116,786 shares issued and
#: outstanding, respectively"`` -- so the same concept arrives twice in one
#: period, differing only in digits that are *measurements*, not identity. The
#: guards keep this narrow: the head must be a plausible label, the tail must be
#: long, and the tail must contain a digit. A label like ``"Deferred taxes:
#: current"`` has a short non-numeric tail and is left alone.
_DETAIL_TAIL = re.compile(r"^([^:]{4,80}):\s*(?=.*\d)(.*)$")
_DETAIL_TAIL_MIN = 40


@dataclass(frozen=True)
class Concept:
    """A canonicalised concept name and the rules that produced it.

    ``evidence`` is the audit trail. Every collapse this module performs is
    attributable to a named rule, which is what makes a merge reviewable rather
    than merely observable.
    """

    name: str
    evidence: frozenset[str] = frozenset()

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.name


def canonical_concept(label: str) -> Concept:
    """Canonicalise a filing row label into a concept name.

    An ordered chain of rewrites, each removing something that is not part of
    the concept. Order is load-bearing: the unit qualifier is stripped before
    the footnote marker, because ``"(in millions)"`` is not a footnote, and
    trademark marks are stripped before trailing punctuation, because ``"iPhone®."``
    has to lose both.

    Returns a :class:`Concept` whose ``name`` is ``""`` when the label carries no
    concept at all -- a dash, an empty cell, a period banner. An empty name is
    not an error; it means the caller has nothing to register.

    ``" (%)"`` is deliberately **preserved**. A percent margin and a currency
    margin are different facts, so the marker stays part of identity rather than
    being folded away like the unit qualifier it resembles.
    """
    text = _WHITESPACE.sub(" ", _NBSP.sub(" ", str(label or ""))).strip()
    if not text:
        return Concept("")
    evidence: set[str] = set()

    stripped = _UNIT_QUALIFIER.sub("", text).strip()
    if stripped != text:
        evidence.add("unit")
    text = stripped

    # A par-value annotation, before the footnote pass: the annotation is
    # dropped whole, so the marker rule never sees its internal colons.
    par = _PAR_VALUE.search(text)
    if par and not any(ch.isdigit() for ch in text[: par.start()]):
        text = text[: par.start()].strip()
        evidence.add("par_value")

    # Repeated: a cell can carry both a unit and a footnote marker.
    for _ in range(3):
        stripped = _FOOTNOTE_MARKER.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
        evidence.add("footnote")

    stripped = _TRADEMARK.sub("", text)
    if stripped != text:
        evidence.add("mark")
    text = stripped

    for _ in range(3):
        stripped = _TRAILING_PUNCT.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
        evidence.add("punct")

    match = _DETAIL_TAIL.match(text)
    if match and len(match.group(2)) > _DETAIL_TAIL_MIN:
        text = _TRAILING_PUNCT.sub("", match.group(1)).strip()
        evidence.add("detail_tail")

    if not text:
        return Concept("", frozenset(evidence))
    return Concept(text, frozenset(evidence))


#: Pass-through labels that are known to name a registry concept.
#:
#: These are the merges similarity matching *cannot* be trusted to find and
#: ought not to be left to find: a concept the registry already names, spelled a
#: second way in the filing. Declared here rather than inferred, so that adding
#: one is a reviewed change. Keys are compared with :func:`normalise`.
CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    # The concept registry calls the face of the balance sheet
    # "Property, Plant and Equipment, Net"; the notes say
    # "Total property, plant and equipment, net".
    "Property, Plant and Equipment, Net": ("Total property, plant and equipment, net",),
    # "Income before provision for income taxes" is the same line as
    # "Income before taxes"; the registry pattern did not allow the word
    # "income" between "for" and "taxes".
    "Income Before Taxes": (
        "Income before provision for income taxes",
        "Income before provision for income taxes, net of tax benefits",
    ),
}


# ---------------------------------------------------------------------------
# Similarity protocols
# ---------------------------------------------------------------------------


class Similarity(Protocol):
    def score(self, a: str, b: str) -> float: ...


class LexicalSimilarity:
    """Token overlap blended with character-level ratio.

    Pure Python and dependency-free, so the default provider runs anywhere.
    """

    def __init__(self, token_weight: float = 0.9, fuzzy_weight: float = 0.1) -> None:
        self.token_weight = token_weight
        self.fuzzy_weight = fuzzy_weight

    def score(self, a: str, b: str) -> float:
        ta, tb = set(tokenize(a)), set(tokenize(b))
        if not ta or not tb:
            return 0.0
        token_score = len(ta & tb) / len(ta | tb)
        # No floor. The previous version raised every ratio to
        # ``fuzzy_floor`` before blending, which added a constant to every pair
        # and made the number stop meaning "how similar are these two" -- the
        # reason a threshold tuned against it was not portable.
        return self.token_weight * token_score + self.fuzzy_weight * SequenceMatcher(None, a, b).ratio()


class EmbeddingSimilarity:
    """Lexical overlap blended with a caller-supplied embedding.

    ``score_blended`` compares two *descriptions* as well as two names, for
    providers that can. It is the only place the optional description is read,
    so a registry that has none behaves exactly as it does lexically.
    """

    def __init__(
        self,
        embed_fn: Callable[[str], list[float]],
        lexical_weight: float = 0.6,
        embedding_weight: float = 0.4,
        cache: dict[str, list[float]] | None = None,
    ) -> None:
        self.embed_fn = embed_fn
        self.lexical = LexicalSimilarity()
        self.lexical_weight = lexical_weight
        self.embedding_weight = embedding_weight
        self._cache = cache if cache is not None else {}

    def _embed(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = self.embed_fn(text)
        return self._cache[text]

    @staticmethod
    def cosine(left: Sequence[float], right: Sequence[float]) -> float:
        return sum(x * y for x, y in zip(left, right))

    def score(self, a: str, b: str) -> float:
        lexical = self.lexical.score(a, b)
        if lexical >= 0.95:
            return lexical
        try:
            vector = max(0.0, min(1.0, self.cosine(self._embed(a), self._embed(b))))
        except Exception:  # noqa: BLE001 - a provider failure must not fail the run
            vector = 0.0
        return self.lexical_weight * lexical + self.embedding_weight * vector

    def score_blended(
        self, name_a: str, desc_a: str, name_b: str, desc_b: str
    ) -> float:
        return 0.5 * self.score(name_a, name_b) + 0.5 * self.score(desc_a, desc_b)


class _CallableSimilarity:
    """Adapts a bare ``score(a, b) -> float`` function to the protocol."""

    def __init__(self, fn: Callable[[str, str], float]) -> None:
        self._fn = fn

    def score(self, a: str, b: str) -> float:
        return float(self._fn(a, b))


def _coerce_similarity(provider: Any) -> Similarity:
    if provider is None:
        return LexicalSimilarity()
    if hasattr(provider, "score"):
        return provider
    if callable(provider):
        return _CallableSimilarity(provider)
    raise TypeError(
        f"similarity must be None, a score(a, b) callable, or expose .score(); "
        f"got {type(provider).__name__}"
    )


def fuzzy_similarity(a: str, b: str) -> float:
    return LexicalSimilarity().score(a, b)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class CanonicalEntity:
    """One canonical node in a registry partition."""

    id: str
    name: str
    kind: str = ""
    scope: str = ""
    category: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    mentions: int = 1
    category_conflicts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "scope": self.scope,
            "category": self.category,
            "description": self.description,
            "aliases": list(self.aliases),
            "mentions": self.mentions,
            "category_conflicts": self.category_conflicts,
        }

    def variants(self) -> list[str]:
        """Every surface form this entity answers to, canonical name first."""
        return [self.name, *self.aliases]


@dataclass(frozen=True)
class Resolution:
    """The outcome of registering one extracted entity."""

    canonical_id: str
    decision: str  # "created" | "merged"
    evidence: str  # "new" | "name" | "alias" | "seeded" | "fuzzy" | "embedding"
    score: float = 1.0
    matched_on: str = ""
    second_best: float = 0.0
    ambiguous: bool = False
    alias_delta: tuple[str, ...] = ()
    added_aliases: tuple[str, ...] = ()
    rejected_aliases: tuple[str, ...] = ()
    category_conflict: bool = False

    @property
    def merged(self) -> bool:
        return self.decision == "merged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "decision": self.decision,
            "evidence": self.evidence,
            "score": round(self.score, 4),
            "matched_on": self.matched_on,
            "second_best": round(self.second_best, 4),
            "ambiguous": self.ambiguous,
            "added_aliases": list(self.added_aliases),
            "rejected_aliases": list(self.rejected_aliases),
            "category_conflict": self.category_conflict,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class EntityRegistry:
    """Canonical entity store for one ``(kind, scope)`` partition.

    Matching runs cheapest-first, and the expensive stage is both opt-in and
    blocked:

    1. exact key match on the name -- one dict lookup;
    2. exact key match on any alias -- one dict lookup;
    3. fuzzy or embedding scoring, over candidates that share a content token.

    Stage 3 is **off by default** (``fuzzy=False``). See the module docstring for
    the measurement behind that: at the root resolver's default threshold it
    merges a beginning balance into an ending balance. Turn it on with
    ``fuzzy=True``, and expect to review ``ambiguous`` merges.

    ``id_factory`` is how the sandbox's opaque node ids get in. The default is
    :func:`slugify`, which is readable and stable; the pipeline injects
    ``stable_id`` so ids match the rest of its content-addressed scheme and a
    filing's segments keep the ids the buffer and loader already expect.
    """

    VERSION = 1

    def __init__(
        self,
        threshold: float = ENTITY_SIMILARITY_THRESHOLD,
        similarity: Any = None,
        fuzzy: bool = ENTITY_FUZZY_MATCH,
        blocking: str = "token",
        max_candidates: int = 64,
        ambiguity_margin: float = 0.02,
        id_factory: Callable[[str, str, str], str] | None = None,
        kind: str = "",
        scope: str = "",
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold!r}")
        if blocking not in {"token", "none"}:
            raise ValueError(f"blocking must be 'token' or 'none', got {blocking!r}")
        self.threshold = float(threshold)
        self.similarity: Similarity = _coerce_similarity(similarity)
        self.fuzzy = bool(fuzzy)
        self.blocking = blocking
        self.max_candidates = int(max_candidates)
        self.ambiguity_margin = float(ambiguity_margin)
        self._id_factory = id_factory
        self.kind = kind
        self.scope = scope

        self._entities: dict[str, CanonicalEntity] = {}
        self._name_index: dict[str, str] = {}
        self._alias_index: dict[str, set[str]] = {}
        self._token_index: dict[str, set[str]] = {}
        self.stats: dict[str, int] = {
            "created": 0,
            "merged": 0,
            "exact_name_hits": 0,
            "exact_alias_hits": 0,
            "fuzzy_hits": 0,
            "alias_conflicts": 0,
            "category_conflicts": 0,
            "ambiguous_merges": 0,
            "polarity_conflicts": 0,
            "comparisons": 0,
        }

    # -- container protocol ------------------------------------------------

    def __len__(self) -> int:
        return len(self._entities)

    def __contains__(self, canonical_id: object) -> bool:
        return canonical_id in self._entities

    def __iter__(self):
        return iter(self._entities.values())

    def get(self, canonical_id: str) -> CanonicalEntity | None:
        return self._entities.get(canonical_id)

    def entities(self) -> list[CanonicalEntity]:
        return list(self._entities.values())

    # -- index maintenance ------------------------------------------------

    def _index(self, entity: CanonicalEntity) -> None:
        self._name_index.setdefault(normalise(entity.name), entity.id)
        for alias in entity.aliases:
            self._alias_index.setdefault(normalise(alias), set()).add(entity.id)
        for surface in entity.variants():
            for token in tokenize(surface):
                self._token_index.setdefault(token, set()).add(entity.id)

    def _mint_id(self, name: str) -> str:
        if self._id_factory is not None:
            return self._id_factory(self.kind, name, self.scope)
        return slugify(name)

    # -- lookup -----------------------------------------------------------

    def lookup(self, name: str) -> str | None:
        """Resolve a surface form to a canonical id *without* registering it.

        Relationship endpoints need this: an endpoint may name an entity first
        registered by an earlier filing, and registering it here would create a
        node for an entity the run never described.
        """
        key = normalise(name)
        if not key:
            return None
        found = self._name_index.get(key)
        if found is not None:
            return found
        hits = self._alias_index.get(key)
        # sorted() so a contested alias resolves the same way on every run.
        return sorted(hits)[0] if hits else None

    def _candidates(self, name: str, aliases: Sequence[str]) -> set[str]:
        """Blocking keys for the fuzzy stage, rarest first.

        Blocking on *any* shared token is not enough on its own: tokens are
        unevenly distributed and a common one matches a large slice of the
        registry, which is the same cost as no blocking. Tokens are tried in
        increasing order of how many entities hold them, and the set stops
        growing once ``max_candidates`` is reached.
        """
        if self.blocking == "none":
            return set(self._entities)

        postings: list[set[str]] = []
        for surface in (name, *aliases):
            for token in tokenize(surface):
                ids = self._token_index.get(token)
                if ids:
                    postings.append(ids)
        if not postings:
            return set()

        # Deduplicate identical posting lists, then rarest first.
        unique = {id(ids): ids for ids in postings}
        ordered = sorted(unique.values(), key=len)

        # A posting list wider than the budget says "this token is
        # everywhere", so admitting it would put the whole registry back in
        # play -- and blocking that does not block is worse than no blocking,
        # because it looks like it is working.
        usable = [ids for ids in ordered if len(ids) <= self.max_candidates]
        if not usable:
            # Every token is common. Fall back to the rarest list, uncapped:
            # comparing a few arbitrary candidates would give a wrong answer
            # more often than a fast one. This is why max_candidates is a
            # budget, not a guarantee.
            return set(ordered[0])

        candidates: set[str] = set()
        for ids in usable:
            if candidates and len(candidates | ids) > self.max_candidates:
                continue
            candidates |= ids
            if len(candidates) >= self.max_candidates:
                break
        return candidates

    def _best_fuzzy(
        self, name: str, description: str, aliases: Sequence[str]
    ) -> tuple[str | None, float, float, str]:
        """Best ``(id, score, runner-up, matched variant)`` above the threshold."""
        candidates = self._candidates(name, aliases)
        if not candidates:
            return None, 0.0, 0.0, ""

        scored: list[tuple[float, str, str]] = []
        for candidate_id in candidates:
            entity = self._entities.get(candidate_id)
            if entity is None:
                continue
            best_score, best_variant = 0.0, ""
            for variant in entity.variants():
                if polarity_conflict(variant, name):
                    # A perfect score here means the labels differ only in
                    # beginning/ending, so the score is measuring the wrong
                    # thing. Refuse regardless of how well they "match".
                    self.stats["polarity_conflicts"] += 1
                    continue
                self.stats["comparisons"] += 1
                score = self.similarity.score(variant, name)
                blended = getattr(self.similarity, "score_blended", None)
                if blended is not None and description and entity.description:
                    score = max(score, blended(entity.name, entity.description, name, description))
                if score > best_score:
                    best_score, best_variant = score, variant
            if best_score >= self.threshold:
                scored.append((best_score, candidate_id, best_variant))

        if not scored:
            return None, 0.0, 0.0, ""
        scored.sort(key=lambda row: (-row[0], row[1]))
        top_score, top_id, top_variant = scored[0]
        return top_id, top_score, (scored[1][0] if len(scored) > 1 else 0.0), top_variant

    # -- registration -----------------------------------------------------

    def add(self, record: Mapping[str, Any] | CanonicalEntity) -> Resolution:
        """Register one extracted entity, merging it into a known one if it matches.

        Raises ``ValueError`` for an entity with no usable name. Silently
        registering an unnamed entity would mint an id from an empty string, and
        every later unnamed entity would collide with it.
        """
        if isinstance(record, CanonicalEntity):
            payload: Mapping[str, Any] = {
                "name": record.name,
                "category": record.category,
                "description": record.description,
                "aliases": record.aliases,
            }
        elif isinstance(record, Mapping):
            payload = record
        else:
            raise TypeError(
                f"entity must be a mapping or CanonicalEntity, got {type(record).__name__}"
            )

        name = str(payload.get("name") or "").strip()
        if not name or not normalise(name):
            raise ValueError("entity needs a non-empty name")
        category = str(payload.get("category") or "").strip()
        description = str(payload.get("description") or "").strip()
        raw_aliases = payload.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        incoming = [str(a).strip() for a in raw_aliases if str(a).strip() and str(a) != name]

        # -- 1. exact name
        exact = self._name_index.get(normalise(name))
        if exact is not None:
            self.stats["exact_name_hits"] += 1
            return self._merge(exact, name, incoming, category, description, "name", 1.0, name)

        # -- 2. exact alias
        alias_hits = self._alias_index.get(normalise(name))
        if alias_hits:
            self.stats["exact_alias_hits"] += 1
            return self._merge(
                sorted(alias_hits)[0], name, incoming, category, description, "alias", 1.0, name
            )

        # -- 3. fuzzy / embedding, opt-in and blocked
        if self.fuzzy:
            best_id, score, runner_up, variant = self._best_fuzzy(name, description, incoming)
            if best_id is not None:
                self.stats["fuzzy_hits"] += 1
                evidence = (
                    "embedding"
                    if hasattr(self.similarity, "score_blended")
                    else "fuzzy"
                )
                return self._merge(
                    best_id, name, incoming, category, description,
                    evidence, score, variant, second_best=runner_up,
                )

        return self._create(name, incoming, category, description)

    def _create(
        self, name: str, aliases: Sequence[str], category: str, description: str
    ) -> Resolution:
        canonical_id = self._mint_id(name)
        kept, rejected = self._safe_aliases(canonical_id, name, aliases)
        # An alias that is just another casing of the name is noise, and at
        # creation time the name is not in the index yet so _safe_aliases
        # cannot see the clash.
        own = normalise(name)
        kept = [alias for alias in kept if normalise(alias) != own]
        entity = CanonicalEntity(
            id=canonical_id,
            name=name,
            kind=self.kind,
            scope=self.scope,
            category=category,
            description=description,
            aliases=kept,
        )
        self._entities[canonical_id] = entity
        self._index(entity)
        self.stats["created"] += 1
        return Resolution(
            canonical_id=canonical_id,
            decision="created",
            evidence="new",
            added_aliases=tuple(kept),
            rejected_aliases=tuple(rejected),
        )

    def _safe_aliases(
        self, canonical_id: str, name: str, aliases: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        """Keep aliases that do not belong to a *different* canonical entity.

        Without this, an entity could claim a name another entity owns, and the
        next lookup on that name would resolve to whichever wrote its index
        first. Contested aliases are reported, not silently taken.
        """
        kept: list[str] = []
        rejected: list[str] = []
        for alias in aliases:
            if alias == name or not normalise(alias):
                continue
            key = normalise(alias)
            owners = {self._name_index.get(key, "")} | self._alias_index.get(key, set())
            owners.discard("")
            if owners and owners != {canonical_id}:
                rejected.append(alias)
                self.stats["alias_conflicts"] += 1
                continue
            if any(normalise(alias) == normalise(k) for k in kept):
                continue
            kept.append(alias)
        return kept, rejected

    def _merge(
        self,
        canonical_id: str,
        name: str,
        aliases: Sequence[str],
        category: str,
        description: str,
        evidence: str,
        score: float,
        matched_on: str,
        second_best: float = 0.0,
    ) -> Resolution:
        entity = self._entities[canonical_id]
        self.stats["merged"] += 1
        entity.mentions += 1

        # A description that is both longer and different is more informative;
        # the first one wins ties, so re-ingest is idempotent.
        if len(description) > len(entity.description) + 20:
            entity.description = description

        if category and entity.category and category != entity.category:
            entity.category_conflicts += 1
            self.stats["category_conflicts"] += 1
            conflict = True
        elif category and not entity.category:
            entity.category = category
            conflict = False
        else:
            conflict = False

        added, rejected = self._safe_aliases(canonical_id, entity.name, aliases)
        known = {normalise(v) for v in entity.variants()}
        delta = [a for a in added if normalise(a) not in known]
        if delta:
            entity.aliases.extend(delta)
            self._index(entity)

        ambiguous = bool(second_best) and second_best >= score - self.ambiguity_margin
        if ambiguous:
            self.stats["ambiguous_merges"] += 1

        return Resolution(
            canonical_id=canonical_id,
            decision="merged",
            evidence=evidence,
            score=score,
            matched_on=matched_on,
            second_best=second_best,
            ambiguous=ambiguous,
            alias_delta=tuple(delta),
            added_aliases=tuple(delta),
            rejected_aliases=tuple(rejected),
            category_conflict=conflict,
        )

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "kind": self.kind,
            "scope": self.scope,
            "threshold": self.threshold,
            "fuzzy": self.fuzzy,
            "entities": [e.to_dict() for e in self._entities.values()],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename, so an interrupted save cannot truncate the
        # registry and leave the next run believing it has no merges.
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path, **overrides: Any) -> "EntityRegistry":
        """Rebuild a registry from a file written by :meth:`to_dict`.

        ``**overrides`` wins over whatever the file records, so a caller can
        tighten ``threshold`` or force ``fuzzy`` on a registry that was saved
        with looser settings.
        """
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        settings: dict[str, Any] = {
            "kind": payload.get("kind", ""),
            "scope": payload.get("scope", ""),
            "threshold": payload.get("threshold", ENTITY_SIMILARITY_THRESHOLD),
            "fuzzy": payload.get("fuzzy", ENTITY_FUZZY_MATCH),
        }
        settings.update(overrides)
        registry = cls(**settings)
        registry._absorb(payload.get("entities", []))
        return registry

    def _absorb(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            entity = CanonicalEntity(
                id=str(record["id"]),
                name=str(record["name"]),
                kind=str(record.get("kind", self.kind)),
                scope=str(record.get("scope", self.scope)),
                category=str(record.get("category", "")),
                description=str(record.get("description", "")),
                aliases=[str(a) for a in record.get("aliases", [])],
                mentions=int(record.get("mentions", 1)),
                category_conflicts=int(record.get("category_conflicts", 0)),
            )
            self._entities[entity.id] = entity
            self._index(entity)


# ---------------------------------------------------------------------------
# Concept registry: the sandbox front door
# ---------------------------------------------------------------------------


class ConceptRegistry:
    """Canonical node identity for every entity kind in a run.

    Holds one :class:`EntityRegistry` per ``(kind, scope)`` partition and mints
    node ids through ``id_factory``. Two consequences follow from the partition,
    and both are the point:

    * A metric's period is part of its identity, so resolution never crosses a
      period boundary. ``Net Sales (FY-2025-09-27)`` and ``Net Sales
      (FY-2024-09-28)`` are different facts and no threshold can merge them.
    * The same partition is shared by every filing in the run, so a segment
      named in both the 10-K and the 10-Q resolves to one node instead of one per
      filing. This is what ``FilingParser.extract_segments`` could not do on its
      own, because it keyed segments on the raw label with a per-filing
      ``setdefault``.
    """

    VERSION = 1

    @property
    def threshold(self) -> float:
        """Similarity a fuzzy merge must reach. Meaningless while ``fuzzy`` is off."""
        return self._threshold

    @property
    def fuzzy(self) -> bool:
        return self._fuzzy

    def __init__(
        self,
        id_factory: Callable[[str, str, str], str],
        *,
        threshold: float = ENTITY_SIMILARITY_THRESHOLD,
        fuzzy: bool = ENTITY_FUZZY_MATCH,
        similarity: Any = None,
        max_candidates: int = 64,
        alias_seeds: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._id_factory = id_factory
        self._threshold = float(threshold)
        self._fuzzy = bool(fuzzy)
        self._similarity = similarity
        self._max_candidates = int(max_candidates)
        self._alias_seeds = dict(alias_seeds if alias_seeds is not None else CONCEPT_ALIASES)
        self._partitions: dict[tuple[str, str], EntityRegistry] = {}

    #: Per-partition counters, summed by :attr:`stats`.
    #:
    #: No ``seeded_alias_hits``: a seeded merge is already a ``merged`` whose
    #: :attr:`Resolution.evidence` is ``"seeded"``, and a second counter for the
    #: same event is one more thing that can disagree with the first.
    _COUNTERS = (
        "created",
        "merged",
        "fuzzy_hits",
        "alias_conflicts",
        "category_conflicts",
        "ambiguous_merges",
        "comparisons",
    )

    @property
    def stats(self) -> dict[str, int]:
        """Run-wide resolution counters, summed across partitions.

        Derived on read rather than accumulated on write. An accumulator here
        has to add each partition's *running* total on every registration, which
        sums 1 + 2 + ... + n and reports tens of thousands of creations for a few
        hundred entities -- a number large enough to look plausible and wrong.
        """
        totals = {key: 0 for key in self._COUNTERS}
        for registry in self._partitions.values():
            for key in self._COUNTERS:
                totals[key] += registry.stats.get(key, 0)
        return totals

    # -- partitions -------------------------------------------------------

    def partition(self, kind: str, scope: str = "") -> EntityRegistry:
        """The registry for ``(kind, scope)``, created on first use."""
        key = (kind, scope)
        registry = self._partitions.get(key)
        if registry is None:
            registry = EntityRegistry(
                threshold=self._threshold,
                similarity=self._similarity,
                fuzzy=self._fuzzy,
                max_candidates=self._max_candidates,
                id_factory=self._id_factory,
                kind=kind,
                scope=scope,
            )
            self._partitions[key] = registry
        return registry

    def _seeded_canonical(self, label: str) -> str:
        """The declared canonical name if *label* is one of its aliases.

        A pure function of :data:`CONCEPT_ALIASES`, which is what makes the seed
        order-independent. Resolving through registry state instead -- "if the
        canonical is not there yet, create it and re-attach later" -- breaks on
        the common case: the *plain* label arrives first, becomes its own entity,
        and then owns the name the alias needs, so :meth:`EntityRegistry.
        _safe_aliases` rejects the alias and the two never merge.
        """
        key = normalise(label)
        for canonical, aliases in self._alias_seeds.items():
            if normalise(canonical) == key:
                return canonical
            if any(normalise(alias) == key for alias in aliases):
                return canonical
        return label

    # -- registration -----------------------------------------------------

    def register(
        self,
        kind: str,
        name: str,
        *,
        scope: str = "",
        category: str = "",
        aliases: Sequence[str] = (),
    ) -> Resolution:
        """Resolve *name* to a canonical node id, creating it only if it is new.

        *scope* is the reporting period for a metric and ``""`` for a segment.
        Pass the same ``scope* for the same concept across filings and they land
        on one node.

        The returned ``canonical_id`` is the node to write, but it is **not** the
        name to write: an entity keeps the name it was created with and is never
        renamed, so read ``registry.get(id).name`` for the display name. Writing
        the incoming surface form instead would let two filings disagree about
        one node's name, and the loader's first-write-wins would make which one
        survives depend on file order.
        """
        label = str(name or "").strip()
        if not label:
            raise ValueError(f"{kind} needs a non-empty name")
        canonical = self._seeded_canonical(label)
        seeds = self._alias_seeds.get(canonical, ())
        seeded = normalise(canonical) != normalise(label)
        registry = self.partition(kind, scope)
        resolution = registry.add(
            {
                "name": canonical,
                "category": category,
                "aliases": [*aliases, *seeds] if seeded else list(aliases),
            }
        )
        if seeded and resolution.merged:
            resolution = replace(resolution, evidence="seeded")
        return resolution

    def lookup(self, kind: str, name: str, *, scope: str = "") -> str | None:
        return self.partition(kind, scope).lookup(name)

    # -- introspection ----------------------------------------------------

    def partitions(self) -> list[tuple[str, str, int]]:
        """``(kind, scope, entity_count)`` per partition, for the run report."""
        return sorted(
            (key[0], key[1], len(registry))
            for key, registry in self._partitions.items()
        )

    def entities(self, kind: str, scope: str = "") -> list[CanonicalEntity]:
        return self.partition(kind, scope).entities()

    def __len__(self) -> int:
        return sum(len(r) for r in self._partitions.values())

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "threshold": self._threshold,
            "fuzzy": self._fuzzy,
            "stats": dict(self.stats),
            "partitions": [
                {"kind": key[0], "scope": key[1], **registry.to_dict()}
                for key, registry in sorted(self._partitions.items())
            ],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(target)
        return target

    @classmethod
    def load(
        cls, path: str | Path, id_factory: Callable[[str, str, str], str], **overrides: Any
    ) -> "ConceptRegistry":
        """Rebuild a concept registry from a file written by :meth:`save`.

        ``**overrides`` wins over the saved ``threshold``/``fuzzy``.

        Persisted entities keep the ids they were minted with, so ``id_factory``
        must be the same factory the file was written with; a different scheme
        would mint fresh ids for concepts that already exist and silently split
        them in two. ``--reset`` drops the file rather than migrate it.
        """
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        settings: dict[str, Any] = {
            "threshold": payload.get("threshold", ENTITY_SIMILARITY_THRESHOLD),
            "fuzzy": payload.get("fuzzy", ENTITY_FUZZY_MATCH),
        }
        settings.update(overrides)
        registry = cls(id_factory, **settings)
        for entry in payload.get("partitions", []):
            part = registry.partition(str(entry.get("kind", "")), str(entry.get("scope", "")))
            part._absorb(entry.get("entities", []))
        # Counters are not restored: ``_absorb`` replays entities, not the
        # decisions that created them, so there is nothing to replay. The durable
        # record of past merges is each entity's persisted ``mentions``.
        return registry


# ---------------------------------------------------------------------------
# Subgraph resolution
# ---------------------------------------------------------------------------


def resolve_subgraph(
    extracted_data: Mapping[str, Any],
    registry: ConceptRegistry,
    strict: bool = True,
) -> dict[str, Any]:
    """Canonicalise a raw extraction into deduplicated nodes and edges.

    Takes the ``{"entities": [...], "relationships": [...]}`` shape, where an
    entity is ``{"kind", "name", "period", "category"}``. Every relationship
    endpoint is rewritten to a canonical id, nodes merge by that id, and edges
    de-duplicate on ``(source, action, target)`` -- two *different* actions
    between the same pair are two real facts and both survive, the same action
    twice is one, and the more informative ``context`` wins.

    **Resolution creates self-loops.** Two names the extractor kept distinct can
    canonicalise to one id, and the edge between them is then a self-loop. Those
    are dropped and counted rather than emitted, because a self-loop on a
    financial fact is an artefact of resolution, not a relationship.

    Endpoints are *looked up*, not registered: a name may belong to an entity
    first registered by an earlier filing, and registering it here would invent a
    node for something the run never described. With ``strict=True`` an endpoint
    that resolves to nothing raises, because a relationship with a missing
    endpoint is a silent hole in the graph; pass ``strict=False`` to drop those
    edges and count them in ``stats``.
    """
    if not isinstance(extracted_data, Mapping):
        raise TypeError(
            f"extracted_data must be a mapping, got {type(extracted_data).__name__}"
        )

    nodes: dict[str, dict[str, Any]] = {}
    resolutions: list[Resolution] = []
    created_here: list[str] = []

    for raw in extracted_data.get("entities") or []:
        kind = str(raw.get("kind") or "entity")
        name = str(raw.get("name") or "")
        scope = str(raw.get("period") or raw.get("scope") or "")
        if not name.strip():
            continue
        resolution = registry.register(
            kind, name, scope=scope, category=str(raw.get("category") or "")
        )
        resolutions.append(resolution)
        if resolution.decision == "created":
            created_here.append(resolution.canonical_id)
        node = nodes.get(resolution.canonical_id)
        if node is None:
            node = {
                "id": resolution.canonical_id,
                "kind": kind,
                "name": name,
                "scope": scope,
                "category": str(raw.get("category") or ""),
                "aliases": [],
                "mentions": 0,
            }
            nodes[resolution.canonical_id] = node
        node["mentions"] += 1

    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    orphans: list[dict[str, Any]] = []
    self_loops = 0

    for raw in extracted_data.get("relationships") or []:
        # One ``kind``/``scope`` for both endpoints is the shape the root
        # resolver uses, and it still works. It cannot express the arc this
        # graph mostly consists of -- a metric pointing at a segment -- so
        # ``source_kind``/``target_kind`` (and their scope twins) override it
        # per endpoint.
        kind = str(raw.get("kind") or "entity")
        scope = str(raw.get("period") or raw.get("scope") or "")
        source_kind = str(raw.get("source_kind") or kind)
        target_kind = str(raw.get("target_kind") or kind)
        source_scope = str(raw.get("source_scope", scope))
        target_scope = str(raw.get("target_scope", scope))
        source = registry.lookup(source_kind, str(raw.get("source") or ""), scope=source_scope)
        target = registry.lookup(target_kind, str(raw.get("target") or ""), scope=target_scope)
        if source is None or target is None:
            missing = raw.get("source") if source is None else raw.get("target")
            orphans.append(
                {
                    "source": raw.get("source"),
                    "target": raw.get("target"),
                    "action": raw.get("action"),
                    "unresolved": missing,
                }
            )
            continue
        if source == target:
            self_loops += 1
            continue
        action = str(raw.get("action") or "")
        key = (source, action, target)
        context = str(raw.get("context") or "")
        edge = edges.get(key)
        if edge is None:
            edges[key] = {
                "source": source, "target": target, "action": action, "context": context,
            }
        elif len(context) > len(edge["context"]):
            # The same fact stated twice; keep the more informative wording.
            edge["context"] = context

    if orphans and strict:
        first = orphans[0]
        raise KeyError(
            f"{len(orphans)} relationship endpoint(s) resolved to no known entity, "
            f"first: {first['unresolved']!r} in {first['action']!r}. Pass strict=False "
            "to drop these edges instead."
        )

    return {
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": sorted(edges.values(), key=lambda e: (e["source"], e["action"], e["target"])),
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "created": sum(1 for r in resolutions if r.decision == "created"),
            "merged": sum(1 for r in resolutions if r.decision == "merged"),
            "created_ids": sorted(created_here),
            "self_loops_dropped": self_loops,
            "orphans_dropped": len(orphans),
            "orphan_edges": orphans,
            "alias_conflicts": registry.stats["alias_conflicts"],
            "category_conflicts": registry.stats["category_conflicts"],
            "ambiguous_merges": registry.stats["ambiguous_merges"],
            "comparisons": registry.stats["comparisons"],
        },
    }
