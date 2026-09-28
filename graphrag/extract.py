"""Entity and relationship extraction from passage text.

The prompts here contain *format* rules but zero domain rules. The taxonomy
hint is explicitly presented as a non-exhaustive example so the model is free
to invent the categories the document actually needs, which is what keeps the
graph schema-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import GraphRAGConfig
from .llm import LLMClient, LLMError
from .resolve import is_usable_name, slugify

EXTRACTION_SYSTEM = """\
You are a knowledge-graph extraction engine. You read a passage from an \
arbitrary document and return the entities and relationships it states.

Return a single JSON object with exactly two keys:

{
  "entities": [
    {"name": string, "type": string, "description": string}
  ],
  "relationships": [
    {"source": string, "target": string, "relation": string, "description": string}
  ]
}

Rules for "entities":
- "name": the name as the passage writes it. Use the full canonical name, not a \
pronoun or an abbreviation the passage never expands.
- "type": a short lowercase category invented from the passage's own subject \
matter. Common choices include organism, material, method, instrument, person, \
organization, place, period, measurement, process, concept, work. This list is \
an example, not a limit: use whatever fits the document.
- "description": one clause stating what the passage says this entity is or does.

Rules for "relationships":
- "source" and "target" must exactly match a "name" you listed in "entities".
- "relation": a short lowercase verb phrase naming the connection the passage \
asserts, such as "produces", "depends_on", "contains", "lives_in", "member_of". \
Prefer a precise verb over a vague one; a weak relation is worse than none.
- "description": one clause, or an empty string if the relation is self-evident.

Discipline:
- Extract only what the passage states or directly entails. Do not add outside \
knowledge and do not speculate.
- Include a relationship ONLY when the passage asserts that specific \
connection. A passage usually asserts far fewer relationships than you might \
expect. Omitting an uncertain relationship is always better than including a \
speculative one.
- Never reuse one relation verb to connect a source to several unrelated \
targets. If you notice yourself repeating a verb, you are padding the list.
- Prefer specific named entities over bare common nouns.
- Emit an empty array for a key when the passage supports nothing for it. Never \
return placeholder entries such as "unknown" or "N/A".
- Reply with the JSON object only.
"""

IDENTIFY_SYSTEM = """\
You resolve a user question against a knowledge graph by naming the entities it \
mentions.

Return a single JSON object:

{"entities": [string]}

Rules:
- Include only proper names, defined terms, and distinctive noun phrases that a \
graph node could plausibly be named.
- Do not include stopwords, question words, or generic verbs.
- Do not invent entities the question does not mention.
- An empty list is the correct answer when the question names nothing findable.
- Reply with the JSON object only.
"""

ANSWER_SYSTEM = """\
You answer a question using only a retrieved subgraph from a knowledge graph.

You are given entities and relationships, each carrying a bracketed tag like \
[E1] or [E12].

Rules:
- Use only the supplied context. If it does not contain the answer, say so \
plainly and name what is missing.
- Cite every claim with the tags of the entities or relationships it rests on, \
placed at the end of the sentence or clause, for example: [E1] [E4].
- Never invent a tag that is not in the context.
- Answer directly in prose. No preamble, no restating the question.
"""


# Constrained-decoding schemas. These are written in the strict JSON Schema
# subset (every property listed in `required`, `additionalProperties: false`) so
# providers that support schema-constrained decoding — Gemini and OpenAI — can
# enforce the shape at decode time instead of relying on the model to comply.
# Providers without support ignore these and the payload is still validated by
# parse_extraction, so behaviour degrades rather than breaks.

_STRING = {"type": "string"}
_ENTITY_ITEM = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "entity_type": _STRING,
        "description": _STRING,
    },
    "required": ["name", "entity_type", "description"],
    "additionalProperties": False,
}
_RELATIONSHIP_ITEM = {
    "type": "object",
    "properties": {
        "source": _STRING,
        "target": _STRING,
        "relation": _STRING,
        "description": _STRING,
    },
    "required": ["source", "target", "relation", "description"],
    "additionalProperties": False,
}
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": _ENTITY_ITEM},
        "relationships": {"type": "array", "items": _RELATIONSHIP_ITEM},
    },
    "required": ["entities", "relationships"],
    "additionalProperties": False,
}
IDENTIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": _STRING},
    },
    "required": ["entities"],
    "additionalProperties": False,
}


@dataclass
class ExtractedEntity:
    name: str
    entity_type: str
    description: str
    id: str = ""


@dataclass
class ExtractedRelationship:
    source: str
    target: str
    relation: str
    description: str
    source_id: str = ""
    target_id: str = ""


@dataclass
class ChunkExtraction:
    entities: list[ExtractedEntity] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)
    error: str | None = None
    # Records rejected by validation. Non-zero means the model output needed
    # repair, which is a quality signal worth surfacing.
    rejected: int = 0


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _coerce_list(value: Any) -> list[dict[str, Any]]:
    """Accept a list, a single dict, or a wrapper key, and return dicts."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _tokens(text: str) -> list[str]:
    """Split *text* into comparable word tokens.

    Tokenising before slugifying matters: ``slugify`` deliberately removes every
    separator, so "Riftia pachyptila" and "RiftiaPachyptila" both collapse to one
    token. Splitting on whitespace first keeps word boundaries available for
    phrase matching.
    """
    out: list[str] = []
    for word in re.split(r"[\s\-_/]+", text or ""):
        slug = slugify(word)
        if slug:
            out.append(slug)
    return out


def _slug_text(text: str) -> str:
    """Normalise prose to a space-separated token stream for containment checks."""
    return " ".join(_tokens(text))


def is_grounded(name: str, passage_slugs: str) -> bool:
    """Whether *name* has lexical support in the source passage.

    A name is grounded when its token sequence appears contiguously in the
    passage, or when every significant token in it appears somewhere in the
    passage. The second test matters: a model legitimately composes "Sourdough
    starter" out of "Sourdough bread method" and "a starter of flour", and
    demanding one contiguous match would reject correct extractions.

    This catches names invented wholesale, but not a name whose tokens all
    happen to occur in unrelated contexts — that is a semantic judgement, left
    to the model, with fan-out reporting as the backstop.
    """
    name_tokens = _tokens(name)
    if not name_tokens:
        return False
    # Names made only of very short tokens carry no verifiable signal.
    significant = [t for t in name_tokens if len(t) >= 3]
    if not significant:
        return False

    passage_tokens = passage_slugs.split()
    span = len(name_tokens)
    for start in range(len(passage_tokens) - span + 1):
        if passage_tokens[start : start + span] == name_tokens:
            return True
    return all(token in passage_tokens for token in significant)


def parse_extraction(
    payload: dict[str, Any], config: GraphRAGConfig, passage: str | None = None
) -> ChunkExtraction:
    """Turn a raw JSON payload into validated extraction records.

    Everything here is defensive: unknown keys are ignored, wrong types are
    dropped, unusable names are discarded, relationships whose endpoints were
    not extracted are dropped, and — when *passage* is given — anything not
    grounded in that passage is dropped as hallucinated.

    Returns records plus a count of what was rejected, so a caller can report
    extraction quality rather than silently losing data.
    """
    result = ChunkExtraction()
    passage_slugs = _slug_text(passage) if passage else None

    for record in _coerce_list(payload.get("entities"))[: config.max_entities_per_chunk]:
        name = _as_text(record.get("name") or record.get("entity") or record.get("title"))
        if not is_usable_name(name, config.min_entity_name_length):
            continue
        entity_id = slugify(name)
        if not entity_id:
            continue
        kind = _as_text(record.get("type") or record.get("entity_type")) or "unspecified"
        desc = _as_text(record.get("description"))
        if len(desc) < config.min_description_length:
            desc = ""
        result.entities.append(
            ExtractedEntity(
                name=name, entity_type=kind.lower(), description=desc, id=entity_id
            )
        )

    # De-duplicate by id inside a single chunk, keeping the richer record.
    by_id: dict[str, ExtractedEntity] = {}
    for entity in result.entities:
        existing = by_id.get(entity.id)
        if existing is None:
            by_id[entity.id] = entity
        else:
            if not existing.description and entity.description:
                existing.description = entity.description
            if existing.entity_type == "unspecified" and entity.entity_type != "unspecified":
                existing.entity_type = entity.entity_type
    result.entities = list(by_id.values())

    for record in _coerce_list(payload.get("relationships"))[
        : config.max_relationships_per_chunk
    ]:
        source = _as_text(record.get("source") or record.get("from") or record.get("head"))
        target = _as_text(record.get("target") or record.get("to") or record.get("tail"))
        if not source or not target:
            continue
        source_id = slugify(source)
        target_id = slugify(target)
        if not source_id or not target_id or source_id == target_id:
            result.rejected += 1
            continue
        # A self-referential or dangling endpoint is dropped: the graph has no
        # way to represent it without inventing a node.
        if source_id not in by_id or target_id not in by_id:
            result.rejected += 1
            continue
        if passage_slugs is not None and not (
            is_grounded(source, passage_slugs) and is_grounded(target, passage_slugs)
        ):
            result.rejected += 1
            continue
        relation = _as_text(
            record.get("relation") or record.get("type") or record.get("label")
        ) or "related_to"
        desc = _as_text(record.get("description"))
        if len(desc) < config.min_description_length:
            desc = ""
        result.relationships.append(
            ExtractedRelationship(
                source=source,
                target=target,
                relation=relation.strip().lower(),
                description=desc,
                source_id=source_id,
                target_id=target_id,
            )
        )

    seen_edges: set[tuple[str, str, str]] = set()
    deduped: list[ExtractedRelationship] = []
    for edge in result.relationships:
        key = (edge.source_id, edge.target_id, edge.relation)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        deduped.append(edge)
    result.relationships = deduped
    return result


def extract_chunk(
    client: LLMClient,
    passage: str,
    config: GraphRAGConfig,
    location: str = "",
) -> ChunkExtraction:
    """Run extraction over one chunk, returning a record even on failure.

    A failed chunk is reported rather than raised: one bad passage should not
    abort a multi-hundred-page ingestion.
    """
    where = f" (from {location})" if location else ""
    user = (
        f"Extract the entities and relationships stated in this passage{where}.\n\n"
        "PASSAGE:\n"
        f"{passage}\n\n"
        "Respond with the JSON object only."
    )
    try:
        payload = client.complete_json(EXTRACTION_SYSTEM, user, EXTRACTION_SCHEMA)
    except LLMError as exc:
        return ChunkExtraction(error=str(exc))
    except Exception as exc:  # noqa: BLE001 - provider exceptions vary
        return ChunkExtraction(error=f"unexpected error: {exc}")
    return parse_extraction(payload, config, passage=passage)


def identify_entities(client: LLMClient, question: str) -> list[str]:
    """Ask the model which graph entities a question refers to.

    A provider failure propagates: returning ``[]`` would be indistinguishable
    from "the question names nothing", and the caller would report a wrong
    reason for an empty result.
    """
    user = (
        "List the entities named in this question.\n\n"
        f"QUESTION: {question}\n\n"
        "Respond with the JSON object only."
    )
    payload = client.complete_json(IDENTIFY_SYSTEM, user, IDENTIFY_SCHEMA)

    raw = payload.get("entities")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("entity")
        name = _as_text(item)
        if not name:
            continue
        key = slugify(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


_WS = re.compile(r"\s+")


def normalise_text(value: str) -> str:
    return _WS.sub(" ", value or "").strip()
