"""Domain-agnostic knowledge-graph extraction from unstructured text.

One entry point: :func:`extract_triples`. Give it a passage, get back entities
and relationships, whatever the subject matter.

    >>> extract_triples("Mitochondria generate ATP via oxidative phosphorylation.")
    {'entities': [{'name': 'Mitochondria', 'category': 'BIOLOGICAL_STRUCTURE', ...}],
     'relationships': [{'source': 'Mitochondria', 'target': 'ATP', ...}]}

Design notes worth knowing before changing anything here:

- **No fixed vocabulary.** Neither the JSON schema nor the prompt constrains
  ``category`` or ``action`` to an enum. A closed list is the fastest way to
  make a "domain-agnostic" extractor quietly domain-specific: the model starts
  forcing a biology entity into ``CONCEPT`` because nothing else is allowed.
  Categories are invented per passage and actions are normalised verbs, so
  finance, mathematics, and molecular biology all work through the same path.
- **The schema is a contract, not a suggestion.** Constrained decoding is used
  when the provider supports it, so the model cannot emit a missing key or an
  invented one. Everything after that is defence in depth: the model is asked
  politely, then the output is repaired, then validated, then filtered.
- **A relationship is only kept if both ends resolve.** Models name endpoints
  inconsistently -- an alias, a different capitalisation, a near-miss. Those
  are resolved back to real entity names, and a relationship whose endpoints
  cannot be resolved is dropped rather than inventing a phantom node. The
  count of dropped relationships is reported rather than swallowed.
- **Malformed JSON is expected, not exceptional.** Models wrap JSON in prose,
  fence it, emit Python literals, trail commas, or run out of tokens mid-object.
  :func:`recover_json` handles each of those, and a truncated extraction is
  salvaged rather than thrown away.

Dependencies: ``openai`` (for OpenAI and OpenRouter, which is OpenAI-compatible)
and, optionally, ``pydantic`` for validation. Without pydantic the module falls
back to an equivalent hand-written validator with the same semantics.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "EXTRACTION_SYSTEM",
    "ExtractionError",
    "ExtractionResult",
    "build_extraction_schema",
    "create_client",
    "extract_corpus",
    "extract_triples",
    "normalise_action",
    "recover_json",
    "validate_payload",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: The exact shape the caller is promised. Order matters only for readability;
#: every key is required so constrained decoding cannot omit one.
_STRING = {"type": "string"}
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

ENTITY_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "category": _STRING,
        "description": _STRING,
        "aliases": _STRING_ARRAY,
    },
    "required": ["name", "category", "description", "aliases"],
    "additionalProperties": False,
}

RELATIONSHIP_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": _STRING,
        "target": _STRING,
        "action": _STRING,
        "context": _STRING,
    },
    "required": ["source", "target", "action", "context"],
    "additionalProperties": False,
}


def build_extraction_schema(name: str = "knowledge_graph") -> dict[str, Any]:
    """The JSON Schema for one extraction, in ``json_schema`` response format.

    ``category`` and ``action`` are deliberately plain strings rather than enums;
    see the module docstring.
    """
    return {
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "entities": {"type": "array", "items": ENTITY_ITEM_SCHEMA},
                "relationships": {"type": "array", "items": RELATIONSHIP_ITEM_SCHEMA},
            },
            "required": ["entities", "relationships"],
            "additionalProperties": False,
        },
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """\
You are a knowledge-graph extraction engine. You read a passage from an \
arbitrary document and return the entities and relationships it states, in JSON.

Return a single JSON object with exactly two keys:

{
  "entities": [
    {"name": string, "category": string, "description": string, "aliases": [string]}
  ],
  "relationships": [
    {"source": string, "target": string, "action": string, "context": string}
  ]
}

Rules for "entities":
- "name": the canonical name, written the way the passage writes it. Use the full \
name, not a pronoun, and not an abbreviation the passage never expands. \
"Euler-Lagrange Equation", "EBITDA", "Mitochondria".
- "category": a high-level classification in UPPER_SNAKE_CASE, invented from the \
passage's own subject matter. EQUATION, METRIC, BIOLOGICAL_STRUCTURE and CONCEPT \
are examples of the style, not a permitted list. For a finance passage you would \
write METRIC or COMPANY; for a mathematics passage, EQUATION or THEOREM. Never \
reuse CONCEPT merely because nothing better occurred to you.
- "description": one sentence saying what the passage states this entity is or does.
- "aliases": other names, abbreviations, symbols, and acronyms for the same thing, \
each a non-empty string. For example ["L", "Lagrangian function"] for the \
Lagrangian. Use an empty list when the entity has none. Do not repeat "name" in \
this list.

Rules for "relationships":
- "source" and "target" must exactly match a "name" you listed in "entities". Never \
invent an endpoint, and never point at an alias instead of the canonical name.
- "action": a single normalised verb in UPPER_SNAKE_CASE naming the connection, \
such as MINIMIZES, CORRELATES_WITH, DERIVES, OWNS, ENCODES, PART_OF. Prefer a \
precise verb; a weak action is worse than no relationship. SCREAMING_SNAKE_CASE, \
never a phrase like "is used for".
- "context": one sentence giving the concrete reason the two connect, grounded in \
what the passage says.

Discipline:
- Extract only what the passage states or directly entails. No outside knowledge, \
no speculation.
- A passage asserts far fewer relationships than you might expect. Include one only \
when the passage asserts that specific connection. Omitting an uncertain \
relationship is always better than including a speculative one.
- Do not pad the list by reusing one action to connect a source to several \
unrelated targets. If you notice yourself repeating an action, you are padding.
- Prefer specific named entities over bare common nouns.
- Emit an empty array for a key when the passage supports nothing for it. Never \
return placeholders such as "unknown", "N/A", or "various".
- Reply with the JSON object only.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExtractionError(RuntimeError):
    """Raised when a passage cannot be turned into a valid extraction.

    Distinct from a passage that legitimately yields nothing: an empty result is
    a successful extraction, this is a failure to obtain one.
    """


# ---------------------------------------------------------------------------
# JSON recovery
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_PY_LITERALS = (
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
    (re.compile(r"\bNaN\b"), "null"),
)


def _outermost_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` in *text*, ignoring braces in strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _close_brackets(value: str) -> str:
    """Append the closing brackets a truncated value is missing."""
    depth_obj = 0
    depth_arr = 0
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth_obj += 1
        elif char == "}":
            depth_obj -= 1
        elif char == "[":
            depth_arr += 1
        elif char == "]":
            depth_arr -= 1
    if in_string:
        value += '"'
    # A dangling key or comma cannot be closed, so drop it.
    value = value.rstrip().rstrip(",")
    value = re.sub(r'"[^"]*"\s*:\s*$', "", value).rstrip().rstrip(",")
    return value + "]" * max(depth_arr, 0) + "}" * max(depth_obj, 0)


def _close_truncated(value: str) -> str:
    """Salvage output that ran out of tokens part-way through.

    Simply appending closing brackets is not enough: a truncation usually lands
    in the middle of the *last* record, so that record has to be discarded
    before the structure can be closed. Cutting back to the last complete value
    and closing from there keeps every record that did finish -- which is the
    point of salvaging at all.
    """
    best: str | None = None
    for closer in ("}", "]"):
        index = value.rfind(closer)
        if index == -1:
            continue
        candidate = _close_brackets(value[: index + 1])
        try:
            json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        # Prefer the cut that kept the most content.
        if best is None or len(candidate) > len(best):
            best = candidate
    if best is not None:
        return best
    return _close_brackets(value)


def _repair(candidate: str) -> str:
    """Best-effort fixes for JSON that is nearly right."""
    out = candidate
    for pattern, replacement in _PY_LITERALS:
        out = pattern.sub(replacement, out)
    # Single-quoted keys/values, only when no double quotes would be disturbed.
    if '"' not in out and "'" in out:
        out = out.replace("'", '"')
    # Raw control characters inside strings break the parser outright. Tab is
    # legal JSON so it stays; newlines and carriage returns become spaces,
    # which costs nothing because every field is whitespace-collapsed anyway.
    out = re.sub(r"[\x00-\x08\x0a-\x0d\x0e-\x1f]", " ", out)
    out = _TRAILING_COMMA.sub(r"\1", out)
    return out


def recover_json(raw: str) -> dict[str, Any]:
    """Parse model output into a dict, repairing the ways it tends to be broken.

    Tried in order: a direct parse, a fenced block, the first balanced object in
    the text, Python-literal and trailing-comma repair, and finally closing a
    truncated object. Raises :class:`ExtractionError` only when all of those fail.
    """
    if raw is None:
        raise ExtractionError("model returned no content")
    text = raw.strip()
    if not text:
        raise ExtractionError("model returned empty content")

    attempts: list[str] = [text]

    fenced = _FENCE.search(text)
    if fenced:
        attempts.append(fenced.group(1).strip())

    balanced = _outermost_object(text)
    if balanced:
        attempts.append(balanced)

    # Repaired forms of every candidate above.
    for candidate in list(attempts):
        attempts.append(_repair(candidate))

    # Truncated output is worth one salvage attempt: a long extraction cut off
    # mid-array still yields the entities that were already written.
    for candidate in list(attempts):
        if candidate.count("{") > candidate.count("}"):
            attempts.append(_close_truncated(candidate))

    errors: list[str] = []
    for candidate in attempts:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if isinstance(parsed, dict):
            return parsed
        errors.append(f"top level was {type(parsed).__name__}, not an object")

    raise ExtractionError(
        f"could not parse model output as JSON ({len(errors)} attempts failed): "
        f"{raw[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_CAMEL_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_HUMP = re.compile(r"([a-z0-9])([A-Z])")


def _camel_to_snake(value: str) -> str:
    """``HTTPServer`` -> ``HTTP_Server``, ``derivesFrom`` -> ``derives_From``.

    Splitting on every capital instead would turn ``HTTPServer`` into
    ``H_T_T_P_Server``, so boundaries are only placed where a run of capitals
    actually ends or a lowercase is followed by an uppercase.
    """
    value = _CAMEL_BOUNDARY.sub(r"\1_\2", value)
    return _CAMEL_HUMP.sub(r"\1_\2", value)


def normalise_action(action: str) -> str:
    """Fold a verb phrase to a single UPPER_SNAKE_CASE token.

    ``"is derived from"``, ``"derived-from"``, ``"derivedFrom"`` and the
    already-canonical ``"DERIVED_FROM"`` all become ``DERIVED_FROM``. An empty or
    punctuation-only verb returns ``""``.

    An all-caps input is left alone: the camel-case split only makes sense when
    there are lowercase letters to break against, and applying it to
    ``DERIVES_FROM`` would shred it into single letters.
    """
    if not isinstance(action, str):
        return ""
    cleaned = _WS.sub("_", action.strip())
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return ""
    if not any(char.islower() for char in cleaned):
        return cleaned.upper()
    return _camel_to_snake(cleaned).upper()


def _clean_text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = _WS.sub(" ", value).strip()
    return text[:limit]


def _clean_aliases(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            # Tolerate [{"alias": "x"}] and [{"name": "x"}] shapes.
            item = next(
                (v for v in item.values() if isinstance(v, str)), None
            )
        text = _clean_text(item, 200)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        out.append(text)
    return out


def _canonical_key(text: str) -> str:
    """A comparison key tolerant of case, spacing, and punctuation."""
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _looks_like_placeholder(text: str) -> bool:
    """Reject "unknown", "N/A", "various" and friends as entity names."""
    return _canonical_key(text) in {
        "",
        "na",
        "n a",
        "none",
        "null",
        "unknown",
        "various",
        "etc",
        "other",
        "others",
        "tbd",
        "unnamed",
        "unspecified",
    } or _canonical_key(text) in {
        "unknownentity",
        "placeholder",
        "entityname",
    }


# -- pydantic path ---------------------------------------------------------

try:  # pragma: no cover - exercised by whichever path is installed
    from pydantic import BaseModel, Field, field_validator

    PYDANTIC_AVAILABLE = True

    class _EntityModel(BaseModel):
        name: str
        category: str
        description: str
        aliases: list[str] = Field(default_factory=list)

        @field_validator("name", "category", "description", mode="before")
        @classmethod
        def _text(cls, value: Any) -> str:
            return _clean_text(value)

    class _RelationshipModel(BaseModel):
        source: str
        target: str
        action: str
        context: str

        @field_validator("source", "target", "context", mode="before")
        @classmethod
        def _text(cls, value: Any) -> str:
            return _clean_text(value)

        @field_validator("action", mode="before")
        @classmethod
        def _action(cls, value: Any) -> str:
            return normalise_action(value if isinstance(value, str) else "")

    class _PayloadModel(BaseModel):
        entities: list[_EntityModel] = Field(default_factory=list)
        relationships: list[_RelationshipModel] = Field(default_factory=list)

except ImportError:  # pragma: no cover - fallback path
    PYDANTIC_AVAILABLE = False
    _EntityModel = _RelationshipModel = _PayloadModel = None  # type: ignore[assignment]


@dataclass
class ExtractionResult:
    """A validated extraction plus the counters a caller needs to trust it."""

    entities: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    entities_dropped: int = 0
    relationships_dropped: int = 0
    repaired: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The plain contract shape: entities and relationships, nothing else."""
        return {
            "entities": self.entities,
            "relationships": self.relationships,
        }


def _coerce_records(value: Any) -> list[dict[str, Any]]:
    """Get a list of dicts out of whatever shape arrived."""
    if value is None:
        return []
    if isinstance(value, dict):
        # A model that returned {"entities": {"0": {...}}} or a single record.
        if any(k in value for k in ("name", "source", "category")):
            return [value]
        return [v for v in value.values() if isinstance(v, dict)]
    if isinstance(value, str):
        inner = recover_json(value)
        return _coerce_records(inner.get("entities", [])) if "name" in inner else []
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _pre_entity(item: dict[str, Any]) -> dict[str, Any]:
    """Coerce one raw entity into the exact field types the schema promises.

    Done *before* validation on purpose: a model that omits ``category``
    entirely should keep the entity with a default rather than lose it, and the
    pydantic and non-pydantic paths must agree byte for byte.
    """
    return {
        "name": _clean_text(item.get("name")),
        "category": _clean_text(item.get("category")) or "CONCEPT",
        "description": _clean_text(item.get("description")),
        "aliases": _clean_aliases(item.get("aliases")),
    }


def _pre_relationship(item: dict[str, Any]) -> dict[str, Any]:
    """Coerce one raw relationship into the exact field types."""
    action = item.get("action")
    return {
        "source": _clean_text(item.get("source")),
        "target": _clean_text(item.get("target")),
        "action": normalise_action(action if isinstance(action, str) else ""),
        "context": _clean_text(item.get("context")),
    }


def validate_payload(payload: Any) -> ExtractionResult:
    """Turn a parsed payload into clean, connected records.

    Drops unusable entities, de-duplicates them by canonical name, resolves
    relationship endpoints through names and aliases, normalises actions, and
    discards relationships whose endpoints do not resolve. Every drop is
    counted on the result rather than happening silently.
    """
    if not isinstance(payload, dict):
        raise ExtractionError(
            f"extraction payload was {type(payload).__name__}, not an object"
        )

    result = ExtractionResult()
    raw_entities = _coerce_records(payload.get("entities"))
    raw_relationships = _coerce_records(payload.get("relationships"))

    # -- entities ---------------------------------------------------------
    for item in raw_entities:
        record = _pre_entity(item)
        if PYDANTIC_AVAILABLE:
            try:
                record = _EntityModel(**record).model_dump()  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                result.entities_dropped += 1
                continue
        if not record["name"] or _looks_like_placeholder(record["name"]):
            result.entities_dropped += 1
            continue
        result.entities.append(record)

    # De-duplicate by canonical name, merging aliases into the first claimant.
    seen_names: set[str] = set()
    kept: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    for entity in result.entities:
        key = _canonical_key(entity["name"])
        if key in seen_names:
            for existing in kept:
                if _canonical_key(existing["name"]) == key:
                    have = {_canonical_key(a) for a in existing["aliases"]}
                    for alias in entity["aliases"]:
                        if _canonical_key(alias) not in have:
                            existing["aliases"].append(alias)
            result.entities_dropped += 1
            continue
        seen_names.add(key)
        if not entity["category"]:
            entity["category"] = "CONCEPT"
        entity["aliases"] = [
            a for a in entity["aliases"] if _canonical_key(a) != key
        ]
        kept.append(entity)
        index[key] = entity["name"]
        for alias in entity["aliases"]:
            index.setdefault(_canonical_key(alias), entity["name"])
    result.entities = kept

    # -- relationships ----------------------------------------------------
    def endpoint(value: Any) -> str:
        key = _canonical_key(_clean_text(value))
        return index.get(key, "")

    for item in raw_relationships:
        record = _pre_relationship(item)
        if PYDANTIC_AVAILABLE:
            try:
                record = _RelationshipModel(**record).model_dump()  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                result.relationships_dropped += 1
                continue
        action = record["action"]
        source = endpoint(record["source"])
        target = endpoint(record["target"])
        if not action or not source or not target:
            result.relationships_dropped += 1
            continue
        if source == target:
            # A self-loop carries no information and pollutes degree counts.
            result.relationships_dropped += 1
            continue
        result.relationships.append(
            {
                "source": source,
                "target": target,
                "action": action,
                "context": record["context"],
            }
        )

    # A repeated triple adds nothing and inflates the edge count.
    deduped: list[dict[str, Any]] = []
    seen_triples: set[tuple[str, str, str]] = set()
    for edge in result.relationships:
        triple = (edge["source"], edge["action"], edge["target"])
        if triple in seen_triples:
            result.relationships_dropped += 1
            continue
        seen_triples.add(triple)
        deduped.append(edge)
    result.relationships = deduped

    return result


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def create_client(
    model: str | None = None,
    provider: str = "auto",
    api_key: str | None = None,
    base_url: str | None = None,
) -> Any:
    """Build an OpenAI SDK client pointed at OpenAI or OpenRouter.

    ``provider="auto"`` prefers an OpenRouter key when one is present, because
    an OpenRouter key is a single credential covering many models.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExtractionError(
            "the openai package is required: pip install openai"
        ) from exc

    openrouter_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    openai_key = api_key or os.environ.get("OPENAI_API_KEY")

    if provider == "auto":
        if openrouter_key:
            provider = "openrouter"
        elif openai_key:
            provider = "openai"
        else:
            raise ExtractionError(
                "no API key: set OPENROUTER_API_KEY or OPENAI_API_KEY, or pass "
                "api_key=..."
            )
    elif provider not in {"openrouter", "openai"}:
        raise ExtractionError(
            f"unknown provider {provider!r}; use 'openrouter', 'openai' or 'auto'"
        )

    if provider == "openrouter":
        key = openrouter_key
        base = base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
    else:
        key = openai_key or os.environ.get("OPENAI_BASE_URL")
        base = base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL

    if not key:
        raise ExtractionError(f"no API key for provider {provider!r}")

    return OpenAI(api_key=key, base_url=base)


def _response_format(schema: dict[str, Any], strict: bool) -> dict[str, Any]:
    if not strict:
        # OpenRouter and older gateways accept plain JSON mode.
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": schema}


def _chat_json(
    client: Any,
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """One chat call returning raw text, degrading if strict decoding is refused."""
    attempts: list[dict[str, Any]] = [
        _response_format(schema, strict=True),
        _response_format(schema, strict=False),
        # Last resort: no response_format at all, repaired by recover_json.
        {"type": "text"},
    ]
    last: Exception | None = None
    for index, response_format in enumerate(attempts):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "timeout": timeout,
        }
        if index < 2:
            kwargs["response_format"] = response_format
        try:
            completion = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - provider errors vary widely
            last = exc
            message = str(exc).lower()
            # Only a complaint about the response format is worth retrying
            # differently; a quota or auth failure will fail identically.
            if not any(
                token in message
                for token in ("response_format", "json_schema", "schema", "400")
            ):
                raise ExtractionError(f"LLM request failed: {exc}") from exc
            continue
        content = completion.choices[0].message.content
        if not content:
            raise ExtractionError("LLM returned no content")
        return content
    raise ExtractionError(f"LLM request failed: {last}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

_CLIENT: Any = None
_MODEL: str | None = None


def _resolve(
    text_chunk: str,
    client: Any,
    model: str | None,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> ExtractionResult:
    global _CLIENT, _MODEL

    if client is None:
        if _CLIENT is None or (model and model != _MODEL):
            _CLIENT = create_client(model=model)
            _MODEL = model or os.environ.get("GRAPHRAG_MODEL")
        client = _CLIENT
        if model is None:
            model = _MODEL
    if not model:
        model = os.environ.get("GRAPHRAG_MODEL") or "openai/gpt-oss-20b"

    schema = build_extraction_schema()
    raw = _chat_json(
        client,
        model,
        EXTRACTION_SYSTEM,
        text_chunk,
        schema,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    payload = recover_json(raw)
    result = validate_payload(payload)
    # True when the model wrapped its JSON in prose or a code fence and the
    # object had to be dug out, which is worth surfacing for prompt tuning.
    result.repaired = not raw.strip().startswith("{")
    return result


def extract_triples(
    text_chunk: str,
    client: Any = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Extract a knowledge graph from one passage.

    Returns exactly ``{"entities": [...], "relationships": [...]}`` with each
    entity as ``{name, category, description, aliases}`` and each relationship as
    ``{source, target, action, context}``.

    A passage that states nothing extractable returns two empty lists -- that is
    a successful extraction, not an error. An :class:`ExtractionError` means the
    model could not be reached or produced nothing recoverable.
    """
    if not isinstance(text_chunk, str):
        raise ExtractionError(f"text_chunk must be str, got {type(text_chunk).__name__}")
    chunk = text_chunk.strip()
    if not chunk:
        return {"entities": [], "relationships": []}

    return _resolve(chunk, client, model, temperature, max_tokens, timeout).to_dict()


def extract_corpus(
    chunks: Sequence[str] | Iterable[str],
    on_error: str = "collect",
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract from many passages, returning ``(results, errors)``.

    One malformed passage must not cost you the rest of a corpus, so failures
    are collected by default. ``on_error="raise"`` fails fast instead.
    """
    if on_error not in {"collect", "raise"}:
        raise ExtractionError(f"on_error must be 'collect' or 'raise', not {on_error!r}")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        try:
            results.append(extract_triples(chunk, **kwargs))
        except ExtractionError as exc:
            if on_error == "raise":
                raise
            errors.append({"index": index, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never lose the corpus
            if on_error == "raise":
                raise ExtractionError(f"chunk {index} failed: {exc}") from exc
            errors.append({"index": index, "error": f"unexpected: {exc}"})
    return results, errors
