"""Canonicalise entities extracted from disparate chunks into one deduplicated graph.

The pipeline that produces these entities runs once per chunk, on a model that
has not seen the other chunks. So the same real-world thing arrives spelled five
ways across five passages, and a naive merge on either end produces a graph that
is either duplicated or wrong. This module is the dedup step between extraction
and storage.

    registry = EntityRegistry()
    resolved = registry.add({"name": "Euler-Lagrange equation", "aliases": [], ...})
    # Resolution(canonical_id='euler-lagrange-equation', decision='created')

    graph = resolve_subgraph(extraction_output, registry)
    # {'nodes': [...], 'edges': [...], 'stats': {...}}

Three properties drive the design:

**Identity is stable, aliases accumulate.** A canonical record keeps the name it
was created with and only ever gains aliases. It is never renamed. Re-ingesting
a document therefore produces the same ids and does not churn the graph, which
matters because those ids are foreign keys in whatever store is downstream.

**Evidence is recorded, not just a boolean.** Every merge returns why it merged
-- exact name, exact alias, fuzzy name, embedding -- plus the runner-up score.
Record linkage is a domain where a wrong merge is far more expensive than a
missed one, and you cannot review a decision you did not record.

**Resolution creates self-loops.** Two names that the extractor correctly kept
distinct can canonicalise to one id, and the edge between them becomes a
self-loop *after* merging. Those are dropped and counted; see
:func:`resolve_subgraph`.

Similarity is pluggable. The default :class:`LexicalSimilarity` is pure Python
and needs no dependencies, so the module runs anywhere. Inject
:class:`EmbeddingSimilarity` (or any object with ``score(a, b) -> float``) when
you have an embedding function and want to catch synonyms that share no tokens
at all -- which token blocking cannot see, by design.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

__all__ = [
    "CanonicalEntity",
    "EmbeddingSimilarity",
    "EntityRegistry",
    "LexicalSimilarity",
    "Resolution",
    "fuzzy_similarity",
    "normalise",
    "resolve_subgraph",
    "slugify",
    "tokenize",
]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_PERIOD = re.compile(r"\.", re.UNICODE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_UNDERSCORE = re.compile(r"[\s_]+", re.UNICODE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")

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
    to ``"euler lagrange equation"``. Diacritics are stripped rather than kept
    as their own characters so ``"café"`` matches ``"cafe"``.

    Periods are *deleted* while other punctuation becomes a space. That
    asymmetry is deliberate: in ``"U.S. Treasury"`` the dot is an abbreviation
    mark, so spacing it gives ``"u s treasury"`` and fails to match
    ``"US Treasury"``, whereas a hyphen in ``"Euler-Lagrange"`` really does
    separate two words. The cost is that ``"3.5"`` folds to ``"35"``, which
    matters for values but not for entity names.

    The cost of folding this hard is that indistinguishable names become
    indistinguishable: ``"C++"`` and ``"C"`` both reduce to ``"c"`` and will
    merge. That is inherent to character-level normalisation, and the way out is
    embeddings, not a longer stopword list.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = _PUNCT.sub(" ", _PERIOD.sub("", stripped.casefold()))
    return _UNDERSCORE.sub(" ", spaced).strip()


def slugify(text: str) -> str:
    """The canonical-id form of a name.

    Falls back to a hash when a name has no latin or digit characters at all;
    otherwise every non-latin name would slug to the same empty string and
    collide with every other one.
    """
    slug = _NON_SLUG.sub("-", normalise(text)).strip("-")
    if slug:
        return slug
    import hashlib

    return "entity-" + hashlib.sha1(normalise(text).encode("utf-8")).hexdigest()[:8]


@lru_cache(maxsize=8192)
def tokenize(text: str) -> tuple[str, ...]:
    """Content tokens, order preserved, for order-insensitive comparison.

    Single characters are kept on purpose: ``"C"``, ``"L"`` and ``"T"`` are real
    entity names, and dropping them would make short names unmatchable.
    """
    return tuple(t for t in normalise(text).split() if t not in _STOPWORDS)


@lru_cache(maxsize=16384)
def _ratio(left: str, right: str) -> float:
    """Symmetric character-level ratio, 0.0-1.0.

    ``difflib.SequenceMatcher`` is *not* symmetric: it finds different matching
    blocks depending on which string comes first, so it scores
    ``("ebitda", "earnings before ...")`` at 0.19 one way and 0.07 the other. A
    merge score that depends on argument order is not a merge score, so the two
    directions are averaged. The mean is used rather than the min (which would
    suppress real morphological matches) or the max (which would bias toward
    false merges).

    ``autojunk`` is off because these strings are short and name-shaped; the
    heuristic that discards "popular" elements misfires on things like "ATP".
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    forward = SequenceMatcher(None, left, right, autojunk=False).ratio()
    backward = SequenceMatcher(None, right, left, autojunk=False).ratio()
    return (forward + backward) / 2.0


# ---------------------------------------------------------------------------
# Similarity providers
# ---------------------------------------------------------------------------


class Similarity(Protocol):
    """Anything that can score two strings from 0.0 to 1.0."""

    def score(self, left: str, right: str) -> float:  # pragma: no cover - protocol
        ...


def _is_inflection(short: str, long: str) -> bool:
    """True when one token is the other plus a short suffix.

    This is what separates the two near-miss cases lexical similarity cannot
    otherwise tell apart, and the distinction decides merges:

    * ``equation`` / ``equations`` -- one word, pluralised. Same entity.
    * ``encoder`` / ``decoder`` -- two different words that happen to share a
      suffix. Different entities, and merging them corrupts a graph silently.

    Both are "one near-miss token out of two", so token counts cannot separate
    them. Requiring a shared stem of near-identical length can.
    """
    if len(short) > len(long):
        short, long = long, short
    # A suffix of at most two characters: "s", "es", "ed", "ing". Wider than
    # that and it stops being an inflection -- "cat"/"cattle" is a coincidence
    # of spelling, not a plural.
    return long.startswith(short) and len(long) - len(short) <= 2


def _token_alignment(left: Sequence[str], right: Sequence[str]) -> float:
    """Greedy one-to-one token matching, forgiving inflections only.

    Distinct words earn no credit, which is what holds sibling concepts apart.
    Unlike a character ratio over the joined token strings, a token is atomic:
    ``"encoder transformer"`` and ``"decoder transformer"`` share 18 of their 19
    characters, so every character-based measure rates them ~0.95, while this
    rates them 0.5.
    """
    if not left or not right:
        return 0.0
    remaining = list(right)
    matched = 0.0
    for token in left:
        for index, other in enumerate(remaining):
            if token == other or _is_inflection(token, other):
                matched += 1.0
                remaining.pop(index)
                break
    return matched / max(len(left), len(right))


def fuzzy_similarity(left: str, right: str) -> float:
    """Token-aware string similarity, 0.0-1.0, pure Python.

    Two regimes, because the right signal depends on how the names relate:

    * **overlapping tokens** -- decided by token alignment plus Jaccard. This is
      what links ``"Euler-Lagrange Equation"`` to ``"Euler Lagrange Equations"``
      while holding ``"Transformer Encoder"`` and ``"Transformer Decoder"``
      apart.
    * **disjoint tokens** -- decided by a character ratio, the only case where
      one is trusted, because nothing else is available. It links
      ``"Mitochondria"`` to ``"mitochondrion"``, which share no token at all.

    Character comparison is confined to the second regime on purpose. Letting it
    vote whenever tokens overlap merges ``"Mitochondrial DNA"`` with
    ``"Mitochondrial RNA"`` (0.94) -- one short token differing across twenty
    identical characters.

    The popular ``token_set_ratio`` is deliberately not used either: it scores
    ``"EBITDA"`` against ``"Adjusted EBITDA"`` at 1.0 because the intersection
    equals the shorter set, and for entity resolution every prefix of a name
    matching its parent is a merge bug waiting to happen.
    """
    left_norm, right_norm = normalise(left), normalise(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0

    left_tokens, right_tokens = tokenize(left), tokenize(right)
    if not left_tokens or not right_tokens:
        return _ratio(left_norm, right_norm)

    left_set, right_set = set(left_tokens), set(right_tokens)
    intersection = left_set & right_set
    if not intersection:
        # Disjoint tokens, so no token signal is meaningful. Character overlap
        # is all that is left, and it is usually near zero for real synonyms.
        return _ratio(left_norm, right_norm)

    jaccard = len(intersection) / len(left_set | right_set)
    return max(_token_alignment(left_tokens, right_tokens), jaccard)


@dataclass
class LexicalSimilarity:
    """The default, dependency-free provider."""

    def score(self, left: str, right: str) -> float:
        return fuzzy_similarity(left, right)


@dataclass
class EmbeddingSimilarity:
    """Wraps any batched embedding callable in the :class:`Similarity` protocol.

    ``embed_fn`` takes a list of strings and returns one vector per string. It is
    called in batches and every result is cached by text, so a registry asks for
    an embedding once per distinct name no matter how many candidates it scores.

    Catches what token blocking structurally cannot: ``"EBITDA"`` and
    ``"earnings before interest, taxes, depreciation and amortisation"`` share no
    tokens, so no lexical provider will ever pair them.
    """

    embed_fn: Callable[[Sequence[str]], Sequence[Sequence[float]]]
    name_weight: float = 0.8
    cache: dict[str, tuple[float, ...]] = field(default_factory=dict)

    def _vector(self, text: str) -> tuple[float, ...]:
        cached = self.cache.get(text)
        if cached is None:
            vectors = self.embed_fn([text])
            if not vectors or not vectors[0]:
                raise ValueError(f"embed_fn returned no vector for {text!r}")
            cached = tuple(float(x) for x in vectors[0])
            self.cache[text] = cached
        return cached

    def warm(self, texts: Iterable[str]) -> int:
        """Embed a batch up front; returns how many vectors were fetched."""
        missing = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if not missing:
            return 0
        for start in range(0, len(missing), 256):
            batch = missing[start : start + 256]
            for text, vector in zip(batch, self.embed_fn(batch)):
                self.cache[text] = tuple(float(x) for x in vector)
        return len(missing)

    @staticmethod
    def cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right):
            raise ValueError(
                f"embedding dimensions differ: {len(left)} vs {len(right)}"
            )
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = sum(a * a for a in left) ** 0.5
        right_norm = sum(b * b for b in right) ** 0.5
        if not left_norm or not right_norm:
            return 0.0
        return max(-1.0, min(1.0, dot / (left_norm * right_norm)))

    def score(self, left: str, right: str) -> float:
        return self.cosine(self._vector(left), self._vector(right))

    def score_blended(self, name_a: str, desc_a: str, name_b: str, desc_b: str) -> float:
        """Name similarity, nudged by description similarity.

        The name carries identity, so it dominates. A description can rescue a
        borderline name match but cannot manufacture one on its own -- generic
        descriptions would otherwise merge unrelated entities.
        """
        name_score = self.score(name_a, name_b)
        if not desc_a or not desc_b:
            return name_score
        return (
            self.name_weight * name_score
            + (1.0 - self.name_weight) * self.score(desc_a, desc_b)
        )


def _coerce_similarity(provider: Any) -> Similarity:
    if provider is None:
        return LexicalSimilarity()
    if hasattr(provider, "score"):
        return provider
    if callable(provider):
        return _CallableSimilarity(provider)
    raise TypeError(
        "similarity must be None, an object with .score(left, right), or a "
        f"callable, got {type(provider).__name__}"
    )


@dataclass
class _CallableSimilarity:
    fn: Callable[[str, str], float]

    def score(self, left: str, right: str) -> float:
        return float(self.fn(left, right))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class CanonicalEntity:
    """One node in the canonical registry."""

    id: str
    name: str
    category: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    mentions: int = 1
    category_conflicts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "aliases": list(self.aliases),
            "mentions": self.mentions,
        }

    def variants(self) -> list[str]:
        """Every surface form this entity answers to, canonical name first."""
        return [self.name, *self.aliases]


@dataclass
class Resolution:
    """The outcome of registering one extracted entity."""

    canonical_id: str
    decision: str  # "created" | "merged"
    evidence: str  # "new" | "name" | "alias" | "fuzzy" | "embedding"
    score: float = 1.0
    matched_on: str = ""
    second_best: float = 0.0
    ambiguous: bool = False
    added_aliases: list[str] = field(default_factory=list)
    rejected_aliases: list[str] = field(default_factory=list)
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
            "added_aliases": self.added_aliases,
            "rejected_aliases": self.rejected_aliases,
            "category_conflict": self.category_conflict,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class EntityRegistry:
    """Canonical entity store with lexical-then-semantic matching.

    Matching runs cheapest-first, and the expensive stage only sees candidates
    that token blocking says could possibly match:

    1. exact key match on the name -- one dict lookup;
    2. exact key match on any alias -- one dict lookup;
    3. fuzzy or embedding scoring, over candidates that share a content token.

    Stage 3 is where the cost lives, so it is blocked: an entity sharing no
    content token with the incoming name scores ~0 under any token-based
    provider, and scoring it anyway is pure waste. Set ``blocking="none"`` to
    disable that and compare against everything, which is what the tests use to
    prove blocking changes speed and not results.
    """

    def __init__(
        self,
        threshold: float = 0.88,
        similarity: Any = None,
        blocking: str = "token",
        max_candidates: int = 64,
        ambiguity_margin: float = 0.02,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold!r}")
        if blocking not in {"token", "none"}:
            raise ValueError(
                f"blocking must be 'token' or 'none', got {blocking!r}"
            )
        self.threshold = float(threshold)
        self.similarity: Similarity = _coerce_similarity(similarity)
        self.blocking = blocking
        self.max_candidates = int(max_candidates)
        self.ambiguity_margin = float(ambiguity_margin)

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
            "comparisons": 0,
            "fuzzy_lookups": 0,
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
        for token in tokenize(entity.name):
            self._token_index.setdefault(token, set()).add(entity.id)
        for alias in entity.aliases:
            for token in tokenize(alias):
                self._token_index.setdefault(token, set()).add(entity.id)

    def _unique_id(self, name: str) -> str:
        # A slug is a function of the normalised name, and normalisation maps to
        # a restricted alphabet, so distinct names cannot collide here. The
        # disambiguation suffix would be unreachable code, so it is left out
        # rather than left in as untested defensive code.
        return slugify(name)

    # -- lookup -----------------------------------------------------------

    def lookup(self, name: str) -> str | None:
        """Resolve a surface form to a canonical id without registering it.

        Used by :func:`resolve_subgraph` for relationship endpoints, which may
        name an entity first registered by an earlier chunk.
        """
        key = normalise(name)
        if not key:
            return None
        found = self._name_index.get(key)
        if found is not None:
            return found
        alias_hits = self._alias_index.get(key)
        if alias_hits:
            # Deterministic when an alias is contested.
            return sorted(alias_hits)[0]
        return None

    def _candidates(self, name: str, aliases: Sequence[str]) -> set[str]:
        """Blocking keys for the fuzzy stage, rarest first.

        Blocking on *any* shared token is not enough on its own. Tokens are
        unevenly distributed, and a common one ("attention", "system", or in a
        synthetic corpus the literal word "entity") matches a large slice of the
        registry, which is the same cost as no blocking at all. So tokens are
        tried in increasing order of how many entities hold them, and the set
        stops growing once ``max_candidates`` is reached: rare tokens are the
        discriminative ones, so this bounds the work without losing the matches
        that matter.
        """
        if self.blocking == "none":
            return set(self._entities)

        postings: list[tuple[int, set[str]]] = []
        for surface in (name, *aliases):
            for token in tokenize(surface):
                ids = self._token_index.get(token)
                if ids:
                    postings.append((len(ids), ids))
        if not postings:
            return set()

        # Deduplicate identical posting lists, then rarest first.
        seen: dict[int, set[str]] = {}
        for _, ids in postings:
            seen[id(ids)] = ids
        ordered = sorted(seen.values(), key=len)

        # Prefer discriminative keys. A posting list wider than the budget says
        # "this token is everywhere", so admitting it would put the whole
        # registry back in play -- and blocking that does not block is worse than
        # no blocking, because it looks like it is working.
        usable = [ids for ids in ordered if len(ids) <= self.max_candidates]
        if not usable:
            # Every token is common, so no selective key exists. Fall back to the
            # rarest posting list, uncapped: comparing a few arbitrary candidates
            # would give a wrong answer more often than it would give a fast
            # one, and this is the correctness-over-speed branch by design. It is
            # also the reason max_candidates is a budget, not a guarantee.
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
        """Best (id, score, runner-up, matched variant) above the threshold."""
        self.stats["fuzzy_lookups"] += 1
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
                self.stats["comparisons"] += 1
                score = self.similarity.score(variant, name)
                if hasattr(self.similarity, "score_blended") and description and entity.description:
                    blended = self.similarity.score_blended(
                        entity.name, entity.description, name, description
                    )
                    if blended > score:
                        score, best_variant = blended, variant
                if score > best_score:
                    best_score, best_variant = score, variant
            if best_score >= self.threshold:
                scored.append((best_score, candidate_id, best_variant))

        if not scored:
            return None, 0.0, 0.0, ""
        scored.sort(key=lambda row: (-row[0], row[1]))
        top_score, top_id, top_variant = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        return top_id, top_score, runner_up, top_variant

    # -- registration -----------------------------------------------------

    def add(
        self, entity: Mapping[str, Any] | CanonicalEntity
    ) -> Resolution:
        """Register one extracted entity, merging it if it matches a known one."""
        if isinstance(entity, CanonicalEntity):
            record: Mapping[str, Any] = {
                "name": entity.name,
                "category": entity.category,
                "description": entity.description,
                "aliases": entity.aliases,
            }
        elif isinstance(entity, Mapping):
            record = entity
        else:
            raise TypeError(
                f"entity must be a mapping or CanonicalEntity, got {type(entity).__name__}"
            )

        name = str(record.get("name") or "").strip()
        if not name or not normalise(name):
            raise ValueError("entity needs a non-empty name")
        description = str(record.get("description") or "").strip()
        category = str(record.get("category") or "").strip()
        raw_aliases = record.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        incoming_aliases = [
            str(a).strip() for a in raw_aliases if str(a).strip() and str(a) != name
        ]

        # -- 1. exact name
        exact = self._name_index.get(normalise(name))
        if exact is not None:
            self.stats["exact_name_hits"] += 1
            return self._merge(exact, name, incoming_aliases, category, description,
                               evidence="name", score=1.0, matched_on=name)

        # -- 2. exact alias
        alias_hits = self._alias_index.get(normalise(name))
        if alias_hits:
            self.stats["exact_alias_hits"] += 1
            target = sorted(alias_hits)[0]
            return self._merge(target, name, incoming_aliases, category, description,
                               evidence="alias", score=1.0, matched_on=name)

        # -- 3. fuzzy / embedding
        best_id, score, runner_up, variant = self._best_fuzzy(name, description, incoming_aliases)
        if best_id is not None:
            self.stats["fuzzy_hits"] += 1
            evidence = "embedding" if hasattr(self.similarity, "score_blended") else "fuzzy"
            return self._merge(
                best_id, name, incoming_aliases, category, description,
                evidence=evidence, score=score, matched_on=variant,
                second_best=runner_up,
            )

        return self._create(name, incoming_aliases, category, description)

    def _create(
        self, name: str, aliases: Sequence[str], category: str, description: str
    ) -> Resolution:
        canonical_id = self._unique_id(name)
        kept, rejected = self._safe_aliases(canonical_id, name, aliases)
        # An alias that is just another casing of the name is noise, and at
        # creation time the name is not in the index yet so _safe_aliases
        # cannot see the clash.
        own_key = normalise(name)
        kept = [alias for alias in kept if normalise(alias) != own_key]
        entity = CanonicalEntity(
            id=canonical_id,
            name=name,
            category=category,
            description=description,
            aliases=kept,
            mentions=1,
        )
        self._entities[canonical_id] = entity
        self._index(entity)
        self.stats["created"] += 1
        return Resolution(
            canonical_id=canonical_id,
            decision="created",
            evidence="new",
            score=1.0,
            added_aliases=list(kept),
            rejected_aliases=rejected,
        )

    def _safe_aliases(
        self, canonical_id: str, name: str, aliases: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        """Keep aliases that do not belong to a different canonical entity.

        Without this, an entity could claim a name another entity owns, and the
        next lookup on that name would resolve to whichever wrote its index
        first. Contested aliases are reported instead of silently taken.
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
        # the first one wins ties so re-ingest is idempotent.
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
        for alias in added:
            if normalise(alias) not in {normalise(v) for v in entity.variants()}:
                entity.aliases.append(alias)
        if added:
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
            added_aliases=added,
            rejected_aliases=rejected,
            category_conflict=conflict,
        )

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "threshold": self.threshold,
            "entities": [
                {
                    "id": e.id,
                    "name": e.name,
                    "category": e.category,
                    "description": e.description,
                    "aliases": e.aliases,
                    "mentions": e.mentions,
                    "category_conflicts": e.category_conflicts,
                }
                for e in self._entities.values()
            ],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted save cannot truncate the registry.
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(target)
        return target

    @classmethod
    def load(
        cls, path: str | Path, threshold: float | None = None, **kwargs: Any
    ) -> "EntityRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        registry = cls(
            threshold=payload.get("threshold", 0.88) if threshold is None else threshold,
            **kwargs,
        )
        for record in payload.get("entities", []):
            entity = CanonicalEntity(
                id=record["id"],
                name=record["name"],
                category=record.get("category", ""),
                description=record.get("description", ""),
                aliases=list(record.get("aliases", [])),
                mentions=int(record.get("mentions", 1)),
                category_conflicts=int(record.get("category_conflicts", 0)),
            )
            registry._entities[entity.id] = entity
            registry._index(entity)
        return registry


# ---------------------------------------------------------------------------
# Subgraph resolution
# ---------------------------------------------------------------------------


def resolve_subgraph(
    extracted_data: Mapping[str, Any],
    registry: EntityRegistry | None = None,
    threshold: float = 0.88,
    similarity: Any = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Canonicalise a raw extraction into deduplicated nodes and edges.

    Takes the ``{"entities": [...], "relationships": [...]}`` shape that
    ``graph_extractor.extract_triples`` returns. Every relationship endpoint is
    rewritten to a canonical id, nodes are merged by name, and edges are
    de-duplicated on ``(source, target, action)`` -- two actions between the same
    pair are two real facts and both survive, while the same action twice is
    one.

    Relationship endpoints are looked up rather than assumed present: a name may
    belong to an entity registered by an earlier chunk, so an endpoint that is
    not in *this* payload is still resolvable. With ``strict=True`` (the
    default) an endpoint that resolves to nothing is an error, because a
    relationship with a missing endpoint silently becomes a hole in the graph.
    Pass ``strict=False`` to drop those edges and report them in ``stats``.

    Resolution can create self-loops: two names the extractor kept distinct may
    canonicalise to one id, and the edge between them is then a self-loop. Those
    are dropped and counted rather than emitted.
    """
    if not isinstance(extracted_data, Mapping):
        raise TypeError(
            f"extracted_data must be a mapping, got {type(extracted_data).__name__}"
        )
    if registry is None:
        registry = EntityRegistry(threshold=threshold, similarity=similarity)

    nodes: dict[str, dict[str, Any]] = {}
    resolutions: list[Resolution] = []
    created_here: list[str] = []

    for raw in extracted_data.get("entities") or []:
        resolution = registry.add(raw)
        resolutions.append(resolution)
        if resolution.decision == "created":
            created_here.append(resolution.canonical_id)
        entity = registry.get(resolution.canonical_id)
        if entity is None:  # pragma: no cover - registry guarantees it exists
            continue
        node = nodes.get(entity.id)
        if node is None:
            node = {
                "id": entity.id,
                "name": entity.name,
                "category": entity.category,
                "description": entity.description,
                "aliases": list(entity.aliases),
                "mentions": 0,
            }
            nodes[entity.id] = node
        node["mentions"] += 1
        if len(entity.description) > len(node["description"]):
            node["description"] = entity.description
        if not node["category"] and entity.category:
            node["category"] = entity.category
        known = {normalise(a) for a in node["aliases"]}
        for alias in entity.aliases:
            if normalise(alias) not in known:
                node["aliases"].append(alias)
                known.add(normalise(alias))

    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    orphans: list[dict[str, Any]] = []
    self_loops = 0

    for raw in extracted_data.get("relationships") or []:
        source = registry.lookup(str(raw.get("source") or ""))
        target = registry.lookup(str(raw.get("target") or ""))
        if source is None or target is None:
            missing = str(raw.get("source") if source is None else raw.get("target"))
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
        edge = edges.get(key)
        context = str(raw.get("context") or "")
        if edge is None:
            edges[key] = {
                "source": source,
                "target": target,
                "action": action,
                "context": context,
            }
        elif len(context) > len(edge["context"]):
            # Same fact stated twice; keep the more informative wording.
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
